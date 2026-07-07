# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Shahab Nedaei
"""Dashboard: everything at a glance across all signed-in accounts.

A two-pane surface (like Mail/Calendar): the left pane is a section switcher with
live counts (Today / Calendar / Mail / Files / Pinned); the right pane shows the
selected section. **Today** leads with at-a-glance stat cards (unread, events
today, the next event with a countdown, mounted libraries) plus short previews.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from gettext import gettext as _
from pathlib import Path

from gi.repository import Adw, Gio, GLib, Gtk, Pango

from ..modules.microsoft365.mounts import (
    MountManager,
    mount_base_for,
    mount_root,
    sync_root,
)
from .event_window import EventDetailWindow
from .file_browser_utils import recent_changes
from .format import parse_iso_utc, relative_time
from .metrics import SPACE_L, SPACE_M
from .month_grid import MonthGrid
from .source_nav import is_pinned, run_async


class DashboardView(Adw.Bin):
    __gtype_name__ = "CloudyDashboardView"

    def __init__(self, window):
        super().__init__()
        self._window = window
        self._registry = window.get_application().registry
        self._data: dict = {}
        self._section = "today"
        self._section_rows: dict = {}

        # -- left pane: section switcher with counts ---------------------
        self._list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self._list.add_css_class("navigation-sidebar")
        # Selection (not activation): single-click on a navigation-sidebar row
        # fires row-selected; the rows aren't activatable so row-activated never
        # fired and the view never switched.
        self._list.connect("row-selected", self._on_section_selected)
        sidebar_tb = Adw.ToolbarView()
        sidebar_tb.add_top_bar(Adw.HeaderBar(
            show_start_title_buttons=False, show_end_title_buttons=False,
            title_widget=Gtk.Label(label=_("Overview"))))
        sidebar_tb.set_content(Gtk.ScrolledWindow(
            hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True, child=self._list))
        sidebar_page = Adw.NavigationPage(title=_("Overview"), tag="sections")
        sidebar_page.set_child(sidebar_tb)

        # -- right pane: the selected section ----------------------------
        self._content = Adw.Bin()
        self._content_header = Adw.HeaderBar(
            show_start_title_buttons=False, show_end_title_buttons=False)
        self._content_title = Adw.WindowTitle(title=_("Today"), subtitle="")
        self._content_header.set_title_widget(self._content_title)
        content_tb = Adw.ToolbarView()
        content_tb.add_top_bar(self._content_header)
        content_tb.set_content(self._content)
        content_page = Adw.NavigationPage(title=_("Today"), tag="section")
        content_page.set_child(content_tb)

        self._split = Adw.NavigationSplitView(
            min_sidebar_width=240, max_sidebar_width=320, sidebar_width_fraction=0.28)
        self._split.set_sidebar(sidebar_page)
        self._split.set_content(content_page)
        self.set_child(self._split)

        engine = window.get_application().engine
        self._accounts = [
            a for a in self._registry.accounts()
            if a.signed_in and a.provider in ("microsoft", "google")
            and engine.is_enabled(a.module_id)
        ]
        # Work/school Microsoft accounts are the ones with Teams chats + channels;
        # only they contribute to the Activity feed.
        self._ms_accounts = [
            a for a in self._accounts
            if a.provider == "microsoft" and not a.is_personal
        ]
        self._build_sections()
        if not self._accounts:
            self._content.set_child(Adw.StatusPage(
                icon_name="dialog-information-symbolic",
                title=_("Nothing to show yet"),
                description=_("Sign in to an account to see your day here.")))
            return
        self._content.set_child(self._spinner(_("Loading your day…")))
        self._load_async()

    # -- sections sidebar -------------------------------------------------
    def _sections(self) -> list:
        secs = [
            ("today", _("Today"), "go-home-symbolic"),
            ("calendar", _("Calendar"), "x-office-calendar-symbolic"),
            ("mail", _("Mail"), "mail-unread-symbolic"),
            ("files", _("Files"), "folder-symbolic"),
        ]
        if self._ms_accounts:
            secs.append(("activity", _("Activity"), "user-available-symbolic"))
        # The Pinned section only collects mail/calendar pins; starred channels
        # and chats live in the Activity feed instead.
        if any(self._is_mailcal_pin(p)
               for a in self._accounts for p in (a.pinned_sources or [])):
            secs.append(("pinned", _("Pinned"), "starred-symbolic"))
        return secs

    @staticmethod
    def _is_mailcal_pin(pin) -> bool:
        return pin.get("kind") in ("mail", "calendar")

    def _build_sections(self) -> None:
        child = self._list.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self._list.remove(child)
            child = nxt
        self._section_rows = {}
        for key, title, icon in self._sections():
            row = Adw.ActionRow(title=title)
            row.add_prefix(Gtk.Image.new_from_icon_name(icon))
            badge = Gtk.Label()
            badge.add_css_class("dim-label")
            badge.add_css_class("numeric")
            row.add_suffix(badge)
            row._section = key  # type: ignore[attr-defined]
            row._badge = badge  # type: ignore[attr-defined]
            self._list.append(row)
            self._section_rows[key] = row
            if key == self._section:
                self._list.select_row(row)

    def _on_section_selected(self, _list, row) -> None:
        if row is None:
            return
        section = getattr(row, "_section", None)
        if section and section != self._section:
            self._section = section
            self._render_section()

    def _goto(self, section: str) -> None:
        """Jump to a section (e.g. from a clicked stat card) by selecting its
        sidebar row, which drives _on_section_selected."""
        row = self._section_rows.get(section)
        if row is not None:
            self._list.select_row(row)

    def _set_badge(self, key: str, count: int) -> None:
        row = self._section_rows.get(key)
        if row is not None:
            row._badge.set_text(str(count) if count else "")

    # -- aggregation (off the UI thread) ---------------------------------
    _CACHE_KEY = "dashboard:overview"

    def _load_async(self) -> None:
        # Cache the whole aggregate on the (persistent) app cache so flipping to
        # Overview renders instantly from cache; we only refetch when the cache
        # is stale (>TTL) or on an explicit Refresh — not on every visit.
        app = self._window.get_application()
        cached = app.cache.get(self._CACHE_KEY)
        if cached is not None:
            self._on_loaded(cached[0])
            if cached[1]:  # still fresh — don't hit the network
                return

        now = datetime.now(timezone.utc)
        start_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        # ~5 weeks ahead so the dashboard month grid is populated (the Today
        # previews still show only the soonest few).
        end_iso = (now + timedelta(days=35)).strftime("%Y-%m-%dT%H:%M:%SZ")
        accounts = list(self._accounts)

        def work():
            from .clients import build_account_client

            events, messages, pinned = [], [], []
            chats, activity = [], []
            for account in accounts:
                try:
                    client = build_account_client(app, account)
                except Exception:  # noqa: BLE001
                    continue
                # Separate try-blocks so a calendar failure (e.g. one provider's
                # scope error) doesn't also wipe this account's mail from the
                # overview, and vice versa. Log so a persistent fault is
                # diagnosable instead of the feed just silently going empty.
                try:
                    for ev in client.list_events(start_iso, end_iso):
                        events.append((account, ev))
                except Exception as exc:  # noqa: BLE001 - one bad account shouldn't blank the view
                    print(f"[dashboard] {account.id}: events failed: {exc}")
                try:
                    for msg in client.list_messages():
                        messages.append((account, msg))
                except Exception as exc:  # noqa: BLE001
                    print(f"[dashboard] {account.id}: mail failed: {exc}")
                # Pinned sources. Mail/calendar pins fold their events + unread
                # mail into the overview (and list under Pinned). Starred channels
                # contribute their latest post to the Activity feed.
                for p in account.pinned_sources or []:
                    if self._is_mailcal_pin(p):
                        detail, p_events, p_msgs = self._pin_items(
                            client, p, start_iso, end_iso)
                        events.extend((account, e) for e in p_events)
                        messages.extend((account, m) for m in p_msgs)
                        pinned.append((account, p, detail))
                    elif p.get("kind") == "channel":
                        item = self._channel_activity(client, p)
                        if item is not None:
                            activity.append((account, item))
                # Recent chats (work/school Microsoft only): one cheap call with
                # last-message previews. Starred chats float to the top.
                if account in self._ms_accounts:
                    try:
                        page, _next = client.list_chats_page(limit=15)
                        for c in page:
                            chats.append((account, c))
                    except Exception:  # noqa: BLE001 - Teams may be unavailable
                        pass
            events.sort(key=lambda pair: pair[1].get("start", ""))
            # Unread first; stable sort keeps the API's newest-first order within.
            messages.sort(key=lambda pair: pair[1].get("is_read", True))
            activity.sort(key=lambda pair: pair[1].get("when", ""), reverse=True)
            # Newest-first, then a stable pass floating starred chats to the top
            # (a stable sort keeps the newest-first order within each group).
            chats.sort(key=lambda pair: pair[1].get("last_at", ""), reverse=True)
            chats.sort(key=lambda pair: not is_pinned(
                pair[0], "chat", "teams", pair[1].get("id", "")))
            files = recent_changes(self._scan_roots(accounts))
            return {"events": events, "messages": messages, "pinned": pinned,
                    "chats": chats, "activity": activity,
                    "files": files, "mounted": self._count_mounted(accounts)}

        def done(res, _error):
            if not res or self.get_root() is None:  # navigated away mid-fetch
                return
            app.cache.set(self._CACHE_KEY, res)
            self._on_loaded(res)

        run_async(work, done)

    @staticmethod
    def _count_mounted(accounts) -> int:
        # Count via the kernel mount table (stall-proof) rather than statting
        # each child with os.path.ismount, which can block on a hung FUSE mount.
        roots = {str(mount_root())}  # backward-compat with any flat (pre-namespacing) mounts
        for account in accounts:
            roots.add(str(mount_base_for(account)))
        return sum(1 for mp in MountManager.active_mounts()
                   if os.path.dirname(mp) in roots)

    @staticmethod
    def _pin_items(client, pin, start_iso, end_iso):
        """Fetch a pinned source's content. Returns (detail, events, unread_mail)
        — events/unread are merged into the overview; detail labels the Pinned
        row. Unread-only for mail so the overview isn't flooded."""
        try:
            if pin["kind"] == "calendar":
                if pin["source"] == "teams":
                    evs = client.list_group_events(pin["id"], start_iso, end_iso)
                else:
                    evs = client.list_shared_events(pin["id"], start_iso, end_iso)
                return _("%d upcoming") % len(evs), list(evs), []
            if pin["source"] == "teams":
                msgs = client.list_messages(f"group:{pin['id']}")
            else:
                folders = client.list_shared_folders(pin["id"])
                inbox = next((f for f in folders if f["name"].lower() == "inbox"),
                             folders[0] if folders else None)
                msgs = client.list_messages(inbox["id"]) if inbox else []
            unread_mail = [m for m in msgs if not m.get("is_read", True)]
            detail = (_("%d unread") % len(unread_mail) if unread_mail
                      else _("%d recent") % len(msgs))
            return detail, [], unread_mail
        except Exception:  # noqa: BLE001
            return "", [], []

    @staticmethod
    def _channel_activity(client, pin):
        """Fetch a starred channel's latest post as an Activity item, or None.
        ``when`` is the post (or newest reply) timestamp so the feed sorts right."""
        try:
            posts, _next = client.list_channel_messages_page(
                pin.get("team_id", ""), pin["id"], limit=5)
        except Exception:  # noqa: BLE001 - channel may be inaccessible
            return None
        if not posts:
            return None
        latest = posts[-1]  # page is oldest-last, so the last entry is newest
        replies = latest.get("replies") or []
        tip = replies[-1] if replies else latest
        snippet = (latest.get("subject") or latest.get("text") or "").strip()
        return {
            "channel": pin.get("name", ""),
            "team": pin.get("team_name", ""),
            "from": tip.get("from", ""),
            "snippet": snippet,
            "replies": len(replies),
            "when": tip.get("sent", "") or latest.get("sent", ""),
        }

    def _scan_roots(self, accounts) -> list:
        # Scan each account's own mount base (not the shared mount_root, which
        # contains them all) so recent_changes gives every account a fair scan
        # share — otherwise one big account folder starves the rest and the
        # Dashboard shows only one account's files.
        roots = [sync_root()]
        for account in accounts:
            roots.append(mount_base_for(account))
        return roots

    def _on_loaded(self, data) -> bool:
        self._data = data
        self._set_badge("calendar", len(data.get("events", [])))
        self._set_badge("mail", sum(
            1 for _a, m in data.get("messages", []) if not m.get("is_read", True)))
        self._set_badge("files", len(data.get("files", [])))
        self._set_badge("pinned", len(data.get("pinned", [])))
        # Activity badge = unread chats + starred channels with a new post.
        unread_chats = sum(1 for _a, c in data.get("chats", []) if c.get("unread"))
        self._set_badge("activity", unread_chats + len(data.get("activity", [])))
        self._render_section()
        return False

    # -- section rendering ------------------------------------------------
    def _render_section(self) -> None:
        titles = {"today": _("Today"), "calendar": _("Upcoming"),
                  "mail": _("Recent mail"), "files": _("File changes"),
                  "activity": _("Activity"), "pinned": _("Pinned")}
        self._content_title.set_title(titles.get(self._section, _("Overview")))
        self._content_title.set_subtitle(datetime.now().strftime("%A, %d %B"))
        builder = {
            "today": self._build_today, "calendar": self._build_calendar,
            "mail": self._build_mail, "files": self._build_files,
            "activity": self._build_activity, "pinned": self._build_pinned,
        }.get(self._section, self._build_today)
        self._content.set_child(builder())

    def _section_page(self) -> tuple[Gtk.Widget, callable]:
        """A full-width scrolling section (vs. Adw.PreferencesPage, which clamps
        content to a narrow centred column). Returns (scroller, add_group)."""
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=SPACE_L)
        box.add_css_class("cloudy-section")
        scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER,
                                      vexpand=True, child=box)
        return scroller, box.append

    @staticmethod
    def _by_account(pairs: list) -> list:
        """Group (account, item) pairs by account, preserving first-seen order."""
        groups: dict = {}
        order: list = []
        for account, item in pairs:
            if account.id not in groups:
                groups[account.id] = (account, [])
                order.append(account.id)
            groups[account.id][1].append(item)
        return [groups[aid] for aid in order]

    def _account_group(self, account, *, subtitle: str = "") -> Adw.PreferencesGroup:
        from .format import esc
        g = Adw.PreferencesGroup(title=esc(account.display_name))
        if subtitle:
            g.set_description(esc(subtitle))
        return g

    def _build_today(self) -> Gtk.Widget:
        events = self._data.get("events", [])
        messages = self._data.get("messages", [])
        unread = sum(1 for _a, m in messages if not m.get("is_read", True))
        today = datetime.now().date().isoformat()
        events_today = sum(1 for _a, e in events
                           if (e.get("start", "") or "").startswith(today))
        mounted = self._data.get("mounted", 0)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=SPACE_L)
        box.add_css_class("cloudy-section")

        # Stat cards (clickable → jump to the matching section).
        stats = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=SPACE_M,
                        homogeneous=True)
        stats.append(self._stat(unread, _("Unread"), "mail-unread-symbolic", "mail"))
        stats.append(self._stat(events_today, _("Events today"),
                                "x-office-calendar-symbolic", "calendar"))
        if self._ms_accounts:
            unread_chats = sum(1 for _a, c in self._data.get("chats", [])
                               if c.get("unread"))
            stats.append(self._stat(unread_chats, _("New chats"),
                                    "user-available-symbolic", "activity"))
        stats.append(self._stat(len(self._data.get("files", [])), _("File changes"),
                                "document-open-recent-symbolic", "files"))
        stats.append(self._stat(mounted, _("Mounted"), "folder-remote-symbolic",
                                "files"))
        box.append(stats)

        # Next event highlight.
        nxt = self._next_event(events)
        box.append(self._next_event_card(nxt))

        # Short previews: next few events + unread mail.
        previews = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12,
                           homogeneous=True, vexpand=True)
        previews.append(self._preview_group(
            _("Up next"), [self._event_row(a, e) for a, e in events[:5]],
            _("No upcoming events.")))
        unread_msgs = [(a, m) for a, m in messages if not m.get("is_read", True)][:5]
        previews.append(self._preview_group(
            _("Unread"), [self._mail_row(a, m) for a, m in unread_msgs],
            _("Inbox zero. 🎉")))
        if self._ms_accounts:
            previews.append(self._preview_group(
                _("Activity"), self._recent_activity_rows(5),
                _("No recent chats or channel posts.")))
        box.append(previews)
        return Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER,
                                  vexpand=True, child=box)

    def _recent_activity_rows(self, limit: int) -> list:
        """The newest few items across starred channels and chats, interleaved
        by time, as ready-to-add rows for the Today preview."""
        merged = []
        for account, item in self._data.get("activity", []):
            merged.append((item.get("when", ""),
                           lambda a=account, it=item: self._channel_row(a, it)))
        for account, chat in self._data.get("chats", [])[:limit]:
            merged.append((chat.get("last_at", ""),
                           lambda a=account, c=chat: self._chat_row(a, c)))
        merged.sort(key=lambda t: t[0], reverse=True)
        return [build() for _when, build in merged[:limit]]

    def _pinned_group(self, add, *, kind: str) -> None:
        """Prepend a Pinned group (filtered to ``kind``: mail|calendar) so the
        starred shared/team sources stay in view on the relevant tab."""
        pins = [(a, p, d) for a, p, d in self._data.get("pinned", [])
                if p.get("kind") == kind]
        if not pins:
            return
        group = Adw.PreferencesGroup(title=_("Pinned"))
        for account, pin, detail in pins:
            group.add(self._pinned_row(account, pin, detail))
        add(group)

    def _build_calendar(self) -> Gtk.Widget:
        # A real month grid across all accounts; clicking an event opens it in a
        # window. Each event carries its account id so the click can route back.
        grid = MonthGrid(on_event=self._open_event)
        evs = []
        for account, ev in self._data.get("events", []):
            tagged = dict(ev)
            tagged["_account_id"] = account.id
            evs.append(tagged)
        grid.set_events(evs)
        return grid

    def _open_event(self, ev) -> None:
        account = self._registry.get(ev.get("_account_id", ""))
        if account is not None and ev.get("id"):
            # on_changed re-aggregates: the editor invalidated the caches, so
            # this refetch really hits the server instead of the stale copy.
            EventDetailWindow(self._window, account, ev["id"],
                              on_changed=self._load_async).present()

    def _build_mail(self) -> Gtk.Widget:
        scroller, add = self._section_page()
        self._pinned_group(add, kind="mail")
        messages = self._data.get("messages", [])
        if not messages:
            empty = Adw.PreferencesGroup()
            empty.add(Adw.ActionRow(title=_("No recent mail.")))
            add(empty)
            return scroller
        for account, msgs in self._by_account(messages):
            unread = sum(1 for m in msgs if not m.get("is_read", True))
            group = self._account_group(
                account, subtitle=_("%d unread") % unread if unread else "")
            for msg in msgs[:40]:
                group.add(self._mail_row(account, msg))
            add(group)
        return scroller

    def _build_files(self) -> Gtk.Widget:
        scroller, add = self._section_page()
        files = self._data.get("files", [])
        if not files:
            empty = Adw.PreferencesGroup(
                title=_("Recent changes"),
                description=_("Newest edits in mounted or synced libraries."))
            empty.add(Adw.ActionRow(
                title=_("No recent changes"),
                subtitle=_("Mount or sync a library to see activity here.")))
            add(empty)
            return scroller
        # Categorised by the library (mount/sync folder) the file lives in.
        from .format import esc
        by_lib: dict = {}
        order: list = []
        for f in files:
            lib = self._file_library(f.get("path", ""))
            if lib not in by_lib:
                by_lib[lib] = []
                order.append(lib)
            by_lib[lib].append(f)
        for lib in order:
            group = Adw.PreferencesGroup(title=esc(lib or _("Files")))
            for f in by_lib[lib]:
                group.add(self._file_row(f))
            add(group)
        return scroller

    def _file_library(self, path: str) -> str:
        """Label a file by the account folder it lives in, for grouping. Uses the
        broad mount/sync roots (not the per-account scan roots) so the first path
        component is the account name — which disambiguates same-named drives
        across accounts. Falls back to the parent directory name."""
        p = Path(path)
        for root in (mount_root(), sync_root()):
            try:
                rel = p.relative_to(root)
            except ValueError:
                continue
            if rel.parts:
                return rel.parts[0]
        return p.parent.name

    def _build_pinned(self) -> Gtk.Widget:
        scroller, add = self._section_page()
        pinned = self._data.get("pinned", [])
        if not pinned:
            empty = Adw.PreferencesGroup(
                title=_("Pinned"),
                description=_("Shared mailboxes and team calendars you starred."))
            empty.add(Adw.ActionRow(title=_("Nothing pinned yet.")))
            add(empty)
            return scroller
        for account, items in self._by_account([(a, (p, d)) for a, p, d in pinned]):
            group = self._account_group(account)
            for pin, detail in items:
                group.add(self._pinned_row(account, pin, detail))
            add(group)
        return scroller

    def _build_activity(self) -> Gtk.Widget:
        scroller, add = self._section_page()
        activity = self._data.get("activity", [])
        chats = self._data.get("chats", [])
        if not activity and not chats:
            empty = Adw.PreferencesGroup(
                title=_("Activity"),
                description=_("Recent posts in starred channels and your chats."))
            empty.add(Adw.ActionRow(
                title=_("Nothing recent"),
                subtitle=_("Star a channel (★ in Teams) to track its posts here.")))
            add(empty)
            return scroller
        if activity:
            group = Adw.PreferencesGroup(
                title=_("Team channels"),
                description=_("Latest posts in channels you starred."))
            for account, item in activity:
                group.add(self._channel_row(account, item))
            add(group)
        if chats:
            group = Adw.PreferencesGroup(
                title=_("Chats"), description=_("Your most recent conversations."))
            for account, chat in chats[:20]:
                group.add(self._chat_row(account, chat))
            add(group)
        return scroller

    def _channel_row(self, account, item) -> Adw.ActionRow:
        from .format import esc

        title = item.get("channel") or _("Channel")
        bits = [b for b in (item.get("team"), relative_time(item.get("when", "")),
                            account.display_name) if b]
        sender = item.get("from")
        snippet = item.get("snippet") or ""
        if item.get("replies"):
            snippet = _("%(n)d replies · %(text)s") % {
                "n": item["replies"], "text": snippet}
        if sender:
            snippet = f"{sender}: {snippet}" if snippet else sender
        subtitle = snippet or " · ".join(bits)
        row = Adw.ActionRow(title=esc(title), subtitle=esc(subtitle))
        row.set_title_lines(1)
        row.set_subtitle_lines(2)
        row.add_prefix(Gtk.Image.new_from_icon_name("user-available-symbolic"))
        row.add_suffix(Gtk.Image.new_from_icon_name("go-next-symbolic"))
        row.set_activatable(True)
        row.connect("activated",
                    lambda _r, a=account: self._window.open_account_tab(a, "teams"))
        return row

    def _chat_row(self, account, chat) -> Adw.ActionRow:
        from .format import esc

        when = relative_time(chat.get("last_at", ""))
        subtitle = chat.get("preview") or " · ".join(
            x for x in (when, account.display_name) if x)
        row = Adw.ActionRow(title=esc(chat.get("name") or _("Chat")),
                            subtitle=esc(subtitle))
        row.set_title_lines(1)
        row.set_subtitle_lines(1)
        if chat.get("unread"):
            dot = Gtk.Image.new_from_icon_name("media-record-symbolic")
            dot.add_css_class("accent")
            row.add_prefix(dot)
        else:
            row.add_prefix(Gtk.Image.new_from_icon_name("user-available-symbolic"))
        if is_pinned(account, "chat", "teams", chat.get("id", "")):
            row.add_suffix(Gtk.Image.new_from_icon_name("starred-symbolic"))
        row.add_suffix(Gtk.Image.new_from_icon_name("go-next-symbolic"))
        row.set_activatable(True)
        row.connect(
            "activated",
            lambda _r, a=account, cid=chat.get("id"): self._window.open_chat(a, cid))
        return row

    # -- widgets ----------------------------------------------------------
    def _stat(self, number, caption, icon, section: str | None = None) -> Gtk.Widget:
        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6,
                        margin_top=18, margin_bottom=18, margin_start=12,
                        margin_end=12, halign=Gtk.Align.CENTER)
        img = Gtk.Image.new_from_icon_name(icon)
        img.set_pixel_size(24)
        img.add_css_class("dim-label")
        inner.append(img)
        num = Gtk.Label(label=str(number))
        num.add_css_class("title-1")
        inner.append(num)
        cap = Gtk.Label(label=caption)
        cap.add_css_class("caption")
        cap.add_css_class("dim-label")
        inner.append(cap)
        if section is None:
            inner.add_css_class("card")
            return inner
        # Clickable: a flat card button that jumps to the matching section.
        btn = Gtk.Button(child=inner)
        btn.add_css_class("card")
        btn.add_css_class("flat")
        btn.set_tooltip_text(_("Open %s") % caption)
        btn.connect("clicked", lambda *_: self._goto(section))
        return btn

    def _next_event_card(self, pair) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=14,
                      margin_top=16, margin_bottom=16, margin_start=16, margin_end=16)
        icon = Gtk.Image.new_from_icon_name("alarm-symbolic")
        icon.set_pixel_size(28)
        icon.set_valign(Gtk.Align.CENTER)
        box.append(icon)
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3, hexpand=True,
                       valign=Gtk.Align.CENTER)
        box.append(text)
        if pair is None:
            head = Gtk.Label(label=_("Nothing left today"), xalign=0)
            head.add_css_class("heading")
            text.append(head)
            sub = Gtk.Label(label=_("Enjoy the quiet."), xalign=0)
            sub.add_css_class("dim-label")
            text.append(sub)
            box.add_css_class("card")
            return box
        account, ev = pair
        head = Gtk.Label(label=ev.get("subject") or _("(no title)"), xalign=0,
                         ellipsize=Pango.EllipsizeMode.END)
        head.add_css_class("heading")
        text.append(head)
        when = _rel_time(ev.get("start", ""))
        loc = ev.get("location")
        sub = " · ".join(x for x in (when, loc, account.display_name) if x)
        sublbl = Gtk.Label(label=sub, xalign=0, ellipsize=Pango.EllipsizeMode.END)
        sublbl.add_css_class("dim-label")
        text.append(sublbl)
        chevron = Gtk.Image.new_from_icon_name("go-next-symbolic")
        chevron.add_css_class("dim-label")
        chevron.set_valign(Gtk.Align.CENTER)
        box.append(chevron)
        # Clickable: open the account's calendar on the upcoming event.
        btn = Gtk.Button(child=box)
        btn.add_css_class("card")
        btn.add_css_class("flat")
        btn.connect("clicked",
                    lambda *_: self._window.open_account_tab(account, "calendar"))
        return btn

    def _preview_group(self, title, rows, empty) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        head = Gtk.Label(label=title, xalign=0)
        head.add_css_class("heading")
        box.append(head)
        listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        listbox.add_css_class("boxed-list")
        if rows:
            for r in rows:
                listbox.append(r)
        else:
            listbox.append(Adw.ActionRow(title=empty))
        box.append(listbox)
        return box

    @staticmethod
    def _next_event(events):
        now = datetime.now(timezone.utc)
        for account, ev in events:
            dt = parse_iso_utc(ev.get("start", ""))
            if dt is not None and dt >= now:
                return (account, ev)
        return None

    def _spinner(self, text) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                      halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER,
                      hexpand=True, vexpand=True)
        sp = Gtk.Spinner(width_request=32, height_request=32)
        sp.start()
        box.append(sp)
        lbl = Gtk.Label(label=text)
        lbl.add_css_class("dim-label")
        box.append(lbl)
        return box

    # -- rows -------------------------------------------------------------
    def _pinned_row(self, account, pin, detail) -> Adw.ActionRow:
        from .format import esc

        is_cal = pin.get("kind") == "calendar"
        icon = "x-office-calendar-symbolic" if is_cal else "mail-unread-symbolic"
        kind = _("Team") if pin.get("source") == "teams" else _("Shared")
        subtitle = f"{kind} · {account.display_name}"
        if detail:
            subtitle = f"{detail} · {subtitle}"
        row = Adw.ActionRow(title=esc(pin.get("name", "")), subtitle=esc(subtitle))
        row.add_prefix(Gtk.Image.new_from_icon_name(icon))
        row.set_activatable(True)
        row.add_suffix(Gtk.Image.new_from_icon_name("go-next-symbolic"))
        tab = "calendar" if is_cal else "mail"
        row.connect("activated", lambda _r, a=account: self._window.open_account_tab(a, tab))
        return row

    def _file_row(self, f) -> Adw.ActionRow:
        from .format import esc

        row = Adw.ActionRow(title=esc(f.get("name", "")), subtitle=esc(_ago(f.get("mtime", 0))))
        row.add_prefix(Gtk.Image.new_from_icon_name("text-x-generic-symbolic"))
        row.set_activatable(True)
        row.add_suffix(Gtk.Image.new_from_icon_name("go-next-symbolic"))
        row.connect("activated", lambda _r, p=f.get("path", ""): self._open_path(p))
        return row

    def _open_path(self, path: str) -> None:
        if not path:
            return
        uri = "file://" + GLib.Uri.escape_string(path, "/", False)
        try:
            Gio.AppInfo.launch_default_for_uri(uri, None)
        except Exception as exc:  # noqa: BLE001
            self._window.add_toast(_("Couldn't open: %s") % exc)

    def _event_row(self, account, ev) -> Adw.ActionRow:
        from .format import esc

        when = _fmt(ev.get("start", ""), ev.get("all_day"))
        subtitle = f"{when} · {account.display_name}" if when else account.display_name
        row = Adw.ActionRow(title=esc(ev.get("subject") or _("(no title)")),
                            subtitle=esc(subtitle))
        row.set_title_lines(1)
        row.add_prefix(Gtk.Image.new_from_icon_name("x-office-calendar-symbolic"))
        if ev.get("location"):
            lbl = Gtk.Label(label=ev["location"], css_classes=["dim-label"])
            lbl.set_ellipsize(Pango.EllipsizeMode.END)
            row.add_suffix(lbl)
        row.set_activatable(True)
        row.connect("activated",
                    lambda _r, a=account: self._window.open_account_tab(a, "calendar"))
        return row

    def _mail_row(self, account, msg) -> Adw.ActionRow:
        from .format import esc, sender_name

        subtitle = f"{sender_name(msg.get('from', ''))} · {account.display_name}"
        row = Adw.ActionRow(title=esc(msg.get("subject") or _("(no subject)")),
                            subtitle=esc(subtitle))
        row.set_title_lines(1)
        row.set_subtitle_lines(1)
        if not msg.get("is_read", True):
            dot = Gtk.Image.new_from_icon_name("media-record-symbolic")
            dot.add_css_class("accent")
            row.add_prefix(dot)
        else:
            row.add_prefix(Gtk.Image.new_from_icon_name("mail-read-symbolic"))
        row.set_activatable(True)
        row.add_suffix(Gtk.Image.new_from_icon_name("go-next-symbolic"))
        row.connect(
            "activated",
            lambda _r, a=account, mid=msg.get("id"): self._window.open_mail(a, mid))
        return row


def _fmt(start: str, all_day: bool) -> str:
    if not start or "T" not in start:
        return start
    date, _sep, rest = start.partition("T")
    return date if all_day else f"{date} {rest[:5]}"


def _rel_time(start: str) -> str:
    dt = parse_iso_utc(start)
    if dt is None:
        return ""
    delta = (dt - datetime.now(timezone.utc)).total_seconds()
    if delta < 0:
        return _("now")
    if delta < 3600:
        return _("in %d min") % max(1, int(delta // 60))
    if delta < 86400:
        h = int(delta // 3600)
        m = int((delta % 3600) // 60)
        return _("in %dh %02dm") % (h, m) if m else _("in %dh") % h
    return _("in %d days") % int(delta // 86400)


def _ago(mtime: float) -> str:
    if not mtime:
        return ""
    delta = datetime.now(timezone.utc).timestamp() - mtime
    if delta < 60:
        return _("just now")
    if delta < 3600:
        return _("%d min ago") % (delta // 60)
    if delta < 86400:
        return _("%d h ago") % (delta // 3600)
    return _("%d d ago") % (delta // 86400)
