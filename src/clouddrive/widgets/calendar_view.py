# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Fiber Elements
"""Calendar surface: upcoming events for the next week."""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from gettext import gettext as _

from gi.repository import Adw, GLib, Gtk


class CalendarView(Adw.Bin):
    __gtype_name__ = "ClouddriveCalendarView"

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
        self._loading = Adw.ActionRow(title=_("Loading calendar…"))
        self._group.add(self._loading)

        self._load_async()

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
        self._group.remove(self._loading)
        if error:
            self._group.add(
                Adw.ActionRow(title=_("Couldn't load calendar"), subtitle=error)
            )
            return False
        if not events:
            self._group.add(Adw.ActionRow(title=_("No events in the next 7 days.")))
            return False
        for event in events:
            self._group.add(self._event_row(event))
        return False

    def _event_row(self, event) -> Adw.ActionRow:
        when = _format_range(event.get("start", ""), event.get("end", ""), event.get("all_day"))
        subtitle = when
        if event.get("location"):
            subtitle = f"{when} · {event['location']}" if when else event["location"]
        row = Adw.ActionRow(title=event["subject"], subtitle=subtitle)
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
