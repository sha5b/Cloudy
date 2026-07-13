# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Shahab Nedaei
"""Mount OneDrive/SharePoint libraries so they appear in the file manager.

A mounted library shows up in Nautilus like a network drive. We get that two
ways, combined:

  * a **FUSE mount** (``rclone mount`` by default; ``onedriver`` as an
    alternative) exposes the library as a folder, on-demand;
  * a **GTK bookmark** pointing at the mountpoint makes it appear in the
    Nautilus sidebar automatically (the same trick the abraunegg client uses).

The heavy lifting stays in the proven backends; this module only detects what is
available, generates config, runs the mount, and manages the bookmark. The mount
daemons run on the host (outside the Flatpak sandbox); see docs/ARCHITECTURE.md.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

from gi.repository import GLib


def _in_flatpak() -> bool:
    return os.path.exists("/.flatpak-info")


def _host_prefix() -> list[str]:
    """Argv prefix to run a command on the HOST instead of inside the sandbox.

    Empty outside Flatpak. A FUSE mount made inside the sandbox lives in the
    sandbox's private mount namespace and is invisible to the host file manager,
    so in Flatpak we run rclone/fusermount on the host (needs the
    ``org.freedesktop.Flatpak`` talk-name + ``--device=fuse``)."""
    return ["flatpak-spawn", "--host"] if _in_flatpak() else []


def _data_dir() -> Path:
    # In Flatpak, mounts and the rclone config must live on a real HOST path
    # (shared via --filesystem) so host-side rclone mounts there and the host
    # file manager sees the drive. GLib.get_user_data_dir() is redirected into
    # the sandbox, so use the real host XDG data dir under the (real) home.
    if _in_flatpak():
        return Path.home() / ".local" / "share" / "cloudy"
    return Path(GLib.get_user_data_dir()) / "cloudy"


def _setting(key: str, default: str = "") -> str:
    """Read a Cloudy GSettings string, tolerating an unavailable schema.

    Gio.Settings.new() *aborts* the process if the schema isn't installed, so we
    must look it up first rather than rely on try/except.
    """
    try:
        from gi.repository import Gio

        source = Gio.SettingsSchemaSource.get_default()
        if source is None or source.lookup("io.github.sha5b.Cloudy", True) is None:
            return default
        return Gio.Settings.new("io.github.sha5b.Cloudy").get_string(key) or default
    except Exception:  # noqa: BLE001
        return default


def mount_root() -> Path:
    """Where libraries are mounted (configurable; default ``…/cloudy/mounts``)."""
    loc = _setting("mount-location")
    return Path(loc) if loc else _data_dir() / "mounts"


def sync_root() -> Path:
    """Where two-way-synced offline copies live (``…/cloudy/synced``)."""
    return _data_dir() / "synced"


def log_root() -> Path:
    """Where per-mount rclone daemon logs live (``…/cloudy/logs``).

    The mount runs with ``--daemon``, which detaches from any terminal, so
    without an explicit ``--log-file`` all output (crucially, *upload failures*
    — SharePoint throttling, name/size rejections, mid-session token expiry) is
    discarded. Logging each mount to its own file is what turns a silent
    "my file never reached the server" into something diagnosable."""
    return _data_dir() / "logs"


def rclone_cache_dir() -> Path:
    """rclone's VFS cache/metadata dir (its default ``<cache>/rclone``).

    We deliberately DON'T override rclone's cache location: moving it would
    strand any files still waiting to upload in the old cache (data loss). We
    only *read* it — rclone writes one small JSON per opened/written file under
    ``vfsMeta/<remote>/…`` with a ``Dirty`` flag; ``Dirty: true`` means "written
    locally, not yet on the server", which is how ``upload_status`` reports
    pending uploads without a control socket (``--rc`` can't coexist with
    ``--daemon`` — it binds before the fork, so the child hits 'address already
    in use'). In Flatpak the daemon runs on the host, so this is the *host*
    cache dir (the manifest shares it read-only); degrade gracefully if unread."""
    if _in_flatpak():
        return Path.home() / ".cache" / "rclone"
    return Path(GLib.get_user_cache_dir()) / "rclone"


# -- remembered mounts (auto-remount on startup) -------------------------
# A FUSE mount is a live ``rclone mount --daemon`` process; it dies on reboot.
# To bring drives back automatically we persist *which* drives the user mounted
# (not the live state) in a small JSON file, and remount them when Cloudy
# starts. The rclone remote + OAuth token already persist, so remounting needs
# no re-auth.

def _mount_state_file() -> Path:
    return _data_dir() / "mounts.json"


def _record_key(account_id: str, drive_name: str) -> tuple[str, str]:
    return (account_id, drive_name)


def load_mount_records() -> list[dict]:
    """Drives the user has asked to keep mounted, as a list of dicts with keys
    ``account_id``, ``provider``, ``drive_name``, ``drive_id``, ``drive_kind``."""
    path = _mount_state_file()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def _save_mount_records(records: list[dict]) -> None:
    path = _mount_state_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(records, indent=2) + "\n")


def record_mount(account_id: str, drive, mountpoint: str | None = None) -> None:
    """Remember a mounted drive so it remounts on the next startup."""
    records = [
        r for r in load_mount_records()
        if _record_key(r.get("account_id", ""), r.get("drive_name", ""))
        != _record_key(account_id, getattr(drive, "name", ""))
    ]
    records.append({
        "account_id": account_id,
        "drive_name": getattr(drive, "name", ""),
        "drive_id": getattr(drive, "id", "") or "",
        "drive_kind": getattr(drive, "kind", "") or "",
        "mountpoint": mountpoint,
    })
    _save_mount_records(records)


def forget_mount(account_id: str, drive_name: str) -> None:
    """Drop a drive from the remembered set (on explicit Unmount)."""
    records = [
        r for r in load_mount_records()
        if _record_key(r.get("account_id", ""), r.get("drive_name", ""))
        != _record_key(account_id, drive_name)
    ]
    _save_mount_records(records)


def _account_label(account) -> str:
    """A stable, human-readable folder name for an account's mounts."""
    return getattr(account, "display_name", "") or getattr(account, "id", "account")


def mount_base_for(account) -> Path:
    """The directory under which *this account's* drives mount.

    Each account gets its own folder so drives that share a name across accounts
    (e.g. two Google "My Drive"s) never collide on a single mountpoint — and so a
    live mount can be attributed back to the account that owns it (the basis for
    "is this drive already mounted for this account?" checks that stop us mounting
    the same thing repeatedly).

      * ``individual`` layout → the account's own configured ``mount_location``.
      * ``one-folder`` layout → a per-account subfolder of the global mount root.
    """
    loc = getattr(account, "mount_location", "")
    if _setting("mount-layout", "one-folder") == "individual" and loc:
        return Path(loc)
    return mount_root() / MountManager._safe_name(_account_label(account))


def cache_mode() -> str:
    return _setting("cache-mode", "full") or "full"


def _bookmarks_file() -> Path:
    # GTK 3 and 4 share this bookmarks file; Nautilus reads it for the sidebar.
    # In Flatpak, the HOST Nautilus reads the host file, so target the real host
    # config dir (shared via --filesystem) rather than the sandbox-redirected one.
    base = (Path.home() / ".config") if _in_flatpak() \
        else Path(GLib.get_user_config_dir())
    return base / "gtk-3.0" / "bookmarks"


@dataclass
class Backend:
    name: str
    binary: str

    def path(self) -> str | None:
        from ...core.provisioner import resolve

        return resolve(self.binary)

    @property
    def available(self) -> bool:
        return self.path() is not None


RCLONE = Backend("rclone", "rclone")
ONEDRIVER = Backend("onedriver", "onedriver")


@dataclass
class MountInfo:
    name: str
    mountpoint: Path
    backend: str
    drive_id: str = ""
    active: bool = False


@dataclass
class MountManager:
    """Detects backends and mounts/unmounts libraries with sidebar bookmarks."""

    backends: list[Backend] = field(
        default_factory=lambda: [RCLONE, ONEDRIVER]
    )

    # -- backend discovery ------------------------------------------------
    def available_backends(self) -> list[Backend]:
        return [b for b in self.backends if b.available]

    def preferred_backend(self) -> Backend | None:
        for b in self.available_backends():
            return b
        return None

    # -- mountpoint helpers ----------------------------------------------
    @staticmethod
    def _safe_name(name: str) -> str:
        return "".join(c if c.isalnum() or c in "-_ " else "_" for c in name).strip()

    @classmethod
    def remote_name(cls, drive_name: str, account_id: str = "") -> str:
        """rclone config-section name for a drive, scoped per account: two
        accounts each having a drive literally named "OneDrive" must NOT share
        one remote — each mount rewrote the shared section with its own token,
        clobbering the token the other account's live daemon refreshes into
        it. Unscoped (legacy) names are still read as a fallback when adopting
        old mounts (see _reconstruct_record)."""
        base = cls._safe_name(drive_name)
        return f"{base}--{cls._safe_name(account_id)}" if account_id else base

    def mountpoint_for(self, name: str, base: Path | None = None) -> Path:
        """Mountpoint for a library. ``base`` overrides the global mount root
        (used for per-account mount locations); falls back to ``mount_root()``."""
        return (base or mount_root()) / self._safe_name(name)

    @staticmethod
    def active_mounts() -> set[str]:
        """All currently-mounted paths, read from the kernel mount table.

        Stall-proof and reliable across sessions: parses ``/proc/self/mountinfo``
        directly, so it never stats (and never blocks on) a possibly-hung FUSE
        mountpoint the way ``os.path.ismount`` would. In Flatpak it reads the
        HOST table (mounts run on the host), since the sandbox's own table
        wouldn't show them. Returns absolute paths."""
        if _in_flatpak():
            try:
                out = subprocess.run(
                    [*_host_prefix(), "cat", "/proc/self/mountinfo"],
                    capture_output=True, text=True, timeout=10).stdout
            except (OSError, subprocess.SubprocessError):
                out = ""
            lines = out.splitlines()
        else:
            try:
                with open("/proc/self/mountinfo", encoding="utf-8") as fh:
                    lines = fh.readlines()
            except OSError:
                lines = []
        paths: set[str] = set()
        for line in lines:
            fields = line.split(" ")
            if len(fields) > 4:
                # Field 5 is the mount point; mountinfo octal-escapes
                # space/tab/newline/backslash in paths.
                mp = (fields[4].replace("\\040", " ").replace("\\011", "\t")
                      .replace("\\012", "\n").replace("\\134", "\\"))
                paths.add(mp)
        return paths

    def is_mounted(self, mountpoint: Path) -> bool:
        return str(mountpoint) in self.active_mounts()

    def _await_mount(self, mountpoint: Path, timeout: float = 10.0) -> bool:
        """Poll the mount table until ``mountpoint`` appears (the FUSE daemon
        comes up shortly after `--daemon` forks). Returns True once mounted, or
        False if it never shows within ``timeout``. Runs on a worker thread
        (mount() is called via run_async), so a short sleep is fine."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.is_mounted(mountpoint):
                return True
            time.sleep(0.25)
        return self.is_mounted(mountpoint)

    @staticmethod
    def _process_cmdlines() -> str:
        """All running process command lines (host-aware), as one blob.

        Used to tell whether a live ``rclone``/``onedriver`` daemon still backs a
        mountpoint without ever touching the FUSE path (a stat on a hung mount
        can stall). Best-effort — returns ``""`` if ``ps`` can't run."""
        try:
            return subprocess.run(
                [*_host_prefix(), "ps", "-eo", "args"],
                capture_output=True, text=True, timeout=10).stdout
        except (OSError, subprocess.SubprocessError):
            return ""

    def _has_mount_process(self, mountpoint: Path) -> bool:
        mp = str(mountpoint)
        for line in self._process_cmdlines().splitlines():
            if mp in line and "mount" in line and ("rclone" in line or "onedriver" in line):
                return True
        return False

    def mount_health(self, mountpoint: Path) -> str:
        """Health of a mountpoint, without touching it (stall-proof):

          * ``"active"`` — in the mount table *and* a daemon still serves it;
          * ``"stale"``  — in the mount table but the daemon is gone (I/O would
            fail with "transport endpoint is not connected"); needs clearing
            before it can be remounted;
          * ``"absent"`` — not mounted.
        """
        if not self.is_mounted(mountpoint):
            return "absent"
        return "active" if self._has_mount_process(mountpoint) else "stale"

    def _lazy_unmount(self, mountpoint: Path) -> None:
        """Detach a stale/dead FUSE mount (lazy ``-z``, so even a hung endpoint
        releases). Leaves the sidebar bookmark in place — the path is unchanged
        and a remount reuses it."""
        for tool in ("fusermount3", "fusermount"):
            res = subprocess.run(
                [*_host_prefix(), tool, "-uz", str(mountpoint)],
                capture_output=True, text=True)
            if res.returncode == 0:
                return

    # -- host-aware rclone execution -------------------------------------
    def _rclone_binary(self) -> str | None:
        """Path to an rclone the mount command can execute. Outside Flatpak this
        is the resolved/provisioned binary. Inside Flatpak the mount runs on the
        host (flatpak-spawn), which can't see ``/app`` — so copy the bundled
        static rclone once into the shared host dir and run that host-side path."""
        if not _in_flatpak():
            return RCLONE.path()
        host_bin = _data_dir() / "bin" / "rclone"   # real host path (shared)
        if not host_bin.exists():
            bundled = RCLONE.path() or "/app/bin/rclone"
            if not os.path.exists(bundled):
                return None
            host_bin.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(bundled, host_bin)
            host_bin.chmod(0o755)
        return str(host_bin)

    def _rclone_argv(self, *args: str) -> list[str]:
        """Full argv to run rclone, host-prefixed in Flatpak. Caller must have
        checked ``_rclone_binary()`` is not None."""
        return [*_host_prefix(), self._rclone_binary() or RCLONE.binary, *args]

    # -- rclone command construction (testable without running it) -------
    def _mount_key(self, mountpoint: Path) -> str:
        """Stable per-mount slug (account-folder + drive) for log/socket names,
        so drives that share a name across accounts never collide."""
        return self._safe_name(f"{mountpoint.parent.name}-{mountpoint.name}")

    def log_file_for(self, mountpoint: Path) -> Path:
        """Per-mount daemon log path."""
        return log_root() / f"rclone-{self._mount_key(mountpoint)}.log"

    def _rotate_log(self, log_file: Path, max_bytes: int = 5 * 1024 * 1024) -> None:
        """Keep one previous log around and cap growth: if the current log is
        over ``max_bytes``, move it aside to ``<name>.1`` before the daemon
        reopens it (rclone has no built-in rotation and would append forever)."""
        try:
            if log_file.exists() and log_file.stat().st_size > max_bytes:
                log_file.replace(log_file.with_suffix(log_file.suffix + ".1"))
        except OSError:
            pass

    # -- config + live sync status --------------------------------------
    def config_dump(self) -> dict:
        """All rclone remotes as ``{name: {type, drive_id, …}}`` (``config
        dump``). Used to reconstruct a lost mount record from an orphaned
        bookmark. Best-effort — ``{}`` if rclone is missing or errors."""
        if not self._rclone_binary():
            return {}
        try:
            out = subprocess.run(self._rclone_argv("config", "dump"),
                                 capture_output=True, text=True, timeout=30)
            return json.loads(out.stdout or "{}")
        except (OSError, subprocess.SubprocessError, ValueError):
            return {}

    def upload_status(self, remote: str) -> dict:
        """How many written files are still waiting to reach the server.

        Reads rclone's VFS metadata (one small JSON per opened/written file
        under ``<cache-dir>/vfsMeta/<remote>/``) and counts entries flagged
        ``Dirty`` — i.e. present in the local cache but not yet uploaded. This
        needs no control socket (``--rc`` can't coexist with ``--daemon``) and
        never touches the FUSE path. Returns ``{"pending": int}``; ``pending`` is
        0 when everything has landed (or nothing's been written). Bounded scan
        so a huge cache can't stall the poll."""
        meta = rclone_cache_dir() / "vfsMeta" / self._safe_name(remote)
        if not meta.is_dir():
            return {"pending": 0}
        pending = 0
        scanned = 0
        try:
            for jf in meta.rglob("*"):
                if not jf.is_file():
                    continue
                scanned += 1
                if scanned > 5000:  # safety bound; real dirty sets are tiny
                    break
                try:
                    if json.loads(jf.read_text()).get("Dirty") is True:
                        pending += 1
                except (OSError, ValueError):
                    continue
        except OSError:
            return {"pending": 0}
        return {"pending": pending}

    def rclone_mount_argv(self, remote: str, mountpoint: Path,
                          onedrive: bool = False) -> list[str]:
        # Tuned for: open-to-load, stay-cached, and auto-refresh on server change.
        #   --vfs-cache-mode full  : download a file when opened, keep it on disk
        #                            (random-access reads work — needed by most
        #                            doc/PDF/office viewers; 'off'/'minimal' can
        #                            otherwise read back empty/garbled).
        #   --poll-interval 15s    : OneDrive/Drive change-polling — edits on the
        #                            server show locally within ~15s.
        #   --dir-cache-time 5m    : snappy listings; polling invalidates on change.
        #   --vfs-cache-max-age 72h: keep opened files cached (instant re-open).
        #   --vfs-write-back 5s    : push a written/copied file to the server 5s
        #                            after it's closed. Explicit (not relying on
        #                            the default) so the upload window is known.
        #   --vfs-read-chunk-size  : start serving large files fast, grow chunks.
        #   --log-file/--log-level : capture uploads *and failures* to a per-mount
        #                            log; the --daemon fork otherwise discards all
        #                            output, making a failed upload invisible.
        # (rclone's default --cache-dir is kept on purpose — see rclone_cache_dir.)
        log_file = self.log_file_for(mountpoint)
        log_file.parent.mkdir(parents=True, exist_ok=True)
        self._rotate_log(log_file)
        argv = self._rclone_argv(
            "mount", f"{remote}:", str(mountpoint),
            "--vfs-cache-mode", cache_mode(),
            "--dir-cache-time", "5m",
            "--poll-interval", "15s",
            "--vfs-cache-max-age", "72h",
            "--vfs-write-back", "5s",
            "--vfs-read-chunk-size", "16M",
            "--vfs-read-chunk-size-limit", "512M",
            #   --attr-timeout 1m  : how long the KERNEL caches file attributes.
            #     rclone's default is 1s, which means every stat() from a file
            #     chooser, Nautilus, or thumbnailer goes back to rclone (and
            #     potentially the network) almost every time — this is what made
            #     file-open dialogs across the desktop crawl once a drive was
            #     mounted. 1m is rclone's own recommended safe raise; change
            #     detection still works via --poll-interval invalidation.
            "--attr-timeout", "1m",
            "--log-file", str(log_file),
            "--log-level", "INFO",
        )
        if onedrive:
            # SharePoint / OneDrive-for-Business *rewrites* Office documents
            # (.docx/.pptx/.xlsx) server-side, so the stored file ends up a
            # different size/hash than what we uploaded. rclone's post-upload
            # verification then reports "corrupted on transfer: sizes differ",
            # deletes its copy, and retries forever — the file NEVER lands on
            # the server (plain files are unaffected). Disabling the size/hash
            # re-check is rclone's documented workaround for SharePoint; the
            # upload itself is fine, only the paranoid verify is wrong here.
            argv += ["--ignore-size", "--ignore-checksum"]
        argv.append("--daemon")
        return argv

    # -- two-way sync (rclone bisync) ------------------------------------
    def synced_dir_for(self, name: str) -> Path:
        return sync_root() / self._safe_name(name)

    def rclone_bisync_argv(self, remote: str, localdir: Path,
                           resync: bool = False) -> list[str]:
        """rclone bisync argv. ``--resync`` establishes the baseline on the very
        first run; afterwards plain bisync propagates changes both ways."""
        argv = self._rclone_argv(
            "bisync", f"{remote}:", str(localdir),
            "--create-empty-src-dirs",
            "--conflict-resolve", "newer",
            "--resilient",
        )
        if resync:
            argv.append("--resync")
        return argv

    def bisync(self, remote: str, name: str, *, timeout: int = 1800) -> Path:
        """Run a two-way sync for ``remote`` into its local folder. Resyncs once
        to seed the baseline (tracked by a marker), then bisyncs incrementally.
        Blocking — call off the UI thread. Returns the local directory."""
        if not self._rclone_binary():
            raise RuntimeError("rclone is not available")
        localdir = self.synced_dir_for(name)
        localdir.mkdir(parents=True, exist_ok=True)
        marker = sync_root() / ".state" / f"{self._safe_name(name)}.resynced"
        first = not marker.exists()
        subprocess.run(
            self.rclone_bisync_argv(remote, localdir, resync=first),
            check=True, capture_output=True, text=True, timeout=timeout,
        )
        if first:
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text("")
        return localdir

    # -- rclone OneDrive auth + remote config ----------------------------
    @staticmethod
    def drive_type_for(kind: str) -> str:
        """Map our Drive.kind to rclone's onedrive drive_type."""
        return {
            "personal": "personal",
            "business": "business",
            "documentLibrary": "documentLibrary",
            "team": "documentLibrary",
        }.get(kind, "documentLibrary")

    def has_remote(self, remote: str) -> bool:
        if not self._rclone_binary():
            return False
        out = subprocess.run(self._rclone_argv("listremotes"),
                             capture_output=True, text=True)
        # One remote per line; splitting on whitespace broke any remote whose
        # name contains a space (allowed by _safe_name, e.g. "My Drive").
        return f"{remote}:" in (l.strip() for l in out.stdout.splitlines())

    def authorize(self, backend: str, timeout: int = 300) -> str:
        """Run rclone's own browser OAuth for a backend (its built-in app = no
        registration). Opens the system browser, waits for the redirect, and
        returns the token JSON blob. Blocking — call off the UI thread.
        """
        if not self._rclone_binary():
            raise RuntimeError("rclone is not available")
        proc = subprocess.run(
            self._rclone_argv("authorize", backend),
            capture_output=True, text=True, timeout=timeout,
        )
        blob = (proc.stdout or "") + "\n" + (proc.stderr or "")
        start = blob.find("{")
        end = blob.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise RuntimeError(
                f"rclone authorization did not return a token (exit {proc.returncode})"
            )
        return blob[start : end + 1].strip()

    def create_remote(self, remote: str, backend: str, opts: dict) -> None:
        if not self._rclone_binary():
            raise RuntimeError("rclone is not available")
        args = self._rclone_argv("config", "create", remote, backend)
        args += [f"{k}={v}" for k, v in opts.items()]
        args.append("--non-interactive")
        subprocess.run(args, check=True, capture_output=True, text=True)

    def list_google_shared_drives(self, token_json: str) -> list[dict]:
        """Shared (Team) Drives the Google account can access, as ``[{id, name}]``.

        The app holds no Google Drive OAuth scope (Drive is mounted entirely
        through rclone's own auth), so we enumerate via rclone: spin up a
        throwaway ``drive`` remote from the stored token, ask its backend for the
        shared-drive list, then drop the remote. Best-effort — returns ``[]`` if
        rclone is missing, the token is empty, or anything goes wrong."""
        if not self._rclone_binary() or not token_json:
            return []
        probe = "cloudy-gdrive-probe"
        try:
            self.create_remote(probe, "drive", {"token": token_json, "scope": "drive"})
            out = subprocess.run(
                self._rclone_argv("backend", "drives", f"{probe}:"),
                capture_output=True, text=True, timeout=60,
            )
            data = json.loads(out.stdout or "[]")
            return [{"id": d.get("id", ""), "name": d.get("name", "")}
                    for d in data if d.get("id")]
        except Exception:  # noqa: BLE001 - enumeration is best-effort
            return []
        finally:
            self.delete_remote(probe)

    def delete_remote(self, remote: str) -> None:
        if self._rclone_binary() and self.has_remote(remote):
            subprocess.run(self._rclone_argv("config", "delete", remote), check=False)

    # -- mount / unmount --------------------------------------------------
    def mount(self, *, name: str, remote: str, backend: Backend | None = None,
              drive_id: str = "", base: Path | None = None,
              onedrive: bool = False) -> MountInfo:
        backend = backend or self.preferred_backend()
        if backend is None:
            raise RuntimeError(
                "No mount backend found. Install rclone or onedriver "
                "(see docs/BUILDING.md)."
            )
        mountpoint = self.mountpoint_for(name, base)
        mountpoint.mkdir(parents=True, exist_ok=True)

        # A stale mount (in the mount table but its daemon is dead) must be
        # cleared first: is_mounted() alone would treat it as already-mounted
        # and skip the real mount, reporting success for a drive that only
        # returns "transport endpoint is not connected" — and a hung endpoint
        # also stalls every file chooser on the desktop that stats it.
        if self.mount_health(mountpoint) == "stale":
            self._lazy_unmount(mountpoint)

        if not self.is_mounted(mountpoint):
            if backend is RCLONE:
                # `rclone mount --daemon` forks and the parent exits 0 *before*
                # the FUSE mount is actually up, so a 0 return tells us nothing;
                # poll the mount table until it really appears.
                #
                # MUST NOT capture_output here: with --daemon + --log-file the
                # forked daemon inherits the stdout/stderr pipes and holds them
                # open, so subprocess.run() would block reading them *forever*
                # (hung mount worker). Errors go to the per-mount --log-file
                # instead; DEVNULL lets the parent return the instant it forks.
                log_file = self.log_file_for(mountpoint)
                proc = subprocess.run(
                    self.rclone_mount_argv(remote, mountpoint, onedrive=onedrive),
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                if proc.returncode != 0:
                    raise RuntimeError(
                        "rclone mount failed (exit %d); see %s"
                        % (proc.returncode, log_file))
                if not self._await_mount(mountpoint):
                    raise RuntimeError(
                        "rclone mount didn't come up (timed out); see %s" % log_file)
            elif backend is ONEDRIVER:
                # onedriver runs in the FOREGROUND by default — subprocess.run
                # would block this worker thread forever and the mount would
                # appear to hang. Detach it and poll the mount table instead.
                subprocess.Popen(
                    [ONEDRIVER.path() or ONEDRIVER.binary, str(mountpoint)],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    start_new_session=True)
                if not self._await_mount(mountpoint):
                    raise RuntimeError("onedriver mount didn't come up (timed out)")

        self.add_bookmark(mountpoint, name)
        return MountInfo(
            name=name, mountpoint=mountpoint, backend=backend.name,
            drive_id=drive_id, active=self.is_mounted(mountpoint),
        )

    def mount_drive(self, *, provider: str, drive, token: str,
                    base: Path | None = None, account_id: str = "") -> MountInfo:
        """Create the rclone remote for ``drive`` from a stored token and mount
        it. Shared by the Files view and the startup auto-remount so the remote
        options stay in one place. Blocking — call off the UI thread."""
        google = provider == "google"
        backend = "drive" if google else "onedrive"
        remote = self.remote_name(drive.name, account_id)
        if google:
            opts = {"token": token, "scope": "drive"}
            # "Shared with me" and Shared Drives are the same backend with a
            # different view selector (rclone drive config keys).
            if drive.kind == "google_shared_with_me":
                opts["shared_with_me"] = "true"
            elif drive.kind == "google_shared_drive" and drive.id:
                opts["team_drive"] = drive.id
        else:
            opts = {
                "token": token,
                "drive_id": drive.id,
                "drive_type": self.drive_type_for(drive.kind),
            }
        self.create_remote(remote, backend, opts)
        return self.mount(name=drive.name, remote=remote,
                          drive_id=drive.id, base=base, onedrive=not google)

    def unmount(self, mountpoint: Path) -> None:
        if self.is_mounted(mountpoint):
            # fusermount works for both rclone and onedriver FUSE mounts; run it
            # on the host in Flatpak (the mount lives in the host namespace).
            res = subprocess.run(
                [*_host_prefix(), "fusermount3", "-u", str(mountpoint)],
                capture_output=True, text=True)
            if res.returncode != 0:
                subprocess.run([*_host_prefix(), "fusermount", "-u", str(mountpoint)],
                               check=False)
            # A busy mount (any file still open) makes both attempts fail with
            # exit 1. Swallowing that made the caller forget the mount record
            # and toast "Unmounted" while the drive stayed mounted — raise
            # instead so the UI can report the real situation.
            if self.is_mounted(mountpoint):
                detail = (res.stderr or res.stdout or "").strip()
                raise RuntimeError(
                    detail or "the drive is busy (a file may still be open)")
        self.remove_bookmark(mountpoint)

    # -- Nautilus sidebar bookmark ---------------------------------------
    def _bookmark_line(self, mountpoint: Path, label: str) -> str:
        uri = "file://" + quote(str(mountpoint))
        return f"{uri} {label}"

    def add_bookmark(self, mountpoint: Path, label: str) -> None:
        path = _bookmarks_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        line = self._bookmark_line(mountpoint, label)
        existing = path.read_text().splitlines() if path.exists() else []
        uri = line.split(" ", 1)[0]
        if any(l.split(" ", 1)[0] == uri for l in existing):
            return
        existing.append(line)
        path.write_text("\n".join(existing) + "\n")

    def remove_bookmark(self, mountpoint: Path) -> None:
        path = _bookmarks_file()
        if not path.exists():
            return
        uri = "file://" + quote(str(mountpoint))
        kept = [l for l in path.read_text().splitlines() if l.split(" ", 1)[0] != uri]
        path.write_text("\n".join(kept) + ("\n" if kept else ""))

    def bookmark_paths(self) -> list[Path]:
        """Local filesystem paths of every GTK bookmark (``file://`` only).

        Used to find Cloudy sidebar bookmarks that outlived their mount so
        reconciliation can adopt or clear them."""
        path = _bookmarks_file()
        if not path.exists():
            return []
        from urllib.parse import unquote, urlparse

        out: list[Path] = []
        for line in path.read_text().splitlines():
            uri = line.split(" ", 1)[0].strip()
            if uri.startswith("file://"):
                out.append(Path(unquote(urlparse(uri).path)))
        return out


def remount_saved(registry, secrets, log=lambda _m: None) -> int:
    """Remount every remembered drive that is not currently healthy.

    Doubles as the startup restore *and* the periodic health watchdog: for each
    remembered drive it recomputes the per-account mount base, checks health,
    skips anything already healthy, clears a stale (dead-daemon) mount first,
    then remounts via ``mount_drive`` from the stored token (no re-auth).
    Best-effort per drive — a failure is logged and skipped, never raised.
    Returns the number of drives (re)mounted.
    """
    from .graph import Drive

    records = load_mount_records()
    if not records:
        return 0
    mgr = MountManager()
    if mgr.preferred_backend() is None:
        log("no mount backend available; skipping auto-remount")
        return 0

    remounted = 0
    for rec in records:
        account_id = rec.get("account_id", "")
        drive_name = rec.get("drive_name", "")
        account = registry.get(account_id)
        if account is None:
            log(f"skipping {drive_name!r}: account {account_id} is gone")
            continue
        base = mount_base_for(account)
        mountpoint = mgr.mountpoint_for(drive_name, base)
        health = mgr.mount_health(mountpoint)
        if health == "active":
            continue
        if health == "stale":
            log(f"clearing stale mount at {mountpoint} (daemon gone)")
            mgr._lazy_unmount(mountpoint)
        provider = getattr(account, "provider", "")
        token_kind = "rclone-gdrive" if provider == "google" else "rclone-onedrive"
        token = secrets.lookup(account_id, token_kind)
        if not token:
            log(f"skipping {drive_name!r}: no saved token (mount it once to reconnect)")
            continue
        drive = Drive(id=rec.get("drive_id", ""), name=drive_name,
                      kind=rec.get("drive_kind", ""), web_url="")
        try:
            mgr.mount_drive(provider=provider, drive=drive, token=token,
                            base=base, account_id=account_id)
            log(f"remounted {drive_name!r} at {mountpoint}")
            remounted += 1
        except Exception as exc:  # noqa: BLE001 - one bad drive must not block others
            log(f"failed to remount {drive_name!r}: {exc}")
    return remounted


def _reconstruct_record(account, folder: str, dump: dict) -> dict | None:
    """Rebuild a mount record for a drive whose bookmark survived but whose
    ``mounts.json`` entry was lost, from the rclone remote config (which already
    holds the token, ``drive_id`` and ``drive_type``). ``folder`` is the
    mountpoint's final path component; the remote is the account-scoped name,
    falling back to the legacy unscoped one for remotes created before the
    per-account scoping."""
    scoped = MountManager.remote_name(folder, getattr(account, "id", ""))
    cfg = dump.get(scoped) or dump.get(folder)
    if cfg is None:
        return None
    if cfg.get("type") == "drive":  # google
        if cfg.get("shared_with_me") == "true":
            kind, drive_id = "google_shared_with_me", ""
        elif cfg.get("team_drive"):
            kind, drive_id = "google_shared_drive", cfg.get("team_drive", "")
        else:
            kind, drive_id = "google_mydrive", ""
    else:  # onedrive / sharepoint
        kind = cfg.get("drive_type", "documentLibrary")
        drive_id = cfg.get("drive_id", "")
    return {
        "account_id": account.id,
        "drive_name": folder,
        "drive_id": drive_id,
        "drive_kind": kind,
        "mountpoint": str(mount_base_for(account) / MountManager._safe_name(folder)),
    }


def _remember(rec: dict) -> None:
    """Add or replace a mount record (dedup by ``account_id`` + ``drive_name``)."""
    key = _record_key(rec["account_id"], rec["drive_name"])
    records = [r for r in load_mount_records()
               if _record_key(r.get("account_id", ""), r.get("drive_name", "")) != key]
    records.append(rec)
    _save_mount_records(records)


def reconcile_mounts(registry, log=lambda _m: None) -> dict:
    """Heal drift between Nautilus sidebar bookmarks, remembered mounts, and the
    rclone remote config.

    This fixes the silent-data-loss class of bug where a sidebar bookmark
    outlives its mount: opening it in Nautilus shows a *bare local stub* folder,
    so files dropped there write to disk and never upload. For every Cloudy
    bookmark whose drive is NOT currently mounted:

      * if its rclone remote still exists and the mountpoint maps to a known
        account → **adopt** it (write a mount record so the auto-remount brings
        it back — the remote already holds the token/drive_id, so no re-auth);
      * otherwise → **remove** the stale bookmark.

    A live-but-unremembered mount is recorded so it survives the next restart.
    Best-effort; never raises. Returns ``{"adopted", "recorded", "removed"}``.
    Run once at startup (before ``remount_saved``), not on the health watchdog.
    """
    mgr = MountManager()
    counts = {"adopted": 0, "recorded": 0, "removed": 0}
    bases: dict[str, object] = {}
    for account in registry.accounts():
        try:
            bases[str(mount_base_for(account))] = account
        except Exception:  # noqa: BLE001 - never let one account block the sweep
            continue
    dump = mgr.config_dump() if mgr.preferred_backend() else {}
    root = str(mount_root())
    recorded_mps = {r.get("mountpoint", "") for r in load_mount_records()}

    for p in mgr.bookmark_paths():
        sp = str(p)
        account = bases.get(str(p.parent))
        # Only touch Cloudy-managed bookmarks: under a known account base, or
        # (renamed/removed account) anywhere under the mount root. Everything
        # else in the sidebar (Documents, Music, …) is left untouched.
        if account is None and not sp.startswith(root + os.sep):
            continue
        folder = p.name
        if mgr.is_mounted(p):
            if account is not None and sp not in recorded_mps:
                rec = _reconstruct_record(account, folder, dump)
                if rec:
                    _remember(rec)
                    recorded_mps.add(sp)
                    counts["recorded"] += 1
                    log(f"recorded already-mounted {folder!r}")
            continue
        if account is not None and folder in dump:
            rec = _reconstruct_record(account, folder, dump)
            if rec:
                _remember(rec)
                recorded_mps.add(sp)
                counts["adopted"] += 1
                log(f"adopted orphaned bookmark {folder!r} (will remount)")
                continue
        mgr.remove_bookmark(p)
        counts["removed"] += 1
        log(f"removed stale bookmark {sp} (no live mount / no remote)")
    return counts
