<!--
SPDX-License-Identifier: GPL-3.0-or-later
SPDX-FileCopyrightText: 2026 Shahab Nedaei
-->

# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added (second wave)
- **Teams channel notifications**: starred channels are polled for new posts
  (watermarked, muted channels respected) — a banner deep-links to the Teams
  tab via a new `app.notify-open-teams` action, and the tab carries a new-post
  badge that clears when you read the channel.
- **Chat**: search hits jump to the message; "(edited)" markers; reactions can
  be toggled off.
- **Privacy**: the mail reader blocks remote images by default with a
  per-message "Load images" banner (cid:/data: images always render).
- **Auth**: Google sign-in enforces a per-flow OAuth `state` (mismatches get a
  403 and never exchange a code), and concurrent sign-ins can no longer
  cross-contaminate their redirect results.

### Fixed (second wave)
- **Mail live refresh keeps pagination**: page 1 merges into the loaded list
  ("Load older" pages, cursor, selection and scroll all survive) instead of
  collapsing back to page 1.
- **Calendar multi-day events show their full span** ("30 Aug – 2 Sep · All
  day", "30 Aug, 15:00 – 31 Aug, 09:00") in event details and invite cards,
  with provider-correct end-date handling; single-day output unchanged.
- **OneNote drafts survive navigation**: the page editor moved from the
  fragile inline swap into a non-modal window; failed saves keep the draft
  for retry, and saving refreshes the section only if still on screen.
- **Dashboard channel snippets** no longer surface system events ("Call
  started") as the latest post, and only genuinely-new posts count.

### Added (safe test harness)
- **Headless UI sweep suite** (`tests/unit/test_chat_sweep.py`,
  `test_mount_sweep.py`): drives the real Chat and Files views against a fake
  Teams client injected into the app's client cache and a simulated mount
  snapshot — no network, no real data, nothing to break. Exercises list
  kinds/sections/presence/unread, all message shapes (links, quotes, images,
  reactions, system events, tombstones), send/adopt/retry flows, pagination,
  read-ack gating, scroll-state derivation, search cleanup, and render-time
  ceilings (freeze guards). It immediately caught and fixed two more bugs: a
  permanent "Loading chats…" placeholder row after a cold cache, and messages
  delivered to a hidden tab never being marked read once it was shown again.

### Fixed (full-app audit fix pass — chat, calendar, mail, Teams, dashboard, mounts)
- **Startup mounting is parallel and stops re-writing rclone config.** The
  auto-remount ran one drive at a time and re-ran `rclone config create` for
  every remembered drive on every boot; remote creation is now serial-but-
  skip-when-present and the mounts themselves run in parallel (up to 4), the
  mount-table poll cadence was halved, and the first offline bisync waits ~90 s
  so it stops competing with the remount burst.
- **Bookmark reconciliation adopts scoped remotes.** The healing pass checked
  only the legacy unscoped remote name, so since remotes became account-scoped
  it deleted bookmarks it should have adopted.
- **Share links resolve through the remembered mount base.** The fallback path
  guessed the account folder from the account *id* (real folders use the
  display name) and never matched, silently routing links at the wrong drive;
  records now remember their base, and resolution no longer `resolve()`s
  through the FUSE mount.
- **Chat: an in-flight message stays in its own conversation.** The optimistic
  "sending…" echo carried no chat id, so switching chats mid-send could append
  it (and its Retry button) to the wrong thread forever; failed bubbles were
  filed under whichever chat was open at callback time.
- **Chat: rich sends no longer duplicate.** Echo adoption compared raw composer
  text against the HTML-stripped server copy — never equal for markdown,
  @mentions or multi-line+image sends, leaving two bubbles. Adoption now
  matches the message id returned by the send.
- **Chat: composer drafts and staged attachments no longer leak into the next
  conversation** (previously they were silently sent to whichever chat you
  switched to).
- **Chat: messages are no longer marked read while the tab is hidden.** The
  open-chat poll acked arrivals on focus alone, reading-and-clearing messages
  on every device that were never displayed.
- **Chat: an underscore no longer breaks sending.** The italic regex matched
  `snake_case` and URLs, mangling them on Microsoft and failing outright on
  Google; Google text-only sends now always take the plain path with a
  fallback. Background refreshes also keep paged-in conversations instead of
  collapsing the list to page 1, deleted-message tombstones actually render,
  and stale search rows no longer linger under new results.
- **Calendar: meeting-invite emails open again.** `startDateTime` on an
  eventMessage is a string, not a dateTimeTimeZone dict — the invite-card
  builder crashed every meetingRequest/meetingCancelled mail.
- **Calendar: saving an event no longer drifts its time by the DST delta.**
  The editor converted local→UTC with *today's* offset; it now resolves the
  local IANA zone for the target date. Cross-zone `.ics` invites keep their
  TZID (converted to your zone), month fetches pad a day so edge-of-grid
  events aren't dropped, multi-day events appear on every day they span, and
  Google event times are normalized to local (display, sorting, "Live now").
- **Calendar: optional attendees stay optional.** Every editor save re-sent
  the whole list as "required"; types are now carried through round-trips.
  RSVPing (from the event window or a mail invite) refreshes the pending-
  invite badge immediately, and the EDS mirror writes an exclusive all-day
  DTEND for both providers.
- **Mail: attachments open from the pop-out reader** (the window had no
  application, so every chip click errored).
- **Mail: timestamps render in local time** (rows and the reader sliced the
  raw UTC string); reading mail in the Inbox decrements the sidebar badge
  (the guard compared against a literal id the opaque Graph inbox never had);
  folder-load failures surface an error/re-sign-in instead of a blank Unread
  view; composer attach and attachment saves moved off the GTK thread
  (downloading from a mount froze the UI); inline-image-only drafts are no
  longer deleted after sending a flattened copy; re-saving a resumed draft
  updates it instead of duplicating.
- **Teams: forwarded posts render their quoted original** (previously an empty
  card), channel images authenticate with channel scopes, the poll picks up
  edits/reactions/attachments, a failed post restores the typed text instead
  of discarding it, scrolled-up reading survives re-renders, inline images are
  cached per channel, older posts paginate, system events and deleted messages
  render, and OneNote title edits are actually saved.
- **Dashboard: file-change rows open asynchronously** (the synchronous launch
  on FUSE paths could freeze the app); the Files upload indicator works (it
  queried the unscoped remote name, so "Uploading…" could never appear); the
  Unread stat uses the server-side total it already fetched; cross-provider
  event sorting/today-counting parses timestamps instead of comparing mixed
  ISO strings; the Activity badge no longer counts starred channels forever;
  the Activity feed keeps its content on transient refresh errors and renders
  instantly from a cache; and the shared `status_page()` helper escapes its
  text.
- Unit suite grew from 153 to 256 tests (chat logic, Teams channels, calendar
  spans/DST/ics/EDS, mail `_ical`/timestamps/drafts, dashboard ordering).

### Fixed (mount-state snapshot refactor)
- **Mount state is read once and shared, not re-probed constantly.** The Files
  tab, the Dashboard, the Nautilus D-Bus service and the remount watchdog each
  probed the system for themselves — the Files tab on every open and close, the
  sync poll every 3 seconds, the watchdog once per remembered drive. In Flatpak
  every one of those is a `flatpak-spawn --host` round-trip, so a few drives
  turned into a steady stream of host processes. There is now one shared
  snapshot (`core/mount_state.py`) refreshed only when something can have
  changed: at startup, on kernel mount events (`Gio.UnixMountMonitor`), right
  after Cloudy mounts or unmounts, and on a slow fallback tick. Readers get it
  from memory and never block.
- **A dead mount is cleared the moment it is noticed.** When an rclone daemon
  dies its mountpoint stays in the mount table, and every `stat()` on it then
  hangs in the kernel. Cloudy also puts each mountpoint in the GTK bookmarks
  file, so the file manager sidebar, every GTK file chooser and Shell search
  all stat it — one dead daemon froze the desktop's file handling. Each
  snapshot refresh now detaches stale Cloudy mountpoints, instead of waiting up
  to 90 seconds for the watchdog.
- **Unmounting removes the sidebar bookmark before anything else can stat it**,
  and both `fusermount` attempts have timeouts.
- **Account switching no longer builds every tab.** One switch used to create
  all five views and fire about six concurrent network loads for tabs nobody
  was looking at — including a Files tab that re-enumerated libraries. Each tab
  is now built the first time it is shown.
- **Huge folders no longer lock the browser.** A folder is rendered up to a cap
  with an honest "N more not shown" note; before, every entry became a widget
  on the GTK thread.
- **Expanding a folder inline no longer races navigation**: a subfolder scan
  that lands after the user has moved on is dropped instead of writing into the
  new folder's state.
- **The mount cache has a ceiling** (`cache-max-size`, default 10G). With
  `--vfs-cache-mode full` and a 72h retention it could otherwise grow until the
  disk was full, which stalls the whole desktop.
- **Nautilus extension**: dropped the `tip` argument from its menu items.
  nautilus-python removed that property, so passing it raised and took the
  whole Cloudy menu with it.

### Changed
- The remount watchdog runs every 5 minutes instead of every 90 seconds, and
  costs one pair of `/proc` reads instead of one `ps` per remembered drive. The
  mount monitor now covers the cases the watchdog used to poll for.

## [0.3.4] - 2026-08-04

Full-app stability audit: file-browser freezes, chat/badge correctness, and
threading races across the app.

### Fixed
- **Files no longer freeze the app**: opening a file, the "Open in system
  Files" button, and Rename/New-folder confirmation all touched the FUSE mount
  on the GTK thread — on a slow or hung mount this locked the whole UI
  (forever, if the rclone daemon had died). File opens are now async, the
  destination checks are pure string logic, and saves/attachment reads in Chat
  run off-thread too.
- **Flatpak Files tab no longer stalls on tab switch**: every mount-state
  check spawned a synchronous `flatpak-spawn --host` round-trip per drive on
  the main thread. The mount table is now cached briefly and refreshed
  off-thread; the upload-status poll filters mounted drives in the worker.
- **Dead mounts are detected instead of hanging "Loading…"**: opening a
  library now checks the mount daemon is alive first (a hung mount shows
  "isn't responding — unmount and mount it again"); the Dashboard's
  recent-files scan only walks mountpoints with a live daemon.
- **Chat unread is finally coherent**: a chat opened once can now show unread
  again (read state is a message-time watermark, not a permanent flag); the
  badge no longer lights up—and sticks—for the conversation you're actively
  reading (the notifier knows which chat is on screen, and new messages in the
  open focused chat advance the server read marker); deleted-message
  tombstones from 0.3.3 actually render (they were filtered out as empty).
- **Failed sends survive**: a failed chat message (Retry button + attached
  images) is no longer silently destroyed when a background poll re-renders
  the thread.
- **Badges update while a composer window is focused**: notifier pushes went
  through the *focused* window, so unread counts silently froze while writing
  mail in the non-modal composer.
- **Teams channels**: a poll re-render no longer wipes in-progress reply text
  or yanks the scroll position; names/subjects with "&" no longer display
  double-escaped; a post with malformed formatting falls back to plain text
  instead of rendering blank.
- **Google**: one deleted message no longer blanks the whole Gmail page;
  Google Chat activity can badge (spaces expose their last-active time);
  concurrent token refreshes no longer race (which could lose a rotated
  refresh token and force a re-sign-in); Graph chat actions work for
  addresses containing an apostrophe.
- **Notifier**: polls no longer stack up under provider throttling (one
  in-flight poll per account and kind); a shutdown no longer removes an
  already-fired timer id; the Dashboard shows an error state instead of
  spinning forever when the first load fails, and its Unread stat uses the
  server-side count instead of the first page.
- **EDS calendar mirror**: concurrent publishes no longer corrupt the uid
  index; enabling the toggle no longer freezes the app; startup no longer
  re-runs the legacy dconf migration every launch.
- Escaped account names in the "turned off" page and sign-in placeholder
  (Pango markup).

## [0.3.3] - 2026-07-14

### Added
- **Chat status events**: Teams system messages now render as centered status
  lines instead of being silently dropped — "X added Y" (including the
  Teams-style "shared chat history from the past N days" suffix), "X removed
  Y", "Y left the chat", chat renames, and call started/ended.
- **Deleted-message tombstones**: a deleted chat message keeps its place in
  the thread as a dim "X deleted this message" placeholder instead of
  vanishing (which silently reshuffled the history).

### Fixed
- Status rows and tombstones offer no reply/edit/delete/selection actions —
  they're events, not messages.

## [0.3.2] - 2026-07-14

Meeting invitations become first-class, plus fixes from a full audit of the
Microsoft Graph and Google API clients.

### Added
- **Meeting-invite cards for Microsoft mail**: an Outlook meeting invitation
  (Graph `eventMessage`) now renders the same invite card Gmail `.ics` invites
  get — when/where, Join link, Accept / Tentative / Decline — via the
  documented `$expand=microsoft.graph.eventMessage/event` follow-up. Invite
  rows carry a calendar icon in the message list.
- **Pending-invitation badge + notification**: the notifier sweeps the next
  two weeks for events you haven't answered, badges the Calendar tab with the
  count, and raises a "You're invited" banner (tier 1) for new invitations.
- **Nested Outlook mail folders**: the folder list now walks `childFolders`
  recursively (flattened as "Inbox / Projects / …") — subfolders used to be
  invisible.
- **Secondary personal calendars**: the Me source merges every other owned or
  shared-in calendar with the default `calendarView` (best-effort per
  calendar, bounded fan-out).
- **Large mail attachments**: attachments that would push a request over
  Graph's ~4 MB cap are sent through `createUploadSession` chunked uploads
  (send, reply and draft paths); mixed batches split automatically.
- **Native forward for Microsoft mail**: forwards go through Graph's
  `forward`/`createForward` action, so the original HTML body, inline images
  and attachments survive (the client-built forward flattened to plain text).

### Fixed
- **Nautilus extension froze the file manager**: `ManagedRoots` was fetched
  with a *synchronous* D-Bus call (up to 1.5 s) on Nautilus's UI thread every
  time the 30 s cache expired, and the proxy was created synchronously. Menu
  hooks now always return the cached snapshot immediately; the proxy and the
  roots refresh are fully async (`DO_NOT_LOAD_PROPERTIES` /
  `DO_NOT_CONNECT_SIGNALS`).
- **Invite RSVP now reaches the organizer's tracking**: answering a Microsoft
  invite email uses `/me/events/{id}/accept|tentativelyAccept|decline` with
  `sendResponse: true` on the auto-staged event. The old hand-built iMIP
  `.ics` file attachment (which Exchange organizers don't auto-process, sent
  with `send=False`) remains only as the fallback for plain `.ics` invites.
- **Replies dropped the quoted conversation**: the reply action now passes the
  new text as `comment` — setting `message.body` replaced Exchange's reply
  draft (which is where the quoted thread lives), so recipients got
  context-free replies.
- **Group-conversation replies always 403'd**: `Group.Read.All` →
  `Group.ReadWrite.All` (the `threads/{id}/reply` action requires write).
  Existing accounts must Sign Out → Sign In once.
- **Files sent in Teams chats were unopenable**: the reference attachment
  pointed into the sender's OneDrive with no permission grant, so every
  recipient got 403. Cloudy now invites the chat's members to the file (org
  view-link fallback), mirroring what Teams itself does.
- **Google — calendar list could never load**: the requested
  `calendar.events` scope isn't accepted by `calendarList.list`, so listing
  calendars always 403'd and multi-calendar aggregation silently fell back to
  primary-only. Added `calendar.calendarlist.readonly` (re-sign-in to pick it
  up).
- **Google Chat — editing a message never worked**: the required
  `updateMask=text` parameter was missing (guaranteed 400).
- **Google Chat — own messages rendered as someone else's**: `is_mine` was
  hard-coded `False`; it now compares `sender.name` against the OIDC `sub`
  (`users/<sub>`), restoring right-side bubbles and edit/delete of own
  messages.
- **Google mail bodies in non-UTF-8 charsets** (ISO-8859-1, windows-1252, …)
  rendered as mojibake: bodies are now decoded with the part's declared
  charset.
- **Google calendar months were truncated**: `list_events` now follows
  `nextPageToken` per calendar instead of stopping at the first page (50
  items).
- **Google sign-in aged out needlessly**: a rotated `refresh_token` in a
  refresh response was dropped instead of persisted.
- **Google granular-consent 403s** ("insufficient authentication scopes") now
  trigger the inline "Re-sign in" recovery instead of reading like an error.

## [0.3.0] - 2026-07-10

### Added
- **Group chats**: the New chat composer now autocompletes and accepts several
  recipients (pick one, keep typing the next) and starts a group chat when you
  add 2+ people. In an existing 1:1, an **Add people** button starts a fresh
  group chat with the existing person plus whoever you add — Graph can't add a
  member to a 1:1, so this mirrors Teams' behaviour.
- **Live file-sync status**: Files view rows show `↑ Uploading N…` / `Synced` /
  `⚠ N didn't upload`, read from each rclone mount's VFS stats over an `--rc`
  socket and polled while a queue drains.
- **rclone daemon logging**: every mount logs to
  `~/.local/share/cloudy/logs/rclone-<account>-<drive>.log` (rotated at 5 MB),
  so upload problems are no longer invisible.
- **Undo for file operations**: Move to Trash, Rename, Move and Copy (and
  drag-drop copies) now show an **Undo** button in the toast that reverses the
  action (trash-restore is best-effort on network mounts).
- **Drag and drop in the file browser**: drag files out to other apps
  (Nautilus, mail, browser) as real files, and drop files in to copy them into
  the current folder (i.e. upload into a mount).
- **Jump to a forwarded message's original**: clicking a "Forwarded from …"
  quote scrolls to the original message, opening the chat it was forwarded from
  first when that's a different conversation.

### Fixed
- **Chat intermittently failing to render / crashing on a 1:1 chat**: two stale
  references (`self._PRESENCE`, `self._presence_dot`) left behind when presence
  logic moved into `chat_avatar` crashed the header/roster; both now use the
  shared helpers.
- **Forwarded chat messages showing only an "attachment"**: a forwarded Teams
  message carries its content in a `forwardedMessageReference` attachment (not the
  body), so it now renders as a "Forwarded from …" quote instead of a bare file
  chip. A file forwarded alongside still shows as its own chip.
- **Chat scrolling jumping when you react to a message**: reacting swapped the
  bubble in place but left the thread signature stale, so the next background
  poll did a full rebuild that reloaded images and yanked the scroll. The
  in-place update now keeps the signature in step, so the view stays put.
- **A sent file not appearing until you refreshed**: the optimistic "sending"
  bubble only echoed in-memory images and was then reused as-is on confirm, so a
  file attachment never rendered. A confirmed message with an attachment now
  rebuilds its bubble in place, showing the file immediately.
- **A deleted message coming back until you refreshed**: Graph's soft-delete is
  eventually consistent, so the poll briefly re-added it. Locally-deleted
  messages are now hidden until the server stops returning them.
- **Clicking a forward from a chat you're not in stranding you on an error
  page**: it now bounces back to the previous chat with a toast instead.
- **SharePoint/OneDrive Office uploads never reaching the server**: OneDrive for
  Business rewrites `.docx`/`.pptx`/`.xlsx` server-side, so rclone's size/hash
  verify failed, deleted its copy and retried forever. OneDrive mounts now pass
  `--ignore-size --ignore-checksum` (Google keeps its integrity checks).
- **GNOME Calendar showing event times shifted by the local offset** (e.g. a
  15:00 event displayed as 17:00): Graph often returns a Windows zone name the
  mirror couldn't parse and wrongly treated the wall-clock as UTC. It now falls
  back to the system's local zone. The mirror rebuilds on upgrade, correcting
  events already written with the wrong time.
- **GNOME Calendar keeping stale data after edits/deletes**: the background sync
  now re-mirrors the whole current month on each poll, not only when a brand-new
  event appears, so changes made elsewhere (phone/Outlook) reach GNOME.
- **Mounts silently forgotten / bookmarks outliving their mount**: a startup
  reconciliation pass adopts live-but-unremembered mounts and removes stale
  Nautilus bookmarks whose drive is no longer mounted (which otherwise swallowed
  writes into a local stub that never uploaded).
- **Nautilus "Copy share link" copied nothing (you pasted a local path)**: the
  app never wired the share-link handler to the D-Bus service, so it always
  returned an empty string. It now resolves the file's owning account and
  creates a real OneDrive/SharePoint sharing link (Graph `createLink`), off the
  main thread, and the extension waits long enough for the network round-trip.

## [0.2.9] - 2026-07-07

### Added
- **Mail organization**: right-click a message for Mark as unread/read, Flag for
  follow-up, Move to folder and Move to Trash — on both Microsoft 365 and Gmail
  (new `move_message` / `set_flag` client methods).
- **Drafts**: a Save draft button in the composer (`save_draft` on both
  clients), and opening a message in the Drafts folder resumes it in the
  composer; sending deletes the draft.
- **Invite → calendar sync**: answering a meeting invite from Mail now also
  sets the response on the copy Exchange/Google staged on your calendar (looked
  up by iMIP UID via the new `find_event_by_uid`), or creates the event locally
  for external invites. Cancellation mails get a "Remove from calendar" button.

### Fixed
- **Stale calendar/dashboard data** (the "edited event still shows the old
  time" bug): every event and mail write now invalidates the
  stale-while-revalidate caches (`invalidate_cached` helper), the Dashboard
  passes `on_changed` to the event editor, and the background poller drops
  caches when it detects new mail/events.
- **Graph `TimeZoneNotSupportedException: 'CEST'`**: the local timezone for
  `Prefer: outlook.timezone` and event slots is now resolved from the
  `/etc/localtime` symlink to a real IANA name, falling back to `UTC` — never
  an abbreviation.
- **Detail/compose windows loading forever**: the `run_async` liveness guard
  dropped results for toplevel windows (a window never has a parent); it now
  checks `get_root()` instead.
- **"Tried to remove non-child" warning flood**: `patch_listbox` no longer
  removes brand-new rows that were never added.
- **Crash-proofing**: one malformed Gmail header or Graph list item no longer
  breaks the whole folder/month render; `.ics` files opened via the system
  handler now go through the full RFC 5545 parser (folded lines no longer
  truncate titles/descriptions); a corrupt persisted account entry no longer
  prevents startup; a garbled `SEQUENCE:` no longer aborts invite parsing.
- **Shared-calendar RSVP** routed to the wrong mailbox (malformed `/me` path).
- Channel names are escaped before Pango markup in Teams empty states.

### Changed
- **GraphClient split by domain**: `graph.py` (1,847 lines) is now an assembly
  over `graph_http` / `graph_files` / `graph_mail` / `graph_calendar` /
  `graph_chat` / `graph_teams` mixins — same public API, verbatim bodies.
- Consolidated ISO-8601 parsing (`format.parse_iso_utc`), list keyboard
  navigation (`data_rows`/`move_selection`), sender formatting and the event
  slot builders into shared helpers; removed dead code
  (`_refresh_sync_row_sensitivity`, `OneDriveFiles.unmount_drive`).

## [0.2.8] - 2026-06-29

### Added
- Per-account API client cache in `CloudyApplication`, reused across Mail/Calendar/Chat views and evicted on sign-out/removal.
- `patch_listbox()` helper for incremental list updates; Mail and Chat lists now refresh in place instead of rebuilding every row.
- New unit-test coverage for client caching, OneDrive share-link path resolution, RFC 5545 iCalendar parsing, rclone provisioning trust model, and Graph calendar routing/timezones.

### Fixed
- **Microsoft 365 share links**: local paths are resolved back to the correct `(drive_id, relative_path)` via remembered mount records before asking Graph for a share link.
- **Graph calendar timezone handling**: create/update events now send local wall-clock time with the local IANA timezone; `list_events` routes specific/shared/group calendars correctly and requests `Prefer: outlook.timezone`.
- **Graph pagination**: OneNote notebooks/sections/pages now follow `@odata.nextLink`.
- **iCalendar parser**: properly handles RFC 5545 escaping and quoted parameters.
- **Google OAuth redirect receiver**: always shuts down the local HTTP server cleanly.
- **Resource registration on older PyGObject**: the launcher now calls `_register()` when `register()` is unavailable.

### Changed
- **Provisioned rclone trust model**: pinned to `v1.74.3` with hard-coded SHA-256 sums for `amd64`/`arm64` so the download site cannot swap binaries undetected.
- Refactored monoliths: extracted `graph_markup.py`, `file_browser_utils.py`, and `chat_avatar.py`; removed the unused `abraunegg.py` stub.
- Cleaned up verbose/AI-generated docstrings and redundant comments across tests and source.

## [0.2.5] - 2026-06-21

### Added
- **Full sender and recipient details in mail**: the message header now shows the
  sender's complete email address alongside the To, Cc and Bcc recipients. Every
  address is a click-to-copy link, so you can grab someone's address without
  retyping it.
- **Open a message in its own window**: double-click a message in the list to pop
  it out into a separate window and read it side-by-side with the list and other
  mail.

### Fixed
- **Accurate Chat presence**: a contact who went offline could keep a stale green
  / away dot because an authoritative "offline" status was treated like a
  transient unknown and never applied. Offline now correctly clears the dot.

### Changed
- Internal cleanup: shared image-decode and attachment helpers (removing
  duplicated code across the mail, chat, Teams and composer surfaces), plus
  smaller fixes (tenant-id caching, a dashboard refresh guard).

## [0.2.4] - 2026-06-18

### Added
- **Calendar RSVP for everyone**: Accept / Tentative / Decline now works for both
  Microsoft and Google accounts (Google gained `respond_event`, which patches
  your attendee status and notifies the organiser). The event detail shows your
  current answer above the buttons, Teams-style.
- **Unanswered invites are visible**: invites you haven't replied to now appear in
  the agenda and month grid — dimmed with a "needs reply" marker but still
  clickable — instead of being invisible. Declined events are struck through.
- **Reply to meeting invites from Mail**: an invite email shows Accept / Tentative
  / Decline buttons. Answering sends a standards-based `METHOD:REPLY` iMIP
  message back to the organiser (`core/ics.py`), so RSVP works for Google,
  external and forwarded invites — not just ones in your calendar.
- **Activity tab**: a new per-account tab — first in the row and selected on a
  fresh launch — that aggregates recent mail, upcoming and unanswered invites,
  and recent chats into one time-sorted feed. Each row deep-links into its tab.
  For Microsoft it also surfaces Teams-style "X reacted to your message" and
  "X mentioned you" from a bounded scan of your most-recent chats.
- **Read-receipt request**: a toggle in the mail composer asks the recipient's
  client to confirm they opened the message (Microsoft `isReadReceiptRequested`;
  shown only for Microsoft accounts, as consumer Gmail has no equivalent).

### Changed
- **Image viewer**: scroll the wheel to zoom and drag to pan; the toolbar gains
  zoom-in / zoom-out / fit buttons (default stays fit-to-window).
- **Chat galleries**: several images in one message lay out as a wrapping gallery
  rather than a vertical stack.

## [0.2.3] - 2026-06-17

### Added
- **Live chat list**: a new chat message now bumps its conversation to the top
  of the Chat list and lights its unread mark immediately — no manual refresh —
  the same liveness the Mail list already has.
- **Reply quotes in chat & channels**: a replied-to message renders as a compact
  quote (author + snippet) above the reply instead of a bare "attachment". Click
  the quote in a chat to jump to — and briefly flash — the original message.
- **Nautilus integration toggle**: a new switch in Preferences → General turns
  the GNOME Files (Nautilus) extension on or off and installs/removes it on the
  spot.

### Changed
- **Flat chat avatars**: chat avatars use a flat, solid per-person colour from a
  calm 8-colour palette (stable per contact) instead of Adwaita's glossy
  per-name gradient rainbow.
- **Instant scroll on send**: every sent message (including images) now appears
  and scrolls to the bottom immediately via an optimistic echo, instead of
  waiting for the server round-trip.

### Fixed
- **Flat avatars now actually apply**: the override targeted the wrong CSS node
  (Adw.Avatar's colour lives on an internal child gizmo, not the widget the
  class was on), so the gradient rainbow stayed visible. The selector now reaches
  the inner node.
- **Chat presence dots are reliable**: the online/away/busy indicator on 1:1
  chats no longer flickers in and disappear. A transient `PresenceUnknown` from
  the per-chat member fetch could overwrite a freshly-resolved status; presence
  is now merged without downgrading a known value. The dot is also drawn in CSS
  (not a symbolic icon) so it can't go missing in a runtime without that icon,
  and offline/unknown now shows a grey dot.
- **Large images no longer crash the renderer**: chat and OneNote images are now
  downscaled *while* decoding, so a very large picture (a OneNote scan, a
  high-res screenshot) can't exhaust memory or overrun the GPU texture limit.
- **Replies no longer show as a bare "attachment"**: a Teams reply's quoted
  message is parsed and rendered as a quote with its text.
- Mail message titles, calendar/event error pages, and the Files status page no
  longer render blank when the text contains `&`/`<` (proper escaping).
- A mail folder or calendar event missing an `id` no longer crashes folder
  population / multi-delete.
- The command palette's Tab key now wraps through results instead of dead-ending.
- The Files right-click menu no longer leaks a popover per click.

## [0.2.2] - 2026-06-16

### Added
- **Dashboard "Activity" feed** (work/school Microsoft accounts): a new section
  with the latest post from each **starred channel** and your **recent chats**
  (starred chats first), plus a Today preview column and a "New chats" stat card.
- **Star channels and chats**: a ★ button in the Teams (channel) and Chat
  headers pins a conversation; pinned channels/chats surface in the Dashboard
  Activity feed. Mail/calendar pins continue to appear under **Pinned**.
- **Notification attention controls**: honour the system Do Not Disturb state;
  optional **quiet hours**; a relevance level (*Everything* / *Direct now,
  routine in a summary* / *Direct messages & important only*); and **per-chat /
  per-channel mute** (bell toggle in the Chat and Teams headers). Badges always
  update — only the interruptive banner is gated.
- **Batched (digest) notifications**: at the *Direct now, routine in a summary*
  level, direct messages, important mail and calendar reminders still alert
  immediately, while group-chat chatter and ordinary mail are collected into one
  periodic summary banner ("3 new messages in 2 chats · 2 new emails") instead
  of pinging per message. The digest is held while Do Not Disturb / quiet hours
  are active and released once focus clears, so nothing is lost.
- **Message delivery status**: your most-recent chat message shows a single
  Teams-style indicator (clock while sending → check once sent).
- **Google multi-calendar**: the Calendar agenda + month grid now merge events
  from **every calendar you show** in Google Calendar (primary, birthdays,
  holidays, subscribed and secondary), not just your primary one — each event
  labelled with its calendar. Edits and deletes route back to the owning
  calendar; read-only ones (holidays/birthdays) decline gracefully.
- **Google Drive sources**: the Files tab now lists **My Drive**, **Shared with
  me**, and your **Shared Drives** (Workspace Team Drives, enumerated through
  rclone) — mirroring the OneDrive + Team-library layout on the Microsoft side.
- **Command palette (Ctrl+K)**: a keyboard-first jump-to — search every signed-in
  account's Files/Mail/Calendar/Chat/Teams surface and app actions, ↑/↓ to move,
  Enter to go, Esc to dismiss.
- **Offline cache**: mail, agenda and chat lists are persisted to disk, so a
  fresh launch shows your last-known data immediately (even offline) and then
  revalidates when the network returns.

### Changed
- **Preferences reorganised**: notification settings (relevance, Do Not Disturb,
  quiet hours) moved to their own **Notifications** tab instead of sharing the
  General page, with Quiet hours in a dedicated group; clarified several setting
  subtitles (file caching, sync type, start-at-login, background).

### Fixed
- **New mail now appears on its own**: the open mail list reloads when the
  background poller spots new mail (every ~2 min), instead of only on a manual
  refresh or a tab switch. Updates even while a banner is suppressed by Do Not
  Disturb / quiet hours.
- **Chat images no longer re-download on every send/receive**: decoded inline
  thumbnails are cached per conversation, so reconciling a sent message (a full
  thread rebuild) reuses them instead of re-fetching every picture.
- **OneNote pages render full-width** and split very long text across multiple
  labels, so a single huge paragraph can no longer exceed the GL texture limit
  and re-trigger the `gsk_gpu_upload_cairo_op` renderer crash.
- **Gmail folder dropdown** now spans the full column width (it was constrained
  as the header's centred title widget).

## [0.2.1] - 2026-06-15

### Added
- **Teams**: a new top-level **Teams** tab (Microsoft work/school accounts) —
  the hierarchical Team → channel surface, distinct from the flat **Chat** tab.
  Pick a team to expand its channels; selecting a channel opens a
  **Conversation / Notes** tab strip:
  - **Conversation**: the channel's **posts** rendered like Teams (root post +
    threaded replies), with inline image thumbnails and file chips matching the
    Chat view, a composer to start a post, and an inline reply box per post
    (`ChannelMessage.Read.All` / `ChannelMessage.Send`, tenant-admin consent).
  - **Notes**: the team's **OneNote** notebook (`Notes.ReadWrite.All` /
    `Notes.Create`) — browse sections and pages, read pages **rendered natively**
    (text + inline images, no embedded browser), and **create / edit** pages
    with the rich-text editor.
  Google has no channel/notes equivalent (its Chat spaces remain under the Chat
  tab), so the Teams tab is offered for Microsoft accounts only.

### Fixed
- **Flatpak release builds** no longer fetch `blueprint-compiler` from
  `gitlab.gnome.org` (a 503 there broke a release); the GNOME 48+ SDK already
  bundles it.
- **Notes no longer crash the renderer**: OneNote pages are drawn with native
  widgets instead of a full-page WebView, which on long pages overran the GPU
  texture limit (`gsk_gpu_upload_cairo_op` segfault). Images are fetched with
  the bearer token and shown as native thumbnails.

## [0.2.0] - 2026-06-15

### Added
- **Chat**: a new Teams-style messenger capability (`ChatCapability`) for
  **Teams chats** (work/school Microsoft accounts, `Chat.ReadWrite`) and
  **Google Chat** (Workspace). 1:1, group and meeting threads with inline
  images, emoji reactions, @mentions, reply/forward, edit/delete, multi-select,
  presence dots, message search, and live (polled) updates. Chat unread shows a
  red sidebar badge and raises notifications that deep-link into the thread.
  Threads update **incrementally** (new messages fade in without rebuilding or
  reloading the rest of the thread), and jump-to-latest is smoothly animated.
- **Packaging**: Fedora **RPM** (`make rpm`) and a single-file **Flatpak bundle**
  (`make flatpak-bundle`); `make release` produces both into `release/` and
  installs the bundle. OAuth client IDs/secrets are baked at build time from
  `.env` into a GSettings vendor override (the committed repo ships none).
- **Calendar redesign**: a month **grid** (in the Calendar tab and the Dashboard)
  with past + future events, plus an agenda list.
- **Event window**: clicking an event opens a non-modal detail window with an
  **attendee response tracker** (pills grouped by RSVP status), Join/Open, RSVP,
  Delete, and **inline edit** (subject, all-day, day, start/end time, location,
  removable attendees, description) → `update_event`.
- **Mail compose / reply / reply-all** in a non-modal editor; **contacts
  autocomplete** (Microsoft People API; Google connections + other-contacts).
- **Shared / group sources** for Mail & Calendar (Microsoft): **Me / Teams /
  Shared** tabs, add-shared-mailbox dialog, and **★ pin** sources to the
  Dashboard with live unread/event counts.
- **Dashboard** rework: pinned sources, upcoming events, recent mail, and recent
  file changes — cached (stale-while-revalidate) with a Refresh button.
- **Mount layout** setting (`one-folder` vs `individual`) + per-account mount
  location; host-visible Flatpak mounts via `flatpak-spawn --host`.
- **Desktop integration**: registers as the system `mailto:` / `.ics` handler;
  new-mail / upcoming-event notifications; optional Evolution Data Server
  calendar mirror; run-in-background mode.

### Changed
- **App ID renamed** `io.github.sha5b.Clouddrive` → **`io.github.sha5b.Cloudy`**
  (schema id, D-Bus name/paths, icons, manifest, scripts). A best-effort dconf
  migration carries pre-rename accounts/prefs over on host/RPM installs.
- **Per-account mountpoints**: each account's drives mount under their own
  folder (`…/mounts/<account>/<drive>`), so identically-named drives across
  accounts no longer collide and a live mount is attributable to its account —
  fixing repeated/duplicate mounting.
- **Design system**: added a shared stylesheet (`data/style.css`) + a 4px
  spacing scale (`widgets/metrics.py`), unified empty/loading states
  (`source_nav.status_page` / `loading_box`), one secondary-text style
  (`.cloudy-meta`), semantic calendar classes (`.cloudy-day`/`.cloudy-chip`/
  `.cloudy-pill`), a consistent title hierarchy, and standard editor/detail
  window sizes. Removed the dead `preferences.blp`.
- **Rebranded** `com.fiberelements.Cloudy` → `io.github.sha5b.Clouddrive`
  (schema id, D-Bus name/paths, icons, desktop/metainfo all renamed).
- **Files backend** is now `rclone` FUSE mounts (live two-way network drives);
  the abraunegg/onedriver path is retired as the primary mechanism.
- Preferences reorganized into **General** + **Accounts** (per-account services
  on/off, offline-sync toggle, mount location); the Modules tab was removed.

### Fixed
- Inline event editor: removing **all** attendees now clears them server-side
  (was a no-op — an empty list was treated as "leave unchanged"); **multi-day**
  events no longer collapse to a single day when edited.
- Notifications: the "prime once" poll timer no longer fires forever alongside
  the steady timer (it was doubling poll traffic).

- Preferences → General: mount location (folder chooser), file-caching mode
  (on-demand "full" vs minimal streaming), and start-at-login (writes a host
  autostart entry). Mount location + cache mode are honored by the rclone mount.
- Google Drive for Gmail accounts: the Files tab now appears for Google and
  mounts "My Drive" via rclone's "drive" backend (its own browser auth, no
  registration). rclone auth/remote config generalized to any backend.
