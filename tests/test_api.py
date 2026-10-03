"""Tests for the Poczta Polska API client."""
import json
import logging
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

from custom_components.poczta_polska.api import (
    PocztaPolskaApiClient,
    PocztaPolskaApiError,
    PocztaPolskaAuthError,
)
from custom_components.poczta_polska.const import (
    PORTAL_URL,
    TRACKING_API_BASE,
    TRACKING_API_URL,
)

from .payloads import (
    ACTIVE_CODE,
    active_sample,
    event,
    not_found_sample,
    record,
)

CODE = ACTIVE_CODE
# A made-up stand-in; the real value is never written down anywhere.
KEY = "test-widget-key-0001"
NEW_KEY = "test-widget-key-0002"


def _page(key: str | None = KEY, base: str | None = TRACKING_API_BASE) -> str:
    attrs = ""
    if base is not None:
        attrs += f' data-urltracking="{base}"'
    if key is not None:
        attrs += f' data-apikey="{key}"'
    return f'<html><body><div class="x"></div><div id="widgetTracking"{attrs}></div></body></html>'


def _response(status: int, body: object = None, *, headers: dict | None = None):
    response = AsyncMock()
    response.status = status
    response.headers = headers or {}
    response.text = AsyncMock(return_value=body if isinstance(body, str) else "")
    if isinstance(body, str):
        response.json = AsyncMock(side_effect=json.JSONDecodeError("x", body, 0))
    else:
        response.json = AsyncMock(return_value=body)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=response)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx


def _session(posts: list, pages: list | None = None) -> MagicMock:
    session = MagicMock()
    session.get = MagicMock(side_effect=pages or [_response(200, _page())])
    session.post = MagicMock(side_effect=posts)
    return session


async def test_get_parcel_sends_the_lookup_with_the_page_key():
    session = _session([_response(200, active_sample())])
    parcel = await PocztaPolskaApiClient(session).async_get_parcel(CODE)

    assert parcel["number"] == CODE
    session.get.assert_called_once_with(PORTAL_URL)
    url = session.post.call_args[0][0]
    assert url == TRACKING_API_URL
    kwargs = session.post.call_args[1]
    assert kwargs["headers"] == {"API_KEY": KEY}
    assert kwargs["json"] == {
        "language": "EN",
        "number": CODE,
        "addPostOfficeInfo": False,
    }


async def test_key_is_read_once_and_reused():
    session = _session([_response(200, active_sample()), _response(200, active_sample())])
    client = PocztaPolskaApiClient(session)
    await client.async_get_parcel(CODE)
    await client.async_get_parcel(CODE)
    assert session.get.call_count == 1


async def test_widget_attributes_may_come_in_either_order_and_quote_style():
    html = (
        f"<div data-apikey='{KEY}' id='widgetTracking' "
        f"data-urltracking='{TRACKING_API_BASE}'></div>"
    )
    session = _session([_response(200, active_sample())], [_response(200, html)])
    assert await PocztaPolskaApiClient(session).async_get_parcel(CODE)


async def test_401_rereads_the_key_once_and_retries():
    session = _session(
        [_response(401, "UNAUTHORIZED"), _response(200, active_sample())],
        [_response(200, _page()), _response(200, _page(NEW_KEY))],
    )
    parcel = await PocztaPolskaApiClient(session).async_get_parcel(CODE)

    assert parcel is not None
    assert session.get.call_count == 2
    assert session.post.call_args_list[1][1]["headers"] == {"API_KEY": NEW_KEY}


async def test_repeated_401_raises_an_auth_error_without_the_key():
    session = _session(
        [_response(401), _response(401)],
        [_response(200, _page()), _response(200, _page(NEW_KEY))],
    )
    with pytest.raises(PocztaPolskaAuthError) as err:
        await PocztaPolskaApiClient(session).async_get_parcel(CODE)

    assert err.value.status_code == 401
    assert isinstance(err.value, PocztaPolskaApiError)
    assert KEY not in str(err.value) and NEW_KEY not in str(err.value)


async def test_key_never_reaches_the_log(caplog):
    caplog.set_level(logging.DEBUG)
    session = _session(
        [_response(200, {"number": CODE, "mailStatus": 0, "mailInfo": {}})]
    )
    await PocztaPolskaApiClient(session).async_get_parcel(CODE)
    assert KEY not in caplog.text


async def test_concurrent_401s_share_one_page_read():
    """A stale key hit by every parcel of a poll must cost a single page read."""
    import asyncio

    def post(url, *, json, headers, allow_redirects):
        ctx = _response(200 if headers["API_KEY"] == NEW_KEY else 401, active_sample())
        enter = ctx.__aenter__

        async def slow_enter(*args):
            await asyncio.sleep(0)  # let the other lookup interleave
            return await enter(*args)

        ctx.__aenter__ = slow_enter
        return ctx

    session = _session([], [_response(200, _page()), _response(200, _page(NEW_KEY))])
    session.post = MagicMock(side_effect=post)
    client = PocztaPolskaApiClient(session)
    results = await asyncio.gather(
        client.async_get_parcel(CODE), client.async_get_parcel(CODE)
    )
    assert all(results)
    assert session.get.call_count == 2


@pytest.mark.parametrize(
    "page",
    [
        "<html></html>",
        _page(key=None),
        _page(base=None),
        _page(key=""),
    ],
)
async def test_missing_widget_attributes_raise(page):
    session = _session([], [_response(200, page)])
    with pytest.raises(PocztaPolskaApiError, match="widget"):
        await PocztaPolskaApiClient(session).async_get_parcel(CODE)
    session.post.assert_not_called()


