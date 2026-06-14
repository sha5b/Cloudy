# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Fiber Elements
"""Provider-agnostic client factory for the Mail/Calendar views.

Returns a client exposing ``list_messages()`` and ``list_events(start, end)``
with normalized dict shapes, regardless of provider (Microsoft Graph or Google).
"""

from __future__ import annotations


def build_account_client(app, account):
    if account.provider == "google":
        from ..core.auth.google_oauth import GoogleAuth
        from ..modules.gmail.google_client import GoogleClient

        auth = GoogleAuth(app.google_client_id(), app.secrets, account.id)
        return GoogleClient(lambda scopes: auth.acquire_token(scopes))

    # Default: Microsoft 365 / Graph.
    from .graph_helper import build_graph_client

    return build_graph_client(app, account)
