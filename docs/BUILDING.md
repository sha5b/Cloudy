<!--
SPDX-License-Identifier: GPL-3.0-or-later
SPDX-FileCopyrightText: 2026 Shahab Nedaei
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
**v0.16.0** (both in `io.github.sha5b.Clouddrive.yml` and
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
    _build/flatpak io.github.sha5b.Clouddrive.yml

flatpak run io.github.sha5b.Clouddrive
```

GNOME Builder: *Open Project* → it detects `io.github.sha5b.Clouddrive.yml`
and the Run button builds & launches the Flatpak.

### Flatpak & file mounts (host-visible)

A FUSE mount made *inside* the sandbox is invisible to the host file manager, so
in Flatpak the app runs rclone **on the host** via `flatpak-spawn --host` and
mounts into the real host `~/.local/share/cloudy/mounts` (shared into the sandbox
via `--filesystem`). The mount then shows in the host's Nautilus and the Flatpak
reads it through mount propagation. This needs two broad permissions in the
manifest:

- `--talk-name=org.freedesktop.Flatpak` — run host commands (rclone/fusermount).
- `--filesystem=~/.local/share/cloudy` and `~/.config/gtk-3.0` — the mount dir
  and the Nautilus sidebar-bookmark file.

> Note: `--talk-name=org.freedesktop.Flatpak` is effectively host access and
> would be flagged by Flathub review. It's the price of host-visible FUSE mounts;
> the **RPM/host install** avoids the question entirely (it *is* the host).
> rclone is bundled in the Flatpak and copied to the host dir on first mount.

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

## Option C — RPM (Fedora)

A meson-based, `noarch` spec lives at [`packaging/cloudy.spec`](../packaging/cloudy.spec).
The `make rpm` target builds a reproducible source tarball (secrets and build
cruft excluded) into a self-contained `_build/rpm/` tree — no `~/rpmbuild`, no
root:

```bash
make rpm        # -> _build/rpm/RPMS/noarch/cloudy-<ver>.noarch.rpm  (+ SRPMS)
make srpm       # source RPM only

sudo dnf install ./_build/rpm/RPMS/noarch/cloudy-*.noarch.rpm
```

On a real Fedora build host (with `meson`, `blueprint-compiler` and the other
`BuildRequires` installed) the spec also builds the canonical way:
`rpmbuild -ba packaging/cloudy.spec` after dropping the tarball in `SOURCES/`.
`make rpm` uses `--nodeps` + the in-tree `blueprint-compiler` subproject so it
works on a dev box where those build deps aren't root-installed.

## Credentials — how shipped builds "just work"

The committed source ships **zero** credentials: the GSettings schema defaults
are empty and there is no `.env` in git. A **release** build bakes OAuth client
IDs in at build time via meson options, which generate a GSettings *vendor
override* (`90_<app-id>.gschema.override`) — the schema source is never touched:

```bash
meson setup _build \
  -Dms_client_id=...  -Dgoogle_client_id=...  -Dgoogle_client_secret=...
```

`make rpm` and `make flatpak` read these from `.env` automatically (and never
write them into the committed spec/manifest — the Flatpak uses a generated
local manifest under `_build/`). A build with no options ships no credentials
and sign-in then needs per-user `CLOUDY_*` env / GSettings (see
[SECRETS.md](SECRETS.md)).

> ⚠️ Baked credentials are **extractable** from the installed artifact — this is
> unavoidable for any desktop app. It is acceptable only for OAuth *public/
> desktop* clients (Microsoft public client = no secret; Google "Desktop app"
> secret = non-confidential by Google's design, protected by PKCE). Never bake a
> confidential web-client secret. Rotate the Google secret before public release
> and move its OAuth consent screen to *Production*.

## Uninstall

Removing the package drops every *packaged* file; live FUSE mounts and per-user
data (tokens, settings, caches, the host Nautilus extension) are handled by
[`scripts/uninstall-cloudy.sh`](../scripts/uninstall-cloudy.sh):

```bash
scripts/uninstall-cloudy.sh           # unmount mounts + print removal steps
scripts/uninstall-cloudy.sh --purge   # ALSO delete tokens, settings, caches

# then remove the package itself:
sudo dnf remove cloudy                              # RPM
flatpak uninstall --user io.github.sha5b.Clouddrive # Flatpak (--delete-data too)
```

## Host backends (runtime dependencies, not build deps)

The sync/mount backends run on the **host**, not in the sandbox:

```bash
sudo dnf install onedrive            # abraunegg client (selective sync)
sudo dnf install rclone              # optional, VFS mount
# onedriver: see https://github.com/jstaf/onedriver (COPR / build from source)
```

The Nautilus extension also lives on the host, but is now **installed
automatically** (needs `nautilus-python` present — `sudo dnf install
nautilus-python`):

- **RPM:** packaged to `/usr/share/nautilus-python/extensions/`; `dnf remove`
  deletes it.
- **Flatpak:** the app copies the bundled extension to
  `~/.local/share/nautilus-python/extensions/` on first run (the sandbox can't
  write it at build time). `flatpak uninstall` can't remove it — the purge
  script (`scripts/uninstall-cloudy.sh --purge`) does.
- **Dev (`make`):** `make install-nautilus` / `make uninstall-nautilus`.

Run `nautilus -q` once after first install so Nautilus loads it. It adds an
**Unmount (Cloudy)** item when you right-click inside a mounted library.

## Troubleshooting

- **Schema not found at runtime** → ensure `GSETTINGS_SCHEMA_DIR` points at the
  installed schemas, or run via Flatpak.
- **Blueprint errors** → confirm `blueprint-compiler` ≥ 0.12 or let the wrap
  fetch it.
- **Nautilus extension not loading** → clear `__pycache__`, run `nautilus -q`,
  confirm `python3-nautilus` 4.x is installed.
