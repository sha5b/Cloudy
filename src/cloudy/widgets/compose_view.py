# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Shahab Nedaei
"""Compose / reply editor for the Mail surface.

A **non-modal window** (see ``editor_window.EditorWindow``) so you can copy from
other emails while writing. The To/Cc/Bcc fields autocomplete from the current
account's contacts (Microsoft Graph / Google People), matching name or email and
inserting ``Name <email>``; multiple comma-separated recipients are supported.

The body is a :class:`~cloudy.widgets.rich_editor.RichTextEditor` (formatting +
inline images), serialized to HTML on send. File attachments show as removable
chips, and a toggle marks the message high-importance.

``send_fn(to, subject, body_html, *, cc, bcc, attachments, importance)`` does the
actual send off-thread; the window closes on success and toasts on failure.
"""

from __future__ import annotations

from email.utils import getaddresses
from gettext import gettext as _

from gi.repository import GLib, Gtk, Pango

from .editor_window import EditorWindow
from .format import esc
from .rich_editor import RichTextEditor
from .source_nav import local_initial_folder, run_async


class ComposeWindow(EditorWindow):
    def __init__(self, window, account, *, from_label: str, send_fn,
                 to: str = "", subject: str = "", body: str = "",
                 cc: str = "", bcc: str = "",
                 title: str | None = None, draft_fn=None):
        super().__init__(window, title=title or _("New message"),
                         primary_label=_("Send"))
        self._window = window
        self._account = account
        self._send_fn = send_fn
        self._draft_fn = draft_fn
        self._attachments: list[dict] = []  # [{name, content_type, data, widget}]
        if draft_fn is not None:
            self._draft_btn = self.add_secondary(_("Save draft"),
                                                 self._on_save_draft)

        # From (read-only identity).
        from_lbl = Gtk.Label(label=_("From: %s") % from_label, xalign=0)
        from_lbl.add_css_class("dim-label")

        # Recipients, each with contacts autocomplete sharing one model.
        self._store = Gtk.ListStore(str, str)  # (label, lowercase search key)
        self._to = self._recipient_entry(_("Recipients (comma-separated)"))
        self._to.set_text(to)
        self._cc = self._recipient_entry(_("Cc (comma-separated)"))
        self._bcc = self._recipient_entry(_("Bcc (comma-separated)"))

        # Cc/Bcc start hidden behind toggles next to the To field.
        self._cc_revealer = Gtk.Revealer(child=self._field(_("Cc"), self._cc))
        self._bcc_revealer = Gtk.Revealer(child=self._field(_("Bcc"), self._bcc))
        cc_toggle = Gtk.ToggleButton(label=_("Cc"))
        cc_toggle.add_css_class("flat")
        cc_toggle.connect("toggled",
                          lambda b: self._cc_revealer.set_reveal_child(b.get_active()))
        bcc_toggle = Gtk.ToggleButton(label=_("Bcc"))
        bcc_toggle.add_css_class("flat")
        bcc_toggle.connect("toggled",
                           lambda b: self._bcc_revealer.set_reveal_child(b.get_active()))
        # Prefill (e.g. reopening a draft) — reveal the fields so nothing the
        # user previously typed is hidden, and later silently dropped on send.
        if cc:
            self._cc.set_text(cc)
            cc_toggle.set_active(True)
        if bcc:
            self._bcc.set_text(bcc)
            bcc_toggle.set_active(True)
        to_row = self._field(_("To"), self._to)
        to_row.append(cc_toggle)
        to_row.append(bcc_toggle)

        self._subject = Gtk.Entry(placeholder_text=_("Subject"), hexpand=True)
        self._subject.set_text(subject)

        self._editor = RichTextEditor()
        if body:
            self._editor.set_plain_text(body)

        # Attachment chips (revealed when there's at least one).
        self._chips = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE,
                                  max_children_per_line=3, column_spacing=6,
                                  row_spacing=6, margin_top=4)
        self._chips_revealer = Gtk.Revealer(child=self._chips)

        # Footer actions: attach files + high importance.
        attach_btn = Gtk.Button(icon_name="mail-attachment-symbolic")
        attach_btn.set_child(self._btn_content("mail-attachment-symbolic",
                                               _("Attach files")))
        attach_btn.connect("clicked", self._on_attach)
        self._importance = Gtk.ToggleButton(
            tooltip_text=_("Mark as high importance"))
        self._importance.set_child(self._btn_content("mail-mark-important-symbolic",
                                                     _("High importance")))
        # Read receipt asks the recipient's client to confirm they opened it. Only
        # Microsoft honours this for us (Graph isReadReceiptRequested); consumer
        # Gmail has no API for it, so the toggle is hidden for Google accounts.
        self._read_receipt = Gtk.ToggleButton(
            tooltip_text=_("Request a read receipt"))
        self._read_receipt.set_child(self._btn_content("emblem-ok-symbolic",
                                                       _("Read receipt")))
        self._read_receipt.set_visible(getattr(account, "provider", "") == "microsoft")
        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8,
                          margin_top=4)
        actions.append(attach_btn)
        actions.append(self._importance)
        actions.append(self._read_receipt)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8,
                      margin_top=12, margin_bottom=12, margin_start=12, margin_end=12)
        box.append(from_lbl)
        box.append(to_row)
        box.append(self._cc_revealer)
        box.append(self._bcc_revealer)
        box.append(self._field(_("Subject"), self._subject))
        box.append(self._editor)
        box.append(self._chips_revealer)
        box.append(actions)
        self.set_body(box)

        self._load_contacts()
        focus = self._editor if (to and subject) else self._to
        self.connect("map", lambda *_a: focus.grab_focus())

    @staticmethod
    def _field(label: str, widget: Gtk.Widget) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        lbl = Gtk.Label(label=label, xalign=1, width_chars=7)
        lbl.add_css_class("dim-label")
        box.append(lbl)
        box.append(widget)
        return box

    @staticmethod
    def _btn_content(icon: str, label: str) -> Gtk.Widget:
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        row.append(Gtk.Image.new_from_icon_name(icon))
        row.append(Gtk.Label(label=label))
        return row

    def _recipient_entry(self, placeholder: str) -> Gtk.Entry:
        entry = Gtk.Entry(placeholder_text=placeholder, hexpand=True)
        completion = Gtk.EntryCompletion(model=self._store)
        renderer = Gtk.CellRendererText()
        completion.pack_start(renderer, True)
        completion.add_attribute(renderer, "text", 0)
        completion.set_match_func(self._match, entry)
        completion.set_minimum_key_length(1)
        completion.set_popup_completion(True)
        completion.set_popup_single_match(True)
        completion.connect("match-selected", self._on_match_selected, entry)
        entry.set_completion(completion)
        return entry

    # -- contacts autocomplete -------------------------------------------
    @staticmethod
    def _last_token(entry: Gtk.Entry) -> str:
        return entry.get_text().split(",")[-1].strip().lower()

    def _match(self, _completion, _key, tree_iter, entry) -> bool:
        token = self._last_token(entry)
        if not token:
            return False
        return token in self._store.get_value(tree_iter, 1)

    def _on_match_selected(self, _completion, model, tree_iter, entry) -> bool:
        label = model.get_value(tree_iter, 0)
        parts = [p.strip() for p in entry.get_text().split(",")[:-1] if p.strip()]
        entry.set_text(", ".join(parts + [label]) + ", ")
        entry.set_position(-1)
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

    # -- attachments ------------------------------------------------------
    def _on_attach(self, _btn) -> None:
        dialog = Gtk.FileDialog(title=_("Attach files"))
        folder = local_initial_folder()
        if folder is not None:
            dialog.set_initial_folder(folder)
        dialog.open_multiple(self, None, self._on_attach_chosen)

    def _on_attach_chosen(self, dialog, result) -> None:
        try:
            files = dialog.open_multiple_finish(result)
        except GLib.Error:
            return
        if files is None:
            return
        for i in range(files.get_n_items()):
            self._add_attachment(files.get_item(i))

    def _add_attachment(self, gfile) -> None:
        try:
            ok, data, _etag = gfile.load_contents(None)
            if not ok:
                return
            name = gfile.get_basename() or _("attachment")
            ctype = "application/octet-stream"
            info = gfile.query_info("standard::content-type", 0, None)
            if info and info.get_content_type():
                ctype = info.get_content_type()
        except GLib.Error as exc:
            self.toast(_("Couldn't read file: %s") % exc.message)
            return
        entry = {"name": name, "content_type": ctype, "data": bytes(data)}
        chip = self._chip(entry)
        entry["widget"] = chip
        self._attachments.append(entry)
        self._chips.append(chip)
        self._chips_revealer.set_reveal_child(True)

    def _chip(self, entry) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        box.add_css_class("card")
        box.set_margin_top(2)
        inner = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6,
                        margin_top=4, margin_bottom=4, margin_start=8, margin_end=4)
        inner.append(Gtk.Image.new_from_icon_name("mail-attachment-symbolic"))
        inner.append(Gtk.Label(label=esc(entry["name"]),
                               ellipsize=Pango.EllipsizeMode.MIDDLE,
                               max_width_chars=22))
        remove = Gtk.Button(icon_name="window-close-symbolic")
        remove.add_css_class("flat")
        remove.connect("clicked", lambda *_a: self._remove_attachment(entry))
        inner.append(remove)
        box.append(inner)
        return box

    def _remove_attachment(self, entry) -> None:
        if entry in self._attachments:
            self._attachments.remove(entry)
        child = entry.get("widget")
        if child is not None:
            # FlowBox wraps each child in a FlowBoxChild.
            parent = child.get_parent()
            self._chips.remove(parent if isinstance(parent, Gtk.FlowBoxChild) else child)
        self._chips_revealer.set_reveal_child(bool(self._attachments))

    # -- send / save draft -------------------------------------------------
    def _collect(self):
        """Gather the message fields as ``(to, cc, bcc, subject, body_html,
        attachments)`` — shared by Send and Save draft."""
        recipients = [e for _n, e in getaddresses([self._to.get_text()]) if e]
        cc = [e for _n, e in getaddresses([self._cc.get_text()]) if e]
        bcc = [e for _n, e in getaddresses([self._bcc.get_text()]) if e]
        subject = self._subject.get_text().strip()
        body_html, inline = self._editor.get_html()
        # File attachments + the editor's inline images (cid-referenced).
        attachments = [
            {"name": a["name"], "content_type": a["content_type"], "data": a["data"]}
            for a in self._attachments
        ] + [
            {"name": "image", "content_type": img["content_type"], "data": img["data"],
             "inline": True, "content_id": img["content_id"]}
            for img in inline
        ]
        return recipients, cc, bcc, subject, body_html, attachments

    def on_primary(self) -> None:
        recipients, cc, bcc, subject, body_html, attachments = self._collect()
        if not recipients:
            self.toast(_("Add at least one recipient."))
            return
        importance = "high" if self._importance.get_active() else "normal"
        read_receipt = self._read_receipt.get_active()

        self.primary_btn.set_sensitive(False)
        self.toast(_("Sending…"))
        send_fn = self._send_fn
        run_async(
            lambda: send_fn(recipients, subject, body_html, cc=cc, bcc=bcc,
                            attachments=attachments, importance=importance,
                            read_receipt=read_receipt),
            self._on_sent)

    def _on_save_draft(self) -> None:
        recipients, cc, bcc, subject, body_html, attachments = self._collect()
        self._draft_btn.set_sensitive(False)
        self.toast(_("Saving draft…"))
        draft_fn = self._draft_fn
        run_async(
            lambda: draft_fn(recipients, subject, body_html, cc=cc, bcc=bcc,
                             attachments=attachments),
            self._on_draft_saved)

    def _on_draft_saved(self, _result, error) -> bool:
        if error:
            self._draft_btn.set_sensitive(True)
            self.toast(_("Couldn't save draft: %s") % error)
            return False
        self._window.add_toast(_("Draft saved."))
        self.close()
        return False

    def _on_sent(self, _result, error) -> bool:
        if error:
            self.primary_btn.set_sensitive(True)
            self.toast(_("Couldn't send: %s") % error)
            return False
        self._window.add_toast(_("Message sent."))
        self.close()
        return False
