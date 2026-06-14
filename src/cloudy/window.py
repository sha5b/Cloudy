# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Fiber Elements
"""The main application window, loaded from the Blueprint-compiled template."""

import threading
from gettext import gettext as _

from gi.repository import Adw, Gio, GLib, Gtk

from .core.interfaces import capabilities_of

RESOURCE_PREFIX = "/com/fiberelements/Cloudy"

# Capability key -> (translated label, symbolic icon).
CAPABILITY_UI = {
    "files": (_("Files"), "folder-symbolic"),
    "mail": (_("Mail"), "mail-unread-symbolic"),
    "calendar": (_("Calendar"), "x-office-calendar-symbolic"),
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

        self._bind_window_state()
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

    # -- sidebar ----------------------------------------------------------
    def _refresh_sidebar(self) -> None:
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
        module = self._engine.get(account.module_id)
        icon = module.icon_name if module else "avatar-default-symbolic"
        subtitle = _("Signed in") if account.signed_in else _("Sign-in pending")
        row = Adw.ActionRow(title=account.display_name, subtitle=subtitle)
        row.add_prefix(Gtk.Image.new_from_icon_name(icon))
        row.set_activatable(True)
        row._account_id = account.id  # carry the id for selection
        return row

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
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(header)
        toolbar.set_content(DashboardView(self))
        page = Adw.NavigationPage(title=_("Overview"), tag="overview")
        page.set_child(toolbar)
        self.content_nav.replace([page])

    # -- per-account content ---------------------------------------------
    def _show_account(self, account) -> None:
        module = self._engine.get(account.module_id)
        caps = capabilities_of(module) if module else []

        stack = Adw.ViewStack()
        for key in caps:
            label, icon = CAPABILITY_UI.get(key, (key, "application-x-addon-symbolic"))
            page = stack.add_titled(
                self._capability_placeholder(account, key, label), key, label
            )
            page.set_icon_name(icon)

        header = Adw.HeaderBar()
        switcher = Adw.ViewSwitcher(policy=Adw.ViewSwitcherPolicy.WIDE)
        switcher.set_stack(stack)
        header.set_title_widget(switcher)

        switcher_bar = Adw.ViewSwitcherBar()
        switcher_bar.set_stack(stack)
        switcher_bar.set_reveal(True)

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(header)
        toolbar.add_bottom_bar(switcher_bar)
        toolbar.set_content(stack)

        page = Adw.NavigationPage(title=account.display_name, tag=f"account:{account.id}")
        page.set_child(toolbar)
        self.content_nav.replace([page])

    def _capability_placeholder(self, account, key, label) -> Gtk.Widget:
        # Signed-in surfaces get their real views.
        if account.signed_in:
            if key == "files" and account.provider == "microsoft":
                from .widgets.files_view import FilesView

                return FilesView(self, account)
            if key == "mail":
                from .widgets.mail_view import MailView

                return MailView(self, account)
            if key == "calendar":
                from .widgets.calendar_view import CalendarView

                return CalendarView(self, account)

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
            SCOPES_FILES,
            SCOPES_MAIL,
        )

        try:
            auth = GraphAuth(client_id, secrets, account.id)
            # Request all capability scopes up front so a single consent covers
            # Files, Mail and Calendar (silent tokens work for every surface).
            result = auth.sign_in_interactive(
                SCOPES_BASE + SCOPES_FILES + SCOPES_MAIL
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

    def open_uri(self, uri: str) -> None:
        """Open a URI via the portal-aware launcher, on the main thread."""
        GLib.idle_add(lambda: (Gtk.show_uri(self, uri, 0), False)[1])

    # -- helpers ----------------------------------------------------------
    def add_toast(self, message: str) -> None:
        self.toast_overlay.add_toast(Adw.Toast(title=message))
