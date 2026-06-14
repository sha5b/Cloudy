# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Fiber Elements
"""Thin wrapper around the host ``onedrive`` (abraunegg) binary.

Responsibilities (stages 3+):
  * generate per-account / per-SharePoint-library config profiles
    (one client instance per library — abraunegg's documented constraint),
  * manage host user systemd units (``onedrive@.service``),
  * run/parse ``--monitor`` output for status,
  * discover SharePoint drive IDs (``--get-sharepoint-drive-id``),
  * create share links (``--create-share-link``).

Stage 0: command surface only.
"""

from __future__ import annotations

import shutil
import subprocess

BINARY = "onedrive"


class AbrauneggClient:
    def available(self) -> bool:
        return shutil.which(BINARY) is not None

    def version(self) -> str | None:
        if not self.available():
            return None
        out = subprocess.run(
            [BINARY, "--version"], capture_output=True, text=True, check=False
        )
        return out.stdout.strip() or None

    def get_sharepoint_drive_id(self, library_name: str) -> str:
        # TODO(stage 3): subprocess [BINARY, "--get-sharepoint-drive-id", name]
        raise NotImplementedError

    def create_share_link(self, path: str, *, editable: bool = False) -> str:
        # TODO(stage 3): [BINARY, "--create-share-link", path]
        #   + ["--with-editing-perms"] when editable.
        raise NotImplementedError
