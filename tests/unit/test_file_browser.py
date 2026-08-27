# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Shahab Nedaei

import unittest
from pathlib import Path

import gi_setup  # pins GI versions; exposes AVAILABLE

if gi_setup.AVAILABLE:
    from gi.repository import Gtk

    from cloudy.widgets.file_browser import FileBrowserPane

_skip = unittest.skipUnless(gi_setup.AVAILABLE,
                            "GTK/Adw typelibs unavailable (headless build)")


def _entries(n, prefix="f"):
    return [{"name": f"{prefix}{i}", "is_dir": False, "path": f"/m/{prefix}{i}",
             "size": i, "mtime": float(i)} for i in range(n)]


def _rows(listbox):
    out, child = [], listbox.get_first_child()
    while child is not None:
        out.append(child)
        child = child.get_next_sibling()
    return out


@_skip
class TestRenderCap(unittest.TestCase):
    """A folder with tens of thousands of files must not build a widget per
    entry on the GTK thread — that is a multi-second freeze."""

    def setUp(self):
        Gtk.init_check()
        self.pane = FileBrowserPane(None)

    def test_list_render_is_capped_and_says_how_many_are_hidden(self):
        self.pane._entries = _entries(self.pane._RENDER_CAP + 25)
        self.pane._render_list()
        # header row + capped rows + the "N more not shown" row
        self.assertEqual(len(_rows(self.pane._list)), self.pane._RENDER_CAP + 2)

    def test_small_folder_renders_completely_with_no_notice(self):
        self.pane._entries = _entries(7)
        self.pane._render_list()
        self.assertEqual(len(_rows(self.pane._list)), 8)  # header + 7

    def test_grid_render_is_capped_too(self):
        self.pane._entries = _entries(self.pane._RENDER_CAP + 3)
        self.pane._render_grid()
        self.assertEqual(len(_rows(self.pane._flow)), self.pane._RENDER_CAP + 1)


@_skip
class TestExpandRace(unittest.TestCase):
    """An inline folder expansion scans off-thread. If the user navigates away
    first, the late result must be dropped — not written into the new folder's
    state and re-rendered over it."""

    def setUp(self):
        Gtk.init_check()
        self.pane = FileBrowserPane(None)
        self.pane._root = Path("/m")
        self.pane._history = [Path("/m/one"), Path("/m/two")]

    def test_result_for_the_previous_folder_is_ignored(self):
        self.pane._hpos = 1                       # now viewing /m/two
        self.pane._on_children(Path("/m/one"), "/m/one/sub", _entries(3), None)
        self.assertEqual(self.pane._child_cache, {})

    def test_result_for_the_current_folder_is_kept(self):
        self.pane._hpos = 1
        self.pane._on_children(Path("/m/two"), "/m/two/sub", _entries(3), None)
        self.assertIn("/m/two/sub", self.pane._child_cache)


if __name__ == "__main__":
    unittest.main()
