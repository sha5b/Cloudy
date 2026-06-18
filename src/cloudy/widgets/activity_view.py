# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Shahab Nedaei
"""Activity — a per-account notifier feed (the app's "what happened" surface).

This is the first tab and the one selected on a fresh launch. It aggregates the
streams the account exposes into one time-sorted list: recent mail, upcoming and
unanswered calendar invites, and recent chat/Teams conversations (mentions and
reactions where the provider surfaces them). There is no single Graph/Google
"activity feed" API, so we synthesize the feed from the same list calls the rest
of the app already uses; each row deep-links into the matching tab.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from gettext import gettext as _

from gi.repository import Adw, Gtk, Pango

from .format import sender_name, short_time
from .source_nav import run_async

_ISO = "%Y-%m-%dT%H:%M:%SZ"
# kind → (icon, css accent for the leading dot when the item wants attention)
_KIND_ICON = {
    "mail": "mail-unread-symbolic",
    "event": "x-office-calendar-symbolic",
    "invite": "x-office-calendar-symbolic",
    "chat": "user-available-symbolic",
    "reaction": "emote-love-symbolic",
    "mention": "user-available-symbolic",
}
# Activity kinds that deep-link into the Chat tab.
_CHAT_KINDS = {"chat", "reaction", "mention"}


class ActivityView(Adw.Bin):
    __gtype_name__ = "CloudyActivityView"

    def __init__(self, window, account):
        super().__init__()
        self._window = window
        self._account = account

        self._list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self._list.add_css_class("boxed-list")
        self._list.connect("row-activated", self._on_row_activated)

        clamp = Adw.Clamp(maximum_size=720, margin_top=16, margin_bottom=16,
                          margin_start=12, margin_end=12)
        clamp.set_child(self._list)
        scroller = Gtk.ScrolledWindow(hexpand=True, vexpand=True)
        scroller.set_child(clamp)
        self._scroller = scroller
        self.set_child(scroller)

        self._set_status("content-loading-symbolic", _("Loading activity…"))
        self._load_async()

    # -- loading ----------------------------------------------------------
    def _load_async(self) -> None:
        account = self._account

        def work():
            from .clients import build_account_client

            client = build_account_client(self._window.get_application(), account)
            return _collect_feed(client)

        run_async(work, self._on_loaded)

    def _on_loaded(self, items, error) -> bool:
        if error:
            self._set_status("dialog-error-symbolic",
                             _("Couldn't load activity"), str(error))
            return False
        if not items:
            self._set_status("emblem-ok-symbolic", _("You're all caught up"),
                             _("New mail, invites and messages will show up here."))
            return False
        self.set_child(self._scroller)
        child = self._list.get_first_child()
        while child is not None:
            self._list.remove(child)
            child = self._list.get_first_child()
        for item in items:
            self._list.append(_activity_row(item))
        return False

    def _set_status(self, icon, title, description="") -> None:
        page = Adw.StatusPage(icon_name=icon, title=title)
        if description:
            page.set_description(description)
        page.set_vexpand(True)
        self.set_child(page)

    # -- activation (deep-link into the matching tab) ---------------------
    def _on_row_activated(self, _list, row) -> None:
        item = getattr(row, "_item", None)
        if not item:
            return
        kind, id_ = item["kind"], item["id"]
        if kind == "mail":
            self._window.open_mail(self._account, id_)
        elif kind in ("event", "invite"):
            self._window.open_calendar_event(self._account, id_)
        elif kind in _CHAT_KINDS:
            self._window.open_chat(self._account, id_)


def _collect_feed(client) -> list[dict]:
    """Pull each available stream (mail / calendar / chat) and merge them into one
    time-sorted feed. Each stream is guarded so an unavailable one (e.g. Chat on a
    consumer account) just contributes nothing instead of sinking the whole feed."""
    items: list[dict] = []

    try:
        for m in client.list_messages(limit=12):
            items.append({
                "kind": "mail",
                "id": m["id"],
                "title": sender_name(m.get("from", "")) or _("(unknown sender)"),
                "subtitle": m.get("subject") or _("(no subject)"),
                "when": m.get("received", ""),
                "attention": not m.get("is_read", True),
            })
    except Exception:  # noqa: BLE001
        pass

    try:
        now = datetime.now(timezone.utc)
        start = (now - timedelta(days=2)).strftime(_ISO)
        end = (now + timedelta(days=14)).strftime(_ISO)
        for e in client.list_events(start, end, limit=30):
            resp = (e.get("response") or "").lower()
            pending = resp in ("notresponded", "needsaction")
            items.append({
                "kind": "invite" if pending else "event",
                "id": e["id"],
                "title": e.get("subject") or _("(no title)"),
                "subtitle": (_("Invitation — needs your reply") if pending
                             else (e.get("location") or _("Event"))),
                "when": e.get("start", ""),
                "attention": pending,
            })
    except Exception:  # noqa: BLE001
        pass

    try:
        for c in client.list_chats(limit=20):
            preview = c.get("preview") or _("(no preview)")
            items.append({
                "kind": "chat",
                "id": c["id"],
                "title": c.get("name") or _("Conversation"),
                "subtitle": preview,
                "when": c.get("last_at", ""),
                "attention": bool(c.get("unread")),
            })
    except Exception:  # noqa: BLE001
        pass

    # Teams-style targeted activity: reactions on your messages and @mentions of
    # you. Microsoft only — the method is absent on the Google client (no Chat
    # API on consumer Gmail), so AttributeError just skips it.
    try:
        for a in client.recent_chat_activity():
            who = a.get("who") or _("Someone")
            text = a.get("text") or ""
            if a["kind"] == "reaction":
                emoji = a.get("emoji") or "❤"
                title = _("%s reacted %s to your message") % (who, emoji)
            else:
                title = _("%s mentioned you") % who
            items.append({
                "kind": a["kind"],
                "id": a["chat_id"],
                "title": title,
                "subtitle": text or _("in chat"),
                "when": a.get("when", ""),
                "attention": True,
            })
    except Exception:  # noqa: BLE001
        pass

    # Newest first; items without a timestamp sink to the bottom.
    items.sort(key=lambda it: it.get("when") or "", reverse=True)
    return items[:60]


def _activity_row(item: dict) -> Gtk.ListBoxRow:
    row = Gtk.ListBoxRow(activatable=True)
    row._item = item  # type: ignore[attr-defined]
    box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12,
                  margin_top=8, margin_bottom=8, margin_start=12, margin_end=12)
    row.set_child(box)

    icon = Gtk.Image.new_from_icon_name(_KIND_ICON.get(item["kind"], "dialog-information-symbolic"))
    icon.set_valign(Gtk.Align.START)
    if item.get("attention"):
        icon.add_css_class("accent")
    box.append(icon)

    text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True)
    box.append(text)
    title = Gtk.Label(label=item["title"], xalign=0, ellipsize=Pango.EllipsizeMode.END)
    title.add_css_class("body")
    if item.get("attention"):
        title.add_css_class("heading")  # bold unread/needs-action items
    text.append(title)
    sub = Gtk.Label(label=item["subtitle"], xalign=0, ellipsize=Pango.EllipsizeMode.END)
    sub.add_css_class("caption")
    sub.add_css_class("dim-label")
    text.append(sub)

    if item.get("when"):
        when = Gtk.Label(label=short_time(item["when"]), xalign=1, valign=Gtk.Align.START)
        when.add_css_class("caption")
        when.add_css_class("dim-label")
        box.append(when)
    return row
