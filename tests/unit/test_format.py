# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Shahab Nedaei

import os
import time
import unittest

from cloudy.widgets.format import short_time


class PinnedTZ(unittest.TestCase):
    """Pin the process local zone so local-time conversion is deterministic.

    POSIX TZ strings avoid any tzdata dependency: "TEST-2" is a fixed UTC+2
    zone year-round (the offset's sign is west-positive, hence "-2")."""

    def setUp(self):
        self._tz = os.environ.get("TZ")
        os.environ["TZ"] = "TEST-2"
        time.tzset()

    def tearDown(self):
        if self._tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = self._tz
        time.tzset()


class TestShortTime(PinnedTZ):
    def test_z_suffix_converted_to_local(self):
        # 09:30 UTC is 11:30 in the pinned UTC+2 zone — the old raw-ISO slice
        # showed the provider's UTC wall clock ("2026-06-14 09:30").
        self.assertEqual(short_time("2026-06-14T09:30:00Z"), "2026-06-14 11:30")

    def test_offset_stamp_converted_to_local(self):
        self.assertEqual(short_time("2026-06-14T11:30:00+02:00"),
                         "2026-06-14 11:30")
        self.assertEqual(short_time("2026-06-14T04:30:00-05:00"),
                         "2026-06-14 11:30")

    def test_output_shape_preserved(self):
        # "YYYY-MM-DD HH:MM", just in local time.
        self.assertRegex(short_time("2026-06-14T09:30:00Z"),
                         r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$")

    def test_naive_stamp_treated_as_local_wall_clock(self):
        # Naive stamps are already local wall clock (Prefer: outlook.timezone).
        self.assertEqual(short_time("2026-06-14T11:30:00"), "2026-06-14 11:30")

    def test_unparsable_returned_unchanged(self):
        self.assertEqual(short_time("not a date"), "not a date")
        self.assertEqual(short_time(""), "")
        self.assertEqual(short_time("2026-06-14"), "2026-06-14")


if __name__ == "__main__":
    unittest.main()
