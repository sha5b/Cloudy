# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Shahab Nedaei

import unittest

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
