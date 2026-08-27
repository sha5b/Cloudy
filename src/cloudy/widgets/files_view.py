# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Shahab Nedaei
"""Files surface: a two-pane browser (like Mail and Calendar).

Left pane = your libraries (OneDrive drives + Teams, or Google My Drive), each
with a Mount/Unmount button. Mounting makes the library a real FUSE network
drive (it also appears in the system Files sidebar). Clicking a *mounted*
library loads it into the right pane — a Nautilus-style file/folder browser
(``widgets/file_browser.FileBrowserPane``).
"""

from __future__ import annotations

from gettext import gettext as _

from gi.repository import Adw, GLib, Gtk

from ..core.mount_state import MountState
from ..modules.microsoft365.graph import Drive
from ..modules.microsoft365.mounts import (
    MountManager, forget_mount, mount_base_for, record_mount,
)
from .file_browser import FileBrowserPane
from .source_nav import clear_listbox, is_scope_error, message_row, run_async


class FilesView(Adw.Bin):
    __gtype_name__ = "CloudyFilesView"

    def __init__(self, window, account):
        super().__init__()
        self._window = window
        self._account = account
        self._mounts = MountManager()
        self._state = MountState.get()
        self._libraries: list[dict] = []   # [{drive, icon, subtitle}]
        self._rows: dict = {}              # mountpoint path -> [row, button]
        self._open_name = None             # which library is shown in the pane
        self._status_poll_id = 0           # GLib timer id for the sync-status poll

        # -- left pane: libraries + mount buttons ------------------------
        self._list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE,
                                 valign=Gtk.Align.START)
        self._list.add_css_class("navigation-sidebar")
        self._list.connect("row-activated", self._on_row_activated)
        list_scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER,
                                         vexpand=True, child=self._list)
        sidebar_tb = Adw.ToolbarView()
        sidebar_tb.add_top_bar(Adw.HeaderBar(
            show_start_title_buttons=False, show_end_title_buttons=False,
            title_widget=Gtk.Label(label=_("Libraries"))))
        sidebar_tb.set_content(list_scroll)
        sidebar_page = Adw.NavigationPage(title=_("Libraries"), tag="libraries")
        sidebar_page.set_child(sidebar_tb)

        # -- right pane: the file browser --------------------------------
        self._pane = FileBrowserPane(window)
        self._pane.show_placeholder(_("Mount a library, then click it to browse."))
        content_page = Adw.NavigationPage(title=_("Files"), tag="browser")
        content_page.set_child(self._pane)

        self._split = Adw.NavigationSplitView(
            min_sidebar_width=280, max_sidebar_width=420, sidebar_width_fraction=0.32)
        self._split.set_sidebar(sidebar_page)
        self._split.set_content(content_page)
        self.set_child(self._split)

        self._set_message(_("Loading libraries…"))
        self._load_libraries()
        # Mount state is pushed, not pulled: the shared snapshot tells us when a
        # drive appears or dies (kernel mount events / a Cloudy mount finishing),
        # so opening and closing this tab costs nothing. It used to re-read the
        # mount table on every map — once per drive, and in Flatpak that is a
        # host subprocess each time.
        self._state_handler = self._state.connect(
            "changed", lambda *_: self._apply_mount_states())
        self.connect("map", lambda *_: self._on_map())
        # Stop the sync-status poll when the tab isn't visible (nothing to show).
        self.connect("unmap", lambda *_: self._cancel_status_poll())
        self.connect("destroy", lambda *_: self._state.disconnect(self._state_handler))

    # -- list helpers -----------------------------------------------------
    def _set_message(self, text: str) -> None:
        clear_listbox(self._list)
        self._list.append(message_row(text))

    def _mount_base(self):
        # Per-account folder: keeps same-named drives from different accounts
        # from colliding on one mountpoint, and lets _is_mounted attribute a
        # live mount to this account (so we never re-mount what's already there).
        return mount_base_for(self._account)

    def _is_mounted(self, drive) -> bool:
        return self._state.is_mounted(
            self._mounts.mountpoint_for(drive.name, self._mount_base()))

    def _row_key(self, drive) -> str:
        """Sidebar rows are keyed by mountpoint path, not drive name — two
        libraries can sanitize to the same name ("a/b" and "a_b"), and the key
        doubles as the mountpoint the sync-status poll reports against."""
        return str(self._mounts.mountpoint_for(drive.name, self._mount_base()))

    # -- load library list -----------------------------------------------
    def _libraries_key(self) -> str:
        return f"{self._account.id}:libraries"

    def _load_libraries(self) -> None:
        if self._account.provider == "google":
            self._load_google_libraries()
            return

        # Stale-while-revalidate: show cached libraries instantly (drive/team
        # enumeration is an N+1 round-trip, so without this it's slow every
        # time the Files tab is rebuilt), then refresh in the background.
        cached = self._window.get_application().cache.get(self._libraries_key())
        if cached is not None:
            self._libraries = cached[0]
            self._render_list()
            if cached[1]:
                return

        def work():
            from .graph_helper import build_graph_client

            graph = build_graph_client(self._window.get_application(), self._account)

            def safe(fn):
                try:
                    return (fn(), None)
                except Exception as exc:  # noqa: BLE001
                    return (None, str(exc))

            return safe(graph.list_drives), safe(graph.list_teams)

        run_async(work, self._on_ms_libraries)

    def _load_google_libraries(self) -> None:
        """Google Drive sources, mirroring Microsoft's OneDrive + Team libraries:
        **My Drive** and **Shared with me** are always shown; **Shared Drives**
        (Workspace Team Drives) are enumerated through rclone in the background
        and appended once available (needs a stored rclone token first)."""
        my = Drive(id="", name="My Drive", kind="google_mydrive", web_url="")
        shared = Drive(id="", name=_("Shared with me"),
                       kind="google_shared_with_me", web_url="")
        self._libraries = [
            {"drive": my, "icon": "folder-symbolic", "subtitle": _("Google Drive")},
            {"drive": shared, "icon": "folder-publicshare-symbolic",
             "subtitle": _("Files others have shared with you")},
        ]
        self._render_list()

        # Shared (Team) Drives need a Workspace account and an existing rclone
        # token; enumerate off-thread and append, degrading silently otherwise.
        token = self._window.get_application().secrets.lookup(
            self._account.id, "rclone-gdrive")
        if not token:
            return

        def work():
            return self._mounts.list_google_shared_drives(token)

        run_async(work, self._on_google_shared_drives)

    def _on_google_shared_drives(self, drives, error) -> bool:
        if error or not drives:
            return False
        for d in drives:
            drive = Drive(id=d["id"], name=d["name"],
                          kind="google_shared_drive", web_url="")
            self._libraries.append({
                "drive": drive, "icon": "system-users-symbolic",
                "subtitle": _("Shared drive")})
        self._render_list()
        return False

    def _on_ms_libraries(self, res, error) -> bool:
        if error or not res:
            if not self._libraries:  # keep any cached list on a refresh error
                self._set_message(_("Couldn't load libraries: %s")
                                  % (error or _("unknown error")))
            return False
        (drives, derr), (teams, terr) = res
        libs: list[dict] = []
        for d in drives or []:
            libs.append({"drive": d, "icon": "folder-remote-symbolic",
                         "subtitle": d.kind})
        for t in teams or []:
            libs.append({"drive": t, "icon": "system-users-symbolic",
                         "subtitle": _("Team library")})
        err = derr or terr
        if not libs and err:
            if self._libraries:
                return False  # keep cached render on a partial failure
            if is_scope_error(err) or "scope" in err.lower():
                err = _("New permission needed. Open Preferences → Accounts and "
                        "“Sign Out / Re-sign In” to grant access.")
            self._set_message(err)
            return False
        self._libraries = libs
        self._window.get_application().cache.set(self._libraries_key(), libs)
        self._render_list()
        return False

    def _render_list(self) -> None:
        clear_listbox(self._list)
        self._rows = {}
        if not self._libraries:
            self._set_message(_("No libraries found."))
            return
        for lib in self._libraries:
            self._list.append(self._library_row(lib))

    def _library_row(self, lib) -> Adw.ActionRow:
        from .format import esc

        drive = lib["drive"]
        row = Adw.ActionRow(title=esc(drive.name), activatable=True)
        row._lib = lib  # type: ignore[attr-defined]
        row.add_prefix(Gtk.Image.new_from_icon_name(lib["icon"]))
        self._rows[self._row_key(drive)] = [row, None]
        self._apply_button(lib)
        return row

    def _apply_button(self, lib) -> None:
        from .format import esc

        drive = lib["drive"]
        entry = self._rows.get(self._row_key(drive))
        if entry is None:
            return
        row, old = entry
        if old is not None:
            row.remove(old)
        button = Gtk.Button(valign=Gtk.Align.CENTER)
        if self._is_mounted(drive):
            row.set_subtitle(esc(_("Mounted · click to browse")))
            button.set_icon_name("media-eject-symbolic")
            button.set_tooltip_text(_("Unmount"))
            button.connect("clicked", lambda *_: self._unmount(lib))
        else:
            row.set_subtitle(esc(lib["subtitle"]))
            button.set_label(_("Mount"))
            button.add_css_class("suggested-action")
            button.connect("clicked", lambda *_: self._mount(lib))
        # Flat on both: a subtle accent "Mount" and a quiet eject icon (the solid
        # pill was too loud in the sidebar).
        button.add_css_class("flat")
        row.add_suffix(button)
        entry[1] = button

    def _on_map(self) -> None:
        """Coming back to the Files tab. Render from the shared snapshot (free)
        and restart the sync-status poll; no mount probing happens here."""
        self._apply_mount_states()

    def _apply_mount_states(self) -> None:
        """Re-render every row's Mount/Unmount state from the shared snapshot.
        Pure memory reads — no subprocess, no filesystem, no blocking."""
        if not self.get_mapped():
            return
        for lib in self._libraries:
            self._apply_button(lib)
        self._refresh_upload_status()

    # -- live sync status (via each mount's --rc socket) -----------------
    def _cancel_status_poll(self) -> None:
        if self._status_poll_id:
            GLib.source_remove(self._status_poll_id)
            self._status_poll_id = 0

    def _refresh_upload_status(self) -> None:
        """Show per-drive sync state (Uploading N… / Synced) for mounted drives.
        Reads rclone's VFS cache metadata off-thread (never touches FUSE), then
        re-polls every few seconds while anything is still uploading and stops
        once everything has landed on the server."""
        mounted = self._state.mounted
        # Mountpoint key -> account-scoped remote name. upload_status() reads
        # <cache>/vfsMeta/<remote>/, and remotes are scoped per account
        # ("OneDrive--<account id>") — the bare drive name reads another
        # account's metadata (or nothing), leaving the indicator dead.
        jobs = {self._row_key(lib["drive"]):
                self._mounts.remote_name(lib["drive"].name, self._account.id)
                for lib in self._libraries}
        jobs = {key: remote for key, remote in jobs.items() if key in mounted}
        if not jobs:
            self._cancel_status_poll()
            return

        def work():
            return {key: self._mounts.upload_status(remote)
                    for key, remote in jobs.items()}

        run_async(work, self._on_upload_status)

    def _on_upload_status(self, statuses, error) -> bool:
        from .format import esc

        # A result can land after unmap cancelled the timer (the fetch was
        # already in flight); re-arming from here would keep the poll — and its
        # per-drive mount-table checks — running while the tab is hidden.
        if error or not self.get_mapped():
            return False
        if not statuses:  # nothing mounted — stop watching
            self._cancel_status_poll()
            return False
        busy = False
        for key, st in statuses.items():
            entry = self._rows.get(key)
            if not entry:
                continue
            row = entry[0]
            pending = (st or {}).get("pending", 0)
            if pending:
                busy = True
                row.set_subtitle(esc(_("↑ Uploading %d…") % pending))
            else:
                row.set_subtitle(esc(_("Synced · click to browse")))
        self._cancel_status_poll()
        if busy:  # keep watching until the queue drains
            self._status_poll_id = GLib.timeout_add_seconds(
                3, self._on_status_tick)
        return False

    def _on_status_tick(self) -> bool:
        self._status_poll_id = 0
        self._refresh_upload_status()
        return False  # one-shot; _on_upload_status reschedules if still busy

    # -- selection --------------------------------------------------------
    def _on_row_activated(self, _list, row) -> None:
        lib = getattr(row, "_lib", None)
        if lib is None:
            return
        drive = lib["drive"]
        mp = self._mounts.mountpoint_for(drive.name, self._mount_base())

        # A mountpoint can sit in the mount table with its daemon dead
        # ("stale") — opening it would hang the browser's scan forever on
        # "Loading…". The snapshot already knows, so this costs nothing.
        health = self._state.health(mp)
        if health == "absent":
            self._window.add_toast(_("Mount %s first.") % drive.name)
            return
        if health == "stale":
            self._window.add_toast(
                _("%s isn't responding — unmount and mount it again.") % drive.name)
            self._apply_button(lib)
            return
        self._open_name = drive.name
        self._pane.open_root(mp, drive.name)
        self._split.set_show_content(True)  # reveal when collapsed

    # -- mount / unmount --------------------------------------------------
    def _mount(self, lib) -> None:
        drive = lib["drive"]
        if self._mounts.preferred_backend() is None:
            self._window.add_toast(_("No mount backend available."))
            return
        google = self._account.provider == "google"
        token_kind = "rclone-gdrive" if google else "rclone-onedrive"
        secrets = self._window.get_application().secrets
        token = secrets.lookup(self._account.id, token_kind)
        if not token:
            self._window.add_toast(_("Opening your browser to connect…"))

        base = self._mount_base()

        def work():
            backend = "drive" if google else "onedrive"
            tok = token
            if not tok:
                tok = self._mounts.authorize(backend)
                secrets.store(self._account.id, token_kind, tok)
            return self._mounts.mount_drive(
                provider=self._account.provider, drive=drive, token=tok,
                base=base, account_id=self._account.id)

        run_async(work, lambda info, error: self._on_mounted(lib, info, error))

    def _on_mounted(self, lib, info, error) -> bool:
        if error:
            self._window.add_toast(_("Mount failed: %s") % error)
            return False
        # Remember it so it remounts automatically on the next startup.
        record_mount(self._account.id, lib["drive"], mountpoint=str(info.mountpoint))
        self._apply_button(lib)
        self._window.add_toast(_("%s is ready.") % lib["drive"].name)
        # Open it straight away for a one-click feel.
        self._open_name = lib["drive"].name
        mp = self._mounts.mountpoint_for(lib["drive"].name, self._mount_base())
        self._pane.open_root(mp, lib["drive"].name)
        self._split.set_show_content(True)
        return False

    def _unmount(self, lib) -> None:
        mountpoint = self._mounts.mountpoint_for(lib["drive"].name, self._mount_base())
        run_async(
            lambda: self._mounts.unmount(mountpoint),
            lambda _r, error: self._on_unmounted(lib, error),
        )

    def _on_unmounted(self, lib, error) -> bool:
        if error:
            self._window.add_toast(_("Unmount failed: %s") % error)
            return False
        # Forget it so it stays unmounted on the next startup.
        forget_mount(self._account.id, lib["drive"].name)
        self._apply_button(lib)
        if self._open_name == lib["drive"].name:
            self._open_name = None
            self._pane.show_placeholder(_("Mount a library, then click it to browse."))
        self._window.add_toast(_("Unmounted %s.") % lib["drive"].name)
        return False
