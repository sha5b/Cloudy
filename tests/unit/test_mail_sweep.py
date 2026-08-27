# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Shahab Nedaei
"""Headless sweep of the Mail list's live-refresh pagination merge.

Drives the real ``MailView`` against a fake mail client (injected through the
app's per-account client cache, like ``test_chat_sweep`` does for Chat):
page 1 loads, "Load older" pulls page 2, then a notifier-style
``refresh_live`` must MERGE the fresh page 1 into the loaded list — new mail
on top, older pages kept, the "Load older" cursor and the row selection
intact — instead of snapping the list back to page one. The pure merge
helper (``_merge_message_pages``) is covered separately below.
"""

import unittest

import gi_setup  # pins GI versions; exposes AVAILABLE

if gi_setup.AVAILABLE:
    from gi.repository import GLib, Gtk

_skip = unittest.skipUnless(gi_setup.AVAILABLE,
                            "GTK/Adw typelibs unavailable (headless build)")

if gi_setup.AVAILABLE:
    from cloudy.core.account_registry import Account
    from cloudy.core.cache import MemoryCache
    from cloudy.widgets.mail_view import MailView, _merge_message_pages
    from fakes import FakeRegistry, FakeSettings


def _pump(ms: int = 400) -> None:
    """Run a short main loop so queued idles (run_async callbacks) land."""
    loop = GLib.MainLoop()
    GLib.timeout_add(ms, loop.quit)
    loop.run()


class _FakeApp:
    def __init__(self):
        self.settings = FakeSettings()
        self.registry = FakeRegistry()
        self.cache = MemoryCache()
        self.notifier = None  # MailView getattr-guards this
        self._clients = {}

    def get_account_client(self, account):
        return self._clients.get(account.id)

    def set_account_client(self, account, client):
        self._clients[account.id] = client


class _FakeWindow:
    def __init__(self, app):
        self._app = app
        self.folders = {}
        self.toasts = []

    def get_application(self):
        return self._app

    def add_toast(self, message):
        self.toasts.append(str(message))

    def last_mail_folder(self, account_id):
        return self.folders.get(account_id)

    def remember_mail_folder(self, account_id, folder_id):
        self.folders[account_id] = folder_id

    def sign_in_account(self, account):
        pass


def _mail(mid, day, **extra):
    row = {"id": mid, "subject": f"Subject {mid}",
           "from": "Ann Peak <ann@corp.com>", "preview": "preview",
           "received": f"2026-08-{day:02d}T09:00:00Z", "is_read": True}
    row.update(extra)
    return row


class FakeMailClient:
    """The mail surface MailView uses, over two in-memory pages."""

    def __init__(self):
        self.page1 = []
        self.page2 = []
        self.token1 = "p2"      # cursor page 1 hands out
        self.token2 = "p3"      # cursor page 2 hands out

    def list_folders(self):
        return [{"id": "INBOX", "name": "Inbox", "well_known": "inbox",
                 "unread": 0}]

    def list_messages_page(self, folder, *, query=None, page_token=None):
        if page_token:
            return list(self.page2), self.token2
        return list(self.page1), self.token1

    def get_message(self, mid):
        known = {m["id"]: m for m in self.page1 + self.page2}
        base = known.get(mid, {"id": mid})
        return dict(base, body="", body_html=False)

    def mark_read(self, mid, read):
        pass


