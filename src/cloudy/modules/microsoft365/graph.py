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

import base64
import concurrent.futures
import html
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Callable, Sequence

from ...core.auth.msal_graph import (
    SCOPES_BASE,
    SCOPES_CHANNELS,
    SCOPES_CHAT,
    SCOPES_FILES,
    SCOPES_GROUPS,
    SCOPES_MAIL,
    SCOPES_MAIL_SHARED,
    SCOPES_NOTES,
    SCOPES_PEOPLE,
    SCOPES_PRESENCE,
    SCOPES_TEAMS,
)

BASE_URL = "https://graph.microsoft.com/v1.0"

_TAG_RE = re.compile(r"<[^>]+>")

# Teams' named reaction types → emoji (custom reactions arrive as unicode).
_REACTIONS = {
    "like": "👍", "heart": "❤️", "laugh": "😆", "surprised": "😮",
    "sad": "😢", "angry": "😠",
}


def _strip_html(value: str) -> str:
    """Collapse an HTML message body to readable single-line plain text."""
    if not value:
        return ""
    text = _TAG_RE.sub(" ", value)
    return html.unescape(re.sub(r"\s+", " ", text)).strip()


def _strip_reply_placeholder(content: str) -> str:
    """Drop the ``<attachment id=…>`` placeholder Teams leaves in a reply's body
    where the quoted message goes — we render the quote ourselves from the
    ``messageReference`` attachment, so the empty tag would otherwise show as a
    stray gap (or a bogus "attachment" chip)."""
    if not content:
        return content
    content = re.sub(r"(?is)<attachment\b[^>]*>.*?</attachment>", "", content)
    return re.sub(r"(?is)<attachment\b[^>]*/?>", "", content)


def _parse_message_reference(att: dict) -> dict:
    """Turn a Teams ``messageReference`` attachment into a normalized reply quote
    ``{id, text, from}`` so a replied-to message shows what it was (and can be
    clicked to jump to the original) instead of rendering as "attachment"."""
    raw = att.get("content") or ""
    ref_id = str(att.get("id") or "")
    preview, sender = "", ""
    try:
        data = json.loads(raw) if raw else {}
        preview = _strip_html(data.get("messagePreview") or "")
        ref_id = str(data.get("messageId") or ref_id)
        user = (data.get("messageSender") or {}).get("user") or {}
        sender = html.unescape(user.get("displayName") or "")
    except (ValueError, TypeError):
        pass
    return {"id": ref_id, "text": preview, "from": sender}


def _split_attachments(m: dict):
    """Split a message's attachments into (reply_to, file/image attachments),
    pulling out the ``messageReference`` (reply quote) so it isn't rendered as a
    file chip. Returns ``(reply_to_or_None, [attachment dicts])``."""
    reply_to = None
    attachments = []
    for a in (m.get("attachments") or []):
        if (a.get("contentType") or "") == "messageReference":
            if reply_to is None:
                reply_to = _parse_message_reference(a)
            continue
        attachments.append({
            "name": a.get("name") or "attachment",
            "url": a.get("contentUrl", ""),
            "content_type": a.get("contentType", ""),
        })
    return reply_to, attachments


# HTML inline tags that map cleanly onto Pango markup, so chat bubbles can show
# the same bold/italic/underline/strike/links Teams renders (instead of flat
# plain text). Everything else is dropped; block tags become line breaks.
_PANGO_TAGS = {
    "b": "b", "strong": "b", "i": "i", "em": "i", "u": "u",
    "s": "s", "strike": "s", "del": "s", "code": "tt", "pre": "tt",
}


