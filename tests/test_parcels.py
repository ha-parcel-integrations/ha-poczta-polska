"""Tests for the pure parcel-mapping helpers.

These need no Home Assistant instance — the whole point of keeping
``parcels.py`` free of I/O is that the carrier-specific mapping (the part you
rewrite per carrier) can be tested as plain functions.
"""
from datetime import datetime, timedelta, timezone

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.poczta_polska.const import (
    CAPABILITIES,
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    DOMAIN,
    KNOWN_CAPABILITIES,
    PENDING_CAPABILITIES,
    ParcelStatus,
)
from custom_components.poczta_polska.parcels import (
    apply_delivered_filter,
    build_history,
    map_event_status,
    map_parcel_status,
    normalize_parcel,
    parse_iso,
    sort_parcels_by_ts,
)

from .payloads import (
    ACTIVE_CODE,
    DELIVERED_CODE,
    active_sample,
    delivered_sample,
    event,
    pickup_sample,
    record,
)

# ---------------------------------------------------------------------------
# map_parcel_status / map_event_status
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "code,expected",
    [
        ("P_REJ_KN1", ParcelStatus.REGISTERED),
        ("P_PPDN", ParcelStatus.IN_TRANSIT),
        ("P_NAD", ParcelStatus.IN_TRANSIT),
        ("P_PZL", ParcelStatus.IN_TRANSIT),
        ("P_WZL", ParcelStatus.IN_TRANSIT),
        ("P_WEOC", ParcelStatus.IN_TRANSIT),
        ("P_WYPL", ParcelStatus.IN_TRANSIT),
        ("P_WYOC", ParcelStatus.IN_TRANSIT),
        ("P_WEPL", ParcelStatus.IN_TRANSIT),
        ("P_WPUCPP", ParcelStatus.IN_TRANSIT),
        ("P_ZWC", ParcelStatus.IN_TRANSIT),
        ("P_WD", ParcelStatus.OUT_FOR_DELIVERY),
        ("P_WDML", ParcelStatus.OUT_FOR_DELIVERY),
        ("P_NDD", ParcelStatus.OUT_FOR_DELIVERY),
        ("P_A", ParcelStatus.AT_PICKUP_POINT),
        ("P_KWD", ParcelStatus.AT_PICKUP_POINT),
        ("P_PA", ParcelStatus.AT_PICKUP_POINT),
        ("P_D", ParcelStatus.DELIVERED),
        ("P_OWU", ParcelStatus.DELIVERED),
        ("P_NDZ", ParcelStatus.RETURNING),
        ("P_ND", ParcelStatus.PROBLEM),
        ("P_NDZK", ParcelStatus.PROBLEM),
        ("P_NDPD", ParcelStatus.PROBLEM),
        ("P_R", ParcelStatus.PROBLEM),
        ("P_NDZAP", ParcelStatus.PROBLEM),
        ("P_NDZKON", ParcelStatus.PROBLEM),
        ("P_ZDUN", ParcelStatus.PROBLEM),
        ("P_CZDKN", ParcelStatus.PROBLEM),
        ("P_NDPJ", ParcelStatus.PROBLEM),
    ],
)
def test_map_parcel_status_known(code, expected):
    assert map_parcel_status(code) == expected


@pytest.mark.parametrize("code", ["P_ZWOLDDOR", "P_BRAND_NEW"])
def test_unlisted_codes_are_unknown_and_warn(code, caplog):
    """Unsettled scans must stay visible as unknown, not be guessed into a band."""
    with caplog.at_level("WARNING"):
        assert map_parcel_status(code) == ParcelStatus.UNKNOWN
    assert code in caplog.text


def test_map_parcel_status_missing_is_unknown():
    assert map_parcel_status(None) == ParcelStatus.UNKNOWN
    assert map_parcel_status("") == ParcelStatus.UNKNOWN


def test_map_parcel_status_unmapped_is_unknown():
    assert map_parcel_status("TELEPORTED") == ParcelStatus.UNKNOWN


