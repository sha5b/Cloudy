# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Shahab Nedaei
"""Read view for a single mail message.

The body is rendered as real HTML in a sandboxed ``WebKit.WebView`` (JavaScript
disabled, links opened in the system browser, background matched to the GTK
theme). If WebKitGTK isn't available we fall back to tidy plain text so the app
still works everywhere.
"""

from __future__ import annotations

import html
import io
import re
from gettext import gettext as _

from gi.repository import Adw, Gtk, Pango

from .format import esc, sender_email, sender_name, short_time

_TAG_RE = re.compile(r"<[^>]+>")
_STYLE_RE = re.compile(r"<(script|style|head)[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE)
_BLOCK_RE = re.compile(r"</(p|div|tr|table|h[1-6]|li|ul|ol|blockquote)>", re.IGNORECASE)


def _to_text(body: str) -> str:
    """Convert an HTML or plain body to tidy, readable plain text (fallback)."""
    if "<" in body and ">" in body:
        body = _STYLE_RE.sub("", body)
        body = re.sub(r"<br\s*/?>", "\n", body, flags=re.IGNORECASE)
        body = _BLOCK_RE.sub("\n", body)
        body = _TAG_RE.sub("", body)
        body = html.unescape(body)

    lines = [ln.strip() for ln in body.replace("\r", "").split("\n")]
    text = "\n".join(lines)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# -- HTML rendering ------------------------------------------------------
# Emails are authored for a light background; like every desktop mail client we
# always render them on a white "page" so the message's own (usually dark) text
# stays readable regardless of the app's light/dark theme.
_PAGE_BG = "#ffffff"


_CID_IMG_RE = re.compile(r"<img\b[^>]*\bsrc\s*=\s*[\"']cid:[^>]*>", re.IGNORECASE)
_CID_SRC_RE = re.compile(r"""src\s*=\s*["']cid:([^"']+)["']""", re.IGNORECASE)

# Remote image sources are blocked by default to prevent tracking pixels / IP
# leaks. Only http(s) image URLs are replaced; cid: (resolved to data:) and
# other schemes are left alone. A future toggle can pass load_remote=True.
_REMOTE_IMG_RE = re.compile(
    r"(<img\b[^>]*\bsrc\s*=\s*['\"])(https?://[^'\"]+)(['\"][^>]*>)",
    re.IGNORECASE,
)
_REMOTE_BG_RE = re.compile(
    r"(background(?:-image)?\s*:\s*[^;]*url\s*\(\s*['\"]?)(https?://[^'\"\)]+)(['\"]?\s*\))",
    re.IGNORECASE,
)
_REMOTE_IMG_PLACEHOLDER = (
    "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='24' "
    "height='24'%3E%3Ctext x='4' y='17' font-size='12' fill='%23999'%3E?%3C/text%3E%3C/svg%3E"
)


_INLINE_MAX_EDGE = 1400  # downscale inline images past this (longest edge)


def _shrink_inline(b64: str, content_type: str):
    """Downscale an over-large inline image so the WebView stays smooth to
    scroll (full-resolution screenshots embedded as data URIs make it crawl).
    Returns ``(b64, content_type)`` unchanged if it's already small or on any
    decode/encode error."""
    import base64

    try:
        from .imaging import shrink_image_bytes

        data = base64.b64decode(b64)
        # Pillow gives the decoded size without full decode/scale, so we can
        # bail out early for small images.
        from PIL import Image
        with Image.open(io.BytesIO(data)) as img:
            w, h = img.size
            if max(w, h) <= _INLINE_MAX_EDGE:
                return b64, content_type
        png = shrink_image_bytes(data, _INLINE_MAX_EDGE)
        return base64.b64encode(png).decode(), "image/png"
    except Exception:  # noqa: BLE001 - undecodable → keep the original
        pass
    return b64, content_type


def _resolve_cids(content: str, inline_images) -> str:
    """Replace ``src="cid:…"`` references with inline ``data:`` URIs built from
    the message's inline attachments (contentId match, ``<>`` tolerated)."""
    uris = {}
    for img in inline_images or []:
        cid = (img.get("content_id") or "").strip().strip("<>")
        if cid and img.get("content_bytes"):
            b64, ctype = _shrink_inline(
                img["content_bytes"], img.get("content_type") or "image/png")
            uris[cid] = f"data:{ctype};base64,{b64}"

    def repl(match):
        uri = uris.get(match.group(1).strip().strip("<>"))
        return f'src="{uri}"' if uri else match.group(0)

    return _CID_SRC_RE.sub(repl, content)


def _block_remote_images(body: str) -> str:
    """Replace remote image URLs with a local placeholder."""
    body = _REMOTE_IMG_RE.sub(r"\1" + _REMOTE_IMG_PLACEHOLDER + r"\3", body)
    body = _REMOTE_BG_RE.sub(r"\1" + _REMOTE_IMG_PLACEHOLDER + r"\3", body)
    return body


def _wrap_html(content: str, is_html: bool, inline_images=None,
               load_remote: bool = False) -> str:
    """Wrap a message body in a minimal HTML document on a light page."""
    fg, bg, link, quote = "#1a1a1a", _PAGE_BG, "#1a73e8", "#5e5c64"

    if is_html:
        # Resolve inline (cid:) images to data URIs so they render; any cid
        # without a matching attachment is dropped (it'd be a broken "?" — common
        # in Outlook/Teams meeting invites). http(s) images are blocked by
        # default to avoid tracking pixels / IP leaks.
        body = _resolve_cids(content, inline_images)
        body = _CID_IMG_RE.sub("", body)
        if not load_remote:
            body = _block_remote_images(body)
    else:
        body = "<pre>%s</pre>" % html.escape(content)

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  html, body {{ margin: 0; padding: 18px 20px; background: {bg}; color: {fg};
    font-family: -gtk-system-font, "Cantarell", sans-serif; font-size: 15px;
    line-height: 1.55; word-wrap: break-word; overflow-wrap: break-word; }}
  img {{ max-width: 100%; height: auto; }}
  a {{ color: {link}; }}
  table {{ max-width: 100% !important; border-collapse: collapse; }}
  pre {{ white-space: pre-wrap; word-wrap: break-word;
    font-family: -gtk-system-font, "Cantarell", sans-serif; margin: 0; }}
  blockquote {{ margin: 0 0 0 12px; padding-left: 12px;
    border-left: 3px solid {quote}; color: {quote}; }}
</style></head>
<body>{body}</body></html>"""


def _body_widget(msg: dict) -> Gtk.Widget:
    body = msg.get("body", "") or ""
    inline = msg.get("inline_images") or []
    has_images = bool(inline) or "<img" in body.lower()
    # Some messages (notably Microsoft meeting accept/decline notifications)
    # carry no body at all — fall back to the server preview, then to a clear
    # placeholder, rather than rendering a blank white page. An image-only body
    # is NOT empty (its text strips to "" but the pictures are the content).
    if not _to_text(body).strip() and not has_images:
        if msg.get("meeting_response"):
            return _meeting_response_card(msg)
        preview = (msg.get("preview") or "").strip()
        if preview:
            return html_body_widget(preview, False)
    return html_body_widget(body, msg.get("body_html", False), inline)


def _meeting_response_card(msg: dict) -> Gtk.Widget:
    """For an empty-bodied meeting accept/decline notification, show who
    responded and how, instead of a blank page."""
    meta = {
        "accepted": ("emblem-ok-symbolic", _("accepted"), "success"),
        "tentative": ("dialog-question-symbolic", _("tentatively accepted"), "warning"),
        "declined": ("window-close-symbolic", _("declined"), "error"),
    }
    icon_name, verb, accent = meta.get(
        msg.get("meeting_response"), ("mail-read-symbolic", _("responded"), "dim-label"))
    who = sender_name(msg.get("from", "")) or _("Someone")

    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, vexpand=True,
                  hexpand=True, valign=Gtk.Align.CENTER, halign=Gtk.Align.CENTER,
                  margin_start=24, margin_end=24)
    icon = Gtk.Image.new_from_icon_name(icon_name)
    icon.set_pixel_size(48)
    icon.add_css_class(accent)
    box.append(icon)
    line = Gtk.Label(label=_("%(who)s %(verb)s the meeting") % {"who": who, "verb": verb},
                     wrap=True, justify=Gtk.Justification.CENTER)
    line.add_css_class("title-3")
    box.append(line)
    subject = (msg.get("subject") or "").strip()
    if subject:
        sub = Gtk.Label(label=subject, wrap=True, justify=Gtk.Justification.CENTER)
        sub.add_css_class("dim-label")
        box.append(sub)
    return box


def html_body_widget(content: str, is_html: bool, inline_images=None) -> Gtk.Widget:
    """A WebKit view of an HTML/plain body (links open externally), or a
    plain-text label fallback if WebKitGTK isn't available. Reused by the mail
    reader and the calendar event detail."""
    content = content or ""
    # Empty body → a clear placeholder instead of a blank white page (covers
    # meeting notifications and any content-less message/event). Inline images
    # count as content even though their text strips to "".
    has_images = bool(inline_images) or "<img" in content.lower()
    if not _to_text(content).strip() and not has_images:
        return _empty_placeholder()
    view = _build_webview(_wrap_html(content, is_html, inline_images))
    return view if view is not None else _text_fallback(content)


def _open_uri_external(uri: str) -> None:
    """Open a clicked mail link in the user's browser. Prefers ``Gtk.show_uri``,
    which routes through the OpenURI portal under Flatpak — where
    ``Gio.AppInfo.launch_default_for_uri`` finds no handler and silently fails,
    the reason links wouldn't open. Falls back to ``Gio.AppInfo`` off-sandbox."""
    if not uri:
        return
    try:
        Gtk.show_uri(None, uri, 0)  # 0 == GDK_CURRENT_TIME
        return
    except Exception:  # noqa: BLE001 - fall back below
        pass
    try:
        from gi.repository import Gio

        Gio.AppInfo.launch_default_for_uri(uri, None)
    except Exception:  # noqa: BLE001
        pass


def _build_webview(html_doc: str):
    """Build the shared sandboxed WebView for a complete HTML document, or None
    if WebKitGTK isn't available on this runtime. Links open in the browser."""
    from ..core.gi_compat import require

    if require("WebKit", ("6.0", "6.1")) is None:
        return None  # no WebKitGTK on this runtime
    try:
        from gi.repository import Gdk, WebKit
    except ImportError:
        return None

    view = WebKit.WebView(vexpand=True, hexpand=True)

    settings = view.get_settings()
    settings.set_enable_javascript(False)
    for off in ("set_enable_javascript_markup", "set_enable_webgl",
                "set_enable_html5_local_storage", "set_enable_html5_database"):
        try:
            getattr(settings, off)(False)
        except Exception:  # noqa: BLE001 - setting may not exist on this build
            pass

    rgba = Gdk.RGBA()
    rgba.parse(_PAGE_BG)
    view.set_background_color(rgba)

    # Links open in the user's browser; never navigate inside the reader.
    def _on_decide(_view, decision, decision_type):
        new_window = decision_type == WebKit.PolicyDecisionType.NEW_WINDOW_ACTION
        if new_window or decision_type == WebKit.PolicyDecisionType.NAVIGATION_ACTION:
            nav = decision.get_navigation_action()
            clicked = nav.get_navigation_type() == WebKit.NavigationType.LINK_CLICKED
            # A plain click, or a target="_blank" link (which arrives as a
            # new-window request — scripting is off, so nothing else handles it).
            if clicked or new_window:
                _open_uri_external(nav.get_request().get_uri())
                decision.ignore()
                return True
        return False

    view.connect("decide-policy", _on_decide)
    view.load_html(html_doc, None)
    return view


def _empty_placeholder() -> Gtk.Widget:
    # The one shared empty-state widget, so this matches every other "nothing
    # here" surface in the app instead of a bespoke icon+title box.
    from .source_nav import status_page

    return status_page("mail-read-symbolic", _("No message content"))


def _text_fallback(content: str) -> Gtk.Widget:
    scrolled = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
    clamp = Adw.Clamp(maximum_size=720, margin_top=12, margin_bottom=24,
                      margin_start=18, margin_end=18)
    label = Gtk.Label(label=_to_text(content) or _("(empty message)"),
                      xalign=0, yalign=0, wrap=True, selectable=True)
    label.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
    label.add_css_class("body")
    clamp.set_child(label)
    scrolled.set_child(clamp)
    return scrolled


def _attachments_bar(attachments, on_open) -> Gtk.Widget:
    """A wrapping row of attachment chips; clicking one calls ``on_open(att)``."""
    flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE,
                       max_children_per_line=4, column_spacing=6, row_spacing=6,
                       margin_top=8, margin_bottom=8, margin_start=20, margin_end=20)
    for att in attachments:
        chip = Gtk.Button()
        chip.add_css_class("card")
        inner = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6,
                        margin_top=4, margin_bottom=4, margin_start=8, margin_end=8)
        icon = ("image-x-generic-symbolic"
                if (att.get("content_type") or "").lower().startswith("image")
                else "mail-attachment-symbolic")
        inner.append(Gtk.Image.new_from_icon_name(icon))
        inner.append(Gtk.Label(label=att.get("name") or _("attachment"),
                               ellipsize=Pango.EllipsizeMode.MIDDLE, max_width_chars=24))
        chip.set_child(inner)
        chip.connect("clicked", lambda _b, a=att: on_open(a))
        flow.append(chip)
    return flow


