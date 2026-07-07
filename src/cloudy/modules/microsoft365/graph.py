# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Shahab Nedaei
"""Microsoft Graph REST client shared by all Microsoft 365 capabilities.

A single instance serves Files (drive/site enumeration, share links), Mail, and
Calendar from the same OAuth token, supplied lazily by ``token_provider(scopes)``
so the caller controls auth/refresh (see core.auth.msal_graph).

Files enumeration and share links are implemented here; mail/calendar land in
stage 6.
"""

from __future__ import annotations

from .graph_calendar import GraphCalendarMixin
from .graph_chat import GraphChatMixin
from .graph_files import GraphFilesMixin
from .graph_http import (  # noqa: F401 — re-exported for existing importers
    BASE_URL,
    Drive,
    GraphError,
    GraphHttp,
    _split_id,
    _StripAuthOnRedirect,
)
from .graph_mail import GraphMailMixin
from .graph_teams import GraphTeamsMixin

__all__ = ["BASE_URL", "Drive", "GraphClient", "GraphError"]


class GraphClient(GraphFilesMixin, GraphMailMixin, GraphCalendarMixin,
                  GraphChatMixin, GraphTeamsMixin, GraphHttp):
    """The assembled Microsoft Graph client — one instance per account, its
    behavior split across per-domain mixins over the shared HTTP base."""
