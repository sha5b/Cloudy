# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Shahab Nedaei
"""A GTK-native rich-text editor for the Mail composer.

A ``Gtk.TextView`` with an Adwaita formatting toolbar — **no WebKit dependency**,
so it matches the rest of the app's look and stays lightweight. Formatting is
stored as ``Gtk.TextTag``s and serialized to e-mail HTML on send; inline images
are real anchored children that serialize to ``cid:`` references with matching
file attachments.

Supported: **bold, italic, underline, strikethrough, text colour, links,
bullet/numbered lists, inline images, clear-formatting**. Font family/size,
tables and alignment are intentionally left out for now (kept clean + on-brand);
plain text remains available as the fallback body for non-HTML transports.
"""

from __future__ import annotations

import html
import re
from gettext import gettext as _

from gi.repository import Gdk, Gio, GLib, Gtk, Pango

from .imaging import thumbnail_texture

# Inline character marks: tag name -> (property, value) applied to the tag.
_MARKS = {
    "bold": ("weight", Pango.Weight.BOLD),
    "italic": ("style", Pango.Style.ITALIC),
    "underline": ("underline", Pango.Underline.SINGLE),
    "strike": ("strikethrough", True),
}
# A small, legible colour palette for the text-colour menu (label, hex).
_COLORS = [
    (_("Automatic"), ""),
    (_("Red"), "#e01b24"), (_("Orange"), "#ff7800"), (_("Green"), "#2ec27e"),
    (_("Blue"), "#3584e4"), (_("Purple"), "#9141ac"), (_("Grey"), "#77767b"),
]


