# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Shahab Nedaei
"""Teams-channels domain of the Graph client: joined teams, channels,
channel posts/replies, and the team's OneNote notebooks."""

from __future__ import annotations

import html
import re
import urllib.error
import urllib.parse  # used explicitly (urlsplit); don't rely on side-effect imports
import urllib.request

from .graph_http import _REACTIONS, BASE_URL, GraphError, _StripAuthOnRedirect

from .graph_markup import (
    html_to_pango,
    split_attachments,
    strip_html,
    strip_reply_placeholder,
)

from ...core.auth.msal_graph import (
    SCOPES_CHANNELS,
    SCOPES_NOTES,
    SCOPES_TEAMS,
)


class GraphTeamsMixin:
    # -- Teams channels (the Teams tab's Conversation) --------------------
    def list_joined_teams(self) -> list[dict]:
        """The Teams the user belongs to: ``[{id, name}]`` (the team id is also
        the backing M365 group id, used for channels + the group notebook).

        Lighter than :meth:`list_teams`, which additionally resolves each team's
        document library; the Teams tab only needs id + name. Needs
        ``Team.ReadBasic.All``."""
        teams = [{"id": t["id"], "name": t.get("displayName") or "Untitled Team"}
                 for t in self._get_all("/me/joinedTeams?$select=id,displayName",
                                        SCOPES_TEAMS) if t.get("id")]
        teams.sort(key=lambda t: t["name"].lower())
        return teams

    def list_team_channels(self, team_id: str) -> list[dict]:
        """A team's channels: ``[{id, name, description}]``, General first.
        Needs ``Channel.ReadBasic.All`` (tenant-admin consent)."""
        chans = [{"id": c["id"],
                  "name": c.get("displayName") or "Channel",
                  "description": c.get("description") or ""}
                 for c in self._get_all(
                     f"/teams/{team_id}/channels?$select=id,displayName,description",
                     SCOPES_CHANNELS) if c.get("id")]
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
        reply_to, forward, attachments = split_attachments(m)
        if is_html:
            content = strip_reply_placeholder(content)
        text = strip_html(content) if is_html else content
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
            "id": m.get("id", ""),
            "subject": html.unescape(m.get("subject") or ""),
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
        items = self._get_all(
            f"/groups/{team_id}/onenote/notebooks?$select=id,displayName",
            SCOPES_NOTES)
        return [{"id": n["id"], "name": n.get("displayName") or "Notebook"}
                for n in items if n.get("id")]

    def list_note_sections(self, team_id: str,
                           notebook_id: str = "") -> list[dict]:
        """Sections in a team notebook (or every section when ``notebook_id`` is
        empty): ``[{id, name}]``."""
        if notebook_id:
            path = f"/groups/{team_id}/onenote/notebooks/{notebook_id}/sections"
        else:
            path = f"/groups/{team_id}/onenote/sections"
        items = self._get_all(f"{path}?$select=id,displayName", SCOPES_NOTES)
        return [{"id": s["id"], "name": s.get("displayName") or "Section"}
                for s in items if s.get("id")]

    def list_note_pages(self, team_id: str, section_id: str, *,
                        limit: int = 50) -> list[dict]:
        """Pages in a section, newest first:
        ``[{id, title, web_url, last_at}]``."""
        items = self._get_all(
            f"/groups/{team_id}/onenote/sections/{section_id}/pages"
            "?$select=id,title,links,lastModifiedDateTime"
            f"&$top={limit}&$orderby=lastModifiedDateTime%20desc",
            SCOPES_NOTES)
        out = []
        for p in items:
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
