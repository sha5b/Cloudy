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
import time
from datetime import datetime
from gettext import gettext as _
from pathlib import Path

from gi.repository import Adw, Gio, GLib, Gtk, Pango

from .source_nav import run_async


def recent_changes(roots: list[Path], *, limit: int = 8, max_scan: int = 3000,
                   time_budget: float = 4.0) -> list[dict]:
    """Most-recently-modified files under ``roots`` (for the Dashboard).

    Bounded three ways so a slow network mount can never hang the caller: by
    file count (``max_scan``) and by a wall-clock ``time_budget`` (seconds) —
    whichever trips first stops the walk and we return what we found. A Google
    Drive rclone mount, for instance, can take seconds *per directory*, so the
    count guard alone isn't enough; the deadline is what actually caps it.
    """
    found: list[dict] = []
    seen: set[str] = set()
    roots = [Path(r) for r in roots]
    dirs = [r for r in roots if r.is_dir() and str(r) not in seen
            and not seen.add(str(r))]
    if not dirs:
        return []
    overall_deadline = time.monotonic() + time_budget
    # Fair share per root: one big/slow account folder must not starve the
    # others (that's why the Dashboard previously showed only one account). Each
    # root gets its own file cap and time slice, plus the overall deadline.
    per_root_cap = max(50, max_scan // len(dirs))
    per_root_budget = time_budget / len(dirs)
    for root in dirs:
        root_scanned = 0
        root_deadline = min(overall_deadline, time.monotonic() + per_root_budget)
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            for fn in filenames:
                if fn.startswith("."):
                    continue
                root_scanned += 1
                fp = os.path.join(dirpath, fn)
                try:
                    found.append({"name": fn, "path": fp, "mtime": os.path.getmtime(fp)})
                except OSError:
                    continue
                # Check inside the inner loop too: a single huge directory on a
                # FUSE mount can exceed the budget before the outer check.
                if root_scanned >= per_root_cap or time.monotonic() >= root_deadline:
                    break
            if root_scanned >= per_root_cap or time.monotonic() >= root_deadline:
                break
        if time.monotonic() >= overall_deadline:
            break
    found.sort(key=lambda e: e["mtime"], reverse=True)
    return found[:limit]


def _scan(path: Path) -> list[dict]:
    """List a directory with size/mtime, folders flagged."""
    out = []
    with os.scandir(path) as it:
        for entry in it:
            if entry.name.startswith("."):
                continue
            try:
                is_dir = entry.is_dir()
                st = entry.stat()
            except OSError:
                continue
            out.append({
                "name": entry.name, "is_dir": is_dir, "path": entry.path,
                "size": 0 if is_dir else st.st_size, "mtime": st.st_mtime,
            })
    return out


def _human_size(n: int) -> str:
    size = float(n)
    for unit in ("B", "kB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return (f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}")
        size /= 1024
    return f"{size:.1f} TB"


def _human_time(mtime: float) -> str:
    try:
        dt = datetime.fromtimestamp(mtime)
    except (OSError, OverflowError, ValueError):
        return ""
    today = datetime.now().date()
    if dt.date() == today:
        return _("Today %s") % dt.strftime("%H:%M")
    return dt.strftime("%Y-%m-%d %H:%M")


def _type_label(entry: dict) -> str:
    if entry["is_dir"]:
        return _("Folder")
    ctype, _unc = Gio.content_type_guess(entry["name"], None)
    if ctype:
        return Gio.content_type_get_description(ctype)
    return _("File")


def _icon_for(entry: dict) -> Gio.Icon:
    if entry["is_dir"]:
        return Gio.ThemedIcon.new("folder")
    ctype, _unc = Gio.content_type_guess(entry["name"], None)
    if ctype:
        return Gio.content_type_get_icon(ctype)
    return Gio.ThemedIcon.new("text-x-generic")


def _gdk_rect(x: float, y: float):
    from gi.repository import Gdk

    rect = Gdk.Rectangle()
    rect.x, rect.y, rect.width, rect.height = int(x), int(y), 1, 1
    return rect


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
        self._header.pack_end(self._view_button())
        self._header.pack_end(self._open_ext_btn)
        self._header.pack_end(self._upload_btn)
        self._header.pack_end(self._new_folder_btn)

        # -- views -------------------------------------------------------
        # Single click selects; double click opens (Nautilus-style).
        self._flow = Gtk.FlowBox(
            selection_mode=Gtk.SelectionMode.SINGLE, homogeneous=True,
            activate_on_single_click=False,
            valign=Gtk.Align.START, max_children_per_line=12, min_children_per_line=2,
            row_spacing=6, column_spacing=6, margin_top=12, margin_bottom=12,
            margin_start=12, margin_end=12)
        self._flow.connect("child-activated", self._on_flow_activated)
        grid_scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER,
                                         vexpand=True, child=self._flow)

        # The list fills the pane directly (like the grid view) — no extra card.
        self._list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE,
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
        toolbar.set_content(self._stack)
        self.set_child(toolbar)

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
        # New directory: drop any inline-expanded folders from the previous one.
        self._expanded = set()
        self._child_cache = {}
        if self._toggle_src:
            GLib.source_remove(self._toggle_src)
            self._toggle_src = None
        self._set_actions(True)
        self._update_nav()
        self._build_crumbs()
        self._show_status("content-loading-symbolic", _("Loading…"), "")
        run_async(lambda: _scan(path), self._on_scanned)

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
    def _on_scanned(self, entries, error) -> bool:
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
            keyf = lambda e: _type_label(e).lower()
        dirs = sorted((e for e in entries if e["is_dir"]), key=keyf,
                      reverse=self._sort_desc)
        files = sorted((e for e in entries if not e["is_dir"]), key=keyf,
                       reverse=self._sort_desc)
        return dirs + files

    def _render_entries(self) -> None:
        if not self._entries:
            self._show_status("folder-symbolic", _("Empty folder"),
                              _("There's nothing here yet."))
            return
        if self._view == "grid":
            self._render_grid()
            self._stack.set_visible_child_name("grid")
        else:
            self._render_list()
            self._stack.set_visible_child_name("list")

    def _render_grid(self) -> None:
        self._clear(self._flow)
        for entry in self._sort(self._entries):
            self._flow.append(self._grid_item(entry))

    def _render_list(self) -> None:
        self._clear(self._list)
        self._list.append(self._list_header_row())

        def walk(entries, depth):
            for entry in self._sort(entries):
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
        image.set_from_gicon(_icon_for(entry))
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
        icon.set_from_gicon(_icon_for(entry))
        name_box.append(icon)
        name = Gtk.Label(label=entry["name"], xalign=0, hexpand=True,
                         ellipsize=Pango.EllipsizeMode.END)
        name_box.append(name)
        box.append(name_box)
        box.append(self._cell(_human_size(entry["size"]) if not entry["is_dir"] else "",
                              96))
        box.append(self._cell(_type_label(entry), 130))
        box.append(self._cell(_human_time(entry["mtime"]), 160))
        row.set_child(box)

        click = Gtk.GestureClick(button=1)
        click.connect("pressed", self._on_list_pressed, entry)
        row.add_controller(click)
        self._attach_menu(row, entry)
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
    def _on_list_pressed(self, _gesture, n_press, _x, _y, entry) -> None:
        if entry is None:
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
            run_async(lambda: _scan(Path(path)),
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
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, margin_top=6,
                      margin_bottom=6, margin_start=6, margin_end=6)
        popover = Gtk.Popover(child=box, has_arrow=True)
        popover.set_parent(widget)
        popover.set_pointing_to(_gdk_rect(x, y))

        def add(label, handler, *, destructive=False):
            btn = Gtk.Button(label=label)
            btn.add_css_class("flat")
            if destructive:
                btn.add_css_class("destructive-action")
            btn.connect("clicked", lambda *_a: (popover.popdown(), handler(entry)))
            box.append(btn)

        add(_("Open"), self._activate)
        add(_("Rename"), self._rename)
        add(_("Move to Trash"), self._trash, destructive=True)
        popover.popup()

    def _open_uri(self, path) -> None:
        if path is None:
            return
        uri = "file://" + GLib.Uri.escape_string(str(path), "/", False)
        try:
            Gio.AppInfo.launch_default_for_uri(uri, None)
        except Exception as exc:  # noqa: BLE001
            self._window.add_toast(_("Couldn't open: %s") % exc)

    # -- file operations (off-thread; trash is recoverable) --------------
    def _set_actions(self, enabled: bool) -> None:
        for btn in (self._new_folder_btn, self._upload_btn, self._open_ext_btn):
            btn.set_sensitive(enabled)

    def _toast_then_reload(self, error, ok_msg: str, err_msg: str) -> bool:
        if error:
            self._window.add_toast(err_msg % error)
        else:
            self._window.add_toast(ok_msg)
            self._load()
        return False

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
            dest = os.path.join(str(cur), name)
            src = entry["path"]
            run_async(lambda: os.rename(src, dest),
                      lambda _r, err: self._toast_then_reload(
                          err, _("Renamed."), _("Couldn't rename: %s")))

        dialog.connect("response", on_response)
        dialog.present(self._window)

    def _trash(self, entry: dict) -> None:
        dialog = Adw.AlertDialog(
            heading=_("Move to Trash?"),
            body=_("“%s” will be moved to the Trash.") % entry["name"])
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("trash", _("Move to Trash"))
        dialog.set_response_appearance("trash", Adw.ResponseAppearance.DESTRUCTIVE)

        def on_response(_d, response):
            if response != "trash":
                return
            path = entry["path"]
            run_async(
                lambda: Gio.File.new_for_path(path).trash(None),
                lambda _r, err: self._toast_then_reload(
                    err, _("Moved to Trash."), _("Couldn't delete: %s")))

        dialog.connect("response", on_response)
        dialog.present(self._window)

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
            dest = os.path.join(str(cur), name)
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
                    shutil.copy2(src, os.path.join(dest_dir, os.path.basename(src)))

            self._window.add_toast(_("Uploading…"))
            run_async(work, lambda _r, err: self._toast_then_reload(
                err, _("Upload complete."), _("Upload failed: %s")))

        dialog.open_multiple(self._window, None, on_pick)

    def _show_status(self, icon: str, title: str, description: str) -> None:
        self._status.set_icon_name(icon)
        self._status.set_title(title)
        self._status.set_description(description or None)
        self._stack.set_visible_child_name("status")
