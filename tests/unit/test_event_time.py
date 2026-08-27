# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Shahab Nedaei

import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from gi_setup import gi  # noqa: F401 - pins GI versions before import

from cloudy.widgets import event_time
from cloudy.widgets.event_time import (
    iso_to_local_naive,
    local_to_utc_iso,
    parse_hhmm,
)


class TestParseHhmm(unittest.TestCase):
    def test_valid_time(self):
        self.assertEqual(parse_hhmm("14:30", (0, 0)), (14, 30))

    def test_fallback_on_malformed(self):
        self.assertEqual(parse_hhmm("abc", (9, 0)), (9, 0))
        self.assertEqual(parse_hhmm("25:00", (9, 0)), (9, 0))


class TestIsoToLocalNaive(unittest.TestCase):
    def test_z_utc_converted_to_local(self):
        dt = iso_to_local_naive("2026-06-29T12:00:00Z")
        self.assertIsNotNone(dt)
        self.assertIsNone(dt.tzinfo)

    def test_offset_preserved(self):
        dt = iso_to_local_naive("2026-06-29T12:00:00+02:00")
        self.assertIsNotNone(dt)
        self.assertIsNone(dt.tzinfo)

    def test_empty_returns_none(self):
        self.assertIsNone(iso_to_local_naive(""))


class TestLocalToUtcIso(unittest.TestCase):
    def test_all_day_uses_utc_midnight_of_picked_date(self):
        dt = datetime(2026, 6, 15, 22, 30)  # local wall-clock pick
        iso = local_to_utc_iso(dt, all_day=True)
        self.assertTrue(iso.endswith("Z"))
        self.assertEqual(iso[:10], "2026-06-15")

    def test_timed_converts_local_to_utc(self):
        dt = datetime(2026, 6, 15, 12, 0)
        iso = local_to_utc_iso(dt, all_day=False)
        self.assertTrue(iso.endswith("Z"))
        # The exact time depends on the host timezone, but it must parse back.
        back = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        self.assertIsNotNone(back.tzinfo)


class TestDstSafeConversion(unittest.TestCase):
    """local_to_utc_iso must use the offset of the TARGET date, not today's
    (a fixed offset drifted every save for the other DST half-year)."""

    def setUp(self):
        self._orig = event_time.local_tz_key
        event_time.local_tz_key = lambda: "Europe/Vienna"

    def tearDown(self):
        event_time.local_tz_key = self._orig

    def test_summer_date_uses_dst_offset(self):
        # Vienna in July: CEST = UTC+02:00 → 12:00 local = 10:00 UTC.
        iso = local_to_utc_iso(datetime(2026, 7, 15, 12, 0), all_day=False)
        self.assertEqual(iso, "2026-07-15T10:00:00Z")

    def test_winter_date_uses_standard_offset(self):
        # Vienna in January: CET = UTC+01:00 → 12:00 local = 11:00 UTC.
        iso = local_to_utc_iso(datetime(2027, 1, 15, 12, 0), all_day=False)
        self.assertEqual(iso, "2027-01-15T11:00:00Z")

    def test_zone_resolved_from_key(self):
        self.assertEqual(event_time._local_tzinfo(), ZoneInfo("Europe/Vienna"))


if __name__ == "__main__":
    unittest.main()
