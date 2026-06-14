<!--
SPDX-License-Identifier: GPL-3.0-or-later
SPDX-FileCopyrightText: 2026 Fiber Elements
-->

# Cloudy

A native **GTK4 / Libadwaita** super-app for Fedora that makes Microsoft
**OneDrive for Business** (including Teams / SharePoint document libraries) easy
to use on Linux — with file-manager (Nautilus) integration, selective &
on-demand sync, and a unified **mail + calendar** surface for Microsoft 365 and
Google accounts.

> Status: **early scaffold.** This repository currently contains the project
> structure, documentation, licensing, and a runnable application shell. The
> service modules are stubs with documented interfaces — see
> [docs/ROADMAP.md](docs/ROADMAP.md).

Cloudy does **not** reinvent sync engines. It *orchestrates* proven Linux
backends — [`abraunegg/onedrive`](https://github.com/abraunegg/onedrive),
[`onedriver`](https://github.com/jstaf/onedriver), and `rclone` — behind one
adaptive, GNOME-native UI inspired by [Alpaca](https://github.com/Jeffser/Alpaca).

## Features (target)

- OneDrive for Business / Microsoft 365 / SharePoint & Teams libraries
- Selective sync (full local copies) **and** files-on-demand (FUSE)
- Nautilus integration: sidebar entry, sync-status emblems, right-click controls,
  "Copy share link"
- Unified mail + calendar over **Microsoft Graph** and the **Gmail API**
  (Graph-only for Exchange — EWS is retired April 2027)
- Secrets stored via **libsecret** / the Secret Service portal — never plaintext
- Packaged as a **Flatpak** on the `org.gnome.Platform` 50 runtime

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
```

Common targets: `make build`, `make test`, `make lint`, `make clean`. See
[docs/BUILDING.md](docs/BUILDING.md) and the [Makefile](Makefile).

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
