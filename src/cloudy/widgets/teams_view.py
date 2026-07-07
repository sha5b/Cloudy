# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Shahab Nedaei
"""Teams surface: the hierarchical Team → channel → channel-content view.

Left pane = the Teams the user belongs to, each expanding to its channels.
Right pane = the selected channel, with the same tab strip Teams shows:

  * Conversation — the channel's posts (root post + threaded replies), with a
    composer to start a post and an inline reply box under each post.
  * Notes        — the team's (group) OneNote notebook: sections → pages, read
    as HTML and created/edited with the shared rich-text editor.

Microsoft work/school accounts only. Channel reads need ``ChannelMessage.Read.All``
(tenant-admin consent); a missing scope surfaces the same "Re-sign in" prompt the
Mail/Chat views use. Google has no channel/notes equivalent, so the Teams tab is
never offered for Google accounts (see core.interfaces.capabilities_of)."""

from __future__ import annotations

import html
import re
from gettext import gettext as _

from gi.repository import Adw, Gdk, GLib, Gtk, Pango

from ..modules.microsoft365.graph_markup import html_to_pango, strip_html
from .format import esc, relative_time
from .imaging import shrink_image_bytes, texture_from_png_bytes
from .source_nav import (
    SCOPE_HINT,
    action_row,
    attachment_chip,
    clear_listbox,
    friendly_error,
    is_muted,
    is_pinned,
    is_scope_error,
    loading_box,
    run_async,
    status_page,
    toggle_mute,
    toggle_pin,
)


