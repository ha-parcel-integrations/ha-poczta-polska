"""Tests for Poczta Polska diagnostics."""
import json
from datetime import timedelta
from unittest.mock import MagicMock

from custom_components.poczta_polska.diagnostics import (
    async_get_config_entry_diagnostics,
)

from .payloads import delivered_sample


async def test_diagnostics_redacts_and_counts(hass):
    """Diagnostics get pasted into public issues — nothing identifying may survive."""
    entry = MagicMock()
    entry.options = {"parcels": [{"tracking_code": "PX1234567890"}]}
    entry.runtime_data.coordinator.current_tier_minutes = 15
    entry.runtime_data.coordinator.update_interval = timedelta(minutes=15)
    entry.runtime_data.coordinator.data = [
        {
            "barcode": "PX1234567890",
            "sender": "Example Shop",
            "receiver": "Jane Doe",
            "status": "out_for_delivery",
            "raw": {
                "number": "PX1234567890",
                "recipient": "Jane Doe",
                "deliveryAddress": {"city": "Rotterdam", "street": "Coolsingel 1"},
            },
        }
    ]
    entry.runtime_data.coordinator.delivered = []
    entry.runtime_data.coordinator.delivered_codes = set()

    result = await async_get_config_entry_diagnostics(hass, entry)

    assert result["counts"] == {
        "incoming_active": 1,
        "delivered": 0,
        "skipped_from_fetch": 0,
    }
    assert result["polling"] == {
        "tier_minutes": 15,
        "update_interval_seconds": 900.0,
        "suspended": False,
    }
    # tracking codes and payload PII are redacted, at every nesting level
    assert result["entry_options"]["parcels"][0]["tracking_code"] == "**REDACTED**"
    assert result["incoming"][0]["barcode"] == "**REDACTED**"
    assert result["incoming"][0]["receiver"] == "**REDACTED**"
    assert result["incoming"][0]["raw"]["recipient"] == "**REDACTED**"
    assert result["incoming"][0]["raw"]["deliveryAddress"] == "**REDACTED**"
    # non-identifying fields survive, or the diagnostics would be useless
    assert result["incoming"][0]["status"] == "out_for_delivery"


async def test_diagnostics_reports_suspended_polling(hass):
    """update_interval None (Section 2.1's full stop) must be visible, not just absent."""
    entry = MagicMock()
    entry.options = {"parcels": []}
    entry.runtime_data.coordinator.current_tier_minutes = None
    entry.runtime_data.coordinator.update_interval = None
    entry.runtime_data.coordinator.data = []
    entry.runtime_data.coordinator.delivered = []

    result = await async_get_config_entry_diagnostics(hass, entry)

    assert result["polling"] == {
        "tier_minutes": None,
        "update_interval_seconds": None,
        "suspended": True,
    }


async def test_diagnostics_hide_post_office_blocks_but_keep_the_scan_codes(hass):
    """Post-office blocks can be street addresses; they stay in raw only."""
    entry = MagicMock()
    entry.options = {"parcels": []}
    entry.runtime_data.coordinator.current_tier_minutes = 45
    entry.runtime_data.coordinator.update_interval = timedelta(minutes=45)
    entry.runtime_data.coordinator.data = [{"barcode": "PX1234567890", "raw": delivered_sample()}]
    entry.runtime_data.coordinator.delivered = []
    entry.runtime_data.coordinator.delivered_codes = set()

    result = await async_get_config_entry_diagnostics(hass, entry)

    dumped = json.dumps(result)
    assert "Placeholder Post Office" not in dumped
    assert "PX1234567890" not in dumped
    raw = result["incoming"][0]["raw"]
    assert raw["number"] == "**REDACTED**"
    assert raw["mailInfo"]["dispatchPostOffice"] == "**REDACTED**"
    assert raw["mailInfo"]["recipientPostOffice"] == "**REDACTED**"
    assert all(e["postOffice"] == "**REDACTED**" for e in raw["mailInfo"]["events"])
    assert raw["mailInfo"]["events"][-1]["code"] == "P_D"
