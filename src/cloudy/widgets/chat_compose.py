# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Shahab Nedaei
"""New-chat composer for the Chat surface.

A **non-modal window** (``editor_window.EditorWindow``), matching the Mail/Event
windows. The To field autocompletes from the account's contacts (people you
work with), the same source the mail composer uses, and accepts **several**
recipients (comma/semicolon separated) to start a group chat — with an optional
group name. ``create_fn(recipients, topic, text)`` starts the chat off-thread
and returns the new chat id; the window closes on success and calls
``on_created(chat_id)``.
"""

from __future__ import annotations

import re
from email.utils import getaddresses
from gettext import gettext as _

from gi.repository import Gtk

from .editor_window import EditorWindow
from .source_nav import run_async


class ChatComposeWindow(EditorWindow):
    def __init__(self, window, account, *, create_fn, on_created=None, body=""):
        super().__init__(window, title=_("New chat"), primary_label=_("Start chat"))
        self._window = window
        self._account = account
        self._create_fn = create_fn
        self._on_created = on_created
        self._initial_body = body

        self._to = Gtk.Entry(
            placeholder_text=_("Names or emails — separate with commas"),
            hexpand=True)
        self._setup_completion()
        self._topic = Gtk.Entry(
            placeholder_text=_("Group name (optional, for 3+ people)"),
            hexpand=True)

        self._body = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR,
                                  top_margin=10, bottom_margin=10,
                                  left_margin=12, right_margin=12)
        if self._initial_body:
            self._body.get_buffer().set_text(self._initial_body)
        body_scroll = Gtk.ScrolledWindow(vexpand=True, hexpand=True,
                                         hscrollbar_policy=Gtk.PolicyType.NEVER,
                                         child=self._body)
        body_scroll.add_css_class("card")

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8,
                      margin_top=12, margin_bottom=12, margin_start=12, margin_end=12)
        box.append(self._field(_("To"), self._to))
        box.append(self._field(_("Name"), self._topic))
        box.append(body_scroll)
        self.set_body(box)

        self._load_contacts()
        self.connect("map", lambda *_a: self._to.grab_focus())

    @staticmethod
    def _field(label: str, widget: Gtk.Widget) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        lbl = Gtk.Label(label=label, xalign=1, width_chars=7)
        lbl.add_css_class("dim-label")
        box.append(lbl)
        box.append(widget)
        return box

    # -- contacts autocomplete (several recipients) ----------------------
    # The To field accepts several comma/semicolon-separated recipients, so both
    # matching and selection operate on the *last* segment only — otherwise the
    # already-typed recipients swallow the search key (nothing pops up for the
    # 2nd person) and picking a match would replace everyone typed so far.
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
        """The text after the last comma/semicolon — what the user is typing now."""
        return re.split(r"[,;]", self._to.get_text())[-1].strip().lower()

    def _match(self, _completion, _key, tree_iter) -> bool:
        token = self._last_token()
        if not token:
            return False
        return token in self._store.get_value(tree_iter, 1)

    def _on_match_selected(self, _completion, model, tree_iter) -> bool:
        label = model.get_value(tree_iter, 0)
        text = self._to.get_text()
        # Replace only the segment being typed; keep earlier recipients and leave
        # a trailing ", " so the next name can be typed straight away.
        head = re.match(r"^(.*[,;])\s*[^,;]*$", text)
        self._to.set_text(f"{head.group(1)} {label}, " if head else f"{label}, ")
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
            return False  # autocomplete is a nicety; ignore failures
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

    # -- create -----------------------------------------------------------
    def on_primary(self) -> None:
        # De-duplicate while preserving order (a group chat with the same person
        # twice is rejected by Graph).
        recipients = list(dict.fromkeys(
            e for _n, e in getaddresses([self._to.get_text()]) if e))
        if not recipients:
            self.toast(_("Pick someone to chat with."))
            return
        buf = self._body.get_buffer()
        text = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), True).strip()
        if not text:
            self.toast(_("Write a message to start the chat."))
            return
        topic = self._topic.get_text().strip()
        self.primary_btn.set_sensitive(False)
        self.toast(_("Starting…"))
        create_fn = self._create_fn
        run_async(lambda: create_fn(recipients, topic, text),
                  self._on_created_result)

    def _on_created_result(self, chat_id, error) -> bool:
        if error:
            self.primary_btn.set_sensitive(True)
            self.toast(_("Couldn't start chat: %s") % error)
            return False
        self._window.add_toast(_("Chat started."))
        if self._on_created is not None:
            self._on_created(chat_id)
        self.close()
        return False