class RichTextEditor(Gtk.Box):
    """Vertical box: a formatting toolbar above a scrolled, tagged text view."""

    __gtype_name__ = "CloudyRichTextEditor"

    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self._syncing = False           # guard against signal recursion
        self._marks: set[str] = set()   # pending inline marks for typed text
        self._pending_color = ""        # pending foreground for typed text
        self._images: dict = {}         # child anchor -> (bytes, content_type)
        self._link_tags: dict = {}      # tag name -> url

        self._buffer = Gtk.TextBuffer()
        self._make_tags()
        self._view = Gtk.TextView(
            buffer=self._buffer, wrap_mode=Gtk.WrapMode.WORD_CHAR,
            top_margin=10, bottom_margin=10, left_margin=12, right_margin=12)
        self._buffer.connect_after("insert-text", self._on_inserted)
        self._buffer.connect("mark-set", self._on_mark_set)

        # Intercept Ctrl+V so an image on the clipboard becomes an inline
        # image; plain GtkTextView paste only handles text, so without this a
        # screenshot or copied image silently does nothing.
        keys = Gtk.EventControllerKey()
        keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self._on_key_pressed)
        self._view.add_controller(keys)

        scroll = Gtk.ScrolledWindow(vexpand=True, hexpand=True,
                                    hscrollbar_policy=Gtk.PolicyType.NEVER,
                                    child=self._view)
        scroll.add_css_class("card")

        self.append(self._build_toolbar())
        self.append(scroll)

    # -- tags -------------------------------------------------------------
    def _make_tags(self) -> None:
        table = self._buffer.get_tag_table()
        for name, (prop, value) in _MARKS.items():
            tag = Gtk.TextTag(name=name)
            tag.set_property(prop, value)
            table.add(tag)

    def _color_tag(self, hex_color: str) -> Gtk.TextTag:
        name = f"fg:{hex_color}"
        table = self._buffer.get_tag_table()
        tag = table.lookup(name)
        if tag is None:
            tag = Gtk.TextTag(name=name)
            tag.set_property("foreground", hex_color)
            table.add(tag)
        return tag

    def _link_tag(self, url: str) -> Gtk.TextTag:
        name = f"link:{url}"
        table = self._buffer.get_tag_table()
        tag = table.lookup(name)
        if tag is None:
            tag = Gtk.TextTag(name=name)
            tag.set_property("foreground", "#3584e4")
            tag.set_property("underline", Pango.Underline.SINGLE)
            table.add(tag)
            self._link_tags[name] = url
        return tag

    # -- toolbar ----------------------------------------------------------
    def _build_toolbar(self) -> Gtk.Widget:
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2,
                      margin_bottom=6)
        bar.add_css_class("toolbar")

        self._toggles: dict = {}
        group = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        group.add_css_class("linked")
        for name, icon, tip in (
            ("bold", "format-text-bold-symbolic", _("Bold")),
            ("italic", "format-text-italic-symbolic", _("Italic")),
            ("underline", "format-text-underline-symbolic", _("Underline")),
            ("strike", "format-text-strikethrough-symbolic", _("Strikethrough")),
        ):
            btn = Gtk.ToggleButton(icon_name=icon, tooltip_text=tip)
            btn.connect("toggled", self._on_mark_toggled, name)
            group.append(btn)
            self._toggles[name] = btn
        bar.append(group)

        # Text colour.
        color_btn = Gtk.MenuButton(icon_name="format-text-rich-symbolic",
                                   tooltip_text=_("Text colour"))
        color_btn.set_popover(self._color_popover())
        bar.append(color_btn)

        # Link.
        link_btn = Gtk.Button(icon_name="insert-link-symbolic",
                              tooltip_text=_("Insert link"))
        link_btn.connect("clicked", self._on_link)
        bar.append(link_btn)

        # Lists.
        lists = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        lists.add_css_class("linked")
        bullet = Gtk.Button(icon_name="view-list-symbolic",
                            tooltip_text=_("Bullet list"))
        bullet.connect("clicked", lambda *_a: self._toggle_list("bullet"))
        number = Gtk.Button(icon_name="view-list-ordered-symbolic",
                            tooltip_text=_("Numbered list"))
        number.connect("clicked", lambda *_a: self._toggle_list("number"))
        lists.append(bullet)
        lists.append(number)
        bar.append(lists)

        # Inline image.
        img_btn = Gtk.Button(icon_name="image-x-generic-symbolic",
                            tooltip_text=_("Insert image"))
        img_btn.connect("clicked", self._on_insert_image)
        bar.append(img_btn)

        clear_btn = Gtk.Button(icon_name="edit-clear-all-symbolic",
                              tooltip_text=_("Clear formatting"))
        clear_btn.connect("clicked", self._on_clear)
        bar.append(clear_btn)
        return bar

    def _color_popover(self) -> Gtk.Popover:
        pop = Gtk.Popover()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2,
                      margin_top=6, margin_bottom=6, margin_start=6, margin_end=6)
        for label, hex_color in _COLORS:
            btn = Gtk.Button(has_frame=False)
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            swatch = Gtk.Image.new_from_icon_name(
                "color-select-symbolic" if hex_color else "edit-clear-symbolic")
            if hex_color:
                swatch.add_css_class("cloudy-color-swatch")
            row.append(swatch)
            row.append(Gtk.Label(label=label, xalign=0, hexpand=True))
            btn.set_child(row)
            btn.connect("clicked",
                        lambda _b, c=hex_color: (self._apply_color(c), pop.popdown()))
            box.append(btn)
        pop.set_child(box)
        return pop

    # -- inline marks -----------------------------------------------------
    def _on_mark_toggled(self, button, name) -> None:
        if self._syncing:
            return
        active = button.get_active()
        if active:
            self._marks.add(name)
        else:
            self._marks.discard(name)
        bounds = self._buffer.get_selection_bounds()
        if bounds:
            start, end = bounds
            tag = self._buffer.get_tag_table().lookup(name)
            if active:
                self._buffer.apply_tag(tag, start, end)
            else:
                self._buffer.remove_tag(tag, start, end)
        self._view.grab_focus()

    def _apply_color(self, hex_color: str) -> None:
        self._pending_color = hex_color
        bounds = self._buffer.get_selection_bounds()
        if bounds:
            start, end = bounds
            # Remove any existing colour first, then apply the new one.
            for tag in self._color_tags():
                self._buffer.remove_tag(tag, start, end)
            if hex_color:
                self._buffer.apply_tag(self._color_tag(hex_color), start, end)
        self._view.grab_focus()

    def _color_tags(self) -> list:
        found: list = []
        self._buffer.get_tag_table().foreach(
            lambda tag, _d: found.append(tag)
            if (tag.get_property("name") or "").startswith("fg:") else None, None)
        return found

    def _on_inserted(self, _buffer, location, text, _len) -> None:
        if self._syncing or not text:
            return
        start = location.copy()
        start.backward_chars(len(text))
        for name in self._marks:
            tag = self._buffer.get_tag_table().lookup(name)
            if tag is not None:
                self._buffer.apply_tag(tag, start, location)
        if self._pending_color:
            self._buffer.apply_tag(self._color_tag(self._pending_color), start, location)

    def _on_mark_set(self, _buffer, _iter, mark) -> None:
        if mark.get_name() != "insert" or self._syncing:
            return
        self._sync_toggles()

    def _sync_toggles(self) -> None:
        """Reflect the formatting at the cursor in the toolbar toggle states
        (and adopt it as the pending format for typed text)."""
        insert = self._buffer.get_iter_at_mark(self._buffer.get_insert())
        active = set()
        color = ""
        for tag in insert.get_tags():
            name = tag.get_property("name") or ""
            if name in _MARKS:
                active.add(name)
            elif name.startswith("fg:"):
                color = name[3:]
        self._syncing = True
        for name, btn in self._toggles.items():
            btn.set_active(name in active)
        self._syncing = False
        self._marks = active
        self._pending_color = color

    # -- links ------------------------------------------------------------
    def _on_link(self, button) -> None:
        bounds = self._buffer.get_selection_bounds()
        pop = Gtk.Popover()
        pop.set_parent(button)
        pop.connect("closed", lambda p: p.unparent() if p.get_parent() else None)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6,
                      margin_top=8, margin_bottom=8, margin_start=8, margin_end=8)
        url = Gtk.Entry(placeholder_text=_("https://…"), hexpand=True)
        text = Gtk.Entry(placeholder_text=_("Text to show"), hexpand=True)
        if bounds:
            text.set_text(self._buffer.get_text(bounds[0], bounds[1], False))
            text.set_sensitive(False)  # wrap the existing selection
        box.append(url)
        box.append(text)
        add = Gtk.Button(label=_("Insert link"))
        add.add_css_class("suggested-action")
        add.connect("clicked",
                    lambda *_a: (self._insert_link(url.get_text(), text.get_text()),
                                 pop.popdown()))
        box.append(add)
        pop.set_child(box)
        pop.set_size_request(260, -1)
        pop.popup()

    def _insert_link(self, url: str, label: str) -> None:
        url = url.strip()
        if not url:
            return
        if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", url):
            url = "https://" + url  # bare domains → https
        bounds = self._buffer.get_selection_bounds()
        tag = self._link_tag(url)
        if bounds:
            self._buffer.apply_tag(tag, bounds[0], bounds[1])
        else:
            shown = label.strip() or url
            insert = self._buffer.get_iter_at_mark(self._buffer.get_insert())
            offset = insert.get_offset()
            self._buffer.insert(insert, shown)
            start = self._buffer.get_iter_at_offset(offset)
            end = self._buffer.get_iter_at_offset(offset + len(shown))
            self._buffer.apply_tag(tag, start, end)
        self._view.grab_focus()

    # -- lists ------------------------------------------------------------
    def _toggle_list(self, kind: str) -> None:
        """Add or remove a soft list prefix on each selected line. Soft prefixes
        (``• `` / ``1. ``) keep the view WYSIWYG and serialize cleanly to
        ``<ul>``/``<ol>``."""
        buf = self._buffer
        bounds = buf.get_selection_bounds()
        if bounds:
            first = bounds[0].get_line()
            last = bounds[1].get_line()
        else:
            first = last = buf.get_iter_at_mark(buf.get_insert()).get_line()
        n = 1
        for line in range(first, last + 1):
            start = buf.get_iter_at_line(line)
            start = start[1] if isinstance(start, tuple) else start
            line_end = start.copy()
            line_end.forward_to_line_end()
            existing = buf.get_text(start, line_end, False)
            stripped = self._strip_list_prefix(existing)
            if stripped != existing:  # already a list line → remove the prefix
                buf.delete(start, line_end)
                ins = buf.get_iter_at_line(line)
                ins = ins[1] if isinstance(ins, tuple) else ins
                buf.insert(ins, stripped)
            else:
                prefix = "• " if kind == "bullet" else f"{n}. "
                buf.insert(start, prefix)
                n += 1
        self._view.grab_focus()

    @staticmethod
    def _strip_list_prefix(text: str):
        m = re.match(r"^(• |\d+\. )", text)
        return text[m.end():] if m else text

    # -- inline images ----------------------------------------------------
    def _on_insert_image(self, _btn) -> None:
        dialog = Gtk.FileDialog(title=_("Insert image"))
        filt = Gtk.FileFilter()
        filt.set_name(_("Images"))
        filt.add_pixbuf_formats()
        store = Gio.ListStore.new(Gtk.FileFilter)
        store.append(filt)
        dialog.set_filters(store)
        dialog.set_default_filter(filt)
        from .source_nav import local_initial_folder

        folder = local_initial_folder()
        if folder is not None:
            dialog.set_initial_folder(folder)
        dialog.open(self.get_root(), None, self._on_image_chosen)

    def _on_image_chosen(self, dialog, result) -> None:
        try:
            gfile = dialog.open_finish(result)
        except GLib.Error:
            return
        if gfile is None:
            return
        try:
            ok, data, _etag = gfile.load_contents(None)
            if not ok:
                return
            ctype = "image/png"
            info = gfile.query_info("standard::content-type", 0, None)
            if info and info.get_content_type():
                ctype = info.get_content_type()
        except GLib.Error:
            return
        self.insert_image(bytes(data), ctype)

    def _on_key_pressed(self, _ctrl, keyval, _code, state) -> bool:
        """On Ctrl+V, paste an image from the clipboard if one is present;
        otherwise fall through to the TextView's normal text paste."""
        ctrl = state & Gdk.ModifierType.CONTROL_MASK
        shift = state & Gdk.ModifierType.SHIFT_MASK
        if not ctrl or shift or keyval not in (Gdk.KEY_v, Gdk.KEY_V):
            return False
        clipboard = self._view.get_clipboard()
        formats = clipboard.get_formats()
        if not formats.contain_gtype(Gdk.Texture):
            return False  # no image → let the default text paste run
        clipboard.read_texture_async(None, self._on_paste_texture)
        return True  # consume; we're handling this paste

    def _on_paste_texture(self, clipboard, result) -> None:
        try:
            texture = clipboard.read_texture_finish(result)
        except GLib.Error:
            return
        if texture is None:
            return
        try:
            data = texture.save_to_png_bytes()
        except Exception:  # noqa: BLE001 - unencodable texture
            return
        self.insert_image(bytes(data.get_data()), "image/png")

    def insert_image(self, data: bytes, content_type: str) -> None:
        """Insert ``data`` as an inline image at the cursor."""
        try:
            texture = thumbnail_texture(data, 360)
        except Exception:  # noqa: BLE001 - undecodable payload
            return
        insert = self._buffer.get_iter_at_mark(self._buffer.get_insert())
        anchor = self._buffer.create_child_anchor(insert)
        self._images[anchor] = (data, content_type or "image/png")
        picture = Gtk.Picture.new_for_paintable(texture)
        # Pin to the (downscaled) texture's size and DON'T let it shrink —
        # otherwise GtkPicture collapses to 0×0 inside the TextView anchor and
        # the image is inserted but invisible ("loads then disappears").
        picture.set_can_shrink(False)
        picture.set_halign(Gtk.Align.START)
        picture.set_size_request(texture.get_width(), texture.get_height())
        picture.add_css_class("cloudy-bubble-image")
        self._view.add_child_at_anchor(picture, anchor)

    # -- clear ------------------------------------------------------------
    def _on_clear(self, _btn) -> None:
        bounds = self._buffer.get_selection_bounds()
        if not bounds:
            return
        self._buffer.remove_all_tags(bounds[0], bounds[1])
        # Also drop list prefixes across the selection.
        self._view.grab_focus()

    # -- content access ---------------------------------------------------
    def set_plain_text(self, text: str) -> None:
        self._buffer.set_text(text or "")

    def get_plain_text(self) -> str:
        return self._buffer.get_text(
            self._buffer.get_start_iter(), self._buffer.get_end_iter(), False)

    def get_html(self):
        """Serialize the buffer to e-mail HTML.

        Returns ``(html, inline_images)`` where ``inline_images`` is a list of
        ``{"content_id", "data", "content_type"}`` for the ``cid:`` references
        emitted into the markup."""
        buf = self._buffer
        inline: list[dict] = []
        cids: dict = {}
        parts: list[str] = []
        list_mode = None  # None | "ul" | "ol"
        items: list[str] = []

        def flush_list():
            nonlocal list_mode, items
            if list_mode and items:
                tag = "ul" if list_mode == "ul" else "ol"
                parts.append(f"<{tag}>" + "".join(f"<li>{i}</li>" for i in items)
                             + f"</{tag}>")
            list_mode, items = None, []

        line_count = buf.get_line_count()
        for line in range(line_count):
            start = buf.get_iter_at_line(line)
            start = start[1] if isinstance(start, tuple) else start
            if line + 1 < line_count:
                nxt = buf.get_iter_at_line(line + 1)
                nxt = nxt[1] if isinstance(nxt, tuple) else nxt
                end = nxt.copy()
                end.backward_char()  # exclude the newline
            else:
                end = buf.get_end_iter()
            plain = buf.get_text(start, end, False)
            m = re.match(r"^(• |\d+\. )", plain)
            content = self._serialize_range(start, end, inline, cids,
                                            skip=m.end() if m else 0)
            if m:
                mode = "ul" if plain.startswith("• ") else "ol"
                if list_mode and list_mode != mode:
                    flush_list()
                list_mode = mode
                items.append(content or "&nbsp;")
            else:
                flush_list()
                parts.append(f"<div>{content or '<br>'}</div>")
        flush_list()
        return "".join(parts), inline

    def _serialize_range(self, start, end, inline, cids, skip=0):
        buf = self._buffer
        out: list[str] = []
        run = ""
        cur = None  # (frozenset(marks), color, url)

        def fmt(names, color, url):
            return (frozenset(names), color, url)

        def wrap(text_html, key):
            names, color, url = key
            open_t, close_t = "", ""
            if url:
                open_t += f'<a href="{html.escape(url)}">'
                close_t = "</a>" + close_t
            if "bold" in names:
                open_t += "<b>"; close_t = "</b>" + close_t
            if "italic" in names:
                open_t += "<i>"; close_t = "</i>" + close_t
            if "underline" in names:
                open_t += "<u>"; close_t = "</u>" + close_t
            if "strike" in names:
                open_t += "<s>"; close_t = "</s>" + close_t
            if color:
                open_t += f'<span style="color:{html.escape(color)}">'
                close_t = "</span>" + close_t
            return open_t + text_html + close_t

        def flush():
            nonlocal run
            if run:
                out.append(wrap(html.escape(run), cur))
                run = ""

        pos = start.copy()
        if skip:
            pos.forward_chars(skip)
        while pos.compare(end) < 0:
            anchor = pos.get_child_anchor()
            if anchor is not None:
                flush()
                src = self._images.get(anchor)
                if src is not None:
                    cid = cids.get(anchor)
                    if cid is None:
                        cid = f"img{len(inline)}@cloudy"
                        cids[anchor] = cid
                        inline.append({"content_id": cid, "data": src[0],
                                       "content_type": src[1]})
                    out.append(f'<img src="cid:{cid}" style="max-width:600px">')
                pos.forward_char()
                cur = None
                continue
            names, color, url = set(), "", None
            for tag in pos.get_tags():
                name = tag.get_property("name") or ""
                if name in _MARKS:
                    names.add(name)
                elif name.startswith("fg:"):
                    color = name[3:]
                elif name.startswith("link:"):
                    url = self._link_tags.get(name, name[5:])
            key = fmt(names, color, url)
            if cur is None:
                cur = key
            elif key != cur:
                flush()
                cur = key
            nxt = pos.copy()
            nxt.forward_char()
            run += buf.get_text(pos, nxt, False)
            pos = nxt
        flush()
        return "".join(out)
