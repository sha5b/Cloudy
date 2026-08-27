# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Shahab Nedaei
"""Teams-tab logic tests: channel-message shaping (graph_teams), the
fetch_bytes scope override (graph_http), OneNote title patching, the
TeamsView poll fingerprint / older-post merge / failed-send restore, and the
OneNote page editor living in its own non-modal EditorWindow."""

import time
import unittest
import unittest.mock
import urllib.request

import gi_setup  # pins GI versions; exposes AVAILABLE

# graph.py pulls in core.auth.msal_graph -> `import msal`, an app runtime dep
# that isn't present in a minimal RPM build chroot (same skip pattern as
# test_graph.py).
try:
    from cloudy.modules.microsoft365.graph import GraphClient
    from cloudy.core.auth.msal_graph import SCOPES_CHANNELS, SCOPES_CHAT
    _GRAPH_OK = True
except ImportError:
    _GRAPH_OK = False

_skip_graph = unittest.skipUnless(_GRAPH_OK,
                                  "msal not installed (graph import unavailable)")

# teams_view needs GI typelibs as well.
_teams_view = None
if gi_setup.AVAILABLE:
    try:
        from cloudy.widgets.teams_view import TeamsView
        _teams_view = TeamsView
    except ImportError:
        _teams_view = None

_skip_view = unittest.skipUnless(_teams_view is not None,
                                 "GTK/Adw typelibs unavailable (headless build)")


def _graph_client():
    # A bare instance (no __init__ network/auth setup): the methods under
    # test only need _get/_patch/_me_id/_token_provider, all mocked.
    client = GraphClient.__new__(GraphClient)
    client._me_id = unittest.mock.Mock(return_value="u2")
    return client


def _channel_payload():
    """One page of channel messages as Graph returns it: newest-first, with a
    soft-deleted post, a normal post (one live + one deleted reply), a system
    event, and an unknown type that must stay hidden."""
    return {
        "value": [
            {"id": "m-del", "messageType": "message",
             "deletedDateTime": "2026-08-04T00:00:00Z",
             "from": {"user": {"id": "u2", "displayName": "Bob"}},
             "createdDateTime": "2026-08-04T00:00:00Z"},
            {"id": "m-ok", "messageType": "message",
             "from": {"user": {"id": "u2", "displayName": "Bob"}},
             "body": {"contentType": "text", "content": "hello"},
             "createdDateTime": "2026-08-03T00:00:00Z",
             "replies": [
                 {"id": "r-live", "messageType": "message",
                  "from": {"user": {"id": "u1", "displayName": "Ann"}},
                  "body": {"contentType": "text", "content": "hi"},
                  "createdDateTime": "2026-08-03T01:00:00Z"},
                 {"id": "r-del", "messageType": "message",
                  "deletedDateTime": "2026-08-03T02:00:00Z",
                  "from": {"user": {"id": "u1", "displayName": "Ann"}},
                  "createdDateTime": "2026-08-03T02:00:00Z"},
             ]},
            {"id": "m-sys", "messageType": "systemEventMessage",
             "createdDateTime": "2026-08-02T00:00:00Z",
             "eventDetail": {
                 "@odata.type":
                     "#microsoft.graph.membersAddedEventMessageDetail",
                 "initiator": {"user": {"id": "u1", "displayName": "Ann"}},
                 "members": [{"id": "u3", "displayName": "Cleo"}]}},
            {"id": "m-unk", "messageType": "typingEventMessage",
             "createdDateTime": "2026-08-01T00:00:00Z"},
        ],
        "@odata.nextLink": "https://graph.microsoft.com/v1.0/next",
    }


