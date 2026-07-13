# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Shahab Nedaei
"""Per-account two-way offline sync (rclone bisync).

Distinct from the live streaming *mount*: when an account has ``full_sync`` on,
its libraries are kept as real local folders under ``…/cloudy/synced`` that stay
in sync both ways — local edits upload, cloud changes download. We seed the
baseline with ``rclone bisync --resync`` once, then bisync incrementally on a
timer.

The sync is non-interactive: it only configures an rclone remote from a token
the account already authorized (via the Files → Mount flow). Libraries with no
stored rclone token are skipped (mount one once to grant access).
"""

from __future__ import annotations

import threading
from gettext import gettext as _

from gi.repository import GLib

from ..modules.microsoft365.mounts import MountManager

#: how often (seconds) to re-bisync each enabled account
SYNC_INTERVAL = 600


class SyncManager:
    def __init__(self, app):
        self._app = app
        self._mounts = MountManager()
        self._timers: dict[str, int] = {}  # account_id -> GLib source id
        self._busy: set[str] = set()

    # -- lifecycle --------------------------------------------------------
    def start(self) -> None:
        """Begin syncing every account that has full_sync enabled."""
        self._settings_changed = self._app.settings.connect(
            "changed::offline-sync-enabled", lambda *_: self._on_master_toggled())
        if not self._master_enabled():
            return
        for account in self._app.registry.accounts():
            if account.signed_in and getattr(account, "full_sync", False):
                self.enable(account)

    def stop(self) -> None:
        """Stop all sync timers and disconnect settings handler."""
        if hasattr(self, "_settings_changed") and self._settings_changed:
            self._app.settings.disconnect(self._settings_changed)
            self._settings_changed = None
        for account_id in list(self._timers):
            self._disable_by_id(account_id)

    def _master_enabled(self) -> bool:
        try:
            return self._app.settings.get_boolean("offline-sync-enabled")
        except Exception:  # noqa: BLE001
            return True  # default to enabled if setting is missing

    def _on_master_toggled(self) -> None:
        if self._master_enabled():
            for account in self._app.registry.accounts():
                if account.signed_in and getattr(account, "full_sync", False):
                    self.enable(account)
        else:
            for account_id in list(self._timers):
                self._disable_by_id(account_id)

    def enable(self, account) -> None:
        if not self._master_enabled():
            return
        if account.id not in self._timers:
            self._timers[account.id] = GLib.timeout_add_seconds(
                SYNC_INTERVAL, self._on_timer, account.id
            )
        self.sync_now(account)

    def disable(self, account) -> None:
        self._disable_by_id(account.id)

    def _disable_by_id(self, account_id: str) -> None:
        source = self._timers.pop(account_id, None)
        if source is not None:
            GLib.source_remove(source)

    def _on_timer(self, account_id: str) -> bool:
        account = self._app.registry.get(account_id)
        # signed_in too: a signed-out account must not keep bisyncing on its
        # stored rclone token until the app restarts.
        if account is None or not self._master_enabled() \
                or not getattr(account, "full_sync", False) \
                or not getattr(account, "signed_in", True):
            self._timers.pop(account_id, None)
            return False  # GLib removes the timer
        self.sync_now(account)
        return True

    # -- syncing ----------------------------------------------------------
    def sync_now(self, account) -> None:
        if account.id in self._busy:
            return  # a sync for this account is already running
        self._busy.add(account.id)
        threading.Thread(target=self._worker, args=(account,), daemon=True).start()

    def _worker(self, account) -> None:
        try:
            drives = self._enumerate(account)
            synced = 0
            for drive in drives:
                # Account-scoped, matching mount_drive — the shared unscoped
                # name let two accounts' same-named drives clobber tokens.
                remote = self._mounts.remote_name(drive.name, account.id)
                if not self._ensure_remote(account, drive, remote):
                    continue
                try:
                    self._mounts.bisync(remote, drive.name)
                    synced += 1
                except Exception as exc:  # noqa: BLE001 - one bad library shouldn't stop the rest
                    print(f"[sync] {drive.name}: {exc}")
            GLib.idle_add(self._done, account, synced, len(drives))
        finally:
            self._busy.discard(account.id)

    def _ensure_remote(self, account, drive, remote: str) -> bool:
        """Make sure an rclone remote exists for this drive. Non-interactive:
        reuses the rclone token the account stored when it first mounted."""
        if self._mounts.has_remote(remote):
            return True
        google = account.provider == "google"
        token_kind = "rclone-gdrive" if google else "rclone-onedrive"
        token = self._app.secrets.lookup(account.id, token_kind)
        if not token:
            return False  # rclone never authorized for this account → skip
        try:
            if google:
                self._mounts.create_remote(remote, "drive", {"token": token, "scope": "drive"})
            else:
                self._mounts.create_remote(remote, "onedrive", {
                    "token": token, "drive_id": drive.id,
                    "drive_type": self._mounts.drive_type_for(drive.kind),
                })
            return True
        except Exception:  # noqa: BLE001
            return False

    def _enumerate(self, account) -> list:
        from ..modules.microsoft365.graph import Drive

        if account.provider == "google":
            return [Drive(id="", name="My Drive", kind="google_mydrive", web_url="")]

        from ..widgets.graph_helper import build_graph_client

        drives: list = []
        try:
            graph = build_graph_client(self._app, account)
        except Exception:  # noqa: BLE001
            return drives
        for fetch in (graph.list_drives, graph.list_teams):
            try:
                drives += fetch()
            except Exception:  # noqa: BLE001
                continue
        return drives

    def _done(self, account, synced: int, total: int) -> bool:
        window = self._app.props.active_window
        if window is not None and synced:
            window.add_toast(
                _("Synced %(n)d of %(total)d libraries for %(name)s")
                % {"n": synced, "total": total, "name": account.display_name}
            )
        return False
