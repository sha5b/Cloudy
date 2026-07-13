# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Shahab Nedaei
"""In-app, Nautilus-style file browser for a mounted library.

A mounted library is a real folder on disk (its FUSE mountpoint), so browsing is
just listing directories. ``FileBrowserPane`` is the right pane of the Files
view and behaves like Nautilus: back/forward history + a clickable breadcrumb,
a **Grid** or **List** view (with sortable Name/Size/Type/Modified columns),
folders first, single-click to select, double-click to open (folders expand
inline in list view), and right-click for Open/Rename/Trash. Listing runs off
the UI thread because a network mount can be slow to stat.
"""

from __future__ import annotations

import os
import shutil
from gettext import gettext as _
from pathlib import Path

from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk, Pango

from .file_browser_utils import (
    gdk_rect,
    human_size,
    human_time,
    icon_for,
    scan_directory,
    type_label,
)
from .format import esc
from .source_nav import run_async


def _restore_from_trash(orig_paths) -> int:
    """Best-effort restore of freshly-trashed items back to their original
    locations, matching by the freedesktop ``trash::orig-path`` attribute.
    Returns the number restored (0 if none matched — e.g. the item lives in a
    per-filesystem trash the ``trash://`` backend doesn't aggregate). Runs on a
    worker thread; raises only on an unexpected enumerator failure."""
    want = {os.path.normpath(p) for p in orig_paths}
    trash = Gio.File.new_for_uri("trash:///")
    attrs = "standard::name,trash::orig-path"
    enumerator = trash.enumerate_children(attrs, Gio.FileQueryInfoFlags.NONE, None)
    restored = 0
    for info in enumerator:
        orig = info.get_attribute_byte_string("trash::orig-path")
        if not orig or os.path.normpath(orig) not in want:
            continue
        item = trash.get_child(info.get_name())
        dest = Gio.File.new_for_path(orig)
        try:
            item.move(dest, Gio.FileCopyFlags.NOFOLLOW_SYMLINKS, None, None, None)
            restored += 1
        except GLib.Error:
            pass  # one bad restore shouldn't abort the rest
    return restored


