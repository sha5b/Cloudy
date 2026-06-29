# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Shahab Nedaei
"""OneDrive / SharePoint files — the Files capability of Microsoft 365.

Orchestration only: drive/site enumeration goes through the shared Graph client;
mounting a library into the file manager is delegated to the MountManager
(rclone/onedriver) which also adds the Nautilus sidebar bookmark. See
docs/MODULES.md and docs/ARCHITECTURE.md.
"""

from __future__ import annotations

from pathlib import Path

from .mounts import MountManager, load_mount_records, mount_root


class OneDriveFiles:
    def __init__(self, graph):
        self._graph = graph
        self.mounts = MountManager()

    # -- enumeration (Graph) ---------------------------------------------
    def list_drives(self) -> list:
        return self._graph.list_drives()

    def list_teams(self) -> list:
        return self._graph.list_teams()

    def search_sites(self, query: str) -> list:
        return self._graph.search_sites(query)

    def list_site_drives(self, site_id: str) -> list:
        return self._graph.list_site_drives(site_id)

    # -- mount control ----------------------------------------------------
    def mount_drive(self, drive) -> "object":
        """Mount a drive/library so it appears in the file manager.

        ``drive.name`` becomes the mountpoint and sidebar label. The rclone/
        onedriver remote auth is handled by the backend on first mount (live
        integration); here we orchestrate the mount + bookmark.
        """
        remote = self.mounts._safe_name(drive.name)
        return self.mounts.mount(name=drive.name, remote=remote, drive_id=drive.id)

    def unmount_drive(self, drive) -> None:
        self.mounts.unmount(self.mounts.mountpoint_for(drive.name))

    def is_mounted(self, drive) -> bool:
        return self.mounts.is_mounted(self.mounts.mountpoint_for(drive.name))

    # -- share links ------------------------------------------------------
    def _resolve_path(self, path: str) -> tuple[str, str]:
        """Map a local filesystem path to ``(drive_id, relative_path)``.

        Uses remembered mount records (including the stored mountpoint) so a
        SharePoint/team-drive file resolves to the correct drive/item instead of
        being treated as the user's personal OneDrive.
        """
        p = Path(path).expanduser().resolve()

        # 1. Match against stored mountpoints.
        for rec in load_mount_records():
            mp = rec.get("mountpoint")
            if not mp:
                continue
            try:
                rel = p.relative_to(Path(mp))
            except ValueError:
                continue
            drive_id = rec.get("drive_id") or ""
            if drive_id:
                return drive_id, str(rel)

        # 2. Fallback: recompute the per-account mount base from the account id.
        for rec in load_mount_records():
            account_id = rec.get("account_id", "")
            drive_name = rec.get("drive_name", "")
            drive_id = rec.get("drive_id") or ""
            if not account_id or not drive_id:
                continue
            base = mount_root() / MountManager._safe_name(account_id)
            mp = self.mounts.mountpoint_for(drive_name, base)
            try:
                rel = p.relative_to(mp)
            except ValueError:
                continue
            return drive_id, str(rel)

        # 3. Last resort: assume the user's default drive and a path relative to
        #    the home directory so a bare file path doesn't fail completely.
        home = Path.home()
        try:
            return "me", str(p.relative_to(home))
        except ValueError:
            return "me", str(p)

    def create_share_link(self, path: str, *, editable: bool = False) -> str:
        drive_id, rel_path = self._resolve_path(path)
        item = self._graph.item_by_path(drive_id, rel_path)
        return self._graph.create_share_link(
            drive_id, item["id"], editable=editable
        )
