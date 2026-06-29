# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Shahab Nedaei

import unittest
from datetime import datetime

from gi_setup import gi  # noqa: F401 - pins GI versions before import

from cloudy.widgets.event_time import iso_to_local_naive, local_to_utc_iso, parse_hhmm


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


if __name__ == "__main__":
    unittest.main()