@_skip_graph
class TestChannelMessagesPage(unittest.TestCase):
    def _rows(self, page_token=None):
        client = _graph_client()
        with unittest.mock.patch.object(
                client, "_get", return_value=_channel_payload()) as get:
            rows, next_token = client.list_channel_messages_page(
                "t1", "c1", page_token=page_token)
        return get, rows, next_token

    def test_oldest_last_and_next_token_passthrough(self):
        get, rows, next_token = self._rows()
        self.assertEqual([r["id"] for r in rows],
                         ["m-sys", "m-ok", "m-del"])  # oldest-last
        self.assertEqual(next_token,
                         "https://graph.microsoft.com/v1.0/next")
        get.assert_called_once()

    def test_system_event_passes_through_as_status_row(self):
        _get, rows, _next = self._rows()
        sys_row = rows[0]
        self.assertTrue(sys_row["system"])
        self.assertEqual(sys_row["text"], "Ann added Cleo")
        self.assertEqual(sys_row["replies"], [])

    def test_soft_deleted_root_keeps_tombstone(self):
        _get, rows, _next = self._rows()
        tomb = rows[2]
        self.assertTrue(tomb["deleted"])
        self.assertEqual(tomb["from"], "Bob")
        self.assertTrue(tomb["is_mine"])  # me == u2
        self.assertEqual(tomb["text"], "")

    def test_replies_keep_live_and_deleted_shapes(self):
        _get, rows, _next = self._rows()
        replies = rows[1]["replies"]
        self.assertEqual([r["id"] for r in replies], ["r-live", "r-del"])
        self.assertEqual(replies[0]["text"], "hi")
        self.assertFalse(replies[0].get("deleted", False))
        self.assertTrue(replies[1]["deleted"])

    def test_unknown_message_types_stay_hidden(self):
        _get, rows, _next = self._rows()
        self.assertNotIn("m-unk", [r["id"] for r in rows])

    def test_page_token_used_as_the_url(self):
        client = _graph_client()
        with unittest.mock.patch.object(client, "_get") as get:
            get.return_value = {"value": []}
            client.list_channel_messages_page(
                "t1", "c1", page_token="https://graph/next")
        get.assert_called_once_with("https://graph/next", SCOPES_CHANNELS)


@_skip_graph
class TestUpdateNotePageTitle(unittest.TestCase):
    def _commands(self, **kwargs):
        client = _graph_client()
        with unittest.mock.patch.object(client, "_patch") as patch:
            client.update_note_page("t1", "p1", "<p>body</p>", **kwargs)
        return patch.call_args.args

    def test_body_only_without_title(self):
        url, commands, scopes = self._commands()
        self.assertEqual(len(commands), 1)
        self.assertEqual(commands[0], {"target": "body", "action": "replace",
                                       "content": "<p>body</p>"})

    def test_unchanged_title_not_patched(self):
        _url, commands, _scopes = self._commands(title="Same",
                                                 original_title="Same")
        self.assertEqual(len(commands), 1)

    def test_changed_title_appends_title_command(self):
        _url, commands, _scopes = self._commands(title="New",
                                                 original_title="Old")
        self.assertEqual(commands[1], {"target": "title", "action": "replace",
                                       "content": "New"})


@_skip_graph
class TestFetchBytesScopes(unittest.TestCase):
    class _Resp:
        def read(self):
            return b"data"

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def _fetch(self, url, scopes=None):
        client = GraphClient.__new__(GraphClient)
        provider = unittest.mock.Mock(return_value="tok")
        client._token_provider = provider
        opener = unittest.mock.Mock()
        opener.open.return_value = self._Resp()
        with unittest.mock.patch.object(urllib.request, "build_opener",
                                        return_value=opener):
            data = client.fetch_bytes(url, scopes) if scopes is not None \
                else client.fetch_bytes(url)
        return provider, opener, data

    def test_default_uses_chat_scopes(self):
        provider, _opener, data = self._fetch(
            "https://graph.microsoft.com/v1.0/me/x/$value")
        self.assertEqual(data, b"data")
        provider.assert_called_once_with(SCOPES_CHAT)

    def test_scopes_override_reaches_the_token_provider(self):
        provider, _opener, _data = self._fetch(
            "https://graph.microsoft.com/v1.0/teams/t/channels/c/messages"
            "/m/hostedContents/h/$value", scopes=SCOPES_CHANNELS)
        provider.assert_called_once_with(SCOPES_CHANNELS)

    def test_non_graph_host_gets_no_token(self):
        provider, _opener, _data = self._fetch("https://cdn.example.com/x")
        provider.assert_not_called()


