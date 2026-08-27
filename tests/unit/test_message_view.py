# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Shahab Nedaei

import unittest

import gi_setup  # pins GI versions; exposes AVAILABLE

if gi_setup.AVAILABLE:
    from cloudy.widgets.message_view import (
        _block_remote_images,
        _has_remote_images,
        _resolve_cids,
        _to_text,
        _wrap_html,
    )

_skip = unittest.skipUnless(gi_setup.AVAILABLE,
                            "GTK/Adw typelibs unavailable (headless build)")


@_skip
class TestToText(unittest.TestCase):
    def test_html_to_text(self):
        text = _to_text("<p>Hello<br/>world</p>")
        self.assertIn("Hello", text)
        self.assertIn("world", text)

    def test_plain_passthrough(self):
        self.assertEqual(_to_text("plain text"), "plain text")


@_skip
class TestResolveCids(unittest.TestCase):
    def test_inline_image_replaced_with_data_uri(self):
        inline = [{"content_id": "img1", "content_bytes": "YWJj",
                   "content_type": "image/png"}]
        out = _resolve_cids('<img src="cid:img1">', inline)
        self.assertIn("data:image/png;base64,YWJj", out)

    def test_missing_attachment_keeps_original(self):
        out = _resolve_cids('<img src="cid:missing">', [])
        self.assertIn('src="cid:missing"', out)


@_skip
class TestBlockRemoteImages(unittest.TestCase):
    def test_http_image_blocked(self):
        body = '<img src="http://evil.com/track.png">'
        out = _block_remote_images(body)
        self.assertNotIn("http://evil.com/track.png", out)
        self.assertIn("<img", out)
        self.assertIn('data-cloudy-blocked="1"', out)
        self.assertIn("data:image/gif;base64,", out)  # transparent placeholder

    def test_https_image_blocked(self):
        body = "<img src='https://example.com/pic.jpg' width='10'>"
        out = _block_remote_images(body)
        self.assertNotIn("https://example.com/pic.jpg", out)
        self.assertIn('data-cloudy-blocked="1"', out)
        self.assertIn("width='10'", out)  # other attributes survive intact

    def test_cid_untouched(self):
        body = '<img src="cid:img1">'
        self.assertEqual(_block_remote_images(body), body)

    def test_data_uri_unchanged(self):
        body = '<img src="data:image/png;base64,abc">'
        out = _block_remote_images(body)
        self.assertIn("data:image/png;base64,abc", out)
        self.assertNotIn("data-cloudy-blocked", out)

    def test_already_blocked_tag_left_alone(self):
        body = '<img src="http://e.com/a.png" data-cloudy-blocked="1">'
        self.assertEqual(_block_remote_images(body), body)

    def test_idempotent(self):
        once = _block_remote_images('<img src="http://e.com/a.png" width="4">')
        self.assertEqual(_block_remote_images(once), once)

    def test_data_src_attribute_not_touched(self):
        body = '<img data-src="http://e.com/lazy.png" src="data:image/png;base64,aa">'
        out = _block_remote_images(body)
        self.assertIn("http://e.com/lazy.png", out)  # not a live src

    def test_background_image_blocked(self):
        body = '<div style="background-image: url(http://x.com/bg.png)">x</div>'
        out = _block_remote_images(body)
        self.assertNotIn("http://x.com/bg.png", out)


@_skip
class TestRemoteImageOptIn(unittest.TestCase):
    """The un-block path: the same body rendered with load_remote=True."""

    BODY = '<p>hi</p><img src="https://example.com/pic.jpg">'

    def test_blocked_by_default(self):
        doc = _wrap_html(self.BODY, True)
        self.assertNotIn("https://example.com/pic.jpg", doc)
        self.assertIn('data-cloudy-blocked="1"', doc)

    def test_load_remote_keeps_images(self):
        doc = _wrap_html(self.BODY, True, load_remote=True)
        self.assertIn("https://example.com/pic.jpg", doc)
        self.assertNotIn("data-cloudy-blocked", doc)

    def test_has_remote_images(self):
        self.assertTrue(_has_remote_images(self.BODY))
        self.assertTrue(
            _has_remote_images('<div style="background: url(https://x/b.png)">'))
        self.assertFalse(_has_remote_images('<img src="cid:pic">'))
        self.assertFalse(_has_remote_images("plain text"))


@_skip
class TestLoadImagesBanner(unittest.TestCase):
    """The reader banner: shown only when remote images were neutralized."""

    def setUp(self):
        from gi.repository import Gtk

        Gtk.init_check()

    def _walk(self, widget):
        yield widget
        child = widget.get_first_child()
        while child is not None:
            yield from self._walk(child)
            child = child.get_next_sibling()

    def _banners(self, content):
        from gi.repository import Adw

        return [w for w in self._walk(content) if isinstance(w, Adw.Banner)]

    def test_banner_offered_and_load_re_renders(self):
        from cloudy.widgets.message_view import build_message_content

        msg = {"subject": "pics", "from": "a@b.c",
               "received": "2026-08-27T09:00:00Z",
               "body": '<p>see</p><img src="https://x.com/pic.png">',
               "body_html": True}
        content = build_message_content(msg)
        banners = self._banners(content)
        self.assertEqual(len(banners), 1)
        self.assertTrue(banners[0].get_revealed())
        banners[0].emit("button-clicked")
        self.assertFalse(banners[0].get_revealed())  # per-message, one-shot

    def test_no_banner_without_remote_images(self):
        from cloudy.widgets.message_view import build_message_content

        msg = {"subject": "plain", "from": "a@b.c",
               "received": "2026-08-27T09:00:00Z",
               "body": "<p>hello</p>", "body_html": True}
        content = build_message_content(msg)
        self.assertEqual(self._banners(content), [])


if __name__ == "__main__":
    unittest.main()
