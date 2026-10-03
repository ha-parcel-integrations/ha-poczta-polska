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

import logging
from datetime import datetime, timedelta, timezone
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
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
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
        parsed = parsed.replace(tzinfo=ZoneInfo(CARRIER_TIMEZONE))
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
    times = [_carrier_datetime(event.get("time")) for event in usable]
    if any(time is None for time in times):
        return usable
    return [event for _, event in sorted(zip(times, usable), key=lambda pair: pair[0])]


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


def normalize_parcel(raw: dict, *, include_history: bool = False) -> dict:
    """Return a carrier-agnostic parcel dict with the payload under ``raw``.

    The **keys of the returned dict are the contract**: every carrier in the
    suite returns exactly these, in this order. A key is ``None`` when the
    carrier does not expose it — never omitted.

    * ``status`` comes from the newest event's scan code; ``delivered`` from the
      record's own ``finished`` flag, which is meant to agree with it.
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
    # A returned parcel the carrier closes is finished but never reached the
    # recipient; counting it delivered would also stop it from being fetched.
    finished = mail_info.get("finished") is True and status is not ParcelStatus.RETURNING
    delivered = finished or status is ParcelStatus.DELIVERED
    if delivered and status is not ParcelStatus.DELIVERED:
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

    pickup_point = None
    if status is ParcelStatus.AT_PICKUP_POINT:
        office = latest.get("postOffice")
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
        "status": status,
        "raw_status": latest.get("name") or latest_code,
        "delivered": delivered,
        "delivered_at": _carrier_iso(latest.get("time")) if delivered else None,
        "planned_from": None,
        "planned_to": None,
        "pickup": status is ParcelStatus.AT_PICKUP_POINT,
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
    filter_type = options.get(
        CONF_DELIVERED_FILTER_TYPE, DEFAULT_DELIVERED_FILTER_TYPE
    )
    amount = int(
        options.get(CONF_DELIVERED_FILTER_AMOUNT, DEFAULT_DELIVERED_FILTER_AMOUNT)
    )
    if filter_type == "days":
        cutoff = datetime.now(timezone.utc) - timedelta(days=amount)
        return [
            parcel
            for parcel in parcels
            if (parsed := parse_iso(parcel.get("delivered_at"))) is None
            or parsed >= cutoff
        ]
    return parcels[:amount]