# -- TeamsView pure-ish logic (GTK types only, no display) -----------------
def _post(pid, text="hi", replies=(), atts=0, reactions=(), **extra):
    return {
        "id": pid, "sent": "t1", "text": text,
        "attachments": [{"name": f"a{i}"} for i in range(atts)],
        "reactions": [{"emoji": e, "count": c} for e, c in reactions],
        "replies": [dict(r) for r in replies],
        **extra,
    }


def _reply(rid, text="re", atts=0, reactions=()):
    return {"id": rid, "sent": "t2", "text": text,
            "attachments": [{"name": f"a{i}"} for i in range(atts)],
            "reactions": [{"emoji": e, "count": c} for e, c in reactions]}


@_skip_view
class TestPostsSignature(unittest.TestCase):
    def test_identical_posts_match(self):
        posts = [_post("p1", replies=[_reply("r1")])]
        self.assertEqual(TeamsView._posts_signature(posts),
                         TeamsView._posts_signature(
                             [_post("p1", replies=[_reply("r1")])]))

    def test_root_text_edit_detected(self):
        base = [_post("p1", text="before")]
        self.assertNotEqual(TeamsView._posts_signature(base),
                            TeamsView._posts_signature(
                                [_post("p1", text="after")]))

    def test_attachment_added_detected(self):
        base = [_post("p1", atts=0)]
        self.assertNotEqual(TeamsView._posts_signature(base),
                            TeamsView._posts_signature(
                                [_post("p1", atts=1)]))

    def test_reply_reaction_change_detected(self):
        base = [_post("p1", replies=[_reply("r1", reactions=[("👍", 1)])])]
        changed = [_post("p1", replies=[_reply("r1", reactions=[("👍", 2)])])]
        self.assertNotEqual(TeamsView._posts_signature(base),
                            TeamsView._posts_signature(changed))

    def test_reply_text_edit_detected(self):
        base = [_post("p1", replies=[_reply("r1", text="a")])]
        changed = [_post("p1", replies=[_reply("r1", text="b")])]
        self.assertNotEqual(TeamsView._posts_signature(base),
                            TeamsView._posts_signature(changed))

    def test_tombstone_transition_detected(self):
        base = [_post("p1")]
        deleted = [_post("p1", deleted=True)]
        self.assertNotEqual(TeamsView._posts_signature(base),
                            TeamsView._posts_signature(deleted))


@_skip_view
class TestMergePosts(unittest.TestCase):
    def test_no_cache_returns_page(self):
        page = [_post("p1"), _post("p2")]
        self.assertEqual(TeamsView._merge_posts([], page), page)

    def test_older_cached_pages_survive_a_new_page(self):
        cached = [_post("old1", sent="t1"), _post("old2", sent="t2"),
                  _post("p1", sent="t5")]
        page = [_post("p1", text="edited", sent="t5")]
        merged = TeamsView._merge_posts(cached, page)
        self.assertEqual([p["id"] for p in merged], ["old1", "old2", "p1"])
        self.assertEqual(merged[-1]["text"], "edited")

    def test_newer_cached_post_missing_from_page_dropped(self):
        # A cached message inside (newer than) the fetched window that the page
        # no longer returns was deleted server-side — drop it.
        cached = [_post("old", sent="t1"), _post("gone", sent="t7")]
        page = [_post("win", sent="t5")]
        merged = TeamsView._merge_posts(cached, page)
        self.assertEqual([p["id"] for p in merged], ["old", "win"])

    def test_same_second_seam_message_kept(self):
        # Two messages sharing the window-start timestamp must not be dropped
        # as "deleted" (the id-set exclusion already prevents duplicates).
        cached = [_post("seam", sent="t5"), _post("win", sent="t5")]
        page = [_post("win", sent="t5")]
        merged = TeamsView._merge_posts(cached, page)
        self.assertEqual([p["id"] for p in merged], ["seam", "win"])


