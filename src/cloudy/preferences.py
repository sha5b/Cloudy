# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Fiber Elements
"""Preferences dialog: the module manager lives here."""

from gi.repository import Adw, Gtk

RESOURCE_PREFIX = "/com/fiberelements/Cloudy"


@Gtk.Template(resource_path=f"{RESOURCE_PREFIX}/ui/preferences.ui")
class CloudyPreferences(Adw.PreferencesDialog):
    __gtype_name__ = "CloudyPreferences"

    modules_group = Gtk.Template.Child()

    def __init__(self, engine, **kwargs):
        super().__init__(**kwargs)
        self._engine = engine
        self._populate_modules()

    def _populate_modules(self) -> None:
        for module in self._engine.modules():
            row = Adw.SwitchRow(title=module.name, subtitle=module.id)
            row.set_active(self._engine.is_enabled(module.id))
            row.connect(
                "notify::active",
                lambda r, _p, mid=module.id: self._engine.set_enabled(
                    mid, r.get_active()
                ),
            )
            self.modules_group.add(row)
