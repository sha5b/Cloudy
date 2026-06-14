# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Fiber Elements
"""Preferences: General (mount location, cache mode, autostart) + Modules."""

from gettext import gettext as _
from pathlib import Path

from gi.repository import Adw, Gio, GLib, Gtk


class CloudyPreferences(Adw.PreferencesDialog):
    __gtype_name__ = "CloudyPreferences"

    def __init__(self, engine, settings: Gio.Settings, **kwargs):
        super().__init__(**kwargs)
        self.set_title(_("Preferences"))
        self._engine = engine
        self._settings = settings

        self.add(self._general_page())
        self.add(self._modules_page())

    # -- General ----------------------------------------------------------
    def _general_page(self) -> Adw.PreferencesPage:
        page = Adw.PreferencesPage(title=_("General"), icon_name="emblem-system-symbolic")

        files = Adw.PreferencesGroup(
            title=_("Files"),
            description=_("Where mounted libraries live and how they are cached."),
        )
        page.add(files)

        # Mount location
        self._location_row = Adw.ActionRow(title=_("Mount location"))
        self._location_row.set_subtitle(self._mount_location_display())
        choose = Gtk.Button(label=_("Choose…"), valign=Gtk.Align.CENTER)
        choose.connect("clicked", self._on_choose_location)
        self._location_row.add_suffix(choose)
        files.add(self._location_row)

        # Cache mode
        cache = Adw.ComboRow(title=_("File caching"))
        model = Gtk.StringList()
        self._cache_values = ["full", "minimal"]
        model.append(_("On-demand (cache opened files)"))
        model.append(_("Streaming (minimal disk use)"))
        cache.set_model(model)
        current = self._settings.get_string("cache-mode")
        cache.set_selected(self._cache_values.index(current) if current in self._cache_values else 0)
        cache.connect("notify::selected", self._on_cache_changed)
        files.add(cache)

        startup = Adw.PreferencesGroup(title=_("Startup"))
        page.add(startup)
        autostart = Adw.SwitchRow(
            title=_("Start at login"),
            subtitle=_("Keep mounts and sync available in the background."),
        )
        autostart.set_active(self._settings.get_boolean("autostart"))
        autostart.connect("notify::active", self._on_autostart_changed)
        startup.add(autostart)

        return page

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

    def _on_cache_changed(self, combo, _param) -> None:
        self._settings.set_string("cache-mode", self._cache_values[combo.get_selected()])

    def _on_autostart_changed(self, switch, _param) -> None:
        enabled = switch.get_active()
        self._settings.set_boolean("autostart", enabled)
        _write_autostart(enabled)

    # -- Modules ----------------------------------------------------------
    def _modules_page(self) -> Adw.PreferencesPage:
        page = Adw.PreferencesPage(title=_("Modules"), icon_name="puzzle-piece-symbolic")
        group = Adw.PreferencesGroup(
            title=_("Service Modules"),
            description=_("Enable the services you want Cloudy to manage."),
        )
        page.add(group)
        for module in self._engine.modules():
            row = Adw.SwitchRow(title=module.name, subtitle=module.id)
            row.set_active(self._engine.is_enabled(module.id))
            row.connect(
                "notify::active",
                lambda r, _p, mid=module.id: self._engine.set_enabled(mid, r.get_active()),
            )
            group.add(row)
        return page


def _write_autostart(enabled: bool) -> None:
    """Create/remove a host autostart .desktop entry (rootless)."""
    path = Path(GLib.get_user_config_dir()) / "autostart" / "com.fiberelements.Cloudy.desktop"
    if enabled:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "[Desktop Entry]\n"
            "Type=Application\n"
            "Name=Cloudy\n"
            "Exec=cloudy --gapplication-service\n"
            "X-GNOME-Autostart-enabled=true\n"
        )
    elif path.exists():
        path.unlink()
