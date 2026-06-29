# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Shahab Nedaei
"""Shared mail-attachment opening, used by both the mail reader pane and the
pop-out message window."""

from __future__ import annotations

from gettext import gettext as _
from typing import Callable

from gi.repository import GLib, Gtk

from .source_nav import run_async


def open_attachment(parent: Gtk.Window, account, mid: str, att: dict,
                    toast: Callable[[str], None]) -> None:
    """Fetch the attachment's bytes off-thread, then show images in a viewer
    window and offer to save anything else. ``parent`` roots the viewer/save
    dialogs; ``toast(text)`` reports progress and errors."""
    if not mid or not att.get("id"):
        return
    name = att.get("name") or _("attachment")
    toast(_("Opening %s…") % name)

    def work():
        from .clients import build_account_client

        client = build_account_client(parent.get_application(), account)
        return client.fetch_mail_attachment(mid, att["id"])

    def done(data, error):
        if error or not data:
            toast(_("Couldn't open attachment: %s") % (error or _("no data")))
            return False
        if (att.get("content_type") or "").lower().startswith("image"):
            from .media_window import ImageWindow

            ImageWindow(parent, data, name).present()
        else:
            _save(parent, toast, data, name)
        return False

    run_async(work, done)


def save_bytes_dialog(parent: Gtk.Window, data: bytes, name: str,
                      on_done: Callable[[str | None], None]) -> None:
    """Show a save dialog for ``data`` and write it when the user confirms.

    ``on_done(message)`` is called with ``None`` on success or an error string.
    """
    from .source_nav import local_initial_folder

    dialog = Gtk.FileDialog(title=_("Save"), initial_name=name)
    folder = local_initial_folder()
    if folder is not None:
        dialog.set_initial_folder(folder)

    def _on_saved(d, result):
        try:
            gfile = d.save_finish(result)
        except GLib.Error:
            on_done(None)  # user cancelled
            return
        if gfile is None:
            on_done(None)
            return
        try:
            from gi.repository import Gio

            gfile.replace_contents(data, None, False,
                                   Gio.FileCreateFlags.NONE, None)
            on_done(None)
        except GLib.Error as exc:
            on_done(exc.message)

    dialog.save(parent, None, _on_saved)


def _save(parent: Gtk.Window, toast: Callable[[str], None], data, name) -> None:
    def on_done(error):
        if error:
            toast(_("Couldn't save: %s") % error)
        else:
            toast(_("Saved"))

    save_bytes_dialog(parent, data, name, on_done)
