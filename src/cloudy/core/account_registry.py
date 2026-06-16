# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Shahab Nedaei
"""Registry of configured accounts.

Analogous to Alpaca's ``instance_manager``. Holds account metadata (non-secret)
and emits a ``changed`` signal the UI binds to. Secrets live in core.secrets;
this class only stores identifiers and display state, persisted as JSON in the
``accounts`` GSettings key.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field

from gi.repository import GObject


@dataclass
class Account:
    id: str
    display_name: str
    provider: str  # "microsoft" | "google"
    module_id: str  # which module owns it, e.g. "microsoft365"
    signed_in: bool = False  # flipped true once auth completes (stage 2)
    full_sync: bool = False  # keep a two-way (bisync) offline copy on disk
    # Per-account mount folder override (used when mount-layout == 'individual';
    # empty means the global mount location). See preferences → Accounts.
    mount_location: str = ""
    # Shared/other mailbox addresses the user has added (delegated access). The
    # Calendar/Mail views also reach these mailboxes' calendars and folders.
    shared_mailboxes: list = field(default_factory=list)
    # Pinned shared/group sources shown on the Dashboard. Each entry:
    # {"kind": "mail"|"calendar", "source": "shared"|"teams",
    #  "id": <address-or-group-id>, "name": <label>}.
    pinned_sources: list = field(default_factory=list)
    # Muted chats/channels — no notification banner or badge. Each entry:
    # {"kind": "chat"|"channel", "id": <chat-or-channel-id>}.
    muted_sources: list = field(default_factory=list)

    # Consumer mail domains that have no Teams/SharePoint/Chat/Workspace and no
    # shared-mailbox delegation — the business-only surfaces are hidden for them.
    _PERSONAL_DOMAINS = {
        "google": {"gmail.com", "googlemail.com"},
        "microsoft": {"outlook.com", "hotmail.com", "live.com", "msn.com",
                      "passport.com", "outlook.de", "live.de"},
    }

    @property
    def is_personal(self) -> bool:
        """True for a consumer account (vs. a work/school or Workspace one),
        decided from the signed-in email domain. Business/Workspace and unknown
        accounts return False so nothing is hidden for them."""
        domain = (self.display_name or "").strip().lower().rpartition("@")[2]
        return domain in self._PERSONAL_DOMAINS.get(self.provider, set())

    @property
    def is_business(self) -> bool:
        """A signed-in work/school or Workspace account (qualifies for Teams /
        SharePoint / Chat / shared mailboxes)."""
        return self.signed_in and not self.is_personal

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Account":
        return cls(
            id=data["id"],
            display_name=data["display_name"],
            provider=data["provider"],
            module_id=data["module_id"],
            signed_in=data.get("signed_in", False),
            full_sync=data.get("full_sync", False),
            mount_location=data.get("mount_location", ""),
            shared_mailboxes=list(data.get("shared_mailboxes", [])),
            pinned_sources=list(data.get("pinned_sources", [])),
            muted_sources=list(data.get("muted_sources", [])),
        )


class AccountRegistry(GObject.Object):
    __gsignals__ = {
        "changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self, settings):
        super().__init__()
        self._settings = settings
        self._accounts: dict[str, Account] = {}
        self._load()

    # -- persistence ------------------------------------------------------
    def _load(self) -> None:
        raw = self._settings.get_string("accounts")
        try:
            data = json.loads(raw) if raw else []
        except json.JSONDecodeError:
            data = []
        self._accounts = {
            d["id"]: Account.from_dict(d) for d in data if "id" in d
        }

    def _save(self) -> None:
        data = [a.to_dict() for a in self._accounts.values()]
        self._settings.set_string("accounts", json.dumps(data))

    # -- access -----------------------------------------------------------
    def accounts(self) -> list[Account]:
        return list(self._accounts.values())

    def get(self, account_id: str) -> Account | None:
        return self._accounts.get(account_id)

    def is_empty(self) -> bool:
        return not self._accounts

    # -- mutation ---------------------------------------------------------
    def add(self, account: Account) -> None:
        self._accounts[account.id] = account
        self._save()
        self.emit("changed")

    # Persist an in-place mutation of an existing account.
    update = add

    def remove(self, account_id: str) -> None:
        if self._accounts.pop(account_id, None) is not None:
            self._save()
            self.emit("changed")

    def new_id(self, provider: str) -> str:
        """Return a stable-ish unique id for a new account of ``provider``."""
        n = 1
        while f"{provider}-{n}" in self._accounts:
            n += 1
        return f"{provider}-{n}"
