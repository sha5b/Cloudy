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
import re
from gettext import gettext as _

from gi.repository import Adw, Gtk

from .format import sender_name, short_time

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


def _wrap_html(content: str, is_html: bool) -> str:
    """Wrap a message body in a minimal HTML document on a light page."""
    fg, bg, link, quote = "#1a1a1a", _PAGE_BG, "#1a73e8", "#5e5c64"

    if is_html:
        # Drop inline (cid:) images — they reference mail attachments we don't
        # fetch, so they'd render as broken-image "?" placeholders (common in
        # Outlook/Teams meeting invites). http(s) images still load normally.
        body = _CID_IMG_RE.sub("", content)
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
    # Some messages (notably Microsoft meeting accept/decline notifications)
    # carry no body at all — fall back to the server preview, then to a clear
    # placeholder, rather than rendering a blank white page.
    if not _to_text(body).strip():
        if msg.get("meeting_response"):
            return _meeting_response_card(msg)
        preview = (msg.get("preview") or "").strip()
        if preview:
            return html_body_widget(preview, False)
    return html_body_widget(body, msg.get("body_html", False))


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


def html_body_widget(content: str, is_html: bool) -> Gtk.Widget:
    """A WebKit view of an HTML/plain body (links open externally), or a
    plain-text label fallback if WebKitGTK isn't available. Reused by the mail
    reader and the calendar event detail."""
    content = content or ""
    # Empty body → a clear placeholder instead of a blank white page (covers
    # meeting notifications and any content-less message/event).
    if not _to_text(content).strip():
        return _empty_placeholder()
    from ..core.gi_compat import require

    if require("WebKit", ("6.0", "6.1")) is None:
        return _text_fallback(content)  # no WebKitGTK on this runtime
    try:
        from gi.repository import Gdk, Gio, WebKit
    except ImportError:
        return _text_fallback(content)

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
        if decision_type == WebKit.PolicyDecisionType.NAVIGATION_ACTION:
            nav = decision.get_navigation_action()
            if nav.get_navigation_type() == WebKit.NavigationType.LINK_CLICKED:
                uri = nav.get_request().get_uri()
                try:
                    Gio.AppInfo.launch_default_for_uri(uri, None)
                except Exception:  # noqa: BLE001
                    pass
                decision.ignore()
                return True
        return False

    view.connect("decide-policy", _on_decide)
    view.load_html(_wrap_html(content, is_html), None)
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


# -- public builders -----------------------------------------------------
def build_message_content(msg: dict) -> Gtk.Widget:
    """Reader content: a fixed header (subject/sender/date) + the body view.

    Suitable for embedding in a reading pane (two-pane layout) or a page.
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

    if msg.get("to"):
        to = Gtk.Label(label=_("To: %s") % msg["to"], xalign=0, wrap=True)
        to.add_css_class("dim-label")
        to.add_css_class("caption")
        header.append(to)

    box.append(Gtk.Separator())
    box.append(_body_widget(msg))
    return box


def make_message_page(msg: dict) -> Adw.NavigationPage:
    """Build a standalone NavigationPage for one message (single-pane callers)."""
    toolbar = Adw.ToolbarView()
    toolbar.add_top_bar(Adw.HeaderBar())
    toolbar.set_content(build_message_content(msg))

    title = (msg.get("subject") or _("Message"))[:40]
    page = Adw.NavigationPage(title=title, tag="message")
    page.set_child(toolbar)
    return page
