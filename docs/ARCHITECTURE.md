<!--
SPDX-License-Identifier: GPL-3.0-or-later
SPDX-FileCopyrightText: 2026 Fiber Elements
-->

# Architecture

Cloudy is a GNOME-native (GTK4 / Libadwaita) Python application that
**orchestrates** existing Linux backends rather than reimplementing sync or mail
protocols. It is modeled on [Alpaca](https://github.com/Jeffser/Alpaca): pure
Python (PyGObject), Meson build, Blueprint UI compiled into a GResource, packaged
as a Flatpak on the GNOME runtime, with a clean "instance manager + provider
class" module pattern.

## High-level picture

```
                        ┌──────────────────────────────────────┐
                        │  Cloudy (Flatpak, sandboxed UI)    │
                        │                                        │
   Adw.NavigationSplitView ── sidebar (accounts / modules)       │
                        │   └─ content stack (Files/Mail/Cal)    │
                        │                                        │
   core/  ── module engine · account registry · secrets · auth   │
                        │                                        │
   modules/ ─ microsoft365 · gmail · eds_reader (opt.)           │
                        └───────────────┬────────────────────────┘
                                        │ D-Bus
            ┌───────────────────────────┴───────────────────────────┐
            │            Host (outside the sandbox)                  │
            │  sync/mount daemons: onedrive (abraunegg) · onedriver  │
            │  · rclone     +     Nautilus extension (nautilus-python)│
            └────────────────────────────────────────────────────────┘
```

**Why the split?** FUSE mounts and host Nautilus extensions do not work cleanly
from inside a Flatpak sandbox. The pragmatic, validated design runs the
sync/mount daemons and the Nautilus extension **on the host** (as user systemd
services / host extensions) and has the sandboxed UI talk to them over **D-Bus**.

## Process model

| Component | Where it runs | Language | Talks to |
|---|---|---|---|
| Cloudy UI | Flatpak sandbox | Python / PyGObject | D-Bus, libsecret portal, Graph/Gmail HTTPS |
| `onedrive` (abraunegg) | host user systemd unit | D (binary) | OneDrive/SharePoint via Graph |
| `onedriver` | host user systemd unit | Go (binary) | OneDrive via Graph (FUSE on-demand) |
| `rclone` (optional) | host | Go (binary) | OneDrive via Graph (VFS mount) |
| Nautilus extension | host | Python (`nautilus-python`) | Cloudy D-Bus status service |

## Layers inside the app

### 1. Shell (`src/application.py`, `src/window.py`)
- `Adw.Application` subclass; single-instance; actions (`quit`, `preferences`,
  `about`) and accelerators.
- `Adw.NavigationSplitView` shell: sidebar lists accounts and module surfaces;
  the content side is an `Adw.ViewStack` switched per module.
- Adaptive (small-window friendly) per the GNOME HIG.

### 2. Core (`src/core/`)
- **`interfaces.py`** — the stable contracts. `ServiceModule` plus capability
  mix-ins `FilesCapability`, `MailCapability`, `CalendarCapability`. Modules are
  decoupled from the shell through these.
- **`account_registry.py`** — analogous to Alpaca's `instance_manager.py`. Holds
  configured accounts, persists non-secret state in GSettings, emits change
  signals the UI binds to.
- **`plugin_engine.py`** — module discovery. Starts as a simple dynamic-import
  registry over `src/modules/`; designed to migrate to **`libpeas-2`** once the
  interfaces stabilize (see [MODULES.md](MODULES.md)).
- **`secrets.py`** — libsecret wrapper (Secret Service portal inside Flatpak).
  Stores OAuth/MSAL token caches; never plaintext.
- **`dbus_service.py`** — exports per-path sync status consumed by the Nautilus
  extension; also the channel for "sync this folder / free up space" commands.
- **`auth/`** — `msal_graph.py` (MSAL device-code / PKCE for Microsoft Graph)
  and `google_oauth.py` (Google OAuth2 for Gmail). See [AUTH.md](AUTH.md).

### 3. Modules (`src/modules/`)
**A module is a provider = one account = one login.** It implements
`ServiceModule` plus one or more capability mix-ins, and surfaces *all* of them
from a single authentication. The shell renders surfaces per capability, not per
module.

- **`microsoft365/`** — one Microsoft Graph login surfacing three capabilities:
  Files (OneDrive/SharePoint/Teams), Mail, and Calendar. **OneDrive is the Files
  capability, not a separate account.** Files orchestrates
  `abraunegg/onedrive` (selective sync) + `onedriver`/`rclone` (on-demand);
  mail/calendar use the shared Graph client. (`module.py` + `files.py` +
  `graph.py` + `abraunegg.py`.)
- **`gmail/`** — the Google provider: Gmail + Google Calendar from one Google
  login.
- **`eds_reader/`** (optional) — read calendars/contacts the user already
  configured in GNOME Online Accounts via Evolution Data Server.

### 4. Widgets (`src/widgets/`)
Reusable GTK4 subclasses (account rows, file rows, sync-status badges).

## Authentication UX

Sign-in opens the user's **system browser** (auth-code + PKCE via a loopback
redirect); no credentials touch the app, and tokens land in libsecret. Cloudy
ships its own **multi-tenant client ID** so there is **no manual app
registration** — one click → browser → consent → done. Device-code is the
headless fallback. See [AUTH.md](AUTH.md).

## Files in Nautilus: the "network folder" model

The user's mental model is a **mapped network drive**: pick a Teams/SharePoint
library and it just **appears in Nautilus** (under *Other Locations / Network*
and the sidebar). We get that automatically by **mounting**, not by writing
shell code:

- A library is exposed as a **GVfs / GMount** (via `onedriver` FUSE, `rclone
  mount`, or the GNOME OneDrive gvfs backend). Any GMount shows up in Nautilus's
  Network + sidebar **with no extra integration** — the same way GNOME Online
  Accounts' OneDrive appears today.
- So the flow is: **select a Teams library in Cloudy → we mount it → it
  appears in the file manager.** On-demand (network-drive-like) is the default
  for Teams/SharePoint; full local sync (abraunegg) is the opt-in alternative.
- The `nautilus-python` extension adds only the **extras** on top of that mount:
  sync-status emblems and right-click *Free up space / Copy share link / Sync
  this folder*, driven by the app's D-Bus status service.

This keeps the heavy lifting in proven mount backends and limits our custom shell
code to the value-add layer.

## Key external decisions (web-verified June 2026)

- **Runtime**: `org.gnome.Platform` **50** (Fedora 44 ships GNOME 50; the 48
  runtime reached EOL on 2026-03-24).
- **Exchange**: **Microsoft Graph only.** EWS soft-blocks 2026-10-01 and is fully
  retired 2027-04-01.
- **On-demand files**: Linux has no kernel cloud-filter API (no CFAPI/File
  Provider equivalent), so on-demand is approximated with FUSE (`onedriver`) or
  rclone's VFS cache. `abraunegg/onedrive` 2.5.x also advertises a Windows-style
  on-demand mode now. True placeholder/overlay semantics are **not achievable**.

See [RESEARCH.md](RESEARCH.md) for the full findings and sources.
