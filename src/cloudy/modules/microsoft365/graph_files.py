# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Shahab Nedaei
"""Files domain of the Graph client: drive/site/team enumeration, share links."""

from __future__ import annotations

import concurrent.futures
import urllib.parse

from .graph_http import Drive, GraphError

from ...core.auth.msal_graph import (
    SCOPES_FILES,
    SCOPES_TEAMS,
)


class GraphFilesMixin:
    # -- Files: drives & sites -------------------------------------------
    def list_drives(self) -> list[Drive]:
        """The user's own drives (personal OneDrive / business)."""
        return [self._drive_from_json(d)
                for d in self._get_all("/me/drives", SCOPES_FILES)]

    def search_sites(self, query: str) -> list[dict]:
        """Search SharePoint sites (for Teams/SharePoint libraries)."""
        q = urllib.parse.quote(query)
        return [
            {"id": s["id"], "name": s.get("displayName", s.get("name", "")),
             "web_url": s.get("webUrl", "")}
            for s in self._get_all(f"/sites?search={q}", SCOPES_FILES)
        ]

    def list_site_drives(self, site_id: str) -> list[Drive]:
        """Document libraries of a SharePoint site (Teams files live here)."""
        drives = []
        for d in self._get_all(f"/sites/{site_id}/drives", SCOPES_FILES):
            drive = self._drive_from_json(d)
            drive.site_id = site_id
            drives.append(drive)
        return drives

    def list_teams(self) -> list[Drive]:
        """Each Team the user belongs to, as its default document library (drive).

        We mount at the **team level** (the team's Files root), not channels or
        subfolders. Requires the Team.ReadBasic.All scope.

        Each team's drive is a separate request, so we fetch them **concurrently**
        (this is the cold-load bottleneck for users in many Teams). Already runs
        on a worker thread via the views' ``run_async``.
        """
        teams = [t for t in self._get_all("/me/joinedTeams", SCOPES_TEAMS)
                 if t.get("id")]
        if not teams:
            return []
        # Warm the token once so the parallel calls reuse the cached token
        # instead of racing MSAL's cache.
        self._token_provider(SCOPES_FILES)

        def fetch(team) -> Drive | None:
            try:
                d = self._get(f"/groups/{team['id']}/drive", SCOPES_FILES)
            except GraphError:
                return None  # some teams have no provisioned files / no access
            drive = self._drive_from_json(d)
            drive.name = team.get("displayName") or drive.name or "Untitled Team"
            drive.kind = "team"
            return drive

        workers = min(8, len(teams))
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            drives = [d for d in pool.map(fetch, teams) if d is not None]
        drives.sort(key=lambda d: d.name.lower())
        return drives

    def item_by_path(self, drive_id: str, rel_path: str) -> dict:
        """Resolve a path relative to a drive root to a driveItem dict.

        ``drive_id`` may be ``"me"`` to target the current user's default drive.
        """
        rel = urllib.parse.quote(rel_path.lstrip("/"), safe="/")
        if drive_id == "me":
            return self._get(f"/me/drive/root:/{rel}", SCOPES_FILES)
        return self._get(f"/drives/{drive_id}/root:/{rel}", SCOPES_FILES)

    def create_share_link(self, drive_id: str, item_id: str, *, editable: bool = False) -> str:
        body = {"type": "edit" if editable else "view", "scope": "organization"}
        if drive_id == "me":
            path = f"/me/drive/items/{item_id}/createLink"
        else:
            path = f"/drives/{drive_id}/items/{item_id}/createLink"
        data = self._post(path, body, SCOPES_FILES)
        return data.get("link", {}).get("webUrl", "")

    @staticmethod
    def _drive_from_json(d: dict) -> Drive:
        return Drive(
            id=d["id"],
            name=d.get("name", d.get("driveType", "drive")),
            kind=d.get("driveType", "documentLibrary"),
            web_url=d.get("webUrl", ""),
        )
