# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Shahab Nedaei
"""Gmail + Google Calendar REST client.

Returns the same normalized dict shapes as the Microsoft GraphClient so the
Mail/Calendar views are provider-agnostic:

  message: {id, subject, from, received, preview, is_read}
  event:   {id, subject, start, end, location, all_day}

Tokens are supplied lazily by ``token_provider(scopes)`` (see
core.auth.google_oauth).
"""

from __future__ import annotations

import base64
import html
import json
import urllib.error
import urllib.parse
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Callable, Sequence

from ...core.auth.google_oauth import (
    SCOPES_CALENDAR,
    SCOPES_CHAT,
    SCOPES_CONTACTS,
    SCOPES_MAIL,
)

GMAIL = "https://gmail.googleapis.com/gmail/v1"
CALENDAR = "https://www.googleapis.com/calendar/v3"
PEOPLE = "https://people.googleapis.com/v1"
CHAT = "https://chat.googleapis.com/v1"


class GoogleError(Exception):
    pass


def _decode_b64url(data: str) -> str:
    padded = data + "=" * (-len(data) % 4)
    try:
        return base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return ""


def _extract_rich(payload: dict) -> tuple[str, bool]:
    """Walk a Gmail payload, preferring text/html. Returns (content, is_html).

    The reader renders HTML, so we surface the richest body we can find and
    fall back to text/plain only when there's no HTML alternative.
    """
    mime = payload.get("mimeType", "")
    data = payload.get("body", {}).get("data")
    if mime == "text/html" and data:
        return _decode_b64url(data), True
    if mime == "text/plain" and data:
        return _decode_b64url(data), False

    html_part = ""
    text_part = ""
    for part in payload.get("parts", []):
        content, is_html = _extract_rich(part)
        if not content:
            continue
        if is_html and not html_part:
            html_part = content
        elif not is_html and not text_part:
            text_part = content
    if html_part:
        return html_part, True
    return text_part, False


