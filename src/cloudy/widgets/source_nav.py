# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Shahab Nedaei
"""Shared building blocks for the Mail and Calendar surfaces.

Both are two-pane views over a Microsoft account's **Me / Teams / Shared**
sources, so they'd otherwise duplicate the same scaffolding. This module holds
the reusable pieces: the source toggle tabs, listbox placeholder rows, the
shared-mailbox add dialog, and scope-error detection.
"""

from __future__ import annotations

import threading
from gettext import gettext as _
from typing import Callable

from gi.repository import Adw, GLib, Gtk

from .metrics import ICON_LG, SPACE_L, SPACE_M


# -- background work ------------------------------------------------------
def run_async(work: Callable[[], object], on_done: Callable[[object, str | None], object]
              ) -> None:
    """Run ``work()`` on a daemon thread and deliver its outcome to
    ``on_done(result, error)`` back on the GTK main loop.

    Any exception becomes the ``error`` string (and ``result`` is ``None``).
    This is the one place the views' "fetch off-thread, render on-thread"
    pattern lives.
    """
    def worker():
        try:
            result = work()
            GLib.idle_add(on_done, result, None)
        except Exception as exc:  # noqa: BLE001 - surfaced to the UI as a string
            GLib.idle_add(on_done, None, str(exc))

    threading.Thread(target=worker, daemon=True).start()


# -- listbox helpers ------------------------------------------------------
def clear_listbox(listbox: Gtk.ListBox) -> None:
    """Remove every row from a ``Gtk.ListBox``."""
    child = listbox.get_first_child()
    while child is not None:
        nxt = child.get_next_sibling()
        listbox.remove(child)
        child = nxt


def message_row(text: str) -> Gtk.ListBoxRow:
    """A non-interactive placeholder row with a dimmed, centered label."""
    row = Gtk.ListBoxRow(activatable=False, selectable=False)
    label = Gtk.Label(label=text, margin_top=SPACE_L, margin_bottom=SPACE_L,
                      wrap=True, justify=Gtk.Justification.CENTER)
    label.add_css_class("dim-label")
    row.set_child(label)
    return row


def status_page(icon: str, title: str, description: str | None = None
                ) -> Adw.StatusPage:
    """The one empty/error-state widget for the whole app. Use this instead of
    hand-rolling icon+title boxes so every "nothing here" / "couldn't load"
    surface looks the same."""
    page = Adw.StatusPage(icon_name=icon, title=title, vexpand=True)
    if description:
        page.set_description(description)
    return page


def loading_box(text: str | None = None) -> Gtk.Widget:
    """The one loading widget for the whole app: a centered spinner with an
    optional dimmed caption."""
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=SPACE_M,
                  halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER,
                  hexpand=True, vexpand=True)
    spinner = Gtk.Spinner(width_request=ICON_LG, height_request=ICON_LG)
    spinner.start()
    box.append(spinner)
    if text:
        label = Gtk.Label(label=text)
        label.add_css_class("dim-label")
        box.append(label)
    return box


def action_row(text: str, button_label: str, on_click: Callable[[], None]
               ) -> Gtk.ListBoxRow:
    """A placeholder row with a call-to-action button (e.g. re-sign-in)."""
    row = Gtk.ListBoxRow(activatable=False, selectable=False)
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=SPACE_M,
                  margin_top=SPACE_L, margin_bottom=SPACE_L,
                  margin_start=SPACE_M, margin_end=SPACE_M)
    label = Gtk.Label(label=text, wrap=True, justify=Gtk.Justification.CENTER)
    label.add_css_class("dim-label")
    box.append(label)
    btn = Gtk.Button(label=button_label, halign=Gtk.Align.CENTER)
    btn.add_css_class("pill")
    btn.add_css_class("suggested-action")
    btn.connect("clicked", lambda *_a: on_click())
    box.append(btn)
    row.set_child(box)
    return row


