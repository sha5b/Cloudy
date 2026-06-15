# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Shahab Nedaei
"""Cloudy Nautilus extension (host-side, nautilus-python 4.x / GTK4).

Runs in the HOST Nautilus process (not the Flatpak sandbox). It talks to the
Cloudy app over D-Bus (io.github.sha5b.Cloudy, see cloudy.core.dbus_service)
to add right-click controls (MenuProvider): Copy share link / Free up space /
Sync this folder.

Unmounting is handled by the app itself (Files → Unmount) and by GNOME's native
eject on the mounted drive — nautilus-python can't add items to sidebar entries,
so there's no extension-side unmount.

Install to ~/.local/share/nautilus-python/extensions/ and run `nautilus -q`.
Requires the python3-nautilus (4.x) bindings.

All D-Bus calls are best-effort: if the app is not running they fail quietly,
so the extension never breaks the file manager.
"""

import gi

# Nautilus loads its own typelib before importing us, and the version varies by
# distro (4.0 on some, 4.1 on others). Request whichever is present; if Nautilus
# is already loaded, requiring the non-matching version raises — so we just try
# the candidates and fall through (the import below uses the loaded version).
for _ver in ("4.1", "4.0"):
    try:
        gi.require_version("Nautilus", _ver)
        break
    except ValueError:
        continue
from gi.repository import Gio, GLib, GObject, Nautilus  # noqa: E402

BUS_NAME = "io.github.sha5b.Cloudy"
OBJECT_PATH = "/io/github/sha5b/Cloudy/Sync"
INTERFACE = "io.github.sha5b.Cloudy.Sync"

_DBUS_TIMEOUT_MS = 400


def _proxy():
    """Return a cached D-Bus proxy to the Cloudy sync service, or None."""
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


class CloudyMenuProvider(GObject.GObject, Nautilus.MenuProvider):
    """Right-click controls for Cloudy-managed files/folders."""

    def get_file_items(self, files):  # API 4.0: no window arg
        # Only offer controls for managed paths.
        managed = [f for f in files if self._is_managed(_path_of(f))]
        if not managed:
            return []

        copy_link = Nautilus.MenuItem(
            name="Cloudy::copy_share_link",
            label="Copy OneDrive Share Link",
            tip="Create and copy a sharing link via Cloudy",
        )
        copy_link.connect("activate", self._on_copy_link, managed)

        free_space = Nautilus.MenuItem(
            name="Cloudy::free_up_space",
            label="Free Up Space",
            tip="Remove the local copy; keep the file online",
        )
        free_space.connect("activate", self._on_free_space, managed)
        return [copy_link, free_space]

    def get_background_items(self, folder):
        if not self._is_managed(_path_of(folder)):
            return []
        sync = Nautilus.MenuItem(
            name="Cloudy::sync_folder",
            label="Sync This Folder with Cloudy",
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
