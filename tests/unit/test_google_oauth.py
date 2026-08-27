# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Shahab Nedaei
"""Google loopback OAuth security invariants — no network involved.

Covers the two gaps from the security backlog: the ``state`` parameter
(CSRF / authorization-code injection session binding) and per-flow redirect
storage (results live on each sign-in's own server instance, never on the
handler class where concurrent flows would cross-contaminate).
"""

import unittest
from types import SimpleNamespace

from cloudy.core.auth.google_oauth import (
    _STATE_MISMATCH,
    _CodeHandler,
    _record_redirect,
    _state_matches,
)


def _fake_server(expected_state: str = "st-expected"):
    """Stand-in for one sign-in flow's HTTPServer instance (no socket)."""
    return SimpleNamespace(expected_state=expected_state, auth_code=None,
                           auth_error=None, auth_state=None)


def _handler_for(path: str, server) -> tuple:
    """A _CodeHandler wired to a fake server.

    ``_respond`` is captured instead of writing to a socket, so the
    handler's redirect decision is testable offline.
    """
    sent = []
    handler = object.__new__(_CodeHandler)
    handler.server = server
    handler.path = path
    handler._respond = lambda status, body: sent.append((status, body))
    return handler, sent


class TestStateCheck(unittest.TestCase):
    """_state_matches: matching state passes; missing/mismatched fails."""

    def test_matching_state_passes(self):
        self.assertTrue(_state_matches("st-expected", "st-expected"))

    def test_mismatched_state_fails(self):
        self.assertFalse(_state_matches("st-expected", "attacker-value"))

    def test_missing_received_state_fails(self):
        self.assertFalse(_state_matches("st-expected", None))

    def test_no_expected_state_fails_closed(self):
        # A flow without an expected state must reject everything.
        self.assertFalse(_state_matches(None, None))
        self.assertFalse(_state_matches(None, "anything"))
        self.assertFalse(_state_matches("", ""))


class TestPerFlowStorage(unittest.TestCase):
    """Redirect results are stored on the flow's own server instance."""

    def test_records_code_and_state_on_own_server(self):
        server = _fake_server()
        _record_redirect(server, {"code": ["c1"], "state": ["st-expected"]})
        self.assertEqual(server.auth_code, "c1")
        self.assertIsNone(server.auth_error)
        self.assertEqual(server.auth_state, "st-expected")

    def test_records_error_redirect(self):
        server = _fake_server()
        _record_redirect(server, {"error": ["access_denied"],
                                  "state": ["st-expected"]})
        self.assertEqual(server.auth_error, "access_denied")
        self.assertIsNone(server.auth_code)

    def test_concurrent_flows_do_not_cross_contaminate(self):
        # Two live sign-ins = two server instances; a result landing on one
        # must stay invisible to the other (the old class-attribute bug).
        a, b = _fake_server("st-a"), _fake_server("st-b")
        _record_redirect(a, {"code": ["code-a"], "state": ["st-a"]})
        _record_redirect(b, {"error": ["access_denied"], "state": ["st-b"]})
        self.assertEqual(a.auth_code, "code-a")
        self.assertIsNone(a.auth_error)
        self.assertIsNone(b.auth_code)
        self.assertEqual(b.auth_error, "access_denied")


class TestHandlerStateEnforcement(unittest.TestCase):
    """The loopback handler itself must reject a bad-state redirect."""

    def test_valid_state_recorded_and_200(self):
        server = _fake_server("st-expected")
        handler, sent = _handler_for("/?code=c9&state=st-expected", server)
        handler.do_GET()
        self.assertEqual(sent[0][0], 200)
        self.assertEqual(server.auth_code, "c9")
        self.assertIsNone(server.auth_error)
        self.assertEqual(server.auth_state, "st-expected")

    def test_mismatched_state_rejected_with_4xx(self):
        server = _fake_server("st-expected")
        handler, sent = _handler_for("/?code=evil&state=attacker", server)
        handler.do_GET()
        self.assertGreaterEqual(sent[0][0], 400)
        # The injected code is never recorded; the flow sees a clear error.
        self.assertIsNone(server.auth_code)
        self.assertEqual(server.auth_error, _STATE_MISMATCH)

    def test_missing_state_rejected_with_4xx(self):
        server = _fake_server("st-expected")
        handler, sent = _handler_for("/?code=evil", server)
        handler.do_GET()
        self.assertGreaterEqual(sent[0][0], 400)
        self.assertIsNone(server.auth_code)
        self.assertEqual(server.auth_error, _STATE_MISMATCH)

    def test_error_redirect_with_valid_state_still_recorded(self):
        # Happy-path error (user clicked "Cancel"): unchanged behavior.
        server = _fake_server("st-expected")
        handler, sent = _handler_for("/?error=access_denied&state=st-expected",
                                     server)
        handler.do_GET()
        self.assertEqual(sent[0][0], 200)
        self.assertEqual(server.auth_error, "access_denied")
        self.assertIsNone(server.auth_code)

    def test_favicon_probe_ignored(self):
        # Browsers request /favicon.ico right after the redirect — it must
        # neither wipe a recorded result nor trip the state check.
        server = _fake_server("st-expected")
        _record_redirect(server, {"code": ["c1"], "state": ["st-expected"]})
        handler, sent = _handler_for("/favicon.ico", server)
        handler.do_GET()
        self.assertEqual(sent[0][0], 200)
        self.assertEqual(server.auth_code, "c1")
        self.assertIsNone(server.auth_error)


if __name__ == "__main__":
    unittest.main()
