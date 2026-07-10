# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Shahab Nedaei
"""Avatar + presence-dot builder for the chat list."""

from __future__ import annotations

from gettext import gettext as _

from gi.repository import Adw, Gtk

AVATAR_COLORS = 8

_PRESENCE = {
    "Available": ("available", _("Available")),
    "AvailableIdle": ("available", _("Available")),
    "Away": ("away", _("Away")),
    "BeRightBack": ("away", _("Be right back")),
    "Busy": ("busy", _("Busy")),
    "BusyIdle": ("busy", _("Busy")),
    "DoNotDisturb": ("dnd", _("Do not disturb")),
    "Offline": ("offline", _("Offline")),
    "PresenceUnknown": ("offline", _("Offline")),
    "OffWork": ("offline", _("Off work")),
}


def color_index(chat: dict) -> int:
    key = chat.get("name") or chat.get("id") or "?"
    return sum(key.encode("utf-8")) % AVATAR_COLORS


def presence_label(availability: str) -> str:
    """Human-readable presence label ("Available", "Busy", …), or "" when
    unknown — used for the 1:1 chat header subtitle."""
    if not availability:
        return ""
    return _PRESENCE.get(availability, ("offline", availability))[1]


def presence_dot(availability: str) -> Gtk.Widget | None:
    if not availability:
        return None
    state, label = _PRESENCE.get(availability, ("offline", availability))
    dot = Gtk.Box()
    dot.set_size_request(13, 13)
    dot.add_css_class("cloudy-presence")
    dot.add_css_class(f"cloudy-presence-{state}")
    dot.set_tooltip_text(label)
    return dot


def presence_dot_for(chat: dict, presence: dict) -> Gtk.Widget | None:
    if chat.get("kind") not in ("oneOnOne", ""):
        return None
    ids = chat.get("member_ids") or []
    if len(ids) != 1:
        return None
    avail = (presence.get(ids[0]) or {}).get("availability", "")
    return presence_dot(avail)


def build_avatar(chat: dict, is_meeting: bool, unread: bool,
                 presence: dict | None = None) -> Gtk.Overlay:
    overlay = Gtk.Overlay(valign=Gtk.Align.CENTER)
    face = Adw.Avatar(size=38)
    face.add_css_class("cloudy-avatar-flat")
    if is_meeting:
        face.set_show_initials(False)
        face.set_icon_name("x-office-calendar-symbolic")
    else:
        face.set_show_initials(True)
        face.set_text(chat.get("name", "") or "?")
        face.add_css_class(f"cloudy-avatar-c{color_index(chat)}")
    overlay.set_child(face)
    overlay._presence_dot = None

    dot = presence_dot_for(chat, presence or {})
    if dot is not None:
        dot.set_halign(Gtk.Align.END)
        dot.set_valign(Gtk.Align.END)
        overlay.add_overlay(dot)
        overlay._presence_dot = dot

    if unread:
        udot = Gtk.Image.new_from_icon_name("media-record-symbolic")
        udot.set_pixel_size(11)
        udot.add_css_class("cloudy-unread-dot")
        udot.set_halign(Gtk.Align.END)
        udot.set_valign(Gtk.Align.START)
        overlay.add_overlay(udot)
    return overlay


def refresh_presence(overlay: Gtk.Overlay, chat: dict, presence: dict) -> None:
    old = getattr(overlay, "_presence_dot", None)
    if old is not None:
        overlay.remove_overlay(old)
        overlay._presence_dot = None
    dot = presence_dot_for(chat, presence)
    if dot is not None:
        dot.set_halign(Gtk.Align.END)
        dot.set_valign(Gtk.Align.END)
        overlay.add_overlay(dot)
        overlay._presence_dot = dot
