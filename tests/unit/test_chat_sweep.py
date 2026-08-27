# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Shahab Nedaei
"""Headless end-to-end sweep of the Chat tab against a FAKE Teams client.

Nothing real is touched: a ``FakeChatClient`` is injected into the app's
per-account client cache (``build_account_client`` returns the cache hit), so
list loads, thread renders, sends, reactions, deletes, pagination, search and
scroll state all run through the real ChatView code paths against canned data.
``_pump()`` runs a short GLib main loop so ``run_async`` worker results are
delivered, exactly like the live app.

Covers the regression classes from the 2026-08 audit fix pass: chat-scoped
optimistic echoes, adoption-by-id after rich/plain sends, composer clearing on
switch, wrong-chat failed bubbles, background-refresh pagination collapse,
read-while-hidden, tombstone un-hiding, search-row leaks — plus render-time
ceilings as freeze guards.
"""

import io
import time
import unittest
import unittest.mock

import gi_setup  # pins GI versions; exposes AVAILABLE

if gi_setup.AVAILABLE:
    from gi.repository import GLib, Gtk

_skip = unittest.skipUnless(gi_setup.AVAILABLE,
                            "GTK/Adw typelibs unavailable (headless build)")

if gi_setup.AVAILABLE:
    from cloudy.widgets.chat_view import ChatView
    from cloudy.core.account_registry import Account
    from cloudy.core.cache import MemoryCache
    from fakes import FakeSettings, FakeRegistry


def _pump(ms: int = 400) -> None:
    """Run a short main loop so queued idles/timeouts (run_async callbacks,
    debounces) are delivered."""
    loop = GLib.MainLoop()
    GLib.timeout_add(ms, loop.quit)
    loop.run()


def _png(size: int = 96, color=(160, 60, 200)) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (size, size), color).save(buf, format="PNG")
    return buf.getvalue()


def _walk(widget):
    yield widget
    child = widget.get_first_child()
    while child is not None:
        yield from _walk(child)
        child = child.get_next_sibling()


def _labels_with(widget, css_class: str):
    return [w for w in _walk(widget)
            if isinstance(w, Gtk.Label) and css_class in w.get_css_classes()]


class _FakeApp:
    def __init__(self):
        self.settings = FakeSettings()
        self.registry = FakeRegistry()
        self.cache = MemoryCache()
        self.application_id = "io.github.sha5b.Cloudy"
        self.notifier = None  # ChatView getattr-guards this
        self.opened_uris = []
        self._clients = {}
        self.props = type("P", (), {"active_window": None})()

    def get_account_client(self, account):
        return self._clients.get(account.id)

    def set_account_client(self, account, client):
        self._clients[account.id] = client


class _FakeWindow:
    def __init__(self, app):
        self._app = app
        self.toasts = []

    def get_application(self):
        return self._app

    def add_toast(self, message):
        self.toasts.append(str(message))

    def open_uri(self, uri):
        self._app.opened_uris.append(uri)

    def sign_in_account(self, account):
        pass


def _chat(cid, name, kind="oneOnOne", **extra):
    row = {"id": cid, "name": name, "kind": kind, "preview": "",
           "last_at": "2026-08-2%dT10:00:00Z" % (7 - int(cid[-1]) % 7),
           "unread": False, "from_me": False, "member_ids": [],
           "member_count": 0}
    row.update(extra)
    return row


def _msg(mid, text="", **extra):
    row = {"id": mid, "text": text, "markup": "", "from": "Ann",
           "sent": "2026-08-20T1%d:00:00Z" % (int(mid[-1]) % 10 if mid[-1].isdigit() else 0),
           "is_mine": False, "attachments": [], "reactions": [],
           "web_url": "", "reply_to": None, "forward": None}
    row.update(extra)
    return row


