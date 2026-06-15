# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Shahab Nedaei
"""Cloudy design tokens — one place for spacing, icon sizes and window sizes.

The UI previously hardcoded an ad-hoc ladder (2/3/6/10/14/18/20…). These tokens
snap everything to a **4px scale** so margins, gaps and padding stay in rhythm
across every surface. Import the names rather than typing literals.

Pair with ``data/style.css`` (loaded in ``application.py``) for the semantic CSS
classes (`.cloudy-meta`, `.attendee-pill`, `.calendar-cell`, …) and the helpers
in ``widgets/states.py`` for empty/loading states.
"""

from __future__ import annotations

# -- spacing scale (px) --------------------------------------------------
SPACE_XS = 4
SPACE_S = 8
SPACE_M = 12
SPACE_L = 16
SPACE_XL = 24

# Standard panel edge inset (leading/trailing) and a header's vertical padding.
EDGE = SPACE_L          # 16 — horizontal inset for panel headers/content
PAD = SPACE_M           # 12 — default content padding for forms/boxes

# -- icon glyph sizes (px) ----------------------------------------------
ICON_SM = 16            # inline / list-row leading glyphs
ICON_MD = 24            # stat / section glyphs
ICON_LG = 48            # grid tiles, empty-state art

# -- standard window sizes (w, h) ---------------------------------------
WIN_FORM = (640, 620)   # editors: compose, new/edit event
WIN_READ = (620, 720)   # read/detail surfaces
