# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Fiber Elements
"""Small display formatters shared by the mail/calendar/dashboard views."""

from __future__ import annotations

import re

_ANGLE_RE = re.compile(r"^(.*?)<([^>]+)>\s*$")


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


def short_time(iso: str) -> str:
    """'2026-06-14T09:30:00Z' -> '2026-06-14 09:30'."""
    if not iso or "T" not in iso:
        return iso
    date, _, rest = iso.partition("T")
    return f"{date} {rest[:5]}"
