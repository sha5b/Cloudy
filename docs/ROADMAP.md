<!--
SPDX-License-Identifier: GPL-3.0-or-later
SPDX-FileCopyrightText: 2026 Fiber Elements
-->

# Roadmap

Staged plan. Each stage is independently useful and testable on Fedora 44.

## Stage 0 — Scaffold ✅ (this commit)
- Project layout, licensing, docs, Flatpak manifest.
- Runnable Adwaita shell + module registry + module stubs.
- Nautilus extension stub.

## Stage 1 — Shell + module engine ✅
- `Adw.NavigationSplitView` shell with a sidebar bound to the account registry.
- Add-account dialog (provider picker); per-account capability surfaces
  (Files/Mail/Calendar) via `Adw.ViewSwitcher`; accounts persisted in GSettings.
- `ServiceModule` / capability interfaces + `capabilities_of()`.

## Stage 2 — Auth core (browser-based, one click)
- **System-browser auth-code + PKCE** via a loopback redirect, opened through
  the OpenURI portal; device-code as the headless fallback.
- **Ship a multi-tenant client ID** so users register nothing (configurable for
  those who want their own). Google OAuth2 the same way.
- MSAL token cache persisted via libsecret / Secret Service portal; flip
  `Account.signed_in` and refresh silently. See [AUTH.md](AUTH.md).

## Stage 3 — Files: mount a library into the Network view
- Select a Teams/SharePoint library → **mount it** so it appears automatically
  in Nautilus (*Other Locations / Network* + sidebar). Default backend:
  `onedriver`/`rclone mount` (network-drive, on-demand feel).
- Enumerate drives/sites via the shared Graph client; "Copy share link" via
  `onedrive --create-share-link`.
- Host user systemd units for the mount/sync daemons.

## Stage 4 — Nautilus integration (the extras)
- `nautilus-python` (API 4.0) `MenuProvider` + `InfoProvider` layered on the
  mount: sync-status emblems and right-click *Free up space / Copy share link /
  Sync this folder*.
- Extension talks to the app's D-Bus status service.

## Stage 5 — Full sync mode (offline copies)
- Add `abraunegg/onedrive` selective sync as the opt-in alternative to mounting
  (one client instance per SharePoint library), for users who want local copies.

## Stage 6 — Mail + Calendar
- `MailProvider` / `CalendarProvider` against **Microsoft Graph** first
  (messages, threads, events, free/busy via `getSchedule`, contacts), then the
  **Gmail API**.
- Optional `eds_reader` to surface existing GNOME accounts.

## Stage 7 — Packaging & polish
- Flatpak on Flathub; Background/autostart portal for the sync service.
- Adaptive UI pass, translations, metainfo screenshots & release notes.

## Known hard limits (set expectations)
- No CFAPI-grade placeholders / kernel overlay icons / seamless dehydration on
  Linux.
- `abraunegg` supports **one SharePoint library per client instance**.
- Some EWS capabilities have no Graph equivalent (public-folder CRUD, certain
  recurring-event delta semantics; tasks live in the To Do API).
