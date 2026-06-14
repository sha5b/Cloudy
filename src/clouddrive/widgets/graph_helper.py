# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Fiber Elements
"""Shared helper: build a per-account Microsoft Graph client for the UI.

The capability views (Files/Mail/Calendar) each talk to Graph on behalf of one
signed-in account, refreshing tokens silently from the libsecret-backed MSAL
cache. Raises AuthError if no client ID is configured.
"""

from __future__ import annotations


def build_graph_client(app, account):
    from ..core.auth.msal_graph import GraphAuth
    from ..modules.microsoft365.graph import GraphClient

    auth = GraphAuth(app.microsoft_client_id(), app.secrets, account.id)
    return GraphClient(lambda scopes: auth.acquire_token_silent(scopes))