@_skip_view
class TestAfterSendRestore(unittest.TestCase):
    """A failed post/reply must put the typed text back, not just toast."""

    class _Window:
        def __init__(self):
            self.toasts = []

        def add_toast(self, msg):
            self.toasts.append(msg)

    def _view(self, channel_id="c1"):
        from gi.repository import Gtk

        Gtk.init_check()
        view = TeamsView.__new__(TeamsView)  # skip __init__ (no app/window)
        view._window = self._Window()
        view._account = unittest.mock.Mock()
        view._account.id = "acct1"
        view._channel_id = channel_id
        view._reply_entries = {}
        return view

    def _entry(self):
        from gi.repository import Gtk

        return Gtk.Entry()

    def test_failed_post_restores_text(self):
        view = self._view()
        entry = self._entry()
        view._after_send("c1", "boom", entry, "typed text")
        self.assertEqual(entry.get_text(), "typed text")
        self.assertTrue(view._window.toasts)

    def test_failed_reply_restores_text(self):
        view = self._view()
        entry = self._entry()
        view._reply_entries["p1"] = entry  # a poll re-render rebuilt it
        view._after_send("c1", "boom", entry, "reply text")
        self.assertEqual(entry.get_text(), "reply text")

    def test_existing_draft_not_overwritten(self):
        view = self._view()
        entry = self._entry()
        entry.set_text("newer draft")
        view._after_send("c1", "boom", entry, "old text")
        self.assertEqual(entry.get_text(), "newer draft")

    def test_no_restore_when_user_switched_channel(self):
        view = self._view(channel_id="c2")
        entry = self._entry()
        view._after_send("c1", "boom", entry, "other channel text")
        self.assertEqual(entry.get_text(), "")
        self.assertTrue(view._window.toasts)


@_skip_view
class TestTeamsViewSmoke(unittest.TestCase):
    """Headless instantiation smoke test (AGENTS.md: a typo once passed import
    but crashed MonthGrid())."""

    def test_instantiates_with_fake_app(self):
        from gi.repository import Gtk

        from cloudy.core.cache import MemoryCache
        from cloudy.widgets.teams_view import TeamsView as View

        Gtk.init_check()

        class App:
            cache = MemoryCache(ttl=90)

        class Window:
            def get_application(self):
                return App()

            def add_toast(self, _msg):
                pass

        account = unittest.mock.Mock()
        account.id = "a1"
        account.provider = "microsoft"
        view = View(Window(), account)
        self.assertIsNotNone(view)
        # New per-conversation state exists and starts clean.
        self.assertEqual(view._image_cache, {})
        self.assertEqual(view._conv_tokens, {})
        self.assertFalse(view._conv_loading)


