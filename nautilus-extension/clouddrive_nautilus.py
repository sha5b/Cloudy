# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Fiber Elements
"""Clouddrive Nautilus extension (host-side, nautilus-python API 4.0 / GTK4).

Runs in the HOST Nautilus process (not the Flatpak sandbox). It talks to the
Clouddrive app over D-Bus (com.fiberelements.Clouddrive, see
clouddrive.core.dbus_service) to:
  * draw per-file sync-status emblems (InfoProvider), and
  * add right-click controls (MenuProvider): Sync this folder / Free up space /
    Copy share link.

Install to ~/.local/share/nautilus-python/extensions/ and run `nautilus -q`.
Requires the python3-nautilus (4.x) bindings.

API 4.0 notes: MenuProvider.get_file_items(files) takes no window argument;
PropertyPageProvider was replaced by PropertiesModelProvider.

All D-Bus calls are best-effort: if the app is not running they fail quietly,
so the extension never breaks the file manager.
"""

import gi

gi.require_version("Nautilus", "4.0")
from gi.repository import Gio, GLib, GObject, Nautilus  # noqa: E402

BUS_NAME = "com.fiberelements.Clouddrive"
OBJECT_PATH = "/com/fiberelements/Clouddrive/Sync"
INTERFACE = "com.fiberelements.Clouddrive.Sync"

# Map service status -> Nautilus emblem name.
_EMBLEMS = {
    "synced": "emblem-default",
    "offline": "emblem-synchronizing",
}

_DBUS_TIMEOUT_MS = 400


def _proxy():
    """Return a cached D-Bus proxy to the Clouddrive sync service, or None."""
    if not hasattr(_proxy, "_p"):
        try:
            _proxy._p = Gio.DBusProxy.new_for_bus_sync(
                Gio.BusType.SESSION,
                Gio.DBusProxyFlags.DO_NOT_AUTO_START,
                None, BUS_NAME, OBJECT_PATH, INTERFACE, None,
            )
        except GLib.Error:
            _proxy._p = None
    return _proxy._p


def _call(method, variant, reply_type):
    proxy = _proxy()
    if proxy is None:
        return None
    try:
        return proxy.call_sync(
            method, variant, Gio.DBusCallFlags.NONE, _DBUS_TIMEOUT_MS, None
        )
    except GLib.Error:
        return None


def _path_of(file):
    location = file.get_location()
    return location.get_path() if location else None


class ClouddriveInfoProvider(GObject.GObject, Nautilus.InfoProvider):
    """Per-file sync-status emblems."""

    def update_file_info(self, file):
        path = _path_of(file)
        if not path:
            return Nautilus.OperationResult.COMPLETE
        result = _call("StatusForPath", GLib.Variant("(s)", (path,)), "(s)")
        if result is not None:
            (status,) = result.unpack()
            emblem = _EMBLEMS.get(status)
            if emblem:
                file.add_emblem(emblem)
        return Nautilus.OperationResult.COMPLETE


class ClouddriveMenuProvider(GObject.GObject, Nautilus.MenuProvider):
    """Right-click controls for Clouddrive-managed files/folders."""

    def get_file_items(self, files):  # API 4.0: no window arg
        if not files:
            return []
        # Only offer controls for managed paths.
        managed = [f for f in files if self._is_managed(_path_of(f))]
        if not managed:
            return []

        copy_link = Nautilus.MenuItem(
            name="Clouddrive::copy_share_link",
            label="Copy OneDrive Share Link",
            tip="Create and copy a sharing link via Clouddrive",
        )
        copy_link.connect("activate", self._on_copy_link, managed)

        free_space = Nautilus.MenuItem(
            name="Clouddrive::free_up_space",
            label="Free Up Space",
            tip="Remove the local copy; keep the file online",
        )
        free_space.connect("activate", self._on_free_space, managed)
        return [copy_link, free_space]

    def get_background_items(self, folder):
        path = _path_of(folder)
        if not self._is_managed(path):
            return []
        sync = Nautilus.MenuItem(
            name="Clouddrive::sync_folder",
            label="Sync This Folder with Clouddrive",
            tip="Mark this folder for synchronization",
        )
        sync.connect("activate", self._on_sync_folder, folder)
        return [sync]

    # -- helpers ----------------------------------------------------------
    @staticmethod
    def _is_managed(path):
        if not path:
            return False
        result = _call("StatusForPath", GLib.Variant("(s)", (path,)), "(s)")
        if result is None:
            return False
        (status,) = result.unpack()
        return status != "ignored"

    def _on_copy_link(self, _menu, files):
        path = _path_of(files[0])
        result = _call(
            "CreateShareLink", GLib.Variant("(sb)", (path, False)), "(s)"
        )
        if result is None:
            return
        (url,) = result.unpack()
        if url:
            Gio.Application.get_default()  # no-op; clipboard set via display
            display = self._clipboard()
            if display is not None:
                display.set(url)

    def _on_free_space(self, _menu, files):
        for f in files:
            _call("FreeUpSpace", GLib.Variant("(s)", (_path_of(f),)), None)

    def _on_sync_folder(self, _menu, folder):
        _call("SyncPath", GLib.Variant("(s)", (_path_of(folder),)), None)

    @staticmethod
    def _clipboard():
        from gi.repository import Gdk

        display = Gdk.Display.get_default()
        return display.get_clipboard() if display else None
