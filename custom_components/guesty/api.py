"""Guesty API client with OAuth2 client-credentials authentication.

Guesty's Open API uses a standard OAuth2 client-credentials grant. The
access token it returns is a bearer token valid for 24 hours; there is no
refresh token, so a new token must simply be requested again with the same
client_id/client_secret once the old one is close to expiring.

Guesty caps token requests at 5 per client_id per 24h, so the token is
cached in memory (and persisted by the caller via `on_token_refreshed`)
rather than re-fetched on every Home Assistant restart.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Awaitable, Callable

from aiohttp import ClientError, ClientResponseError, ClientSession

from .const import (
    API_BASE_URL,
    API_PAGE_SIZE,
    LISTINGS_PATH,
    OAUTH_SCOPE,
    OAUTH_TOKEN_URL,
    RESERVATIONS_PATH,
    TASKS_PATH,
    TOKEN_EXPIRY_BUFFER_SECONDS,
    WEBHOOK_SECRET_PATH,
    WEBHOOKS_PATH,
)

# Fields requested from /reservations - keep list/single-fetch queries
# consistent so both go through the same reservation-shaping code.
RESERVATION_FIELDS = (
    "status confirmationCode guestId listingId checkIn checkOut "
    "checkInDateLocalized checkOutDateLocalized source channel "
    "guestsCount numberOfGuests nightsCount keyCode guest.fullName "
    "listing.nickname listing.title createdAt"
)

# Columns requested from /tasks-open-api/tasks. createdAt isn't in Guesty's
# own example query but is needed here to sort by most-recently-created.
TASK_COLUMNS = (
    "id taskTitle status listing assignee canStartAfter mustFinishBefore createdAt"
)

_LOGGER = logging.getLogger(__name__)

# Guesty documents this as a 403, but some gateways surface auth/expiry
# problems as a 401 too - treat both as "needs a fresh token".
TOKEN_EXPIRED_STATUSES = (401, 403)


class GuestyApiError(Exception):
    """Generic error talking to the Guesty API."""


class GuestyAuthError(GuestyApiError):
    """Raised when authentication with Guesty fails (bad credentials)."""


class GuestyApiClient:
    """Handles authentication and authenticated requests to Guesty."""

    def __init__(
        self,
        session: ClientSession,
        client_id: str,
        client_secret: str,
        on_token_refreshed: Callable[[str, float], Awaitable[None]] | None = None,
    ) -> None:
        self._session = session
        self._client_id = client_id
        self._client_secret = client_secret
        # Called with (access_token, expires_at_epoch) whenever a new token
        # is fetched, so the caller can persist it across restarts.
        self._on_token_refreshed = on_token_refreshed

        self._access_token: str | None = None
        # Epoch time (time.time()) after which the cached token should be
        # considered stale and re-fetched. Epoch, not monotonic, so a
        # restored value survives a Home Assistant restart.
        self._token_expires_at: float = 0.0

    def restore_cached_token(self, access_token: str, expires_at: float) -> None:
        """Seed the in-memory cache from a previously persisted token.

        Avoids burning one of the 5 allowed daily token requests on every
        Home Assistant restart when the old token is still valid.
        """
        if expires_at > time.time():
            self._access_token = access_token
            self._token_expires_at = expires_at

    async def async_get_access_token(self) -> str:
        """Return a valid bearer token, fetching a new one if needed."""
        if self._access_token is None or time.time() >= self._token_expires_at:
            await self._async_fetch_token()
        return self._access_token  # type: ignore[return-value]

    async def _async_fetch_token(self) -> None:
        """Request a new bearer token from Guesty."""
        data = {
            "grant_type": "client_credentials",
            "scope": OAUTH_SCOPE,
            "client_id": self._client_id,
            "client_secret": self._client_secret,
        }

        try:
            response = await self._session.post(
                OAUTH_TOKEN_URL,
                data=data,
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
            )
        except ClientError as err:
            raise GuestyApiError(f"Error connecting to Guesty: {err}") from err

        if response.status in (400, 401, 403):
            raise GuestyAuthError(
                f"Guesty rejected the supplied client_id/client_secret "
                f"(HTTP {response.status})"
            )

        try:
            response.raise_for_status()
        except ClientResponseError as err:
            raise GuestyApiError(
                f"Unexpected error fetching Guesty token: {err}"
            ) from err

        payload: dict[str, Any] = await response.json()

        access_token = payload.get("access_token")
        if not access_token:
            raise GuestyAuthError("Guesty token response did not include an access_token")

        expires_in = int(payload.get("expires_in", 86400))
        self._access_token = access_token
        self._token_expires_at = (
            time.time() + expires_in - TOKEN_EXPIRY_BUFFER_SECONDS
        )
        _LOGGER.debug("Fetched new Guesty access token, expires_in=%s", expires_in)

        if self._on_token_refreshed is not None:
            await self._on_token_refreshed(access_token, self._token_expires_at)

    async def async_validate_credentials(self) -> None:
        """Raise GuestyAuthError/GuestyApiError if the credentials are invalid.

        Used by the config flow to verify user input before creating an entry.
        """
        await self._async_fetch_token()

    async def async_request(
        self,
        method: str,
        path: str,
        **kwargs: Any,
    ) -> Any:
        """Make an authenticated request against the Guesty API and return JSON."""
        token = await self.async_get_access_token()
        headers = kwargs.pop("headers", {})
        headers["Authorization"] = f"Bearer {token}"

        url = f"{API_BASE_URL}{path}"

        try:
            response = await self._session.request(method, url, headers=headers, **kwargs)
        except ClientError as err:
            raise GuestyApiError(f"Error connecting to Guesty: {err}") from err

        if response.status in TOKEN_EXPIRED_STATUSES:
            # Token may have expired or been invalidated server-side; retry
            # once with a freshly fetched token before giving up. Only once,
            # since Guesty limits token requests to 5/day per client_id.
            await self._async_fetch_token()
            headers["Authorization"] = f"Bearer {self._access_token}"
            try:
                response = await self._session.request(
                    method, url, headers=headers, **kwargs
                )
            except ClientError as err:
                raise GuestyApiError(f"Error connecting to Guesty: {err}") from err

        try:
            response.raise_for_status()
        except ClientResponseError as err:
            raise GuestyApiError(f"Guesty API error: {err}") from err

        if response.status == 204:
            return None

        return await response.json()

    async def async_get_listings(self, *, active_only: bool = True) -> list[dict[str, Any]]:
        """Fetch all listings (properties), paginating as needed."""
        params: dict[str, Any] = {"limit": API_PAGE_SIZE, "skip": 0}
        if active_only:
            params["active"] = "true"

        listings: list[dict[str, Any]] = []
        while True:
            page = await self.async_request("GET", LISTINGS_PATH, params=params)
            results = page["results"]
            listings.extend(results)
            if len(results) < API_PAGE_SIZE:
                break
            params["skip"] += API_PAGE_SIZE

        return listings

    async def async_search_reservations(
        self,
        *,
        checkout_from: str | None = None,
        status: str | None = None,
        listing_id: str | None = None,
        sort: str = "checkIn",
    ) -> list[dict[str, Any]]:
        """Search reservations, paginating through all result pages.

        `checkout_from` (a full ISO datetime) filters out reservations that
        checked out before it - used as a loose lower bound (this endpoint's
        `$gt` operator isn't inclusive, so pass a value safely before the
        real cutoff; exact current-vs-upcoming filtering happens
        client-side). `status` restricts to e.g. "confirmed" - without it,
        Guesty also returns unconfirmed `inquiry` holds that aren't real
        bookings. `listing_id` restricts to a single property.
        """
        filters: list[dict[str, str]] = []
        if status:
            filters.append({"field": "status", "operator": "$eq", "value": status})
        if checkout_from:
            filters.append(
                {"field": "checkOut", "operator": "$gt", "value": checkout_from}
            )
        if listing_id:
            filters.append({"field": "listingId", "operator": "$eq", "value": listing_id})

        params: dict[str, Any] = {
            "sort": sort,
            "limit": API_PAGE_SIZE,
            "skip": 0,
            "fields": RESERVATION_FIELDS,
        }
        if filters:
            params["filters"] = json.dumps(filters)

        reservations: list[dict[str, Any]] = []
        while True:
            page = await self.async_request("GET", RESERVATIONS_PATH, params=params)
            results = page["results"]
            reservations.extend(results)
            total = page.get("count", 0)
            if not results or params["skip"] + len(results) >= total:
                break
            params["skip"] += API_PAGE_SIZE

        return reservations

    async def async_get_tasks(
        self, *, exclude_statuses: list[str] | None = None
    ) -> list[dict[str, Any]]:
        """Fetch tasks, paginating through all result pages.

        Guesty's tasks-open-api uses a distinct filter syntax from both
        /reservations and /reservations-v3/search: a JSON-object-shaped
        filter with `@`-prefixed operators, e.g.
        `{'status': {'@nin': ['completed']}}` - confirmed working with
        single-quoted (Python dict repr) formatting, not necessarily
        double-quoted JSON, so `str()` is used here rather than
        `json.dumps()`.
        """
        filters: dict[str, Any] = {}
        if exclude_statuses:
            filters["status"] = {"@nin": exclude_statuses}

        params: dict[str, Any] = {
            "columns": TASK_COLUMNS,
            "limit": API_PAGE_SIZE,
            "skip": 0,
        }
        if filters:
            params["filters"] = str(filters)

        tasks: list[dict[str, Any]] = []
        while True:
            page = await self.async_request("GET", TASKS_PATH, params=params)
            results = page.get("results", []) if isinstance(page, dict) else (page or [])
            tasks.extend(results)
            if len(results) < API_PAGE_SIZE:
                break
            params["skip"] += API_PAGE_SIZE

        return tasks

    async def async_complete_task(self, task_id: str) -> None:
        """Mark a task as completed. The only supported task write."""
        await self.async_request(
            "PUT", f"/tasks-open-api/{task_id}", json={"status": "completed"}
        )

    async def async_get_reservation(self, reservation_id: str) -> dict[str, Any]:
        """Fetch one reservation's current, authoritative state by ID.

        Used after a webhook delivery instead of trusting the webhook's own
        payload - Guesty can send multiple webhook deliveries per event, and
        recommends re-fetching the latest state rather than relying on
        webhook payload contents.
        """
        return await self.async_request(
            "GET",
            f"{RESERVATIONS_PATH}/{reservation_id}",
            params={"fields": RESERVATION_FIELDS},
        )

    async def async_get_reservation_custom_fields(
        self, reservation_id: str
    ) -> list[dict[str, Any]]:
        """Fetch the populated custom field values for one reservation.

        Uses the v3 custom-fields endpoint deliberately, not v1's - v1 as a
        whole is being sunset by Guesty, while this specific v3 sub-resource
        is released/stable (unlike /reservations-v3/search, which is beta
        and misses some channels' reservations entirely).
        """
        payload = await self.async_request(
            "GET", f"/reservations-v3/{reservation_id}/custom-fields"
        )
        if isinstance(payload, dict):
            return payload.get("customFields", [])
        return payload or []

    async def async_get_guest(self, guest_id: str) -> dict[str, Any]:
        """Fetch a guest's profile (full name, returning-guest status, ...)."""
        return await self.async_request("GET", f"/guests-crud/{guest_id}")

    async def async_get_account_custom_fields(
        self, account_id: str
    ) -> list[dict[str, Any]]:
        """Fetch this account's custom field definitions (fieldId + displayName)."""
        return await self.async_request(
            "GET", f"/accounts/{account_id}/custom-fields"
        )

    async def async_get_listing_custom_fields(
        self, listing_id: str
    ) -> list[dict[str, Any]]:
        """Fetch the populated custom field values for one listing.

        Guesty's `/listings` response doesn't embed `customFields` inline,
        so this is a dedicated per-listing lookup instead.
        """
        payload = await self.async_request(
            "GET", f"/listings/{listing_id}/custom-fields"
        )
        if isinstance(payload, dict):
            return payload.get("customFields", [])
        return payload or []

    async def async_list_webhooks(self) -> list[dict[str, Any]]:
        """List all webhook subscriptions configured on this account."""
        payload = await self.async_request("GET", WEBHOOKS_PATH)
        if isinstance(payload, dict):
            return payload.get("results", [])
        return payload or []

    async def async_create_webhook(
        self, url: str, events: list[str]
    ) -> dict[str, Any]:
        """Create a new webhook subscription. Guesty only supports managing

        webhooks via this API - there is no dashboard equivalent.
        """
        return await self.async_request(
            "POST", WEBHOOKS_PATH, json={"url": url, "events": events}
        )

    async def async_update_webhook(
        self, webhook_id: str, url: str, events: list[str]
    ) -> dict[str, Any] | None:
        """Update an existing webhook subscription's URL/events in place.

        Guesty returns 204 No Content on success (no body) - callers must
        not assume a dict comes back.
        """
        return await self.async_request(
            "PUT", f"{WEBHOOKS_PATH}/{webhook_id}", json={"url": url, "events": events}
        )

    async def async_get_webhook_secret(self, notification_url: str) -> str:
        """Fetch the Svix signing secret Guesty uses for a webhook URL.

        Guesty must already have a webhook subscription pointing at
        `notification_url` for this to succeed.
        """
        payload = await self.async_request(
            "GET", WEBHOOK_SECRET_PATH, params={"url": notification_url}
        )
        if isinstance(payload, dict):
            secret = payload.get("secret") or payload.get("signingSecret") or payload.get("key")
            if secret:
                return secret
            raise GuestyApiError(
                f"Unexpected webhook secret response shape: {sorted(payload.keys())}"
            )
        if isinstance(payload, str):
            return payload
        raise GuestyApiError(f"Unexpected webhook secret response type: {type(payload)}")

    async def async_set_reservation_custom_field(
        self, reservation_id: str, field_id: str, value: Any
    ) -> None:
        """Write a single custom field value on a reservation.

        Uses the v3 path (matching the GET custom-fields endpoint), not v1.
        """
        await self.async_request(
            "PUT",
            f"/reservations-v3/{reservation_id}/custom-fields",
            json={"customFields": [{"fieldId": field_id, "value": value}]},
        )
