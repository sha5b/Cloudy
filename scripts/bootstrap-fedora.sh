#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Shahab Nedaei
#
# System-package half of the Cloudy development setup (Fedora 44 / GNOME 50).
#
# This script installs ONLY what dnf must provide because pip/uv cannot: C
# libraries, GObject introspection typelibs, code generators and host daemons.
# The Python half — meson, ninja, ruff, msal, Pillow — lives in the uv
# virtualenv instead; run `make venv` for that (no root needed).
#
#   dnf  (root, here)   gtk4/libadwaita/glib headers, glib-compile-resources,
#                       python3-gobject, appstream validators, rclone, Nautilus
#                       bindings, flatpak runtimes
#   uv   (`make venv`)  meson, ninja, ruff, msal, Pillow
#
# `make bootstrap` runs this and then `make venv`. Safe to re-run.
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

# --- Native build deps + GTK4/Libadwaita/PyGObject ---------------------------
# NOT listed on purpose:
#   * meson / ninja      -> the uv venv provides them (`make venv`)
#   * blueprint-compiler -> Meson fetches the PINNED v0.16.0 from
#                           subprojects/blueprint-compiler.wrap. Installing the
#                           distro one silently overrides that pin, so builds
#                           would stop being reproducible across machines.
# glib2-devel is what carries glib-compile-resources and the gio-2.0 pkg-config
# file; without it Meson cannot even configure.
log "Installing native build dependencies and GTK4/Libadwaita libraries"
sudo dnf install -y \
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

log "System packages done. Next: 'make venv' for the Python side, then 'make run'."
