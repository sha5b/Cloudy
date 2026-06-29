# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Shahab Nedaei
"""Utility helpers shared by the file browser and dashboard file cards."""

from __future__ import annotations

import os
import time
from datetime import datetime
from gettext import gettext as _
from pathlib import Path

from gi.repository import Gio


def recent_changes(roots: list[Path], *, limit: int = 8, max_scan: int = 3000,
                   time_budget: float = 4.0) -> list[dict]:
    """Most-recently-modified files under ``roots`` (for the Dashboard).

    Bounded by file count and wall-clock time so a slow network mount can never
    hang the caller.
    """
    found: list[dict] = []
    seen: set[str] = set()
    roots = [Path(r) for r in roots]
    dirs = [r for r in roots if r.is_dir() and str(r) not in seen
            and not seen.add(str(r))]
    if not dirs:
        return []
    overall_deadline = time.monotonic() + time_budget
    per_root_cap = max(50, max_scan // len(dirs))
    per_root_budget = time_budget / len(dirs)
    for root in dirs:
        root_scanned = 0
        root_deadline = min(overall_deadline, time.monotonic() + per_root_budget)
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            for fn in filenames:
                if fn.startswith("."):
                    continue
                root_scanned += 1
                fp = os.path.join(dirpath, fn)
                try:
                    found.append({"name": fn, "path": fp, "mtime": os.path.getmtime(fp)})
                except OSError:
                    continue
                if root_scanned >= per_root_cap or time.monotonic() >= root_deadline:
                    break
            if root_scanned >= per_root_cap or time.monotonic() >= root_deadline:
                break
        if time.monotonic() >= overall_deadline:
            break
    found.sort(key=lambda e: e["mtime"], reverse=True)
    return found[:limit]


def scan_directory(path: Path) -> list[dict]:
    """List a directory with size/mtime, folders flagged."""
    out = []
    with os.scandir(path) as it:
        for entry in it:
            if entry.name.startswith("."):
                continue
            try:
                is_dir = entry.is_dir()
                st = entry.stat()
            except OSError:
                continue
            out.append({
                "name": entry.name, "is_dir": is_dir, "path": entry.path,
                "size": 0 if is_dir else st.st_size, "mtime": st.st_mtime,
            })
    return out


def human_size(n: int) -> str:
    size = float(n)
    for unit in ("B", "kB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def human_time(mtime: float) -> str:
    try:
        dt = datetime.fromtimestamp(mtime)
    except (OSError, OverflowError, ValueError):
        return ""
    today = datetime.now().date()
    if dt.date() == today:
        return _("Today %s") % dt.strftime("%H:%M")
    return dt.strftime("%Y-%m-%d %H:%M")


def type_label(entry: dict) -> str:
    if entry["is_dir"]:
        return _("Folder")
    ctype, _unc = Gio.content_type_guess(entry["name"], None)
    if ctype:
        return Gio.content_type_get_description(ctype)
    return _("File")


def icon_for(entry: dict) -> Gio.Icon:
    if entry["is_dir"]:
        return Gio.ThemedIcon.new("folder")
    ctype, _unc = Gio.content_type_guess(entry["name"], None)
    if ctype:
        return Gio.content_type_get_icon(ctype)
    return Gio.ThemedIcon.new("text-x-generic")


def gdk_rect(x: float, y: float):
    from gi.repository import Gdk

    rect = Gdk.Rectangle()
    rect.x, rect.y, rect.width, rect.height = int(x), int(y), 1, 1
    return rect
