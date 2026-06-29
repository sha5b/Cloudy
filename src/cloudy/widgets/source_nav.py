# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Shahab Nedaei
"""Shared building blocks for the Mail and Calendar surfaces.

Both are two-pane views over a Microsoft account's **Me / Teams / Shared**
sources, so they'd otherwise duplicate the same scaffolding. This module holds
the reusable pieces: the source toggle tabs, listbox placeholder rows, the
shared-mailbox add dialog, and scope-error detection.
"""

from __future__ import annotations

import json
import re
import threading
from gettext import gettext as _
from typing import Callable

from gi.repository import Adw, GLib, Gtk, Pango

from .metrics import ICON_LG, SPACE_L, SPACE_M


def local_initial_folder():
    """A fast local folder (XDG Documents, else home) to open file pickers in.

    Without this, the portal restores its last-used location and stats every
    mounted volume for its sidebar — and an rclone FUSE *network* mount answers
    slowly, so the dialog can hang for seconds before it appears. Pinning a
    local start folder sidesteps that."""
    from gi.repository import Gio

    path = (GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_DOCUMENTS)
            or GLib.get_home_dir())
    return Gio.File.new_for_path(path) if path else None


# -- background work ------------------------------------------------------
def run_async(work: Callable[[], object], on_done: Callable[[object, str | None], object]
              ) -> None:
    """Run ``work()`` on a daemon thread and deliver its outcome to
    ``on_done(result, error)`` back on the GTK main loop.

    Any exception becomes the ``error`` string (and ``result`` is ``None``).
    This is the one place the views' "fetch off-thread, render on-thread"
    pattern lives.
    """
    # Capture the calling widget (if any) so we can drop the callback when the
    # widget is destroyed before the worker finishes. This avoids idle callbacks
    # touching finalized GTK objects.
    caller = None
    frame = __import__("sys")._getframe(1)
    self_var = frame.f_locals.get("self")
    if isinstance(self_var, Gtk.Widget):
        caller = self_var

    def guarded_on_done(result, error):
        if caller is not None and not caller.get_parent():
            return False
        return on_done(result, error)

    def worker():
        try:
            result = work()
            GLib.idle_add(guarded_on_done, result, None)
        except Exception as exc:  # noqa: BLE001 - surfaced to the UI as a string
            GLib.idle_add(guarded_on_done, None, str(exc))

    threading.Thread(target=worker, daemon=True).start()


# -- listbox helpers ------------------------------------------------------
def clear_listbox(listbox: Gtk.ListBox) -> None:
    """Remove every row from a ``Gtk.ListBox``."""
    child = listbox.get_first_child()
    while child is not None:
        nxt = child.get_next_sibling()
        listbox.remove(child)
        child = nxt


def patch_listbox(listbox: Gtk.ListBox, items: list[dict],
                  key_func: Callable[[dict], str],
                  factory: Callable[[dict], Gtk.ListBoxRow],
                  update: Callable[[Gtk.ListBoxRow, dict], None] | None = None,
                  *, selection: list[str] | None = None) -> dict[str, Gtk.ListBoxRow]:
    """Update a ``Gtk.ListBox`` to match ``items`` while reusing existing rows.

    Rows created by ``factory`` should have ``row._patch_key`` set to the item
    key (the helper sets it automatically for new rows). ``update(row, item)``
    is called for reused rows so labels/icons can refresh in place instead of
    rebuilding the widget tree. Any row without ``_patch_key`` is left alone.

    ``selection`` is a list of keys that should be selected after patching; the
    caller must restore selection because brand-new rows lose it.
    """
    desired_keys = [key_func(i) for i in items]
    desired_set = set(desired_keys)

    # Index existing data rows by key.
    existing: dict[str, Gtk.ListBoxRow] = {}
    child = listbox.get_first_child()
    while child is not None:
        key = getattr(child, "_patch_key", None)
        if key is not None:
            existing[key] = child
        child = child.get_next_sibling()

    # Update or create rows for the desired set.
    rows_by_key: dict[str, Gtk.ListBoxRow] = {}
    for item in items:
        key = key_func(item)
        row = existing.get(key)
        if row is None:
            row = factory(item)
            row._patch_key = key
        elif update is not None:
            update(row, item)
        rows_by_key[key] = row

    # Drop rows that disappeared.
    for key, row in list(existing.items()):
        if key not in desired_set:
            listbox.remove(row)

    # Re-add data rows in the desired order. Removing and re-inserting the same
    # widget is cheap and gives us correct ordering without recreating widgets.
    for key in desired_keys:
        row = rows_by_key[key]
        listbox.remove(row)
        listbox.append(row)

    # Restore selection for the requested keys.
    if selection:
        for key in selection:
            row = rows_by_key.get(key)
            if row is not None:
                listbox.select_row(row)

    return rows_by_key


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


def attachment_chip(att: dict, window) -> Gtk.Widget:
    """A flat icon+name chip for a non-image attachment (chat & teams). Only
    real http(s) file links open in a browser; hosted-content URLs need an auth
    token and are shown inline instead, so their chips render disabled."""
    name = att.get("name", "") or _("Attachment")
    url = att.get("url", "")
    btn = Gtk.Button(halign=Gtk.Align.START)
    btn.add_css_class("flat")
    content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
    content.append(Gtk.Image.new_from_icon_name("mail-attachment-symbolic"))
    content.append(Gtk.Label(label=name, ellipsize=Pango.EllipsizeMode.MIDDLE))
    btn.set_child(content)
    openable = bool(url) and url.startswith("http")
    btn.set_sensitive(openable)
    if openable:
        btn.connect("clicked", lambda *_a: window.open_uri(url))
    return btn


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


