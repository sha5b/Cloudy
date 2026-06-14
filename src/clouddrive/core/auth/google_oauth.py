# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Fiber Elements
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
import urllib.parse
import urllib.request
from typing import Sequence

AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URI = "https://oauth2.googleapis.com/token"
USERINFO = "https://openidconnect.googleapis.com/v1/userinfo"

SCOPES_BASE = ["openid", "email", "profile"]
SCOPES_MAIL = ["https://www.googleapis.com/auth/gmail.readonly"]
SCOPES_CALENDAR = ["https://www.googleapis.com/auth/calendar.readonly"]

_TOKEN_KIND = "google-token"


class AuthError(Exception):
    pass


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


class _CodeHandler(http.server.BaseHTTPRequestHandler):
    code = None
    error = None

    def do_GET(self):  # noqa: N802 - http.server API
        query = urllib.parse.urlparse(self.path).query
        params = urllib.parse.parse_qs(query)
        _CodeHandler.code = params.get("code", [None])[0]
        _CodeHandler.error = params.get("error", [None])[0]
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(
            b"<html><body><h2>Clouddrive</h2>"
            b"<p>Sign-in complete. You can close this tab.</p></body></html>"
        )

    def log_message(self, *_args):  # silence the default stderr logging
        pass


class GoogleAuth:
    def __init__(self, client_id: str, secrets, account_id: str, client_secret: str = ""):
        if not client_id:
            raise AuthError(
                "No Google client ID configured. See docs/AUTH.md "
                "(set CLOUDDRIVE_GOOGLE_CLIENT_ID or the google-client-id setting)."
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
    def sign_in_interactive(self, scopes: Sequence[str] = None) -> dict:
        scopes = list(scopes or (SCOPES_BASE + SCOPES_MAIL + SCOPES_CALENDAR))
        verifier = _b64url(_secrets.token_bytes(48))
        challenge = _b64url(hashlib.sha256(verifier.encode()).digest())

        _CodeHandler.code = None
        _CodeHandler.error = None
        server = http.server.HTTPServer(("127.0.0.1", 0), _CodeHandler)
        port = server.server_address[1]
        redirect_uri = f"http://localhost:{port}/"

        params = {
            "client_id": self._client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(scopes),
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "access_type": "offline",
            "prompt": "consent",
        }
        url = f"{AUTH_URI}?{urllib.parse.urlencode(params)}"

        # Open the browser; the portal/Gtk launcher is used by the caller's UI,
        # here we fall back to xdg-open via webbrowser for thread-safety.
        import webbrowser

        webbrowser.open(url)

        threading.Thread(target=server.handle_request, daemon=True).start()
        # Wait (bounded) for the redirect to arrive.
        for _ in range(600):  # up to ~120s
            if _CodeHandler.code or _CodeHandler.error:
                break
            time.sleep(0.2)
        server.server_close()

        if _CodeHandler.error or not _CodeHandler.code:
            raise AuthError(_CodeHandler.error or "no authorization code received")

        token = self._exchange_code(_CodeHandler.code, verifier, redirect_uri)
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
        self._token["access_token"] = resp.get("access_token", "")
        self._token["expiry"] = time.time() + resp.get("expires_in", 3600)
        self._store()
        return self._token.get("access_token")

    def sign_out(self) -> None:
        self._token = {}
        self._secrets.clear(self._account_id, _TOKEN_KIND)

    # -- helpers ----------------------------------------------------------
    @staticmethod
    def _post_token(data: dict) -> dict:
        body = urllib.parse.urlencode(data).encode()
        req = urllib.request.Request(TOKEN_URI, data=body, method="POST")
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())

    @staticmethod
    def fetch_email(access_token: str) -> str | None:
        req = urllib.request.Request(
            USERINFO, headers={"Authorization": f"Bearer {access_token}"}
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode()).get("email")
