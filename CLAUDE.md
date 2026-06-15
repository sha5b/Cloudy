# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Cloudy is a **GTK4 / Libadwaita (Python / PyGObject)** super-app for **Microsoft 365** (OneDrive + Teams/SharePoint, Mail, Calendar) and **Google** (Gmail, Calendar, Drive) on **Fedora 44 (GNOME 50)**. It *orchestrates* proven backends (rclone for file mounts; Microsoft Graph / Google REST for mail/calendar) rather than reimplementing them.

`docs/HANDOFF.md` is the living, detailed status/gotchas doc — read it for depth. `docs/ARCHITECTURE.md`, `docs/AUTH.md`, `docs/SECRETS.md` cover specific subsystems.

## Build / run / test

Toolchain here is **user-space**: `meson`/`ninja` via `pip --user` — ensure `export PATH="$HOME/.local/bin:$PATH"`.

```bash
make run      # build + install into ./_install, then launch ./_install/bin/cloudy
make build    # meson compile (auto-configures _build/ first)
make install  # install into local prefix (prunes the old package tree first)
make test     # meson test: 4 validation tests (desktop/schema/metainfo/blueprint)
make lint     # just py_compile over src + nautilus-extension (no ruff/pyflakes here)
make clean
```

- **GUI can't be driven from a headless/agent shell** (the Wayland single-instance handoff kills the wrapper shell). Verify changes with: `make build` + `make test` + a **headless import smoke test** (`gi.require_version(...)` then `importlib.import_module` each widget module) + `py_compile`. Then ask the user to `make run` to eyeball.
- `window.py` **cannot be imported standalone** in a smoke test — its `Gtk.Template` needs the compiled gresource. Skip it (and `application.py`, which imports it). py_compile still validates them.
- **Single-instance** app: quit a running instance before relaunching, or a new launch just hands off and exits 0.
- **New GSettings keys require `make build`** (recompiles + reinstalls the schema). `Gio.Settings.new()` *aborts the process* if the schema isn't installed.
- App icons: edit the SVG at `data/icons/hicolor/scalable/apps/io.github.sha5b.Cloudy.svg`, then regenerate the PNG sizes with `magick` (the only raster tool available here — no rsvg/inkscape) into `data/icons/hicolor/<size>x<size>/apps/` (sizes 48/64/128/256/512, all wired into `data/icons/meson.build`).

## Credentials

Real OAuth IDs/secrets live **outside git** in `.env` (repo root, gitignored) or `~/.config/cloudy/secrets.env`, loaded into `CLOUDY_*` env on startup by `core/credentials.py`. The committed repo has zero real secrets. Env vars (`CLOUDY_MS_CLIENT_ID`, `CLOUDY_GOOGLE_CLIENT_ID`, `CLOUDY_GOOGLE_CLIENT_SECRET`) override the matching GSettings keys.

## Architecture

**Two independent capability domains, one shell:**

- **Files = rclone FUSE mounts** (`modules/microsoft365/mounts.py`). rclone does its *own* browser auth (built-in app id), reused per account. Mount → live two-way network drive (not a synced copy) that appears in Nautilus and the in-app browser. Mount layout (`mount-layout` setting) is `one-folder` or `individual` (per-account `Account.mount_location`).
- **Mail + Calendar = REST clients** built per account by `widgets/clients.py::build_account_client` → `GraphClient` (`modules/microsoft365/graph.py`) or `GoogleClient` (`modules/gmail/google_client.py`). **Both clients return the same normalized dict shapes** so the views are provider-agnostic.

**Auth** (`core/auth/`): Microsoft = MSAL (`msal_graph.py`); Google = hand-rolled loopback+PKCE on urllib (`google_oauth.py`). Tokens live in **libsecret** (`core/secrets.py`). Sign-in requests **all** scopes up front (concatenated in `window.py::_microsoft_sign_in_worker` / the Google default list) so one consent covers everything. **Adding a scope forces existing accounts to Sign Out → Sign In** — the Mail/Calendar views surface this with an inline "Re-sign in" button on scope errors (`is_scope_error`).

**Shared/group sources** (Microsoft only): Mail & Calendar share a **Me / Teams / Shared** source model. Common scaffolding is in `widgets/source_nav.py`: `SourceTabs`, `run_async`, listbox/placeholder helpers, `is_scope_error`, the add-shared dialog, and pin helpers. IDs are prefixed `shared:<addr>:` / `group:<id>:` so `get_message`/`get_event` route back to the right API path + scope.