def _pango_escape(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _html_to_pango(content: str) -> str:
    """Convert a chat message's HTML body to a safe Pango-markup subset
    (bold/italic/underline/strike/monospace/links). Returns ``""`` when the
    result carries no markup, so callers can fall back to plain text."""
    if not content:
        return ""
    s = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", "", content)
    s = re.sub(r"(?is)<img[^>]*>", "", s)              # images render separately
    s = re.sub(r"(?i)<br\s*/?>", "\n", s)
    s = re.sub(r"(?i)</(p|div|tr|h[1-6])>", "\n", s)
    s = re.sub(r"(?i)<li[^>]*>", "\n• ", s)
    out: list[str] = []
    has_markup = False
    pos = 0
    for m in re.finditer(r"<[^>]+>", s):
        chunk = s[pos:m.start()]
        if chunk:
            out.append(_pango_escape(html.unescape(chunk)))
        pos = m.end()
        tag = m.group(0)
        tm = re.match(r"</?\s*([a-zA-Z0-9]+)", tag)
        if not tm:
            continue
        name, closing = tm.group(1).lower(), tag.startswith("</")
        if name == "a":
            if closing:
                out.append("</a>")
            else:
                hm = (re.search(r'href="([^"]*)"', tag)
                      or re.search(r"href='([^']*)'", tag))
                href = _pango_escape(html.unescape(hm.group(1))) if hm else ""
                out.append(f'<a href="{href}">')
            has_markup = True
        elif name in _PANGO_TAGS:
            pt = _PANGO_TAGS[name]
            out.append(f"</{pt}>" if closing else f"<{pt}>")
            has_markup = True
    tail = s[pos:]
    if tail:
        out.append(_pango_escape(html.unescape(tail)))
    if not has_markup:
        return ""  # nothing to gain over plain text
    result = re.sub(r"\n{3,}", "\n\n", "".join(out)).strip()
    return result


class GraphError(Exception):
    pass


class _StripAuthOnRedirect(urllib.request.HTTPRedirectHandler):
    """Redirect handler that drops the ``Authorization`` header when the redirect
    points at a different host (so a Graph bearer token isn't leaked to — and
    rejected by — pre-authenticated storage that hosted content redirects to)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None:
            old_host = urllib.parse.urlsplit(req.full_url).hostname
            new_host = urllib.parse.urlsplit(newurl).hostname
            if old_host != new_host:
                new.headers.pop("Authorization", None)
                new.headers.pop("authorization", None)
        return new


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
        folders = [
            {"id": f["id"], "name": f.get("displayName", ""),
             "unread": f.get("unreadItemCount", 0), "well_known": ""}
            for f in data.get("value", [])
        ]
        # Tag the Inbox via the locale-independent well-known alias (the Graph id
        # is opaque and the name is localized, e.g. "Posteingang"). The alias is
        # the same one inbox_unread uses, so it's known-good.
        try:
            inbox_id = self._get(f"{base}/inbox?$select=id", scopes).get("id")
        except GraphError:
            inbox_id = None
        if inbox_id:
            for f in folders:
                if f["id"] == inbox_id:
                    f["well_known"] = "inbox"
                    break
        return folders

    def inbox_unread(self) -> int:
        """Unread message count of the personal Inbox (cheap single call)."""
        data = self._get(
            "/me/mailFolders/inbox?$select=unreadItemCount", SCOPES_MAIL)
        return int(data.get("unreadItemCount", 0) or 0)

    def list_messages(self, folder_id: str = "inbox", *, limit: int = 25) -> list[dict]:
        return self.list_messages_page(folder_id, limit=limit)[0]

    def list_messages_page(self, folder_id: str = "inbox", *, limit: int = 25,
                           page_token: str | None = None):
        """Like :meth:`list_messages` but returns ``(messages, next_token)``.

        ``next_token`` is the Graph ``@odata.nextLink`` (an absolute URL) or
        ``None`` when there are no older messages; pass it back as
        ``page_token`` to fetch the following page."""
        if folder_id.startswith("group:"):
            return self._list_group_threads(
                folder_id.split(":", 1)[1], limit, page_token)
        if folder_id.startswith("shared:"):
            _, address, fid = _split_id(folder_id, 3)
            return self._list_folder_messages(
                f"/users/{address}", fid, limit, id_prefix=f"shared:{address}:",
                scopes=SCOPES_MAIL_SHARED, url=page_token)
        return self._list_folder_messages("/me", folder_id, limit, url=page_token)

    def _list_folder_messages(self, scope_base: str, folder_id: str, limit: int,
                              *, id_prefix: str = "", scopes=SCOPES_MAIL,
                              url: str | None = None):
        # A page_token is a full @odata.nextLink (absolute URL); otherwise build
        # page 1. Keep $/commas literal (Graph-friendly); only the space in the
        # orderby value needs encoding (a raw space makes urllib reject the URL).
        if url is None:
            orderby = urllib.parse.quote("receivedDateTime desc")
            url = (
                f"{scope_base}/mailFolders/{folder_id}/messages"
                f"?$top={limit}"
                f"&$select=subject,from,receivedDateTime,bodyPreview,isRead,importance,flag"
                f"&$orderby={orderby}"
            )
        data = self._get(url, scopes)
        out = []
        for m in data.get("value", []):
            row = self._message_row(m)
            row["id"] = f"{id_prefix}{m['id']}"
            out.append(row)
        return out, data.get("@odata.nextLink")

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

    def _list_group_threads(self, group_id: str, limit: int,
                            page_token: str | None = None):
        """A group mailbox's conversation threads, shaped like messages."""
        url = page_token or f"/groups/{group_id}/threads?$top={limit}"
        data = self._get(url, SCOPES_GROUPS)
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
        return out, data.get("@odata.nextLink")

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
            f"?$select=subject,from,toRecipients,receivedDateTime,body,bodyPreview,"
            f"hasAttachments",
            scopes,
        )
        sender = (data.get("from") or {}).get("emailAddress", {})
        to = ", ".join(
            r.get("emailAddress", {}).get("address", "")
            for r in data.get("toRecipients", [])
        )
        body = data.get("body") or {}
        content = body.get("content", "")
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
            "body": content,
            "body_html": body.get("contentType") == "html",
            # Inline (cid:) images so the reader can render them as data URIs
            # instead of dropping them — only fetched when the body references
            # one, and server-filtered to inline so big file attachments aren't
            # downloaded just to read the message.
            "inline_images": self._inline_images(scope_base, raw_id, content, scopes),
            # Non-inline file attachments (metadata only — bytes fetched on click
            # via fetch_mail_attachment) so the reader can show openable chips.
            "attachments": self._file_attachments(
                scope_base, raw_id, data.get("hasAttachments"), scopes),
            # Server-rendered text preview — used as a fallback when the body is
            # empty (e.g. meeting acceptance/decline notifications carry no body).
            "preview": html.unescape(data.get("bodyPreview", "")),
            "meeting_response": meeting_response,
        }

    def _file_attachments(self, scope_base: str, raw_id: str, has: bool,
                          scopes) -> list[dict]:
        """Non-inline attachments as ``[{id, name, content_type, size}]`` — just
        metadata (no contentBytes), so opening a message with a big attachment
        stays cheap; the bytes are fetched on demand by fetch_mail_attachment."""
        if not has:
            return []
        flt = urllib.parse.quote("isInline eq false")
        try:
            data = self._get(
                f"{scope_base}/messages/{raw_id}/attachments"
                f"?$filter={flt}&$select=id,name,contentType,size", scopes)
        except GraphError:
            return []
        out = []
        for a in data.get("value", []):
            if a.get("id"):
                out.append({
                    "id": a["id"],
                    "name": a.get("name") or "attachment",
                    "content_type": a.get("contentType", "") or "",
                    "size": a.get("size", 0) or 0,
                })
        return out

    def fetch_mail_attachment(self, message_id: str, attachment_id: str) -> bytes:
        """Download one mail attachment's bytes (for opening/saving)."""
        scope_base, raw_id, scopes = self._message_scope(message_id)
        token = self._token_provider(scopes)
        if not token:
            raise GraphError("not signed in (no token for the requested scopes)")
        url = (f"{BASE_URL}{scope_base}/messages/{raw_id}"
               f"/attachments/{attachment_id}/$value")
        opener = urllib.request.build_opener(_StripAuthOnRedirect())
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        try:
            with opener.open(req, timeout=60) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            raise GraphError(f"{exc.code}: couldn't fetch attachment") from exc

    def _inline_images(self, scope_base: str, raw_id: str, content: str,
                       scopes) -> list[dict]:
        """Inline attachments referenced by ``cid:`` in ``content``, as
        ``[{content_id, content_type, content_bytes(base64)}]``."""
        if "cid:" not in (content or "").lower():
            return []
        # $filter has spaces — urllib rejects unencoded spaces in a URL, so
        # percent-encode the clause (see the "Graph query URLs" gotcha).
        flt = urllib.parse.quote("isInline eq true")
        try:
            data = self._get(
                f"{scope_base}/messages/{raw_id}/attachments?$filter={flt}",
                scopes)
        except GraphError:
            return []
        out = []
        for a in data.get("value", []):
            cid, b64 = a.get("contentId"), a.get("contentBytes")
            if cid and b64:
                out.append({
                    "content_id": cid,
                    "content_type": a.get("contentType", "image/png"),
                    "content_bytes": b64,
                })
        return out

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

    @staticmethod
    def _build_attachments(attachments) -> list[dict]:
        """Graph fileAttachment objects from ``[{name, content_type, data,
        inline?, content_id?}]``. Inline images carry ``isInline`` + a
        ``contentId`` matching a ``cid:`` reference in an HTML body."""
        out = []
        for a in attachments or []:
            obj = {
                "@odata.type": "#microsoft.graph.fileAttachment",
                "name": a.get("name") or "attachment",
                "contentType": a.get("content_type") or "application/octet-stream",
                "contentBytes": base64.b64encode(a["data"]).decode(),
            }
            if a.get("inline"):
                obj["isInline"] = True
            if a.get("content_id"):
                obj["contentId"] = a["content_id"]
            out.append(obj)
        return out

    def send_mail(self, *, to, subject: str, body: str, source: str = "me",
                  address: str | None = None, cc=None, bcc=None, html: bool = False,
                  attachments=None, importance: str | None = None,
                  read_receipt: bool = False) -> None:
        """Send a new message *as the current source*.

        ``source='me'`` sends from the signed-in user (``/me/sendMail``);
        ``source='shared'`` sends as a shared mailbox you have Send-As on
        (``/users/{address}/sendMail`` + Mail.Send.Shared). M365 groups have no
        plain sendMail identity, so the Mail view falls back to ``'me'`` for new
        messages (group replies go through :meth:`reply_mail`). ``attachments``
        are ``[{name, content_type, data, inline?, content_id?}]``; ``importance``
        is ``"low" | "normal" | "high"``. ``read_receipt`` asks the recipient's
        client for a read notification (Graph ``isReadReceiptRequested``)."""
        message = {
            "subject": subject,
            "body": {"contentType": "HTML" if html else "Text", "content": body},
            "toRecipients": self._recipients(to),
        }
        if cc:
            message["ccRecipients"] = self._recipients(cc)
        if bcc:
            message["bccRecipients"] = self._recipients(bcc)
        if importance:
            message["importance"] = importance
        if read_receipt:
            message["isReadReceiptRequested"] = True
        if attachments:
            message["attachments"] = self._build_attachments(attachments)
        payload = {"message": message, "saveToSentItems": True}
        if source == "shared" and address:
            self._post(f"/users/{address}/sendMail", payload, SCOPES_MAIL_SHARED)
        else:
            self._post("/me/sendMail", payload, SCOPES_MAIL)

    def reply_mail(self, message_id: str, body: str, *, reply_all: bool = False,
                   html: bool = False, attachments=None,
                   read_receipt: bool = False) -> None:
        """Reply to a message, keeping it on its thread.

        Personal/shared messages use Graph's ``reply``/``replyAll`` action; a
        group conversation thread is answered with a new post on the thread."""
        comment = {"contentType": "HTML" if html else "Text", "content": body}
        if message_id.startswith("group:"):
            _, gid, tid = _split_id(message_id, 3)
            post = {"body": comment}
            if attachments:
                post["attachments"] = self._build_attachments(attachments)
            self._post(f"/groups/{gid}/threads/{tid}/reply",
                       {"post": post}, SCOPES_GROUPS)
            return
        base, raw_id, scopes = self._message_scope(message_id)
        action = "replyAll" if reply_all else "reply"
        # The reply action accepts a draft override via "message"; we only set the
        # body (and any attachments) so Graph keeps the recipients/subject/thread.
        msg = {"body": comment}
        if read_receipt:
            msg["isReadReceiptRequested"] = True
        if attachments:
            msg["attachments"] = self._build_attachments(attachments)
        self._post(f"{base}/messages/{raw_id}/{action}",
                   {"message": msg}, scopes)

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
            "$select": "subject,start,end,location,isAllDay,responseStatus",
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
                # none|organizer|tentativelyAccepted|accepted|declined|notResponded
                # Drives the greyed "unanswered invite" styling in the agenda.
                "response": (e.get("responseStatus") or {}).get("response", ""),
            })
        return out

    # -- Chat (Teams) -----------------------------------------------------
    def _me_id(self) -> str:
        """The signed-in user's AAD object id (cached), to mark own messages."""
        if not getattr(self, "_cached_me_id", None):
            data = self._get("/me?$select=id", SCOPES_BASE)
            self._cached_me_id = data.get("id", "")
        return self._cached_me_id

    def list_chats(self, *, limit: int = 50) -> list[dict]:
        """The user's Teams chats (1:1 + group), most-recent first.

        ``[{id, name, kind, preview, last_at}]``. Needs ``Chat.ReadWrite`` —
        work/school accounts only (consumer Microsoft accounts have no chats)."""
        chats = self.list_chats_page(limit=limit)[0]
        chats.sort(key=lambda c: c.get("last_at", ""), reverse=True)
        return chats

    def list_chats_page(self, *, limit: int = 50, page_token: str | None = None):
        """A page of chats, returning ``(chats, next_token)`` for "load more".
        Pages aren't reliably ordered server-side; the caller re-sorts the
        merged list by ``last_at``."""
        url = page_token or (
            f"/me/chats?$top={limit}&$expand=members,lastMessagePreview")
        data = self._get(url, SCOPES_CHAT)
        me = self._me_id()
        chats = [self._chat_row(c, me) for c in data.get("value", [])]
        return chats, data.get("@odata.nextLink")

    def start_chat(self, recipient: str, text: str = "") -> str:
        """Create (or reuse) a 1:1 chat with ``recipient`` (UPN/email) and send
        ``text``. Returns the new chat id. Needs ``Chat.ReadWrite``."""
        me = self._me_id()
        body = {
            "chatType": "oneOnOne",
            "members": [
                {"@odata.type": "#microsoft.graph.aadUserConversationMember",
                 "roles": ["owner"],
                 "user@odata.bind": f"{BASE_URL}/users('{me}')"},
                {"@odata.type": "#microsoft.graph.aadUserConversationMember",
                 "roles": ["owner"],
                 "user@odata.bind": f"{BASE_URL}/users('{recipient}')"},
            ],
        }
        chat = self._post("/chats", body, SCOPES_CHAT)
        chat_id = chat.get("id", "")
        if chat_id and text:
            self.send_chat_message(chat_id, text)
        return chat_id

    @staticmethod
    def _chat_row(c: dict, me_id: str) -> dict:
        topic = c.get("topic")
        members = c.get("members") or []
        others = [m.get("displayName", "") for m in members
                  if m.get("userId") != me_id and m.get("displayName")]
        # AAD ids of the *other* participants — the view batches these into one
        # presence lookup (skip self; presence of your own account is implicit).
        other_ids = [m["userId"] for m in members
                     if m.get("userId") and m.get("userId") != me_id]
        ctype = c.get("chatType", "")  # oneOnOne | group | meeting
        if topic:
            name = topic
        elif others:
            name = ", ".join(others)
        else:
            name = "Chat"
        lmp = c.get("lastMessagePreview") or {}
        preview = _strip_html((lmp.get("body") or {}).get("content", ""))
        last_at = lmp.get("createdDateTime", "")
        # Unread only when we have a read marker AND the last message is newer
        # than it (compared at second precision to dodge fractional-second
        # mismatches). A missing read marker means "read" so chats don't all
        # show bold — the red sidebar badge still flags genuinely new ones.
        read_at = (c.get("viewpoint") or {}).get("lastMessageReadDateTime", "")
        last_user = (lmp.get("from") or {}).get("user") or {}
        from_me = bool(last_user.get("id")) and last_user.get("id") == me_id
        # Unread only when the last message is someone else's AND newer than our
        # read marker. Your own last message (or a missing marker) → read.
        unread = (not from_me and bool(read_at)
                  and (last_at or "")[:19] > (read_at or "")[:19])
        return {
            "id": c["id"],
            "name": html.unescape(name),
            "kind": ctype,  # oneOnOne | group | meeting
            "preview": preview,
            "last_at": last_at,
            "unread": unread,
            "from_me": from_me,
            "member_ids": other_ids,
            "member_count": len([m for m in members if m.get("userId")]),
        }

    def list_chat_messages(self, chat_id: str, *, limit: int = 30) -> list[dict]:
        return self.list_chat_messages_page(chat_id, limit=limit)[0]

    def list_chat_messages_page(self, chat_id: str, *, limit: int = 30,
                                page_token: str | None = None):
        """A page of a chat's messages, oldest-first within the page.

        Returns ``(messages, next_token)``; ``next_token`` (the Graph
        ``@odata.nextLink``) fetches the *older* page above the current one."""
        url = page_token or f"/me/chats/{chat_id}/messages?$top={limit}"
        data = self._get(url, SCOPES_CHAT)
        me = self._me_id()
        out = [self._chat_message_row(m, me, chat_id)
               for m in data.get("value", [])
               if m.get("messageType") == "message" and not m.get("deletedDateTime")]
        out.reverse()
        return out, data.get("@odata.nextLink")

    def recent_chat_activity(self, *, max_chats: int = 8,
                             per_chat: int = 15) -> list[dict]:
        """A bounded scan of your most-recent chats for activity that targets
        *you*: reactions on your own messages and @mentions of you. Powers the
        Activity feed's Teams-style "X reacted to your message" / "X mentioned
        you" rows. Returns ``[{kind, chat_id, who, emoji, text, when}]`` newest
        first. Bounded to ``max_chats`` conversations (a handful of parallel
        calls) so it stays cheap enough to run on every feed load."""
        try:
            chats = self.list_chats(limit=max_chats)
        except GraphError:
            return []
        me = self._me_id()

        def scan(chat: dict) -> list[dict]:
            cid = chat.get("id", "")
            try:
                data = self._get(f"/me/chats/{cid}/messages?$top={per_chat}", SCOPES_CHAT)
            except GraphError:
                return []
            items: list[dict] = []
            for m in data.get("value", []):
                if m.get("messageType") != "message" or m.get("deletedDateTime"):
                    continue
                user = (m.get("from") or {}).get("user") or {}
                mine = bool(user.get("id")) and user.get("id") == me
                body = m.get("body") or {}
                text = (_strip_html(body.get("content", ""))
                        if body.get("contentType") == "html"
                        else body.get("content", "")).strip()
                when = m.get("createdDateTime", "")
                if mine:
                    for r in m.get("reactions") or []:
                        ru = (r.get("user") or {}).get("user") or {}
                        if not ru.get("displayName") or ru.get("id") == me:
                            continue  # skip my own reactions / unnamed
                        rt = r.get("reactionType", "") or ""
                        items.append({
                            "kind": "reaction", "chat_id": cid,
                            "who": html.unescape(ru.get("displayName", "")),
                            "emoji": _REACTIONS.get(rt, rt),
                            "text": text[:90],
                            "when": r.get("createdDateTime", "") or when,
                        })
                for men in m.get("mentions") or []:
                    mu = (men.get("mentioned") or {}).get("user") or {}
                    if mu.get("id") == me:
                        items.append({
                            "kind": "mention", "chat_id": cid,
                            "who": html.unescape(user.get("displayName", "")),
                            "emoji": "", "text": text[:90], "when": when,
                        })
                        break
            return items

        out: list[dict] = []
        workers = min(6, max(1, len(chats)))
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            for res in pool.map(scan, chats):
                out.extend(res)
        out.sort(key=lambda i: i.get("when") or "", reverse=True)
        return out

    @staticmethod
    def _chat_message_row(m: dict, me_id: str, chat_id: str = "") -> dict:
        user = (m.get("from") or {}).get("user") or {}
        body = m.get("body") or {}
        content = body.get("content", "")
        is_html = body.get("contentType") == "html"
        reply_to, attachments = _split_attachments(m)
        # Strip the reply placeholder so the quoted text doesn't pollute the body
        # (we render the quote separately from ``reply_to``).
        if is_html:
            content = _strip_reply_placeholder(content)
        text = _strip_html(content) if is_html else content
        # Inline images (pasted screenshots, GIFs) live as <img> in the HTML body
        # via hosted contents. Resolve relative src= to the absolute, auth-gated
        # hosted-content URL so we can fetch + thumbnail them.
        if is_html and "<img" in content.lower():
            cid = m.get("chatId") or chat_id
            for src in re.findall(r"""<img[^>]+src=["']([^"']+)["']""",
                                  content, re.IGNORECASE):
                url = html.unescape(src)
                if not url.lower().startswith("http"):
                    hc = re.search(r"hostedContents/([^/\"']+)", url)
                    if hc and cid and m.get("id"):
                        url = (f"{BASE_URL}/chats/{cid}/messages/{m['id']}"
                               f"/hostedContents/{hc.group(1)}/$value")
                attachments.append(
                    {"name": "Image", "url": url, "content_type": "image"})
        # Reactions, collapsed to {emoji: count}.
        reactions: dict = {}
        for r in m.get("reactions") or []:
            rt = r.get("reactionType", "") or ""
            emoji = _REACTIONS.get(rt, rt)
            if emoji:
                reactions[emoji] = reactions.get(emoji, 0) + 1
        return {
            "id": m["id"],
            "text": text,
            "markup": _html_to_pango(content) if is_html else "",
            "from": html.unescape(user.get("displayName", "") or ""),
            "sent": m.get("createdDateTime", ""),
            "is_mine": bool(user.get("id")) and user.get("id") == me_id,
            "attachments": attachments,
            "reactions": [{"emoji": e, "count": c} for e, c in reactions.items()],
            "web_url": m.get("webUrl", "") or "",
            "reply_to": reply_to,
        }

    def send_chat_message(self, chat_id: str, text: str) -> dict:
        return self._post(
            f"/me/chats/{chat_id}/messages",
            {"body": {"contentType": "text", "content": text}},
            SCOPES_CHAT,
        )

    def fetch_bytes(self, url: str) -> bytes:
        """Download a hosted content / attachment (needs the bearer token, so a
        plain URL open would 401). Used to render inline chat images.

        Graph hosted-content ``$value`` often 302-redirects to pre-authenticated
        storage; the Authorization header must be dropped on a cross-host hop or
        that storage rejects the (Graph) bearer token, so use an opener that
        strips it on redirect."""
        opener = urllib.request.build_opener(_StripAuthOnRedirect())
        headers = {"User-Agent": "Cloudy/1.0"}
        # Only Graph endpoints want (and accept) our bearer token; an external
        # CDN (Giphy/Tenor/SharePoint CDN) may reject a request carrying it.
        host = (urllib.parse.urlsplit(url).hostname or "").lower()
        if host.endswith("graph.microsoft.com"):
            token = self._token_provider(SCOPES_CHAT)
            if not token:
                raise GraphError("not signed in (no token for the requested scopes)")
            headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(url, headers=headers)
        try:
            with opener.open(req, timeout=30) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            raise GraphError(f"{exc.code}: couldn't fetch image") from exc

    def send_chat_images(self, chat_id: str, images, text: str = "") -> dict:
        """Send one message with ``text`` plus inline image hosted contents (the
        same shape Teams uses), so it renders inline for everyone.

        ``images`` is a list of ``(bytes, content_type)``."""
        hosted, tags = [], []
        for i, (data, ctype) in enumerate(images, start=1):
            hosted.append({
                "@microsoft.graph.temporaryId": str(i),
                "contentBytes": base64.b64encode(data).decode(),
                "contentType": ctype or "image/png",
            })
            tags.append(
                f'<img src="../hostedContents/{i}/$value" style="max-width:400px">')
        body_html = (f"<div>{html.escape(text)}</div>" if text else "") + "".join(tags)
        body = {
            "body": {"contentType": "html", "content": body_html},
            "hostedContents": hosted,
        }
        return self._post(f"/me/chats/{chat_id}/messages", body, SCOPES_CHAT)

    def delete_chat_message(self, chat_id: str, message_id: str) -> None:
        """Soft-delete one of your own chat messages (Teams' delete)."""
        self._post(
            f"/me/chats/{chat_id}/messages/{message_id}/softDelete", None, SCOPES_CHAT)

    def edit_chat_message(self, chat_id: str, message_id: str, text: str) -> None:
        """Edit one of your own chat messages (PATCH, delegated)."""
        self._patch(
            f"/me/chats/{chat_id}/messages/{message_id}",
            {"body": {"contentType": "text", "content": text}}, SCOPES_CHAT)

    def set_reaction(self, chat_id: str, message_id: str, emoji: str) -> None:
        """Add a reaction to a chat message.

        The reaction endpoints live under ``/chats/{id}`` (NOT ``/me/chats/{id}``
        — that path 404s "API not supported"), and v1.0 wants the actual emoji
        *Unicode* character as ``reactionType`` (the old named types like ``like``
        are rejected: "Unicode 'like' … is not supported")."""
        self._post(f"/chats/{chat_id}/messages/{message_id}/setReaction",
                   {"reactionType": emoji}, SCOPES_CHAT)

    def list_chat_members(self, chat_id: str) -> list[dict]:
        """Members of a chat as ``[{id, name, membership_id, email}]``.

        Source for @mentions, presence and the people roster; ``membership_id``
        (the conversationMember id, distinct from the AAD user id) is what
        :meth:`remove_chat_member` deletes."""
        data = self._get(f"/me/chats/{chat_id}/members", SCOPES_CHAT)
        out = []
        for m in data.get("value", []):
            uid, name = m.get("userId"), m.get("displayName", "")
            if uid and name:
                out.append({
                    "id": uid,
                    "name": html.unescape(name),
                    "membership_id": m.get("id", ""),
                    "email": m.get("email", "") or "",
                })
        return out

    # -- presence ---------------------------------------------------------
    def get_presences(self, user_ids) -> dict:
        """Batch-fetch presence for ``user_ids`` (AAD object ids).

        Returns ``{user_id: {"availability": str, "activity": str}}``. Needs
        ``Presence.Read.All`` (the only delegated option). Up to 650 ids per
        call — the view passes the handful of people in the visible chats."""
        ids = [u for u in dict.fromkeys(user_ids) if u]  # dedupe, drop blanks
        if not ids:
            return {}
        data = self._post("/communications/getPresencesByUserId",
                           {"ids": ids}, SCOPES_PRESENCE)
        out = {}
        for p in data.get("value", []):
            pid = p.get("id")
            if pid:
                out[pid] = {
                    "availability": p.get("availability", "") or "",
                    "activity": p.get("activity", "") or "",
                }
        return out

    # -- read state -------------------------------------------------------
    def _tenant_id(self) -> str:
        """The signed-in user's tenant id, decoded from the access token's
        ``tid`` claim (cached). Needed for the markChatReadForUser identity."""
        if getattr(self, "_cached_tenant_id", None):
            return self._cached_tenant_id
        token = self._token_provider(SCOPES_CHAT) or ""
        tid = ""
        try:
            payload = token.split(".")[1]
            payload += "=" * (-len(payload) % 4)  # pad base64
            claims = json.loads(base64.urlsafe_b64decode(payload))
            tid = claims.get("tid", "") or ""
        except Exception:  # noqa: BLE001 - best-effort; same-tenant works w/o it
            tid = ""
        self._cached_tenant_id = tid
        return tid

    def mark_chat_read(self, chat_id: str) -> None:
        """Mark a chat read for the signed-in user (clears unread across all
        their devices, not just this one)."""
        user = {"id": self._me_id()}
        tid = self._tenant_id()
        if tid:
            user["tenantId"] = tid
        self._post(f"/chats/{chat_id}/markChatReadForUser", {"user": user},
                   SCOPES_CHAT)

    # -- group chats ------------------------------------------------------
    def start_group_chat(self, recipients, topic: str = "",
                         text: str = "") -> str:
        """Create a group chat with ``recipients`` (UPNs/emails) and an optional
        ``topic``, then send ``text``. Returns the new chat id."""
        me = self._me_id()
        members = [
            {"@odata.type": "#microsoft.graph.aadUserConversationMember",
             "roles": ["owner"],
             "user@odata.bind": f"{BASE_URL}/users('{me}')"},
        ]
        for r in recipients:
            members.append(
                {"@odata.type": "#microsoft.graph.aadUserConversationMember",
                 "roles": ["owner"],
                 "user@odata.bind": f"{BASE_URL}/users('{r}')"})
        body = {"chatType": "group", "members": members}
        if topic:
            body["topic"] = topic
        chat = self._post("/chats", body, SCOPES_CHAT)
        chat_id = chat.get("id", "")
        if chat_id and text:
            self.send_chat_message(chat_id, text)
        return chat_id

    def add_chat_member(self, chat_id: str, recipient: str,
                        share_history: bool = True) -> None:
        """Add a user (UPN/email) to a group chat. ``share_history`` controls
        whether they can see messages sent before they joined."""
        body = {
            "@odata.type": "#microsoft.graph.aadUserConversationMember",
            "roles": ["owner"],
            "user@odata.bind": f"{BASE_URL}/users('{recipient}')",
            "visibleHistoryStartDateTime":
                "0001-01-01T00:00:00Z" if share_history else None,
        }
        if not share_history:
            del body["visibleHistoryStartDateTime"]
        self._post(f"/chats/{chat_id}/members", body, SCOPES_CHAT)

    def remove_chat_member(self, chat_id: str, membership_id: str) -> None:
        """Remove a member (by conversationMember id) from a group chat."""
        self._delete(f"/chats/{chat_id}/members/{membership_id}", SCOPES_CHAT)

    def rename_chat(self, chat_id: str, topic: str) -> None:
        """Set a group chat's topic (its display name)."""
        self._patch(f"/chats/{chat_id}", {"topic": topic}, SCOPES_CHAT)

    def send_chat_html(self, chat_id: str, content_html: str,
                      mentions=None, images=None) -> dict:
        """Send an HTML message (carries @mentions and/or inline images).

        ``content_html`` already contains the escaped text + any ``<at>`` tags;
        ``mentions`` is the Graph mentions array; ``images`` is ``[(bytes, ctype)]``."""
        for i, (data, ctype) in enumerate(images or [], start=1):
            content_html += (
                f'<img src="../hostedContents/{i}/$value" style="max-width:400px">')
        body = {"body": {"contentType": "html", "content": content_html}}
        if mentions:
            body["mentions"] = mentions
        if images:
            body["hostedContents"] = [{
                "@microsoft.graph.temporaryId": str(i),
                "contentBytes": base64.b64encode(data).decode(),
                "contentType": ctype or "image/png",
            } for i, (data, ctype) in enumerate(images, start=1)]
        return self._post(f"/me/chats/{chat_id}/messages", body, SCOPES_CHAT)

    def search_messages(self, query: str, *, limit: int = 25) -> list[dict]:
        """Server-side search across the user's chat messages (Microsoft Search).

        Returns ``[{chat_id, message_id, from, snippet, sent}]``."""
        body = {"requests": [{
            "entityTypes": ["chatMessage"],
            "query": {"queryString": query},
            "from": 0, "size": limit,
        }]}
        data = self._post("/search/query", body, SCOPES_CHAT)
        out = []
        for resp in data.get("value", []):
            for container in resp.get("hitsContainers", []):
                for hit in container.get("hits", []):
                    r = hit.get("resource", {})
                    sender = ((r.get("from") or {}).get("user") or {})
                    summary = hit.get("summary") or _strip_html(
                        (r.get("body") or {}).get("content", ""))
                    out.append({
                        "chat_id": r.get("chatId", ""),
                        "message_id": r.get("id", ""),
                        "from": html.unescape(sender.get("displayName", "") or ""),
                        "snippet": _strip_html(summary),
                        "sent": r.get("createdDateTime", ""),
                    })
        return out

    # -- Teams channels (the Teams tab's Conversation) --------------------
    def list_joined_teams(self) -> list[dict]:
        """The Teams the user belongs to: ``[{id, name}]`` (the team id is also
        the backing M365 group id, used for channels + the group notebook).

        Lighter than :meth:`list_teams`, which additionally resolves each team's
        document library; the Teams tab only needs id + name. Needs
        ``Team.ReadBasic.All``."""
        data = self._get("/me/joinedTeams?$select=id,displayName", SCOPES_TEAMS)
        teams = [{"id": t["id"], "name": t.get("displayName") or "Untitled Team"}
                 for t in data.get("value", []) if t.get("id")]
        teams.sort(key=lambda t: t["name"].lower())
        return teams

    def list_team_channels(self, team_id: str) -> list[dict]:
        """A team's channels: ``[{id, name, description}]``, General first.
        Needs ``Channel.ReadBasic.All`` (tenant-admin consent)."""
        data = self._get(
            f"/teams/{team_id}/channels?$select=id,displayName,description",
            SCOPES_CHANNELS)
        chans = [{"id": c["id"],
                  "name": c.get("displayName") or "Channel",
                  "description": c.get("description") or ""}
                 for c in data.get("value", []) if c.get("id")]
        # General is the default channel — pin it to the top, then alphabetical.
        chans.sort(key=lambda c: (c["name"].lower() != "general", c["name"].lower()))
        return chans

    def list_channel_messages(self, team_id: str, channel_id: str, *,
                              limit: int = 20) -> list[dict]:
        return self.list_channel_messages_page(
            team_id, channel_id, limit=limit)[0]

    def list_channel_messages_page(self, team_id: str, channel_id: str, *,
                                   limit: int = 20, page_token: str | None = None):
        """A page of a channel's root posts (oldest-last), each carrying its
        ``replies`` list — channel conversations are post+replies, not a flat
        stream. ``next_token`` (Graph ``@odata.nextLink``) fetches the older
        page above. Needs ``ChannelMessage.Read.All`` (tenant-admin consent)."""
        url = page_token or (
            f"/teams/{team_id}/channels/{channel_id}/messages"
            f"?$top={limit}&$expand=replies")
        data = self._get(url, SCOPES_CHANNELS)
        me = self._me_id()
        out = []
        for m in data.get("value", []):
            if m.get("messageType") != "message" or m.get("deletedDateTime"):
                continue
            row = self._channel_message_row(m, me, team_id, channel_id)
            replies = [self._channel_message_row(r, me, team_id, channel_id)
                       for r in (m.get("replies") or [])
                       if r.get("messageType") == "message"
                       and not r.get("deletedDateTime")]
            replies.sort(key=lambda r: r.get("sent", ""))
            row["replies"] = replies
            out.append(row)
        out.reverse()  # Graph returns newest-first; render oldest-last like chat
        return out, data.get("@odata.nextLink")

    @staticmethod
    def _channel_message_row(m: dict, me_id: str, team_id: str = "",
                             channel_id: str = "") -> dict:
        """Normalize a channel post/reply to the chat-message shape, plus a
        ``subject`` (the post's bold header) and ``replies`` (filled by the
        caller for root posts)."""
        user = (m.get("from") or {}).get("user") or {}
        body = m.get("body") or {}
        content = body.get("content", "")
        is_html = body.get("contentType") == "html"
        reply_to, attachments = _split_attachments(m)
        if is_html:
            content = _strip_reply_placeholder(content)
        text = _strip_html(content) if is_html else content
        # Inline images live as <img> in the HTML body via hosted contents;
        # resolve relative src= to the absolute, auth-gated channel URL.
        if is_html and "<img" in content.lower():
            for src in re.findall(r"""<img[^>]+src=["']([^"']+)["']""",
                                  content, re.IGNORECASE):
                url = html.unescape(src)
                if not url.lower().startswith("http"):
                    hc = re.search(r"hostedContents/([^/\"']+)", url)
                    if hc and team_id and channel_id and m.get("id"):
                        url = (f"{BASE_URL}/teams/{team_id}/channels/{channel_id}"
                               f"/messages/{m['id']}/hostedContents/"
                               f"{hc.group(1)}/$value")
                attachments.append(
                    {"name": "Image", "url": url, "content_type": "image"})
        reactions: dict = {}
        for r in m.get("reactions") or []:
            rt = r.get("reactionType", "") or ""
            emoji = _REACTIONS.get(rt, rt)
            if emoji:
                reactions[emoji] = reactions.get(emoji, 0) + 1
        return {
            "id": m["id"],
            "subject": html.unescape(m.get("subject") or ""),
            "text": text,
            "markup": _html_to_pango(content) if is_html else "",
            "from": html.unescape(user.get("displayName", "") or ""),
            "sent": m.get("createdDateTime", ""),
            "is_mine": bool(user.get("id")) and user.get("id") == me_id,
            "attachments": attachments,
            "reactions": [{"emoji": e, "count": c} for e, c in reactions.items()],
            "web_url": m.get("webUrl", "") or "",
            "reply_to": reply_to,
            "replies": [],
        }

    def send_channel_message(self, team_id: str, channel_id: str,
                             text: str) -> dict:
        """Start a new post in a channel. Needs ``ChannelMessage.Send``."""
        return self._post(
            f"/teams/{team_id}/channels/{channel_id}/messages",
            {"body": {"contentType": "text", "content": text}},
            SCOPES_CHANNELS)

    def reply_channel_message(self, team_id: str, channel_id: str,
                              message_id: str, text: str) -> dict:
        """Reply under an existing channel post."""
        return self._post(
            f"/teams/{team_id}/channels/{channel_id}/messages/{message_id}/replies",
            {"body": {"contentType": "text", "content": text}},
            SCOPES_CHANNELS)

    # -- OneNote (the team's group notebook, a channel's Notes tab) -------
    def list_notebooks(self, team_id: str) -> list[dict]:
        """The team (group) OneNote notebooks: ``[{id, name}]``. Group notebooks
        require ``Notes.ReadWrite.All`` (the non-".All" Notes scope can't reach
        them)."""
        data = self._get(
            f"/groups/{team_id}/onenote/notebooks?$select=id,displayName",
            SCOPES_NOTES)
        return [{"id": n["id"], "name": n.get("displayName") or "Notebook"}
                for n in data.get("value", []) if n.get("id")]

    def list_note_sections(self, team_id: str,
                           notebook_id: str = "") -> list[dict]:
        """Sections in a team notebook (or every section when ``notebook_id`` is
        empty): ``[{id, name}]``."""
        if notebook_id:
            path = f"/groups/{team_id}/onenote/notebooks/{notebook_id}/sections"
        else:
            path = f"/groups/{team_id}/onenote/sections"
        data = self._get(f"{path}?$select=id,displayName", SCOPES_NOTES)
        return [{"id": s["id"], "name": s.get("displayName") or "Section"}
                for s in data.get("value", []) if s.get("id")]

    def list_note_pages(self, team_id: str, section_id: str, *,
                        limit: int = 50) -> list[dict]:
        """Pages in a section, newest first:
        ``[{id, title, web_url, last_at}]``."""
        data = self._get(
            f"/groups/{team_id}/onenote/sections/{section_id}/pages"
            "?$select=id,title,links,lastModifiedDateTime"
            f"&$top={limit}&$orderby=lastModifiedDateTime%20desc",
            SCOPES_NOTES)
        out = []
        for p in data.get("value", []):
            if not p.get("id"):
                continue
            links = p.get("links") or {}
            out.append({
                "id": p["id"],
                "title": p.get("title") or "Untitled page",
                "web_url": (links.get("oneNoteWebUrl") or {}).get("href", ""),
                "last_at": p.get("lastModifiedDateTime", ""),
            })
        return out

    def get_note_page(self, team_id: str, page_id: str) -> str:
        """A page's raw HTML content. Images stay as Graph URLs; the reader
        renders them natively via :meth:`fetch_note_image` (rendering the page
        in an embedded browser blows past the GPU texture limit on long pages)."""
        token = self._token_provider(SCOPES_NOTES)
        if not token:
            raise GraphError("not signed in (no token for the requested scopes)")
        url = f"{BASE_URL}/groups/{team_id}/onenote/pages/{page_id}/content"
        req = urllib.request.Request(
            url, headers={"Authorization": f"Bearer {token}"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read().decode(errors="replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")
            raise GraphError(f"Graph {exc.code}: {detail}") from exc

    def fetch_note_image(self, url: str) -> bytes:
        """Download a OneNote image resource (bearer-authenticated). ``$value``
        may 302 to storage, so drop the Authorization header on a cross-host
        hop (the same handling as chat hosted content)."""
        opener = urllib.request.build_opener(_StripAuthOnRedirect())
        headers = {"User-Agent": "Cloudy/1.0"}
        host = (urllib.parse.urlsplit(url).hostname or "").lower()
        if host.endswith("graph.microsoft.com"):
            token = self._token_provider(SCOPES_NOTES)
            if not token:
                raise GraphError("not signed in (no token for the requested scopes)")
            headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(url, headers=headers)
        try:
            with opener.open(req, timeout=30) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            raise GraphError(f"{exc.code}: couldn't fetch image") from exc

    def create_note_page(self, team_id: str, section_id: str, title: str,
                         body_html: str) -> dict:
        """Create a page in a section from an HTML body. OneNote applies the
        write asynchronously, so it may take a moment to appear. Needs
        ``Notes.Create``."""
        safe_title = html.escape(title or "Untitled page")
        doc = (f"<!DOCTYPE html><html><head><title>{safe_title}</title></head>"
               f"<body>{body_html or ''}</body></html>")
        return self._post_html(
            f"/groups/{team_id}/onenote/sections/{section_id}/pages",
            doc, SCOPES_NOTES)

    def update_note_page(self, team_id: str, page_id: str,
                         body_html: str) -> None:
        """Replace a page's body (async; the change may take a moment)."""
        commands = [{"target": "body", "action": "replace",
                     "content": body_html or ""}]
        self._patch(f"/groups/{team_id}/onenote/pages/{page_id}/content",
                    commands, SCOPES_NOTES)

    def _post_html(self, path: str, html_doc: str, scopes: Sequence[str]) -> dict:
        """POST a OneNote HTML document (distinct from the JSON ``_post``)."""
        token = self._token_provider(scopes)
        if not token:
            raise GraphError("not signed in (no token for the requested scopes)")
        req = urllib.request.Request(
            f"{BASE_URL}{path}", data=html_doc.encode("utf-8"), method="POST",
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": "text/html"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")
            raise GraphError(f"Graph {exc.code}: {detail}") from exc
