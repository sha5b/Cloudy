<!--
SPDX-License-Identifier: GPL-3.0-or-later
SPDX-FileCopyrightText: 2026 Fiber Elements
-->

# Building Cloudy

Target platform: **Fedora 44 (GNOME 50)**.

## Reproducible quickstart

Everything below is wrapped by [`scripts/bootstrap-fedora.sh`](../scripts/bootstrap-fedora.sh)
and the [Makefile](../Makefile), so a clean machine can be set up with two
commands:

```bash
make bootstrap     # installs toolchain + host backends + GNOME 50 Flatpak runtime
make run           # local Meson build + install + launch
# or
make flatpak flatpak-run
```

Pinned for reproducibility: GNOME runtime/SDK **50**, `blueprint-compiler`
**v0.16.0** (both in `com.fiberelements.Cloudy.yml` and
`subprojects/blueprint-compiler.wrap`).

The manual steps follow.

### No root? (CI / restricted environments)

`meson` and `ninja` install into user space, and Meson auto-fetches the pinned
`blueprint-compiler` from `subprojects/blueprint-compiler.wrap` when it is not on
`PATH` — so a full local build needs no system packages beyond the GTK4 /
Libadwaita / PyGObject runtime (already present on a GNOME desktop):

```bash
python3 -m pip install --user meson ninja
export PATH="$HOME/.local/bin:$PATH"
meson setup _build --prefix="$PWD/_install"   # clones blueprint-compiler v0.16.0
meson install -C _build
GSETTINGS_SCHEMA_DIR="$PWD/_install/share/glib-2.0/schemas" ./_install/bin/cloudy
```

## Option A — Flatpak (recommended)

This matches how the app ships and how GNOME Builder builds it.

```bash
# One-time: install the toolchain and runtimes
sudo dnf install flatpak flatpak-builder
flatpak remote-add --if-not-exists --user \
    flathub https://flathub.org/repo/flathub.flatpakrepo
flatpak install --user \
    org.gnome.Platform//50 org.gnome.Sdk//50

# Build + install into the user installation
flatpak-builder --user --install --force-clean \
    _build/flatpak com.fiberelements.Cloudy.yml

flatpak run com.fiberelements.Cloudy
```

GNOME Builder: *Open Project* → it detects `com.fiberelements.Cloudy.yml`
and the Run button builds & launches the Flatpak.

## Option B — Meson into a prefix (fast iteration)

Needs the host to provide GTK4, Libadwaita and PyGObject (Fedora 44 does).

```bash
sudo dnf install meson ninja-build blueprint-compiler \
    gtk4-devel libadwaita-devel python3-gobject \
    desktop-file-utils 'pkgconfig(gio-2.0)'

meson setup _build --prefix="$PWD/_install"
meson compile -C _build
meson install -C _build

# Run (GSettings schema is installed into the prefix)
GSETTINGS_SCHEMA_DIR="$PWD/_install/share/glib-2.0/schemas" \
    ./_install/bin/cloudy
```

If `blueprint-compiler` is not packaged for your system, the Meson build pulls
it as a subproject via `subprojects/blueprint-compiler.wrap`.

## Host backends (runtime dependencies, not build deps)

The sync/mount backends run on the **host**, not in the sandbox:

```bash
sudo dnf install onedrive            # abraunegg client (selective sync)
sudo dnf install rclone              # optional, VFS mount
# onedriver: see https://github.com/jstaf/onedriver (COPR / build from source)
```

The Nautilus extension also lives on the host:

```bash
sudo dnf install nautilus-python     # python3-nautilus (API 4.0 / GTK4)
# then install nautilus-extension/cloudy_nautilus.py into
#   ~/.local/share/nautilus-python/extensions/
nautilus -q   # restart Nautilus to load it
```

## Troubleshooting

- **Schema not found at runtime** → ensure `GSETTINGS_SCHEMA_DIR` points at the
  installed schemas, or run via Flatpak.
- **Blueprint errors** → confirm `blueprint-compiler` ≥ 0.12 or let the wrap
  fetch it.
- **Nautilus extension not loading** → clear `__pycache__`, run `nautilus -q`,
  confirm `python3-nautilus` 4.x is installed.