class FakeChatClient:
    """The Graph chat surface ChatView uses, over in-memory pages."""

    def __init__(self):
        self.chats_page1 = []
        self.chats_page2 = []
        self.pages = {}          # chat id -> list of messages (newest last)
        self.older = {}          # chat id -> (messages, next_token)
        self.members = {}
        self.presences = {}
        self.hits = []
        self.marked_read = []
        self.reactions = []
        self.deleted = []
        self.edits = []
        self.sends = []
        self.fail_send = False
        self._next = 0

    def _new_id(self):
        self._next += 1
        return f"srv-{self._next}"

    # -- list -----------------------------------------------------------
    def list_chats_page(self, *, limit=50, page_token=None):
        if page_token:
            return list(self.chats_page2), None
        return list(self.chats_page1), "page2-token" if self.chats_page2 else None

    # -- thread ---------------------------------------------------------
    def list_chat_messages_page(self, chat_id, *, limit=30, page_token=None):
        if page_token:
            older = self.older.get(chat_id)
            return (list(older[0]), older[1]) if older else ([], None)
        return list(self.pages.get(chat_id, [])), \
            "older-token" if chat_id in self.older else None

    def send_chat_message(self, chat_id, text):
        if self.fail_send:
            raise RuntimeError("Graph 503 (fake)")
        mid = self._new_id()
        self.sends.append((chat_id, text))
        self.pages.setdefault(chat_id, []).append(
            _msg(mid, text, is_mine=True, from_=""))
        return {"id": mid}

    def send_chat_html(self, chat_id, content_html, mentions=None,
                       images=None, file_attachments=None):
        if self.fail_send:
            raise RuntimeError("Graph 503 (fake)")
        mid = self._new_id()
        self.sends.append((chat_id, content_html))
        return {"id": mid}

    def mark_chat_read(self, chat_id):
        self.marked_read.append(chat_id)

    def list_chat_members(self, chat_id):
        return list(self.members.get(chat_id, []))

    def get_presences(self, user_ids):
        return dict(self.presences)

    def search_messages(self, query):
        return list(self.hits)

    def set_reaction(self, chat_id, message_id, emoji):
        self.reactions.append((chat_id, message_id, emoji))

    def delete_chat_message(self, chat_id, message_id):
        self.deleted.append((chat_id, message_id))

    def edit_chat_message(self, chat_id, message_id, text):
        self.edits.append((chat_id, message_id, text))

    def fetch_bytes(self, url, scopes=None):
        return _png()


class _ChatSweepBase(unittest.TestCase):
    def setUp(self):
        Gtk.init_check()
        self.app = _FakeApp()
        self.window = _FakeWindow(self.app)
        self.client = FakeChatClient()
        self.account = Account.from_dict(
            {"id": "ms-1", "display_name": "me@corp.com",
             "provider": "microsoft", "signed_in": True})
        self.app.registry._accounts = [self.account]
        self.app.set_account_client(self.account, self.client)
        self.view = ChatView(self.window, self.account)
        # run_async drops callbacks for unrooted widgets — parent the view in a
        # real (never-shown) window so it has a root, like the live app.
        self.host = Gtk.Window()
        self.host.set_child(self.view)
        self.addCleanup(self.view._stop_poll)
        self.addCleanup(self._stop_presence_timer)

    def _stop_presence_timer(self):
        if getattr(self.view, "_presence_source", None):
            GLib.source_remove(self.view._presence_source)
            self.view._presence_source = None

    def _reload_chats(self):
        """Seed-then-load: the constructor already fetched the (empty) fake
        list, so re-trigger the load after the test seeds its data."""
        self.view._load_chats()
        _pump()

    def _thread_ids(self):
        return [s[0] for s in getattr(self.view, "_rendered_sigs", [])]


