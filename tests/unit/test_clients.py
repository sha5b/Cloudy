# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Shahab Nedaei

import unittest
import unittest.mock

from cloudy.widgets.clients import build_account_client


class _FakeApp:
    def __init__(self):
        self._clients = {}

    def get_account_client(self, account):
        return self._clients.get(account.id)

    def set_account_client(self, account, client):
        self._clients[account.id] = client

    def evict_account_client(self, account_id):
        self._clients.pop(account_id, None)


class _FakeAccount:
    def __init__(self, provider, account_id):
        self.provider = provider
        self.id = account_id


class TestClientCache(unittest.TestCase):
    def test_reuses_cached_client(self):
        app = _FakeApp()
        account = _FakeAccount("microsoft", "ms-1")
        sentinel = object()

        with unittest.mock.patch("cloudy.widgets.clients._build_client", return_value=sentinel) as builder:
            first = build_account_client(app, account)
            second = build_account_client(app, account)
            builder.assert_called_once()
        self.assertIs(first, sentinel)
        self.assertIs(second, sentinel)

    def test_evict_rebuilds_next_call(self):
        app = _FakeApp()
        account = _FakeAccount("google", "g-1")

        with unittest.mock.patch("cloudy.widgets.clients._build_client", side_effect=[object(), object()]) as builder:
            build_account_client(app, account)
            app.evict_account_client(account.id)
            build_account_client(app, account)
            self.assertEqual(builder.call_count, 2)


if __name__ == "__main__":
    unittest.main()
