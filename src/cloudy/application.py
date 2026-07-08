# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Shahab Nedaei
"""The Adw.Application subclass: actions, lifecycle, and the main window."""

import os
import threading
from gettext import gettext as _
from typing import Any

from gi.repository import Adw, Gio, GLib, Gtk

from .core.account_registry import AccountRegistry
from .core.cache import MemoryCache
from .core.interfaces import ModuleContext
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

        # The app id was renamed io.github.sha5b.Clouddrive -> .Cloudy, which
        # moves the GSettings/dconf path; carry the user's accounts + prefs over.
        self._migrate_legacy_settings()

        # Core services, constructed once and shared with the window/modules.
        self.settings = Gio.Settings.new(application_id)
        self.settings.connect("changed::eds-publish-enabled",
                              self._on_eds_publish_enabled_changed)
        self.secrets = SecretStore()
        self.registry = AccountRegistry(self.settings)
        self.engine = PluginEngine(self.settings)
        self.engine.set_context(
            ModuleContext(settings=self.settings,
                          secrets=self.secrets,
                          registry=self.registry))
        # Per-account API clients are expensive to build (MSAL token cache,
        # PublicClientApplication, etc.) and are reused for the lifetime of the
        # account. Evicted on sign-out / removal so stale tokens aren't reused.
        self._account_client_lock = threading.RLock()
        self._account_clients: dict[str, Any] = {}
        # Persist the cache so mail/agenda show last-known data offline on launch.
        from pathlib import Path

        cache_path = Path(GLib.get_user_cache_dir()) / "cloudy" / "cache.json"
        self.cache = MemoryCache(path=cache_path)

        from .core.sync import SyncManager

        self.sync_manager = SyncManager(self)

        from .core.notifications import NotificationManager

        self.notifier = NotificationManager(self)

        self._setup_actions()

    @staticmethod
    def _migrate_legacy_settings() -> None:
        """One-time, best-effort copy of pre-rename settings (accounts + prefs).

        The app id changed ``io.github.sha5b.Clouddrive`` → ``.Cloudy``, moving
        the dconf subtree; without this the user's accounts and preferences would
        come up empty after the rename. Copies the old subtree to the new path on
        first run only — guarded so it never raises, no-ops if ``dconf`` isn't on
        PATH (e.g. inside the Flatpak runtime), the old data is gone, or the new
        config already exists (don't clobber a fresh/already-migrated setup)."""
        import shutil
        import subprocess

        dconf = shutil.which("dconf")
        if not dconf:
            return
        old, new = "/io/github/sha5b/Clouddrive/", "/io/github/sha5b/Cloudy/"
        try:
            existing = subprocess.run(
                [dconf, "read", new + "accounts"],
                capture_output=True, text=True, timeout=5).stdout.strip()
            if existing:
                return  # already migrated or a fresh config — leave it alone
            dump = subprocess.run(
                [dconf, "dump", old], capture_output=True, text=True, timeout=5)
            if dump.returncode != 0 or not dump.stdout.strip():
                return  # nothing to migrate
            subprocess.run([dconf, "load", new], input=dump.stdout, text=True,
                           timeout=5, check=False)
        except (OSError, subprocess.SubprocessError):
            pass

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

    # -- Per-account API client cache --------------------------------------
    def get_account_client(self, account) -> Any:
        """Return the cached client for ``account``, if any."""
        with self._account_client_lock:
            return self._account_clients.get(account.id)

    def set_account_client(self, account, client: Any) -> None:
        with self._account_client_lock:
            self._account_clients[account.id] = client

    def evict_account_client(self, account_id: str) -> None:
        """Drop a cached client (called on sign-out / account removal)."""
        with self._account_client_lock:
            self._account_clients.pop(account_id, None)

    def _setup_actions(self) -> None:
        self._add_action("quit", self._on_quit, ["<primary>q"])
        self._add_action("about", self._on_about)
        self._add_action("preferences", self._on_preferences, ["<primary>comma"])
        self._add_action("add-account", self._on_add_account)
        self._add_action("command-palette", self._on_command_palette,
                         ["<primary>k"])
        # Notification deep-links carry a string target.
        self._add_target_action("notify-open-mail", self._on_notify_open_mail)
        self._add_target_action("notify-open-calendar", self._on_notify_open_calendar)
        self._add_target_action("notify-open-chat", self._on_notify_open_chat)
        self._add_target_action("notify-join-meeting", self._on_notify_join_meeting)

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

    def _on_eds_publish_enabled_changed(self, settings, _key) -> None:
        """When the EDS mirror is disabled, wipe the Cloudy calendar so stale
        events don't linger in GNOME Calendar. Re-enabling is handled by the
        preferences backfill path."""
        if settings.get_boolean("eds-publish-enabled"):
            return
        try:
            from .core.eds_publish import clear_all_events_async

            clear_all_events_async(self)
        except Exception:  # noqa: BLE001 - EDS cleanup is best-effort
            pass

    def do_startup(self):
        Adw.Application.do_startup(self)
        self._load_styles()
        # Discover modules now; activation happens per user settings.
        self.engine.discover()
        self._provision_backends()

    def do_shutdown(self):
        # Stop background timers so they don't outlive the app.
        if hasattr(self, "notifier") and self.notifier is not None:
            try:
                self.notifier.stop()
            except Exception:  # noqa: BLE001 - never block shutdown
                pass
        if hasattr(self, "engine") and self.engine is not None:
            try:
                self.engine.shutdown()
            except Exception:  # noqa: BLE001 - never block shutdown
                pass
        if hasattr(self, "sync_manager") and self.sync_manager is not None:
            try:
                self.sync_manager.stop()
            except Exception:  # noqa: BLE001 - never block shutdown
                pass
        # Persist any pending cache so the next launch shows mail/agenda offline.
        try:
            self.cache.flush()
        except Exception:  # noqa: BLE001 - never block shutdown on the cache
            pass
        Adw.Application.do_shutdown(self)

    def _load_styles(self) -> None:
        """Load the app stylesheet (widgets/metrics.py holds the matching
        spacing scale). Best-effort: a missing resource must never crash."""
        try:
            from gi.repository import Gdk, Gtk

            provider = Gtk.CssProvider()
            provider.load_from_resource(
                "/io/github/sha5b/Cloudy/style.css")
            display = Gdk.Display.get_default()
            if display is not None:
                Gtk.StyleContext.add_provider_for_display(
                    display, provider,
                    Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
            self._css_provider = provider
        except Exception as exc:  # noqa: BLE001
            print(f"[style] could not load stylesheet: {exc}")

    def _provision_backends(self) -> None:
        # Ensure rclone is available without any user/system install (rootless
        # download). Best-effort, off the main thread; the shipped Flatpak
        # bundles rclone so this is usually a no-op.
        def worker():
            try:
                from .core.provisioner import (
                    ensure_rclone,
                    set_host_nautilus_extension,
                )

                ensure_rclone(log=lambda m: print(f"[provision] {m}"))
                # Install/remove the host Nautilus extension to match the
                # user's toggle (default on).
                enabled = True
                try:
                    enabled = self.settings.get_boolean("nautilus-extension-enabled")
                except Exception:  # noqa: BLE001 - never block startup on settings
                    pass
                set_host_nautilus_extension(
                    enabled, log=lambda m: print(f"[provision] {m}"))
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
            self._log_active_mounts()
            self._remount_saved_drives(verbose=True)
            # Health watchdog: re-check remembered mounts periodically so a
            # crashed rclone daemon reconnects on its own (every 90s; quiet).
            GLib.timeout_add_seconds(
                90, lambda: (self._remount_saved_drives(verbose=False), True)[1])

    def _log_active_mounts(self) -> None:
        """Print which Cloudy drives are mounted at startup.

        Checkable in both RPM and Flatpak via the app's stdout / journal
        (``journalctl --user -t cloudy | grep mounts`` or the launch terminal).
        Reads the kernel mount table, so it never stalls on a hung FUSE mount.
        """
        try:
            from .modules.microsoft365.mounts import MountManager, mount_root

            root = str(mount_root())
            mounted = sorted(
                m for m in MountManager.active_mounts() if m.startswith(root)
            )
            if mounted:
                print(f"[mounts] active Cloudy mounts at startup ({len(mounted)}):")
                for m in mounted:
                    print(f"[mounts]   • {m}")
            else:
                print("[mounts] no Cloudy drives mounted at startup")
        except Exception as exc:  # noqa: BLE001 - never block startup on logging
            print(f"[mounts] could not enumerate mounts: {exc}")

    def _remount_saved_drives(self, *, verbose: bool) -> None:
        """Bring back drives the user mounted that aren't currently healthy.

        FUSE mounts are live ``rclone mount`` daemons that die on reboot (and can
        crash mid-session), so we re-run the mounts the user asked to keep. Used
        both at startup (``verbose``) and as a periodic watchdog (quiet). Mounting
        is blocking (rclone subprocesses + a ``ps`` health probe), so it runs on a
        daemon thread and never stalls the UI.
        """
        def worker():
            try:
                from .modules.microsoft365.mounts import remount_saved

                log = (lambda m: print(f"[mounts] {m}")) if verbose else (lambda _m: None)
                n = remount_saved(self.registry, self.secrets, log=log)
                if n:  # always report an actual (re)mount, even when quiet
                    print(f"[mounts] auto-(re)mounted {n} drive(s)")
            except Exception as exc:  # noqa: BLE001 - never block on the watchdog
                if verbose:
                    print(f"[mounts] auto-remount failed: {exc}")

        threading.Thread(target=worker, daemon=True).start()

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
        if account is not None:
            if mid:
                window.open_mail(account, mid)
            else:  # digest summary: no single message — just open the mailbox
                window.open_account_tab(account, "mail")
        window.present()

    def _on_notify_open_calendar(self, _action, param):
        self.activate()
        window = self.props.active_window
        if window is None:
            return
        account_id, _sep, event_id = param.get_string().partition("\x1f")
        account = self.registry.get(account_id)
        if account is not None:
            if event_id:
                window.open_calendar_event(account, event_id)
            else:
                window.open_account_tab(account, "calendar")
        window.present()

    def _on_notify_join_meeting(self, _action, param):
        """The "Join" button on a meeting-start notification: open the meeting's
        join URL in the browser (no need to raise the app window). Uses the
        portal-aware launcher so it works in the Flatpak sandbox, where
        Gio.AppInfo.launch_default_for_uri silently no-ops."""
        uri = param.get_string()
        if not uri:
            return
        win = self.props.active_window
        if win is not None and hasattr(win, "open_uri"):
            win.open_uri(uri)
            return
        from gi.repository import Gtk

        try:
            Gtk.show_uri(None, uri, 0)
        except Exception:  # noqa: BLE001
            Gio.AppInfo.launch_default_for_uri(uri, None)

    def _on_notify_open_chat(self, _action, param):
        self.activate()
        window = self.props.active_window
        if window is None:
            return
        account_id, _sep, chat_id = param.get_string().partition("\x1f")
        account = self.registry.get(account_id)
        if account is not None:
            if chat_id:
                window.open_chat(account, chat_id)
            else:  # digest summary: no single chat — just open the chat tab
                window.open_account_tab(account, "chat")
        window.present()

    def _on_command_palette(self, *_args):
        window = self.props.active_window
        if window is None:
            return
        from .widgets.command_palette import CommandPalette

        CommandPalette(window).present(window)

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
            license_type=Gtk.License.GPL_3_0_OR_LATER,
            website="https://github.com/sha5b/Cloudy",
            issue_url="https://github.com/sha5b/Cloudy/issues",
            copyright="© 2026 Shahab Nedaei",
        )
        about.present(self.props.active_window)
