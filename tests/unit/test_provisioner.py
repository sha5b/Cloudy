# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Shahab Nedaei

import hashlib
import io
import os
import tempfile
import unittest
import unittest.mock
import zipfile
from pathlib import Path

from cloudy.core.provisioner import RCLONE_SHA256, ensure_rclone, resolve


class TestEnsureRclone(unittest.TestCase):
    def test_checksum_mismatch_refuses_to_install(self):
        bad_blob = b"not a real rclone archive"
        with unittest.mock.patch("cloudy.core.provisioner.resolve", return_value=None), \
             unittest.mock.patch("cloudy.core.provisioner._rclone_arch", return_value="amd64"), \
             unittest.mock.patch("cloudy.core.provisioner._fetch", return_value=bad_blob):
            with self.assertRaises(RuntimeError) as ctx:
                ensure_rclone()
            self.assertIn("checksum mismatch", str(ctx.exception).lower())

    def test_good_archive_installs_verified_binary(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("rclone-vX-linux-amd64/rclone", b"#!/bin/sh\necho ok\n")
        blob = buf.getvalue()
        expected = "amd64"
        fake_sha = {expected: hashlib.sha256(blob).hexdigest()}
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            with unittest.mock.patch("cloudy.core.provisioner.resolve", return_value=None), \
                 unittest.mock.patch("cloudy.core.provisioner._rclone_arch", return_value=expected), \
                 unittest.mock.patch("cloudy.core.provisioner.RCLONE_SHA256", fake_sha), \
                 unittest.mock.patch("cloudy.core.provisioner.deps_bin_dir", return_value=tmp_path), \
                 unittest.mock.patch("cloudy.core.provisioner._fetch", return_value=blob):
                path = ensure_rclone()
            self.assertTrue(Path(path).exists())
            self.assertTrue(os.access(path, os.X_OK))


if __name__ == "__main__":
    unittest.main()
