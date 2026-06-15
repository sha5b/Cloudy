# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Shahab Nedaei
"""Microsoft Graph REST client shared by all Microsoft 365 capabilities.

A single instance serves Files (drive/site enumeration, share links), Mail, and
Calendar from the same OAuth token, supplied lazily by ``token_provider(scopes)``
so the caller controls auth/refresh (see core.auth.msal_graph).

Files enumeration and share links are implemented here; mail/calendar land in
stage 6.
"""

from __future__ import annotations

import concurrent.futures
import html
import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Callable, Sequence

from ...core.auth.msal_graph import (
    SCOPES_FILES,
    SCOPES_GROUPS,
    SCOPES_MAIL,
    SCOPES_MAIL_SHARED,
    SCOPES_PEOPLE,
    SCOPES_TEAMS,
)

BASE_URL = "https://graph.microsoft.com/v1.0"


class GraphError(Exception):
    pass


def _split_id(value: str, count: int) -> list[str]:
    """Split a prefixed source/message ID (``shared:addr:id`` / ``group:id:id``)
    into exactly ``count`` parts, or raise rather than ``ValueError``-crash on a
    malformed/truncated ID coming back from the UI."""
    parts = value.split(":", count - 1)
    if len(parts) != count:
        raise GraphError(f"malformed ID: {value!r}")
    return parts


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
        return self._write("POST", path, body, scopes)

    def _patch(self, path: str, body: dict, scopes: Sequence[str]) -> dict:
        return self._write("PATCH", path, body, scopes)

    def _write(self, method: str, path: str, body: dict | None,
               scopes: Sequence[str]) -> dict:
        token = self._token_provider(scopes)
        if not token:
            raise GraphError("not signed in (no token for the requested scopes)")
        url = f"{BASE_URL}{path}"
        headers = {"Authorization": f"Bearer {token}"}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")
            raise GraphError(f"Graph {exc.code}: {detail}") from exc

    def _delete(self, path: str, scopes: Sequence[str]) -> None:
        token = self._token_provider(scopes)
        if not token:
            raise GraphError("not signed in (no token for the requested scopes)")
        req = urllib.request.Request(
            f"{BASE_URL}{path}", method="DELETE",
            headers={"Authorization": f"Bearer {token}"},
        )
        try:
            with urllib.request.urlopen(req, timeout=30):
                return
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

    def list_teams(self) -> list[Drive]:
        """Each Team the user belongs to, as its default document library (drive).

        We mount at the **team level** (the team's Files root), not channels or
        subfolders. Requires the Team.ReadBasic.All scope.

        Each team's drive is a separate request, so we fetch them **concurrently**
        (this is the cold-load bottleneck for users in many Teams). Already runs
        on a worker thread via the views' ``run_async``.
        """
        data = self._get("/me/joinedTeams", SCOPES_TEAMS)
        teams = [t for t in data.get("value", []) if t.get("id")]
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
    # Surface the everyday folders first; everything else falls in alphabetically.
    _FOLDER_PRIORITY = {
        "Inbox": 0, "Drafts": 1, "Sent Items": 2, "Archive": 3,
        "Deleted Items": 4, "Junk Email": 5, "Outbox": 6,
    }

    def list_folders(self) -> list[dict]:
        """Personal mail folders: ``[{id, name, unread}]``, inbox first.

        M365 group mailboxes are surfaced separately via :meth:`list_groups`
        (the Mail view shows them under a dedicated "Groups" tab)."""
        folders = self.list_mail_folders()
        folders.sort(
            key=lambda f: (self._FOLDER_PRIORITY.get(f["name"], 99), f["name"].lower())
        )
        return folders

    def list_groups(self) -> list[dict]:
        """M365 (Unified) groups the user belongs to: ``[{id, name}]``.

        These have a shared mailbox (conversations) and a group calendar.
        Needs Group.Read.All (usually admin-consented).
        """
        data = self._get(
            "/me/memberOf?$select=id,displayName,groupTypes,mailEnabled&$top=100",
            SCOPES_GROUPS,
        )
        out = []
        for g in data.get("value", []):
            if g.get("@odata.type") != "#microsoft.graph.group":
                continue
            if "Unified" not in (g.get("groupTypes") or []):
                continue  # only M365 groups have a mailbox + calendar
            out.append({"id": g["id"], "name": g.get("displayName", "")})
        out.sort(key=lambda g: g["name"].lower())
        return out

    def list_mail_folders(self) -> list[dict]:
        return self._mail_folders("/me/mailFolders", SCOPES_MAIL)

    def list_shared_folders(self, address: str) -> list[dict]:
        """Folders of a shared/other mailbox you have delegated access to.
        Folder ids are prefixed ``shared:<address>:`` (needs Mail.ReadWrite.Shared)."""
        folders = self._mail_folders(f"/users/{address}/mailFolders", SCOPES_MAIL_SHARED)
        for f in folders:
            f["id"] = f"shared:{address}:{f['id']}"
        folders.sort(
            key=lambda f: (self._FOLDER_PRIORITY.get(f["name"], 99), f["name"].lower())
        )
        return folders

    def _mail_folders(self, base: str, scopes) -> list[dict]:
        data = self._get(f"{base}?$top=50", scopes)
        return [
            {"id": f["id"], "name": f.get("displayName", ""),
             "unread": f.get("unreadItemCount", 0)}
            for f in data.get("value", [])
        ]

    def list_messages(self, folder_id: str = "inbox", *, limit: int = 25) -> list[dict]:
        if folder_id.startswith("group:"):
            return self._list_group_threads(folder_id.split(":", 1)[1], limit)
        if folder_id.startswith("shared:"):
            _, address, fid = _split_id(folder_id, 3)
            return self._list_folder_messages(f"/users/{address}", fid, limit,
                                              id_prefix=f"shared:{address}:",
                                              scopes=SCOPES_MAIL_SHARED)
        return self._list_folder_messages("/me", folder_id, limit)

    def _list_folder_messages(self, scope_base: str, folder_id: str, limit: int,
                              *, id_prefix: str = "", scopes=SCOPES_MAIL) -> list[dict]:
        # Keep $/commas literal (Graph-friendly); only the space in the orderby
        # value needs encoding (a raw space makes urllib reject the URL).
        orderby = urllib.parse.quote("receivedDateTime desc")
        path = (
            f"{scope_base}/mailFolders/{folder_id}/messages"
            f"?$top={limit}"
            f"&$select=subject,from,receivedDateTime,bodyPreview,isRead,importance,flag"
            f"&$orderby={orderby}"
        )
        data = self._get(path, scopes)
        out = []
        for m in data.get("value", []):
            row = self._message_row(m)
            row["id"] = f"{id_prefix}{m['id']}"
            out.append(row)
        return out

    @staticmethod
    def _message_row(m: dict) -> dict:
        sender = m.get("from", {}).get("emailAddress", {}) if m.get("from") else {}
        return {
            "id": m["id"],
            "subject": html.unescape(m.get("subject", "(no subject)")),
            "from": html.unescape(sender.get("name") or sender.get("address", "")),
            "received": m.get("receivedDateTime", ""),
            "preview": html.unescape(m.get("bodyPreview", "")),
            "is_read": m.get("isRead", True),
            "important": m.get("importance") == "high",
            "starred": (m.get("flag") or {}).get("flagStatus") == "flagged",
        }

    def _list_group_threads(self, group_id: str, limit: int) -> list[dict]:
        """A group mailbox's conversation threads, shaped like messages."""
        data = self._get(f"/groups/{group_id}/threads?$top={limit}", SCOPES_GROUPS)
        out = []
        for t in data.get("value", []):
            senders = t.get("uniqueSenders") or []
            out.append({
                "id": f"group:{group_id}:{t['id']}",
                "subject": html.unescape(t.get("topic", "") or "(no subject)"),
                "from": html.unescape(senders[-1] if senders else ""),
                "received": t.get("lastDeliveredDateTime", ""),
                "preview": html.unescape(t.get("preview", "")),
                "is_read": True,  # group conversations have no per-user read state
                "important": False,
                "starred": False,
            })
        # Threads aren't reliably ordered server-side; newest first locally.
        out.sort(key=lambda m: m.get("received", ""), reverse=True)
        return out

    def _get_group_thread(self, message_id: str) -> dict:
        """Render a whole group conversation thread (all posts) as one message."""
        _, group_id, thread_id = _split_id(message_id, 3)
        data = self._get(
            f"/groups/{group_id}/threads/{thread_id}?$expand=posts", SCOPES_GROUPS
        )
        posts = data.get("posts", []) or []
        parts, first_from = [], ""
        for p in posts:
            sender = (p.get("from") or {}).get("emailAddress", {})
            name = sender.get("name") or sender.get("address", "")
            first_from = first_from or name
            when = p.get("receivedDateTime", "")
            body = (p.get("body") or {}).get("content", "")
            parts.append(
                "<div style='margin:0 0 22px'>"
                "<div style='color:#5e5c64;font-size:13px;margin-bottom:6px'>"
                f"<b>{html.escape(name)}</b> · {html.escape(when)}</div>"
                f"{body}</div>"
            )
        return {
            "id": message_id,
            "subject": html.unescape(data.get("topic", "") or "(no subject)"),
            "from": html.unescape(first_from),
            "to": "",
            "received": posts[0].get("receivedDateTime", "") if posts else "",
            "body": "<hr style='border:none;border-top:1px solid #e0e0e0'>".join(parts),
            "body_html": True,
        }

    def get_message(self, message_id: str) -> dict:
        """Full message; ``body`` is the original HTML when available.

        We deliberately do *not* ask Graph for a text body anymore — the reader
        renders the real HTML, so we keep the formatting/links/images intact.
        """
        if message_id.startswith("group:"):
            return self._get_group_thread(message_id)
        scope_base, raw_id, scopes = self._message_scope(message_id)
        data = self._get(
            f"{scope_base}/messages/{raw_id}"
            f"?$select=subject,from,toRecipients,receivedDateTime,body,bodyPreview",
            scopes,
        )
        sender = (data.get("from") or {}).get("emailAddress", {})
        to = ", ".join(
            r.get("emailAddress", {}).get("address", "")
            for r in data.get("toRecipients", [])
        )
        body = data.get("body") or {}
        # NB: meetingMessageType (which would let us synthesize a "X accepted"
        # card) lives on the derived eventMessage type and isn't reachable via
        # $select or an entity-cast on this Graph endpoint (both 400). So empty-
        # bodied meeting notifications just fall back to the reader's
        # "No message content" placeholder — see message_view.
        meeting_response = ""
        return {
            "id": message_id,
            "subject": html.unescape(data.get("subject", "(no subject)")),
            "from": html.unescape(sender.get("name") or sender.get("address", "")),
            "to": html.unescape(to),
            "received": data.get("receivedDateTime", ""),
            "body": body.get("content", ""),
            "body_html": body.get("contentType") == "html",
            # Server-rendered text preview — used as a fallback when the body is
            # empty (e.g. meeting acceptance/decline notifications carry no body).
            "preview": html.unescape(data.get("bodyPreview", "")),
            "meeting_response": meeting_response,
        }

    @staticmethod
    def _message_scope(message_id: str):
        """Return (api base, raw message id, scopes) for a personal/shared message."""
        if message_id.startswith("shared:"):
            _, address, raw_id = _split_id(message_id, 3)
            return f"/users/{address}", raw_id, SCOPES_MAIL_SHARED
        return "/me", message_id, SCOPES_MAIL

    def mark_read(self, message_id: str, read: bool = True) -> None:
        """Set the read/unread state of a message (needs Mail.ReadWrite[.Shared])."""
        if message_id.startswith("group:"):
            return  # group conversations have no per-user read state
        base, raw_id, scopes = self._message_scope(message_id)
        self._patch(f"{base}/messages/{raw_id}", {"isRead": read}, scopes)

    def delete_message(self, message_id: str) -> None:
        """Move a message to Deleted Items (Graph DELETE is recoverable)."""
        if message_id.startswith("group:"):
            raise GraphError("Group conversations can't be deleted from here.")
        base, raw_id, scopes = self._message_scope(message_id)
        self._delete(f"{base}/messages/{raw_id}", scopes)

    # -- compose / reply --------------------------------------------------
    @staticmethod
    def _recipients(addresses) -> list[dict]:
        return [{"emailAddress": {"address": a}} for a in addresses if a]

    def send_mail(self, *, to, subject: str, body: str, source: str = "me",
                  address: str | None = None, cc=None, html: bool = False) -> None:
        """Send a new message *as the current source*.

        ``source='me'`` sends from the signed-in user (``/me/sendMail``);
        ``source='shared'`` sends as a shared mailbox you have Send-As on
        (``/users/{address}/sendMail`` + Mail.Send.Shared). M365 groups have no
        plain sendMail identity, so the Mail view falls back to ``'me'`` for new
        messages (group replies go through :meth:`reply_mail`)."""
        message = {
            "subject": subject,
            "body": {"contentType": "HTML" if html else "Text", "content": body},
            "toRecipients": self._recipients(to),
        }
        if cc:
            message["ccRecipients"] = self._recipients(cc)
        payload = {"message": message, "saveToSentItems": True}
        if source == "shared" and address:
            self._post(f"/users/{address}/sendMail", payload, SCOPES_MAIL_SHARED)
        else:
            self._post("/me/sendMail", payload, SCOPES_MAIL)

    def reply_mail(self, message_id: str, body: str, *, reply_all: bool = False,
                   html: bool = False) -> None:
        """Reply to a message, keeping it on its thread.

        Personal/shared messages use Graph's ``reply``/``replyAll`` action; a
        group conversation thread is answered with a new post on the thread."""
        comment = {"contentType": "HTML" if html else "Text", "content": body}
        if message_id.startswith("group:"):
            _, gid, tid = _split_id(message_id, 3)
            self._post(f"/groups/{gid}/threads/{tid}/reply",
                       {"post": {"body": comment}}, SCOPES_GROUPS)
            return
        base, raw_id, scopes = self._message_scope(message_id)
        action = "replyAll" if reply_all else "reply"
        # The reply action accepts a draft override via "message"; we only set the
        # body so Graph keeps the original recipients/subject/threading.
        self._post(f"{base}/messages/{raw_id}/{action}",
                   {"message": {"body": comment}}, scopes)

    # -- Contacts ---------------------------------------------------------
    def list_contacts(self, *, limit: int = 200) -> list[dict]:
        """People for To-field autocomplete as ``[{name, email}]``.

        Primary source is the **People API** (``/me/people``) — relevance-ranked
        colleagues + frequent contacts, including the org directory — because the
        personal ``/me/contacts`` folder is empty for most business accounts. We
        merge the personal contacts in too. Each source is best-effort so a
        missing scope on one doesn't wipe out the other."""
        out: list[dict] = []
        seen: set[str] = set()

        def add(name: str, addr: str) -> None:
            key = (addr or "").lower()
            if addr and key not in seen:
                seen.add(key)
                out.append({"name": name or addr, "email": addr})

        try:
            data = self._get(
                f"/me/people?$select=displayName,scoredEmailAddresses&$top={limit}",
                SCOPES_PEOPLE)
            for p in data.get("value", []):
                name = p.get("displayName", "")
                for ea in p.get("scoredEmailAddresses", []) or []:
                    add(name, ea.get("address"))
        except GraphError:
            pass  # People.Read not granted yet (account predates the scope)

        try:
            data = self._get(
                f"/me/contacts?$select=displayName,emailAddresses&$top={limit}",
                SCOPES_MAIL)
            for c in data.get("value", []):
                name = c.get("displayName", "")
                for ea in c.get("emailAddresses", []) or []:
                    add(name or ea.get("name", ""), ea.get("address"))
        except GraphError:
            pass
        return out

    # -- Calendar ---------------------------------------------------------
    def list_calendars(self) -> list[dict]:
        data = self._get("/me/calendars?$top=50", SCOPES_MAIL)
        return [
            {"id": c["id"], "name": c.get("name", "")}
            for c in data.get("value", [])
        ]

    def list_events(self, start_iso: str, end_iso: str, *, limit: int = 50) -> list[dict]:
        """The user's own calendar view between two ISO-8601 UTC timestamps."""
        data = self._get(
            f"/me/calendarView?{self._calview_params(start_iso, end_iso, limit)}",
            SCOPES_MAIL,
        )
        return self._events_from_json(data)

    def list_group_events(self, group_id: str, start_iso: str, end_iso: str,
                          *, limit: int = 50) -> list[dict]:
        """A group/team calendar view (needs Group.Read.All). Event ids are
        prefixed ``group:<groupId>:`` so detail/RSVP know the group context."""
        data = self._get(
            f"/groups/{group_id}/calendarView?"
            f"{self._calview_params(start_iso, end_iso, limit)}",
            SCOPES_GROUPS,
        )
        events = self._events_from_json(data)
        for e in events:
            e["id"] = f"group:{group_id}:{e['id']}"
        return events

    def list_shared_events(self, address: str, start_iso: str, end_iso: str,
                           *, limit: int = 50) -> list[dict]:
        """A shared/other mailbox's calendar view (needs Mail.ReadWrite.Shared,
        i.e. delegated calendar access). Event ids are prefixed
        ``shared:<address>:`` so detail routes back to that mailbox."""
        data = self._get(
            f"/users/{address}/calendarView?"
            f"{self._calview_params(start_iso, end_iso, limit)}",
            SCOPES_MAIL_SHARED,
        )
        events = self._events_from_json(data)
        for e in events:
            e["id"] = f"shared:{address}:{e['id']}"
        return events

    def get_event(self, event_id: str) -> dict:
        """Full event detail for the reading pane."""
        select = ("subject,start,end,location,body,organizer,attendees,"
                  "isAllDay,isOnlineMeeting,onlineMeeting,webLink,responseStatus")
        if event_id.startswith("group:"):
            _, gid, eid = _split_id(event_id, 3)
            data = self._get(f"/groups/{gid}/events/{eid}?$select={select}", SCOPES_GROUPS)
            can_respond = False
        elif event_id.startswith("shared:"):
            _, address, eid = _split_id(event_id, 3)
            data = self._get(f"/users/{address}/events/{eid}?$select={select}",
                             SCOPES_MAIL_SHARED)
            can_respond = False  # delegated RSVP isn't offered from here
        else:
            data = self._get(f"/me/events/{event_id}?$select={select}", SCOPES_MAIL)
            can_respond = True
        organizer = ((data.get("organizer") or {}).get("emailAddress") or {})
        attendees = []
        for a in data.get("attendees", []) or []:
            ea = a.get("emailAddress") or {}
            attendees.append({
                "name": html.unescape(ea.get("name") or ea.get("address", "")),
                # email is needed by the inline editor: removing an attendee
                # re-sends the *full* desired list (PATCH keeps attendees if omitted).
                "email": ea.get("address", ""),
                # none|organizer|tentativelyAccepted|accepted|declined|notResponded
                "response": (a.get("status") or {}).get("response", "none"),
            })
        body = data.get("body") or {}
        response = (data.get("responseStatus") or {}).get("response", "none")
        return {
            "id": event_id,
            "subject": html.unescape(data.get("subject", "") or "(no title)"),
            "start": (data.get("start") or {}).get("dateTime", ""),
            "end": (data.get("end") or {}).get("dateTime", ""),
            "all_day": data.get("isAllDay", False),
            "location": html.unescape((data.get("location") or {}).get("displayName", "")),
            "organizer": html.unescape(organizer.get("name") or organizer.get("address", "")),
            "attendees": attendees,
            "body": body.get("content", ""),
            "body_html": body.get("contentType") == "html",
            "online_url": (data.get("onlineMeeting") or {}).get("joinUrl", "")
            if data.get("isOnlineMeeting") else "",
            "web_link": data.get("webLink", ""),
            "response": response,
            # Can RSVP only to invites you haven't organized.
            "can_respond": can_respond and response not in ("organizer",),
        }

    def respond_event(self, event_id: str, action: str,
                      comment: str = "", send: bool = True) -> None:
        """RSVP to a meeting (action: accept | tentativelyAccept | decline).
        Needs Calendars.ReadWrite. Not applicable to group events."""
        if event_id.startswith("group:"):
            raise GraphError("Group events can't be answered from here.")
        if action not in ("accept", "tentativelyAccept", "decline"):
            raise GraphError(f"unknown RSVP action: {action}")
        self._post(
            f"/me/events/{event_id}/{action}",
            {"sendResponse": send, "comment": comment},
            SCOPES_MAIL,
        )

    def create_event(self, *, subject: str, start_iso: str, end_iso: str,
                     source: str = "me", address: str | None = None,
                     location: str = "", body: str = "", attendees=None,
                     all_day: bool = False, html: bool = False) -> dict:
        """Create an event on the current source's calendar.

        ``me`` → ``/me/events``; ``shared`` → ``/users/{address}/events`` (needs
        Calendars.ReadWrite.Shared). Times are ISO-8601; a trailing ``Z`` is
        dropped and the slot is sent as UTC. Group/team calendars are read-only
        here (the Calendar view doesn't offer New there)."""
        event = {
            "subject": subject,
            "start": {"dateTime": start_iso.rstrip("Z"), "timeZone": "UTC"},
            "end": {"dateTime": end_iso.rstrip("Z"), "timeZone": "UTC"},
            "isAllDay": all_day,
        }
        if location:
            event["location"] = {"displayName": location}
        if body:
            event["body"] = {"contentType": "HTML" if html else "Text", "content": body}
        if attendees:
            event["attendees"] = [
                {"emailAddress": {"address": a}, "type": "required"}
                for a in attendees if a
            ]
        if source == "shared" and address:
            return self._post(f"/users/{address}/events", event, SCOPES_MAIL_SHARED)
        return self._post("/me/events", event, SCOPES_MAIL)

    def update_event(self, event_id: str, *, subject: str, start_iso: str,
                     end_iso: str, location: str = "", body: str = "",
                     attendees=None, all_day: bool = False, html: bool = False) -> dict:
        """Edit an existing event (PATCH). Group/team events are read-only."""
        if event_id.startswith("group:"):
            raise GraphError("Group events can't be edited from here.")
        event: dict = {
            "subject": subject,
            "start": {"dateTime": start_iso.rstrip("Z"), "timeZone": "UTC"},
            "end": {"dateTime": end_iso.rstrip("Z"), "timeZone": "UTC"},
            "isAllDay": all_day,
        }
        # PATCH leaves omitted fields untouched — so an empty body/location keeps
        # the server's (don't wipe a meeting's body when only the time changed).
        if location:
            event["location"] = {"displayName": location}
        if body:
            event["body"] = {"contentType": "HTML" if html else "Text", "content": body}
        # attendees: ``None`` = leave untouched; a list (even empty) = set it, so
        # removing every attendee in the editor actually clears them server-side.
        if attendees is not None:
            event["attendees"] = [
                {"emailAddress": {"address": a}, "type": "required"}
                for a in attendees if a
            ]
        if event_id.startswith("shared:"):
            _, address, eid = _split_id(event_id, 3)
            return self._patch(f"/users/{address}/events/{eid}", event, SCOPES_MAIL_SHARED)
        return self._patch(f"/me/events/{event_id}", event, SCOPES_MAIL)

    def delete_event(self, event_id: str) -> None:
        """Delete an event from its calendar (group/team events are read-only)."""
        if event_id.startswith("group:"):
            raise GraphError("Group events can't be deleted from here.")
        if event_id.startswith("shared:"):
            _, address, eid = _split_id(event_id, 3)
            self._delete(f"/users/{address}/events/{eid}", SCOPES_MAIL_SHARED)
            return
        self._delete(f"/me/events/{event_id}", SCOPES_MAIL)

    @staticmethod
    def _calview_params(start_iso: str, end_iso: str, limit: int) -> str:
        return urllib.parse.urlencode({
            "startDateTime": start_iso,
            "endDateTime": end_iso,
            "$orderby": "start/dateTime",
            "$top": str(limit),
            "$select": "subject,start,end,location,isAllDay",
        })

    @staticmethod
    def _events_from_json(data: dict) -> list[dict]:
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
