# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Shahab Nedaei
"""Mirror Cloudy events into a local Evolution Data Server (EDS) calendar.

GNOME Shell's top-bar calendar reads from EDS, so to make Cloudy events show
there we keep a dedicated local calendar named "Cloudy" and create/update/remove
VEVENT components in it from the normalized event dicts the views already load.

This is **best-effort and fully guarded**: EDS (libecal/libedataserver via GI)
may be missing or unreachable (e.g. inside a sandbox). Any failure disables the
feature for the session rather than disturbing the app. Honours the
``eds-publish-enabled`` GSetting (off by default).

The mirror is kept in sync per account + month. When a published month is
refetched and an event is missing (deleted server-side, or the month now has no
events), the corresponding VEVENT is removed from EDS so GNOME Calendar does not
show stale data.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

_CAL_NAME = "Cloudy"
_PRODID = "-//Shahab Nedaei//Cloudy//EN"

# Bumped whenever the mirror format changes (timezone handling, UID format,
# source parent, etc.) so stale/wrong events are wiped and rebuilt instead of
# left behind.
_EDS_INDEX_VERSION = 2


def _log(msg: str) -> None:
    print(f"[eds] {msg}")


def _cache_dir() -> Path:
    return Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "cloudy"


def _uid_index_path() -> Path:
    return _cache_dir() / "eds_uids.json"


def _load_uid_index() -> dict:
    path = _uid_index_path()
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("_version") == _EDS_INDEX_VERSION:
                return data
    except (OSError, ValueError):
        pass
    return {"_version": _EDS_INDEX_VERSION}


def _save_uid_index(index: dict) -> None:
    path = _uid_index_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        index["_version"] = _EDS_INDEX_VERSION
        path.write_text(json.dumps(index, indent=2) + "\n", encoding="utf-8")
    except OSError:
        pass


def _reset_eds_calendar(app) -> bool:
    """Delete and recreate the Cloudy calendar source under the right parent.

    Used when the mirror format/version changes so old/wrong events don't linger
    in GNOME Calendar. After a reset the cached ECal client is dropped so the
    next publish reconnects to the fresh source.
    """
    try:
        from .gi_compat import require

        if require("EDataServer", ("1.2", "1.3")) is None or \
                require("ECal", ("2.0", "3.0")) is None:
            return False
        from gi.repository import EDataServer

        registry = EDataServer.SourceRegistry.new_sync(None)
        ext = EDataServer.SOURCE_EXTENSION_CALENDAR
        for source in registry.list_sources(ext):
            if source.get_display_name() == _CAL_NAME:
                try:
                    registry.remove_source_sync(source, None)
                except Exception as exc:  # noqa: BLE001
                    _log(f"could not remove old source: {exc}")
        source = EDataServer.Source.new(None, None)
        # "local" is the canonical parent for on-disk Evolution calendars.
        source.set_parent("local")
        source.set_display_name(_CAL_NAME)
        backend = source.get_extension(ext)
        backend.set_backend_name("local")
        registry.commit_source_sync(source, None)
        # Drop the cached client so the next publish reconnects to the new source.
        app._eds_client = None
        app._eds_disabled = False
        _save_uid_index({})
        _log("calendar reset (format changed); rebuilding from current data")
        return True
    except Exception as exc:  # noqa: BLE001
        _log(f"calendar reset failed: {exc}")
        return False


def _ensure_fresh_calendar(app) -> bool:
    """Reset the calendar once when the stored index version is stale."""
    index = _load_uid_index()
    if index.get("_version") == _EDS_INDEX_VERSION:
        return True
    return _reset_eds_calendar(app)


def _month_of(ev: dict) -> str | None:
    start = ev.get("start", "") or ""
    if len(start) >= 7:
        return start[:7]
    return None


def _parse_naive(value: str) -> datetime | None:
    if not value or "T" not in value:
        return None
    txt = value.strip()
    # Graph returns up to 7 fractional digits; fromisoformat handles them.
    try:
        return datetime.fromisoformat(txt.replace("Z", "+00:00"))
    except ValueError:
        try:
            return datetime.fromisoformat(txt.split(".", 1)[0])
        except ValueError:
            return None


def _utc_datetime(value: str, tz_name: str | None) -> datetime | None:
    """Convert a Graph/Google wall-clock datetime to UTC.

    Graph calendarView returns ``dateTime`` in the requested IANA timezone
    (without a Z suffix). Interpreting it as UTC would shift the event by the
    timezone offset, so we attach the timezone from ``timeZone`` and convert.
    Google returns a UTC ``Z`` suffix, in which case the timezone name is
    ignored. Missing/broken timezone falls back to UTC.
    """
    dt = _parse_naive(value)
    if dt is None:
        return None
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc)
    tz = _resolve_tz(tz_name)
    if tz is None:
        tz = timezone.utc
    return dt.replace(tzinfo=tz).astimezone(timezone.utc)


def _resolve_tz(tz_name: str | None) -> ZoneInfo | timezone | None:
    if not tz_name or tz_name == "UTC":
        return timezone.utc
    try:
        return ZoneInfo(tz_name)
    except Exception:  # noqa: BLE001
        pass
    # Windows-style "Romance Standard Time" etc. are not handled here; EDS
    # itself won't accept them either. Fall back to UTC to avoid crashing.
    return None


def _esc(text: str) -> str:
    return (text or "").replace("\\", "\\\\").replace(";", "\\;") \
        .replace(",", "\\,").replace("\n", "\\n")


def _date_only(value: str) -> str | None:
    if value and len(value) >= 10:
        return value[:10].replace("-", "")
    return None


def _vevent(uid: str, ev: dict) -> str | None:
    """Build a VEVENT block for one normalized event, or None if untimed/bad."""
    summary = ev.get("subject") or "(no title)"
    if ev.get("all_day"):
        start = _date_only(ev.get("start", ""))
        end = _date_only(ev.get("end", ""))
        if start is None:
            return None
        if end is None:
            end = start
        dt_lines = f"DTSTART;VALUE=DATE:{start}\r\nDTEND;VALUE=DATE:{end}"
    else:
        start = _utc_datetime(ev.get("start", ""), ev.get("start_tz"))
        end = _utc_datetime(ev.get("end", ""), ev.get("end_tz"))
        if start is None:
            return None
        if end is None:
            end = start + timedelta(hours=1)
        dt_lines = (f"DTSTART:{start.strftime('%Y%m%dT%H%M%SZ')}\r\n"
                    f"DTEND:{end.strftime('%Y%m%dT%H%M%SZ')}")
    lines = [
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"SUMMARY:{_esc(summary)}",
        dt_lines,
    ]
    if ev.get("location"):
        lines.append(f"LOCATION:{_esc(ev['location'])}")
    lines.append("END:VEVENT")
    return "\r\n".join(lines)


def _event_uid(account, ev: dict) -> str:
    return f"cloudy-{account.id}-{ev.get('id', '')}@cloudy"


def publish_events(app, account, events, month: str | None = None) -> None:
    """Sync the given events into the local 'Cloudy' EDS calendar.

    ``month`` is the ``YYYY-MM`` these events belong to (used to scope deletions
    so switching months doesn't wipe other months). When omitted it is inferred
    from the first event; callers should pass it explicitly.

    Safe to call from a worker thread (EDS sync calls block). No-ops when the
    setting is off or EDS is unavailable; never raises.
    """
    try:
        if not app.settings.get_boolean("eds-publish-enabled"):
            return
    except Exception:  # noqa: BLE001
        return
    if getattr(app, "_eds_disabled", False):
        return
    if not _ensure_fresh_calendar(app):
        return
    client = _get_client(app)
    if client is None:
        return

    from .gi_compat import require

    if require("ECal", ("2.0", "3.0")) is None or \
            require("ICalGLib", ("3.0", "4.0")) is None:
        return  # EDS not available on this runtime
    from gi.repository import ECal, ICalGLib

    # Normalize input and determine the month bucket.
    events = events or []
    if month is None and events:
        month = _month_of(events[0])
    if month is None:
        month = "unknown"

    desired: dict[str, dict] = {}
    for ev in events:
        uid = _event_uid(account, ev)
        block = _vevent(uid, ev)
        if block is None:
            continue
        desired[uid] = block

    index = _load_uid_index()
    bucket = f"{account.id}:{month}"
    previous = set(index.get(bucket, []))
    current = set(desired.keys())

    # Remove events that disappeared from this month.
    for uid in previous - current:
        try:
            client.remove_object_sync(
                uid, None, ECal.ObjModType.ALL, ECal.OperationFlags.NONE, None)
        except Exception:  # noqa: BLE001 - may not exist
            pass

    # Create or update the rest.
    for uid, block in desired.items():
        try:
            icomp = ICalGLib.Component.new_from_string(block)
            if icomp is None:
                continue
            existing = None
            try:
                existing = client.get_object_sync(uid, None, None)
            except Exception:  # noqa: BLE001 - not present yet
                existing = None
            if existing is not None:
                client.modify_object_sync(
                    icomp, ECal.ObjModType.ALL, ECal.OperationFlags.NONE, None)
            else:
                client.create_object_sync(icomp, ECal.OperationFlags.NONE, None)
        except Exception as exc:  # noqa: BLE001 - one bad event shouldn't abort
            _log(f"skip event: {exc}")

    index[bucket] = list(current)
    _save_uid_index(index)


def remove_account_events(app, account_id: str) -> None:
    """Best-effort remove every EDS event previously published for ``account_id``.

    Called when an account is removed/signed out so stale events don't linger in
    GNOME Calendar.
    """
    if getattr(app, "_eds_disabled", False):
        return
    client = _get_client(app)
    if client is None:
        return
    from .gi_compat import require
    if require("ECal", ("2.0", "3.0")) is None:
        return
    from gi.repository import ECal

    index = _load_uid_index()
    prefix = f"{account_id}:"
    buckets = [k for k in list(index.keys()) if k.startswith(prefix)]
    removed_any = False
    for bucket in buckets:
        for uid in index.pop(bucket, []):
            try:
                client.remove_object_sync(
                    uid, None, ECal.ObjModType.ALL, ECal.OperationFlags.NONE, None)
                removed_any = True
            except Exception:  # noqa: BLE001
                pass
    if removed_any or buckets:
        _save_uid_index(index)
    _eds_publish_throttle.pop(account_id, None)


def remove_account_events_async(app, account_id: str) -> None:
    """Off-thread variant of ``remove_account_events`` (EDS calls can block)."""
    import threading

    def work():
        try:
            remove_account_events(app, account_id)
        except Exception:  # noqa: BLE001 - EDS cleanup is best-effort
            pass

    threading.Thread(target=work, daemon=True).start()


def clear_all_events(app) -> None:
    """Remove every event from the Cloudy EDS calendar and reset the index.

    Called when the user disables the EDS mirror so GNOME Calendar doesn't keep
    showing stale Cloudy events.
    """
    if not getattr(app, "_eds_disabled", False):
        client = _get_client(app)
        if client is not None:
            from .gi_compat import require
            if require("ECal", ("2.0", "3.0")) is not None:
                from gi.repository import ECal
                index = _load_uid_index()
                for bucket, uids in list(index.items()):
                    if bucket.startswith("_"):
                        continue
                    for uid in uids:
                        try:
                            client.remove_object_sync(
                                uid, None, ECal.ObjModType.ALL,
                                ECal.OperationFlags.NONE, None)
                        except Exception:  # noqa: BLE001
                            pass
    _save_uid_index({})
    _eds_publish_throttle.clear()


def clear_all_events_async(app) -> None:
    """Off-thread variant of ``clear_all_events`` (EDS calls can block)."""
    import threading

    def work():
        try:
            clear_all_events(app)
        except Exception:  # noqa: BLE001 - EDS cleanup is best-effort
            pass

    threading.Thread(target=work, daemon=True).start()


def publish_all_cached_events(app) -> None:
    """Backfill EDS from whatever events are already cached.

    Called when the user turns on the EDS setting, so GNOME Calendar updates
    immediately without waiting for a fresh fetch.
    """
    if getattr(app, "_eds_disabled", False):
        return
    if not _ensure_fresh_calendar(app):
        return
    cache = getattr(app, "cache", None)
    if cache is None:
        return
    registry = getattr(app, "registry", None)
    if registry is None:
        return

    for account in registry.accounts():
        # Only mirror the user's own calendar, matching the view's rule.
        for key, events in cache.items():
            if not key.startswith(f"{account.id}:events:me:"):
                continue
            month = key.split(":")[-1]
            if len(month) == 7:  # YYYY-MM
                try:
                    publish_events(app, account, events, month=month)
                except Exception:  # noqa: BLE001 - EDS mirroring never affects UI
                    pass


# Throttle background full-month EDS sync so a busy notification poll doesn't
# hammer the calendar API. Keyed by account id; value is monotonic timestamp.
_eds_publish_throttle: dict[str, float] = {}
_EDS_BACKGROUND_INTERVAL_S = 300  # 5 minutes


def publish_account_current_month_async(app, account) -> None:
    """Fetch this account's own calendar for the current month and mirror it.

    Used by the background notifier so GNOME Calendar stays up to date even
    when the Calendar view is not visible. Throttled to avoid polling the API
    too aggressively.
    """
    import threading

    if getattr(app, "_eds_disabled", False):
        return
    try:
        if not app.settings.get_boolean("eds-publish-enabled"):
            return
    except Exception:  # noqa: BLE001
        return

    now = datetime.now(timezone.utc)
    last = _eds_publish_throttle.get(account.id, 0)
    if now.timestamp() - last < _EDS_BACKGROUND_INTERVAL_S:
        return
    _eds_publish_throttle[account.id] = now.timestamp()

    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    next_month = (month_start + timedelta(days=32)).replace(day=1)
    start_iso = month_start.strftime("%Y-%m-%dT%H:%M:%SZ")
    end_iso = next_month.strftime("%Y-%m-%dT%H:%M:%SZ")
    month = month_start.strftime("%Y-%m")

    def work():
        try:
            from ..widgets.clients import build_account_client

            client = build_account_client(app, account)
            events = client.list_events(start_iso, end_iso)
            publish_events(app, account, events, month=month)
        except Exception:  # noqa: BLE001 - EDS mirroring never affects UI
            pass

    threading.Thread(target=work, daemon=True).start()


def _get_client(app):
    """Lazily build (and cache) the ECal client for the Cloudy calendar."""
    client = getattr(app, "_eds_client", None)
    if client is not None:
        return client
    try:
        from .gi_compat import require

        if require("EDataServer", ("1.2", "1.3")) is None or \
                require("ECal", ("2.0", "3.0")) is None:
            raise RuntimeError("EDS namespaces unavailable")
        from gi.repository import EDataServer, ECal

        registry = EDataServer.SourceRegistry.new_sync(None)
        source = _find_or_create_source(EDataServer, registry)
        client = ECal.Client.connect_sync(
            source, ECal.ClientSourceType.EVENTS, 30, None)
        app._eds_client = client
        return client
    except Exception as exc:  # noqa: BLE001 - disable for the session
        _log(f"unavailable, disabling: {exc}")
        app._eds_disabled = True
        return None


def _find_or_create_source(EDataServer, registry):
    ext = EDataServer.SOURCE_EXTENSION_CALENDAR
    for source in registry.list_sources(ext):
        if source.get_display_name() == _CAL_NAME:
            return source
    source = EDataServer.Source.new(None, None)
    # "local" is the canonical parent for on-disk Evolution calendars.
    # "local-stub" works in some versions but is not reliably shown by GNOME
    # Calendar / Shell, so prefer the documented parent UID.
    source.set_parent("local")
    source.set_display_name(_CAL_NAME)
    backend = source.get_extension(ext)
    backend.set_backend_name("local")
    registry.commit_source_sync(source, None)
    return source
