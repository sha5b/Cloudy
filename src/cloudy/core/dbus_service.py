# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Shahab Nedaei
"""D-Bus status service consumed by the host Nautilus extension.

The app publishes per-path sync status (and accepts commands like
"sync this folder" / "free up space" / "copy share link") on the session bus,
under the application's own bus name. The host Nautilus extension
(cloudy_nautilus.py) calls this to draw emblems and menu items.

Status values returned by StatusForPath:
  * "synced"   — path is inside an active Cloudy mount
  * "offline"  — path is a managed mount that is not currently mounted
  * "ignored"  — path is not managed by Cloudy (no emblem / no menu)
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Callable, Optional

from gi.repository import Gio, GLib

BUS_NAME = "io.github.sha5b.Cloudy"
OBJECT_PATH = "/io/github/sha5b/Cloudy/Sync"
INTERFACE = "io.github.sha5b.Cloudy.Sync"

INTROSPECTION_XML = """
<node>
  <interface name="io.github.sha5b.Cloudy.Sync">
    <method name="StatusForPath">
      <arg type="s" name="path" direction="in"/>
      <arg type="s" name="status" direction="out"/>
    </method>
    <method name="ManagedRoots">
      <arg type="as" name="roots" direction="out"/>
    </method>
    <method name="SyncPath">
      <arg type="s" name="path" direction="in"/>
    </method>
    <method name="FreeUpSpace">
      <arg type="s" name="path" direction="in"/>
    </method>
    <method name="CreateShareLink">
      <arg type="s" name="path" direction="in"/>
      <arg type="b" name="editable" direction="in"/>
      <arg type="s" name="url" direction="out"/>
    </method>
    <signal name="StatusChanged">
      <arg type="s" name="path"/>
      <arg type="s" name="status"/>
    </signal>
  </interface>
</node>
"""


class SyncStatusService:
    """Registers the Sync object on an existing D-Bus connection.

    ``mount_root`` is the directory holding Cloudy mounts; status is derived
    from it plus the kernel mount table. ``share_link_fn(path, editable) -> str``
    is optional and used for CreateShareLink.
    """

    _ACTIVE_MOUNTS_TTL_S = 5.0

    def __init__(
        self,
        connection: Gio.DBusConnection,
        mount_root: Path,
        share_link_fn: Optional[Callable[[str, bool], str]] = None,
    ):
        self._connection = connection
        self._mount_root = Path(mount_root)
        self._share_link_fn = share_link_fn
        self._reg_id = 0
        self._active_mounts_cache: tuple[set[str], float] | None = None

    def publish(self) -> None:
        node = Gio.DBusNodeInfo.new_for_xml(INTROSPECTION_XML)
        self._reg_id = self._connection.register_object(
            OBJECT_PATH, node.interfaces[0], self._on_method_call, None, None
        )

    def unpublish(self) -> None:
        if self._reg_id:
            self._connection.unregister_object(self._reg_id)
            self._reg_id = 0

    # -- status logic -----------------------------------------------------
    def status_for(self, path: str) -> str:
        # Pure string/prefix check — never resolve() or stat the path. A hung
        # FUSE mount can block os.path.realpath for an unbounded time, and this
        # runs on the app's main loop.
        try:
            p = os.path.abspath(os.path.normpath(path))
            root = os.path.abspath(os.path.normpath(str(self._mount_root)))
        except OSError:
            return "ignored"
        if not (p == root or p.startswith(root + os.sep)):
            return "ignored"
        # A managed mount sits at root/<drive> or root/<account>/<drive> (drives
        # are namespaced per account). Walk up to root; if any ancestor is an
        # active mountpoint, the path lives on a mounted Cloudy drive.
        #
        # We read the kernel mount table (cached briefly) rather than
        # os.path.ismount(): ismount *stats* the path, which BLOCKS indefinitely
        # on a hung/slow FUSE network mount — and this runs on the app's main
        # loop for every Nautilus emblem/menu query, so a stalled mount would
        # freeze both the app and the file manager. The mount table never touches
        # the filesystem, so it can't hang.
        active = self._active_mounts()
        node = p
        while True:
            if node in active:
                return "synced"
            if node == root:
                return "offline"
            parent = os.path.dirname(node)
            if parent == node:
                return "offline"
            node = parent

    def _active_mounts(self) -> set[str]:
        """Cached, stall-proof view of the kernel mount table."""
        now = time.monotonic()
        cache = self._active_mounts_cache
        if cache is not None and (now - cache[1]) < self._ACTIVE_MOUNTS_TTL_S:
            return cache[0]
        from ..modules.microsoft365.mounts import MountManager

        active = MountManager.active_mounts()
        self._active_mounts_cache = (active, now)
        return active

    def _managed_roots(self) -> list:
        """The directories Cloudy manages (mount root + sync root). The host
        Nautilus extension fetches these once and prefilters paths locally, so it
        never has to make a D-Bus call per file/selection just to learn whether a
        path is even ours."""
        roots = [str(self._mount_root)]
        try:
            from ..modules.microsoft365.mounts import sync_root

            roots.append(str(sync_root()))
        except Exception:  # noqa: BLE001 - sync root is best-effort
            pass
        return roots

    def emit_status_changed(self, path: str, status: str) -> None:
        if not self._reg_id:
            return
        self._connection.emit_signal(
            None, OBJECT_PATH, INTERFACE, "StatusChanged",
            GLib.Variant("(ss)", (path, status)),
        )

    # -- method dispatch --------------------------------------------------
    def _on_method_call(
        self, _conn, _sender, _path, _iface, method, params, invocation
    ) -> None:
        if method == "StatusForPath":
            (path,) = params.unpack()
            invocation.return_value(GLib.Variant("(s)", (self.status_for(path),)))
        elif method == "ManagedRoots":
            invocation.return_value(
                GLib.Variant("(as)", (self._managed_roots(),)))
        elif method == "SyncPath":
            # TODO(stage 5): trigger sync/hydration of this path.
            invocation.return_value(None)
        elif method == "FreeUpSpace":
            # TODO(stage 5): dehydrate (drop local cache) for this path.
            invocation.return_value(None)
        elif method == "CreateShareLink":
            path, editable = params.unpack()
            if self._share_link_fn is None:
                invocation.return_value(GLib.Variant("(s)", ("",)))
                return
            # Creating a share link is two Graph round-trips (resolve item +
            # createLink). Run it on a worker thread and reply asynchronously so
            # the app's main loop never blocks on the network; the reply is
            # marshalled back via idle_add.
            import threading

            fn = self._share_link_fn

            def work():
                try:
                    url = fn(path, editable) or ""
                except Exception:  # noqa: BLE001 - report empty rather than crash the bus
                    url = ""
                GLib.idle_add(
                    lambda: invocation.return_value(GLib.Variant("(s)", (url,)))
                    and False)

            threading.Thread(target=work, daemon=True).start()
        else:
            invocation.return_error_literal(
                Gio.dbus_error_quark(),
                Gio.DBusError.UNKNOWN_METHOD,
                f"Unknown method {method}",
            )
