# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Shahab Nedaei

import unittest
import unittest.mock

# graph.py pulls in core.auth.msal_graph -> `import msal`, an app runtime dep
# that isn't present in a minimal RPM build chroot. Skip rather than error there
# (the helpers under test are pure; they just live behind that import).
try:
    from cloudy.modules.microsoft365.graph import GraphClient, GraphError, _split_id
    from cloudy.modules.microsoft365.graph_mail import _ical_dt
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
class TestIcalDt(unittest.TestCase):
    """The eventMessage invite-card builder feeds _ical_dt BOTH shapes Graph
    exposes: the expanded event's dateTimeTimeZone dict and the bare
    Edm.DateTimeOffset string of startDateTime/endDateTime (a str, which used
    to raise AttributeError in the old nested dict-only helper)."""

    def test_dict_utc_gets_z(self):
        self.assertEqual(
            _ical_dt({"dateTime": "2026-06-14T09:30:00.0000000",
                      "timeZone": "UTC"}),
            "20260614T093000Z")

    def test_dict_local_zone_no_z(self):
        self.assertEqual(
            _ical_dt({"dateTime": "2026-06-14T09:30:00",
                      "timeZone": "W. Europe Standard Time"}),
            "20260614T093000")

    def test_bare_utc_string_gets_z(self):
        # eventMessage startDateTime is a plain Edm.DateTimeOffset string.
        self.assertEqual(_ical_dt("2026-06-14T09:30:00Z"), "20260614T093000Z")

    def test_bare_offset_string_keeps_wall_clock(self):
        self.assertEqual(_ical_dt("2026-06-14T11:30:00+02:00"),
                         "20260614T113000")

    def test_none_and_empty_shapes(self):
        self.assertEqual(_ical_dt(None), "")
        self.assertEqual(_ical_dt({}), "")
        self.assertEqual(_ical_dt(""), "")


@_skip
class TestMeetingInvite(unittest.TestCase):
    def _invite(self, payload):
        client = GraphClient.__new__(GraphClient)
        with unittest.mock.patch.object(client, "_get", return_value=payload):
            return client._meeting_invite("MID1")

    def test_string_datetimes_do_not_crash(self):
        # The eventMessage's startDateTime/endDateTime are plain strings —
        # the old dict-only helper raised AttributeError on them.
        invite = self._invite({
            "meetingMessageType": "meetingRequest", "subject": "Sync",
            "startDateTime": "2026-06-14T09:30:00Z",
            "endDateTime": "2026-06-14T10:30:00Z",
        })
        self.assertEqual(invite["dtstart"], "20260614T093000Z")
        self.assertEqual(invite["dtend"], "20260614T103000Z")

    def test_event_dicts_preferred_over_strings(self):
        invite = self._invite({
            "meetingMessageType": "meetingRequest", "subject": "Sync",
            "startDateTime": "2026-06-14T23:30:00Z",
            "endDateTime": "2026-06-15T00:30:00Z",
            "event": {
                "subject": "Sync",
                "start": {"dateTime": "2026-06-14T09:30:00", "timeZone": "UTC"},
                "end": {"dateTime": "2026-06-14T10:30:00", "timeZone": "UTC"},
            },
        })
        self.assertEqual(invite["dtstart"], "20260614T093000Z")
        self.assertEqual(invite["dtend"], "20260614T103000Z")


@_skip
class TestSaveDraft(unittest.TestCase):
    def test_new_draft_uses_create_path(self):
        client = GraphClient.__new__(GraphClient)
        with unittest.mock.patch.object(
                client, "_draft_with_attachments",
                return_value={"id": "D1"}) as create:
            out = client.save_draft(to=["a@b"], subject="S", body="B")
        self.assertEqual(out, {"id": "D1"})
        create.assert_called_once()
        self.assertEqual(create.call_args.args[0], "/me")  # scope base

    def test_draft_id_patches_in_place(self):
        client = GraphClient.__new__(GraphClient)
        with unittest.mock.patch.object(client, "_patch") as patch, \
                unittest.mock.patch.object(client, "_post") as post:
            out = client.save_draft(to=["a@b"], subject="S", body="B",
                                    draft_id="DRAFT1")
        patch.assert_called_once()
        self.assertEqual(patch.call_args.args[0], "/me/messages/DRAFT1")
        payload = patch.call_args.args[1]
        self.assertEqual(payload["subject"], "S")
        self.assertEqual(payload["toRecipients"],
                         [{"emailAddress": {"address": "a@b"}}])
        post.assert_not_called()  # nothing new attached → no extra POSTs
        self.assertEqual(out, {"id": "DRAFT1"})

    def test_draft_id_attachments_added_to_same_draft(self):
        client = GraphClient.__new__(GraphClient)
        att = {"name": "f.txt", "content_type": "text/plain", "data": b"x"}
        with unittest.mock.patch.object(client, "_patch"), \
                unittest.mock.patch.object(client, "_post") as post:
            client.save_draft(to=["a@b"], subject="S", body="B",
                              attachments=[att], draft_id="DRAFT1")
        self.assertEqual(post.call_args.args[0],
                         "/me/messages/DRAFT1/attachments")
        self.assertEqual(post.call_args.args[1]["name"], "f.txt")


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
            # The "Me" source now aggregates: first call is the default
            # calendarView (with the timezone header), later calls enumerate
            # the other calendars.
            args = get_all.call_args_list[0].args
            self.assertEqual(len(args), 3)
            self.assertIn("/me/calendarView", args[0])
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


