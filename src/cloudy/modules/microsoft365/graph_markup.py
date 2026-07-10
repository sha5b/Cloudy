# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Shahab Nedaei
"""HTML → Pango/plain-text helpers shared by Graph chat/team rendering."""

from __future__ import annotations

import html
import json
import re

_TAG_RE = re.compile(r"<[^>]+>")

# HTML inline tags that map cleanly onto Pango markup.
_PANGO_TAGS = {
    "b": "b", "strong": "b", "i": "i", "em": "i", "u": "u",
    "s": "s", "strike": "s", "del": "s", "code": "tt", "pre": "tt",
}


def strip_html(value: str) -> str:
    """Collapse an HTML body to readable single-line plain text."""
    if not value:
        return ""
    text = _TAG_RE.sub(" ", value)
    return html.unescape(re.sub(r"\s+", " ", text)).strip()


def strip_reply_placeholder(content: str) -> str:
    """Drop the ``<attachment id=…>`` placeholder Teams leaves in replies."""
    if not content:
        return content
    content = re.sub(r"(?is)<attachment\b[^>]*>.*?</attachment>", "", content)
    return re.sub(r"(?is)<attachment\b[^>]*/?>", "", content)


def parse_message_reference(att: dict) -> dict:
    """Turn a Teams ``messageReference`` attachment into a reply quote."""
    raw = att.get("content") or ""
    ref_id = str(att.get("id") or "")
    preview, sender = "", ""
    try:
        data = json.loads(raw) if raw else {}
        preview = strip_html(data.get("messagePreview") or "")
        ref_id = str(data.get("messageId") or ref_id)
        user = (data.get("messageSender") or {}).get("user") or {}
        sender = html.unescape(user.get("displayName") or "")
    except (ValueError, TypeError):
        pass
    return {"id": ref_id, "text": preview, "from": sender}


def parse_forwarded_message(att: dict) -> dict:
    """Parse a Teams ``forwardedMessageReference`` attachment into a quote dict
    ``{id, from, text}``.

    Teams puts a forwarded message's original content in the attachment's JSON
    ``content`` under ``originalMessageContent`` (HTML) + ``originalMessageSender``
    — the message ``body`` itself is just an ``<attachment>`` placeholder, which
    is why an unparsed forward shows as a bare attachment. ``displayName`` is
    sometimes null in the reference, so an empty sender is expected."""
    raw = att.get("content") or ""
    text, sender_name, ref_id = "", "", str(att.get("id") or "")
    try:
        data = json.loads(raw) if raw else {}
    except (ValueError, TypeError):
        data = {}
    if isinstance(data, dict):
        text = strip_html(data.get("originalMessageContent") or "")
        ref_id = str(data.get("originalMessageId") or ref_id)
        user = (data.get("originalMessageSender") or {}).get("user") or {}
        sender_name = html.unescape(user.get("displayName") or "")
    return {"id": ref_id, "from": sender_name, "text": text}


def split_attachments(m: dict):
    """Split a message's attachments into ``(reply_to, forward, file_attachments)``.

    ``reply_to`` is a reply quote (``messageReference``), ``forward`` is a
    forwarded-message quote (``forwardedMessageReference``), and the rest are
    file attachments (files/images carried as ``reference`` etc.). A forwarded
    message that also carried a file surfaces as *both* a forward quote and a
    file chip, so the file still renders."""
    reply_to = None
    forward = None
    attachments = []
    for a in (m.get("attachments") or []):
        ctype = a.get("contentType") or ""
        if ctype == "messageReference":
            if reply_to is None:
                reply_to = parse_message_reference(a)
            continue
        if ctype == "forwardedMessageReference":
            if forward is None:
                forward = parse_forwarded_message(a)
            continue
        attachments.append({
            "name": a.get("name") or "attachment",
            "url": a.get("contentUrl", ""),
            "content_type": ctype,
        })
    return reply_to, forward, attachments


def _pango_escape(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def html_to_pango(content: str) -> str:
    """Convert chat HTML to a safe Pango-markup subset.

    Returns ``""`` when the result carries no markup, so callers can fall back
    to plain text.
    """
    if not content:
        return ""
    s = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", "", content)
    s = re.sub(r"(?is)<img[^>]*>", "", s)
    s = re.sub(r"(?i)<br\s*/?>", "\n", s)
    s = re.sub(r"(?i)</(p|div|tr|h[1-6])>", "\n", s)
    s = re.sub(r"(?i)<li[^>]*>", "\n• ", s)
    out: list[str] = []
    has_markup = False
    pos = 0
    for m in re.finditer(r"<[^>]+>", s):
        chunk = s[pos:m.start()]
        if chunk:
            out.append(_pango_escape(html.unescape(chunk)))
        pos = m.end()
        tag = m.group(0)
        tm = re.match(r"</?\s*([a-zA-Z0-9]+)", tag)
        if not tm:
            continue
        name, closing = tm.group(1).lower(), tag.startswith("</")
        if name == "a":
            if closing:
                out.append("</a>")
            else:
                hm = (re.search(r'href="([^"]*)"', tag)
                      or re.search(r"href='([^']*)'", tag))
                href = _pango_escape(html.unescape(hm.group(1))) if hm else ""
                out.append(f'<a href="{href}">')
            has_markup = True
        elif name in _PANGO_TAGS:
            pt = _PANGO_TAGS[name]
            out.append(f"</{pt}>" if closing else f"<{pt}>")
            has_markup = True
    tail = s[pos:]
    if tail:
        out.append(_pango_escape(html.unescape(tail)))
    if not has_markup:
        return ""
    return re.sub(r"\n{3,}", "\n\n", "".join(out)).strip()
