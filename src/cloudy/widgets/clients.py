# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Shahab Nedaei
"""Provider-agnostic client factory for the Mail/Calendar views.

Returns a client exposing ``list_messages()`` and ``list_events(start, end)``
with normalized dict shapes, regardless of provider (Microsoft Graph or Google).
Clients are cached per account on the application object and reused across
views/requests to avoid rebuilding MSAL/Google auth state every time.
"""

from __future__ import annotations


def _build_client(app, account):
    if account.provider == "google":
        from ..core.auth.google_oauth import GoogleAuth
        from ..modules.gmail.google_client import GoogleClient

        auth = GoogleAuth(
            app.google_client_id(),
            app.secrets,
            account.id,
            client_secret=app.google_client_secret(),
        )
        return GoogleClient(lambda scopes: auth.acquire_token(scopes))

    # Default: Microsoft 365 / Graph.
    from .graph_helper import build_graph_client

    return build_graph_client(app, account)


def build_account_client(app, account):
    """Return the cached client for ``account`` or build and cache it."""
    cached = app.get_account_client(account)
    if cached is not None:
        return cached
    client = _build_client(app, account)
    app.set_account_client(account, client)
    return client
