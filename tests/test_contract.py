"""The parcel contract's checks, run over every normalised sample."""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from custom_components.poczta_polska.const import ParcelStatus
from custom_components.poczta_polska.parcels import (
    normalize_parcel,
    parse_iso,
    planned_window,
)

from .payloads import (
    ACTIVE_CODE,
    active_sample,
    delivered_sample,
    event,
    pickup_sample,
    record,
)

CANONICAL_KEYS = [
    "carrier",
    "barcode",
    "sender",
    "receiver",
    "contents",
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

_WARSAW = ZoneInfo("Europe/Warsaw")


def _collected() -> dict:
    return record(
        ACTIVE_CODE,
        [
            event("P_PZL", "2026-04-28T15:52:17"),
            event("P_A", "2026-04-29T10:00:00"),
            event("P_KWD", "2026-04-29T10:05:00"),
            event("P_D", "2026-04-30T16:00:00", finished=True),
        ],
        finished=True,
    )


def _returning() -> dict:
    return record(
        ACTIVE_CODE,
        [event("P_WD", "2026-04-29T08:00:00"), event("P_NDZ", "2026-04-29T15:00:00")],
    )


def _closed_return() -> dict:
    sample = _returning()
    sample["mailInfo"]["finished"] = True
    return sample


def _returned_after_pickup() -> dict:
    return record(
        ACTIVE_CODE,
        [event("P_KWD", "2026-04-29T10:05:00"), event("P_NDZ", "2026-05-06T15:00:00")],
    )


SAMPLES = {
    "delivered": delivered_sample,
    "active": active_sample,
    "at_pickup_point": pickup_sample,
    "collected": _collected,
    "returning": _returning,
    "closed_return": _closed_return,
    "returned_after_pickup": _returned_after_pickup,
    "problem": lambda: record(ACTIVE_CODE, [event("P_ND", "2026-04-29T12:00:00")]),
    "bare": lambda: {"number": ACTIVE_CODE},
}


@pytest.fixture(params=SAMPLES)
def parcel(request) -> dict:
    return normalize_parcel(SAMPLES[request.param]())


def test_keys_and_order(parcel):
    assert list(parcel) == CANONICAL_KEYS
    assert parcel["contents"] is None


def test_delivered_follows_status(parcel):
    assert parcel["delivered"] is (parcel["status"] is ParcelStatus.DELIVERED)


def test_delivered_at_only_when_delivered(parcel):
    if not parcel["delivered"]:
        assert parcel["delivered_at"] is None


def test_planned_window_is_ordered_and_anchored(parcel):
    if parcel["planned_to"] is not None:
        assert parcel["planned_from"] is not None
        assert parse_iso(parcel["planned_to"]) > parse_iso(parcel["planned_from"])


def test_planned_is_empty_when_the_parcel_is_done_or_waiting(parcel):
    if parcel["status"] in (
        ParcelStatus.DELIVERED,
        ParcelStatus.AT_PICKUP_POINT,
        ParcelStatus.RETURNING,
    ):
        assert parcel["planned_from"] is None
        assert parcel["planned_to"] is None


def test_pickup_is_a_bool_and_the_point_needs_it(parcel):
    assert isinstance(parcel["pickup"], bool)
    if not parcel["pickup"]:
        assert parcel["pickup_point"] is None


def test_a_collected_pickup_parcel_stays_pickup():
    parcel = normalize_parcel(_collected())
    assert parcel["status"] is ParcelStatus.DELIVERED
    assert parcel["delivered"] is True
    assert parcel["delivered_at"] == "2026-04-30T16:00:00+02:00"
    assert parcel["pickup"] is True
    assert parcel["pickup_point"] == "Placeholder Post Office"


def test_a_finished_flag_beats_a_newest_pickup_point_scan():
    sample = record(
        ACTIVE_CODE,
        [event("P_A", "2026-04-29T10:00:00"), event("P_KWD", "2026-04-29T10:05:00")],
        finished=True,
    )
    parcel = normalize_parcel(sample)
    assert parcel["status"] is ParcelStatus.DELIVERED
    assert parcel["delivered"] is True
    assert parcel["pickup"] is True


def test_a_home_delivery_is_not_pickup():
    parcel = normalize_parcel(delivered_sample())
    assert parcel["pickup"] is False
    assert parcel["pickup_point"] is None


def test_a_return_after_pickup_keeps_pickup_but_not_delivered():
    parcel = normalize_parcel(_returned_after_pickup())
    assert parcel["status"] is ParcelStatus.RETURNING
    assert parcel["delivered"] is False
    assert parcel["pickup"] is True
    assert parcel["pickup_point"] == "Placeholder Post Office"


def test_a_pickup_point_comes_from_the_pickup_scan_not_the_newest_one():
    sample = _returned_after_pickup()
    sample["mailInfo"]["events"][-1]["postOffice"]["name"] = "Sorting Hub"
    assert normalize_parcel(sample)["pickup_point"] == "Placeholder Post Office"


def test_a_closed_return_is_not_delivered():
    parcel = normalize_parcel(_closed_return())
    assert parcel["status"] is ParcelStatus.RETURNING
    assert parcel["delivered"] is False


def test_delivered_at_is_the_delivery_scan_not_the_newest_one():
    sample = delivered_sample()
    sample["mailInfo"]["events"].append(event("P_PZL", "2026-05-02T10:00:00"))
    sample["mailInfo"]["finished"] = True
    parcel = normalize_parcel(sample)
    assert parcel["delivered"] is True
    assert parcel["delivered_at"] == "2026-04-29T13:12:42+02:00"


def test_a_moment_ends_at_the_end_of_that_day():
    start, end = planned_window("2099-04-29T13:00:00", None, zone=_WARSAW)
    assert datetime.fromisoformat(end).time().isoformat() == "23:59:59"
    assert parse_iso(end).date() == parse_iso(start).date()


def test_a_date_covers_the_whole_day():
    start, end = planned_window("2099-04-29", None, zone=_WARSAW)
    assert datetime.fromisoformat(start).time().isoformat() == "00:00:00"
    assert datetime.fromisoformat(end).time().isoformat() == "23:59:59"
    assert start[:10] == end[:10]
