<!--
SPDX-License-Identifier: GPL-3.0-or-later
SPDX-FileCopyrightText: 2026 Shahab Nedaei
-->

# Cloudy — Agent Guidance

This file is the single source of truth for automated coding agents working in this repository. It complements `CLAUDE.md` and `README.md` with actionable, agent-specific rules. Read it before every non-trivial change.

## Project at a glance

- **Name:** Cloudy
- **Stack:** GTK4 / Libadwaita, Python 3 / PyGObject, Meson, Blueprint UI
- **Target platform:** Fedora 44 / GNOME 50
- **What it does:** GNOME-native "super-app" that orchestrates Microsoft 365 (OneDrive, SharePoint, Mail, Calendar, Teams chat, Teams channels, OneNote) and Google (Gmail, Calendar, Drive, Chat) behind one adaptive window.
- **Backends:** `rclone` (FUSE file mounts), Microsoft Graph REST, Google REST. The app does **not** reimplement sync/mail protocols.
- **App ID:** `io.github.sha5b.Cloudy` (used for GSettings, D-Bus, desktop, metainfo, gresource).
- **Version:** see `meson.build`.

## Quick commands

```bash
# Build / run / test
make build        # meson compile (auto-configures _build/ first)
make test         # meson test: schema/desktop/metainfo/blueprint validators + unit suite
make test-unit    # headless logic unit tests only (fast, no schema/build needed)
make lint         # py_compile over src/ + nautilus-extension/
make run          # build + install into ./_install, then launch
make clean        # wipe _build/, _build/flatpak/, __pycache__

# Packaging
make flatpak flatpak-run   # sandboxed local build
make release               # RPM + .flatpak into release/
```

Dev toolchain is user-space (`meson`/`ninja` via `pip --user`). Ensure `export PATH="$HOME/.local/bin:$PATH"`.

## Verification rules (non-negotiable)

1. **GUI cannot be driven headlessly.** The Wayland single-instance handoff kills the wrapper shell. Verify with:
   - `make build`
   - `make test`
   - `make lint`
   - **Headless import/instantiate smoke test** for affected widget modules (`gi.require_version` then `importlib.import_module` then instantiate classes; `Gtk.init_check()` works headless here).
   - Then ask the user to `make run` to eyeball.
2. **`window.py` and `application.py` cannot be imported standalone** in smoke tests — their `Gtk.Template` needs the compiled gresource. `py_compile` still validates them.
3. **Single-instance app:** quit the running instance before relaunching, or the new launch hands off and exits 0.
4. **New GSettings keys require `make build`** to recompile + reinstall the schema. `Gio.Settings.new()` aborts the process if the schema is missing.
5. **Smoke-test by instantiating widgets**, not just importing. A typo once passed import but crashed `MonthGrid()`.
6. After any packaging/data/icon change, run `make test` to catch schema/desktop/metainfo validators.

## Architecture map

```
src/cloudy/
  application.py          # Adw.Application, lifecycle, actions, do_open, owns core services
  window.py               # Main shell, sidebar, per-account ViewStack, deep links
  preferences.py          # Preferences window
  account_dialog.py       # Account add/remove/sign-in/out dialog
  main.py                 # Entry point
  core/
    interfaces.py         # ServiceModule + capability mix-ins
    plugin_engine.py      # Module discovery
    account_registry.py   # Account model + persistence
    secrets.py            # libsecret wrapper
    credentials.py        # Loads CLOUDY_* env from .env / secrets.env
    cache.py              # MemoryCache (SWR, TTL, persisted offline)
    auth/                 # MSAL (msal_graph.py), Google loopback+PKCE (google_oauth.py)
    dbus_service.py       # D-Bus status service for Nautilus extension
    sync.py               # rclone bisync offline sync
    notifications.py      # New-mail/event polling + banners
    ics.py                # iCal parser + reply builder
    eds_publish.py        # Best-effort EDS calendar mirror
    gi_compat.py          # Optional GI namespace loader (degrade on None)
  modules/
    microsoft365/         # graph.py, files.py, mounts.py, abraunegg.py, module.py
    gmail/                # google_client.py
  widgets/
    clients.py            # build_account_client factory
    source_nav.py         # Shared Me/Teams/Shared source scaffolding + run_async
    files_view.py, mail_view.py, calendar_view.py, chat_view.py,
    teams_view.py, dashboard_view.py, activity_view.py, file_browser.py,
    message_view.py, message_window.py, event_view.py, event_window.py,
    event_time.py, chat_compose.py, compose_view.py, command_palette.py,
    media_window.py, rich_editor.py, editor_window.py, month_grid.py,
    format.py, metrics.py, imaging.py, attachments.py
```

