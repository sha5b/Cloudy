# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Fiber Elements
"""Dashboard: everything at a glance across all signed-in accounts.

Merges calendar events from every account into one timeline, and groups recent
mail by account (unread first), so the user sees their whole day in one view.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from gettext import gettext as _

from gi.repository import Adw, GLib, Gtk


class DashboardView(Adw.Bin):
    __gtype_name__ = "CloudyDashboardView"

    def __init__(self, window):
        super().__init__()
        self._window = window
        self._registry = window.get_application().registry

        self._page = Adw.PreferencesPage()
        self.set_child(self._page)

        self._cal_group = Adw.PreferencesGroup(
            title=_("Upcoming"), description=_("All calendars, next 7 days.")
        )
        self._page.add(self._cal_group)
        self._cal_loading = Adw.ActionRow(title=_("Loading calendars…"))
        self._cal_group.add(self._cal_loading)

        self._mail_group = Adw.PreferencesGroup(
            title=_("Recent mail"), description=_("Across all your accounts.")
        )
        self._page.add(self._mail_group)
        self._mail_loading = Adw.ActionRow(title=_("Loading mail…"))
        self._mail_group.add(self._mail_loading)

        self._accounts = [
            a for a in self._registry.accounts()
            if a.signed_in and a.provider in ("microsoft", "google")
        ]
        if not self._accounts:
            self._cal_group.remove(self._cal_loading)
            self._mail_group.remove(self._mail_loading)
            self._page.add(self._empty_group())
            return

        self._load_async()

    def _empty_group(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup()
        group.add(
            Adw.ActionRow(
                title=_("Nothing to show yet"),
                subtitle=_("Sign in to an account to see your day here."),
            )
        )
        return group

    # -- aggregation (off the UI thread) ---------------------------------
    def _load_async(self) -> None:
        now = datetime.now(timezone.utc)
        start_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        end_iso = (now + timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
        accounts = list(self._accounts)

        def worker():
            from .clients import build_account_client

            events, messages = [], []
            for account in accounts:
                try:
                    client = build_account_client(
                        self._window.get_application(), account
                    )
                    for ev in client.list_events(start_iso, end_iso):
                        events.append((account, ev))
                    for msg in client.list_messages():
                        messages.append((account, msg))
                except Exception:  # noqa: BLE001 - one bad account shouldn't blank the view
                    continue
            events.sort(key=lambda pair: pair[1].get("start", ""))
            # Unread first, then newest.
            messages.sort(
                key=lambda pair: (pair[1].get("is_read", True), ),
            )
            GLib.idle_add(self._populate, events, messages)

        threading.Thread(target=worker, daemon=True).start()

    def _populate(self, events, messages) -> bool:
        self._cal_group.remove(self._cal_loading)
        self._mail_group.remove(self._mail_loading)

        if not events:
            self._cal_group.add(Adw.ActionRow(title=_("No upcoming events.")))
        for account, ev in events[:20]:
            self._cal_group.add(self._event_row(account, ev))

        if not messages:
            self._mail_group.add(Adw.ActionRow(title=_("No recent mail.")))
        for account, msg in messages[:20]:
            self._mail_group.add(self._mail_row(account, msg))
        return False

    # -- rows -------------------------------------------------------------
    def _event_row(self, account, ev) -> Adw.ActionRow:
        when = _fmt(ev.get("start", ""), ev.get("all_day"))
        subtitle = f"{when} · {account.display_name}" if when else account.display_name
        row = Adw.ActionRow(title=ev.get("subject", _("(no title)")), subtitle=subtitle)
        row.add_prefix(Gtk.Image.new_from_icon_name("x-office-calendar-symbolic"))
        if ev.get("location"):
            row.add_suffix(Gtk.Label(label=ev["location"], css_classes=["dim-label"]))
        return row

    def _mail_row(self, account, msg) -> Adw.ActionRow:
        subtitle = f"{msg.get('from', '')} · {account.display_name}"
        row = Adw.ActionRow(title=msg.get("subject") or _("(no subject)"),
                            subtitle=subtitle)
        row.set_title_lines(1)
        row.set_subtitle_lines(1)
        if not msg.get("is_read", True):
            dot = Gtk.Image.new_from_icon_name("media-record-symbolic")
            dot.add_css_class("accent")
            row.add_prefix(dot)
        else:
            row.add_prefix(Gtk.Image.new_from_icon_name("mail-read-symbolic"))
        return row


def _fmt(start: str, all_day: bool) -> str:
    if not start or "T" not in start:
        return start
    date, _, rest = start.partition("T")
    return date if all_day else f"{date} {rest[:5]}"
