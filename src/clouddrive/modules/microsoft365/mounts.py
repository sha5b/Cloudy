# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Fiber Elements
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
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

from gi.repository import GLib


def _data_dir() -> Path:
    return Path(GLib.get_user_data_dir()) / "clouddrive"


def mount_root() -> Path:
    """Where libraries are mounted (``…/clouddrive/mounts/<name>``)."""
    return _data_dir() / "mounts"


def _bookmarks_file() -> Path:
    # GTK 3 and 4 share this bookmarks file; Nautilus reads it for the sidebar.
    return Path(GLib.get_user_config_dir()) / "gtk-3.0" / "bookmarks"


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

    def mountpoint_for(self, name: str) -> Path:
        return mount_root() / self._safe_name(name)

    def is_mounted(self, mountpoint: Path) -> bool:
        # os.path.ismount is true for an active FUSE mount.
        return mountpoint.is_dir() and os.path.ismount(mountpoint)

    # -- rclone command construction (testable without running it) -------
    def rclone_mount_argv(self, remote: str, mountpoint: Path) -> list[str]:
        return [
            RCLONE.path() or RCLONE.binary, "mount", f"{remote}:", str(mountpoint),
            "--vfs-cache-mode", "full",
            "--dir-cache-time", "30s",
            "--daemon",
        ]

    # -- mount / unmount --------------------------------------------------
    def mount(self, *, name: str, remote: str, backend: Backend | None = None,
              drive_id: str = "") -> MountInfo:
        backend = backend or self.preferred_backend()
        if backend is None:
            raise RuntimeError(
                "No mount backend found. Install rclone or onedriver "
                "(see docs/BUILDING.md)."
            )
        mountpoint = self.mountpoint_for(name)
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
            # fusermount works for both rclone and onedriver FUSE mounts.
            subprocess.run(["fusermount", "-u", str(mountpoint)], check=False)
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
