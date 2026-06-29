# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Shahab Nedaei

import tempfile
import unittest
import unittest.mock
from pathlib import Path

from cloudy.modules.microsoft365.files import OneDriveFiles
from cloudy.modules.microsoft365.mounts import (
    _save_mount_records,
    load_mount_records,
)


class _StubGraph:
    def __init__(self):
        self.calls = []

    def item_by_path(self, drive_id, rel_path):
        self.calls.append(("item_by_path", drive_id, rel_path))
        return {"id": "ITEM1"}

    def create_share_link(self, drive_id, item_id, *, editable=False):
        self.calls.append(("create_share_link", drive_id, item_id, editable))
        return "https://share.example/ITEM1"


class TestResolvePath(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.addCleanup(self.tempdir.cleanup)
        self.patch_root = unittest.mock.patch(
            "cloudy.modules.microsoft365.files.mount_root", return_value=self.root
        )
        self.patch_root.start()
        self.addCleanup(self.patch_root.stop)
        _save_mount_records([])
        self.addCleanup(_save_mount_records, [])

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


if __name__ == "__main__":
    unittest.main()
