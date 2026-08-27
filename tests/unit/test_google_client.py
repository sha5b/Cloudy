# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Shahab Nedaei

import base64
import unittest
from zoneinfo import ZoneInfo

from cloudy.modules.gmail import google_client
from cloudy.modules.gmail.google_client import (
    GoogleClient,
    GoogleError,
    _decode_b64url,
    _part_charset,
)


class TestBodyCharset(unittest.TestCase):
    def test_latin1_body_decodes_with_declared_charset(self):
        raw = "Grüße & café".encode("iso-8859-1")
        data = base64.urlsafe_b64encode(raw).decode()
        self.assertEqual(_decode_b64url(data, "iso-8859-1"), "Grüße & café")

    def test_bad_charset_falls_back_to_utf8(self):
        data = base64.urlsafe_b64encode("hällo".encode()).decode()
        self.assertEqual(_decode_b64url(data, "no-such-charset"), "hällo")

    def test_part_charset_parsed_from_content_type(self):
        part = {"headers": [
            {"name": "Content-Type",
             "value": 'text/plain; charset="ISO-8859-1"; format=flowed'}]}
        self.assertEqual(_part_charset(part), "ISO-8859-1")

    def test_part_charset_missing(self):
        self.assertEqual(_part_charset({"headers": []}), "")


class TestNormalization(unittest.TestCase):
    def setUp(self):
        # Pin the local zone so timed-event normalization is deterministic.
        self._orig = google_client._local_zone
        google_client._local_zone = lambda: ZoneInfo("Europe/Vienna")

    def tearDown(self):
        google_client._local_zone = self._orig

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
        # Timed dateTimes are normalized to naive LOCAL wall-clock (Graph
        # convention): June = CEST (+02:00) in the pinned Vienna zone.
        self.assertEqual(row["start"], "2026-06-16T11:00:00")
        self.assertEqual(row["end"], "2026-06-16T11:15:00")
        self.assertFalse(row["all_day"])
        self.assertEqual(row["location"], "Room")

    def test_event_from_json_timed_winter_offset(self):
        # January = CET (+01:00): the DST offset of the TARGET date is used.
        e = {"id": "e9", "summary": "Review",
             "start": {"dateTime": "2027-01-16T09:00:00Z"},
             "end": {"dateTime": "2027-01-16T09:30:00Z"}}
        row = GoogleClient._event_from_json(e)
        self.assertEqual(row["start"], "2027-01-16T10:00:00")
        self.assertEqual(row["end"], "2027-01-16T10:30:00")

    def test_event_from_json_offset_datetime(self):
        # An explicit offset is honored (converted through the same instant).
        e = {"id": "e10", "summary": "Off",
             "start": {"dateTime": "2026-06-16T03:00:00-04:00"},
             "end": {"dateTime": "2026-06-16T04:00:00-04:00"}}
        row = GoogleClient._event_from_json(e)
        self.assertEqual(row["start"], "2026-06-16T09:00:00")
        self.assertEqual(row["end"], "2026-06-16T10:00:00")

    def test_event_from_json_all_day(self):
        e = {"id": "e2", "summary": "Holiday",
             "start": {"date": "2026-12-25"}, "end": {"date": "2026-12-26"}}
        row = GoogleClient._event_from_json(e)
        self.assertTrue(row["all_day"])
        self.assertEqual(row["start"], "2026-12-25")
        # Google returns exclusive end dates; we normalize to the last actual day.
        self.assertEqual(row["end"], "2026-12-25")

    def test_event_from_json_all_day_multi_day(self):
        e = {"id": "e3", "summary": "Trip",
             "start": {"date": "2026-06-10"}, "end": {"date": "2026-06-13"}}
        row = GoogleClient._event_from_json(e)
        self.assertTrue(row["all_day"])
        self.assertEqual(row["start"], "2026-06-10")
        self.assertEqual(row["end"], "2026-06-12")

    @staticmethod
    def _client(me: str = "users/self"):
        # _chat_message_row needs the cached own-user id (used for is_mine).
        client = GoogleClient.__new__(GoogleClient)
        client._chat_me = me
        return client

    def test_chat_message_row(self):
        m = {"name": "spaces/A/messages/1", "text": "hi",
             "sender": {"displayName": "Bob &amp; co", "name": "users/bob"},
             "createTime": "t",
             "attachments": [{"contentName": "f.png", "downloadUri": "u",
                              "contentType": "image/png"}]}
        row = self._client()._chat_message_row(m)
        self.assertEqual(row["id"], "spaces/A/messages/1")
        self.assertEqual(row["from"], "Bob & co")
        self.assertFalse(row["is_mine"])
        self.assertEqual(row["attachments"][0]["name"], "f.png")

    def test_chat_message_row_own_message(self):
        m = {"name": "spaces/A/messages/3", "text": "hi",
             "sender": {"displayName": "Me", "name": "users/self"},
             "createTime": "t"}
        self.assertTrue(self._client()._chat_message_row(m)["is_mine"])
        # Unknown own-id must never claim someone else's message as ours.
        self.assertFalse(self._client(me="")._chat_message_row(m)["is_mine"])

    def test_chat_message_row_legacy_attachment_key(self):
        # Older payloads may use the singular "attachment" key; keep fallback.
        m = {"name": "spaces/A/messages/2", "text": "hi",
             "sender": {"displayName": "Bob"}, "createTime": "t",
             "attachment": [{"contentName": "legacy.png", "downloadUri": "u",
                             "contentType": "image/png"}]}
        row = self._client()._chat_message_row(m)
        self.assertEqual(row["attachments"][0]["name"], "legacy.png")


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


