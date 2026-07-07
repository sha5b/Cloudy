# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Shahab Nedaei
"""Standalone event-detail window.

Opening an event (from the Calendar grid, the agenda list, or the Dashboard)
presents this **non-modal** window — the project convention for read/act
surfaces — rather than swapping an inline pane. It fetches the event off-thread,
shows the detail (time, location, organizer, the attendee response tracker, body)
with Join/Open/RSVP, and Delete/Edit actions in the header.

**Edit happens inline**: the ✏️ button toggles the detail pane into an edit
form (subject, all-day, day, start/end time, location, removable attendees,
description) with Save/Cancel in the header, then calls ``client.update_event``
off-thread. ``on_changed`` is invoked after a successful RSVP, edit or delete so
the opener can refresh.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from gettext import gettext as _

from gi.repository import Adw, GLib, Gtk

from .event_time import iso_to_local_naive, local_to_utc_iso, parse_hhmm
from .format import esc
from .metrics import WIN_READ
from .source_nav import friendly_error, invalidate_cached, run_async


class EventDetailWindow(Adw.Window):
    __gtype_name__ = "CloudyEventDetailWindow"

    def __init__(self, window, account, event_id: str, *, on_changed=None):
        # NOT transient_for: an independent toplevel gets minimize/maximize
        # (GNOME hides those on transient "dialog" windows).
        super().__init__(modal=False, default_width=WIN_READ[0],
                         default_height=WIN_READ[1], title=_("Event"))
        self._window = window
        self._account = account
        self._eid = event_id
        self._on_changed = on_changed
        self._event: dict = {}
        self._editing = False
        # Edit-form widgets, built lazily on first Edit; kept so Save can read them.
        self._form: dict = {}
        self._attendee_emails: list[str] = []

        self._content = Adw.Bin(vexpand=True)
        self._content.set_child(self._spinner())

        # -- detail-mode header buttons --
        self._delete_btn = Gtk.Button(
            icon_name="user-trash-symbolic", tooltip_text=_("Delete event"),
            sensitive=False)
        self._delete_btn.connect("clicked", self._on_delete_clicked)
        self._edit_btn = Gtk.Button(
            icon_name="document-edit-symbolic", tooltip_text=_("Edit event"),
            sensitive=False)
        self._edit_btn.connect("clicked", self._on_edit_clicked)
        # -- edit-mode header buttons --
        self._cancel_btn = Gtk.Button(label=_("Cancel"))
        self._cancel_btn.connect("clicked", lambda _b: self._exit_edit())
        self._save_btn = Gtk.Button(label=_("Save"))
        self._save_btn.add_css_class("suggested-action")
        self._save_btn.connect("clicked", self._on_save_clicked)

        header = Adw.HeaderBar()
        header.set_decoration_layout(":minimize,maximize,close")
        header.pack_start(self._cancel_btn)
        header.pack_end(self._delete_btn)
        header.pack_end(self._edit_btn)
        header.pack_end(self._save_btn)
        self._header = header
        self._set_mode(editing=False)

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(header)
        toolbar.set_content(self._content)
        self.set_content(toolbar)

        self._load()

    # -- load -------------------------------------------------------------
    def _load(self) -> None:
        def work():
            from .clients import build_account_client

            client = build_account_client(self._window.get_application(), self._account)
            return client.get_event(self._eid)

        run_async(work, self._on_loaded)

    def _on_loaded(self, event, error) -> bool:
        if error:
            self._content.set_child(Adw.StatusPage(
                icon_name="dialog-error-symbolic",
                title=_("Couldn't open event"), description=esc(error)))
            return False
        self._event = event
        if event.get("subject"):
            self.set_title(event["subject"])
        self._render_detail()
        editable = not str(self._eid).startswith("group:")
        self._delete_btn.set_sensitive(editable)
        self._edit_btn.set_sensitive(editable)
        return False

    def _render_detail(self) -> None:
        from .event_view import build_event_content

        self._set_mode(editing=False)
        self._content.set_child(build_event_content(self._event, on_rsvp=self._on_rsvp))

    def _set_mode(self, *, editing: bool) -> None:
        """Swap the header between detail (Edit/Delete) and edit (Cancel/Save)."""
        self._editing = editing
        self._edit_btn.set_visible(not editing)
        self._delete_btn.set_visible(not editing)
        self._cancel_btn.set_visible(editing)
        self._save_btn.set_visible(editing)
        self.set_title(_("Edit event") if editing
                       else (self._event.get("subject") or _("Event")))

    # -- edit -------------------------------------------------------------
    def _on_edit_clicked(self, _btn) -> None:
        self._set_mode(editing=True)
        self._content.set_child(self._build_edit_form(self._event))

    def _exit_edit(self) -> None:
        self._form = {}
        self._attendee_emails = []
        self._att_rows = []
        self._render_detail()

    def _build_edit_form(self, ev: dict) -> Gtk.Widget:
        group = Adw.PreferencesGroup()
        subject = Adw.EntryRow(title=_("Title"))
        subject.set_text(ev.get("subject", "") or "")
        group.add(subject)

        all_day = Adw.SwitchRow(title=_("All day"))
        all_day.set_active(bool(ev.get("all_day")))
        group.add(all_day)

        start_dt = iso_to_local_naive(ev.get("start", ""))
        end_dt = iso_to_local_naive(ev.get("end", ""))
        # The form has a single day picker; preserve a multi-day span so editing
        # the time/day of a multi-day event doesn't silently collapse it to one day.
        self._edit_day_span = 0
        if start_dt is not None and end_dt is not None:
            self._edit_day_span = max(0, (end_dt.date() - start_dt.date()).days)
        start_time = Adw.EntryRow(title=_("Start (HH:MM)"))
        start_time.set_text(start_dt.strftime("%H:%M") if start_dt else "09:00")
        group.add(start_time)
        end_time = Adw.EntryRow(title=_("End (HH:MM)"))
        end_time.set_text(end_dt.strftime("%H:%M") if end_dt else "10:00")
        group.add(end_time)

        location = Adw.EntryRow(title=_("Location"))
        location.set_text(ev.get("location", "") or "")
        group.add(location)

        def sync_timed(*_a):
            timed = not all_day.get_active()
            start_time.set_sensitive(timed)
            end_time.set_sensitive(timed)
        all_day.connect("notify::active", sync_timed)
        sync_timed()

        calendar = Gtk.Calendar()
        if start_dt is not None:
            calendar.select_day(GLib.DateTime.new_local(
                start_dt.year, start_dt.month, start_dt.day,
                start_dt.hour, start_dt.minute, 0))
        cal_group = Adw.PreferencesGroup(title=_("Day"))
        cal_group.add(calendar)

        # Attendees: existing ones are removable rows; an entry adds more.
        self._attendee_emails = [
            a.get("email", "") for a in (ev.get("attendees") or [])
            if isinstance(a, dict) and a.get("email")]
        self._attendee_names = {
            a.get("email", ""): a.get("name", "")
            for a in (ev.get("attendees") or []) if isinstance(a, dict)}
        att_group = Adw.PreferencesGroup(
            title=_("Attendees"),
            description=_("Removing an attendee re-sends the updated list."))
        add_row = Adw.EntryRow(title=_("Add attendees (comma-separated)"))
        att_group.add(add_row)
        self._att_group = att_group
        self._att_add_row = add_row
        self._att_rows: list[Gtk.Widget] = []
        self._rebuild_attendee_rows()

        # Description: prefill only plain-text bodies. update_event omits an empty
        # body (PATCH keeps the server's), so an HTML-bodied event stays intact
        # unless the user types here — avoids dumping raw HTML into a text field.
        body_view = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR, top_margin=10,
                                 bottom_margin=10, left_margin=12, right_margin=12)
        body = ev.get("body", "") or ""
        if body.strip() and not ev.get("body_html"):
            body_view.get_buffer().set_text(body)
        body_scroll = Gtk.ScrolledWindow(
            vexpand=True, hexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER,
            height_request=140, child=body_view)
        body_scroll.add_css_class("card")
        body_group = Adw.PreferencesGroup(title=_("Description"))
        if ev.get("body_html"):
            body_group.set_description(
                _("Leave blank to keep the current description."))
        body_group.add(body_scroll)

        self._form = {
            "subject": subject, "all_day": all_day, "start_time": start_time,
            "end_time": end_time, "location": location, "calendar": calendar,
            "body": body_view,
        }

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14,
                          margin_top=12, margin_bottom=12, margin_start=12,
                          margin_end=12)
        for w in (group, cal_group, att_group, body_group):
            content.append(w)
        return Gtk.ScrolledWindow(
            vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER, child=content)

    def _rebuild_attendee_rows(self) -> None:
        for row in self._att_rows:
            self._att_group.remove(row)
        self._att_rows = []
        for email in self._attendee_emails:
            name = self._attendee_names.get(email, "")
            row = Adw.ActionRow(title=name or email,
                                subtitle=email if name else "")
            remove = Gtk.Button(icon_name="list-remove-symbolic",
                                tooltip_text=_("Remove attendee"),
                                valign=Gtk.Align.CENTER)
            remove.add_css_class("flat")
            remove.connect("clicked", lambda _b, e=email: self._remove_attendee(e))
            row.add_suffix(remove)
            self._att_group.add(row)
            self._att_rows.append(row)

    def _remove_attendee(self, email: str) -> None:
        self._attendee_emails = [e for e in self._attendee_emails if e != email]
        self._rebuild_attendee_rows()

    def _on_save_clicked(self, _btn) -> None:
        f = self._form
        subject = f["subject"].get_text().strip()
        if not subject:
            self._window.add_toast(_("Give the event a title."))
            return
        gdate = f["calendar"].get_date()
        day = datetime(gdate.get_year(), gdate.get_month(), gdate.get_day_of_month())
        all_day = f["all_day"].get_active()
        span = getattr(self, "_edit_day_span", 0)
        if all_day:
            # All-day end is exclusive (1 day past the last day); keep the span.
            start, end = day, day + timedelta(days=max(span, 1))
        else:
            sh, sm = parse_hhmm(f["start_time"].get_text(), (9, 0))
            eh, em = parse_hhmm(f["end_time"].get_text(), (10, 0))
            start = day.replace(hour=sh, minute=sm)
            end = (day + timedelta(days=span)).replace(hour=eh, minute=em)
            if end <= start:
                end = start + timedelta(hours=1)

        # Desired attendee list = remaining rows + any newly typed addresses.
        attendees = list(self._attendee_emails)
        for a in self._att_add_row.get_text().split(","):
            a = a.strip()
            if a and a not in attendees:
                attendees.append(a)

        buf = f["body"].get_buffer()
        body = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), True)
        location = f["location"].get_text().strip()

        eid = self._eid
        account = self._account
        win = self._window
        self._save_btn.set_sensitive(False)
        self._window.add_toast(_("Saving…"))

        def work():
            from .clients import build_account_client

            client = build_account_client(win.get_application(), account)
            return client.update_event(
                eid, subject=subject,
                start_iso=local_to_utc_iso(start, all_day=all_day),
                end_iso=local_to_utc_iso(end, all_day=all_day),
                location=location, body=body, attendees=attendees, all_day=all_day)

        run_async(work, self._on_saved)

    def _on_saved(self, _result, error) -> bool:
        self._save_btn.set_sensitive(True)
        if error:
            self._window.add_toast(_("Couldn't save event: %s") % friendly_error(error))
            return False
        self._window.add_toast(_("Event saved."))
        self._invalidate()
        if self._on_changed is not None:
            self._on_changed()
        self._form = {}
        self._load()  # reloads detail (also flips back to detail mode)
        return False

    # -- actions ----------------------------------------------------------
    def _on_rsvp(self, action: str) -> None:
        self._window.add_toast(_("Sending response…"))

        def work():
            from .clients import build_account_client

            client = build_account_client(self._window.get_application(), self._account)
            client.respond_event(self._eid, action)

        run_async(work, self._rsvp_done)

    def _rsvp_done(self, _result, error) -> bool:
        if error:
            self._window.add_toast(_("Couldn't send response: %s") % friendly_error(error))
            return False
        self._window.add_toast(_("Response sent."))
        self._invalidate()
        if self._on_changed is not None:
            self._on_changed()
        self._load()  # refresh the detail (response state changed)
        return False

    def _on_delete_clicked(self, _btn) -> None:
        dialog = Adw.AlertDialog(
            heading=_("Delete event?"),
            body=_("This removes the event from the calendar."))
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("delete", _("Delete"))
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.connect("response",
                       lambda _d, r: self._do_delete() if r == "delete" else None)
        dialog.present(self)

    def _do_delete(self) -> None:
        self._window.add_toast(_("Deleting…"))

        def work():
            from .clients import build_account_client

            client = build_account_client(self._window.get_application(), self._account)
            client.delete_event(self._eid)

        run_async(work, self._deleted)

    def _deleted(self, _result, error) -> bool:
        if error:
            self._window.add_toast(_("Couldn't delete event: %s") % friendly_error(error))
            return False
        self._window.add_toast(_("Event deleted."))
        self._invalidate()
        if self._on_changed is not None:
            self._on_changed()
        self.close()
        return False

    def _invalidate(self) -> None:
        # Every opener (Calendar tab, Dashboard, notifications) may hold a
        # "fresh" cached copy of this event; drop them all so the next render
        # can't serve the pre-write state.
        invalidate_cached(self._window.get_application(),
                          self._account.id, "events")

    @staticmethod
    def _spinner() -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, halign=Gtk.Align.CENTER,
                      valign=Gtk.Align.CENTER, hexpand=True, vexpand=True)
        sp = Gtk.Spinner(width_request=32, height_request=32)
        sp.start()
        box.append(sp)
        return box
