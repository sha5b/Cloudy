# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Fiber Elements
"""Add-account dialog: pick a provider discovered by the plugin engine.

The provider list is built from the modules (one provider = one account = one
login). Choosing a provider creates a pending account in the registry; the
actual OAuth sign-in is triggered from the account view (stage 2).
"""

from gettext import gettext as _

from gi.repository import Adw, Gtk

from .core.account_registry import Account


class AddAccountDialog(Adw.Dialog):
    __gtype_name__ = "CloudyAddAccountDialog"

    def __init__(self, *, engine, registry, on_added=None):
        super().__init__(title=_("Add Account"), content_width=420)
        self._engine = engine
        self._registry = registry
        self._on_added = on_added

        page = Adw.PreferencesPage()
        group = Adw.PreferencesGroup(
            title=_("Choose a provider"),
            description=_("You will sign in once; the account provides all its services."),
        )
        page.add(group)

        for module in self._engine.modules():
            row = Adw.ActionRow(title=module.name, subtitle=self._provider_subtitle(module))
            row.add_prefix(Gtk.Image.new_from_icon_name(module.icon_name))
            row.add_suffix(Gtk.Image.new_from_icon_name("go-next-symbolic"))
            row.set_activatable(True)
            row.connect("activated", self._on_provider_chosen, module)
            group.add(row)

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())
        toolbar.set_content(page)
        self.set_child(toolbar)

    def _provider_subtitle(self, module) -> str:
        from .core.interfaces import capabilities_of

        caps = capabilities_of(module)
        labels = {"files": _("Files"), "mail": _("Mail"), "calendar": _("Calendar")}
        return " · ".join(labels.get(c, c) for c in caps)

    def _on_provider_chosen(self, _row, module) -> None:
        account = Account(
            id=self._registry.new_id(module.provider or module.id),
            display_name=module.name,
            provider=module.provider or module.id,
            module_id=module.id,
            signed_in=False,
        )
        self._registry.add(account)
        if self._on_added is not None:
            self._on_added(account)
        self.close()