class TestAttendeeSlots(unittest.TestCase):
    def test_string_defaults_to_required(self):
        self.assertEqual(google_client._attendee_slot("a@b.c"),
                         {"email": "a@b.c", "optional": False})

    def test_dict_type_maps_to_optional_bool(self):
        self.assertEqual(google_client._attendee_slot({"email": "a@b.c"}),
                         {"email": "a@b.c", "optional": False})
        self.assertEqual(
            google_client._attendee_slot({"email": "a@b.c", "type": "optional"}),
            {"email": "a@b.c", "optional": True})

    def test_empty_entry_dropped(self):
        self.assertIsNone(google_client._attendee_slot(""))
        self.assertIsNone(google_client._attendee_slot({"email": ""}))


class TestDraftSave(unittest.TestCase):
    def test_new_draft_posts_update_puts(self):
        class GC(GoogleClient):
            def __init__(self):
                super().__init__(lambda s: "t")
                self.calls = []

            def _post(self, url, body, scopes):
                self.calls.append(("POST", url, body))
                return {}

            def _put(self, url, body, scopes):
                self.calls.append(("PUT", url, body))
                return {}

        gc = GC()
        gc.save_draft(["a@b.c"], "S", "B")
        gc.save_draft(["a@b.c"], "S2", "B2", draft_id="d7")
        method, url, body = gc.calls[0]
        self.assertEqual((method, url), ("POST", f"{google_client.GMAIL}/users/me/drafts"))
        method, url, body = gc.calls[1]
        self.assertEqual(method, "PUT")
        self.assertTrue(url.endswith("/drafts/d7"))
        self.assertIn("message", body)  # PUT wraps the raw message the same way


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


class _PageGC(GoogleClient):
    """Scripted message-page client: ``fail`` ids raise with ``status``."""

    def __init__(self, fail=(), status=404):
        super().__init__(lambda scopes: "token")
        self._fail = set(fail)
        self._status = status

    def _get(self, url, scopes):
        if "/messages?" in url:
            return {"messages": [{"id": "m1"}, {"id": "m2"}, {"id": "m3"}],
                    "nextPageToken": "tok"}
        mid = url.split("/messages/")[1].split("?")[0]
        if mid in self._fail:
            raise GoogleError(f"Google {self._status}: nope",
                              status=self._status)
        return {"id": mid, "payload": {"headers": []}}


class TestMessagePageTolerance(unittest.TestCase):
    def test_deleted_message_404_skipped(self):
        # A message deleted between list and get must not sink the page.
        msgs, token = _PageGC(fail={"m2"}).list_messages_page("INBOX")
        self.assertEqual([m["id"] for m in msgs], ["m1", "m3"])
        self.assertEqual(token, "tok")

    def test_page_intact_without_failures(self):
        msgs, _ = _PageGC().list_messages_page("INBOX")
        self.assertEqual(len(msgs), 3)

    def test_scope_error_still_propagates(self):
        # 401/403 affects every row alike — must surface, not be swallowed.
        with self.assertRaises(GoogleError):
            _PageGC(fail={"m2"}, status=403).list_messages_page("INBOX")