class FileBrowserPane(Adw.Bin):
    """The right pane: browse one mounted library, Nautilus-style."""

    _COLUMNS = (
        ("name", _("Name"), -1), ("size", _("Size"), 96),
        ("type", _("Type"), 130), ("mtime", _("Modified"), 160),
    )

    def __init__(self, window):
        super().__init__()
        self._window = window
        self._root: Path | None = None
        self._root_title = ""
        self._history: list[Path] = []
        self._hpos = -1
        self._entries: list[dict] = []
        self._view = "grid"        # "grid" | "list"
        self._sort_key = "name"
        self._sort_desc = False
        self._expanded: set[str] = set()   # folder paths expanded inline (list view)
        self._child_cache: dict[str, list] = {}
        self._toggle_src = None            # pending single-click expand timer
        self._filter = ""                  # in-folder name filter (current dir only)

        # -- header ------------------------------------------------------
        self._back = Gtk.Button(icon_name="go-previous-symbolic",
                                tooltip_text=_("Back"), sensitive=False)
        self._back.connect("clicked", lambda *_a: self._go(-1))
        self._fwd = Gtk.Button(icon_name="go-next-symbolic",
                               tooltip_text=_("Forward"), sensitive=False)
        self._fwd.connect("clicked", lambda *_a: self._go(1))
        nav = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, css_classes=["linked"])
        nav.append(self._back)
        nav.append(self._fwd)

        self._crumbs = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2,
                               css_classes=["linked"])
        self._header = Adw.HeaderBar(show_start_title_buttons=False,
                                     show_end_title_buttons=False,
                                     title_widget=self._crumbs)
        self._header.pack_start(nav)

        self._new_folder_btn = Gtk.Button(icon_name="folder-new-symbolic",
                                          tooltip_text=_("New folder"), sensitive=False)
        self._new_folder_btn.connect("clicked", self._on_new_folder)
        self._upload_btn = Gtk.Button(icon_name="document-send-symbolic",
                                      tooltip_text=_("Upload files here"), sensitive=False)
        self._upload_btn.connect("clicked", self._on_upload)
        self._open_ext_btn = Gtk.Button(icon_name="folder-open-symbolic",
                                        tooltip_text=_("Open in system Files"),
                                        sensitive=False)
        self._open_ext_btn.connect("clicked", lambda *_a: self._open_uri(self._cur()))
        self._search_btn = Gtk.ToggleButton(icon_name="system-search-symbolic",
                                            tooltip_text=_("Search this folder"),
                                            sensitive=False)
        self._header.pack_end(self._view_button())
        self._header.pack_end(self._open_ext_btn)
        self._header.pack_end(self._upload_btn)
        self._header.pack_end(self._new_folder_btn)
        self._header.pack_end(self._search_btn)

        # -- search bar (filters the current folder by name) -------------
        self._search_entry = Gtk.SearchEntry(
            placeholder_text=_("Filter this folder by name…"), hexpand=True)
        self._search_entry.connect("search-changed", self._on_filter_changed)
        self._search_bar = Gtk.SearchBar()
        self._search_bar.set_child(self._search_entry)
        self._search_bar.connect_entry(self._search_entry)
        self._search_bar.set_key_capture_widget(self)
        self._search_btn.bind_property(
            "active", self._search_bar, "search-mode-enabled",
            GObject.BindingFlags.BIDIRECTIONAL)
        self._search_bar.connect("notify::search-mode-enabled",
                                 self._on_search_mode)

        # -- views -------------------------------------------------------
        # Single click selects (Ctrl/Shift extends); double click opens.
        self._flow = Gtk.FlowBox(
            selection_mode=Gtk.SelectionMode.MULTIPLE, homogeneous=True,
            activate_on_single_click=False,
            valign=Gtk.Align.START, max_children_per_line=12, min_children_per_line=2,
            row_spacing=6, column_spacing=6, margin_top=12, margin_bottom=12,
            margin_start=12, margin_end=12)
        self._flow.connect("child-activated", self._on_flow_activated)
        grid_scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER,
                                         vexpand=True, child=self._flow)

        # The list fills the pane directly (like the grid view) — no extra card.
        self._list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.MULTIPLE,
                                 activate_on_single_click=False)
        self._list.set_show_separators(True)
        list_box = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER,
                                      vexpand=True, child=self._list)

        self._status = Adw.StatusPage(icon_name="folder-symbolic")
        self._stack = Gtk.Stack()
        self._stack.add_named(grid_scroll, "grid")
        self._stack.add_named(list_box, "list")
        self._stack.add_named(self._status, "status")

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(self._header)
        toolbar.add_top_bar(self._search_bar)
        toolbar.set_content(self._stack)
        self.set_child(toolbar)

        # Drop files from other apps (or Nautilus) into the current folder —
        # copying into a mount is how you upload. Accepts a GdkFileList.
        drop = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)
        drop.connect("drop", self._on_drop)
        self._stack.add_controller(drop)

        # Delete trashes the current selection (Nautilus-style).
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_key)
        self.add_controller(keys)

    # -- view / sort dropdown --------------------------------------------
    def _view_button(self) -> Gtk.MenuButton:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin_top=8,
                      margin_bottom=8, margin_start=8, margin_end=8)
        box.append(self._caption(_("View as")))
        grid_t = Gtk.ToggleButton(icon_name="view-grid-symbolic", active=True,
                                  tooltip_text=_("Grid"))
        list_t = Gtk.ToggleButton(icon_name="view-list-symbolic",
                                  tooltip_text=_("List"), group=grid_t)
        grid_t.connect("toggled", lambda b: b.get_active() and self._set_view("grid"))
        list_t.connect("toggled", lambda b: b.get_active() and self._set_view("list"))
        toggles = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, css_classes=["linked"],
                          halign=Gtk.Align.CENTER)
        toggles.append(grid_t)
        toggles.append(list_t)
        box.append(toggles)
        box.append(Gtk.Separator())
        box.append(self._caption(_("Sort by")))
        first = None
        for key, label, _w in self._COLUMNS:
            rb = Gtk.CheckButton(label=label, active=(key == self._sort_key))
            if first is None:
                first = rb
            else:
                rb.set_group(first)
            rb.connect("toggled", lambda b, k=key: b.get_active() and self._set_sort(k))
            box.append(rb)
        popover = Gtk.Popover(child=box)
        btn = Gtk.MenuButton(icon_name="view-grid-symbolic",
                             tooltip_text=_("View and sort"), popover=popover)
        btn.set_always_show_arrow(True)  # Nautilus-style dropdown chevron
        self._view_menu_btn = btn
        return btn

    @staticmethod
    def _caption(text: str) -> Gtk.Label:
        lbl = Gtk.Label(label=text, xalign=0)
        lbl.add_css_class("caption-heading")
        lbl.add_css_class("dim-label")
        return lbl

    def _set_view(self, view: str) -> None:
        if view == self._view:
            return
        self._view = view
        if getattr(self, "_view_menu_btn", None) is not None:
            self._view_menu_btn.set_icon_name(
                "view-list-symbolic" if view == "list" else "view-grid-symbolic")
        self._render_entries()

    def _set_sort(self, key: str) -> None:
        if key == self._sort_key:
            self._sort_desc = not self._sort_desc
        else:
            self._sort_key = key
            self._sort_desc = False
        self._render_entries()

    # -- search / filter --------------------------------------------------
    def _on_filter_changed(self, entry) -> None:
        self._filter = entry.get_text().strip().lower()
        self._render_entries()

    def _on_search_mode(self, bar, _pspec) -> None:
        # Closing the search bar clears the filter and restores the full listing.
        if not bar.get_search_mode() and self._filter:
            self._filter = ""
            self._search_entry.set_text("")
            self._render_entries()

    # -- selection (multi-select aware) ----------------------------------
    def _selection(self) -> list[dict]:
        """The entries currently selected in the active view."""
        if self._view == "grid":
            widgets = self._flow.get_selected_children()
        else:
            widgets = self._list.get_selected_rows()
        return [e for e in (getattr(w, "_entry", None) for w in widgets) if e]

    def _on_key(self, _ctrl, keyval, _code, _state) -> bool:
        from gi.repository import Gdk

        if keyval in (Gdk.KEY_Delete, Gdk.KEY_KP_Delete):
            targets = self._selection()
            if targets:
                self._trash(targets)
                return True
        return False

    # -- list header (a non-selectable first row, so columns align) ------
    def _list_header_row(self) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow(selectable=False, activatable=False)
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL,
                      margin_start=12, margin_end=12, margin_top=4, margin_bottom=4)
        spacer = Gtk.Box()
        spacer.set_size_request(50, -1)  # aligns past the chevron + icon columns
        box.append(spacer)
        for key, label, width in self._COLUMNS:
            if key == self._sort_key:
                label = label + (" ▾" if self._sort_desc else " ▴")
            btn = Gtk.Button(label=label, has_frame=False)
            btn.get_child().set_xalign(0)
            btn.add_css_class("flat")
            btn.add_css_class("caption-heading")
            btn.add_css_class("dim-label")
            btn.connect("clicked", lambda _b, k=key: self._set_sort(k))
            if width > 0:
                btn.set_size_request(width, -1)
            else:
                btn.set_hexpand(True)
            box.append(btn)
        row.set_child(box)
        return row

    # -- public -----------------------------------------------------------
    def show_placeholder(self, text: str) -> None:
        self._root = None
        self._history = []
        self._hpos = -1
        self._entries = []
        self._build_crumbs()
        self._set_actions(False)
        self._update_nav()
        self._show_status("folder-symbolic", _("Files"), text)

    def open_root(self, path, title: str) -> None:
        self._root = Path(path)
        self._root_title = title
        self._history = []
        self._hpos = -1
        self._push(self._root)

    # -- navigation -------------------------------------------------------
    def _cur(self) -> Path | None:
        if 0 <= self._hpos < len(self._history):
            return self._history[self._hpos]
        return None

    def _push(self, path: Path) -> None:
        self._history = self._history[:self._hpos + 1]
        self._history.append(Path(path))
        self._hpos = len(self._history) - 1
        self._load()

    def _go(self, delta: int) -> None:
        new = self._hpos + delta
        if 0 <= new < len(self._history):
            self._hpos = new
            self._load()

    def _navigate(self, path: Path) -> None:
        if self._cur() != Path(path):
            self._push(Path(path))

    def _load(self) -> None:
        path = self._cur()
        if path is None:
            return
        # New directory: drop any inline-expanded folders from the previous one,
        # and reset the (current-folder-only) name filter.
        self._expanded = set()
        self._child_cache = {}
        if self._search_btn.get_active():
            self._search_btn.set_active(False)  # also clears _filter via _on_search_mode
        self._filter = ""
        if self._toggle_src:
            GLib.source_remove(self._toggle_src)
            self._toggle_src = None
        self._set_actions(True)
        self._update_nav()
        self._build_crumbs()
        self._show_status("content-loading-symbolic", _("Loading…"), "")
        # Tag the result with the folder it was scanned for: a slow scan (a
        # hung FUSE folder can take seconds) must not overwrite the listing of
        # a folder the user has since navigated to.
        run_async(lambda: scan_directory(path),
                  lambda entries, err: self._on_scanned(path, entries, err))

    def _update_nav(self) -> None:
        self._back.set_sensitive(self._hpos > 0)
        self._fwd.set_sensitive(self._hpos < len(self._history) - 1)

    def _build_crumbs(self) -> None:
        child = self._crumbs.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self._crumbs.remove(child)
            child = nxt
        cur = self._cur()
        if self._root is None or cur is None:
            return
        try:
            rel_parts = cur.relative_to(self._root).parts
        except ValueError:
            rel_parts = ()
        segments = [(self._root_title, self._root)]
        acc = self._root
        for part in rel_parts:
            acc = acc / part
            segments.append((part, acc))
        for i, (label, target) in enumerate(segments):
            btn = Gtk.Button(label=label)
            btn.add_css_class("flat")
            if i == len(segments) - 1:
                btn.add_css_class("suggested-action")
            btn.connect("clicked", lambda _b, t=target: self._navigate(t))
            self._crumbs.append(btn)

    # -- rendering --------------------------------------------------------
    def _on_scanned(self, path, entries, error) -> bool:
        if path != self._cur():
            return False  # stale scan for a folder we've navigated away from
        if error is not None:
            self._show_status("dialog-error-symbolic",
                              _("Couldn't open this folder"), str(error))
            return False
        self._entries = entries
        self._render_entries()
        return False

    def _sort(self, entries) -> list[dict]:
        key = self._sort_key
        if key == "name":
            keyf = lambda e: e["name"].lower()
        elif key == "size":
            keyf = lambda e: e["size"]
        elif key == "mtime":
            keyf = lambda e: e["mtime"]
        else:  # type
            keyf = lambda e: type_label(e).lower()
        dirs = sorted((e for e in entries if e["is_dir"]), key=keyf,
                      reverse=self._sort_desc)
        files = sorted((e for e in entries if not e["is_dir"]), key=keyf,
                       reverse=self._sort_desc)
        return dirs + files

    def _filtered(self, entries) -> list[dict]:
        """Entries whose name contains the active filter (case-insensitive)."""
        if not self._filter:
            return entries
        return [e for e in entries if self._filter in e["name"].lower()]

    def _render_entries(self) -> None:
        if not self._entries:
            self._show_status("folder-symbolic", _("Empty folder"),
                              _("There's nothing here yet."))
            return
        if self._filter and not self._filtered(self._entries):
            self._show_status("system-search-symbolic", _("No matches"),
                              _("Nothing in this folder matches “%s”.") % self._filter)
            return
        if self._view == "grid":
            self._render_grid()
            self._stack.set_visible_child_name("grid")
        else:
            self._render_list()
            self._stack.set_visible_child_name("list")

    def _render_grid(self) -> None:
        self._clear(self._flow)
        for entry in self._sort(self._filtered(self._entries)):
            self._flow.append(self._grid_item(entry))

    def _render_list(self) -> None:
        self._clear(self._list)
        self._list.append(self._list_header_row())

        def walk(entries, depth):
            for entry in self._sort(self._filtered(entries)):
                self._list.append(self._list_row(entry, depth))
                if entry["is_dir"] and entry["path"] in self._expanded:
                    children = self._child_cache.get(entry["path"])
                    if children:
                        walk(children, depth + 1)

        walk(self._entries, 0)

    @staticmethod
    def _clear(container) -> None:
        child = container.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            container.remove(child)
            child = nxt

    def _grid_item(self, entry: dict) -> Gtk.FlowBoxChild:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, margin_top=8,
                      margin_bottom=8, margin_start=4, margin_end=4, width_request=92)
        image = Gtk.Image(pixel_size=48)
        image.set_from_gicon(icon_for(entry))
        box.append(image)
        label = Gtk.Label(label=entry["name"], justify=Gtk.Justification.CENTER,
                          wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR,
                          ellipsize=Pango.EllipsizeMode.END, lines=2, max_width_chars=12)
        label.add_css_class("caption")
        box.append(label)
        child = Gtk.FlowBoxChild()
        child.set_child(box)
        child._entry = entry  # type: ignore[attr-defined]
        child.set_tooltip_text(entry["name"])
        self._attach_menu(child, entry)
        self._attach_drag(child, entry)
        return child

    def _list_row(self, entry: dict, depth: int = 0) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow(activatable=True)
        row._entry = entry  # type: ignore[attr-defined]
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4,
                      margin_top=6, margin_bottom=6,
                      margin_start=12 + depth * 16, margin_end=12)
        # Folders get a disclosure chevron; files a matching spacer so icons line up.
        if entry["is_dir"]:
            chevron = Gtk.Image.new_from_icon_name(
                "pan-down-symbolic" if entry["path"] in self._expanded
                else "pan-end-symbolic")
            chevron.add_css_class("dim-label")
            box.append(chevron)
        else:
            spacer = Gtk.Box()
            spacer.set_size_request(16, -1)
            box.append(spacer)
        name_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10,
                           hexpand=True)
        icon = Gtk.Image(pixel_size=20)
        icon.set_from_gicon(icon_for(entry))
        name_box.append(icon)
        name = Gtk.Label(label=entry["name"], xalign=0, hexpand=True,
                         ellipsize=Pango.EllipsizeMode.END)
        name_box.append(name)
        box.append(name_box)
        box.append(self._cell(human_size(entry["size"]) if not entry["is_dir"] else "",
                              96))
        box.append(self._cell(type_label(entry), 130))
        box.append(self._cell(human_time(entry["mtime"]), 160))
        row.set_child(box)

        click = Gtk.GestureClick(button=1)
        click.connect("pressed", self._on_list_pressed, entry)
        row.add_controller(click)
        self._attach_menu(row, entry)
        self._attach_drag(row, entry)
        return row

    @staticmethod
    def _cell(text: str, width: int) -> Gtk.Label:
        lbl = Gtk.Label(label=text, xalign=0, ellipsize=Pango.EllipsizeMode.END)
        lbl.add_css_class("dim-label")
        lbl.add_css_class("caption")
        lbl.set_size_request(width, -1)
        return lbl

    # In the list, single-click a folder to expand it inline (a tree drop-down);
    # double-click to navigate into it. Files open on double-click.
    def _on_list_pressed(self, gesture, n_press, _x, _y, entry) -> None:
        if entry is None:
            return
        from gi.repository import Gdk

        # Ctrl/Shift-click is multi-selection — let the listbox handle it; don't
        # also expand/navigate the folder under the pointer.
        if gesture.get_current_event_state() & (
                Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.SHIFT_MASK):
            return
        if not entry["is_dir"]:
            if n_press == 2:
                self._activate(entry)
            return
        if n_press >= 2:
            if self._toggle_src:
                GLib.source_remove(self._toggle_src)
                self._toggle_src = None
            self._activate(entry)  # navigate into the folder
        elif n_press == 1:
            if self._toggle_src:
                GLib.source_remove(self._toggle_src)
            self._toggle_src = GLib.timeout_add(250, self._do_toggle, entry["path"])

    def _do_toggle(self, path: str) -> bool:
        self._toggle_src = None
        self._toggle_expand(path)
        return False

    def _toggle_expand(self, path: str) -> None:
        if path in self._expanded:
            self._expanded.discard(path)
            self._render_list()
            return
        self._expanded.add(path)
        if path not in self._child_cache:
            run_async(lambda: scan_directory(Path(path)),
                      lambda res, err: self._on_children(path, res, err))
        self._render_list()  # chevron flips now; children appear once loaded

    def _on_children(self, path, entries, error) -> bool:
        if error is not None or entries is None:
            self._expanded.discard(path)
        else:
            self._child_cache[path] = entries
        if self._view == "list":
            self._render_list()
        return False

    # -- activation / context --------------------------------------------
    def _on_flow_activated(self, _flow, child) -> None:
        self._activate(getattr(child, "_entry", None))

    def _activate(self, entry) -> None:
        if entry is None:
            return
        if entry["is_dir"]:
            self._navigate(Path(entry["path"]))
        else:
            self._open_uri(Path(entry["path"]))

    def _attach_menu(self, widget, entry) -> None:
        gesture = Gtk.GestureClick(button=3)
        gesture.connect("pressed", self._on_right_click, widget, entry)
        widget.add_controller(gesture)

    def _on_right_click(self, _gesture, _n, x, y, widget, entry) -> None:
        # Operate on the whole selection when the clicked item is part of a
        # multi-selection; otherwise act on (and select) just this item.
        selection = self._selection()
        sel_paths = {e["path"] for e in selection}
        multi = entry["path"] in sel_paths and len(selection) > 1
        if not multi:
            if self._view == "grid":
                self._flow.unselect_all()
                self._flow.select_child(widget)
            else:
                self._list.unselect_all()
                self._list.select_row(widget)
        targets = selection if multi else [entry]

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, margin_top=6,
                      margin_bottom=6, margin_start=6, margin_end=6)
        popover = Gtk.Popover(child=box, has_arrow=True)
        popover.set_parent(widget)
        popover.set_pointing_to(gdk_rect(x, y))
        # Tear the popover down once dismissed, otherwise each right-click leaks
        # an unparented-on-popdown popover attached to the row.
        popover.connect("closed", lambda p: p.unparent() if p.get_parent() else None)

        def add(label, handler, arg, *, destructive=False):
            btn = Gtk.Button(label=label)
            btn.add_css_class("flat")
            if destructive:
                btn.add_css_class("destructive-action")
            btn.connect("clicked", lambda *_a: (popover.popdown(), handler(arg)))
            box.append(btn)

        if not multi:  # single-item-only actions
            add(_("Open"), self._activate, entry)
            add(_("Rename…"), self._rename, entry)
        add(_("Copy to…"), self._copy_to, targets)
        add(_("Move to…"), self._move_to, targets)
        add(_("Move to Trash"), self._trash, targets, destructive=True)
        popover.popup()

    def _open_uri(self, path) -> None:
        if path is None:
            return
        uri = "file://" + GLib.Uri.escape_string(str(path), "/", False)
        try:
            Gio.AppInfo.launch_default_for_uri(uri, None)
        except Exception as exc:  # noqa: BLE001
            self._window.add_toast(_("Couldn't open: %s") % exc)

    # -- drag and drop ----------------------------------------------------
    def _attach_drag(self, widget, entry: dict) -> None:
        """Let a row/tile be dragged out to other apps (Nautilus, mail, browser)
        as a real file. Dragging an item that's part of the current multi-select
        drags the whole selection."""
        source = Gtk.DragSource(actions=Gdk.DragAction.COPY)
        source.connect("prepare", self._on_drag_prepare, entry)
        widget.add_controller(source)

    def _on_drag_prepare(self, _source, _x, _y, entry):
        # Drag the whole selection if this item is in it; otherwise just this one.
        selected = self._selection()
        paths = [e["path"] for e in selected] if entry in selected else []
        if entry["path"] not in paths:
            paths = [entry["path"]]
        files = [Gio.File.new_for_path(p) for p in paths if p]
        if not files:
            return None
        return Gdk.ContentProvider.new_for_value(
            GObject.Value(Gdk.FileList, Gdk.FileList.new_from_list(files)))

    def _on_drop(self, _target, value, _x, _y) -> bool:
        """Copy files dropped from another app into the current folder."""
        cur = self._cur()
        if cur is None:
            return False
        dest_dir = str(cur)
        try:
            files = value.get_files()
        except AttributeError:
            return False
        srcs = []
        for f in files:
            p = f.get_path()
            # Skip items already sitting in this folder (a drop onto itself).
            if p and os.path.dirname(os.path.normpath(p)) != os.path.normpath(dest_dir):
                srcs.append(p)
        if not srcs:
            return False

        def work():
            # Conflict-free targets, probed off-thread (a stat on a FUSE mount
            # can block): silently overwriting a same-named file destroyed it —
            # and the copy's Undo then deleted the victim for good.
            targets = []
            for src in srcs:
                target = self._conflict_free(
                    dest_dir, os.path.basename(src.rstrip("/")))
                targets.append(target)
                if os.path.isdir(src):
                    shutil.copytree(src, target)
                else:
                    shutil.copy2(src, target)
            return targets

        self._window.add_toast(_("Copying here…"))
        run_async(work, lambda targets, err: self._after_transfer(
            err, False, srcs, targets, reload=True))
        return True

    @staticmethod
    def _conflict_free(dest_dir: str, name: str) -> str:
        """A target path in ``dest_dir`` that doesn't collide with an existing
        entry: "report.docx" → "report (2).docx". Touches the filesystem —
        call off-thread for network mounts."""
        target = os.path.join(dest_dir, name)
        if not os.path.lexists(target):
            return target
        stem, ext = os.path.splitext(name)
        for i in range(2, 1000):
            cand = os.path.join(dest_dir, f"{stem} ({i}){ext}")
            if not os.path.lexists(cand):
                return cand
        return target

    # -- file operations (off-thread; trash is recoverable) --------------
    def _set_actions(self, enabled: bool) -> None:
        for btn in (self._new_folder_btn, self._upload_btn, self._open_ext_btn,
                    self._search_btn):
            btn.set_sensitive(enabled)

    @staticmethod
    def _as_list(entries) -> list[dict]:
        items = entries if isinstance(entries, list) else [entries]
        return [e for e in items if e]

    def _toast_then_reload(self, error, ok_msg: str, err_msg: str) -> bool:
        if error:
            self._window.add_toast(err_msg % error)
        else:
            self._window.add_toast(ok_msg)
            self._load()
        return False

    def _reload_with_undo(self, error, ok_msg: str, err_msg: str, undo) -> bool:
        """Like ``_toast_then_reload`` but, on success, shows a toast whose
        Undo button reverses the operation via ``undo()``."""
        if error:
            self._window.add_toast(err_msg % error)
        else:
            self._window.add_undo_toast(ok_msg, undo)
            self._load()
        return False

    @staticmethod
    def _valid_child_name(name: str):
        """Validate a user-typed file/folder name. Returns an error message to
        toast, or None when the name is usable as a single path component."""
        if name in (".", ".."):
            return _("That name is reserved.")
        if "/" in name or "\x00" in name:
            return _("Names can't contain “/”.")
        if len(name.encode()) > 255:
            return _("That name is too long.")
        return None

    def _rename(self, entry: dict) -> None:
        dialog = Adw.AlertDialog(heading=_("Rename"),
                                 body=_("Enter a new name for “%s”.") % entry["name"])
        row = Adw.EntryRow(title=_("Name"))
        row.set_text(entry["name"])
        group = Adw.PreferencesGroup()
        group.add(row)
        dialog.set_extra_child(group)
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("rename", _("Rename"))
        dialog.set_response_appearance("rename", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("rename")

        def on_response(_d, response):
            cur = self._cur()
            if response != "rename" or cur is None:
                return
            name = row.get_text().strip()
            if not name or name == entry["name"]:
                return
            err = self._valid_child_name(name)
            if err:
                self._window.add_toast(err)
                return
            dest = os.path.join(str(cur), name)
            src = entry["path"]
            try:
                if not Path(dest).resolve().is_relative_to(Path(cur).resolve()):
                    self._window.add_toast(_("Invalid destination."))
                    return
            except OSError:
                self._window.add_toast(_("Invalid destination."))
                return

            def undo():
                run_async(lambda: os.rename(dest, src),
                          lambda _r, err: self._toast_then_reload(
                              err, _("Rename undone."), _("Couldn't undo: %s")))

            run_async(lambda: os.rename(src, dest),
                      lambda _r, err: self._reload_with_undo(
                          err, _("Renamed."), _("Couldn't rename: %s"), undo))

        dialog.connect("response", on_response)
        dialog.present(self._window)

    def _trash(self, entries) -> None:
        entries = self._as_list(entries)
        if not entries:
            return
        n = len(entries)
        dialog = Adw.AlertDialog(
            heading=_("Move to Trash?"),
            body=(_("“%s” will be moved to the Trash.") % entries[0]["name"]
                  if n == 1 else
                  _("%d items will be moved to the Trash.") % n))
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("trash", _("Move to Trash"))
        dialog.set_response_appearance("trash", Adw.ResponseAppearance.DESTRUCTIVE)

        def on_response(_d, response):
            if response != "trash":
                return
            paths = [e["path"] for e in entries]

            def work():
                for path in paths:
                    Gio.File.new_for_path(path).trash(None)

            def undo():
                self._window.add_toast(_("Restoring…"))
                run_async(lambda: _restore_from_trash(paths),
                          lambda restored, err: self._toast_then_reload(
                              err or (None if restored else _("nothing to restore")),
                              _("Restored from Trash."),
                              _("Couldn't undo: %s")))

            run_async(work, lambda _r, err: self._reload_with_undo(
                err, _("Moved to Trash."), _("Couldn't delete: %s"), undo))

        dialog.connect("response", on_response)
        dialog.present(self._window)

    # Copy/Move to a chosen folder. Copying to a local folder is how you
    # "download" a file off a network mount.
    def _copy_to(self, entries) -> None:
        self._transfer(self._as_list(entries), move=False)

    def _move_to(self, entries) -> None:
        self._transfer(self._as_list(entries), move=True)

    def _transfer(self, entries, *, move: bool) -> None:
        if not entries:
            return
        dialog = Gtk.FileDialog(
            title=_("Move to folder") if move else _("Copy to folder"))

        def on_pick(fd, result):
            try:
                folder = fd.select_folder_finish(result)
            except GLib.Error:
                return  # cancelled
            dest_dir = folder.get_path() if folder else None
            if not dest_dir:
                return
            srcs = [e["path"] for e in entries]

            def work():
                # Conflict-free targets, probed off-thread (see _conflict_free)
                # so an existing same-named file is never silently replaced.
                targets = []
                for src in srcs:
                    target = self._conflict_free(
                        dest_dir, os.path.basename(src.rstrip("/")))
                    targets.append(target)
                    if move:
                        shutil.move(src, target)
                    elif os.path.isdir(src):
                        shutil.copytree(src, target)
                    else:
                        shutil.copy2(src, target)
                return targets

            self._window.add_toast(_("Moving…") if move else _("Copying…"))
            run_async(work, lambda targets, err: self._after_transfer(
                err, move, srcs, targets))

        dialog.select_folder(self._window, None, on_pick)

    def _after_transfer(self, error, move: bool, srcs=None, targets=None,
                        reload: bool = False) -> bool:
        if error:
            self._window.add_toast(
                (_("Move failed: %s") if move else _("Copy failed: %s")) % error)
            return False

        def undo():
            # Move back to where it came from; a copy is undone by removing the
            # new copies (the originals are untouched).
            def work():
                for src, target in zip(srcs or [], targets or []):
                    if move:
                        shutil.move(target, src)
                    elif os.path.isdir(target):
                        shutil.rmtree(target, ignore_errors=True)
                    else:
                        try:
                            os.remove(target)
                        except OSError:
                            pass
            run_async(work, lambda _r, err: self._toast_then_reload(
                err, _("Move undone.") if move else _("Copy undone."),
                _("Couldn't undo: %s")))

        self._window.add_undo_toast(_("Moved.") if move else _("Copied."), undo)
        if move or reload:
            self._load()  # sources gone (move) or new copies landed here (drop)
        return False

    def _on_new_folder(self, _btn) -> None:
        dialog = Adw.AlertDialog(heading=_("New folder"),
                                 body=_("Name the new folder."))
        row = Adw.EntryRow(title=_("Name"))
        row.set_text(_("New folder"))
        group = Adw.PreferencesGroup()
        group.add(row)
        dialog.set_extra_child(group)
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("create", _("Create"))
        dialog.set_response_appearance("create", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("create")

        def on_response(_d, response):
            cur = self._cur()
            if response != "create" or cur is None:
                return
            name = row.get_text().strip()
            if not name:
                return
            err = self._valid_child_name(name)
            if err:
                self._window.add_toast(err)
                return
            dest = os.path.join(str(cur), name)
            try:
                if not Path(dest).resolve().is_relative_to(Path(cur).resolve()):
                    self._window.add_toast(_("Invalid destination."))
                    return
            except OSError:
                self._window.add_toast(_("Invalid destination."))
                return
            run_async(lambda: os.mkdir(dest),
                      lambda _r, err: self._toast_then_reload(
                          err, _("Folder created."), _("Couldn't create folder: %s")))

        dialog.connect("response", on_response)
        dialog.present(self._window)

    def _on_upload(self, _btn) -> None:
        cur = self._cur()
        if cur is None:
            return
        dialog = Gtk.FileDialog(title=_("Upload files"))

        def on_pick(fd, result):
            try:
                files = fd.open_multiple_finish(result)
            except GLib.Error:
                return
            paths = [f.get_path() for f in files if f.get_path()]
            if not paths:
                return
            dest_dir = str(cur)

            def work():
                for src in paths:
                    shutil.copy2(src, self._conflict_free(
                        dest_dir, os.path.basename(src)))

            self._window.add_toast(_("Uploading…"))
            run_async(work, lambda _r, err: self._toast_then_reload(
                err, _("Upload complete."), _("Upload failed: %s")))

        dialog.open_multiple(self._window, None, on_pick)

    def _show_status(self, icon: str, title: str, description: str) -> None:
        # StatusPage parses title/description as Pango markup — escape both so an
        # error string or folder name containing '&'/'<' doesn't render blank.
        self._status.set_icon_name(icon)
        self._status.set_title(esc(title))
        self._status.set_description(esc(description) if description else None)
        self._stack.set_visible_child_name("status")
