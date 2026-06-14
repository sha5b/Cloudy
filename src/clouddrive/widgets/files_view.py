# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Fiber Elements
"""Files surface for a signed-in Microsoft 365 account.

Lists the account's drives/libraries and lets the user mount one so it appears
in the file manager. Drive listing is done off the UI thread.
"""

from __future__ import annotations

import threading
from gettext import gettext as _

from gi.repository import Adw, Gio, GLib, Gtk

from ..modules.microsoft365.mounts import MountManager


class FilesView(Adw.Bin):
    __gtype_name__ = "ClouddriveFilesView"

    def __init__(self, window, account):
        super().__init__()
        self._window = window
        self._account = account
        self._mounts = MountManager()

        self._page = Adw.PreferencesPage()
        self.set_child(self._page)

        self._backend_group = Adw.PreferencesGroup(title=_("Storage backend"))
        self._page.add(self._backend_group)
        self._show_backend_status()

        self._library_group = Adw.PreferencesGroup(
            title=_("Your libraries"),
            description=_("Mount a library to open it in Files like a network drive."),
        )
        self._page.add(self._library_group)
        self._loading_row = Adw.ActionRow(title=_("Loading libraries…"))
        self._library_group.add(self._loading_row)

        self._load_drives_async()

    # -- backend status ---------------------------------------------------
    def _show_backend_status(self) -> None:
        backend = self._mounts.preferred_backend()
        if backend is not None:
            row = Adw.ActionRow(
                title=_("Mounting via %s") % backend.name,
                subtitle=_("Libraries you mount appear in the Files sidebar."),
            )
            row.add_prefix(Gtk.Image.new_from_icon_name("emblem-ok-symbolic"))
        else:
            row = Adw.ActionRow(
                title=_("No mount backend found"),
                subtitle=_("Install rclone or onedriver to mount libraries."),
            )
            row.add_prefix(Gtk.Image.new_from_icon_name("dialog-warning-symbolic"))
        self._backend_group.add(row)

    # -- drive loading (off the UI thread) --------------------------------
    def _load_drives_async(self) -> None:
        def worker():
            try:
                from .graph_helper import build_graph_client

                graph = build_graph_client(self._window.get_application(), self._account)
                drives = graph.list_drives()
                GLib.idle_add(self._on_drives_loaded, drives, None)
            except Exception as exc:  # noqa: BLE001
                GLib.idle_add(self._on_drives_loaded, None, str(exc))

        threading.Thread(target=worker, daemon=True).start()

    def _on_drives_loaded(self, drives, error) -> bool:
        self._library_group.remove(self._loading_row)
        if error:
            self._library_group.add(
                Adw.ActionRow(
                    title=_("Couldn't load libraries"),
                    subtitle=error,
                )
            )
            return False
        if not drives:
            self._library_group.add(
                Adw.ActionRow(title=_("No libraries found for this account."))
            )
            return False
        for drive in drives:
            self._library_group.add(self._drive_row(drive))
        return False

    def _drive_row(self, drive) -> Adw.ActionRow:
        row = Adw.ActionRow(title=drive.name, subtitle=drive.kind)
        row.add_prefix(Gtk.Image.new_from_icon_name("folder-remote-symbolic"))

        if self._mounts.is_mounted(self._mounts.mountpoint_for(drive.name)):
            button = Gtk.Button(label=_("Open"), valign=Gtk.Align.CENTER)
            button.connect("clicked", lambda *_: self._open(drive))
        else:
            button = Gtk.Button(label=_("Mount"), valign=Gtk.Align.CENTER)
            button.add_css_class("suggested-action")
            button.connect("clicked", lambda *_: self._mount(drive))
        row.add_suffix(button)
        return row

    # -- actions ----------------------------------------------------------
    def _mount(self, drive) -> None:
        try:
            info = self._mounts.mount(
                name=drive.name,
                remote=self._mounts._safe_name(drive.name),
                drive_id=drive.id,
            )
        except Exception as exc:  # noqa: BLE001
            self._window.add_toast(_("Mount failed: %s") % exc)
            return
        self._window.add_toast(
            _("%s is now in your Files sidebar.") % drive.name
            if info.active
            else _("Mount requested for %s.") % drive.name
        )

    def _open(self, drive) -> None:
        uri = self._mounts.mountpoint_for(drive.name).as_uri()
        Gio.AppInfo.launch_default_for_uri(uri, None)
