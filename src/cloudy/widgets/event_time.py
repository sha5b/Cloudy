# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Shahab Nedaei
"""Shared date/time helpers for the event editor surfaces.

Both the new-event editor (``event_compose``) and the inline event editor
(``event_window``) collect a naive *local* wall-clock pick (a ``Gtk.Calendar``
day + ``HH:MM`` entries) and must convert it to the UTC ISO-8601 slot the Graph
and Google clients send. Keep that conversion in one place.
"""

from __future__ import annotations

from datetime import datetime, timezone


def iso_to_local_naive(iso: str) -> datetime | None:
    """Parse an ISO start/end to a naive local datetime for editor prefill
    (the editors treat their fields as local wall-clock)."""
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone()
    return dt.replace(tzinfo=None)


def parse_hhmm(text: str, fallback: tuple[int, int]) -> tuple[int, int]:
    """Parse an ``HH:MM`` entry, falling back on anything malformed."""
    try:
        h, _sep, m = text.strip().partition(":")
        hh, mm = int(h), int(m or 0)
        if 0 <= hh < 24 and 0 <= mm < 60:
            return hh, mm
    except (ValueError, TypeError):
        pass
    return fallback


def local_to_utc_iso(dt: datetime, *, all_day: bool) -> str:
    """Naive local wall-clock → UTC ISO-8601 (trailing ``Z``).

    All-day events are returned as the picked calendar date at UTC midnight;
    callers slice ``[:10]`` to obtain the date. Using the local midnight with a
    ``Z`` suffix shifted events east of UTC to the previous calendar day.
    """
    if all_day:
        return datetime(dt.year, dt.month, dt.day, 0, 0,
                        tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    local_tz = datetime.now().astimezone().tzinfo
    return (dt.replace(tzinfo=local_tz).astimezone(timezone.utc)
            .strftime("%Y-%m-%dT%H:%M:%SZ"))