@_skip
class TestChatListSweep(_ChatSweepBase):
    def test_kinds_sections_presence_and_unread(self):
        self.client.chats_page1 = [
            _chat("c1", "Ann Peak", member_ids=["u1"], unread=True,
                  preview="hi there"),
            _chat("c2", "Project X", kind="group", member_count=4),
            _chat("c3", "Daily sync", kind="meeting"),
        ]
        self.client.presences = {"u1": {"availability": "Available",
                                        "activity": "Available"}}
        self._reload_chats()

        rows = {getattr(r, "_chat_id", None): r
                for r in _walk(self.view._list)
                if isinstance(r, Gtk.ListBoxRow)}
        self.assertIn("c1", rows)
        self.assertIn("c2", rows)
        self.assertIn("c3", rows)
        # No stray placeholder row ("Loading chats…" / "No conversations.")
        # above the rendered list — every row is patch-keyed or a section.
        strays = [r for r in rows.values()
                  if getattr(r, "_patch_key", None) is None]
        self.assertEqual(strays, [])
        # Meetings are grouped under their own header row.
        sections = [w.get_text() for w in _labels_with(self.view._list,
                                                       "caption-heading")]
        self.assertIn("Meetings & calls", sections)
        # 1:1 chats get a presence dot after the batch fetch.
        _pump(600)
        self.assertIsNotNone(getattr(rows["c1"]._avatar_overlay,
                                     "_presence_dot", None))
        # Unread chat renders bold; opening it greys the row in place.
        self.assertTrue(_labels_with(rows["c1"], "heading"))
        self.view.open_chat("c1")
        _pump()
        self.assertFalse(_labels_with(rows["c1"], "heading"))

    def test_background_refresh_keeps_paged_in_conversations(self):
        self.client.chats_page1 = [_chat("c1", "Ann")]
        self.client.chats_page2 = [_chat("c3", "Zoe")]
        self._reload_chats()
        self.view._load_more_chats()
        _pump()
        self.assertIn("c3", [c["id"] for c in self.view._all_chats])
        # A notifier-driven refresh (page 1 again) must not drop c3, and the
        # cursor must survive a page-1 response that carries its own token.
        self.view._load_chats()
        _pump()
        ids = [c["id"] for c in self.view._all_chats]
        self.assertIn("c3", ids)
        self.assertEqual(self.view._chats_next_token, "page2-token")


