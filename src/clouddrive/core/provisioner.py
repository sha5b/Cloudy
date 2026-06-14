# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Fiber Elements
"""Self-provisioning of host backends the app needs (rootless).

Clouddrive should "just work" without the user installing anything. For the
shipped Flatpak the rclone binary is bundled at build time (/app/bin). For other
installs, this module ensures rclone is available by downloading the official
static binary into a user-writable dir — no sudo, no system package manager,
checksum-verified.

We never invoke a system package manager or sudo: an app silently elevating on
someone's PC is exactly what we don't do.
"""

from __future__ import annotations

import hashlib
import io
import os
import platform
import shutil
import stat
import urllib.request
import zipfile
from pathlib import Path

from gi.repository import GLib

DOWNLOADS = "https://downloads.rclone.org"


def deps_bin_dir() -> Path:
    return Path(GLib.get_user_data_dir()) / "clouddrive" / "bin"


def resolve(binary: str) -> str | None:
    """Find a backend binary: PATH first, then our provisioned dir."""
    found = shutil.which(binary)
    if found:
        return found
    local = deps_bin_dir() / binary
    return str(local) if local.exists() and os.access(local, os.X_OK) else None


def _rclone_arch() -> str:
    machine = platform.machine().lower()
    return {
        "x86_64": "amd64",
        "amd64": "amd64",
        "aarch64": "arm64",
        "arm64": "arm64",
    }.get(machine, "amd64")


def _fetch(url: str, timeout: int = 60) -> bytes:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return resp.read()


def ensure_rclone(log=lambda _m: None) -> str:
    """Return a path to rclone, downloading it (rootless) if necessary."""
    existing = resolve("rclone")
    if existing:
        return existing

    arch = _rclone_arch()
    version = _fetch(f"{DOWNLOADS}/version.txt").decode().split()[-1]  # e.g. v1.71.0
    zip_name = f"rclone-{version}-linux-{arch}.zip"
    log(f"Downloading {zip_name}…")

    # Verify against the published SHA256SUMS for this version.
    sums = _fetch(f"{DOWNLOADS}/{version}/SHA256SUMS").decode()
    expected = None
    for line in sums.splitlines():
        if zip_name in line:
            expected = line.split()[0]
            break
    if expected is None:
        raise RuntimeError(f"no checksum found for {zip_name}")

    blob = _fetch(f"{DOWNLOADS}/{version}/{zip_name}")
    actual = hashlib.sha256(blob).hexdigest()
    if actual != expected:
        raise RuntimeError(f"checksum mismatch for {zip_name}")

    target_dir = deps_bin_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / "rclone"
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        member = next(n for n in zf.namelist() if n.endswith("/rclone"))
        with zf.open(member) as src, open(target, "wb") as dst:
            shutil.copyfileobj(src, dst)
    target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    log(f"Installed rclone {version} to {target}")
    return str(target)
