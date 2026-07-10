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


def parse_embedded_message(att: dict) -> dict | None:
    """Best-effort parse of a *forwarded* (embedded) chat message attachment into
    a quote dict ``{id, from, text}``.

    Teams represents a forwarded message as an attachment whose JSON ``content``
    carries the original message's preview/body + sender — but with a
    ``contentType`` *other* than the reply ``messageReference`` (so it would
    otherwise fall through to a bare, unhelpful file chip). Returns ``None`` when
    the attachment doesn't look like an embedded message, so the caller can treat
    it as a normal file."""
    raw = att.get("content") or ""
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    # Preview text under any of the shapes Teams has used for embedded messages.
    preview = (data.get("messagePreview") or data.get("previewText")
               or data.get("messageContent") or data.get("text") or "")
    if not preview:
        body = data.get("messageBody") or data.get("body")
        if isinstance(body, dict):
            preview = body.get("content", "")
        elif isinstance(body, str):
            preview = body
    preview = strip_html(preview)
    sender = data.get("messageSender") or data.get("from") or {}
    sender_name = ""
    if isinstance(sender, dict):
        user = sender.get("user") or sender
        sender_name = html.unescape(user.get("displayName") or "")
    if not preview and not sender_name:
        return None  # nothing message-like — let it render as a file
    return {
        "id": str(data.get("messageId") or att.get("id") or ""),
        "from": sender_name,
        "text": preview,
    }


def split_attachments(m: dict):
    """Split a message's attachments into ``(reply_to, forward, file_attachments)``.

    ``reply_to`` is a reply quote (the ``messageReference`` attachment), ``forward``
    is a *forwarded* message quote (an embedded message under a different
    contentType), and the rest are file attachments."""
    reply_to = None
    forward = None
    attachments = []
    for a in (m.get("attachments") or []):
        if (a.get("contentType") or "") == "messageReference":
            if reply_to is None:
                reply_to = parse_message_reference(a)
            continue
        # A forwarded message: an embedded message-reference-like attachment that
        # isn't the reply type and carries no downloadable file URL.
        if not a.get("contentUrl"):
            embedded = parse_embedded_message(a)
            if embedded is not None:
                if forward is None:
                    forward = embedded
                continue
        attachments.append({
            "name": a.get("name") or "attachment",
            "url": a.get("contentUrl", ""),
            "content_type": a.get("contentType", ""),
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
