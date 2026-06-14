# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Fiber Elements
"""Mail surface for a signed-in Microsoft 365 account: the Inbox message list."""

from __future__ import annotations

import threading
from gettext import gettext as _

from gi.repository import Adw, GLib, Gtk


class MailView(Adw.Bin):
    __gtype_name__ = "ClouddriveMailView"

    def __init__(self, window, account):
        super().__init__()
        self._window = window
        self._account = account

        self._page = Adw.PreferencesPage()
        self.set_child(self._page)
        self._group = Adw.PreferencesGroup(title=_("Inbox"))
        self._page.add(self._group)
        self._loading = Adw.ActionRow(title=_("Loading mail…"))
        self._group.add(self._loading)

        self._load_async()

    def _load_async(self) -> None:
        def worker():
            try:
                from .clients import build_account_client

                client = build_account_client(
                    self._window.get_application(), self._account
                )
                messages = client.list_messages()
                GLib.idle_add(self._on_loaded, messages, None)
            except Exception as exc:  # noqa: BLE001
                GLib.idle_add(self._on_loaded, None, str(exc))

        threading.Thread(target=worker, daemon=True).start()

    def _on_loaded(self, messages, error) -> bool:
        self._group.remove(self._loading)
        if error:
            self._group.add(Adw.ActionRow(title=_("Couldn't load mail"), subtitle=error))
            return False
        if not messages:
            self._group.add(Adw.ActionRow(title=_("Your inbox is empty.")))
            return False
        for msg in messages:
            self._group.add(self._message_row(msg))
        return False

    def _message_row(self, msg) -> Adw.ActionRow:
        subtitle = msg["from"]
        when = _format_when(msg.get("received", ""))
        if when:
            subtitle = f"{subtitle} · {when}"
        row = Adw.ActionRow(title=msg["subject"] or _("(no subject)"), subtitle=subtitle)
        row.set_title_lines(1)
        row.set_subtitle_lines(1)
        # Unread messages get a leading accent dot.
        if not msg.get("is_read", True):
            dot = Gtk.Image.new_from_icon_name("media-record-symbolic")
            dot.add_css_class("accent")
            row.add_prefix(dot)
        row.add_suffix(Gtk.Image.new_from_icon_name("go-next-symbolic"))
        row.set_activatable(True)
        row.connect("activated", lambda *_: self._open_message(msg["id"]))
        return row

    def _open_message(self, message_id) -> None:
        def worker():
            try:
                from .clients import build_account_client

                client = build_account_client(
                    self._window.get_application(), self._account
                )
                full = client.get_message(message_id)
                GLib.idle_add(self._show_message, full, None)
            except Exception as exc:  # noqa: BLE001
                GLib.idle_add(self._show_message, None, str(exc))

        threading.Thread(target=worker, daemon=True).start()

    def _show_message(self, msg, error) -> bool:
        if error:
            self._window.add_toast(_("Couldn't open message: %s") % error)
            return False
        from .message_dialog import MessageDialog

        MessageDialog(msg).present(self._window)
        return False


def _format_when(iso: str) -> str:
    # Graph returns e.g. "2026-06-14T09:30:00Z"; show "2026-06-14 09:30".
    if not iso or "T" not in iso:
        return iso
    date, _, rest = iso.partition("T")
    return f"{date} {rest[:5]}"