def test_map_event_status_missing_and_unmapped_are_none():
    """History keeps ``null`` rather than ``unknown`` so consumers can tell
    "no mapping" from "mapped to unknown"."""
    assert map_event_status(None) is None
    assert map_event_status("SOMETHING_NEW") is None
    assert map_event_status("P_D") == ParcelStatus.DELIVERED


def test_unmapped_status_warns_only_once(caplog):
    assert map_parcel_status("ABDUCTED") == ParcelStatus.UNKNOWN
    assert map_parcel_status("ABDUCTED") == ParcelStatus.UNKNOWN
    assert caplog.text.count("ABDUCTED") == 1
    assert "issues/new" in caplog.text


# ---------------------------------------------------------------------------
# timestamp helpers
# ---------------------------------------------------------------------------


def test_parse_iso_handles_z_naive_and_garbage():
    assert parse_iso("2026-04-29T13:12:42Z").tzinfo is not None
    # A naive value is assumed UTC so mixed lists still sort.
    assert parse_iso("2026-04-29T13:12:42").tzinfo == timezone.utc
    assert parse_iso("not-a-date") is None
    assert parse_iso(None) is None


def test_event_times_are_read_as_polish_local_time():
    """The wire format has no offset; summer time is UTC+2, winter UTC+1."""
    summer = build_history([event("P_NAD", "2026-07-01T12:00:00")])
    winter = build_history([event("P_NAD", "2026-01-15T12:00:00")])
    assert parse_iso(summer[0]["timestamp"]) == datetime(2026, 7, 1, 10, tzinfo=timezone.utc)
    assert parse_iso(winter[0]["timestamp"]) == datetime(2026, 1, 15, 11, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# build_history
# ---------------------------------------------------------------------------


def test_build_history_keeps_wire_order_newest_last():
    history = build_history(delivered_sample()["mailInfo"]["events"])
    assert len(history) == 5
    assert history[0]["raw_status"] == "Electronic item data received"
    assert history[0]["status"] == ParcelStatus.REGISTERED
    assert history[-1]["status"] == ParcelStatus.DELIVERED


def test_build_history_caps_to_max_events_keeping_the_newest():
    events = [event("P_PZL", f"2026-04-{day:02d}T10:00:00") for day in range(1, 26)]
    history = build_history(events, max_events=20)
    assert len(history) == 20
    assert history[-1]["timestamp"].startswith("2026-04-25")


def test_build_history_handles_missing_and_malformed():
    assert build_history(None) == []
    assert build_history([{"code": "P_PZL"}]) == []  # no timestamp
    assert build_history(["not-a-dict"]) == []


def test_build_history_drops_unparseable_timestamp_and_warns_once(caplog):
    history = build_history(
        [
            event("P_REJ_KN1", "2026-04-24T10:00:00"),
            event("P_PZL", "not-a-date"),
            event("P_PZL", "also-not-a-date"),
        ]
    )
    assert len(history) == 1
    assert history[0]["timestamp"].startswith("2026-04-24")
    assert caplog.text.count("not ISO 8601") == 1


def test_unparseable_delivery_time_leaves_delivered_at_empty():
    raw = record(
        DELIVERED_CODE,
        [event("P_WD", "2026-04-29T08:00:00"), event("P_D", "not-a-date", finished=True)],
        finished=True,
    )
    parcel = normalize_parcel(raw)
    assert parcel["delivered"] is True
    assert parcel["delivered_at"] is None


def test_status_follows_the_newest_scan_even_out_of_wire_order():
    raw = record(
        DELIVERED_CODE,
        [event("P_D", "2026-04-29T13:00:00", finished=True), event("P_WD", "2026-04-29T08:00:00")],
        finished=True,
    )
    parcel = normalize_parcel(raw)
    assert parcel["status"] == ParcelStatus.DELIVERED
    assert parcel["delivered_at"] == "2026-04-29T13:00:00+02:00"


def test_a_finished_return_is_not_delivered():
    raw = record(
        DELIVERED_CODE,
        [event("P_WD", "2026-04-29T08:00:00"), event("P_NDZ", "2026-04-29T15:00:00")],
        finished=True,
    )
    parcel = normalize_parcel(raw)
    assert parcel["status"] == ParcelStatus.RETURNING
    assert parcel["delivered"] is False
    assert parcel["delivered_at"] is None


def test_build_history_falls_back_to_code_without_text():
    scan = event("P_PZL", "2026-04-24T10:00:00")
    scan["name"] = ""
    assert build_history([scan])[0]["raw_status"] == "P_PZL"


# ---------------------------------------------------------------------------
# normalize_parcel — the canonical contract
# ---------------------------------------------------------------------------

CANONICAL_KEYS = [
    "carrier",
    "barcode",
    "sender",
    "receiver",
    "status",
    "raw_status",
    "delivered",
    "delivered_at",
    "planned_from",
    "planned_to",
    "pickup",
    "pickup_point",
    "url",
    "weight",
    "dimensions",
    "history",
    "raw",
]


def test_normalize_publishes_exactly_the_canonical_keys():
    """The aggregator and cross-carrier dashboards depend on this key set."""
    assert list(normalize_parcel(delivered_sample())) == CANONICAL_KEYS


def test_capabilities_are_known_values():
    """A typo here would silently misreport this carrier on the docs site."""
    assert CAPABILITIES <= KNOWN_CAPABILITIES
    assert PENDING_CAPABILITIES <= KNOWN_CAPABILITIES


def test_a_capability_is_never_both_populated_and_pending():
    """The docs site would have to pick one; "awaiting data" must not hide a confirmed field."""
    assert not CAPABILITIES & PENDING_CAPABILITIES


def test_capabilities_match_what_normalize_parcel_actually_returns():
    """Every declared CAPABILITIES entry must come true somewhere in a sample.

    Copy this test into a real carrier's own test_parcels.py verbatim — it
    stays correct for whatever subset of CAPABILITIES that carrier declares.
    """
    delivered = normalize_parcel(delivered_sample())
    active = normalize_parcel(active_sample())
    pickup = normalize_parcel(pickup_sample())
    with_history = normalize_parcel(delivered_sample(), include_history=True)

    if "weight" in CAPABILITIES:
        assert delivered["weight"] is not None
    if "dimensions" in CAPABILITIES:
        assert delivered["dimensions"] is not None
    if "delivery_window" in CAPABILITIES:
        assert active["planned_from"] is not None or active["planned_to"] is not None
    if "delivery_window" in PENDING_CAPABILITIES:
        assert active["planned_from"] is None and active["planned_to"] is None
    if "pickup_point" in CAPABILITIES:
        assert pickup["pickup_point"] is not None
    if "pickup_point" in PENDING_CAPABILITIES:
        assert pickup["pickup_point"] is None
    if "url" in CAPABILITIES:
        assert delivered["url"] is not None
    if "url" in PENDING_CAPABILITIES:
        assert delivered["url"] is None
    if "history" in CAPABILITIES:
        assert with_history["history"] is not None


def test_normalize_delivered_parcel():
    parcel = normalize_parcel(delivered_sample())
    assert parcel["carrier"] == "Poczta Polska"
    assert parcel["barcode"] == DELIVERED_CODE
    assert parcel["sender"] is None
    assert parcel["receiver"] is None
    assert parcel["status"] == ParcelStatus.DELIVERED
    assert parcel["raw_status"] == "Final delivery"
    assert parcel["delivered"] is True
    assert parcel["delivered_at"] == "2026-04-29T13:12:42+02:00"
    assert parcel["planned_from"] is None
    assert parcel["planned_to"] is None
    assert parcel["pickup_point"] is None
    assert parcel["url"].endswith(f"?{DELIVERED_CODE}")
    assert parcel["weight"] == 1.25
    assert parcel["dimensions"] is None
    assert parcel["history"] is None  # opt-in, default off


def test_normalize_history_is_opt_in():
    parcel = normalize_parcel(delivered_sample(), include_history=True)
    assert len(parcel["history"]) == 5
    assert parcel["history"][0]["status"] == ParcelStatus.REGISTERED


def test_normalize_active_parcel():
    parcel = normalize_parcel(active_sample())
    assert parcel["status"] == ParcelStatus.OUT_FOR_DELIVERY
    assert parcel["delivered"] is False
    assert parcel["delivered_at"] is None
    assert parcel["pickup"] is False


def test_normalize_pickup_parcel():
    """P_A directly precedes P_KWD live; both are pickup, not a problem."""
    parcel = normalize_parcel(pickup_sample())
    assert parcel["status"] == ParcelStatus.AT_PICKUP_POINT
    assert parcel["pickup"] is True
    assert parcel["pickup_point"] == "Placeholder Post Office"
    assert normalize_parcel(record(ACTIVE_CODE, pickup_sample()["mailInfo"]["events"][:2]))[
        "status"
    ] == ParcelStatus.AT_PICKUP_POINT


def test_scan_code_wins_over_a_lagging_state_label():
    """After a failed delivery the state label can still say "in delivery"."""
    raw = record(
        ACTIVE_CODE,
        [event("P_WD", "2026-04-29T08:46:00"), event("P_NDZ", "2026-04-29T15:00:00")],
    )
    raw["mailInfo"]["events"][-1]["state"] = {"code": "DOR", "name": "In delivery"}
    assert normalize_parcel(raw)["status"] == ParcelStatus.RETURNING


def test_repeated_codes_use_the_newest_event():
    """A parcel can pass P_WD twice after a failed delivery round."""
    raw = record(
        ACTIVE_CODE,
        [
            event("P_WD", "2026-04-28T08:00:00"),
            event("P_KWD", "2026-04-28T12:00:00"),
            event("P_PZL", "2026-04-29T06:00:00"),
        ],
    )
    assert normalize_parcel(raw)["status"] == ParcelStatus.IN_TRANSIT


def test_cancelled_event_is_ignored_for_status_and_history():
    raw = record(
        ACTIVE_CODE,
        [
            event("P_PZL", "2026-04-28T15:00:00"),
            event("P_D", "2026-04-29T09:00:00", canceled=True),
        ],
    )
    parcel = normalize_parcel(raw, include_history=True)
    assert parcel["status"] == ParcelStatus.IN_TRANSIT
    assert len(parcel["history"]) == 1
    assert len(raw["mailInfo"]["events"]) == 2  # raw is never trimmed


def test_finished_flag_marks_delivered_and_warns_when_the_scan_disagrees(caplog):
    raw = record(ACTIVE_CODE, [event("P_PZL", "2026-04-29T09:00:00")], finished=True)
    parcel = normalize_parcel(raw)
    assert parcel["delivered"] is True
    assert parcel["status"] == ParcelStatus.IN_TRANSIT
    assert "marks a parcel finished" in caplog.text
    assert "issues/new" in caplog.text
    normalize_parcel(raw)
    assert caplog.text.count("marks a parcel finished") == 1


def test_delivery_scan_without_finished_flag_still_counts_as_delivered():
    raw = record(ACTIVE_CODE, [event("P_D", "2026-04-29T09:00:00")])
    assert normalize_parcel(raw)["delivered"] is True


def test_normalize_pending_placeholder():
    """A tracked-but-not-yet-scanned code still yields a full parcel dict."""
    parcel = normalize_parcel({"number": ACTIVE_CODE})
    assert parcel["barcode"] == ACTIVE_CODE
    assert parcel["status"] == ParcelStatus.UNKNOWN
    assert parcel["delivered"] is False
    assert parcel["raw_status"] is None
    assert parcel["weight"] is None
    assert parcel["history"] is None


@pytest.mark.parametrize("weight", [None, 0, -1, "1.2", True])
def test_normalize_drops_unusable_weight(weight):
    raw = active_sample()
    raw["mailInfo"]["weight"] = weight
    assert normalize_parcel(raw)["weight"] is None


def test_normalize_survives_a_malformed_mail_info():
    parcel = normalize_parcel({"number": ACTIVE_CODE, "mailInfo": "oops"})
    assert parcel["status"] == ParcelStatus.UNKNOWN


def test_normalize_keeps_the_full_raw_payload():
    raw = active_sample()
    assert normalize_parcel(raw)["raw"] is raw
    assert "postOffice" in raw["mailInfo"]["events"][0]


# ---------------------------------------------------------------------------
# sort_parcels_by_ts
# ---------------------------------------------------------------------------


def test_sort_parcels_ascending_puts_unparseable_last():
    parcels = [
        {"barcode": "a", "planned_from": "2026-05-02T10:00:00Z"},
        {"barcode": "b", "planned_from": None},
        {"barcode": "c", "planned_from": "2026-05-01T10:00:00Z"},
    ]
    ordered = [p["barcode"] for p in sort_parcels_by_ts(parcels, "planned_from")]
    assert ordered == ["c", "a", "b"]


def test_sort_parcels_descending_still_puts_unparseable_last():
    parcels = [
        {"barcode": "a", "delivered_at": "2026-05-02T10:00:00Z"},
        {"barcode": "b", "delivered_at": "nonsense"},
        {"barcode": "c", "delivered_at": "2026-05-01T10:00:00Z"},
    ]
    ordered = [
        p["barcode"]
        for p in sort_parcels_by_ts(parcels, "delivered_at", descending=True)
    ]
    assert ordered == ["a", "c", "b"]


# ---------------------------------------------------------------------------
# apply_delivered_filter
# ---------------------------------------------------------------------------


def _entry(filter_type: str, amount: int) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        options={
            CONF_DELIVERED_FILTER_TYPE: filter_type,
            CONF_DELIVERED_FILTER_AMOUNT: amount,
        },
        unique_id=DOMAIN,
    )


