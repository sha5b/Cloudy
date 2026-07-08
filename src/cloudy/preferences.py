# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Shahab Nedaei
"""Preferences, split cleanly in two:

* **General** — app-wide file setup: where libraries mount, the mount layout
  (one folder vs. individual), file caching, the default sync type (streaming
  vs. full offline copy), and startup.
* **Accounts** — per account: an on/off switch for its services, sign
  in/out/remove, plus (in an expander) whether to sync that account's files
  offline and where it mounts.
"""

import re
from gettext import gettext as _
from pathlib import Path

from gi.repository import Adw, Gio, GLib, Gtk

_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


class CloudyPreferences(Adw.PreferencesDialog):
    __gtype_name__ = "CloudyPreferences"

    def __init__(self, engine, settings: Gio.Settings, app=None, **kwargs):
        super().__init__(**kwargs)
        self.set_title(_("Preferences"))
        self._engine = engine
        self._settings = settings
        self._app = app

        self._account_rows = []
        self._sync_rows = []  # (Adw.SwitchRow, account) for master-switch updates
        self._reg_handler = None

        self.add(self._general_page())
        self.add(self._notifications_page())
        self.add(self._accounts_page())

    # -- General ----------------------------------------------------------
    def _general_page(self) -> Adw.PreferencesPage:
        page = Adw.PreferencesPage(title=_("General"), icon_name="emblem-system-symbolic")

        files = Adw.PreferencesGroup(
            title=_("Files"),
            description=_("Where your cloud drives appear on this computer."),
        )
        page.add(files)

        self._location_row = Adw.ActionRow(title=_("Mount location"))
        self._location_row.set_subtitle(self._mount_location_display())
        choose = Gtk.Button(label=_("Choose…"), valign=Gtk.Align.CENTER)
        choose.connect("clicked", self._on_choose_location)
        self._location_row.add_suffix(choose)
        files.add(self._location_row)

        layout = Adw.ComboRow(title=_("Mount layout"))
        layout.set_subtitle(_("One folder for all, or a folder per account."))
        self._layout_values = ["one-folder", "individual"]
        lm = Gtk.StringList()
        lm.append(_("One folder (subfolder per library)"))
        lm.append(_("Individual (each account picks its folder)"))
        layout.set_model(lm)
        layout.set_selected(self._index_of("mount-layout", self._layout_values))
        layout.connect("notify::selected", self._on_layout_changed)
        files.add(layout)

        cache = Adw.ComboRow(title=_("File caching"))
        cache.set_subtitle(_("How much is kept on disk versus fetched on demand."))
        self._cache_values = ["full", "minimal"]
        cm = Gtk.StringList()
        cm.append(_("On-demand (cache opened files)"))
        cm.append(_("Streaming (minimal disk use)"))
        cache.set_model(cm)
        cache.set_selected(self._index_of("cache-mode", self._cache_values))
        cache.connect("notify::selected", self._on_cache_changed)
        files.add(cache)

        sync = Adw.PreferencesGroup(
            title=_("Sync"),
            description=_(
                "The default for accounts where you turn on offline sync "
                "(on the Accounts page)."
            ),
        )
        page.add(sync)
        sync_type = Adw.ComboRow(title=_("Sync type"))
        sync_type.set_subtitle(_("Stream files on demand, or keep a full offline copy."))
        self._sync_values = ["stream", "full"]
        sm = Gtk.StringList()
        sm.append(_("Streaming (mount on demand, no local copy)"))
        sm.append(_("Full sync (two-way offline copy on disk)"))
        sync_type.set_model(sm)
        sync_type.set_selected(self._index_of("default-sync-type", self._sync_values))
        sync_type.connect("notify::selected", self._on_sync_type_changed)
        sync.add(sync_type)

        offline_master = Adw.SwitchRow(
            title=_("Offline sync"),
            subtitle=_("Allow accounts to keep two-way offline copies."))
        self._settings.bind("offline-sync-enabled", offline_master, "active",
                            Gio.SettingsBindFlags.DEFAULT)
        offline_master.connect("notify::active", self._on_offline_sync_toggled)
        sync.add(offline_master)

        startup = Adw.PreferencesGroup(title=_("Startup"))
        page.add(startup)
        autostart = Adw.SwitchRow(
            title=_("Start at login"),
            subtitle=_("Launch Cloudy automatically when you sign in."),
        )
        autostart.set_active(self._settings.get_boolean("autostart"))
        autostart.connect("notify::active", self._on_autostart_changed)
        startup.add(autostart)

        # Background / desktop integration (notifications live on their own tab).
        integ = Adw.PreferencesGroup(
            title=_("Background"),
            description=_("Keep Cloudy working when its window is closed."),
        )
        page.add(integ)

        background = Adw.SwitchRow(
            title=_("Keep running in the background"),
            subtitle=_("Closing the window hides it; quit with Ctrl+Q."))
        self._settings.bind("run-in-background", background, "active",
                            Gio.SettingsBindFlags.DEFAULT)
        integ.add(background)

        eds = Adw.SwitchRow(
            title=_("Show events in the GNOME calendar"),
            subtitle=_("Mirror your calendar into the top-bar calendar (Evolution)."))
        self._settings.bind("eds-publish-enabled", eds, "active",
                            Gio.SettingsBindFlags.DEFAULT)
        eds.connect("notify::active", self._on_eds_toggled)
        integ.add(eds)

        nautilus = Adw.SwitchRow(
            title=_("Nautilus file-manager integration"),
            subtitle=_("Show Cloudy mounts and sync status in the GNOME Files "
                       "sidebar (restart Files to apply)."))
        self._settings.bind("nautilus-extension-enabled", nautilus, "active",
                            Gio.SettingsBindFlags.DEFAULT)
        nautilus.connect("notify::active", self._on_nautilus_toggled)
        integ.add(nautilus)

        return page

    def _on_nautilus_toggled(self, row, _param) -> None:
        """Install or remove the host Nautilus extension to match the toggle."""
        from .core.provisioner import set_host_nautilus_extension

        set_host_nautilus_extension(
            row.get_active(), log=lambda m: print(f"[provision] {m}"))

    # -- Notifications ----------------------------------------------------
    def _notifications_page(self) -> Adw.PreferencesPage:
        page = Adw.PreferencesPage(title=_("Notifications"),
                                   icon_name="preferences-system-notifications-symbolic")

        alerts = Adw.PreferencesGroup(
            title=_("Alerts"),
            description=_("When and how Cloudy interrupts you."),
        )
        page.add(alerts)

        notify = Adw.SwitchRow(
            title=_("Desktop notifications"),
            subtitle=_("Alert me about new mail and upcoming events."))
        self._settings.bind("notifications-enabled", notify, "active",
                            Gio.SettingsBindFlags.DEFAULT)
        alerts.add(notify)

        # Relevance level: everything, or only direct/important (group-chat
        # chatter and ordinary mail then update badges silently).
        level = Adw.ComboRow(title=_("Notify me about"))
        level.set_subtitle(_("Limit interruptions to what matters."))
        self._notify_level_values = ["all", "digest", "priority"]
        lvl_model = Gtk.StringList()
        lvl_model.append(_("Everything"))
        lvl_model.append(_("Direct now, routine in a summary"))
        lvl_model.append(_("Direct messages & important only"))
        level.set_model(lvl_model)
        level.set_selected(self._index_of("notify-level", self._notify_level_values))
        level.connect("notify::selected", self._on_notify_level_changed)
        alerts.add(level)

        dnd = Adw.SwitchRow(
            title=_("Respect system Do Not Disturb"),
            subtitle=_("Stay silent while GNOME Do Not Disturb is on."))
        self._settings.bind("notify-respect-system-dnd", dnd, "active",
                            Gio.SettingsBindFlags.DEFAULT)
        alerts.add(dnd)

        # Quiet hours in their own group.
        quiet_group = Adw.PreferencesGroup(
            title=_("Quiet hours"),
            description=_("Silence banners overnight — badges still update."),
        )
        page.add(quiet_group)

        quiet = Adw.SwitchRow(
            title=_("Enable quiet hours"),
            subtitle=_("Hold back banners during the window below."))
        self._settings.bind("quiet-hours-enabled", quiet, "active",
                            Gio.SettingsBindFlags.DEFAULT)
        quiet_group.add(quiet)
        quiet_group.add(self._time_row(_("Start"), "quiet-hours-start"))
        quiet_group.add(self._time_row(_("End"), "quiet-hours-end"))

        return page

    # -- Accounts ---------------------------------------------------------
    def _accounts_page(self) -> Adw.PreferencesPage:
        page = Adw.PreferencesPage(title=_("Accounts"),
                                   icon_name="system-users-symbolic")
        self._accounts_group = Adw.PreferencesGroup(
            title=_("Accounts"),
            description=_("Sign in, sign out, and choose how each account's files sync."),
        )
        add_btn = Gtk.Button(icon_name="list-add-symbolic", valign=Gtk.Align.CENTER,
                             tooltip_text=_("Add account"))
        add_btn.add_css_class("flat")
        add_btn.connect("clicked", lambda *_: self._app.activate_action("add-account", None))
        self._accounts_group.set_header_suffix(add_btn)
        page.add(self._accounts_group)

        self._rebuild_accounts()
        registry = getattr(self._app, "registry", None)
        if registry is not None:
            self._reg_handler = registry.connect(
                "changed", lambda *_: self._rebuild_accounts()
            )
            self.connect("closed", self._on_closed)
        return page

    def _on_closed(self, *_args) -> None:
        registry = getattr(self._app, "registry", None)
        if registry is not None and self._reg_handler is not None:
            registry.disconnect(self._reg_handler)
            self._reg_handler = None

    def _rebuild_accounts(self) -> None:
        for row in self._account_rows:
            self._accounts_group.remove(row)
        self._account_rows = []
        self._sync_rows = []
        registry = getattr(self._app, "registry", None)
        accounts = registry.accounts() if registry else []
        if not accounts:
            row = Adw.ActionRow(
                title=_("No accounts yet"),
                subtitle=_("Use + to add a Microsoft 365 or Google account."),
            )
            self._accounts_group.add(row)
            self._account_rows.append(row)
            return
        for account in accounts:
            row = self._account_row(account)
            self._accounts_group.add(row)
            self._account_rows.append(row)

    def _account_row(self, account) -> Adw.ExpanderRow:
        from .widgets.format import esc

        status = _("Signed in") if account.signed_in else _("Signed out")
        row = Adw.ExpanderRow(title=esc(account.display_name), subtitle=status)
        row.add_prefix(Gtk.Image.new_from_icon_name(
            "emblem-ok-symbolic" if account.signed_in else "action-unavailable-symbolic"
        ))

        # Activate/deactivate this account's services (replaces the Modules tab).
        active = Gtk.Switch(valign=Gtk.Align.CENTER,
                            tooltip_text=_("Turn this account's services on or off"))
        active.set_active(self._engine.is_enabled(account.module_id))
        active.connect("notify::active", self._on_account_active, account)
        row.add_suffix(active)

        # Sign in/out + remove as row suffixes.
        if account.signed_in:
            auth = Gtk.Button(label=_("Sign Out"), valign=Gtk.Align.CENTER)
            auth.connect("clicked", lambda *_: self._account_action("sign_out_account", account))
        else:
            auth = Gtk.Button(label=_("Sign In"), valign=Gtk.Align.CENTER)
            auth.add_css_class("suggested-action")
            auth.connect("clicked", lambda *_: self._account_action("sign_in_account", account))
        row.add_suffix(auth)
        remove = Gtk.Button(icon_name="user-trash-symbolic", valign=Gtk.Align.CENTER,
                            tooltip_text=_("Remove account"))
        remove.add_css_class("flat")
        remove.connect("clicked", lambda *_: self._account_action("remove_account", account))
        row.add_suffix(remove)

        # Per-account file settings (greyed out until their prerequisite in
        # General is set, so the options are always discoverable).
        row.add_row(self._sync_row(account))
        row.add_row(self._mount_location_row(account))
        row.add_row(self._signature_row(account))
        return row

    # -- per-account email signature -------------------------------------
    def _signature_row(self, account) -> Adw.ActionRow:
        sig = (getattr(account, "signature", "") or "").strip()
        row = Adw.ActionRow(
            title=_("Email signature"),
            subtitle=(sig.splitlines()[0] if sig else _("Not set — added to new mail, replies and forwards")))
        edit = Gtk.Button(label=_("Edit…"), valign=Gtk.Align.CENTER)
        edit.connect("clicked", lambda *_: self._edit_signature(account, row))
        row.add_suffix(edit)
        return row

    def _edit_signature(self, account, row) -> None:
        dialog = Adw.Dialog()
        dialog.set_title(_("Email signature"))
        dialog.set_content_width(520)
        dialog.set_content_height(360)
        view = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR, top_margin=10,
                            bottom_margin=10, left_margin=10, right_margin=10)
        view.get_buffer().set_text(getattr(account, "signature", "") or "")
        scrolled = Gtk.ScrolledWindow(vexpand=True, child=view)
        cancel = Gtk.Button(label=_("Cancel"))
        cancel.connect("clicked", lambda *_: dialog.close())
        save = Gtk.Button(label=_("Save"))
        save.add_css_class("suggested-action")
        header = Adw.HeaderBar(show_start_title_buttons=False,
                               show_end_title_buttons=False)
        header.pack_start(cancel)
        header.pack_end(save)
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(header)
        toolbar.set_content(scrolled)
        dialog.set_child(toolbar)

        def do_save(*_a):
            buf = view.get_buffer()
            text = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False)
            account.signature = text.strip()
            if getattr(self._app, "registry", None) is not None:
                self._app.registry.update(account)
            sig = account.signature
            row.set_subtitle(sig.splitlines()[0] if sig
                             else _("Not set — added to new mail, replies and forwards"))
            dialog.close()

        save.connect("clicked", do_save)
        dialog.present(self)

    def _sync_row(self, account) -> Adw.SwitchRow:
        full = self._setting_str("default-sync-type") == "full"
        master = False
        try:
            master = self._settings.get_boolean("offline-sync-enabled")
        except Exception:  # noqa: BLE001
            pass
        if not master:
            sub = _("Offline sync is disabled in General")
        elif full:
            sub = _("Keep a two-way offline copy of this account's files")
        else:
            sub = _("Set Sync type to “Full sync” in General to enable")
        sync_row = Adw.SwitchRow(title=_("Sync files offline"), subtitle=sub)
        sync_row.set_sensitive(full and master and account.signed_in)
        sync_row.set_active(bool(getattr(account, "full_sync", False)) and full and master)
        sync_row.connect("notify::active", self._on_sync_toggled, account)
        self._sync_rows.append((sync_row, account))
        return sync_row

    def _mount_location_row(self, account) -> Adw.ActionRow:
        individual = self._setting_str("mount-layout") == "individual"
        if individual:
            loc = account.mount_location or _("Default (%s)") % self._mount_location_display()
        else:
            loc = _("Set Mount layout to “Individual” in General to choose")
        row = Adw.ActionRow(title=_("Mount location"), subtitle=loc)
        row.set_sensitive(individual)
        choose = Gtk.Button(label=_("Choose…"), valign=Gtk.Align.CENTER)
        choose.connect("clicked", lambda *_: self._on_choose_account_location(account, row))
        row.add_suffix(choose)
        if individual and account.mount_location:
            clear = Gtk.Button(icon_name="edit-clear-symbolic", valign=Gtk.Align.CENTER,
                               tooltip_text=_("Use the default location"))
            clear.add_css_class("flat")
            clear.connect("clicked", lambda *_: self._set_account_location(account, "", row))
            row.add_suffix(clear)
        return row

    def _on_account_active(self, switch, _param, account) -> None:
        self._engine.set_enabled(account.module_id, switch.get_active())
        # Refresh the main window's sidebar so the on/off state shows there too.
        registry = getattr(self._app, "registry", None)
        if registry is not None:
            registry.emit("changed")

    def _account_action(self, method: str, account) -> None:
        window = self._app.props.active_window if self._app else None
        if window is not None and hasattr(window, method):
            getattr(window, method)(account)

    # -- per-account sync toggle -----------------------------------------
    def _on_sync_toggled(self, row, _param, account) -> None:
        enabled = row.get_active()
        account.full_sync = enabled
        if getattr(self._app, "registry", None) is not None:
            self._app.registry.update(account)
        manager = getattr(self._app, "sync_manager", None)
        if manager is None:
            return
        if enabled:
            manager.enable(account)
        else:
            manager.disable(account)

    # -- per-account mount location --------------------------------------
    def _on_choose_account_location(self, account, row) -> None:
        dialog = Gtk.FileDialog(title=_("Choose mount location for %s") % account.display_name)
        dialog.select_folder(
            self.get_root(), None,
            lambda d, r: self._on_account_location_chosen(d, r, account, row),
        )

    def _on_account_location_chosen(self, dialog, result, account, row) -> None:
        try:
            folder = dialog.select_folder_finish(result)
        except GLib.Error:
            return
        if folder is not None:
            self._set_account_location(account, folder.get_path(), row)

    def _set_account_location(self, account, path: str, row) -> None:
        account.mount_location = path
        if getattr(self._app, "registry", None) is not None:
            self._app.registry.update(account)
        row.set_subtitle(path or _("Default (%s)") % self._mount_location_display())

    # -- General settings handlers ---------------------------------------
    def _mount_location_display(self) -> str:
        loc = self._settings.get_string("mount-location")
        if not loc:
            loc = str(Path(GLib.get_user_data_dir()) / "cloudy" / "mounts")
        return loc

    def _on_choose_location(self, _button) -> None:
        dialog = Gtk.FileDialog(title=_("Choose mount location"))
        dialog.select_folder(self.get_root(), None, self._on_location_chosen)

    def _on_location_chosen(self, dialog, result) -> None:
        try:
            folder = dialog.select_folder_finish(result)
        except GLib.Error:
            return
        if folder is not None:
            self._settings.set_string("mount-location", folder.get_path())
            self._location_row.set_subtitle(self._mount_location_display())

    def _on_layout_changed(self, combo, _param) -> None:
        self._settings.set_string("mount-layout", self._layout_values[combo.get_selected()])
        self._rebuild_accounts()  # show/hide the per-account location rows

    def _on_cache_changed(self, combo, _param) -> None:
        self._settings.set_string("cache-mode", self._cache_values[combo.get_selected()])

    def _on_notify_level_changed(self, combo, _param) -> None:
        self._settings.set_string(
            "notify-level", self._notify_level_values[combo.get_selected()])

    def _time_row(self, title: str, key: str) -> Adw.EntryRow:
        """An HH:MM time entry that persists ``key`` only on a valid value (so a
        half-typed time never clobbers the setting), reformatted to zero-padded
        HH:MM for the lexical comparison the notifier does."""
        row = Adw.EntryRow(title=title)
        row.set_text(self._settings.get_string(key))
        row.connect("changed", self._on_time_changed, key)
        return row

    def _on_time_changed(self, row, key: str) -> None:
        m = _TIME_RE.match(row.get_text().strip())
        if m:
            self._settings.set_string(key, f"{int(m.group(1)):02d}:{m.group(2)}")

    def _on_sync_type_changed(self, combo, _param) -> None:
        self._settings.set_string("default-sync-type", self._sync_values[combo.get_selected()])
        self._rebuild_accounts()  # refresh the per-account sync toggles' state

    def _on_offline_sync_toggled(self, _switch, _param) -> None:
        self._rebuild_accounts()  # refresh the per-account sync toggles' state

    def _on_eds_toggled(self, switch, _param) -> None:
        """When the user enables the EDS mirror, backfill from cached events."""
        if switch.get_active() and self._app is not None:
            try:
                from .core.eds_publish import publish_all_cached_events

                publish_all_cached_events(self._app)
            except Exception:  # noqa: BLE001 - preferences must not break on EDS
                pass

    def _on_autostart_changed(self, switch, _param) -> None:
        enabled = switch.get_active()
        self._settings.set_boolean("autostart", enabled)
        _write_autostart(enabled)

    # -- settings helpers -------------------------------------------------
    def _setting_str(self, key: str) -> str:
        return self._settings.get_string(key)

    def _index_of(self, key: str, values: list) -> int:
        current = self._settings.get_string(key)
        return values.index(current) if current in values else 0


