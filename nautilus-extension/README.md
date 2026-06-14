<!--
SPDX-License-Identifier: GPL-3.0-or-later
SPDX-FileCopyrightText: 2026 Fiber Elements
-->

# Cloudy Nautilus extension

This extension runs **on the host**, inside the Nautilus process — Nautilus does
not load extensions from inside a Flatpak sandbox. It communicates with the
sandboxed Cloudy app over **D-Bus** (`com.fiberelements.Cloudy`).

## What it provides

- **Right-click controls** (`MenuProvider`): *Copy OneDrive Share Link*, *Free Up
  Space*, *Sync This Folder*.
- **Sync-status emblems** (`InfoProvider`): per-file badges reflecting the
  daemon's state.

## Requirements

- `python3-nautilus` (the **4.x** bindings — Nautilus 43+/GTK4).
  On Fedora: `sudo dnf install nautilus-python`.

## Install

```bash
mkdir -p ~/.local/share/nautilus-python/extensions
cp cloudy_nautilus.py ~/.local/share/nautilus-python/extensions/
nautilus -q   # restart Nautilus so it reloads extensions
```

If you change the file, clear `__pycache__` in that directory and run
`nautilus -q` again.

## API notes (nautilus-python 4.0)

- `MenuProvider.get_file_items(files)` — **no window argument** (changed in 4.0).
- `PropertyPageProvider` was replaced by `PropertiesModelProvider`.
- Menu-item ordering is influenced by the extension filename (alphabetical).
