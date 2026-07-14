# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Shahab Nedaei
"""Low-level Microsoft Graph HTTP plumbing shared by every domain mixin.

Holds the token-provider wiring, the raw GET/POST/PATCH/PUT/DELETE helpers,
paging (`_get_all`), the shared ``GraphError``/``Drive`` types and the
redirect handler that keeps bearer tokens off cross-host redirects.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Callable, Sequence

from ...core.auth.msal_graph import (
    SCOPES_BASE,
    SCOPES_CHAT,
)

BASE_URL = "https://graph.microsoft.com/v1.0"

# Teams' named reaction types → emoji (custom reactions arrive as unicode).
_REACTIONS = {
    "like": "👍", "heart": "❤️", "laugh": "😆", "surprised": "😮",
    "sad": "😢", "angry": "😠",
}


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


class GraphHttp:
    def __init__(self, token_provider: Callable[[Sequence[str]], str | None]):
        self._token_provider = token_provider

    # -- low-level --------------------------------------------------------
    @staticmethod
    def _open_retry(req, *, timeout: int = 30, attempts: int = 3):
        """urlopen with a bounded retry on Graph throttling (429, honoring
        Retry-After) and, for GETs, transient 503/504. All Graph calls run on
        worker threads, so a short sleep is fine. Non-idempotent verbs retry
        only 429 — a throttled request was rejected, never processed — so a
        retry can't double-send anything."""
        for attempt in range(attempts):
            try:
                return urllib.request.urlopen(req, timeout=timeout)
            except urllib.error.HTTPError as exc:
                method = (req.get_method() or "GET").upper()
                retriable = exc.code == 429 or (
                    exc.code in (503, 504) and method == "GET")
                if not retriable or attempt == attempts - 1:
                    raise
                try:
                    delay = float(exc.headers.get("Retry-After", "") or 1.5)
                except (TypeError, ValueError):
                    delay = 1.5
                try:
                    exc.read()  # drain so the connection can be reused
                except OSError:
                    pass
                time.sleep(min(max(delay, 0.5), 10.0))

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
            with self._open_retry(req, timeout=30) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")
            raise GraphError(f"Graph {exc.code}: {detail}") from exc

    def _get_all(self, path: str, scopes: Sequence[str],
                 headers: dict | None = None, *, max_pages: int = 50) -> list[dict]:
        """GET a collection, following ``@odata.nextLink`` so enumerations aren't
        capped at one page (Graph silently truncates at ``$top``/its default).
        Returns the concatenated ``value`` arrays. ``max_pages`` bounds a runaway
        cursor. Use for full enumerations (folders, groups, drives, contacts) —
        NOT for message/event lists, which page intentionally via the UI."""
        items: list[dict] = []
        url: str | None = path
        for _ in range(max_pages):
            data = self._get(url, scopes, headers)
            items.extend(data.get("value", []))
            url = data.get("@odata.nextLink")
            if not url:
                break
        return items

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
            with self._open_retry(req, timeout=30) as resp:
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
            with self._open_retry(req, timeout=30):
                return
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")
            raise GraphError(f"Graph {exc.code}: {detail}") from exc

    def _me_id(self) -> str:
        """The signed-in user's AAD object id (cached), to mark own messages."""
        if not getattr(self, "_cached_me_id", None):
            data = self._get("/me?$select=id", SCOPES_BASE)
            self._cached_me_id = data.get("id", "")
        return self._cached_me_id

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

    def _put_bytes(self, path: str, data: bytes, content_type: str,
                   scopes: Sequence[str]) -> dict:
        token = self._token_provider(scopes)
        if not token:
            raise GraphError("not signed in (no token for the requested scopes)")
        req = urllib.request.Request(
            f"{BASE_URL}{path}", data=data, method="PUT",
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": content_type or "application/octet-stream"})
        try:
            with self._open_retry(req, timeout=300) as resp:
                raw = resp.read().decode()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")
            raise GraphError(f"Graph {exc.code}: {detail}") from exc

    def _put_chunk(self, url: str, part: bytes, start: int, total: int) -> dict:
        """PUT one Content-Range chunk of an upload session. The uploadUrl is
        pre-authenticated (its auth travels in the URL), so no bearer header —
        sending one can even get the request rejected."""
        req = urllib.request.Request(
            url, data=part, method="PUT",
            headers={
                "Content-Type": "application/octet-stream",
                "Content-Range":
                    f"bytes {start}-{start + len(part) - 1}/{total}",
            })
        try:
            with self._open_retry(req, timeout=300) as resp:
                raw = resp.read().decode()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")
            raise GraphError(f"Graph {exc.code}: {detail}") from exc

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
            with self._open_retry(req, timeout=30) as resp:
                raw = resp.read().decode()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")
            raise GraphError(f"Graph {exc.code}: {detail}") from exc
