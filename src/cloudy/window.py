# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Shahab Nedaei
"""The main application window, loaded from the Blueprint-compiled template."""

import threading
from gettext import gettext as _

from gi.repository import Adw, Gio, GLib, Gtk

from .core.interfaces import capabilities_of

RESOURCE_PREFIX = "/io/github/sha5b/Cloudy"

# Capability key -> (translated label, symbolic icon).
CAPABILITY_UI = {
    "files": (_("Files"), "folder-symbolic"),
    "mail": (_("Mail"), "mail-unread-symbolic"),
    "calendar": (_("Calendar"), "x-office-calendar-symbolic"),
    "chat": (_("Chat"), "user-available-symbolic"),
    "teams": (_("Teams"), "system-users-symbolic"),
}


@Gtk.Template(resource_path=f"{RESOURCE_PREFIX}/ui/window.ui")
class CloudyWindow(Adw.ApplicationWindow):
    __gtype_name__ = "CloudyWindow"

    toast_overlay = Gtk.Template.Child()
    split_view = Gtk.Template.Child()
    sidebar_list = Gtk.Template.Child()
    content_nav = Gtk.Template.Child()

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        app = self.get_application()
        self._settings: Gio.Settings = app.settings
        self._registry = app.registry
        self._engine = app.engine

        self._account_stack = None
        self._account_mail_view = None
        self._account_chat_view = None
        self._account_calendar_view = None
        self._account_shown = None
        self._last_tab: dict = {}  # account id -> last-viewed tab name
        self._mail_folder_by_account: dict = {}  # account id -> last mail folder id
        self._account_badges: dict = {}  # account id -> unread-mail Gtk.Label
        self._account_chat_badges: dict = {}  # account id -> unread-chat Gtk.Label

        self._bind_window_state()
        self.connect("close-request", self._on_close_request)
        self.sidebar_list.connect("row-selected", self._on_row_selected)
        self._registry.connect("changed", lambda *_: self._refresh_sidebar())
        self._refresh_sidebar()

    def _bind_window_state(self) -> None:
        self._settings.bind(
            "window-width", self, "default-width", Gio.SettingsBindFlags.DEFAULT
        )
        self._settings.bind(
            "window-height", self, "default-height", Gio.SettingsBindFlags.DEFAULT
        )
        self._settings.bind(
            "window-maximized", self, "maximized", Gio.SettingsBindFlags.DEFAULT
        )

    def _on_close_request(self, *_args) -> bool:
        """Closing the window hides it and keeps Cloudy running in the
        background (Quit with Ctrl+Q or GNOME's Background Apps menu to exit).
        When background mode is off, fall through to a normal close."""
        app = self.get_application()
        if app is not None and app.wants_background():
            app.enter_background(self)
            return True  # handled: don't destroy the window
        return False

    # -- sidebar ----------------------------------------------------------
    def _refresh_sidebar(self) -> None:
        self._account_badges.clear()  # rows are rebuilt; drop stale widget refs
        self._account_chat_badges.clear()
        self.sidebar_list.remove_all()
        if not self._registry.is_empty():
            self.sidebar_list.append(self._make_overview_row())
        for account in self._registry.accounts():
            self.sidebar_list.append(self._make_account_row(account))

        if self._registry.is_empty():
            self.content_nav.pop_to_tag("welcome")

    def _make_overview_row(self) -> Gtk.ListBoxRow:
        row = Adw.ActionRow(title=_("Overview"), subtitle=_("Calendars and mail at a glance"))
        row.add_prefix(Gtk.Image.new_from_icon_name("view-grid-symbolic"))
        row.set_activatable(True)
        row._overview = True
        return row

    def _make_account_row(self, account) -> Gtk.ListBoxRow:
        from .widgets.format import esc

        module = self._engine.get(account.module_id)
        icon = module.icon_name if module else "avatar-default-symbolic"
        if module is not None and not self._engine.is_enabled(account.module_id):
            subtitle = _("Turned off")
        else:
            subtitle = _("Signed in") if account.signed_in else _("Sign-in pending")
        row = Adw.ActionRow(title=esc(account.display_name), subtitle=subtitle)
        row.add_prefix(Gtk.Image.new_from_icon_name(icon))

        # Red chat badge (new Teams/Chat messages) + accent unread-mail badge.
        # Both are filled in by the notifier's poll; hidden when their count is 0.
        app = self.get_application()
        notifier = getattr(app, "notifier", None)

        chat_badge = Gtk.Label(valign=Gtk.Align.CENTER)
        chat_badge.add_css_class("cloudy-badge")
        chat_badge.add_css_class("chat")
        chat_badge.add_css_class("numeric")
        row.add_suffix(chat_badge)
        self._account_chat_badges[account.id] = chat_badge
        self._set_badge(chat_badge, notifier.chat_unread_count(account.id) if notifier else 0)

        badge = Gtk.Label(valign=Gtk.Align.CENTER)
        badge.add_css_class("cloudy-badge")
        badge.add_css_class("numeric")
        row.add_suffix(badge)
        self._account_badges[account.id] = badge
        self._set_badge(badge, notifier.unread_count(account.id) if notifier else 0)

        row.set_activatable(True)
        row._account_id = account.id  # carry the id for selection
        return row

    @staticmethod
    def _set_badge(badge: Gtk.Label, count: int) -> None:
        badge.set_text(str(count) if count else "")
        badge.set_visible(bool(count))

    def _set_tab_badge(self, key: str, count: int) -> None:
        """Set the numeric badge on a tab (Adw.ViewStackPage) of the currently
        shown account's view. The ViewSwitcher draws it like Mail/Calendar do."""
        page = getattr(self, "_tab_pages", {}).get(key)
        if page is not None:
            page.set_badge_number(max(0, int(count)))
            page.set_needs_attention(count > 0)

    def set_account_unread(self, account_id: str, count: int) -> None:
        """Update an account row's unread-mail badge (called by the notifier)."""
        badge = self._account_badges.get(account_id)
        if badge is not None:
            self._set_badge(badge, count)
        if account_id == getattr(self, "_account_shown", None):
            self._set_tab_badge("mail", count)

    def set_account_chat_unread(self, account_id: str, count: int) -> None:
        """Update an account row's red chat badge (called by the notifier)."""
        badge = self._account_chat_badges.get(account_id)
        if badge is not None:
            self._set_badge(badge, count)
        if account_id == getattr(self, "_account_shown", None):
            self._set_tab_badge("chat", count)

    def refresh_account_mail(self, account_id: str) -> None:
        """Reload the open mail list when the notifier sees new mail, so it
        updates live (not only on a manual refresh or tab switch)."""
        if self._account_shown != account_id:
            return
        view = self._account_mail_view
        if view is not None:
            view.refresh_live()

    # -- per-account mail folder memory (survives account switches) -------
    def remember_mail_folder(self, account_id: str, folder_id: str) -> None:
        self._mail_folder_by_account[account_id] = folder_id

    def last_mail_folder(self, account_id: str):
        return self._mail_folder_by_account.get(account_id)

    def _on_row_selected(self, _list, row) -> None:
        if row is None:
            return
        if getattr(row, "_overview", False):
            self._show_dashboard()
            return
        account = self._registry.get(getattr(row, "_account_id", ""))
        if account is not None:
            self._show_account(account)

    def _show_dashboard(self) -> None:
        from .widgets.dashboard_view import DashboardView

        header = Adw.HeaderBar()
        refresh = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text=_("Refresh"))
        refresh.connect("clicked", lambda *_: self._refresh_overview())
        header.pack_end(refresh)
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(header)
        toolbar.set_content(DashboardView(self))
        page = Adw.NavigationPage(title=_("Overview"), tag="overview")
        page.set_child(toolbar)
        self.content_nav.replace([page])

    def _refresh_overview(self) -> None:
        # Drop the cached aggregate so the rebuilt dashboard fetches fresh.
        self.get_application().cache.invalidate(prefix="dashboard:")
        self._show_dashboard()

    # -- per-account content ---------------------------------------------
    def _show_account(self, account) -> None:
        module = self._engine.get(account.module_id)
        if module is not None and not self._engine.is_enabled(account.module_id):
            self._show_disabled_account(account)
            return
        caps = capabilities_of(module) if module else []
        # Hide surfaces a personal (consumer) account can't use: Teams Chat and
        # the Teams (channels) tab have no consumer API. Mail/Calendar's own
        # Teams/Shared sources are likewise hidden inside those views (see
        # MailView/CalendarView).
        if account.is_personal:
            caps = [c for c in caps if c not in ("chat", "teams")]

        stack = Adw.ViewStack()
        self._account_stack = stack
        self._tab_pages = {}  # capability key -> Adw.ViewStackPage (for tab badges)
        self._account_mail_view = None
        self._account_chat_view = None
        self._account_calendar_view = None
        self._account_shown = account.id
        for key in caps:
            label, icon = CAPABILITY_UI.get(key, (key, "application-x-addon-symbolic"))
            child = self._capability_placeholder(account, key, label)
            if key == "mail":
                from .widgets.mail_view import MailView

                if isinstance(child, MailView):
                    self._account_mail_view = child
            elif key == "chat":
                from .widgets.chat_view import ChatView

                if isinstance(child, ChatView):
                    self._account_chat_view = child
            elif key == "calendar":
                from .widgets.calendar_view import CalendarView

                if isinstance(child, CalendarView):
                    self._account_calendar_view = child
            page = stack.add_titled(child, key, label)
            page.set_icon_name(icon)
            self._tab_pages[key] = page

        # Seed the Mail/Chat tab badges with the same unread counts the sidebar
        # shows (Calendar/Files have no "new since you looked" count yet).
        app = self.get_application()
        notifier = getattr(app, "notifier", None)
        if notifier is not None:
            self._set_tab_badge("mail", notifier.unread_count(account.id))
            self._set_tab_badge("chat", notifier.chat_unread_count(account.id))

        # Re-open on the tab the user last left for this account.
        remembered = self._last_tab.get(account.id)
        if remembered and stack.get_child_by_name(remembered) is not None:
            stack.set_visible_child_name(remembered)
        stack.connect("notify::visible-child-name", self._on_tab_changed)

        header = Adw.HeaderBar()
        switcher = Adw.ViewSwitcher(policy=Adw.ViewSwitcherPolicy.WIDE)
        switcher.set_stack(stack)
        header.set_title_widget(switcher)
        # Account sign-in/out/remove live in Preferences → Accounts now; each tab
        # carries its own contextual settings (e.g. the calendar's Calendars
        # popover). The header just switches tabs and refreshes.
        refresh = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text=_("Refresh"))
        refresh.connect("clicked", lambda *_: self._refresh_account(account))
        header.pack_end(refresh)

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(header)
        toolbar.set_content(stack)

        page = Adw.NavigationPage(title=account.display_name, tag=f"account:{account.id}")
        page.set_child(toolbar)
        self.content_nav.replace([page])

    def _on_tab_changed(self, stack, _pspec) -> None:
        name = stack.get_visible_child_name()
        if self._account_shown and name:
            self._last_tab[self._account_shown] = name

    def _show_disabled_account(self, account) -> None:
        self._account_stack = None
        self._account_mail_view = None
        self._account_chat_view = None
        self._account_calendar_view = None
        self._account_shown = account.id
        status = Adw.StatusPage(
            icon_name="action-unavailable-symbolic",
            title=_("%s is turned off") % account.display_name,
            description=_("Enable this account in Preferences → Accounts."),
        )
        header = Adw.HeaderBar()
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(header)
        toolbar.set_content(status)
        page = Adw.NavigationPage(title=account.display_name, tag=f"account:{account.id}")
        page.set_child(toolbar)
        self.content_nav.replace([page])

    def _refresh_account(self, account) -> None:
        self.get_application().cache.invalidate(prefix=account.id)
        self._show_account(account)

    # -- account actions exposed to Preferences ---------------------------
    def sign_in_account(self, account) -> None:
        self._on_sign_in(account)

    def sign_out_account(self, account) -> None:
        self._sign_out(account)

    def remove_account(self, account) -> None:
        self._remove_account(account)

    # -- deep links (e.g. from the dashboard) -----------------------------
    def open_mail(self, account, message_id) -> None:
        """Show the account's Mail tab and open a specific message there."""
        self._select_sidebar_account(account.id)  # builds the account view
        if self._account_shown != account.id:
            return
        if self._account_stack is not None:
            self._account_stack.set_visible_child_name("mail")
        if self._account_mail_view is not None:
            self._account_mail_view.open_message(message_id)

    def open_account_tab(self, account, tab) -> None:
        """Show an account and switch to its Files/Mail/Calendar tab (used by the
        Dashboard's pinned-source shortcuts)."""
        self._select_sidebar_account(account.id)
        if self._account_shown != account.id:
            return
        if self._account_stack is not None:
            self._account_stack.set_visible_child_name(tab)

    def open_chat(self, account, chat_id) -> None:
        """Show the account's Chat tab and open a specific conversation."""
        self._select_sidebar_account(account.id)
        if self._account_shown != account.id:
            return
        if self._account_stack is not None:
            self._account_stack.set_visible_child_name("chat")
        if self._account_chat_view is not None:
            self._account_chat_view.open_chat(chat_id)

    def open_calendar_event(self, account, event_id) -> None:
        """Show the account's Calendar tab and open a specific event."""
        self._select_sidebar_account(account.id)
        if self._account_shown != account.id:
            return
        if self._account_stack is not None:
            self._account_stack.set_visible_child_name("calendar")
        if self._account_calendar_view is not None:
            self._account_calendar_view.open_event(event_id)

    def _select_sidebar_account(self, account_id) -> None:
        row = self.sidebar_list.get_first_child()
        while row is not None:
            if getattr(row, "_account_id", None) == account_id:
                if self.sidebar_list.get_selected_row() is row:
                    self._show_account(self._registry.get(account_id))
                else:
                    self.sidebar_list.select_row(row)  # emits row-selected
                return
            row = row.get_next_sibling()

    def _account_menu_button(self, account) -> Gtk.MenuButton:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, margin_top=6,
                      margin_bottom=6, margin_start=6, margin_end=6)
        if account.signed_in:
            resync = Gtk.Button(label=_("Sign Out / Re-sign In"))
            resync.add_css_class("flat")
            resync.connect("clicked", lambda *_: self._sign_out(account))
            box.append(resync)
        remove = Gtk.Button(label=_("Remove Account"))
        remove.add_css_class("flat")
        remove.add_css_class("destructive-action")
        remove.connect("clicked", lambda *_: self._remove_account(account))
        box.append(remove)

        popover = Gtk.Popover()
        popover.set_child(box)
        menu = Gtk.MenuButton(icon_name="view-more-symbolic", tooltip_text=_("Account"))
        menu.set_popover(popover)
        return menu

    def _sign_out(self, account) -> None:
        app = self.get_application()
        try:
            if account.provider == "microsoft":
                from .core.auth.msal_graph import GraphAuth

                GraphAuth(app.microsoft_client_id(), app.secrets, account.id).sign_out()
            elif account.provider == "google":
                from .core.auth.google_oauth import GoogleAuth

                GoogleAuth(
                    app.google_client_id(), app.secrets, account.id,
                    client_secret=app.google_client_secret(),
                ).sign_out()
        except Exception:  # noqa: BLE001 - clearing local token is best-effort
            pass
        account.signed_in = False
        self._registry.update(account)
        self.add_toast(_("Signed out. Sign in again to refresh permissions."))
        self._show_account(account)

    def _remove_account(self, account) -> None:
        secrets = self.get_application().secrets
        for kind in ("msal-cache", "google-token", "rclone-onedrive"):
            try:
                secrets.clear(account.id, kind)
            except Exception:  # noqa: BLE001
                pass
        self._registry.remove(account.id)
        self.content_nav.pop_to_tag("welcome")
        self.add_toast(_("Removed %s.") % account.display_name)

    def _capability_placeholder(self, account, key, label) -> Gtk.Widget:
        # Signed-in surfaces get their real views.
        if account.signed_in:
            if key == "files":
                from .widgets.files_view import FilesView

                return FilesView(self, account)
            if key == "mail":
                from .widgets.mail_view import MailView

                return MailView(self, account)
            if key == "calendar":
                from .widgets.calendar_view import CalendarView

                return CalendarView(self, account)
            if key == "chat":
                from .widgets.chat_view import ChatView

                return ChatView(self, account)
            if key == "teams":
                from .widgets.teams_view import TeamsView

                return TeamsView(self, account)

        status = Adw.StatusPage(
            icon_name=CAPABILITY_UI.get(key, (None, "application-x-addon-symbolic"))[1],
            title=label,
        )
        if account.signed_in:
            status.set_description(_("No items to show yet."))
        else:
            status.set_description(
                _("Sign in to %s to load your %s.") % (account.display_name, label.lower())
            )
            button = Gtk.Button(label=_("Sign In"), halign=Gtk.Align.CENTER)
            button.add_css_class("pill")
            button.add_css_class("suggested-action")
            button.connect("clicked", lambda *_: self._on_sign_in(account))
            status.set_child(button)
        return status

    def _on_sign_in(self, account) -> None:
        app = self.get_application()
        if account.provider == "microsoft":
            client_id = app.microsoft_client_id()
            label = _("Microsoft")
        elif account.provider == "google":
            client_id = app.google_client_id()
            label = _("Google")
        else:
            self.add_toast(_("Sign-in for this provider arrives later."))
            return

        if not client_id:
            self._show_setup_needed(label)
            return

        self.add_toast(_("Opening your browser to sign in…"))
        worker = (
            self._microsoft_sign_in_worker
            if account.provider == "microsoft"
            else self._google_sign_in_worker
        )
        threading.Thread(
            target=worker, args=(account, client_id, app.secrets), daemon=True
        ).start()

    def _microsoft_sign_in_worker(self, account, client_id, secrets) -> None:
        from .core.auth.msal_graph import (
            GraphAuth,
            SCOPES_BASE,
            SCOPES_CHANNELS,
            SCOPES_CHAT,
            SCOPES_FILES,
            SCOPES_GROUPS,
            SCOPES_MAIL,
            SCOPES_MAIL_SHARED,
            SCOPES_NOTES,
            SCOPES_PEOPLE,
            SCOPES_PRESENCE,
            SCOPES_TEAMS,
        )

        try:
            auth = GraphAuth(client_id, secrets, account.id)
            # Request all capability scopes up front so a single consent covers
            # Files, Teams, Mail, Calendar, group + shared mailboxes/calendars,
            # Chat (Teams messages), Teams channels + OneNote (the Teams tab),
            # and People (To-field autocomplete).
            result = auth.sign_in_interactive(
                SCOPES_BASE + SCOPES_FILES + SCOPES_TEAMS + SCOPES_GROUPS
                + SCOPES_MAIL + SCOPES_MAIL_SHARED + SCOPES_PEOPLE + SCOPES_CHAT
                + SCOPES_PRESENCE + SCOPES_CHANNELS + SCOPES_NOTES
            )
            try:
                ident = GraphAuth.fetch_userprincipalname(result["access_token"])
            except Exception:  # noqa: BLE001 - identity lookup is best-effort
                ident = None
            GLib.idle_add(self._on_sign_in_result, account, ident, None)
        except Exception as exc:  # noqa: BLE001 - surface any auth failure as a toast
            GLib.idle_add(self._on_sign_in_result, account, None, str(exc))

    def _google_sign_in_worker(self, account, client_id, secrets) -> None:
        from .core.auth.google_oauth import GoogleAuth

        try:
            auth = GoogleAuth(
                client_id, secrets, account.id,
                client_secret=self.get_application().google_client_secret(),
            )
            result = auth.sign_in_interactive(open_url=self.open_uri)
            try:
                ident = GoogleAuth.fetch_email(result["access_token"])
            except Exception:  # noqa: BLE001 - identity lookup is best-effort
                ident = None
            GLib.idle_add(self._on_sign_in_result, account, ident, None)
        except Exception as exc:  # noqa: BLE001 - surface any auth failure as a toast
            GLib.idle_add(self._on_sign_in_result, account, None, str(exc))

    def _on_sign_in_result(self, account, upn, error) -> bool:
        if error:
            self.add_toast(_("Sign-in failed: %s") % error)
            return False
        account.signed_in = True
        if upn:
            account.display_name = upn
        self._registry.update(account)
        self.add_toast(_("Signed in as %s") % account.display_name)
        self._show_account(account)
        return False

    def _show_setup_needed(self, provider_label) -> None:
        dialog = Adw.AlertDialog(
            heading=_("%s sign-in isn’t set up yet") % provider_label,
            body=_(
                "Cloudy needs a %s app ID before it can open the sign-in "
                "page. This is a one-time setup by whoever builds the app — set "
                "CLOUDY_MS_CLIENT_ID / CLOUDY_GOOGLE_CLIENT_ID or the "
                "matching setting. See docs/AUTH.md."
            )
            % provider_label,
        )
        dialog.add_response("ok", _("OK"))
        dialog.present(self)

    def push_content(self, page) -> None:
        """Push a page into the content navigation stack (e.g. a mail message)."""
        self.content_nav.push(page)

    def open_uri(self, uri: str) -> None:
        """Open a URI via the portal-aware launcher, on the main thread."""
        GLib.idle_add(lambda: (Gtk.show_uri(self, uri, 0), False)[1])

    # -- system handler entry points (mailto: / .ics) ---------------------
    def _default_mail_account(self):
        """First signed-in mail/calendar-capable account (for system handoffs)."""
        for account in self._registry.accounts():
            if account.signed_in and account.provider in ("microsoft", "google"):
                return account
        return None

    def open_compose_from_mailto(self, uri: str) -> None:
        """Open the compose dialog for a ``mailto:`` URI (default-mail-app role)."""
        from urllib.parse import parse_qs, unquote, urlparse

        account = self._default_mail_account()
        if account is None:
            self.add_toast(_("Sign in to an account to send mail."))
            return
        parsed = urlparse(uri)
        to = unquote(parsed.path)
        query = parse_qs(parsed.query)
        subject = query.get("subject", [""])[0]
        body = query.get("body", [""])[0]

        from .widgets.compose_view import ComposeWindow

        def send(recipients, subj, bod, *, cc=None, bcc=None,
                 attachments=None, importance="normal"):
            from .widgets.clients import build_account_client

            client = build_account_client(self.get_application(), account)
            client.send_mail(to=recipients, subject=subj, body=bod, cc=cc, bcc=bcc,
                             html=True, attachments=attachments, importance=importance)

        ComposeWindow(self, account, from_label=account.display_name, send_fn=send,
                      to=to, subject=subject, body=body).present()

    def open_event_from_ics(self, path: str) -> None:
        """Open the New event dialog pre-filled from an ``.ics`` file."""
        account = self._default_mail_account()
        if account is None:
            self.add_toast(_("Sign in to an account to add the event."))
            return
        from .widgets.event_compose import EventWindow, parse_ics

        try:
            initial = parse_ics(path)
        except OSError as exc:
            self.add_toast(_("Couldn't read the invite: %s") % exc)
            return

        def create(**fields):
            from .widgets.clients import build_account_client

            client = build_account_client(self.get_application(), account)
            return client.create_event(**fields)

        EventWindow(self, on_calendar=account.display_name, create_fn=create,
                    initial=initial, title=_("Add event")).present()

    # -- helpers ----------------------------------------------------------
    def add_toast(self, message: str) -> None:
        self.toast_overlay.add_toast(Adw.Toast(title=message))
