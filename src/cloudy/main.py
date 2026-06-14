# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Fiber Elements
"""Entry point: create and run the Adwaita application."""

import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from .application import CloudyApplication


def main(version: str = "0.0.0", app_id: str = "com.fiberelements.Cloudy") -> int:
    """Run the application. Returns the process exit code."""
    from .core.credentials import load_local_env

    load_local_env()  # pull CLOUDY_* secrets from ~/.config/cloudy/secrets.env
    app = CloudyApplication(application_id=app_id, version=version)
    return app.run(sys.argv)


if __name__ == "__main__":
    sys.exit(main())