# A token lacks a scope the call needed — the account signed in before that
# scope was consented (e.g. shared-mailbox access). Both REST clients raise this
# exact phrase when the token provider returns nothing.
SCOPE_HINT = _(
    "Cloudy needs permission for shared mailboxes and team calendars. Re-sign "
    "in to grant access — everything else keeps working."
)


def is_scope_error(error: str | None) -> bool:
    """True when a call failed for lack of a required scope (vs. a real error)."""
    return bool(error) and "requested scopes" in error


# -- pinned (starred) sources ---------------------------------------------
# A pin marks a whole shared/group mailbox or calendar as a favorite. Each entry
# is {"kind": "mail"|"calendar", "source": "shared"|"teams", "id": str,
# "name": str}; pins surface on the Dashboard.
def find_pin(account, kind: str, source: str, sid: str):
    for p in account.pinned_sources or []:
        if p.get("kind") == kind and p.get("source") == source and p.get("id") == sid:
            return p
    return None


def is_pinned(account, kind: str, source: str, sid: str) -> bool:
    return find_pin(account, kind, source, sid) is not None


def toggle_pin(window, account, *, kind: str, source: str, sid: str, name: str) -> bool:
    """Add/remove a pin for a source; persist and return the new pinned state."""
    pins = list(account.pinned_sources or [])
    existing = find_pin(account, kind, source, sid)
    if existing:
        pins = [p for p in pins if p is not existing]
        pinned = False
    else:
        pins.append({"kind": kind, "source": source, "id": sid, "name": name})
        pinned = True
    account.pinned_sources = pins
    window.get_application().registry.update(account)
    return pinned


# -- source tabs (Me / Teams / Shared) ------------------------------------
class SourceTabs(Gtk.Box):
    """Linked Me / Teams / Shared toggle buttons.

    Calls ``on_changed(source)`` with ``'me' | 'teams' | 'shared'`` once per
    switch (only the newly-activated button fires).
    """

    def __init__(self, on_changed: Callable[[str], None]):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, css_classes=["linked"])
        self._on_changed = on_changed
        self._me = Gtk.ToggleButton(label=_("Me"), active=True)
        self._teams = Gtk.ToggleButton(label=_("Teams"))
        self._shared = Gtk.ToggleButton(label=_("Shared"))
        for t in (self._teams, self._shared):
            t.set_group(self._me)
        for t in (self._me, self._teams, self._shared):
            t.connect("toggled", self._on_toggled)
            self.append(t)

    def _on_toggled(self, btn: Gtk.ToggleButton) -> None:
        if btn.get_active():  # ignore the matching deactivate of the old tab
            self._on_changed(self.source())

    def source(self) -> str:
        if self._teams.get_active():
            return "teams"
        if self._shared.get_active():
            return "shared"
        return "me"


# -- shared-mailbox add dialog --------------------------------------------
def present_add_shared_dialog(window, account, on_added: Callable[[str], None]) -> None:
    """Ask for a shared-mailbox address, persist it on the account, then call
    ``on_added(address)`` so the caller can reload its source list."""
    dialog = Adw.AlertDialog(
        heading=_("Add shared mailbox"),
        body=_("Enter the email address of a shared mailbox you have access to."),
    )
    entry = Gtk.Entry(input_purpose=Gtk.InputPurpose.EMAIL,
                      placeholder_text=_("name@company.com"))
    dialog.set_extra_child(entry)
    dialog.add_response("cancel", _("Cancel"))
    dialog.add_response("add", _("Add"))
    dialog.set_response_appearance("add", Adw.ResponseAppearance.SUGGESTED)
    dialog.set_default_response("add")

    def on_response(_d, response):
        if response != "add":
            return
        address = entry.get_text().strip()
        if not address:
            return
        shared = list(account.shared_mailboxes or [])
        if address not in shared:
            shared.append(address)
            account.shared_mailboxes = shared
            window.get_application().registry.update(account)
        on_added(address)

    dialog.connect("response", on_response)
    dialog.present(window)
