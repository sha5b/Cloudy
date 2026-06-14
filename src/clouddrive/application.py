# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Fiber Elements
"""The Adw.Application subclass: actions, lifecycle, and the main window."""

from gettext import gettext as _

from gi.repository import Adw, Gio, Gtk

from .core.account_registry import AccountRegistry
from .core.plugin_engine import PluginEngine
from .window import ClouddriveWindow


class ClouddriveApplication(Adw.Application):
    """Top-level application object."""

    def __init__(self, application_id: str, version: str):
        super().__init__(
            application_id=application_id,
            flags=Gio.ApplicationFlags.DEFAULT_FLAGS,
        )
        self.version = version
        self.application_id = application_id

        # Core services, constructed once and shared with the window/modules.
        self.settings = Gio.Settings.new(application_id)
        self.registry = AccountRegistry(self.settings)
        self.engine = PluginEngine(self.settings)

        self._setup_actions()

    def _setup_actions(self) -> None:
        self._add_action("quit", self._on_quit, ["<primary>q"])
        self._add_action("about", self._on_about)
        self._add_action("preferences", self._on_preferences, ["<primary>comma"])
        self._add_action("add-account", self._on_add_account)

    def _add_action(self, name, callback, accels=None):
        action = Gio.SimpleAction.new(name, None)
        action.connect("activate", callback)
        self.add_action(action)
        if accels:
            self.set_accels_for_action(f"app.{name}", accels)

    # -- GApplication lifecycle ------------------------------------------
    def do_startup(self):
        Adw.Application.do_startup(self)
        # Discover modules now; activation happens per user settings.
        self.engine.discover()

    def do_activate(self):
        window = self.props.active_window
        if not window:
            window = ClouddriveWindow(application=self)
        window.present()

    # -- Action handlers --------------------------------------------------
    def _on_quit(self, *_args):
        self.quit()

    def _on_preferences(self, *_args):
        from .preferences import ClouddrivePreferences

        prefs = ClouddrivePreferences(engine=self.engine)
        prefs.present(self.props.active_window)

    def _on_add_account(self, *_args):
        from .account_dialog import AddAccountDialog

        window = self.props.active_window
        dialog = AddAccountDialog(
            engine=self.engine,
            registry=self.registry,
            on_added=lambda acct: window
            and window.add_toast(_("Added %s. Sign in to finish.") % acct.display_name),
        )
        dialog.present(window)

    def _on_about(self, *_args):
        about = Adw.AboutDialog(
            application_name=_("Clouddrive"),
            application_icon=self.application_id,
            developer_name=_("Fiber Elements"),
            version=self.version,
            license_type=Gtk.License.GPL_3_0,
            website="https://github.com/sha5b/Clouddrive-Fedora",
            issue_url="https://github.com/sha5b/Clouddrive-Fedora/issues",
            copyright="© 2026 Fiber Elements",
        )
        about.present(self.props.active_window)
