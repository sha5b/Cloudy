# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Shahab Nedaei
"""Pin / mute / scope-error helpers used by the Mail/Calendar/Chat views."""

import unittest

import gi_setup  # noqa: F401  (pins GI versions before the widget import)
from fakes import FakeApp, FakeWindow

from cloudy.core.account_registry import Account
from cloudy.widgets.source_nav import (
    find_pin,
    is_muted,
    is_pinned,
    is_scope_error,
    toggle_mute,
    toggle_pin,
)


def _acct():
    return Account(id="a1", display_name="me@contoso.com",
                   provider="microsoft", module_id="microsoft365")


class TestScopeError(unittest.TestCase):
    def test_detects_scope_phrase(self):
        self.assertTrue(is_scope_error("AADSTS: requested scopes are missing"))

    def test_plain_error_is_not_scope(self):
        self.assertFalse(is_scope_error("HTTP 500 server error"))

    def test_none_is_not_scope(self):
        self.assertFalse(is_scope_error(None))


class TestPins(unittest.TestCase):
    def setUp(self):
        self.app = FakeApp()
        self.win = FakeWindow(self.app)
        self.acct = _acct()

    def test_toggle_pin_adds_then_removes(self):
        self.assertFalse(is_pinned(self.acct, "calendar", "shared", "s@x.com"))
        added = toggle_pin(self.win, self.acct, kind="calendar", source="shared",
                           sid="s@x.com", name="Shared")
        self.assertTrue(added)
        self.assertTrue(is_pinned(self.acct, "calendar", "shared", "s@x.com"))

        removed = toggle_pin(self.win, self.acct, kind="calendar", source="shared",
                             sid="s@x.com", name="Shared")
        self.assertFalse(removed)
        self.assertFalse(is_pinned(self.acct, "calendar", "shared", "s@x.com"))
        self.assertEqual(len(self.app.registry.updated), 2)  # persisted both times

    def test_toggle_pin_stores_extra_fields(self):
        toggle_pin(self.win, self.acct, kind="channel", source="teams",
                   sid="ch1", name="General", team_id="t1", team_name="Eng")
        pin = find_pin(self.acct, "channel", "teams", "ch1")
        self.assertEqual(pin["team_id"], "t1")
        self.assertEqual(pin["team_name"], "Eng")

    def test_pins_are_independent_by_kind(self):
        toggle_pin(self.win, self.acct, kind="mail", source="shared",
                   sid="s@x.com", name="S")
        self.assertTrue(is_pinned(self.acct, "mail", "shared", "s@x.com"))
        self.assertFalse(is_pinned(self.acct, "calendar", "shared", "s@x.com"))


class TestMutes(unittest.TestCase):
    def setUp(self):
        self.app = FakeApp()
        self.win = FakeWindow(self.app)
        self.acct = _acct()

    def test_toggle_mute_adds_then_removes(self):
        self.assertFalse(is_muted(self.acct, "chat", "c1"))
        self.assertTrue(toggle_mute(self.win, self.acct, kind="chat", sid="c1"))
        self.assertTrue(is_muted(self.acct, "chat", "c1"))
        self.assertFalse(toggle_mute(self.win, self.acct, kind="chat", sid="c1"))
        self.assertFalse(is_muted(self.acct, "chat", "c1"))

    def test_mute_is_kind_specific(self):
        toggle_mute(self.win, self.acct, kind="chat", sid="x")
        self.assertTrue(is_muted(self.acct, "chat", "x"))
        self.assertFalse(is_muted(self.acct, "channel", "x"))


if __name__ == "__main__":
    unittest.main()
