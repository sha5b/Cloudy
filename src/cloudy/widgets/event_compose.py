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


def _ics_dt(value: str) -> tuple[datetime | None, bool]:
    """Parse an ICS date/date-time value → ``(datetime, all_day)``."""
    value = value.strip()
    for fmt, all_day in (("%Y%m%dT%H%M%SZ", False), ("%Y%m%dT%H%M%S", False),
                         ("%Y%m%d", True)):
        try:
            return datetime.strptime(value, fmt), all_day
        except ValueError:
            continue
    return None, False


def parse_ics(path: str) -> dict:
    """Minimal VEVENT reader: title, start/end, location, description."""
    fields: dict = {}
    with open(path, encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            line = raw.rstrip("\n").rstrip("\r")
            key, _sep, val = line.partition(":")
            name = key.split(";", 1)[0].upper()
            if name == "SUMMARY":
                fields["subject"] = val
            elif name == "LOCATION":
                fields["location"] = val
            elif name == "DESCRIPTION":
                fields["body"] = val.replace("\\n", "\n").replace("\\,", ",")
            elif name == "DTSTART":
                dt, all_day = _ics_dt(val)
                fields["start_dt"], fields["all_day"] = dt, all_day
            elif name == "DTEND":
                dt, _ad = _ics_dt(val)
                fields["end_dt"] = dt
    return fields


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
                              attendees=attendees, all_day=all_day),
            self._on_created)

    def _on_created(self, _result, error) -> bool:
        if error:
            self.primary_btn.set_sensitive(True)
            self.toast(_("Couldn't create event: %s") % error)
            return False
        self._window.add_toast(_("Event created."))
        self.close()
        return False
