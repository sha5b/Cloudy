# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Shahab Nedaei
"""The Adw.Application subclass: actions, lifecycle, and the main window."""

import os
from gettext import gettext as _

from gi.repository import Adw, Gio, GLib, Gtk

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
            # HANDLES_OPEN so we can act as the system Mail/Calendar handler:
            # mailto: URIs and .ics/webcal files are delivered to do_open().
            flags=Gio.ApplicationFlags.HANDLES_OPEN,
        )
        self.version = version
        self.application_id = application_id

        # Core services, constructed once and shared with the window/modules.
        self.settings = Gio.Settings.new(application_id)
        self.secrets = SecretStore()
        self.registry = AccountRegistry(self.settings)
        self.engine = PluginEngine(self.settings)
        self.cache = MemoryCache()

        from .core.sync import SyncManager

        self.sync_manager = SyncManager(self)

        from .core.notifications import NotificationManager

        self.notifier = NotificationManager(self)

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
        # Notification deep-links carry a string target.
        self._add_target_action("notify-open-mail", self._on_notify_open_mail)
        self._add_target_action("notify-open-calendar", self._on_notify_open_calendar)

    def _add_target_action(self, name, callback):
        action = Gio.SimpleAction.new(name, GLib.VariantType.new("s"))
        action.connect("activate", callback)
        self.add_action(action)

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
                from .core.provisioner import (
                    ensure_host_nautilus_extension,
                    ensure_rclone,
                )

                ensure_rclone(log=lambda m: print(f"[provision] {m}"))
                # In Flatpak, place the host Nautilus extension on first run.
                ensure_host_nautilus_extension(
                    log=lambda m: print(f"[provision] {m}"))
            except Exception as exc:  # noqa: BLE001 - never block startup
                print(f"[provision] rclone not provisioned: {exc}")

        threading.Thread(target=worker, daemon=True).start()

    def do_activate(self):
        window = self.props.active_window
        if not window:
            window = CloudyWindow(application=self)
        window.present()
        # Coming back to the foreground: drop the background hold (the visible
        # window keeps the app alive from here).
        if getattr(self, "_held", False):
            self.release()
            self._held = False
        if not getattr(self, "_sync_started", False):
            self._sync_started = True
            self.sync_manager.start()
            self.notifier.start()

    # -- run in background ------------------------------------------------
    def wants_background(self) -> bool:
        try:
            return self.settings.get_boolean("run-in-background")
        except Exception:  # noqa: BLE001
            return False

    def enter_background(self, window) -> None:
        """Hide the window but keep the app (and its pollers) running."""
        window.set_visible(False)
        if not getattr(self, "_held", False):
            self.hold()  # survive having no visible window
            self._held = True
        self._request_portal_background()

    def _request_portal_background(self) -> None:
        # Best-effort: ask the desktop portal to allow running in the background
        # (and list us in GNOME's "Background Apps"). Harmless if unavailable.
        if getattr(self, "_portal_asked", False):
            return
        self._portal_asked = True
        try:
            from .core.gi_compat import require

            if require("Xdp", ("1.0",)) is None:
                return  # no portal backend on this runtime
            from gi.repository import Xdp

            portal = Xdp.Portal.new()
            portal.request_background(
                None, _("Keep mail and calendar up to date"),
                ["cloudy", "--gapplication-service"],
                Xdp.BackgroundFlags.NONE, None, None, None)
        except Exception as exc:  # noqa: BLE001 - never block on the portal
            print(f"[background] portal request skipped: {exc}")

    def do_open(self, files, _n_files, _hint):
        # Invoked when launched as the default Mail/Calendar app. Ensure a window
        # exists, then route each argument by scheme/type.
        self.activate()
        window = self.props.active_window
        if window is None:
            return
        for gfile in files:
            uri = gfile.get_uri() or ""
            if uri.startswith("mailto:"):
                window.open_compose_from_mailto(uri)
            elif uri.startswith("webcal:"):
                # A remote calendar subscription; hand off to the browser/portal.
                window.open_uri("https" + uri[len("webcal"):])
            else:
                path = gfile.get_path()
                if path and path.lower().endswith(".ics"):
                    window.open_event_from_ics(path)

    # -- Action handlers --------------------------------------------------
    def _on_notify_open_mail(self, _action, param):
        self.activate()
        window = self.props.active_window
        if window is None:
            return
        account_id, _sep, mid = param.get_string().partition("\x1f")
        account = self.registry.get(account_id)
        if account is not None and mid:
            window.open_mail(account, mid)
        window.present()

    def _on_notify_open_calendar(self, _action, param):
        self.activate()
        window = self.props.active_window
        if window is None:
            return
        account = self.registry.get(param.get_string())
        if account is not None:
            window.open_account_tab(account, "calendar")
        window.present()

    def _on_quit(self, *_args):
        self.quit()

    def _on_preferences(self, *_args):
        from .preferences import CloudyPreferences

        prefs = CloudyPreferences(engine=self.engine, settings=self.settings, app=self)
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
            developer_name=_("Shahab Nedaei"),
            version=self.version,
            license_type=Gtk.License.GPL_3_0,
            website="https://github.com/sha5b/Clouddrive-Fedora",
            issue_url="https://github.com/sha5b/Clouddrive-Fedora/issues",
            copyright="© 2026 Shahab Nedaei",
        )
        about.present(self.props.active_window)
