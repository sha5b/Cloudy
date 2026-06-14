# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Fiber Elements
"""Gmail + Google Calendar module. Stage 6. See docs/AUTH.md."""

from gettext import gettext as _

from ...core.interfaces import (
    CalendarCapability,
    FilesCapability,
    MailCapability,
    ModuleContext,
    ModuleStatus,
    ServiceModule,
    StatusKind,
)


class GmailModule(ServiceModule, FilesCapability, MailCapability, CalendarCapability):
    id = "gmail"
    name = _("Gmail")
    icon_name = "mail-unread-symbolic"
    provider = "google"

    def activate(self, ctx: ModuleContext) -> None:
        self._ctx = ctx

    def deactivate(self) -> None:
        self._ctx = None

    def status(self) -> ModuleStatus:
        return ModuleStatus(StatusKind.UNCONFIGURED)

    # FilesCapability — Google Drive is mounted via rclone's "drive" backend;
    # the Files view supplies the entries (My Drive). These remain for the
    # interface contract.
    def list_drives(self) -> list:
        return []

    def create_share_link(self, path: str, *, editable: bool = False) -> str:
        return ""

    def list_folders(self) -> list:
        return []

    def list_messages(self, folder_id: str, *, limit: int = 50) -> list:
        return []

    def list_calendars(self) -> list:
        return []

    def list_events(self, calendar_id: str, start, end) -> list:
        return []


MODULE = GmailModule
