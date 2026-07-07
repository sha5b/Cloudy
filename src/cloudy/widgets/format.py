# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Shahab Nedaei
"""Small display formatters shared by the mail/calendar/dashboard views."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from gettext import gettext as _

_ANGLE_RE = re.compile(r"^(.*?)<([^>]+)>\s*$")


def esc(text: str) -> str:
    """Escape ``& < >`` for Pango markup labels."""
    return ((text or "").replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))


def sender_name(value: str) -> str:
    """Turn an RFC 5322 'From' into a clean display name.

    'Ray Smith <ray@x.com>'      -> 'Ray Smith'
    '"Smith, Ray" <ray@x.com>'   -> 'Smith, Ray'
    'ray@x.com'                  -> 'ray@x.com'
    """
    value = (value or "").strip()
    m = _ANGLE_RE.match(value)
    if m:
        name = m.group(1).strip().strip('"').strip()
        return name or m.group(2).strip()
    return value


def sender_email(value: str) -> str:
    """Pull the bare address out of an RFC 5322 'From'/'To' entry.

    'Ray Smith <ray@x.com>'      -> 'ray@x.com'
    'ray@x.com'                  -> 'ray@x.com'
    'Ray Smith'                  -> ''      (no address present)
    """
    value = (value or "").strip()
    m = _ANGLE_RE.match(value)
    if m:
        return m.group(2).strip()
    return value if "@" in value else ""


def short_time(iso: str) -> str:
    """'2026-06-14T09:30:00Z' -> '2026-06-14 09:30'."""
    if not iso or "T" not in iso:
        return iso
    date, _sep, rest = iso.partition("T")
    return f"{date} {rest[:5]}"


def _parse_iso(iso: str):
    if not iso or "T" not in iso:
        return None
    txt = iso.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(txt)
    except ValueError:
        # Graph sometimes returns 7-digit fractional seconds; trim the fraction
        # and retry, preserving any trailing timezone offset.
        head, _sep, tail = txt.partition(".")
        tz = ""
        for marker in ("+", "-"):
            if marker in tail:
                tz = marker + tail.split(marker, 1)[1]
                break
        try:
            dt = datetime.fromisoformat(head + tz)
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone()  # local time


def parse_iso_utc(iso: str):
    """The same tolerant ISO-8601 parse, normalized to UTC — the one parser for
    comparisons against ``datetime.now(timezone.utc)`` (calendar live markers,
    dashboard countdowns, notification reminders)."""
    dt = _parse_iso(iso)
    return dt.astimezone(timezone.utc) if dt is not None else None


def relative_time(iso: str) -> str:
    """Conversational timestamp: today → 'HH:MM', yesterday → 'Yesterday',
    this week → weekday, this year → '14 Jun', else 'YYYY-MM-DD'."""
    dt = _parse_iso(iso)
    if dt is None:
        return iso or ""
    today = datetime.now().astimezone().date()
    day = dt.date()
    if day == today:
        return dt.strftime("%H:%M")
    if day == today - timedelta(days=1):
        return _("Yesterday")
    delta = (today - day).days
    if 0 < delta < 7:
        return dt.strftime("%a")  # Mon, Tue, …
    if day.year == today.year:
        return dt.strftime("%-d %b")  # 14 Jun
    return dt.strftime("%Y-%m-%d")
