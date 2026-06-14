# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Fiber Elements
"""The main application window, loaded from the Blueprint-compiled template."""

from gettext import gettext as _

from gi.repository import Adw, Gio, Gtk

from .core.interfaces import capabilities_of

RESOURCE_PREFIX = "/com/fiberelements/Clouddrive"

# Capability key -> (translated label, symbolic icon).
CAPABILITY_UI = {
    "files": (_("Files"), "folder-symbolic"),
    "mail": (_("Mail"), "mail-unread-symbolic"),
    "calendar": (_("Calendar"), "x-office-calendar-symbolic"),
}


@Gtk.Template(resource_path=f"{RESOURCE_PREFIX}/ui/window.ui")
class ClouddriveWindow(Adw.ApplicationWindow):
    __gtype_name__ = "ClouddriveWindow"

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
        for account in self._registry.accounts():
            self.sidebar_list.append(self._make_account_row(account))

        if self._registry.is_empty():
            self.content_nav.pop_to_tag("welcome")

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
        account = self._registry.get(getattr(row, "_account_id", ""))
        if account is not None:
            self._show_account(account)

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
        # TODO(stage 2): launch the Graph/Google OAuth flow for this account.
        self.add_toast(_("Sign-in arrives in the next milestone."))

    # -- helpers ----------------------------------------------------------
    def add_toast(self, message: str) -> None:
        self.toast_overlay.add_toast(Adw.Toast(title=message))
