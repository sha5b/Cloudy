# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Shahab Nedaei
"""Starred-channel notification sweep + Dashboard channel-activity gating.

The notifier keeps a per-channel watermark (the newest post's ``sent`` the
user has seen): a newer post from someone else badges + banners unless the
channel is muted (watermark still advances). The Dashboard's Activity rows
skip system/deleted rows and only surface channels with something newer than
the watermark, degrading to show-latest when no watermark exists yet.
"""

import time
import unittest
from unittest import mock

import gi_setup  # pins GI versions; exposes AVAILABLE

from fakes import FakeApp, FakeSettings, FakeWindow

from cloudy.core.account_registry import Account
from cloudy.core.cache import MemoryCache
from cloudy.core.interfaces import ServiceModule, TeamsCapability
from cloudy.core.notifications import NotificationManager, _stamp_newer

_settings = {
    "notifications-enabled": True, "notify-respect-system-dnd": False,
    "quiet-hours-enabled": False, "notify-level": "all",
}


class SweepApp(FakeApp):
    """FakeApp plus the cache/registry/engine surface the sweep touches."""

    def __init__(self):
        super().__init__(settings=FakeSettings(dict(_settings)))
        self.cache = MemoryCache()
        self.engine = _Engine(_TeamsModule())
        self.windows: list = []

    def get_windows(self):
        return list(self.windows)


class _Engine:
    def __init__(self, module):
        self._module = module

    def get(self, module_id):
        return self._module

    def is_enabled(self, module_id):
        return True


class _TeamsModule(ServiceModule, TeamsCapability):
    id = "microsoft365"
    name = "Microsoft 365"

    def activate(self, ctx):
        pass

    def deactivate(self):
        pass

    def list_teams(self):
        return []

    def list_team_channels(self, team_id):
        return []

    def list_channel_messages(self, team_id, channel_id, *, limit=20):
        return []


def _mgr(app=None):
    return NotificationManager(app or SweepApp())


def _pin(cid="ch1", name="general", team="Design"):
    return {"kind": "channel", "source": "teams", "id": cid, "name": name,
            "team_id": "t1", "team_name": team}


def _post(pid, sent, sender="Alice", text="hello", mine=False, replies=()):
    return {"id": pid, "subject": "", "text": text, "from": sender,
            "sent": sent, "is_mine": mine, "replies": list(replies)}


def _acct(**extra):
    args = dict(id="a1", display_name="me@contoso.com", provider="microsoft",
                module_id="microsoft365", signed_in=True)
    args.update(extra)
    return Account(**args)


class TestStampNewer(unittest.TestCase):
    def test_fractional_seconds_beat_lexical_order(self):
        # Lexically "…12.345Z" < "…12Z" ('.' sorts before 'Z') — the parsed
        # compare must call the fractional stamp the newer one.
        self.assertTrue(_stamp_newer("2026-08-27T09:31:12.345Z",
                                     "2026-08-27T09:31:12Z"))
        self.assertFalse(_stamp_newer("2026-08-27T09:31:12Z",
                                      "2026-08-27T09:31:12.345Z"))

    def test_graph_seven_digit_fractions_parse(self):
        self.assertTrue(_stamp_newer("2026-08-27T09:31:12.1234567Z",
                                     "2026-08-27T09:31:12Z"))

    def test_empty_is_never_newer(self):
        self.assertFalse(_stamp_newer("", "2026-08-27T09:31:12Z"))
        self.assertFalse(_stamp_newer("", ""))


