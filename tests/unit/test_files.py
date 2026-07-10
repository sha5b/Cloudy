# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Shahab Nedaei

import tempfile
import unittest
import unittest.mock
from pathlib import Path

from cloudy.core.account_registry import Account
from cloudy.modules.microsoft365.files import OneDriveFiles
from cloudy.modules.microsoft365 import mounts as mounts_mod
from cloudy.modules.microsoft365.mounts import (
    MountManager,
    _save_mount_records,
    load_mount_records,
    reconcile_mounts,
)
from fakes import FakeRegistry


class _StubGraph:
    def __init__(self):
        self.calls = []

    def item_by_path(self, drive_id, rel_path):
        self.calls.append(("item_by_path", drive_id, rel_path))
        return {"id": "ITEM1"}

    def create_share_link(self, drive_id, item_id, *, editable=False):
        self.calls.append(("create_share_link", drive_id, item_id, editable))
        return "https://share.example/ITEM1"


def _isolate_state_file(test):
    """Point mount records at a throwaway file so tests never touch the user's
    real ``mounts.json`` (writing ``[]`` to the real one would wipe live mounts)."""
    state = Path(test.tempdir.name) / "mounts.json"
    p = unittest.mock.patch(
        "cloudy.modules.microsoft365.mounts._mount_state_file", return_value=state)
    p.start()
    test.addCleanup(p.stop)
    return state


