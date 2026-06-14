# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Fiber Elements
"""Microsoft Graph REST client shared by all Microsoft 365 capabilities.

A single instance serves Files (drive/site enumeration, share links), Mail, and
Calendar from the same OAuth token, supplied lazily by ``token_provider(scopes)``
so the caller controls auth/refresh (see core.auth.msal_graph).

Files enumeration and share links are implemented here; mail/calendar land in
stage 6.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Callable, Sequence

from ...core.auth.msal_graph import SCOPES_FILES, SCOPES_MAIL

BASE_URL = "https://graph.microsoft.com/v1.0"


class GraphError(Exception):
    pass


@dataclass
class Drive:
    """A OneDrive/SharePoint drive (document library)."""

    id: str
    name: str
    kind: str  # "personal" | "business" | "documentLibrary"
    web_url: str
    site_id: str = ""  # set for SharePoint/Teams libraries


class GraphClient:
    def __init__(self, token_provider: Callable[[Sequence[str]], str | None]):
        self._token_provider = token_provider

    # -- low-level --------------------------------------------------------
    def _get(self, path: str, scopes: Sequence[str], headers: dict | None = None) -> dict:
        token = self._token_provider(scopes)
        if not token:
            raise GraphError("not signed in (no token for the requested scopes)")
        url = path if path.startswith("http") else f"{BASE_URL}{path}"
        hdrs = {"Authorization": f"Bearer {token}"}
        if headers:
            hdrs.update(headers)
        req = urllib.request.Request(url, headers=hdrs)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")
            raise GraphError(f"Graph {exc.code}: {detail}") from exc

    def _post(self, path: str, body: dict, scopes: Sequence[str]) -> dict:
        token = self._token_provider(scopes)
        if not token:
            raise GraphError("not signed in (no token for the requested scopes)")
        url = f"{BASE_URL}{path}"
        data = json.dumps(body).encode()
        req = urllib.request.Request(
            url,
            data=data,
            method="POST",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")
            raise GraphError(f"Graph {exc.code}: {detail}") from exc

    # -- Files: drives & sites -------------------------------------------
    def list_drives(self) -> list[Drive]:
        """The user's own drives (personal OneDrive / business)."""
        data = self._get("/me/drives", SCOPES_FILES)
        return [self._drive_from_json(d) for d in data.get("value", [])]

    def search_sites(self, query: str) -> list[dict]:
        """Search SharePoint sites (for Teams/SharePoint libraries)."""
        q = urllib.parse.quote(query)
        data = self._get(f"/sites?search={q}", SCOPES_FILES)
        return [
            {"id": s["id"], "name": s.get("displayName", s.get("name", "")),
             "web_url": s.get("webUrl", "")}
            for s in data.get("value", [])
        ]

    def site_by_path(self, hostname: str, site_path: str) -> dict:
        """Resolve a site from a hostname + server-relative path."""
        data = self._get(f"/sites/{hostname}:{site_path}", SCOPES_FILES)
        return {"id": data["id"], "name": data.get("displayName", ""),
                "web_url": data.get("webUrl", "")}

    def list_site_drives(self, site_id: str) -> list[Drive]:
        """Document libraries of a SharePoint site (Teams files live here)."""
        data = self._get(f"/sites/{site_id}/drives", SCOPES_FILES)
        drives = []
        for d in data.get("value", []):
            drive = self._drive_from_json(d)
            drive.site_id = site_id
            drives.append(drive)
        return drives

    def create_share_link(self, drive_id: str, item_id: str, *, editable: bool = False) -> str:
        body = {"type": "edit" if editable else "view", "scope": "organization"}
        data = self._post(
            f"/drives/{drive_id}/items/{item_id}/createLink", body, SCOPES_FILES
        )
        return data.get("link", {}).get("webUrl", "")

    @staticmethod
    def _drive_from_json(d: dict) -> Drive:
        return Drive(
            id=d["id"],
            name=d.get("name", d.get("driveType", "drive")),
            kind=d.get("driveType", "documentLibrary"),
            web_url=d.get("webUrl", ""),
        )

    # -- Mail -------------------------------------------------------------
    def list_mail_folders(self) -> list[dict]:
        data = self._get("/me/mailFolders?$top=50", SCOPES_MAIL)
        return [
            {"id": f["id"], "name": f.get("displayName", ""),
             "unread": f.get("unreadItemCount", 0)}
            for f in data.get("value", [])
        ]

    def list_messages(self, folder_id: str = "inbox", *, limit: int = 25) -> list[dict]:
        path = (
            f"/me/mailFolders/{folder_id}/messages"
            f"?$top={limit}&$select=subject,from,receivedDateTime,bodyPreview,isRead"
            f"&$orderby=receivedDateTime desc"
        )
        data = self._get(path, SCOPES_MAIL)
        out = []
        for m in data.get("value", []):
            sender = (
                m.get("from", {}).get("emailAddress", {}) if m.get("from") else {}
            )
            out.append({
                "id": m["id"],
                "subject": m.get("subject", "(no subject)"),
                "from": sender.get("name") or sender.get("address", ""),
                "received": m.get("receivedDateTime", ""),
                "preview": m.get("bodyPreview", ""),
                "is_read": m.get("isRead", True),
            })
        return out

    def get_message(self, message_id: str) -> dict:
        """Full message with a plain-text body."""
        data = self._get(
            f"/me/messages/{message_id}"
            f"?$select=subject,from,toRecipients,receivedDateTime,body",
            SCOPES_MAIL,
            headers={"Prefer": 'outlook.body-content-type="text"'},
        )
        sender = (data.get("from") or {}).get("emailAddress", {})
        to = ", ".join(
            r.get("emailAddress", {}).get("address", "")
            for r in data.get("toRecipients", [])
        )
        return {
            "id": data.get("id", message_id),
            "subject": data.get("subject", "(no subject)"),
            "from": sender.get("name") or sender.get("address", ""),
            "to": to,
            "received": data.get("receivedDateTime", ""),
            "body": (data.get("body") or {}).get("content", ""),
        }

    # -- Calendar ---------------------------------------------------------
    def list_calendars(self) -> list[dict]:
        data = self._get("/me/calendars?$top=50", SCOPES_MAIL)
        return [
            {"id": c["id"], "name": c.get("name", "")}
            for c in data.get("value", [])
        ]

    def list_events(self, start_iso: str, end_iso: str, *, limit: int = 50) -> list[dict]:
        """Calendar view between two ISO-8601 UTC timestamps."""
        params = urllib.parse.urlencode({
            "startDateTime": start_iso,
            "endDateTime": end_iso,
            "$orderby": "start/dateTime",
            "$top": str(limit),
            "$select": "subject,start,end,location,isAllDay",
        })
        data = self._get(f"/me/calendarView?{params}", SCOPES_MAIL)
        out = []
        for e in data.get("value", []):
            out.append({
                "id": e["id"],
                "subject": e.get("subject", "(no title)"),
                "start": e.get("start", {}).get("dateTime", ""),
                "end": e.get("end", {}).get("dateTime", ""),
                "location": (e.get("location") or {}).get("displayName", ""),
                "all_day": e.get("isAllDay", False),
            })
        return out