class TestChannelSweep(unittest.TestCase):
    def setUp(self):
        self.app = SweepApp()
        self.nm = NotificationManager(self.app)
        self.acct = _acct(pinned_sources=[_pin()])

    def _sweep(self, posts):
        self.nm._on_channels(self.acct, [(_pin(), posts)], None)

    def test_first_run_baselines_without_notifying(self):
        self._sweep([_post("p1", "2026-08-27T09:00:00Z")])
        self.assertEqual(self.nm.channel_last_seen("a1", "ch1"),
                         "2026-08-27T09:00:00Z")
        self.assertEqual(self.nm.channel_unread_count("a1"), 0)
        self.assertEqual(self.app.sent, [])
        # Baselines persist, so a restart doesn't re-announce history.
        stored = self.app.cache.get("channel-seen:a1")
        self.assertIsNotNone(stored)
        self.assertEqual(stored[0], {"ch1": "2026-08-27T09:00:00Z"})

    def test_new_post_advances_watermark_badges_and_banners(self):
        self._sweep([_post("p1", "2026-08-27T09:00:00Z")])  # baseline
        with mock.patch("gi.repository.Gio.Notification.new") as m_new:
            m_new.return_value = mock.MagicMock()
            self._sweep([_post("p1", "2026-08-27T09:05:00Z",
                               sender="Bob", text="Standup notes")])
        self.assertEqual(self.nm.channel_last_seen("a1", "ch1"),
                         "2026-08-27T09:05:00Z")
        self.assertEqual(self.nm.channel_unread_count("a1"), 1)
        # Banner shape: title names the channel and team, body the post.
        self.assertEqual(m_new.call_args[0][0], "New post in #general · Design")
        note = m_new.return_value
        self.assertEqual(note.set_body.call_args[0][0], "Bob: Standup notes")
        # Deep-link: the Teams tab action with account + channel payload
        # (GLib prints the \x1f separator escaped inside the detailed name).
        action = note.set_default_action.call_args[0][0]
        self.assertTrue(action.startswith("app.notify-open-teams"))
        self.assertIn("a1", action)
        self.assertEqual(self.app.sent[0][0], "channel-a1-ch1")

    def test_new_reply_on_older_thread_counts_as_new(self):
        self._sweep([_post("p1", "2026-08-27T09:00:00Z")])  # baseline
        reply = _post("r1", "2026-08-27T09:10:00Z", sender="Bob", text="fyi")
        with mock.patch("gi.repository.Gio.Notification.new") as m_new:
            m_new.return_value = mock.MagicMock()
            self._sweep([_post("p1", "2026-08-27T09:00:00Z", replies=[reply])])
        self.assertEqual(self.nm.channel_last_seen("a1", "ch1"),
                         "2026-08-27T09:10:00Z")
        self.assertEqual(self.nm.channel_unread_count("a1"), 1)
        self.assertEqual(m_new.return_value.set_body.call_args[0][0],
                         "Bob: fyi")
        self.assertEqual(self.app.sent[0][0], "channel-a1-ch1")

    def test_muted_channel_advances_watermark_only(self):
        self.acct = _acct(pinned_sources=[_pin()],
                          muted_sources=[{"kind": "channel", "id": "ch1"}])
        self._sweep([_post("p1", "2026-08-27T09:00:00Z")])  # baseline
        self._sweep([_post("p1", "2026-08-27T09:05:00Z",
                           sender="Bob", text="Standup notes")])
        self.assertEqual(self.nm.channel_last_seen("a1", "ch1"),
                         "2026-08-27T09:05:00Z")  # watermark still advances
        self.assertEqual(self.nm.channel_unread_count("a1"), 0)  # no badge
        self.assertEqual(self.app.sent, [])  # no banner

    def test_own_post_advances_watermark_without_pinging(self):
        self._sweep([_post("p1", "2026-08-27T09:00:00Z")])  # baseline
        self._sweep([_post("p2", "2026-08-27T09:05:00Z",
                           sender="Alice", text="mine", mine=True)])
        self.assertEqual(self.nm.channel_last_seen("a1", "ch1"),
                         "2026-08-27T09:05:00Z")
        self.assertEqual(self.nm.channel_unread_count("a1"), 0)
        self.assertEqual(self.app.sent, [])

    def test_system_and_deleted_rows_neither_watermark_nor_announce(self):
        self._sweep([_post("p1", "2026-08-27T09:00:00Z")])  # baseline
        self._sweep([
            _post("p1", "2026-08-27T09:00:00Z"),
            {"id": "s1", "text": "Call started", "from": "",
             "sent": "2026-08-27T09:30:00Z", "system": True, "replies": []},
            {"id": "d1", "text": "", "from": "Bob",
             "sent": "2026-08-27T09:40:00Z", "deleted": True, "replies": []},
        ])
        self.assertEqual(self.nm.channel_last_seen("a1", "ch1"),
                         "2026-08-27T09:00:00Z")
        self.assertEqual(self.nm.channel_unread_count("a1"), 0)
        self.assertEqual(self.app.sent, [])

    def test_persisted_watermark_seeds_a_new_manager(self):
        self._sweep([_post("p1", "2026-08-27T09:00:00Z")])
        restarted = NotificationManager(self.app)  # same app = same cache
        self.assertEqual(restarted.channel_last_seen("a1", "ch1"),
                         "2026-08-27T09:00:00Z")
        # And the restart treats a newer post as new, not as a baseline.
        restarted._on_channels(
            self.acct, [(_pin(), [_post("p2", "2026-08-27T09:05:00Z",
                                        sender="Bob")])], None)
        self.assertEqual(restarted.channel_unread_count("a1"), 1)

    def test_mark_channel_read_clears_flag_and_pushes_badge(self):
        # _main_window() probes for set_account_unread to find the shell.
        win = FakeWindow(self.app)
        win.set_account_unread = None
        win.set_account_channel_unread = mock.MagicMock()
        self.app.windows.append(win)
        self._sweep([_post("p1", "2026-08-27T09:00:00Z")])
        self._sweep([_post("p2", "2026-08-27T09:05:00Z", sender="Bob")])
        win.set_account_channel_unread.assert_called_with("a1", 1)
        self.nm.mark_channel_read("a1", "ch1")
        self.assertEqual(self.nm.channel_unread_count("a1"), 0)
        win.set_account_channel_unread.assert_called_with("a1", 0)

    def test_note_channel_seen_prevents_rebadging_read_posts(self):
        # The user reads the channel in the Teams view before the sweep lands;
        # advancing the watermark means the sweep must not badge it afterwards.
        self._sweep([_post("p1", "2026-08-27T09:00:00Z")])
        self.nm.note_channel_seen("a1", "ch1", "2026-08-27T09:05:00Z")
        self._sweep([_post("p2", "2026-08-27T09:05:00Z", sender="Bob")])
        self.assertEqual(self.nm.channel_unread_count("a1"), 0)
        self.assertEqual(self.app.sent, [])

    def test_unstarred_channel_drops_out_of_the_count(self):
        self._sweep([_post("p1", "2026-08-27T09:00:00Z")])
        self._sweep([_post("p2", "2026-08-27T09:05:00Z", sender="Bob")])
        self.assertEqual(self.nm.channel_unread_count("a1"), 1)
        # Next cycle the pin is gone: the flag must not linger forever.
        self.acct = _acct(pinned_sources=[])
        self.nm._on_channels(self.acct, [], None)
        self.assertEqual(self.nm.channel_unread_count("a1"), 0)


