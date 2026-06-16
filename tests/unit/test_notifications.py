# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Shahab Nedaei
"""NotificationManager gating + digest batching (no GUI, no network)."""

import unittest
from unittest import mock

from fakes import FakeApp, FakeSettings

from cloudy.core.account_registry import Account
from cloudy.core.notifications import NotificationManager


def _mgr(**settings):
    base = {"notifications-enabled": True, "notify-respect-system-dnd": False,
            "quiet-hours-enabled": False, "notify-level": "all"}
    base.update(settings)
    return NotificationManager(FakeApp(settings=FakeSettings(base)))


class TestGating(unittest.TestCase):
    def test_disabled_blocks_everything(self):
        nm = _mgr(**{"notifications-enabled": False})
        self.assertFalse(nm._allowed(1))
        self.assertFalse(nm._allowed(2))

    def test_level_all_allows_both_tiers(self):
        nm = _mgr(**{"notify-level": "all"})
        self.assertTrue(nm._allowed(1))
        self.assertTrue(nm._allowed(2))
        self.assertFalse(nm._digest_active())

    def test_level_priority_blocks_tier2(self):
        nm = _mgr(**{"notify-level": "priority"})
        self.assertTrue(nm._allowed(1))
        self.assertFalse(nm._allowed(2))
        self.assertFalse(nm._digest_active())

    def test_level_digest_holds_tier2_and_is_active(self):
        nm = _mgr(**{"notify-level": "digest"})
        self.assertTrue(nm._allowed(1))
        self.assertFalse(nm._allowed(2))   # held back from immediate banner
        self.assertTrue(nm._digest_active())

    def test_focus_blocks_immediate_banners(self):
        nm = _mgr()
        nm._focus_active = lambda: True
        self.assertFalse(nm._allowed(1))


class TestQuietHours(unittest.TestCase):
    def _now(self, hhmm):
        fake = mock.MagicMock()
        fake.now.return_value.strftime.return_value = hhmm
        return mock.patch("cloudy.core.notifications.datetime", fake)

    def test_disabled(self):
        nm = _mgr(**{"quiet-hours-enabled": False,
                     "quiet-hours-start": "22:00", "quiet-hours-end": "08:00"})
        self.assertFalse(nm._quiet_hours_active())

    def test_equal_window_is_inactive(self):
        nm = _mgr(**{"quiet-hours-enabled": True,
                     "quiet-hours-start": "09:00", "quiet-hours-end": "09:00"})
        self.assertFalse(nm._quiet_hours_active())

    def test_same_day_window(self):
        nm = _mgr(**{"quiet-hours-enabled": True,
                     "quiet-hours-start": "09:00", "quiet-hours-end": "17:00"})
        with self._now("12:00"):
            self.assertTrue(nm._quiet_hours_active())
        with self._now("18:00"):
            self.assertFalse(nm._quiet_hours_active())

    def test_overnight_window_wraps_midnight(self):
        nm = _mgr(**{"quiet-hours-enabled": True,
                     "quiet-hours-start": "22:00", "quiet-hours-end": "08:00"})
        with self._now("23:30"):
            self.assertTrue(nm._quiet_hours_active())
        with self._now("02:00"):
            self.assertTrue(nm._quiet_hours_active())
        with self._now("12:00"):
            self.assertFalse(nm._quiet_hours_active())


class TestMutedIds(unittest.TestCase):
    def test_filters_by_kind(self):
        acct = Account(id="a", display_name="n", provider="microsoft",
                       module_id="microsoft365",
                       muted_sources=[{"kind": "chat", "id": "c1"},
                                      {"kind": "channel", "id": "ch9"},
                                      {"kind": "chat", "id": "c2"}])
        self.assertEqual(NotificationManager._muted_ids(acct, "chat"), {"c1", "c2"})
        self.assertEqual(NotificationManager._muted_ids(acct, "channel"), {"ch9"})

    def test_no_muted_sources(self):
        acct = Account(id="a", display_name="n", provider="google", module_id="gmail")
        self.assertEqual(NotificationManager._muted_ids(acct, "chat"), set())


class TestDigest(unittest.TestCase):
    def setUp(self):
        self.nm = _mgr(**{"notify-level": "digest"})
        self.acct = Account(id="a1", display_name="Work", provider="microsoft",
                            module_id="microsoft365")

    def test_enqueue_counts_messages_and_chats(self):
        self.nm._enqueue_chat(self.acct, {"id": "c1", "name": "Alice"})
        self.nm._enqueue_chat(self.acct, {"id": "c1", "name": "Alice"})
        self.nm._enqueue_chat(self.acct, {"id": "c2", "name": "Team"})
        self.nm._enqueue_mail(self.acct)
        bucket = self.nm._digest["a1"]
        self.assertEqual(len(bucket["chats"]), 2)
        self.assertEqual(bucket["msgs"], 3)
        self.assertEqual(bucket["mail"], 1)

    def test_build_digest_plurals(self):
        captured = []
        with mock.patch("gi.repository.Gio.Notification.set_body",
                        lambda self, t: captured.append(t)):
            self.nm._enqueue_chat(self.acct, {"id": "c1", "name": "A"})
            self.nm._enqueue_chat(self.acct, {"id": "c2", "name": "B"})
            self.nm._enqueue_chat(self.acct, {"id": "c2", "name": "B"})
            self.nm._enqueue_mail(self.acct)
            self.nm._build_digest("a1", self.nm._digest["a1"])
        self.assertEqual(captured[-1], "3 new messages in 2 chats · 1 new email")

    def test_flush_holds_during_focus_then_releases(self):
        self.nm._enqueue_mail(self.acct)
        self.nm._focus_active = lambda: True
        self.nm._flush_digest()
        self.assertIn("a1", self.nm._digest)        # held
        self.nm._focus_active = lambda: False
        self.nm._flush_digest()
        self.assertNotIn("a1", self.nm._digest)     # released + cleared
        self.assertEqual(self.nm._app.sent[0][0], "digest-a1")

    def test_flush_drops_queue_when_notifications_disabled(self):
        self.nm._enqueue_mail(self.acct)
        self.nm._app.settings.set_boolean("notifications-enabled", False)
        self.nm._flush_digest()
        self.assertEqual(self.nm._digest, {})
        self.assertEqual(self.nm._app.sent, [])


if __name__ == "__main__":
    unittest.main()
