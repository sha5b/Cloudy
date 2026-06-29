# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Shahab Nedaei
"""A tiny cache for API results (stale-while-revalidate), optionally persisted.

Lives on the Application so it survives view rebuilds: switching accounts or
tabs redisplays instantly from cache, then refreshes in the background. Keyed by
strings like "<account_id>:messages:inbox".

When given a ``path``, JSON-serializable entries (mail/events/chat dicts) are
also written to disk, so a fresh launch can show your last-known mail and agenda
**offline**, then revalidate when the network returns. Entries loaded from disk
are deliberately marked stale so a revalidation always fires.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path


class MemoryCache:
    # Cap in-memory entries so long-running background sessions don't grow
    # without bound. The disk snapshot is also trimmed to this size.
    DEFAULT_MAX_ENTRIES = 2000

    def __init__(self, ttl: float = 90.0, path: str | Path | None = None,
                 *, max_entries: int = DEFAULT_MAX_ENTRIES):
        self._ttl = ttl
        self._max_entries = max(1, max_entries)
        self._store: dict[str, tuple[float, object]] = {}
        # Reads/writes come from both the GTK main loop and the off-thread
        # workers (run_async); a lock keeps the dict from being mutated mid-
        # iteration ("dictionary changed size during iteration").
        self._lock = threading.Lock()
        self._path = Path(path) if path else None
        self._dirty = False
        self._last_flush = 0.0
        if self._path is not None:
            self._load()

    def get(self, key: str):
        """Return (value, is_fresh) if cached, else None."""
        with self._lock:
            entry = self._store.get(key)
        if entry is None:
            return None
        ts, value = entry
        return value, (time.monotonic() - ts) < self._ttl

    def set(self, key: str, value) -> None:
        with self._lock:
            self._store[key] = (time.monotonic(), value)
            self._trim_locked()
        if self._path is not None:
            self._maybe_flush()

    def invalidate(self, prefix: str | None = None) -> None:
        with self._lock:
            if prefix is None:
                self._store.clear()
            else:
                self._store = {k: v for k, v in self._store.items()
                               if not k.startswith(prefix)}
            self._dirty = True
        if self._path is not None:
            self._maybe_flush()

    # -- disk persistence -------------------------------------------------
    def _load(self) -> None:
        """Seed the cache from disk; loaded entries are backdated so they read
        as stale (shown instantly, then revalidated)."""
        try:
            raw = json.loads(self._path.read_text())
        except Exception:  # noqa: BLE001 - missing/corrupt cache is fine
            return
        if not isinstance(raw, dict):
            return
        stale_ts = time.monotonic() - self._ttl - 1
        with self._lock:
            for key, value in raw.items():
                self._store[key] = (stale_ts, value)

    def _maybe_flush(self) -> None:
        with self._lock:
            self._dirty = True
        # Throttle disk writes; a steady-state session flushes at most ~every 5s.
        if time.time() - self._last_flush >= 5.0:
            self.flush()

    def _trim_locked(self) -> None:
        """Drop oldest entries when the store exceeds its cap."""
        if len(self._store) <= self._max_entries:
            return
        # Sort by timestamp (oldest first) and keep the newest max_entries.
        sorted_items = sorted(self._store.items(), key=lambda kv: kv[1][0])
        cutoff = len(sorted_items) - self._max_entries
        for key, _ in sorted_items[:cutoff]:
            del self._store[key]

    def flush(self) -> None:
        """Write JSON-serializable entries to disk atomically (best-effort).
        Skips values that don't serialize (e.g. lists holding Drive objects)."""
        with self._lock:
            dirty = self._dirty
            items = list(self._store.items())
        if self._path is None or not dirty:
            return
        snapshot: dict[str, object] = {}
        for key, (_ts, value) in items:
            try:
                json.dumps(value)
            except (TypeError, ValueError):
                continue  # not serializable — skip (kept in memory only)
            snapshot[key] = value
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps(snapshot))
            tmp.replace(self._path)
            with self._lock:
                self._last_flush = time.time()
                self._dirty = False
        except Exception:  # noqa: BLE001 - a cache that can't persist still works
            pass
