# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Fiber Elements
"""Calendar surface: upcoming events for the next week."""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from gettext import gettext as _

from gi.repository import Adw, GLib, Gtk


class CalendarView(Adw.Bin):
    __gtype_name__ = "CloudyCalendarView"

    def __init__(self, window, account):
        super().__init__()
        self._window = window
        self._account = account

        self._page = Adw.PreferencesPage()
        self.set_child(self._page)
        self._group = Adw.PreferencesGroup(
            title=_("Upcoming"), description=_("Events in the next 7 days.")
        )
        self._page.add(self._group)
        self._rows: list = []
        self._has_data = False

        self._cache_key = f"{account.id}:events:7d"
        cached = self._window.get_application().cache.get(self._cache_key)
        if cached is not None:
            self._render(cached[0])
            if cached[1]:
                return
        else:
            self._set_rows([Adw.ActionRow(title=_("Loading calendar…"))])
        self._load_async()

    def _set_rows(self, rows) -> None:
        for r in self._rows:
            self._group.remove(r)
        self._rows = list(rows)
        for r in self._rows:
            self._group.add(r)

    def _load_async(self) -> None:
        now = datetime.now(timezone.utc)
        start_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        end_iso = (now + timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")

        def worker():
            try:
                from .clients import build_account_client

                client = build_account_client(
                    self._window.get_application(), self._account
                )
                events = client.list_events(start_iso, end_iso)
                GLib.idle_add(self._on_loaded, events, None)
            except Exception as exc:  # noqa: BLE001
                GLib.idle_add(self._on_loaded, None, str(exc))

        threading.Thread(target=worker, daemon=True).start()

    def _on_loaded(self, events, error) -> bool:
        if error:
            if not self._has_data:
                self._set_rows([Adw.ActionRow(title=_("Couldn't load calendar"),
                                              subtitle=error)])
            return False
        self._window.get_application().cache.set(self._cache_key, events)
        self._render(events)
        return False

    def _render(self, events) -> None:
        if not events:
            self._set_rows([Adw.ActionRow(title=_("No events in the next 7 days."))])
            return
        self._set_rows([self._event_row(e) for e in events])
        self._has_data = True

    def _event_row(self, event) -> Adw.ActionRow:
        from .format import esc

        when = _format_range(event.get("start", ""), event.get("end", ""), event.get("all_day"))
        subtitle = when
        if event.get("location"):
            subtitle = f"{when} · {event['location']}" if when else event["location"]
        row = Adw.ActionRow(title=esc(event.get("subject") or _("(no title)")),
                            subtitle=esc(subtitle))
        row.add_prefix(Gtk.Image.new_from_icon_name("x-office-calendar-symbolic"))
        return row


def _format_range(start: str, end: str, all_day: bool) -> str:
    if not start or "T" not in start:
        return start
    date, _, rest = start.partition("T")
    if all_day:
        return date
    start_t = rest[:5]
    end_t = end.partition("T")[2][:5] if end and "T" in end else ""
    return f"{date} {start_t}–{end_t}" if end_t else f"{date} {start_t}"
