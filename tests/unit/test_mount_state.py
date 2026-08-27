# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Shahab Nedaei

import unittest
import unittest.mock

from cloudy.core.mount_state import MountState
from cloudy.modules.microsoft365 import mounts as mounts_mod


class _State(unittest.TestCase):
    """Base: a MountState fed from stubbed system probes, never the real ones."""

    def setUp(self):
        self.state = MountState()
        self.cleared = []

    def refresh(self, mounted, cmdlines, records=()):
        with unittest.mock.patch.object(
                mounts_mod, "read_mount_table", return_value=set(mounted)), \
             unittest.mock.patch.object(
                mounts_mod, "read_process_cmdlines", return_value=cmdlines), \
             unittest.mock.patch.object(
                mounts_mod, "load_mount_records", return_value=list(records)), \
             unittest.mock.patch.object(
                mounts_mod.MountManager, "lazy_unmount",
                side_effect=lambda mp: self.cleared.append(str(mp))):
            return self.state.refresh_blocking()


class TestSnapshot(_State):
    def test_health_classifies_active_stale_and_absent(self):
        # /m/a has a live rclone daemon; /m/b is in the mount table with none.
        self.refresh({"/m/a", "/m/b"}, "rclone mount remote: /m/a --daemon\n")
        self.assertEqual(self.state.health("/m/a"), "active")
        self.assertEqual(self.state.health("/m/b"), "stale")
        self.assertEqual(self.state.health("/m/c"), "absent")

    def test_only_rclone_and_onedriver_mounts_count_as_healthy(self):
        # A gvfs/portal FUSE mount is somebody else's; it must never be reported
        # as a healthy Cloudy drive (or scanned by the Dashboard).
        self.refresh({"/run/user/1000/doc", "/m/a"},
                     "rclone mount remote: /m/a --daemon\n/usr/libexec/xdg-document-portal\n")
        self.assertEqual(self.state.healthy, frozenset({"/m/a"}))

    def test_onedriver_backed_mount_is_healthy(self):
        self.refresh({"/m/od"}, "onedriver mount /m/od\n")
        self.assertEqual(self.state.healthy, frozenset({"/m/od"}))

    def test_reports_change_only_when_the_snapshot_moves(self):
        cmd = "rclone mount remote: /m/a --daemon\n"
        self.assertTrue(self.refresh({"/m/a"}, cmd))
        self.assertFalse(self.refresh({"/m/a"}, cmd))
        self.assertTrue(self.refresh({"/m/a", "/m/b"}, cmd))

    def test_table_only_refresh_skips_the_process_read(self):
        self.refresh({"/m/a"}, "rclone mount remote: /m/a --daemon\n")
        with unittest.mock.patch.object(
                mounts_mod, "read_mount_table", return_value={"/m/a", "/m/b"}), \
             unittest.mock.patch.object(
                mounts_mod, "read_process_cmdlines") as ps:
            self.state.refresh_table_blocking()
        ps.assert_not_called()
        self.assertTrue(self.state.is_mounted("/m/b"))
        # Health is only known for mounts the last full refresh saw.
        self.assertEqual(self.state.health("/m/a"), "active")


class TestStaleClearing(_State):
    """A stale FUSE endpoint hangs every stat() on it — including the ones
    Nautilus makes for its sidebar bookmark — so it must be detached at once."""

    def test_clears_a_remembered_mount_whose_daemon_died(self):
        self.refresh({"/m/a", "/m/dead"}, "rclone mount remote: /m/a --daemon\n",
                     records=[{"mountpoint": "/m/dead"}])
        self.assertEqual(self.cleared, ["/m/dead"])

    def test_leaves_mounts_cloudy_does_not_own_alone(self):
        self.refresh({"/run/user/1000/doc"}, "", records=[{"mountpoint": "/m/dead"}])
        self.assertEqual(self.cleared, [])

    def test_leaves_healthy_mounts_alone(self):
        self.refresh({"/m/a"}, "rclone mount remote: /m/a --daemon\n",
                     records=[{"mountpoint": "/m/a"}])
        self.assertEqual(self.cleared, [])


class TestReadMountTable(unittest.TestCase):
    def test_unescapes_mountinfo_paths(self):
        line = ("36 25 0:32 / /home/u/My\\040Drive rw,relatime shared:1 "
                "- fuse.rclone remote: rw\n")
        with unittest.mock.patch.object(mounts_mod, "_in_flatpak", return_value=False), \
             unittest.mock.patch("builtins.open",
                                 unittest.mock.mock_open(read_data=line)):
            paths = mounts_mod.read_mount_table()
        self.assertIn("/home/u/My Drive", paths)


if __name__ == "__main__":
    unittest.main()
