# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Shahab Nedaei
"""Graph client pure helpers: id splitting, scope routing, event normalization."""

import unittest

from cloudy.modules.microsoft365.graph import GraphClient, GraphError, _split_id


class TestSplitId(unittest.TestCase):
    def test_splits_exact_count(self):
        self.assertEqual(_split_id("shared:a@x.com:AAA", 3),
                         ["shared", "a@x.com", "AAA"])

    def test_keeps_trailing_colons_in_last_part(self):
        # id portion may itself contain ':' — only the first count-1 are split
        self.assertEqual(_split_id("group:gid:th:read", 3),
                         ["group", "gid", "th:read"])

    def test_malformed_raises_grapherror(self):
        with self.assertRaises(GraphError):
            _split_id("justone", 3)


class TestMessageScope(unittest.TestCase):
    def test_personal_message(self):
        base, raw, scopes = GraphClient._message_scope("AAA")
        self.assertEqual(base, "/me")
        self.assertEqual(raw, "AAA")

    def test_shared_message(self):
        base, raw, _scopes = GraphClient._message_scope("shared:a@x.com:AAA")
        self.assertEqual(base, "/users/a@x.com")
        self.assertEqual(raw, "AAA")


class TestEventsFromJson(unittest.TestCase):
    def test_normalizes_fields(self):
        data = {"value": [{
            "id": "e1", "subject": "Sync",
            "start": {"dateTime": "2026-06-16T09:00:00"},
            "end": {"dateTime": "2026-06-16T10:00:00"},
            "location": {"displayName": "Room 1"}, "isAllDay": False,
        }]}
        out = GraphClient._events_from_json(data)
        self.assertEqual(len(out), 1)
        ev = out[0]
        self.assertEqual(ev["subject"], "Sync")
        self.assertEqual(ev["location"], "Room 1")
        self.assertFalse(ev["all_day"])

    def test_missing_location_defaults_blank(self):
        data = {"value": [{"id": "e", "start": {}, "end": {}}]}
        ev = GraphClient._events_from_json(data)[0]
        self.assertEqual(ev["location"], "")
        self.assertEqual(ev["subject"], "(no title)")

    def test_empty(self):
        self.assertEqual(GraphClient._events_from_json({}), [])


if __name__ == "__main__":
    unittest.main()
