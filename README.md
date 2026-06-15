<!--
SPDX-License-Identifier: GPL-3.0-or-later
SPDX-FileCopyrightText: 2026 Shahab Nedaei
-->

# Cloudy

A native **GTK4 / Libadwaita** super-app for Fedora that brings Microsoft
**365** (OneDrive + Teams/SharePoint, Mail, Calendar) and **Google** (Gmail,
Calendar, Drive) into one GNOME-native window — with file-manager (Nautilus)
integration, live network-drive mounts, and a unified, provider-agnostic
**mail + calendar** surface.

> Status: **working app.** Sign-in (Microsoft + Google), Files (mount/unmount +
> an in-app browser), Mail and Calendar (read, compose/reply, RSVP, create/edit/
> delete events), a Dashboard, and desktop/Nautilus integration are all
> functional. Packaged as both an **RPM** and a single-file **Flatpak**. See
> [docs/HANDOFF.md](docs/HANDOFF.md) for the detailed status and the open backlog.

Cloudy does **not** reinvent sync engines. It *orchestrates* proven backends —
`rclone` for the FUSE file mounts (with [`onedriver`](https://github.com/jstaf/onedriver)
as an alternate), and **Microsoft Graph** / **Google REST** for mail & calendar —
behind one adaptive UI inspired by [Alpaca](https://github.com/Jeffser/Alpaca).

## Features

- **Microsoft 365** (OneDrive + Teams/SharePoint libraries) and **Google**
  (Gmail, Calendar, My Drive) accounts side by side.
- **Files** = live `rclone` FUSE mounts (two-way network drives, not synced
  copies) that appear in Nautilus and an in-app file browser. Optional offline
  `bisync` for full-copy accounts.
- **Mail**: read (HTML), compose, reply/reply-all, with **Me / Teams / Shared**
  mailbox sources (Microsoft) and contacts autocomplete.
- **Calendar**: month grid + agenda, event detail with an attendee response
  tracker and RSVP, and **create / inline-edit / delete** events.
- **Dashboard**: pinned sources, upcoming events, recent mail, and recent file
  changes aggregated across every account.
- **Desktop integration**: acts as the system `mailto:` / `.ics` handler, raises
  notifications for new mail and upcoming events, and a Nautilus extension draws
  status emblems + Unmount/Copy-share-link menu items.
- **Secrets** stored via **libsecret** — never plaintext. OAuth client IDs are
  baked at build time or supplied via `.env` (see [docs/SECRETS.md](docs/SECRETS.md)).
- Packaged as a **Flatpak** (`org.gnome.Platform` 50) and an **RPM**.

## Platform

Built and tested for **Fedora 44 (GNOME 50)**. See [docs/BUILDING.md](docs/BUILDING.md).

## Quick start (development)

Reproducible from a clean Fedora 44 checkout:

```bash
# 1. Install everything (toolchain + host backends + Flatpak runtime):
make bootstrap                 # == ./scripts/bootstrap-fedora.sh --all

# 2a. Build, install, and run locally:
make run

# 2b. …or build + run as a sandboxed Flatpak (pinned GNOME 50 runtime):
make flatpak flatpak-run

# 2c. …or build distributable artifacts (RPM + single-file .flatpak) into
#     release/ and install the bundle so the running app matches:
make release
```

Common targets: `make build`, `make test`, `make lint`, `make clean`,
`make rpm`, `make flatpak-bundle`. Artifacts under `release/` embed baked
credentials and are gitignored. See [docs/BUILDING.md](docs/BUILDING.md) and the
[Makefile](Makefile).

## Continuing development

Resuming in a new session? Start with **[docs/HANDOFF.md](docs/HANDOFF.md)** —
current status, how things work, gotchas, and the next-steps backlog.

## Architecture

A Libadwaita navigation-split-view shell + a module/plugin engine. Each service
(OneDrive, Graph mail/calendar, Gmail) is a self-contained module implementing
capability interfaces. See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) and our [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).

## License

Cloudy is free software, licensed under the **GNU General Public License
v3.0 or later**. See [COPYING](COPYING). Each source file carries an SPDX
identifier.
