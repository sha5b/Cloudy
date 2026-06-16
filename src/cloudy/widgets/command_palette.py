# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Shahab Nedaei
"""A keyboard-first command palette (Ctrl+K).

A quick, fuzzy-filtered jump-to: every signed-in account's Files/Mail/Calendar/
Chat/Teams surface, plus app actions (Preferences, Add account). Type to filter,
↑/↓ to move, Enter to go, Esc to dismiss — so the whole app is reachable without
the mouse, the way every keyboard-driven mail client works.
"""

from __future__ import annotations

from gettext import gettext as _

from gi.repository import Adw, Gdk, Gtk

from ..core.interfaces import capabilities_of


class CommandPalette(Adw.Dialog):
    __gtype_name__ = "CloudyCommandPalette"

    def __init__(self, window):
        super().__init__()
        self._window = window
        self._entries = self._build_entries()
        self._query = ""

        self.set_title(_("Go to"))
        self.set_content_width(560)
        self.set_content_height(440)

        self._search = Gtk.SearchEntry(
            placeholder_text=_("Search accounts, mail, calendar, chat…"),
            hexpand=True, margin_top=10, margin_bottom=6,
            margin_start=10, margin_end=10)
        self._search.connect("search-changed", self._on_search_changed)
        self._search.connect("activate", lambda *_: self._activate_first())
        # Let ↑/↓ from the entry drive the list without leaving the field.
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_key)
        self._search.add_controller(keys)

        self._list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self._list.add_css_class("navigation-sidebar")
        self._list.set_filter_func(self._filter_row)
        self._list.connect("row-activated", self._on_row_activated)
        scroll = Gtk.ScrolledWindow(
            hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        scroll.set_child(self._list)

        for entry in self._entries:
            self._list.append(self._row(entry))
        self._select_first()

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.append(self._search)
        box.append(scroll)
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())
        toolbar.set_content(box)
        self.set_child(toolbar)
        self.set_focus(self._search)

    # -- entries ----------------------------------------------------------
    def _build_entries(self) -> list[dict]:
        from ..window import CAPABILITY_UI

        app = self._window.get_application()
        registry = app.registry
        engine = app.engine
        entries: list[dict] = []
        for account in registry.accounts():
            if not account.signed_in:
                continue
            module = engine.get(account.module_id)
            if module is None or not engine.is_enabled(account.module_id):
                continue
            caps = capabilities_of(module)
            if account.is_personal:  # consumer accounts can't use Teams/Chat
                caps = [c for c in caps if c not in ("chat", "teams")]
            for key in caps:
                label, icon = CAPABILITY_UI.get(
                    key, (key, "application-x-addon-symbolic"))
                entries.append({
                    "title": label,
                    "subtitle": account.display_name,
                    "icon": icon,
                    "action": (lambda a=account, k=key:
                               self._window.open_account_tab(a, k)),
                })
        # App-level actions, always available.
        entries.append({"title": _("Preferences"), "subtitle": _("Settings"),
                        "icon": "emblem-system-symbolic",
                        "action": lambda: app.activate_action("preferences", None)})
        entries.append({"title": _("Add account"), "subtitle": _("Settings"),
                        "icon": "list-add-symbolic",
                        "action": lambda: app.activate_action("add-account", None)})
        return entries

    def _row(self, entry: dict) -> Gtk.ListBoxRow:
        from .format import esc

        row = Gtk.ListBoxRow()
        row._entry = entry  # type: ignore[attr-defined]
        row._search = f"{entry['title']} {entry['subtitle']}".lower()
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12,
                      margin_top=8, margin_bottom=8, margin_start=12, margin_end=12)
        box.append(Gtk.Image.new_from_icon_name(entry["icon"]))
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
        title = Gtk.Label(label=esc(entry["title"]), xalign=0, use_markup=True)
        title.add_css_class("body")
        text.append(title)
        sub = Gtk.Label(label=esc(entry["subtitle"]), xalign=0, use_markup=True)
        sub.add_css_class("caption")
        sub.add_css_class("dim-label")
        text.append(sub)
        box.append(text)
        row.set_child(box)
        return row

    # -- filtering / selection -------------------------------------------
    def _on_search_changed(self, entry) -> None:
        self._query = entry.get_text().strip().lower()
        self._list.invalidate_filter()
        self._select_first()

    def _filter_row(self, row) -> bool:
        if not self._query:
            return True
        text = getattr(row, "_search", "")
        return all(part in text for part in self._query.split())

    def _visible_rows(self) -> list:
        rows = []
        child = self._list.get_first_child()
        while child is not None:
            if self._filter_row(child):
                rows.append(child)
            child = child.get_next_sibling()
        return rows

    def _select_first(self) -> None:
        rows = self._visible_rows()
        self._list.select_row(rows[0] if rows else None)

    # -- keyboard / activation -------------------------------------------
    def _on_key(self, _ctrl, keyval, _code, _state) -> bool:
        rows = self._visible_rows()
        if not rows:
            return False
        cur = self._list.get_selected_row()
        idx = rows.index(cur) if cur in rows else -1
        if keyval in (Gdk.KEY_Down, Gdk.KEY_Tab):
            self._list.select_row(rows[min(len(rows) - 1, idx + 1)])
            return True
        if keyval in (Gdk.KEY_Up, Gdk.KEY_ISO_Left_Tab):
            self._list.select_row(rows[max(0, idx - 1)])
            return True
        return False

    def _activate_first(self) -> None:
        row = self._list.get_selected_row() or (self._visible_rows() or [None])[0]
        if row is not None:
            self._run(row._entry)

    def _on_row_activated(self, _list, row) -> None:
        self._run(row._entry)

    def _run(self, entry: dict) -> None:
        self.close()
        entry["action"]()