def _copy_to_clipboard(widget: Gtk.Widget, text: str) -> None:
    """Copy ``text`` and, if a toast overlay is in the ancestry, confirm it.

    Both reader hosts (the mail pane and the pop-out window) wrap their content
    in an ``Adw.ToastOverlay``; walk up to whichever one we're inside."""
    widget.get_clipboard().set(text)
    node = widget.get_parent()
    while node is not None and not isinstance(node, Adw.ToastOverlay):
        node = node.get_parent()
    if node is not None:
        node.add_toast(Adw.Toast(title=_("Copied %s") % text, timeout=2))


def _on_address_link(label: Gtk.Label, uri: str) -> bool:
    # Our address links use a private "copy:<addr>" scheme so clicking copies
    # instead of trying to launch a handler. Returning True suppresses the
    # default (open-URI) behaviour.
    if uri.startswith("copy:"):
        _copy_to_clipboard(label, uri[len("copy:"):])
    return True


def _address_row(prefix: str, value: str) -> Gtk.Widget | None:
    """A dim "Prefix:" line whose addresses are each a click-to-copy link.

    Rendered as a single wrapping markup label so many recipients flow onto
    multiple lines naturally."""
    addrs = [a.strip() for a in (value or "").split(",") if a.strip()]
    if not addrs:
        return None
    links = ", ".join(f'<a href="copy:{esc(a)}">{esc(a)}</a>' for a in addrs)
    row = Gtk.Label(xalign=0, wrap=True, use_markup=True)
    row.set_markup(f"{esc(prefix)} {links}")
    row.add_css_class("dim-label")
    row.add_css_class("caption")
    row.set_tooltip_text(_("Click an address to copy it"))
    row.connect("activate-link", _on_address_link)
    return row