class TestResolvePath(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.addCleanup(self.tempdir.cleanup)
        _isolate_state_file(self)
        self.patch_root = unittest.mock.patch(
            "cloudy.modules.microsoft365.files.mount_root", return_value=self.root
        )
        self.patch_root.start()
        self.addCleanup(self.patch_root.stop)
        _save_mount_records([])

    def _record(self, account_id, drive_name, drive_id, mountpoint):
        recs = load_mount_records()
        recs.append({
            "account_id": account_id,
            "drive_name": drive_name,
            "drive_id": drive_id,
            "drive_kind": "documentLibrary",
            "mountpoint": str(mountpoint),
        })
        _save_mount_records(recs)

    def test_share_link_resolves_stored_mountpoint(self):
        mp = self.root / "My-Drive"
        mp.mkdir()
        file = mp / "folder" / "doc.txt"
        file.parent.mkdir()
        file.write_text("hello")
        self._record("acc1", "My Drive", "DRIVEA", mp)

        files = OneDriveFiles(_StubGraph())
        url = files.create_share_link(str(file))
        self.assertEqual(url, "https://share.example/ITEM1")
        calls = files._graph.calls
        self.assertEqual(calls[0], ("item_by_path", "DRIVEA", "folder/doc.txt"))
        self.assertEqual(calls[1][:3], ("create_share_link", "DRIVEA", "ITEM1"))

    def test_share_link_falls_back_to_default_drive(self):
        home = Path.home()
        rel = "Documents/report.pdf"
        path = home / rel

        files = OneDriveFiles(_StubGraph())
        files.create_share_link(str(path))
        self.assertEqual(files._graph.calls[0], ("item_by_path", "me", rel))


class TestMountArgv(unittest.TestCase):
    """Pin the rclone mount arguments that make uploads reliable + diagnosable."""

    def setUp(self):
        self.mgr = MountManager()
        self.mp = Path("/tmp/cloudy-test/acct/Drive")

    def test_always_logs_and_writes_back(self):
        # Without a log file the --daemon fork discards upload failures; without
        # an explicit write-back the push window is unknown. Both must be present
        # on every mount regardless of provider.
        argv = self.mgr.rclone_mount_argv("remote", self.mp)
        self.assertIn("--log-file", argv)
        self.assertIn("--log-level", argv)
        self.assertIn("--vfs-write-back", argv)

    def test_onedrive_ignores_size_and_checksum(self):
        # SharePoint rewrites Office files server-side; without these flags the
        # upload loops forever on "corrupted on transfer: sizes differ".
        argv = self.mgr.rclone_mount_argv("remote", self.mp, onedrive=True)
        self.assertIn("--ignore-size", argv)
        self.assertIn("--ignore-checksum", argv)

    def test_google_keeps_integrity_checks(self):
        # Google Drive doesn't mangle files, so it must keep its size/hash checks.
        argv = self.mgr.rclone_mount_argv("remote", self.mp, onedrive=False)
        self.assertNotIn("--ignore-size", argv)
        self.assertNotIn("--ignore-checksum", argv)

    def test_daemon_flag_is_last(self):
        # --daemon must stay the final arg (mount() relies on the fork behaviour).
        argv = self.mgr.rclone_mount_argv("remote", self.mp, onedrive=True)
        self.assertEqual(argv[-1], "--daemon")

    def test_no_rc_flags(self):
        # --rc cannot coexist with --daemon (binds before the fork → the child
        # hits "address already in use"), so it must NOT be on the mount.
        argv = self.mgr.rclone_mount_argv("remote", self.mp)
        self.assertNotIn("--rc", argv)
        self.assertNotIn("--rc-addr", argv)

    def test_cache_dir_not_overridden(self):
        # We must NOT override rclone's --cache-dir: moving it would strand files
        # still waiting to upload in the old cache (data loss).
        argv = self.mgr.rclone_mount_argv("remote", self.mp)
        self.assertNotIn("--cache-dir", argv)


class TestUploadStatus(unittest.TestCase):
    """upload_status counts rclone VFS 'Dirty' metadata entries (pending uploads)."""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.cache = Path(self.tempdir.name) / "vfscache"
        p = unittest.mock.patch.object(
            mounts_mod, "rclone_cache_dir", return_value=self.cache)
        p.start()
        self.addCleanup(p.stop)
        self.meta = self.cache / "vfsMeta" / "MyDrive"
        self.meta.mkdir(parents=True)
        self.mgr = MountManager()

    def _entry(self, name, dirty):
        (self.meta / name).write_text('{"Dirty": %s}' % ("true" if dirty else "false"))

    def test_counts_only_dirty(self):
        self._entry("a.txt", True)
        self._entry("b.txt", True)
        self._entry("c.txt", False)
        self.assertEqual(self.mgr.upload_status("MyDrive"), {"pending": 2})

    def test_zero_when_all_uploaded(self):
        self._entry("a.txt", False)
        self.assertEqual(self.mgr.upload_status("MyDrive"), {"pending": 0})

    def test_zero_when_no_cache(self):
        self.assertEqual(self.mgr.upload_status("Unknown"), {"pending": 0})


class TestReconcile(unittest.TestCase):
    """Bookmark ⟷ record ⟷ remote reconciliation (the stale-bookmark fix)."""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name) / "mounts"
        self.bookmarks = Path(self.tempdir.name) / "bookmarks"
        _isolate_state_file(self)
        _save_mount_records([])
        for target, ret in (("mount_root", self.root),
                            ("_bookmarks_file", self.bookmarks)):
            p = unittest.mock.patch.object(mounts_mod, target, return_value=ret)
            p.start()
            self.addCleanup(p.stop)
        # A mount backend must look available for config_dump to be consulted.
        p = unittest.mock.patch.object(
            MountManager, "preferred_backend", return_value=mounts_mod.RCLONE)
        p.start()
        self.addCleanup(p.stop)

        self.account = Account.from_dict(
            {"id": "ms-1", "display_name": "user@corp.com", "provider": "microsoft"})
        self.registry = FakeRegistry()
        self.registry._accounts = [self.account]
        # base = mount_root()/safe_name(display_name)
        self.base = self.root / "user_corp_com"
        self.base.mkdir(parents=True)

    def _write_bookmarks(self, lines):
        self.bookmarks.write_text("\n".join(lines) + "\n")

    def _uri(self, path):
        from urllib.parse import quote
        return "file://" + quote(str(path))

    def test_adopts_orphan_with_remote_and_removes_true_orphan(self):
        live = self.base / "MyLib"        # has a remote → adopt
        ghost = self.base / "Ghost"       # no remote → remove
        other = Path.home() / "Documents"  # not ours → untouched
        self._write_bookmarks([
            f"{self._uri(live)} MyLib",
            f"{self._uri(ghost)} Ghost",
            f"{self._uri(other)} Documents",
        ])
        dump = {"MyLib": {"type": "onedrive", "drive_id": "D1",
                          "drive_type": "documentLibrary"}}
        with unittest.mock.patch.object(MountManager, "config_dump", return_value=dump), \
             unittest.mock.patch.object(MountManager, "is_mounted", return_value=False):
            counts = reconcile_mounts(self.registry)

        self.assertEqual(counts["adopted"], 1)
        self.assertEqual(counts["removed"], 1)
        # MyLib now remembered, pointing at the right account + mountpoint.
        recs = load_mount_records()
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["account_id"], "ms-1")
        self.assertEqual(recs[0]["drive_id"], "D1")
        self.assertEqual(recs[0]["mountpoint"], str(live))
        # The MyLib + Documents bookmarks survive; the ghost stub is gone.
        kept = self.bookmarks.read_text()
        self.assertIn("MyLib", kept)
        self.assertIn("Documents", kept)
        self.assertNotIn("Ghost", kept)

    def test_leaves_non_cloudy_bookmarks_alone(self):
        other = Path.home() / "Music"
        self._write_bookmarks([f"{self._uri(other)} Music"])
        with unittest.mock.patch.object(MountManager, "config_dump", return_value={}), \
             unittest.mock.patch.object(MountManager, "is_mounted", return_value=False):
            counts = reconcile_mounts(self.registry)
        self.assertEqual(counts, {"adopted": 0, "recorded": 0, "removed": 0})
        self.assertIn("Music", self.bookmarks.read_text())


if __name__ == "__main__":
    unittest.main()
