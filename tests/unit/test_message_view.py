# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Shahab Nedaei

import unittest

from gi_setup import gi  # noqa: F401 - pins GI versions before import

from cloudy.widgets.message_view import _block_remote_images, _resolve_cids, _to_text


class TestToText(unittest.TestCase):
    def test_html_to_text(self):
        text = _to_text("<p>Hello<br/>world</p>")
        self.assertIn("Hello", text)
        self.assertIn("world", text)

    def test_plain_passthrough(self):
        self.assertEqual(_to_text("plain text"), "plain text")


class TestResolveCids(unittest.TestCase):
    def test_inline_image_replaced_with_data_uri(self):
        inline = [{"content_id": "img1", "content_bytes": "YWJj",
                   "content_type": "image/png"}]
        out = _resolve_cids('<img src="cid:img1">', inline)
        self.assertIn("data:image/png;base64,YWJj", out)

    def test_missing_attachment_keeps_original(self):
        out = _resolve_cids('<img src="cid:missing">', [])
        self.assertIn('src="cid:missing"', out)


class TestBlockRemoteImages(unittest.TestCase):
    def test_http_image_blocked(self):
        body = '<img src="http://evil.com/track.png">'
        out = _block_remote_images(body)
        self.assertNotIn("http://evil.com/track.png", out)
        self.assertIn("<img", out)

    def test_https_image_blocked(self):
        body = "<img src='https://example.com/pic.jpg'>"
        out = _block_remote_images(body)
        self.assertNotIn("https://example.com/pic.jpg", out)

    def test_data_uri_unchanged(self):
        body = '<img src="data:image/png;base64,abc">'
        out = _block_remote_images(body)
        self.assertIn("data:image/png;base64,abc", out)

    def test_background_image_blocked(self):
        body = '<div style="background-image: url(http://x.com/bg.png)">x</div>'
        out = _block_remote_images(body)
        self.assertNotIn("http://x.com/bg.png", out)


if __name__ == "__main__":
    unittest.main()
