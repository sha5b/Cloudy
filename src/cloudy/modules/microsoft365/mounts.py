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

import os
import shutil
import subprocess
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
        if source is None or source.lookup("io.github.sha5b.Clouddrive", True) is None:
            return default
        return Gio.Settings.new("io.github.sha5b.Clouddrive").get_string(key) or default
    except Exception:  # noqa: BLE001
        return default


def mount_root() -> Path:
    """Where libraries are mounted (configurable; default ``…/cloudy/mounts``)."""
    loc = _setting("mount-location")
    return Path(loc) if loc else _data_dir() / "mounts"


def sync_root() -> Path:
    """Where two-way-synced offline copies live (``…/cloudy/synced``)."""
    return _data_dir() / "synced"


def account_mount_base(mount_location: str) -> Path | None:
    """Resolve an account's mount base: its own folder when the layout is
    'individual' and an override is set, otherwise None (use the default root)."""
    if _setting("mount-layout", "one-folder") == "individual" and mount_location:
        return Path(mount_location)
    return None


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
    def rclone_mount_argv(self, remote: str, mountpoint: Path) -> list[str]:
        # Tuned for: open-to-load, stay-cached, and auto-refresh on server change.
        #   --vfs-cache-mode full  : download a file when opened, keep it on disk
        #                            (random-access reads work — needed by most
        #                            doc/PDF/office viewers; 'off'/'minimal' can
        #                            otherwise read back empty/garbled).
        #   --poll-interval 15s    : OneDrive/Drive change-polling — edits on the
        #                            server show locally within ~15s.
        #   --dir-cache-time 5m    : snappy listings; polling invalidates on change.
        #   --vfs-cache-max-age 72h: keep opened files cached (instant re-open).
        #   --vfs-read-chunk-size  : start serving large files fast, grow chunks.
        return self._rclone_argv(
            "mount", f"{remote}:", str(mountpoint),
            "--vfs-cache-mode", cache_mode(),
            "--dir-cache-time", "5m",
            "--poll-interval", "15s",
            "--vfs-cache-max-age", "72h",
            "--vfs-read-chunk-size", "16M",
            "--vfs-read-chunk-size-limit", "512M",
            "--daemon",
        )

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
        return f"{remote}:" in out.stdout.split()

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

    # OneDrive convenience wrappers (kept for the Microsoft module).
    def authorize_onedrive(self, timeout: int = 300) -> str:
        return self.authorize("onedrive", timeout=timeout)

    def create_onedrive_remote(
        self, remote: str, token_json: str, drive_id: str, drive_type: str
    ) -> None:
        self.create_remote(remote, "onedrive", {
            "token": token_json, "drive_id": drive_id, "drive_type": drive_type,
        })

    def delete_remote(self, remote: str) -> None:
        if self._rclone_binary() and self.has_remote(remote):
            subprocess.run(self._rclone_argv("config", "delete", remote), check=False)

    # -- mount / unmount --------------------------------------------------
    def mount(self, *, name: str, remote: str, backend: Backend | None = None,
              drive_id: str = "", base: Path | None = None) -> MountInfo:
        backend = backend or self.preferred_backend()
        if backend is None:
            raise RuntimeError(
                "No mount backend found. Install rclone or onedriver "
                "(see docs/BUILDING.md)."
            )
        mountpoint = self.mountpoint_for(name, base)
        mountpoint.mkdir(parents=True, exist_ok=True)

        if not self.is_mounted(mountpoint):
            if backend is RCLONE:
                subprocess.run(self.rclone_mount_argv(remote, mountpoint), check=True)
            elif backend is ONEDRIVER:
                subprocess.run([ONEDRIVER.path() or ONEDRIVER.binary, str(mountpoint)], check=True)

        self.add_bookmark(mountpoint, name)
        return MountInfo(
            name=name, mountpoint=mountpoint, backend=backend.name,
            drive_id=drive_id, active=self.is_mounted(mountpoint),
        )

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
