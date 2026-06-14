<!--
SPDX-License-Identifier: GPL-3.0-or-later
SPDX-FileCopyrightText: 2026 Fiber Elements
-->

# Module system

Cloudy's functionality is delivered by **modules**. A module is a self-
contained Python package under `src/modules/` that implements the contracts in
[`src/core/interfaces.py`](../src/core/interfaces.py).

## One module = one provider = one account = one login

A module represents a **provider the user signs into once**, surfacing every
capability that login grants. A Microsoft 365 account is a single Graph OAuth
token that provides OneDrive/SharePoint files **and** mail **and** calendar — so
it is **one** `microsoft365` module implementing all three capabilities, not
separate "OneDrive" and "Outlook" modules that would each demand their own
login. The shell renders surfaces per *capability*; auth/token is shared across
them. The same holds for Google (Gmail + Calendar = one `gmail` provider).

## Interfaces

```python
class ServiceModule:
    """Base contract every module implements."""
    id: str          # e.g. "microsoft365"
    name: str        # human-readable, translatable
    icon_name: str   # symbolic icon

    def activate(self, ctx): ...      # called when enabled
    def deactivate(self): ...
    def preferences_page(self): ...   # -> Adw.PreferencesPage | None
    def status(self) -> ModuleStatus: ...
```

Capability mix-ins declare *what* a module can surface in the UI:

- `FilesCapability` — sync/mount control, drive listing, share links.
- `MailCapability` — folders, message list, threads, send.
- `CalendarCapability` — calendars, events, free/busy.

A module implements `ServiceModule` plus any capabilities it supports. The shell
queries capabilities to decide which sidebar surfaces and content panes to show.

## Discovery (now → later)

**Now:** `plugin_engine.py` scans `src/modules/` for packages exposing a
`MODULE` class and instantiates those implementing `ServiceModule`. Simple,
no extra dependency.

**Later:** migrate to **`libpeas-2`** (the engine behind gedit/Builder/Totem).
Benefits: multiple extension points (define your own `GInterface`s), lazy
language loaders, and `PeasEngine` as a `GListModel` of `PeasPluginInfo` you can
bind directly to a Libadwaita toggle list. The **interface contracts stay the
same**, so this migration is internal.

## Adding a module — checklist

1. Create `src/modules/<name>/__init__.py` exposing `MODULE = <YourModule>`.
2. Implement `ServiceModule` + the capability mix-ins you support.
3. Store secrets through `core.secrets` (never write tokens to disk yourself).
4. Persist non-secret settings in GSettings (extend the gschema).
5. Provide a `preferences_page()` returning an `Adw.PreferencesPage`.
6. Add any user-visible strings to `po/POTFILES.in`.
7. If the module supervises a host daemon, expose its status via
   `core.dbus_service` so the Nautilus extension can reflect it.

## The Microsoft 365 module specifically

It signs in once via Graph and exposes Files + Mail + Calendar. The **Files**
capability (OneDrive/SharePoint) is an orchestration layer, not a sync engine:

- **Selective sync** → `abraunegg/onedrive` (one client instance per SharePoint
  library; discover IDs with `onedrive --get-sharepoint-drive-id 'Library'`).
- **Files on-demand** → `onedriver` (FUSE) or `rclone mount --vfs-cache-mode`.
- **Share links** → `onedrive --create-share-link <path>` (add
  `--with-editing-perms` for read-write).

The module generates per-account config, manages host user systemd units, parses
status/notifications, and publishes status on D-Bus.