@_skip
class TestChatThreadSweep(_ChatSweepBase):
    def _seed_thread(self, load=True):
        self.client.chats_page1 = [_chat("c1", "Ann Peak", member_ids=["u1"])]
        self.client.pages["c1"] = [
            _msg("m1", "plain text message"),
            _msg("m2", "see the docs",
                 markup='see the <a href="https://example.com/doc">docs</a>',
                 web_url="https://teams.example/m2"),
            _msg("m3", "bold one", markup="<b>bold one</b>"),
            _msg("m4", "reply body", reply_to={
                "id": "m1", "from": "Ann", "text": "plain text message"}),
            _msg("m5", "", forward={
                "id": "m0", "from": "Bob", "text": "forwarded original",
                "chat_id": "c1"}),
            _msg("m6", "", attachments=[
                {"name": "shot.png", "url": "https://graph/img1",
                 "content_type": "image/png"},
                {"name": "report.pdf", "url": "https://graph/f1",
                 "content_type": "application/pdf"}]),
            _msg("m7", "with reactions",
                 reactions=[{"emoji": "👍", "count": 2}]),
            {"id": "m8", "text": "Ann added Cleo", "markup": "", "from": "",
             "sent": "2026-08-20T18:00:00Z", "is_mine": False,
             "attachments": [], "reactions": [], "web_url": "",
             "reply_to": None, "forward": None, "system": True},
            {"id": "m9", "text": "", "markup": "", "from": "Ann",
             "sent": "2026-08-20T19:00:00Z", "is_mine": False,
             "attachments": [], "reactions": [], "web_url": "",
             "reply_to": None, "forward": None, "deleted": True},
        ]
        self.client.members["c1"] = [
            {"id": "u1", "name": "Ann Peak", "membership_id": "mem1",
             "email": "ann@corp.com"}]
        if load:
            self._reload_chats()

    def test_renders_every_message_shape(self):
        self._seed_thread()
        _pump()
        self.view.open_chat("c1")
        _pump(600)  # members + image fetch land

        for mid in ("m1", "m2", "m3", "m4", "m5", "m6", "m7", "m9"):
            self.assertIn(mid, self.view._bubble_widgets,
                          f"bubble for {mid} missing")
        thread = self.view._thread
        # Link markup survived into a label (clickable via activate-link).
        link_labels = [w for w in _walk(thread)
                       if isinstance(w, Gtk.Label)
                       and 'href="https://example.com/doc"' in (w.get_label() or "")]
        self.assertTrue(link_labels)
        # Reply + forward quotes render their accent-bar boxes.
        self.assertTrue([w for w in _walk(thread)
                         if "cloudy-reply-quote" in w.get_css_classes()])
        # Inline image fetched (auth-gated URL) and decoded into a Picture.
        self.assertIn("https://graph/img1", self.view._image_cache)
        self.assertTrue([w for w in _walk(thread)
                         if isinstance(w, Gtk.Picture)])
        # Reactions render as pills.
        pills = [w.get_text() for w in _walk(thread)
                 if isinstance(w, Gtk.Label)
                 and "cloudy-reaction" in w.get_css_classes()]
        self.assertIn("👍 2", pills)
        # System event + tombstone rows exist.
        texts = [w.get_text() for w in _walk(thread)
                 if isinstance(w, Gtk.Label)]
        self.assertTrue(any("added Cleo" in t for t in texts))
        self.assertTrue(any("deleted this message" in t for t in texts))

    def test_link_click_opens_uri(self):
        self.view._on_link_activated(None, "https://example.com/doc")
        self.assertIn("https://example.com/doc", self.app.opened_uris)

    def test_send_adopted_by_id_no_duplicate(self):
        self._seed_thread()
        _pump()
        self.view.open_chat("c1")
        _pump()
        self.view._entry.set_text("hello sweep")
        self.view._on_send()
        _pump(300)  # worker -> _on_message_sent pins confirmed_id

        opt = self.view._optimistic
        self.assertIsNotNone(opt)
        self.assertEqual(opt.get("chat_id"), "c1")
        self.assertIsNotNone(opt.get("confirmed_id"))
        # The reconcile poll delivers the server copy: the echo is adopted
        # (cleared) instead of duplicated next to the server bubble.
        messages, token = self.client.list_chat_messages_page("c1")
        self.view._on_poll("c1", (messages, token), None)
        self.assertIsNone(self.view._optimistic)
        mid = opt["confirmed_id"]
        self.assertIn(mid, self.view._bubble_widgets)
        widget = self.view._bubble_widgets[mid]
        icon = getattr(widget, "_status_icon", None)
        self.assertIsNotNone(icon)
        self.assertEqual(icon.get_icon_name(), "object-select-symbolic")
        self.assertTrue(icon.get_visible())

    def test_failed_send_stays_in_its_own_chat(self):
        self.client.chats_page1 = [_chat("c1", "Ann"), _chat("c2", "Project")]
        self.client.pages["c1"] = [_msg("m1", "x")]
        self.client.pages["c2"] = [_msg("n1", "y")]
        self._reload_chats()
        self.view.open_chat("c1")
        _pump()
        self.client.fail_send = True
        self.view._entry.set_text("doomed")
        self.view._on_send()
        _pump(300)
        # Switch away before the failure callback lands.
        self.view.open_chat("c2")
        _pump(300)
        self.assertTrue(self.view._failed_bubbles)
        chat_ids = {cid for cid, _w in self.view._failed_bubbles}
        self.assertEqual(chat_ids, {"c1"})  # recorded for the SEND chat
        # A full render of chat B must not resurrect chat A's retry bubble.
        self.view._full_render(list(self.client.pages["c2"]))
        for _cid, w in self.view._failed_bubbles:
            self.assertIsNot(w.get_parent(), self.view._thread)

    def test_composer_cleared_on_switch(self):
        self.client.chats_page1 = [_chat("c1", "Ann"), _chat("c2", "Project")]
        self._reload_chats()
        self.view.open_chat("c1")
        _pump()
        self.view._entry.set_text("draft for c1")
        self.view._stage_bytes(_png(), "image/png", "shot.png")
        self.assertEqual(len(self.view._pending), 1)
        self.view.open_chat("c2")
        self.assertEqual(self.view._entry.get_text(), "")
        self.assertEqual(self.view._pending, [])
        self.assertFalse(self.view._preview.get_visible())

    def test_load_older_prepends_in_order_and_dedups(self):
        self.client.chats_page1 = [_chat("c1", "Ann")]
        self.client.pages["c1"] = [_msg("m2", "b"), _msg("m3", "c")]
        # An older page as list_chat_messages_page returns it: oldest-first,
        # with m2 overlapping the newest page's seam (Graph skiptokens do).
        self.client.older["c1"] = ([_msg("m0", "oldest"), _msg("m1", "a"),
                                    _msg("m2", "b")], None)
        self._reload_chats()
        self.view.open_chat("c1")
        _pump()
        self.view._load_older()
        _pump()
        self.assertEqual(self._thread_ids(), ["m0", "m1", "m2", "m3"])
        self.assertIsNone(self.view._msg_next_token)
        self.assertIsNone(self.view._older_row)

    def test_ack_only_when_mapped_and_focused(self):
        self.client.chats_page1 = [_chat("c1", "Ann")]
        self.client.pages["c1"] = [_msg("m1", "x")]
        self._reload_chats()
        self.view.open_chat("c1")
        _pump()
        self.client.marked_read.clear()
        # Headless: the tab is NOT mapped — a poll delivering a new message
        # must not mark it read on the server. (m2's timestamp is past the
        # watermark open_chat recorded from the chat's last_at.)
        newer = [_msg("m1", "x", sent="2026-08-20T10:00:00Z"),
                 _msg("m2", "new arrival", sent="2026-08-28T09:00:00Z")]
        self.view._on_poll("c1", (newer, None), None)
        self.assertEqual(self.client.marked_read, [])
        # A repeat poll with identical content is a signature no-op — the ack
        # must instead happen when the tab becomes visible again (map).
        self.view._on_poll("c1", (newer, None), None)
        self.assertEqual(self.client.marked_read, [])
        # Tab shown: the map handler acks the backlog. get_root() is pointed
        # at the plain FakeWindow (no is_active attr → treated as focused).
        with unittest.mock.patch.object(self.view, "get_mapped",
                                        return_value=True), \
             unittest.mock.patch.object(self.view, "get_root",
                                        return_value=self.window):
            self.view._on_mapped()
        self.assertEqual(self.client.marked_read, ["c1"])

    def test_delete_unhides_tombstone_once_server_confirms(self):
        self.client.chats_page1 = [_chat("c1", "Ann")]
        self.client.pages["c1"] = [_msg("m1", "keep"), _msg("m2", "gone")]
        self._reload_chats()
        self.view.open_chat("c1")
        _pump()
        self.view._delete_msg({"id": "m2", "text": "gone", "is_mine": True})
        self.assertIn("m2", self.view._deleted_ids)
        self.assertNotIn("m2", self.view._bubble_widgets)
        # Server now returns it as a tombstone: un-hide so it renders.
        tomb = _msg("m2", "", deleted=True)
        self.view._on_poll("c1", ([_msg("m1", "keep"), tomb], None), None)
        self.assertNotIn("m2", self.view._deleted_ids)
        self.assertIn("m2", self.view._bubble_widgets)

    def test_search_results_keyed_and_cleaned_up(self):
        self.client.chats_page1 = [_chat("c1", "Ann Peak")]
        self.client.hits = [{"chat_id": "c1", "message_id": "m9",
                             "from": "Ann", "snippet": "needle here",
                             "sent": "2026-08-20T10:00:00Z"}]
        self._reload_chats()
        self.view._search.set_text("needle")
        _pump(700)  # debounce (350ms) + fetch
        keys = [getattr(r, "_patch_key", None)
                for r in _walk(self.view._list)
                if isinstance(r, Gtk.ListBoxRow)]
        self.assertIn("hit:m9", keys)
        # Leaving search restores the plain chat list without leftovers.
        self.view._search.set_text("")
        _pump()
        keys = [getattr(r, "_patch_key", None)
                for r in _walk(self.view._list)
                if isinstance(r, Gtk.ListBoxRow)]
        self.assertNotIn("hit:m9", keys)
        self.assertIn("c1", keys)

    def test_scroll_state_derivation(self):
        adj = self.view._thread_scroll.get_vadjustment()
        adj.configure(0.0, 0.0, 1000.0, 10.0, 100.0, 200.0)
        # Raw set_value == a user scroll (value-changed fires; the view's own
        # programmatic _set_scroll is deliberately ignored by the handler).
        adj.set_value(800.0)          # near the bottom → pinned, button hidden
        self.assertTrue(self.view._autoscroll)
        self.assertFalse(self.view._to_bottom_btn.get_visible())
        adj.set_value(300.0)          # scrolled up → unpinned, anchor, button
        self.assertFalse(self.view._autoscroll)
        self.assertAlmostEqual(self.view._anchor_bottom, 700.0)
        self.assertTrue(self.view._to_bottom_btn.get_visible())
        # "Go to latest" re-pins.
        self.view._scroll_to_bottom()
        self.assertTrue(self.view._autoscroll)
        self.assertFalse(self.view._to_bottom_btn.get_visible())


