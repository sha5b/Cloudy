# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Fiber Elements
"""The Adw.Application subclass: actions, lifecycle, and the main window."""

import os
from gettext import gettext as _

from gi.repository import Adw, Gio, Gtk

from .core.account_registry import AccountRegistry
from .core.cache import MemoryCache
from .core.plugin_engine import PluginEngine
from .core.secrets import SecretStore
from .window import CloudyWindow


class CloudyApplication(Adw.Application):
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
        self.secrets = SecretStore()
        self.registry = AccountRegistry(self.settings)
        self.engine = PluginEngine(self.settings)
        self.cache = MemoryCache()

        self._setup_actions()

    # -- OAuth client ids (env override wins over GSettings) -------------
    def microsoft_client_id(self) -> str:
        return os.environ.get("CLOUDY_MS_CLIENT_ID") or self.settings.get_string(
            "microsoft-client-id"
        )

    def google_client_id(self) -> str:
        return os.environ.get("CLOUDY_GOOGLE_CLIENT_ID") or self.settings.get_string(
            "google-client-id"
        )

    def google_client_secret(self) -> str:
        return os.environ.get(
            "CLOUDY_GOOGLE_CLIENT_SECRET"
        ) or self.settings.get_string("google-client-secret")

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
    def do_dbus_register(self, connection, object_path):
        # Register the sync-status service on the app's own bus name so the
        # host Nautilus extension can query emblems / issue commands.
        if not Adw.Application.do_dbus_register(self, connection, object_path):
            return False
        try:
            from .core.dbus_service import SyncStatusService
            from .modules.microsoft365.mounts import mount_root

            self._sync_service = SyncStatusService(connection, mount_root())
            self._sync_service.publish()
        except Exception as exc:  # noqa: BLE001 - never block startup on the bus
            print(f"[dbus] sync service not published: {exc}")
        return True

    def do_dbus_unregister(self, connection, object_path):
        service = getattr(self, "_sync_service", None)
        if service is not None:
            service.unpublish()
        Adw.Application.do_dbus_unregister(self, connection, object_path)

    def do_startup(self):
        Adw.Application.do_startup(self)
        # Discover modules now; activation happens per user settings.
        self.engine.discover()
        self._provision_backends()

    def _provision_backends(self) -> None:
        # Ensure rclone is available without any user/system install (rootless
        # download). Best-effort, off the main thread; the shipped Flatpak
        # bundles rclone so this is usually a no-op.
        import threading

        def worker():
            try:
                from .core.provisioner import ensure_rclone

                ensure_rclone(log=lambda m: print(f"[provision] {m}"))
            except Exception as exc:  # noqa: BLE001 - never block startup
                print(f"[provision] rclone not provisioned: {exc}")

        threading.Thread(target=worker, daemon=True).start()

    def do_activate(self):
        window = self.props.active_window
        if not window:
            window = CloudyWindow(application=self)
        window.present()

    # -- Action handlers --------------------------------------------------
    def _on_quit(self, *_args):
        self.quit()

    def _on_preferences(self, *_args):
        from .preferences import CloudyPreferences

        prefs = CloudyPreferences(engine=self.engine)
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
            application_name=_("Cloudy"),
            application_icon=self.application_id,
            developer_name=_("Fiber Elements"),
            version=self.version,
            license_type=Gtk.License.GPL_3_0,
            website="https://github.com/sha5b/Clouddrive-Fedora",
            issue_url="https://github.com/sha5b/Clouddrive-Fedora/issues",
            copyright="© 2026 Fiber Elements",
        )
        about.present(self.props.active_window)
