# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Shahab Nedaei

import unittest

from cloudy.core.interfaces import (
    CalendarCapability,
    ChatCapability,
    FilesCapability,
    MailCapability,
    ServiceModule,
    capabilities_of,
)
from cloudy.modules.microsoft365.mounts import MountManager
from cloudy.widgets.format import esc


class TestMountHelpers(unittest.TestCase):
    def test_safe_name_sanitizes(self):
        # Path-breaking chars (/, #) become '_'; spaces, '-', '_' and alnum stay.
        self.assertEqual(MountManager._safe_name("My Drive / Shared#1"),
                         "My Drive _ Shared_1")
        self.assertEqual(MountManager._safe_name("OneDrive"), "OneDrive")
        self.assertEqual(MountManager._safe_name("a/b:c*d"), "a_b_c_d")

    def test_drive_type_mapping(self):
        self.assertEqual(MountManager.drive_type_for("personal"), "personal")
        self.assertEqual(MountManager.drive_type_for("business"), "business")
        self.assertEqual(MountManager.drive_type_for("team"), "documentLibrary")
        self.assertEqual(MountManager.drive_type_for("unknown"), "documentLibrary")

    def test_shared_drive_enumeration_degrades_without_token(self):
        self.assertEqual(MountManager().list_google_shared_drives(""), [])


class TestCapabilities(unittest.TestCase):
    def test_order_and_subset(self):
        class Mod(ServiceModule, FilesCapability, MailCapability):
            id = "m"
            name = "M"

            def activate(self, ctx):
                pass

            def deactivate(self):
                pass

            def list_drives(self):
                return []

            def create_share_link(self, path, *, editable=False):
                return ""

            def list_folders(self):
                return []

            def list_messages(self, folder_id, *, limit=50):
                return []

        caps = capabilities_of(Mod())
        self.assertEqual(caps, ["files", "mail"])  # order preserved, subset only

    def test_real_gmail_module_caps(self):
        from cloudy.modules.gmail import MODULE

        caps = capabilities_of(MODULE())
        for expected in ("files", "mail", "calendar", "chat"):
            self.assertIn(expected, caps)


class TestEsc(unittest.TestCase):
    def test_escapes_markup_breakers(self):
        self.assertEqual(esc("R&D <tag>"), "R&amp;D &lt;tag&gt;")

    def test_leaves_quotes_alone(self):
        self.assertEqual(esc("Couldn't \"do\""), "Couldn't \"do\"")

    def test_none_safe(self):
        self.assertEqual(esc(None), "")


if __name__ == "__main__":
    unittest.main()
