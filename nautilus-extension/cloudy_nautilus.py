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

import os
import time

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

_DBUS_TIMEOUT_MS = 1500
# Creating a share link hits the network (Graph resolve + createLink), so it
# needs a far longer budget than the fast, local status/roots queries.
_SHARE_LINK_TIMEOUT_MS = 20000
# How long to trust the cached "managed roots" before asking the app again.
# These paths almost never change, so a long TTL keeps the file manager fast.
_ROOTS_TTL_S = 30.0
# If creating the D-Bus proxy fails (app not running / bus stalled), remember
# that for a short while so every menu rebuild doesn't retry.
_PROXY_FAILURE_TTL_S = 10.0

# Shared extension state. get_file_items/get_background_items run on Nautilus's
# UI thread with NO async variant, so nothing here may ever block: the proxy is
# created asynchronously, the roots cache is refreshed asynchronously, and the
# menu hooks only ever read the last cached snapshot.
_state = {
    "proxy": None,          # Gio.DBusProxy once created
    "creating": False,      # a new_for_bus() is in flight
    "failed_at": 0.0,       # last proxy-creation failure (monotonic)
    "pending": [],          # callbacks waiting for the proxy
    "roots": (),            # cached managed roots (normalized paths)
    "roots_at": 0.0,        # when the roots were last refreshed (monotonic)
    "refreshing": False,    # a ManagedRoots call is in flight
}


def _ensure_proxy(on_ready=None):
    """Create (once, asynchronously) the D-Bus proxy to the Cloudy service.

    ``on_ready(proxy_or_None)`` fires from the GLib main loop when the proxy is
    available (or creation failed). Never blocks the calling thread.
    """
    proxy = _state["proxy"]
    if proxy is not None:
        if on_ready is not None:
            on_ready(proxy)
        return
    now = time.monotonic()
    if now - _state["failed_at"] < _PROXY_FAILURE_TTL_S:
        if on_ready is not None:
            on_ready(None)
        return
    if on_ready is not None:
        _state["pending"].append(on_ready)
    if _state["creating"]:
        return
    _state["creating"] = True

    def _on_created(_source, res):
        _state["creating"] = False
        try:
            _state["proxy"] = Gio.DBusProxy.new_for_bus_finish(res)
        except GLib.Error:
            _state["proxy"] = None
            _state["failed_at"] = time.monotonic()
        waiters, _state["pending"] = _state["pending"], []
        for cb in waiters:
            cb(_state["proxy"])

    Gio.DBusProxy.new_for_bus(
        Gio.BusType.SESSION,
        # Skip the GetAll-properties round-trip and signal subscription — this
        # service exposes methods only, and both add blocking bus traffic.
        Gio.DBusProxyFlags.DO_NOT_AUTO_START
        | Gio.DBusProxyFlags.DO_NOT_LOAD_PROPERTIES
        | Gio.DBusProxyFlags.DO_NOT_CONNECT_SIGNALS,
        None, BUS_NAME, OBJECT_PATH, INTERFACE, None,
        _on_created,
    )


def _call_async(method, variant, reply_type=None, callback=None,
                timeout_ms=_DBUS_TIMEOUT_MS):
    """Fire a D-Bus call without blocking Nautilus's UI thread.

    ``callback(result, error)`` is invoked from the GLib main loop when the call
    finishes. If the proxy is unavailable the callback is invoked with
    (None, None) once creation has failed.
    """

    def _with_proxy(proxy):
        if proxy is None:
            if callback is not None:
                callback(None, None)
            return

        def _on_done(_proxy, res):
            result = None
            err = None
            try:
                result = _proxy.call_finish(res)
                if reply_type is not None and result is not None \
                        and result.get_type_string() != reply_type:
                    # Best-effort sanity check: a mismatched variant signature
                    # is treated as no result (keeps the extension safe against
                    # future interface changes).
                    result = None
            except GLib.Error as exc:
                err = exc
            if callback is not None:
                callback(result, err)

        proxy.call(method, variant, Gio.DBusCallFlags.NONE, timeout_ms,
                   None, _on_done)

    _ensure_proxy(_with_proxy)


def _path_of(file):
    location = file.get_location()
    return location.get_path() if location else None