def _delivered_pair() -> list[dict]:
    now = datetime.now(timezone.utc)
    return [
        {"barcode": "RECENT", "delivered_at": (now - timedelta(days=1)).isoformat()},
        {"barcode": "OLD", "delivered_at": (now - timedelta(days=30)).isoformat()},
    ]


def test_delivered_filter_by_days():
    kept = apply_delivered_filter(_delivered_pair(), _entry("days", 7))
    assert [p["barcode"] for p in kept] == ["RECENT"]


def test_delivered_filter_by_count():
    parcels = _delivered_pair()
    assert apply_delivered_filter(parcels, _entry("parcels", 1)) == parcels[:1]


def test_delivered_filter_keeps_unparseable_timestamp():
    """Better to show a parcel with a broken date than to silently drop it."""
    parcels = [{"barcode": "WEIRD", "delivered_at": "nonsense"}]
    assert apply_delivered_filter(parcels, _entry("days", 7)) == parcels


def test_pickup_point_names_the_post_office_only_while_at_pickup():
    assert normalize_parcel(pickup_sample())["pickup_point"] == "Placeholder Post Office"
    assert normalize_parcel(active_sample())["pickup_point"] is None
    assert normalize_parcel(delivered_sample())["pickup_point"] is None


def test_pickup_point_tolerates_a_missing_post_office():
    raw = pickup_sample()
    raw["mailInfo"]["events"][-1]["postOffice"] = {}
    assert normalize_parcel(raw)["pickup_point"] is None


def test_url_links_the_public_tracking_page():
    parcel = normalize_parcel(delivered_sample())
    assert parcel["url"] == (
        f"https://www.poczta-polska.pl/sledzenie-przesylek/?{parcel['barcode']}"
    )
