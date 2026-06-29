# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Shahab Nedaei
"""Microsoft 365 provider module.

A single Microsoft 365 account authenticates once (Microsoft Graph, OAuth2) and
exposes three capabilities from that one login:

  * Files    — OneDrive, SharePoint and Teams document libraries
  * Mail     — Exchange Online mail (Graph; EWS is retired 2027)
  * Calendar — Exchange Online calendars / free-busy

OneDrive is therefore *not* a separate account or module — it is the Files
capability of this provider. The shared :class:`GraphAuth` token is reused by
every capability; the file backends (abraunegg/onedriver) are orchestrated by
the Files implementation. See docs/ARCHITECTURE.md and docs/MODULES.md.

Stage 0: implements the interface with stubbed behavior so the shell can list
and toggle it. Real auth + Graph calls land in stages 2–6.
"""

from __future__ import annotations

from gettext import gettext as _

from ...core.interfaces import (
    CalendarCapability,
    ChatCapability,
    FilesCapability,
    MailCapability,
    ModuleContext,
    ModuleStatus,
    ServiceModule,
    StatusKind,
    TeamsCapability,
)
from .files import OneDriveFiles
from .graph import GraphClient


class Microsoft365Module(
    ServiceModule, FilesCapability, MailCapability, CalendarCapability,
    ChatCapability, TeamsCapability,
):
    id = "microsoft365"
    name = _("Microsoft 365")
    icon_name = "folder-remote-symbolic"
    provider = "microsoft"

    def __init__(self):
        self._ctx: ModuleContext | None = None
        self._auth = None  # core.auth.msal_graph.GraphAuth, set on activate
        self._graph = GraphClient(self._token_provider)
        self._files = OneDriveFiles(self._graph)

    # -- shared auth ------------------------------------------------------
    def _token_provider(self, scopes):
        """Return a Graph access token for the given scopes (stage 2)."""
        if self._auth is None:
            raise RuntimeError("Microsoft 365 account is not signed in")
        return self._auth.acquire_token_silent(scopes)

    # -- ServiceModule ----------------------------------------------------
    def activate(self, ctx: ModuleContext) -> None:
        self._ctx = ctx
        # TODO(stage 2): build GraphAuth from the configured account + secrets,
        # then (stage 3) ensure the host onedrive units exist and start syncing.

    def deactivate(self) -> None:
        self._ctx = None

    def status(self) -> ModuleStatus:
        if self._ctx is None:
            return ModuleStatus(StatusKind.UNCONFIGURED)
        return ModuleStatus(StatusKind.IDLE, detail=_("Not yet implemented"))

    # -- FilesCapability (OneDrive / SharePoint) --------------------------
    def list_drives(self) -> list:
        return self._files.list_drives()

    def create_share_link(self, path: str, *, editable: bool = False) -> str:
        return self._files.create_share_link(path, editable=editable)

    # -- MailCapability ---------------------------------------------------
    def list_folders(self) -> list:
        return self._graph.list_mail_folders()

    def list_messages(self, folder_id: str, *, limit: int = 50) -> list:
        return self._graph.list_messages(folder_id, limit=limit)

    # -- CalendarCapability -----------------------------------------------
    def list_calendars(self) -> list:
        return self._graph.list_calendars()

    def list_events(self, calendar_id: str, start, end) -> list:
        # calendarView spans the selected calendar; start/end are ISO-8601 UTC.
        return self._graph.list_events(start, end, calendar_id=calendar_id)

    # -- ChatCapability (Teams chats) -------------------------------------
    def list_chats(self) -> list:
        return self._graph.list_chats()

    def list_chat_messages(self, chat_id: str, *, limit: int = 30) -> list:
        return self._graph.list_chat_messages(chat_id, limit=limit)

    def send_chat_message(self, chat_id: str, text: str):
        return self._graph.send_chat_message(chat_id, text)

    # -- TeamsCapability (Teams channels + OneNote) -----------------------
    def list_teams(self) -> list:
        return self._graph.list_joined_teams()

    def list_team_channels(self, team_id: str) -> list:
        return self._graph.list_team_channels(team_id)

    def list_channel_messages(self, team_id: str, channel_id: str, *,
                              limit: int = 20) -> list:
        return self._graph.list_channel_messages(team_id, channel_id, limit=limit)
