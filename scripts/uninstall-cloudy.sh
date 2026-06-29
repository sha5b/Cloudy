#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Shahab Nedaei
#
# Clean uninstall helper for Cloudy. The RPM/Flatpak removal itself drops every
# *packaged* file; this script handles what a package manager cannot: live FUSE
# mounts and per-user data (tokens, settings, caches, bookmarks, the host
# Nautilus extension).
#
# Usage:
#   scripts/uninstall-cloudy.sh            # unmount + print removal steps (safe)
#   scripts/uninstall-cloudy.sh --purge    # ALSO delete all per-user data
#
# It never runs `dnf`/`flatpak` for you (those need your decision/root); it
# prints the exact command.

set -euo pipefail

APP_ID="io.github.sha5b.Cloudy"
SCHEMA_PATH="/io/github/sha5b/Cloudy/"
SECRET_SCHEMA="io.github.sha5b.Cloudy.Token"
DATA_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/cloudy"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/cloudy"
MOUNTS_DIR="$DATA_DIR/mounts"
NAUTILUS_EXT="${XDG_DATA_HOME:-$HOME/.local/share}/nautilus-python/extensions/cloudy_nautilus.py"
GTK3_BOOKMARKS="${XDG_CONFIG_HOME:-$HOME/.config}/gtk-3.0/bookmarks"
AUTOSTART_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/autostart"

PURGE=0
[ "${1:-}" = "--purge" ] && PURGE=1

say() { printf '\033[1m%s\033[0m\n' "$*"; }

# --- 1. Unmount any live rclone mounts (always; a hung mount blocks deletion).
if [ -d "$MOUNTS_DIR" ]; then
  say "Unmounting Cloudy FUSE mounts under $MOUNTS_DIR …"
  while IFS= read -r mp; do
    [ -n "$mp" ] || continue
    fusermount3 -u "$mp" 2>/dev/null || fusermount -u "$mp" 2>/dev/null \
      || umount "$mp" 2>/dev/null || echo "  could not unmount $mp (already gone?)"
  done < <(mount | awk -v d="$MOUNTS_DIR" '$3 ~ d {print $3}')
fi

if [ "$PURGE" -eq 1 ]; then
  say "Purging per-user data …"
  # GSettings / dconf
  if command -v dconf >/dev/null 2>&1; then
    dconf reset -f "$SCHEMA_PATH" 2>/dev/null && echo "  reset dconf $SCHEMA_PATH"
  fi
  # libsecret tokens (every secret tagged with our schema)
  if command -v secret-tool >/dev/null 2>&1; then
    secret-tool clear xdg:schema "$SECRET_SCHEMA" 2>/dev/null \
      && echo "  cleared stored OAuth tokens" || true
  fi
  # Data + config dirs (rclone binary, mounts/, synced/, secrets.env)
  rm -rf "$DATA_DIR"   && echo "  removed $DATA_DIR"
  rm -rf "$CONFIG_DIR" && echo "  removed $CONFIG_DIR"
  # Nautilus bookmarks Cloudy added (lines pointing at the mounts dir)
  if [ -f "$GTK3_BOOKMARKS" ]; then
    sed -i "\#$MOUNTS_DIR#d" "$GTK3_BOOKMARKS" && echo "  cleaned gtk-3.0 bookmarks"
  fi
  # Host Nautilus extension
  rm -f "$NAUTILUS_EXT" && echo "  removed Nautilus extension"
  # Autostart desktop entry
  rm -f "$AUTOSTART_DIR/$APP_ID.desktop" && echo "  removed autostart entry"
  # Flatpak per-app data, if any
  if command -v flatpak >/dev/null 2>&1; then
    rm -rf "$HOME/.var/app/$APP_ID" 2>/dev/null && echo "  removed ~/.var/app/$APP_ID" || true
  fi
else
  say "Per-user data kept. Re-run with --purge to delete it:"
  echo "  $DATA_DIR"
  echo "  $CONFIG_DIR"
  echo "  dconf $SCHEMA_PATH, libsecret schema $SECRET_SCHEMA, Nautilus extension"
fi

echo
say "Now remove the package itself:"
echo "  RPM:     sudo dnf remove cloudy"
echo "  Flatpak: flatpak uninstall --user $APP_ID        # add --delete-data to drop sandbox data"
