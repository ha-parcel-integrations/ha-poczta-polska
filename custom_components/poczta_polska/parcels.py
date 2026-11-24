"""Canonical parcel shape, status mapping and list helpers.

Everything in this module is a **pure function** — no I/O, no Home Assistant
objects beyond the config entry's options. That is deliberate: it keeps the
carrier-specific mapping (which you rewrite per carrier) apart from the
coordinator (which is nearly identical everywhere), and it makes the mapping
trivially unit-testable without spinning up HA.

Two things here are carrier-specific: :data:`_STATUS_MAP` and
:func:`normalize_parcel`. Everything else — the
timestamp parsing, the history builder, the sort contract, the delivered
filter, the one-shot warning for unmapped statuses — is suite-wide machinery
and should be left alone.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, date, datetime, time, timedelta, tzinfo
import logging
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

from homeassistant.config_entries import ConfigEntry

from .const import (
    CARRIER_TIMEZONE,
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    DEFAULT_DELIVERED_FILTER_AMOUNT,
    DEFAULT_DELIVERED_FILTER_TYPE,
    HISTORY_MAX_EVENTS,
    TRACKING_PAGE_URL,
    ParcelStatus,
)

_LOGGER = logging.getLogger(__name__)

# Built once at import: constructing a ZoneInfo reads the tz file from disk.
_CARRIER_TZ = ZoneInfo(CARRIER_TIMEZONE)

# Where users report a status we do not map yet. Rewritten by the bootstrap
# script; it must point at the carrier's own repo so the log line is
# copy-pasteable straight into a new issue.
#
# The ``?template=`` parameter matters: without it the link opens a blank form,
# and the report comes back missing the version and the log line we need.
NEW_ISSUE_URL = (
    "https://github.com/ha-parcel-integrations/ha-poczta-polska/issues/new"
    "?template=unrecognised_status.yml"
)

# Keyed on the scan code of an event, never on the coarser state label: after a
# failed delivery the state can stay "in delivery" while the scan code already
# says otherwise.
#
# A bare customs scan is in transit, as elsewhere in the suite. P_ZWOLDDOR is
# left out on purpose: never seen on a real parcel, so it reports ``unknown``
# with a one-shot warning.
_STATUS_MAP: dict[str, ParcelStatus] = {
    "P_REJ_KN1": ParcelStatus.REGISTERED,
    "P_PPDN": ParcelStatus.IN_TRANSIT,
    "P_NAD": ParcelStatus.IN_TRANSIT,
    "P_PZL": ParcelStatus.IN_TRANSIT,
    "P_WZL": ParcelStatus.IN_TRANSIT,
    "P_WEOC": ParcelStatus.IN_TRANSIT,
    "P_WYPL": ParcelStatus.IN_TRANSIT,
    "P_WYOC": ParcelStatus.IN_TRANSIT,
    "P_WEPL": ParcelStatus.IN_TRANSIT,
    "P_WPUCPP": ParcelStatus.IN_TRANSIT,
    "P_ZWC": ParcelStatus.IN_TRANSIT,
    "P_WD": ParcelStatus.OUT_FOR_DELIVERY,
    "P_WDML": ParcelStatus.OUT_FOR_DELIVERY,
    "P_NDD": ParcelStatus.OUT_FOR_DELIVERY,
    "P_A": ParcelStatus.AT_PICKUP_POINT,
    "P_KWD": ParcelStatus.AT_PICKUP_POINT,
    "P_PA": ParcelStatus.AT_PICKUP_POINT,
    "P_D": ParcelStatus.DELIVERED,
    "P_OWU": ParcelStatus.DELIVERED,
    "P_NDZ": ParcelStatus.RETURNING,
    "P_ND": ParcelStatus.PROBLEM,
    "P_NDZK": ParcelStatus.PROBLEM,
    "P_NDPD": ParcelStatus.PROBLEM,
    "P_R": ParcelStatus.PROBLEM,
    "P_NDZAP": ParcelStatus.PROBLEM,
    "P_NDZKON": ParcelStatus.PROBLEM,
    "P_ZDUN": ParcelStatus.PROBLEM,
    "P_CZDKN": ParcelStatus.PROBLEM,
    "P_NDPJ": ParcelStatus.PROBLEM,
}

# Status codes we have already warned about, so each unmapped one is logged
# only once per HA session instead of on every poll.
_unmapped_statuses_logged: set[str] = set()


def _warn_unmapped_status(code: str) -> None:
    """Log an unmapped carrier status once, with a copy-paste issue link."""
    if code in _unmapped_statuses_logged:
        return
    _unmapped_statuses_logged.add(code)
    _LOGGER.warning(
        "Unrecognised Poczta Polska status — help us map it. Open an issue "
        "and paste this line: %s\n  status=%s → reported as 'unknown'",
        NEW_ISSUE_URL,
        code,
    )


def map_parcel_status(code: str | None) -> ParcelStatus:
    """Map a carrier status code to a canonical :class:`ParcelStatus`.

    ``None`` (a not-yet-scanned parcel) reports ``unknown`` silently; an
    unrecognised code reports ``unknown`` with a one-shot warning.
    """
    if not code:
        return ParcelStatus.UNKNOWN
    mapped = _STATUS_MAP.get(code)
    if mapped is not None:
        return mapped
    _warn_unmapped_status(code)
    return ParcelStatus.UNKNOWN


def map_event_status(code: str | None) -> ParcelStatus | None:
    """Map a history entry's status code to a canonical status, or ``None``.

    Unmapped codes keep ``status: null`` on the history entry (rather than
    ``unknown``, so a consumer can tell "no mapping" from "mapped to unknown")
    and warn once, reusing the parcel-status one-shot set.
    """
    if not code:
        return None
    mapped = _STATUS_MAP.get(code)
    if mapped is not None:
        return mapped
    _warn_unmapped_status(code)
    return None


def parse_iso(value: str | None) -> datetime | None:
    """Parse an ISO 8601 string to an aware datetime, or ``None`` on failure.

    Naive values are treated as UTC so a list always sorts without crashing on
    a mixed set.
    """
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _carrier_datetime(value: Any) -> datetime | None:
    """Return an offset-aware datetime for a carrier event time.

    The wire format has no UTC offset; it is Polish local time, so a naive
    value is attached to that zone rather than read as UTC. A value that does
    not parse is warned about once and dropped: a non-ISO string in a
    canonical timestamp would never sort or age out.
    """
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        _warn_once_timestamp()
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_CARRIER_TZ)
    return parsed


def _carrier_iso(value: Any) -> str | None:
    """Return an offset-aware ISO string for a carrier event time."""
    parsed = _carrier_datetime(value)
    return parsed.isoformat() if parsed else None


# One-shot keys for shape surprises other than an unmapped status.
_shape_warnings_logged: set[str] = set()


def _warn_once_timestamp() -> None:
    """Warn once that an event time was not in the expected format."""
    if "timestamp" in _shape_warnings_logged:
        return
    _shape_warnings_logged.add("timestamp")
    _LOGGER.warning(
        "Poczta Polska sent an event time that is not ISO 8601; ignoring "
        "it. Please report it: %s",
        NEW_ISSUE_URL,
    )


def usable_events(raw: dict) -> list[dict]:
    """Return the non-cancelled events, oldest first.

    Sorted rather than trusted in wire order, so status and history can never
    disagree about which scan is newest. With any untimed event there is no
    sound order to sort into, so the wire order stands.
    """
    mail_info = raw.get("mailInfo")
    events = mail_info.get("events") if isinstance(mail_info, dict) else None
    usable = [
        event
        for event in events or []
        if isinstance(event, dict) and not event.get("canceled")
    ]
    timed: list[tuple[datetime, dict]] = []
    for event in usable:
        moment = _carrier_datetime(event.get("time"))
        if moment is None:
            return usable
        timed.append((moment, event))
    return [event for _, event in sorted(timed, key=lambda pair: pair[0])]


def build_history(
    events: list | None, *, max_events: int = HISTORY_MAX_EVENTS
) -> list[dict]:
    """Build the canonical ``history`` list from the carrier's event list.

    Each entry is ``{timestamp, status, raw_status}`` — identical across all
    suite carriers, and top-level (not under ``raw``) so it survives the
    aggregator's ``strip_raw()``. ``raw_status`` is the carrier's own event
    text, or its event code when the text is missing. Sorted oldest → newest
    and capped to the most recent ``max_events``.
    """
    timed: list[tuple[datetime, dict]] = []
    for event in events or []:
        if not isinstance(event, dict):
            continue
        parsed = _carrier_datetime(event.get("time"))
        if parsed is None:
            continue
        timed.append(
            (
                parsed,
                {
                    "timestamp": parsed.isoformat(),
                    "status": map_event_status(event.get("code")),
                    "raw_status": event.get("name") or event.get("code"),
                },
            )
        )
    timed.sort(key=lambda item: item[0])
    return [entry for _, entry in timed][-max_events:]


def _moment(value: Any, zone: tzinfo) -> tuple[datetime, bool, str] | None:
    """Return ``(aware datetime, date_only, text)`` for a carrier time, or ``None``.

    A naive time or a bare date gets ``zone``; an aware time keeps its offset
    and, when it came in as a string, its own spelling.
    """
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        moment = datetime.combine(value, time.min, tzinfo=zone)
        return moment, True, moment.isoformat()
    elif isinstance(value, str) and value:
        try:
            if len(value) == 10:
                moment = datetime.combine(
                    date.fromisoformat(value), time.min, tzinfo=zone
                )
                return moment, True, moment.isoformat()
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        if parsed.tzinfo:
            return parsed, False, value
    else:
        return None
    moment = parsed if parsed.tzinfo else parsed.replace(tzinfo=zone)
    return moment, False, moment.isoformat()


def _end_of_day(moment: datetime, zone: tzinfo) -> datetime:
    local = moment.astimezone(zone)
    return datetime.combine(local.date(), time(23, 59, 59), tzinfo=zone)


def planned_window(
    start: Any, end: Any, *, zone: tzinfo
) -> tuple[str | None, str | None]:
    """Return ``(planned_from, planned_to)`` in the canonical window shape.

    ``start`` and ``end`` are ISO strings, dates or datetimes, or ``None``; an
    epoch number must be converted first (see ``to_iso_timestamp``). A window
    keeps both ends. One moment with a time ends at ``23:59:59`` the same local
    day; a date covers ``00:00:00`` to ``23:59:59``; two dates run from the
    first day's start to the last day's end. Equal values are one moment,
    reversed values are swapped, and ``planned_to`` is never set without
    ``planned_from``. ``zone`` is the carrier's own zone: it dates naive times
    and bare dates and decides which day a moment ends on, so a ``Z`` moment
    still ends on the carrier's local day. When nothing is left of that day
    after the moment, there is no ``planned_to``.
    """
    first = _moment(start, zone)
    last = _moment(end, zone)
    if first is None:
        first, last = last, None
    if first is None:
        return None, None

    def closing(moment: tuple[datetime, bool, str]) -> datetime:
        return _end_of_day(moment[0], zone) if moment[1] else moment[0]

    if last is not None and closing(last) < first[0]:
        first, last = last, first
    if last is not None and closing(last) == first[0]:
        last = None

    if last is None:
        end_of_day = _end_of_day(first[0], zone)
        return first[2], end_of_day.isoformat() if end_of_day > first[0] else None
    return first[2], closing(last).isoformat() if last[1] else last[2]


_NO_WINDOW_STATUSES = frozenset(
    {ParcelStatus.AT_PICKUP_POINT, ParcelStatus.RETURNING, ParcelStatus.DELIVERED}
)


def planned_for_status(
    status: ParcelStatus, planned_from: str | None, planned_to: str | None
) -> tuple[str | None, str | None]:
    """Empty the delivery window once the parcel is at its destination or done."""
    if status in _NO_WINDOW_STATUSES:
        return None, None
    return planned_from, planned_to


def is_pickup(
    route_flag: bool | None,
    status: ParcelStatus,
    event_statuses: Iterable[ParcelStatus | None] = (),
) -> bool:
    """Whether the delivery route ends at a pickup point.

    The carrier's own route flag wins; otherwise the parcel having reached
    ``AT_PICKUP_POINT`` at any point counts, so the value survives collection.
    """
    if route_flag:
        return True
    return status is ParcelStatus.AT_PICKUP_POINT or any(
        event is ParcelStatus.AT_PICKUP_POINT for event in event_statuses
    )


def normalize_parcel(raw: dict, *, include_history: bool = False) -> dict:
    """Return a carrier-agnostic parcel dict with the payload under ``raw``.

    The **keys of the returned dict are the contract**: every carrier in the
    suite returns exactly these, in this order. A key is ``None`` when the
    carrier does not expose it — never omitted.

    * ``status`` comes from the newest event's scan code, with the record's own
      ``finished`` flag folded in; ``delivered`` is ``status is DELIVERED``.
    * ``weight`` is kilograms.
    * ``history`` is ``None`` when the option is off — the key still exists.
    * ``raw`` is the full record, untouched. Privacy lives in diagnostics.
    """
    mail_info = raw.get("mailInfo")
    if not isinstance(mail_info, dict):
        mail_info = {}
    events = usable_events(raw)
    latest = events[-1] if events else {}
    latest_code = latest.get("code")

    status = map_parcel_status(latest_code)
    # The carrier's closed flag is part of the status: a closed parcel whose
    # newest scan is not a delivery is reported delivered, a collected
    # pickup-point parcel included. A return stays returning: nothing in the
    # payload shows it reached the sender.
    if mail_info.get("finished") is True and status not in (
        ParcelStatus.DELIVERED,
        ParcelStatus.RETURNING,
    ):
        key = f"finished:{latest_code}"
        if key not in _shape_warnings_logged:
            _shape_warnings_logged.add(key)
            _LOGGER.warning(
                "Poczta Polska marks a parcel finished although its newest "
                "scan is not a delivery; reported as delivered. Please "
                "report it: %s\n  code=%s → status=%s",
                NEW_ISSUE_URL,
                latest_code,
                status.value,
            )
        status = ParcelStatus.DELIVERED
    delivered = status is ParcelStatus.DELIVERED

    delivered_event = next(
        (
            event
            for event in reversed(events)
            if map_event_status(event.get("code")) is ParcelStatus.DELIVERED
        ),
        {},
    )
    planned_from, planned_to = planned_for_status(
        status, *planned_window(None, None, zone=_CARRIER_TZ)
    )

    event_statuses = [map_event_status(event.get("code")) for event in events]
    pickup = is_pickup(None, status, event_statuses)
    pickup_point = None
    if pickup:
        office = next(
            (
                event.get("postOffice")
                for event in reversed(events)
                if map_event_status(event.get("code")) is ParcelStatus.AT_PICKUP_POINT
            ),
            None,
        )
        if isinstance(office, dict):
            pickup_point = office.get("name") or None

    barcode = raw.get("number")
    url = f"{TRACKING_PAGE_URL}?{quote(barcode)}" if barcode else None

    weight = mail_info.get("weight")
    if isinstance(weight, bool) or not isinstance(weight, (int, float)) or weight <= 0:
        weight = None

    return {
        "carrier": "Poczta Polska",
        "barcode": barcode,
        "sender": None,
        "receiver": None,
        "contents": None,
        "status": status,
        "raw_status": latest.get("name") or latest_code,
        "delivered": delivered,
        "delivered_at": (
            _carrier_iso(delivered_event.get("time")) if delivered else None
        ),
        "planned_from": planned_from,
        "planned_to": planned_to,
        "pickup": pickup,
        "pickup_point": pickup_point,
        "url": url,
        "weight": weight,
        "dimensions": None,
        "history": build_history(events) if include_history else None,
        "raw": raw,
    }


def sort_parcels_by_ts(
    parcels: list[dict], key_field: str, *, descending: bool = False
) -> list[dict]:
    """Return normalised parcels sorted by the ISO timestamp at ``key_field``.

    The suite's sort contract: incoming/outgoing ascending on ``planned_from``,
    delivered descending on ``delivered_at``. Parcels whose value is missing or
    unparseable always sort to the end, regardless of ``descending``.
    """
    with_ts: list[tuple[datetime, dict]] = []
    without_ts: list[dict] = []
    for parcel in parcels:
        parsed = parse_iso(parcel.get(key_field))
        if parsed is None:
            without_ts.append(parcel)
        else:
            with_ts.append((parsed, parcel))
    with_ts.sort(key=lambda item: item[0], reverse=descending)
    return [parcel for _, parcel in with_ts] + without_ts


def apply_delivered_filter(parcels: list[dict], entry: ConfigEntry) -> list[dict]:
    """Trim the delivered list per the entry's retention option.

    ``parcels`` must already be sorted newest-first. ``days`` keeps deliveries
    from the last N days (an unparseable ``delivered_at`` is kept rather than
    silently dropped); the ``parcels`` type keeps the N most recent. Parcels
    stay *tracked* either way — this only controls what the delivered sensor
    shows.
    """
    options = entry.options
    filter_type = options.get(CONF_DELIVERED_FILTER_TYPE, DEFAULT_DELIVERED_FILTER_TYPE)
    amount = int(
        options.get(CONF_DELIVERED_FILTER_AMOUNT, DEFAULT_DELIVERED_FILTER_AMOUNT)
    )
    if filter_type == "days":
        cutoff = datetime.now(UTC) - timedelta(days=amount)
        return [
            parcel
            for parcel in parcels
            if (parsed := parse_iso(parcel.get("delivered_at"))) is None
            or parsed >= cutoff
        ]
    return parcels[:amount]
