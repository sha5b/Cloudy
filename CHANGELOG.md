<!--
SPDX-License-Identifier: GPL-3.0-or-later
SPDX-FileCopyrightText: 2026 Shahab Nedaei
-->

# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **Dashboard "Activity" feed** (work/school Microsoft accounts): a new section
  with the latest post from each **starred channel** and your **recent chats**
  (starred chats first), plus a Today preview column and a "New chats" stat card.
- **Star channels and chats**: a ★ button in the Teams (channel) and Chat
  headers pins a conversation; pinned channels/chats surface in the Dashboard
  Activity feed. Mail/calendar pins continue to appear under **Pinned**.
- **Notification attention controls**: honour the system Do Not Disturb state;
  optional **quiet hours**; a relevance level (*Everything* vs *Direct messages &
  important only*) that batches group-chat/ordinary-mail to badges; and
  **per-chat / per-channel mute** (bell toggle in the Chat and Teams headers).
  Badges always update — only the interruptive banner is gated.
- **Message delivery status**: your most-recent chat message shows a single
  Teams-style indicator (clock while sending → check once sent).

### Changed
- **Preferences reorganised**: notification settings (relevance, Do Not Disturb,
  quiet hours) moved to their own **Notifications** tab instead of sharing the
  General page, with Quiet hours in a dedicated group; clarified several setting
  subtitles (file caching, sync type, start-at-login, background).

### Fixed
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