@_skip
class TestMergeMessagePages(unittest.TestCase):
    """The pure helper: fresh page 1 merged into the loaded list by id."""

    def test_fresh_page_replaces_its_copies_and_prepends_new_mail(self):
        existing = [_mail("a", 27), _mail("b", 26), _mail("c", 25)]
        fresh = [_mail("n", 28), _mail("a", 27, subject="updated")]
        merged = _merge_message_pages(existing, fresh)
        self.assertEqual([m["id"] for m in merged], ["n", "a", "b", "c"])
        self.assertEqual(merged[1]["subject"], "updated")  # fresh copy wins

    def test_older_loaded_pages_are_kept(self):
        existing = [_mail("a", 27), _mail("b", 20)]  # b paged in via Load older
        fresh = [_mail("a2", 26), _mail("a", 25)]
        merged = _merge_message_pages(existing, fresh)
        self.assertEqual([m["id"] for m in merged], ["a2", "a", "b"])

    def test_cached_mail_newer_than_the_page_window_is_dropped(self):
        # The fresh page is authoritative for the newest window: a cached
        # message in it that the page no longer returns moved/vanished.
        existing = [_mail("ghost", 28)]
        fresh = [_mail("a", 27), _mail("b", 26)]
        merged = _merge_message_pages(existing, fresh)
        self.assertEqual([m["id"] for m in merged], ["a", "b"])

    def test_empty_fresh_page_clears_the_list(self):
        merged = _merge_message_pages([_mail("a", 27), _mail("b", 20)], [])
        self.assertEqual(merged, [])


class _MailSweepBase(unittest.TestCase):
    def setUp(self):
        Gtk.init_check()
        self.app = _FakeApp()
        self.window = _FakeWindow(self.app)
        self.client = FakeMailClient()
        self.account = Account.from_dict(
            {"id": "g-1", "display_name": "me@gmail.com",
             "provider": "google", "signed_in": True})
        self.app.registry._accounts = [self.account]
        self.app.set_account_client(self.account, self.client)
        self.view = MailView(self.window, self.account)
        # run_async drops callbacks for unrooted widgets — parent the view in a
        # real (never-shown) window so it has a root, like the live app.
        self.host = Gtk.Window()
        self.host.set_child(self.view)
        self.addCleanup(self.host.destroy)

    def _reload(self):
        """Seed-then-load: the constructor already fetched the (empty) fake
        page, so re-trigger the load after the test seeds its data."""
        self.view._load_async()
        _pump()

    def _row_ids(self):
        return [r._mid for r in self.view._message_rows()]


@_skip
class TestLiveRefreshKeepsPagination(_MailSweepBase):
    def test_refresh_live_merges_instead_of_collapsing(self):
        self.client.page1 = [_mail(f"m{i}", 28 - i) for i in range(1, 6)]   # 27..23
        self.client.page2 = [_mail(f"m{i}", 28 - i) for i in range(6, 11)]  # 22..18
        self._reload()
        self.assertEqual(self._row_ids(), ["m1", "m2", "m3", "m4", "m5"])
        self.assertEqual(self.view._next_token, "p2")

        self.view._load_more()
        _pump()
        self.assertEqual(self._row_ids(), [f"m{i}" for i in range(1, 11)])
        self.assertEqual(self.view._next_token, "p3")

        # A poll spots new mail: it lands on top of page 1 and the server's
        # page-1 cursor has moved on. The live refresh must merge — not snap
        # the list back to page one or reset the "Load older" cursor.
        self.client.page1 = [_mail("m0", 28)] + self.client.page1
        self.client.token1 = "p2-moved"
        self.view.refresh_live()
        _pump()

        self.assertEqual(self._row_ids(), [f"m{i}" for i in range(0, 11)])
        self.assertEqual(self.view._next_token, "p3")  # cursor survived
        cached = self.app.cache.get(f"{self.account.id}:messages:INBOX")
        self.assertEqual([m["id"] for m in cached[0]],
                         [f"m{i}" for i in range(0, 11)])

    def test_refresh_live_preserves_selection(self):
        self.client.page1 = [_mail(f"m{i}", 28 - i) for i in range(1, 6)]
        self.client.page2 = [_mail(f"m{i}", 28 - i) for i in range(6, 11)]
        self._reload()
        self.view._load_more()
        _pump()

        self.view._list.select_row(self.view._rows_by_id["m7"])
        _pump()  # the reader fetch for the newly selected message
        self.assertEqual(self.view._selected_mids(), ["m7"])

        self.client.page1 = [_mail("m0", 28)] + self.client.page1
        self.view.refresh_live()
        _pump()

        self.assertEqual(self.view._selected_mids(), ["m7"])
        self.assertEqual(self.view._open_mid, "m7")  # reader untouched


if __name__ == "__main__":
    unittest.main()
