<!--
SPDX-License-Identifier: GPL-3.0-or-later
SPDX-FileCopyrightText: 2026 Shahab Nedaei
-->

# Cloudy — Handoff / Continue Here

Pick-up doc for a fresh session. Cloudy is a **GTK4 / Libadwaita (Python /
PyGObject)** super-app for **Microsoft 365 (OneDrive + Teams/SharePoint, Mail,
Calendar)** and **Google (Gmail, Calendar, Drive)** on Fedora 44 (GNOME 50). It
*orchestrates* proven backends (rclone for mounts; Microsoft Graph / Google REST
for mail/calendar) rather than reimplementing them. Read `docs/ARCHITECTURE.md`,
`docs/AUTH.md`, `docs/SECRETS.md`, `docs/ROADMAP.md` for depth.

## ⏭ Continue here — 0.2.3 released + avatar/presence fixes (2026-06-17, latest)

**Where we stopped.** Shipped **v0.2.3** to GitHub (Actions `release.yml` ran on
the `v0.2.3` tag → built RPM + Flatpak with secrets, attached both; release notes
are the 0.2.3 CHANGELOG section). The user's local Flatpak was reinstalled from
the bundle, so the running app == the release. Commit `fab15e5`, pushed to
`main`. **Done and verified** (build + headless smoke + the user eyeballed flat
colours and working presence dots).

### What was actually wrong (and fixed) in the chat avatars/presence
- **Flat avatars never applied** — the killer detail: `Adw.Avatar`'s coloured
  background lives on an **internal child gizmo** whose CSS node is `avatar` (and
  carries `.color1–.color14`), *not* on the `Adw.Avatar` widget we hold (node
  `widget`). So the old `avatar.cloudy-avatar-flat` selector matched nothing. Fix:
  reach the inner node as a descendant — `.cloudy-avatar-flat.cloudy-avatar-flat
  avatar { … }`. Confirm with a widget-tree walk: `Avatar(widget) → AdwGizmo
  (avatar, .colorN) → Label/Image`.