class TeamsView(Adw.Bin):
    __gtype_name__ = "CloudyTeamsView"

    def __init__(self, window, account):
        super().__init__()
        self._window = window
        self._account = account

        # -- selection / data state --------------------------------------
        self._team_id = ""
        self._team_name = ""
        self._channel_id = ""
        self._channel_name = ""
        self._team_rows: dict[str, Adw.ExpanderRow] = {}
        self._loaded_channels: set[str] = set()
        # Notes (team group notebook)
        self._notebook_id = ""
        self._section_id = ""
        self._page_id = ""
        self._notes_loaded_for = ""  # team id whose notebook is currently shown
        # Live conversation poll: refetch the open channel's posts on a timer
        # (only while the view is on screen) so new posts/replies appear without
        # a manual refresh. _conv_sig fingerprints the rendered posts so a poll
        # re-renders only on a real change (no scroll jump / lost reply text).
        self._conv_poll_id = None
        self._conv_sig = None

        self.set_child(self._build_layout())
        self._load_teams()
        self.connect("map", self._on_map)
        self.connect("unmap", self._on_unmap)

    # ------------------------------------------------------------------ #
    # Layout
    # ------------------------------------------------------------------ #
    def _build_layout(self) -> Gtk.Widget:
        # -- sidebar: Teams, each expanding to its channels --------------
        self._teams_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self._teams_list.add_css_class("boxed-list")
        self._teams_list.set_margin_top(8)
        self._teams_list.set_margin_bottom(8)
        self._teams_list.set_margin_start(8)
        self._teams_list.set_margin_end(8)
        teams_scroll = Gtk.ScrolledWindow(
            vexpand=True, hexpand=True, child=self._teams_list)

        refresh = Gtk.Button(icon_name="view-refresh-symbolic",
                             tooltip_text=_("Refresh"))
        refresh.add_css_class("flat")
        refresh.connect("clicked", lambda *_a: self._reload())
        side_header = Adw.HeaderBar(
            show_start_title_buttons=False, show_end_title_buttons=False,
            title_widget=Gtk.Label(label=_("Teams")))
        side_header.pack_end(refresh)
        side_tb = Adw.ToolbarView()
        side_tb.add_top_bar(side_header)
        side_tb.set_content(teams_scroll)
        sidebar_page = Adw.NavigationPage(title=_("Teams"), child=side_tb)

        # -- content: the selected channel, with Conversation / Notes ----
        self._inner_stack = Adw.ViewStack()
        self._inner_stack.add_titled(
            self._build_conversation_pane(), "conversation", _("Conversation")
        ).set_icon_name("user-available-symbolic")
        self._inner_stack.add_titled(
            self._build_notes_pane(), "notes", _("Notes")
        ).set_icon_name("accessories-text-editor-symbolic")
        self._inner_stack.connect("notify::visible-child-name",
                                  self._on_inner_tab_changed)

        switcher = Adw.ViewSwitcher(policy=Adw.ViewSwitcherPolicy.WIDE)
        switcher.set_stack(self._inner_stack)
        content_header = Adw.HeaderBar(
            show_start_title_buttons=False, show_end_title_buttons=False,
            title_widget=switcher)
        # Star the open channel → it surfaces on the Dashboard Activity feed.
        self._star_btn = Gtk.Button(
            icon_name="non-starred-symbolic", visible=False,
            tooltip_text=_("Star this channel for the Dashboard"))
        self._star_btn.add_css_class("flat")
        self._star_btn.connect("clicked", self._on_star_clicked)
        content_header.pack_end(self._star_btn)
        # Mute the open channel → no notification banner or badge for it.
        self._mute_btn = Gtk.Button(
            icon_name="preferences-system-notifications-symbolic", visible=False,
            tooltip_text=_("Mute notifications for this channel"))
        self._mute_btn.add_css_class("flat")
        self._mute_btn.connect("clicked", self._on_mute_clicked)
        content_header.pack_end(self._mute_btn)
        content_tb = Adw.ToolbarView()
        content_tb.add_top_bar(content_header)
        content_tb.set_content(self._inner_stack)
        content_page = Adw.NavigationPage(title=_("Channel"), child=content_tb)

        self._split = Adw.NavigationSplitView(
            min_sidebar_width=280, max_sidebar_width=420,
            sidebar_width_fraction=0.34)
        self._split.set_sidebar(sidebar_page)
        self._split.set_content(content_page)
        return self._split

    def _build_conversation_pane(self) -> Gtk.Widget:
        self._conv_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10,
                                margin_top=12, margin_bottom=12,
                                margin_start=12, margin_end=12)
        self._conv_scroll = Gtk.ScrolledWindow(vexpand=True, hexpand=True,
                                               child=self._conv_box)

        self._post_entry = Gtk.Entry(
            hexpand=True, placeholder_text=_("Start a post in this channel…"))
        self._post_entry.connect("activate", lambda *_a: self._send_post())
        post_btn = Gtk.Button(icon_name="document-send-symbolic",
                             tooltip_text=_("Post"))
        post_btn.add_css_class("suggested-action")
        post_btn.connect("clicked", lambda *_a: self._send_post())
        self._composer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6,
                                margin_top=6, margin_bottom=10,
                                margin_start=12, margin_end=12)
        self._composer.append(self._post_entry)
        self._composer.append(post_btn)
        self._composer.set_visible(False)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.append(self._conv_scroll)
        box.append(self._composer)
        return box

    def _build_notes_pane(self) -> Gtk.Widget:
        # Section + Page pickers in one top bar, content full-width below — a
        # flat layout (no nested split) so nothing gets clipped by the outer
        # Teams sidebar.
        self._section_dd = Gtk.DropDown.new_from_strings([])
        self._section_dd.set_hexpand(True)
        self._section_dd.connect("notify::selected", self._on_section_selected)
        self._page_dd = Gtk.DropDown.new_from_strings([])
        self._page_dd.set_hexpand(True)
        self._page_dd.connect("notify::selected", self._on_page_selected)
        self._suppress_page_dd = False
        new_page_btn = Gtk.Button(icon_name="document-new-symbolic",
                                  tooltip_text=_("New page"))
        new_page_btn.add_css_class("flat")
        new_page_btn.connect("clicked", lambda *_a: self._new_page())
        self._notes_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6,
                                 margin_top=8, margin_bottom=8,
                                 margin_start=12, margin_end=12)
        self._notes_bar.append(Gtk.Label(label=_("Section")))
        self._notes_bar.append(self._section_dd)
        self._notes_bar.append(Gtk.Label(label=_("Page")))
        self._notes_bar.append(self._page_dd)
        self._notes_bar.append(new_page_btn)

        self._page_content = Adw.Bin(hexpand=True, vexpand=True)
        self._page_content.set_child(
            status_page("accessories-text-editor-symbolic", _("Notes"),
                        _("Select a page to read it.")))

        self._notes_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self._notes_box.append(self._notes_bar)
        self._notes_box.append(Gtk.Separator())
        self._notes_box.append(self._page_content)
        # Until a channel is picked, the Notes tab shows a hint.
        self._notes_placeholder = status_page(
            "system-users-symbolic", _("No channel selected"),
            _("Pick a team and channel to see its notes."))
        self._notes_bin = Adw.Bin(child=self._notes_placeholder)
        return self._notes_bin

    # ------------------------------------------------------------------ #
    # Shared helpers
    # ------------------------------------------------------------------ #
    @property
    def _cache(self):
        return self._window.get_application().cache

    def _client(self):
        from .clients import build_account_client

        return build_account_client(self._window.get_application(), self._account)

    def _reload(self) -> None:
        self._cache.invalidate(prefix=self._account.id)
        self._loaded_channels.clear()
        self._notes_loaded_for = ""
        self._load_teams()
        if self._channel_id:
            self._load_conversation()

    def _unavailable_text(self, error: str) -> str:
        return _("Teams isn't available on this account. %s") % error

    # ------------------------------------------------------------------ #
    # Teams list (sidebar)
    # ------------------------------------------------------------------ #
    def _load_teams(self) -> None:
        cached = self._cache.get(f"{self._account.id}:teams")
        if cached is not None:
            self._render_teams(cached[0])
            if cached[1]:
                return  # fresh — no revalidate
        else:
            clear_listbox(self._teams_list)
            self._teams_list.append(self._loading_row(_("Loading teams…")))

        def work():
            return self._client().list_joined_teams()

        run_async(work, self._on_teams)

    def _on_teams(self, result, error) -> bool:
        if error is not None:
            clear_listbox(self._teams_list)
            if is_scope_error(error):
                self._teams_list.append(action_row(
                    SCOPE_HINT, _("Re-sign in"),
                    lambda: self._window.sign_in_account(self._account)))
            else:
                self._teams_list.append(self._message_row(
                    self._unavailable_text(error)))
            return False
        self._cache.set(f"{self._account.id}:teams", result)
        self._render_teams(result)
        return False

    def _render_teams(self, teams) -> None:
        clear_listbox(self._teams_list)
        self._team_rows.clear()
        if not teams:
            self._teams_list.append(self._message_row(_("You're not a member of any team.")))
            return
        for team in teams:
            row = Adw.ExpanderRow(title=esc(team["name"]))
            row.set_icon_name("system-users-symbolic")
            self._team_rows[team["id"]] = row
            # Lazy-load channels the first time the team is expanded.
            row.connect("notify::expanded",
                        lambda r, _p, t=team: self._on_team_expanded(t, r))
            self._teams_list.append(row)

    def _on_team_expanded(self, team, row) -> None:
        if not row.get_expanded() or team["id"] in self._loaded_channels:
            return
        self._loaded_channels.add(team["id"])
        spinner = Adw.ActionRow(title=_("Loading channels…"))
        row.add_row(spinner)

        cached = self._cache.get(f"{self._account.id}:channels:{team['id']}")
        if cached is not None:
            row.remove(spinner)
            self._render_channels(team, row, cached[0])
            if cached[1]:
                return

        def work():
            return self._client().list_team_channels(team["id"])

        def done(result, error):
            try:
                row.remove(spinner)
            except Exception:  # noqa: BLE001 - already removed by cache path
                pass
            if error is not None:
                msg = (SCOPE_HINT if is_scope_error(error)
                       else self._unavailable_text(error))
                err_row = Adw.ActionRow(title=esc(msg))
                err_row.add_css_class("dim-label")
                row.add_row(err_row)
                # cached path may have rendered already; avoid duplicate channels
                self._loaded_channels.discard(team["id"])
                return False
            self._cache.set(f"{self._account.id}:channels:{team['id']}", result)
            self._render_channels(team, row, result)
            return False

        run_async(work, done)

    def _render_channels(self, team, row, channels) -> None:
        # Clear any previously-added child rows (revalidate path); ExpanderRow
        # exposes no list, so we track the rows we added ourselves.
        for existing in list(getattr(row, "_cloudy_channel_rows", [])):
            try:
                row.remove(existing)
            except Exception:  # noqa: BLE001
                pass
        added = []
        if not channels:
            empty = Adw.ActionRow(title=_("No channels"))
            empty.add_css_class("dim-label")
            row.add_row(empty)
            added.append(empty)
        for chan in channels:
            crow = Adw.ActionRow(title=esc(chan["name"]), activatable=True)
            crow.add_prefix(Gtk.Image.new_from_icon_name(
                "user-available-symbolic"))
            if chan.get("description"):
                crow.set_subtitle(esc(chan["description"]))
            crow.connect("activated",
                         lambda _r, t=team, c=chan: self._open_channel(t, c))
            row.add_row(crow)
            added.append(crow)
        row._cloudy_channel_rows = added

    # ------------------------------------------------------------------ #
    # Open a channel
    # ------------------------------------------------------------------ #
    def _open_channel(self, team, channel) -> None:
        self._team_id = team["id"]
        self._team_name = team["name"]
        self._channel_id = channel["id"]
        self._channel_name = channel["name"]
        self._split.set_show_content(True)
        self._composer.set_visible(True)
        self._update_star()
        self._load_conversation()
        # Notes are team-scoped; reload the notebook when the team changes.
        if self._notes_loaded_for != team["id"]:
            self._notes_loaded_for = ""
            self._notebook_id = ""
            self._section_id = ""
            self._page_id = ""
        if self._inner_stack.get_visible_child_name() == "notes":
            self._ensure_notes_loaded()

    def _update_star(self) -> None:
        active = bool(self._channel_id)
        self._star_btn.set_visible(active)
        self._mute_btn.set_visible(active)
        if not active:
            return
        pinned = is_pinned(self._account, "channel", "teams", self._channel_id)
        self._star_btn.set_icon_name(
            "starred-symbolic" if pinned else "non-starred-symbolic")
        muted = is_muted(self._account, "channel", self._channel_id)
        self._mute_btn.set_icon_name(
            "notifications-disabled-symbolic" if muted
            else "preferences-system-notifications-symbolic")
        self._mute_btn.set_tooltip_text(
            _("Unmute this channel") if muted
            else _("Mute notifications for this channel"))

    def _on_mute_clicked(self, _btn) -> None:
        if not self._channel_id:
            return
        muted = toggle_mute(self._window, self._account, kind="channel",
                            sid=self._channel_id)
        self._update_star()
        self._window.add_toast(
            _("Channel muted") if muted else _("Channel unmuted"))

    def _on_star_clicked(self, _btn) -> None:
        if not self._channel_id:
            return
        pinned = toggle_pin(
            self._window, self._account, kind="channel", source="teams",
            sid=self._channel_id, name=self._channel_name,
            team_id=self._team_id, team_name=self._team_name)
        self._star_btn.set_icon_name(
            "starred-symbolic" if pinned else "non-starred-symbolic")
        self._window.add_toast(
            _("Channel starred") if pinned else _("Channel unstarred"))

    def _on_inner_tab_changed(self, *_a) -> None:
        if (self._inner_stack.get_visible_child_name() == "notes"
                and self._channel_id):
            self._ensure_notes_loaded()

    # ------------------------------------------------------------------ #
    # Live conversation poll (only while the view is on screen)
    # ------------------------------------------------------------------ #
    _CONV_POLL_SECONDS = 25

    def _on_map(self, *_a) -> None:
        if self._conv_poll_id is None:
            self._conv_poll_id = GLib.timeout_add_seconds(
                self._CONV_POLL_SECONDS, self._conv_poll)

    def _on_unmap(self, *_a) -> None:
        if self._conv_poll_id is not None:
            GLib.source_remove(self._conv_poll_id)
            self._conv_poll_id = None

    def _conv_poll(self) -> bool:
        # Only poll the channel the user is actually looking at.
        if self._channel_id and \
                self._inner_stack.get_visible_child_name() == "conversation":
            team_id, channel_id = self._team_id, self._channel_id

            def work():
                return self._client().list_channel_messages(team_id, channel_id)

            run_async(work,
                      lambda res, err: self._on_conv_poll(channel_id, res, err))
        return True  # keep the timer alive (removed on unmap)

    def _on_conv_poll(self, channel_id, result, error) -> bool:
        if error or result is None or channel_id != self._channel_id:
            return False
        self._cache.set(self._conv_key(), result)
        if self._posts_signature(result) != self._conv_sig:
            self._render_posts(result)  # only re-render on a real change
        return False

    @staticmethod
    def _posts_signature(posts) -> tuple:
        """A fingerprint of the posts (ids, timestamps, reply ids, reaction
        totals) — changes exactly when something the reader would notice does."""
        sig: list = []
        for p in posts or []:
            sig.append((p.get("id", ""), p.get("sent", ""),
                        sum(r.get("count", 0) for r in (p.get("reactions") or []))))
            for r in p.get("replies") or []:
                sig.append((r.get("id", ""), r.get("sent", "")))
        return tuple(sig)

    # ------------------------------------------------------------------ #
    # Conversation (channel posts)
    # ------------------------------------------------------------------ #
    def _conv_key(self) -> str:
        return f"{self._account.id}:channelmsgs:{self._channel_id}"

    def _load_conversation(self) -> None:
        cached = self._cache.get(self._conv_key())
        if cached is not None:
            self._render_posts(cached[0])
            if cached[1]:
                return
        else:
            self._clear_box(self._conv_box)
            self._conv_box.append(loading_box(_("Loading posts…")))

        team_id, channel_id = self._team_id, self._channel_id

        def work():
            return self._client().list_channel_messages(team_id, channel_id)

        run_async(work,
                  lambda res, err: self._on_conversation(channel_id, res, err))

    def _on_conversation(self, channel_id, result, error) -> bool:
        if channel_id != self._channel_id:
            return False  # the user moved on to another channel
        if error is not None:
            self._clear_box(self._conv_box)
            if is_scope_error(error):
                self._conv_box.append(action_row(
                    SCOPE_HINT, _("Re-sign in"),
                    lambda: self._window.sign_in_account(self._account)))
            else:
                self._conv_box.append(status_page(
                    "dialog-warning-symbolic", _("Couldn't load posts"),
                    esc(error)))
            return False
        self._cache.set(self._conv_key(), result)
        self._render_posts(result)
        return False

    def _render_posts(self, posts) -> None:
        self._conv_sig = self._posts_signature(posts)
        self._clear_box(self._conv_box)
        title = Gtk.Label(label=esc(self._channel_name), xalign=0)
        title.add_css_class("title-2")
        title.set_use_markup(True)
        self._conv_box.append(title)
        if not posts:
            self._conv_box.append(status_page(
                "user-available-symbolic", _("No posts yet"),
                _("Be the first to post in %s.") % esc(self._channel_name)))
            GLib.idle_add(self._scroll_conv_to_bottom)
            return
        for post in posts:
            self._conv_box.append(self._post_card(post))
        GLib.idle_add(self._scroll_conv_to_bottom)

    def _post_card(self, post) -> Gtk.Widget:
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        card.add_css_class("card")
        card.set_margin_start(2)
        card.set_margin_end(2)
        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6,
                       margin_top=10, margin_bottom=10,
                       margin_start=12, margin_end=12)
        if post.get("subject"):
            subj = Gtk.Label(label=esc(post["subject"]), xalign=0, wrap=True)
            subj.add_css_class("heading")
            inner.append(subj)
        inner.append(self._message_block(post, heading=True))
        # Replies, indented under the root post.
        replies = post.get("replies") or []
        if replies:
            rbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4,
                          margin_start=16, margin_top=4)
            rbox.add_css_class("cloudy-replies")
            for reply in replies:
                rbox.append(self._message_block(reply, heading=True))
            inner.append(rbox)
        # Inline reply composer.
        reply_entry = Gtk.Entry(hexpand=True, placeholder_text=_("Reply…"))
        send = Gtk.Button(icon_name="mail-reply-sender-symbolic",
                         tooltip_text=_("Reply"))
        send.add_css_class("flat")
        pid = post.get("id", "")
        reply_entry.connect(
            "activate", lambda e, mid=pid: self._send_reply(mid, e))
        send.connect(
            "clicked", lambda _b, e=reply_entry, mid=pid: self._send_reply(mid, e))
        rcomp = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6,
                       margin_top=4)
        rcomp.append(reply_entry)
        rcomp.append(send)
        inner.append(rcomp)
        card.append(inner)
        return card

    def _message_block(self, msg, *, heading: bool) -> Gtk.Widget:
        """One post/reply: sender + time header, then body, attachments,
        reactions. Shared by root posts and replies."""
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        sender = (msg.get("from", "") or _("Unknown")).strip()
        name = Gtk.Label(label=esc(sender), xalign=0)
        name.add_css_class("caption-heading")
        head.append(name)
        if msg.get("sent"):
            when = Gtk.Label(label=relative_time(msg["sent"]), xalign=0)
            when.add_css_class("dim-label")
            when.add_css_class("caption")
            head.append(when)
        box.append(head)

        # A replied-to message renders as a small quote (accent bar + author +
        # snippet) instead of a bogus "attachment" chip.
        reply = msg.get("reply_to")
        if reply and (reply.get("text") or reply.get("from")):
            box.append(self._reply_quote(reply))

        text = (msg.get("text", "") or "").strip()
        markup = (msg.get("markup", "") or "").strip()
        if text or markup:
            body = Gtk.Label(label=text, xalign=0, wrap=True, selectable=True)
            body.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
            if markup:
                try:
                    body.set_markup(markup)
                    body.connect("activate-link", self._on_link_activated)
                except Exception:  # noqa: BLE001 - bad markup → plain text
                    body.set_text(text)
            box.append(body)

        for att in msg.get("attachments", []) or []:
            # Images render as inline thumbnails (like Chat); other files as a
            # chip. Mirrors chat_view._bubble so the two surfaces look the same.
            if (att.get("content_type") or "").lower().startswith("image") \
                    and att.get("url"):
                box.append(self._lazy_image(
                    lambda u=att["url"]: self._client().fetch_bytes(u),
                    att.get("name") or "image", max_px=320))
            else:
                box.append(attachment_chip(att, self._window))

        reactions = msg.get("reactions") or []
        if reactions:
            rbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4,
                          margin_top=2)
            for r in reactions:
                label = (f"{r['emoji']} {r['count']}"
                         if r.get("count", 0) > 1 else r["emoji"])
                chip = Gtk.Label(label=label)
                chip.add_css_class("cloudy-reaction")
                rbox.append(chip)
            box.append(rbox)
        return box

    @staticmethod
    def _reply_quote(reply) -> Gtk.Widget:
        """A compact quote of the message a post/reply is answering: an accent
        bar, the quoted author and a one-line snippet."""
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        box.add_css_class("cloudy-reply-quote")
        bar = Gtk.Box()
        bar.add_css_class("cloudy-reply-bar")
        box.append(bar)
        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
        who = (reply.get("from") or "").strip() or _("Message")
        wlbl = Gtk.Label(label=esc(who), xalign=0,
                         ellipsize=Pango.EllipsizeMode.END)
        wlbl.add_css_class("caption-heading")
        inner.append(wlbl)
        snippet = (reply.get("text") or _("(image)")).replace("\n", " ").strip()
        slbl = Gtk.Label(label=snippet[:120], xalign=0,
                         ellipsize=Pango.EllipsizeMode.END)
        slbl.add_css_class("caption")
        slbl.add_css_class("dim-label")
        inner.append(slbl)
        box.append(inner)
        return box


    def _lazy_image(self, fetch, name: str = "image", *, max_px: int = 320
                    ) -> Gtk.Widget:
        """A thumbnail that lazily downloads an (auth-gated) image via ``fetch``
        (a no-arg callable returning bytes) and, on click, opens it full size.
        Shared by channel image attachments and OneNote page images."""
        placeholder = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        placeholder.append(Gtk.Image.new_from_icon_name("image-x-generic-symbolic"))
        placeholder.append(Gtk.Label(label=_("Loading image…")))
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.append(placeholder)

        def done(result, error):
            if error or not result:
                placeholder.get_last_child().set_text(_("Image unavailable"))
                if error:
                    placeholder.set_tooltip_text(str(error))
                return False
            data, png = result
            try:
                texture = texture_from_png_bytes(png)
            except Exception as exc:  # noqa: BLE001 - undecodable payload
                placeholder.get_last_child().set_text(_("Image"))
                placeholder.set_tooltip_text(str(exc))
                return False
            pic = Gtk.Picture.new_for_paintable(texture)
            pic.set_can_shrink(False)
            pic.set_halign(Gtk.Align.START)
            pic.add_css_class("cloudy-bubble-image")
            pic.set_size_request(texture.get_width(), texture.get_height())
            pic.set_cursor(Gdk.Cursor.new_from_name("pointer", None))
            tap = Gtk.GestureClick()
            tap.connect("released", lambda *_a: self._open_image_viewer(data, name))
            pic.add_controller(tap)
            box.remove(placeholder)
            box.append(pic)
            return False

        def work():
            data = fetch()
            png = shrink_image_bytes(data, max_px)
            return data, png

        run_async(work, done)
        return box

    def _open_image_viewer(self, data: bytes, name: str = "image") -> None:
        from .media_window import ImageWindow

        ImageWindow(self._window, data, name).present()


    def _send_post(self) -> None:
        text = self._post_entry.get_text().strip()
        if not text or not self._channel_id:
            return
        self._post_entry.set_text("")
        team_id, channel_id = self._team_id, self._channel_id

        def work():
            return self._client().send_channel_message(team_id, channel_id, text)

        run_async(work,
                  lambda _r, err: self._after_send(channel_id, err))

    def _send_reply(self, message_id, entry) -> None:
        text = entry.get_text().strip()
        if not text or not message_id or not self._channel_id:
            return
        entry.set_text("")
        team_id, channel_id = self._team_id, self._channel_id

        def work():
            return self._client().reply_channel_message(
                team_id, channel_id, message_id, text)

        run_async(work,
                  lambda _r, err: self._after_send(channel_id, err))

    def _after_send(self, channel_id, error) -> bool:
        if error is not None:
            self._window.add_toast(_("Couldn't send: %s") % friendly_error(error))
            return False
        self._cache.invalidate(prefix=self._conv_key())
        if channel_id == self._channel_id:
            self._load_conversation()
        return False

    def _on_link_activated(self, _label, uri) -> bool:
        self._window.open_uri(uri)
        return True

    def _scroll_conv_to_bottom(self) -> bool:
        adj = self._conv_scroll.get_vadjustment()
        adj.set_value(adj.get_upper() - adj.get_page_size())
        return False

    # ------------------------------------------------------------------ #
    # Notes (team OneNote notebook)
    # ------------------------------------------------------------------ #
    def _ensure_notes_loaded(self) -> None:
        if not self._channel_id:
            self._notes_bin.set_child(self._notes_placeholder)
            return
        self._notes_bin.set_child(self._notes_box)
        if self._notes_loaded_for == self._team_id:
            return
        self._notes_loaded_for = self._team_id
        self._load_notebook()

    def _load_notebook(self) -> None:
        team_id = self._team_id
        self._page_content.set_child(loading_box(_("Loading notebook…")))

        def work():
            client = self._client()
            notebooks = client.list_notebooks(team_id)
            if not notebooks:
                return {"sections": []}
            notebook_id = notebooks[0]["id"]
            sections = client.list_note_sections(team_id, notebook_id)
            return {"notebook_id": notebook_id, "sections": sections}

        run_async(work, lambda res, err: self._on_notebook(team_id, res, err))

    def _on_notebook(self, team_id, result, error) -> bool:
        if team_id != self._team_id:
            return False
        if error is not None:
            msg = (SCOPE_HINT if is_scope_error(error) else esc(error))
            self._page_content.set_child(status_page(
                "dialog-warning-symbolic", _("Couldn't load notes"), msg))
            self._set_sections([])
            return False
        self._notebook_id = result.get("notebook_id", "")
        sections = result.get("sections", [])
        self._sections = sections
        if not sections:
            self._section_id = ""
            self._set_sections(sections)
            self._page_content.set_child(status_page(
                "accessories-text-editor-symbolic", _("No notes yet"),
                _("This team's notebook has no sections.")))
            return False
        # Set the active section *before* populating the dropdown so its
        # notify::selected handler short-circuits (no duplicate page fetch).
        self._section_id = sections[0]["id"]
        self._set_sections(sections)
        self._load_pages()
        return False

    def _set_sections(self, sections) -> None:
        model = Gtk.StringList()
        for s in sections:
            model.append(s["name"])
        self._section_dd.set_model(model)
        self._section_dd.set_sensitive(bool(sections))

    def _on_section_selected(self, *_a) -> None:
        idx = self._section_dd.get_selected()
        sections = getattr(self, "_sections", [])
        if idx < 0 or idx >= len(sections):
            return
        section_id = sections[idx]["id"]
        if section_id == self._section_id:
            return
        self._section_id = section_id
        self._load_pages()

    def _load_pages(self) -> None:
        team_id, section_id = self._team_id, self._section_id
        self._set_pages([])
        self._page_content.set_child(loading_box(_("Loading pages…")))

        def work():
            return self._client().list_note_pages(team_id, section_id)

        run_async(work, lambda res, err: self._on_pages(section_id, res, err))

    def _on_pages(self, section_id, result, error) -> bool:
        if section_id != self._section_id:
            return False
        if error is not None:
            self._set_pages([])
            self._page_content.set_child(status_page(
                "dialog-warning-symbolic", _("Couldn't load pages"), esc(error)))
            return False
        self._set_pages(result or [])
        if not result:
            self._page_content.set_child(status_page(
                "accessories-text-editor-symbolic", _("No pages"),
                _("This section has no pages yet.")))
            return False
        # Open the first (most recent) page straight away.
        self._open_page(result[0])
        return False

    def _set_pages(self, pages) -> None:
        self._pages = pages
        self._suppress_page_dd = True
        model = Gtk.StringList()
        for p in pages:
            model.append(p["title"])
        self._page_dd.set_model(model)
        self._page_dd.set_sensitive(bool(pages))
        if pages:
            self._page_dd.set_selected(0)
        self._suppress_page_dd = False

    def _on_page_selected(self, *_a) -> None:
        if self._suppress_page_dd:
            return
        idx = self._page_dd.get_selected()
        pages = getattr(self, "_pages", [])
        if 0 <= idx < len(pages) and pages[idx]["id"] != self._page_id:
            self._open_page(pages[idx])

    def _open_page(self, page) -> None:
        self._page_id = page["id"]
        team_id, page_id = self._team_id, page["id"]
        self._page_content.set_child(loading_box(_("Loading page…")))

        def work():
            return self._client().get_note_page(team_id, page_id)

        run_async(work,
                  lambda res, err: self._on_page_content(page, res, err))

    def _on_page_content(self, page, result, error) -> bool:
        if page["id"] != self._page_id:
            return False
        if error is not None:
            self._page_content.set_child(status_page(
                "dialog-warning-symbolic", _("Couldn't load page"), esc(error)))
            return False
        self._show_page_reader(page, result or "")
        return False

    def _show_page_reader(self, page, content_html: str) -> None:
        toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6,
                         margin_top=8, margin_bottom=8,
                         margin_start=16, margin_end=16)
        title = Gtk.Label(label=esc(page["title"]), xalign=0, hexpand=True,
                         wrap=True)
        title.add_css_class("title-3")
        toolbar.append(title)
        if page.get("web_url"):
            open_btn = Gtk.Button(icon_name="external-link-symbolic",
                                 tooltip_text=_("Open in OneNote"))
            open_btn.add_css_class("flat")
            open_btn.connect("clicked",
                            lambda *_a: self._window.open_uri(page["web_url"]))
            toolbar.append(open_btn)
        edit_btn = Gtk.Button(label=_("Edit"))
        edit_btn.add_css_class("flat")
        edit_btn.connect("clicked",
                        lambda *_a: self._edit_page(page, content_html))
        toolbar.append(edit_btn)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.append(toolbar)
        box.append(Gtk.Separator())
        box.append(self._render_note_body(content_html))
        self._page_content.set_child(box)

    # The longest side of an offscreen widget GTK can upload as a single GL
    # texture is ~16k px on the Mesa/Intel path; a OneNote paragraph long enough
    # to render past that aborts in gsk_gpu_upload_cairo_op (the same crash we
    # dodged by dropping WebKit). One wrapped label of this many characters stays
    # comfortably under that ceiling, so any longer block is split across labels.
    _MAX_LABEL_CHARS = 12000

    def _render_note_body(self, content_html: str) -> Gtk.Widget:
        """Render a OneNote page natively: text blocks as wrapping labels and
        images as inline thumbnails, in a scrolled reading column.

        We deliberately avoid a WebView here — OneNote pages can be long, and a
        full-page WebKit snapshot overruns the GPU texture limit and crashes the
        renderer. Native widgets are clipped/culled, but a *single* very long
        paragraph can still grow one label past the GL texture limit and crash
        the same way, so over-long blocks are split across several labels."""
        column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        body = content_html or ""
        m = re.search(r"(?is)<body[^>]*>(.*)</body>", body)
        inner = m.group(1) if m else body
        # Walk the body in document order, splitting out <img> tags so text and
        # pictures interleave the way they appear on the page.
        segments = re.split(r"(?is)(<img\b[^>]*?>)", inner)
        rendered_any = False
        for seg in segments:
            if not seg:
                continue
            if re.match(r"(?is)^<img\b", seg):
                src_m = re.search(r"""(?is)\bsrc=["']([^"']+)["']""", seg)
                if not src_m:
                    continue
                url = html.unescape(src_m.group(1))
                column.append(self._lazy_image(
                    lambda u=url: self._client().fetch_note_image(u),
                    _("image"), max_px=760))
                rendered_any = True
                continue
            markup = html_to_pango(seg)
            text = strip_html(seg)
            if not text:
                continue
            # A short block keeps its inline formatting (bold/links/…); a block
            # long enough to risk the texture ceiling is rendered as plain text
            # split into bounded chunks (markup can't be split mid-tag safely).
            if markup and len(text) <= self._MAX_LABEL_CHARS:
                column.append(self._note_label(markup=markup, text=text))
            else:
                for chunk in self._split_text(text, self._MAX_LABEL_CHARS):
                    column.append(self._note_label(text=chunk))
            rendered_any = True
        if not rendered_any:
            return status_page("accessories-text-editor-symbolic",
                               _("Empty page"), _("This page has no content."))
        column.set_margin_top(16)
        column.set_margin_bottom(24)
        column.set_margin_start(20)
        column.set_margin_end(20)
        return Gtk.ScrolledWindow(
            vexpand=True, hexpand=True,
            hscrollbar_policy=Gtk.PolicyType.NEVER, child=column)

    def _note_label(self, *, markup: str = "", text: str = "") -> Gtk.Label:
        label = Gtk.Label(xalign=0, wrap=True, selectable=True, hexpand=True)
        label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        if markup:
            try:
                label.set_markup(markup)
                label.connect("activate-link", self._on_link_activated)
            except Exception:  # noqa: BLE001 - bad markup → plain text
                label.set_text(text)
        else:
            label.set_text(text)
        label.add_css_class("body")
        return label

    @staticmethod
    def _split_text(text: str, limit: int) -> list[str]:
        """Split ``text`` into chunks of at most ``limit`` characters, breaking
        on a newline/space near the boundary so words stay intact."""
        if len(text) <= limit:
            return [text]
        chunks, start = [], 0
        while start < len(text):
            end = start + limit
            if end >= len(text):
                chunks.append(text[start:])
                break
            cut = max(text.rfind("\n", start, end), text.rfind(" ", start, end))
            if cut <= start:
                cut = end  # no break point — hard-split
            chunks.append(text[start:cut])
            start = cut + 1 if text[cut:cut + 1] in (" ", "\n") else cut
        return chunks

    # -- create / edit ----------------------------------------------------
    def _new_page(self) -> None:
        if not self._section_id:
            self._window.add_toast(_("Pick a section first"))
            return
        self._edit_page(None, "")

    def _edit_page(self, page, content_html: str) -> None:
        from .rich_editor import RichTextEditor
        from .message_view import _to_text  # plain-text fold of the page HTML

        title_entry = Gtk.Entry(
            hexpand=True, placeholder_text=_("Page title"),
            text=("" if page is None else page.get("title", "")))
        editor = RichTextEditor()
        if page is not None and content_html:
            # The editor seeds from plain text, so existing formatting is not
            # preserved on edit (a known v1 limitation noted to the user).
            try:
                editor.set_plain_text(_to_text(content_html).strip())
            except Exception:  # noqa: BLE001
                pass

        cancel = Gtk.Button(label=_("Cancel"))
        cancel.add_css_class("flat")
        save = Gtk.Button(label=_("Save"))
        save.add_css_class("suggested-action")
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6,
                     margin_top=8, margin_bottom=8, margin_start=12, margin_end=12)
        bar.append(title_entry)
        bar.append(cancel)
        bar.append(save)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.append(bar)
        box.append(Gtk.Separator())
        editor.set_margin_top(8)
        editor.set_margin_bottom(8)
        editor.set_margin_start(12)
        editor.set_margin_end(12)
        editor.set_vexpand(True)
        box.append(editor)
        self._page_content.set_child(box)

        cancel.connect("clicked", lambda *_a: self._after_notes_change(page))
        save.connect("clicked",
                    lambda *_a: self._save_page(page, title_entry, editor))

    def _save_page(self, page, title_entry, editor) -> None:
        title = title_entry.get_text().strip() or _("Untitled page")
        try:
            body_html, _imgs = editor.get_html()
        except Exception:  # noqa: BLE001 - fall back to plain text on editor error
            body_html = esc(editor.get_plain_text())
        team_id = self._team_id
        section_id = self._section_id
        page_id = None if page is None else page.get("id")

        def work():
            client = self._client()
            if page_id is None:
                client.create_note_page(team_id, section_id, title, body_html)
            else:
                client.update_note_page(team_id, page_id, body_html)
            return True

        self._page_content.set_child(loading_box(_("Saving…")))
        run_async(work, lambda _r, err: self._on_page_saved(page, err))

    def _on_page_saved(self, page, error) -> bool:
        if error is not None:
            self._window.add_toast(_("Couldn't save: %s") % friendly_error(error))
            self._after_notes_change(page)
            return False
        # OneNote writes are async; give the service a moment, then refresh.
        self._window.add_toast(_("Saved — it may take a moment to appear"))
        self._cache.invalidate(
            prefix=f"{self._account.id}:notepages:{self._section_id}")
        self._after_notes_change(None)
        self._load_pages()
        return False

    def _after_notes_change(self, page) -> None:
        if page is not None:
            self._open_page(page)
        else:
            self._page_content.set_child(status_page(
                "accessories-text-editor-symbolic", _("Notes"),
                _("Select a page to read it.")))

    # ------------------------------------------------------------------ #
    # Small shared widgets
    # ------------------------------------------------------------------ #
    @staticmethod
    def _clear_box(box: Gtk.Box) -> None:
        child = box.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            box.remove(child)
            child = nxt

    @staticmethod
    def _loading_row(text: str) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow(activatable=False, selectable=False)
        b = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8,
                   margin_top=10, margin_bottom=10, halign=Gtk.Align.CENTER)
        sp = Gtk.Spinner()
        sp.start()
        b.append(sp)
        b.append(Gtk.Label(label=text))
        row.set_child(b)
        return row

    @staticmethod
    def _message_row(text: str) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow(activatable=False, selectable=False)
        lbl = Gtk.Label(label=text, wrap=True, justify=Gtk.Justification.CENTER,
                       margin_top=12, margin_bottom=12,
                       margin_start=10, margin_end=10)
        lbl.add_css_class("dim-label")
        row.set_child(lbl)
        return row
