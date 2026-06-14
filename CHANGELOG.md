<!--
SPDX-License-Identifier: GPL-3.0-or-later
SPDX-FileCopyrightText: 2026 Fiber Elements
-->

# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
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
  (`com.fiberelements.Cloudy` `…/Sync`) registered on its own bus name; the
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

[Unreleased]: https://github.com/sha5b/Clouddrive-Fedora
