# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Shahab Nedaei
"""Standalone read-only message window.

Double-clicking a message in the mail list pops it out into its own top-level
window (the same idea as the compose/event windows) so you can read it
side-by-side with the list and other mail. Read-only: it reuses
``message_view.build_message_content`` for the body and supports opening
attachments, but edits/replies stay in the main window's reader.
"""

from __future__ import annotations

from gettext import gettext as _

from gi.repository import Adw, Gtk

from .format import esc
from .metrics import WIN_READ
from .source_nav import run_async


class MessageWindow(Adw.Window):
    def __init__(self, parent, account, mid):
        # Not transient_for: GNOME hides min/max on transient windows. As an
        # independent toplevel it can be parked/expanded like the editors.
        super().__init__(modal=False)
        self._parent = parent  # the main app window (clients, application)
        self._account = account
        self._mid = mid
        self.set_title(_("Message"))
        self.set_default_size(*WIN_READ)

        header = Adw.HeaderBar()
        header.set_decoration_layout(":minimize,maximize,close")

        self._scroll = Gtk.ScrolledWindow(
            hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        self._scroll.set_child(self._loading())

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(header)
        toolbar.set_content(self._scroll)
        self._toast = Adw.ToastOverlay(child=toolbar)
        self.set_content(self._toast)

        self._load()

    def _loading(self) -> Gtk.Widget:
        return Adw.StatusPage(
            title=_("Loading…"),
            child=Gtk.Spinner(spinning=True, width_request=32, height_request=32))

    # -- fetch ------------------------------------------------------------
    def _load(self) -> None:
        def work():
            from .clients import build_account_client

            client = build_account_client(
                self._parent.get_application(), self._account)
            return client.get_message(self._mid)

        run_async(work, self._on_loaded)

    def _on_loaded(self, msg, error) -> bool:
        if error or not msg:
            self._scroll.set_child(Adw.StatusPage(
                icon_name="dialog-error-symbolic",
                title=_("Couldn't open message"),
                description=esc(str(error or _("No data")))))
            return False
        from .message_view import build_message_content

        subject = msg.get("subject") or _("(no subject)")
        self.set_title(subject)
        # No RSVP bar in the popout — meeting replies stay in the main reader.
        self._scroll.set_child(build_message_content(
            msg, on_open_attachment=self._open_attachment))
        return False

    def _toast_msg(self, text: str) -> None:
        self._toast.add_toast(Adw.Toast(title=text))

    # -- attachments ------------------------------------------------------
    def _open_attachment(self, att) -> None:
        from .attachments import open_attachment

        open_attachment(self, self._account, self._mid, att, self._toast_msg)
