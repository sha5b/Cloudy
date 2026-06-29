# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Shahab Nedaei

import unittest
import unittest.mock

# graph.py pulls in core.auth.msal_graph -> `import msal`, an app runtime dep
# that isn't present in a minimal RPM build chroot. Skip rather than error there
# (the helpers under test are pure; they just live behind that import).
try:
    from cloudy.modules.microsoft365.graph import GraphClient, GraphError, _split_id
    _OK = True
except ImportError:
    _OK = False

_skip = unittest.skipUnless(_OK, "msal not installed (graph import unavailable)")


@_skip
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


@_skip
class TestMessageScope(unittest.TestCase):
    def test_personal_message(self):
        base, raw, scopes = GraphClient._message_scope("AAA")
        self.assertEqual(base, "/me")
        self.assertEqual(raw, "AAA")

    def test_shared_message(self):
        base, raw, _scopes = GraphClient._message_scope("shared:a@x.com:AAA")
        self.assertEqual(base, "/users/a@x.com")
        self.assertEqual(raw, "AAA")


@_skip
class TestCreateUpdateEvent(unittest.TestCase):
    def test_create_event_uses_local_wall_clock_and_timezone(self):
        client = GraphClient.__new__(GraphClient)
        with unittest.mock.patch.object(client, "_post") as post:
            # 12:00 UTC -> should be rendered as local wall-clock if test tz differs
            client.create_event(
                subject="S",
                start_iso="2026-06-16T12:00:00Z",
                end_iso="2026-06-16T13:00:00Z",
            )
            payload = post.call_args.args[1]
            self.assertEqual(payload["start"]["timeZone"], payload["end"]["timeZone"])
            self.assertIn(payload["start"]["dateTime"], ("2026-06-16T12:00:00", "2026-06-16T14:00:00"))
            self.assertIn(payload["end"]["dateTime"], ("2026-06-16T13:00:00", "2026-06-16T15:00:00"))
            self.assertFalse(payload["isAllDay"])

    def test_create_all_day_event_marks_midnight(self):
        client = GraphClient.__new__(GraphClient)
        with unittest.mock.patch.object(client, "_post") as post:
            client.create_event(
                subject="S",
                start_iso="2026-06-16T12:00:00Z",
                end_iso="2026-06-17T12:00:00Z",
                all_day=True,
            )
            payload = post.call_args.args[1]
            self.assertTrue(payload["isAllDay"])
            self.assertIn("T00:00:00", payload["start"]["dateTime"])
            self.assertIn("T00:00:00", payload["end"]["dateTime"])

    def test_update_event_rejects_group(self):
        client = GraphClient.__new__(GraphClient)
        with self.assertRaises(GraphError):
            client.update_event("group:g:e", subject="S", start_iso="2026-06-16T12:00:00Z", end_iso="2026-06-16T13:00:00Z")


@_skip
class TestListEvents(unittest.TestCase):
    def test_list_events_requests_local_timezone_header(self):
        client = GraphClient.__new__(GraphClient)
        with unittest.mock.patch.object(client, "_get_all", return_value=[]) as get_all:
            list(client.list_events("2026-06-16T00:00:00Z", "2026-06-17T00:00:00Z"))
            args = get_all.call_args.args
            self.assertEqual(len(args), 3)
            headers = args[2]
            self.assertIn("Prefer", headers)
            self.assertIn('outlook.timezone="', headers["Prefer"])

    def test_list_events_routes_specific_calendar(self):
        client = GraphClient.__new__(GraphClient)
        with unittest.mock.patch.object(client, "_get_all", return_value=[]) as get_all:
            list(client.list_events("2026-06-16T00:00:00Z", "2026-06-17T00:00:00Z",
                                    calendar_id="me:CAL1"))
            path = get_all.call_args.args[0]
            self.assertIn("/me/calendars/CAL1/calendarView", path)

    def test_list_events_routes_shared_calendar(self):
        client = GraphClient.__new__(GraphClient)
        with unittest.mock.patch.object(client, "_get_all", return_value=[]) as get_all:
            list(client.list_events("2026-06-16T00:00:00Z", "2026-06-17T00:00:00Z",
                                    calendar_id="shared:a@x.com:CAL2"))
            path, scope = get_all.call_args.args[0], get_all.call_args.args[1]
            self.assertIn("/users/a@x.com/calendars/CAL2/calendarView", path)
            self.assertIn("Calendars.ReadWrite.Shared", scope)

    def test_list_events_routes_group_calendar(self):
        client = GraphClient.__new__(GraphClient)
        with unittest.mock.patch.object(client, "_get_all", return_value=[{"id": "E1"}]) as get_all:
            events = list(client.list_events("2026-06-16T00:00:00Z", "2026-06-17T00:00:00Z",
                                             calendar_id="group:G1"))
            path = get_all.call_args.args[0]
            self.assertIn("/groups/G1/calendarView", path)
            self.assertEqual(events[0]["id"], "group:G1:E1")


@_skip
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
