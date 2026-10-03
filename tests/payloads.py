"""Sample Poczta Polska API payloads shared by the test modules.

Shaped after the live response (types and field names), with every
identifier replaced by a made-up value. Post-office names are neutral
placeholders; real ones can be street addresses and must never be copied in.
Keep them in one module rather than inline in each test — when the payload
shape turns out to be different from what was assumed, there is then exactly
one place to fix.
"""
from __future__ import annotations

ACTIVE_CODE = "PX9999999999"
DELIVERED_CODE = "PX1234567890"

# State labels are coarser than the scan code and can lag it.
_STATE = {
    "P_REJ_KN1": ("PRZ", "Electronic item data received"),
    "P_NAD": ("NA", "Posting/collection"),
    "P_PZL": ("TR", "In transport"),
    "P_WD": ("DOR", "In delivery"),
    "P_ND": ("DOR", "Delivery attempt failed"),
    "P_A": ("AW", "Notification left"),
    "P_KWD": ("ODB", "Ready for pick-up at the Post Office"),
    "P_D": ("DO", "Final delivery"),
    "P_NDZ": ("ZW", "Returned"),
}


def event(code: str, time: str, *, finished: bool = False, canceled: bool = False) -> dict:
    """One entry of the carrier's own event timeline."""
    state_code, name = _STATE.get(code, ("TR", f"Scan {code}"))
    return {
        "code": code,
        "name": name,
        "time": time,
        "postOffice": {"name": "Placeholder Post Office", "code": "000000"},
        "finished": finished,
        "canceled": canceled,
        "state": {"code": state_code, "name": name},
    }


def record(code: str, events: list[dict], *, finished: bool = False) -> dict:
    """A found-parcel response around ``events`` (oldest first)."""
    return {
        "number": code,
        "mailStatus": 0,
        "mailInfo": {
            "dispatchDate": "2026-04-27",
            "dispatchCountryCode": "PL",
            "dispatchCountryName": "Poland",
            "dispatchPostOffice": {"code": "000000", "name": "Placeholder Post Office"},
            "recipientCountryCode": "PL",
            "recipientCountryName": "Poland",
            "recipientPostOffice": {"code": "000001", "name": "Placeholder Post Office"},
            "typeOfMailCode": "PX",
            "typeOfMailName": "Parcel",
            "format": "M",
            "weight": 1.25,
            "deliveryReceipt": False,
            "finished": finished,
            "events": events,
        },
    }


def delivered_sample(code: str = DELIVERED_CODE) -> dict:
    """A delivered parcel that needed a second delivery round."""
    return record(
        code,
        [
            event("P_REJ_KN1", "2026-04-27T23:03:58"),
            event("P_NAD", "2026-04-28T09:10:00"),
            event("P_PZL", "2026-04-28T15:52:17"),
            event("P_WD", "2026-04-29T08:46:00"),
            event("P_D", "2026-04-29T13:12:42", finished=True),
        ],
        finished=True,
    )


def active_sample(code: str = ACTIVE_CODE) -> dict:
    """A parcel out for delivery."""
    return record(
        code,
        [
            event("P_REJ_KN1", "2026-04-27T23:03:58"),
            event("P_PZL", "2026-04-28T15:52:17"),
            event("P_WD", "2026-04-29T08:46:00"),
        ],
    )


def pickup_sample(code: str = ACTIVE_CODE) -> dict:
    """A parcel waiting at a post office."""
    return record(
        code,
        [
            event("P_PZL", "2026-04-28T15:52:17"),
            event("P_A", "2026-04-29T10:00:00"),
            event("P_KWD", "2026-04-29T10:05:00"),
        ],
    )


def not_found_sample(code: str = ACTIVE_CODE) -> dict:
    """What the endpoint answers for a number it does not know."""
    return {"number": code, "mailStatus": -1}
