# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Fiber Elements
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
from pathlib import Path
from typing import Callable, Optional

from gi.repository import Gio, GLib

BUS_NAME = "com.fiberelements.Cloudy"
OBJECT_PATH = "/com/fiberelements/Cloudy/Sync"
INTERFACE = "com.fiberelements.Cloudy.Sync"

INTROSPECTION_XML = """
<node>
  <interface name="com.fiberelements.Cloudy.Sync">
    <method name="StatusForPath">
      <arg type="s" name="path" direction="in"/>
      <arg type="s" name="status" direction="out"/>
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
    from it plus ``os.path.ismount``. ``share_link_fn(path, editable) -> str`` is
    optional and used for CreateShareLink.
    """

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
        try:
            p = Path(path).resolve()
            root = self._mount_root.resolve()
        except OSError:
            return "ignored"
        if root not in p.parents and p != root:
            return "ignored"
        # The managed mount is the immediate child of mount_root.
        rel = p.relative_to(root)
        mountpoint = root / rel.parts[0] if rel.parts else root
        return "synced" if os.path.ismount(mountpoint) else "offline"

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
        elif method == "SyncPath":
            (path,) = params.unpack()
            # TODO(stage 5): trigger sync/hydration of this path.
            self.emit_status_changed(path, self.status_for(path))
            invocation.return_value(None)
        elif method == "FreeUpSpace":
            (path,) = params.unpack()
            # TODO(stage 5): dehydrate (drop local cache) for this path.
            self.emit_status_changed(path, self.status_for(path))
            invocation.return_value(None)
        elif method == "CreateShareLink":
            path, editable = params.unpack()
            url = ""
            if self._share_link_fn is not None:
                try:
                    url = self._share_link_fn(path, editable) or ""
                except Exception:  # noqa: BLE001 - report empty rather than crash the bus
                    url = ""
            invocation.return_value(GLib.Variant("(s)", (url,)))
        else:
            invocation.return_error_literal(
                Gio.dbus_error_quark(),
                Gio.DBusError.UNKNOWN_METHOD,
                f"Unknown method {method}",
            )