@_skip
class TestChatRenderBenchmarks(_ChatSweepBase):
    """Main-thread render ceilings — the headless stand-in for 'does it
    freeze'. Times are printed for the report; the asserts are generous
    regression guards, not performance claims."""

    def test_render_timings(self):
        self.view._chat_id = "bench"
        msgs = [_msg(f"b{i}", f"message number {i}",
                     reactions=[{"emoji": "👍", "count": 1}])
                for i in range(300)]

        t0 = time.perf_counter()
        self.view._render_thread("bench", msgs)  # first render = full build
        full = time.perf_counter() - t0
        self.assertEqual(len(self.view._rendered_sigs), 300)

        # One new message: the fast append path must be far cheaper.
        t0 = time.perf_counter()
        self.view._render_thread("bench", msgs + [_msg("b300", "new")])
        append = time.perf_counter() - t0
        self.assertEqual(len(self.view._rendered_sigs), 301)

        # Unchanged poll: signature check only, no rebuild.
        t0 = time.perf_counter()
        self.view._render_thread("bench", msgs + [_msg("b300", "new")])
        unchanged = time.perf_counter() - t0

        self.client.chats_page1 = [
            _chat(f"g{i}", f"Conversation {i}") for i in range(150)]
        t0 = time.perf_counter()
        self.view._render_chats(self.client.chats_page1)
        listrender = time.perf_counter() - t0

        print(f"\n[chat-bench] 300-msg full render: {full*1000:.1f} ms, "
              f"append-1: {append*1000:.1f} ms, unchanged poll: "
              f"{unchanged*1000:.2f} ms, 150-chat list: "
              f"{listrender*1000:.1f} ms")
        self.assertLess(full, 3.0)
        self.assertLess(append, 1.0)
        self.assertLess(unchanged, 0.5)
        self.assertLess(listrender, 2.0)


if __name__ == "__main__":
    unittest.main()
