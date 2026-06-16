# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Shahab Nedaei
"""GoogleClient: normalization + multi-calendar aggregation/routing (no network)."""

import unittest

from cloudy.modules.gmail.google_client import GoogleClient


class TestNormalization(unittest.TestCase):
    def test_message_from_json_unescapes_and_reads_labels(self):
        msg = {
            "id": "m1",
            "internalDate": "0",
            "snippet": "Tom &amp; Jerry",
            "labelIds": ["UNREAD", "IMPORTANT", "STARRED"],
            "payload": {"headers": [
                {"name": "Subject", "value": "Q&amp;A"},
                {"name": "From", "value": "A &lt;a@x.com&gt;"},
            ]},
        }
        row = GoogleClient._message_from_json(msg)
        self.assertEqual(row["subject"], "Q&A")
        self.assertEqual(row["from"], "A <a@x.com>")
        self.assertEqual(row["preview"], "Tom & Jerry")
        self.assertFalse(row["is_read"])      # UNREAD present
        self.assertTrue(row["important"])
        self.assertTrue(row["starred"])

    def test_message_read_when_no_unread_label(self):
        row = GoogleClient._message_from_json({"id": "m", "labelIds": ["INBOX"]})
        self.assertTrue(row["is_read"])
        self.assertEqual(row["subject"], "(no subject)")

    def test_event_from_json_timed(self):
        e = {"id": "e1", "summary": "Standup",
             "start": {"dateTime": "2026-06-16T09:00:00Z"},
             "end": {"dateTime": "2026-06-16T09:15:00Z"}, "location": "Room"}
        row = GoogleClient._event_from_json(e)
        self.assertEqual(row["start"], "2026-06-16T09:00:00Z")
        self.assertFalse(row["all_day"])
        self.assertEqual(row["location"], "Room")

    def test_event_from_json_all_day(self):
        e = {"id": "e2", "summary": "Holiday",
             "start": {"date": "2026-12-25"}, "end": {"date": "2026-12-26"}}
        row = GoogleClient._event_from_json(e)
        self.assertTrue(row["all_day"])
        self.assertEqual(row["start"], "2026-12-25")

    def test_chat_message_row(self):
        m = {"name": "spaces/A/messages/1", "text": "hi",
             "sender": {"displayName": "Bob &amp; co"}, "createTime": "t",
             "attachment": [{"contentName": "f.png", "downloadUri": "u",
                             "contentType": "image/png"}]}
        row = GoogleClient._chat_message_row(m)
        self.assertEqual(row["id"], "spaces/A/messages/1")
        self.assertEqual(row["from"], "Bob & co")
        self.assertFalse(row["is_mine"])
        self.assertEqual(row["attachments"][0]["name"], "f.png")


class TestCalendarIds(unittest.TestCase):
    def test_primary_id_unwrapped(self):
        self.assertEqual(GoogleClient._wrap_event_id("primary", "e"), "e")
        self.assertEqual(GoogleClient._unwrap_event_id("e"), ("primary", "e"))

    def test_non_primary_roundtrip(self):
        cal = "holidays@group.v.calendar.google.com"
        wrapped = GoogleClient._wrap_event_id(cal, "ev1")
        self.assertTrue(wrapped.startswith("gcal\x1f"))
        self.assertEqual(GoogleClient._unwrap_event_id(wrapped), (cal, "ev1"))

    def test_cal_path_encodes_specials(self):
        self.assertEqual(GoogleClient._cal_path("a@b#c"), "a%40b%23c")
        self.assertEqual(GoogleClient._cal_path(""), "primary")


class _FakeGC(GoogleClient):
    """GoogleClient with a scripted HTTP layer for list/route tests."""

    def __init__(self):
        super().__init__(lambda scopes: "token")
        self.writes = []

    def _get(self, url, scopes):
        if "calendarList" in url:
            return {"items": [
                {"id": "primary", "summary": "Me", "primary": True,
                 "selected": True, "accessRole": "owner"},
                {"id": "hol@group.v.calendar.google.com", "summary": "Holidays",
                 "selected": True, "accessRole": "reader"},
                {"id": "hidden@x", "summary": "Hidden", "selected": False,
                 "accessRole": "owner"},
            ]}
        if "hol%40group" in url:
            return {"items": [{"id": "h1", "summary": "Xmas",
                               "start": {"date": "2026-12-25"},
                               "end": {"date": "2026-12-26"}}]}
        return {"items": [{"id": "p1", "summary": "Standup",
                           "start": {"dateTime": "2026-06-16T09:00:00Z"},
                           "end": {"dateTime": "2026-06-16T09:15:00Z"}}]}

    def _patch(self, url, body, scopes):
        self.writes.append(("PATCH", url))
        return {}

    def _delete(self, url, scopes):
        self.writes.append(("DELETE", url))


class TestMultiCalendar(unittest.TestCase):
    def setUp(self):
        self.gc = _FakeGC()
        self.events = self.gc.list_events("2026-06-01T00:00:00Z",
                                          "2026-06-30T23:59:59Z")

    def test_aggregates_shown_calendars(self):
        subjects = {e["subject"] for e in self.events}
        self.assertIn("Standup", subjects)
        self.assertIn("Xmas", subjects)

    def test_excludes_hidden_calendar(self):
        self.assertFalse(any(e.get("calendar") == "Hidden" for e in self.events))

    def test_tags_calendar_name_and_wraps_nonprimary_id(self):
        standup = next(e for e in self.events if e["subject"] == "Standup")
        xmas = next(e for e in self.events if e["subject"] == "Xmas")
        self.assertEqual(standup["id"], "p1")            # primary stays bare
        self.assertEqual(standup["calendar"], "Me")
        self.assertTrue(xmas["id"].startswith("gcal\x1f"))
        self.assertEqual(xmas["calendar"], "Holidays")

    def test_sorted_by_start(self):
        starts = [e["start"] for e in self.events]
        self.assertEqual(starts, sorted(starts))

    def test_edit_and_delete_route_to_owning_calendar(self):
        xmas = next(e for e in self.events if e["subject"] == "Xmas")
        self.gc.update_event(xmas["id"], subject="x",
                             start_iso="2026-12-25T00:00:00Z",
                             end_iso="2026-12-26T00:00:00Z", all_day=True)
        self.gc.delete_event(xmas["id"])
        self.assertTrue(all("hol%40group" in url for _, url in self.gc.writes))
        self.assertEqual(len(self.gc.writes), 2)


class TestFolders(unittest.TestCase):
    def test_system_labels_lead_then_user_alpha(self):
        class GC(GoogleClient):
            def _get(self, url, scopes):
                return {"labels": [
                    {"id": "INBOX", "type": "system"},
                    {"id": "SENT", "type": "system"},
                    {"id": "Lbl_z", "name": "Zebra", "type": "user"},
                    {"id": "Lbl_a", "name": "apple", "type": "user"},
                ]}

        folders = GC(lambda s: "t").list_folders()
        names = [f["name"] for f in folders]
        self.assertEqual(names[0], "Inbox")          # system order preserved
        self.assertEqual(names.index("apple"), names.index("Zebra") - 1)  # alpha
        self.assertTrue(all(f["unread"] == 0 for f in folders))


if __name__ == "__main__":
    unittest.main()
