# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Shahab Nedaei
"""Shared date/time helpers for the event editor surfaces.

Both the new-event editor (``event_compose``) and the inline event editor
(``event_window``) collect a naive *local* wall-clock pick (a ``Gtk.Calendar``
day + ``HH:MM`` entries) and must convert it to the UTC ISO-8601 slot the Graph
and Google clients send. Keep that conversion in one place.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone


def local_tz_key() -> str:
    """Best-effort local IANA timezone key (``""`` when unresolvable).

    Same detection graph_calendar/eds_publish use: the reliable source on Linux
    is the /etc/localtime symlink into the zoneinfo db. A key (not an offset)
    is what DST-correct conversions need."""
    tz = os.environ.get("TZ", "")
    if "/" in tz:  # a real IANA name, not an abbreviation
        return tz
    try:
        target = os.path.realpath("/etc/localtime")
        if "/zoneinfo/" in target:
            return target.split("/zoneinfo/", 1)[1]
    except OSError:
        pass
    return ""


def _local_tzinfo():
    """A tzinfo for the local zone — a ``ZoneInfo`` when the key resolves
    (so the offset is computed for the TARGET date, DST included), else today's
    fixed offset as a graceful fallback."""
    key = local_tz_key()
    if key:
        try:
            from zoneinfo import ZoneInfo

            return ZoneInfo(key)
        except Exception:  # noqa: BLE001 - unresolvable key → fixed offset
            pass
    return datetime.now().astimezone().tzinfo


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
    # NOT datetime.now().astimezone().tzinfo: that is a fixed offset for TODAY,
    # which drifted every save made for a date in the other DST half-year.
    # The resolved IANA zone computes the offset for the target date.
    return (dt.replace(tzinfo=_local_tzinfo()).astimezone(timezone.utc)
            .strftime("%Y-%m-%dT%H:%M:%SZ"))
