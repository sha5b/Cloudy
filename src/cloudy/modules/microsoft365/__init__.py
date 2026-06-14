# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Fiber Elements
"""Microsoft 365 module package.

One Microsoft 365 account = one Graph login that surfaces several capabilities:
OneDrive/SharePoint files, mail, and calendar. OneDrive is not a separate
account — it is the Files capability of this provider. See docs/MODULES.md.
"""

from .module import Microsoft365Module

#: Discovered by the plugin engine.
MODULE = Microsoft365Module