async def test_unexpected_endpoint_never_receives_the_key():
    session = _session([], [_response(200, _page(base="https://elsewhere.test/track"))])
    with pytest.raises(PocztaPolskaApiError, match="unexpected endpoint") as err:
        await PocztaPolskaApiClient(session).async_get_parcel(CODE)
    session.post.assert_not_called()
    assert KEY not in str(err.value)


async def test_portal_error_status_raises():
    session = _session([], [_response(503)])
    with pytest.raises(PocztaPolskaApiError) as err:
        await PocztaPolskaApiClient(session).async_get_parcel(CODE)
    assert err.value.status_code == 503


async def test_portal_429_carries_retry_after():
    session = _session([], [_response(429, headers={"Retry-After": "30"})])
    with pytest.raises(PocztaPolskaApiError) as err:
        await PocztaPolskaApiClient(session).async_get_parcel(CODE)
    assert err.value.status_code == 429
    assert err.value.retry_after == 30.0


async def test_429_carries_retry_after_seconds():
    session = _session([_response(429, headers={"Retry-After": "120"})])
    with pytest.raises(PocztaPolskaApiError) as err:
        await PocztaPolskaApiClient(session).async_get_parcel(CODE)
    assert err.value.status_code == 429
    assert err.value.retry_after == 120.0


@pytest.mark.parametrize("header", [None, "Wed, 21 Oct 2026 07:28:00 GMT"])
async def test_429_without_usable_retry_after(header):
    headers = {"Retry-After": header} if header else {}
    session = _session([_response(429, headers=headers)])
    with pytest.raises(PocztaPolskaApiError) as err:
        await PocztaPolskaApiClient(session).async_get_parcel(CODE)
    assert err.value.retry_after is None


async def test_error_status_raises():
    session = _session([_response(500)])
    with pytest.raises(PocztaPolskaApiError) as err:
        await PocztaPolskaApiClient(session).async_get_parcel(CODE)
    assert err.value.status_code == 500


async def test_unparseable_body_raises():
    session = _session([_response(200, "not json")])
    with pytest.raises(PocztaPolskaApiError, match="unparseable"):
        await PocztaPolskaApiClient(session).async_get_parcel(CODE)


async def test_non_object_body_raises():
    session = _session([_response(200, ["not", "a", "dict"])])
    with pytest.raises(PocztaPolskaApiError, match="not a JSON object"):
        await PocztaPolskaApiClient(session).async_get_parcel(CODE)


async def test_unknown_number_is_none_without_a_warning(caplog):
    """mailStatus -1 with no mailInfo is the normal not-found answer."""
    session = _session([_response(200, not_found_sample())])
    assert await PocztaPolskaApiClient(session).async_get_parcel(CODE) is None
    assert caplog.text == ""


async def test_missing_mail_info_without_the_not_found_marker_warns_once(caplog):
    body = {"number": CODE, "mailStatus": 0}
    session = _session([_response(200, body), _response(200, body)])
    client = PocztaPolskaApiClient(session)
    assert await client.async_get_parcel(CODE) is None
    assert await client.async_get_parcel(CODE) is None
    assert caplog.text.count("without parcel details") == 1


async def test_grouped_consignment_is_no_data_and_warns_once(caplog):
    body = record(CODE, [event("P_PZL", "2026-04-28T15:52:17")])
    body["mailInfo"]["components"] = [{"number": "x"}]
    session = _session([_response(200, body), _response(200, body)])
    client = PocztaPolskaApiClient(session)
    assert await client.async_get_parcel(CODE) is None
    assert await client.async_get_parcel(CODE) is None
    assert caplog.text.count("grouped consignment") == 1


@pytest.mark.parametrize(
    "events",
    [
        [],
        None,
        "oops",
        ["not-a-dict"],
        [event("P_PZL", "2026-04-28T15:52:17", canceled=True)],
    ],
)
async def test_empty_or_fully_cancelled_history_is_no_data(events):
    body = record(CODE, [])
    body["mailInfo"]["events"] = events
    session = _session([_response(200, body)])
    assert await PocztaPolskaApiClient(session).async_get_parcel(CODE) is None


async def test_cancelled_events_stay_in_the_returned_record():
    """raw is never trimmed; ignoring cancelled scans is the normaliser's job."""
    body = record(
        CODE,
        [event("P_PZL", "2026-04-28T15:52:17"), event("P_D", "2026-04-29T10:00:00", canceled=True)],
    )
    session = _session([_response(200, body)])
    parcel = await PocztaPolskaApiClient(session).async_get_parcel(CODE)
    assert len(parcel["mailInfo"]["events"]) == 2


async def test_network_error_is_left_alone():
    """ClientError is left alone — DataUpdateCoordinator already wraps it."""
    session = _session([aiohttp.ClientError("boom")])
    with pytest.raises(aiohttp.ClientError):
        await PocztaPolskaApiClient(session).async_get_parcel(CODE)


async def test_the_first_widget_element_wins():
    html = _page() + _page(NEW_KEY)
    session = _session([_response(200, active_sample())], [_response(200, html)])
    await PocztaPolskaApiClient(session).async_get_parcel(CODE)
    assert session.post.call_args[1]["headers"] == {"API_KEY": KEY}
