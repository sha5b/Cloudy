# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Shahab Nedaei
"""Day-span helpers: month-grid/agenda expansion and the EDS DTEND convention."""

import unittest

from gi_setup import gi  # noqa: F401 - pins GI versions before import

from cloudy.core.eds_publish import _vevent
from cloudy.widgets.month_grid import expand_days


class TestExpandDays(unittest.TestCase):
    WINDOW = ("2026-06-01", "2026-07-12")

    def test_single_day_event(self):
        ev = {"start": "2026-06-16T09:00:00", "end": "2026-06-16T10:00:00"}
        self.assertEqual(expand_days(ev, *self.WINDOW), ["2026-06-16"])

    def test_timed_multi_day_spans_all_days(self):
        ev = {"start": "2026-06-16T22:00:00", "end": "2026-06-18T03:00:00"}
        self.assertEqual(expand_days(ev, *self.WINDOW),
                         ["2026-06-16", "2026-06-17", "2026-06-18"])

    def test_google_all_day_inclusive_end(self):
        # google_client normalizes the end to the inclusive last day.
        ev = {"start": "2026-06-10", "end": "2026-06-12", "all_day": True}
        self.assertEqual(expand_days(ev, *self.WINDOW),
                         ["2026-06-10", "2026-06-11", "2026-06-12"])

    def test_graph_all_day_exclusive_midnight_end(self):
        # Graph ends an all-day event at the midnight AFTER the last day.
        ev = {"start": "2026-06-10T00:00:00", "end": "2026-06-13T00:00:00",
              "all_day": True}
        self.assertEqual(expand_days(ev, *self.WINDOW),
                         ["2026-06-10", "2026-06-11", "2026-06-12"])

    def test_year_long_event_clipped_to_window(self):
        ev = {"start": "2026-01-01", "end": "2026-12-31", "all_day": True}
        days = expand_days(ev, *self.WINDOW)
        self.assertEqual(days[0], "2026-06-01")
        self.assertEqual(days[-1], "2026-07-12")
        self.assertEqual(len(days), 42)  # the 6-week grid, not 365 entries

    def test_event_outside_window_empty(self):
        ev = {"start": "2026-08-05T09:00:00", "end": "2026-08-05T10:00:00"}
        self.assertEqual(expand_days(ev, *self.WINDOW), [])

    def test_bad_dates_degrade_to_start_day(self):
        ev = {"start": "not-a-date", "end": "2026-06-18"}
        self.assertEqual(expand_days(ev, *self.WINDOW), ["not-a-date"])
        self.assertEqual(expand_days({}, *self.WINDOW), [])


class TestEdsAllDayDtend(unittest.TestCase):
    def _dt(self, ev: dict) -> str:
        block = _vevent("uid-x", ev)
        self.assertIsNotNone(block)
        for line in block.split("\r\n"):
            if line.startswith("DTEND;VALUE=DATE:"):
                return line.split(":", 1)[1]
        self.fail("no DTEND in block")

    def test_one_day_event_dtend_next_day(self):
        # Inclusive end == start → DTEND = DTSTART + 1 day (iCal is exclusive).
        ev = {"subject": "Dentist", "all_day": True,
              "start": "2026-07-01", "end": "2026-07-01"}
        self.assertEqual(self._dt(ev), "20260702")

    def test_three_day_span_dtend_day_four(self):
        ev = {"subject": "Trip", "all_day": True,
              "start": "2026-07-01", "end": "2026-07-03"}
        self.assertEqual(self._dt(ev), "20260704")

    def test_graph_midnight_end_steps_back_first(self):
        # Graph's exclusive 2026-07-04T00:00:00 covers through 07-03.
        ev = {"subject": "Conf", "all_day": True,
              "start": "2026-07-01T00:00:00", "end": "2026-07-04T00:00:00"}
        self.assertEqual(self._dt(ev), "20260704")

    def test_missing_end_defaults_to_one_day(self):
        ev = {"subject": "Bare", "all_day": True, "start": "2026-07-01"}
        self.assertEqual(self._dt(ev), "20260702")


if __name__ == "__main__":
    unittest.main()
