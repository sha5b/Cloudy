# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Fiber Elements
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
import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Callable, Sequence

from ...core.auth.google_oauth import SCOPES_CALENDAR, SCOPES_MAIL

GMAIL = "https://gmail.googleapis.com/gmail/v1"
CALENDAR = "https://www.googleapis.com/calendar/v3"


class GoogleError(Exception):
    pass


def _decode_b64url(data: str) -> str:
    padded = data + "=" * (-len(data) % 4)
    try:
        return base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return ""


def _extract_body(payload: dict) -> str:
    """Walk a Gmail payload, preferring text/plain, then text/html."""
    mime = payload.get("mimeType", "")
    body_data = payload.get("body", {}).get("data")
    if mime == "text/plain" and body_data:
        return _decode_b64url(body_data)
    # Recurse into parts; collect a plain-text candidate, fall back to html.
    html = ""
    for part in payload.get("parts", []):
        text = _extract_body(part)
        if part.get("mimeType") == "text/plain" and text:
            return text
        if part.get("mimeType") == "text/html" and text and not html:
            html = text
        elif text and not html:
            html = text
    if mime == "text/html" and body_data:
        return _decode_b64url(body_data)
    return html


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

    # -- Mail (Gmail) -----------------------------------------------------
    def list_messages(self, folder_id: str = "INBOX", *, limit: int = 15) -> list[dict]:
        listing = self._get(
            f"{GMAIL}/users/me/messages?labelIds={folder_id}&maxResults={limit}",
            SCOPES_MAIL,
        )
        out = []
        for ref in listing.get("messages", []):
            msg = self._get(
                f"{GMAIL}/users/me/messages/{ref['id']}"
                f"?format=metadata&metadataHeaders=Subject&metadataHeaders=From",
                SCOPES_MAIL,
            )
            out.append(self._message_from_json(msg))
        return out

    @staticmethod
    def _message_from_json(msg: dict) -> dict:
        headers = {
            h["name"].lower(): h["value"]
            for h in msg.get("payload", {}).get("headers", [])
        }
        received = ""
        internal = msg.get("internalDate")
        if internal:
            received = datetime.fromtimestamp(
                int(internal) / 1000, tz=timezone.utc
            ).strftime("%Y-%m-%dT%H:%M:%SZ")
        return {
            "id": msg.get("id", ""),
            "subject": headers.get("subject", "(no subject)"),
            "from": headers.get("from", ""),
            "received": received,
            "preview": msg.get("snippet", ""),
            "is_read": "UNREAD" not in msg.get("labelIds", []),
        }

    def get_message(self, message_id: str) -> dict:
        data = self._get(f"{GMAIL}/users/me/messages/{message_id}?format=full", SCOPES_MAIL)
        payload = data.get("payload", {})
        headers = {h["name"].lower(): h["value"] for h in payload.get("headers", [])}
        received = ""
        if data.get("internalDate"):
            received = datetime.fromtimestamp(
                int(data["internalDate"]) / 1000, tz=timezone.utc
            ).strftime("%Y-%m-%dT%H:%M:%SZ")
        return {
            "id": data.get("id", message_id),
            "subject": headers.get("subject", "(no subject)"),
            "from": headers.get("from", ""),
            "to": headers.get("to", ""),
            "received": received,
            "body": _extract_body(payload) or data.get("snippet", ""),
        }

    # -- Calendar ---------------------------------------------------------
    def list_events(self, start_iso: str, end_iso: str, *, limit: int = 50) -> list[dict]:
        params = urllib.parse.urlencode({
            "timeMin": start_iso,
            "timeMax": end_iso,
            "singleEvents": "true",
            "orderBy": "startTime",
            "maxResults": str(limit),
        })
        data = self._get(f"{CALENDAR}/calendars/primary/events?{params}", SCOPES_CALENDAR)
        out = []
        for e in data.get("items", []):
            start = e.get("start", {})
            end = e.get("end", {})
            all_day = "date" in start
            out.append({
                "id": e.get("id", ""),
                "subject": e.get("summary", "(no title)"),
                "start": start.get("dateTime") or start.get("date", ""),
                "end": end.get("dateTime") or end.get("date", ""),
                "location": e.get("location", ""),
                "all_day": all_day,
            })
        return out