@_skip
class TestMessageRowMeetingFlag(unittest.TestCase):
    def test_event_message_is_flagged(self):
        m = {"id": "A", "subject": "S",
             "@odata.type": "#microsoft.graph.eventMessageRequest"}
        self.assertTrue(GraphClient._message_row(m)["meeting"])

    def test_plain_message_is_not(self):
        self.assertFalse(GraphClient._message_row({"id": "A"})["meeting"])


@_skip
class TestSplitAttachments(unittest.TestCase):
    def test_small_stay_inline(self):
        atts = [{"name": "a", "data": b"x" * 1000}]
        small, big = GraphClient._split_attachments(atts)
        self.assertEqual(len(small), 1)
        self.assertEqual(big, [])

    def test_large_goes_to_upload_session(self):
        atts = [{"name": "big", "data": b"x" * 3_000_000}]
        small, big = GraphClient._split_attachments(atts)
        self.assertEqual(small, [])
        self.assertEqual(len(big), 1)

    def test_budget_is_cumulative(self):
        # Two 1.5 MB files fit the ~4 MB request cap individually but not
        # together — the second must spill to an upload session.
        atts = [{"name": "a", "data": b"x" * 1_500_000},
                {"name": "b", "data": b"y" * 1_500_000}]
        small, big = GraphClient._split_attachments(atts)
        self.assertEqual([a["name"] for a in small], ["a"])
        self.assertEqual([a["name"] for a in big], ["b"])

    def test_none_is_empty(self):
        self.assertEqual(GraphClient._split_attachments(None), ([], []))


@_skip
class TestChatSystemEvents(unittest.TestCase):
    @staticmethod
    def _event(dtype, detail=None, **msg):
        d = {"@odata.type": f"#microsoft.graph.{dtype}"}
        d.update(detail or {})
        return {"id": "m1", "createdDateTime": "2026-07-14T08:00:00Z",
                "eventDetail": d, **msg}

    def test_members_added(self):
        row = GraphClient._system_event_row(self._event(
            "membersAddedEventMessageDetail",
            {"initiator": {"user": {"id": "u1", "displayName": "Philip"}},
             "members": [{"id": "u2", "displayName": "Jacob"}],
             "visibleHistoryStartDateTime": "2026-07-04T08:00:00Z"}))
        self.assertTrue(row["system"])
        self.assertEqual(
            row["text"],
            "Philip added Jacob and shared chat history from the past 10 days")

    def test_member_left_when_initiator_removed_self(self):
        row = GraphClient._system_event_row(self._event(
            "membersDeletedEventMessageDetail",
            {"initiator": {"user": {"id": "u2", "displayName": "Jacob"}},
             "members": [{"id": "u2", "displayName": "Jacob"}]}))
        self.assertEqual(row["text"], "Jacob left the chat")

    def test_member_removed_by_other(self):
        row = GraphClient._system_event_row(self._event(
            "membersDeletedEventMessageDetail",
            {"initiator": {"user": {"id": "u1", "displayName": "Philip"}},
             "members": [{"id": "u2", "displayName": "Jacob"}]}))
        self.assertEqual(row["text"], "Philip removed Jacob")

    def test_missing_names_fall_back(self):
        row = GraphClient._system_event_row(self._event(
            "membersAddedEventMessageDetail",
            {"members": [{"id": "u2"}]}))
        self.assertEqual(row["text"], "Someone added Someone")

    def test_rename_and_calls(self):
        row = GraphClient._system_event_row(self._event(
            "chatRenamedEventMessageDetail",
            {"initiator": {"user": {"id": "u1", "displayName": "P"}},
             "chatDisplayName": "New name"}))
        self.assertEqual(row["text"], "P renamed the chat to “New name”")
        self.assertEqual(GraphClient._system_event_row(self._event(
            "callEndedEventMessageDetail"))["text"], "Call ended")

    def test_unknown_event_hidden(self):
        self.assertIsNone(GraphClient._system_event_row(self._event(
            "teamsAppInstalledEventMessageDetail")))

    def test_all_history_suffix(self):
        row = GraphClient._system_event_row(self._event(
            "membersAddedEventMessageDetail",
            {"members": [{"id": "u2", "displayName": "J"}],
             "visibleHistoryStartDateTime": "0001-01-01T00:00:00Z"}))
        self.assertIn("shared all chat history", row["text"])


@_skip
class TestUserBind(unittest.TestCase):
    def test_plain_upn(self):
        from cloudy.modules.microsoft365.graph_chat import _user_bind
        self.assertTrue(_user_bind("a@x.com").endswith("/users('a@x.com')"))

    def test_apostrophe_doubled(self):
        # OData quoting: an embedded ' in a UPN (o'brien@…) must be doubled
        # or the user@odata.bind URL breaks the whole request.
        from cloudy.modules.microsoft365.graph_chat import _user_bind
        self.assertTrue(_user_bind("o'brien@x.com")
                        .endswith("/users('o''brien@x.com')"))


if __name__ == "__main__":
    unittest.main()
