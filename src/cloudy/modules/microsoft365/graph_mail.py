# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Shahab Nedaei
"""Mail domain of the Graph client: folders, messages, attachments,
compose/reply, and contacts for autocomplete."""

from __future__ import annotations

import base64
import html
import urllib.error
import urllib.parse
import urllib.request

from .graph_http import BASE_URL, GraphError, _split_id, _StripAuthOnRedirect

from ...core.auth.msal_graph import (
    SCOPES_GROUPS,
    SCOPES_MAIL,
    SCOPES_MAIL_SHARED,
    SCOPES_PEOPLE,
)


class GraphMailMixin:
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
        groups = self._get_all(
            "/me/memberOf?$select=id,displayName,groupTypes,mailEnabled&$top=100",
            SCOPES_GROUPS,
        )
        out = []
        for g in groups:
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

    # How deep to walk nested mail folders (Inbox / Projects / 2026 / …).
    _MAX_FOLDER_DEPTH = 4

    def _mail_folders(self, base: str, scopes) -> list[dict]:
        folders = self._folder_tree(base, scopes)
        # Tag Inbox and Drafts via the locale-independent well-known aliases
        # (the Graph id is opaque and the name is localized, e.g. "Posteingang").
        # Drafts lets the Mail view open a draft back into the composer.
        for alias in ("inbox", "drafts"):
            try:
                wk_id = self._get(f"{base}/{alias}?$select=id", scopes).get("id")
            except GraphError:
                continue
            for f in folders:
                if f["id"] == wk_id:
                    f["well_known"] = alias
                    break
        return folders

    def _folder_tree(self, base: str, scopes, *, prefix: str = "",
                     depth: int = 0, url: str | None = None) -> list[dict]:
        """Flatten the folder hierarchy into "Parent / Child" entries.
        ``/mailFolders`` returns TOP-LEVEL folders only, so anything filed
        under a subfolder (everyday Outlook practice) used to be invisible.
        Recurses only into folders that report children, bounded by depth."""
        sel = "$select=id,displayName,unreadItemCount,childFolderCount"
        items = self._get_all(f"{url or base}?$top=50&{sel}", scopes)
        out = []
        for f in items:
            name = f"{prefix}{f.get('displayName', '')}"
            out.append({"id": f["id"], "name": name,
                        "unread": f.get("unreadItemCount", 0),
                        "well_known": ""})
            if f.get("childFolderCount") and depth < self._MAX_FOLDER_DEPTH:
                out.extend(self._folder_tree(
                    base, scopes, prefix=f"{name} / ", depth=depth + 1,
                    url=f"{base}/{f['id']}/childFolders"))
        return out

    def inbox_unread(self) -> int:
        """Unread message count of the personal Inbox (cheap single call)."""
        data = self._get(
            "/me/mailFolders/inbox?$select=unreadItemCount", SCOPES_MAIL)
        return int(data.get("unreadItemCount", 0) or 0)

    def list_messages(self, folder_id: str = "inbox", *, limit: int = 25) -> list[dict]:
        return self.list_messages_page(folder_id, limit=limit)[0]

    def list_messages_page(self, folder_id: str = "inbox", *, limit: int = 25,
                           page_token: str | None = None, query: str = ""):
        """Return ``(messages, next_token)`` for a mail folder page."""
        if folder_id.startswith("group:"):
            return self._list_group_threads(
                folder_id.split(":", 1)[1], limit, page_token)
        if folder_id.startswith("shared:"):
            _, address, fid = _split_id(folder_id, 3)
            return self._list_folder_messages(
                f"/users/{address}", fid, limit, id_prefix=f"shared:{address}:",
                scopes=SCOPES_MAIL_SHARED, url=page_token, query=query)
        return self._list_folder_messages("/me", folder_id, limit, url=page_token,
                                          query=query)

    def _list_folder_messages(self, scope_base: str, folder_id: str, limit: int,
                              *, id_prefix: str = "", scopes=SCOPES_MAIL,
                              url: str | None = None, query: str = ""):
        # A page_token is a full @odata.nextLink (absolute URL); otherwise build
        # page 1. Keep $/commas literal (Graph-friendly); only values with spaces
        # (the orderby direction, the search string) need encoding — a raw space
        # makes urllib reject the URL.
        if url is None:
            select = "subject,from,receivedDateTime,bodyPreview,isRead,importance,flag"
            if query:
                # $search ranks by relevance and cannot be combined with
                # $orderby; the KQL value must be double-quoted and encoded.
                search = urllib.parse.quote(f'"{query}"')
                url = (
                    f"{scope_base}/mailFolders/{folder_id}/messages"
                    f"?$top={limit}&$select={select}&$search={search}"
                )
            else:
                orderby = urllib.parse.quote("receivedDateTime desc")
                url = (
                    f"{scope_base}/mailFolders/{folder_id}/messages"
                    f"?$top={limit}&$select={select}&$orderby={orderby}"
                )
        data = self._get(url, scopes)
        out = []
        for m in data.get("value", []):
            if not m.get("id"):
                continue  # a malformed item must not sink the whole page
            row = self._message_row(m)
            row["id"] = f"{id_prefix}{m['id']}"
            out.append(row)
        return out, data.get("@odata.nextLink")

    @staticmethod
    def _message_row(m: dict) -> dict:
        sender = m.get("from", {}).get("emailAddress", {}) if m.get("from") else {}
        return {
            "id": m.get("id", ""),
            "subject": html.unescape(m.get("subject", "(no subject)")),
            "from": html.unescape(sender.get("name") or sender.get("address", "")),
            "received": m.get("receivedDateTime", ""),
            "preview": html.unescape(m.get("bodyPreview", "")),
            "is_read": m.get("isRead", True),
            "important": m.get("importance") == "high",
            "starred": (m.get("flag") or {}).get("flagStatus") == "flagged",
            # Derived-type marker: meeting invites/updates/cancellations come
            # back as microsoft.graph.eventMessage* — even under $select the
            # OData type annotation is present for derived types, so the list
            # can show a calendar marker without extra requests.
            "meeting": "eventmessage" in (m.get("@odata.type") or "").lower(),
        }

    def _list_group_threads(self, group_id: str, limit: int,
                            page_token: str | None = None):
        """A group mailbox's conversation threads, shaped like messages."""
        url = page_token or f"/groups/{group_id}/threads?$top={limit}"
        data = self._get(url, SCOPES_GROUPS)
        out = []
        for t in data.get("value", []):
            if not t.get("id"):
                continue
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
            f"?$select=subject,from,toRecipients,ccRecipients,bccRecipients,"
            f"receivedDateTime,body,bodyPreview,hasAttachments",
            scopes,
        )
        sender = (data.get("from") or {}).get("emailAddress", {})
        # Keep the sender's address alongside the name (RFC 5322 "Name <addr>",
        # matching the Gmail client) so the reader can reveal the real address on
        # hover — a name-only ``from`` hid who actually sent it, and also left
        # Reply with no address to send to. ``sender_name()`` still strips this
        # back to just the name for list/title display.
        sname = (sender.get("name") or "").strip()
        saddr = (sender.get("address") or "").strip()
        if sname and saddr and sname != saddr:
            from_disp = f"{sname} <{saddr}>"
        else:
            from_disp = sname or saddr
        def _addrs(recipients) -> str:
            return ", ".join(
                r.get("emailAddress", {}).get("address", "")
                for r in (recipients or [])
            )

        to = _addrs(data.get("toRecipients"))
        cc = _addrs(data.get("ccRecipients"))
        bcc = _addrs(data.get("bccRecipients"))
        body = data.get("body") or {}
        content = body.get("content", "")
        # meetingMessageType can't be $select-ed on the base /messages endpoint
        # (400), but derived types still carry their @odata.type annotation — so
        # when this message is an eventMessage, fetch the invite details (and
        # the calendar event Exchange auto-staged) with the documented
        # $expand=microsoft.graph.eventMessage/event follow-up. Personal
        # mailbox only: RSVP on a shared mailbox isn't offered from here.
        meeting_response = ""
        invite = None
        if scope_base == "/me" and \
                "eventmessage" in (data.get("@odata.type") or "").lower():
            invite = self._meeting_invite(raw_id)
        return {
            **({"invite": invite} if invite else {}),
            "id": message_id,
            "subject": html.unescape(data.get("subject", "(no subject)")),
            "from": html.unescape(from_disp),
            "to": html.unescape(to),
            "cc": html.unescape(cc),
            "bcc": html.unescape(bcc),
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

    def _meeting_invite(self, raw_id: str) -> dict | None:
        """Meeting-invite details for an eventMessage, shaped like the parsed
        iMIP dict from ``core.ics`` so the reader's invite card and RSVP bar
        render it exactly like a Gmail ``.ics`` invite. Includes ``event_id``
        (the copy Exchange auto-staged on the user's calendar) so the RSVP can
        go through the proper ``/me/events/{id}/accept`` action instead of a
        hand-built iMIP mail. Best-effort: ``None`` just means no invite card."""
        try:
            data = self._get(
                f"/me/messages/{raw_id}"
                f"?$expand=microsoft.graph.eventMessage/event",
                SCOPES_MAIL)
        except Exception:  # noqa: BLE001 - the message still renders without it
            return None
        method = {"meetingRequest": "REQUEST",
                  "meetingCancelled": "CANCEL"}.get(
            data.get("meetingMessageType", ""))
        if method is None:
            return None  # accepted/declined notifications aren't actionable
        event = data.get("event") or {}

        def _ical(dt: dict | None) -> str:
            """Graph dateTimeTimeZone → iCal basic format (UTC gets a Z)."""
            val = ((dt or {}).get("dateTime") or "")[:19]
            if not val:
                return ""
            compact = val.replace("-", "").replace(":", "")
            return compact + ("Z" if (dt or {}).get("timeZone") == "UTC" else "")

        organizer = ((event.get("organizer") or {}).get("emailAddress")
                     or (data.get("from") or {}).get("emailAddress") or {})
        start = data.get("startDateTime") or event.get("start")
        end = data.get("endDateTime") or event.get("end")
        location = ((data.get("location") or {}).get("displayName")
                    or (event.get("location") or {}).get("displayName") or "")
        return {
            "method": method,
            "status": "CANCELLED" if method == "CANCEL" else "",
            "summary": html.unescape(event.get("subject")
                                     or data.get("subject", "") or ""),
            "dtstart": _ical(start),
            "dtend": _ical(end),
            "all_day": bool(event.get("isAllDay") or data.get("isAllDay")),
            "location": html.unescape(location),
            "organizer_email": organizer.get("address", ""),
            "uid": event.get("iCalUId", ""),
            "join_url": (event.get("onlineMeeting") or {}).get("joinUrl", ""),
            # Graph vocabulary (notResponded/accepted/…) — the RSVP bar's
            # normalizer understands it alongside the iCal PARTSTAT one.
            "my_response": (event.get("responseStatus") or {}).get("response", ""),
            "event_id": event.get("id", ""),
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

    def move_message(self, message_id: str, folder_id: str, *,
                     from_folder: str | None = None) -> None:
        """Move a message to another folder (Graph ``move`` action). The
        destination may carry the ``shared:`` prefix like the folder list does;
        ``from_folder`` is accepted for API parity with the Gmail client."""
        if message_id.startswith("group:"):
            raise GraphError("Group conversations can't be moved.")
        base, raw_id, scopes = self._message_scope(message_id)
        dest = folder_id
        if dest.startswith("shared:"):
            dest = _split_id(dest, 3)[2]
        self._post(f"{base}/messages/{raw_id}/move",
                   {"destinationId": dest}, scopes)

    def set_flag(self, message_id: str, flagged: bool = True) -> None:
        """Flag/unflag a message for follow-up (Outlook's red flag)."""
        if message_id.startswith("group:"):
            raise GraphError("Group conversations can't be flagged.")
        base, raw_id, scopes = self._message_scope(message_id)
        status = "flagged" if flagged else "notFlagged"
        self._patch(f"{base}/messages/{raw_id}",
                    {"flag": {"flagStatus": status}}, scopes)

    # -- compose / reply --------------------------------------------------
    @staticmethod
    def _recipients(addresses) -> list[dict]:
        return [{"emailAddress": {"address": a}} for a in addresses if a]

    # Graph rejects any single request over ~4 MB, and attachments from ~3 MB
    # up must go through an upload session (createUploadSession + chunked
    # PUTs). The budget is on the base64-encoded size, and it's cumulative, so
    # several mid-size files can't push one JSON payload over the cap together.
    _INLINE_ATTACH_BUDGET = 2_500_000
    _UPLOAD_CHUNK = 12 * 327_680  # session chunks must be 320 KiB multiples

    @classmethod
    def _split_attachments(cls, attachments) -> tuple[list, list]:
        """(inline-able, needs-upload-session) split of raw attachment dicts."""
        small, big = [], []
        budget = cls._INLINE_ATTACH_BUDGET
        for a in attachments or []:
            cost = (len(a.get("data") or b"") * 4) // 3  # base64 size
            if cost <= budget:
                budget -= cost
                small.append(a)
            else:
                big.append(a)
        return small, big

    def _upload_attachment(self, base: str, message_id: str, a: dict,
                           scopes) -> None:
        """Attach one large file to a draft via an upload session."""
        data = a.get("data") or b""
        item = {
            "attachmentType": "file",
            "name": a.get("name") or "attachment",
            "contentType": a.get("content_type") or "application/octet-stream",
            "size": len(data),
        }
        if a.get("inline"):
            item["isInline"] = True
        if a.get("content_id"):
            item["contentId"] = a["content_id"]
        sess = self._post(
            f"{base}/messages/{message_id}/attachments/createUploadSession",
            {"AttachmentItem": item}, scopes)
        url = sess.get("uploadUrl")
        if not url:
            raise GraphError("upload session came back without an uploadUrl")
        for off in range(0, len(data), self._UPLOAD_CHUNK):
            self._put_chunk(url, data[off:off + self._UPLOAD_CHUNK],
                            off, len(data))

    def _draft_with_attachments(self, base: str, message: dict, attachments,
                                scopes) -> dict:
        """Create a draft carrying attachments of any size — small ones inline
        in the create, large ones via upload sessions. Returns the draft."""
        small, big = self._split_attachments(attachments)
        if small:
            message["attachments"] = self._build_attachments(small)
        draft = self._post(f"{base}/messages", message, scopes)
        for a in big:
            self._upload_attachment(base, draft["id"], a, scopes)
        return draft

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
        if source == "shared" and address:
            base, scopes = f"/users/{address}", SCOPES_MAIL_SHARED
        else:
            base, scopes = "/me", SCOPES_MAIL
        _small, big = self._split_attachments(attachments)
        if big:
            # Too large for a single sendMail request (Graph caps at ~4 MB):
            # stage a draft, stream the big files up in chunks, then send it.
            draft = self._draft_with_attachments(base, message, attachments,
                                                 scopes)
            self._post(f"{base}/messages/{draft['id']}/send", None, scopes)
            return
        if attachments:
            message["attachments"] = self._build_attachments(attachments)
        self._post(f"{base}/sendMail",
                   {"message": message, "saveToSentItems": True}, scopes)

    def save_draft(self, *, to, subject: str, body: str, cc=None, bcc=None,
                   html: bool = False, attachments=None, source: str = "me",
                   address: str | None = None) -> dict:
        """Save an unfinished message into Drafts (POST creates a draft, unlike
        ``sendMail``) so it can be resumed here or in Outlook."""
        message = {
            "subject": subject,
            "body": {"contentType": "HTML" if html else "Text", "content": body},
            "toRecipients": self._recipients(to),
        }
        if cc:
            message["ccRecipients"] = self._recipients(cc)
        if bcc:
            message["bccRecipients"] = self._recipients(bcc)
        if source == "shared" and address:
            return self._draft_with_attachments(
                f"/users/{address}", message, attachments, SCOPES_MAIL_SHARED)
        return self._draft_with_attachments(
            "/me", message, attachments, SCOPES_MAIL)

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
        _small, big = self._split_attachments(attachments)
        if big:
            self._reply_via_draft(base, raw_id, action, body, html=html,
                                  attachments=attachments,
                                  read_receipt=read_receipt, scopes=scopes)
            return
        # Send the new text as the action's "comment": Exchange builds the
        # reply draft itself and keeps the quoted conversation underneath.
        # Setting message.body instead REPLACES that draft body — which is why
        # replies used to arrive with no conversation history at all.
        payload: dict = {"comment": self._comment_html(body, html)}
        msg = {}
        if read_receipt:
            msg["isReadReceiptRequested"] = True
        if attachments:
            msg["attachments"] = self._build_attachments(attachments)
        if msg:
            payload["message"] = msg
        self._post(f"{base}/messages/{raw_id}/{action}", payload, scopes)

    @staticmethod
    def _comment_html(body: str, is_html: bool) -> str:
        """The reply text as HTML (Exchange folds the comment into an HTML
        reply draft; plain text needs its newlines preserved)."""
        if is_html:
            return body
        return html.escape(body).replace("\n", "<br>")

    def _reply_via_draft(self, base: str, raw_id: str, action: str, body: str,
                         *, html: bool, attachments, read_receipt: bool,
                         scopes) -> None:
        """Reply with attachments too big to inline: let Exchange build the
        reply draft (createReply keeps the quoted original + recipients),
        prepend the new text, stream the big files up, then send."""
        create = "createReplyAll" if action == "replyAll" else "createReply"
        draft = self._post(f"{base}/messages/{raw_id}/{create}", None, scopes)
        did = draft["id"]
        quoted = (draft.get("body") or {}).get("content", "")
        patch: dict = {"body": {
            "contentType": "HTML",
            "content": f"<div>{self._comment_html(body, html)}</div>{quoted}",
        }}
        if read_receipt:
            patch["isReadReceiptRequested"] = True
        self._patch(f"{base}/messages/{did}", patch, scopes)
        small, big = self._split_attachments(attachments)
        for obj in self._build_attachments(small):
            self._post(f"{base}/messages/{did}/attachments", obj, scopes)
        for a in big:
            self._upload_attachment(base, did, a, scopes)
        self._post(f"{base}/messages/{did}/send", None, scopes)

    def forward_mail(self, message_id: str, to, comment: str = "", *,
                     html: bool = False, cc=None, attachments=None) -> None:
        """Forward through Graph's native action: Exchange rebuilds the
        message itself, keeping the original HTML body, inline (cid:) images
        and every original attachment — the old client-built forward flattened
        the body to plain text and dropped inline images. ``attachments`` are
        *extra* files the user added while forwarding."""
        if message_id.startswith("group:"):
            raise GraphError("Group conversations can't be forwarded.")
        base, raw_id, scopes = self._message_scope(message_id)
        _small, big = self._split_attachments(attachments)
        if big:
            # Same draft dance as large-attachment replies: createForward
            # keeps the original content, then stream the new files up.
            draft = self._post(f"{base}/messages/{raw_id}/createForward",
                               None, scopes)
            did = draft["id"]
            quoted = (draft.get("body") or {}).get("content", "")
            patch: dict = {
                "body": {"contentType": "HTML",
                         "content": f"<div>{self._comment_html(comment, html)}"
                                    f"</div>{quoted}"},
                "toRecipients": self._recipients(to),
            }
            if cc:
                patch["ccRecipients"] = self._recipients(cc)
            self._patch(f"{base}/messages/{did}", patch, scopes)
            small, big2 = self._split_attachments(attachments)
            for obj in self._build_attachments(small):
                self._post(f"{base}/messages/{did}/attachments", obj, scopes)
            for a in big2:
                self._upload_attachment(base, did, a, scopes)
            self._post(f"{base}/messages/{did}/send", None, scopes)
            return
        payload: dict = {
            "toRecipients": self._recipients(to),
            "comment": self._comment_html(comment, html),
        }
        msg = {}
        if cc:
            msg["ccRecipients"] = self._recipients(cc)
        if attachments:
            msg["attachments"] = self._build_attachments(attachments)
        if msg:
            payload["message"] = msg
        self._post(f"{base}/messages/{raw_id}/forward", payload, scopes)

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
            for p in self._get_all(
                    f"/me/people?$select=displayName,scoredEmailAddresses&$top={limit}",
                    SCOPES_PEOPLE):
                name = p.get("displayName", "")
                for ea in p.get("scoredEmailAddresses", []) or []:
                    add(name, ea.get("address"))
        except GraphError:
            pass  # People.Read not granted yet (account predates the scope)

        try:
            for c in self._get_all(
                    f"/me/contacts?$select=displayName,emailAddresses&$top={limit}",
                    SCOPES_MAIL):
                name = c.get("displayName", "")
                for ea in c.get("emailAddresses", []) or []:
                    add(name or ea.get("name", ""), ea.get("address"))
        except GraphError:
            pass
        return out
