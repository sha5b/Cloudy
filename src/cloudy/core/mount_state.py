# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Shahab Nedaei
"""One shared, cached answer to "which Cloudy drives are mounted and alive?".

Everything that used to ask this question asked it *separately* and *often*: the
Files tab re-read the kernel mount table on every map (once per drive), the
sync-status poll re-read it every 3 s, the D-Bus service kept its own 5 s cache,
the Dashboard shelled out to ``ps``, and the remount watchdog ran one ``ps`` per
remembered drive. In Flatpak every one of those reads is a synchronous
``flatpak-spawn --host`` round-trip. That is the storm behind "the file manager
freezes".

So: read it **once**, keep the snapshot here, and refresh only when it can
actually have changed —

  * at startup,
  * when the kernel mount table changes (``Gio.UnixMountMonitor``),
  * right after Cloudy itself mounts or unmounts something,
  * and on a slow fallback tick, because a Flatpak sandbox does not see the
    host's mount events.

Readers get the snapshot with **zero I/O** and never block. Views listen to the
``changed`` signal instead of polling.

The refresh also does the one bit of hygiene that keeps Nautilus alive: a
*stale* Cloudy mount (still in the mount table, but its rclone daemon died) is
lazily detached. A stale FUSE endpoint makes every ``stat()`` on it hang in the
kernel, uninterruptibly — and since Cloudy puts each mountpoint in the GTK
bookmarks file, the file manager sidebar, every GTK file chooser and Shell
search will stat it. One dead daemon therefore freezes the whole desktop's file
handling until the mountpoint is cleared.
"""

from __future__ import annotations

import os
import threading

from gi.repository import GLib, GObject


def _daemon_serves(mountpoint: str, cmdlines: list[str]) -> bool:
    """True when a live rclone/onedriver *mount* process backs ``mountpoint``.

    Matched against process command lines, never by touching the path itself —
    a stat on a hung mount is exactly what we are trying to avoid."""
    return any(mountpoint in line and "mount" in line
               and ("rclone" in line or "onedriver" in line)
               for line in cmdlines)