class TestSweepGating(unittest.TestCase):
    def _tick(self, account, app=None):
        app = app or SweepApp()
        app.registry._accounts = [account] if account else []
        nm = NotificationManager(app)
        with mock.patch.object(nm, "_spawn"):  # hermetic: no worker threads
            nm._tick()
        return app

    def test_runs_for_microsoft_work_accounts(self):
        with mock.patch.object(NotificationManager, "_poll_channels") as m:
            self._tick(_acct())
        m.assert_called_once()

    def test_never_runs_for_personal_accounts(self):
        with mock.patch.object(NotificationManager, "_poll_channels") as m:
            self._tick(_acct(display_name="me@outlook.com"))
        m.assert_not_called()

    def test_never_runs_for_google_accounts(self):
        # Google's real module carries no "teams" capability, so the caps
        # check excludes it before the provider guard even matters.
        from cloudy.modules.gmail import MODULE as GmailModule

        app = SweepApp()
        app.engine = _Engine(GmailModule())
        with mock.patch.object(NotificationManager, "_poll_channels") as m:
            self._tick(Account(id="g1", display_name="me@somewhere.com",
                               provider="google", module_id="gmail",
                               signed_in=True), app=app)
        m.assert_not_called()

    def test_sweep_throttles_to_its_own_cadence(self):
        app = SweepApp()
        nm = NotificationManager(app)
        nm._channel_polled["a1"] = time.monotonic()  # just swept
        with mock.patch.object(nm, "_spawn") as m:
            nm._poll_channels(_acct())
        m.assert_not_called()


if gi_setup.AVAILABLE:
    from cloudy.widgets.dashboard_view import DashboardView

