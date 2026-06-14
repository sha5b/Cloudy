# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Fiber Elements
"""Files surface for a signed-in Microsoft 365 account.

Two sections: the user's own OneDrive drives and the document libraries of the
Teams they belong to (mounted at the team level). Enumeration runs off the UI
thread.
"""

from __future__ import annotations

import threading
from gettext import gettext as _

from gi.repository import Adw, GLib, Gtk

from ..modules.microsoft365.mounts import MountManager


class FilesView(Adw.Bin):
    __gtype_name__ = "CloudyFilesView"

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

        self._drives_group = Adw.PreferencesGroup(
            title=_("Your OneDrive"),
            description=_("Mount a library to open it in Files like a network drive."),
        )
        self._page.add(self._drives_group)
        self._drives_loading = Adw.ActionRow(title=_("Loading libraries…"))
        self._drives_group.add(self._drives_loading)

        self._teams_group = Adw.PreferencesGroup(
            title=_("Teams"),
            description=_("Document libraries of the Teams you belong to."),
        )
        self._page.add(self._teams_group)
        self._teams_loading = Adw.ActionRow(title=_("Loading Teams…"))
        self._teams_group.add(self._teams_loading)

        # name -> (row, action_button, base_subtitle) so we can flip Mount/Unmount.
        self._rows: dict = {}
        self._load_async()

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

    # -- loading (off the UI thread) --------------------------------------
    def _load_async(self) -> None:
        def worker():
            from .graph_helper import build_graph_client

            try:
                graph = build_graph_client(self._window.get_application(), self._account)
            except Exception as exc:  # noqa: BLE001 - no client/auth
                GLib.idle_add(self._fill, self._drives_group, self._drives_loading, None, str(exc), False)
                GLib.idle_add(self._fill, self._teams_group, self._teams_loading, None, str(exc), True)
                return

            try:
                drives = graph.list_drives()
                GLib.idle_add(self._fill, self._drives_group, self._drives_loading, drives, None, False)
            except Exception as exc:  # noqa: BLE001
                GLib.idle_add(self._fill, self._drives_group, self._drives_loading, None, str(exc), False)

            try:
                teams = graph.list_teams()
                GLib.idle_add(self._fill, self._teams_group, self._teams_loading, teams, None, True)
            except Exception as exc:  # noqa: BLE001
                GLib.idle_add(self._fill, self._teams_group, self._teams_loading, None, str(exc), True)

        threading.Thread(target=worker, daemon=True).start()

    def _fill(self, group, loading_row, drives, error, is_team) -> bool:
        group.remove(loading_row)
        if error:
            if "no token" in error or "scope" in error.lower():
                error = _(
                    "New permission needed. Use the account menu (⋮) → "
                    "“Sign Out / Re-sign In” to grant access."
                )
            group.add(Adw.ActionRow(title=_("Couldn't load"), subtitle=error))
            return False
        if not drives:
            empty = _("You don't belong to any Teams.") if is_team else _("No libraries found.")
            group.add(Adw.ActionRow(title=empty))
            return False
        for drive in drives:
            group.add(self._drive_row(drive, is_team))
        return False

    def _drive_row(self, drive, is_team) -> Adw.ActionRow:
        from .format import esc

        base_subtitle = _("Team library") if is_team else drive.kind
        row = Adw.ActionRow(title=esc(drive.name))
        icon = "system-users-symbolic" if is_team else "folder-remote-symbolic"
        row.add_prefix(Gtk.Image.new_from_icon_name(icon))
        self._rows[drive.name] = [row, None, base_subtitle]
        self._apply_button(drive)
        return row

    def _apply_button(self, drive) -> None:
        """Set the row's button/subtitle to reflect mounted state."""
        from .format import esc

        entry = self._rows.get(drive.name)
        if entry is None:
            return
        row, old_button, base_subtitle = entry
        if old_button is not None:
            row.remove(old_button)

        mounted = self._mounts.is_mounted(self._mounts.mountpoint_for(drive.name))
        button = Gtk.Button(valign=Gtk.Align.CENTER)
        if mounted:
            row.set_subtitle(esc(_("Mounted · in the Files sidebar")))
            button.set_label(_("Unmount"))
            button.connect("clicked", lambda *_: self._unmount(drive))
        else:
            row.set_subtitle(esc(base_subtitle))
            button.set_label(_("Mount"))
            button.add_css_class("suggested-action")
            button.connect("clicked", lambda *_: self._mount(drive))
        row.add_suffix(button)
        entry[1] = button

    # -- actions ----------------------------------------------------------
    def _mount(self, drive) -> None:
        if self._mounts.preferred_backend() is None:
            self._window.add_toast(_("No mount backend available."))
            return
        secrets = self._window.get_application().secrets
        token = secrets.lookup(self._account.id, "rclone-onedrive")
        if not token:
            self._window.add_toast(_("Opening your browser to connect OneDrive…"))
        threading.Thread(
            target=self._mount_worker, args=(drive, secrets, token), daemon=True
        ).start()

    def _mount_worker(self, drive, secrets, token) -> None:
        try:
            # One rclone browser auth per account; reused for every library.
            if not token:
                token = self._mounts.authorize_onedrive()
                secrets.store(self._account.id, "rclone-onedrive", token)

            remote = self._mounts._safe_name(drive.name)
            self._mounts.create_onedrive_remote(
                remote, token, drive.id, self._mounts.drive_type_for(drive.kind)
            )
            info = self._mounts.mount(name=drive.name, remote=remote, drive_id=drive.id)
            GLib.idle_add(self._on_mounted, drive, info, None)
        except Exception as exc:  # noqa: BLE001
            GLib.idle_add(self._on_mounted, drive, None, str(exc))

    def _on_mounted(self, drive, info, error) -> bool:
        if error:
            self._window.add_toast(_("Mount failed: %s") % error)
            return False
        self._apply_button(drive)  # flip the button to "Unmount"
        self._window.add_toast(
            _("%s is now in your Files sidebar.") % drive.name
            if info and info.active
            else _("Mount requested for %s.") % drive.name
        )
        return False

    def _unmount(self, drive) -> None:
        def worker():
            try:
                self._mounts.unmount(self._mounts.mountpoint_for(drive.name))
                GLib.idle_add(self._on_unmounted, drive, None)
            except Exception as exc:  # noqa: BLE001
                GLib.idle_add(self._on_unmounted, drive, str(exc))

        threading.Thread(target=worker, daemon=True).start()

    def _on_unmounted(self, drive, error) -> bool:
        if error:
            self._window.add_toast(_("Unmount failed: %s") % error)
            return False
        self._apply_button(drive)  # flip the button back to "Mount"
        self._window.add_toast(_("Unmounted %s.") % drive.name)
        return False
