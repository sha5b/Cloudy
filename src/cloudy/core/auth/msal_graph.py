# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Shahab Nedaei
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
import urllib.error
import urllib.request
from typing import Sequence

import msal
import requests

AUTHORITY = "https://login.microsoftonline.com/common"
GRAPH_ME = "https://graph.microsoft.com/v1.0/me"

# MSAL's default requests.Session has NO timeout, so a silent token refresh can
# hang indefinitely on a stalled connection (and freeze whatever view triggered
# it). Inject a default timeout on every request unless the caller set one.
_HTTP_TIMEOUT = 30


class _TimeoutSession(requests.Session):
    def request(self, *args, **kwargs):
        kwargs.setdefault("timeout", _HTTP_TIMEOUT)
        return super().request(*args, **kwargs)

# Delegated scopes. Request only the subset a given feature needs (incremental
# consent). offline_access/openid/profile are added by MSAL automatically.
SCOPES_BASE = ["User.Read"]
SCOPES_FILES = ["Files.ReadWrite.All", "Sites.ReadWrite.All"]
# Mail.Send is a distinct grant from Mail.ReadWrite (read/modify/delete does NOT
# allow sending); Calendars.ReadWrite already covers create/delete of events.
SCOPES_MAIL = [
    "Mail.ReadWrite", "Mail.Send", "Calendars.ReadWrite", "Contacts.ReadWrite",
]
# Used ONLY for shared/other mailbox (/users/{address}) access. Kept separate so
# normal /me mail keeps working on tokens that predate this scope (no forced
# re-consent for everyday mail; re-sign-in only unlocks shared mailboxes). Adds
# send-as-shared and delegated calendar write for the shared sources.
SCOPES_MAIL_SHARED = [
    "Mail.ReadWrite.Shared", "Mail.Send.Shared", "Calendars.ReadWrite.Shared",
]
# Relevance-ranked people (org colleagues + frequent contacts) for the To-field
# autocomplete — the personal /me/contacts folder is empty for most org users.
SCOPES_PEOPLE = ["People.Read"]
# Enumerate the Teams the user belongs to (each Team's files = a doc library).
SCOPES_TEAMS = ["Team.ReadBasic.All"]
# M365 group mailboxes (conversations) and group/team calendars. ReadWrite —
# not Read — because replying to a group conversation thread
# (POST /groups/{id}/threads/{tid}/reply) requires Group.ReadWrite.All; with
# the read-only scope every group reply 403'd. Usually requires tenant-admin
# consent. Adding this forces existing accounts to Sign Out → Sign In once.
SCOPES_GROUPS = ["Group.ReadWrite.All"]
# Read + send the signed-in user's Teams chats (1:1 and group). Delegated and
# unmetered (per-message billing only applies to *application* permissions).
# Work/school accounts only — consumer Microsoft accounts have no Graph chats.
SCOPES_CHAT = ["Chat.ReadWrite"]
# Read presence (availability/activity) of the people you chat with, to show the
# Teams-style green/away/busy/DND dots. Delegated work/school only; the only
# delegated option is the read-all scope (per-user read isn't offered). Adding
# this forces existing accounts to Sign Out → Sign In once.
SCOPES_PRESENCE = ["Presence.Read.All"]
# List a Team's channels and read/post their messages (the Teams tab's
# Conversation view). Channel reads require tenant-admin consent (like
# Group.Read.All); unmetered for delegated access. Work/school accounts only.
SCOPES_CHANNELS = [
    "Channel.ReadBasic.All", "ChannelMessage.Read.All", "ChannelMessage.Send",
]
# Read/write the Team (group) OneNote notebooks behind a channel's Notes tab.
# Notes.ReadWrite.All (not the non-".All" form) is required to reach *group*
# notebooks; Notes.Create allows new pages. No admin consent needed.
SCOPES_NOTES = ["Notes.ReadWrite.All", "Notes.Create"]

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
            client_id, authority=AUTHORITY, token_cache=self._cache,
            http_client=_TimeoutSession(),
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
        try:
            result = self._app.acquire_token_silent(list(scopes), account=accounts[0])
        except (requests.RequestException, OSError):
            # Network error mid-refresh: behave like "no token", callers retry.
            return None
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
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode())
        except (urllib.error.URLError, OSError, ValueError):
            # Best-effort: the UPN is only used to label the account.
            return None
        return data.get("userPrincipalName") or data.get("mail")
