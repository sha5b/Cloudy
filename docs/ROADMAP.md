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

## Stage 4 — Nautilus integration (the extras) ✅
- App exports a **D-Bus sync-status service** (`com.fiberelements.Clouddrive`,
  `…/Sync`): `StatusForPath`, `SyncPath`, `FreeUpSpace`, `CreateShareLink`,
  `StatusChanged`.
- `nautilus-python` (API 4.0) `InfoProvider` draws emblems and `MenuProvider`
  adds *Copy share link / Free up space / Sync this folder*, all via D-Bus
  (best-effort; silent when the app is not running).
- Install with `make install-nautilus`.
- Remaining: `CreateShareLink`/`FreeUpSpace`/`SyncPath` are accepted but their
  effects land with the live mount/sync wiring (stage 5).

## Stage 5 — Full sync mode (offline copies)
- Add `abraunegg/onedrive` selective sync as the opt-in alternative to mounting
  (one client instance per SharePoint library), for users who want local copies.

## Stage 6 — Mail + Calendar (Microsoft Graph) ✅ (first pass)
- GraphClient: mail folders + Inbox messages, calendars + 7-day calendarView.
- MailView (Inbox list, unread dot) and CalendarView (upcoming events) surfaces,
  loaded off the UI thread; sign-in requests Mail/Calendar scopes up front.
- **Gmail provider** ✅: Google browser sign-in (loopback + PKCE, urllib),
  Gmail Inbox + Google Calendar via a GoogleClient normalized to the same shape
  as Graph; Mail/Calendar views are now provider-agnostic.
- Still to do: message reading/compose, folder switching, free/busy via
  `getSchedule`, contacts; optional `eds_reader`.

## Stage 7 — Packaging & polish
- Flatpak on Flathub; Background/autostart portal for the sync service.
- Adaptive UI pass, translations, metainfo screenshots & release notes.

## Known hard limits (set expectations)
- No CFAPI-grade placeholders / kernel overlay icons / seamless dehydration on
  Linux.
- `abraunegg` supports **one SharePoint library per client instance**.
- Some EWS capabilities have no Graph equivalent (public-folder CRUD, certain
  recurring-event delta semantics; tasks live in the To Do API).
