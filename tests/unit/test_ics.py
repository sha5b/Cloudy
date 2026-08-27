# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Shahab Nedaei

import unittest
from zoneinfo import ZoneInfo

from cloudy.core import ics
from cloudy.core.ics import build_reply, parse_invite


class TestParseInvite(unittest.TestCase):
    def test_basic_invite(self):
        text = (
            "BEGIN:VCALENDAR\r\n"
            "METHOD:REQUEST\r\n"
            "BEGIN:VEVENT\r\n"
            "UID:abc-123\r\n"
            "SUMMARY:Team sync\r\n"
            "DTSTART:20260616T120000Z\r\n"
            "DTEND:20260616T130000Z\r\n"
            "ORGANIZER;CN=Alice Smith:mailto:alice@example.com\r\n"
            "ATTENDEE;CN=Bob Jones;PARTSTAT=NEEDS-ACTION:mailto:bob@example.com\r\n"
            "END:VEVENT\r\n"
            "END:VCALENDAR\r\n"
        )
        ev = parse_invite(text)
        self.assertIsNotNone(ev)
        self.assertEqual(ev["method"], "REQUEST")
        self.assertEqual(ev["uid"], "abc-123")
        self.assertEqual(ev["summary"], "Team sync")
        self.assertEqual(ev["organizer_email"], "alice@example.com")
        self.assertEqual(ev["organizer_cn"], "Alice Smith")
        self.assertEqual(len(ev["attendees"]), 1)
        self.assertEqual(ev["attendees"][0]["email"], "bob@example.com")
        self.assertEqual(ev["attendees"][0]["cn"], "Bob Jones")

    def test_escaped_values_and_newlines(self):
        text = (
            "BEGIN:VCALENDAR\r\n"
            "METHOD:REQUEST\r\n"
            "BEGIN:VEVENT\r\n"
            "UID:e-1\r\n"
            "SUMMARY:Foo\\, Bar and Baz\\nMore\r\n"
            "DESCRIPTION:Line one\\nLine two\\, ok\r\n"
            "END:VEVENT\r\n"
            "END:VCALENDAR\r\n"
        )
        ev = parse_invite(text)
        self.assertEqual(ev["summary"], "Foo, Bar and Baz More")
        self.assertEqual(ev["description"], "Line one\nLine two, ok")

    def test_quoted_param_with_semicolon(self):
        text = (
            "BEGIN:VCALENDAR\r\n"
            "METHOD:REQUEST\r\n"
            "BEGIN:VEVENT\r\n"
            "UID:e-2\r\n"
            "ATTENDEE;CN=\"Doe; John\":mailto:john@example.com\r\n"
            "END:VEVENT\r\n"
            "END:VCALENDAR\r\n"
        )
        ev = parse_invite(text)
        self.assertEqual(ev["attendees"][0]["cn"], "Doe; John")


class TestTzidConversion(unittest.TestCase):
    """DTSTART/DTEND;TZID=… values are converted to the USER's local zone at
    parse time (downstream consumers treat naive values as local)."""

    def setUp(self):
        self._orig = ics._local_zone
        ics._local_zone = lambda: ZoneInfo("Europe/Vienna")

    def tearDown(self):
        ics._local_zone = self._orig

    def _invite(self, dtstart: str, dtend: str) -> dict:
        text = (
            "BEGIN:VCALENDAR\r\nMETHOD:REQUEST\r\nBEGIN:VEVENT\r\nUID:tz-1\r\n"
            f"DTSTART{dtstart}\r\nDTEND{dtend}\r\n"
            "END:VEVENT\r\nEND:VCALENDAR\r\n"
        )
        return parse_invite(text)

    def test_us_tzid_converted_to_local_zone(self):
        # 12:00 America/New_York (EDT, UTC-4) = 18:00 Europe/Vienna (CEST, +2).
        ev = self._invite(";TZID=America/New_York:20260616T120000",
                          ";TZID=America/New_York:20260616T130000")
        self.assertEqual(ev["dtstart"], "20260616T180000")
        self.assertEqual(ev["dtend"], "20260616T190000")

    def test_winter_offsets_differ(self):
        # 12:00 New_York (EST, UTC-5) in January = 18:00 Vienna (CET, +1).
        ev = self._invite(";TZID=America/New_York:20270116T120000",
                          ";TZID=America/New_York:20270116T130000")
        self.assertEqual(ev["dtstart"], "20270116T180000")

    def test_utc_and_unknown_tzid_untouched(self):
        ev = self._invite(":20260616T120000Z", ";TZID=Mars/Olympus:20260616T130000")
        self.assertEqual(ev["dtstart"], "20260616T120000Z")
        self.assertEqual(ev["dtend"], "20260616T130000")


class TestBuildReply(unittest.TestCase):
    def test_roundtrip_escaping(self):
        invite = {
            "uid": "u1", "sequence": 2, "summary": "A; B, C\nD",
            "organizer_email": "org@example.com", "organizer_cn": "",
        }
        reply = build_reply(invite, attendee_email="att@example.com",
                            action="accept")
        self.assertIn("UID:u1", reply)
        self.assertIn("PARTSTAT=ACCEPTED", reply)
        self.assertIn("A\\; B\\, C\\nD", reply)


if __name__ == "__main__":
    unittest.main()
