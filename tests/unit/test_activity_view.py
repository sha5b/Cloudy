# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Shahab Nedaei
"""Logic tests for the Activity feed's load guard and SWR cache.

A transient error on a notifier-driven refresh must not wipe a feed that's
already on screen — only the first load swaps to a full-page status — and a
successful load is cached under ``<account>:activity`` so the next mount
renders instantly instead of flashing the loading page."""

import unittest

import gi_setup  # pins GI versions; exposes AVAILABLE

if gi_setup.AVAILABLE:
    from gi.repository import Adw, Gtk

    from cloudy.core.account_registry import Account  # pure — always importable
    from cloudy.core.cache import MemoryCache
    from cloudy.widgets import activity_view
    from cloudy.widgets.activity_view import ActivityView

    from fakes import FakeApp, FakeWindow

_skip = unittest.skipUnless(gi_setup.AVAILABLE,
                            "GTK/Adw typelibs unavailable (headless build)")


def _feed_items():
    return [{"kind": "mail", "id": "m1", "title": "Sender",
             "subtitle": "Subject", "when": "2026-08-27T10:00:00Z",
             "attention": True}]


@_skip
class TestActivityLoadGuard(unittest.TestCase):
    def setUp(self):
        Gtk.init_check()
        # No background fetch from the constructor — these tests drive
        # _on_loaded directly.
        self._real_run_async = activity_view.run_async
        activity_view.run_async = lambda work, on_done: None
        self.toasts: list[str] = []
        self.app = FakeApp()
        self.app.cache = MemoryCache()
        self.win = FakeWindow(self.app)
        self.win.add_toast = self.toasts.append
        self.acct = Account(id="a1", display_name="me", provider="microsoft",
                            module_id="microsoft365")
        self.view = ActivityView(self.win, self.acct)

    def tearDown(self):
        activity_view.run_async = self._real_run_async

    def test_first_load_error_shows_the_error_page(self):
        self.view._on_loaded(None, "Graph 500: boom & <err>")
        self.assertIsInstance(self.view.get_child(), Adw.StatusPage)
        self.assertFalse(self.view._has_data)

    def test_transient_error_keeps_the_rendered_feed_and_toasts(self):
        self.view._on_loaded(_feed_items(), None)
        feed = self.view.get_child()
        self.assertTrue(self.view._has_data)
        self.view._on_loaded(None, "Graph 503: flaky")
        self.assertIs(self.view.get_child(), feed)  # not swapped for an error page
        self.assertTrue(self.toasts)

    def test_transient_empty_result_keeps_the_rendered_feed(self):
        # Every stream in _collect_feed is individually guarded — a network
        # blip surfaces as an empty list, not an error string.
        self.view._on_loaded(_feed_items(), None)
        feed = self.view.get_child()
        self.view._on_loaded([], None)
        self.assertIs(self.view.get_child(), feed)

    def test_successful_load_is_cached_under_account_activity(self):
        items = _feed_items()
        self.view._on_loaded(items, None)
        cached = self.app.cache.get("a1:activity")
        self.assertIsNotNone(cached)
        self.assertEqual(cached[0], items)

    def test_mount_with_warm_cache_renders_without_refetching(self):
        self.app.cache.set("a1:activity", _feed_items())  # fresh entry
        view = ActivityView(self.win, self.acct)
        self.assertTrue(view._has_data)
        self.assertIsNotNone(view.get_child())
        self.assertNotIsInstance(view.get_child(), Adw.StatusPage)


if __name__ == "__main__":
    unittest.main()
