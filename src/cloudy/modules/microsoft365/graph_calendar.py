# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Shahab Nedaei
"""Calendar domain of the Graph client: calendars, events (own/shared/group),
RSVP and event CRUD."""

from __future__ import annotations

import html
import urllib.parse
from datetime import datetime

from .graph_http import GraphError, _split_id

from ...core.auth.msal_graph import (
    SCOPES_GROUPS,
    SCOPES_MAIL,
    SCOPES_MAIL_SHARED,
)


class GraphCalendarMixin:
    # -- Calendar ---------------------------------------------------------
    def list_calendars(self) -> list[dict]:
        return [
            {"id": c["id"], "name": c.get("name", ""),
             "default": bool(c.get("isDefaultCalendar"))}
            for c in self._get_all(
                "/me/calendars?$select=id,name,isDefaultCalendar&$top=50",
                SCOPES_MAIL)
        ]

    def list_events(self, start_iso: str, end_iso: str, *,
                    calendar_id: str | None = None, limit: int = 50) -> list[dict]:
        """Calendar view between two ISO-8601 UTC timestamps.

        ``calendar_id`` selects a specific calendar:
        * ``None`` or empty — the user's default calendar (`/me/calendarView`).
        * ``"me:<id>"`` or just ``"<id>"`` — a specific owned calendar.
        * ``"shared:<address>:<id>"`` — a delegated/shared calendar.
        * ``"group:<group_id>"`` — a group/team calendar.
        """
        headers = self._calendar_headers()
        if not calendar_id:
            return self._all_my_events(start_iso, end_iso, limit, headers)
        path = self._calendar_view_path(calendar_id, start_iso, end_iso, limit)
        scope = self._calendar_scope(calendar_id)
        items = self._get_all(path, scope, headers)
        events = self._events_from_json({"value": items})
        if calendar_id and calendar_id.startswith("group:"):
            _, gid = calendar_id.split(":", 1)
            for e in events:
                e["id"] = f"group:{gid}:{e['id']}"
        elif calendar_id and calendar_id.startswith("shared:"):
            parts = calendar_id.split(":", 2)
            if len(parts) >= 2:
                addr = parts[1]
                for e in events:
                    e["id"] = f"shared:{addr}:{e['id']}"
        return events

    def _all_my_events(self, start_iso: str, end_iso: str, limit: int,
                       headers) -> list[dict]:
        """The "Me" source: default calendarView merged with every other owned
        or shared-in calendar. ``/me/calendarView`` alone covers ONLY the
        default calendar, which silently hid secondary calendars (custom,
        holidays, calendars shared into the mailbox). Extra calendars are
        best-effort so one broken share can't blank the whole month."""
        params = self._calview_params(start_iso, end_iso, limit)
        items = self._get_all(f"/me/calendarView?{params}", SCOPES_MAIL, headers)
        events = self._events_from_json({"value": items})
        seen = {e["id"] for e in events}
        try:
            extras = [c for c in self.list_calendars() if not c.get("default")]
        except GraphError:
            extras = []
        for cal in extras[:10]:  # bound the fan-out on calendar-hoarder accounts
            try:
                more = self._get_all(
                    f"/me/calendars/{cal['id']}/calendarView?{params}",
                    SCOPES_MAIL, headers)
            except GraphError:
                continue
            for e in self._events_from_json({"value": more}):
                if e["id"] not in seen:
                    seen.add(e["id"])
                    events.append(e)
        events.sort(key=lambda e: (e.get("start", ""), e.get("subject", "")))
        return events

    def _calendar_view_path(self, calendar_id: str | None,
                            start_iso: str, end_iso: str, limit: int) -> str:
        params = self._calview_params(start_iso, end_iso, limit)
        if not calendar_id:
            return f"/me/calendarView?{params}"
        if calendar_id.startswith("group:"):
            _, gid = calendar_id.split(":", 1)
            return f"/groups/{gid}/calendarView?{params}"
        if calendar_id.startswith("shared:"):
            parts = calendar_id.split(":", 2)
            address = parts[1] if len(parts) > 1 else ""
            cal = parts[2] if len(parts) > 2 else ""
            if cal:
                return f"/users/{address}/calendars/{cal}/calendarView?{params}"
            return f"/users/{address}/calendarView?{params}"
        if calendar_id.startswith("me:"):
            calendar_id = calendar_id.split(":", 1)[1]
        return f"/me/calendars/{calendar_id}/calendarView?{params}"

    def _calendar_scope(self, calendar_id: str | None) -> list[str]:
        if calendar_id and calendar_id.startswith("shared:"):
            return SCOPES_MAIL_SHARED
        if calendar_id and calendar_id.startswith("group:"):
            return SCOPES_GROUPS
        return SCOPES_MAIL

    def list_group_events(self, group_id: str, start_iso: str, end_iso: str,
                          *, limit: int = 50) -> list[dict]:
        """A group/team calendar view (needs Group.Read.All). Event ids are
        prefixed ``group:<groupId>:`` so detail/RSVP know the group context."""
        headers = self._calendar_headers()
        items = self._get_all(
            f"/groups/{group_id}/calendarView?"
            f"{self._calview_params(start_iso, end_iso, limit)}",
            SCOPES_GROUPS, headers,
        )
        events = self._events_from_json({"value": items})
        for e in events:
            e["id"] = f"group:{group_id}:{e['id']}"
        return events

    def list_shared_events(self, address: str, start_iso: str, end_iso: str,
                           *, limit: int = 50) -> list[dict]:
        """A shared/other mailbox's calendar view (needs Mail.ReadWrite.Shared,
        i.e. delegated calendar access). Event ids are prefixed
        ``shared:<address>:`` so detail routes back to that mailbox."""
        headers = self._calendar_headers()
        items = self._get_all(
            f"/users/{address}/calendarView?"
            f"{self._calview_params(start_iso, end_iso, limit)}",
            SCOPES_MAIL_SHARED, headers,
        )
        events = self._events_from_json({"value": items})
        for e in events:
            e["id"] = f"shared:{address}:{e['id']}"
        return events

    def get_event(self, event_id: str) -> dict:
        """Full event detail for the reading pane."""
        select = ("subject,start,end,location,body,organizer,attendees,"
                  "isAllDay,isOnlineMeeting,onlineMeeting,webLink,responseStatus")
        # Same Prefer: outlook.timezone header as list_events — without it
        # Graph returns start/end in UTC while the rest of the app treats
        # naive times as local, so the detail pane showed (and every re-save
        # silently SHIFTED) the event by the local UTC offset.
        headers = self._calendar_headers()
        if event_id.startswith("group:"):
            _, gid, eid = _split_id(event_id, 3)
            data = self._get(f"/groups/{gid}/events/{eid}?$select={select}",
                             SCOPES_GROUPS, headers)
            can_respond = False
        elif event_id.startswith("shared:"):
            _, address, eid = _split_id(event_id, 3)
            data = self._get(f"/users/{address}/events/{eid}?$select={select}",
                             SCOPES_MAIL_SHARED, headers)
            can_respond = False  # delegated RSVP isn't offered from here
        else:
            data = self._get(f"/me/events/{event_id}?$select={select}",
                             SCOPES_MAIL, headers)
            can_respond = True
        organizer = ((data.get("organizer") or {}).get("emailAddress") or {})
        attendees = []
        for a in data.get("attendees", []) or []:
            ea = a.get("emailAddress") or {}
            attendees.append({
                "name": html.unescape(ea.get("name") or ea.get("address", "")),
                # email is needed by the inline editor: removing an attendee
                # re-sends the *full* desired list (PATCH keeps attendees if omitted).
                "email": ea.get("address", ""),
                # none|organizer|tentativelyAccepted|accepted|declined|notResponded
                "response": (a.get("status") or {}).get("response", "none"),
            })
        body = data.get("body") or {}
        response = (data.get("responseStatus") or {}).get("response", "none")
        return {
            "id": event_id,
            "subject": html.unescape(data.get("subject", "") or "(no title)"),
            "start": (data.get("start") or {}).get("dateTime", ""),
            "end": (data.get("end") or {}).get("dateTime", ""),
            "all_day": data.get("isAllDay", False),
            "location": html.unescape((data.get("location") or {}).get("displayName", "")),
            "organizer": html.unescape(organizer.get("name") or organizer.get("address", "")),
            "attendees": attendees,
            "body": body.get("content", ""),
            "body_html": body.get("contentType") == "html",
            "online_url": (data.get("onlineMeeting") or {}).get("joinUrl", "")
            if data.get("isOnlineMeeting") else "",
            "web_link": data.get("webLink", ""),
            "response": response,
            # Can RSVP only to invites you haven't organized.
            "can_respond": can_respond and response not in ("organizer",),
        }

    def respond_event(self, event_id: str, action: str,
                      comment: str = "", send: bool = True) -> None:
        """RSVP to a meeting (action: accept | tentativelyAccept | decline).
        Needs Calendars.ReadWrite. Not applicable to group events. A
        ``shared:<address>:`` id answers on that delegated calendar (needs the
        shared scopes) — previously it built a malformed ``/me`` path."""
        if event_id.startswith("group:"):
            raise GraphError("Group events can't be answered from here.")
        if action not in ("accept", "tentativelyAccept", "decline"):
            raise GraphError(f"unknown RSVP action: {action}")
        body = {"sendResponse": send, "comment": comment}
        if event_id.startswith("shared:"):
            _, address, eid = _split_id(event_id, 3)
            self._post(f"/users/{address}/events/{eid}/{action}", body,
                       SCOPES_MAIL_SHARED)
            return
        self._post(f"/me/events/{event_id}/{action}", body, SCOPES_MAIL)

    def find_event_by_uid(self, uid: str) -> str:
        """The id of the /me event whose iCalUId matches an iMIP UID, or ``""``.
        Lets the Mail view reconcile an emailed invite with the copy Exchange
        auto-staged on the user's calendar."""
        if not uid:
            return ""
        odata_uid = uid.replace("'", "''")  # OData string-literal escaping
        flt = urllib.parse.quote(f"iCalUId eq '{odata_uid}'")
        data = self._get(f"/me/events?$filter={flt}&$select=id&$top=1", SCOPES_MAIL)
        values = data.get("value", [])
        return values[0].get("id", "") if values else ""

    def create_event(self, *, subject: str, start_iso: str, end_iso: str,
                     source: str = "me", address: str | None = None,
                     location: str = "", body: str = "", attendees=None,
                     all_day: bool = False, html: bool = False,
                     online: bool = False) -> dict:
        """Create an event on the current source's calendar.

        ``me`` → ``/me/events``; ``shared`` → ``/users/{address}/events`` (needs
        Calendars.ReadWrite.Shared). ``online=True`` makes it a Teams meeting —
        Graph provisions the meeting and fills ``onlineMeeting.joinUrl`` on the
        returned event. Group/team calendars are read-only here (the Calendar
        view doesn't offer New there)."""
        event = {
            "subject": subject,
            "start": self._event_slot(start_iso, all_day),
            "end": self._event_slot(end_iso, all_day),
            "isAllDay": all_day,
        }
        if online:
            event["isOnlineMeeting"] = True
            event["onlineMeetingProvider"] = "teamsForBusiness"
        if location:
            event["location"] = {"displayName": location}
        if body:
            event["body"] = {"contentType": "HTML" if html else "Text", "content": body}
        if attendees:
            event["attendees"] = [
                {"emailAddress": {"address": a}, "type": "required"}
                for a in attendees if a
            ]
        if source == "shared" and address:
            return self._post(f"/users/{address}/events", event, SCOPES_MAIL_SHARED)
        return self._post("/me/events", event, SCOPES_MAIL)

    def update_event(self, event_id: str, *, subject: str, start_iso: str,
                     end_iso: str, location: str = "", body: str = "",
                     attendees=None, all_day: bool = False, html: bool = False) -> dict:
        """Edit an existing event (PATCH). Group/team events are read-only."""
        if event_id.startswith("group:"):
            raise GraphError("Group events can't be edited from here.")
        event: dict = {
            "subject": subject,
            "start": self._event_slot(start_iso, all_day),
            "end": self._event_slot(end_iso, all_day),
            "isAllDay": all_day,
        }
        # PATCH leaves omitted fields untouched — so an empty body/location keeps
        # the server's (don't wipe a meeting's body when only the time changed).
        if location:
            event["location"] = {"displayName": location}
        if body:
            event["body"] = {"contentType": "HTML" if html else "Text", "content": body}
        # attendees: ``None`` = leave untouched; a list (even empty) = set it, so
        # removing every attendee in the editor actually clears them server-side.
        if attendees is not None:
            event["attendees"] = [
                {"emailAddress": {"address": a}, "type": "required"}
                for a in attendees if a
            ]
        if event_id.startswith("shared:"):
            _, address, eid = _split_id(event_id, 3)
            return self._patch(f"/users/{address}/events/{eid}", event, SCOPES_MAIL_SHARED)
        return self._patch(f"/me/events/{event_id}", event, SCOPES_MAIL)

    def delete_event(self, event_id: str) -> None:
        """Delete an event from its calendar (group/team events are read-only)."""
        if event_id.startswith("group:"):
            raise GraphError("Group events can't be deleted from here.")
        if event_id.startswith("shared:"):
            _, address, eid = _split_id(event_id, 3)
            self._delete(f"/users/{address}/events/{eid}", SCOPES_MAIL_SHARED)
            return
        self._delete(f"/me/events/{event_id}", SCOPES_MAIL)

    @classmethod
    def _event_slot(cls, iso: str, all_day: bool) -> dict:
        """A Graph start/end slot — the editor sends a UTC ISO, but Graph
        stores wall-clock time + IANA tz (shared by create/update)."""
        if all_day:
            # The editor encodes the picked DATE as UTC midnight (see
            # local_to_utc_iso); take that date verbatim. Converting to local
            # time first landed the event on the PREVIOUS day for every zone
            # west of UTC (UTC midnight is the prior evening there).
            return {"dateTime": f"{iso[:10]}T00:00:00",
                    "timeZone": cls._local_tz()}
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone()
        return {"dateTime": dt.strftime("%Y-%m-%dT%H:%M:%S"),
                "timeZone": cls._local_tz()}

    @staticmethod
    def _calview_params(start_iso: str, end_iso: str, limit: int) -> str:
        return urllib.parse.urlencode({
            "startDateTime": start_iso,
            "endDateTime": end_iso,
            "$orderby": "start/dateTime",
            "$top": str(limit),
            "$select": ("subject,start,end,location,isAllDay,responseStatus,"
                        "isOnlineMeeting,onlineMeeting"),
        })

    @staticmethod
    def _local_tz() -> str:
        """Best-effort local IANA timezone name for Graph Prefer headers.

        Graph rejects abbreviations ("CEST" → TimeZoneNotSupportedException),
        so only ever return a zoneinfo key (has a "/", or "UTC"). The reliable
        source on Linux is the /etc/localtime symlink into the zoneinfo db —
        datetime.now().astimezone().tzinfo is a fixed-offset timezone, not a
        ZoneInfo, so it can't provide the key."""
        import os

        tz = os.environ.get("TZ", "")
        if "/" in tz:  # a real IANA name, not an abbreviation
            return tz
        try:
            target = os.path.realpath("/etc/localtime")
            if "/zoneinfo/" in target:
                return target.split("/zoneinfo/", 1)[1]
        except OSError:
            pass
        try:
            from zoneinfo import ZoneInfo
            tzinfo = datetime.now().astimezone().tzinfo
            if isinstance(tzinfo, ZoneInfo):
                return tzinfo.key
        except Exception:  # noqa: BLE001
            pass
        return "UTC"  # always valid; times still render correctly, just in UTC

    @classmethod
    def _calendar_headers(cls) -> dict:
        return {"Prefer": f'outlook.timezone="{cls._local_tz()}"'}

    @staticmethod
    def _events_from_json(data: dict) -> list[dict]:
        out = []
        for e in data.get("value", []):
            if not e.get("id"):
                continue  # a malformed item must not sink the whole month
            start = e.get("start", {})
            end = e.get("end", {})
            out.append({
                "id": e["id"],
                "subject": e.get("subject", "(no title)"),
                "start": start.get("dateTime", ""),
                "end": end.get("dateTime", ""),
                "start_tz": start.get("timeZone", ""),
                "end_tz": end.get("timeZone", ""),
                "location": (e.get("location") or {}).get("displayName", ""),
                "all_day": e.get("isAllDay", False),
                # none|organizer|tentativelyAccepted|accepted|declined|notResponded
                # Drives the greyed "unanswered invite" styling in the agenda.
                "response": (e.get("responseStatus") or {}).get("response", ""),
                "online_url": (e.get("onlineMeeting") or {}).get("joinUrl", "")
                if e.get("isOnlineMeeting") else "",
            })
        return out
