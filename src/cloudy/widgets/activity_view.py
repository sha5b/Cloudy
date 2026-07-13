# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Shahab Nedaei
"""Activity — a per-account notifier feed (the app's "what happened" surface).

This is the first tab and the one selected on a fresh launch. It aggregates the
streams the account exposes into Dashboard-style grouped sections: events
happening now, invites awaiting a reply, mentions/reactions, recent mail,
upcoming events and recent chats. There is no single Graph/Google "activity feed"
API, so we synthesize it from the same list calls the rest of the app uses; each
row deep-links into the matching tab.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from gettext import gettext as _

from gi.repository import Adw, Gtk, Pango

from .format import esc, relative_time, sender_name
from .metrics import SPACE_L
from .source_nav import run_async

_ISO = "%Y-%m-%dT%H:%M:%SZ"
# kind → icon for the row prefix (when the item doesn't warrant an accent dot).
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

        self._set_status("content-loading-symbolic", _("Loading activity…"))
        self._load_async()

    def refresh_live(self) -> None:
        """Re-pull the feed in place (no loading flicker). Called by the notifier
        when its poll spots new mail/chat, so the feed updates on its own."""
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
        self.set_child(self._build_feed(items))
        return False

    def _set_status(self, icon, title, description="") -> None:
        # StatusPage parses title/description as Pango markup — escape, or a
        # raw server error ('&', '<' in JSON/HTML payloads) renders blank.
        page = Adw.StatusPage(icon_name=icon, title=esc(title))
        if description:
            page.set_description(esc(description))
        page.set_vexpand(True)
        self.set_child(page)

    # -- grouped sections (Dashboard style) ------------------------------
    def _build_feed(self, items: list[dict]) -> Gtk.Widget:
        live = [it for it in items if it.get("live")]
        sections = [
            (_("Happening now"), _("Events going on right now."), live),
            (_("Needs your reply"), _("Invitations waiting for a response."),
             [it for it in items if it["kind"] == "invite" and not it.get("live")]),
            (_("Mentions & reactions"), _("Where you were mentioned or reacted to."),
             [it for it in items if it["kind"] in ("mention", "reaction")]),
            (_("Mail"), _("Recent messages."),
             [it for it in items if it["kind"] == "mail"]),
            (_("Calendar"), _("Upcoming events."),
             [it for it in items if it["kind"] == "event" and not it.get("live")]),
            (_("Chats"), _("Recent conversations."),
             [it for it in items if it["kind"] == "chat"]),
        ]
        # Drop the empty sections; what's left is laid into up to two balanced
        # columns (like the Overview). Each column is its own vertical box, so a
        # short section keeps its natural height. (A homogeneous FlowBox — the
        # previous approach — stretched *every* child to the tallest section's
        # height, leaving a huge empty gap under each short one.)
        filled = [(t, d, rows) for t, d, rows in sections if rows]
        n_cols = min(2, len(filled)) or 1
        columns = [Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=SPACE_L,
                           valign=Gtk.Align.START, hexpand=True)
                   for _ in range(n_cols)]
        heights = [0] * n_cols
        for title, description, rows in filled:
            # Greedy balance: each section joins the currently shortest column.
            idx = heights.index(min(heights))
            group = Adw.PreferencesGroup(title=title, description=description,
                                         hexpand=True, valign=Gtk.Align.START)
            for item in rows[:30]:
                group.add(self._row(item))
            columns[idx].append(group)
            heights[idx] += min(len(rows), 30) + 2  # +2 ≈ the title/description

        grid = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=SPACE_L,
                       homogeneous=n_cols > 1, valign=Gtk.Align.START)
        grid.add_css_class("cloudy-section")
        for col in columns:
            grid.append(col)
        for side in ("top", "bottom", "start", "end"):
            getattr(grid, f"set_margin_{side}")(SPACE_L)
        return Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER,
                                  vexpand=True, child=grid)

    def _row(self, item: dict) -> Adw.ActionRow:
        row = Adw.ActionRow(title=esc(item["title"]), subtitle=esc(item["subtitle"]))
        row.set_title_lines(1)
        row.set_subtitle_lines(2)
        # An attention item (unread / needs-action / live) leads with an accent
        # dot; everything else with its kind icon.
        if item.get("attention"):
            dot = Gtk.Image.new_from_icon_name("media-record-symbolic")
            dot.add_css_class("accent")
            row.add_prefix(dot)
        else:
            row.add_prefix(Gtk.Image.new_from_icon_name(
                _KIND_ICON.get(item["kind"], "dialog-information-symbolic")))
        when = relative_time(item["when"]) if item.get("when") else ""
        if when:
            lbl = Gtk.Label(label=when, valign=Gtk.Align.CENTER)
            lbl.add_css_class("caption")
            lbl.add_css_class("dim-label")
            lbl.set_ellipsize(Pango.EllipsizeMode.END)
            row.add_suffix(lbl)
        row.add_suffix(Gtk.Image.new_from_icon_name("go-next-symbolic"))
        row.set_activatable(True)
        row.connect("activated", lambda _r, it=item: self._open(it))
        return row

    # -- activation (deep-link into the matching tab) ---------------------
    def _open(self, item: dict) -> None:
        kind, id_ = item["kind"], item["id"]
        if kind == "mail":
            self._window.open_mail(self._account, id_)
        elif kind in ("event", "invite"):
            self._window.open_calendar_event(self._account, id_)
        elif kind in _CHAT_KINDS:
            self._window.open_chat(self._account, id_)


def _is_live(start: str, end: str, all_day: bool) -> bool:
    """True when an event spans the current moment (start ≤ now ≤ end)."""
    if all_day:
        return False
    s, e = _parse(start), _parse(end)
    return s is not None and e is not None and s <= datetime.now(timezone.utc) <= e


def _parse(value: str):
    if not value or "T" not in value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            dt = datetime.fromisoformat(value.split(".", 1)[0])
        except ValueError:
            return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


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
            live = _is_live(e.get("start", ""), e.get("end", ""), e.get("all_day"))
            items.append({
                "kind": "invite" if pending else "event",
                "id": e["id"],
                "title": e.get("subject") or _("(no title)"),
                "subtitle": (_("● Live now") if live else
                             _("Invitation — needs your reply") if pending
                             else (e.get("location") or _("Event"))),
                "when": e.get("start", ""),
                "attention": pending or live,
                "live": live,
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
