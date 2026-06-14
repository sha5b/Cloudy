# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Fiber Elements
"""Microsoft Graph authentication via MSAL.

Public-client (no secret) flow. ``sign_in_interactive`` opens the user's system
browser and runs a loopback redirect — MSAL handles the local HTTP server and
PKCE for us. The MSAL ``SerializableTokenCache`` is persisted into libsecret so
sign-in survives restarts; ``acquire_token_silent`` refreshes transparently.

Must be called off the GTK main thread (the interactive call blocks until the
browser flow completes). See docs/AUTH.md for the one-time app registration.
"""

from __future__ import annotations

import json
import urllib.request
from typing import Sequence

import msal

AUTHORITY = "https://login.microsoftonline.com/common"
GRAPH_ME = "https://graph.microsoft.com/v1.0/me"

# Delegated scopes. Request only the subset a given feature needs (incremental
# consent). offline_access/openid/profile are added by MSAL automatically.
SCOPES_BASE = ["User.Read"]
SCOPES_FILES = ["Files.ReadWrite.All", "Sites.ReadWrite.All"]
SCOPES_MAIL = ["Mail.ReadWrite", "Calendars.ReadWrite", "Contacts.ReadWrite"]

_CACHE_KIND = "msal-cache"


class AuthError(Exception):
    """Raised when a token could not be acquired."""


class GraphAuth:
    def __init__(self, client_id: str, secrets, account_id: str):
        if not client_id:
            raise AuthError(
                "No Microsoft client ID configured. See docs/AUTH.md "
                "(set CLOUDY_MS_CLIENT_ID or the microsoft-client-id setting)."
            )
        self._client_id = client_id
        self._secrets = secrets
        self._account_id = account_id

        self._cache = msal.SerializableTokenCache()
        blob = secrets.lookup(account_id, _CACHE_KIND)
        if blob:
            self._cache.deserialize(blob)

        self._app = msal.PublicClientApplication(
            client_id, authority=AUTHORITY, token_cache=self._cache
        )

    def _persist(self) -> None:
        if self._cache.has_state_changed:
            self._secrets.store(self._account_id, _CACHE_KIND, self._cache.serialize())

    # -- interactive (system browser + loopback) --------------------------
    def sign_in_interactive(self, scopes: Sequence[str] = SCOPES_BASE) -> dict:
        result = self._app.acquire_token_interactive(list(scopes))
        self._persist()
        if "access_token" not in result:
            raise AuthError(
                result.get("error_description", result.get("error", "sign-in failed"))
            )
        return result

    # -- silent refresh ---------------------------------------------------
    def acquire_token_silent(self, scopes: Sequence[str]) -> str | None:
        accounts = self._app.get_accounts()
        if not accounts:
            return None
        result = self._app.acquire_token_silent(list(scopes), account=accounts[0])
        self._persist()
        return result.get("access_token") if result else None

    def sign_out(self) -> None:
        for account in self._app.get_accounts():
            self._app.remove_account(account)
        self._secrets.clear(self._account_id, _CACHE_KIND)

    # -- helpers ----------------------------------------------------------
    @staticmethod
    def fetch_userprincipalname(access_token: str) -> str | None:
        """Return the signed-in user's UPN/email via Graph /me."""
        req = urllib.request.Request(
            GRAPH_ME, headers={"Authorization": f"Bearer {access_token}"}
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
        return data.get("userPrincipalName") or data.get("mail")
