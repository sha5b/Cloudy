<!--
SPDX-License-Identifier: GPL-3.0-or-later
SPDX-FileCopyrightText: 2026 Fiber Elements
-->

# Research & rationale

This file records the findings that shaped Cloudy's design, with the
**web-verified status as of June 2026**. It is the "why" behind
[ARCHITECTURE.md](ARCHITECTURE.md).

## Verified facts (June 2026)

| Topic | Finding | Source |
|---|---|---|
| Fedora 44 desktop | Ships **GNOME 50** (released 2026-04-28) | [Fedora Magazine][f44] |
| GNOME Flatpak runtime | Use `org.gnome.Platform`/`Sdk` **50**; the **48** runtime reached EOL **2026-03-24** | [Ubuntu Discourse][gn48eol] |
| OneDrive client | `abraunegg/onedrive` 2.5.x is the gold-standard OSS client and now advertises a Windows-style **on-demand** mode | [abraunegg/onedrive][abe] |
| On-demand FUSE | `onedriver` (jstaf) actively maintained (release activity Jan 2026); true download-on-access FUSE filesystem | [jstaf/onedriver][odr] |
| Nautilus extensions | `nautilus-python` API **4.0** (GTK4): `get_file_items(files)` drops the window arg; `PropertyPageProvider` → `PropertiesModelProvider` | [nautilus-python 4.0 migration][np4] |
| Exchange / EWS | EWS soft-blocks for non-MS apps **2026-10-01**, fully retired **2027-04-01**; allow-list exemptions must be set before end of Aug 2026; **Graph** is the replacement | [Microsoft Learn][ews] |

## Part 1 — OneDrive as a network drive with file-manager integration

**No single tool replicates the Windows OneDrive experience on Linux.** The real
options:

- **`abraunegg/onedrive`** — full bidirectional sync, best Business/SharePoint
  support, rules-based selective sync; **one client instance per SharePoint
  library**. Now also offers an on-demand mode.
- **`onedriver`** — true FUSE on-demand (download on `open()`); SharePoint /
  shared-library support is weaker.
- **`rclone mount`** — VFS cache (on-demand-ish), include/exclude filters; not a
  true two-way sync engine.
- **GNOME Online Accounts + gvfs** (libmsgraph, GVfs 1.54+) — browse/stream only,
  no sync, no selective sync, no SharePoint picker; historically fragile.

**Files-on-demand is fundamentally weaker on Linux.** Windows has the kernel
Cloud Filter API (CFAPI / `cldflt.sys`); macOS has File Provider extensions.
Linux has **no kernel cloud-filter facility**, so every tool approximates with
FUSE. There is no OS-level placeholder concept, no dehydration UX, and no
kernel-driven overlay icons.

**Nautilus integration is achievable but limited.** `nautilus-python` lets you
add right-click menu items (`MenuProvider`), emblems / per-file status
(`InfoProvider`), and a properties model (`PropertiesModelProvider`). You can
build "Sync this folder / Free up space / Copy share link" and status emblems,
but not CFAPI-grade overlay icons driven by the filesystem.

**Decision:** orchestrate `abraunegg` (selective sync, SharePoint/Teams) +
`onedriver`/`rclone` (on-demand), surfaced in Nautilus via a `nautilus-python`
extension that talks to the app over D-Bus.

## Part 2 — Unified mail + calendar

**EWS is a dead end; Microsoft Graph is the only future-proof path.** With EWS
blocking from 2026-10-01 and full shutdown 2027-04-01, anything built on EWS (or
`evolution-ews`) has a hard expiry.

**Two architectures, do a hybrid:**

- **(A) Build on Evolution Data Server (EDS)** — inherit Google + Exchange
  plumbing and GOA accounts "for free", but tied to EDS's account model and the
  EWS sunset.
- **(B) Direct Microsoft Graph + Gmail API** — future-proof, full feature
  control, at the cost of writing the sync/cache layer.

**Decision:** Build the mail/calendar module against **Graph** and **Gmail**
directly (Option B) behind a provider abstraction; optionally add an **EDS
reader** to surface calendars/contacts the user already configured in GNOME.

## Part 3 — App architecture & stack

- **Stack:** Python + PyGObject (fastest iteration; the exact backends we need —
  `msal`, Graph/Gmail SDKs, `nautilus-python`, subprocess orchestration — are
  Python-friendly). A performance-critical sync daemon can later be extracted to
  Rust behind D-Bus without touching the UI.
- **Template:** Alpaca — GPL-3.0, pure Python, Meson 3-subdir layout,
  Blueprint→GResource, `@Gtk.Template`, instance-manager module pattern, Flatpak
  on the GNOME runtime.
- **Plugin engine:** start with a dynamic-import registry; migrate to
  **`libpeas-2`** once interfaces stabilize.
- **UI:** `Adw.NavigationSplitView` shell, `Adw.ViewStack`/`ViewSwitcher`,
  `Adw.PreferencesWindow`, `Adw.Toast`, `Adw.StatusPage`, `Adw.AboutDialog`.
- **Packaging:** Flatpak on `org.gnome.Platform` 50; secrets via libsecret /
  Secret Service portal; Background portal for autostart of the sync service.
  Run FUSE/mount daemons and the Nautilus extension **on the host**.

## Honest limits

- No CFAPI-grade placeholders, kernel overlay icons, or seamless dehydration on
  Linux.
- `abraunegg`: one SharePoint library per client instance.
- `onedriver` SharePoint/shared-library support is limited — verify per tenant.
- Some EWS capabilities have no Graph equivalent (public-folder CRUD, certain
  recurring-event delta semantics; tasks moved to the To Do API).
- Business tenants often require **admin consent** for `*.All` scopes.

[f44]: https://fedoramagazine.org/announcing-fedora-linux-44/
[gn48eol]: https://discourse.ubuntu.com/t/the-gnome-48-runtime-is-no-longer-supported-as-of-march-24-2026-ubuntu-24-04-4-lts-flatpak/80598
[abe]: https://github.com/abraunegg/onedrive
[odr]: https://github.com/jstaf/onedriver
[np4]: https://gnome.pages.gitlab.gnome.org/nautilus-python/nautilus-python-migrating-to-4.html
[ews]: https://learn.microsoft.com/en-us/exchange/clients-and-mobile-in-exchange-online/deprecation-of-ews-exchange-online