- In-memory cache (stale-while-revalidate) for mail/calendar + a Refresh button.
- Files: Mount ↔ Unmount toggle with a "Mounted" indicator.

- Initial project scaffold: GNOME 50 (GTK4 / Libadwaita) Python application
  structure following the Alpaca 3-subdir layout (`data/`, `src/`, `po/`).
- GPL-3.0-or-later licensing (`COPYING`) with per-file SPDX headers.
- Documentation set under `docs/` (architecture, roadmap, modules, building,
  auth, research).
- Flatpak manifest targeting `org.gnome.Platform` 50.
- Runnable Adwaita application shell (navigation split view) with a module
  registry. Modules follow a provider model (one account = one login = many
  capabilities): a `microsoft365` module surfaces OneDrive/SharePoint Files,
  Mail and Calendar from one Graph login, plus a `gmail` provider stub.
- Host-side Nautilus extension stub (`nautilus-python`, API 4.0).
- Browser-based Microsoft Graph sign-in (MSAL interactive: system browser +
  loopback PKCE), run off the UI thread; token cache persisted via libsecret;
  the Sign In button flips the account to signed-in and shows the user's UPN.
  Client ID is configurable (`microsoft-client-id` setting / `CLOUDY_MS_CLIENT_ID`).
- Files surface: enumerate OneDrive/SharePoint drives via Graph; mount a library
  (rclone/onedriver) so it appears in the Nautilus sidebar (auto-added GTK
  bookmark), network-drive style; "Open in Files". Backend auto-detected with a
  clear prompt to install rclone/onedriver when absent. Sign-in now requests
  Files scopes so file access works without a second consent.