class TestMessagePageOrder(unittest.TestCase):
    def test_order_preserved_under_random_latency(self):
        # The per-message GETs run in a thread pool; however they COMPLETE,
        # the page must come back in the id list's order.
        import random
        import time

        ids = [f"m{i:02d}" for i in range(12)]
        rng = random.Random(42)

        class SlowGC(GoogleClient):
            def __init__(self):
                super().__init__(lambda scopes: "token")

            def _get(self, url, scopes):
                if "/messages?" in url:
                    return {"messages": [{"id": i} for i in ids]}
                time.sleep(rng.uniform(0, 0.03))
                mid = url.split("/messages/")[1].split("?")[0]
                return {"id": mid, "payload": {"headers": []}}

        msgs, _ = SlowGC().list_messages_page("INBOX")
        self.assertEqual([m["id"] for m in msgs], ids)


class TestReplyRecipients(unittest.TestCase):
    """reply_all must carry the original To + Cc (minus self, deduped) —
    the bug where it went only to the original sender."""

    @staticmethod
    def _sent_headers(headers, me="me@x.com", **reply_kwargs):
        class GC(GoogleClient):
            def _get(self, url, scopes):
                assert "/messages/" in url, f"unexpected GET {url}"
                return {"threadId": "t1",
                        "payload": {"headers": [
                            {"name": k, "value": v} for k, v in headers.items()]}}

            def _post(self, url, body, scopes):
                self.sent = body
                return {}

        gc = GC(lambda s: "t")
        gc._email = me  # skip the profile lookup
        gc.reply_mail("m1", "hi there", **reply_kwargs)
        from email import message_from_bytes

        raw = gc.sent["raw"]
        padded = raw + "=" * (-len(raw) % 4)
        msg = message_from_bytes(base64.urlsafe_b64decode(padded))
        return msg, gc.sent

    def test_reply_all_includes_to_and_cc(self):
        msg, sent = self._sent_headers({
            "Subject": "Planning",
            "From": "Ann <ann@x.com>",
            "To": "me@x.com, Bob <bob@x.com>",
            "Cc": "Carol <carol@x.com>, ann@x.com",
        }, reply_all=True)
        to = (msg["To"] or "").lower()
        self.assertIn("ann@x.com", to)         # original sender
        self.assertIn("bob@x.com", to)         # original To survives
        self.assertNotIn("me@x.com", to)       # never To: myself
        # Cc carries the original cc only — ann is deduped against To and our
        # own address never appears.
        self.assertEqual(msg["Cc"], "Carol <carol@x.com>")
        self.assertEqual(sent["threadId"], "t1")  # stays on the thread
        self.assertEqual(msg["Subject"], "Re: Planning")

    def test_reply_all_dedupes_across_buckets(self):
        # The sender reappears in To — one copy total.
        msg, _ = self._sent_headers({
            "From": "ann@x.com",
            "To": "Ann <ann@x.com>, Bob <bob@x.com>",
        }, reply_all=True)
        to = msg["To"].lower()
        self.assertEqual(to.count("ann@x.com"), 1)
        self.assertIn("bob@x.com", to)

    def test_plain_reply_goes_to_sender_only(self):
        msg, _ = self._sent_headers({
            "From": "Ann <ann@x.com>",
            "To": "Bob <bob@x.com>",
            "Cc": "Carol <carol@x.com>",
        })
        self.assertEqual([a.strip() for a in msg["To"].split(",")],
                         ["Ann <ann@x.com>"])
        self.assertIsNone(msg["Cc"])


class TestChatSpaces(unittest.TestCase):
    def test_last_at_from_last_active_time(self):
        class GC(GoogleClient):
            def _get(self, url, scopes):
                return {"spaces": [
                    {"name": "spaces/A", "spaceType": "DIRECT_MESSAGE",
                     "lastActiveTime": "2026-08-01T10:00:00Z"},
                    {"name": "spaces/B", "displayName": "Team"},
                ]}

        chats = GC(lambda s: "t").list_chats()
        by_id = {c["id"]: c for c in chats}
        # lastActiveTime feeds the notifier's change detection (keyed on
        # last_at); absent field keeps the "" fallback.
        self.assertEqual(by_id["spaces/A"]["last_at"], "2026-08-01T10:00:00Z")
        self.assertEqual(by_id["spaces/B"]["last_at"], "")


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