# -- public builders -----------------------------------------------------
def build_message_content(msg: dict, on_open_attachment=None, on_rsvp=None) -> Gtk.Widget:
    """Reader content: a fixed header (subject/sender/date) + the body view.

    ``on_open_attachment(att)`` (optional) is called when an attachment chip is
    clicked; without it, chips are hidden. When the message carries a meeting
    invite (``msg["invite"]``) and ``on_rsvp`` is given, an Accept / Tentative /
    Decline bar is shown above the body. Suitable for a reading pane or page.
    """
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)

    header = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4,
                     margin_top=16, margin_bottom=10, margin_start=20, margin_end=20)
    box.append(header)

    subject = Gtk.Label(label=msg.get("subject") or _("(no subject)"),
                        xalign=0, wrap=True)
    subject.add_css_class("title-2")
    header.append(subject)

    if msg.get("from") or msg.get("received"):
        line = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8, margin_top=4)
        if msg.get("from"):
            who = Gtk.Label(label=sender_name(msg["from"]), xalign=0, hexpand=True, wrap=True)
            who.add_css_class("heading")
            line.append(who)
        if msg.get("received"):
            when = Gtk.Label(label=short_time(msg["received"]), xalign=1,
                             valign=Gtk.Align.START)
            when.add_css_class("dim-label")
            when.add_css_class("caption")
            line.append(when)
        header.append(line)

    # From / To / Cc / Bcc — each address spelled out in full and individually
    # clickable to copy it to the clipboard. From mirrors the recipient rows;
    # Bcc is normally only present on mail you sent.
    for prefix, value in ((_("From:"), sender_email(msg.get("from", ""))),
                          (_("To:"), msg.get("to")),
                          (_("Cc:"), msg.get("cc")),
                          (_("Bcc:"), msg.get("bcc"))):
        row = _address_row(prefix, value)
        if row is not None:
            header.append(row)

    invite = msg.get("invite")
    if invite and on_rsvp is not None:
        from .event_view import build_invite_card

        bar = build_invite_card(invite, on_rsvp)
        bar.set_margin_start(20)
        bar.set_margin_end(20)
        bar.set_margin_bottom(6)
        box.append(Gtk.Separator())
        box.append(bar)

    attachments = msg.get("attachments") or []
    if attachments and on_open_attachment is not None:
        box.append(Gtk.Separator())
        box.append(_attachments_bar(attachments, on_open_attachment))
    box.append(Gtk.Separator())
    box.append(_body_widget(msg))
    return box