def _write_autostart(enabled: bool) -> None:
    """Create/remove a host autostart .desktop entry (rootless)."""
    path = Path(GLib.get_user_config_dir()) / "autostart" / "io.github.sha5b.Cloudy.desktop"
    try:
        if enabled:
            path.parent.mkdir(parents=True, exist_ok=True)
            # If the app's installed desktop entry is available, copy and adapt
            # it so the autostart file stays consistent with packaging.
            installed = Path(GLib.get_user_data_dir()).parent / "applications" / "io.github.sha5b.Cloudy.desktop"
            if installed.exists():
                text = installed.read_text(encoding="utf-8")
                text = text.replace("Exec=cloudy", "Exec=cloudy --gapplication-service")
                text += "X-GNOME-Autostart-enabled=true\n"
                path.write_text(text, encoding="utf-8")
                return
            path.write_text(
                "[Desktop Entry]\n"
                "Type=Application\n"
                "Name=Cloudy\n"
                "Comment=Cloudy background service\n"
                "Exec=cloudy --gapplication-service\n"
                "Icon=io.github.sha5b.Cloudy\n"
                "Categories=Network;Office;\n"
                "Terminal=false\n"
                "X-GNOME-Autostart-enabled=true\n",
                encoding="utf-8",
            )
        elif path.exists():
            path.unlink()
    except OSError:
        pass  # best-effort: autostart is not security-critical