## Conventions to preserve

- **Off-thread work:** use `source_nav.run_async(work, on_done)` (daemon thread + `GLib.idle_add` callback `(result, error)`). Never spawn raw `threading.Thread` directly in views. Capture extra IDs with lambdas.
- **Pango markup safety:** Adw row/title/StatusPage text is parsed as markup — wrap dynamic text with `widgets/format.esc()`. Plain `Gtk.Label` is immune.
- **Graph URL encoding:** encode values containing spaces (e.g. `$orderby=... desc`) or urllib rejects the URL.
- **Account model:** `core/account_registry.py::Account.from_dict` tolerates missing keys but **new fields must be added to the allowlist** or they silently drop on load.
- **Module on/off is per-provider:** toggling a module affects all accounts of that provider.
- **Editor surfaces are non-modal `Adw.Window`s** (`widgets/editor_window.py::EditorWindow`). Do not use modal `Adw.Dialog` for edit-and-submit surfaces.
- **Optional GI namespaces:** route through `core/gi_compat.py::require(namespace, candidates)` and treat `None` as "feature unavailable" (degrade, never crash). `Gtk` (4.0) and `Adw` (1) are intentionally pinned.
- **Network-mount scans are dangerous:** `os.walk` over FUSE mounts can stall or trigger downloads. Keep scanning bounded (`max_scan`, deadline checks).
- **Windows must be non-transient** for GNOME to show minimize/maximize.
- **GTK4 CSS has no `!important`.** Use plain longhand at `PRIORITY_APPLICATION`.
- **Secrets:** real OAuth IDs live in `.env` (gitignored) or `~/.config/cloudy/secrets.env`. The committed repo must contain zero real secrets.

## Common pitfalls (checklist)

- [ ] Did you import UI modules headlessly? Skip `window.py`/`application.py`.
- [ ] Did you add a new setting key? Run `make build` before testing.
- [ ] Does dynamic text touch Adw rows/titles/StatusPage? Use `format.esc()`.
- [ ] Are you doing blocking I/O on the GTK thread? Move it into `run_async`.
- [ ] Did you create a popover menu? `unparent()` on `closed` to avoid leaks.
- [ ] Are timers/callbacks captured in closures that outlive the widget? Check `get_root() is None` or use `weakref`.
- [ ] Did you change the app ID? Update every data file, schema, D-Bus path, icon path, and gresource prefix.
- [ ] Are you using `set_default_action_and_target_value`? Use `Gio.Action.print_detailed_name` + `set_default_action` instead (GLib binding quirk).
- [ ] Did you rely on `localhost` resolving to `127.0.0.1` in OAuth? Use the same literal for bind + redirect_uri.

## Credentials

Build-time credentials are read from `.env`:

```bash
CLOUDY_MS_CLIENT_ID=
CLOUDY_GOOGLE_CLIENT_ID=
CLOUDY_GOOGLE_CLIENT_SECRET=
```

They override the matching GSettings keys at runtime via `core/credentials.py`. Never commit real values.

## Refactoring priorities

If you are asked to "clean up" or "optimize", prioritize in this order:

1. **Memory leaks / orphaned references:** popover unparenting, timer cancellation, callback closures holding widgets.
2. **Main-thread blocking:** network, file I/O, image decode, rclone enumeration.
3. **Repeated expensive work:** rebuild-on-every-poll lists, auth client rebuild every request, N+1 network calls.
4. **Unbounded growth:** seen-mail/event sets, caches, lists, pagination state.
5. **Overcomplicated control flow:** deeply nested conditionals, duplicated helpers, magic constants.

Always keep changes minimal and targeted. Run `make test` and `make lint` after every edit.

## When in doubt

- Read `docs/HANDOFF.md` for current status, known bugs, and deliberately deferred work.
- Read `docs/ARCHITECTURE.md`, `docs/AUTH.md`, `docs/SECRETS.md`, `docs/BUILDING.md`, `docs/MODULES.md`.
- Check `CHANGELOG.md` for recent decisions.
- Do not reimplement anything already listed under "Already investigated — NOT implementable" in `docs/HANDOFF.md`.