- **Flat per-person colours** — the user then wanted distinct *flat* colours (not
  the uniform grey, not Adwaita's gloss). `_avatar` adds `cloudy-avatar-c{0..7}`
  by a stable byte-sum hash of the contact name (`_avatar_color_index`); palette
  is `.cloudy-avatar-cN avatar` in `style.css` (solid fills, white initials).
- **Presence dot appeared then vanished** — two presence fetches (`_refresh_
  presence` list batch + `_on_members` per-chat fetch ~1s after open); the second
  often returned `PresenceUnknown`/`""` and `_on_presence` did a blind
  `dict.update`, erasing a freshly-resolved status. Fix: merge **without
  downgrading** a known availability to blank/unknown. The dot is now **CSS-drawn**
  (a sized `Gtk.Box` with `.cloudy-presence`/`-{state}`), not a `media-record-
  symbolic` icon, so it can't go missing in a runtime theme; `Offline`/unknown now
  shows a grey dot so a fetched 1:1 always shows *something*.
- Diagnostic `print("[presence] …")` lines were added during debugging and
  **removed** before release — don't reintroduce them.

### CI
- `.github/workflows/release.yml` bumped `actions/checkout@v4 → v6` and
  `softprops/action-gh-release@v2 → v3` (Node 20 → 24 deprecation). Triggers
  unchanged: push a `v*` tag (or workflow_dispatch with an existing tag).
- **Release recipe:** `gh release create vX.Y.Z --target main --notes-file
  <changelog-section>` creates the tag (→ triggers the build) *and* sets the
  notes. `action-gh-release` preserves an existing body, so set notes at create
  time. Locally, `make release` builds RPM + Flatpak bundle and reinstalls the
  bundle so the running app matches.

## ⏭ Earlier — Chat/Teams/OneNote rework (2026-06-17)

**Where we stopped.** Reworked the Chat surface and hardened Teams/OneNote per a
bug-sweep request. All green (`make build` + 5 meson tests) and verified
headlessly (module imports + the new Graph reply-parse helpers). **Not yet
eyeballed — `make run`** and check Chat (avatars, replies, live list, image
send-scroll) and a large OneNote page.

### Chat (`widgets/chat_view.py`, `data/style.css`)
- **Flat avatars.** `_avatar` adds the `cloudy-avatar-flat` style class; CSS
  overrides `Adw.Avatar`'s per-name rainbow (`.color1–.color14`).
  **Superseded — see the top section:** the selector had to target the inner
  gizmo node (`.cloudy-avatar-flat … avatar`), and avatars now use a flat
  *per-person* palette (`cloudy-avatar-c0…c7`), not one accent fill.
- **Live chat list.** New `ChatView.refresh_live()` re-fetches the chat list so a
  new message bumps its conversation to the top (and lights its unread mark)
  without a manual refresh — mirrors Mail. Wired: `notifications._on_chat` →
  `window.refresh_account_chat` → `view.refresh_live()` (only for the shown
  account; skipped while a search is active). The open thread keeps its own 5/30s
  adaptive poll. `_render_filtered` now re-selects the open chat after a live
  re-render.
- **Reply quotes.** Graph now parses Teams `messageReference` attachments into a
  `reply_to` dict (see below) instead of a bare "attachment" chip. `_bubble`
  renders a clickable quote (accent bar + author + snippet) above the body;
  clicking it calls `_scroll_to_message(id)` which scrolls to and flashes the
  original (or toasts "scroll up to load older" if it isn't loaded). Styles:
  `.cloudy-reply-quote` / `.cloudy-reply-bar` / `.cloudy-bubble-flash`.
- **Immediate scroll on send.** Optimistic echo now covers *every* send (was
  plain-text only): `_render_pending(chat_id, text, images)` renders attached
  images straight from memory via `_local_image_widget` (no fetch round-trip), so
  an image/rich send appears and scrolls to the bottom instantly instead of after
  the ~1.5s reconcile poll.
- **Image decode is memory-safe.** `_thumb_texture` (chat) and
  `_texture_from_bytes` (teams) now downscale *during* decode via the loader's
  `size-prepared` signal, so a huge source image (a OneNote scan, a high-res
  paste) is never fully decoded into memory — fixes OOM/renderer crashes on big
  images. Both raise `ValueError` on an undecodable payload (callers already
  catch).

### Graph (`modules/microsoft365/graph.py`)
- `_strip_reply_placeholder` drops the `<attachment id=…>` placeholder Teams
  leaves where the quoted message goes. `_parse_message_reference` turns a
  `messageReference` attachment's JSON (`messageId`/`messagePreview`/sender) into
  `{id, text, from}`. `_split_attachments` pulls the reply quote out of the
  attachment list. Both `_chat_message_row` and `_channel_message_row` now return
  a `reply_to` field and a cleaned body.

### Teams (`widgets/teams_view.py`)
- `_message_block` renders the same reply quote (`_reply_quote`) for channel
  posts/replies, so a quoted channel reply shows its text instead of "attachment".

### Cross-tab bug sweep (same session)
Fixed alongside the chat rework after a per-tab review:
- **Mail:** `message_view.make_message_page` now escapes the NavigationPage title
  (subjects with `&`/`<` no longer blank it); `mail_view._populate_folders` /
  `_on_folder_changed` use `.get("id")` instead of hard subscripts (a folder
  missing `id` no longer `KeyError`s).
- **Calendar:** `_delete_selected` uses `r._ev.get("id")` (was `r._ev["id"]`
  inside the filter — `KeyError` on an id-less event); `_on_groups_loaded` does
  `"Group.Read" in str(error)` (was `in error` → `TypeError` on a non-string
  error); `event_window._on_loaded` escapes the `StatusPage` description.
- **Files:** `file_browser._show_status` escapes `StatusPage` title/description;
  the right-click `Popover` is `unparent()`-ed on `closed` (was leaking one per
  right-click); imported `format.esc`.
- **Command palette:** Down/Tab and Up/Shift+Tab now wrap around (Tab no longer
  dead-ends on the last row).
- **Styling:** added the `.cloudy-bubble-image` rule (rounded corners on inline
  chat/channel/OneNote thumbnails — was referenced but undefined).

### Full audit + cleanup pass (same session)
A four-angle review (performance / lifecycle-leaks / backend-clients / code-quality)
was run. Fixed now (safe, verified, all green):
- **Flat avatars actually work now.** GTK4 CSS has no `!important` (it errors
  "junk at end of value" — that's why the first attempts silently failed). The
  rule is plain longhand at `PRIORITY_APPLICATION`. Also: this is a single-instance
  app, so a still-running instance must be quit for CSS to reload.
- **Dead code removed:** `message_view.make_message_page`, `chat_view._initials` +
  `_QUICK_REACTIONS`, `RichTextEditor.is_empty`, `graph.site_by_path`,
  `send_chat_image`/`unset_reaction` (graph + google_client), `window._account_menu_button`,
  `interfaces.CAPABILITY_KEYS`, `dashboard._pretty_day` (dup), `mounts.authorize_onedrive`/
  `create_onedrive_remote`. (`Account.is_business` was *kept* — it's covered by the
  unit suite.)
- **ChatView teardown was reverted.** A first attempt stopped the poll/presence
  timers on `unrealize`, but `Adw.ViewStack` unrealizes the hidden Chat page on
  tab switch, so it killed presence dots permanently (timer never restarted).
  The timers already self-cancel via their `get_root() is None` checks, so the
  orphan-timer leak is minor and accepted; a targeted teardown (only on real
  account removal) is the proper future fix.
- **Popover leaks:** chat emoji + members popovers and the rich-editor link
  popover now `unparent()` on `closed`.
- **Dashboard:** mail and calendar fetches are now separate try-blocks that log
  the failure, so one provider's scope error no longer silently blanks the whole
  account's overview.

### Biggest remaining wins — NOT done (architectural; need GUI/live-API testing, get sign-off)
1. **Auth/client is rebuilt on every request** (`widgets/clients.py` →
   `core/auth/msal_graph.py`): each `build_account_client` does a synchronous
   libsecret lookup + deserializes the MSAL cache + builds a new
   `PublicClientApplication`, and `_me_id`/`_tenant_id` caches (instance-level)
   are thrown away — so chat polling/presence re-auth-setup every few seconds and
   issue an extra `/me` per op. **Fix:** memoize one auth object per `account.id`
   on the app (MSAL is designed to be reused), invalidate on sign-in/out. Highest
   single perf win; touches auth, so test sign-in after.
2. **Lazy view construction** (`window._show_account`): every account switch
   eagerly builds ALL five tab views and fires ~6 concurrent network loads even
   though one tab is visible. Build the visible/remembered tab eagerly, the rest
   on first `notify::visible-child-name`.
3. **Gmail inbox is a serial N+1** (`google_client.list_messages_page`): one
   blocking GET per message. Parallelize with a ThreadPoolExecutor (pattern
   already in `list_events`) or use the Gmail batch endpoint.
4. **Image decode on the main thread** in chat/teams `done()` callbacks and
   `message_view.html_body_widget` (inline-image shrink+re-encode) — move the
   decode into the worker thread.
5. **Shared-helper dedup** ("AI slop"): one image `texture_from_bytes` (4 copies:
   media_window/rich_editor/teams_view/chat_view), one `_attachment_chip` (2),
   one `_reply_quote` (2). Extract to a shared module.
6. **Live re-render churn:** `chat_view._render_filtered` / `mail_view._render`
   rebuild the whole ListBox on every notifier tick — diff/patch rows instead.
7. **Mail `refresh_live` collapses pagination**; **Files** nav race + unbounded
   FUSE-folder render.

### Known, deliberately NOT fixed here (need live-API verification)
- **Calendar times may display in UTC, not local.** `_time_label` /
  `month_grid._chip` / `event_view._format_when` slice the raw ISO `start`
  (`[:5]`); Graph `calendarView` is fetched without a `Prefer: outlook.timezone`
  header and ignores the per-event `start.timeZone`, so non-UTC users can see a
  shifted wall-clock. Also `_format_when` shows no end-date for multi-day/all-day
  spans. This is a deep, cross-provider (MS + Google) change that must be tested
  against the live API before touching — flagged, not changed.
- **Mail `refresh_live` collapses pagination.** A live refresh re-renders only
  page 1, discarding already-loaded "Load older" pages and resetting scroll.
- **Files `_load`/`_toggle_expand` last-write-wins race** on rapid navigation
  (no nav-token guard); `_scan` over a FUSE mount is unbounded for huge folders.

## ⏭ Earlier — Command palette + offline cache (2026-06-16)

**Where we stopped.** Added a keyboard-first command palette and a persistent
offline cache, then wrapped up the session. All green (`make build` + 4 meson
tests + `make lint`) and verified headlessly (cache persist/stale-reload/skip
round-trip; palette + cache imports). **Not yet eyeballed — `make flatpak-run`**
and press **Ctrl+K**; relaunch offline to confirm mail/agenda still render.

### Command palette (`widgets/command_palette.py`, `application.py`)
- `Adw.Dialog` opened by the `command-palette` app action (accel **Ctrl+K**).
  Lists every signed-in + enabled account's visible capability surfaces
  (Files/Mail/Calendar/Chat/Teams, with the `is_personal` Chat/Teams filter) plus
  app actions (Preferences, Add account). Type to filter (all-words match), ↑/↓
  (and Tab) move, Enter activates the selection, Esc dismisses. Routes through the
  existing `window.open_account_tab` / `app.activate_action`.
- Imports `CAPABILITY_UI` from `window` *inside* `_build_entries` (not at module
  top) so the module stays importable in headless smoke tests (window needs the
  compiled gresource).

### Persistent offline cache (`core/cache.py`, `application.py`)
- `MemoryCache(ttl, path=…)` now persists JSON-serializable entries to
  `~/.cache/cloudy/cache.json` (atomic write, throttled to ~5s, plus a
  `flush()` in `do_shutdown`). Non-serializable values (e.g. Files libraries
  holding `Drive` objects) are skipped — kept in memory only.
- Entries loaded from disk are **backdated past the TTL** so they read as *stale*:
  views show last-known mail/agenda/chat instantly (offline), then revalidate via
  the normal stale-while-revalidate path. No view changes needed.

### Headless logic test suite (`tests/unit/`, wired into `make test`)
- 69 `unittest` tests over the pure/logic layer — **no GUI, no network**: cache
  (TTL/invalidate/persist/stale-reload/skip-non-serializable), Account
  dict-roundtrip + personal/business, pin/mute/scope helpers, GoogleClient
  normalization + multi-calendar aggregation/id-routing, Graph `_split_id`/
  `_message_scope`/event-normalization, notifier gating + quiet-hours wrap +
  digest enqueue/flush/build, mount `_safe_name`/`drive_type_for`/shared-drive
  degradation, `capabilities_of`, `esc`.
- Run via `make test` (meson test 4/5, ~0.3s) or `make test-unit` (fast, no build).
  GUI widgets aren't covered — they need the gresource/display; this is the
  business logic. `tests/unit/gi_setup.py` pins GI versions, `fakes.py` provides
  FakeApp/FakeSettings/FakeRegistry/FakeWindow.

### Deferred by judgment (too big for a wrap-up — clearly scoped follow-ups)
- **Conversation threading** in Mail: the list is a flat `ListBox` entangled with
  pagination, optimistic send-reconcile, unread filter, multi-select and search;
  grouping by Graph `conversationId` / Gmail `threadId` is a few-hundred-line
  rewrite + client shape changes. Do as its own focused pass.
- **Send outbox** (queue/retry sends offline): pairs with the offline cache but is
  a separate, non-trivial surface.
- **Google Calendar RSVP** (`can_respond` still False), free/busy `getSchedule`,
  week/day calendar views, unified cross-account inbox/agenda, Tasks. See the
  ROADMAP "still to do" + the earlier P2 notes below.

## ⏭ Earlier — Google feature parity: multi-calendar + Drive sources (2026-06-16)

**Where we stopped.** Closed the implementable Google-vs-Microsoft gaps. All green
(`make build` + 4 meson tests + `make lint`) and verified headlessly (calendar
aggregation/id-routing with a faked HTTP layer, view/module imports, shared-drive
enumeration degrades to `[]` without a token). **Not yet eyeballed — ask the user
to `make flatpak-run`** with a personal Google account: the agenda should now show
birthdays/holidays/secondary calendars, and Files should list *My Drive* +
*Shared with me*.

### What was investigated first (don't redo)
A `deep-research`-style check confirmed two gaps are **not implementable** and were
deliberately skipped:
- **Google Chat for personal accounts** — the Chat *product* exists for consumer
  Gmail, but the Chat *REST API* requires a Business/Enterprise Workspace account
  (docs: "A Business or Enterprise Google Workspace account with access to Google
  Chat"). So hiding the Chat tab for personal Google accounts
  (`window.py` `is_personal` filter) is correct. Nothing to build.
- **Delegated/shared Gmail mailboxes** — Gmail has no end-user delegated-mailbox
  REST access (needs domain-wide delegation via a service account + admin),
  impossible for a desktop OAuth app. Skipped.
- **Google Tasks** — deferred: it's net-*new* (Microsoft To Do isn't in the app
  either), not parity. A candidate for a future shared Tasks capability.

### Google multi-calendar (`modules/gmail/google_client.py`, `widgets/calendar_view.py`)
- `list_events` now **aggregates every shown calendar** (calendarList where
  `selected` or `primary`) in parallel (`ThreadPoolExecutor`, ≤8), not just
  `primary` — matching what the user sees in Google Calendar. One bad calendar
  returns `[]` and never sinks the agenda.
- **Id routing** mirrors Graph's `group:`/`shared:` trick: non-primary event ids
  are wrapped `gcal\x1f<calendarId>\x1f<eventId>` (`_wrap_event_id` /
  `_unwrap_event_id`); `get_event`/`update_event`/`delete_event` parse it and hit
  `/calendars/<calId>/events/<id>` (calId URL-encoded via `_cal_path`). Read-only
  calendars (holidays/birthdays) return a Google 403 → surfaced as a toast.
- Each event carries `calendar` (name) + `color`; the agenda row shows the
  calendar name as its subtitle when there's no location. Create still targets
  `primary` (the New-event "me" context). RSVP still off for Google
  (`can_respond=False`) — a separate follow-up.

### Google Drive sources (`widgets/files_view.py`, `modules/microsoft365/mounts.py`)
- Files now lists **My Drive** + **Shared with me** (always) and **Shared Drives**
  (Workspace Team Drives) for Google, like OneDrive + Team libraries on the MS
  side. `_load_google_libraries` renders the first two instantly, then enumerates
  Shared Drives off-thread.
- The app holds **no Google Drive OAuth scope** (Drive is mounted entirely via
  rclone's own auth), so Shared Drives are enumerated through rclone:
  `MountManager.list_google_shared_drives(token)` spins up a throwaway `drive`
  remote, runs `rclone backend drives`, parses JSON, drops the remote — best-effort,
  `[]` on any failure or missing token. So Shared Drives only appear **after** the
  user has mounted something once (an rclone token then exists).
- Mount opts branch on `drive.kind`: `google_shared_with_me` adds
  `shared_with_me=true`; `google_shared_drive` adds `team_drive=<id>`.
- **UNTESTED on a real Workspace account** (the user only has personal Google).
  Shared-with-me is testable on personal; Shared Drives + the rclone `backend
  drives` JSON shape need a Workspace account to confirm.

## ⏭ Earlier — P2 #1 notification batching/digest (2026-06-16)

**Where we stopped.** Implemented the first P2 item: batched (digest) notifications.
Also (earlier this session) split notification settings onto their own
**Notifications** Preferences tab and refined several setting subtitles. All green
(`make build` + `make install` + 4 meson tests + `make lint`) and verified
headlessly (digest enqueue/flush/focus-hold + plural wording, 3-tab Preferences
against the installed schema). **Not yet eyeballed in the GUI — ask the user to
`make flatpak-run`** and try the *Direct now, routine in a summary* level.

### Digest batching (`core/notifications.py`, `preferences.py`, schema, `application.py`)
- **New `notify-level` value `digest`** (now `all` | `digest` | `priority`).
  `all` = everything immediate; `digest` = tier-1 immediate + tier-2 batched;
  `priority` = tier-1 only, tier-2 silent-to-badge. `_allowed(tier)` now holds
  tier-2 at *any* level other than `all`; `_digest_active()` is true only at
  `digest`.
- **Per-account pending buffer** `self._digest` (account id → `{name, chats:{id:name},
  msgs, mail}`). `_on_chat`/`_on_mail` enqueue tier-2 items (`_enqueue_chat`/
  `_enqueue_mail`) instead of firing. Mail counts **all** routine mail for the
  digest but still caps live tier-1 banners at `_MAX_MAIL_PER_TICK`.
- **`_flush_digest` on a `_DIGEST_SECONDS` (600s) timer.** Builds one LOW-priority
  summary per account ("3 new messages in 2 chats · 2 new emails", `ngettext`
  plurals) and clears the buffer. **Holds the queue while `_focus_active()`** (DND
  / quiet hours) and releases once focus clears — nothing dropped.
- **Digest routing.** Summary banners carry an empty id (`"{account_id}\x1f"`);
  `application.py` `_on_notify_open_chat`/`_on_notify_open_mail` now fall back to
  `open_account_tab(account, "chat"|"mail")` when the id is empty.

### Preferences split (`preferences.py`)
- Notification settings moved from the General page to a new **Notifications** tab
  (`_notifications_page`): an **Alerts** group (enable / relevance / respect-DND)
  and a dedicated **Quiet hours** group. General keeps a slimmed **Background**
  group (run-in-background + EDS calendar). Several subtitles clarified.

## ⏭ Earlier — Attention/notifications (P1) + chat status polish (2026-06-15)

**Where we stopped.** Finished a research-driven notifications pass ("P1") and a
round of chat-bubble polish. Everything below is built into `_install`, all
green (`make build` + `make install` + 4 meson tests + `make lint`), and
verified headlessly (notifier gating logic, mute persistence + `from_dict`
round-trip, view construction, Preferences against the installed schema). **Not
yet eyeballed in the GUI — ask the user to `make run`** and try DND / quiet
hours / mute / sending a message.

**Backstory:** a `deep-research` run on CSCW/HCI collaboration (Dourish &
Bellotti 1992; Fogarty 2004; Dabbish & Kraut 2003; Mark 2008; Iqbal & Bailey
2007/08) produced a refinement backlog. The through-line: *abstract beats full
beats none*, *presence ≠ availability* (only changing **delivery** reduces
interruptions), and interruptions cost wellbeing fast — so gate banners, don't
add more signals. P1 below implements the delivery-gating half.

### P1 notifications (`core/notifications.py`, `preferences.py`, schema)
- **System DND + quiet hours.** `NotificationManager._focus_active()` = system
  DND **or** quiet hours. System DND reads GNOME's
  `org.gnome.desktop.notifications` `show-banners` (False = DND on) via a
  schema-guarded, cached `_gnome_notif_settings()` that degrades to "not in DND"
  if the schema is absent (e.g. minimal Flatpak runtime). Quiet hours = a nightly
  HH:MM window that wraps midnight (lexical string compare on zero-padded times).
- **Relevance tiers.** Each item is tier-1 (1:1 chat / important mail / calendar
  reminder → `HIGH`) or tier-2 (group chat / ordinary mail → `NORMAL`).
  `_allowed(tier)` gates the **banner only** (badges/unread always update, so
  nothing is lost). `notify-level` = `all` | `priority`; priority suppresses
  tier-2 banners.
- **Per-chat/-channel mute.** `Account.muted_sources` (added to the `from_dict`
  allowlist — required or new fields silently drop) + `is_muted`/`toggle_mute`
  in `source_nav.py`. Bell toggle in the Chat header (`chat_view._mute_btn`) and
  Teams channel header (`teams_view._mute_btn`); muted ⇒ no banner **and** no
  badge. Notifier reads `account.muted_sources` directly (no widgets import).
- **New GSettings keys** (require `make build` to reinstall the schema):
  `notify-level`, `notify-respect-system-dnd`, `quiet-hours-enabled`,
  `quiet-hours-start`, `quiet-hours-end`. Preferences → *Notifications &
  background* exposes all of them (time entries validate to zero-padded HH:MM
  and never persist a half-typed value).

### Chat bubble polish (`widgets/chat_view.py`)
- **Fluid send (no rebuild, no image reload).** The optimistic echo is remembered
  (`self._optimistic = {widget, text}`); when the server confirms, that exact
  widget is **adopted** (kept, registered under the real id) instead of
  rebuilding the thread — so images never reload and the view never jumps.
  Full rebuild now only for genuine structural changes / rare races. (The earlier
  per-URL `_image_cache` is still there as a second line of defence.)
- **Single delivery indicator.** `_apply_status()` shows ONE glyph on my
  most-recent message only (Teams-style — not a check per line): clock while
  sending → check once sent. **No "Seen"/eye** — the Graph chat API exposes no
  read receipt for the other party (only Teams' private service has it), so we
  deliberately don't fake it. User chose "check only" over an inferred eye.
- **Reactions are pills below the bubble** (a vertical `col` wraps bubble +
  reactions), with horizontal insets so indicators aren't jammed to the edge.

### What's next (P2 from the same backlog)
1. ✅ **Notification batching/digest** — done 2026-06-16 (see the top section).
2. **Meeting auto-focus**: derive an in-meeting state from the user's own
   calendar and auto-enable focus (suppress tier-2) — coarse only, never expose
   meeting titles (Fogarty calendar-busy). Detection misfires silently swallow
   notifications, so gate behind a setting and keep items recoverable via badges.
3. **Dashboard catch-up** ("since you were away"): unread markers + per-channel
   unread counts. NOTE: cheap per-channel unread isn't available from Graph — the
   chats half is feasible, channels need a workaround. (Deliberately dropped #6
   "appear offline / invisible" per the user.)
   Caveat the research itself flagged: no direct evidence was found on
   threading-vs-linear or grounding/search — treat those as a separate pass.

## ⏭ Earlier — Dashboard Activity + chat/notes fixes (2026-06-15)

- **Dashboard "Activity" feed** (`widgets/dashboard_view.py`): a new section
  (shown when there's a work/school MS account) with two groups — **Team
  channels** (latest post from each *starred channel*) and **Chats** (recent
  conversations from one cheap `list_chats_page` call, starred chats floated to
  the top). A Today preview column + a "New chats" stat card interleave the
  newest items. Channel rows route to the Teams tab; chat rows to `open_chat`.
  Aggregation runs inside the existing off-thread `work()`; `_channel_activity`
  fetches the latest channel post. The **Pinned** section now only collects
  mail/calendar pins (`_is_mailcal_pin`) — channel/chat pins go to Activity.
- **Star channels & chats**: `TeamsView` (content header ★, pins the open
  channel) and `ChatView` (header ★, pins the open chat) call `toggle_pin`,
  which now takes `**extra` so a channel pin carries `team_id`/`team_name`. Pin
  kinds are `"channel"` / `"chat"` (`source="teams"`); see `source_nav.py`.
- **Chat images no longer reload on every send/receive**: `ChatView` now caches
  decoded thumbnails by URL (`_image_cache`, reset on chat switch). A full
  rebuild (reconciling an optimistic send) reuses them instantly via
  `_picture_for` instead of re-fetching every picture. Fast-path append was
  already in place; the cache covers the rebuild case.
- **OneNote crash hardening + full width**: `_render_note_body` dropped the
  `Adw.Clamp` (content is now full-width with margins) and splits any text block
  over `_MAX_LABEL_CHARS` (12k) across multiple labels, so a single very long
  paragraph can't grow one label past the GL texture ceiling and re-trigger the
  `gsk_gpu_upload_cairo_op` segfault that killed the WebView path.
- **Gmail folder dropdown** spanned only its natural width because it was the
  `Adw.HeaderBar` title widget (centred/capped). It's now in its own full-width
  bar below the header, mirroring the Microsoft layout (`mail_view.py`).

Verified: `make build` + 4 meson tests + `make lint`; headless instantiate of
Dashboard/Teams/Chat/Mail (MS + Gmail) and render of the Activity section with
fake data. GUI not yet eyeballed — ask the user to `make run`.

## ⏭ Earlier — Teams tab: channels + OneNote (2026-06-15, v0.2.1)

A new top-level **Teams** capability/tab, distinct from the flat **Chat** tab.
Microsoft work/school only (`gmail` does **not** declare `TeamsCapability`, so
Google accounts get no Teams tab; their Chat *spaces* stay under Chat). Shipped
and built into the install tree.

- **Capability wiring** mirrors the others: `TeamsCapability` in
  `core/interfaces.py` (+ `"teams"` in `CAPABILITY_KEYS` and `capabilities_of`),
  implemented by `Microsoft365Module`; `"teams"` in `window.py` `CAPABILITY_UI`
  and gated out for personal accounts alongside `"chat"`; view built in
  `_capability_placeholder`.
- **New scopes** (`core/auth/msal_graph.py`, requested at sign-in in
  `window.py`): `SCOPES_CHANNELS` (`Channel.ReadBasic.All`,
  `ChannelMessage.Read.All`, `ChannelMessage.Send` — **tenant-admin consent**)
  and `SCOPES_NOTES` (`Notes.ReadWrite.All`, `Notes.Create` — no admin consent).
  Adding scopes forces existing MS accounts to **Sign Out → Sign In** once; the
  view shows the shared "Re-sign in" prompt on a scope error.
- **Graph client** (`modules/microsoft365/graph.py`): `list_joined_teams`
  (lightweight id+name; **not** the file-mount `list_teams` which returns
  `Drive`s), `list_team_channels`, `list_channel_messages_page`
  (`$expand=replies`, normalized via `_channel_message_row`), `send/
  reply_channel_message`; OneNote against the **group** notebook
  (`/groups/{teamId}/onenote/…`): `list_notebooks`, `list_note_sections`,
  `list_note_pages`, `get_note_page` (raw HTML), `create_note_page`
  (`_post_html`, text/html), `update_note_page` (JSON replace command), and
  `fetch_note_image` (bearer-authenticated image bytes).
- **`widgets/teams_view.py`**: `Adw.NavigationSplitView` — sidebar of teams
  (`Adw.ExpanderRow`, channels lazy-loaded on expand), content = a channel with
  an inner `Adw.ViewStack`/`ViewSwitcher` of **Conversation** + **Notes**.
  Conversation renders posts as cards (subject/sender/time/body, threaded
  replies, per-post reply entry, bottom composer); image attachments are inline
  thumbnails and files are chips, matching `chat_view`. Notes = Section + Page
  dropdowns + full-width reader.
- **Notes rendering is NATIVE, not WebKit** (important): OneNote pages are long;
  a full-page WebView snapshot overran the GPU texture limit and segfaulted in
  `gsk_gpu_upload_cairo_op` (GTK 4.22, Intel/Mesa, Wayland; `WEBKIT_DISABLE_
  DMABUF_RENDERER=1` is already set, so WebKit falls back to one big cairo
  surface). `_render_note_body` walks the page HTML, splitting `<img>` from
  text: text → wrapping labels (`_html_to_pango`/`_strip_html` reused from
  `graph.py`), images → lazy native `GdkTexture` thumbnails, all in a scrolled
  `Adw.Clamp`. Editing seeds the rich-text editor from **plain text**, so
  existing formatting is lost on save of an existing page (new pages full
  fidelity) — known v1 limitation, next to improve.
- **Release/CI**: `io.github.sha5b.Cloudy.yml` no longer builds
  `blueprint-compiler` from gitlab.gnome.org (SDK 48+ bundles it) — a 503 there
  had broken the 0.2.0 release build.

Verified: `make build` + `make install` + 4 meson tests green; `make lint`;
headless instantiate + render of `TeamsView` (teams/channels/posts, note body,
attachment paths). GUI not yet eyeballed for this exact build — ask the user to
`make run` and open Teams → channel → Conversation/Notes.

## ⏭ Continue here — Chat scroll smoothness + animations (2026-06-15, earlier)

Polish pass over the Chat thread's scrolling and motion (`widgets/chat_view.py`,
`data/style.css`). Builds + 4 meson tests green; headless widget import verified.
The Adw animation API (`CallbackAnimationTarget`, `TimedAnimation`, `Easing`) was
confirmed present in this libadwaita binding before use.

- **Incremental thread updates** — `_render_thread` no longer tears down and
  rebuilds every bubble on each poll/refresh. It keeps a per-message fingerprint
  list (`_rendered_sigs`, `_msg_sig`); when the live thread is an unchanged
  **prefix** of the new one (the common case: a message just arrived) it only
  **appends** the new bubbles (`_appended_only` → `_full_render` is the fallback
  for edits/reactions/deletes to older messages). This stops the whole thread
  **flickering and re-downloading every inline image** every 5 s, and removes the
  scroll lurch that came with it. An un-acked optimistic echo forces a full
  rebuild so it's replaced, never duplicated (`_has_optimistic`).
- **Scroll state is now derived from the adjustment**, via its `value-changed`
  signal (`_on_thread_scrolled`) — *replacing* the old `EventControllerScroll`.
  This is the deliberate reversal of the previous note: wheel **and** trackpad
  **and** scrollbar-drag **and** keyboard (PageUp/Home/End) now all update the
  pinned/scrolled-up state and the "jump to latest" button identically. Our own
  programmatic moves go through `_set_scroll`, which sets an `_adjusting` guard so
  the handler ignores them. `changed` (`_on_thread_resized`) still re-pins on
  height changes.
- **Per-frame position hold** (`_hold_position`, a `add_tick_callback` over a
  ~350 ms settle window) replaces the old one-shot `idle_add`+`timeout(120)`
  re-pin. Re-asserting the pin/anchor on *every* frame collapses the
  "jumps multiple times" seen when **loading older history**: several older
  bubbles' images decode a frame or two apart, and a reactive (`changed`-only)
  correction left a visible one-frame "peek" each time. `_on_older` now also
  fixes a real bug — it updates `_rendered_sigs`/`_thread_sig` so a later poll
  takes the cheap append path instead of a full image-reloading rebuild.
- **Animated "jump to latest"** — the floating button glides with an
  `Adw.TimedAnimation` (`EASE_OUT_CUBIC`, 250 ms) instead of snapping; auto-pins
  stay instant.
- **New-bubble fade-in** — appended/optimistic bubbles get a one-shot
  `.cloudy-bubble-new` CSS class (`@keyframes cloudy-bubble-in`, opacity only,
  220 ms), removed after it plays so an in-place rebuild won't replay it.
- **Caching/perf (reviewed, already good)**: chat list, each thread, members and
  contacts are cached on `app.cache` (stale-while-revalidate, 90 s) — switching
  chats is instant then revalidates. Client calls **paginate** (`$top=50` chats,
  `$top=30` messages) and batch (presence, contacts) — nothing loads everything.
  The cache is **in-memory only** (cleared on restart); persisting it to disk for
  an instant cold start is a possible future win, not done.

## ⏭ Continue here — Chat capability + UX session (2026-06-15, later)

A large session that added a **Chat capability** (Teams chats / Google Chat) and
polished Mail/Calendar. Everything below is shipped and built into the
RPM + Flatpak (`make release` reinstalls the user Flatpak, so running app ==
release). Builds + 4 meson tests green; verified by headless widget instantiation
(see [[cloudy-widget-smoke-test]] pattern in memory).

### New: Chat capability (`widgets/chat_view.py`, `widgets/chat_compose.py`)
A 4th capability alongside Files/Mail/Calendar. Wired exactly like the others:
`ChatCapability` in `core/interfaces.py` (+ `"chat"` in `CAPABILITY_KEYS` and
`capabilities_of`), declared by both `Microsoft365Module` and `GmailModule`,
`"chat"` in `window.CAPABILITY_UI`, and a `ChatView` branch in
`window._capability_placeholder`. Clients gained chat methods (see below).

- **Provider scope**: Teams chat works on **work/school** Microsoft accounts only
  (delegated `Chat.ReadWrite`, unmetered). Google Chat is **Workspace-only**;
  consumer Gmail has no Chat API → the view degrades to a clear message and most
  write ops raise a friendly `GoogleError`. **The Chat tab + Teams/Shared mail
  sources are hidden for *personal* accounts** via `Account.is_personal`
  (email-domain heuristic) — `window._show_account` drops `"chat"`, and
  Mail/Calendar set `self._is_ms = provider == microsoft and not is_personal`.
- **Chat list**: 1:1 / group / **meeting** chats; `Adw.Avatar` initials (calendar
  glyph for meeting chats); unread = bold name + accent dot (Teams
  `viewpoint.lastMessageReadDateTime` vs last message, and **never** when the
  last message is `from_me` or the marker is missing — that was a false-unread
  bug); "You:" preview prefix; conversational dates (`format.relative_time`:
  Today→`HH:MM`, Yesterday, weekday, `14 Jun`, date); newest-first; **pagination**
  ("Load older conversations"); **filter by name** (type) + **server-side message
  search** (Enter → Graph `/search/query` chatMessage → hit rows open the chat).
- **Thread**: bubbles (mine right/accent), oldest-first, **older-message
  pagination** (top button + auto-load near the top), reliably **pins to bottom**
  on open. (Scroll handling was reworked in the *latest* session — see the top
  section. The state is now derived from the adjustment's `value-changed`, with a
  `changed` re-pin and a per-frame `_hold_position` settle; the old
  `EventControllerScroll` is gone.) Empty/system messages are skipped so a bare
  timestamp never shows as its own bubble.
- **Compose**: Enter sends (no send button — removed by request). **Attach button**
  (paperclip, `Gtk.FileDialog`, images) + **Ctrl+V paste** both *stage* a
  thumbnail in a strip; caption optional; sent on Enter. Images go as Teams
  **inline hosted content** (base64) so they render inline both sides. **@mentions**:
  type `@` → popover of chat members (`list_chat_members`, cached) → inserts
  `@Name`, recorded; on send builds HTML `<at id>` tags + the `mentions[]` array.
- **Per-message right-click menu**: emoji **reaction** row (`setReaction`), Reply
  (inline quote via a context bar), Forward (opens New Chat prefilled), Copy,
  **Select** (enter multi-select), **Download** (attachments), **Copy link**
  (`web_url`), Edit (own, `PATCH`), Delete (own, `softDelete`). Reactions display
  as chips under bubbles.
- **Inline images**: downloaded with the bearer token (hosted-content URLs need
  auth — a plain open 401s), **downscaled to a 240px thumbnail** (`_thumb_texture`;
  a `Gtk.Picture`'s natural size = its paintable's, so scale the *pixbuf*, not
  `set_size_request`). Relative `../hostedContents/..` srcs are resolved to the
  absolute message URL.
- **New chat** (`chat_compose.ChatComposeWindow`, an `EditorWindow`): To-field
  contacts autocomplete; `start_chat` creates/reuses a 1:1 (Teams) then sends.
- **Notifications**: chat polled alongside mail/calendar (`notifications.py`
  `_poll_chat`), raises a popup + deep-links via `app.notify-open-chat` →
  `window.open_chat`. Red **chat unread badge** on the sidebar account row
  (`notifier._chat_unread`, `chat_unread_count`, `mark_chat_read` cleared on open).

Client chat methods — Graph (`modules/microsoft365/graph.py`) and Google
(`modules/gmail/google_client.py`), same normalized shapes:
`list_chats[_page]`, `list_chat_messages[_page]`, `list_chat_members`,
`send_chat_message`, `send_chat_images`, `send_chat_html`, `fetch_bytes`,
`edit_chat_message`, `delete_chat_message`, `set_reaction`/`unset_reaction`,
`start_chat`, `search_messages`. Google raises `GoogleError` for the
Teams-only write ops. Scopes: `SCOPES_CHAT` added to `msal_graph` (`Chat.ReadWrite`)
and `google_oauth` (Workspace chat scopes), both in the sign-in consent — **adding
them forces existing accounts to Sign Out → Sign In once**.

### Also this session — Mail / Calendar / shell
- **Sidebar unread badges** per account: accent mail-unread pill (Graph inbox
  `unreadItemCount` / Gmail `INBOX.messagesUnread` via `client.inbox_unread()`)
  + red chat pill. `window.set_account_unread` / `set_account_chat_unread`,
  styled `.cloudy-badge` / `.cloudy-badge.chat`.
- **Mail pagination** — `list_messages_page` (Graph `@odata.nextLink` / Gmail
  `nextPageToken`) + a "Load older messages" row. **"Unread" virtual folder**
  (default at startup, filters the inbox client-side) + **per-account folder
  memory** (`window.remember_mail_folder`/`last_mail_folder`, survives account
  switches). **Multi-select** (MULTIPLE mode, Shift/Ctrl) + **Delete on
  selection**; **arrow nav** (↑/↓/←/→ open in reader, Shift extends), Ctrl+A,
  Ctrl+R reply, Ctrl+N new. **Search** (`set_filter_func`).
- **Calendar** — **"● Live now"** marker on ongoing events (`_is_live`);
  multi-select + arrow nav + Delete on selected events; **search**
  (`set_filter_func`). **Notification deep-link fix**: event notifications now
  carry the event id and open the specific event (`open_calendar_event` →
  `CalendarView.open_event`) — previously only landed on the tab.
- **Teal accent**: `data/style.css` overrides `@accent_bg_color`/`accent_fg_color`/
  `accent_color` (`#2190a4`) at APPLICATION priority.

### Shipped since (was deferred, now built)
- **Group-chat creation + member management** — `start_group_chat`
  (`POST /chats`, group) from the New-chat composer with 2+ recipients; the
  conversation header's **people button** opens a roster popover to **rename**
  (`rename_chat`), **add** (`add_chat_member`) and **remove** (`remove_chat_member`)
  members, each with a presence dot.
- **Presence dots** (`get_presences` → `POST /communications/getPresencesByUserId`,
  `Presence.Read`): Teams-style dot on 1:1 chat avatars + the conversation header
  subtitle, batch-refreshed every 60 s and patched in place (no list rebuild).
- **Multi-select** in the thread (Shift-click a bubble) → a select bar to
  **Forward / Copy / Delete** several messages at once.

### Next / deferred (asked for, not yet built)
- **Arbitrary file attachments** (non-image: PDFs/docs) — OneDrive upload then
  reference as a `reference` attachment.
- **Forward into an existing chat** (chat picker) — today Forward opens a *new*
  chat prefilled.
- **Google Chat**: image send, edit, reactions, member list, message search are
  all stubbed (`GoogleError`/`[]`) — Workspace media-upload + APIs needed.
- Not feasible for a delegated desktop client (don't promise): typing indicators,
  read receipts, mute/pin/hide chat, true push (we poll), threaded chat replies.

## ⏭ Continue here — earlier session (2026-06-15)

### Done this session
- **Inline event edit** (NEXT #1, see below) — shipped + built into RPM/Flatpak.
- **Refactor**: shared event date/time helpers extracted to
  `widgets/event_time.py` (`iso_to_local_naive`, `parse_hhmm`,
  `local_to_utc_iso`); `event_window.py` + `event_compose.py` both use them.
- **Full-app bug audit** (7 parallel review passes). High-confidence fixes
  applied; the rest triaged below as **Known issues**.
- **Bug fixes applied**:
  1. `update_event` (both clients) now treats `attendees=None` as "leave
     untouched" but a list — **including `[]`** — as "set it", so removing every
     attendee in the inline editor actually clears them server-side (was a
     no-op: `if attendees:` skipped empty lists).
  2. Inline editor **multi-day events no longer collapse to one day** — the
     original start→end day span is preserved across an edit (`_edit_day_span`).
  3. `notifications.py` priming timer was sharing `_tick` (which returns `True`)
     so the "prime once" timer fired **forever** alongside the steady timer,
     doubling poll traffic — now a one-shot `_prime_once` returning `False`.
- **App-ID rename** `io.github.sha5b.Clouddrive` → **`io.github.sha5b.Cloudy`**
  (data files, schema id, D-Bus name/paths, icons, gresource prefix, Makefile,
  uninstall script). Repo URLs kept as `Cloudy` (repo renamed later).
  Best-effort dconf migration in `application._migrate_legacy_settings` carries
  accounts/prefs over (works on host/RPM; Flatpak runtime has no `dconf` CLI →
  re-add accounts there once).
- **Per-account mountpoints** (`mounts.mount_base_for`): each account's drives
  mount under their own folder so same-named drives across accounts don't
  collide and a mount is attributable per account (no more re-mounting). D-Bus
  status walks ancestors for the new nesting. Pruned stale flat-mount bookmarks.
- **Nautilus extension**: removed all unmount/mountpoint code (nautilus-python
  can't add sidebar items — unmount is via in-app Files → Unmount or native ⏏).
- **Design system** (all 4 audit recommendations): `data/style.css` (loaded in
  `application._load_styles`), `widgets/metrics.py` 4px scale + window sizes,
  `source_nav.status_page`/`loading_box`, `.cloudy-meta`/`.cloudy-day`/
  `.cloudy-chip`/`.cloudy-pill`/`.cloudy-section`, title-hierarchy fix, dead
  `preferences.blp` removed, POTFILES completed. **Helpers are in place; rolling
  `.cloudy-meta`/`loading_box` into every remaining caption/loading spot is
  incremental polish best done with the app open to eyeball.**

### Known issues (from the audit — NOT yet fixed, triaged by priority)
**Security / privacy (needs a deliberate decision — behavior change):**
- **Google OAuth has no `state` parameter** (`core/auth/google_oauth.py`) — CSRF /
  auth-code-injection gap; PKCE covers the token exchange but not session
  binding. Add a random `state`, validate on the redirect.
- **Google OAuth stores `code`/`error` on the _class_, not the instance** — two
  concurrent Google sign-ins race and cross-contaminate. Move result state onto
  the per-flow `HTTPServer` instance.
- **OAuth loopback binds `127.0.0.1` but the `redirect_uri` says `localhost`** —
  Google treats these as distinct redirect URIs and `localhost` may resolve to
  IPv6 `::1`; can make sign-in hang/fail. Make both use the same literal.
- **Mail reader loads remote content** (`message_view.py`) — only JS is disabled;
  external images load → tracking-pixel/IP leak on open. Consider blocking remote
  resources by default with a "load remote images" opt-in.

**Correctness (safe to fix, just not done yet):**
- **No `@odata.nextLink` pagination** anywhere in `graph.py` (folders, groups,
  contacts, drives, calendarView) — accounts with >`$top` items silently
  truncate. Loop on `@odata.nextLink`.
- **Mount success/failure mis-detected** (`mounts.py`): `rclone mount --daemon`
  forks and returns 0 before the FUSE mount exists, so `is_mounted()` right after
  reports `active=False` and real mount failures are swallowed. Poll with a short
  timeout + capture daemon stderr.
- **`recent_changes` walk isn't bounded _within_ a single directory**
  (`file_browser.py`) — the deadline/count check is only at the top of the
  per-dir loop, so one huge dir on a FUSE mount blocks past the budget. Check the
  deadline in the inner loop and prune `dirnames` when over budget.
- **Google `reply_all` drops CC/other recipients** (`google_client.py`) — only
  replies to the original sender even when `reply_all=True`.
- **Google all-day `end` is exclusive** (`_event_from_json`) — off-by-one vs the
  Graph shape in views that compute/display the end day.
- **`respond_event` only blocks `group:` ids** (`graph.py`) — a `shared:` id would
  be prefixed into a `/me/events/shared:…/accept` path; reject/route it.
- **Unbounded dedup growth** (`notifications.py`) — `_seen_mail` /
  `_notified_events` only ever grow; cap/trim them (matters in background mode).

**Low / robustness:** `create_share_link` hardcodes `scope:"organization"`
(invalid for consumer OneDrive); Google `get_event` `body_html` heuristic
(`"<" in s and ">" in s`) false-positives plain text; cid-image strip regex in
`message_view.py` over-matches on `>` in attribute values and misses unquoted
`src=cid:`; `file_browser` rename/new-folder accept names with `/`/`..`
(path traversal within the browser); EDS publish builds a bare `VEVENT` with a
likely-wrong parent UID so it may never publish. Full per-finding detail is in
the session transcript.

## ⏭ Continue here — session (2026-06-14)

A very large session. **Everything below is built + reinstalled into the Flatpak,
but NOT committed to git** — first task next time: review the working tree and
commit on a branch (logical commits).

### Done this session
- **Rebrand**: `com.fiberelements.Cloudy` → **`io.github.sha5b.Cloudy`**;
  author → **Shahab Nedaei <ned.tabulov@gmail.com>** (sha5b). All file names,
  schema id, D-Bus name/paths, icons renamed.
- **Packaging**: Fedora **RPM** (`packaging/cloudy.spec`, noarch, meson),
  `make rpm`/`srpm`; **Flatpak bundle** `make flatpak-bundle`; **`make release`**
  → `release/` (RPM + single-file `.flatpak`) **and installs the bundle** so the
  running app matches. `release/` is gitignored (artifacts embed baked creds).
- **Credentials** baked at build time via `meson.options`
  (`ms_client_id`/`google_client_id`/`google_client_secret`) → a generated
  GSettings **vendor override** (`data/cloudy.gschema.override.in`); read from
  `.env` by the Makefile. Source ships empty defaults. Security-audited: no
  secrets in git history/tree.
- **Host-visible Flatpak mounts**: rclone runs on the host via `flatpak-spawn
  --host` into `~/.local/share/cloudy/mounts` (manifest grants
  `--talk-name=org.freedesktop.Flatpak` + `--filesystem`). `mounts.active_mounts()`
  reads `/proc/self/mountinfo` (stall-proof) — fixed the dashboard hang.
- **Nautilus**: quick **Unmount (Cloudy)** menu item (file view, not sidebar —
  API can't touch sidebar bookmarks); extension **auto-installs** (RPM → system
  path; Flatpak → copied to host on first run via `provisioner.ensure_host_
  nautilus_extension`). Fixed dotted D-Bus `OBJECT_PATH`.
- **Calendar redesign**: month **grid** (`widgets/month_grid.py`) in the Calendar
  tab + Dashboard; **past events** (loads the visible month); clicking an event
  opens a **non-modal event window** (`widgets/event_window.py`) with detail +
  RSVP + delete + **Edit**.
- **Meeting responses**: attendee **response tracker** on the event (pills grouped
  by status via `Adw.WrapBox`); empty-bodied accept/decline emails show a
  placeholder (see gotcha below).
- **Dashboard**: aggregate **cached** (stale-while-revalidate, no reload on every
  switch) + a Refresh button; pinned sources' **unread mail + events merged**
  into the overview.
- **Contacts autocomplete**: MS via **People API** (`/me/people` + `/me/contacts`,
  new `People.Read` scope); Google connections + **otherContacts** (new
  `contacts.other.readonly`). Both need **re-sign-in** (see below).
- **Account add** now auto-enables the provider's module (Google wasn't
  activating — `enabled-modules` defaulted to microsoft365 only).
- Fixes: WebKit blank mail (`WEBKIT_DISABLE_DMABUF_RENDERER=1` in `main.py` +
  manifest); strip `cid:` inline images; `update_event` (Graph + Google) for the
  Edit flow; non-transient editor/event windows so **minimize/maximize** show;
  event-compose timezone (local→UTC); escaped `&` in a preferences title.

### NEXT (asked for, deferred to here)
1. ~~**Inline event edit**~~ — **DONE** (next session, 2026-06-15). The Edit (✏️)
   button now toggles `EventDetailWindow`'s detail pane into an inline edit form
   (subject, all-day, day, start/end time, location, **removable attendees**,
   description) with Save/Cancel in the header → `client.update_event(eid, …)`,
   then reloads the detail. `get_event` now returns attendees with `email` (Graph
   `emailAddress.address` / Google `email`) so the editor re-sends the full
   desired attendee list (PATCH keeps attendees if omitted). Description prefills
   only **plain-text** bodies — HTML-bodied events start blank (empty body is
   omitted on PATCH, so the server's is preserved unless the user types).
   *Edge*: removing **all** attendees doesn't clear them server-side, because
   `update_event` only sends `attendees` when the list is non-empty.
   `event_compose.EventWindow` is still used for **New** event creation.
2. **Contacts dropdown**: only suggests after **Sign Out → Sign In** per account
   (grants `People.Read` / `contacts.other.readonly`). If it still doesn't show
   after re-consent, `GtkEntryCompletion` is deprecated/flaky in GTK4 — replace
   `compose_view._setup_completion` with a custom suggestion popover.
3. **Commit** the session.

### Gotchas learned this session
- **Smoke-test by INSTANTIATING widgets**, not just importing — see
  `[[cloudy-widget-smoke-test]]`. `Gtk.init_check()` works headless here; a
  `monthdatescal` typo passed import but crashed `MonthGrid()`.
- **`meetingMessageType`** can't be `$select`ed or entity-cast on the messages
  endpoint (both 400) — the "X accepted" card was dropped; empty meeting-response
  emails fall back to the "No message content" placeholder.
- **`make release` installs the bundle**; other flatpak builds need
  `make flatpak-test` to update the *running* app (stale-build confusion bit us
  repeatedly — symptoms looked like the fix didn't work).
- **Windows must be non-transient** (no `transient_for`) for GNOME to show
  minimize/maximize.

## Build / run / test
```bash
cd <repo>
make run            # meson build+install into _install, then launch ./_install/bin/cloudy
make build|test|lint|clean
make flatpak flatpak-run   # sandboxed (org.gnome.Platform 50)
```
- Dev toolchain here is **user-space**: `meson`/`ninja` via `pip --user`
  (`export PATH="$HOME/.local/bin:$PATH"`); `blueprint-compiler` auto-fetched via
  the wrap; `msal` via `pip --user`. `rclone` is **auto-provisioned** (rootless)
  into `~/.local/share/cloudy/bin/rclone` on first run; also bundled in the Flatpak.
- The app is **single-instance** (GApplication) — quit the running one before
  relaunching, or a new launch just hands off and exits 0.
- `make lint` is just `py_compile` (no pyflakes/ruff installed here). Useful extra
  checks: `python3 -m py_compile <files>`; a **headless import smoke test**
  (`gi.require_version` then `importlib.import_module` each widget module —
  catches missing imports/NameErrors without a display). NOTE: `window.py` can't
  be imported standalone (its `Gtk.Template` needs the compiled gresource); skip
  it in smoke tests. `meson test` runs 4 validation tests (desktop/schema/
  metainfo/blueprint).
- **Driving the GUI from a headless/agent shell fails** (the Wayland app handoff
  signals and kills the wrapper shell, exit 144) — verify via build + tests +
  import/logic smoke, then ask the user to `make run` to eyeball.

## Credentials (already set up locally; repo is public-safe)
- **Microsoft**: multi-tenant Entra client ID `dcd8ee18-6e62-4c5a-b01f-86f9556f8fed`
  (public client — not a secret). **Google**: Desktop OAuth client.
- Real values live **outside git** in `.env` (repo root, gitignored) and/or
  `~/.config/cloudy/secrets.env`, loaded into `CLOUDY_*` env on startup by
  `core/credentials.py`. The committed repo contains **zero** real IDs/secrets.
  `.env.example` is the template. See `docs/SECRETS.md`.
- Env vars: `CLOUDY_MS_CLIENT_ID`, `CLOUDY_GOOGLE_CLIENT_ID`,
  `CLOUDY_GOOGLE_CLIENT_SECRET` (also GSettings keys; env wins).
- ⚠️ The Google client secret was pasted in chat during setup — **rotate it**
  in Google Cloud Console before any public release.

## How it works (key decisions)
- **Auth**: system browser + loopback. Microsoft = MSAL (`core/auth/msal_graph.py`);
  Google = hand-rolled loopback+PKCE on urllib (`core/auth/google_oauth.py`).
  Tokens in **libsecret** (`core/secrets.py`). Sign-in requests **all** scopes up
  front (Files+Teams+Groups+Mail+Calendar+**Mail.ReadWrite.Shared**) so one
  consent covers everything, **including shared mailboxes/calendars**.
- **Shared/group sources**: Graph `list_shared_folders` / `list_shared_events`
  use `/users/{address}` + `Mail.ReadWrite.Shared`; group mail/calendars use
  `/groups/{id}` + `Group.Read.All`. IDs are prefixed `shared:<addr>:` /
  `group:<id>:` so `get_message`/`get_event` route back correctly. Accessing your
  **own** address as a "shared" source returns Graph 403 ErrorAccessDenied —
  that's expected (use the **Me** source for your own mailbox).
- **Mail & Calendar share one source model** (Microsoft only): **Me / Teams /
  Shared** tabs. The common scaffolding lives in **`widgets/source_nav.py`**:
  `SourceTabs`, `run_async(work, on_done)` (the off-thread→idle_add helper used by
  every view), `clear_listbox`, `message_row`/`action_row` placeholders,
  `is_scope_error`, `present_add_shared_dialog`, and the **pin** helpers
  (`toggle_pin`/`is_pinned`/`find_pin`). When a shared/group call fails for lack
  of scope, the view shows a **Re-sign in** action row (re-consent grants the new
  scope; everyday mail keeps working).
- **Files = rclone mounts** (`modules/microsoft365/mounts.py`): rclone does its
  **own** browser auth (built-in app id → no registration), reused per account.
  Mount → FUSE network drive + a GTK sidebar bookmark → appears in Nautilus.
  **It's a live network drive (two-way), not a synced copy.** Cache mode + mount
  location come from Settings. **Mount layout** (`mount-layout` setting): either
  `one-folder` (everything under the global mount location) or `individual` (each
  account picks its own folder via `Account.mount_location`). `mountpoint_for`/
  `mount` take an optional `base` override; `account_mount_base(loc)` resolves it.
- **In-app file browser** (`widgets/file_browser.py`): the Files tab is an
  `Adw.NavigationView` — **Libraries** (mount toggles) at the root; a *mounted*
  library row is clickable → pushes a `FileBrowserPage` that lists the mountpoint
  (folders first, drill in, click a file → opens in the default app). Listing
  runs off-thread; `recent_changes(roots)` (bounded scan) powers the Dashboard.
- **Offline sync** (`core/sync.py`): when `default-sync-type` = `full` and an
  account's per-account toggle is on, `SyncManager` runs `rclone bisync` into
  `…/cloudy/synced` on a timer. When type = `stream`, the per-account toggle is
  disabled (mounting stays manual). Streaming auto-mount-on-login is **not** built.
- **Caching**: `core/cache.py` MemoryCache on `app.cache` (stale-while-
  revalidate, 90s TTL) for mail/calendar; per-source cache keys; Refresh
  invalidates per account.
- **Nautilus**: app exports a D-Bus status service (`core/dbus_service.py`); host
  extension draws emblems + menu (`make install-nautilus`).
- **UI shell** (`window.py`): sidebar (Overview + accounts) → per-account
  `ViewSwitcher` over Files/Mail/Calendar; header Refresh.
  `open_mail(account, mid)` and `open_account_tab(account, tab)` are deep-link
  entry points used by the Dashboard. A **turned-off** account (its module
  disabled) shows "Turned off" in the sidebar and a disabled status page.
- **Dashboard** (`widgets/dashboard_view.py`): **Pinned** (starred shared/group
  sources with live counts, click to jump) → **Upcoming** (your calendars) →
  **Recent mail** → **Recent file changes** (newest edits in mounted/synced dirs).
- **Preferences** (`preferences.py`), two pages only:
  - **General** — Mount location · Mount layout · File caching · Sync type
    (stream/full) · Start at login.
  - **Accounts** — each account is an `ExpanderRow`: an **on/off switch** for its
    services (replaces the old Modules tab → `enabled-modules` setting), Sign
    In/Out, Remove; expands to **Sync files offline** + **Mount location** (both
    shown but greyed until their General prerequisite is set).
- **Pinning ("star")**: the ★ button in Mail/Calendar (Teams/Shared sources)
  toggles `Account.pinned_sources` entries
  `{kind: mail|calendar, source: shared|teams, id, name}`; the Dashboard renders
  them.

## Gotchas / conventions (don't relearn the hard way)
- **Use `source_nav.run_async`** for off-thread work, not raw
  `threading.Thread`+`GLib.idle_add` — callback signature is `(result, error)`;
  capture extra ids via a lambda (`lambda res, err: self._on_x(id, res, err)`).
- **Pango markup**: Adw row/title/StatusPage text is parsed as markup — wrap
  dynamic text with `widgets/format.esc()`. Mail/agenda lists use plain
  `Gtk.Label` (immune).
- **Graph URLs**: encode query values with spaces (e.g. `$orderby=... desc`) or
  urllib aborts ("URL can't contain control characters").
- **GSettings**: `Gio.Settings.new()` *aborts the process* if the schema isn't
  installed — look it up via `SettingsSchemaSource` first (see `mounts._setting`).
  New schema keys need `make build` (recompiles + reinstalls the schema).
- **New scopes need re-consent**: existing accounts must re-sign-in (Preferences →
  Accounts → Sign Out, then Sign In) to pick up newly-added scopes. The Mail/
  Calendar views surface this with an inline **Re-sign in** button on scope errors.
- **`Account` model** (`core/account_registry.py`): `from_dict` tolerates missing
  keys, so adding fields is safe; removing a field just drops it on next save
  (this is how `group_calendars` was retired). Current extra fields:
  `full_sync`, `mount_location`, `shared_mailboxes`, `pinned_sources`.
- **Network-mount scans are dangerous**: `os.walk` over a FUSE mount can stall /
  trigger downloads. `recent_changes` is bounded by `max_scan`; keep any new
  scanning bounded too.
- **Module on/off is per-provider**: the Accounts on/off switch toggles the whole
  `module_id` (`enabled-modules`), so all accounts of a provider share it.
- **Google "Testing" publishing status** expires refresh tokens after 7 days;
  publish to production for longer-lived use.
- **meson install doesn't prune**: `make install` removes the installed package
  tree first so renamed/removed modules don't linger.

## Status: done (working, all builds + 4 meson tests green)
Sign-in (MS + Google), Files (OneDrive + Teams + Google My Drive) with
Mount↔Unmount **and an in-app file browser**, Mail and Calendar both with
**Me/Teams/Shared** sources + shared-mailbox add + **★ pin to Dashboard** + inline
re-sign-in on scope errors, message reader, event detail + RSVP, **reworked
Dashboard** (pinned/upcoming/mail/file-changes), **reorganized Preferences**
(General vs Accounts; per-account services on/off, offline-sync toggle, mount
location; **Modules tab removed**), mount layout (one-folder/individual) +
per-account mount location, caching + Refresh, rclone auto-provision, secrets,
Nautilus D-Bus + extension. Shared view code deduped into `widgets/source_nav.py`.

## Next steps (the backlog)
1. **Verify shared/group sources end-to-end** against a *real* shared mailbox /
   group the user has delegated access to (not their own address — that 403s).
2. **Streaming sync activation** — make the per-account toggle, when sync type =
   `stream`, actually auto-mount the account's libraries at startup (today it's
   disabled; only `full` bisync is wired).
3. **Calendar grid** — real month/agenda view (currently a 7-day list).
4. **Live transfer status** — mount rclone with `--rc`, poll `core/stats` for
   ↓/↑ activity; feed the D-Bus service so Nautilus shows transferring/online.
   This is also the better long-term source for the Dashboard "Recent file
   changes" than walking the mount.
5. **Compose/reply** for mail; **file ops** (rename/delete/upload) in the browser.
6. **Multi-account-per-module**: the Accounts on/off switch currently toggles the
   whole module (all accounts of that provider). If multiple same-provider
   accounts become common, add a real per-account `enabled` flag.

## Layout
`src/cloudy/{main,application,window,preferences,account_dialog}.py`,
`core/` (interfaces, plugin_engine, account_registry, secrets, cache,
credentials, provisioner, dbus_service, sync, auth/), `modules/microsoft365/`
(graph, files, mounts, abraunegg), `modules/gmail/` (google_client),
`widgets/` (files/mail/calendar/dashboard/message/event views, **source_nav**,
**file_browser**, clients, graph_helper, format). Data in `data/` (gschema,
desktop, metainfo, blueprints, icons), Flatpak manifest
`io.github.sha5b.Cloudy.yml`.
```
widgets/source_nav.py   shared: SourceTabs, run_async, listbox/placeholder
                        helpers, is_scope_error, add-shared dialog, pin helpers
widgets/file_browser.py in-app browser (FileBrowserPage) + recent_changes()
```
