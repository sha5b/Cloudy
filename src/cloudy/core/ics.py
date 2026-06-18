# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Shahab Nedaei
"""iMIP (RFC 5545 / 5546) helpers for meeting invitations carried in email.

``parse_invite`` reads the ``text/calendar`` part of an invite mail into a small
dict; ``build_reply`` produces the ``METHOD:REPLY`` VCALENDAR a recipient sends
back to the organizer to Accept / Tentatively accept / Decline. This is the
provider-agnostic path the Mail view uses so RSVP works for any invite (Google,
external, forwarded), not just ones that round-trip through a calendar API.
"""

from __future__ import annotations

from datetime import datetime, timezone

# RSVP action → (iCalendar PARTSTAT, email subject prefix). Action names match
# the calendar RSVP vocabulary (graph/event_view) so one set of buttons drives
# both calendar events and email invites.
RSVP_PARTSTAT = {
    "accept": ("ACCEPTED", "Accepted"),
    "tentativelyAccept": ("TENTATIVE", "Tentative"),
    "decline": ("DECLINED", "Declined"),
}


def _unfold(text: str) -> list[str]:
    """Undo RFC 5545 line folding (continuation lines start with space/tab)."""
    out: list[str] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and out:
            out[-1] += raw[1:]
        else:
            out.append(raw)
    return out


def _split(line: str) -> tuple[str, dict, str]:
    """``NAME;P=v:value`` → (NAME upper, {param: value}, value)."""
    head, _sep, value = line.partition(":")
    parts = head.split(";")
    name = parts[0].upper()
    params = {}
    for p in parts[1:]:
        k, _s, v = p.partition("=")
        params[k.upper()] = v.strip('"')
    return name, params, value


def _mailto(value: str) -> str:
    return value.split(":", 1)[1].strip() if value.lower().startswith("mailto:") else value.strip()


def parse_invite(text: str) -> dict | None:
    """Parse a VCALENDAR string. Returns the invite dict when it carries a
    VEVENT (with ``method``, ``uid``, ``sequence``, ``summary``, ``location``,
    ``dtstart``/``dtend``, ``organizer_email``/``organizer_cn`` and
    ``attendees`` = ``[{email, cn, partstat}]``), else ``None``."""
    method = ""
    in_event = False
    ev: dict = {"sequence": 0, "attendees": []}
    have_event = False
    for line in _unfold(text):
        name, params, value = _split(line)
        if name == "METHOD":
            method = value.strip().upper()
        elif name == "BEGIN" and value.strip().upper() == "VEVENT":
            in_event = True
            have_event = True
        elif name == "END" and value.strip().upper() == "VEVENT":
            in_event = False
        elif not in_event:
            continue
        elif name == "UID":
            ev["uid"] = value.strip()
        elif name == "SEQUENCE":
            ev["sequence"] = int(value.strip() or 0)
        elif name == "SUMMARY":
            ev["summary"] = value.replace("\\,", ",").replace("\\n", " ").strip()
        elif name == "LOCATION":
            ev["location"] = value.replace("\\,", ",").strip()
        elif name == "DTSTART":
            ev["dtstart"] = value.strip()
        elif name == "DTEND":
            ev["dtend"] = value.strip()
        elif name == "ORGANIZER":
            ev["organizer_email"] = _mailto(value)
            ev["organizer_cn"] = params.get("CN", "")
        elif name == "ATTENDEE":
            ev["attendees"].append({
                "email": _mailto(value),
                "cn": params.get("CN", ""),
                "partstat": params.get("PARTSTAT", "NEEDS-ACTION").upper(),
            })
    if not have_event:
        return None
    ev["method"] = method
    ev.setdefault("uid", "")
    ev.setdefault("summary", "")
    ev.setdefault("organizer_email", "")
    return ev


def find_attendee(invite: dict, email: str) -> dict | None:
    """The invite's ATTENDEE entry matching ``email`` (case-insensitive)."""
    target = (email or "").strip().lower()
    for a in invite.get("attendees", []):
        if a.get("email", "").strip().lower() == target:
            return a
    return None


def _esc(value: str) -> str:
    return value.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")


def build_reply(invite: dict, *, attendee_email: str, attendee_cn: str = "",
                action: str, prodid: str = "-//Cloudy//RSVP//EN") -> str:
    """A ``METHOD:REPLY`` VCALENDAR answering ``invite`` with ``action``
    (accept | tentativelyAccept | decline), echoing the UID/SEQUENCE/ORGANIZER
    so the organizer's calendar can match it to the original request."""
    partstat = RSVP_PARTSTAT[action][0]
    dtstamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    cn = f';CN="{attendee_cn}"' if attendee_cn else ""
    org = invite.get("organizer_email", "")
    org_cn = f';CN="{invite["organizer_cn"]}"' if invite.get("organizer_cn") else ""
    lines = [
        "BEGIN:VCALENDAR",
        f"PRODID:{prodid}",
        "VERSION:2.0",
        "METHOD:REPLY",
        "BEGIN:VEVENT",
        f"UID:{invite.get('uid', '')}",
        f"SEQUENCE:{invite.get('sequence', 0)}",
    ]
    if org:
        lines.append(f"ORGANIZER{org_cn}:mailto:{org}")
    lines += [
        f"ATTENDEE;PARTSTAT={partstat}{cn}:mailto:{attendee_email}",
        f"DTSTAMP:{dtstamp}",
        f"SUMMARY:{_esc(invite.get('summary', ''))}",
        "REQUEST-STATUS:2.0;Success",
        "END:VEVENT",
        "END:VCALENDAR",
    ]
    return "\r\n".join(lines) + "\r\n"
