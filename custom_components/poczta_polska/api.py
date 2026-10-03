"""Poczta Polska public tracking client.

The coordinator relies on this contract:

* ``async_get_parcel`` returns the full per-parcel response dict on success,
* returns ``None`` when there is nothing to show yet — an unknown number, a
  grouped consignment, or a record without any usable event. That is a normal,
  expected state, never an error,
* raises :class:`PocztaPolskaAuthError` when the carrier keeps rejecting the
  widget key after it was re-read once,
* raises :class:`PocztaPolskaApiError` for anything else, with ``status_code``
  set on a non-2xx response and ``retry_after`` set when a 429 carried a
  ``Retry-After`` in seconds,
* lets ``aiohttp.ClientError`` propagate untouched — ``DataUpdateCoordinator``
  already wraps those into ``UpdateFailed``.
"""
from __future__ import annotations

import asyncio
import logging
from html.parser import HTMLParser
from typing import Any

import aiohttp

from .const import PORTAL_URL, TRACKING_API_BASE, TRACKING_API_URL

_LOGGER = logging.getLogger(__name__)

_WIDGET_ID = "widgetTracking"
_KEY_HEADER = "API_KEY"

# Things we have already warned about this session, so a shape we do not
# understand is reported once instead of on every poll.
_warned: set[str] = set()


def _warn_once(key: str, message: str) -> None:
    """Log ``message`` at WARNING the first time ``key`` is seen."""
    if key in _warned:
        return
    _warned.add(key)
    _LOGGER.warning(message)


class PocztaPolskaApiError(Exception):
    """Raised when a Poczta Polska API call returns an unexpected response."""

    def __init__(
        self,
        detail: str,
        *,
        status_code: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        """Store the status code and the ``Retry-After`` header, if any."""
        super().__init__(f"Poczta Polska API request failed: {detail}")
        self.detail = detail
        self.status_code = status_code
        self.retry_after = retry_after


class PocztaPolskaAuthError(PocztaPolskaApiError):
    """Raised when the carrier rejects the access value even after a refresh.

    There is no user credential behind it, so this never opens a reauth flow:
    the coordinator treats it as an outage. The message never carries the key.
    """


class _WidgetParser(HTMLParser):
    """Collect the attributes of the portal's tracking widget element."""

    def __init__(self) -> None:
        super().__init__()
        self.attrs: dict[str, str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Remember the first element whose id is the tracking widget."""
        if self.attrs is not None:
            return
        found = {name: value or "" for name, value in attrs}
        if found.get("id") == _WIDGET_ID:
            self.attrs = found


def _parse_widget(html: str) -> tuple[str, str]:
    """Return ``(tracking base URL, key)`` from the portal page markup."""
    parser = _WidgetParser()
    parser.feed(html)
    attrs = parser.attrs or {}
    base = attrs.get("data-urltracking", "").strip()
    key = attrs.get("data-apikey", "").strip()
    if not base or not key:
        raise PocztaPolskaApiError("tracking widget not found on the portal page")
    return base, key


def _retry_after(response: aiohttp.ClientResponse) -> float | None:
    """Parse a ``Retry-After`` expressed in seconds, else ``None``."""
    header = response.headers.get("Retry-After")
    try:
        return float(header) if header else None
    except ValueError:
        return None  # an HTTP-date, not seconds; let the caller's own backoff handle it


class PocztaPolskaApiClient:
    """Client for the public Poczta Polska tracking endpoint.

    The access value is fetched when first needed, held in memory only, and
    refreshed once when the endpoint answers 401.
    """

    def __init__(self, session: aiohttp.ClientSession) -> None:
        """Initialise the client with an aiohttp session."""
        self._session = session
        self._key: str | None = None
        # One refresh serves every parcel of a poll that hits a stale key.
        self._key_lock = asyncio.Lock()

    async def _async_read_key(self) -> str:
        """Fetch a fresh access value."""
        async with self._session.get(PORTAL_URL) as response:
            if response.status != 200:
                raise PocztaPolskaApiError(
                    f"portal page HTTP {response.status}",
                    status_code=response.status,
                    retry_after=_retry_after(response)
                    if response.status == 429
                    else None,
                )
            html = await response.text()
        base, key = _parse_widget(html)
        if base != TRACKING_API_BASE:
            # The access value must never be sent to a host we did not expect.
            raise PocztaPolskaApiError("portal advertises an unexpected endpoint")
        return key

    async def _async_get_key(self, *, stale: str | None = None) -> str:
        """Return the cached key, fetching a new one if it is missing or stale."""
        async with self._key_lock:
            if self._key is None or self._key == stale:
                self._key = await self._async_read_key()
            return self._key

    async def _async_post(self, tracking_code: str, key: str) -> Any:
        """POST one lookup; return the parsed body, or ``None`` on a 401."""
        body = {"language": "EN", "number": tracking_code, "addPostOfficeInfo": False}
        async with self._session.post(
            TRACKING_API_URL,
            json=body,
            headers={_KEY_HEADER: key},
            # A redirect would carry the key header to a host never validated.
            allow_redirects=False,
        ) as response:
            if response.status == 401:
                return None
            if response.status == 429:
                raise PocztaPolskaApiError(
                    "HTTP 429", status_code=429, retry_after=_retry_after(response)
                )
            if response.status != 200:
                raise PocztaPolskaApiError(
                    f"HTTP {response.status}", status_code=response.status
                )
            try:
                # content_type=None: consumer endpoints routinely serve JSON as
                # text/plain, and aiohttp would otherwise refuse to parse it.
                return await response.json(content_type=None)
            except ValueError as err:
                raise PocztaPolskaApiError(f"unparseable body ({err})") from err

    async def async_get_parcel(self, tracking_code: str) -> dict[str, Any] | None:
        """Fetch one parcel's tracking details.

        Returns the response dict for a parcel with usable events, or ``None``
        for an unknown code, a grouped consignment or an empty history.
        """
        key = await self._async_get_key()
        payload = await self._async_post(tracking_code, key)
        if payload is None:
            key = await self._async_get_key(stale=key)
            payload = await self._async_post(tracking_code, key)
            if payload is None:
                raise PocztaPolskaAuthError(
                    "authorization rejected (HTTP 401)", status_code=401
                )

        if not isinstance(payload, dict):
            raise PocztaPolskaApiError("unexpected body (not a JSON object)")

        mail_info = payload.get("mailInfo")
        if not isinstance(mail_info, dict):
            if payload.get("mailStatus") != -1:
                _warn_once(
                    "no-mail-info",
                    "Poczta Polska answered without parcel details and without "
                    "the usual not-found marker; treating the parcel as unknown. "
                    f"Response keys: {sorted(payload)}",
                )
            return None

        if mail_info.get("components"):
            _warn_once(
                "grouped",
                "Poczta Polska returned a grouped consignment, which is not "
                "supported; the parcel is shown as unknown.",
            )
            return None

        events = mail_info.get("events")
        if not isinstance(events, list) or not any(
            isinstance(event, dict) and not event.get("canceled") for event in events
        ):
            return None
        return payload
