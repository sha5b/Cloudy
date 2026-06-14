# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Fiber Elements
"""A tiny in-memory cache for API results (stale-while-revalidate).

Lives on the Application so it survives view rebuilds: switching accounts or
tabs redisplays instantly from cache, then refreshes in the background. Keyed by
strings like "<account_id>:messages:inbox".
"""

from __future__ import annotations

import time


class MemoryCache:
    def __init__(self, ttl: float = 90.0):
        self._ttl = ttl
        self._store: dict[str, tuple[float, object]] = {}

    def get(self, key: str):
        """Return (value, is_fresh) if cached, else None."""
        entry = self._store.get(key)
        if entry is None:
            return None
        ts, value = entry
        return value, (time.monotonic() - ts) < self._ttl

    def set(self, key: str, value) -> None:
        self._store[key] = (time.monotonic(), value)

    def invalidate(self, prefix: str | None = None) -> None:
        if prefix is None:
            self._store.clear()
        else:
            for k in [k for k in self._store if k.startswith(prefix)]:
                self._store.pop(k, None)
