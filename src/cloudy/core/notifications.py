# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Shahab Nedaei
"""Desktop notifications for new mail and upcoming events.

A small background poller that runs on the GTK main loop (network work is
off-loaded to threads) and raises ``Gio.Notification``s through the application.
Clicking a notification activates the app and deep-links to the message/calendar
(see the ``notify-open-*`` actions in ``application.py``).

Best-effort and self-contained: it never touches the views and silently skips
accounts it can't reach. Honours the ``notifications-enabled`` GSetting.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from gettext import gettext as _

from gi.repository import Gio, GLib

from .interfaces import capabilities_of

# How often to poll, and how far ahead an event must be to trigger a reminder.
_POLL_SECONDS = 120
_FIRST_POLL_SECONDS = 12
_REMINDER_WINDOW = timedelta(minutes=15)
_MAX_MAIL_PER_TICK = 4  # don't flood on a busy inbox


def _short_sender(value: str) -> str:
    """A display name from a 'Name <addr>' string (best-effort)."""
    value = (value or "").strip()
    if "<" in value:
        name = value.split("<", 1)[0].strip().strip('"')
        return name or value
    return value


def _parse_dt(value: str) -> datetime | None:
    """Parse an event start into an aware UTC datetime (Graph times are UTC)."""
    if not value:
        return None
    txt = value.strip()
    if "T" not in txt:  # a bare date (all-day) → no minute-level reminder
        return None
    try:
        dt = datetime.fromisoformat(txt.replace("Z", "+00:00"))
    except ValueError:
        # Graph sometimes returns 7-digit fractional seconds; trim and retry.
        head = txt.split(".", 1)[0]
        try:
            dt = datetime.fromisoformat(head)
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


class NotificationManager:
    def __init__(self, app):
        self._app = app
        self._seen_mail: dict[str, set] = {}      # account id -> {message id}
        self._primed: set = set()                 # accounts whose baseline is set
        self._notified_events: set = set()        # f"{acct}:{event id}"
        self._timer = None

    # -- lifecycle --------------------------------------------------------
    def start(self) -> None:
        if self._timer is not None:
            return
        # Prime soon (baseline, no notifications), then poll on a steady cadence.
        # The priming tick is one-shot — return False so it doesn't keep firing
        # every _FIRST_POLL_SECONDS alongside the steady timer (doubling traffic).
        GLib.timeout_add_seconds(_FIRST_POLL_SECONDS, self._prime_once)
        self._timer = GLib.timeout_add_seconds(_POLL_SECONDS, self._tick)

    def _prime_once(self) -> bool:
        self._tick()
        return False  # GLib removes this source

    def _enabled(self) -> bool:
        try:
            return self._app.settings.get_boolean("notifications-enabled")
        except Exception:  # noqa: BLE001 - never let a settings hiccup crash polling
            return True

    # -- polling ----------------------------------------------------------
    def _tick(self) -> bool:
        if not self._enabled():
            return True
        for account in self._app.registry.accounts():
            if not account.signed_in:
                continue
            module = self._app.engine.get(account.module_id)
            if module is not None and not self._app.engine.is_enabled(account.module_id):
                continue
            caps = capabilities_of(module) if module else []
            if "mail" in caps:
                self._poll_mail(account)
            if "calendar" in caps:
                self._poll_calendar(account)
        return True  # keep the timer alive

    def _poll_mail(self, account) -> None:
        first_time = account.id not in self._primed

        def work():
            from ..widgets.clients import build_account_client

            client = build_account_client(self._app, account)
            folder = "INBOX" if account.provider == "google" else "inbox"
            return client.list_messages(folder)

        threading.Thread(
            target=self._run, args=(work, lambda r, e: self._on_mail(account, first_time, r, e)),
            daemon=True).start()

    def _on_mail(self, account, first_time, messages, error) -> bool:
        if error or messages is None:
            return False
        seen = self._seen_mail.setdefault(account.id, set())
        ids = {m.get("id") for m in messages}
        if first_time:  # baseline only: learn current inbox without notifying
            seen.update(ids)
            self._primed.add(account.id)
            return False
        fresh = [m for m in messages
                 if m.get("id") not in seen and not m.get("is_read", True)]
        seen.update(ids)
        for msg in fresh[:_MAX_MAIL_PER_TICK]:
            self._notify_mail(account, msg)
        return False

    def _notify_mail(self, account, msg) -> None:
        sender = _short_sender(msg.get("from", "")) or _("Someone")
        subject = msg.get("subject", "") or _("(no subject)")
        note = Gio.Notification.new(_("New mail · %s") % account.display_name)
        note.set_body(f"{sender}: {subject}")
        note.set_icon(Gio.ThemedIcon.new(self._app.application_id))
        note.set_priority(Gio.NotificationPriority.NORMAL)
        payload = GLib.Variant("s", f"{account.id}\x1f{msg.get('id', '')}")
        note.set_default_action(
            Gio.Action.print_detailed_name("app.notify-open-mail", payload))
        self._app.send_notification(f"mail-{account.id}-{msg.get('id', '')}", note)

    def _poll_calendar(self, account) -> None:
        now = datetime.now(timezone.utc)
        start_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        end_iso = (now + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")

        def work():
            from ..widgets.clients import build_account_client

            client = build_account_client(self._app, account)
            return client.list_events(start_iso, end_iso)

        threading.Thread(
            target=self._run, args=(work, lambda r, e: self._on_calendar(account, r, e)),
            daemon=True).start()

    def _on_calendar(self, account, events, error) -> bool:
        if error or not events:
            return False
        now = datetime.now(timezone.utc)
        for ev in events:
            if ev.get("all_day"):
                continue
            key = f"{account.id}:{ev.get('id')}"
            if key in self._notified_events:
                continue
            start = _parse_dt(ev.get("start", ""))
            if start is None:
                continue
            delta = start - now
            if timedelta(0) <= delta <= _REMINDER_WINDOW:
                self._notify_event(account, ev, start)
                self._notified_events.add(key)
        return False

    def _notify_event(self, account, ev, start) -> None:
        subject = ev.get("subject", "") or _("(no title)")
        when = start.astimezone().strftime("%H:%M")
        note = Gio.Notification.new(_("Upcoming event"))
        note.set_body(_("%(title)s at %(time)s") % {"title": subject, "time": when})
        note.set_icon(Gio.ThemedIcon.new(self._app.application_id))
        note.set_priority(Gio.NotificationPriority.HIGH)
        note.set_default_action(Gio.Action.print_detailed_name(
            "app.notify-open-calendar", GLib.Variant("s", account.id)))
        self._app.send_notification(f"event-{account.id}-{ev.get('id', '')}", note)

    # -- thread plumbing --------------------------------------------------
    @staticmethod
    def _run(work, on_done) -> None:
        try:
            result = work()
            GLib.idle_add(on_done, result, None)
        except Exception as exc:  # noqa: BLE001 - surfaced as an error arg
            GLib.idle_add(on_done, None, str(exc))