_API_ERROR_RE = re.compile(r"^(Graph|Google)\s+(\d+):\s*(.*)$", re.DOTALL)


def _api_message(body: str) -> str:
    """Pull ``error.message`` out of a Microsoft/Google JSON error envelope."""
    try:
        data = json.loads(body)
    except Exception:  # noqa: BLE001 - not JSON; caller falls back to the raw text
        return ""
    err = data.get("error") if isinstance(data, dict) else None
    if isinstance(err, dict):
        msg = err.get("message")
        # Google nested form: error.errors[].message
        if not msg and isinstance(err.get("errors"), list) and err["errors"]:
            msg = (err["errors"][0] or {}).get("message")
        return (msg or "").strip()
    if isinstance(err, str):
        return err.strip()
    return ""


def friendly_error(error) -> str:
    """Turn a raw client error (a GraphError/GoogleError string, or any
    exception) into one concise, human-readable line for a toast.

    Microsoft and Google both wrap failures as ``"Graph <code>: <json>"`` /
    ``"Google <code>: <json>"``; this unwraps the JSON envelope to the real
    message, maps common HTTP status codes to plain language, and special-cases
    the patterns users actually hit. Falls back to a trimmed first line so a toast
    never dumps a multi-line API blob."""
    msg = str(error or "").strip()
    if not msg:
        return _("Something went wrong. Please try again.")
    if is_scope_error(msg):
        return _("Your sign-in needs refreshing — sign out and back in to continue.")

    provider, code, detail = "", None, msg
    m = _API_ERROR_RE.match(msg)
    if m:
        provider = _("Microsoft") if m.group(1) == "Graph" else _("Google")
        try:
            code = int(m.group(2))
        except ValueError:
            code = None
        body = m.group(3).strip()
        detail = _api_message(body) or body

    low = detail.lower()
    # Patterns worth a specific, actionable message regardless of status code.
    if "provided payload is invalid" in low or "request payload" in low:
        return _("The message was rejected — try fewer or smaller attachments "
                 "(at most 10 per message).")
    if code == 401:
        return _("Your sign-in needs refreshing — sign out and back in to continue.")
    if code == 403:
        return _("You don't have permission to do that.")
    if code == 404:
        return _("That item couldn't be found — it may have been moved or deleted.")
    if code == 413 or "too large" in low:
        return _("That's too large to send.")
    if code == 429:
        return _("Too many requests right now — please try again in a moment.")
    if code is not None and code >= 500:
        who = provider or _("the service")
        return _("%s is having trouble right now — please try again shortly.") % who

    # Generic: the service's own message (or the raw error), trimmed to one line.
    line = detail.split("\n", 1)[0].strip()
    return line[:200] if line else _("Something went wrong. Please try again.")


# -- pinned (starred) sources ---------------------------------------------
# A pin marks a favorite source. Each entry is {"kind", "source", "id", "name"}
# plus any extra fields a kind needs (a "channel" pin also carries "team_id" /
# "team_name", for instance). Kinds: "mail"/"calendar" (shared|teams source),
# "channel" (a Teams channel), "chat" (a Teams chat). Pins surface on the
# Dashboard's Activity feed.
def find_pin(account, kind: str, source: str, sid: str):
    for p in account.pinned_sources or []:
        if p.get("kind") == kind and p.get("source") == source and p.get("id") == sid:
            return p
    return None


def is_pinned(account, kind: str, source: str, sid: str) -> bool:
    return find_pin(account, kind, source, sid) is not None


def toggle_pin(window, account, *, kind: str, source: str, sid: str, name: str,
               **extra) -> bool:
    """Add/remove a pin for a source; persist and return the new pinned state.
    ``extra`` keys (e.g. team_id/team_name for a channel pin) are stored on the
    entry so the Dashboard can route/label it without re-fetching."""
    pins = list(account.pinned_sources or [])
    existing = find_pin(account, kind, source, sid)
    if existing:
        pins = [p for p in pins if p is not existing]
        pinned = False
    else:
        pins.append({"kind": kind, "source": source, "id": sid, "name": name,
                     **extra})
        pinned = True
    account.pinned_sources = pins
    window.get_application().registry.update(account)
    return pinned


# -- muted chats / channels -----------------------------------------------
# A mute silences a chat or channel: no notification banner and no unread badge.
# Each entry is {"kind": "chat"|"channel", "id": str}.
def is_muted(account, kind: str, sid: str) -> bool:
    return any(m.get("kind") == kind and m.get("id") == sid
               for m in (account.muted_sources or []))


def toggle_mute(window, account, *, kind: str, sid: str) -> bool:
    """Add/remove a mute for a chat/channel; persist and return the new state."""
    muted = list(account.muted_sources or [])
    existing = next((m for m in muted
                     if m.get("kind") == kind and m.get("id") == sid), None)
    if existing:
        muted = [m for m in muted if m is not existing]
        now_muted = False
    else:
        muted.append({"kind": kind, "id": sid})
        now_muted = True
    account.muted_sources = muted
    window.get_application().registry.update(account)
    return now_muted


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
