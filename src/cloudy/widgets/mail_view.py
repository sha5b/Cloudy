# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Fiber Elements
"""Mail surface: an email-style Inbox list (plain labels, so '&' etc. are safe)."""

from __future__ import annotations

import threading
from gettext import gettext as _

from gi.repository import Adw, GLib, Gtk, Pango

from .format import sender_name, short_time


class MailView(Adw.Bin):
    __gtype_name__ = "CloudyMailView"

    def __init__(self, window, account):
        super().__init__()
        self._window = window
        self._account = account

        scrolled = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        clamp = Adw.Clamp(maximum_size=760, margin_top=12, margin_bottom=12,
                          margin_start=12, margin_end=12)
        self._list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, valign=Gtk.Align.START)
        self._list.add_css_class("boxed-list")
        self._list.connect("row-activated", self._on_row_activated)
        clamp.set_child(self._list)
        scrolled.set_child(clamp)
        self.set_child(scrolled)

        self._has_data = False
        self._cache_key = f"{account.id}:messages:inbox"
        cached = self._window.get_application().cache.get(self._cache_key)
        if cached is not None:
            self._render(cached[0])  # show cached instantly
            if cached[1]:
                return  # fresh enough; skip the network round-trip
        else:
            self._set_placeholder(_("Loading mail…"))
        self._load_async()

    # -- helpers ----------------------------------------------------------
    def _clear(self) -> None:
        child = self._list.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self._list.remove(child)
            child = nxt

    def _set_placeholder(self, text: str) -> None:
        self._clear()
        row = Gtk.ListBoxRow(activatable=False)
        label = Gtk.Label(label=text, margin_top=18, margin_bottom=18)
        label.add_css_class("dim-label")
        row.set_child(label)
        self._list.append(row)

    # -- loading ----------------------------------------------------------
    def _load_async(self) -> None:
        def worker():
            try:
                from .clients import build_account_client

                client = build_account_client(self._window.get_application(), self._account)
                messages = client.list_messages()
                GLib.idle_add(self._on_loaded, messages, None)
            except Exception as exc:  # noqa: BLE001
                GLib.idle_add(self._on_loaded, None, str(exc))

        threading.Thread(target=worker, daemon=True).start()

    def _on_loaded(self, messages, error) -> bool:
        if error:
            # Keep any cached list on screen; only surface errors if we have none.
            if not self._has_data:
                self._set_placeholder(_("Couldn't load mail: %s") % error)
            return False
        self._window.get_application().cache.set(self._cache_key, messages)
        self._render(messages)
        return False

    def _render(self, messages) -> None:
        if not messages:
            self._set_placeholder(_("Your inbox is empty."))
            return
        self._clear()
        for msg in messages:
            self._list.append(self._mail_row(msg))
        self._has_data = True

    # -- a single email row (plain Gtk.Labels: no markup parsing) ---------
    def _mail_row(self, msg) -> Gtk.ListBoxRow:
        unread = not msg.get("is_read", True)
        sender = sender_name(msg.get("from", "")) or _("Unknown sender")
        subject = msg.get("subject") or _("(no subject)")
        preview = (msg.get("preview") or "").replace("\n", " ").strip()

        row = Gtk.ListBoxRow(activatable=True)
        row._mid = msg["id"]

        hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10,
                       margin_top=8, margin_bottom=8, margin_start=12, margin_end=12)
        row.set_child(hbox)

        dot = Gtk.Image.new_from_icon_name(
            "mail-unread-symbolic" if unread else "mail-read-symbolic"
        )
        dot.set_valign(Gtk.Align.CENTER)
        if not unread:
            dot.add_css_class("dim-label")
        hbox.append(dot)

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True)
        hbox.append(body)

        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        body.append(top)
        sender_lbl = Gtk.Label(label=sender, xalign=0, hexpand=True,
                               ellipsize=Pango.EllipsizeMode.END)
        sender_lbl.add_css_class("heading" if unread else "body")
        top.append(sender_lbl)
        time_lbl = Gtk.Label(label=short_time(msg.get("received", "")), xalign=1)
        time_lbl.add_css_class("dim-label")
        time_lbl.add_css_class("caption")
        top.append(time_lbl)

        subj_lbl = Gtk.Label(label=subject, xalign=0, ellipsize=Pango.EllipsizeMode.END)
        if unread:
            subj_lbl.add_css_class("heading")
        body.append(subj_lbl)

        if preview:
            prev_lbl = Gtk.Label(label=preview, xalign=0, ellipsize=Pango.EllipsizeMode.END)
            prev_lbl.add_css_class("dim-label")
            prev_lbl.add_css_class("caption")
            body.append(prev_lbl)

        if msg.get("important") or msg.get("starred"):
            flags = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2,
                            valign=Gtk.Align.CENTER)
            if msg.get("important"):
                flags.append(Gtk.Image.new_from_icon_name("mail-mark-important-symbolic"))
            if msg.get("starred"):
                flags.append(Gtk.Image.new_from_icon_name("starred-symbolic"))
            hbox.append(flags)

        return row

    # -- open a message ---------------------------------------------------
    def _on_row_activated(self, _list, row) -> None:
        mid = getattr(row, "_mid", None)
        if mid is None:
            return
        self._window.add_toast(_("Opening message…"))

        def worker():
            try:
                from .clients import build_account_client

                client = build_account_client(self._window.get_application(), self._account)
                full = client.get_message(mid)
                GLib.idle_add(self._show_message, full, None)
            except Exception as exc:  # noqa: BLE001
                GLib.idle_add(self._show_message, None, str(exc))

        threading.Thread(target=worker, daemon=True).start()

    def _show_message(self, msg, error) -> bool:
        if error:
            self._window.add_toast(_("Couldn't open message: %s") % error)
            return False
        from .message_view import make_message_page

        self._window.push_content(make_message_page(msg))
        return False
