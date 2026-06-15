<!--
SPDX-License-Identifier: GPL-3.0-or-later
SPDX-FileCopyrightText: 2026 Shahab Nedaei
-->

# Cloudy — Handoff / Continue Here

Pick-up doc for a fresh session. Cloudy is a **GTK4 / Libadwaita (Python /
PyGObject)** super-app for **Microsoft 365 (OneDrive + Teams/SharePoint, Mail,
Calendar)** and **Google (Gmail, Calendar, Drive)** on Fedora 44 (GNOME 50). It
*orchestrates* proven backends (rclone for mounts; Microsoft Graph / Google REST
for mail/calendar) rather than reimplementing them. Read `docs/ARCHITECTURE.md`,
`docs/AUTH.md`, `docs/SECRETS.md`, `docs/ROADMAP.md` for depth.

## ⏭ Continue here — latest session (2026-06-15)

### Done this session
- **Inline event edit** (NEXT #1, see below) — shipped + built into RPM/Flatpak.
- **Refactor**: shared event date/time helpers extracted to
  `widgets/event_time.py` (`iso_to_local_naive`, `parse_hhmm`,
  `local_to_utc_iso`); `event_window.py` + `event_compose.py` both use them.
- **Full-app bug audit** (7 parallel review passes). High-confidence fixes
  applied; the rest triaged below as **Known issues**.
- **Bug fixes applied**:
  1. `update_event` (both clients) now treats `attendees=None` as "leave
     untouched" but a list — **including `[]`** — as "set it", so removing every
     attendee in the inline editor actually clears them server-side (was a
     no-op: `if attendees:` skipped empty lists).
  2. Inline editor **multi-day events no longer collapse to one day** — the
     original start→end day span is preserved across an edit (`_edit_day_span`).
  3. `notifications.py` priming timer was sharing `_tick` (which returns `True`)
     so the "prime once" timer fired **forever** alongside the steady timer,
     doubling poll traffic — now a one-shot `_prime_once` returning `False`.
- **App-ID rename** `io.github.sha5b.Clouddrive` → **`io.github.sha5b.Cloudy`**
  (data files, schema id, D-Bus name/paths, icons, gresource prefix, Makefile,
  uninstall script). Repo URLs kept as `Clouddrive-Fedora` (repo renamed later).
  Best-effort dconf migration in `application._migrate_legacy_settings` carries
  accounts/prefs over (works on host/RPM; Flatpak runtime has no `dconf` CLI →
  re-add accounts there once).
- **Per-account mountpoints** (`mounts.mount_base_for`): each account's drives
  mount under their own folder so same-named drives across accounts don't
  collide and a mount is attributable per account (no more re-mounting). D-Bus
  status walks ancestors for the new nesting. Pruned stale flat-mount bookmarks.
- **Nautilus extension**: removed all unmount/mountpoint code (nautilus-python
  can't add sidebar items — unmount is via in-app Files → Unmount or native ⏏).
- **Design system** (all 4 audit recommendations): `data/style.css` (loaded in
  `application._load_styles`), `widgets/metrics.py` 4px scale + window sizes,
  `source_nav.status_page`/`loading_box`, `.cloudy-meta`/`.cloudy-day`/
  `.cloudy-chip`/`.cloudy-pill`/`.cloudy-section`, title-hierarchy fix, dead
  `preferences.blp` removed, POTFILES completed. **Helpers are in place; rolling
  `.cloudy-meta`/`loading_box` into every remaining caption/loading spot is
  incremental polish best done with the app open to eyeball.**

### Known issues (from the audit — NOT yet fixed, triaged by priority)
**Security / privacy (needs a deliberate decision — behavior change):**
- **Google OAuth has no `state` parameter** (`core/auth/google_oauth.py`) — CSRF /
  auth-code-injection gap; PKCE covers the token exchange but not session
  binding. Add a random `state`, validate on the redirect.
- **Google OAuth stores `code`/`error` on the _class_, not the instance** — two
  concurrent Google sign-ins race and cross-contaminate. Move result state onto
  the per-flow `HTTPServer` instance.
- **OAuth loopback binds `127.0.0.1` but the `redirect_uri` says `localhost`** —
  Google treats these as distinct redirect URIs and `localhost` may resolve to
  IPv6 `::1`; can make sign-in hang/fail. Make both use the same literal.
- **Mail reader loads remote content** (`message_view.py`) — only JS is disabled;
  external images load → tracking-pixel/IP leak on open. Consider blocking remote
  resources by default with a "load remote images" opt-in.

**Correctness (safe to fix, just not done yet):**
- **No `@odata.nextLink` pagination** anywhere in `graph.py` (folders, groups,
  contacts, drives, calendarView) — accounts with >`$top` items silently
  truncate. Loop on `@odata.nextLink`.
- **Mount success/failure mis-detected** (`mounts.py`): `rclone mount --daemon`
  forks and returns 0 before the FUSE mount exists, so `is_mounted()` right after
  reports `active=False` and real mount failures are swallowed. Poll with a short
  timeout + capture daemon stderr.
- **`recent_changes` walk isn't bounded _within_ a single directory**
  (`file_browser.py`) — the deadline/count check is only at the top of the
  per-dir loop, so one huge dir on a FUSE mount blocks past the budget. Check the
  deadline in the inner loop and prune `dirnames` when over budget.
- **Google `reply_all` drops CC/other recipients** (`google_client.py`) — only
  replies to the original sender even when `reply_all=True`.
- **Google all-day `end` is exclusive** (`_event_from_json`) — off-by-one vs the
  Graph shape in views that compute/display the end day.
- **`respond_event` only blocks `group:` ids** (`graph.py`) — a `shared:` id would
  be prefixed into a `/me/events/shared:…/accept` path; reject/route it.
- **Unbounded dedup growth** (`notifications.py`) — `_seen_mail` /
  `_notified_events` only ever grow; cap/trim them (matters in background mode).

**Low / robustness:** `create_share_link` hardcodes `scope:"organization"`
(invalid for consumer OneDrive); Google `get_event` `body_html` heuristic
(`"<" in s and ">" in s`) false-positives plain text; cid-image strip regex in
`message_view.py` over-matches on `>` in attribute values and misses unquoted
`src=cid:`; `file_browser` rename/new-folder accept names with `/`/`..`
(path traversal within the browser); EDS publish builds a bare `VEVENT` with a
likely-wrong parent UID so it may never publish. Full per-finding detail is in
the session transcript.

## ⏭ Continue here — session (2026-06-14)

A very large session. **Everything below is built + reinstalled into the Flatpak,
but NOT committed to git** — first task next time: review the working tree and
commit on a branch (logical commits).

### Done this session
- **Rebrand**: `com.fiberelements.Cloudy` → **`io.github.sha5b.Cloudy`**;
  author → **Shahab Nedaei <ned.tabulov@gmail.com>** (sha5b). All file names,
  schema id, D-Bus name/paths, icons renamed.
- **Packaging**: Fedora **RPM** (`packaging/cloudy.spec`, noarch, meson),
  `make rpm`/`srpm`; **Flatpak bundle** `make flatpak-bundle`; **`make release`**
  → `release/` (RPM + single-file `.flatpak`) **and installs the bundle** so the
  running app matches. `release/` is gitignored (artifacts embed baked creds).
- **Credentials** baked at build time via `meson.options`
  (`ms_client_id`/`google_client_id`/`google_client_secret`) → a generated
  GSettings **vendor override** (`data/cloudy.gschema.override.in`); read from
  `.env` by the Makefile. Source ships empty defaults. Security-audited: no
  secrets in git history/tree.
- **Host-visible Flatpak mounts**: rclone runs on the host via `flatpak-spawn
  --host` into `~/.local/share/cloudy/mounts` (manifest grants
  `--talk-name=org.freedesktop.Flatpak` + `--filesystem`). `mounts.active_mounts()`
  reads `/proc/self/mountinfo` (stall-proof) — fixed the dashboard hang.
- **Nautilus**: quick **Unmount (Cloudy)** menu item (file view, not sidebar —
  API can't touch sidebar bookmarks); extension **auto-installs** (RPM → system
  path; Flatpak → copied to host on first run via `provisioner.ensure_host_
  nautilus_extension`). Fixed dotted D-Bus `OBJECT_PATH`.
- **Calendar redesign**: month **grid** (`widgets/month_grid.py`) in the Calendar
  tab + Dashboard; **past events** (loads the visible month); clicking an event
  opens a **non-modal event window** (`widgets/event_window.py`) with detail +
  RSVP + delete + **Edit**.
- **Meeting responses**: attendee **response tracker** on the event (pills grouped
  by status via `Adw.WrapBox`); empty-bodied accept/decline emails show a
  placeholder (see gotcha below).
- **Dashboard**: aggregate **cached** (stale-while-revalidate, no reload on every
  switch) + a Refresh button; pinned sources' **unread mail + events merged**
  into the overview.
- **Contacts autocomplete**: MS via **People API** (`/me/people` + `/me/contacts`,
  new `People.Read` scope); Google connections + **otherContacts** (new
  `contacts.other.readonly`). Both need **re-sign-in** (see below).
- **Account add** now auto-enables the provider's module (Google wasn't
  activating — `enabled-modules` defaulted to microsoft365 only).
- Fixes: WebKit blank mail (`WEBKIT_DISABLE_DMABUF_RENDERER=1` in `main.py` +
  manifest); strip `cid:` inline images; `update_event` (Graph + Google) for the
  Edit flow; non-transient editor/event windows so **minimize/maximize** show;
  event-compose timezone (local→UTC); escaped `&` in a preferences title.

### NEXT (asked for, deferred to here)
1. ~~**Inline event edit**~~ — **DONE** (next session, 2026-06-15). The Edit (✏️)
   button now toggles `EventDetailWindow`'s detail pane into an inline edit form
   (subject, all-day, day, start/end time, location, **removable attendees**,
   description) with Save/Cancel in the header → `client.update_event(eid, …)`,
   then reloads the detail. `get_event` now returns attendees with `email` (Graph
   `emailAddress.address` / Google `email`) so the editor re-sends the full
   desired attendee list (PATCH keeps attendees if omitted). Description prefills
   only **plain-text** bodies — HTML-bodied events start blank (empty body is
   omitted on PATCH, so the server's is preserved unless the user types).
   *Edge*: removing **all** attendees doesn't clear them server-side, because
   `update_event` only sends `attendees` when the list is non-empty.
   `event_compose.EventWindow` is still used for **New** event creation.
2. **Contacts dropdown**: only suggests after **Sign Out → Sign In** per account
   (grants `People.Read` / `contacts.other.readonly`). If it still doesn't show
   after re-consent, `GtkEntryCompletion` is deprecated/flaky in GTK4 — replace
   `compose_view._setup_completion` with a custom suggestion popover.
3. **Commit** the session.

### Gotchas learned this session
- **Smoke-test by INSTANTIATING widgets**, not just importing — see
  `[[cloudy-widget-smoke-test]]`. `Gtk.init_check()` works headless here; a
  `monthdatescal` typo passed import but crashed `MonthGrid()`.
- **`meetingMessageType`** can't be `$select`ed or entity-cast on the messages
  endpoint (both 400) — the "X accepted" card was dropped; empty meeting-response
  emails fall back to the "No message content" placeholder.
- **`make release` installs the bundle**; other flatpak builds need
  `make flatpak-test` to update the *running* app (stale-build confusion bit us
  repeatedly — symptoms looked like the fix didn't work).
- **Windows must be non-transient** (no `transient_for`) for GNOME to show
  minimize/maximize.

## Build / run / test
```bash
cd <repo>
make run            # meson build+install into _install, then launch ./_install/bin/cloudy
make build|test|lint|clean
make flatpak flatpak-run   # sandboxed (org.gnome.Platform 50)
```
- Dev toolchain here is **user-space**: `meson`/`ninja` via `pip --user`
  (`export PATH="$HOME/.local/bin:$PATH"`); `blueprint-compiler` auto-fetched via
  the wrap; `msal` via `pip --user`. `rclone` is **auto-provisioned** (rootless)
  into `~/.local/share/cloudy/bin/rclone` on first run; also bundled in the Flatpak.
- The app is **single-instance** (GApplication) — quit the running one before
  relaunching, or a new launch just hands off and exits 0.
- `make lint` is just `py_compile` (no pyflakes/ruff installed here). Useful extra
  checks: `python3 -m py_compile <files>`; a **headless import smoke test**
  (`gi.require_version` then `importlib.import_module` each widget module —
  catches missing imports/NameErrors without a display). NOTE: `window.py` can't
  be imported standalone (its `Gtk.Template` needs the compiled gresource); skip
  it in smoke tests. `meson test` runs 4 validation tests (desktop/schema/
  metainfo/blueprint).
- **Driving the GUI from a headless/agent shell fails** (the Wayland app handoff
  signals and kills the wrapper shell, exit 144) — verify via build + tests +
  import/logic smoke, then ask the user to `make run` to eyeball.

## Credentials (already set up locally; repo is public-safe)
- **Microsoft**: multi-tenant Entra client ID `dcd8ee18-6e62-4c5a-b01f-86f9556f8fed`
  (public client — not a secret). **Google**: Desktop OAuth client.
- Real values live **outside git** in `.env` (repo root, gitignored) and/or
  `~/.config/cloudy/secrets.env`, loaded into `CLOUDY_*` env on startup by
  `core/credentials.py`. The committed repo contains **zero** real IDs/secrets.
  `.env.example` is the template. See `docs/SECRETS.md`.
- Env vars: `CLOUDY_MS_CLIENT_ID`, `CLOUDY_GOOGLE_CLIENT_ID`,
  `CLOUDY_GOOGLE_CLIENT_SECRET` (also GSettings keys; env wins).
- ⚠️ The Google client secret was pasted in chat during setup — **rotate it**
  in Google Cloud Console before any public release.

## How it works (key decisions)
- **Auth**: system browser + loopback. Microsoft = MSAL (`core/auth/msal_graph.py`);
  Google = hand-rolled loopback+PKCE on urllib (`core/auth/google_oauth.py`).
  Tokens in **libsecret** (`core/secrets.py`). Sign-in requests **all** scopes up
  front (Files+Teams+Groups+Mail+Calendar+**Mail.ReadWrite.Shared**) so one
  consent covers everything, **including shared mailboxes/calendars**.
- **Shared/group sources**: Graph `list_shared_folders` / `list_shared_events`
  use `/users/{address}` + `Mail.ReadWrite.Shared`; group mail/calendars use
  `/groups/{id}` + `Group.Read.All`. IDs are prefixed `shared:<addr>:` /
  `group:<id>:` so `get_message`/`get_event` route back correctly. Accessing your
  **own** address as a "shared" source returns Graph 403 ErrorAccessDenied —
  that's expected (use the **Me** source for your own mailbox).
- **Mail & Calendar share one source model** (Microsoft only): **Me / Teams /
  Shared** tabs. The common scaffolding lives in **`widgets/source_nav.py`**:
  `SourceTabs`, `run_async(work, on_done)` (the off-thread→idle_add helper used by
  every view), `clear_listbox`, `message_row`/`action_row` placeholders,
  `is_scope_error`, `present_add_shared_dialog`, and the **pin** helpers
  (`toggle_pin`/`is_pinned`/`find_pin`). When a shared/group call fails for lack
  of scope, the view shows a **Re-sign in** action row (re-consent grants the new
  scope; everyday mail keeps working).
- **Files = rclone mounts** (`modules/microsoft365/mounts.py`): rclone does its
  **own** browser auth (built-in app id → no registration), reused per account.
  Mount → FUSE network drive + a GTK sidebar bookmark → appears in Nautilus.
  **It's a live network drive (two-way), not a synced copy.** Cache mode + mount
  location come from Settings. **Mount layout** (`mount-layout` setting): either
  `one-folder` (everything under the global mount location) or `individual` (each
  account picks its own folder via `Account.mount_location`). `mountpoint_for`/
  `mount` take an optional `base` override; `account_mount_base(loc)` resolves it.
- **In-app file browser** (`widgets/file_browser.py`): the Files tab is an
  `Adw.NavigationView` — **Libraries** (mount toggles) at the root; a *mounted*
  library row is clickable → pushes a `FileBrowserPage` that lists the mountpoint
  (folders first, drill in, click a file → opens in the default app). Listing
  runs off-thread; `recent_changes(roots)` (bounded scan) powers the Dashboard.
- **Offline sync** (`core/sync.py`): when `default-sync-type` = `full` and an
  account's per-account toggle is on, `SyncManager` runs `rclone bisync` into
  `…/cloudy/synced` on a timer. When type = `stream`, the per-account toggle is
  disabled (mounting stays manual). Streaming auto-mount-on-login is **not** built.
- **Caching**: `core/cache.py` MemoryCache on `app.cache` (stale-while-
  revalidate, 90s TTL) for mail/calendar; per-source cache keys; Refresh
  invalidates per account.
- **Nautilus**: app exports a D-Bus status service (`core/dbus_service.py`); host
  extension draws emblems + menu (`make install-nautilus`).
- **UI shell** (`window.py`): sidebar (Overview + accounts) → per-account
  `ViewSwitcher` over Files/Mail/Calendar; header Refresh.
  `open_mail(account, mid)` and `open_account_tab(account, tab)` are deep-link
  entry points used by the Dashboard. A **turned-off** account (its module
  disabled) shows "Turned off" in the sidebar and a disabled status page.
- **Dashboard** (`widgets/dashboard_view.py`): **Pinned** (starred shared/group
  sources with live counts, click to jump) → **Upcoming** (your calendars) →
  **Recent mail** → **Recent file changes** (newest edits in mounted/synced dirs).
- **Preferences** (`preferences.py`), two pages only:
  - **General** — Mount location · Mount layout · File caching · Sync type
    (stream/full) · Start at login.
  - **Accounts** — each account is an `ExpanderRow`: an **on/off switch** for its
    services (replaces the old Modules tab → `enabled-modules` setting), Sign
    In/Out, Remove; expands to **Sync files offline** + **Mount location** (both
    shown but greyed until their General prerequisite is set).
- **Pinning ("star")**: the ★ button in Mail/Calendar (Teams/Shared sources)
  toggles `Account.pinned_sources` entries
  `{kind: mail|calendar, source: shared|teams, id, name}`; the Dashboard renders
  them.

## Gotchas / conventions (don't relearn the hard way)
- **Use `source_nav.run_async`** for off-thread work, not raw
  `threading.Thread`+`GLib.idle_add` — callback signature is `(result, error)`;
  capture extra ids via a lambda (`lambda res, err: self._on_x(id, res, err)`).
- **Pango markup**: Adw row/title/StatusPage text is parsed as markup — wrap
  dynamic text with `widgets/format.esc()`. Mail/agenda lists use plain
  `Gtk.Label` (immune).
- **Graph URLs**: encode query values with spaces (e.g. `$orderby=... desc`) or
  urllib aborts ("URL can't contain control characters").
- **GSettings**: `Gio.Settings.new()` *aborts the process* if the schema isn't
  installed — look it up via `SettingsSchemaSource` first (see `mounts._setting`).
  New schema keys need `make build` (recompiles + reinstalls the schema).
- **New scopes need re-consent**: existing accounts must re-sign-in (Preferences →
  Accounts → Sign Out, then Sign In) to pick up newly-added scopes. The Mail/
  Calendar views surface this with an inline **Re-sign in** button on scope errors.
- **`Account` model** (`core/account_registry.py`): `from_dict` tolerates missing
  keys, so adding fields is safe; removing a field just drops it on next save
  (this is how `group_calendars` was retired). Current extra fields:
  `full_sync`, `mount_location`, `shared_mailboxes`, `pinned_sources`.
- **Network-mount scans are dangerous**: `os.walk` over a FUSE mount can stall /
  trigger downloads. `recent_changes` is bounded by `max_scan`; keep any new
  scanning bounded too.
- **Module on/off is per-provider**: the Accounts on/off switch toggles the whole
  `module_id` (`enabled-modules`), so all accounts of a provider share it.
- **Google "Testing" publishing status** expires refresh tokens after 7 days;
  publish to production for longer-lived use.
- **meson install doesn't prune**: `make install` removes the installed package
  tree first so renamed/removed modules don't linger.

## Status: done (working, all builds + 4 meson tests green)
Sign-in (MS + Google), Files (OneDrive + Teams + Google My Drive) with
Mount↔Unmount **and an in-app file browser**, Mail and Calendar both with
**Me/Teams/Shared** sources + shared-mailbox add + **★ pin to Dashboard** + inline
re-sign-in on scope errors, message reader, event detail + RSVP, **reworked
Dashboard** (pinned/upcoming/mail/file-changes), **reorganized Preferences**
(General vs Accounts; per-account services on/off, offline-sync toggle, mount
location; **Modules tab removed**), mount layout (one-folder/individual) +
per-account mount location, caching + Refresh, rclone auto-provision, secrets,
Nautilus D-Bus + extension. Shared view code deduped into `widgets/source_nav.py`.

## Next steps (the backlog)
1. **Verify shared/group sources end-to-end** against a *real* shared mailbox /
   group the user has delegated access to (not their own address — that 403s).
2. **Streaming sync activation** — make the per-account toggle, when sync type =
   `stream`, actually auto-mount the account's libraries at startup (today it's
   disabled; only `full` bisync is wired).
3. **Calendar grid** — real month/agenda view (currently a 7-day list).
4. **Live transfer status** — mount rclone with `--rc`, poll `core/stats` for
   ↓/↑ activity; feed the D-Bus service so Nautilus shows transferring/online.
   This is also the better long-term source for the Dashboard "Recent file
   changes" than walking the mount.
5. **Compose/reply** for mail; **file ops** (rename/delete/upload) in the browser.
6. **Multi-account-per-module**: the Accounts on/off switch currently toggles the
   whole module (all accounts of that provider). If multiple same-provider
   accounts become common, add a real per-account `enabled` flag.

## Layout
`src/cloudy/{main,application,window,preferences,account_dialog}.py`,
`core/` (interfaces, plugin_engine, account_registry, secrets, cache,
credentials, provisioner, dbus_service, sync, auth/), `modules/microsoft365/`
(graph, files, mounts, abraunegg), `modules/gmail/` (google_client),
`widgets/` (files/mail/calendar/dashboard/message/event views, **source_nav**,
**file_browser**, clients, graph_helper, format). Data in `data/` (gschema,
desktop, metainfo, blueprints, icons), Flatpak manifest
`io.github.sha5b.Cloudy.yml`.
```
widgets/source_nav.py   shared: SourceTabs, run_async, listbox/placeholder
                        helpers, is_scope_error, add-shared dialog, pin helpers
widgets/file_browser.py in-app browser (FileBrowserPage) + recent_changes()
```
