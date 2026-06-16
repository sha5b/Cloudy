# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Shahab Nedaei
"""Chat surface: a two-pane messenger for Teams chats / Google Chat spaces.

Left pane = the list of chats (most-recent first). Right pane = the selected
chat's message thread (bubbles) with a composer pinned to the bottom. Both the
chat list and each thread are cached (stale-while-revalidate) like Mail.

Provider notes: Teams chats need a work/school account (``Chat.ReadWrite``).
Google Chat is Workspace-only — on a consumer Gmail account the API call fails
and the view shows an "unavailable" placeholder rather than an empty list.
"""

from __future__ import annotations

import html
import re
from gettext import gettext as _

from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango


def _initials(name: str) -> str:
    parts = [p for p in re.split(r"[\s,]+", name or "") if p]
    if not parts:
        return "?"
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][0] + parts[-1][0]).upper()

from .format import esc, relative_time
from .source_nav import (
    SCOPE_HINT,
    action_row,
    clear_listbox,
    is_muted,
    is_pinned,
    is_scope_error,
    local_initial_folder,
    message_row,
    run_async,
    toggle_mute,
    toggle_pin,
)


class ChatView(Adw.Bin):
    __gtype_name__ = "CloudyChatView"

    def __init__(self, window, account):
        super().__init__()
        self._window = window
        self._account = account
        self._all_chats: list[dict] = []  # full list (unfiltered)
        self._query = ""
        self._chat_id = None
        self._chat_name = ""
        self._rows_by_id: dict = {}
        self._msg_next_token = None   # cursor for older messages in the open chat
        self._msg_loading = False
        self._older_row = None        # the "Load older" widget atop the thread
        self._chats_next_token = None  # cursor for older conversations
        self._chats_loading = False
        self._chats_more_row = None
        self._read_ids: set = set()    # chats read in this session (force greyed)
        self._editing = None           # message being edited, if any
        self._chat_members: list = []  # current chat's members (for @mentions)
        self._mentions: list = []      # mentions picked for the pending message
        self._mention_pop = None       # the @-autocomplete popover
        self._search_mode = False      # showing server message-search results
        self._search_timer = None      # debounce for search-as-you-type
        self._bubble_widgets: dict = {}  # message id -> its bubble widget (in-place updates)
        # url -> (texture, original bytes) for already-fetched inline images, so a
        # full thread rebuild (e.g. reconciling an optimistic send) reuses decoded
        # images instantly instead of re-downloading them all.
        self._image_cache: dict = {}
        self._presence: dict = {}      # user_id -> {availability, activity}
        self._presence_source = None   # periodic presence-refresh timer
        self._poll_source = None       # adaptive open-thread poll timer
        self._thread_sig = None        # signature of the rendered thread (poll diff)
        self._members_pop = None       # the group roster/management popover

        # -- left pane: the chat list ------------------------------------
        self._list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE,
                                  valign=Gtk.Align.START)
        self._list.add_css_class("navigation-sidebar")
        self._list.connect("row-activated", self._on_chat_activated)
        list_scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER,
                                         vexpand=True)
        list_scroll.set_child(self._list)
        # Auto-load older conversations when the list is scrolled near the end.
        list_scroll.get_vadjustment().connect("value-changed", self._on_list_scrolled)

        self._search = Gtk.SearchEntry(
            placeholder_text=_("Search chats and messages…"),
            hexpand=True)
        self._search.connect("search-changed", self._on_search_changed)
        self._search.connect("activate", self._on_search_activate)  # message search
        search_bar = Gtk.Box(margin_top=6, margin_bottom=6,
                             margin_start=10, margin_end=10)
        search_bar.append(self._search)

        new_btn = Gtk.Button(icon_name="chat-message-new-symbolic",
                             tooltip_text=_("New chat"))
        new_btn.connect("clicked", self._on_new_chat)

        sidebar_tb = Adw.ToolbarView()
        header = Adw.HeaderBar(
            show_start_title_buttons=False, show_end_title_buttons=False,
            title_widget=Gtk.Label(label=_("Chats")))
        header.pack_start(new_btn)
        sidebar_tb.add_top_bar(header)
        sidebar_tb.add_top_bar(search_bar)
        sidebar_tb.set_content(list_scroll)
        sidebar_page = Adw.NavigationPage(title=_("Chat"), tag="chats")
        sidebar_page.set_child(sidebar_tb)

        # -- right pane: the thread + composer ---------------------------
        self._thread = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6,
                               margin_top=12, margin_bottom=12,
                               margin_start=12, margin_end=12, valign=Gtk.Align.END,
                               vexpand=True)
        self._thread_scroll = Gtk.ScrolledWindow(
            hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        self._thread_scroll.set_child(self._thread)
        # Stick to the bottom: re-pin on every size change (bubbles/images
        # allocate *after* render, so a one-shot scroll lands mid-thread). The
        # pinned/scrolled-up state is derived purely from the adjustment's
        # value, so it stays correct no matter how the user scrolls — wheel,
        # trackpad, scrollbar drag, PageUp/Down or Home/End all route through
        # the same "value-changed" handler.
        self._autoscroll = True
        self._anchor_bottom = None  # distance-from-bottom to preserve on prepend
        self._adjusting = False     # guard: ignore our own programmatic scrolls
        self._scroll_anim = None    # active "jump to latest" animation, if any
        self._rendered_sigs = []    # per-message fingerprints of the live thread
        # The un-acked optimistic echo, if one is showing: {"widget", "text"}.
        # When the server confirms it, that exact widget is adopted in place
        # (status flips Sending→Sent) instead of rebuilding the whole thread —
        # so its decoded image never reloads and the view never jumps.
        self._optimistic = None
        self._hold_tick = None      # frame-clock callback holding scroll position
        self._hold_until = 0        # frame time (µs) the hold expires
        vadj = self._thread_scroll.get_vadjustment()
        vadj.connect("changed", self._on_thread_resized)        # height changed
        vadj.connect("value-changed", self._on_thread_scrolled)  # any scroll
        # Floating "jump to newest" button, shown only when scrolled up.
        self._to_bottom_btn = Gtk.Button(
            icon_name="go-bottom-symbolic", tooltip_text=_("Go to latest"),
            halign=Gtk.Align.END, valign=Gtk.Align.END,
            margin_end=14, margin_bottom=14, visible=False)
        self._to_bottom_btn.add_css_class("circular")
        self._to_bottom_btn.add_css_class("osd")
        self._to_bottom_btn.connect("clicked",
                                    lambda *_a: self._scroll_to_bottom(animate=True))
        self._thread_overlay = Gtk.Overlay()
        self._thread_overlay.set_child(self._thread_scroll)
        self._thread_overlay.add_overlay(self._to_bottom_btn)

        self._reader = Adw.Bin(vexpand=True)
        self._reader.set_child(self._placeholder(
            "user-available-symbolic", _("No chat selected"),
            _("Pick a conversation from the list to read it here.")))

        self._entry = Gtk.Entry(
            hexpand=True, placeholder_text=_("Write a message…"), sensitive=False)
        self._entry.connect("activate", self._on_send)
        self._entry.connect("changed", self._on_entry_changed)  # @-mention picker
        # Ctrl+V of a clipboard image → send it as an attachment (capture phase
        # so we intercept before the entry's own text paste).
        paste = Gtk.EventControllerKey()
        paste.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        paste.connect("key-pressed", self._on_entry_key)
        self._entry.add_controller(paste)
        self._attach_btn = Gtk.Button(
            icon_name="mail-attachment-symbolic", tooltip_text=_("Attach image"),
            sensitive=False)
        self._attach_btn.add_css_class("flat")
        self._attach_btn.connect("clicked", self._on_attach)

        # Formatting + emoji: bold/italic wrap the selection in markdown that the
        # send path converts to HTML; the emoji button opens an insert picker.
        self._emoji_btn = Gtk.Button(
            icon_name="face-smile-symbolic", tooltip_text=_("Emoji"),
            sensitive=False)
        self._emoji_btn.add_css_class("flat")
        self._emoji_btn.connect("clicked", self._on_emoji_picker)
        self._bold_btn = Gtk.Button(
            icon_name="format-text-bold-symbolic", tooltip_text=_("Bold"),
            sensitive=False)
        self._bold_btn.add_css_class("flat")
        self._bold_btn.connect("clicked", lambda *_a: self._wrap_selection("**"))
        self._italic_btn = Gtk.Button(
            icon_name="format-text-italic-symbolic", tooltip_text=_("Italic"),
            sensitive=False)
        self._italic_btn.add_css_class("flat")
        self._italic_btn.connect("clicked", lambda *_a: self._wrap_selection("_"))

        # Reply context bar (shown when replying to a message) and a staged
        # image strip (pasted screenshots wait here until you hit Send).
        self._pending: list = []  # [{data, ctype, widget}]
        self._reply_to = None
        self._reply_bar = Gtk.Box(spacing=8, margin_start=10, margin_end=10,
                                  margin_top=4, visible=False)
        self._reply_lbl = Gtk.Label(xalign=0, hexpand=True,
                                    ellipsize=Pango.EllipsizeMode.END)
        self._reply_lbl.add_css_class("caption")
        self._reply_bar.append(Gtk.Image.new_from_icon_name("mail-reply-sender-symbolic"))
        self._reply_bar.append(self._reply_lbl)
        cancel_reply = Gtk.Button(icon_name="window-close-symbolic")
        cancel_reply.add_css_class("flat")
        cancel_reply.connect("clicked", lambda *_a: self._on_cancel_ctx())
        self._reply_bar.append(cancel_reply)

        self._preview = Gtk.Box(spacing=6, margin_start=10, margin_end=10,
                               margin_top=4, visible=False)

        entry_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6,
                            margin_top=6, margin_bottom=6, margin_start=10, margin_end=10)
        entry_row.append(self._attach_btn)
        entry_row.append(self._emoji_btn)
        entry_row.append(self._bold_btn)
        entry_row.append(self._italic_btn)
        entry_row.append(self._entry)  # Enter sends (no separate send button)

        # Multi-select action bar (revealed in select mode), above the composer.
        self._select_mode = False
        self._selected_msgs: dict = {}  # message id -> msg
        self._select_bar = self._build_select_bar()

        composer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        composer.append(self._select_bar)
        composer.append(self._reply_bar)
        composer.append(self._preview)
        composer.append(entry_row)

        content_tb = Adw.ToolbarView()
        self._content_header = Adw.HeaderBar(
            show_start_title_buttons=False, show_end_title_buttons=False)
        # Title = chat name with a presence dot (1:1) / member count (group),
        # mirroring the Teams conversation header.
        self._header_title = Adw.WindowTitle(title=_("Conversation"), subtitle="")
        self._content_header.set_title_widget(self._header_title)
        self._members_btn = Gtk.Button(icon_name="system-users-symbolic",
                                       tooltip_text=_("People in this chat"),
                                       visible=False)
        self._members_btn.connect("clicked", self._on_show_members)
        self._content_header.pack_end(self._members_btn)
        # Star the open chat → it's pinned to the top of the Dashboard's chats.
        self._star_btn = Gtk.Button(icon_name="non-starred-symbolic",
                                    tooltip_text=_("Star this chat for the Dashboard"),
                                    visible=False)
        self._star_btn.add_css_class("flat")
        self._star_btn.connect("clicked", self._on_star_clicked)
        self._content_header.pack_end(self._star_btn)
        # Mute the open chat → no notification banner or badge for it.
        self._mute_btn = Gtk.Button(icon_name="preferences-system-notifications-symbolic",
                                    tooltip_text=_("Mute notifications for this chat"),
                                    visible=False)
        self._mute_btn.add_css_class("flat")
        self._mute_btn.connect("clicked", self._on_mute_clicked)
        self._content_header.pack_end(self._mute_btn)
        content_tb.add_top_bar(self._content_header)
        content_tb.set_content(self._reader)
        content_tb.add_bottom_bar(composer)
        content_page = Adw.NavigationPage(title=_("Conversation"), tag="thread")
        content_page.set_child(content_tb)

        self._split = Adw.NavigationSplitView(
            min_sidebar_width=280, max_sidebar_width=420, sidebar_width_fraction=0.34)
        self._split.set_sidebar(sidebar_page)
        self._split.set_content(content_page)
        self.set_child(self._split)

        self._load_chats()

    # -- helpers ----------------------------------------------------------
    def _cache(self):
        return self._window.get_application().cache

    def _placeholder(self, icon, title, description) -> Gtk.Widget:
        return Adw.StatusPage(icon_name=icon, title=esc(title),
                              description=esc(description), vexpand=True)

    def _set_list_message(self, text: str) -> None:
        clear_listbox(self._list)
        self._list.append(message_row(text))

    def _reauth_prompt(self) -> None:
        clear_listbox(self._list)
        self._list.append(action_row(
            SCOPE_HINT, _("Re-sign in"),
            lambda: self._window.sign_in_account(self._account)))

    # -- chat list --------------------------------------------------------
    def _on_list_scrolled(self, adj) -> None:
        # Near the bottom of the conversation list → load the next page.
        if (not self._query and self._chats_next_token and not self._chats_loading
                and adj.get_value() >= adj.get_upper() - adj.get_page_size() - 200):
            self._load_more_chats()

    def _load_chats(self) -> None:
        cached = self._cache().get(f"{self._account.id}:chats")
        if cached is not None:
            self._render_chats(cached[0])  # instant; still revalidate for the cursor
        else:
            self._set_list_message(_("Loading chats…"))

        def work():
            from .clients import build_account_client

            client = build_account_client(self._window.get_application(), self._account)
            return client.list_chats_page()

        run_async(work, self._on_chats)

    def _on_chats(self, result, error) -> bool:
        if error:
            if not self._all_chats:
                if is_scope_error(error):
                    self._reauth_prompt()
                else:
                    self._set_list_message(self._unavailable_text(error))
            return False
        chats, next_token = result
        chats = sorted(chats, key=lambda c: c.get("last_at", ""), reverse=True)
        self._chats_next_token = next_token
        self._cache().set(f"{self._account.id}:chats", chats)
        self._render_chats(chats)
        return False

    def _load_more_chats(self) -> None:
        token = self._chats_next_token
        if not token or self._chats_loading:
            return
        self._chats_loading = True
        if self._chats_more_row is not None:
            self._chats_more_row.set_child(self._more_label(_("Loading…")))

        def work():
            from .clients import build_account_client

            client = build_account_client(self._window.get_application(), self._account)
            return client.list_chats_page(page_token=token)

        run_async(work, self._on_more_chats)

    def _on_more_chats(self, result, error) -> bool:
        self._chats_loading = False
        if error:
            self._window.add_toast(_("Couldn't load more: %s") % error)
            self._render_filtered()
            return False
        chats, next_token = result
        self._chats_next_token = next_token
        seen = {c["id"] for c in self._all_chats}
        merged = self._all_chats + [c for c in chats if c["id"] not in seen]
        merged.sort(key=lambda c: c.get("last_at", ""), reverse=True)
        self._cache().set(f"{self._account.id}:chats", merged)
        self._render_chats(merged)
        return False

    def _unavailable_text(self, error: str) -> str:
        if self._account.provider == "google":
            return _("Google Chat needs a Workspace account.\n\n%s") % error
        return _("Couldn't load chats: %s") % error

    def _on_search_changed(self, entry) -> None:
        # Typing filters the loaded chats by name instantly AND kicks off a
        # debounced server-side message search, so results include messages from
        # chats that aren't loaded (we never hold them all at once).
        raw = entry.get_text().strip()
        self._query = raw.lower()
        if self._search_timer is not None:
            GLib.source_remove(self._search_timer)
            self._search_timer = None
        if not raw:
            self._search_mode = False
            self._render_filtered()
            return
        self._render_filtered()  # instant: matching loaded chats
        self._search_timer = GLib.timeout_add(
            350, lambda: self._run_message_search(raw))

    def _on_search_activate(self, entry) -> None:
        # Enter = search now, skipping the debounce.
        raw = entry.get_text().strip()
        if not raw:
            return
        if self._search_timer is not None:
            GLib.source_remove(self._search_timer)
            self._search_timer = None
        self._run_message_search(raw)

    def _run_message_search(self, query) -> bool:
        self._search_timer = None
        self._search_mode = True

        def work():
            from .clients import build_account_client

            client = build_account_client(self._window.get_application(), self._account)
            return client.search_messages(query)

        run_async(work, lambda res, err: self._on_search_results(query, res, err))
        return False  # one-shot timer

    def _render_chats(self, chats) -> None:
        self._all_chats = chats
        if not self._search_mode:  # don't clobber visible search results
            self._render_filtered()
        self._refresh_presence()
        self._start_presence_timer()

    def _render_filtered(self) -> None:
        chats = self._all_chats
        if self._query:
            chats = [c for c in chats if self._query in (c.get("name", "") or "").lower()]
        self._rows_by_id = {}
        if not self._all_chats:
            self._set_list_message(_("No conversations."))
            return
        if not chats:
            self._set_list_message(_("No chats match “%s”.") % self._query)
            return
        clear_listbox(self._list)
        for chat in chats:
            row = self._chat_row(chat)
            self._list.append(row)
            self._rows_by_id[chat["id"]] = row
        # "Load older conversations" — only when not filtering by a search query.
        self._chats_more_row = None
        if not self._query and self._chats_next_token:
            self._chats_more_row = Gtk.ListBoxRow(activatable=True, selectable=False)
            self._chats_more_row._more_chats = True
            self._chats_more_row.set_child(self._more_label(_("Load older conversations")))
            self._list.append(self._chats_more_row)

    @staticmethod
    def _more_label(text: str) -> Gtk.Label:
        lbl = Gtk.Label(label=text, margin_top=10, margin_bottom=10)
        lbl.add_css_class("dim-label")
        return lbl

    def _chat_row(self, chat) -> Gtk.ListBoxRow:
        # Unread if Graph's read-receipt says so OR the notifier flagged a new
        # message (the source of the red account badge) — so the badge count and
        # the bold rows always agree. Cleared once opened this session.
        notifier = getattr(self._window.get_application(), "notifier", None)
        flagged = bool(notifier and notifier.is_chat_unread(self._account.id, chat["id"]))
        unread = ((bool(chat.get("unread")) or flagged)
                  and chat["id"] not in self._read_ids)
        is_meeting = chat.get("kind") == "meeting"
        row = Gtk.ListBoxRow(activatable=True)
        row._chat_id = chat["id"]
        row._chat_name = chat.get("name", "")
        outer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10,
                        margin_top=8, margin_bottom=8, margin_start=12, margin_end=12)
        avatar = self._avatar(chat, is_meeting, unread)
        row._avatar_overlay = avatar  # so presence updates can patch its dot
        outer.append(avatar)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True,
                      valign=Gtk.Align.CENTER)  # center against the avatar (1-line rows)
        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        if is_meeting:
            mic = Gtk.Image.new_from_icon_name("camera-video-symbolic")
            mic.set_valign(Gtk.Align.CENTER)
            mic.add_css_class("dim-label")
            top.append(mic)
        name = Gtk.Label(label=chat.get("name", "") or _("Chat"), xalign=0,
                         hexpand=True, ellipsize=Pango.EllipsizeMode.END)
        name.add_css_class("heading" if unread else "body")
        if not unread:
            name.add_css_class("dim-label")
        top.append(name)
        if chat.get("last_at"):
            when = Gtk.Label(label=relative_time(chat["last_at"]), xalign=1)
            when.add_css_class("dim-label")
            when.add_css_class("caption")
            top.append(when)
        box.append(top)
        preview = (chat.get("preview", "") or "").replace("\n", " ").strip()
        if preview:
            if chat.get("from_me"):
                preview = _("You: %s") % preview
            prev = Gtk.Label(label=preview, xalign=0,
                             ellipsize=Pango.EllipsizeMode.END)
            prev.add_css_class("dim-label")
            prev.add_css_class("caption")
            box.append(prev)
        outer.append(box)
        row.set_child(outer)
        return row

    def _avatar(self, chat, is_meeting: bool, unread: bool) -> Gtk.Widget:
        """A round avatar (Adw.Avatar auto-centers initials) with a Teams-style
        presence dot (bottom-right) and, when unread, an accent dot (top-right
        so it doesn't collide with presence)."""
        overlay = Gtk.Overlay(valign=Gtk.Align.CENTER)
        face = Adw.Avatar(size=38)
        if is_meeting:
            face.set_show_initials(False)
            face.set_icon_name("x-office-calendar-symbolic")
        else:
            face.set_show_initials(True)
            face.set_text(chat.get("name", "") or "?")
        overlay.set_child(face)
        overlay._presence_dot = None  # tracked so presence updates swap it in place
        # Presence dot — only for 1:1 chats (a single "other" member whose
        # availability we know). Groups/meetings show a member count instead.
        dot = self._presence_dot_for(chat)
        if dot is not None:
            dot.set_halign(Gtk.Align.END)
            dot.set_valign(Gtk.Align.END)
            overlay.add_overlay(dot)
            overlay._presence_dot = dot
        if unread:
            udot = Gtk.Image.new_from_icon_name("media-record-symbolic")
            udot.set_pixel_size(11)
            udot.add_css_class("cloudy-unread-dot")
            udot.set_halign(Gtk.Align.END)
            udot.set_valign(Gtk.Align.START)
            overlay.add_overlay(udot)
        return overlay

    # -- presence ---------------------------------------------------------
    # Graph availability values → (css state class, human label). Anything not
    # listed (Offline, PresenceUnknown, "") shows no dot.
    _PRESENCE = {
        "Available": ("available", _("Available")),
        "AvailableIdle": ("available", _("Available")),
        "Away": ("away", _("Away")),
        "BeRightBack": ("away", _("Be right back")),
        "Busy": ("busy", _("Busy")),
        "BusyIdle": ("busy", _("Busy")),
        "DoNotDisturb": ("dnd", _("Do not disturb")),
    }

    def _presence_dot_for(self, chat) -> Gtk.Widget | None:
        if chat.get("kind") not in ("oneOnOne", ""):
            return None
        ids = chat.get("member_ids") or []
        if len(ids) != 1:
            return None
        avail = (self._presence.get(ids[0]) or {}).get("availability", "")
        return self._presence_dot(avail)

    def _presence_dot(self, availability: str) -> Gtk.Widget | None:
        info = self._PRESENCE.get(availability)
        if info is None:
            return None
        dot = Gtk.Image.new_from_icon_name("media-record-symbolic")
        dot.set_pixel_size(12)
        dot.add_css_class("cloudy-presence")
        dot.add_css_class(f"cloudy-presence-{info[0]}")
        dot.set_tooltip_text(info[1])
        return dot

    def _refresh_presence(self) -> None:
        """Batch-fetch presence for everyone in the visible 1:1 chats, then
        repaint the rows whose dot changed."""
        if self._account.provider != "microsoft":
            return
        ids = []
        for c in self._all_chats:
            if c.get("kind") in ("oneOnOne", ""):
                ids.extend(c.get("member_ids") or [])
        ids = [i for i in dict.fromkeys(ids) if i]
        if not ids:
            return

        def work():
            from .clients import build_account_client

            client = build_account_client(self._window.get_application(), self._account)
            return client.get_presences(ids)

        run_async(work, self._on_presence)

    def _on_presence(self, result, error) -> bool:
        if error or not result:
            return False
        changed = self._presence != result
        self._presence.update(result)
        # Patch the dots on existing rows IN PLACE — rebuilding the whole list
        # here would drop the selection and scroll back to the top every refresh.
        if changed and not self._search_mode:
            self._apply_presence_to_rows()
        if self._chat_id is not None:
            self._update_header(self._chat_id, self._chat_name)
        return False

    def _apply_presence_to_rows(self) -> None:
        by_id = {c["id"]: c for c in self._all_chats}
        for chat_id, row in self._rows_by_id.items():
            chat = by_id.get(chat_id)
            overlay = getattr(row, "_avatar_overlay", None)
            if chat is None or overlay is None:
                continue
            old = getattr(overlay, "_presence_dot", None)
            if old is not None:
                overlay.remove_overlay(old)
                overlay._presence_dot = None
            dot = self._presence_dot_for(chat)
            if dot is not None:
                dot.set_halign(Gtk.Align.END)
                dot.set_valign(Gtk.Align.END)
                overlay.add_overlay(dot)
                overlay._presence_dot = dot
        return None

    def _start_presence_timer(self) -> None:
        if self._presence_source is not None or self._account.provider != "microsoft":
            return
        # Refresh every 60s — presence is "soft" state; Teams itself is ~minutes.
        self._presence_source = GLib.timeout_add_seconds(60, self._presence_tick)

    def _presence_tick(self) -> bool:
        if self.get_root() is None:  # widget gone — stop the timer
            self._presence_source = None
            return False
        self._refresh_presence()
        return True

    # -- conversation header ---------------------------------------------
    def _update_header(self, chat_id, name: str) -> None:
        chat = next((c for c in self._all_chats if c["id"] == chat_id), None)
        title = name or (chat or {}).get("name", "") or _("Conversation")
        self._header_title.set_title(title)
        self._update_star(chat_id, title)
        is_group = bool(chat) and chat.get("kind") == "group"
        self._members_btn.set_visible(
            is_group and self._account.provider == "microsoft")
        subtitle = ""
        if chat is not None:
            if is_group:
                n = chat.get("member_count") or len(self._chat_members) or 0
                subtitle = _("%d people") % n if n else _("Group chat")
            else:
                ids = chat.get("member_ids") or []
                if len(ids) == 1:
                    avail = (self._presence.get(ids[0]) or {}).get("availability", "")
                    subtitle = self._PRESENCE.get(avail, ("", ""))[1]
        self._header_title.set_subtitle(subtitle)

    def _update_star(self, chat_id, title: str) -> None:
        self._star_chat_name = title
        active = bool(chat_id)
        self._star_btn.set_visible(active)
        self._mute_btn.set_visible(active)
        if active:
            pinned = is_pinned(self._account, "chat", "teams", chat_id)
            self._star_btn.set_icon_name(
                "starred-symbolic" if pinned else "non-starred-symbolic")
            self._refresh_mute_icon(chat_id)

    def _refresh_mute_icon(self, chat_id) -> None:
        muted = is_muted(self._account, "chat", chat_id)
        self._mute_btn.set_icon_name(
            "notifications-disabled-symbolic" if muted
            else "preferences-system-notifications-symbolic")
        self._mute_btn.set_tooltip_text(
            _("Unmute this chat") if muted else _("Mute notifications for this chat"))

    def _on_mute_clicked(self, _btn) -> None:
        if not self._chat_id:
            return
        muted = toggle_mute(self._window, self._account, kind="chat", sid=self._chat_id)
        self._refresh_mute_icon(self._chat_id)
        self._window.add_toast(_("Chat muted") if muted else _("Chat unmuted"))

    def _on_star_clicked(self, _btn) -> None:
        if not self._chat_id:
            return
        pinned = toggle_pin(
            self._window, self._account, kind="chat", source="teams",
            sid=self._chat_id,
            name=getattr(self, "_star_chat_name", "") or self._chat_name or _("Chat"))
        self._star_btn.set_icon_name(
            "starred-symbolic" if pinned else "non-starred-symbolic")
        self._window.add_toast(
            _("Chat starred") if pinned else _("Chat unstarred"))

    # -- adaptive open-thread poll ---------------------------------------
    # While a chat is open we re-fetch its newest page on a short interval so
    # incoming messages appear without a manual refresh — Teams-like liveness
    # without push infrastructure. The cadence backs off when the window isn't
    # focused (you're not watching) to save battery/network.
    _POLL_ACTIVE = 5    # seconds, window focused
    _POLL_IDLE = 30     # seconds, window unfocused

    def _start_poll(self) -> None:
        self._stop_poll()
        self._poll_source = GLib.timeout_add_seconds(self._POLL_ACTIVE,
                                                     self._poll_tick)

    def _stop_poll(self) -> None:
        if self._poll_source is not None:
            GLib.source_remove(self._poll_source)
            self._poll_source = None

    def _poll_tick(self) -> bool:
        if self.get_root() is None or self._chat_id is None:
            self._poll_source = None
            return False  # view gone / no open chat — stop
        focused = True
        root = self.get_root()
        if root is not None and hasattr(root, "is_active"):
            focused = root.is_active()
        # Don't stack a fetch while one is already in flight or the user is
        # mid-edit (a re-render would clobber what they're reading).
        if not self._msg_loading and not self._search_mode:
            self._poll_fetch(self._chat_id)
        # Re-arm at the cadence that matches the current focus state.
        delay = self._POLL_ACTIVE if focused else self._POLL_IDLE
        self._poll_source = GLib.timeout_add_seconds(delay, self._poll_tick)
        return False  # we re-armed manually (cadence may have changed)

    def _poll_fetch(self, chat_id) -> None:
        def work():
            from .clients import build_account_client

            client = build_account_client(self._window.get_application(), self._account)
            return client.list_chat_messages_page(chat_id)

        run_async(work, lambda res, err: self._on_poll(chat_id, res, err))

    def _on_poll(self, chat_id, result, error) -> bool:
        if error or chat_id != self._chat_id or not result:
            return False  # transient failure / switched away — try again next tick
        messages, next_token = result
        kept = [m for m in messages if self._has_content(m)]
        if self._thread_signature(kept) == self._thread_sig:
            return False  # nothing changed — leave the thread (and scroll) alone
        self._msg_next_token = next_token
        self._cache().set(self._msg_key(chat_id), messages)
        self._render_thread(chat_id, messages)
        return False

    # -- group roster / management popover --------------------------------
    def _on_show_members(self, button) -> None:
        pop = Gtk.Popover()
        pop.set_parent(button)
        self._members_pop = pop
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8,
                      margin_top=10, margin_bottom=10, margin_start=10, margin_end=10)
        box.set_size_request(280, -1)

        # Rename the group.
        rename_row = Gtk.Box(spacing=6)
        topic = Gtk.Entry(hexpand=True, placeholder_text=_("Group name"),
                          text=self._header_title.get_title())
        rename_btn = Gtk.Button(icon_name="document-edit-symbolic",
                                tooltip_text=_("Rename"))
        rename_btn.connect("clicked", lambda *_a: self._rename_chat(topic.get_text()))
        rename_row.append(topic)
        rename_row.append(rename_btn)
        box.append(rename_row)
        box.append(Gtk.Separator())

        # Current members, each with presence + a remove button.
        roster = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        roster.add_css_class("boxed-list")
        for m in self._chat_members:
            roster.append(self._member_row(m))
        scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER,
                                    max_content_height=240, propagate_natural_height=True)
        scroll.set_child(roster)
        box.append(scroll)

        # Add someone.
        add_row = Gtk.Box(spacing=6, margin_top=4)
        add_entry = Gtk.Entry(hexpand=True, placeholder_text=_("Add by email…"))
        add_entry.connect("activate", lambda e: self._add_member(e.get_text()))
        add_btn = Gtk.Button(icon_name="list-add-symbolic", tooltip_text=_("Add"))
        add_btn.connect("clicked", lambda *_a: self._add_member(add_entry.get_text()))
        add_row.append(add_entry)
        add_row.append(add_btn)
        box.append(add_row)

        pop.set_child(box)
        pop.popup()

    def _member_row(self, m) -> Gtk.Widget:
        row = Adw.ActionRow(title=esc(m.get("name", "") or m.get("email", "")))
        avail = (self._presence.get(m.get("id")) or {}).get("availability", "")
        dot = self._presence_dot(avail)
        if dot is not None:
            dot.set_valign(Gtk.Align.CENTER)
            row.add_prefix(dot)
        if m.get("email"):
            row.set_subtitle(esc(m["email"]))
        remove = Gtk.Button(icon_name="list-remove-symbolic", valign=Gtk.Align.CENTER,
                            tooltip_text=_("Remove"))
        remove.add_css_class("flat")
        remove.connect("clicked", lambda *_a: self._remove_member(m))
        row.add_suffix(remove)
        return row

    def _rename_chat(self, topic: str) -> None:
        topic = topic.strip()
        chat_id = self._chat_id
        if not topic or chat_id is None:
            return

        def work():
            from .clients import build_account_client

            client = build_account_client(self._window.get_application(), self._account)
            client.rename_chat(chat_id, topic)
            return None

        run_async(work, lambda _r, err: self._on_group_changed(chat_id, err))
        if self._members_pop is not None:
            self._members_pop.popdown()

    def _add_member(self, email: str) -> None:
        email = email.strip()
        chat_id = self._chat_id
        if not email or chat_id is None:
            return

        def work():
            from .clients import build_account_client

            client = build_account_client(self._window.get_application(), self._account)
            client.add_chat_member(chat_id, email)
            return None

        run_async(work, lambda _r, err: self._on_group_changed(chat_id, err))
        if self._members_pop is not None:
            self._members_pop.popdown()

    def _remove_member(self, member) -> None:
        chat_id, mid = self._chat_id, member.get("membership_id")
        if chat_id is None or not mid:
            return

        def work():
            from .clients import build_account_client

            client = build_account_client(self._window.get_application(), self._account)
            client.remove_chat_member(chat_id, mid)
            return None

        run_async(work, lambda _r, err: self._on_group_changed(chat_id, err))
        if self._members_pop is not None:
            self._members_pop.popdown()

    def _on_group_changed(self, chat_id, error) -> bool:
        if error:
            self._window.add_toast(_("Couldn't update the group: %s") % error)
            return False
        # Refresh the roster + chat list (name/members may have changed).
        self._cache().invalidate(prefix=f"{self._account.id}:chat-members:{chat_id}")
        self._cache().invalidate(prefix=f"{self._account.id}:chats")
        self._load_members(chat_id)
        self._load_chats()
        if chat_id == self._chat_id:
            self._update_header(chat_id, self._chat_name)
        return False

    # -- emoji picker + text formatting -----------------------------------
    _EMOJI = ("😀", "😁", "😂", "🤣", "😊", "😍", "😘", "😎", "🤔", "😴",
              "👍", "👎", "👏", "🙏", "💪", "🔥", "🎉", "❤️", "💯", "✅",
              "👀", "🙌", "😢", "😡", "🤯", "🥳", "🚀", "⭐", "💡", "☕")

    def _on_emoji_picker(self, button) -> None:
        pop = Gtk.Popover()
        pop.set_parent(button)
        flow = Gtk.FlowBox(max_children_per_line=6, min_children_per_line=6,
                           selection_mode=Gtk.SelectionMode.NONE,
                           margin_top=6, margin_bottom=6, margin_start=6, margin_end=6)
        for emoji in self._EMOJI:
            btn = Gtk.Button(label=emoji, has_frame=False)
            btn.add_css_class("flat")
            btn.connect("clicked", lambda _b, e=emoji: (self._insert_text(e),
                                                        pop.popdown()))
            flow.append(btn)
        pop.set_child(flow)
        pop.popup()

    def _insert_text(self, text: str) -> None:
        pos = self._entry.get_position()
        self._entry.get_buffer().insert_text(pos, text, -1)
        self._entry.set_position(pos + len(text))
        self._entry.grab_focus()

    def _wrap_selection(self, marker: str) -> None:
        """Wrap the selected text (or the caret) in a markdown ``marker`` so the
        send path renders it bold/italic. With no selection, drop the markers at
        the caret and place the cursor between them."""
        buf = self._entry.get_buffer()
        bounds = self._entry.get_selection_bounds()
        if bounds:
            start, end = bounds[0], bounds[1]
            sel = self._entry.get_chars(start, end)
            buf.delete_text(start, end - start)
            wrapped = f"{marker}{sel}{marker}"
            buf.insert_text(start, wrapped, -1)
            self._entry.set_position(start + len(wrapped))
        else:
            pos = self._entry.get_position()
            buf.insert_text(pos, marker + marker, -1)
            self._entry.set_position(pos + len(marker))
        self._entry.grab_focus()

    # -- a chat thread ----------------------------------------------------
    def _on_chat_activated(self, _list, row) -> None:
        if getattr(row, "_more_chats", False):
            self._load_more_chats()
            return
        cid = getattr(row, "_chat_id", None)
        if cid is not None:
            self.open_chat(cid, getattr(row, "_chat_name", ""))

    # -- new chat ---------------------------------------------------------
    def _on_new_chat(self, _btn) -> None:
        self._open_new_chat()

    def _open_new_chat(self, body: str = "") -> None:
        from .chat_compose import ChatComposeWindow

        def create(recipients, topic, text):
            from .clients import build_account_client

            client = build_account_client(self._window.get_application(), self._account)
            if len(recipients) > 1:
                return client.start_group_chat(recipients, topic, text)
            return client.start_chat(recipients[0], text)

        ChatComposeWindow(self._window, self._account, create_fn=create,
                          on_created=self._on_chat_created, body=body).present()

    def _on_chat_created(self, chat_id) -> None:
        self._cache().invalidate(prefix=f"{self._account.id}:chats")
        self._load_chats()
        if chat_id:
            self.open_chat(chat_id)

    def open_chat(self, chat_id, name: str = "") -> None:
        if chat_id != self._chat_id:
            self._image_cache = {}  # drop the previous chat's decoded thumbnails
        self._chat_id = chat_id
        self._chat_name = name
        self._msg_next_token = None
        self._older_row = None
        self._mentions = []
        self._chat_members = []
        self._on_cancel_ctx()  # drop any reply/edit context from the previous chat
        if self._select_mode:
            self._exit_select_mode()
        self._thread_sig = None
        self._autoscroll = True  # a freshly-opened chat lands on the newest message
        self._anchor_bottom = None
        for btn in (self._entry, self._attach_btn, self._emoji_btn,
                    self._bold_btn, self._italic_btn):
            btn.set_sensitive(True)
        self._load_members(chat_id)
        self._reader.set_child(self._thread_overlay)
        self._split.set_show_content(True)
        self._update_header(chat_id, name)
        # Opening a chat clears its "new message" marks: the sidebar red badge
        # (notifier) and the per-row unread dot (grey it out locally), and tells
        # the server so the chat reads as caught-up on the user's other devices.
        notifier = getattr(self._window.get_application(), "notifier", None)
        if notifier is not None and hasattr(notifier, "mark_chat_read"):
            notifier.mark_chat_read(self._account.id, chat_id)
        self._mark_row_read(chat_id)
        self._mark_read_server(chat_id)
        row = self._rows_by_id.get(chat_id)
        if row is not None and self._list.get_selected_row() is not row:
            self._list.select_row(row)

        cached = self._cache().get(self._msg_key(chat_id))
        if cached is not None:
            self._render_thread(chat_id, cached[0])
        else:
            self._clear_thread()
            self._thread.append(Gtk.Label(label=_("Loading messages…")))
        # Always revalidate so the older-messages cursor is populated.
        self._load_messages(chat_id)
        self._start_poll()

    # -- server-side read state -------------------------------------------
    def _mark_read_server(self, chat_id) -> None:
        if self._account.provider != "microsoft":
            return  # only Teams exposes markChatReadForUser

        def work():
            from .clients import build_account_client

            client = build_account_client(self._window.get_application(), self._account)
            client.mark_chat_read(chat_id)
            return None

        run_async(work, lambda _r, _e: False)  # best-effort; ignore failures

    def _mark_row_read(self, chat_id) -> None:
        """Grey out a chat's row once opened (rebuild it without the unread mark)."""
        if chat_id in self._read_ids:
            return
        self._read_ids.add(chat_id)
        old = self._rows_by_id.get(chat_id)
        chat = next((c for c in self._all_chats if c["id"] == chat_id), None)
        if old is None or chat is None:
            return
        idx = old.get_index()
        self._list.remove(old)
        new = self._chat_row(chat)
        self._list.insert(new, idx)
        self._rows_by_id[chat_id] = new

    def _msg_key(self, chat_id) -> str:
        return f"{self._account.id}:chat-messages:{chat_id}"

    def _load_messages(self, chat_id) -> None:
        def work():
            from .clients import build_account_client

            client = build_account_client(self._window.get_application(), self._account)
            return client.list_chat_messages_page(chat_id)

        run_async(work, lambda res, error: self._on_messages(chat_id, res, error))

    def _on_messages(self, chat_id, result, error) -> bool:
        if chat_id != self._chat_id:
            return False  # switched away
        if error:
            self._clear_thread()
            self._thread.append(self._placeholder(
                "dialog-error-symbolic", _("Couldn't open chat"), error))
            return False
        messages, next_token = result
        self._msg_next_token = next_token
        self._cache().set(self._msg_key(chat_id), messages)
        self._render_thread(chat_id, messages)
        return False

    def _clear_thread(self) -> None:
        self._bubble_widgets = {}
        self._rendered_sigs = []
        self._optimistic = None
        child = self._thread.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self._thread.remove(child)
            child = nxt

    def _render_thread(self, chat_id, messages) -> None:
        if chat_id != self._chat_id:
            return
        # Skip empty/system rows so a bare timestamp never shows as its own
        # "message" — the time belongs inside a real bubble.
        messages = [m for m in messages if self._has_content(m)]
        new_sig = self._thread_signature(messages)
        if new_sig == self._thread_sig and self._bubble_widgets:
            return  # nothing visible changed — leave the thread (and scroll) alone
        # If the user has scrolled up (not pinned to the bottom), preserve their
        # position across the update instead of snapping to the newest message —
        # so a background refresh (poll), a reaction, or an edit doesn't yank the
        # view up/down. Anchor to the distance-from-bottom (restored in
        # _on_thread_resized once the new content lays out).
        adj = self._thread_scroll.get_vadjustment()
        if not self._autoscroll:
            self._anchor_bottom = adj.get_upper() - adj.get_value()
        if not messages:
            self._full_render(messages)
            self._thread.append(Gtk.Label(
                label=_("No messages yet. Say hello!"), css_classes=["dim-label"]))
            self._thread_sig = new_sig
            return
        # Fast path: the live thread is an exact prefix of the new one (only new
        # messages were appended — the common case for an incoming message). Just
        # append the new bubbles and fade them in, leaving every existing widget
        # (and its already-decoded image) untouched. No flicker, no image reload,
        # no scroll jump. Anything else (an edit, reaction, or deletion to an
        # older message) falls back to a full rebuild.
        appended = self._appended_only(messages)
        if appended is not None:
            for msg in appended:
                # Adopt the optimistic echo as its now-confirmed message: keep
                # the exact widget (and its decoded image), just flip the status
                # icon Sending→Sent and register it under the real id. No new
                # bubble, no teardown — the thread stays perfectly still.
                opt = self._optimistic
                if (opt is not None and msg.get("is_mine")
                        and (msg.get("text", "") or "").strip()
                        == (opt["text"] or "").strip()):
                    widget = opt["widget"]
                    self._set_status(widget, "sent")
                    self._optimistic = None
                else:
                    widget = self._bubble(msg)
                    self._thread.append(widget)
                    self._animate_in(widget)
                if msg.get("id"):
                    self._bubble_widgets[msg["id"]] = widget
        else:
            self._full_render(messages)
        self._rendered_sigs = [self._msg_sig(m) for m in messages]
        self._thread_sig = new_sig
        # Show the single delivery indicator on my most-recent message only.
        self._apply_status(messages)
        if self._autoscroll:
            self._scroll_to_bottom()

    def _full_render(self, messages) -> None:
        """Tear down and rebuild every bubble from scratch (used on first open
        and whenever an in-place append can't preserve the thread)."""
        self._clear_thread()
        self._older_row = None
        if not messages:
            return
        self._sync_older_row()
        for msg in messages:
            widget = self._bubble(msg)
            if msg.get("id"):
                self._bubble_widgets[msg["id"]] = widget
            self._thread.append(widget)

    def _appended_only(self, messages):
        """If the currently-rendered thread is an unchanged prefix of
        ``messages``, return just the trailing messages that need appending
        (which the caller fades in); otherwise return ``None`` to force a full
        rebuild.

        ``self._rendered_sigs`` tracks only the *confirmed* messages, so an
        un-acked optimistic echo isn't counted in the prefix. When one is
        showing we only take the fast path if the tail is exactly that one
        message now confirmed — then it's adopted in place. Any busier change
        (a second message raced in, an edit) falls back to a clean rebuild."""
        old = self._rendered_sigs
        if not old or len(messages) < len(old):
            return None
        for i, sig in enumerate(old):
            if self._msg_sig(messages[i]) != sig:
                return None
        tail = messages[len(old):]
        if self._optimistic is not None:
            opt_text = (self._optimistic["text"] or "").strip()
            if not (len(tail) == 1 and tail[0].get("is_mine")
                    and (tail[0].get("text", "") or "").strip() == opt_text):
                return None
        return tail

    @staticmethod
    def _msg_sig(m):
        """A cheap fingerprint of one message — changes when its text,
        attachments or reactions change (edited/reacted), so a stale bubble is
        detected and rebuilt."""
        return (m.get("id"), m.get("text"), len(m.get("attachments") or []),
                tuple(sorted((r.get("emoji"), r.get("count"))
                             for r in (m.get("reactions") or []))))

    @classmethod
    def _thread_signature(cls, messages):
        """A cheap fingerprint of the whole visible thread, so the adaptive poll
        only touches the bubbles when something actually changed (new/edited/
        reacted/deleted message) instead of on every tick."""
        return tuple(cls._msg_sig(m) for m in messages)

    @staticmethod
    def _has_content(msg) -> bool:
        return bool((msg.get("text", "") or "").strip()) or bool(msg.get("attachments"))

    # -- older-message pagination -----------------------------------------
    def _sync_older_row(self) -> None:
        """Show a "Load older" button at the top of the thread when a cursor
        for older messages exists."""
        if self._older_row is not None:
            self._thread.remove(self._older_row)
            self._older_row = None
        if self._msg_next_token:
            btn = Gtk.Button(
                label=_("Loading…") if self._msg_loading else _("Load older messages"),
                halign=Gtk.Align.CENTER, sensitive=not self._msg_loading)
            btn.add_css_class("flat")
            btn.connect("clicked", lambda *_a: self._load_older())
            self._older_row = btn
            self._thread.prepend(btn)

    def _load_older(self) -> None:
        token, chat_id = self._msg_next_token, self._chat_id
        if not token or self._msg_loading:
            return
        self._msg_loading = True
        self._sync_older_row()  # reflect the loading state

        def work():
            from .clients import build_account_client

            client = build_account_client(self._window.get_application(), self._account)
            return client.list_chat_messages_page(chat_id, page_token=token)

        run_async(work, lambda res, error: self._on_older(chat_id, res, error))

    def _on_older(self, chat_id, result, error) -> bool:
        self._msg_loading = False
        if error or chat_id != self._chat_id:
            self._sync_older_row()
            if error:
                self._window.add_toast(_("Couldn't load more: %s") % error)
            return False
        messages, next_token = result
        self._msg_next_token = next_token
        # Extend the cached thread (older messages go before the current ones).
        cache = self._cache()
        cached = cache.get(self._msg_key(chat_id))
        base = list(cached[0]) if cached else []
        full = messages + base
        cache.set(self._msg_key(chat_id), full)
        # Anchor the view: remember the distance from the bottom so prepending
        # older bubbles grows the thread upward without moving what's on screen,
        # and hold that anchor every frame while the new bubbles' images decode.
        adj = self._thread_scroll.get_vadjustment()
        self._autoscroll = False
        self._anchor_bottom = adj.get_upper() - adj.get_value()
        # Prepend the older bubbles above the existing ones, below the button.
        if self._older_row is not None:
            self._thread.remove(self._older_row)
            self._older_row = None
        for msg in reversed(messages):
            if self._has_content(msg):
                widget = self._bubble(msg)
                if msg.get("id"):
                    self._bubble_widgets[msg["id"]] = widget
                self._thread.insert_child_after(widget, None)
        self._sync_older_row()
        # Keep the render bookkeeping in step with what's now on screen — older +
        # existing — so the next poll/edit takes the cheap in-place path instead
        # of a full rebuild (which would reload every image and jump the view).
        shown = [m for m in full if self._has_content(m)]
        self._rendered_sigs = [self._msg_sig(m) for m in shown]
        self._thread_sig = self._thread_signature(shown)
        self._hold_position()
        return False

    # -- scroll plumbing --------------------------------------------------
    _BOTTOM_SLACK = 60  # px from the true bottom still counted as "pinned"

    def _set_scroll(self, adj, value: float) -> None:
        """Move the adjustment ourselves, flagged so ``_on_thread_scrolled``
        doesn't mistake the programmatic move for the user scrolling away."""
        self._adjusting = True
        adj.set_value(value)
        self._adjusting = False

    def _scroll_to_bottom(self, *, animate: bool = False) -> None:
        self._autoscroll = True
        self._anchor_bottom = None
        self._to_bottom_btn.set_visible(False)
        adj = self._thread_scroll.get_vadjustment()
        target = adj.get_upper() - adj.get_page_size()
        if animate and abs(target - adj.get_value()) > 4:
            self._animate_to(adj, target)
        else:
            self._set_scroll(adj, target)
        # Bubbles + images allocate *after* this call, so the value above lands
        # short of the real bottom. Hold the position every frame for a short
        # settle window (below) instead of a one-shot re-pin, so a late-decoding
        # image can't leave a one-frame "peek" that reads as a jump.
        self._hold_position()

    def _hold_position(self, duration_us: int = 350_000) -> None:
        """Re-assert the pin/anchor on *every* frame for a short settle window.

        Async growth (an image finishing decode, a font/measure pass) lands a
        frame or two after we scroll. Correcting only reactively (on the
        adjustment's ``changed``) leaves a visible one-frame shift each time —
        which, when several older messages with images load at once, reads as the
        view "jumping multiple times". Holding per-frame collapses all of that
        into a single stable position."""
        clock = self._thread_scroll.get_frame_clock()
        if clock is None:  # not realized yet — the reactive handler covers it
            return
        self._hold_until = clock.get_frame_time() + duration_us
        if self._hold_tick is not None:
            return  # already holding; we just extended the window

        def tick(_widget, frame_clock) -> bool:
            adj = self._thread_scroll.get_vadjustment()
            if self._autoscroll:
                self._set_scroll(adj, adj.get_upper() - adj.get_page_size())
            elif self._anchor_bottom is not None:
                self._set_scroll(adj, max(0, adj.get_upper() - self._anchor_bottom))
            if frame_clock.get_frame_time() >= self._hold_until:
                self._hold_tick = None
                return GLib.SOURCE_REMOVE
            return GLib.SOURCE_CONTINUE

        self._hold_tick = self._thread_scroll.add_tick_callback(tick)

    def _animate_to(self, adj, target: float) -> None:
        """Glide the thread to ``target`` with a short eased animation instead of
        a hard jump — used for the "go to latest" button."""
        if self._scroll_anim is not None:
            self._scroll_anim.pause()
        cb = Adw.CallbackAnimationTarget.new(lambda v: self._set_scroll(adj, v))
        anim = Adw.TimedAnimation.new(
            self._thread_scroll, adj.get_value(), target, 250, cb)
        anim.set_easing(Adw.Easing.EASE_OUT_CUBIC)
        self._scroll_anim = anim
        anim.play()

    def _on_thread_resized(self, adj) -> None:
        # Content (or a late-loading image) changed the height.
        if self._autoscroll:
            self._set_scroll(adj, adj.get_upper() - adj.get_page_size())
        elif self._anchor_bottom is not None:
            # Scrolled up: keep the user where they were by holding the same
            # distance from the bottom on EVERY size change (older messages
            # prepended above, late images, etc.) — so orientation never shifts.
            self._set_scroll(adj, max(0, adj.get_upper() - self._anchor_bottom))

    def _on_thread_scrolled(self, adj) -> None:
        # Any scroll input (wheel, trackpad, scrollbar drag, keyboard) lands
        # here. We ignore our own programmatic moves; for a genuine user scroll
        # we derive the pinned/scrolled-up state from the position so every input
        # method behaves identically.
        if self._adjusting:
            return
        near_bottom = (adj.get_value()
                       >= adj.get_upper() - adj.get_page_size() - self._BOTTOM_SLACK)
        self._autoscroll = near_bottom
        self._anchor_bottom = (None if near_bottom
                               else max(0, adj.get_upper() - adj.get_value()))
        self._to_bottom_btn.set_visible(not near_bottom)
        # Near the top → pull older messages automatically (no button).
        if not near_bottom and adj.get_value() <= 120:
            self._load_older()

    # -- bubble entrance animation ----------------------------------------
    def _animate_in(self, widget) -> None:
        """Fade a freshly-arrived bubble in. The CSS class carries a one-shot
        opacity animation; we drop it afterwards so a later in-place rebuild of
        the same message doesn't replay it."""
        widget.add_css_class("cloudy-bubble-new")
        GLib.timeout_add(260, lambda: (widget.remove_css_class("cloudy-bubble-new"),
                                       False)[1])

    @staticmethod
    def _status_glyph(state):
        """(icon, tooltip) for a message delivery status: a clock while sending,
        a check once sent. We don't show a 'Seen' state — the Graph chat API
        exposes no read receipt for the other party, so it can't be known
        (only Teams' private service has it); claiming it would be misleading.
        Icon names are present in both the GTK built-in resource and Adwaita."""
        return {
            "sending": ("content-loading-symbolic", _("Sending…")),
            "sent": ("object-select-symbolic", _("Sent")),
        }.get(state, ("object-select-symbolic", _("Sent")))

    def _set_status(self, outer, state: str) -> None:
        """Flip an existing bubble's status glyph in place (no rebuild)."""
        icon = getattr(outer, "_status_icon", None)
        if icon is None:
            return
        icon_name, tip = self._status_glyph(state)
        icon.set_from_icon_name(icon_name)
        icon.set_tooltip_text(tip)

    def _apply_status(self, messages) -> None:
        """Show a single delivery indicator on ONLY my most-recent message
        (Teams-style — not a check on every line): a clock while the optimistic
        echo is un-acked, a check once sent. All other status icons are hidden.
        Pure icon visibility — no rebuild."""
        # Hide every status icon first (one will be re-shown below).
        widgets = list(self._bubble_widgets.values())
        if self._optimistic is not None:
            widgets.append(self._optimistic["widget"])
        for w in widgets:
            icon = getattr(w, "_status_icon", None)
            if icon is not None:
                icon.set_visible(False)

        mine_idx = [i for i, m in enumerate(messages) if m.get("is_mine")]
        if not mine_idx:
            return
        m = messages[mine_idx[-1]]
        if not m.get("id"):  # the un-acked optimistic echo
            widget = self._optimistic["widget"] if self._optimistic else None
        else:
            widget = self._bubble_widgets.get(m["id"])
        icon = getattr(widget, "_status_icon", None)
        if icon is None:
            return
        self._set_status(widget, "sending" if not m.get("id") else "sent")
        icon.set_visible(True)

    def _bubble(self, msg) -> Gtk.Widget:
        mine = bool(msg.get("is_mine"))
        outer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        outer.set_halign(Gtk.Align.END if mine else Gtk.Align.START)
        # A vertical column holds the bubble and, below it, the reaction pills —
        # so reactions hang off the bottom of the message (Teams-style) instead
        # of sitting inside the bubble body.
        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        bubble = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        bubble.add_css_class("cloudy-bubble")
        if msg.get("id") in self._selected_msgs:
            bubble.add_css_class("cloudy-selected")
        if mine:
            bubble.add_css_class("mine")
        else:
            sender = (msg.get("from", "") or "").strip()
            if sender:
                lbl = Gtk.Label(label=sender, xalign=0, wrap=True)
                lbl.add_css_class("caption-heading")
                bubble.append(lbl)

        text = (msg.get("text", "") or "").strip()
        markup = (msg.get("markup", "") or "").strip()
        if text or markup:
            # Not selectable: a selectable GtkLabel shows its OWN right-click
            # context menu, which would pop up alongside ours. "Copy text" in our
            # menu covers copying.
            body = Gtk.Label(label=text, xalign=0, wrap=True, max_width_chars=48)
            body.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
            if markup and self._valid_markup(markup):
                body.set_markup(markup)
                body.connect("activate-link", self._on_link_activated)
            bubble.append(body)

        for att in msg.get("attachments", []) or []:
            if (att.get("content_type") or "").lower().startswith("image") and att.get("url"):
                bubble.append(self._image_widget(att))
            else:
                bubble.append(self._attachment_chip(att))

        # Footer: timestamp and, on my own messages, a delivery status glyph.
        # The icon is built once but kept hidden here — _apply_status reveals it
        # on ONLY my most-recent message (Teams shows a single indicator at the
        # bottom, not a check on every line) and picks clock/check/eye.
        outer._status_icon = None  # type: ignore[attr-defined]
        if msg.get("sent") or mine:
            foot = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6,
                           halign=Gtk.Align.END if mine else Gtk.Align.START,
                           margin_top=2, margin_start=4, margin_end=4)
            if msg.get("sent"):
                when = Gtk.Label(label=relative_time(msg["sent"]),
                                 xalign=1 if mine else 0)
                when.add_css_class("dim-label")
                when.add_css_class("caption")
                foot.append(when)
            if mine:
                sic = Gtk.Image.new_from_icon_name("object-select-symbolic")
                sic.set_pixel_size(12)
                sic.add_css_class("dim-label")
                sic.set_visible(False)
                foot.append(sic)
                outer._status_icon = sic  # type: ignore[attr-defined]
            bubble.append(foot)
        col.append(bubble)

        # Reactions as pills hanging below the bubble (not inside it), aligned to
        # the bubble's side, with a little inset so they don't touch the edge.
        reactions = msg.get("reactions") or []
        if reactions:
            rbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4,
                           halign=Gtk.Align.END if mine else Gtk.Align.START,
                           margin_start=8, margin_end=8)
            rbox.add_css_class("cloudy-reactions")
            for r in reactions:
                label = f"{r['emoji']} {r['count']}" if r.get("count", 0) > 1 else r["emoji"]
                chip = Gtk.Label(label=label)
                chip.add_css_class("cloudy-reaction")
                rbox.append(chip)
            col.append(rbox)
        outer.append(col)

        # Right-click a message → its actions popover (reactions + Reply/Forward/
        # Copy/Download/Edit/Delete). As a popover it positions itself above or
        # below the bubble, so it never covers the message text.
        menu = Gtk.GestureClick(button=Gdk.BUTTON_SECONDARY)
        menu.connect("pressed",
                     lambda _g, _n, x, y: self._show_actions(msg, bubble, x, y))
        bubble.add_controller(menu)
        # Left-click: toggles selection in select mode; Shift+click enters it.
        tap = Gtk.GestureClick(button=Gdk.BUTTON_PRIMARY)
        tap.connect("pressed", lambda g, _n, _x, _y: self._on_bubble_primary(msg, g))
        bubble.add_controller(tap)
        return outer

    # -- multi-select mode ------------------------------------------------
    def _build_select_bar(self) -> Gtk.Widget:
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6,
                      margin_top=6, margin_bottom=6, margin_start=10, margin_end=10,
                      visible=False)
        self._select_count = Gtk.Label(xalign=0, hexpand=True)
        self._select_count.add_css_class("caption-heading")
        bar.append(self._select_count)
        for icon, tip, fn, cls in (
            ("mail-forward-symbolic", _("Forward"), self._forward_selected, None),
            ("edit-copy-symbolic", _("Copy"), self._copy_selected, None),
            ("user-trash-symbolic", _("Delete"), self._delete_selected, "error"),
            ("window-close-symbolic", _("Cancel"), self._exit_select_mode, None),
        ):
            b = Gtk.Button(icon_name=icon, tooltip_text=tip)
            b.add_css_class("flat")
            if cls:
                b.add_css_class(cls)
            b.connect("clicked", lambda _b, f=fn: f())
            bar.append(b)
        return bar

    def _on_bubble_primary(self, msg, gesture) -> None:
        shift = bool(gesture.get_current_event_state() & Gdk.ModifierType.SHIFT_MASK)
        if self._select_mode:
            self._toggle_select(msg)
        elif shift:
            self._enter_select_mode(msg)

    def _enter_select_mode(self, msg=None) -> None:
        self._select_mode = True
        self._select_bar.set_visible(True)
        if msg is not None and msg.get("id"):
            self._selected_msgs[msg["id"]] = msg
            self._update_one_bubble(msg)
        self._update_select_bar()

    def _exit_select_mode(self) -> None:
        ids = list(self._selected_msgs.keys())
        self._select_mode = False
        self._selected_msgs = {}
        self._select_bar.set_visible(False)
        for mid in ids:
            self._rebuild_bubble_by_id(mid)

    def _toggle_select(self, msg) -> None:
        mid = msg.get("id")
        if not mid:
            return
        if mid in self._selected_msgs:
            del self._selected_msgs[mid]
        else:
            self._selected_msgs[mid] = msg
        self._rebuild_bubble_by_id(mid)
        if not self._selected_msgs:
            self._exit_select_mode()
        else:
            self._update_select_bar()

    def _rebuild_bubble_by_id(self, mid) -> None:
        cached = self._cache().get(self._msg_key(self._chat_id))
        msg = next((m for m in cached[0] if m.get("id") == mid), None) if cached else None
        if msg is not None:
            self._update_one_bubble(msg)

    def _update_select_bar(self) -> None:
        self._select_count.set_text(_("%d selected") % len(self._selected_msgs))

    def _selected_in_order(self) -> list:
        cached = self._cache().get(self._msg_key(self._chat_id))
        msgs = cached[0] if cached else []
        return [m for m in msgs if m.get("id") in self._selected_msgs]

    def _forward_selected(self) -> None:
        body = "\n".join(t for t in
                         ((m.get("text") or "").strip() for m in self._selected_in_order())
                         if t)
        self._exit_select_mode()
        self._open_new_chat(body=body)

    def _copy_selected(self) -> None:
        body = "\n".join(t for t in
                         ((m.get("text") or "").strip() for m in self._selected_in_order())
                         if t)
        if body:
            self.get_clipboard().set_content(Gdk.ContentProvider.new_for_value(body))
            self._window.add_toast(_("Copied"))
        self._exit_select_mode()

    def _delete_selected(self) -> None:
        mine = [m for m in self._selected_in_order() if m.get("is_mine")]
        self._exit_select_mode()
        for m in mine:
            self._delete_msg(m, None)

    # -- per-message actions popover --------------------------------------
    _QUICK_REACTIONS = ("👍", "❤️", "😆", "😮", "😢", "😠")

    def _reaction_popover(self, msg) -> Gtk.Popover:
        pop = Gtk.Popover()
        flow = Gtk.FlowBox(max_children_per_line=6, min_children_per_line=6,
                           selection_mode=Gtk.SelectionMode.NONE,
                           margin_top=6, margin_bottom=6, margin_start=6, margin_end=6)
        for emoji in self._EMOJI:
            b = Gtk.Button(label=emoji, has_frame=False)
            b.add_css_class("flat")
            b.connect("clicked",
                      lambda _b, e=emoji: (pop.popdown(), self._react(msg, e)))
            flow.append(b)
        pop.set_child(flow)
        return pop

    @staticmethod
    def _valid_markup(markup: str) -> bool:
        # GtkLabel accepts <a href> but Pango's own parser doesn't, so validate
        # with links mapped to <span> (same nesting) before trusting set_markup.
        test = re.sub(r'(?i)<a\s+href="[^"]*">', "<span>", markup)
        test = re.sub(r"(?i)</a>", "</span>", test)
        try:
            Pango.parse_markup(test, -1, "\x00")
            return True
        except GLib.GError:
            return False

    def _on_link_activated(self, _label, uri) -> bool:
        self._window.open_uri(uri)
        return True

    def _attachment_chip(self, att) -> Gtk.Widget:
        name = att.get("name", "") or _("Attachment")
        url = att.get("url", "")
        btn = Gtk.Button(halign=Gtk.Align.START)
        btn.add_css_class("flat")
        content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        content.append(Gtk.Image.new_from_icon_name("mail-attachment-symbolic"))
        content.append(Gtk.Label(label=name, ellipsize=Pango.EllipsizeMode.MIDDLE))
        btn.set_child(content)
        # Only real http(s) file links open in a browser; hosted-content URLs
        # need an auth token and are shown inline instead (see _image_widget).
        openable = bool(url) and url.startswith("http")
        btn.set_sensitive(openable)
        if openable:
            btn.connect("clicked", lambda *_a: self._window.open_uri(url))
        return btn

    _THUMB_MAX = 240  # longest edge of an inline image thumbnail (px)

    @staticmethod
    def _thumb_texture(data: bytes, max_edge: int):
        """Decode image bytes and downscale to ``max_edge`` (longest side),
        returning a small Gdk.Texture so the widget's natural size stays tiny."""
        from gi.repository import GdkPixbuf

        loader = GdkPixbuf.PixbufLoader()
        loader.write(data)
        loader.close()
        pix = loader.get_pixbuf()
        w, h = pix.get_width(), pix.get_height()
        scale = min(1.0, max_edge / w, max_edge / h)
        if scale < 1.0:
            pix = pix.scale_simple(max(1, int(w * scale)), max(1, int(h * scale)),
                                   GdkPixbuf.InterpType.BILINEAR)
        return Gdk.Texture.new_for_pixbuf(pix)

    def _image_widget(self, att) -> Gtk.Widget:
        """A small thumbnail that lazily downloads the (auth-gated) image.

        Decoded thumbnails are cached per-view by URL, so a full thread rebuild
        (reconciling an optimistic send, an edit, etc.) reuses the already-decoded
        image instantly instead of re-downloading every picture in the thread."""
        url = att["url"]
        name = self._attachment_filename(att)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)

        cached = self._image_cache.get(url)
        if cached is not None:
            texture, data = cached
            box.append(self._picture_for(texture, data, name))
            return box

        placeholder = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        placeholder.append(Gtk.Image.new_from_icon_name("image-x-generic-symbolic"))
        placeholder.append(Gtk.Label(label=_("Loading image…")))
        box.append(placeholder)

        def work():
            from .clients import build_account_client

            client = build_account_client(self._window.get_application(), self._account)
            return client.fetch_bytes(url)

        def done(data, error):
            if error or not data:
                placeholder.get_last_child().set_text(
                    _("Image unavailable") if error else _("Image"))
                placeholder.set_tooltip_text(str(error) if error else None)
                return False
            try:
                texture = self._thumb_texture(data, self._THUMB_MAX)
            except Exception as exc:  # noqa: BLE001 - undecodable payload → keep label
                placeholder.get_last_child().set_text(_("Image"))
                placeholder.set_tooltip_text(str(exc))
                return False
            self._image_cache[url] = (texture, data)
            box.remove(placeholder)
            box.append(self._picture_for(texture, data, name))
            # The image just grew the thread's height. Re-assert the scroll
            # position for a few frames: pinned-to-bottom snaps to the newest
            # message (the usual reason the view lands just short of the bottom
            # on open), and a scrolled-up/anchored view (e.g. after loading older
            # history) holds steady instead of lurching as the image lands.
            if self._autoscroll:
                self._scroll_to_bottom()
            elif self._anchor_bottom is not None:
                self._hold_position()
            return False

        run_async(work, done)
        return box

    def _picture_for(self, texture, data: bytes, name: str) -> Gtk.Picture:
        """Build a click-to-open Picture pinned to a decoded thumbnail texture."""
        # Pin the picture to the (downscaled) texture's size and DON'T let it
        # shrink — otherwise GtkPicture collapses to 0×0 inside the bubble and
        # the image is fetched but invisible ("loads then disappears").
        pic = Gtk.Picture.new_for_paintable(texture)
        pic.set_can_shrink(False)
        pic.set_halign(Gtk.Align.START)
        pic.add_css_class("cloudy-bubble-image")
        pic.set_size_request(texture.get_width(), texture.get_height())
        # Click → open the full-resolution image in a viewer (we kept the
        # original bytes, so no second download).
        pic.set_cursor(Gdk.Cursor.new_from_name("pointer", None))
        tap = Gtk.GestureClick()
        tap.connect("released", lambda *_a: self._open_image_viewer(data, name))
        pic.add_controller(tap)
        return pic

    # -- image viewer + downloads -----------------------------------------
    def _open_image_viewer(self, data: bytes, name: str = "image") -> None:
        """Open the image in its own draggable, minimizable window."""
        from .media_window import ImageWindow

        ImageWindow(self._window, data, name).present()

    @staticmethod
    def _attachment_filename(att) -> str:
        name = att.get("name") or "attachment"
        if "." not in name:
            ct = (att.get("content_type") or "").lower()
            ext = {"image": "png"}.get(ct) or (ct.split("/")[-1] if "/" in ct else "")
            if ext:
                name = f"{name}.{ext}"
        return name

    def _download_attachments(self, msg) -> None:
        for att in msg.get("attachments", []) or []:
            url = att.get("url")
            if not url:
                continue
            name = self._attachment_filename(att)
            self._window.add_toast(_("Downloading %s…") % name)

            def work(u=url):
                from .clients import build_account_client

                client = build_account_client(
                    self._window.get_application(), self._account)
                return client.fetch_bytes(u)

            run_async(work, lambda data, err, n=name: self._on_downloaded(data, err, n))

    def _on_downloaded(self, data, error, name) -> bool:
        if error or not data:
            self._window.add_toast(_("Couldn't download: %s") % (error or _("no data")))
            return False
        self._save_bytes(data, name)
        return False

    def _save_bytes(self, data: bytes, name: str, parent=None) -> None:
        dialog = Gtk.FileDialog(title=_("Save"), initial_name=name)
        folder = local_initial_folder()
        if folder is not None:
            dialog.set_initial_folder(folder)
        dialog.save(parent or self._window, None,
                    lambda d, r: self._on_save_chosen(d, r, data))

    def _on_save_chosen(self, dialog, result, data) -> None:
        try:
            gfile = dialog.save_finish(result)
        except GLib.Error:
            return  # cancelled
        if gfile is None:
            return
        try:
            gfile.replace_contents(data, None, False,
                                   Gio.FileCreateFlags.NONE, None)
            self._window.add_toast(_("Saved"))
        except GLib.Error as exc:
            self._window.add_toast(_("Couldn't save: %s") % exc.message)

    @staticmethod
    def _has_downloadable(msg) -> bool:
        return any(a.get("url") for a in (msg.get("attachments") or []))

    # -- paste an image (Ctrl+V) → stage it in the composer ---------------
    def _on_entry_key(self, _ctrl, keyval, _code, state) -> bool:
        if keyval in (Gdk.KEY_v, Gdk.KEY_V) and (state & Gdk.ModifierType.CONTROL_MASK):
            if self._try_paste_image():
                return True  # consumed; the entry won't also paste text
        return False

    def _try_paste_image(self) -> bool:
        if self._chat_id is None:
            return False
        clipboard = self.get_clipboard()
        formats = clipboard.get_formats()
        image_mimes = ("image/png", "image/jpeg", "image/bmp", "image/tiff",
                       "image/gif", "image/webp")
        has_image = (formats.contain_gtype(Gdk.Texture.__gtype__)
                     or any(formats.contain_mime_type(m) for m in image_mimes))
        if not has_image:
            return False

        def on_texture(clip, result):
            try:
                texture = clip.read_texture_finish(result)
            except Exception:  # noqa: BLE001 - not actually an image; ignore
                return
            if texture is not None:
                self._stage_image(texture)

        clipboard.read_texture_async(None, on_texture)
        return True

    # -- attach an image from a file --------------------------------------
    def _on_attach(self, _btn) -> None:
        if self._chat_id is None:
            return
        dialog = Gtk.FileDialog(title=_("Attach image"))
        filt = Gtk.FileFilter()
        filt.set_name(_("Images"))
        filt.add_pixbuf_formats()
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(filt)
        dialog.set_filters(filters)
        dialog.set_default_filter(filt)
        folder = local_initial_folder()
        if folder is not None:
            dialog.set_initial_folder(folder)
        dialog.open(self._window, None, self._on_attach_chosen)

    def _on_attach_chosen(self, dialog, result) -> None:
        try:
            gfile = dialog.open_finish(result)
        except GLib.Error:
            return  # cancelled
        if gfile is None:
            return
        try:
            ok, data, _etag = gfile.load_contents(None)
            if not ok:
                return
            ctype = "image/png"
            info = gfile.query_info("standard::content-type", 0, None)
            if info and info.get_content_type():
                ctype = info.get_content_type()
        except GLib.Error as exc:
            self._window.add_toast(_("Couldn't read file: %s") % exc.message)
            return
        self._stage_bytes(bytes(data), ctype)

    def _stage_image(self, texture) -> None:
        self._stage_bytes(texture.save_to_png_bytes().get_data(), "image/png")

    def _stage_bytes(self, data, ctype: str) -> None:
        """Stage an image (paste or file) in the composer; sent on Send.

        Full-resolution bytes are kept for sending; the preview is a small
        thumbnail (a Picture's natural size = its paintable's, so scale the
        paintable, not just set_size_request)."""
        thumb = Gtk.Overlay(valign=Gtk.Align.CENTER)
        try:
            preview = self._thumb_texture(data, 64)
        except Exception:  # noqa: BLE001 - not a decodable image
            self._window.add_toast(_("That file isn't a supported image."))
            return
        pic = Gtk.Picture.new_for_paintable(preview)
        pic.set_can_shrink(True)
        pic.add_css_class("cloudy-bubble-image")
        thumb.set_child(pic)
        remove = Gtk.Button(icon_name="window-close-symbolic",
                            halign=Gtk.Align.END, valign=Gtk.Align.START)
        remove.add_css_class("circular")
        remove.add_css_class("osd")
        entry = {"data": data, "ctype": ctype or "image/png", "widget": thumb}
        remove.connect("clicked", lambda *_a: self._remove_pending(entry))
        thumb.add_overlay(remove)
        self._pending.append(entry)
        self._preview.append(thumb)
        self._preview.set_visible(True)

    def _remove_pending(self, entry) -> None:
        if entry in self._pending:
            self._pending.remove(entry)
        self._preview.remove(entry["widget"])
        self._preview.set_visible(bool(self._pending))

    def _clear_pending(self) -> None:
        for entry in self._pending:
            self._preview.remove(entry["widget"])
        self._pending = []
        self._preview.set_visible(False)

    # -- reply / edit context bar ----------------------------------------
    def _set_reply(self, msg) -> None:
        self._editing = None
        self._reply_to = msg
        if msg is None:
            self._reply_bar.set_visible(False)
            return
        who = _("You") if msg.get("is_mine") else (msg.get("from") or _("someone"))
        snippet = (msg.get("text") or _("(image)")).replace("\n", " ")[:80]
        self._reply_lbl.set_text(_("Replying to %(who)s: %(text)s")
                                 % {"who": who, "text": snippet})
        self._reply_bar.set_visible(True)
        self._entry.grab_focus()

    def _start_edit(self, msg) -> None:
        self._reply_to = None
        self._editing = msg
        self._reply_lbl.set_text(_("Editing message — press Enter to save"))
        self._reply_bar.set_visible(True)
        self._entry.set_text(msg.get("text") or "")
        self._entry.set_position(-1)
        self._entry.grab_focus()

    def _on_cancel_ctx(self) -> None:
        if self._editing is not None:
            self._entry.set_text("")
        self._editing = None
        self._reply_to = None
        self._reply_bar.set_visible(False)

    # -- @mentions --------------------------------------------------------
    def _load_members(self, chat_id) -> None:
        key = f"{self._account.id}:chat-members:{chat_id}"
        cached = self._cache().get(key)
        if cached is not None:
            self._chat_members = cached[0]

        def work():
            from .clients import build_account_client

            client = build_account_client(self._window.get_application(), self._account)
            return client.list_chat_members(chat_id)

        run_async(work, lambda res, err: self._on_members(chat_id, key, res, err))

    def _on_members(self, chat_id, key, members, error) -> bool:
        if error or members is None:
            return False
        self._cache().set(key, members)
        if chat_id == self._chat_id:
            self._chat_members = members
            self._update_header(chat_id, self._chat_name)
            # Pull presence for everyone in this chat (group rosters include
            # people not covered by the 1:1 chat-list presence batch).
            ids = [m["id"] for m in members if m.get("id")]
            if ids and self._account.provider == "microsoft":
                def work():
                    from .clients import build_account_client

                    client = build_account_client(
                        self._window.get_application(), self._account)
                    return client.get_presences(ids)

                run_async(work, self._on_presence)
        return False

    def _on_entry_changed(self, entry) -> None:
        text, pos = entry.get_text(), entry.get_position()
        before = text[:pos]
        at = before.rfind("@")
        token = None
        if at >= 0 and (at == 0 or before[at - 1] in " \n"):
            frag = before[at + 1:]
            if " " not in frag:
                token = frag
        if token is None or not self._chat_members:
            self._hide_mentions()
            return
        matches = [m for m in self._chat_members
                   if token.lower() in m["name"].lower()][:6]
        if matches:
            self._show_mentions(matches, at)
        else:
            self._hide_mentions()

    def _show_mentions(self, matches, at_index) -> None:
        if self._mention_pop is None:
            self._mention_pop = Gtk.Popover(has_arrow=False, autohide=False)
            self._mention_pop.set_parent(self._entry)
            self._mention_pop.set_position(Gtk.PositionType.TOP)
            self._mention_list = Gtk.ListBox()
            self._mention_list.add_css_class("menu")
            self._mention_list.connect("row-activated", self._on_mention_selected)
            self._mention_pop.set_child(self._mention_list)
        clear_listbox(self._mention_list)
        self._mention_at = at_index
        for m in matches:
            row = Gtk.ListBoxRow()
            row._member = m
            row.set_child(Gtk.Label(label=m["name"], xalign=0, margin_top=4,
                                    margin_bottom=4, margin_start=8, margin_end=8))
            self._mention_list.append(row)
        self._mention_pop.popup()

    def _hide_mentions(self) -> None:
        if self._mention_pop is not None:
            self._mention_pop.popdown()

    def _on_mention_selected(self, _list, row) -> None:
        m = row._member
        text, pos = self._entry.get_text(), self._entry.get_position()
        new = text[:self._mention_at] + "@" + m["name"] + " " + text[pos:]
        if not any(x["id"] == m["id"] for x in self._mentions):
            self._mentions.append(m)
        self._hide_mentions()
        self._entry.set_text(new)
        self._entry.set_position(self._mention_at + len(m["name"]) + 2)
        self._entry.grab_focus()

    # -- server-side message search --------------------------------------
    def _on_search_results(self, query, results, error) -> bool:
        # Ignore stale responses (the user kept typing) or a cleared search.
        if not self._search_mode or query.lower() != self._query:
            return False
        clear_listbox(self._list)
        self._rows_by_id = {}
        # Matching loaded chats first (find a conversation by participant name),
        # then the server's message hits (find content in any chat).
        matches = [c for c in self._all_chats
                   if self._query in (c.get("name", "") or "").lower()]
        for chat in matches:
            row = self._chat_row(chat)
            self._list.append(row)
            self._rows_by_id[chat["id"]] = row
        if error:
            self._list.append(self._section_row(_("Message search failed")))
            return False
        hits = results or []
        if matches and hits:
            self._list.append(self._section_row(_("Messages")))
        for hit in hits:
            self._list.append(self._search_row(hit))
        if not matches and not hits:
            self._set_list_message(_("Nothing matches “%s”.") % query)
        return False

    @staticmethod
    def _section_row(text: str) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow(activatable=False, selectable=False)
        lbl = Gtk.Label(label=text, xalign=0, margin_top=8, margin_bottom=4,
                        margin_start=12, margin_end=12)
        lbl.add_css_class("dim-label")
        lbl.add_css_class("caption-heading")
        row.set_child(lbl)
        return row

    def _search_row(self, hit) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow(activatable=True)
        row._chat_id = hit.get("chat_id")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2,
                      margin_top=8, margin_bottom=8, margin_start=12, margin_end=12)
        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        name = Gtk.Label(label=hit.get("from") or _("Message"), xalign=0,
                         hexpand=True, ellipsize=Pango.EllipsizeMode.END)
        name.add_css_class("heading")
        top.append(name)
        if hit.get("sent"):
            when = Gtk.Label(label=relative_time(hit["sent"]), xalign=1)
            when.add_css_class("dim-label")
            when.add_css_class("caption")
            top.append(when)
        box.append(top)
        snippet = (hit.get("snippet") or "").replace("\n", " ").strip()[:120]
        if snippet:
            snip = Gtk.Label(label=snippet, xalign=0,
                             ellipsize=Pango.EllipsizeMode.END)
            snip.add_css_class("dim-label")
            snip.add_css_class("caption")
            box.append(snip)
        row.set_child(box)
        return row

    # -- composer ---------------------------------------------------------
    def _on_send(self, *_args) -> None:
        chat_id = self._chat_id
        if chat_id is None:
            return
        text = self._entry.get_text().strip()

        if self._editing is not None:  # edit mode → PATCH the existing message
            mid = self._editing.get("id")
            self._on_cancel_ctx()
            if not text or not mid:
                return

            def work_edit():
                from .clients import build_account_client

                client = build_account_client(self._window.get_application(), self._account)
                return client.edit_chat_message(chat_id, mid, text)

            run_async(work_edit, lambda _r, error: self._on_sent(chat_id, error))
            return

        images = [(e["data"], e["ctype"]) for e in self._pending]
        mentions = list(self._mentions)
        if not text and not images:
            return
        if self._reply_to is not None:  # prepend a quote of the replied message
            quoted = (self._reply_to.get("text") or _("(image)")).replace("\n", " ")[:200]
            text = f"“{quoted}”\n{text}".strip()
        self._entry.set_text("")
        self._clear_pending()
        self._mentions = []
        self._set_reply(None)
        # Rich (HTML) send when there are images, @mentions, or markdown
        # formatting (**bold** / _italic_); otherwise a cheap plain-text send.
        rich = bool(images or mentions or self._has_markdown(text))

        def work():
            from .clients import build_account_client

            client = build_account_client(self._window.get_application(), self._account)
            if rich:
                content, mention_array = self._compose_html(text, mentions)
                return client.send_chat_html(
                    chat_id, f"<div>{content}</div>", mention_array, images)
            return client.send_chat_message(chat_id, text)

        if not rich:  # optimistic echo for plain text
            self._render_pending(chat_id, text)
        else:
            self._window.add_toast(_("Sending…"))
        run_async(work, lambda _r, error: self._on_message_sent(chat_id, error))

    _MD_BOLD = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
    _MD_ITALIC = re.compile(r"_(.+?)_", re.DOTALL)

    @classmethod
    def _has_markdown(cls, text: str) -> bool:
        return bool(cls._MD_BOLD.search(text) or cls._MD_ITALIC.search(text))

    @classmethod
    def _compose_html(cls, text, mentions):
        """Escape ``text``, wrap each picked '@Name' in an <at> tag (building the
        Graph mentions array), and render **bold** / _italic_ markdown."""
        content = html.escape(text)
        out = []
        for m in mentions:
            token = "@" + html.escape(m["name"])
            if token in content:
                idx = len(out)
                content = content.replace(
                    token, f'<at id="{idx}">{html.escape(m["name"])}</at>', 1)
                out.append({
                    "id": idx, "mentionText": m["name"],
                    "mentioned": {"user": {
                        "id": m["id"], "displayName": m["name"],
                        "userIdentityType": "aadUser"}},
                })
        content = cls._MD_BOLD.sub(r"<b>\1</b>", content)
        content = cls._MD_ITALIC.sub(r"<i>\1</i>", content)
        return content.replace("\n", "<br>"), out

    def _render_pending(self, chat_id, text) -> None:
        if chat_id != self._chat_id or not text:
            return
        first = self._thread.get_first_child()
        if isinstance(first, Gtk.Label):  # clear the "no messages" hint
            self._clear_thread()
        # Stamp "now" so the optimistic bubble shows its time straight away — the
        # authoritative reload then replaces it with the same time, so nothing
        # visibly "pops in" later.
        from datetime import datetime

        now_iso = datetime.now().astimezone().isoformat()
        bubble = self._bubble(
            {"text": text, "is_mine": True, "sent": now_iso, "from": ""})
        self._thread.append(bubble)
        self._animate_in(bubble)
        # Remember this echo so the authoritative reload adopts THIS widget in
        # place (status flips Sending→Sent) rather than rebuilding the thread.
        self._optimistic = {"widget": bubble, "text": text}
        self._scroll_to_bottom()

    def _on_sent(self, chat_id, error) -> bool:
        if error:
            self._window.add_toast(_("Couldn't send: %s") % error)
            return False
        self._cache().invalidate(prefix=self._msg_key(chat_id))
        if chat_id == self._chat_id:
            self._load_messages(chat_id)  # pull the authoritative thread
        return False

    def _on_message_sent(self, chat_id, error) -> bool:
        """After sending a NEW message: don't reload immediately — the server
        often hasn't indexed it yet, so a reload would drop the optimistic bubble
        and jump the view up. The optimistic echo already shows it; reconcile a
        moment later (server has indexed it by then) via the scroll-preserving
        poll path."""
        if error:
            self._window.add_toast(_("Couldn't send: %s") % error)
            self._cache().invalidate(prefix=self._msg_key(chat_id))
            if chat_id == self._chat_id:
                self._load_messages(chat_id)  # drop the failed optimistic echo
            return False

        def reconcile() -> bool:
            self._poll_fetch(chat_id)  # _on_poll updates the cache + re-renders
            return False

        GLib.timeout_add(1500, reconcile)
        return False

    # -- per-message actions popover --------------------------------------
    def _show_actions(self, msg, anchor, x, y) -> None:
        """Pop up the actions menu (quick reactions + Reply/Forward/Copy/…) at
        the click point. As a popover it sits outside the bubble, so it never
        covers the message text."""
        pop = Gtk.Popover(has_arrow=True)
        pop.add_css_class("menu")
        pop.set_parent(anchor)
        rect = Gdk.Rectangle()
        rect.x, rect.y, rect.width, rect.height = int(x), int(y), 1, 1
        pop.set_pointing_to(rect)
        pop.set_child(self._actions_box(msg, pop))
        # Unparent on close, but only if the anchor bubble still exists — a poll
        # re-render can destroy it first (which otherwise trips a GTK assertion).
        pop.connect("closed", lambda p: p.unparent() if p.get_parent() else None)
        pop.popup()

    def _actions_box(self, msg, pop) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2,
                      margin_top=6, margin_bottom=6, margin_start=6, margin_end=6)

        # Two quick reactions + a picker button that opens the full set.
        react = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2,
                        margin_bottom=2)
        for emoji in ("👍", "❤️"):
            rb = Gtk.Button(label=emoji, has_frame=False)
            rb.add_css_class("flat")
            rb.connect("clicked",
                       lambda _b, e=emoji: (pop.popdown(), self._react(msg, e)))
            react.append(rb)
        more_e = Gtk.MenuButton(icon_name="face-smile-symbolic",
                                tooltip_text=_("More emoji"))
        more_e.add_css_class("flat")
        more_e.set_popover(self._reaction_popover(msg))
        react.append(more_e)
        box.append(react)
        box.append(Gtk.Separator(margin_top=2, margin_bottom=2))

        def item(icon, label, fn, *, destructive=False):
            btn = Gtk.Button(has_frame=False)
            btn.add_css_class("flat")
            if destructive:
                btn.add_css_class("error")
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            row.append(Gtk.Image.new_from_icon_name(icon))
            row.append(Gtk.Label(label=label, xalign=0, hexpand=True))
            btn.set_child(row)
            btn.connect("clicked", lambda *_a: (pop.popdown(), fn()))
            box.append(btn)

        item("mail-reply-sender-symbolic", _("Reply"), lambda: self._set_reply(msg))
        item("mail-forward-symbolic", _("Forward"), lambda: self._forward(msg))
        item("edit-copy-symbolic", _("Copy text"), lambda: self._copy_text(msg))
        item("selection-mode-symbolic", _("Select"),
             lambda: self._enter_select_mode(msg))
        if self._has_downloadable(msg):
            item("document-save-symbolic", _("Download"),
                 lambda: self._download_attachments(msg))
        if msg.get("web_url"):
            item("insert-link-symbolic", _("Copy link"), lambda: self._copy_link(msg))
        if msg.get("is_mine"):
            box.append(Gtk.Separator(margin_top=2, margin_bottom=2))
            item("document-edit-symbolic", _("Edit"), lambda: self._start_edit(msg))
            item("user-trash-symbolic", _("Delete"),
                 lambda: self._delete_msg(msg, None), destructive=True)
        return box

    def _copy_link(self, msg) -> None:
        url = (msg.get("web_url") or "").strip()
        if url:
            self.get_clipboard().set_content(Gdk.ContentProvider.new_for_value(url))
            self._window.add_toast(_("Link copied"))

    def _react(self, msg, emoji) -> None:
        chat_id, mid = self._chat_id, msg.get("id")
        if not mid:
            return
        # Optimistic: show the reaction immediately (preserving scroll), then
        # send it. The adaptive poll reconciles the authoritative count; a full
        # reload here would scroll the thread back to the bottom.
        self._add_local_reaction(chat_id, mid, emoji)

        def work():
            from .clients import build_account_client

            client = build_account_client(self._window.get_application(), self._account)
            return client.set_reaction(chat_id, mid, emoji)

        run_async(work, lambda _r, error: self._on_react_done(chat_id, error))

    def _add_local_reaction(self, chat_id, mid, emoji) -> None:
        cache = self._cache()
        cached = cache.get(self._msg_key(chat_id))
        if not cached:
            return
        messages = cached[0]
        target = None
        for m in messages:
            if m.get("id") == mid:
                reactions = m.setdefault("reactions", [])
                hit = next((r for r in reactions if r.get("emoji") == emoji), None)
                if hit:
                    hit["count"] = hit.get("count", 1) + 1
                else:
                    reactions.append({"emoji": emoji, "count": 1})
                target = m
                break
        cache.set(self._msg_key(chat_id), messages)
        # Rebuild ONLY that bubble in place — re-rendering the whole thread would
        # reorder nothing but reload every image and disturb the scroll position.
        if chat_id == self._chat_id and target is not None:
            self._update_one_bubble(target)

    def _update_one_bubble(self, msg) -> None:
        """Swap a single message's bubble for a freshly-built one at the same
        position, leaving the rest of the thread (and the scroll) untouched."""
        mid = msg.get("id")
        old = self._bubble_widgets.get(mid)
        if old is None or old.get_parent() is not self._thread:
            return
        prev = old.get_prev_sibling()
        new = self._bubble(msg)
        self._bubble_widgets[mid] = new
        self._thread.remove(old)
        self._thread.insert_child_after(new, prev)  # prev=None → prepend

    def _on_react_done(self, chat_id, error) -> bool:
        if error:
            self._window.add_toast(_("Couldn't react: %s") % error)
            # Roll back the optimistic reaction by pulling the real thread.
            self._cache().invalidate(prefix=self._msg_key(chat_id))
            if chat_id == self._chat_id:
                self._load_messages(chat_id)
        return False

    def _copy_text(self, msg) -> None:
        text = (msg.get("text") or "").strip()
        if text:
            self.get_clipboard().set_content(Gdk.ContentProvider.new_for_value(text))
            self._window.add_toast(_("Copied"))

    def _forward(self, msg) -> None:
        self._open_new_chat(body=(msg.get("text") or ""))

    def _delete_msg(self, msg, bubble) -> None:
        chat_id, mid = self._chat_id, msg.get("id")
        if not mid:
            return
        if bubble is not None:  # optimistic
            self._thread.remove(bubble)

        def work():
            from .clients import build_account_client

            client = build_account_client(self._window.get_application(), self._account)
            return client.delete_chat_message(chat_id, mid)

        run_async(work, lambda _r, error: self._on_sent(chat_id, error))
