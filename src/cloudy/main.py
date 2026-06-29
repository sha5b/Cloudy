# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Shahab Nedaei

import os
import sys

# WebKitGTK's DMA-BUF renderer paints blank pages intermittently on many
# GPU/driver/compositor combos (mail bodies render as a white void, "sometimes"
# working). Disabling it is the documented, reliable workaround — negligible
# cost for our small mail/event WebViews. Must be set before WebKit's web
# process spawns; here at import time is well before the first WebView.
# `setdefault` so a user/env can still override.
os.environ.setdefault("WEBKIT_DISABLE_DMABUF_RENDERER", "1")

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from .application import CloudyApplication


def main(version: str = "0.0.0", app_id: str = "io.github.sha5b.Cloudy") -> int:
    from .core.credentials import load_local_env

    load_local_env()  # pull CLOUDY_* secrets from ~/.config/cloudy/secrets.env
    app = CloudyApplication(application_id=app_id, version=version)
    return app.run(sys.argv)


if __name__ == "__main__":
    sys.exit(main())
