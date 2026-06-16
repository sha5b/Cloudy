# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Shahab Nedaei
"""MemoryCache: stale-while-revalidate semantics + disk persistence."""

import json
import tempfile
import time
import unittest
from pathlib import Path

from cloudy.core.cache import MemoryCache


class TestMemoryCache(unittest.TestCase):
    def test_get_missing_returns_none(self):
        self.assertIsNone(MemoryCache().get("nope"))

    def test_set_then_fresh(self):
        c = MemoryCache(ttl=90)
        c.set("k", [1, 2, 3])
        value, fresh = c.get("k")
        self.assertEqual(value, [1, 2, 3])
        self.assertTrue(fresh)

    def test_ttl_expiry_marks_stale(self):
        c = MemoryCache(ttl=0.01)
        c.set("k", "v")
        time.sleep(0.02)
        value, fresh = c.get("k")
        self.assertEqual(value, "v")          # still returned
        self.assertFalse(fresh)               # but stale

    def test_invalidate_prefix(self):
        c = MemoryCache()
        c.set("a:1", 1)
        c.set("a:2", 2)
        c.set("b:1", 3)
        c.invalidate(prefix="a:")
        self.assertIsNone(c.get("a:1"))
        self.assertIsNone(c.get("a:2"))
        self.assertIsNotNone(c.get("b:1"))

    def test_invalidate_all(self):
        c = MemoryCache()
        c.set("a", 1)
        c.invalidate()
        self.assertIsNone(c.get("a"))


class TestPersistentCache(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = Path(self.dir) / "sub" / "cache.json"

    def test_persist_and_reload_as_stale(self):
        c = MemoryCache(ttl=90, path=self.path)
        c.set("acct:messages:inbox", [{"id": "1", "subject": "Hi"}])
        c.flush()
        self.assertTrue(self.path.exists())

        c2 = MemoryCache(ttl=90, path=self.path)
        got = c2.get("acct:messages:inbox")
        self.assertIsNotNone(got)
        value, fresh = got
        self.assertEqual(value[0]["subject"], "Hi")
        self.assertFalse(fresh)  # disk entries load stale → force revalidation

    def test_non_serializable_values_are_skipped(self):
        class Drive:  # not JSON-serializable
            pass

        c = MemoryCache(path=self.path)
        c.set("acct:messages", [{"id": "1"}])
        c.set("acct:libraries", [{"drive": Drive()}])
        c.flush()
        on_disk = json.loads(self.path.read_text())
        self.assertIn("acct:messages", on_disk)
        self.assertNotIn("acct:libraries", on_disk)

    def test_corrupt_file_degrades(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("{ this is not json")
        c = MemoryCache(path=self.path)  # must not raise
        self.assertIsNone(c.get("anything"))

    def test_flush_without_path_is_noop(self):
        MemoryCache().flush()  # no path → silently does nothing


if __name__ == "__main__":
    unittest.main()