def _managed_roots():
    """The Cloudy-managed directories (mount + sync roots), as normalized path
    strings. Always returns the cached snapshot IMMEDIATELY (empty until the
    first async fetch lands) and, when the cache is older than ``_ROOTS_TTL_S``,
    kicks off an asynchronous refresh in the background. The menu hooks below
    run on Nautilus's UI thread on every selection change / folder open, so this
    must never wait on D-Bus — a synchronous call here is exactly what froze the
    whole file manager whenever the app was busy or a FUSE mount stalled."""
    now = time.monotonic()
    if (now - _state["roots_at"]) >= _ROOTS_TTL_S and not _state["refreshing"]:
        _state["refreshing"] = True

        def _on_roots(result, _error):
            _state["refreshing"] = False
            _state["roots_at"] = time.monotonic()
            roots = ()
            if result is not None:
                (paths,) = result.unpack()
                roots = tuple(os.path.normpath(p) for p in paths if p)
            _state["roots"] = roots

        _call_async("ManagedRoots", None, "(as)", _on_roots)
    return _state["roots"]


def _under_roots(path, roots):
    """True when ``path`` is at or below one of the managed ``roots``. Pure
    string compare on the normalized path — deliberately no os.path.ismount /
    stat / resolve, since those would touch the filesystem and could block on a
    slow or hung FUSE network mount (freezing Nautilus)."""
    if not path or not roots:
        return False
    p = os.path.normpath(path)
    return any(p == r or p.startswith(r + os.sep) for r in roots)


class CloudyMenuProvider(GObject.GObject, Nautilus.MenuProvider):
    """Right-click controls for Cloudy-managed files/folders."""

    def get_file_items(self, *args):
        # API 4.0: get_file_items(files); API 4.1+: get_file_items(window, files).
        # Accept both by taking the last list-typed argument.
        files = args[-1] if args else []
        # Only offer controls for managed paths. This runs on Nautilus's UI
        # thread on every selection change, so it MUST stay cheap: a local prefix
        # test against the cached roots, no D-Bus per file.
        roots = _managed_roots()
        if not roots:
            return []
        managed = [f for f in files if _under_roots(_path_of(f), roots)]
        if not managed:
            return []

        copy_link = Nautilus.MenuItem(
            name="Cloudy::copy_share_link",
            label="Copy Share Link (Cloudy)",
            tip="Create and copy a OneDrive/SharePoint sharing link via Cloudy",
        )
        copy_link.connect("activate", self._on_copy_link, managed)

        free_space = Nautilus.MenuItem(
            name="Cloudy::free_up_space",
            label="Free Up Space",
            tip="Remove the local copy; keep the file online",
        )
        free_space.connect("activate", self._on_free_space, managed)
        return [copy_link, free_space]

    def get_background_items(self, *args):
        # API 4.0: get_background_items(folder); API 4.1+: get_background_items(window, folder).
        folder = args[-1] if args else None
        # Runs on every folder you open — keep it to a local prefix test.
        if folder is None or not _under_roots(_path_of(folder), _managed_roots()):
            return []
        sync = Nautilus.MenuItem(
            name="Cloudy::sync_folder",
            label="Sync This Folder with Cloudy",
            tip="Mark this folder for synchronization",
        )
        sync.connect("activate", self._on_sync_folder, folder)
        return [sync]

    # -- helpers ----------------------------------------------------------
    def _on_copy_link(self, _menu, files):
        path = _path_of(files[0])

        def _on_result(result, _error):
            if result is None:
                return
            (url,) = result.unpack()
            if url:
                clipboard = self._clipboard()
                if clipboard is not None:
                    # GTK4's Gdk.Clipboard has no set_text() in the Python
                    # bindings (it's skipped in the GIR) — calling it raised
                    # AttributeError and the menu item silently did nothing.
                    # The PyGObject override set() accepts a plain string.
                    clipboard.set(url)

        _call_async(
            "CreateShareLink", GLib.Variant("(sb)", (path, False)), "(s)", _on_result,
            timeout_ms=_SHARE_LINK_TIMEOUT_MS,
        )

    def _on_free_space(self, _menu, files):
        for f in files:
            _call_async(
                "FreeUpSpace", GLib.Variant("(s)", (_path_of(f),)), None, None
            )

    def _on_sync_folder(self, _menu, folder):
        _call_async(
            "SyncPath", GLib.Variant("(s)", (_path_of(folder),)), None, None
        )

    @staticmethod
    def _clipboard():
        from gi.repository import Gdk

        display = Gdk.Display.get_default()
        return display.get_clipboard() if display else None