- Nautilus integration: the app exports a D-Bus sync-status service
  (`io.github.sha5b.Clouddrive` `…/Sync`) registered on its own bus name; the
  host `nautilus-python` (API 4.0) extension draws sync emblems (InfoProvider)
  and adds right-click Copy share link / Free up space / Sync this folder
  (MenuProvider) via D-Bus. Install with `make install-nautilus`.
- Mail + Calendar surfaces (Microsoft Graph): Inbox message list (with unread
  markers) and a 7-day upcoming-events view, each loaded off the UI thread.
  Sign-in now requests Mail/Calendar scopes too, so one consent lights up
  Files, Mail and Calendar.
- Gmail provider: Google browser sign-in (loopback + PKCE on urllib, no Google
  SDKs), Gmail Inbox + Google Calendar via a GoogleClient normalized to the same
  shape as Graph. Mail/Calendar views are now provider-agnostic (one factory
  picks Microsoft or Google per account). `google-client-id` /
  `CLOUDY_GOOGLE_CLIENT_ID` config.
- Message reading: open a mail row to read its body (HTML stripped to text).
- Dashboard ("Overview"): merges every account's calendar into one 7-day
  timeline and lists recent mail across accounts (unread first), each labeled
  with its account — the whole day at a glance.
- Sign-in UX: when no client ID is configured, show a clear "setup needed"
  dialog instead of a silent toast; Google sign-in opens the browser via the
  portal-aware launcher on the main thread.

[Unreleased]: https://github.com/sha5b/Cloudy
