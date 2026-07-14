<!--
SPDX-License-Identifier: GPL-3.0-or-later
SPDX-FileCopyrightText: 2026 Shahab Nedaei
-->

# Cloudy — Handoff / Continue Here

Cloudy is a **GTK4 / Libadwaita (Python / PyGObject)** super-app for **Microsoft 365 (OneDrive + Teams/SharePoint, Mail, Calendar)** and **Google (Gmail, Calendar, Drive)** on Fedora 44 (GNOME 50). It orchestrates proven backends (rclone for mounts; Microsoft Graph / Google REST for mail/calendar) rather than reimplementing them. Read `docs/ARCHITECTURE.md`, `docs/AUTH.md`, `docs/SECRETS.md`, `docs/ROADMAP.md` for depth.

## Current status (v0.3.3, 2026-07-14)

Working and shipped (RPM + Flatpak; `make release` reinstalls the user Flatpak so the running app == release):
- **Sign-in** (Microsoft via MSAL, Google via loopback+PKCE), tokens in libsecret.
- **Files** = rclone FUSE mounts: OneDrive + Teams libraries (MS) and My Drive + Shared with me + Shared Drives (Google), with Mount↔Unmount, an in-app `Adw.NavigationView` browser, and `recent_changes` (bounded scan) feeding the Dashboard.
- **Mail** with Me/Teams/Shared sources, shared-mailbox add, pagination, Unread virtual folder, per-account folder memory, multi-select + arrow-nav + delete, search, CC/BCC, read-receipt request (MS), inline re-sign-in on scope errors, pop-out message window.
- **Calendar** with Me/Teams/Shared sources, month grid + agenda, past events, RSVP for **both** MS and Google, "Live now" marker, multi-select/search/delete. Meeting-invite emails show an invite card with Accept/Tentative/Decline: Microsoft invites are detected as Graph `eventMessage`s (`$expand=microsoft.graph.eventMessage/event`) and answered via the real `/me/events/{id}/accept…` action (`sendResponse: true`, so organizer tracking updates); plain `.ics` invites keep the iMIP `METHOD:REPLY` path (`core/ics.py`). **Both providers aggregate secondary/shared-in calendars** into the Me source, and the notifier badges the Calendar tab with unanswered invitations (+ a "You're invited" banner).
- **Chat** (MS Teams chats; Google Chat Workspace-only) and **Teams** (channels + OneNote) capabilities, work/school MS only — hidden for personal accounts.
- **Activity** tab (first/default): aggregates recent mail + upcoming/unanswered invites + recent chats + Teams reacted/mentioned.
- **Dashboard** (Pinned / Upcoming / Recent mail / Recent file changes, + Activity feed for work MS accounts), **Command palette** (Ctrl+K), **persistent offline cache**, **notifications** with DND/quiet-hours/relevance-tiers/per-source mute/digest batching, Nautilus D-Bus emblems + extension, secrets, rclone auto-provision.
- 0.2.4 added RSVP + Activity; 0.2.5 added fuller mail headers, pop-out message window, accurate chat presence; 0.2.8 added per-account client cache, in-place Mail/Chat list updates, M365 share-link path resolution, RFC 5545 iCalendar parsing, pinned/checksum rclone provisioning, and Graph calendar/OneNote fixes; 0.2.9 added invite→calendar sync (mail RSVP updates your own calendar, cancellations removable), mail organization (right-click mark-unread/flag/move-to-folder, drafts save + resume), cache invalidation on every write (fixes stale event times), the /etc/localtime IANA timezone fix (CEST 400), the run_async toplevel-window fix (modals stuck on spinner), parser hardening, and the GraphClient per-domain split (graph_http/files/mail/calendar/chat/teams).
- 0.3.2 (full Graph/Google client audit) added Graph-native meeting-invite cards + RSVP with organizer tracking, the pending-invitation Calendar badge/notification, nested Outlook mail folders, secondary-calendar aggregation for Microsoft, upload-session sends for >3 MB attachments, replies that keep the quoted thread (`comment`, not `message.body`), native Graph forward (keeps inline images/attachments), `Group.ReadWrite.All` (group replies 403'd on read-only; re-sign-in needed), chat-file permission grants (recipients could never open sent files), a fully async Nautilus extension (the sync `ManagedRoots` D-Bus call froze Nautilus), and the Google fixes: `calendar.calendarlist.readonly` scope (calendar list always 403'd), chat edit `updateMask`, own-message detection via OIDC `sub`, charset-aware body decoding, event pagination, rotated-refresh-token persistence, granular-consent 403 → "Re-sign in".
- 0.3.3 made chat threads tell the whole story: Graph `systemEventMessage`s (member added — with the Teams-style shared-history suffix computed from `visibleHistoryStartDateTime` —, removed, left, chat renamed, call started/ended) render as centered status rows (`_system_event_row` in `graph_chat.py` → `_system_row` in `chat_view.py`; unknown event kinds are hidden, not noise), and deleted messages keep a dim "X deleted this message" tombstone (row flag `deleted`) instead of being filtered out of the page. Neither row type gets the message context menu/selection.

