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
from gettext import ngettext

from gi.repository import Gio, GLib

from .interfaces import capabilities_of

# How often to poll, and how far ahead an event must be to trigger a reminder.
_POLL_SECONDS = 120
_FIRST_POLL_SECONDS = 12
_REMINDER_WINDOW = timedelta(minutes=15)
_MAX_MAIL_PER_TICK = 4  # don't flood on a busy inbox
# Batch routine (tier-2) banners into one summary on this cadence, instead of
# pinging per message (Iqbal & Bailey 2008 breakpoint deferral). Only active at
# notify-level 'digest'; the queue is held while DND/quiet hours are on.
_DIGEST_SECONDS = 600


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
        self._unread: dict[str, int] = {}         # account id -> inbox unread count
        self._seen_chat: dict[str, dict] = {}     # account id -> {chat id: last_at}
        self._chat_unread: dict[str, set] = {}    # account id -> {chat id with new msgs}
        self._digest: dict[str, dict] = {}        # account id -> pending tier-2 summary
        self._timer = None
        self._digest_timer = None

    def unread_count(self, account_id: str) -> int:
        """Last-polled inbox unread count for an account (0 until first poll)."""
        return self._unread.get(account_id, 0)

    def chat_unread_count(self, account_id: str) -> int:
        """Number of chats with unseen new messages (0 until first poll)."""
        return len(self._chat_unread.get(account_id, ()))

    def is_chat_unread(self, account_id: str, chat_id: str) -> bool:
        """Whether a specific chat is flagged unread (so the chat list can show
        the same chats the red badge counts — keeping the two consistent)."""
        return chat_id in self._chat_unread.get(account_id, ())

    def mark_chat_read(self, account_id: str, chat_id: str) -> None:
        """Clear a chat's "new message" mark (called when the user opens it)."""
        seen = self._chat_unread.get(account_id)
        if seen and chat_id in seen:
            seen.discard(chat_id)
            self._push_chat_badge(account_id)

    def _push_chat_badge(self, account_id: str) -> None:
        win = self._app.props.active_window
        if win is not None and hasattr(win, "set_account_chat_unread"):
            win.set_account_chat_unread(account_id, self.chat_unread_count(account_id))

    # -- lifecycle --------------------------------------------------------
    def start(self) -> None:
        if self._timer is not None:
            return
        # Prime soon (baseline, no notifications), then poll on a steady cadence.
        # The priming tick is one-shot — return False so it doesn't keep firing
        # every _FIRST_POLL_SECONDS alongside the steady timer (doubling traffic).
        GLib.timeout_add_seconds(_FIRST_POLL_SECONDS, self._prime_once)
        self._timer = GLib.timeout_add_seconds(_POLL_SECONDS, self._tick)
        self._digest_timer = GLib.timeout_add_seconds(
            _DIGEST_SECONDS, self._flush_digest)

    def _prime_once(self) -> bool:
        self._tick()
        return False  # GLib removes this source

    def _enabled(self) -> bool:
        try:
            return self._app.settings.get_boolean("notifications-enabled")
        except Exception:  # noqa: BLE001 - never let a settings hiccup crash polling
            return True

    def _bool(self, key: str, default: bool) -> bool:
        try:
            return self._app.settings.get_boolean(key)
        except Exception:  # noqa: BLE001
            return default

    def _str(self, key: str, default: str) -> str:
        try:
            return self._app.settings.get_string(key)
        except Exception:  # noqa: BLE001
            return default

    # -- attention gating (DND / quiet hours / relevance tier) -----------
    # Research (Mark et al. 2008; Iqbal & Bailey 2008) is unambiguous that
    # ill-timed, low-relevance interruptions are the costly ones — so banners are
    # gated by the system DND state, a nightly quiet-hours window, and a
    # relevance tier. Badges/unread counts always update; only the *banner* is
    # suppressed, so nothing is lost — it's just not interruptive.
    def _gnome_notif_settings(self):
        """GNOME's notification settings (for the DND/show-banners flag), or None
        when that schema isn't installed (e.g. inside a minimal Flatpak runtime).
        Looked up once and cached; a None result degrades to 'not in DND'."""
        if not hasattr(self, "_gnome_notif"):
            self._gnome_notif = None
            try:
                src = Gio.SettingsSchemaSource.get_default()
                if src is not None and src.lookup(
                        "org.gnome.desktop.notifications", True) is not None:
                    self._gnome_notif = Gio.Settings.new(
                        "org.gnome.desktop.notifications")
            except Exception:  # noqa: BLE001
                self._gnome_notif = None
        return self._gnome_notif

    def _system_dnd_active(self) -> bool:
        if not self._bool("notify-respect-system-dnd", True):
            return False
        try:
            settings = self._gnome_notif_settings()
            # show-banners flips to False while GNOME Do Not Disturb is on.
            return settings is not None and not settings.get_boolean("show-banners")
        except Exception:  # noqa: BLE001
            return False

    def _quiet_hours_active(self) -> bool:
        if not self._bool("quiet-hours-enabled", False):
            return False
        start = self._str("quiet-hours-start", "22:00")
        end = self._str("quiet-hours-end", "08:00")
        if start == end:
            return False
        now = datetime.now().strftime("%H:%M")  # zero-padded → lexical compare ok
        if start < end:
            return start <= now < end
        return now >= start or now < end  # window wraps past midnight

    def _focus_active(self) -> bool:
        """True when banners should be withheld (badges still update)."""
        return self._system_dnd_active() or self._quiet_hours_active()

    def _allowed(self, tier: int) -> bool:
        """Whether a banner of the given relevance tier (1 = direct/important,
        2 = ambient) should interrupt *immediately* right now. At any level other
        than 'all', tier-2 is held back — to a digest ('digest') or to the badge
        only ('priority'); see ``_digest_active`` and ``_on_chat``/``_on_mail``."""
        if not self._enabled() or self._focus_active():
            return False
        if tier > 1 and self._str("notify-level", "all") != "all":
            return False
        return True

    def _digest_active(self) -> bool:
        """Whether routine (tier-2) alerts are batched into a periodic summary."""
        return self._enabled() and self._str("notify-level", "all") == "digest"

    # -- digest (batched tier-2 banners) ----------------------------------
    def _digest_bucket(self, account) -> dict:
        bucket = self._digest.get(account.id)
        if bucket is None:
            bucket = {"name": account.display_name, "chats": {}, "msgs": 0, "mail": 0}
            self._digest[account.id] = bucket
        return bucket

    def _enqueue_chat(self, account, chat) -> None:
        bucket = self._digest_bucket(account)
        bucket["chats"][chat["id"]] = chat.get("name", "") or _("Chat")
        bucket["msgs"] += 1

    def _enqueue_mail(self, account) -> None:
        self._digest_bucket(account)["mail"] += 1

    def _flush_digest(self) -> bool:
        if not self._digest:
            return True
        if not self._enabled():
            self._digest.clear()
            return True
        # Hold the queue while DND / quiet hours are on; release once focus clears
        # so a batched summary still surfaces (nothing is silently dropped).
        if self._focus_active():
            return True
        for account_id, bucket in list(self._digest.items()):
            note = self._build_digest(account_id, bucket)
            if note is not None:
                self._app.send_notification(f"digest-{account_id}", note)
            self._digest.pop(account_id, None)
        return True

    def _build_digest(self, account_id: str, bucket: dict):
        parts = []
        if bucket["msgs"]:
            chats = len(bucket["chats"])
            msgs = bucket["msgs"]
            parts.append(_("%(msgs)s in %(chats)s") % {
                "msgs": ngettext("%d new message", "%d new messages", msgs) % msgs,
                "chats": ngettext("%d chat", "%d chats", chats) % chats,
            })
        if bucket["mail"]:
            mail = bucket["mail"]
            parts.append(ngettext("%d new email", "%d new emails", mail) % mail)
        if not parts:
            return None
        note = Gio.Notification.new(_("New activity · %s") % bucket["name"])
        note.set_body(" · ".join(parts))
        note.set_icon(self._type_icon("preferences-system-notifications-symbolic"))
        note.set_priority(Gio.NotificationPriority.LOW)
        # An empty id routes to the relevant tab (no single message to deep-link).
        payload = GLib.Variant("s", f"{account_id}\x1f")
        action = "app.notify-open-chat" if bucket["chats"] else "app.notify-open-mail"
        note.set_default_action(Gio.Action.print_detailed_name(action, payload))
        return note

    @staticmethod
    def _muted_ids(account, kind: str) -> set:
        return {m.get("id") for m in (getattr(account, "muted_sources", None) or [])
                if m.get("kind") == kind}

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
            if "chat" in caps:
                self._poll_chat(account)
        return True  # keep the timer alive

    def _poll_mail(self, account) -> None:
        first_time = account.id not in self._primed

        def work():
            from ..widgets.clients import build_account_client

            client = build_account_client(self._app, account)
            folder = "INBOX" if account.provider == "google" else "inbox"
            messages = client.list_messages(folder)
            try:
                unread = client.inbox_unread()
            except Exception:  # noqa: BLE001 - count is best-effort; fall back
                unread = sum(1 for m in messages if not m.get("is_read", True))
            return (messages, unread)

        threading.Thread(
            target=self._run, args=(work, lambda r, e: self._on_mail(account, first_time, r, e)),
            daemon=True).start()

    def _on_mail(self, account, first_time, result, error) -> bool:
        if error or result is None:
            return False
        messages, unread = result
        self._unread[account.id] = unread
        win = self._app.props.active_window
        if win is not None and hasattr(win, "set_account_unread"):
            win.set_account_unread(account.id, unread)
        seen = self._seen_mail.setdefault(account.id, set())
        ids = {m.get("id") for m in messages}
        if first_time:  # baseline only: learn current inbox without notifying
            seen.update(ids)
            self._primed.add(account.id)
            return False
        fresh = [m for m in messages
                 if m.get("id") not in seen and not m.get("is_read", True)]
        seen.update(ids)
        # Live-update the open mail list (like the badge, this happens even when
        # the banner is suppressed by DND/quiet hours — nothing is interruptive).
        if fresh and win is not None and hasattr(win, "refresh_account_mail"):
            win.refresh_account_mail(account.id)
        immediate = []
        for msg in fresh:
            # Important mail interrupts (tier 1); ordinary mail is ambient (tier 2).
            tier = 1 if msg.get("important") else 2
            if self._allowed(tier):
                immediate.append((msg, tier))
            elif tier > 1 and self._digest_active():
                self._enqueue_mail(account)  # count all routine mail for the digest
        for msg, tier in immediate[:_MAX_MAIL_PER_TICK]:  # but cap live banners
            self._notify_mail(account, msg, tier)
        return False

    def _type_icon(self, symbolic_name: str) -> Gio.Icon:
        """A per-kind notification icon (mail / calendar / chat) with the app
        logo as the fallback, so GNOME shows a recognizable glyph like its own
        native apps instead of the same Cloudy logo for everything."""
        return Gio.ThemedIcon.new_from_names(
            [symbolic_name, self._app.application_id])

    def _notify_mail(self, account, msg, tier: int = 2) -> None:
        sender = _short_sender(msg.get("from", "")) or _("Someone")
        subject = msg.get("subject", "") or _("(no subject)")
        note = Gio.Notification.new(_("New mail · %s") % account.display_name)
        note.set_body(f"{sender}: {subject}")
        note.set_icon(self._type_icon("mail-unread-symbolic"))
        note.set_priority(Gio.NotificationPriority.HIGH if tier == 1
                          else Gio.NotificationPriority.NORMAL)
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
                # Meeting reminders are time-critical (tier 1). Mark notified even
                # when suppressed (DND/quiet) — firing a stale reminder late is
                # worse than skipping it.
                if self._allowed(1):
                    self._notify_event(account, ev, start)
                self._notified_events.add(key)
        return False

    def _notify_event(self, account, ev, start) -> None:
        subject = ev.get("subject", "") or _("(no title)")
        when = start.astimezone().strftime("%H:%M")
        note = Gio.Notification.new(_("Upcoming event"))
        note.set_body(_("%(title)s at %(time)s") % {"title": subject, "time": when})
        note.set_icon(self._type_icon("x-office-calendar-symbolic"))
        note.set_priority(Gio.NotificationPriority.HIGH)
        payload = GLib.Variant("s", f"{account.id}\x1f{ev.get('id', '')}")
        note.set_default_action(
            Gio.Action.print_detailed_name("app.notify-open-calendar", payload))
        self._app.send_notification(f"event-{account.id}-{ev.get('id', '')}", note)

    # -- chat (Teams / Google Chat) --------------------------------------
    def _poll_chat(self, account) -> None:
        first_time = f"chat:{account.id}" not in self._primed

        def work():
            from ..widgets.clients import build_account_client

            client = build_account_client(self._app, account)
            return client.list_chats()

        threading.Thread(
            target=self._run,
            args=(work, lambda r, e: self._on_chat(account, first_time, r, e)),
            daemon=True).start()

    def _on_chat(self, account, first_time, chats, error) -> bool:
        if error or not chats:
            return False
        seen = self._seen_chat.setdefault(account.id, {})
        if first_time:  # baseline only: learn current chats without notifying
            for c in chats:
                seen[c["id"]] = c.get("last_at", "")
            self._primed.add(f"chat:{account.id}")
            return False
        unread = self._chat_unread.setdefault(account.id, set())
        muted = self._muted_ids(account, "chat")
        badge_changed = False
        to_notify = []
        for c in chats:
            last = c.get("last_at", "")
            changed = bool(last) and last != seen.get(c["id"])
            seen[c["id"]] = last
            # Only notify/badge for messages from SOMEONE ELSE — your own just-sent
            # message also moves the chat's timestamp, but shouldn't ping you.
            if not (changed and not c.get("from_me")):
                continue
            if c["id"] in muted:
                continue  # silenced: no badge, no banner
            unread.add(c["id"])  # light up the red badge (always, even in DND)
            badge_changed = True
            # 1:1 chats are direct (tier 1); group/meeting chatter is ambient (2).
            tier = 1 if c.get("kind") == "oneOnOne" else 2
            if self._allowed(tier):
                to_notify.append((c, tier))
            elif tier > 1 and self._digest_active():
                self._enqueue_chat(account, c)
        if badge_changed:
            self._push_chat_badge(account.id)
        for chat, tier in to_notify[:_MAX_MAIL_PER_TICK]:
            self._notify_chat(account, chat, tier)
        return False

    def _notify_chat(self, account, chat, tier: int = 2) -> None:
        name = chat.get("name", "") or _("Chat")
        preview = (chat.get("preview", "") or "").strip() or _("New message")
        note = Gio.Notification.new(_("New message · %s") % account.display_name)
        note.set_body(f"{name}: {preview}")
        note.set_icon(self._type_icon("user-available-symbolic"))
        note.set_priority(Gio.NotificationPriority.HIGH if tier == 1
                          else Gio.NotificationPriority.NORMAL)
        payload = GLib.Variant("s", f"{account.id}\x1f{chat['id']}")
        note.set_default_action(
            Gio.Action.print_detailed_name("app.notify-open-chat", payload))
        self._app.send_notification(f"chat-{account.id}-{chat['id']}", note)

    # -- thread plumbing --------------------------------------------------
    @staticmethod
    def _run(work, on_done) -> None:
        try:
            result = work()
            GLib.idle_add(on_done, result, None)
        except Exception as exc:  # noqa: BLE001 - surfaced as an error arg
            GLib.idle_add(on_done, None, str(exc))
