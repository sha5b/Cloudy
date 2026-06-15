# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Shahab Nedaei
"""A reusable month calendar grid (used by the Calendar tab and the Dashboard).

Renders one month as a 7×6 grid of day cells with event chips. Navigation moves
between months (so past events are reachable); ``on_range(first, last)`` fires
with the visible date span whenever the month changes so the caller can fetch,
and ``on_event(event)`` fires when an event chip is clicked. Events are the
normalized dicts the REST clients return (``start`` ISO, ``subject``,
``all_day``); whatever dict is passed in is handed back verbatim to ``on_event``.
"""

from __future__ import annotations

import calendar as _calmod
from datetime import date, datetime
from gettext import gettext as _

from gi.repository import Gtk, Pango

from .format import esc
from .metrics import EDGE, SPACE_S, SPACE_XS

_WEEKDAYS = (_("Mon"), _("Tue"), _("Wed"), _("Thu"), _("Fri"), _("Sat"), _("Sun"))
_MAX_CHIPS = 3  # per day cell before "+N more"


def _event_date(ev: dict) -> str:
    return (ev.get("start", "") or "").partition("T")[0]


class MonthGrid(Gtk.Box):
    __gtype_name__ = "CloudyMonthGrid"

    def __init__(self, on_event=None, on_range=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self._on_event = on_event
        self._on_range = on_range
        self._events: list[dict] = []
        today = date.today()
        self._year, self._month = today.year, today.month

        # Header: ‹ Month Year › … Today
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=SPACE_S,
                         margin_top=SPACE_S, margin_bottom=SPACE_S,
                         margin_start=EDGE, margin_end=EDGE)
        prev = Gtk.Button(icon_name="go-previous-symbolic", tooltip_text=_("Previous month"))
        prev.add_css_class("flat")
        prev.connect("clicked", lambda *_a: self._shift(-1))
        nxt = Gtk.Button(icon_name="go-next-symbolic", tooltip_text=_("Next month"))
        nxt.add_css_class("flat")
        nxt.connect("clicked", lambda *_a: self._shift(1))
        self._title = Gtk.Label(xalign=0, hexpand=True)
        self._title.add_css_class("title-3")
        today_btn = Gtk.Button(label=_("Today"))
        today_btn.add_css_class("flat")
        today_btn.connect("clicked", lambda *_a: self._goto_today())
        header.append(prev)
        header.append(nxt)
        header.append(self._title)
        header.append(today_btn)
        self.append(header)

        # Weekday labels.
        self._weekrow = Gtk.Grid(column_homogeneous=True,
                                 margin_start=EDGE, margin_end=EDGE)
        for col, name in enumerate(_WEEKDAYS):
            lbl = Gtk.Label(label=name, xalign=0)
            lbl.add_css_class("cloudy-meta")
            self._weekrow.attach(lbl, col, 0, 1, 1)
        self.append(self._weekrow)

        self._grid = Gtk.Grid(column_homogeneous=True, row_homogeneous=True,
                              column_spacing=SPACE_XS, row_spacing=SPACE_XS,
                              margin_start=EDGE, margin_end=EDGE, margin_bottom=EDGE)
        self._grid.set_vexpand(True)
        self._grid.set_hexpand(True)
        scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER,
                                      vexpand=True, child=self._grid)
        self.append(scroller)

        self._rebuild()

    # -- public API -------------------------------------------------------
    def set_events(self, events: list[dict]) -> None:
        self._events = events or []
        self._rebuild()

    def visible_range(self) -> tuple[str, str]:
        """ISO (date-only) first/last day of the currently displayed grid."""
        weeks = _calmod.Calendar(firstweekday=0).monthdatescalendar(self._year, self._month)
        first, last = weeks[0][0], weeks[-1][-1]
        return first.isoformat(), last.isoformat()

    # -- navigation -------------------------------------------------------
    def _shift(self, delta: int) -> None:
        m = self._month - 1 + delta
        self._year, self._month = self._year + m // 12, m % 12 + 1
        self._rebuild()
        self._emit_range()

    def _goto_today(self) -> None:
        today = date.today()
        if (self._year, self._month) == (today.year, today.month):
            return
        self._year, self._month = today.year, today.month
        self._rebuild()
        self._emit_range()

    def _emit_range(self) -> None:
        if self._on_range is not None:
            first, last = self.visible_range()
            self._on_range(first, last)

    # -- rendering --------------------------------------------------------
    def _rebuild(self) -> None:
        self._title.set_text(date(self._year, self._month, 1).strftime("%B %Y"))
        child = self._grid.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self._grid.remove(child)
            child = nxt

        by_day: dict[str, list[dict]] = {}
        for ev in self._events:
            by_day.setdefault(_event_date(ev), []).append(ev)
        for evs in by_day.values():
            evs.sort(key=lambda e: e.get("start", ""))

        today = date.today()
        weeks = _calmod.Calendar(firstweekday=0).monthdatescalendar(self._year, self._month)
        for r, week in enumerate(weeks):
            for c, day in enumerate(week):
                self._grid.attach(
                    self._day_cell(day, day.month == self._month, day == today,
                                   by_day.get(day.isoformat(), [])),
                    c, r, 1, 1)

    def _day_cell(self, day: date, in_month: bool, is_today: bool,
                  events: list[dict]) -> Gtk.Widget:
        cell = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=SPACE_XS)
        cell.add_css_class("card")
        cell.add_css_class("cloudy-day")
        if not in_month:
            cell.add_css_class("outside")
        if is_today:
            cell.add_css_class("today")
        num = Gtk.Label(label=str(day.day), xalign=0, margin_top=SPACE_XS // 2,
                        margin_start=SPACE_S)
        num.add_css_class("caption")
        if is_today:
            num.add_css_class("accent")
            num.add_css_class("heading")
        cell.append(num)

        for ev in events[:_MAX_CHIPS]:
            cell.append(self._chip(ev))
        if len(events) > _MAX_CHIPS:
            more = Gtk.Label(label=_("+%d more") % (len(events) - _MAX_CHIPS),
                             xalign=0, margin_start=SPACE_S)
            more.add_css_class("cloudy-meta")
            cell.append(more)
        return cell

    def _chip(self, ev: dict) -> Gtk.Widget:
        start = ev.get("start", "")
        when = "" if ev.get("all_day") else start.partition("T")[2][:5]
        text = f"{when} {ev.get('subject') or _('(no title)')}".strip()
        btn = Gtk.Button(has_frame=False)
        btn.add_css_class("flat")
        btn.add_css_class("cloudy-chip")
        lbl = Gtk.Label(label=esc(text), xalign=0, use_markup=True,
                        ellipsize=Pango.EllipsizeMode.END)
        lbl.add_css_class("caption")
        btn.set_child(lbl)
        if self._on_event is not None:
            btn.connect("clicked", lambda *_a, e=ev: self._on_event(e))
        return btn
