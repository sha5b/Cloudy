#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Fiber Elements
#
# Reproducible one-shot setup for Cloudy development on Fedora 44 (GNOME 50).
# Installs the build toolchain, the runtime libraries, and (optionally) the host
# backends and the Flatpak runtimes. Safe to re-run.
#
# Usage:
#   ./scripts/bootstrap-fedora.sh             # build toolchain + GTK libs
#   ./scripts/bootstrap-fedora.sh --backends  # also OneDrive/rclone/nautilus-python
#   ./scripts/bootstrap-fedora.sh --flatpak   # also GNOME 50 Flatpak runtime+SDK
#   ./scripts/bootstrap-fedora.sh --all       # everything

set -euo pipefail

WANT_BACKENDS=0
WANT_FLATPAK=0
for arg in "$@"; do
  case "$arg" in
    --backends) WANT_BACKENDS=1 ;;
    --flatpak)  WANT_FLATPAK=1 ;;
    --all)      WANT_BACKENDS=1; WANT_FLATPAK=1 ;;
    -h|--help)  grep '^#' "$0" | sed 's/^# \?//'; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

log() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }

# --- Build toolchain + GTK4/Libadwaita/PyGObject -----------------------------
log "Installing build toolchain and GTK4/Libadwaita libraries"
sudo dnf install -y \
  meson ninja-build blueprint-compiler \
  gcc pkgconf-pkg-config \
  gtk4-devel libadwaita-devel \
  python3 python3-gobject \
  glib2-devel desktop-file-utils libappstream-glib appstream

# --- Host backends (run outside the Flatpak sandbox) -------------------------
if [[ "$WANT_BACKENDS" == 1 ]]; then
  log "Installing host backends (OneDrive client, rclone, nautilus-python)"
  sudo dnf install -y onedrive rclone nautilus-python || {
    echo "Note: some backends may need a COPR (e.g. onedriver). See docs/BUILDING.md" >&2
  }
fi

# --- Flatpak runtime + SDK ---------------------------------------------------
if [[ "$WANT_FLATPAK" == 1 ]]; then
  log "Installing Flatpak GNOME 50 runtime and SDK"
  sudo dnf install -y flatpak flatpak-builder
  flatpak remote-add --if-not-exists --user \
    flathub https://flathub.org/repo/flathub.flatpakrepo
  flatpak install -y --user org.gnome.Platform//50 org.gnome.Sdk//50
fi

log "Bootstrap complete. Next: 'make run' (local) or 'make flatpak-run' (sandboxed)."
