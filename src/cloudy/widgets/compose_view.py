# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Shahab Nedaei
"""Compose / reply editor for the Mail surface.

A **non-modal window** (see ``editor_window.EditorWindow``) so you can copy from
other emails while writing. The To field autocompletes from the current
account's contacts (Microsoft Graph / Google People), matching name or email and
inserting ``Name <email>``; multiple comma-separated recipients are supported.

``send_fn(to, subject, body)`` does the actual send off-thread; the window closes
on success and toasts on failure.
"""

from __future__ import annotations

from email.utils import getaddresses
from gettext import gettext as _

from gi.repository import Gtk

from .editor_window import EditorWindow
from .source_nav import run_async


class ComposeWindow(EditorWindow):
    def __init__(self, window, account, *, from_label: str, send_fn,
                 to: str = "", subject: str = "", body: str = "",
                 title: str | None = None):
        super().__init__(window, title=title or _("New message"),
                         primary_label=_("Send"))
        self._window = window
        self._account = account
        self._send_fn = send_fn

        # From (read-only identity).
        from_lbl = Gtk.Label(label=_("From: %s") % from_label, xalign=0)
        from_lbl.add_css_class("dim-label")

        # To, with contacts autocomplete.
        self._to = Gtk.Entry(placeholder_text=_("Recipients (comma-separated)"),
                             hexpand=True)
        self._to.set_text(to)
        self._setup_completion()

        self._subject = Gtk.Entry(placeholder_text=_("Subject"), hexpand=True)
        self._subject.set_text(subject)

        self._body = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR,
                                  top_margin=10, bottom_margin=10,
                                  left_margin=12, right_margin=12)
        self._body.get_buffer().set_text(body)
        body_scroll = Gtk.ScrolledWindow(vexpand=True, hexpand=True,
                                         hscrollbar_policy=Gtk.PolicyType.NEVER,
                                         child=self._body)
        body_scroll.add_css_class("card")

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8,
                      margin_top=12, margin_bottom=12, margin_start=12, margin_end=12)
        box.append(from_lbl)
        box.append(self._field(_("To"), self._to))
        box.append(self._field(_("Subject"), self._subject))
        box.append(body_scroll)
        self.set_body(box)

        self._load_contacts()
        focus = self._body if (to and subject) else self._to
        self.connect("map", lambda *_a: focus.grab_focus())

    @staticmethod
    def _field(label: str, widget: Gtk.Widget) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        lbl = Gtk.Label(label=label, xalign=1, width_chars=7)
        lbl.add_css_class("dim-label")
        box.append(lbl)
        box.append(widget)
        return box

    # -- contacts autocomplete -------------------------------------------
    def _setup_completion(self) -> None:
        self._store = Gtk.ListStore(str, str)  # (label, lowercase search key)
        completion = Gtk.EntryCompletion(model=self._store)
        renderer = Gtk.CellRendererText()
        completion.pack_start(renderer, True)
        completion.add_attribute(renderer, "text", 0)
        completion.set_match_func(self._match)
        completion.set_minimum_key_length(1)
        completion.set_popup_completion(True)
        completion.set_popup_single_match(True)
        completion.connect("match-selected", self._on_match_selected)
        self._to.set_completion(completion)

    def _last_token(self) -> str:
        return self._to.get_text().split(",")[-1].strip().lower()

    def _match(self, _completion, _key, tree_iter) -> bool:
        token = self._last_token()
        if not token:
            return False
        return token in self._store.get_value(tree_iter, 1)

    def _on_match_selected(self, _completion, model, tree_iter) -> bool:
        label = model.get_value(tree_iter, 0)
        parts = [p.strip() for p in self._to.get_text().split(",")[:-1] if p.strip()]
        self._to.set_text(", ".join(parts + [label]) + ", ")
        self._to.set_position(-1)
        return True

    def _load_contacts(self) -> None:
        app = self._window.get_application()
        key = f"{self._account.id}:contacts"
        cached = app.cache.get(key)
        if cached is not None:
            self._fill_contacts(cached[0])
            if cached[1]:
                return

        def work():
            from .clients import build_account_client

            client = build_account_client(app, self._account)
            return client.list_contacts()

        run_async(work, lambda res, err: self._on_contacts(key, res, err))

    def _on_contacts(self, key, contacts, error) -> bool:
        if error or contacts is None:
            return False  # contacts are a nicety; ignore failures (e.g. scope)
        self._window.get_application().cache.set(key, contacts)
        self._fill_contacts(contacts)
        return False

    def _fill_contacts(self, contacts) -> None:
        self._store.clear()
        seen = set()
        for c in contacts:
            email = c.get("email")
            if not email or email in seen:
                continue
            seen.add(email)
            name = c.get("name") or ""
            label = f"{name} <{email}>" if name else email
            self._store.append([label, f"{name} {email}".strip().lower()])

    # -- send -------------------------------------------------------------
    def on_primary(self) -> None:
        recipients = [e for _n, e in getaddresses([self._to.get_text()]) if e]
        if not recipients:
            self.toast(_("Add at least one recipient."))
            return
        subject = self._subject.get_text().strip()
        buf = self._body.get_buffer()
        body = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), True)

        self.primary_btn.set_sensitive(False)
        self.toast(_("Sending…"))
        send_fn = self._send_fn
        run_async(lambda: send_fn(recipients, subject, body), self._on_sent)

    def _on_sent(self, _result, error) -> bool:
        if error:
            self.primary_btn.set_sensitive(True)
            self.toast(_("Couldn't send: %s") % error)
            return False
        self._window.add_toast(_("Message sent."))
        self.close()
        return False
