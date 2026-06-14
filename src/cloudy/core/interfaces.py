# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Fiber Elements
"""Stable contracts implemented by service modules.

These are intentionally framework-light (plain ABCs + dataclasses) so the module
boundary stays clear while the plugin engine evolves from a dynamic-import
registry toward libpeas-2. See docs/MODULES.md.
"""

from __future__ import annotations

import enum
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional


class StatusKind(enum.Enum):
    UNCONFIGURED = "unconfigured"
    IDLE = "idle"
    SYNCING = "syncing"
    ERROR = "error"
    OFFLINE = "offline"


@dataclass
class ModuleStatus:
    kind: StatusKind = StatusKind.UNCONFIGURED
    detail: str = ""
    progress: Optional[float] = None  # 0.0–1.0 when syncing, else None


class ServiceModule(ABC):
    """Base contract every module implements.

    Concrete modules also subclass one or more capability mix-ins below.
    """

    #: short stable identifier, e.g. "microsoft365"
    id: str = ""
    #: human-readable, translatable display name
    name: str = ""
    #: symbolic icon name
    icon_name: str = "application-x-addon-symbolic"
    #: auth/secret namespace this provider belongs to, e.g. "microsoft"/"google"
    provider: str = ""

    @abstractmethod
    def activate(self, ctx: "ModuleContext") -> None:
        """Called when the module is enabled. Wire up daemons/clients here."""

    @abstractmethod
    def deactivate(self) -> None:
        """Tear down anything started in :meth:`activate`."""

    def preferences_page(self):  # -> Optional[Adw.PreferencesPage]
        """Return an Adw.PreferencesPage for this module, or None."""
        return None

    def status(self) -> ModuleStatus:
        return ModuleStatus()


@dataclass
class ModuleContext:
    """Handed to a module on activation: access to shared core services."""

    settings: object  # Gio.Settings
    secrets: object  # core.secrets.SecretStore
    registry: object  # core.account_registry.AccountRegistry


# --- Capability mix-ins ------------------------------------------------------
# A module declares what it can surface by also subclassing these. The shell
# checks isinstance() to decide which UI surfaces to offer.


#: Ordered capability keys. The UI maps these to translated labels and icons.
CAPABILITY_KEYS = ("files", "mail", "calendar")


def capabilities_of(obj) -> list[str]:
    """Return the capability keys an object supports, in display order."""
    caps = []
    if isinstance(obj, FilesCapability):
        caps.append("files")
    if isinstance(obj, MailCapability):
        caps.append("mail")
    if isinstance(obj, CalendarCapability):
        caps.append("calendar")
    return caps


class FilesCapability(ABC):
    """File sync / mount control and share links."""

    @abstractmethod
    def list_drives(self) -> list:
        ...

    @abstractmethod
    def create_share_link(self, path: str, *, editable: bool = False) -> str:
        ...


class MailCapability(ABC):
    @abstractmethod
    def list_folders(self) -> list:
        ...

    @abstractmethod
    def list_messages(self, folder_id: str, *, limit: int = 50) -> list:
        ...


class CalendarCapability(ABC):
    @abstractmethod
    def list_calendars(self) -> list:
        ...

    @abstractmethod
    def list_events(self, calendar_id: str, start, end) -> list:
        ...
