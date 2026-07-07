# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Shahab Nedaei
"""New-event editor for the Calendar surface.

A **non-modal window** (``editor_window.EditorWindow``) — same convention as
compose — so it doesn't block the rest of the app. Collects a title, a day
(``Gtk.Calendar``), start/end times (or all-day), location, attendees and a
description, builds UTC ISO-8601 slots and hands them to ``create_fn(**fields)``
off-thread. Also exposes :func:`parse_ics` for opening ``.ics`` invitations.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from gettext import gettext as _

from gi.repository import Adw, GLib, Gtk

from .editor_window import EditorWindow
from .event_time import local_to_utc_iso, parse_hhmm
from .source_nav import run_async


def _ics_dt(value: str) -> datetime | None:
    """A basic-format iCalendar date/date-time as a naive *local* datetime for
    prefilling the form (a trailing ``Z`` is converted from UTC)."""
    from datetime import timezone

    txt = (value or "").strip()
    try:
        if "T" not in txt:
            return datetime.strptime(txt, "%Y%m%d")
        utc = txt.endswith("Z")
        dt = datetime.strptime(txt.rstrip("Z"), "%Y%m%dT%H%M%S")
    except ValueError:
        return None
    if utc:
        dt = dt.replace(tzinfo=timezone.utc).astimezone().replace(tzinfo=None)
    return dt


def parse_ics(path: str) -> dict:
    """VEVENT reader for the system ``.ics`` handler, backed by
    ``core.ics.parse_invite`` (full RFC 5545 line unfolding + escaping — the
    previous line-by-line reader truncated folded SUMMARY/DESCRIPTION values,
    which real Outlook/Google invites always contain)."""
    from ..core import ics

    with open(path, encoding="utf-8", errors="replace") as fh:
        invite = ics.parse_invite(fh.read())
    if not invite:
        return {}
    return {
        "subject": invite.get("summary", ""),
        "location": invite.get("location", ""),
        "body": invite.get("description", ""),
        "all_day": invite.get("all_day", False),
        "start_dt": _ics_dt(invite.get("dtstart", "")),
        "end_dt": _ics_dt(invite.get("dtend", "")),
    }


class EventWindow(EditorWindow):
    def __init__(self, window, *, on_calendar: str, create_fn,
                 title: str | None = None, initial: dict | None = None,
                 primary_label: str | None = None):
        super().__init__(window, title=title or _("New event"),
                         primary_label=primary_label or _("Create"))
        self._window = window
        self._create_fn = create_fn

        group = Adw.PreferencesGroup(description=_("On %s") % on_calendar)
        self._subject = Adw.EntryRow(title=_("Title"))
        group.add(self._subject)
        self._all_day = Adw.SwitchRow(title=_("All day"))
        self._all_day.connect("notify::active", self._on_all_day)
        group.add(self._all_day)
        self._start_time = Adw.EntryRow(title=_("Start (HH:MM)"))
        self._start_time.set_text("09:00")
        group.add(self._start_time)
        self._end_time = Adw.EntryRow(title=_("End (HH:MM)"))
        self._end_time.set_text("10:00")
        group.add(self._end_time)
        self._location = Adw.EntryRow(title=_("Location"))
        group.add(self._location)
        self._attendees = Adw.EntryRow(title=_("Attendees (comma-separated)"))
        group.add(self._attendees)
        # Teams meeting (Microsoft) / Google Meet — the provider provisions the
        # link and fills it on the event, so the detail view shows a Join button.
        self._online = Adw.SwitchRow(
            title=_("Online meeting"),
            subtitle=_("Add a Teams / Google Meet link"))
        group.add(self._online)

        self._calendar = Gtk.Calendar()
        cal_group = Adw.PreferencesGroup(title=_("Day"))
        cal_group.add(self._calendar)

        self._body = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR, top_margin=10,
                                  bottom_margin=10, left_margin=12, right_margin=12)
        body_scroll = Gtk.ScrolledWindow(
            vexpand=True, hexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER,
            child=self._body)
        body_scroll.add_css_class("card")
        body_group = Adw.PreferencesGroup(title=_("Description"))
        body_group.add(body_scroll)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14,
                          margin_top=12, margin_bottom=12, margin_start=12, margin_end=12)
        for w in (group, cal_group, body_group):
            content.append(w)
        self.set_body(Gtk.ScrolledWindow(
            vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER, child=content))

        if initial:
            self._prefill(initial)

    def _prefill(self, initial: dict) -> None:
        self._subject.set_text(initial.get("subject", "") or "")
        self._location.set_text(initial.get("location", "") or "")
        if initial.get("body"):
            self._body.get_buffer().set_text(initial["body"])
        if initial.get("all_day"):
            self._all_day.set_active(True)
        start = initial.get("start_dt")
        end = initial.get("end_dt")
        if start is not None:
            self._calendar.select_day(GLib.DateTime.new_local(
                start.year, start.month, start.day, start.hour, start.minute, 0))
            self._start_time.set_text(start.strftime("%H:%M"))
        if end is not None:
            self._end_time.set_text(end.strftime("%H:%M"))

    def _on_all_day(self, *_a) -> None:
        timed = not self._all_day.get_active()
        self._start_time.set_sensitive(timed)
        self._end_time.set_sensitive(timed)

    def on_primary(self) -> None:
        subject = self._subject.get_text().strip()
        if not subject:
            self.toast(_("Give the event a title."))
            return
        gdate = self._calendar.get_date()
        day = datetime(gdate.get_year(), gdate.get_month(), gdate.get_day_of_month())
        all_day = self._all_day.get_active()
        if all_day:
            start, end = day, day + timedelta(days=1)
        else:
            sh, sm = parse_hhmm(self._start_time.get_text(), (9, 0))
            eh, em = parse_hhmm(self._end_time.get_text(), (10, 0))
            start = day.replace(hour=sh, minute=sm)
            end = day.replace(hour=eh, minute=em)
            if end <= start:
                end = start + timedelta(hours=1)

        # The calendar/time fields give a naive *local* wall-clock pick, but the
        # clients send the ISO slot as UTC (Graph strips the Z + timeZone=UTC;
        # Google honours the Z); local_to_utc_iso does the conversion.
        attendees = [a.strip() for a in self._attendees.get_text().split(",")
                     if a.strip()]
        buf = self._body.get_buffer()
        body = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), True)

        self.primary_btn.set_sensitive(False)
        self.toast(_("Creating event…"))
        create_fn = self._create_fn
        run_async(
            lambda: create_fn(subject=subject,
                              start_iso=local_to_utc_iso(start, all_day=all_day),
                              end_iso=local_to_utc_iso(end, all_day=all_day),
                              location=self._location.get_text().strip(), body=body,
                              attendees=attendees, all_day=all_day,
                              online=self._online.get_active()),
            self._on_created)

    def _on_created(self, _result, error) -> bool:
        if error:
            self.primary_btn.set_sensitive(True)
            self.toast(_("Couldn't create event: %s") % error)
            return False
        self._window.add_toast(_("Event created."))
        self.close()
        return False
