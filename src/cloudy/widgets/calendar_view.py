# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Shahab Nedaei
"""Calendar surface: a month grid with an agenda list alongside.

Left pane = the source switcher (Microsoft: Me / Teams / Shared) and an agenda
list of the displayed month's events (past and future). Right pane = a month
**grid** (``widgets/month_grid.MonthGrid``). Clicking an event — in the agenda
or a grid chip — opens it in a standalone, non-modal **event window**
(``widgets/event_window.EventDetailWindow``) rather than an inline pane.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from gettext import gettext as _

from gi.repository import Adw, Gdk, GLib, Gtk, Pango

from .event_window import EventDetailWindow
from .month_grid import MonthGrid
from .source_nav import (
    SCOPE_HINT,
    SourceTabs,
    action_row,
    clear_listbox,
    is_pinned,
    is_scope_error,
    message_row,
    present_add_shared_dialog,
    run_async,
    toggle_pin,
)


class CalendarView(Adw.Bin):
    __gtype_name__ = "CloudyCalendarView"

    def __init__(self, window, account):
        super().__init__()
        self._window = window
        self._account = account
        # Me / Teams / Shared sources are work/school-only; personal Microsoft
        # accounts (and Google) get just their own calendar.
        self._is_ms = account.provider == "microsoft" and not account.is_personal
        self._source = "me"
        self._context = None        # group id (teams) or address (shared)
        self._groups = None         # lazily loaded list[{id, name}]
        self._ctx_items: list = []
        self._suppress = False
        self._events: list = []
        self._has_data = False
        self._query = ""  # agenda search filter

        # -- right pane: month grid (built first; the loader feeds it) ----
        self._grid = MonthGrid(on_event=self._open_event, on_range=self._on_grid_range)

        # -- left pane: source switcher + agenda list --------------------
        self._ctx_dd = Gtk.DropDown(model=Gtk.StringList.new([]), tooltip_text=_("Choose"))
        self._ctx_dd.add_css_class("flat")
        self._ctx_dd.set_hexpand(True)
        self._ctx_dd.connect("notify::selected", self._on_ctx_changed)
        self._add_shared_btn = Gtk.Button(
            icon_name="list-add-symbolic", tooltip_text=_("Add a shared mailbox"))
        self._add_shared_btn.add_css_class("flat")
        self._add_shared_btn.connect("clicked", self._on_add_shared)
        self._star_btn = Gtk.Button(
            icon_name="non-starred-symbolic",
            tooltip_text=_("Pin this calendar to the Dashboard"))
        self._star_btn.add_css_class("flat")
        self._star_btn.connect("clicked", self._on_star_clicked)
        self._ctx_current = None  # {"id", "name"} of the selected team/shared source

        self._list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.MULTIPLE,
                                 valign=Gtk.Align.START)
        self._list.add_css_class("navigation-sidebar")
        self._list.connect("row-activated", self._on_row_activated)
        self._list.set_filter_func(self._filter_row)
        # ←/→ and ↑/↓ move between events; Shift extends; Delete removes the
        # selection; Enter opens. (Arrows only move — opening spawns a window.)
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_list_key)
        self._list.add_controller(keys)
        list_scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER,
                                         vexpand=True)
        list_scroll.set_child(self._list)

        self._search = Gtk.SearchEntry(placeholder_text=_("Search events…"), hexpand=True)
        self._search.connect("search-changed", self._on_search_changed)

        new_btn = Gtk.Button(
            icon_name="appointment-new-symbolic", tooltip_text=_("New event"))
        new_btn.connect("clicked", self._on_new_event_clicked)

        sidebar_tb = Adw.ToolbarView()
        if self._is_ms:
            header = Adw.HeaderBar(
                show_start_title_buttons=False, show_end_title_buttons=False,
                title_widget=SourceTabs(self._on_source_changed))
            header.pack_start(new_btn)
            sidebar_tb.add_top_bar(header)
            bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6,
                          margin_top=6, margin_bottom=6, margin_start=10, margin_end=10)
            range_lbl = Gtk.Label(label=_("Agenda"), xalign=0, hexpand=True)
            range_lbl.add_css_class("dim-label")
            bar.append(range_lbl)
            bar.append(self._ctx_dd)
            bar.append(self._star_btn)
            bar.append(self._add_shared_btn)
            sidebar_tb.add_top_bar(bar)
        else:
            header = Adw.HeaderBar(
                show_start_title_buttons=False, show_end_title_buttons=False,
                title_widget=Gtk.Label(label=_("Agenda")))
            header.pack_start(new_btn)
            sidebar_tb.add_top_bar(header)
        search_bar = Gtk.Box(margin_top=6, margin_bottom=6,
                            margin_start=10, margin_end=10)
        search_bar.append(self._search)
        sidebar_tb.add_top_bar(search_bar)
        sidebar_tb.set_content(list_scroll)
        sidebar_page = Adw.NavigationPage(title=_("Calendar"), tag="agenda")
        sidebar_page.set_child(sidebar_tb)

        content_header = Adw.HeaderBar(show_start_title_buttons=False,
                                       show_end_title_buttons=False,
                                       title_widget=Gtk.Label(label=_("Calendar")))
        content_tb = Adw.ToolbarView()
        content_tb.add_top_bar(content_header)
        content_tb.set_content(self._grid)
        content_page = Adw.NavigationPage(title=_("Calendar"), tag="month")
        content_page.set_child(content_tb)

        self._split = Adw.NavigationSplitView(
            min_sidebar_width=300, max_sidebar_width=420, sidebar_width_fraction=0.32,
        )
        self._split.set_sidebar(sidebar_page)
        self._split.set_content(content_page)
        self.set_child(self._split)

        self._update_source_ui()
        self._select_context(None)  # load "Me" from cache or the server

    # -- range (the grid's visible month drives what we fetch) -----------
    def _range_iso(self) -> tuple[str, str]:
        first, last = self._grid.visible_range()
        return f"{first}T00:00:00Z", f"{last}T23:59:59Z"

    def _on_grid_range(self, _first: str, _last: str) -> None:
        # The grid moved to another month — refetch that span.
        self._has_data = False
        self._load_async()

    # -- cache + agenda list ---------------------------------------------
    def _cache_key(self) -> str:
        first, _last = self._grid.visible_range()
        month = first[:7]  # YYYY-MM of the visible grid
        if self._source == "teams" and self._context:
            return f"{self._account.id}:events:group:{self._context}:{month}"
        if self._source == "shared" and self._context:
            return f"{self._account.id}:events:shared:{self._context}:{month}"
        return f"{self._account.id}:events:me:{month}"

    def _clear(self) -> None:
        clear_listbox(self._list)

    def _set_message(self, text: str) -> None:
        self._clear()
        self._list.append(message_row(text))

    def _render(self, events) -> None:
        self._events = events
        self._grid.set_events(events)
        self._clear()
        if not events:
            self._set_message(_("No events this month."))
            self._has_data = False
            return
        last_day = None
        for ev in events:
            day = (ev.get("start", "") or "").partition("T")[0]
            if day != last_day:
                self._list.append(self._day_header(day))
                last_day = day
            self._list.append(self._event_row(ev))
        self._has_data = True

    def _day_header(self, day: str) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow(activatable=False, selectable=False)
        label = Gtk.Label(label=_pretty_day(day), xalign=0,
                          margin_top=10, margin_bottom=2, margin_start=12)
        label.add_css_class("heading")
        label.add_css_class("dim-label")
        row.set_child(label)
        return row

    def _event_row(self, ev) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow(activatable=True)
        row._ev = ev  # type: ignore[attr-defined]
        row._search = f"{ev.get('subject', '')} {ev.get('location', '')}".lower()
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12,
                      margin_top=8, margin_bottom=8, margin_start=12, margin_end=12)
        row.set_child(box)

        time_lbl = Gtk.Label(label=_time_label(ev), xalign=0, width_chars=11)
        time_lbl.add_css_class("caption")
        time_lbl.add_css_class("dim-label")
        time_lbl.set_valign(Gtk.Align.START)
        box.append(time_lbl)

        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True)
        box.append(text)
        if _is_live(ev):
            live = Gtk.Label(label=_("● Live now"), xalign=0)
            live.add_css_class("cloudy-live")
            text.append(live)
        title = Gtk.Label(label=ev.get("subject") or _("(no title)"), xalign=0,
                          ellipsize=Pango.EllipsizeMode.END)
        title.add_css_class("body")
        text.append(title)
        # Location, else the owning calendar's name (Google merges several
        # calendars into one agenda — show which one each event came from).
        subtitle = ev.get("location") or ev.get("calendar")
        if subtitle:
            sub = Gtk.Label(label=subtitle, xalign=0,
                            ellipsize=Pango.EllipsizeMode.END)
            sub.add_css_class("caption")
            sub.add_css_class("dim-label")
            text.append(sub)
        return row

    # -- search (filter the agenda) --------------------------------------
    def _on_search_changed(self, entry) -> None:
        self._query = entry.get_text().strip().lower()
        self._list.invalidate_filter()

    def _filter_row(self, row) -> bool:
        if not self._query:
            return True
        text = getattr(row, "_search", None)  # event rows only; hides day headers
        return text is not None and self._query in text

    # -- sources (Me / Teams / Shared) -----------------------------------
    def _shared_addresses(self) -> list:
        return list(self._account.shared_mailboxes or [])

    def _update_source_ui(self) -> None:
        if not self._is_ms:
            return
        self._ctx_dd.set_visible(self._source in ("teams", "shared"))
        self._add_shared_btn.set_visible(self._source == "shared")
        if self._source == "me":
            self._ctx_current = None
        self._update_star()

    def _update_star(self) -> None:
        active = self._source in ("teams", "shared") and self._ctx_current is not None
        self._star_btn.set_visible(active)
        if not active:
            return
        pinned = is_pinned(self._account, "calendar", self._source, self._ctx_current["id"])
        self._star_btn.set_icon_name("starred-symbolic" if pinned else "non-starred-symbolic")

    def _on_star_clicked(self, _btn) -> None:
        if self._ctx_current is None:
            return
        toggle_pin(self._window, self._account, kind="calendar", source=self._source,
                   sid=self._ctx_current["id"], name=self._ctx_current["name"])
        self._update_star()

    def _on_source_changed(self, source) -> None:
        if source == self._source:
            return
        self._source = source
        self._update_source_ui()
        if source == "me":
            self._select_context(None)
        else:
            self._populate_context()

    def _populate_context(self) -> None:
        """Fill the context dropdown (teams list or shared-mailbox list)."""
        if self._source == "teams":
            if self._groups is None:  # first visit: fetch the group list
                self._set_message(_("Loading teams…"))
                self._load_groups_async()
                return
            items = [{"id": g["id"], "name": g["name"]} for g in self._groups]
            empty = _("No team calendars.")
        else:  # shared
            items = [{"id": a, "name": a} for a in self._shared_addresses()]
            empty = _("Add a shared mailbox with +.")
        self._ctx_items = items
        self._suppress = True
        self._ctx_dd.set_model(Gtk.StringList.new([i["name"] for i in items] or [_("None")]))
        self._ctx_dd.set_sensitive(bool(items))
        self._ctx_dd.set_selected(0)
        self._suppress = False
        if not items:
            self._ctx_current = None
            self._update_star()
            self._set_message(empty)
            self._grid.set_events([])
            return
        self._ctx_current = items[0]
        self._update_star()
        self._select_context(items[0]["id"])

    def _on_ctx_changed(self, dropdown, _pspec) -> None:
        if self._suppress:
            return
        idx = dropdown.get_selected()
        if 0 <= idx < len(self._ctx_items):
            self._ctx_current = self._ctx_items[idx]
            self._update_star()
            self._select_context(self._ctx_items[idx]["id"])

    def _on_add_shared(self, _btn) -> None:
        present_add_shared_dialog(
            self._window, self._account, lambda _addr: self._populate_context())

    def _load_groups_async(self) -> None:
        from .graph_helper import build_graph_client

        run_async(
            lambda: build_graph_client(
                self._window.get_application(), self._account).list_groups(),
            self._on_groups_loaded,
        )

    def _on_groups_loaded(self, groups, error) -> bool:
        if error is not None:
            self._groups = []
            if is_scope_error(error) or "Group.Read" in error:
                self._reauth_prompt()
            else:
                self._set_message(_("Couldn't load teams: %s") % error)
            return False
        self._groups = groups
        if self._source == "teams":
            self._populate_context()
        return False

    def _reauth_prompt(self) -> None:
        self._clear()
        self._list.append(action_row(
            SCOPE_HINT, _("Re-sign in"),
            lambda: self._window.sign_in_account(self._account)))

    # -- loading events ---------------------------------------------------
    def _select_context(self, context) -> None:
        """Switch to a calendar source, showing cached events if fresh enough."""
        self._context = context
        self._has_data = False
        cached = self._window.get_application().cache.get(self._cache_key())
        if cached is not None:
            self._render(cached[0])
            if cached[1]:
                return  # fresh; skip the fetch
        else:
            self._set_message(_("Loading calendar…"))
        self._load_async()

    def _load_async(self) -> None:
        start_iso, end_iso = self._range_iso()
        source, context, key = self._source, self._context, self._cache_key()

        def work():
            from .clients import build_account_client

            client = build_account_client(self._window.get_application(), self._account)
            if source == "teams" and context:
                return client.list_group_events(context, start_iso, end_iso)
            if source == "shared" and context:
                return client.list_shared_events(context, start_iso, end_iso)
            return client.list_events(start_iso, end_iso)

        run_async(work, lambda events, error: self._on_loaded(key, events, error))

    def _on_loaded(self, key, events, error) -> bool:
        # Cache successful loads even if the user already switched away.
        if not error and events is not None:
            self._window.get_application().cache.set(key, events)
            # Mirror your own calendar into the GNOME Shell calendar (EDS),
            # best-effort and off-thread (no-op unless the setting is on).
            if key.startswith(f"{self._account.id}:events:me:") and events:
                self._publish_to_eds(events)
        if key != self._cache_key():
            return False  # a stale response for a source/month we left
        if error:
            if not self._has_data:
                if is_scope_error(error):
                    self._reauth_prompt()
                else:
                    self._set_message(_("Couldn't load calendar: %s") % error)
            return False
        self._render(events)
        return False

    # -- open / create ----------------------------------------------------
    def _on_row_activated(self, _list, row) -> None:
        ev = getattr(row, "_ev", None)
        if ev is not None and ev.get("id"):
            self._open_event(ev)

    def _open_event(self, ev) -> None:
        self.open_event(ev["id"])

    def open_event(self, event_id) -> None:
        """Open an event in a detail window (also the notification deep-link)."""
        EventDetailWindow(self._window, self._account, event_id,
                          on_changed=self._reload_current).present()

    # -- keyboard navigation / multi-select ------------------------------
    def _event_rows(self) -> list:
        rows = []
        child = self._list.get_first_child()
        while child is not None:
            if getattr(child, "_ev", None) is not None:
                rows.append(child)
            child = child.get_next_sibling()
        return rows

    def _nav(self, delta: int, *, extend: bool = False) -> None:
        rows = self._event_rows()
        if not rows:
            return
        sel = [r for r in self._list.get_selected_rows() if r in rows]
        if not sel:
            target = rows[0]
        else:
            cur = rows.index(sel[-1])
            target = rows[max(0, min(len(rows) - 1, cur + delta))]
        if not extend:
            self._list.unselect_all()
        self._list.select_row(target)
        target.grab_focus()

    def _on_list_key(self, _ctrl, keyval, _code, state) -> bool:
        shift = bool(state & Gdk.ModifierType.SHIFT_MASK)
        if keyval in (Gdk.KEY_Up, Gdk.KEY_Left):
            self._nav(-1, extend=shift)
            return True
        if keyval in (Gdk.KEY_Down, Gdk.KEY_Right):
            self._nav(+1, extend=shift)
            return True
        if keyval in (Gdk.KEY_Delete, Gdk.KEY_KP_Delete):
            self._delete_selected()
            return True
        return False

    def _delete_selected(self) -> None:
        if self._is_ms and self._source == "teams":
            self._window.add_toast(_("Team calendars are read-only here."))
            return
        rows = [r for r in self._list.get_selected_rows()
                if getattr(r, "_ev", None) is not None]
        ids = [r._ev["id"] for r in rows if r._ev.get("id")]
        if not ids:
            return
        for row in rows:  # optimistic removal
            self._list.remove(row)
        self._window.add_toast(
            _("Deleted %d events") % len(ids) if len(ids) > 1 else _("Event deleted"))

        def work():
            from .clients import build_account_client

            client = build_account_client(self._window.get_application(), self._account)
            for ev_id in ids:
                client.delete_event(ev_id)

        run_async(work, lambda _r, error: self._on_deleted(error))

    def _on_deleted(self, error) -> bool:
        if error:
            self._window.add_toast(_("Couldn't delete: %s") % error)
        self._reload_current()  # resync from the server either way
        return False

    def _create_context(self):
        """Return ``(source, address)`` for the calendar to write to."""
        if self._is_ms and self._source == "shared" and self._ctx_current is not None:
            return "shared", self._ctx_current["id"]
        return "me", None

    def _on_new_event_clicked(self, _btn) -> None:
        if self._is_ms and self._source == "teams":
            self._window.add_toast(
                _("Team calendars are read-only here. Switch to Me or Shared."))
            return
        source, address = self._create_context()
        on_calendar = address if (source == "shared" and address) \
            else self._account.display_name

        def create(**fields):
            from .clients import build_account_client

            client = build_account_client(self._window.get_application(), self._account)
            result = client.create_event(source=source, address=address, **fields)
            GLib.idle_add(self._reload_current)
            return result

        from .event_compose import EventWindow

        EventWindow(self._window, on_calendar=on_calendar,
                    create_fn=create).present()

    def _reload_current(self) -> bool:
        """Force a re-fetch of the active source/month after a write."""
        self._has_data = False
        self._load_async()
        return False

    def _publish_to_eds(self, events) -> None:
        import threading

        app = self._window.get_application()
        account = self._account

        def work():
            try:
                from ..core.eds_publish import publish_events

                publish_events(app, account, events)
            except Exception:  # noqa: BLE001 - EDS mirroring never affects the UI
                pass

        threading.Thread(target=work, daemon=True).start()


def _parse_iso(value: str):
    if not value or "T" not in value:
        return None
    txt = value.strip().replace("Z", "+00:00")
    try:
        d = datetime.fromisoformat(txt)
    except ValueError:
        try:
            d = datetime.fromisoformat(txt.split(".", 1)[0])
        except ValueError:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc)


def _is_live(ev) -> bool:
    """True when the event is happening right now (start ≤ now ≤ end)."""
    if ev.get("all_day"):
        return False
    start = _parse_iso(ev.get("start", ""))
    end = _parse_iso(ev.get("end", ""))
    if start is None or end is None:
        return False
    return start <= datetime.now(timezone.utc) <= end


def _time_label(ev) -> str:
    start = ev.get("start", "")
    if ev.get("all_day"):
        return _("All day")
    if "T" in start:
        return start.partition("T")[2][:5]
    return ""


def _pretty_day(day: str) -> str:
    try:
        d = datetime.strptime(day, "%Y-%m-%d").date()
    except ValueError:
        return day
    today = datetime.now().date()
    if d == today:
        return _("Today · %s") % day
    if d == today + timedelta(days=1):
        return _("Tomorrow · %s") % day
    return day