class MountState(GObject.Object):
    """Process-wide snapshot of the mount table. Use ``MountState.get()``."""

    __gtype_name__ = "CloudyMountState"
    __gsignals__ = {
        # The snapshot changed (a drive appeared, vanished or went stale).
        "changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    #: Fallback refresh cadence. Only needed because a Flatpak sandbox gets no
    #: mount events for host mounts; outside Flatpak the monitor does the work.
    FALLBACK_INTERVAL_S = 300
    #: Coalesce bursts of mount events into a single refresh.
    _DEBOUNCE_MS = 400

    _instance: "MountState | None" = None

    @classmethod
    def get(cls) -> "MountState":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(self):
        super().__init__()
        self._lock = threading.Lock()
        self._mounted: frozenset[str] = frozenset()
        self._healthy: frozenset[str] = frozenset()
        self._refreshing = False
        self._monitor = None
        self._debounce_id = 0
        self._fallback_id = 0

    # -- reading the snapshot (no I/O, safe on the GTK thread) ------------
    @property
    def mounted(self) -> frozenset[str]:
        """Every path in the kernel mount table as of the last refresh."""
        with self._lock:
            return self._mounted

    @property
    def healthy(self) -> frozenset[str]:
        """Mountpoints served by a live rclone/onedriver daemon — the only ones
        safe to list, scan or open."""
        with self._lock:
            return self._healthy

    def is_mounted(self, path) -> bool:
        return str(path) in self.mounted

    def health(self, path) -> str:
        """``"active"`` (mounted, daemon alive), ``"stale"`` (mounted, daemon
        gone — I/O would hang) or ``"absent"``."""
        p = str(path)
        with self._lock:
            if p not in self._mounted:
                return "absent"
            return "active" if p in self._healthy else "stale"

    # -- refreshing -------------------------------------------------------
    def refresh_blocking(self) -> bool:
        """Re-read the mount table *and* the process table, clear any stale
        Cloudy mount, and store the result. Blocking (two /proc reads, or two
        host subprocesses in Flatpak) — call from a worker thread. Returns True
        when the snapshot changed."""
        mounted, healthy = self._read()
        cleared = self._clear_stale(mounted, healthy)
        if cleared:
            mounted, healthy = self._read()
        return self._store(mounted, healthy)

    def refresh_table_blocking(self) -> bool:
        """Cheap variant: re-read only the kernel mount table (no ``ps``, no
        stale sweep). Used while polling for a just-started mount to appear."""
        from ..modules.microsoft365.mounts import read_mount_table

        mounted = frozenset(read_mount_table())
        with self._lock:
            healthy = self._healthy & mounted
        return self._store(mounted, healthy)

    def refresh_async(self, on_done=None) -> None:
        """Refresh off-thread. ``on_done()`` (if given) runs on the GTK loop
        once the snapshot is current. Concurrent calls collapse into one."""
        with self._lock:
            if self._refreshing and on_done is None:
                return  # one already in flight and nobody is waiting on it
            self._refreshing = True

        def work():
            try:
                self.refresh_blocking()
            finally:
                with self._lock:
                    self._refreshing = False
            if on_done is not None:
                GLib.idle_add(lambda: (on_done(), False)[1])

        threading.Thread(target=work, daemon=True).start()

    # -- change monitoring ------------------------------------------------
    def start_monitor(self) -> None:
        """Begin tracking mount changes. Idempotent."""
        if self._fallback_id:
            return
        from gi.repository import Gio

        # Outside Flatpak this fires the moment a FUSE mount appears or dies,
        # which is what makes the Files tab feel live without any polling.
        try:
            self._monitor = Gio.UnixMountMonitor.get()
            self._monitor.connect("mounts-changed", self._on_mounts_changed)
            self._monitor.connect("mountpoints-changed", self._on_mounts_changed)
        except Exception:  # noqa: BLE001 - monitoring is an optimisation
            self._monitor = None
        self._fallback_id = GLib.timeout_add_seconds(
            self.FALLBACK_INTERVAL_S, self._on_fallback_tick)
        self.refresh_async()

    def _on_mounts_changed(self, *_args) -> None:
        if self._debounce_id:
            GLib.source_remove(self._debounce_id)
        self._debounce_id = GLib.timeout_add(self._DEBOUNCE_MS, self._on_debounced)

    def _on_debounced(self) -> bool:
        self._debounce_id = 0
        self.refresh_async()
        return False

    def _on_fallback_tick(self) -> bool:
        self.refresh_async()
        return True

    # -- internals --------------------------------------------------------
    def _read(self) -> tuple[frozenset[str], frozenset[str]]:
        from ..modules.microsoft365.mounts import (
            read_mount_table, read_process_cmdlines)

        mounted = frozenset(read_mount_table())
        cmdlines = read_process_cmdlines().splitlines()
        healthy = frozenset(mp for mp in mounted if _daemon_serves(mp, cmdlines))
        return mounted, healthy

    def _store(self, mounted, healthy) -> bool:
        with self._lock:
            changed = (mounted != self._mounted or healthy != self._healthy)
            self._mounted, self._healthy = mounted, healthy
        if changed:
            GLib.idle_add(lambda: (self.emit("changed"), False)[1])
        return changed

    @staticmethod
    def _clear_stale(mounted: frozenset[str], healthy: frozenset[str]) -> bool:
        """Lazily detach Cloudy mountpoints whose daemon has died. Restricted to
        paths Cloudy remembers mounting — every other FUSE mount on the system
        (gvfs, portals, someone else's sshfs) is none of our business and would
        look "stale" by this test."""
        from ..modules.microsoft365.mounts import MountManager, load_mount_records

        ours = {os.path.normpath(rec["mountpoint"])
                for rec in load_mount_records() if rec.get("mountpoint")}
        stale = [mp for mp in mounted - healthy if os.path.normpath(mp) in ours]
        if not stale:
            return False
        mgr = MountManager()
        for mp in stale:
            print(f"[mounts] clearing stale mount at {mp} (daemon gone)")
            mgr.lazy_unmount(mp)
        return True
