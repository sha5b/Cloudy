# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Fiber Elements
"""Load OAuth credentials from a file OUTSIDE the source tree.

Keeps real secrets (e.g. the Google client secret) out of the public repo. On
startup we read ``$XDG_CONFIG_HOME/cloudy/secrets.env`` (default
``~/.config/cloudy/secrets.env``) — a simple ``KEY=VALUE`` file — into the
environment, without overriding values already set. The app's client-id getters
read these env vars (CLOUDY_MS_CLIENT_ID / CLOUDY_GOOGLE_CLIENT_ID /
CLOUDY_GOOGLE_CLIENT_SECRET).

For shipped builds, the release pipeline injects the same values from a CI
secret store — never from committed source. See docs/SECRETS.md.
"""

from __future__ import annotations

import os

from gi.repository import GLib

_PREFIX = "CLOUDY_"


def secrets_path() -> str:
    return os.path.join(GLib.get_user_config_dir(), "cloudy", "secrets.env")


def load_local_env() -> None:
    """Populate CLOUDY_* env vars from the local secrets file, if present."""
    path = secrets_path()
    try:
        with open(path, encoding="utf-8") as handle:
            lines = handle.readlines()
    except OSError:
        return

    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key.startswith(_PREFIX) and not os.environ.get(key):
            os.environ[key] = value
