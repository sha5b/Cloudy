# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Fiber Elements
"""A simple read view for a single mail message (provider-agnostic).

Bodies are shown as plain text. If only HTML is available we strip tags rather
than pull in WebKitGTK; rich rendering can come later.
"""

from __future__ import annotations

import html
import re
from gettext import gettext as _

from gi.repository import Adw, Gtk

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
    # Collapse excessive blank lines.
    return re.sub(r"\n{3,}", "\n\n", text).strip()


class MessageDialog(Adw.Dialog):
    __gtype_name__ = "ClouddriveMessageDialog"

    def __init__(self, msg: dict):
        super().__init__(
            title=(msg.get("subject") or _("Message"))[:60],
            content_width=660,
            content_height=560,
        )
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())
        self.set_child(toolbar)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_top=12,
                      margin_bottom=12, margin_start=12, margin_end=12)
        toolbar.set_content(box)

        subject = Gtk.Label(label=msg.get("subject") or _("(no subject)"), xalign=0,
                            wrap=True)
        subject.add_css_class("title-2")
        box.append(subject)

        meta = Gtk.Label(xalign=0, wrap=True)
        parts = []
        if msg.get("from"):
            parts.append(_("From: %s") % msg["from"])
        if msg.get("to"):
            parts.append(_("To: %s") % msg["to"])
        if msg.get("received"):
            parts.append(msg["received"].replace("T", " ").rstrip("Z"))
        meta.set_label("\n".join(parts))
        meta.add_css_class("dim-label")
        box.append(meta)

        scrolled = Gtk.ScrolledWindow(vexpand=True)
        view = Gtk.TextView(editable=False, cursor_visible=False,
                            wrap_mode=Gtk.WrapMode.WORD_CHAR)
        view.add_css_class("body")
        view.get_buffer().set_text(_to_text(msg.get("body", "")))
        scrolled.set_child(view)
        box.append(scrolled)
