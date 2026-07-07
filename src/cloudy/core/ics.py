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

import re
from datetime import datetime, timezone

# A conferencing link buried in a free-text DESCRIPTION (Teams/Meet/Zoom/Webex),
# used as a fallback when no machine-readable URL property is present.
_JOIN_RE = re.compile(
    r"https?://[^\s>\"]*(?:teams\.microsoft\.com/l/meetup-join|teams\.live\.com/meet"
    r"|meet\.google\.com|zoom\.us/j/|\.zoom\.us/j/|webex\.com)[^\s>\"]*",
    re.IGNORECASE)

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


def _unescape(value: str) -> str:
    """Undo RFC 5545 escaping (\\, \\;, \\, \\n/\\N → newline)."""
    out: list[str] = []
    i = 0
    while i < len(value):
        ch = value[i]
        if ch == "\\" and i + 1 < len(value):
            nxt = value[i + 1]
            if nxt in "\\;,nN":
                out.append("\n" if nxt in "nN" else nxt)
                i += 2
                continue
        out.append(ch)
        i += 1
    return "".join(out)


def _split_params(head: str) -> list[str]:
    """Split ``NAME;P1=V1;P2="V;2"`` at semicolons, respecting quoted values."""
    parts: list[str] = []
    cur: list[str] = []
    in_quotes = False
    for ch in head:
        if ch == '"':
            in_quotes = not in_quotes
            cur.append(ch)
        elif ch == ";" and not in_quotes:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    if cur or parts:
        parts.append("".join(cur))
    return parts


def _split(line: str) -> tuple[str, dict, str]:
    """``NAME;P=v:value`` → (NAME upper, {param: value}, value).

    Handles escaped colons and quoted parameters with embedded separators."""
    # Split on the first *unescaped* colon.
    idx = 0
    while idx < len(line):
        if line[idx] == ":" and (idx == 0 or line[idx - 1] != "\\"):
            break
        idx += 1
    else:
        idx = len(line)
    head = line[:idx]
    value = line[idx + 1:]

    parts = _split_params(head)
    if not parts:
        return "", {}, value
    name = parts[0].upper()
    params: dict[str, str] = {}
    for p in parts[1:]:
        if "=" not in p:
            continue
        k, v = p.split("=", 1)
        if v.startswith('"') and v.endswith('"'):
            v = v[1:-1]
        params[k.upper()] = _unescape(v)
    return name, params, _unescape(value)


def _mailto(value: str) -> str:
    return value.split(":", 1)[1].strip() if value.lower().startswith("mailto:") else value.strip()


def parse_invite(text: str) -> dict | None:
    """Parse a VCALENDAR string. Returns the invite dict when it carries a
    VEVENT (with ``method``, ``uid``, ``sequence``, ``summary``, ``location``,
    ``dtstart``/``dtend``, ``all_day``, ``status``, ``join_url``, ``description``,
    ``organizer_email``/``organizer_cn`` and ``attendees`` =
    ``[{email, cn, partstat}]``), else ``None``."""
    method = ""
    in_event = False
    ev: dict = {"sequence": 0, "attendees": [], "all_day": False,
                "status": "", "join_url": "", "description": ""}
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
            try:
                ev["sequence"] = int(value.strip() or 0)
            except ValueError:
                pass  # a garbled SEQUENCE must not abort the whole invite
        elif name == "SUMMARY":
            ev["summary"] = value.replace("\n", " ").strip()
        elif name == "LOCATION":
            ev["location"] = value.strip()
        elif name == "STATUS":
            ev["status"] = value.strip().upper()
        elif name == "DTSTART":
            ev["dtstart"] = value.strip()
            ev["all_day"] = params.get("VALUE", "").upper() == "DATE"
        elif name == "DTEND":
            ev["dtend"] = value.strip()
        elif name == "DESCRIPTION":
            ev["description"] = value.strip()
        # Machine-readable conferencing links Microsoft/Google add to invites.
        elif name in ("URL", "X-MICROSOFT-SKYPETEAMSMEETINGURL", "X-GOOGLE-CONFERENCE"):
            if value.strip().lower().startswith("http") and not ev["join_url"]:
                ev["join_url"] = value.strip()
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
    if not ev["join_url"]:  # fall back to a link in the free-text body/location
        match = _JOIN_RE.search(f"{ev.get('location', '')}\n{ev['description']}")
        if match:
            ev["join_url"] = match.group(0)
    return ev


def ical_to_iso(value: str) -> str:
    """A basic-format iCalendar date/date-time (``20260623`` /
    ``20260623T140000[Z]``) as an ISO-8601 string the calendar clients accept,
    or ``""`` when unparsable. A trailing ``Z`` stays UTC; a naive value is
    taken as local wall-clock; a bare date becomes local midnight (Google
    requires an offset on dateTime values, so naive results are made aware)."""
    txt = (value or "").strip()
    try:
        if "T" not in txt:
            return datetime.strptime(txt, "%Y%m%d").astimezone().isoformat()
        utc = txt.endswith("Z")
        dt = datetime.strptime(txt.rstrip("Z"), "%Y%m%dT%H%M%S")
    except ValueError:
        return ""
    if utc:
        return dt.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")
    return dt.astimezone().isoformat()


def find_attendee(invite: dict, email: str) -> dict | None:
    """The invite's ATTENDEE entry matching ``email`` (case-insensitive)."""
    target = (email or "").strip().lower()
    for a in invite.get("attendees", []):
        if a.get("email", "").strip().lower() == target:
            return a
    return None


def _esc(value: str) -> str:
    """Escape property values per RFC 5545."""
    return (value.replace("\\", "\\\\")
            .replace(";", "\\;")
            .replace(",", "\\,")
            .replace("\n", "\\n"))


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
