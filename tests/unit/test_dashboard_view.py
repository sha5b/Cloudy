# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Shahab Nedaei
"""Pure-logic tests for the Dashboard's extracted helpers.

Cross-provider ISO stamps (Graph = local-naive strings, Google = offset/Z-aware
ones) must compare as parsed UTC datetimes, never as raw strings; unparseable
values sink below everything parsed. The Activity badge counts unread chats
only — starred channels carry no newness signal."""

import unittest
from datetime import date, datetime

import gi_setup  # pins GI versions; exposes AVAILABLE

if gi_setup.AVAILABLE:
    from gi.repository import Gtk

    from cloudy.widgets.dashboard_view import (
        _iso_sort_key,
        _local_date,
        _unread_chats,
    )
    from cloudy.widgets.format import esc
    from cloudy.widgets.source_nav import status_page

_skip = unittest.skipUnless(gi_setup.AVAILABLE,
                            "GTK/Adw typelibs unavailable (headless build)")


@_skip
class TestIsoSortKey(unittest.TestCase):
    def test_offset_and_z_stamps_compare_as_utc(self):
        # A raw-string sort would order these 09:00-05:00 < 11:30Z < 12:00+02:00
        # (string comparison), which is the wrong chronological order.
        stamps = ["2026-08-27T11:30:00Z", "2026-08-27T12:00:00+02:00",
                  "2026-08-27T09:00:00-05:00"]
        self.assertEqual(sorted(stamps, key=_iso_sort_key), [
            "2026-08-27T12:00:00+02:00",  # 10:00 UTC
            "2026-08-27T11:30:00Z",       # 11:30 UTC
            "2026-08-27T09:00:00-05:00",  # 14:00 UTC
        ])

    def test_same_instant_in_two_provider_shapes_sorts_equal(self):
        self.assertEqual(_iso_sort_key("2026-08-27T11:30:00Z"),
                         _iso_sort_key("2026-08-27T13:30:00+02:00"))

    def test_naive_graph_stamp_parses_to_a_datetime(self):
        flag, value = _iso_sort_key("2026-08-27T09:30:00")
        self.assertEqual(flag, 0)
        self.assertIsInstance(value, datetime)

    def test_unparseable_values_sink_below_parsed_ones(self):
        stamps = ["junk", "2026-08-27T11:30:00Z", ""]
        ordered = sorted(stamps, key=_iso_sort_key)
        self.assertEqual(ordered[0], "2026-08-27T11:30:00Z")
        self.assertEqual(set(ordered[1:]), {"junk", ""})


@_skip
class TestLocalDate(unittest.TestCase):
    def test_naive_graph_stamp_keeps_its_calendar_date(self):
        # Graph naive stamps are local wall-clock; converting to local must
        # never shift the date (any test-machine timezone).
        self.assertEqual(_local_date("2026-08-27T23:59:00"), date(2026, 8, 27))

    def test_aware_stamp_yields_the_local_date(self):
        stamp = "2026-08-27T23:30:00Z"
        expected = datetime.fromisoformat(
            stamp.replace("Z", "+00:00")).astimezone().date()
        self.assertEqual(_local_date(stamp), expected)

    def test_unparseable_is_none(self):
        self.assertIsNone(_local_date(""))
        self.assertIsNone(_local_date("not-a-stamp"))


@_skip
class TestUnreadChats(unittest.TestCase):
    def test_counts_only_chats_with_an_unread_marker(self):
        chats = [{"id": "1", "unread": True}, {"id": "2"},
                 {"id": "3", "unread": False}, {"id": "4", "unread": 2}]
        self.assertEqual(_unread_chats(chats), 2)

    def test_no_chats_is_zero(self):
        self.assertEqual(_unread_chats([]), 0)

    def test_starred_channels_cannot_inflate_the_badge(self):
        # The badge input is the chat list only; channel posts (the Activity
        # feed's other half) are counted nowhere in this computation.
        self.assertEqual(_unread_chats([{"id": "c"}, {"id": "d"}]), 0)


@_skip
class TestStatusPageEscapes(unittest.TestCase):
    """The shared status_page must escape markup itself — callers pass raw
    text (e.g. a str(error) off a Graph payload with '&'/'<')."""

    def setUp(self):
        Gtk.init_check()

    def test_title_and_description_are_escaped(self):
        page = status_page("dialog-warning-symbolic", "A & B <tag>",
                           "Couldn't load: <err>")
        self.assertEqual(page.get_title(), esc("A & B <tag>"))
        self.assertEqual(page.get_description(), esc("Couldn't load: <err>"))

    def test_description_is_cleared_when_missing(self):
        page = status_page("folder-symbolic", "Empty")
        self.assertEqual(page.get_description(), "")


if __name__ == "__main__":
    unittest.main()
