# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Shahab Nedaei
"""Headless logic tests for ChatView's pure pieces: the composer's markdown
detection (word-bounded so snake_case/URLs don't force a rich send), the
HTML composer, and the chat-list page merge."""

import unittest

import gi_setup  # pins GI versions; exposes AVAILABLE

# chat_view imports Gtk/Adw at module top; only import it when those typelibs
# exist (they don't in a minimal RPM build chroot), else skip these tests.
if gi_setup.AVAILABLE:
    from cloudy.widgets.chat_view import (
        _MD_BOLD,
        _MD_ITALIC,
        _merge_chat_pages,
        ChatView,
    )

_skip = unittest.skipUnless(gi_setup.AVAILABLE,
                            "GTK/Adw typelibs unavailable (headless build)")


@_skip
class TestMarkdownRegexes(unittest.TestCase):
    def test_bold_requires_space_boundaries(self):
        self.assertIsNotNone(_MD_BOLD.search("**bold**"))
        self.assertIsNone(_MD_BOLD.search("a**b**c"))

    def test_italic_requires_word_boundaries(self):
        self.assertIsNotNone(_MD_ITALIC.search("_it_"))
        self.assertIsNone(_MD_ITALIC.search("snake_case_word"))

    def test_bold_spans_lines(self):
        self.assertIsNotNone(_MD_BOLD.search("**line one\nline two**"))


@_skip
class TestHasMarkdown(unittest.TestCase):
    def test_bold_detected(self):
        self.assertTrue(ChatView._has_markdown("hello **world**"))

    def test_italic_detected(self):
        self.assertTrue(ChatView._has_markdown("hello _world_"))
        self.assertTrue(ChatView._has_markdown("_emphasis_"))

    def test_plain_text_has_no_markdown(self):
        self.assertFalse(ChatView._has_markdown("just some words"))

    def test_snake_case_is_not_markdown(self):
        # A false positive here forced the rich (HTML) send path, which Google
        # Chat rejects outright — the bug these regexes are tightened against.
        self.assertFalse(ChatView._has_markdown("my_var and another_value_here"))

    def test_underscored_url_is_not_markdown(self):
        self.assertFalse(ChatView._has_markdown("see https://ex.co/a_b/c_d now"))

    def test_mid_word_bold_is_not_markdown(self):
        self.assertFalse(ChatView._has_markdown("a**b**c"))


@_skip
class TestComposeHtml(unittest.TestCase):
    def test_markdown_converted(self):
        out, mentions = ChatView._compose_html("**bold** and _it_", [])
        self.assertEqual(out, "<b>bold</b> and <i>it</i>")
        self.assertEqual(mentions, [])

    def test_snake_case_left_alone(self):
        out, _ = ChatView._compose_html("my_var_name", [])
        self.assertEqual(out, "my_var_name")

    def test_markup_escaped(self):
        out, _ = ChatView._compose_html("<b> & </b>", [])
        self.assertEqual(out, "&lt;b&gt; &amp; &lt;/b&gt;")

    def test_newlines_become_brs(self):
        out, _ = ChatView._compose_html("a\nb", [])
        self.assertEqual(out, "a<br>b")

    def test_mention_wrapped_and_recorded(self):
        out, mentions = ChatView._compose_html("hi @Bob", [{"id": "u1", "name": "Bob"}])
        self.assertEqual(out, 'hi <at id="0">Bob</at>')
        self.assertEqual(len(mentions), 1)
        self.assertEqual(mentions[0]["mentioned"]["user"]["id"], "u1")

    def test_stale_mention_not_wrapped(self):
        out, mentions = ChatView._compose_html("hi there", [{"id": "u1", "name": "Bob"}])
        self.assertEqual(out, "hi there")
        self.assertEqual(mentions, [])


@_skip
class TestMergeChatPages(unittest.TestCase):
    def test_fresh_page_keeps_paged_in_older_chats(self):
        # A background refresh only holds page 1 — merging (not replacing)
        # keeps every older conversation already paged in.
        existing = [{"id": "a", "last_at": "3"}, {"id": "b", "last_at": "1"}]
        fresh = [{"id": "a", "last_at": "9", "preview": "new"}]
        merged = _merge_chat_pages(existing, fresh)
        self.assertEqual([c["id"] for c in merged], ["a", "b"])
        self.assertEqual(merged[0]["preview"], "new")  # fresh copy wins

    def test_no_duplicates_when_chat_reappears(self):
        existing = [{"id": "a", "last_at": "1"}, {"id": "b", "last_at": "2"}]
        fresh = [{"id": "b", "last_at": "2"}, {"id": "c", "last_at": "3"}]
        merged = _merge_chat_pages(existing, fresh)
        self.assertEqual([c["id"] for c in merged], ["c", "b", "a"])

    def test_sorted_newest_first(self):
        merged = _merge_chat_pages([], [{"id": "x", "last_at": "1"},
                                        {"id": "y", "last_at": "5"}])
        self.assertEqual([c["id"] for c in merged], ["y", "x"])

    def test_empty_inputs(self):
        self.assertEqual(_merge_chat_pages([], []), [])


if __name__ == "__main__":
    unittest.main()