**The off-thread pattern** — `source_nav.run_async(work, on_done)` runs `work()` on a daemon thread and delivers `(result, error)` back on the GTK main loop via `GLib.idle_add`. **Use this for all network/blocking work**, never raw threads in views. Capture extra ids with a lambda: `lambda res, err: self._on_x(id, res, err)`.

**Caching** (`core/cache.py`): a `MemoryCache` on `app.cache` (stale-while-revalidate, 90s TTL), keyed `"<account_id>:<kind>:<id>"`. Views render cached data instantly, then revalidate. `cache.invalidate(prefix=account.id)` on Refresh.

**Editor surfaces are non-modal windows** (the project convention): subclass `widgets/editor_window.py::EditorWindow` (a non-modal `Adw.Window` with Cancel + primary button + in-window toasts). `ComposeWindow` and `EventWindow` follow this so the user can copy from other mail while editing. **Do not use modal `Adw.Dialog` for edit-and-submit surfaces.**

**UI shell** (`window.py`): sidebar (Overview + accounts) → per-account `Adw.ViewStack` over Files/Mail/Calendar with a `ViewSwitcher` in the account header. The account header is the *only* one with window controls — inner split-view headers must set `show_start_title_buttons=False, show_end_title_buttons=False` or controls render twice. `open_mail(account, mid)` and `open_account_tab(account, tab)` are deep-link entry points (used by the Dashboard and notifications).

**Application lifecycle** (`application.py`): `CloudyApplication(Adw.Application)` with `HANDLES_OPEN` so it acts as the system Mail/Calendar handler — `do_open` routes `mailto:` → `ComposeWindow` and `.ics` → `EventWindow` (see `window.open_compose_from_mailto` / `open_event_from_ics`). It owns `registry`, `cache`, `engine`, `secrets`, `sync_manager`, `notifier`. Background mode (`run-in-background`, default off): `window` close-request hides + `app.hold()` instead of quitting.

**Desktop integration**: `core/notifications.py` polls for new mail / upcoming events and raises `Gio.Notification` (use `Gio.Action.print_detailed_name` + `set_default_action` — `set_default_action_and_target_value` is NOT in this GLib binding). `core/eds_publish.py` best-effort mirrors the user's calendar into a local Evolution Data Server calendar (guarded; off by default). `core/dbus_service.py` exports a status service the host Nautilus extension reads for emblems.

**Modules** (`core/plugin_engine.py` + `core/interfaces.py`): provider capabilities are declared per `ServiceModule`; `capabilities_of(module)` drives which tabs an account shows. Module on/off is **per provider** (`enabled-modules` setting toggles the whole `module_id`, shared by all accounts of that provider).

## Conventions / gotchas

- **Pango markup**: Adw row/title/StatusPage text is parsed as markup — wrap dynamic text with `widgets/format.esc()`. Mail/agenda lists use plain `Gtk.Label` (immune).
- **Graph query URLs**: encode values containing spaces (e.g. `$orderby=... desc`) or urllib rejects the URL ("can't contain control characters").
- **`Account` model** (`core/account_registry.py`): `from_dict` tolerates missing keys, so adding fields is safe; removing one just drops it on next save. Extra fields: `full_sync`, `mount_location`, `shared_mailboxes`, `pinned_sources`.
- **Network-mount scans are dangerous**: `os.walk` over a FUSE mount can stall / trigger downloads. Keep any scanning bounded (see `file_browser.recent_changes` `max_scan`).
- **Accessing your *own* address as a "shared" source returns Graph 403** — expected; use the **Me** source for your own mailbox.
- **Google "Testing" publishing status** expires refresh tokens after 7 days.
- `make install` prunes the installed package tree first (meson never deletes), so renamed/removed modules don't linger as phantom providers.
- **Forward-compat for optional namespaces**: never hard-pin a *minor* version of an optional GI namespace. Route WebKit / EDS (EDataServer, ECal, ICalGLib) / Xdp — and any new optional integration — through `core/gi_compat.py::require(namespace, candidates)`, and treat a `None` return as "feature unavailable" (degrade, never crash). `Gtk` (4.0) and `Adw` (1) stay pinned on purpose — they're major API contracts. The Nautilus extension tries `4.1`/`4.0` for the same reason.
