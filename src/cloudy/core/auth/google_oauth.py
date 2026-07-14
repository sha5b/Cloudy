# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Shahab Nedaei
"""Google OAuth2 for the Gmail and Calendar APIs.

Installed-app flow with **PKCE** via the **system browser + a loopback redirect**
— the same UX as the Microsoft side, implemented directly on urllib + a tiny
one-shot HTTP server so we pull in no heavy Google SDKs. The token (access +
refresh) is stored in libsecret; ``access_token`` refreshes transparently.

Must run off the GTK main thread (the interactive call blocks until the browser
flow completes). See docs/AUTH.md.
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import secrets as _secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Sequence

AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URI = "https://oauth2.googleapis.com/token"
USERINFO = "https://openidconnect.googleapis.com/v1/userinfo"

SCOPES_BASE = ["openid", "email", "profile"]
# gmail.modify (read + mark read/unread + trash) so the reader can write back;
# gmail.send adds compose/reply (modify alone does NOT permit sending). Existing
# accounts must re-sign-in (⋮ → Sign Out / Re-sign In) to pick these up.
SCOPES_MAIL = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
]
# calendar.events grants read + create/delete of events (supersedes the old
# read-only scope so the New event / Delete actions work).
# calendarlist.readonly is required for calendarList.list — calendar.events is
# NOT an accepted scope there, so without it listing the user's calendars
# always 403'd and multi-calendar aggregation silently degraded to
# primary-only. Existing accounts must re-sign-in to pick it up.
SCOPES_CALENDAR = [
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/calendar.calendarlist.readonly",
]
# Read-only access to Google Contacts + auto-saved "other contacts" (people you
# email but haven't saved) — for To: autocomplete.
SCOPES_CONTACTS = [
    "https://www.googleapis.com/auth/contacts.readonly",
    "https://www.googleapis.com/auth/contacts.other.readonly",
]
# Google Chat (Workspace only — consumer Gmail has no Chat API). These are
# RESTRICTED scopes: Google app verification is required before non-test users
# can consent, so the Chat tab degrades gracefully when access is denied.
SCOPES_CHAT = [
    "https://www.googleapis.com/auth/chat.spaces.readonly",
    "https://www.googleapis.com/auth/chat.messages",
]

_TOKEN_KIND = "google-token"


class AuthError(Exception):
    pass


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


class _CodeHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - http.server API
        # Record the redirect's params on the *server instance*, not the class,
        # so two concurrent sign-ins can't cross-contaminate each other's code.
        query = urllib.parse.urlparse(self.path).query
        params = urllib.parse.parse_qs(query)
        # Only record a request that actually carries OAuth params: browsers
        # also hit this server for /favicon.ico right after the redirect, and
        # unconditional assignment let that wipe the just-received code
        # (intermittent "no authorization code" / false state-mismatch).
        if "code" in params or "error" in params:
            self.server.auth_code = params.get("code", [None])[0]
            self.server.auth_error = params.get("error", [None])[0]
            self.server.auth_state = params.get("state", [None])[0]
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(
            b"<html><body><h2>Cloudy</h2>"
            b"<p>Sign-in complete. You can close this tab.</p></body></html>"
        )

    def log_message(self, *_args):  # silence the default stderr logging
        pass


class GoogleAuth:
    def __init__(self, client_id: str, secrets, account_id: str, client_secret: str = ""):
        if not client_id:
            raise AuthError(
                "No Google client ID configured. See docs/AUTH.md "
                "(set CLOUDY_GOOGLE_CLIENT_ID or the google-client-id setting)."
            )
        self._client_id = client_id
        self._client_secret = client_secret
        self._secrets = secrets
        self._account_id = account_id
        self._token = self._load()

    # -- persistence ------------------------------------------------------
    def _load(self) -> dict:
        blob = self._secrets.lookup(self._account_id, _TOKEN_KIND)
        return json.loads(blob) if blob else {}

    def _store(self) -> None:
        self._secrets.store(self._account_id, _TOKEN_KIND, json.dumps(self._token))

    # -- interactive (system browser + loopback + PKCE) ------------------
    def sign_in_interactive(self, scopes: Sequence[str] = None, open_url=None) -> dict:
        scopes = list(scopes or (SCOPES_BASE + SCOPES_MAIL + SCOPES_CALENDAR
                                  + SCOPES_CONTACTS + SCOPES_CHAT))
        verifier = _b64url(_secrets.token_bytes(48))
        challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
        state = _b64url(_secrets.token_bytes(24))  # CSRF / auth-code-injection guard

        server = http.server.HTTPServer(("127.0.0.1", 0), _CodeHandler)
        server.allow_reuse_address = True
        server.auth_code = server.auth_error = server.auth_state = None
        port = server.server_address[1]
        # Bind and redirect on the SAME literal (127.0.0.1) — Google treats
        # 127.0.0.1 and localhost as distinct, and localhost can resolve to IPv6
        # ::1 (where we don't listen), which hangs the redirect.
        redirect_uri = f"http://127.0.0.1:{port}/"

        params = {
            "client_id": self._client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(scopes),
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "access_type": "offline",
            "prompt": "consent",
        }
        url = f"{AUTH_URI}?{urllib.parse.urlencode(params)}"

        # Prefer the caller's portal-aware opener (Gtk.show_uri on the main
        # thread); fall back to webbrowser for non-GUI callers.
        if open_url is not None:
            open_url(url)
        else:
            import webbrowser

            webbrowser.open(url)

        # Run the receiver in a daemon thread and shut it down cleanly once the
        # redirect arrives or the timeout expires. ``serve_forever`` blocks until
        # ``shutdown()``, so the socket is never left half-closed.
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            # Wait (bounded) for the redirect to arrive.
            for _ in range(600):  # up to ~120s
                if server.auth_code or server.auth_error:
                    break
                time.sleep(0.2)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        if server.auth_error or not server.auth_code:
            raise AuthError(server.auth_error or "no authorization code received")
        if server.auth_state != state:
            raise AuthError("state mismatch — sign-in aborted (possible CSRF)")

        token = self._exchange_code(server.auth_code, verifier, redirect_uri)
        self._token = token
        self._store()
        return token

    def _exchange_code(self, code: str, verifier: str, redirect_uri: str) -> dict:
        data = {
            "client_id": self._client_id,
            "code": code,
            "code_verifier": verifier,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        }
        if self._client_secret:
            data["client_secret"] = self._client_secret
        resp = self._post_token(data)
        if not resp.get("access_token"):
            raise AuthError(resp.get("error_description")
                            or resp.get("error") or "no access token returned")
        resp["expiry"] = time.time() + resp.get("expires_in", 3600)
        return resp

    # -- silent refresh ---------------------------------------------------
    def acquire_token(self, scopes: Sequence[str] = None) -> str | None:
        if not self._token:
            return None
        if self._token.get("expiry", 0) > time.time() + 60:
            return self._token.get("access_token")
        refresh = self._token.get("refresh_token")
        if not refresh:
            return None
        data = {
            "client_id": self._client_id,
            "refresh_token": refresh,
            "grant_type": "refresh_token",
        }
        if self._client_secret:
            data["client_secret"] = self._client_secret
        resp = self._post_token(data)
        token = resp.get("access_token")
        if not token:
            # Refresh failed (revoked/expired refresh token, network error).
            # Return None so callers surface "re-sign in" rather than crash.
            return None
        self._token["access_token"] = token
        self._token["expiry"] = time.time() + resp.get("expires_in", 3600)
        # Google may rotate the refresh token; dropping the new one meant the
        # stored token eventually died and forced a full re-sign-in.
        if resp.get("refresh_token"):
            self._token["refresh_token"] = resp["refresh_token"]
        self._store()
        return token

    def sign_out(self) -> None:
        self._token = {}
        self._secrets.clear(self._account_id, _TOKEN_KIND)

    # -- helpers ----------------------------------------------------------
    @staticmethod
    def _post_token(data: dict) -> dict:
        body = urllib.parse.urlencode(data).encode()
        req = urllib.request.Request(TOKEN_URI, data=body, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            # Google returns a JSON error body (e.g. invalid_grant) on 4xx.
            try:
                return json.loads(exc.read().decode())
            except (ValueError, OSError):
                raise AuthError(f"token request failed: HTTP {exc.code}") from exc
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise AuthError(f"token request failed: {exc}") from exc

    @staticmethod
    def fetch_email(access_token: str) -> str | None:
        req = urllib.request.Request(
            USERINFO, headers={"Authorization": f"Bearer {access_token}"}
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read().decode()).get("email")
        except (urllib.error.URLError, OSError, ValueError):
            # Best-effort: the email is only used to label the account.
            return None
