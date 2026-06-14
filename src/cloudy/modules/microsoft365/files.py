# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Fiber Elements
"""OneDrive / SharePoint files — the Files capability of Microsoft 365.

Orchestration only: drive/site enumeration goes through the shared Graph client;
mounting a library into the file manager is delegated to the MountManager
(rclone/onedriver) which also adds the Nautilus sidebar bookmark. See
docs/MODULES.md and docs/ARCHITECTURE.md.
"""

from __future__ import annotations

from .abraunegg import AbrauneggClient
from .mounts import MountManager


class OneDriveFiles:
    def __init__(self, graph):
        self._graph = graph
        self.mounts = MountManager()
        self._client = AbrauneggClient()

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
    def create_share_link(self, path: str, *, editable: bool = False) -> str:
        # Path-based link via the host client (used in full-sync mode).
        return self._client.create_share_link(path, editable=editable)
