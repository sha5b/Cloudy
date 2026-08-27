# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Shahab Nedaei
"""Headless Files/mount sweep against a simulated MountState snapshot.

Nothing real is mounted: the shared snapshot's ``mounted``/``healthy`` sets are
swapped for fakes (restored afterwards), rclone's VFS-status read is stubbed,
and the Files view is driven through its real row/state/poll code paths.
"""

import unittest
import unittest.mock

import gi_setup  # pins GI versions; exposes AVAILABLE

if gi_setup.AVAILABLE:
    from gi.repository import Gtk

_skip = unittest.skipUnless(gi_setup.AVAILABLE,
                            "GTK/Adw typelibs unavailable (headless build)")

if gi_setup.AVAILABLE:
    from fakes import FakeSettings, FakeRegistry

from cloudy.core.account_registry import Account


def _pump(ms: int = 400) -> None:
    from gi.repository import GLib

    loop = GLib.MainLoop()
    GLib.timeout_add(ms, loop.quit)
    loop.run()


class _FakeSecrets:
    def lookup(self, account_id, kind):
        return None


class _FakeApp:
    def __init__(self):
        self.settings = FakeSettings()
        self.registry = FakeRegistry()
        from cloudy.core.cache import MemoryCache

        self.cache = MemoryCache()
        self.secrets = _FakeSecrets()
        self.props = type("P", (), {"active_window": None})()


class _FakeWindow:
    def __init__(self, app):
        self._app = app

    def get_application(self):
        return self._app

    def add_toast(self, message):
        pass


@_skip
class TestMountSweep(unittest.TestCase):
    def test_upload_status_queries_scoped_remote_and_updates_row(self):
        Gtk.init_check()
        from cloudy.core.mount_state import MountState
        from cloudy.modules.microsoft365.mounts import MountManager
        from cloudy.widgets.files_view import FilesView

        app = _FakeApp()
        window = _FakeWindow(app)
        account = Account.from_dict(
            {"id": "g-1", "display_name": "me@gmail.com",
             "provider": "google", "signed_in": True})
        app.registry._accounts = [account]
        view = FilesView(window, account)  # google → synchronous library list
        host = Gtk.Window()
        host.set_child(view)
        self.addCleanup(view._cancel_status_poll)

        # Simulate a live, healthy "My Drive" mount in the shared snapshot.
        state = MountState.get()
        old_mounted, old_healthy = state._mounted, state._healthy
        mp = view._row_key(view._libraries[0]["drive"])
        state._mounted = frozenset([mp])
        state._healthy = frozenset([mp])
        self.addCleanup(setattr, state, "_mounted", old_mounted)
        self.addCleanup(setattr, state, "_healthy", old_healthy)

        recorded = {}

        def fake_upload_status(self, remote):
            recorded["remote"] = remote
            return {"pending": 2}

        with unittest.mock.patch.object(MountManager, "upload_status",
                                        fake_upload_status), \
             unittest.mock.patch.object(view, "get_mapped",
                                        return_value=True):
            view._apply_mount_states()
            _pump()

        # The VFS-status read must use the account-scoped remote name — the
        # bare drive name reads a (usually nonexistent) other remote's
        # metadata and the indicator stays dead.
        self.assertEqual(recorded.get("remote"),
                         MountManager.remote_name("My Drive", account.id))
        row = view._rows[mp][0]
        self.assertIn("Uploading 2", row.get_subtitle())

    def test_row_keys_are_mountpoints_not_names(self):
        Gtk.init_check()
        from cloudy.widgets.files_view import FilesView

        app = _FakeApp()
        window = _FakeWindow(app)
        account = Account.from_dict(
            {"id": "g-2", "display_name": "me@gmail.com",
             "provider": "google", "signed_in": True})
        app.registry._accounts = [account]
        view = FilesView(window, account)
        # Two libraries that sanitize apart (My Drive vs Shared with me) get
        # distinct keys; same-named libraries across accounts would too, since
        # the key is the full per-account mountpoint path.
        keys = list(view._rows.keys())
        self.assertEqual(len(keys), len(set(keys)))
        self.assertTrue(all(k.startswith("/") for k in keys))


if __name__ == "__main__":
    unittest.main()