@_skip_view
class TestNoteEditorWindow(unittest.TestCase):
    """The OneNote page editor must be its own non-modal EditorWindow — the
    old inline form was swapped into the Notes pane (``_page_content``), so
    any navigation (channel, section, notebook reload) destroyed it and the
    draft with it."""

    PAGE = {"id": "p1", "title": "Agenda", "web_url": ""}

    def _view(self):
        from gi.repository import Gtk

        from cloudy.core.cache import MemoryCache
        from cloudy.widgets.teams_view import TeamsView as View

        Gtk.init_check()

        # A real Gtk.Application (never started): EditorWindow's
        # set_application() type-checks, and the view needs app.cache.
        app = Gtk.Application()
        app.cache = MemoryCache(ttl=90)

        class Window:
            def get_application(self):
                return app

            def add_toast(self, _msg):
                pass

        account = unittest.mock.Mock()
        account.id = "a1"
        account.provider = "microsoft"
        view = View(Window(), account)
        view._team_id = "t1"
        view._section_id = "s1"
        # Root the view in a hidden toplevel: the editor window's post-save
        # liveness guard checks get_root(), and this presents nothing on the
        # developer's desktop.
        root = Gtk.Window()
        root.set_child(view)
        return view

    def _edit(self, view, page=None, html=""):
        from cloudy.widgets.teams_view import NoteEditorWindow

        with unittest.mock.patch.object(NoteEditorWindow, "present"):
            return view._edit_page(page, html)

    def test_edit_opens_non_modal_editor_window_with_current_content(self):
        from gi.repository import Adw

        from cloudy.widgets.editor_window import EditorWindow
        from cloudy.widgets.teams_view import NoteEditorWindow

        view = self._view()
        win = self._edit(view, dict(self.PAGE),
                         "<html><body><p>old body</p></body></html>")
        self.assertIsInstance(win, NoteEditorWindow)
        self.assertIsInstance(win, EditorWindow)
        self.assertFalse(win.get_modal())
        self.assertEqual(win.get_title(), "Agenda")
        self.assertEqual(win._title_entry.get_text(), "Agenda")
        self.assertIn("old body", win._editor.get_plain_text())
        # No inline swap: the Notes pane still shows its reader placeholder.
        self.assertIsInstance(view._page_content.get_child(), Adw.StatusPage)

    def test_mid_edit_navigation_leaves_editor_untouched(self):
        view = self._view()
        win = self._edit(view, dict(self.PAGE), "<p>seed</p>")
        win._title_entry.set_text("Draft title")
        win._editor.set_plain_text("draft words")
        # The exact paths that used to destroy the inline editor by swapping
        # _page_content's child under it.
        view._channel_id = "c1"
        view._section_id = "s2"
        view._load_pages()
        view._notes_loaded_for = ""
        view._ensure_notes_loaded()
        view._load_notebook()
        self.assertEqual(win._title_entry.get_text(), "Draft title")
        self.assertEqual(win._editor.get_plain_text(), "draft words")
        self.assertIs(win._editor.get_root(), win)  # still inside the window
        self.assertTrue(win.primary_btn.get_sensitive())

    def test_save_patches_captured_ids_after_section_switch(self):
        view = self._view()
        win = self._edit(view, dict(self.PAGE), "<p>seed</p>")
        win._title_entry.set_text("Renamed")
        view._section_id = "s2"  # the user navigated away mid-edit
        client = unittest.mock.Mock()
        with unittest.mock.patch(
                "cloudy.widgets.clients.build_account_client",
                return_value=client):
            win.on_primary()
            deadline = time.monotonic() + 5
            while not client.update_note_page.called \
                    and time.monotonic() < deadline:
                time.sleep(0.01)
        client.update_note_page.assert_called_once_with(
            "t1", "p1", unittest.mock.ANY,
            title="Renamed", original_title="Agenda")

    def test_new_page_creates_in_captured_section(self):
        view = self._view()
        win = self._edit(view, None, "")
        self.assertEqual(win.get_title(), "New page")
        view._section_id = "s2"  # navigated away before saving
        client = unittest.mock.Mock()
        with unittest.mock.patch(
                "cloudy.widgets.clients.build_account_client",
                return_value=client):
            win.on_primary()
            deadline = time.monotonic() + 5
            while not client.create_note_page.called \
                    and time.monotonic() < deadline:
                time.sleep(0.01)
        client.create_note_page.assert_called_once_with(
            "t1", "s1", "Untitled page", unittest.mock.ANY)

    def test_failed_save_keeps_window_open_for_retry(self):
        view = self._view()
        win = self._edit(view, dict(self.PAGE), "<p>seed</p>")
        win._on_saved("boom")
        self.assertTrue(win.primary_btn.get_sensitive())
        self.assertIs(win._editor.get_root(), win)  # draft not torn down

    def test_saved_reload_only_for_the_section_still_on_screen(self):
        view = self._view()
        win = self._edit(view, dict(self.PAGE), "<p>seed</p>")
        with unittest.mock.patch.object(view, "_load_pages") as reload:
            win._on_saved(None)
        reload.assert_called_once()
        # Navigated to another section (and team) mid-save: no reload, and no
        # crash — just the notepages cache-prefix drop.
        for team_id, section_id in (("t1", "s9"), ("t9", "s1")):
            view._team_id, view._section_id = team_id, section_id
            with unittest.mock.patch.object(view, "_load_pages") as reload:
                view._on_note_saved("t1", "s1")
            reload.assert_not_called()


if __name__ == "__main__":
    unittest.main()
