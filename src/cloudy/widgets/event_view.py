# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Shahab Nedaei
"""Reading-pane content for a single calendar event (Outlook-style detail).

Shows the time, location, organizer and attendees, a Join/Open action bar and
RSVP buttons for meeting invites, then the event description rendered as HTML.
"""

from __future__ import annotations

from gettext import gettext as _

from gi.repository import Adw, Gio, Gtk, Pango

from .format import esc, sender_name

_RSVP = (
    ("accept", _("Accept"), "accepted"),
    ("tentativelyAccept", _("Tentative"), "tentativelyAccepted"),
    ("decline", _("Decline"), "declined"),
)

# Canonical response → (icon, label, CSS accent). Covers both Microsoft
# (none/organizer/tentativelyAccepted/accepted/declined/notResponded) and Google
# (needsAction/declined/tentative/accepted) vocabularies via _norm_response.
_RESP_META = {
    "accepted": ("emblem-ok-symbolic", _("Accepted"), "success"),
    "tentative": ("dialog-question-symbolic", _("Tentative"), "warning"),
    "declined": ("window-close-symbolic", _("Declined"), "error"),
    "none": ("mail-unread-symbolic", _("No reply"), "dim-label"),
}
_RESP_ORDER = ("accepted", "tentative", "declined", "none")


def _norm_response(raw: str) -> str:
    r = (raw or "").lower()
    if r == "accepted" or r == "organizer":
        return "accepted"
    if r in ("tentativelyaccepted", "tentative"):
        return "tentative"
    if r == "declined":
        return "declined"
    return "none"  # none / notresponded / needsaction / ""


def _open_uri(uri: str) -> None:
    if not uri:
        return
    try:
        Gio.AppInfo.launch_default_for_uri(uri, None)
    except Exception:  # noqa: BLE001
        pass


def build_event_content(event: dict, *, on_rsvp=None) -> Gtk.Widget:
    """Build the detail widget. ``on_rsvp(action)`` is called for RSVP clicks."""
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)

    header = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6,
                     margin_top=16, margin_bottom=10, margin_start=20, margin_end=20)
    box.append(header)

    subject = Gtk.Label(label=event.get("subject") or _("(no title)"), xalign=0, wrap=True)
    subject.add_css_class("title-2")
    header.append(subject)

    when = _format_when(event.get("start", ""), event.get("end", ""), event.get("all_day"))
    if when:
        header.append(_meta_row("x-office-calendar-symbolic", when))
    if event.get("location"):
        header.append(_meta_row("mark-location-symbolic", event["location"]))
    if event.get("organizer"):
        header.append(_meta_row("contact-new-symbolic",
                                _("Organizer: %s") % sender_name(event["organizer"])))
    attendees = event.get("attendees") or []

    # Action bar: Join / Open in calendar / RSVP.
    actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8, margin_top=6,
                      hexpand=True)
    if event.get("online_url"):
        join = Gtk.Button(label=_("Join meeting"))
        join.add_css_class("suggested-action")
        join.connect("clicked", lambda *_a: _open_uri(event["online_url"]))
        actions.append(join)
    if event.get("web_link"):
        opn = Gtk.Button(label=_("Open in calendar"))
        opn.connect("clicked", lambda *_a: _open_uri(event["web_link"]))
        actions.append(opn)
    if actions.get_first_child() is not None:
        header.append(actions)

    if event.get("can_respond") and on_rsvp is not None:
        header.append(build_rsvp_bar(event.get("response"), on_rsvp))

    if attendees:
        header.append(_responses_section(attendees))

    box.append(Gtk.Separator())

    body = event.get("body", "")
    if body and body.strip():
        from .message_view import html_body_widget

        box.append(html_body_widget(body, event.get("body_html", False)))
    else:
        empty = Adw.StatusPage(icon_name="x-office-calendar-symbolic",
                               title=_("No description"))
        empty.set_vexpand(True)
        box.append(empty)
    return box


