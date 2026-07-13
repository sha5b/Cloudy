# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Shahab Nedaei
"""Mail surface: a two-pane reader.

Left pane = a folder switcher + an email-style message list (plain ``Gtk.Label``s
so '&', '<' etc. are safe). Right pane = the selected message, rendered as real
HTML (see ``message_view``). Folders come from ``client.list_folders()``; each
folder's messages are cached independently (stale-while-revalidate).
"""

from __future__ import annotations

import re
from gettext import gettext as _

from gi.repository import Adw, Gdk, GLib, Gtk, Pango

from .format import esc, sender_name, short_time
from .source_nav import (
    SCOPE_HINT,
    SourceTabs,
    action_row,
    clear_listbox,
    data_rows,
    friendly_error,
    invalidate_cached,
    move_selection,
    is_pinned,
    is_scope_error,
    message_row,
    patch_listbox,
    present_add_shared_dialog,
    run_async,
    toggle_pin,
)

_WS_RE = re.compile(r"\s+")


def _oneline(text: str) -> str:
    """Collapse every run of whitespace (incl. \\r and unicode breaks) to one
    space so list labels never wrap to multiple lines."""
    return _WS_RE.sub(" ", text or "").strip()


class MailView(Adw.Bin):
    __gtype_name__ = "CloudyMailView"

    def __init__(self, window, account):
        super().__init__()
        self._window = window
        self._account = account
        # Work/school Microsoft accounts get three sources: Me / Teams / Shared.
        # Google and *personal* Microsoft accounts (no Teams/SharePoint/shared
        # mailboxes) only have their own mailbox ("me").
        self._is_ms = account.provider == "microsoft" and not account.is_personal
        self._source = "me"
        self._inbox_id = "INBOX" if account.provider == "google" else "inbox"
        # Start on the remembered folder for this account (survives account
        # switches), else the Inbox.
        self._folder_id = window.last_mail_folder(account.id) or self._inbox_id
        self._me_folders: list[dict] = []
        self._teams: list[dict] = []          # [{id, name}] (raw group ids)
        self._shared_folders: dict = {}        # address -> [folders]
        self._suppress = False
        self._open_mid = None
        self._messages_by_id: dict = {}
        self._rows_by_id: dict = {}
        self._next_token = None   # pagination cursor for the active folder
        self._more_row = None     # the "Load older" list row, when present
        self._loading_more = False
        self._query = ""          # live filter over loaded messages (type-ahead)
        self._search_query = ""   # committed server-side search (Enter); "" = browse

        # -- left pane: source tabs + context/folder dropdowns + list ----
        self._ctx_dd = Gtk.DropDown(model=Gtk.StringList.new([]), tooltip_text=_("Choose"))
        self._ctx_dd.add_css_class("flat")
        self._ctx_dd.set_hexpand(True)
        self._ctx_dd.connect("notify::selected", self._on_ctx_changed)
        self._folder_dd = Gtk.DropDown(
            model=Gtk.StringList.new([_("Inbox")]), tooltip_text=_("Choose a folder"))
        self._folder_dd.add_css_class("flat")
        self._folder_dd.set_hexpand(True)
        self._folder_dd.connect("notify::selected", self._on_folder_changed)
        self._add_shared_btn = Gtk.Button(
            icon_name="list-add-symbolic", tooltip_text=_("Add a shared mailbox"))
        self._add_shared_btn.add_css_class("flat")
        self._add_shared_btn.connect("clicked", self._on_add_shared)
        self._star_btn = Gtk.Button(
            icon_name="non-starred-symbolic",
            tooltip_text=_("Pin this mailbox to the Dashboard"))
        self._star_btn.add_css_class("flat")
        self._star_btn.connect("clicked", self._on_star_clicked)
        self._ctx_current = None  # {"id", "name"} of the selected team/shared source

        self._list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.MULTIPLE,
                                 valign=Gtk.Align.START)
        self._list.add_css_class("navigation-sidebar")
        self._list.connect("row-activated", self._on_row_activated)
        # Outlook-style: ↑/↓ and ←/→ move + open in the reader; Shift+arrow or
        # Ctrl/Shift-click multi-selects; Delete trashes the selection; Ctrl+R
        # replies, Ctrl+N composes.
        self._list.connect("selected-rows-changed", self._on_selection_changed)
        self._list.set_filter_func(self._filter_row)
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_list_key)
        self._list.add_controller(keys)
        # Double-click a message → pop it out into its own window (like a new
        # mail), so it can be read alongside the list. A passive observer (no
        # state claim) so single-click selection/activation is unaffected.
        dbl = Gtk.GestureClick(button=Gdk.BUTTON_PRIMARY)
        dbl.connect("pressed", self._on_list_double)
        self._list.add_controller(dbl)
        # Right-click a message → context menu (unread / flag / move / trash).
        ctx = Gtk.GestureClick(button=Gdk.BUTTON_SECONDARY)
        ctx.connect("pressed", self._on_list_context)
        self._list.add_controller(ctx)
        list_scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER,
                                         vexpand=True)
        list_scroll.set_child(self._list)
        # A plain click should select exactly one message (and clicking empty
        # space deselects); only Shift/Ctrl multi-selects. GtkListBox MULTIPLE
        # mode otherwise lets clicks accumulate, so on a bare primary press we
        # clear the selection (capture phase, before the listbox re-selects the
        # clicked row). A GestureClick only fires on button press — NOT on every
        # motion/scroll event — and DENIED lets selection proceed normally.
        click_capture = Gtk.GestureClick(button=Gdk.BUTTON_PRIMARY)
        click_capture.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        click_capture.connect("pressed", self._on_list_pressed)
        list_scroll.add_controller(click_capture)
        # Auto-load older messages when scrolled near the end (no button press).
        list_scroll.get_vadjustment().connect("value-changed", self._on_list_scrolled)
        # Floating "go to latest" button (newest mail is at the top), shown only
        # once the list is scrolled down a screenful.
        self._to_top_btn = Gtk.Button(
            icon_name="go-top-symbolic", tooltip_text=_("Go to latest"),
            halign=Gtk.Align.END, valign=Gtk.Align.END,
            margin_end=14, margin_bottom=14, visible=False)
        self._to_top_btn.add_css_class("circular")
        self._to_top_btn.add_css_class("osd")
        self._to_top_btn.connect("clicked", self._on_go_latest)
        self._list_overlay = Gtk.Overlay()
        self._list_overlay.set_child(list_scroll)
        self._list_overlay.add_overlay(self._to_top_btn)
        self._list_scroll = list_scroll

        compose_btn = Gtk.Button(
            icon_name="mail-message-new-symbolic", tooltip_text=_("New message"))
        compose_btn.connect("clicked", self._on_compose_clicked)

        sidebar_tb = Adw.ToolbarView()
        if self._is_ms:
            tabs = SourceTabs(self._on_source_changed)
            header = Adw.HeaderBar(
                show_start_title_buttons=False, show_end_title_buttons=False,
                title_widget=tabs)
            header.pack_start(compose_btn)
            sidebar_tb.add_top_bar(header)
            bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6,
                          margin_top=6, margin_bottom=6, margin_start=10, margin_end=10)
            bar.append(self._ctx_dd)
            bar.append(self._folder_dd)
            bar.append(self._star_btn)
            bar.append(self._add_shared_btn)
            sidebar_tb.add_top_bar(bar)
        else:
            # An Adw.HeaderBar centres its title widget and caps it at the
            # natural width, so a dropdown placed there never spans the column.
            # Mirror the Microsoft layout: compose in the header, and the folder
            # dropdown in its own full-width bar below.
            header = Adw.HeaderBar(
                show_start_title_buttons=False, show_end_title_buttons=False)
            header.pack_start(compose_btn)
            sidebar_tb.add_top_bar(header)
            bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6,
                          margin_top=6, margin_bottom=6, margin_start=10, margin_end=10)
            bar.append(self._folder_dd)
            sidebar_tb.add_top_bar(bar)
        self._search = Gtk.SearchEntry(
            placeholder_text=_("Filter — press Enter to search the server"),
            hexpand=True)
        self._search.connect("search-changed", self._on_search_changed)
        # Enter searches the whole folder server-side (finds mail not yet loaded).
        self._search.connect("activate", self._on_search_activate)
        search_bar = Gtk.Box(margin_top=6, margin_bottom=6,
                            margin_start=10, margin_end=10)
        search_bar.append(self._search)
        sidebar_tb.add_top_bar(search_bar)
        sidebar_tb.set_content(self._list_overlay)
        sidebar_page = Adw.NavigationPage(title=_("Mail"), tag="messages")
        sidebar_page.set_child(sidebar_tb)

        # -- right pane: the reading area --------------------------------
        self._reader = Adw.Bin()
        self._reader.set_child(self._reader_placeholder(
            "mail-unread-symbolic", _("No message selected"),
            _("Pick an email from the list to read it here."),
        ))
        content_header = Adw.HeaderBar(
            show_start_title_buttons=False, show_end_title_buttons=False,
        )
        self._delete_btn = Gtk.Button(
            icon_name="user-trash-symbolic", tooltip_text=_("Move to Trash"),
            sensitive=False,
        )
        self._delete_btn.connect("clicked", self._on_delete_clicked)
        content_header.pack_end(self._delete_btn)
        self._reply_btn = Gtk.Button(
            icon_name="mail-reply-sender-symbolic", tooltip_text=_("Reply"),
            sensitive=False,
        )
        self._reply_btn.connect("clicked", self._on_reply_clicked)
        content_header.pack_start(self._reply_btn)
        self._reply_all_btn = Gtk.Button(
            icon_name="mail-reply-all-symbolic", tooltip_text=_("Reply all"),
            sensitive=False,
        )
        self._reply_all_btn.connect("clicked", self._on_reply_all_clicked)
        content_header.pack_start(self._reply_all_btn)
        self._forward_btn = Gtk.Button(
            icon_name="mail-forward-symbolic", tooltip_text=_("Forward"),
            sensitive=False,
        )
        self._forward_btn.connect("clicked", self._on_forward_clicked)
        content_header.pack_start(self._forward_btn)
        content_tb = Adw.ToolbarView()
        content_tb.add_top_bar(content_header)
        content_tb.set_content(self._reader)
        content_page = Adw.NavigationPage(title=_("Message"), tag="reader")
        content_page.set_child(content_tb)

        self._split = Adw.NavigationSplitView(
            min_sidebar_width=300, max_sidebar_width=460, sidebar_width_fraction=0.36,
        )
        self._split.set_sidebar(sidebar_page)
        self._split.set_content(content_page)
        self.set_child(self._split)

        self._ctx_items: list = []
        self._folder_items: list = []
        self._has_data = False
        self._update_source_ui()
        self._show_cached_or_placeholder()
        self._load_async()
        self._load_folders_async()

    # -- cache key per folder --------------------------------------------
    def _cache_key(self) -> str:
        return f"{self._account.id}:messages:{self._folder_id}"

    def _show_cached_or_placeholder(self) -> bool:
        """Render cached messages if any; return True if they were fresh."""
        self._has_data = False
        self._next_token = None  # unknown until a live fetch returns the cursor
        cached = self._window.get_application().cache.get(self._cache_key())
        if cached is not None:
            self._render(cached[0])  # show cached instantly
            return bool(cached[1])  # fresh enough → caller may skip the fetch
        self._set_placeholder(_("Loading mail…"))
        return False

    # -- helpers ----------------------------------------------------------
    def _clear(self) -> None:
        clear_listbox(self._list)

    def _set_placeholder(self, text: str) -> None:
        self._clear()
        self._list.append(message_row(text))

    def _reauth_prompt(self) -> None:
        """Show the re-sign-in call-to-action (token lacks the shared scope)."""
        self._clear()
        self._list.append(action_row(
            SCOPE_HINT, _("Re-sign in"),
            lambda: self._window.sign_in_account(self._account)))

    def _reader_placeholder(self, icon: str, title: str, description: str) -> Gtk.Widget:
        # StatusPage parses title/description as Pango markup; escape since one
        # caller passes a raw API error string (may contain < or &).
        return Adw.StatusPage(icon_name=icon, title=esc(title),
                              description=esc(description))

    def _reader_loading(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                      halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER,
                      hexpand=True, vexpand=True)
        spinner = Gtk.Spinner(width_request=32, height_request=32)
        spinner.start()
        box.append(spinner)
        label = Gtk.Label(label=_("Opening…"))
        label.add_css_class("dim-label")
        box.append(label)
        return box

    # -- sources (Me / Teams / Shared) -----------------------------------
    def _shared_addresses(self) -> list:
        return list(self._account.shared_mailboxes or [])

    @staticmethod
    def _label(f) -> str:
        unread = f.get("unread", 0)
        return f"{f['name']} ({unread})" if unread else f["name"]

    def _load_folders_async(self) -> None:
        """Load the Me folders and (for Microsoft) the Teams list up front."""
        def work():
            from .clients import build_account_client

            client = build_account_client(self._window.get_application(), self._account)
            try:
                folders = client.list_folders()
            except Exception:  # noqa: BLE001
                folders = []
            teams = []
            if self._is_ms and hasattr(client, "list_groups"):
                try:
                    teams = client.list_groups()
                except Exception:  # noqa: BLE001 - needs Group.Read.All consent
                    teams = []
            return folders, teams

        # On a hard failure (e.g. no client) fall back to empty sources.
        run_async(work, lambda res, _error: self._on_sources_loaded(*(res or ([], []))))

    def _on_sources_loaded(self, folders, teams) -> bool:
        self._me_folders = folders
        self._teams = teams
        # If we're showing the source the data belongs to, refresh its dropdowns.
        if self._source == "me":
            self._populate_folders(self._me_folders, initial=True)
        elif self._source == "teams":
            self._populate_context()
        return False

    def _update_source_ui(self) -> None:
        """Show the right dropdowns/buttons for the active source."""
        if not self._is_ms:
            return
        self._ctx_dd.set_visible(self._source in ("teams", "shared"))
        self._folder_dd.set_visible(self._source in ("me", "shared"))
        self._add_shared_btn.set_visible(self._source == "shared")
        if self._source == "me":
            self._ctx_current = None
        self._update_star()

    def _on_source_changed(self, source) -> None:
        if source == self._source:
            return
        self._source = source
        self._update_source_ui()
        if source == "me":
            self._populate_folders(self._me_folders)
        else:
            self._populate_context()

    def _populate_context(self) -> None:
        """Fill the context dropdown (teams list or shared-mailbox list)."""
        if self._source == "teams":
            items = [{"id": t["id"], "name": t["name"]} for t in self._teams]
            empty = _("No group mailboxes.")
        else:
            items = [{"id": a, "name": a} for a in self._shared_addresses()]
            empty = _("Add a shared mailbox with +.")
        self._ctx_items = items
        self._suppress = True
        self._ctx_dd.set_model(Gtk.StringList.new([i["name"] for i in items] or [_("None")]))
        self._ctx_dd.set_sensitive(bool(items))
        self._ctx_dd.set_selected(0)
        self._suppress = False
        if not items:
            self._folder_dd.set_visible(False)
            self._set_placeholder(empty)
            return
        self._on_ctx_changed(self._ctx_dd, None)

    def _on_ctx_changed(self, dropdown, _pspec) -> None:
        if self._suppress:
            return
        idx = dropdown.get_selected()
        items = getattr(self, "_ctx_items", [])
        if not (0 <= idx < len(items)):
            return
        self._ctx_current = items[idx]
        self._update_star()
        if self._source == "teams":
            self._select_folder(f"group:{items[idx]['id']}")
        else:  # shared: load that mailbox's folders into the folder dropdown
            self._load_shared_folders(items[idx]["id"])

    # -- pin (star) the current team/shared mailbox ----------------------
    def _update_star(self) -> None:
        active = self._source in ("teams", "shared") and self._ctx_current is not None
        self._star_btn.set_visible(active)
        if not active:
            return
        pinned = is_pinned(self._account, "mail", self._source, self._ctx_current["id"])
        self._star_btn.set_icon_name("starred-symbolic" if pinned else "non-starred-symbolic")

    def _on_star_clicked(self, _btn) -> None:
        if self._ctx_current is None:
            return
        toggle_pin(self._window, self._account, kind="mail", source=self._source,
                   sid=self._ctx_current["id"], name=self._ctx_current["name"])
        self._update_star()

    def _populate_folders(self, folders, *, initial: bool = False) -> None:
        # The "Me" mailbox leads with Inbox, then an "Unread" virtual folder
        # (the inbox filtered to unread), then the remaining folders.
        if self._source == "me":
            # Identify the inbox by its locale-independent well-known name (the
            # Graph id is opaque) or, for Gmail, by its "INBOX" label id.
            inbox = next(
                (f for f in folders
                 if (f.get("well_known") or "") == "inbox"
                 or str(f.get("id", "")).lower() == self._inbox_id.lower()), None)
            inbox_unread = inbox.get("unread", 0) if inbox else 0
            unread_folder = {"id": "unread", "name": _("Unread"), "unread": inbox_unread}
            rest = [f for f in folders if f is not inbox]
            folders = ([inbox] if inbox else []) + [unread_folder] + rest
        self._folder_items = folders
        self._suppress = True
        self._folder_dd.set_model(
            Gtk.StringList.new([self._label(f) for f in folders] or [_("None")]))
        self._folder_dd.set_sensitive(bool(folders))
        idx = next((i for i, f in enumerate(folders)
                    if f.get("id") == self._folder_id), 0)
        self._folder_dd.set_selected(idx)
        self._suppress = False
        if not folders:
            self._set_placeholder(_("No folders."))
            return
        fid = folders[idx].get("id", "")
        if not (initial and fid == self._folder_id):
            self._select_folder(fid)

    def _on_folder_changed(self, dropdown, _pspec) -> None:
        if self._suppress:
            return
        folders = getattr(self, "_folder_items", [])
        idx = dropdown.get_selected()
        if 0 <= idx < len(folders) and folders[idx].get("id") != self._folder_id:
            self._select_folder(folders[idx].get("id", ""))

    def _load_shared_folders(self, address) -> None:
        cached = self._shared_folders.get(address)
        if cached is not None:
            self._populate_folders(cached)
            return
        self._set_placeholder(_("Loading folders…"))

        def work():
            from .clients import build_account_client

            client = build_account_client(self._window.get_application(), self._account)
            return client.list_shared_folders(address)

        run_async(work, lambda folders, error: self._on_shared_folders(address, folders, error))

    def _on_shared_folders(self, address, folders, error) -> bool:
        if is_scope_error(error):
            self._reauth_prompt()
            self._folder_dd.set_sensitive(False)
            return False
        if error or not folders:
            self._set_placeholder(
                _("Couldn't open %(addr)s: %(err)s") % {"addr": address, "err": error}
                if error else _("No folders in %s.") % address
            )
            self._folder_dd.set_sensitive(False)
            return False
        self._shared_folders[address] = folders
        # Only populate if this reply is for the shared mailbox that's STILL
        # selected — a slower reply for a mailbox the user already switched
        # away from must not overwrite the current one's folder list.
        current = (self._ctx_current or {}).get("id")
        if self._source == "shared" and address == current:
            self._populate_folders(folders)
        return False

    def _on_add_shared(self, _btn) -> None:
        present_add_shared_dialog(
            self._window, self._account, lambda _addr: self._populate_context())

    def _select_folder(self, fid) -> None:
        self._folder_id = fid
        # A folder switch leaves search mode and clears the box.
        self._search_query = ""
        self._query = ""
        if self._search.get_text():
            self._search.set_text("")
        if self._source == "me":  # remember the Me-mailbox folder per account
            self._window.remember_mail_folder(self._account.id, fid)
        # Always revalidate (even on a fresh cache) so the pagination cursor for
        # this folder is populated and the "Load older" row can appear.
        self._show_cached_or_placeholder()
        self._load_async()
        self._list.invalidate_filter()  # apply/clear the Unread filter

    # -- loading ----------------------------------------------------------
    def _fetch_folder(self) -> str:
        """The real folder to hit — the inbox for the "Unread" virtual folder."""
        return self._inbox_id if self._folder_id == "unread" else self._folder_id

    def _load_async(self) -> None:
        folder_id = self._folder_id  # logical (may be "unread")
        fetch = self._fetch_folder()
        query = self._search_query

        def work():
            from .clients import build_account_client

            client = build_account_client(self._window.get_application(), self._account)
            return client.list_messages_page(fetch, query=query)

        run_async(work, lambda result, error: self._on_loaded(folder_id, query, result, error))

    def _on_loaded(self, folder_id, query, result, error) -> bool:
        # Ignore responses that no longer match the active folder/search.
        stale = folder_id != self._folder_id or query != self._search_query
        if error:
            # Never cache errors; keep any cached list on screen and only
            # surface the error if the active view has nothing to show.
            if not stale and not self._has_data:
                if is_scope_error(error):
                    self._reauth_prompt()
                else:
                    self._set_placeholder(_("Couldn't load mail: %s") % error)
            return False
        messages, next_token = result
        # Only the plain folder listing is cached; search results are transient.
        if not query:
            # Merge the fresh newest page into the cached list instead of
            # replacing it: "Load older" pages live in the cache too, and a
            # live refresh used to clobber them (the visible list snapped back
            # to page one and the pagination cursor reset). The page is
            # authoritative for its own window; older cached mail is kept.
            cache = self._window.get_application().cache
            key = f"{self._account.id}:messages:{folder_id}"
            cached = cache.get(key)
            base = list(cached[0]) if cached else []
            page_ids = {m.get("id") for m in messages}
            window_end = messages[-1].get("received", "") if messages else ""
            older = [m for m in base if m.get("id") not in page_ids
                     and (m.get("received", "") or "") <= window_end]
            merged = messages + older
            cache.set(key, merged)
            if not stale:
                # Adopt the page cursor only when no older pages are held —
                # resetting it would make "Load older" re-fetch loaded mail.
                if len(merged) == len(messages) or self._next_token is None:
                    self._next_token = next_token
                self._render(merged)
            return False
        if not stale:
            self._next_token = next_token
            self._render(messages)
        return False

    def _render(self, messages) -> None:
        if self._more_row is not None:
            self._list.remove(self._more_row)
            self._more_row = None
        if not messages:
            self._messages_by_id = {}
            self._rows_by_id = {}
            self._set_placeholder(
                _("No messages match “%s”.") % self._search_query
                if self._search_query else _("This folder is empty."))
            self._has_data = False
            return
        if not self._has_data:
            clear_listbox(self._list)
        selected = [r._mid for r in self._list.get_selected_rows()
                    if getattr(r, "_mid", None)]

        def factory(msg):
            return self._mail_row(msg)

        def update(row, msg):
            self._update_mail_row(row, msg)

        self._rows_by_id = patch_listbox(
            self._list, messages, lambda m: m["id"], factory, update,
            selection=selected,
        )
        self._messages_by_id = {m["id"]: m for m in messages}
        self._has_data = True
        self._sync_more_row()

    # -- pagination ("Load older messages") -------------------------------
    def _sync_more_row(self) -> None:
        """Add/remove the trailing "Load older" row to match the cursor."""
        if self._more_row is not None:
            self._list.remove(self._more_row)
            self._more_row = None
        if self._next_token:
            self._more_row = self._make_more_row()
            self._list.append(self._more_row)

    def _make_more_row(self) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow(activatable=True, selectable=False)
        row._more = True
        label = _("Loading…") if self._loading_more else _("Load older messages")
        lbl = Gtk.Label(label=label, margin_top=10, margin_bottom=10)
        lbl.add_css_class("dim-label")
        row.set_child(lbl)
        return row

    def _load_more(self) -> None:
        token = self._next_token
        logical = self._folder_id
        fetch = self._fetch_folder()
        query = self._search_query
        if not token or self._loading_more:
            return
        self._loading_more = True
        if self._more_row is not None:  # reflect the spinner-y state
            idx = self._more_row.get_index()
            self._list.remove(self._more_row)
            self._more_row = self._make_more_row()
            self._list.insert(self._more_row, idx)

        def work():
            from .clients import build_account_client

            client = build_account_client(self._window.get_application(), self._account)
            return client.list_messages_page(fetch, page_token=token, query=query)

        run_async(work, lambda result, error: self._on_more(logical, query, result, error))

    def _on_more(self, folder, query, result, error) -> bool:
        self._loading_more = False
        if error or folder != self._folder_id or query != self._search_query:
            self._sync_more_row()  # restore the button (drop the loading state)
            if error:
                self._window.add_toast(_("Couldn't load more: %s") % friendly_error(error))
            return False
        messages, next_token = result
        self._next_token = next_token
        # Append to the visible list. The plain folder listing also extends its
        # cached page; search results are transient and never cached.
        if query:
            existing = set(self._messages_by_id)
            new = [m for m in messages if m.get("id") not in existing]
        else:
            cache = self._window.get_application().cache
            cached = cache.get(self._cache_key())
            base = list(cached[0]) if cached else []
            existing = {m.get("id") for m in base}
            new = [m for m in messages if m.get("id") not in existing]
            cache.set(self._cache_key(), base + new)
        if self._more_row is not None:
            self._list.remove(self._more_row)
            self._more_row = None
        for msg in new:
            row = self._mail_row(msg)
            # Key the row like patch_listbox does, or the next live refresh
            # treats it as foreign: the message would get a second (duplicate)
            # row and this one would drift out of order.
            row._patch_key = msg["id"]
            self._list.append(row)
            self._messages_by_id[msg["id"]] = msg
            self._rows_by_id[msg["id"]] = row
        self._sync_more_row()
        return False

    # -- keyboard shortcuts (Outlook-style) -------------------------------
    def _on_list_key(self, _ctrl, keyval, _code, state) -> bool:
        ctrl = bool(state & Gdk.ModifierType.CONTROL_MASK)
        shift = bool(state & Gdk.ModifierType.SHIFT_MASK)
        if keyval in (Gdk.KEY_Up, Gdk.KEY_Left):
            self._nav(-1, extend=shift)
            return True
        if keyval in (Gdk.KEY_Down, Gdk.KEY_Right):
            self._nav(+1, extend=shift)
            return True
        if keyval in (Gdk.KEY_Delete, Gdk.KEY_KP_Delete):
            self._on_delete_clicked(None)
            return True
        if ctrl and keyval == Gdk.KEY_a:  # select all messages
            for row in self._message_rows():
                self._list.select_row(row)
            return True
        if ctrl and keyval == Gdk.KEY_r and self._open_mid:
            self._on_reply_clicked(None)
            return True
        if ctrl and keyval == Gdk.KEY_n:
            self._on_compose_clicked(None)
            return True
        return False

    # -- search -----------------------------------------------------------
    # Two layers: typing instantly filters the already-loaded list; pressing
    # Enter runs a server-side search across the whole folder (catches mail not
    # loaded yet). Clearing the box returns to the plain folder listing.
    def _on_search_changed(self, entry) -> None:
        text = entry.get_text().strip()
        if not text and self._search_query:
            # Box cleared while showing server results → back to the folder.
            self._search_query = ""
            self._query = ""
            self._show_cached_or_placeholder()
            self._load_async()
            return
        self._query = text.lower()
        self._list.invalidate_filter()

    def _on_search_activate(self, entry) -> None:
        text = entry.get_text().strip()
        if not text or text == self._search_query:
            return
        self._search_query = text
        self._query = ""  # server results must not be hidden by the live filter
        self._has_data = False
        self._next_token = None
        self._set_placeholder(_("Searching…"))
        self._load_async()

    def _filter_row(self, row) -> bool:
        unread_only = self._folder_id == "unread"
        search = getattr(row, "_search", None)
        if search is None:  # non-message rows
            if getattr(row, "_more", False):
                return not self._query  # keep "Load older" (also in Unread view)
            return not (self._query or unread_only)  # hide placeholders while filtering
        if unread_only and not getattr(row, "_unread", False):
            return False
        if self._query and self._query not in search:
            return False
        return True

    def _refresh_row(self, mid, msg) -> None:
        """Refresh a single row in place (e.g. after marking it read). Must
        reuse the existing widget: a swapped-in replacement would lose its
        ``_patch_key``, so the next patch_listbox pass (live refresh) would
        treat the message as missing and add a duplicate row for it."""
        row = self._rows_by_id.get(mid)
        if row is None:
            return
        self._update_mail_row(row, msg)

    # -- a single email row (plain Gtk.Labels: no markup parsing) ---------
    def _mail_row(self, msg) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow(activatable=True)
        self._update_mail_row(row, msg, create=True)
        return row

    def _update_mail_row(self, row: Gtk.ListBoxRow, msg: dict,
                         create: bool = False) -> None:
        unread = not msg.get("is_read", True)
        sender = _oneline(sender_name(msg.get("from", ""))) or _("Unknown sender")
        subject = _oneline(msg.get("subject", "")) or _("(no subject)")
        preview = _oneline(msg.get("preview", ""))

        row._mid = msg["id"]
        row._search = f"{sender} {subject} {preview}".lower()
        row._unread = unread

        if create:
            hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10,
                           margin_top=8, margin_bottom=8, margin_start=12, margin_end=12)
            row.set_child(hbox)

            dot = Gtk.Image.new_from_icon_name(
                "mail-unread-symbolic" if unread else "mail-read-symbolic"
            )
            dot.set_valign(Gtk.Align.CENTER)
            if not unread:
                dot.add_css_class("dim-label")
            hbox.append(dot)
            row._dot = dot

            body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True)
            hbox.append(body)

            top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            body.append(top)
            sender_lbl = Gtk.Label(label=sender, xalign=0, hexpand=True,
                                   ellipsize=Pango.EllipsizeMode.END)
            sender_lbl.add_css_class("heading" if unread else "body")
            top.append(sender_lbl)
            row._sender_lbl = sender_lbl
            time_lbl = Gtk.Label(label=short_time(msg.get("received", "")), xalign=1)
            time_lbl.add_css_class("dim-label")
            time_lbl.add_css_class("caption")
            top.append(time_lbl)
            row._time_lbl = time_lbl

            subj_lbl = Gtk.Label(label=subject, xalign=0, ellipsize=Pango.EllipsizeMode.END)
            if unread:
                subj_lbl.add_css_class("heading")
            body.append(subj_lbl)
            row._subj_lbl = subj_lbl

            if preview:
                prev_lbl = Gtk.Label(label=preview, xalign=0, ellipsize=Pango.EllipsizeMode.END)
                prev_lbl.add_css_class("dim-label")
                prev_lbl.add_css_class("caption")
                body.append(prev_lbl)
                row._prev_lbl = prev_lbl
            else:
                row._prev_lbl = None

            flags = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2,
                            valign=Gtk.Align.CENTER)
            row._flags = flags
            hbox.append(flags)
        else:
            row._dot.set_from_icon_name(
                "mail-unread-symbolic" if unread else "mail-read-symbolic"
            )
            if unread:
                row._dot.remove_css_class("dim-label")
            else:
                row._dot.add_css_class("dim-label")
            row._sender_lbl.set_label(sender)
            row._sender_lbl.set_css_classes(
                ["heading" if unread else "body"])
            row._time_lbl.set_label(short_time(msg.get("received", "")))
            row._subj_lbl.set_label(subject)
            row._subj_lbl.set_css_classes(
                ["heading"] if unread else [])
            if preview:
                if row._prev_lbl is None:
                    prev_lbl = Gtk.Label(label=preview, xalign=0, ellipsize=Pango.EllipsizeMode.END)
                    prev_lbl.add_css_class("dim-label")
                    prev_lbl.add_css_class("caption")
                    hbox = row.get_child()
                    body = hbox.get_first_child().get_next_sibling()
                    body.append(prev_lbl)
                    row._prev_lbl = prev_lbl
                else:
                    row._prev_lbl.set_label(preview)
                    row._prev_lbl.set_visible(True)
            elif row._prev_lbl is not None:
                row._prev_lbl.set_visible(False)
            # Rebuild flags box.
            child = row._flags.get_first_child()
            while child is not None:
                nxt = child.get_next_sibling()
                row._flags.remove(child)
                child = nxt

        if msg.get("important") or msg.get("starred"):
            if msg.get("important"):
                row._flags.append(Gtk.Image.new_from_icon_name("mail-mark-important-symbolic"))
            if msg.get("starred"):
                row._flags.append(Gtk.Image.new_from_icon_name("starred-symbolic"))
            row._flags.set_visible(True)
        else:
            row._flags.set_visible(False)

    # -- open a message into the reading pane -----------------------------
    def _on_list_scrolled(self, adj) -> None:
        if (self._next_token and not self._loading_more
                and adj.get_value() >= adj.get_upper() - adj.get_page_size() - 300):
            self._load_more()
        # Offer a jump back to the newest (top) once scrolled down a screenful.
        self._to_top_btn.set_visible(adj.get_value() > adj.get_page_size())

    def _on_go_latest(self, _btn) -> None:
        self._list_scroll.get_vadjustment().set_value(0)
        self._to_top_btn.set_visible(False)

    def _on_list_pressed(self, gesture, _n_press, _x, _y) -> None:
        mods = gesture.get_current_event_state()
        if mods & (Gdk.ModifierType.SHIFT_MASK | Gdk.ModifierType.CONTROL_MASK):
            gesture.set_state(Gtk.EventSequenceState.DENIED)
            return  # let Shift/Ctrl extend the selection
        # Bare click: drop the current selection (the listbox then selects the
        # clicked row, or nothing when the click lands on empty space). DENIED
        # so this gesture doesn't consume the press — the listbox still selects.
        self._list.unselect_all()
        gesture.set_state(Gtk.EventSequenceState.DENIED)

    def _on_row_activated(self, _list, row) -> None:
        if getattr(row, "_more", False):
            self._load_more()
            return
        mid = getattr(row, "_mid", None)
        if mid is not None:
            self.open_message(mid)  # guarded; a re-click just reveals the reader

    def _on_list_double(self, _gesture, n_press, _x, y) -> None:
        if n_press != 2:
            return
        row = self._list.get_row_at_y(int(y))
        if row is None or getattr(row, "_more", False):
            return
        mid = getattr(row, "_mid", None)
        if mid is not None:
            self.open_message_window(mid)

    def open_message_window(self, mid) -> None:
        """Pop a message out into its own read-only top-level window."""
        from .message_window import MessageWindow

        MessageWindow(self._window, self._account, mid).present()

    def _on_selection_changed(self, _list) -> None:
        # Open in the reader when exactly one message is selected; a multi-row
        # selection (Shift/Ctrl) keeps the current reader for batch actions.
        sel = self._selected_mids()
        if len(sel) == 1 and sel[0] != self._open_mid:
            self.open_message(sel[0])
        self._delete_btn.set_sensitive(bool(sel) or bool(self._open_mid))

    def _selected_mids(self) -> list:
        return [r._mid for r in self._list.get_selected_rows()
                if getattr(r, "_mid", None) is not None]

    def _message_rows(self) -> list:
        return data_rows(self._list, "_mid")

    def _nav(self, delta: int, *, extend: bool = False) -> None:
        move_selection(self._list, self._message_rows(), delta, extend=extend)

    def refresh_live(self) -> None:
        """Re-fetch the current folder from the server. Called by the notifier
        when its poll spots new mail, so the open list updates on its own instead
        of only on a manual refresh or page switch. No-op while paginating (a
        full reload would drop the "Load older" cursor mid-fetch)."""
        if self._loading_more:
            return
        self._load_async()

    def _in_drafts_folder(self) -> bool:
        """True when the active folder is Drafts (Graph well-known alias, or
        Gmail's DRAFT label)."""
        if str(self._folder_id).upper() == "DRAFT":
            return True
        current = next((f for f in self._folder_items
                        if f.get("id") == self._folder_id), None)
        return (current or {}).get("well_known") == "drafts"

    def _open_draft(self, mid) -> None:
        """Open a draft back into the composer; sending deletes the draft."""
        self._window.add_toast(_("Opening draft…"))

        def work():
            from .clients import build_account_client

            client = build_account_client(self._window.get_application(), self._account)
            return client.get_message(mid)

        def done(msg, error):
            if error:
                self._window.add_toast(
                    _("Couldn't open draft: %s") % friendly_error(error))
                return False
            from .compose_view import ComposeWindow
            from .message_view import _to_text

            source, address = self._send_context()

            def send(to, subject, body, *, cc=None, bcc=None, attachments=None,
                     importance="normal", read_receipt=False):
                from .clients import build_account_client

                client = build_account_client(
                    self._window.get_application(), self._account)
                client.send_mail(to=to, subject=subject, body=body, source=source,
                                 address=address, cc=cc, bcc=bcc, html=True,
                                 attachments=attachments, importance=importance,
                                 read_receipt=read_receipt)
                # Only auto-delete an attachment-less draft: the composer can't
                # carry the draft's server-side attachments over, so deleting
                # one that has them would destroy files the user never saw.
                # A leftover draft is harmless; a destroyed attachment isn't.
                if not msg.get("attachments"):
                    try:
                        client.delete_message(mid)  # the sent copy supersedes it
                    except Exception:  # noqa: BLE001 - a leftover draft is harmless
                        pass
                self._invalidate_messages()

            body = msg.get("body", "") or ""
            if msg.get("attachments"):
                self._window.add_toast(
                    _("This draft has attachments — they stay on the draft "
                      "and won't be attached here."))
            ComposeWindow(self._window, self._account,
                          from_label=self._from_label(), send_fn=send,
                          to=msg.get("to", ""), subject=msg.get("subject", ""),
                          cc=msg.get("cc", ""), bcc=msg.get("bcc", ""),
                          body=_to_text(body) if msg.get("body_html") else body,
                          title=_("Draft"),
                          draft_fn=self._make_draft_fn()).present()
            return False

        run_async(work, done)

    def open_message(self, mid) -> None:
        """Open a message in the reading pane (also used to deep-link from the
        dashboard). Selects its list row when that row is present. A message in
        Drafts opens back into the composer instead."""
        if self._in_drafts_folder():
            self._open_draft(mid)
            return
        if mid == self._open_mid:
            self._split.set_show_content(True)  # already open; just reveal it
            return
        self._open_mid = mid
        # Drop the previous message's body NOW: reply shortcuts (Ctrl+R) fire
        # before the fetch completes, and a stale _open_msg would quietly build
        # the reply from the previously-open message.
        self._open_msg = None
        # Group conversations are read-only (no per-user delete).
        self._delete_btn.set_sensitive(not str(mid).startswith("group:"))
        self._reply_btn.set_sensitive(False)  # enabled once the body loads
        self._reply_all_btn.set_sensitive(False)
        self._forward_btn.set_sensitive(False)
        self._reader.set_child(self._reader_loading())
        self._split.set_show_content(True)  # reveal the reader when collapsed

        row = self._rows_by_id.get(mid)
        if row is not None and row not in self._list.get_selected_rows():
            self._list.unselect_all()
            self._list.select_row(row)

        def work():
            from .clients import build_account_client

            client = build_account_client(self._window.get_application(), self._account)
            msg = client.get_message(mid)
            invite = self._parse_invite(client, mid, msg)
            if invite is not None:
                msg["invite"] = invite
            return msg

        run_async(work, lambda full, error: self._show_message(mid, full, error))

    def _parse_invite(self, client, mid, msg) -> dict | None:
        """If the message carries a ``text/calendar`` invite (REQUEST or CANCEL),
        fetch + parse it so the reader can show the event and offer Accept/Decline.
        Runs on the worker thread (network). Returns the invite dict with
        ``my_response`` filled in, or ``None``."""
        from ..core import ics

        for att in msg.get("attachments") or []:
            ct = (att.get("content_type") or "").lower()
            name = (att.get("name") or "").lower()
            if not (ct.startswith("text/calendar") or name.endswith(".ics")):
                continue
            try:
                raw = client.fetch_mail_attachment(mid, att["id"])
                invite = ics.parse_invite(raw.decode("utf-8", "replace"))
            except Exception:  # noqa: BLE001 - a bad/partial .ics just means no bar
                invite = None
            if not invite or invite.get("method") not in ("REQUEST", "CANCEL"):
                continue
            mine = ics.find_attendee(invite, self._account.display_name)
            invite["my_response"] = (mine or {}).get("partstat", "")
            return invite
        return None

    def _show_message(self, mid, msg, error) -> bool:
        if mid != self._open_mid:
            return False  # user already opened another message
        if error:
            self._reader.set_child(self._reader_placeholder(
                "dialog-error-symbolic", _("Couldn't open message"), error,
            ))
            return False
        from .message_view import build_message_content

        self._reader.set_child(build_message_content(
            msg, on_open_attachment=self._open_attachment,
            on_rsvp=self._on_invite_rsvp if msg.get("invite") else None))
        self._open_msg = msg
        self._reply_btn.set_sensitive(True)
        # Reply-all and Forward only make sense for a real mailbox message, not a
        # read-only group-conversation thread (group: ids).
        is_group = str(mid).startswith("group:")
        self._reply_all_btn.set_sensitive(not is_group)
        self._forward_btn.set_sensitive(not is_group)
        self._mark_read(mid)
        return False

    def _on_invite_rsvp(self, action: str) -> None:
        """Answer the open message's meeting invite: email a METHOD:REPLY
        VCALENDAR back to the organizer (iMIP), then mirror the answer onto the
        user's own calendar so an accepted invite actually shows up there.
        ``action`` is accept | tentativelyAccept | decline — or
        ``removeCancelled`` for the cancellation card's remove button."""
        invite = (self._open_msg or {}).get("invite")
        if not invite:
            return
        if action == "removeCancelled":
            self._remove_cancelled(invite)
            return
        if not invite.get("organizer_email"):
            self._window.add_toast(_("This invite has no organizer to reply to."))
            return
        self._window.add_toast(_("Sending response…"))
        me = self._account.display_name

        def work():
            from ..core import ics
            from .clients import build_account_client

            mine = ics.find_attendee(invite, me)
            reply = ics.build_reply(invite, attendee_email=me,
                                    attendee_cn=(mine or {}).get("cn", ""),
                                    action=action)
            prefix = ics.RSVP_PARTSTAT[action][1]
            subject = "%s: %s" % (prefix, invite.get("summary") or _("Meeting"))
            client = build_account_client(self._window.get_application(), self._account)
            client.send_mail(
                to=[invite["organizer_email"]], subject=subject,
                body=_("%s the invitation.") % prefix,
                attachments=[{"name": "invite.ics",
                              "content_type": "text/calendar; method=REPLY; charset=UTF-8",
                              "data": reply.encode("utf-8")}])
            return action, self._sync_invite_to_calendar(client, invite, action)

        run_async(work, self._on_invite_replied)

    def _sync_invite_to_calendar(self, client, invite, action) -> bool:
        """Mirror a mail RSVP onto the user's own calendar (worker thread).
        Exchange/Google usually auto-stage an emailed invite as a tentative
        event — find that copy by its iMIP UID and set the response there
        *without* re-notifying the organizer (the iMIP reply just did). When the
        provider didn't stage it (an external invite), create the event locally
        on accept/tentative. Best-effort: the reply already went out."""
        from ..core import ics

        try:
            eid = client.find_event_by_uid(invite.get("uid", ""))
            if eid:
                client.respond_event(eid, action, send=False)
            elif action == "decline":
                return True  # nothing staged, nothing to remove
            else:
                start = ics.ical_to_iso(invite.get("dtstart", ""))
                end = ics.ical_to_iso(invite.get("dtend", "")) or start
                if not start:
                    return False
                client.create_event(
                    subject=invite.get("summary") or _("Meeting"),
                    start_iso=start, end_iso=end,
                    location=invite.get("location", ""),
                    body=invite.get("description", ""),
                    all_day=bool(invite.get("all_day")))
            invalidate_cached(self._window.get_application(),
                              self._account.id, "events")
            return True
        except Exception:  # noqa: BLE001 - calendar mirror is best-effort
            return False

    def _remove_cancelled(self, invite) -> None:
        """Take a cancelled meeting off the user's calendar (looked up by its
        iMIP UID)."""
        self._window.add_toast(_("Removing from calendar…"))

        def work():
            from .clients import build_account_client

            client = build_account_client(self._window.get_application(), self._account)
            eid = client.find_event_by_uid(invite.get("uid", ""))
            if not eid:
                return False
            client.delete_event(eid)
            invalidate_cached(self._window.get_application(),
                              self._account.id, "events")
            return True

        def done(removed, error):
            if error:
                self._window.add_toast(
                    _("Couldn't remove it: %s") % friendly_error(error))
            elif removed:
                self._window.add_toast(_("Removed from calendar."))
            else:
                self._window.add_toast(_("That meeting isn't on your calendar."))
            return False

        run_async(work, done)

    def _on_invite_replied(self, result, error) -> bool:
        if error:
            self._window.add_toast(_("Couldn't send response: %s") % friendly_error(error))
            return False
        action, synced = result
        # Reflect the new answer in the open reader without a full reload.
        self._window.add_toast(_("Response sent — calendar updated.") if synced
                               else _("Response sent."))
        if self._open_msg and self._open_msg.get("invite"):
            partstat = {"accept": "ACCEPTED", "tentativelyAccept": "TENTATIVE",
                        "decline": "DECLINED"}.get(action, "")
            self._open_msg["invite"]["my_response"] = partstat
            self._show_message(self._open_mid, self._open_msg, None)
        return False

    # -- attachments ------------------------------------------------------
    def _open_attachment(self, att) -> None:
        from .attachments import open_attachment

        open_attachment(self._window, self._account, self._open_mid, att,
                        self._window.add_toast)

    # -- compose / reply --------------------------------------------------
    def _send_context(self):
        """Return ``(source, address)`` for sending as the active mailbox.

        Shared mailboxes have their own address (send-as); Me and Teams/group
        sources fall back to the signed-in user for new messages."""
        if self._source == "shared" and self._ctx_current is not None:
            return "shared", self._ctx_current["id"]
        return "me", None

    def _from_label(self) -> str:
        source, address = self._send_context()
        if source == "shared" and address:
            return address
        return self._account.display_name

    def _make_draft_fn(self):
        """A ``draft_fn`` for the composer, saving into the provider's Drafts."""
        source, address = self._send_context()

        def save_draft(to, subject, body, *, cc=None, bcc=None, attachments=None):
            from .clients import build_account_client

            client = build_account_client(self._window.get_application(), self._account)
            client.save_draft(to=to, subject=subject, body=body, source=source,
                              address=address, cc=cc, bcc=bcc, html=True,
                              attachments=attachments)
            self._invalidate_messages()

        return save_draft

    def _on_compose_clicked(self, _btn) -> None:
        from .compose_view import ComposeWindow

        source, address = self._send_context()

        def send(to, subject, body, *, cc=None, bcc=None,
                 attachments=None, importance="normal", read_receipt=False):
            from .clients import build_account_client

            client = build_account_client(self._window.get_application(), self._account)
            client.send_mail(to=to, subject=subject, body=body,
                             source=source, address=address, cc=cc, bcc=bcc,
                             html=True, attachments=attachments, importance=importance,
                             read_receipt=read_receipt)
            self._invalidate_messages()

        ComposeWindow(self._window, self._account, from_label=self._from_label(),
                      send_fn=send, body=self._signature_block(),
                      draft_fn=self._make_draft_fn()).present()

    def _on_reply_clicked(self, _btn) -> None:
        self._open_reply(reply_all=False)

    def _on_reply_all_clicked(self, _btn) -> None:
        self._open_reply(reply_all=True)

    def _open_reply(self, *, reply_all: bool) -> None:
        mid = self._open_mid
        if not mid:
            return
        from .compose_view import ComposeWindow

        meta = getattr(self, "_open_msg", None) or self._messages_by_id.get(mid, {})
        subject = meta.get("subject", "")
        if subject and not subject.lower().startswith("re:"):
            subject = _("Re: %s") % subject

        def send(_to, _subject, body, *, cc=None, bcc=None,
                 attachments=None, importance="normal", read_receipt=False):
            from .clients import build_account_client

            client = build_account_client(self._window.get_application(), self._account)
            client.reply_mail(mid, body, reply_all=reply_all, html=True,
                              attachments=attachments, read_receipt=read_receipt)
            self._invalidate_messages()

        ComposeWindow(
            self._window, self._account, from_label=self._account.display_name,
            send_fn=send, to=meta.get("from", ""), subject=subject,
            body=self._signature_block(),
            title=_("Reply all") if reply_all else _("Reply"),
            draft_fn=self._make_draft_fn(),
        ).present()

    def _on_forward_clicked(self, _btn) -> None:
        mid = self._open_mid
        if not mid:
            return
        from .compose_view import ComposeWindow

        msg = getattr(self, "_open_msg", None) or {}
        subject = msg.get("subject", "")
        if subject and not subject.lower().startswith(("fwd:", "fw:")):
            subject = _("Fwd: %s") % subject
        # The compose editor takes a plain-text prefill (it produces the HTML on
        # send), so quote the original as text and re-attach its files so the
        # forward actually carries them.
        body = self._signature_block() + self._forward_quote(msg)
        atts_meta = [a for a in (msg.get("attachments") or []) if a.get("id")]
        source, address = self._send_context()

        def send(to, subject, body, *, cc=None, bcc=None,
                 attachments=None, importance="normal", read_receipt=False):
            from .clients import build_account_client

            client = build_account_client(self._window.get_application(), self._account)
            fwd_atts = list(attachments or [])
            for a in atts_meta:
                try:
                    data = client.fetch_mail_attachment(mid, a["id"])
                    fwd_atts.append({
                        "name": a.get("name") or _("attachment"),
                        "content_type": a.get("content_type") or "application/octet-stream",
                        "data": data})
                except Exception:  # noqa: BLE001 - skip one we can't fetch
                    pass
            client.send_mail(to=to, subject=subject, body=body, source=source,
                             address=address, cc=cc, bcc=bcc, html=True,
                             attachments=fwd_atts, importance=importance,
                             read_receipt=read_receipt)
            self._invalidate_messages()

        ComposeWindow(self._window, self._account, from_label=self._from_label(),
                      send_fn=send, subject=subject, body=body,
                      title=_("Forward"), draft_fn=self._make_draft_fn()).present()

    @staticmethod
    def _forward_quote(msg: dict) -> str:
        """A plain-text quote of the forwarded message (header + body)."""
        from .message_view import _to_text

        lines = [
            "",
            "---------- " + _("Forwarded message") + " ----------",
            _("From: %s") % (msg.get("from", "") or ""),
            _("Date: %s") % (msg.get("received", "") or ""),
            _("Subject: %s") % (msg.get("subject", "") or ""),
            _("To: %s") % (msg.get("to", "") or ""),
            "",
            _to_text(msg.get("body", "") or ""),
        ]
        return "\n".join(lines)

    def _signature_block(self) -> str:
        """The account's signature as a plain-text block to prefill the composer
        (empty when none is set). Leads with blank lines so the cursor/new text
        sits above it."""
        sig = (getattr(self._account, "signature", "") or "").strip()
        return ("\n\n" + sig + "\n") if sig else ""

    def _invalidate_messages(self) -> None:
        """Drop this account's cached mail lists + the Dashboard aggregate after
        a send/reply/forward, so the Sent folder (and unread counts) revalidate
        instead of serving the pre-send cache as fresh. Thread-safe — the send
        wrappers call this from their worker thread."""
        invalidate_cached(self._window.get_application(),
                          self._account.id, "messages")

    # -- write-back: read state / flag / move / delete ---------------------
    def _sync_cached_row(self, mid) -> None:
        """After mutating a message dict in place, refresh its row, re-persist
        the folder cache (same dict identity) and drop the Dashboard aggregate
        (its unread counts changed)."""
        cached = self._messages_by_id.get(mid)
        if cached is not None:
            self._refresh_row(mid, cached)
        cache = self._window.get_application().cache
        entry = cache.get(self._cache_key())
        if entry is not None:
            cache.set(self._cache_key(), entry[0])
        cache.invalidate(prefix="dashboard:")

    def _mark_read(self, mid) -> None:
        cached = self._messages_by_id.get(mid)
        if cached is not None and cached.get("is_read", True):
            return  # already read; no write needed
        self._set_read(mid, True)

    def _set_read(self, mid, read: bool) -> None:
        cached = self._messages_by_id.get(mid)
        if cached is not None:
            cached["is_read"] = read  # also updates the cached list (same dict)
            self._sync_cached_row(mid)
            # Reflect the read in the sidebar/tab unread badge right away. The
            # badge counts the personal inbox only, so don't decrement it for a
            # shared mailbox / non-inbox folder (the next poll re-syncs anyway).
            if (read and self._source == "me"
                    and self._folder_id in (self._inbox_id, "unread")):
                notifier = getattr(self._window.get_application(), "notifier", None)
                if notifier is not None and hasattr(notifier, "mark_mail_read"):
                    notifier.mark_mail_read(self._account.id, mid)

        def work():
            from .clients import build_account_client

            client = build_account_client(self._window.get_application(), self._account)
            client.mark_read(mid, read)

        # Best-effort (e.g. Gmail not re-consented): ignore the outcome.
        run_async(work, lambda _r, _e: False)

    def _set_flag(self, mid, flagged: bool) -> None:
        """Flag/star a message for follow-up (optimistic row update)."""
        cached = self._messages_by_id.get(mid)
        if cached is not None:
            cached["starred"] = flagged
            self._sync_cached_row(mid)

        def work():
            from .clients import build_account_client

            client = build_account_client(self._window.get_application(), self._account)
            client.set_flag(mid, flagged)

        run_async(work, lambda _r, error: self._flag_failed(mid, error)
                  if error else False)

    def _flag_failed(self, mid, error) -> bool:
        cached = self._messages_by_id.get(mid)
        if cached is not None:  # roll the optimistic flip back
            cached["starred"] = not cached.get("starred")
            self._sync_cached_row(mid)
        self._window.add_toast(_("Couldn't update the flag: %s") % friendly_error(error))
        return False

    # -- context menu -------------------------------------------------------
    def _on_list_context(self, _gesture, _n_press, _x, y) -> None:
        row = self._list.get_row_at_y(int(y))
        mid = getattr(row, "_mid", None)
        if mid is None or str(mid).startswith("group:"):
            return  # group threads are read-only
        self._list.unselect_all()
        self._list.select_row(row)
        msg = self._messages_by_id.get(mid, {})
        pop = Gtk.Popover(has_arrow=False)
        pop.set_parent(row)
        pop.connect("closed", lambda p: GLib.idle_add(p.unparent))
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL,
                      margin_top=4, margin_bottom=4)

        def item(label, callback):
            btn = Gtk.Button(label=label)
            btn.add_css_class("flat")
            btn.get_child().set_halign(Gtk.Align.START)
            btn.connect("clicked",
                        lambda _b: (pop.popdown(), callback()) and None)
            box.append(btn)

        read = msg.get("is_read", True)
        item(_("Mark as unread") if read else _("Mark as read"),
             lambda: self._set_read(mid, not read))
        starred = bool(msg.get("starred"))
        item(_("Remove flag") if starred else _("Flag for follow-up"),
             lambda: self._set_flag(mid, not starred))
        item(_("Move to folder…"), lambda: self._move_dialog([mid]))
        item(_("Move to Trash"), lambda: self._on_delete_clicked(None))
        pop.set_child(box)
        pop.popup()

    # -- move to folder -----------------------------------------------------
    def _move_dialog(self, mids) -> None:
        folders = [f for f in self._folder_items
                   if f.get("id") not in ("unread", self._folder_id)]
        if not folders:
            self._window.add_toast(_("There's no other folder to move to."))
            return
        dialog = Adw.AlertDialog(
            heading=_("Move to folder"),
            body=_("Choose a destination for %d message(s).") % len(mids))
        dd = Gtk.DropDown(model=Gtk.StringList.new([f["name"] for f in folders]))
        dialog.set_extra_child(dd)
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("move", _("Move"))
        dialog.set_response_appearance("move", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("move")

        def on_response(_d, response):
            idx = dd.get_selected()
            if response == "move" and 0 <= idx < len(folders):
                self._move_messages(list(mids), folders[idx].get("id", ""))

        dialog.connect("response", on_response)
        dialog.present(self._window)

    def _move_messages(self, mids, dest) -> None:
        """Optimistically drop the rows, then move on the server."""
        src = self._fetch_folder()
        for mid in mids:
            row = self._rows_by_id.pop(mid, None)
            self._messages_by_id.pop(mid, None)
            if row is not None:
                self._list.remove(row)
            self._drop_from_cache(mid)
        if self._open_mid in mids:
            self._open_mid = None
        self._window.add_toast(
            _("Moved %d messages") % len(mids) if len(mids) > 1 else _("Message moved"))

        def work():
            from .clients import build_account_client

            client = build_account_client(self._window.get_application(), self._account)
            for mid in mids:
                client.move_message(mid, dest, from_folder=src)

        run_async(work, lambda _r, error: self._move_failed(error) if error else False)

    def _move_failed(self, error) -> bool:
        self._window.add_toast(_("Couldn't move: %s") % friendly_error(error))
        self._load_async()  # the optimistic removal was wrong; restore from server
        return False

    def _on_delete_clicked(self, _btn=None) -> None:
        # Delete the whole selection (multi-select), or the open message if
        # nothing is selected. Group conversations can't be deleted — skip them.
        mids = [m for m in (self._selected_mids() or
                            ([self._open_mid] if self._open_mid else []))
                if m and not str(m).startswith("group:")]
        if not mids:
            return
        for mid in mids:
            self._delete_message(mid)
        self._open_mid = None
        self._delete_btn.set_sensitive(False)
        self._reader.set_child(self._reader_placeholder(
            "user-trash-symbolic", _("Moved to Trash"),
            _("%d messages moved to Trash.") % len(mids) if len(mids) > 1
            else _("The message was moved to Trash."),
        ))
        self._window.add_toast(
            _("Moved %d to Trash") % len(mids) if len(mids) > 1
            else _("Moved to Trash"))

    def _delete_message(self, mid) -> None:
        """Optimistically drop a message's row and delete it on the server."""
        row = self._rows_by_id.pop(mid, None)
        cached = self._messages_by_id.pop(mid, None)
        if row is not None:
            self._list.remove(row)
        # Deleting an unread inbox message also clears it from the unread badge.
        if (cached is not None and not cached.get("is_read", True)
                and self._source == "me"
                and self._folder_id in (self._inbox_id, "unread")):
            notifier = getattr(self._window.get_application(), "notifier", None)
            if notifier is not None and hasattr(notifier, "mark_mail_read"):
                notifier.mark_mail_read(self._account.id, mid)

        def work():
            from .clients import build_account_client

            client = build_account_client(self._window.get_application(), self._account)
            client.delete_message(mid)

        run_async(work, lambda _r, error:
                  self._delete_failed(error) if error else self._drop_from_cache(mid))

    def _drop_from_cache(self, mid) -> bool:
        cache = self._window.get_application().cache
        cached = cache.get(self._cache_key())
        if cached is not None:
            cache.set(self._cache_key(),
                      [m for m in cached[0] if m.get("id") != mid])
        cache.invalidate(prefix="dashboard:")  # unread counts changed
        return False

    def _delete_failed(self, error) -> bool:
        self._window.add_toast(_("Couldn't delete: %s") % friendly_error(error))
        self._load_async()  # the optimistic removal was wrong; restore from server
        return False
