# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Fiber Elements
"""Inline read view for a single mail message (pushed into the content nav).

Bodies are shown as plain text. If only HTML is available we strip tags rather
than pull in WebKitGTK; rich rendering can come later.
"""

from __future__ import annotations

import html
import re
from gettext import gettext as _

from gi.repository import Adw, Gtk

from .format import sender_name, short_time

_TAG_RE = re.compile(r"<[^>]+>")
_STYLE_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE)


def _to_text(body: str) -> str:
    if "<" not in body or ">" not in body:
        return body
    body = _STYLE_RE.sub("", body)
    body = re.sub(r"<br\s*/?>", "\n", body, flags=re.IGNORECASE)
    body = re.sub(r"</p>", "\n\n", body, flags=re.IGNORECASE)
    text = _TAG_RE.sub("", body)
    text = html.unescape(text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def make_message_page(msg: dict) -> Adw.NavigationPage:
    """Build a NavigationPage for one message (back button via NavigationView)."""
    toolbar = Adw.ToolbarView()
    toolbar.add_top_bar(Adw.HeaderBar())

    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_top=12,
                  margin_bottom=12, margin_start=16, margin_end=16)
    toolbar.set_content(box)

    subject = Gtk.Label(label=msg.get("subject") or _("(no subject)"), xalign=0, wrap=True)
    subject.add_css_class("title-2")
    box.append(subject)

    meta_parts = []
    if msg.get("from"):
        meta_parts.append(_("From: %s") % sender_name(msg["from"]))
    if msg.get("to"):
        meta_parts.append(_("To: %s") % msg["to"])
    if msg.get("received"):
        meta_parts.append(short_time(msg["received"]))
    if meta_parts:
        meta = Gtk.Label(label="\n".join(meta_parts), xalign=0, wrap=True)
        meta.add_css_class("dim-label")
        box.append(meta)

    scrolled = Gtk.ScrolledWindow(vexpand=True)
    view = Gtk.TextView(editable=False, cursor_visible=False,
                        wrap_mode=Gtk.WrapMode.WORD_CHAR)
    view.add_css_class("body")
    view.get_buffer().set_text(_to_text(msg.get("body", "")))
    scrolled.set_child(view)
    box.append(scrolled)

    title = (msg.get("subject") or _("Message"))[:40]
    page = Adw.NavigationPage(title=title, tag="message")
    page.set_child(toolbar)
    return page