def build_rsvp_bar(response, on_rsvp, *, title: str | None = None) -> Gtk.Widget:
    """Accept / Tentative / Decline buttons with a one-line status above them
    (mirroring Teams' "✓ Accepted"). Shared by the calendar event detail and the
    Mail view's meeting-invite bar. ``on_rsvp(action)`` gets the raw action name
    (accept | tentativelyAccept | decline)."""
    current = _norm_response(response)
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, margin_top=4)
    if title:
        head = Gtk.Label(label=title, xalign=0)
        head.add_css_class("heading")
        box.append(head)

    icon, label, accent = _RESP_META[current]
    status = _meta_row(icon, _("Your response: %s") % label
                       if current != "none" else _("You haven't replied yet"))
    status.get_last_child().add_css_class(accent)
    status.get_last_child().remove_css_class("dim-label")
    box.append(status)

    rsvp = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
    rsvp.add_css_class("linked")
    for action, btn_label, state in _RSVP:
        btn = Gtk.Button(label=btn_label)
        if current == _norm_response(state):
            btn.add_css_class("suggested-action")
        btn.connect("clicked", lambda _b, a=action: on_rsvp(a))
        rsvp.append(btn)
    box.append(rsvp)
    return box


def _responses_section(attendees: list) -> Gtk.Widget:
    """A compact attendee response tracker: a tally line, then attendees as small
    pills grouped by status (accepted / tentative / declined / no reply) in a
    wrapping flow — dense and scannable rather than a tall full-width list.
    This consolidates the scattered meeting accept/decline notification emails."""
    counts = {k: 0 for k in _RESP_ORDER}
    groups: dict[str, list] = {k: [] for k in _RESP_ORDER}
    for a in attendees:
        if isinstance(a, dict):
            name, resp = a.get("name", ""), _norm_response(a.get("response"))
        else:  # tolerate the old list[str] shape
            name, resp = a, "none"
        counts[resp] += 1
        groups[resp].append(name)

    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin_top=8)
    head = Gtk.Label(label=_("Responses"), xalign=0)
    head.add_css_class("heading")
    box.append(head)
    tally = " · ".join(
        f"{counts[k]} {_RESP_META[k][1].lower()}" for k in _RESP_ORDER if counts[k])
    if tally:
        box.append(_meta_row("system-users-symbolic", tally))

    # One wrapping group per non-empty status, in priority order.
    for status in _RESP_ORDER:
        names = groups[status]
        if not names:
            continue
        label_txt = _RESP_META[status][1]
        section = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        cap = Gtk.Label(label=f"{label_txt} · {len(names)}", xalign=0)
        cap.add_css_class("cloudy-meta")
        section.append(cap)
        # Adw.WrapBox: plain wrapping layout — unlike Gtk.FlowBox it doesn't wrap
        # each pill in a selectable/hoverable child (that was the odd padded
        # highlight around each name).
        flow = Adw.WrapBox(child_spacing=6, line_spacing=6)
        for name in names[:40]:
            flow.append(_attendee_pill(name, status))
        section.append(flow)
        box.append(section)
    return box


def _attendee_pill(name: str, status: str) -> Gtk.Widget:
    icon_name, _label, accent = _RESP_META[status]
    pill = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
    pill.add_css_class("cloudy-pill")
    dot = Gtk.Image.new_from_icon_name(icon_name)
    dot.set_pixel_size(12)
    dot.add_css_class(accent)
    dot.set_margin_start(6)
    pill.append(dot)
    label = Gtk.Label(label=esc(sender_name(name)) or _("(unknown)"), use_markup=True,
                      ellipsize=Pango.EllipsizeMode.END, max_width_chars=24,
                      margin_top=4, margin_bottom=4, margin_end=8)
    pill.append(label)
    return pill


def _meta_row(icon: str, text: str) -> Gtk.Widget:
    row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
    img = Gtk.Image.new_from_icon_name(icon)
    img.add_css_class("dim-label")
    img.set_valign(Gtk.Align.START)
    row.append(img)
    label = Gtk.Label(label=text, xalign=0, wrap=True, hexpand=True)
    label.add_css_class("dim-label")
    row.append(label)
    return row


def _format_when(start: str, end: str, all_day: bool) -> str:
    if not start:
        return ""
    if "T" not in start:
        return start
    date, _sep, rest = start.partition("T")
    if all_day:
        return _("%s · All day") % date
    start_t = rest[:5]
    end_t = end.partition("T")[2][:5] if end and "T" in end else ""
    return f"{date} · {start_t}–{end_t}" if end_t else f"{date} · {start_t}"
