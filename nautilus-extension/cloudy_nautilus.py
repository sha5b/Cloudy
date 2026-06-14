# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Shahab Nedaei
"""Cloudy Nautilus extension (host-side, nautilus-python 4.x / GTK4).

Runs in the HOST Nautilus process (not the Flatpak sandbox). It talks to the
Cloudy app over D-Bus (io.github.sha5b.Clouddrive, see cloudy.core.dbus_service)
to add right-click controls (MenuProvider): Sync this folder / Free up space /
Copy share link.

Install to ~/.local/share/nautilus-python/extensions/ and run `nautilus -q`.
Requires the python3-nautilus (4.x) bindings.

All D-Bus calls are best-effort: if the app is not running they fail quietly,
so the extension never breaks the file manager.
"""

import os
import subprocess

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

BUS_NAME = "io.github.sha5b.Clouddrive"
OBJECT_PATH = "/io/github/sha5b/Clouddrive/Sync"
INTERFACE = "io.github.sha5b.Clouddrive.Sync"

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


def _cloudy_mountpoints():
    """Set of currently-mounted Cloudy rclone mountpoints (read from the kernel
    mount table — no dependency on the app running). A Cloudy mount is an
    rclone FUSE mount living under a 'cloudy' path."""
    points = set()
    try:
        with open("/proc/self/mountinfo", encoding="utf-8") as fh:
            for line in fh:
                # "... <mountpoint> ... - <fstype> <source> <opts>"
                left, _sep, right = line.partition(" - ")
                fields = left.split(" ")
                if len(fields) <= 4 or not right:
                    continue
                fstype = right.split(" ")[0]
                mp = (fields[4].replace("\\040", " ").replace("\\011", "\t")
                      .replace("\\012", "\n").replace("\\134", "\\"))
                if fstype.startswith("fuse") and "cloudy" in mp:
                    points.add(mp)
    except OSError:
        pass
    return points


class CloudyMenuProvider(GObject.GObject, Nautilus.MenuProvider):
    """Right-click controls for Cloudy-managed files/folders."""

    def get_file_items(self, files):  # API 4.0: no window arg
        if not files:
            return []
        # A single selected Cloudy mountpoint → offer a quick Unmount (the
        # rclone FUSE equivalent of ejecting an external drive).
        if len(files) == 1:
            path = _path_of(files[0])
            if path and path in _cloudy_mountpoints():
                item = Nautilus.MenuItem(
                    name="Cloudy::unmount",
                    label="Unmount (Cloudy)",
                    tip="Unmount this Cloudy network drive",
                )
                item.connect("activate", self._on_unmount, path)
                return [item]
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
        path = _path_of(folder)
        # Right-clicking inside a mounted Cloudy drive → quick Unmount.
        if path and path in _cloudy_mountpoints():
            item = Nautilus.MenuItem(
                name="Cloudy::unmount_bg",
                label="Unmount (Cloudy)",
                tip="Unmount this Cloudy network drive",
            )
            item.connect("activate", self._on_unmount, path)
            return [item]
        if not self._is_managed(path):
            return []
        sync = Nautilus.MenuItem(
            name="Cloudy::sync_folder",
            label="Sync This Folder with Cloudy",
            tip="Mark this folder for synchronization",
        )
        sync.connect("activate", self._on_sync_folder, folder)
        return [sync]

    def _on_unmount(self, _menu, path):
        # Host-side unmount (the mount lives in the host namespace). fusermount3
        # on modern Fedora, fusermount as a fallback. Then drop the sidebar
        # bookmark so it doesn't linger pointing at an empty folder.
        if subprocess.run(["fusermount3", "-u", path]).returncode != 0:
            subprocess.run(["fusermount", "-u", path], check=False)
        self._remove_bookmark(path)

    @staticmethod
    def _remove_bookmark(path):
        bookmarks = os.path.join(
            GLib.get_user_config_dir(), "gtk-3.0", "bookmarks")
        try:
            with open(bookmarks, encoding="utf-8") as fh:
                lines = fh.readlines()
            uri = "file://" + GLib.Uri.escape_string(path, "/", False)
            kept = [ln for ln in lines if ln.split(" ", 1)[0].rstrip("\n") != uri]
            if len(kept) != len(lines):
                with open(bookmarks, "w", encoding="utf-8") as fh:
                    fh.writelines(kept)
        except OSError:
            pass

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
