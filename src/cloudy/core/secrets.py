# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Shahab Nedaei
"""Secret storage via libsecret (Secret Service / portal inside Flatpak).

OAuth/MSAL token caches are stored here, never in plaintext on disk. Inside the
Flatpak sandbox libsecret's simple API transparently uses the Secret Service
portal with a per-app local keyring — no broad secrets D-Bus hole needed.

Secrets are keyed by ``(account_id, kind)``; e.g. kind "msal-cache" holds the
serialized MSAL token cache for a Microsoft account. See docs/AUTH.md.
"""

from __future__ import annotations

from typing import Optional

import gi

gi.require_version("Secret", "1")
from gi.repository import Secret  # noqa: E402

#: Schema describing the attributes we key secrets on.
_SCHEMA = Secret.Schema.new(
    "io.github.sha5b.Cloudy.Token",
    Secret.SchemaFlags.NONE,
    {
        "account": Secret.SchemaAttributeType.STRING,
        "kind": Secret.SchemaAttributeType.STRING,
    },
)


class SecretStore:
    """Stores/retrieves per-account secrets keyed by (account_id, kind)."""

    @staticmethod
    def _attrs(account_id: str, kind: str) -> dict:
        return {"account": account_id, "kind": kind}

    def store(self, account_id: str, kind: str, value: str) -> None:
        Secret.password_store_sync(
            _SCHEMA,
            self._attrs(account_id, kind),
            Secret.COLLECTION_DEFAULT,
            f"Cloudy {kind} for {account_id}",
            value,
            None,  # cancellable
        )

    def lookup(self, account_id: str, kind: str) -> Optional[str]:
        return Secret.password_lookup_sync(
            _SCHEMA, self._attrs(account_id, kind), None
        )

    def clear(self, account_id: str, kind: str) -> None:
        Secret.password_clear_sync(_SCHEMA, self._attrs(account_id, kind), None)