_skip = unittest.skipUnless(gi_setup.AVAILABLE,
                            "GTK/Adw typelibs unavailable (headless build)")


class _ChannelClient:
    def __init__(self, posts):
        self._posts = posts
        self.calls = []

    def list_channel_messages_page(self, team_id, channel_id, *, limit=20,
                                   page_token=None):
        self.calls.append((team_id, channel_id, limit))
        return self._posts, None


@_skip
class TestChannelActivity(unittest.TestCase):
    """The Dashboard's channel snippet/gating (pure static logic)."""

    def test_snippet_skips_system_and_deleted_rows(self):
        # A "Call started" system row and a deleted tombstone are both newer
        # than the real post — neither may become the snippet.
        client = _ChannelClient([
            _post("p1", "2026-08-27T09:00:00Z", sender="Bob", text="Notes"),
            {"id": "s1", "text": "Call started", "from": "",
             "sent": "2026-08-27T09:30:00Z", "system": True, "replies": []},
            {"id": "d1", "text": "", "from": "Bob",
             "sent": "2026-08-27T09:40:00Z", "deleted": True, "replies": []},
        ])
        item = DashboardView._channel_activity(client, _pin())
        self.assertIsNotNone(item)
        self.assertEqual(item["snippet"], "Notes")
        self.assertEqual(item["from"], "Bob")
        self.assertEqual(item["when"], "2026-08-27T09:00:00Z")
        self.assertEqual(item["replies"], 0)
        # Bounded: exactly one newest-page call.
        self.assertEqual(client.calls, [("t1", "ch1", 5)])

    def test_only_system_rows_yields_no_item(self):
        client = _ChannelClient([
            {"id": "s1", "text": "Call started", "from": "",
             "sent": "2026-08-27T09:30:00Z", "system": True, "replies": []},
        ])
        self.assertIsNone(DashboardView._channel_activity(client, _pin()))

    def test_no_watermark_degrades_to_show_latest(self):
        client = _ChannelClient(
            [_post("p1", "2026-08-20T09:00:00Z", sender="Bob", text="old")])
        item = DashboardView._channel_activity(client, _pin(), last_seen="")
        self.assertIsNotNone(item)
        self.assertEqual(item["snippet"], "old")

    def test_watermark_gates_stale_channels_out(self):
        posts = [_post("p1", "2026-08-20T09:00:00Z", sender="Bob", text="old")]
        client = _ChannelClient(posts)
        # The watermark equals (or exceeds) the newest post: nothing new.
        self.assertIsNone(DashboardView._channel_activity(
            client, _pin(), last_seen="2026-08-20T09:00:00Z"))
        self.assertIsNone(DashboardView._channel_activity(
            client, _pin(), last_seen="2026-08-27T00:00:00Z"))

    def test_newer_than_watermark_still_shows(self):
        client = _ChannelClient(
            [_post("p1", "2026-08-27T09:00:00Z", sender="Bob", text="fresh")])
        item = DashboardView._channel_activity(
            client, _pin(), last_seen="2026-08-20T09:00:00Z")
        self.assertIsNotNone(item)
        self.assertEqual(item["snippet"], "fresh")

    def test_new_reply_on_older_thread_counts_as_new(self):
        reply = _post("r1", "2026-08-27T09:10:00Z", sender="Bob", text="fyi")
        client = _ChannelClient(
            [_post("p1", "2026-08-20T09:00:00Z", replies=[reply])])
        item = DashboardView._channel_activity(
            client, _pin(), last_seen="2026-08-26T00:00:00Z")
        self.assertIsNotNone(item)  # the reply is newer than the watermark
        self.assertEqual(item["from"], "Bob")

    def test_system_reply_is_not_the_tip(self):
        sysreply = {"id": "s2", "text": "Call ended", "from": "",
                    "sent": "2026-08-27T10:00:00Z", "system": True}
        client = _ChannelClient([
            _post("p1", "2026-08-27T09:00:00Z", sender="Bob", text="Notes",
                  replies=[sysreply]),
        ])
        item = DashboardView._channel_activity(client, _pin())
        self.assertEqual(item["from"], "Bob")
        self.assertEqual(item["replies"], 0)


if __name__ == "__main__":
    unittest.main()
