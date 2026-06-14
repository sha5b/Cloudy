<!--
SPDX-License-Identifier: GPL-3.0-or-later
SPDX-FileCopyrightText: 2026 Fiber Elements
-->

# Contributing to Cloudy

Thanks for your interest! Cloudy is a GNOME-native (GTK4 / Libadwaita)
Python application.

## Ground rules

- **License**: by contributing you agree your work is licensed under
  **GPL-3.0-or-later**. Add an SPDX header to every new file:

  ```python
  # SPDX-License-Identifier: GPL-3.0-or-later
  # SPDX-FileCopyrightText: 2026 <your name or org>
  ```

- **Style**: follow the [GNOME programming guidelines][gnome-prog] and the
  [GNOME HIG][hig]. Python code targets the conventions used by other GNOME
  Python apps (PyGObject). Indentation is 4 spaces; UI is written in
  **Blueprint** (`.blp`), not hand-written `.ui` XML.
- **Commits**: use clear, imperative subject lines. Keep unrelated changes in
  separate commits.

## Development environment

GNOME Builder is the recommended IDE — open the project and it picks up the
Flatpak manifest automatically. See [docs/BUILDING.md](docs/BUILDING.md) for the
command-line workflow.

## Code layout

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). In short:

- `src/core/` — app shell internals: module engine, account registry, secrets, auth.
- `src/modules/` — one package per service (OneDrive, Graph mail, Gmail).
- `src/widgets/` — reusable GTK4 widget subclasses.
- `data/ui/` — Blueprint UI definitions.
- `nautilus-extension/` — the host-side Nautilus (`nautilus-python`) extension.

## Adding a module

Implement the interfaces in [src/core/interfaces.py](src/core/interfaces.py)
(`ServiceModule` + the relevant capability mix-ins) and register it. See
[docs/MODULES.md](docs/MODULES.md) for a step-by-step guide.

## Before opening a PR

- `meson compile -C _build` succeeds.
- The app launches and your change behaves as described.
- New strings are translatable and added to `po/POTFILES.in`.

[gnome-prog]: https://developer.gnome.org/documentation/guidelines/programming.html
[hig]: https://developer.gnome.org/hig/
