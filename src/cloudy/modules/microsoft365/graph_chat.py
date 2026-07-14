# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Shahab Nedaei
"""Chat (Teams) domain of the Graph client: chats, messages, members,
presence, reactions, group management, hosted-content images and search."""

from __future__ import annotations

import base64
import concurrent.futures
import html
import json
import os
import re
import urllib.parse

from .graph_http import _REACTIONS, BASE_URL, GraphError

from .graph_markup import (
    html_to_pango,
    split_attachments,
    strip_html,
    strip_reply_placeholder,
)

from ...core.auth.msal_graph import (
    SCOPES_CHAT,
    SCOPES_FILES,
    SCOPES_PRESENCE,
)


class GraphChatMixin:
    # -- Chat (Teams) -----------------------------------------------------
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
        # Without $orderby Graph makes no ordering promise for /me/chats, so a
        # recently-active chat can fall outside the first page entirely and
        # never show up as "newest". Order by last activity server-side (the
        # space must be %-encoded or urllib rejects the URL).
        url = page_token or (
            f"/me/chats?$top={limit}&$expand=members,lastMessagePreview"
            "&$orderby=lastMessagePreview/createdDateTime%20desc")
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
        preview = strip_html((lmp.get("body") or {}).get("content", ""))
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
            "id": c.get("id", ""),
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
                text = (strip_html(body.get("content", ""))
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
        reply_to, forward, attachments = split_attachments(m)
        # Diagnostic: when a forwarded/embedded message isn't recognized it
        # collapses to a bare "attachment" chip. Set CLOUDY_DEBUG_CHAT=1 to dump
        # the raw attachment shapes so the parser can be taught the exact schema.
        if os.environ.get("CLOUDY_DEBUG_CHAT") and forward is None \
                and m.get("attachments"):
            try:
                print("[chat-debug] attachments:",
                      json.dumps(m.get("attachments"), indent=2)[:2000])
            except Exception:  # noqa: BLE001 - debug only
                pass
        # Strip the reply placeholder so the quoted text doesn't pollute the body
        # (we render the quote separately from ``reply_to``).
        if is_html:
            content = strip_reply_placeholder(content)
        text = strip_html(content) if is_html else content
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
            "id": m.get("id", ""),
            "text": text,
            "markup": html_to_pango(content) if is_html else "",
            "from": html.unescape(user.get("displayName", "") or ""),
            "sent": m.get("createdDateTime", ""),
            "is_mine": bool(user.get("id")) and user.get("id") == me_id,
            "attachments": attachments,
            "reactions": [{"emoji": e, "count": c} for e, c in reactions.items()],
            "web_url": m.get("webUrl", "") or "",
            "reply_to": reply_to,
            "forward": forward,
        }

    def send_chat_message(self, chat_id: str, text: str) -> dict:
        return self._post(
            f"/me/chats/{chat_id}/messages",
            {"body": {"contentType": "text", "content": text}},
            SCOPES_CHAT,
        )

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
        """Add a Unicode emoji reaction to a chat message."""
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
        cached = getattr(self, "_cached_tenant_id", None)
        if cached is not None:  # "" is a valid (resolved-but-empty) result
            return cached
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
        }
        if share_history:
            body["visibleHistoryStartDateTime"] = "0001-01-01T00:00:00Z"
        self._post(f"/chats/{chat_id}/members", body, SCOPES_CHAT)

    def remove_chat_member(self, chat_id: str, membership_id: str) -> None:
        """Remove a member (by conversationMember id) from a group chat."""
        self._delete(f"/chats/{chat_id}/members/{membership_id}", SCOPES_CHAT)

    def rename_chat(self, chat_id: str, topic: str) -> None:
        """Set a group chat's topic (its display name)."""
        self._patch(f"/chats/{chat_id}", {"topic": topic}, SCOPES_CHAT)

    def send_chat_html(self, chat_id: str, content_html: str,
                      mentions=None, images=None, file_attachments=None) -> dict:
        """Send an HTML message (carries @mentions, inline images and/or file
        attachments).

        ``content_html`` already contains the escaped text + any ``<at>`` tags;
        ``mentions`` is the Graph mentions array; ``images`` is ``[(bytes, ctype)]``
        (rendered inline as hosted contents); ``file_attachments`` is a list of
        ``{id, name, contentUrl}`` reference attachments (already uploaded to the
        user's OneDrive via :meth:`upload_chat_file`)."""
        for i, (data, ctype) in enumerate(images or [], start=1):
            content_html += (
                f'<img src="../hostedContents/{i}/$value" style="max-width:400px">')
        # Each reference attachment needs an <attachment> placeholder in the body
        # whose id matches its entry, or Graph rejects the message.
        for att in file_attachments or []:
            content_html += f'<attachment id="{att["id"]}"></attachment>'
        body = {"body": {"contentType": "html", "content": content_html}}
        if mentions:
            body["mentions"] = mentions
        if images:
            body["hostedContents"] = [{
                "@microsoft.graph.temporaryId": str(i),
                "contentBytes": base64.b64encode(data).decode(),
                "contentType": ctype or "image/png",
            } for i, (data, ctype) in enumerate(images, start=1)]
        if file_attachments:
            body["attachments"] = [{
                "id": att["id"], "contentType": "reference",
                "contentUrl": att["contentUrl"], "name": att["name"],
            } for att in file_attachments]
        return self._post(f"/me/chats/{chat_id}/messages", body, SCOPES_CHAT)

    # Teams stores chat attachments in this OneDrive folder; we do the same so the
    # files show up alongside the ones Teams itself uploads.
    _CHAT_FILES_FOLDER = "Microsoft Teams Chat Files"

    def upload_chat_file(self, filename: str, data: bytes,
                         content_type: str = "", chat_id: str = "") -> dict:
        """Upload a file to the user's OneDrive 'Microsoft Teams Chat Files'
        folder and return a reference-attachment dict ``{id, name, contentUrl}``
        for :meth:`send_chat_html`. This is how non-image files are sent to a
        chat (images go inline via hosted contents instead). Pass ``chat_id``
        so the chat's members are granted access to the file — a reference
        attachment is just a link into *your* OneDrive, and without an explicit
        permission grant every recipient gets a 403 when opening it (Teams
        itself does this grant when you attach a file)."""
        name = filename or "file"
        safe = urllib.parse.quote(name)
        folder = urllib.parse.quote(self._CHAT_FILES_FOLDER)
        path = (f"/me/drive/root:/{folder}/{safe}:/content"
                "?@microsoft.graph.conflictBehavior=rename")
        item = self._put_bytes(path, data, content_type or "application/octet-stream",
                               SCOPES_FILES)
        if chat_id and item.get("id"):
            self._grant_chat_file_access(item["id"], chat_id)
        # Teams keys the <attachment> placeholder on the driveItem eTag's GUID;
        # fall back to the item id if the eTag isn't in the expected shape.
        etag = item.get("eTag", "") or ""
        m = re.search(r"[0-9a-fA-F]{8}-[0-9a-fA-F-]{27,}", etag)
        att_id = m.group(0) if m else item.get("id", "")
        return {
            "id": att_id,
            "name": item.get("name", name),
            "contentUrl": item.get("webUrl", ""),
        }

    def _grant_chat_file_access(self, item_id: str, chat_id: str) -> None:
        """Give a chat's members permission on a just-uploaded file. Prefer a
        per-member invite (mirrors what Teams grants); fall back to an
        organization-scope view link when that fails (guest members, tenants
        that block direct sharing). Best-effort — the message still sends."""
        try:
            me = self._me_id()
            recipients = [
                {"email": m["email"]}
                for m in self.list_chat_members(chat_id)
                if m.get("email") and m.get("id") != me
            ]
            if recipients:
                self._post(f"/me/drive/items/{item_id}/invite", {
                    "recipients": recipients,
                    "requireSignIn": True,
                    "sendInvitation": False,
                    "roles": ["write"],
                }, SCOPES_FILES)
                return
        except GraphError:
            pass
        try:
            self._post(f"/me/drive/items/{item_id}/createLink",
                       {"type": "view", "scope": "organization"}, SCOPES_FILES)
        except GraphError:
            pass

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
                    summary = hit.get("summary") or strip_html(
                        (r.get("body") or {}).get("content", ""))
                    out.append({
                        "chat_id": r.get("chatId", ""),
                        "message_id": r.get("id", ""),
                        "from": html.unescape(sender.get("displayName", "") or ""),
                        "snippet": strip_html(summary),
                        "sent": r.get("createdDateTime", ""),
                    })
        return out