**Verification convention** (applies to nearly every change below): GUI cannot be driven from a headless/agent shell (Wayland handoff kills the wrapper shell, exit 144). So changes are verified by `make build` + `make test` (4–5 meson validators + the `tests/unit/` logic suite, 104 tests) + `make lint` (py_compile) + a **headless import/instantiate smoke test** (`gi.require_version` then import/instantiate each widget module — `window.py`/`application.py` can't be imported standalone, their `Gtk.Template` needs the compiled gresource), then the user runs `make run`/`make flatpak-run` to eyeball. "Not yet eyeballed" boilerplate is omitted per-entry below.

---

## Standing gotchas & known limitations

### Architecture / conventions
- **Use `source_nav.run_async(work, on_done)`** for all off-thread work, never raw `threading.Thread`+`GLib.idle_add`; callback signature is `(result, error)`, capture extra ids via lambda (`lambda res, err: self._on_x(id, res, err)`).
- **Pango markup**: Adw row/title/StatusPage text is parsed as markup — wrap dynamic text with `widgets/format.esc()`. Mail/agenda lists use plain `Gtk.Label` (immune).
- **Graph query URLs**: encode values with spaces (e.g. `$orderby=... desc`) or urllib aborts ("URL can't contain control characters").
- **GSettings**: `Gio.Settings.new()` *aborts the process* if the schema isn't installed — look up via `SettingsSchemaSource` first (see `mounts._setting`). New schema keys need `make build` (recompiles + reinstalls schema).
- **New scopes force re-consent**: existing accounts must Sign Out → Sign In to pick up added scopes; Mail/Calendar surface this with an inline **Re-sign in** button on scope errors (`is_scope_error`).
- **`Account.from_dict`** tolerates missing keys (adding fields safe; removing drops on next save). New fields must be added to the `from_dict` allowlist or they silently drop (this bit `muted_sources`). Extra fields: `full_sync`, `mount_location`, `shared_mailboxes`, `pinned_sources`, `muted_sources`.
- **Network-mount scans are dangerous**: `os.walk` over a FUSE mount can stall / trigger downloads. Keep scanning bounded (`recent_changes` `max_scan`).
- **Module on/off is per-provider**: the Accounts on/off switch toggles the whole `module_id` (`enabled-modules`), shared by all accounts of a provider.
- **Single-instance app**: quit the running instance before relaunching (a new launch hands off and exits 0) — also required for CSS to reload.
- **meson install doesn't prune**: `make install` removes the installed package tree first so renamed/removed modules don't linger as phantom providers.
- **Smoke-test by INSTANTIATING widgets**, not just importing (`Gtk.init_check()` works headless here; a `monthdatescal` typo once passed import but crashed `MonthGrid()`).
- **Windows must be non-transient** (no `transient_for`) for GNOME to show minimize/maximize. Editor surfaces are non-modal `Adw.Window`s (`EditorWindow`), never modal `Adw.Dialog`.
- **GTK4 CSS has no `!important`** (errors "junk at end of value" — that's why early flat-avatar attempts silently failed); use plain longhand at `PRIORITY_APPLICATION`.
- **`make release` installs the bundle**; other flatpak builds need `make flatpak-test` to update the *running* app (stale-build confusion looks like the fix didn't work).

### Avatars / presence (final, correct version — supersedes earlier attempts)
- `Adw.Avatar`'s coloured background lives on an **internal child gizmo** whose CSS node is `avatar` (carries `.color1–.color14`), *not* on the `Adw.Avatar` widget (node `widget`). So a `avatar.cloudy-avatar-flat` selector matches nothing — reach the inner node as a descendant: `.cloudy-avatar-flat.cloudy-avatar-flat avatar { … }`. Widget tree: `Avatar(widget) → AdwGizmo(avatar, .colorN) → Label/Image`.
- Flat **per-person** palette: `_avatar` adds `cloudy-avatar-c{0..7}` by a stable byte-sum hash of the contact name (`_avatar_color_index`); palette `.cloudy-avatar-cN avatar` in `style.css` (solid fills, white initials).
- **Presence dot** is **CSS-drawn** (a sized `Gtk.Box` with `.cloudy-presence`/`-{state}`), not a `media-record-symbolic` icon, so it can't go missing under a runtime theme; Offline/unknown shows a grey dot. `_on_presence` merges **without downgrading** a known availability to blank/unknown (the second per-chat fetch often returns `PresenceUnknown` and a blind `dict.update` erased a freshly-resolved status). Don't reintroduce the removed `print("[presence] …")` diagnostics.

### Read receipts — standing limitation
Neither Graph nor Google exposes whether *another* person read your **chat** message (it's a private Teams feature); the chat status glyph stays Sent/Sending (`_status_glyph`/`_apply_status` shows ONE glyph on my most-recent message: clock→check, **no "Seen"/eye**). **Email** is the only place a read receipt is possible — implemented via Graph `isReadReceiptRequested`, gated to Microsoft accounts in the composer.

### ChatView teardown (accepted minor leak)
A first attempt stopped poll/presence timers on `unrealize`, but `Adw.ViewStack` unrealizes the hidden Chat page on tab switch, so it killed presence dots permanently. Reverted. Timers self-cancel via `get_root() is None`, so the orphan-timer leak is minor and accepted; a targeted teardown (only on real account removal) is the proper future fix.

---

## Already investigated — NOT implementable (don't redo)
- **Google Chat for personal accounts**: the Chat *product* exists for consumer Gmail but the Chat *REST API* requires a Business/Enterprise Workspace account. Hiding the Chat tab for personal Google accounts (`window.py` `is_personal` filter) is correct. Nothing to build.
- **Delegated/shared Gmail mailboxes**: Gmail has no end-user delegated-mailbox REST access (needs domain-wide delegation via a service account + admin) — impossible for a desktop OAuth app.
- **Accessing your *own* address as a "shared" source** returns Graph 403 ErrorAccessDenied — expected; use the **Me** source.
- **Not feasible for a delegated desktop client** (don't promise): typing indicators, chat read receipts, true push (we poll), threaded chat replies. (Mute/pin/hide chat *were* later built; the no-promise note predates that.)
- **`meetingMessageType`** can't be `$select`ed or entity-cast on the messages endpoint (both 400) — the "X accepted" card was dropped; empty meeting-response emails fall back to a "No message content" placeholder.

---

## Recently completed (0.2.8)

- **Per-account client cache** — `widgets/clients.build_account_client` now reuses a cached client per `account.id` stored on `CloudyApplication`; evicted on sign-out/removal. Removes the repeated MSAL/libsecret rebuild on every request.
- **Incremental Mail/Chat list updates** — `source_nav.patch_listbox` diffs rows in place so refreshes preserve selection/scroll instead of rebuilding the whole `ListBox`.
- **Microsoft 365 share-link path resolution** — `files.create_share_link` resolves a local path back to the correct `(drive_id, relative_path)` via remembered mount records.
- **Graph calendar hardening** — create/update send local wall-clock time + IANA timezone; `list_events` routes personal/shared/group calendars and uses `Prefer: outlook.timezone`.
- **OneNote pagination** — notebooks/sections/pages follow `@odata.nextLink`.
- **RFC 5545 iCalendar parser** — `core/ics.py` handles escapes and quoted parameters.
- **Pinned rclone provisioning** — `v1.74.3` with SHA-256 checksums for `amd64`/`arm64`.
- **Google OAuth cleanup** — local redirect server is always shut down.

## Biggest remaining wins — NOT done (architectural; need GUI/live-API testing, get sign-off)
1. **Lazy view construction** (`window._show_account`): every account switch eagerly builds ALL five tab views and fires ~6 concurrent network loads though one tab is visible. Build the visible/remembered tab eagerly, the rest on first `notify::visible-child-name`.
2. **Gmail inbox is a serial N+1** (`google_client.list_messages_page`): one blocking GET per message. Parallelize with a ThreadPoolExecutor (pattern in `list_events`) or use the Gmail batch endpoint.
3. **Image decode on the main thread** in chat/teams `done()` callbacks and `message_view.html_body_widget` (inline-image shrink+re-encode) — move decode into the worker thread.
4. **Shared-helper dedup**: one image `texture_from_bytes` (4 copies: media_window/rich_editor/teams_view/chat_view), one `_attachment_chip` (2), one `_reply_quote` (2) — extract to a shared module.
5. **Mail `refresh_live` collapses pagination**; **Files** nav race + unbounded FUSE-folder render (see below).

---

## Known, deliberately NOT fixed (need live-API verification)
- **Calendar display does not show an end date for multi-day / all-day spans.** `_format_when` prints only the start day. Cross-provider (MS + Google) change — must be tested against the live API before touching.
- **Mail `refresh_live` collapses pagination**: a live refresh re-renders only page 1, discarding loaded "Load older" pages and resetting scroll.
- **Files `_load`/`_toggle_expand` last-write-wins race** on rapid navigation (no nav-token guard); `_scan` over a FUSE mount is unbounded for huge folders.
- **Google Shared Drives UNTESTED on a real Workspace account** (user has only personal Google): `MountManager.list_google_shared_drives` spins a throwaway rclone `drive` remote and parses `rclone backend drives` JSON — the JSON shape + Shared Drives path need a Workspace account to confirm. Shared-with-me is testable on personal. Shared Drives only appear *after* the user has mounted something once (an rclone token must already exist; the app holds no Google Drive OAuth scope).

---

## Deferred follow-ups (scoped, asked-for, not yet built)
- **Conversation threading** in Mail: the list is a flat `ListBox` entangled with pagination, optimistic send-reconcile, unread filter, multi-select and search; grouping by Graph `conversationId` / Gmail `threadId` is a few-hundred-line rewrite + client shape changes. Own focused pass.
- **Send outbox** (queue/retry sends offline): pairs with the offline cache but a separate non-trivial surface.
- **Google Tasks**: net-*new* (Microsoft To Do isn't in the app either), not parity — candidate for a future shared Tasks capability.
- **Free/busy `getSchedule`, week/day calendar views, unified cross-account inbox/agenda.**
- **Arbitrary file attachments** in chat (non-image PDFs/docs): OneDrive upload then reference as a `reference` attachment.
- **Forward into an existing chat** (chat picker) — today Forward opens a *new* chat prefilled.
- **Google Chat write ops** (image send, edit, reactions, member list, message search) are stubbed (`GoogleError`/`[]`) — needs Workspace media-upload + APIs.
- **Streaming sync activation**: when sync type = `stream`, auto-mount the account's libraries at startup (today disabled; only `full` bisync wired).
- **Live transfer status**: mount rclone with `--rc`, poll `core/stats` for ↓/↑, feed the D-Bus service so Nautilus shows transferring/online — also a better source for Dashboard "Recent file changes" than walking the mount.
- **Multi-account-per-module**: the Accounts on/off switch toggles the whole module; if multiple same-provider accounts become common, add a real per-account `enabled` flag.
- **Notifications P2** (from the CSCW/HCI research backlog): **Meeting auto-focus** (derive in-meeting state from the user's own calendar, auto-suppress tier-2 — coarse only, never expose meeting titles; gate behind a setting, keep items recoverable via badges since misfires silently swallow notifications); **Dashboard catch-up** ("since you were away" unread markers + per-channel unread counts — NOTE: cheap per-channel unread isn't available from Graph; chats half feasible, channels need a workaround). #6 "appear offline / invisible" deliberately dropped per the user. Research flagged no direct evidence on threading-vs-linear or grounding/search — separate pass.
- **OneNote edit fidelity (v1 limitation)**: editing an existing page seeds the rich-text editor from **plain text**, so existing formatting is lost on save (new pages are full fidelity).
- **Contacts dropdown** only suggests after Sign Out → Sign In per account (grants `People.Read` / `contacts.other.readonly`). If still missing after re-consent, `GtkEntryCompletion` is deprecated/flaky in GTK4 — replace `compose_view._setup_completion` with a custom suggestion popover.

---

## Open bug backlog (from the full-app audit — triaged, NOT yet fixed)

**Security / privacy (needs a deliberate decision — behavior change):**
- **Google OAuth has no `state` parameter** (`core/auth/google_oauth.py`) — CSRF / auth-code-injection gap (PKCE covers token exchange, not session binding). Add a random `state`, validate on redirect.
- **Google OAuth stores `code`/`error` on the _class_, not the instance** — two concurrent Google sign-ins race and cross-contaminate. Move result state onto the per-flow `HTTPServer` instance.
- **OAuth loopback binds `127.0.0.1` but `redirect_uri` says `localhost`** — Google treats these as distinct, and `localhost` may resolve to IPv6 `::1`; can hang/fail sign-in. Use the same literal for both.
- **Mail reader loads remote content** (`message_view.py`) — only JS disabled; external images load → tracking-pixel/IP leak on open. Consider blocking remote resources by default with a "load remote images" opt-in.

**Correctness (safe to fix, not done):**
- **No `@odata.nextLink` pagination** in `graph.py` for folders, groups, contacts, drives (OneNote and `calendarView` now paginate) — accounts with >`$top` items silently truncate. Loop on `@odata.nextLink`.
- **Mount success/failure mis-detected** (`mounts.py`): `rclone mount --daemon` forks and returns 0 before the FUSE mount exists, so `is_mounted()` right after reports `active=False` and real failures are swallowed. Poll with a short timeout + capture daemon stderr.
- **`recent_changes` walk isn't bounded _within_ a single directory** (`file_browser.py`) — the deadline/count check is only at the top of the per-dir loop, so one huge dir on a FUSE mount blocks past budget. Check the deadline in the inner loop and prune `dirnames` when over budget.
- **Google `reply_all` drops CC/other recipients** (`google_client.py`) — only replies to the original sender even when `reply_all=True`.
- **Google all-day `end` is exclusive** (`_event_from_json`) — off-by-one vs the Graph shape in views that compute/display the end day.
- **`respond_event` only blocks `group:` ids** (`graph.py`) — a `shared:` id would be prefixed into a `/me/events/shared:…/accept` path; reject/route it.
- **Unbounded dedup growth** (`notifications.py`) — `_seen_mail` / `_notified_events` only ever grow; cap/trim (matters in background mode).

**Low / robustness:** `create_share_link` hardcodes `scope:"organization"` (invalid for consumer OneDrive); Google `get_event` `body_html` heuristic (`"<" in s and ">" in s`) false-positives plain text; cid-image strip regex in `message_view.py` over-matches on `>` in attribute values and misses unquoted `src=cid:`; `file_browser` rename/new-folder accept names with `/`/`..` (path traversal within the browser); EDS publish builds a bare `VEVENT` with a likely-wrong parent UID so may never publish.

**Edge in shipped inline event edit:** removing **all** attendees in the inline editor doesn't clear them server-side because `update_event` only sends `attendees` when the list is non-empty. (NOTE: a later session's fix #1 below claims to have addressed `attendees=[]` — verify which is current against `update_event` before relying on either.)

**Credentials note:** ⚠️ the Google client secret was pasted in chat during setup — **rotate it** in Google Cloud Console before any public release.

---

## Changelog (reverse-chronological)

### rclone mount reliability: SharePoint uploads, logging, reconciliation, sync status (2026-07-09)
All in `modules/microsoft365/mounts.py` (+ `widgets/files_view.py`, `application.py`); tests in `tests/unit/test_files.py` (`TestMountArgv`, `TestReconcile`).
- **Root cause of "files uploaded via Nautilus never reach the server"**: SharePoint / OneDrive-for-Business **rewrites Office documents** (`.docx`/`.pptx`/`.xlsx`) server-side, so the stored file differs in size/hash from what rclone uploaded. rclone's post-upload verify then logs `corrupted on transfer: sizes differ`, **deletes its copy, and retries forever** — the file never lands. Plain files (PDFs, images, CAD) are unaffected, which is why it looked intermittent. Fix: `rclone_mount_argv` appends `--ignore-size --ignore-checksum` **for OneDrive mounts only** (new `onedrive=` flag threaded through `mount()`/`mount_drive()`/`FilesModule.mount_drive`); Google keeps its integrity checks. Verified live — every stuck Office file uploaded on try #1.
- **Daemon logging + rotation**: `rclone mount --daemon` discarded ALL output, so upload failures were invisible. Every mount now logs to `~/.local/share/cloudy/logs/rclone-<account>-<drive>.log` (`--log-file --log-level INFO`); `_rotate_log` moves a >5 MB log aside to `.log.1` on (re)mount. Also added explicit `--vfs-write-back 5s`. Helpers: `log_root()`, `log_file_for()`.
- **Bookmark ⟷ record ⟷ remote reconciliation** (`reconcile_mounts`, run once at startup before `remount_saved`): fixes the silent-data-loss class where a Nautilus sidebar bookmark outlives its mount (writes into it land in a bare local stub and never upload). For each Cloudy bookmark whose drive isn't mounted: **adopt** it if its rclone remote still exists + the mountpoint maps to a known account (reconstruct the `mounts.json` record from `config dump` — token/drive_id already in the remote, no re-auth), else **remove** the stale bookmark. Also records live-but-unremembered mounts. This is what heals "`mounts.json` remembered only 1 of 4 mounted drives". Helpers: `bookmark_paths()`, `config_dump()`, `_reconstruct_record()`, `_remember()`.
- **Live sync status** (`--rc` unix socket per mount + `upload_status()` → `{uploading, queued, errored}` from `vfs/stats`): Files view rows show `↑ Uploading N…` / `Synced` / `⚠ N didn't upload`, polled off-thread every 3 s while a queue drains (`_refresh_upload_status`, stops on unmap). **Crucially the mount must survive rc failure**: a leftover socket makes rclone abort the whole mount as fatal, so `mount()` tries with `--rc` then retries once WITHOUT it (`_rclone_mount_once(..., rc=)`); sockets are cleared before mount and on unmount. Socket dir: `rc_root()` = `~/.local/share/cloudy/rc`.
- **Fixed a test-isolation landmine**: `test_files.py` wrote `_save_mount_records([])` to the **real** `mounts.json` (running the suite wiped the user's live-mount records!). Now patches `_mount_state_file` to a temp path via `_isolate_state_file`.
- **STILL LACKING / follow-ups**: (1) the running app is a **Flatpak** — source changes need `make flatpak-test` + app restart to take effect; the 90 s watchdog will otherwise keep remounting with the installed (old) code. (2) `upload_status` reads only VFS counters — a file rejected outright (bad name/too big) shows in the log but not the indicator; consider surfacing `erroredFiles` details. (3) reconciliation adopts by folder-name (safe_name), so an adopted drive's label may show underscores until the user remounts it from the Files view. (4) no global "all drives synced" summary on the Dashboard yet.

### 0.2.4 release — RSVP + Activity feed (2026-06-18)
- Calendar RSVP works for MS *and* Google (Google gained `respond_event`; calendar was always writable, old `can_respond=False` was just unimplemented); unanswered invites render dimmed-but-clickable (`responseStatus` now in the list query).
- Meeting-invite **emails** show Accept/Tentative/Decline sending a standards `METHOD:REPLY` iMIP via new `core/ics.py` (parser + reply builder).
- New **Activity** tab (`widgets/activity_view.py`), first/default — aggregates recent mail + upcoming/unanswered invites + recent chats; MS adds "reacted to"/"mentioned you" via `GraphClient.recent_chat_activity()` (bounded scan of 8 most-recent chats).
- Image viewer (`media_window.py`): scroll-zoom + drag-pan; multi-image chat messages lay out as an `Adw.WrapBox` gallery. Composer gained `isReadReceiptRequested` toggle (MS only).
- CI: `.github/workflows/release.yml` bumped checkout@v4→v6, action-gh-release@v2→v3 (Node 20→24). Release recipe: `gh release create vX.Y.Z --target main --notes-file <section>` creates the tag (triggers build) and sets notes at create time.

### Chat/Teams/OneNote rework + cross-tab bug sweep (2026-06-17)
- **Chat** (`widgets/chat_view.py`): `refresh_live()` re-fetches the chat list so a new message bumps its conversation to top (wired via `notifications._on_chat` → `window.refresh_account_chat`; skipped during search; re-selects open chat). Reply quotes — Graph parses Teams `messageReference` into a `reply_to` dict; `_bubble` renders a clickable quote (`_scroll_to_message` scrolls+flashes, toasts if not loaded). Optimistic echo now covers *every* send (`_render_pending` renders attached images from memory via `_local_image_widget`). Memory-safe image decode: `_thumb_texture`/`_texture_from_bytes` downscale *during* decode via the loader `size-prepared` signal (fixes OOM on huge images), raise `ValueError` on undecodable payload.
- **Graph**: `_strip_reply_placeholder`, `_parse_message_reference`, `_split_attachments`; `_chat_message_row`/`_channel_message_row` return `reply_to` + cleaned body.
- **Teams** (`teams_view.py`): `_message_block` renders the same `_reply_quote` for channel posts/replies.
- **Cross-tab sweep**: escape NavigationPage/StatusPage titles (Mail/Calendar/Files); `.get("id")` instead of hard subscripts (folder/event/delete `KeyError` fixes); `"Group.Read" in str(error)` (was `TypeError`); right-click `Popover` `unparent()` on `closed` (leak); command-palette Down/Tab + Up/Shift+Tab wrap; added `.cloudy-bubble-image` rule.
- **Cleanup pass**: removed dead code (`message_view.make_message_page`, `chat_view._initials`/`_QUICK_REACTIONS`, `RichTextEditor.is_empty`, `graph.site_by_path`, `send_chat_image`/`unset_reaction`, `window._account_menu_button`, `interfaces.CAPABILITY_KEYS`, `dashboard._pretty_day`, `mounts.authorize_onedrive`/`create_onedrive_remote`; `Account.is_business` kept — covered by unit suite); popover leaks fixed; Dashboard mail/calendar fetches split into separate try-blocks so one scope error doesn't blank the account overview.

### Command palette + offline cache + unit suite (2026-06-16)
- **Command palette** (`widgets/command_palette.py`): `Adw.Dialog` via `command-palette` action (Ctrl+K), lists signed-in accounts' visible surfaces + app actions, type-filter, ↑/↓/Tab nav, Enter activates. Imports `CAPABILITY_UI` from `window` *inside* `_build_entries` (keeps module headless-importable).
- **Persistent offline cache** (`core/cache.py`): `MemoryCache(ttl, path=…)` persists JSON-serializable entries to `~/.cache/cloudy/cache.json` (atomic, throttled ~5s, `flush()` in `do_shutdown`); non-serializable values stay in-memory. Disk entries are **backdated past the TTL** so they read as stale — instant offline render, then revalidate. No view changes needed.
- **Headless logic test suite** (`tests/unit/`): 69 `unittest` tests over the pure/logic layer (cache, Account roundtrip, pin/mute/scope helpers, Google/Graph normalization + id-routing, notifier gating/quiet-hours/digest, mount helpers, `capabilities_of`, `esc`). `make test` / `make test-unit`. `gi_setup.py` pins GI; `fakes.py` provides Fake App/Settings/Registry/Window.

### Google parity — multi-calendar + Drive sources (2026-06-16)
- **Multi-calendar** (`google_client.list_events`): aggregates every shown calendar (calendarList `selected`/`primary`) in parallel (`ThreadPoolExecutor` ≤8); a bad calendar returns `[]` without sinking the agenda. Id routing mirrors Graph: non-primary ids wrapped `gcal\x1f<calId>\x1f<eventId>` (`_wrap_event_id`/`_unwrap_event_id`); get/update/delete hit `/calendars/<calId>/events/<id>`. Read-only calendars (holidays/birthdays) return 403 → toast. Each event carries `calendar` name + `color`. Create still targets `primary`.
- **Drive sources** (`files_view.py`, `mounts.py`): Files lists My Drive + Shared with me (instant) + Shared Drives (enumerated off-thread via rclone — see "deliberately NOT verified" above). Mount opts branch on `drive.kind`: `google_shared_with_me`→`shared_with_me=true`, `google_shared_drive`→`team_drive=<id>`.

### Notifications digest batching + Preferences split (2026-06-16)
- **`notify-level` value `digest`** (`all`|`digest`|`priority`): `digest` = tier-1 immediate + tier-2 batched. Per-account pending buffer `self._digest`; `_flush_digest` on a 600s timer builds one LOW summary per account ("3 new messages in 2 chats · 2 new emails", `ngettext` plurals), holds the queue while `_focus_active()` and releases when focus clears (nothing dropped). Digest summary banners carry empty id → `application.py` falls back to `open_account_tab(account, "chat"|"mail")`.
- **Preferences split**: new **Notifications** tab (Alerts + Quiet hours groups); General keeps a slimmed Background group.

### Notifications P1 + chat status polish (2026-06-15)
- Research-driven (CSCW/HCI: *abstract beats full beats none*; *presence ≠ availability*; gate delivery, don't add signals).
- **System DND + quiet hours**: `_focus_active()` = system DND (GNOME `org.gnome.desktop.notifications` `show-banners`, schema-guarded/cached, degrades to "not DND") **or** a nightly HH:MM quiet window (wraps midnight via lexical compare on zero-padded times).
- **Relevance tiers**: tier-1 (1:1 chat / important mail / reminder → HIGH) vs tier-2 (group chat / ordinary mail → NORMAL); `_allowed(tier)` gates the **banner only** (badges/unread always update). `notify-level` `all`|`priority`.
- **Per-chat/-channel mute**: `Account.muted_sources` (added to `from_dict` allowlist) + `is_muted`/`toggle_mute`; bell toggle in Chat/Teams headers; muted ⇒ no banner and no badge.
- New GSettings keys (need `make build`): `notify-level`, `notify-respect-system-dnd`, `quiet-hours-enabled`, `quiet-hours-start`, `quiet-hours-end`.
- **Chat bubble polish**: fluid send (optimistic echo widget is **adopted** under the real id on confirm, no rebuild/image-reload); single delivery indicator (clock→check, no eye); reactions as pills below the bubble.

### Dashboard Activity + chat/notes fixes (2026-06-15)
- Dashboard **Activity** feed (work MS accounts): Team channels (latest post per starred channel) + Chats (recent + starred floated). Channel rows → Teams tab, chat rows → `open_chat`. **Pinned** section now only collects mail/calendar pins.
- **Star channels & chats**: `toggle_pin(**extra)` (channel pin carries `team_id`/`team_name`); kinds `"channel"`/`"chat"`.
- Chat images no longer reload on every send/receive (`_image_cache` by URL, reset on chat switch, reused via `_picture_for`).
- OneNote crash hardening: `_render_note_body` dropped `Adw.Clamp` (full-width) and splits text blocks over `_MAX_LABEL_CHARS` (12k) across labels so one paragraph can't overrun the GL texture ceiling (`gsk_gpu_upload_cairo_op` segfault).
- Gmail folder dropdown moved to its own full-width bar below the header (was the capped HeaderBar title widget).

### Teams tab — channels + OneNote (2026-06-15, v0.2.1)
- New top-level **Teams** capability, MS work/school only. `TeamsCapability` wired like the others. New scopes: `SCOPES_CHANNELS` (`Channel.ReadBasic.All`, `ChannelMessage.Read.All`, `ChannelMessage.Send` — **tenant-admin consent**) + `SCOPES_NOTES` (`Notes.ReadWrite.All`, `Notes.Create`).
- Graph: `list_joined_teams` (id+name, **not** the file-mount `list_teams`), `list_team_channels`, `list_channel_messages_page` (`$expand=replies`), send/reply channel; OneNote against the **group** notebook (`/groups/{teamId}/onenote/…`): list/get/create/update + `fetch_note_image` (bearer-auth).
- `teams_view.py`: `Adw.NavigationSplitView`, channel content = inner ViewStack of Conversation + Notes. **Notes rendering is NATIVE, not WebKit** (a full-page WebView snapshot overran the GPU texture limit and segfaulted in `gsk_gpu_upload_cairo_op` on GTK 4.22 / Intel-Mesa / Wayland even with `WEBKIT_DISABLE_DMABUF_RENDERER=1`); `_render_note_body` walks HTML splitting `<img>` from text.
- CI: manifest no longer builds `blueprint-compiler` from gitlab.gnome.org (SDK 48+ bundles it; a 503 there broke the 0.2.0 build).

### Chat scroll smoothness + animations (2026-06-15)
- **Incremental thread updates**: `_render_thread` keeps per-message fingerprints (`_rendered_sigs`/`_msg_sig`); an unchanged prefix → append only (`_appended_only`), full render only for edits/reactions/deletes — stops the thread flickering and re-downloading every inline image every 5s. Un-acked optimistic echo forces a full rebuild (`_has_optimistic`).
- **Scroll state derived from the adjustment** via `value-changed` (`_on_thread_scrolled`), *replacing* `EventControllerScroll` — wheel/trackpad/scrollbar-drag/keyboard all update pinned state identically; programmatic moves go through `_set_scroll` with an `_adjusting` guard; `changed` re-pins on height change.
- **Per-frame position hold** (`_hold_position`, `add_tick_callback` over ~350ms) replaces the one-shot idle re-pin — collapses multi-jump when loading older history; `_on_older` updates `_rendered_sigs`/`_thread_sig` so a later poll takes the cheap append path.
- Animated "jump to latest" (`Adw.TimedAnimation`, EASE_OUT_CUBIC 250ms); new-bubble fade-in (`.cloudy-bubble-new`, 220ms, removed after play). Caches (list/thread/members/contacts) on `app.cache`, 90s SWR; clients paginate (`$top=50` chats, `$top=30` messages) and batch (presence, contacts).

### Chat capability + Mail/Calendar/shell UX (2026-06-15)
- **New Chat capability** (`chat_view.py`, `chat_compose.py`), 4th alongside Files/Mail/Calendar. Teams chat = work/school MS (delegated `Chat.ReadWrite`); Google Chat = Workspace-only (degrades for consumer Gmail). Tab + Teams/Shared mail sources hidden for personal accounts (`Account.is_personal` email-domain heuristic).
- Chat list: 1:1/group/meeting; unread = bold + accent dot (Teams `viewpoint.lastMessageReadDateTime`, never when `from_me` or marker missing — was a false-unread bug); "You:" prefix; conversational dates; pagination; name filter + server-side `/search/query` message search.
- Thread: bubbles, older-message pagination, pins to bottom on open; empty/system messages skipped.
- Compose: Enter sends (no button); attach (paperclip) + Ctrl+V paste stage thumbnails; images as Teams **inline hosted content** (base64); `@mentions` build HTML `<at id>` tags + `mentions[]`.
- Per-message right-click: reaction (`setReaction`), Reply (inline quote), Forward (new chat prefilled), Copy, Select (multi-select), Download, Copy link, Edit/Delete (own). Inline images downloaded with bearer token (hosted-content URLs 401 on plain open), downscaled to 240px (scale the *pixbuf*, not `set_size_request`); relative `../hostedContents/..` resolved to absolute.
- New chat (`ChatComposeWindow`, an `EditorWindow`): To autocomplete; `start_chat` creates/reuses 1:1. Group chat: `start_group_chat`, header people-button roster popover to rename/add/remove. Presence dots (`POST /communications/getPresencesByUserId`, `Presence.Read`, 60s batch, patched in place).
- Notifications: chat polled (`_poll_chat`), popup + deep-link `app.notify-open-chat` → `window.open_chat`; sidebar red chat-unread badge (`_chat_unread`/`chat_unread_count`/`mark_chat_read`).
- Client chat methods (Graph + Google, same shapes): `list_chats[_page]`, `list_chat_messages[_page]`, `list_chat_members`, `send_chat_message`/`_images`/`_html`, `fetch_bytes`, `edit`/`delete_chat_message`, `set_reaction`, `start_chat`, `search_messages`. Google raises `GoogleError` for Teams-only writes. `SCOPES_CHAT` added (`Chat.ReadWrite` / Workspace chat scopes).
- **Mail/Calendar/shell**: per-account sidebar unread badges (mail accent pill via `inbox_unread()` + red chat pill); mail pagination + Unread virtual folder + per-account folder memory + multi-select/arrow-nav/search; calendar "● Live now" + multi-select/arrow-nav/search + event-id deep-link fix; teal accent `#2190a4` at APPLICATION priority.

### Inline event edit + refactor + bug audit (2026-06-15, earlier)
- **Inline event edit**: Edit (✏️) toggles `EventDetailWindow` detail into an inline form (subject, all-day, day, start/end, location, removable attendees, description) → `client.update_event`. `get_event` returns attendees with `email`. Description prefills plain-text bodies only (HTML-bodied start blank). `EventWindow` still used for New.
- Refactor: shared event date/time helpers → `widgets/event_time.py` (`iso_to_local_naive`, `parse_hhmm`, `local_to_utc_iso`).
- Bug fixes: `update_event` treats `attendees=None` as "leave" but a list (incl `[]`) as "set" (clearing attendees now works); inline multi-day events keep their day span (`_edit_day_span`); `notifications.py` prime timer is now a one-shot `_prime_once` returning `False` (was sharing `_tick` → fired forever, doubling poll traffic).

### Rebrand, packaging, calendar redesign (2026-06-14)
- **App-ID rebrand**: `com.fiberelements.Cloudy` → `io.github.sha5b.Cloudy` (data files, schema id, D-Bus name/paths, icons, gresource prefix, Makefile); author → Shahab Nedaei (sha5b). Best-effort dconf migration in `application._migrate_legacy_settings` (works on host/RPM; Flatpak runtime has no `dconf` CLI → re-add accounts there once).
- **Packaging**: Fedora RPM (`packaging/cloudy.spec`, noarch, meson) `make rpm`/`srpm`; Flatpak bundle `make flatpak-bundle`; `make release` → `release/` (RPM + single-file `.flatpak`) and installs the bundle. Credentials baked at build via `meson.options` → generated GSettings vendor override (`data/cloudy.gschema.override.in`), read from `.env`; source ships empty defaults; no secrets in git.
- **Host-visible Flatpak mounts**: rclone runs on the host via `flatpak-spawn --host` into `~/.local/share/cloudy/mounts` (manifest grants `--talk-name=org.freedesktop.Flatpak` + `--filesystem`); `mounts.active_mounts()` reads `/proc/self/mountinfo` (stall-proof; fixed the dashboard hang).
- **Nautilus**: quick Unmount menu item (file view, not sidebar — API can't touch sidebar bookmarks); extension auto-installs (RPM → system path; Flatpak → copied to host on first run via `provisioner.ensure_host_nautilus_extension`).
- **Calendar redesign**: month grid (`widgets/month_grid.py`) in Calendar tab + Dashboard; past events; clicking an event opens a non-modal `event_window.py` (detail + RSVP + delete + Edit). Meeting attendee response tracker (pills grouped by status via `Adw.WrapBox`).
- **Per-account mountpoints** (`mounts.mount_base_for`): each account's drives mount under their own folder so same-named drives don't collide; D-Bus status walks ancestors for the nesting.
- Dashboard cached (SWR) + Refresh; contacts autocomplete (MS People API `/me/people`+`/me/contacts` + `People.Read`; Google connections + otherContacts + `contacts.other.readonly` — both need re-sign-in); account add auto-enables the provider's module; WebKit blank-mail fix (`WEBKIT_DISABLE_DMABUF_RENDERER=1`); design system (`data/style.css`, `widgets/metrics.py` 4px scale, `source_nav.status_page`/`loading_box`, `.cloudy-meta`/`-day`/`-chip`/`-pill`/`-section`).

---

## Build / run / test
```bash
make run            # meson build+install into _install, then launch ./_install/bin/cloudy
make build|test|lint|clean
make test-unit      # just the headless logic suite (fast, no build/schema)
make flatpak flatpak-run   # sandboxed (org.gnome.Platform 50)
make release        # builds RPM + Flatpak bundle and reinstalls the bundle
```
- Dev toolchain is **user-space**: `meson`/`ninja` via `pip --user` (`export PATH="$HOME/.local/bin:$PATH"`); `blueprint-compiler` auto-fetched via the wrap; `msal` via `pip --user`. `rclone` auto-provisioned (rootless) into `~/.local/share/cloudy/bin/rclone` on first run; also bundled in the Flatpak.
- `meson test` runs 4–5 validation tests (desktop/schema/metainfo/blueprint) + the unit suite.

## Credentials (set up locally; repo is public-safe)
- **Microsoft**: multi-tenant Entra public client ID `dcd8ee18-6e62-4c5a-b01f-86f9556f8fed` (not a secret). **Google**: Desktop OAuth client.
- Real values live outside git in `.env` (gitignored) and/or `~/.config/cloudy/secrets.env`, loaded into `CLOUDY_*` env on startup by `core/credentials.py`. `.env.example` is the template (see `docs/SECRETS.md`). Env vars `CLOUDY_MS_CLIENT_ID`, `CLOUDY_GOOGLE_CLIENT_ID`, `CLOUDY_GOOGLE_CLIENT_SECRET` override the matching GSettings keys.

## How it works (key decisions)
- **Auth**: system browser + loopback. MS = MSAL (`core/auth/msal_graph.py`); Google = loopback+PKCE on urllib (`core/auth/google_oauth.py`). Tokens in libsecret (`core/secrets.py`). Sign-in requests **all** scopes up front (Files+Teams+Channels+Notes+Groups+Mail+Calendar+Chat+`Mail.ReadWrite.Shared`+People) so one consent covers everything.
- **Shared/group sources** (MS): `list_shared_folders`/`list_shared_events` use `/users/{address}` + `Mail.ReadWrite.Shared`; group mail/calendars use `/groups/{id}` + `Group.Read.All`. IDs prefixed `shared:<addr>:` / `group:<id>:` so `get_message`/`get_event` route back. Mail & Calendar share the Me/Teams/Shared model; scaffolding in `widgets/source_nav.py` (`SourceTabs`, `run_async`, `clear_listbox`, placeholders, `is_scope_error`, `present_add_shared_dialog`, pin helpers).
- **Files = rclone mounts** (`modules/microsoft365/mounts.py`): rclone does its own browser auth (built-in app id), reused per account. Mount → live two-way FUSE network drive + GTK sidebar bookmark → appears in Nautilus. **Mount layout** (`mount-layout`): `one-folder` (global mount location) or `individual` (per-account `Account.mount_location`); `mountpoint_for`/`mount` take an optional `base`, `account_mount_base(loc)` resolves it.
- **In-app browser** (`widgets/file_browser.py`): Files tab is an `Adw.NavigationView` — Libraries (mount toggles) at root; a mounted library row pushes a `FileBrowserPage`. Listing off-thread; `recent_changes(roots)` (bounded) powers the Dashboard.
- **Offline sync** (`core/sync.py`): `default-sync-type=full` + per-account toggle → `rclone bisync` into `…/cloudy/synced` on a timer. `stream` disables the per-account toggle (mounting stays manual); auto-mount-on-login not built.
- **Caching**: `core/cache.py` MemoryCache on `app.cache` (SWR, 90s TTL, persisted to disk); Refresh invalidates per account.
- **Nautilus**: D-Bus status service (`core/dbus_service.py`); host extension draws emblems + menu (`make install-nautilus`).
- **UI shell** (`window.py`): sidebar (Overview + accounts) → per-account `ViewSwitcher` over the capability tabs; `open_mail(account, mid)` / `open_account_tab(account, tab)` deep-link entry points. A turned-off account shows "Turned off".
- **Application lifecycle** (`application.py`): `CloudyApplication(Adw.Application)` with `HANDLES_OPEN` (system Mail/Calendar handler): `do_open` routes `mailto:` → `ComposeWindow`, `.ics` → `EventWindow`. Owns registry, cache, engine, secrets, sync_manager, notifier. Background mode (`run-in-background`, default off): close-request hides + `app.hold()`.
- **Preferences** (`preferences.py`): **General** (Mount location · layout · caching · sync type · Start at login + slimmed Background group), **Notifications** (Alerts + Quiet hours), **Accounts** (per-account `ExpanderRow`: services on/off = `enabled-modules`, Sign In/Out, Remove; Sync files offline + Mount location).
- **Pinning ("star")**: ★ in Mail/Calendar/Teams/Chat toggles `Account.pinned_sources` `{kind, source, id, name, **extra}`; Dashboard renders them.

## Layout
`src/cloudy/{main,application,window,preferences,account_dialog}.py`; `core/` (interfaces, plugin_engine, account_registry, secrets, cache, credentials, provisioner, dbus_service, sync, notifications, ics, eds_publish, gi_compat, auth/); `modules/microsoft365/` (graph, files, mounts, abraunegg); `modules/gmail/` (google_client); `widgets/` (files/mail/calendar/dashboard/message/event/chat/teams/activity/media views, source_nav, file_browser, clients, editor_window, format, event_time, command_palette, month_grid, metrics). Data in `data/` (gschema, desktop, metainfo, blueprints, icons); Flatpak manifest `io.github.sha5b.Cloudy.yml`.