class GoogleClient:
    def __init__(self, token_provider: Callable[[Sequence[str]], str | None]):
        self._token_provider = token_provider

    def _get(self, url: str, scopes: Sequence[str]) -> dict:
        token = self._token_provider(scopes)
        if not token:
            raise GoogleError("not signed in (no token for the requested scopes)")
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            raise GoogleError(f"Google {exc.code}: {exc.read().decode(errors='replace')}") from exc

    def _post(self, url: str, body: dict | None, scopes: Sequence[str]) -> dict:
        token = self._token_provider(scopes)
        if not token:
            raise GoogleError("not signed in (no token for the requested scopes)")
        data = json.dumps(body).encode() if body is not None else b""
        req = urllib.request.Request(
            url, data=data, method="POST",
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            raise GoogleError(f"Google {exc.code}: {exc.read().decode(errors='replace')}") from exc

    def _patch(self, url: str, body: dict, scopes: Sequence[str]) -> dict:
        token = self._token_provider(scopes)
        if not token:
            raise GoogleError("not signed in (no token for the requested scopes)")
        req = urllib.request.Request(
            url, data=json.dumps(body).encode(), method="PATCH",
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            raise GoogleError(f"Google {exc.code}: {exc.read().decode(errors='replace')}") from exc

    def _delete(self, url: str, scopes: Sequence[str]) -> None:
        token = self._token_provider(scopes)
        if not token:
            raise GoogleError("not signed in (no token for the requested scopes)")
        req = urllib.request.Request(
            url, method="DELETE", headers={"Authorization": f"Bearer {token}"})
        try:
            with urllib.request.urlopen(req, timeout=30):
                return
        except urllib.error.HTTPError as exc:
            raise GoogleError(f"Google {exc.code}: {exc.read().decode(errors='replace')}") from exc

    # -- Mail (Gmail) -----------------------------------------------------
    # Friendly names + display order for Gmail's system labels; user-created
    # labels follow, alphabetically.
    _SYSTEM_LABELS = [
        ("INBOX", "Inbox"), ("STARRED", "Starred"), ("IMPORTANT", "Important"),
        ("SENT", "Sent"), ("DRAFT", "Drafts"), ("SPAM", "Spam"), ("TRASH", "Trash"),
    ]

    def list_folders(self) -> list[dict]:
        """Provider-agnostic folder list: ``[{id, name, unread}]`` (Gmail labels).

        Gmail's ``labels.list`` carries no unread counts, so ``unread`` is 0;
        the curated system labels lead, then user labels alphabetically.
        """
        data = self._get(f"{GMAIL}/users/me/labels", SCOPES_MAIL)
        by_id = {lab["id"]: lab for lab in data.get("labels", [])}
        out = []
        for lid, name in self._SYSTEM_LABELS:
            if lid in by_id:
                out.append({"id": lid, "name": name, "unread": 0})
        user = sorted(
            (lab for lab in data.get("labels", []) if lab.get("type") == "user"),
            key=lambda lab: lab.get("name", "").lower(),
        )
        for lab in user:
            out.append({"id": lab["id"], "name": lab.get("name", ""), "unread": 0})
        return out

    def inbox_unread(self) -> int:
        """Unread message count of the Inbox (Gmail's INBOX label carries it)."""
        data = self._get(f"{GMAIL}/users/me/labels/INBOX", SCOPES_MAIL)
        return int(data.get("messagesUnread", 0) or 0)

    def list_messages(self, folder_id: str = "INBOX", *, limit: int = 15) -> list[dict]:
        return self.list_messages_page(folder_id, limit=limit)[0]

    def list_messages_page(self, folder_id: str = "INBOX", *, limit: int = 15,
                           page_token: str | None = None, query: str = ""):
        """Like :meth:`list_messages` but returns ``(messages, next_token)``.

        ``next_token`` is Gmail's ``nextPageToken`` or ``None``; pass it back as
        ``page_token`` to fetch the following page of older messages. When
        ``query`` is set it is passed as Gmail's ``q=`` (full Gmail search
        syntax), still scoped to ``folder_id`` via ``labelIds``."""
        url = (f"{GMAIL}/users/me/messages"
               f"?labelIds={folder_id}&maxResults={limit}")
        if query:
            url += f"&q={urllib.parse.quote(query)}"
        if page_token:
            url += f"&pageToken={urllib.parse.quote(page_token)}"
        listing = self._get(url, SCOPES_MAIL)
        out = []
        for ref in listing.get("messages", []):
            if not ref.get("id"):
                continue
            msg = self._get(
                f"{GMAIL}/users/me/messages/{ref['id']}"
                f"?format=metadata&metadataHeaders=Subject&metadataHeaders=From",
                SCOPES_MAIL,
            )
            out.append(self._message_from_json(msg))
        return out, listing.get("nextPageToken")

    @staticmethod
    def _headers(payload: dict) -> dict:
        """Header name→value map, tolerating malformed entries (a single header
        without name/value must not sink the whole folder render)."""
        out: dict = {}
        for h in payload.get("headers", []) or []:
            name = (h.get("name") or "").lower()
            if name:
                out[name] = h.get("value", "") or ""
        return out

    @staticmethod
    def _received(msg: dict) -> str:
        """internalDate (ms since epoch) as an ISO string, "" when unparsable."""
        try:
            return datetime.fromtimestamp(
                int(msg.get("internalDate") or 0) / 1000, tz=timezone.utc
            ).strftime("%Y-%m-%dT%H:%M:%SZ") if msg.get("internalDate") else ""
        except (ValueError, TypeError, OSError, OverflowError):
            return ""

    @classmethod
    def _message_from_json(cls, msg: dict) -> dict:
        headers = cls._headers(msg.get("payload", {}))
        received = cls._received(msg)
        labels = msg.get("labelIds", [])
        # Gmail's snippet (and occasionally header values) are HTML-escaped
        # ('&amp;', '&#39;', …); decode so plain Gtk.Labels read naturally.
        return {
            "id": msg.get("id", ""),
            "subject": html.unescape(headers.get("subject", "(no subject)")),
            "from": html.unescape(headers.get("from", "")),
            "received": received,
            "preview": html.unescape(msg.get("snippet", "")),
            "is_read": "UNREAD" not in labels,
            "important": "IMPORTANT" in labels,
            "starred": "STARRED" in labels,
        }

    def get_message(self, message_id: str) -> dict:
        data = self._get(f"{GMAIL}/users/me/messages/{message_id}?format=full", SCOPES_MAIL)
        payload = data.get("payload", {})
        headers = self._headers(payload)
        received = self._received(data)
        content, is_html = _extract_rich(payload)
        if not content:
            content, is_html = html.unescape(data.get("snippet", "")), False
        return {
            "id": data.get("id", message_id),
            "subject": html.unescape(headers.get("subject", "(no subject)")),
            "from": html.unescape(headers.get("from", "")),
            "to": html.unescape(headers.get("to", "")),
            "cc": html.unescape(headers.get("cc", "")),
            "bcc": html.unescape(headers.get("bcc", "")),
            "received": received,
            "body": content,
            "body_html": is_html,
            "attachments": self._collect_attachments(payload),
        }

    @staticmethod
    def _collect_attachments(payload) -> list[dict]:
        """Named attachment parts as ``[{id, name, content_type, size}]`` (bytes
        fetched on demand by :meth:`fetch_mail_attachment`)."""
        out: list[dict] = []

        def walk(part) -> None:
            body = part.get("body", {}) or {}
            filename = part.get("filename") or ""
            if filename and body.get("attachmentId"):
                out.append({
                    "id": body["attachmentId"],
                    "name": filename,
                    "content_type": part.get("mimeType", "") or "",
                    "size": body.get("size", 0) or 0,
                })
            for sub in part.get("parts", []) or []:
                walk(sub)

        walk(payload or {})
        return out

    def fetch_mail_attachment(self, message_id: str, attachment_id: str) -> bytes:
        """Download one Gmail attachment's bytes (base64url-decoded)."""
        data = self._get(
            f"{GMAIL}/users/me/messages/{message_id}/attachments/{attachment_id}",
            SCOPES_MAIL)
        raw = data.get("data", "")
        return base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)) if raw else b""

    def mark_read(self, message_id: str, read: bool = True) -> None:
        """Mark a message read/unread by toggling the UNREAD label (gmail.modify)."""
        body = {"removeLabelIds": ["UNREAD"]} if read else {"addLabelIds": ["UNREAD"]}
        self._post(f"{GMAIL}/users/me/messages/{message_id}/modify", body, SCOPES_MAIL)

    def delete_message(self, message_id: str) -> None:
        """Move a message to Trash (recoverable; needs gmail.modify)."""
        self._post(f"{GMAIL}/users/me/messages/{message_id}/trash", None, SCOPES_MAIL)

    def move_message(self, message_id: str, folder_id: str, *,
                     from_folder: str | None = None) -> None:
        """Move a message between folders. Gmail folders are labels, so a move
        adds the destination label and drops the source one (gmail.modify)."""
        body: dict = {"addLabelIds": [folder_id]}
        if from_folder and from_folder != folder_id:
            body["removeLabelIds"] = [from_folder]
        self._post(f"{GMAIL}/users/me/messages/{message_id}/modify", body,
                   SCOPES_MAIL)

    def set_flag(self, message_id: str, flagged: bool = True) -> None:
        """Star/unstar a message — Gmail's equivalent of Outlook's follow-up
        flag (gmail.modify)."""
        body = ({"addLabelIds": ["STARRED"]} if flagged
                else {"removeLabelIds": ["STARRED"]})
        self._post(f"{GMAIL}/users/me/messages/{message_id}/modify", body,
                   SCOPES_MAIL)

    # -- compose / reply --------------------------------------------------
    @staticmethod
    def _raw_message(to, subject: str, body: str, *, cc=None, bcc=None,
                     html: bool = False, headers: dict | None = None,
                     attachments=None, importance: str | None = None) -> str:
        """Build an RFC-2822 message, base64url-encoded for Gmail's ``raw`` field.

        ``attachments`` are ``[{name, content_type, data, inline?, content_id?}]``;
        inline images are attached as ``related`` parts of the HTML body and
        referenced by ``cid:``."""
        from email.message import EmailMessage

        msg = EmailMessage()
        msg["To"] = ", ".join(a for a in (to or []) if a)
        if cc:
            msg["Cc"] = ", ".join(a for a in cc if a)
        if bcc:
            msg["Bcc"] = ", ".join(a for a in bcc if a)
        msg["Subject"] = subject
        if importance and importance != "normal":
            # X-Priority 1 = High, 5 = Low; Importance header for good measure.
            msg["X-Priority"] = "1" if importance == "high" else "5"
            msg["Importance"] = "high" if importance == "high" else "low"
        for name, value in (headers or {}).items():
            if value:
                msg[name] = value
        inline = [a for a in (attachments or []) if a.get("inline")]
        files = [a for a in (attachments or []) if not a.get("inline")]
        if html:
            msg.set_content("(This message is in HTML.)")
            msg.add_alternative(body, subtype="html")
            html_part = msg.get_payload()[-1]  # the HTML alternative
            for a in inline:
                maintype, _, subtype = (
                    a.get("content_type") or "image/png").partition("/")
                html_part.add_related(
                    a["data"], maintype, subtype or "png",
                    cid=f"<{a.get('content_id', '')}>")
        else:
            msg.set_content(body)
        for a in files:
            maintype, _, subtype = (
                a.get("content_type") or "application/octet-stream").partition("/")
            msg.add_attachment(a["data"], maintype=maintype,
                               subtype=subtype or "octet-stream",
                               filename=a.get("name") or "attachment")
        return base64.urlsafe_b64encode(msg.as_bytes()).decode()

    def send_mail(self, *, to, subject: str, body: str, source: str = "me",
                  address: str | None = None, cc=None, bcc=None, html: bool = False,
                  attachments=None, importance: str | None = None,
                  read_receipt: bool = False) -> None:
        """Send a new message. ``read_receipt`` is accepted for API parity only."""
        raw = self._raw_message(to, subject, body, cc=cc, bcc=bcc, html=html,
                                attachments=attachments, importance=importance)
        self._post(f"{GMAIL}/users/me/messages/send", {"raw": raw}, SCOPES_MAIL)

    def save_draft(self, *, to, subject: str, body: str, cc=None, bcc=None,
                   html: bool = False, attachments=None, source: str = "me",
                   address: str | None = None) -> None:
        """Save an unfinished message into Gmail's Drafts so it can be resumed
        here or in any other client. ``source``/``address`` accepted for API
        parity with the Graph client."""
        raw = self._raw_message(to, subject, body, cc=cc, bcc=bcc, html=html,
                                attachments=attachments)
        self._post(f"{GMAIL}/users/me/drafts", {"message": {"raw": raw}},
                   SCOPES_MAIL)

    def reply_mail(self, message_id: str, body: str, *, reply_all: bool = False,
                   html: bool = False, attachments=None,
                   read_receipt: bool = False) -> None:
        """Reply on the original thread (subject ``Re: …``). A plain reply goes to
        the original sender; ``reply_all`` also carries the original To recipients
        and Cc (minus your own address), like Outlook/Gmail's Reply all."""
        from email.utils import formataddr, getaddresses

        meta = self._get(
            f"{GMAIL}/users/me/messages/{message_id}"
            f"?format=metadata&metadataHeaders=From&metadataHeaders=To"
            f"&metadataHeaders=Cc&metadataHeaders=Subject"
            f"&metadataHeaders=Message-ID&metadataHeaders=References",
            SCOPES_MAIL,
        )
        hdrs = self._headers(meta.get("payload", {}))
        subject = hdrs.get("subject", "")
        if not subject.lower().startswith("re:"):
            subject = f"Re: {subject}"
        msg_id = hdrs.get("message-id", "")
        references = " ".join(x for x in (hdrs.get("references", ""), msg_id) if x)

        # Build recipients, deduping and dropping our own address so reply-all
        # doesn't loop back to us. To = sender (+ original To on reply-all);
        # Cc = original Cc on reply-all.
        seen = {self._my_address()}
        to, cc = [], []
        for bucket, header in ((to, "from"),
                               *(((to, "to"), (cc, "cc")) if reply_all else ())):
            for name, addr in getaddresses([hdrs.get(header, "")]):
                if addr and addr.lower() not in seen:
                    seen.add(addr.lower())
                    bucket.append(formataddr((name, addr)))

        raw = self._raw_message(
            to, subject, body, cc=cc or None, html=html, attachments=attachments,
            headers={"In-Reply-To": msg_id, "References": references},
        )
        self._post(f"{GMAIL}/users/me/messages/send",
                   {"raw": raw, "threadId": meta.get("threadId", "")}, SCOPES_MAIL)

    def _my_address(self) -> str:
        """The signed-in user's own email (cached) — used to exclude self from
        reply-all recipients. Empty string if the profile can't be fetched."""
        if not hasattr(self, "_email"):
            try:
                self._email = (self._get(f"{GMAIL}/users/me/profile", SCOPES_MAIL)
                               .get("emailAddress", "") or "").lower()
            except GoogleError:
                self._email = ""
        return self._email

    # -- Contacts ---------------------------------------------------------
    def list_contacts(self, *, limit: int = 1000) -> list[dict]:
        """People for To-field autocomplete as ``[{name, email}]``: saved
        contacts (connections) plus auto-saved "other contacts" (people you've
        emailed). Each source is best-effort so one missing scope doesn't wipe
        out the other."""
        out: list[dict] = []
        seen: set[str] = set()

        def add(name: str, addr: str) -> None:
            key = (addr or "").lower()
            if addr and key not in seen:
                seen.add(key)
                out.append({"name": name or addr, "email": addr})

        def harvest(people: list) -> None:
            for p in people or []:
                names = p.get("names") or []
                name = names[0].get("displayName", "") if names else ""
                for ea in p.get("emailAddresses", []) or []:
                    add(name, ea.get("value"))

        try:
            data = self._get(
                f"{PEOPLE}/people/me/connections"
                f"?personFields=names,emailAddresses&pageSize={limit}", SCOPES_CONTACTS)
            harvest(data.get("connections", []))
        except GoogleError:
            pass
        try:
            data = self._get(
                f"{PEOPLE}/otherContacts"
                f"?readMask=names,emailAddresses&pageSize={min(limit, 1000)}",
                SCOPES_CONTACTS)
            harvest(data.get("otherContacts", []))
        except GoogleError:
            pass
        return out

    # -- Calendar ---------------------------------------------------------
    # Google accounts (unlike a personal Microsoft mailbox) routinely have many
    # calendars — a primary, plus birthdays, holidays, subscribed and secondary
    # ones. We aggregate events from every *shown* calendar (mirroring what the
    # user sees in Google Calendar) into one agenda, the way Microsoft merges its
    # Me/Teams/Shared sources. Each non-primary event id is wrapped
    # ``gcal\x1f<calendarId>\x1f<eventId>`` so detail/edit/delete route back to the
    # owning calendar (the same id-prefix trick Graph uses for group:/shared:).
    _CAL_SEP = "\x1f"

    @classmethod
    def _wrap_event_id(cls, calendar_id: str, raw_id: str) -> str:
        if calendar_id and calendar_id != "primary":
            return f"gcal{cls._CAL_SEP}{calendar_id}{cls._CAL_SEP}{raw_id}"
        return raw_id

    @classmethod
    def _unwrap_event_id(cls, event_id: str) -> tuple[str, str]:
        """``(calendar_id, raw_event_id)`` — 'primary' for an unwrapped id."""
        prefix = f"gcal{cls._CAL_SEP}"
        if event_id.startswith(prefix):
            _, calendar_id, raw_id = event_id.split(cls._CAL_SEP, 2)
            return calendar_id, raw_id
        return "primary", event_id

    @staticmethod
    def _cal_path(calendar_id: str) -> str:
        return urllib.parse.quote(calendar_id or "primary", safe="")

    def list_calendars(self) -> list[dict]:
        """The user's calendar list: ``[{id, name, primary, selected, color,
        writable}]`` (needs calendar.events / calendarlist read)."""
        url = (f"{CALENDAR}/users/me/calendarList?maxResults=250&fields="
               "items(id,summary,summaryOverride,primary,selected,"
               "backgroundColor,accessRole)")
        data = self._get(url, SCOPES_CALENDAR)
        out = []
        for c in data.get("items", []):
            out.append({
                "id": c.get("id", ""),
                "name": c.get("summaryOverride") or c.get("summary", ""),
                "primary": bool(c.get("primary")),
                "selected": bool(c.get("selected")),
                "color": c.get("backgroundColor", ""),
                "writable": c.get("accessRole") in ("owner", "writer"),
            })
        return out

    def list_events(self, start_iso: str, end_iso: str, *, limit: int = 50) -> list[dict]:
        # Resolve which calendars to show; fall back to just 'primary' if the
        # calendar list can't be fetched (older token / transient error).
        try:
            calendars = [c for c in self.list_calendars()
                         if c["selected"] or c["primary"]]
        except GoogleError:
            calendars = []
        if not calendars:
            calendars = [{"id": "primary", "name": "", "primary": True,
                          "color": "", "writable": True}]

        params = urllib.parse.urlencode({
            "timeMin": start_iso,
            "timeMax": end_iso,
            "singleEvents": "true",
            "orderBy": "startTime",
            "maxResults": str(limit),
        })

        def fetch(cal: dict) -> list[dict]:
            try:
                data = self._get(
                    f"{CALENDAR}/calendars/{self._cal_path(cal['id'])}/events?{params}",
                    SCOPES_CALENDAR)
            except GoogleError:
                return []  # one unreachable calendar must not sink the whole agenda
            evs = []
            for e in data.get("items", []):
                ev = self._event_from_json(e)
                ev["id"] = self._wrap_event_id(cal["id"], ev["id"])
                ev["calendar"] = cal["name"]
                ev["color"] = cal["color"]
                evs.append(ev)
            return evs

        out: list[dict] = []
        with ThreadPoolExecutor(max_workers=min(8, len(calendars))) as pool:
            for evs in pool.map(fetch, calendars):
                out.extend(evs)
        out.sort(key=lambda e: e.get("start", ""))
        return out

    @staticmethod
    def _event_from_json(e: dict) -> dict:
        start = e.get("start", {})
        end = e.get("end", {})
        # The current user's own attendee entry is flagged ``self: true``; its
        # responseStatus (needsAction|declined|tentative|accepted) drives the
        # greyed "unanswered invite" styling in the agenda.
        response = ""
        for a in e.get("attendees", []) or []:
            if a.get("self"):
                response = a.get("responseStatus", "")
                break
        all_day = "date" in start
        end_value = end.get("dateTime") or end.get("date", "")
        if all_day and end_value:
            # Google returns all-day end dates as exclusive; normalize to the
            # last day the event actually occurs, matching the Graph convention.
            from datetime import timedelta
            try:
                end_value = (datetime.fromisoformat(end_value) - timedelta(days=1)).date().isoformat()
            except ValueError:
                pass
        return {
            "id": e.get("id", ""),
            "subject": e.get("summary", "(no title)"),
            "start": start.get("dateTime") or start.get("date", ""),
            "end": end_value,
            "location": e.get("location", ""),
            "all_day": all_day,
            "response": response,
            "online_url": e.get("hangoutLink", ""),
        }

    @staticmethod
    def _event_slot(iso: str, all_day: bool) -> dict:
        """A Google start/end slot: all-day events use a bare ``date``; timed
        events use the full ``dateTime`` (shared by create/update)."""
        return {"date": iso[:10]} if all_day else {"dateTime": iso}

    def create_event(self, *, subject: str, start_iso: str, end_iso: str,
                     location: str = "", body: str = "", attendees=None,
                     all_day: bool = False, source: str = "me",
                     address: str | None = None, html: bool = False,
                     online: bool = False) -> dict:
        """Create an event on the primary calendar (needs calendar.events).

        All-day events use a ``date`` (the calendar day from the ISO string);
        timed events use the full ``dateTime``. ``online=True`` requests a Google
        Meet link — Google fills ``hangoutLink`` (and ``conferenceData``) on the
        returned event (``conferenceDataVersion=1`` is required for that)."""
        event = {"summary": subject,
                 "start": self._event_slot(start_iso, all_day),
                 "end": self._event_slot(end_iso, all_day)}
        if location:
            event["location"] = location
        if body:
            event["description"] = body
        if attendees:
            event["attendees"] = [{"email": a} for a in attendees if a]
        url = f"{CALENDAR}/calendars/primary/events"
        if online:
            event["conferenceData"] = {"createRequest": {
                "requestId": uuid.uuid4().hex,
                "conferenceSolutionKey": {"type": "hangoutsMeet"}}}
            url += "?conferenceDataVersion=1"
        return self._post(url, event, SCOPES_CALENDAR)

    def update_event(self, event_id: str, *, subject: str, start_iso: str,
                     end_iso: str, location: str = "", body: str = "",
                     attendees=None, all_day: bool = False, source: str = "me",
                     address: str | None = None, html: bool = False) -> dict:
        """Edit an existing event in its calendar (PATCH). Read-only calendars
        (holidays/birthdays) return a Google 403, surfaced as a toast."""
        cal, raw = self._unwrap_event_id(event_id)
        event: dict = {"summary": subject,
                       "start": self._event_slot(start_iso, all_day),
                       "end": self._event_slot(end_iso, all_day)}
        # PATCH: omit empty body/location so they're left unchanged on the server.
        if location:
            event["location"] = location
        if body:
            event["description"] = body
        # attendees: ``None`` = leave untouched; a list (even empty) = set it, so
        # removing every attendee in the editor actually clears them server-side.
        if attendees is not None:
            event["attendees"] = [{"email": a} for a in attendees if a]
        return self._patch(
            f"{CALENDAR}/calendars/{self._cal_path(cal)}/events/{raw}",
            event, SCOPES_CALENDAR)

    def delete_event(self, event_id: str) -> None:
        """Delete an event from its calendar (needs calendar.events)."""
        cal, raw = self._unwrap_event_id(event_id)
        self._delete(
            f"{CALENDAR}/calendars/{self._cal_path(cal)}/events/{raw}", SCOPES_CALENDAR)

    def get_event(self, event_id: str) -> dict:
        """Full event detail for the reading pane (read-only for Google)."""
        cal, raw = self._unwrap_event_id(event_id)
        data = self._get(
            f"{CALENDAR}/calendars/{self._cal_path(cal)}/events/{raw}", SCOPES_CALENDAR)
        base = self._event_from_json(data)
        base["id"] = event_id  # keep the wrapped id so edit/delete route correctly
        organizer = data.get("organizer") or {}
        # {name, email, response}; Google response:
        # needsAction|declined|tentative|accepted. email lets the inline editor
        # re-send the full desired attendee list when one is removed.
        attendees = [{"name": a.get("displayName") or a.get("email", ""),
                      "email": a.get("email", ""),
                      "response": a.get("responseStatus", "needsAction")}
                     for a in data.get("attendees", []) or []]
        description = data.get("description", "") or ""
        # The user can RSVP when they are an attendee and not the organizer.
        is_attendee = any(a.get("self") for a in data.get("attendees", []) or [])
        is_organizer = bool(organizer.get("self"))
        base.update({
            "organizer": organizer.get("displayName") or organizer.get("email", ""),
            "attendees": attendees,
            "body": description,
            "body_html": "<" in description and ">" in description,
            "online_url": data.get("hangoutLink", ""),
            "web_link": data.get("htmlLink", ""),
            # _event_from_json already pulled the self-attendee responseStatus.
            "can_respond": is_attendee and not is_organizer,
        })
        return base

    def find_event_by_uid(self, uid: str) -> str:
        """The primary-calendar event id matching an iMIP UID, or ``""``.
        Google stages emailed invites on the calendar keyed by their iCalUID,
        so the Mail view can reconcile an RSVP with that copy."""
        if not uid:
            return ""
        params = urllib.parse.urlencode({"iCalUID": uid, "maxResults": "1"})
        data = self._get(f"{CALENDAR}/calendars/primary/events?{params}",
                         SCOPES_CALENDAR)
        items = data.get("items", [])
        return items[0].get("id", "") if items else ""

    def respond_event(self, event_id: str, action: str,
                      comment: str = "", send: bool = True) -> None:
        """RSVP to a Google event (action: accept | tentativelyAccept | decline).

        Google has no dedicated accept endpoint: we fetch the event, flip the
        current user's attendee responseStatus, and PATCH the full attendee list
        back (a bare PATCH of ``attendees`` replaces the list, so the others must
        be preserved). ``sendUpdates`` notifies the organizer like Outlook's
        "send response". Needs the calendar.events (write) scope."""
        status = {"accept": "accepted", "tentativelyAccept": "tentative",
                  "decline": "declined"}.get(action)
        if status is None:
            raise GoogleError(f"unknown RSVP action: {action}")
        cal, raw = self._unwrap_event_id(event_id)
        data = self._get(
            f"{CALENDAR}/calendars/{self._cal_path(cal)}/events/{raw}", SCOPES_CALENDAR)
        attendees = data.get("attendees", []) or []
        found = False
        for a in attendees:
            if a.get("self"):
                a["responseStatus"] = status
                found = True
        if not found:
            raise GoogleError("You are not an attendee of this event.")
        self._patch(
            f"{CALENDAR}/calendars/{self._cal_path(cal)}/events/{raw}"
            f"?sendUpdates={'all' if send else 'none'}",
            {"attendees": attendees}, SCOPES_CALENDAR)

    # -- Chat (Google Chat — Workspace only) ------------------------------
    def list_chats(self, *, limit: int = 50) -> list[dict]:
        """The user's Chat spaces (DMs + rooms)."""
        return self.list_chats_page(limit=limit)[0]

    def list_chats_page(self, *, limit: int = 50, page_token: str | None = None):
        """A page of spaces, returning ``(chats, next_token)`` for "load more"."""
        url = f"{CHAT}/spaces?pageSize={limit}"
        if page_token:
            url += f"&pageToken={urllib.parse.quote(page_token)}"
        data = self._get(url, SCOPES_CHAT)
        out = []
        for s in data.get("spaces", []):
            stype = s.get("spaceType") or s.get("type", "")
            name = s.get("displayName") or (
                "Direct message" if stype == "DIRECT_MESSAGE" else "Space")
            out.append({
                "id": s.get("name", ""),  # "spaces/AAA"
                "name": html.unescape(name),
                "kind": stype,
                "preview": "",
                "last_at": "",
                "unread": False,  # Chat API has no simple per-space unread flag
                "from_me": False,
            })
        return out, data.get("nextPageToken")

    def start_chat(self, recipient: str, text: str = "") -> str:
        raise GoogleError(
            "Starting new chats isn't supported for Google Chat yet.")

    def list_chat_messages(self, chat_id: str, *, limit: int = 30) -> list[dict]:
        return self.list_chat_messages_page(chat_id, limit=limit)[0]

    def list_chat_messages_page(self, chat_id: str, *, limit: int = 30,
                                page_token: str | None = None):
        """A page of a space's messages, oldest-first within the page.

        Returns ``(messages, next_token)``; ``next_token`` fetches the *older*
        page above the current one."""
        query = {"pageSize": limit, "orderBy": "createTime desc"}
        if page_token:
            query["pageToken"] = page_token
        params = urllib.parse.urlencode(query)
        data = self._get(f"{CHAT}/{chat_id}/messages?{params}", SCOPES_CHAT)
        out = [self._chat_message_row(m) for m in data.get("messages", [])]
        out.reverse()
        return out, data.get("nextPageToken")

    @staticmethod
    def _chat_message_row(m: dict) -> dict:
        sender = m.get("sender") or {}
        attachments = [
            {"name": a.get("contentName") or "attachment",
             "url": a.get("downloadUri") or a.get("thumbnailUri") or "",
             "content_type": a.get("contentType", "")}
            for a in (m.get("attachments") or m.get("attachment") or [])
        ]
        return {
            "id": m.get("name", ""),
            "text": m.get("text", "") or m.get("formattedText", ""),
            "from": html.unescape(sender.get("displayName", "") or ""),
            "sent": m.get("createTime", ""),
            # User-auth Chat API doesn't expose our own user id for comparison.
            "is_mine": False,
            "attachments": attachments,
        }

    def send_chat_message(self, chat_id: str, text: str) -> dict:
        return self._post(f"{CHAT}/{chat_id}/messages", {"text": text}, SCOPES_CHAT)

    def fetch_bytes(self, url: str) -> bytes:
        """Download an attachment (with the bearer token) for inline display."""
        token = self._token_provider(SCOPES_CHAT)
        if not token:
            raise GoogleError("not signed in (no token for the requested scopes)")
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read()

    def send_chat_images(self, chat_id: str, images, text: str = "") -> dict:
        raise GoogleError("Sending images isn't supported for Google Chat yet.")

    def delete_chat_message(self, chat_id: str, message_id: str) -> None:
        self._delete(f"{CHAT}/{message_id}", SCOPES_CHAT)

    def edit_chat_message(self, chat_id: str, message_id: str, text: str) -> None:
        self._patch(f"{CHAT}/{message_id}", {"text": text}, SCOPES_CHAT)

    def list_chat_members(self, chat_id: str) -> list[dict]:
        return []  # Google Chat @mentions aren't wired yet

    def send_chat_html(self, chat_id: str, content_html: str,
                      mentions=None, images=None) -> dict:
        raise GoogleError("Rich messages aren't supported for Google Chat yet.")

    def search_messages(self, query: str, *, limit: int = 25) -> list[dict]:
        return []  # no cross-space message search wired for Google Chat

    def set_reaction(self, chat_id: str, message_id: str, emoji: str) -> None:
        raise GoogleError("Reactions aren't supported for Google Chat yet.")

    # -- presence / read state / group management -------------------------
    # Google Chat exposes none of these to the user-auth REST API in a way that
    # maps onto the Teams model, so they degrade quietly (the view treats an
    # empty presence map as "unknown" and hides the group-management UI).
    def get_presences(self, user_ids) -> dict:
        return {}

    def mark_chat_read(self, chat_id: str) -> None:
        return None  # no-op; Chat API has no per-space read marker we can set

    def start_group_chat(self, recipients, topic: str = "",
                         text: str = "") -> str:
        raise GoogleError("Starting group chats isn't supported for Google Chat yet.")

    def add_chat_member(self, chat_id: str, recipient: str,
                        share_history: bool = True) -> None:
        raise GoogleError("Managing members isn't supported for Google Chat yet.")

    def remove_chat_member(self, chat_id: str, membership_id: str) -> None:
        raise GoogleError("Managing members isn't supported for Google Chat yet.")

    def rename_chat(self, chat_id: str, topic: str) -> None:
        raise GoogleError("Renaming chats isn't supported for Google Chat yet.")
