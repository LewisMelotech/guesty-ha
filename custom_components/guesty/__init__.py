"""The Guesty integration."""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import timedelta

import voluptuous as vol
from aiohttp.web import Request, Response
from homeassistant.components import persistent_notification, webhook
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady, HomeAssistantError
from homeassistant.helpers import config_validation as cv, device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.service import async_set_service_schema
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .api import GuestyApiClient, GuestyApiError, GuestyAuthError
from .const import (
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET,
    CONF_UPDATE_INTERVAL_MINUTES,
    CONF_WEBHOOK_BASE_URL,
    DEFAULT_UPDATE_INTERVAL_MINUTES,
    DOMAIN,
    EVENT_RESERVATION_NEW,
    INTEGRATION_DEVICE_ID_PREFIX,
    INTEGRATION_DEVICE_NAME,
    RESERVATION_DEVICE_ID_SUFFIX,
    RESERVATION_DEVICE_NAME_SUFFIX,
    RESERVATION_INCLUDED_STATUS,
    SERVICE_GET_FUTURE_RESERVATIONS,
    SERVICE_GET_RESERVATION,
    SERVICE_SET_RESERVATION_CUSTOM_FIELD,
    WEBHOOK_EVENTS,
)
from .coordinator import GuestyDataUpdateCoordinator, GuestyTasksCoordinator
from .custom_fields import field_label, resolve_field_id
from .webhook_utils import (
    build_event_data,
    extract_reservation_id,
    is_handled_event,
    verify_svix_signature,
)

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[str] = ["sensor", "todo", "image"]

STORAGE_VERSION = 1

SET_CUSTOM_FIELD_SCHEMA = vol.Schema(
    {
        vol.Required("reservation_id"): cv.string,
        vol.Required("field"): cv.string,
        vol.Required("value"): vol.Any(str, int, float, bool),
    }
)

GET_RESERVATION_SCHEMA = vol.Schema({vol.Required("reservation_id"): cv.string})

GET_FUTURE_RESERVATIONS_SCHEMA = vol.Schema({vol.Required("listing_id"): cv.string})


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Guesty from a config entry."""
    session = async_get_clientsession(hass)

    # Guesty allows at most 5 token requests per client_id per 24h, so the
    # last-issued token is persisted to disk and reused across HA restarts
    # instead of always fetching a fresh one on startup.
    token_store: Store = Store(hass, STORAGE_VERSION, f"{DOMAIN}_{entry.entry_id}_token")

    async def _async_persist_token(access_token: str, expires_at: float) -> None:
        await token_store.async_save({"access_token": access_token, "expires_at": expires_at})

    client = GuestyApiClient(
        session,
        entry.data[CONF_CLIENT_ID],
        entry.data[CONF_CLIENT_SECRET],
        on_token_refreshed=_async_persist_token,
    )

    cached_token = await token_store.async_load()
    if cached_token:
        client.restore_cached_token(cached_token["access_token"], cached_token["expires_at"])

    try:
        await client.async_get_access_token()
        listings = await client.async_get_listings()
    except GuestyAuthError as err:
        raise ConfigEntryAuthFailed("Guesty rejected the stored credentials") from err
    except GuestyApiError as err:
        raise ConfigEntryNotReady(f"Unable to reach Guesty: {err}") from err

    listings_by_id = {listing["_id"]: listing for listing in listings}

    device_registry = dr.async_get(hass)
    device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, f"{INTEGRATION_DEVICE_ID_PREFIX}_{entry.entry_id}")},
        name=INTEGRATION_DEVICE_NAME,
        manufacturer="Guesty",
    )
    for listing_id, listing in listings_by_id.items():
        property_name = listing.get("nickname") or listing.get("title") or listing_id
        device_registry.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={(DOMAIN, listing_id)},
            name=property_name,
            manufacturer="Guesty",
            model=listing.get("propertyType"),
        )
        device_registry.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={(DOMAIN, f"{listing_id}_{RESERVATION_DEVICE_ID_SUFFIX}")},
            name=f"{property_name}: {RESERVATION_DEVICE_NAME_SUFFIX}",
            manufacturer="Guesty",
            via_device=(DOMAIN, listing_id),
        )

    listing_field_defs, reservation_field_defs = await _async_get_custom_field_defs(
        client, listings
    )
    await _async_attach_listing_custom_field_values(
        client, listings_by_id, listing_field_defs
    )

    update_interval_minutes = entry.options.get(
        CONF_UPDATE_INTERVAL_MINUTES, DEFAULT_UPDATE_INTERVAL_MINUTES
    )

    coordinator = GuestyDataUpdateCoordinator(
        hass, client, list(listings_by_id), update_interval_minutes
    )
    await coordinator.async_config_entry_first_refresh()

    tasks_coordinator = GuestyTasksCoordinator(
        hass, client, list(listings_by_id), update_interval_minutes
    )
    await tasks_coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = {
        "client": client,
        "coordinator": coordinator,
        "tasks_coordinator": tasks_coordinator,
        "listings": listings_by_id,
        "listing_field_defs": listing_field_defs,
        "reservation_field_defs": reservation_field_defs,
    }

    await _async_setup_webhook(hass, entry, client)
    _async_setup_service(hass)
    # Refresh these services' dropdowns from this account's actual custom
    # field names / properties on every load/reload, so they stay current
    # even though the service handlers themselves are only registered once.
    async_set_service_schema(
        hass,
        DOMAIN,
        SERVICE_SET_RESERVATION_CUSTOM_FIELD,
        _build_custom_field_service_schema(reservation_field_defs),
    )
    async_set_service_schema(
        hass,
        DOMAIN,
        SERVICE_GET_FUTURE_RESERVATIONS,
        _build_future_reservations_service_schema(listings_by_id),
    )

    entry.async_on_unload(entry.add_update_listener(_async_reload_on_options_update))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _async_reload_on_options_update(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the entry when its options change (e.g. webhook_base_url)."""
    await hass.config_entries.async_reload(entry.entry_id)


async def _async_get_custom_field_defs(
    client: GuestyApiClient, listings: list[dict]
) -> tuple[list[dict], list[dict]]:
    """Fetch this account's custom field definitions, split by object type.

    Returns (listing_field_defs, reservation_field_defs). Every field found
    becomes a sensor - there's no hardcoded list of "known" fields, so this
    works unchanged for any Guesty account's own custom fields.
    """
    if not listings:
        return [], []

    account_id = listings[0].get("accountId")
    if not account_id:
        _LOGGER.warning("Could not determine Guesty account ID; skipping custom fields")
        return [], []

    try:
        account_fields = await client.async_get_account_custom_fields(account_id)
    except GuestyApiError as err:
        _LOGGER.warning("Could not fetch Guesty custom field definitions: %s", err)
        return [], []

    listing_fields = [f for f in account_fields if f.get("object") == "listing"]
    reservation_fields = [f for f in account_fields if f.get("object") == "reservation"]
    return listing_fields, reservation_fields


async def _async_attach_listing_custom_field_values(
    client: GuestyApiClient,
    listings_by_id: dict[str, dict],
    listing_field_defs: list[dict],
) -> None:
    """Fetch and attach each listing's custom field values, keyed by fieldId.

    Listing custom fields (e.g. "Has Smart Lock") change rarely, so these
    are fetched once at setup rather than on every coordinator poll.
    """
    if not listing_field_defs:
        for listing in listings_by_id.values():
            listing["custom_field_values"] = {}
        return

    async def _fetch(listing_id: str, listing: dict) -> None:
        try:
            fields = await client.async_get_listing_custom_fields(listing_id)
        except GuestyApiError as err:
            _LOGGER.debug(
                "Could not fetch custom fields for listing %s: %s", listing_id, err
            )
            fields = []
        listing["custom_field_values"] = {f["fieldId"]: f.get("value") for f in fields}

    await asyncio.gather(
        *(_fetch(listing_id, listing) for listing_id, listing in listings_by_id.items())
    )


async def _async_setup_webhook(
    hass: HomeAssistant, entry: ConfigEntry, client: GuestyApiClient
) -> None:
    """Register a webhook so Guesty can push reservation.new events.

    The webhook ID, Guesty's own webhook subscription ID, and the Svix
    signing secret are all persisted, so this is idempotent across restarts
    and reconfigurations - see _async_ensure_guesty_webhook.
    """
    webhook_store: Store = Store(hass, STORAGE_VERSION, f"{DOMAIN}_{entry.entry_id}_webhook")
    webhook_data = await webhook_store.async_load() or {}

    webhook_id = webhook_data.get("webhook_id")
    if not webhook_id:
        webhook_id = webhook.async_generate_id()
        webhook_data["webhook_id"] = webhook_id
        await webhook_store.async_save(webhook_data)

    base_url = entry.options.get(CONF_WEBHOOK_BASE_URL)
    webhook_url = (
        f"{base_url.rstrip('/')}/api/webhook/{webhook_id}"
        if base_url
        else webhook.async_generate_url(hass, webhook_id)
    )
    _LOGGER.info("Guesty webhook URL for this Home Assistant instance: %s", webhook_url)

    guesty_webhook_id = await _async_ensure_guesty_webhook(
        client, webhook_store, webhook_data, webhook_url
    )

    # Always refetch the signing secret rather than only when nothing is
    # cached - Svix issues a distinct secret per endpoint URL, so a stale
    # secret from before a webhook_base_url change (or a manual secret
    # rotation on Guesty's side) would otherwise silently reject every
    # delivery. Cheap, idempotent GET with no known rate limit, so doing
    # this on every reload is fine. Falls back to the last known-good
    # secret if the refetch fails (e.g. a transient network issue),
    # instead of breaking a webhook that was working.
    secret = webhook_data.get("secret")
    if guesty_webhook_id:
        try:
            fetched_secret = await client.async_get_webhook_secret(webhook_url)
        except GuestyApiError as err:
            _LOGGER.warning("Could not fetch Guesty webhook signing secret: %s", err)
        else:
            secret = fetched_secret
            webhook_data["secret"] = secret
            await webhook_store.async_save(webhook_data)

    if not secret:
        persistent_notification.async_create(
            hass,
            (
                f"Could not automatically set up the Guesty webhook - check that "
                f"your Guesty API credentials have permission to manage webhooks, "
                f"then reload the Guesty integration.\n\nWebhook URL: `{webhook_url}`"
            ),
            title="Guesty webhook setup incomplete",
            notification_id=f"{DOMAIN}_{entry.entry_id}_webhook",
        )
    else:
        persistent_notification.async_dismiss(hass, f"{DOMAIN}_{entry.entry_id}_webhook")

    # Defensive: async_register() raises if webhook_id is already registered.
    # If a previous setup attempt registered it but failed before finishing
    # (so async_unload_entry never ran), this avoids crashing on retry.
    webhook.async_unregister(hass, webhook_id)
    webhook.async_register(
        hass,
        DOMAIN,
        "Guesty",
        webhook_id,
        _make_webhook_handler(entry.entry_id, webhook_store),
    )
    hass.data[DOMAIN][entry.entry_id]["webhook_id"] = webhook_id


async def _async_ensure_guesty_webhook(
    client: GuestyApiClient,
    webhook_store: Store,
    webhook_data: dict,
    webhook_url: str,
) -> str | None:
    """Create or update the Guesty-side webhook subscription so it points at

    `webhook_url` for the reservation.new event.

    Guesty only supports managing webhooks via this API (no dashboard
    equivalent). To avoid ever creating a duplicate subscription: if we
    already know this subscription's Guesty-assigned ID, PUT (update) it in
    place - this also transparently handles the URL changing later (e.g. a
    webhook_base_url override being added) without creating a second one.
    Only falls back to listing/creating if we don't have an ID yet, or the
    one we had is no longer valid.

    Returns Guesty's webhook subscription ID, or None if none could be
    established.
    """
    guesty_webhook_id = webhook_data.get("guesty_webhook_id")

    if guesty_webhook_id:
        try:
            updated = await client.async_update_webhook(
                guesty_webhook_id, webhook_url, WEBHOOK_EVENTS
            )
            _LOGGER.info(
                "Guesty webhook %s updated - Guesty confirmed URL: %s (expected: %s)",
                guesty_webhook_id,
                updated.get("url") if updated else "(Guesty returned no body; assumed OK)",
                webhook_url,
            )
            return guesty_webhook_id
        except GuestyApiError as err:
            _LOGGER.debug(
                "Could not update existing Guesty webhook %s (%s); will look for "
                "or create one instead",
                guesty_webhook_id,
                err,
            )
            guesty_webhook_id = None

    try:
        existing = await client.async_list_webhooks()
    except GuestyApiError as err:
        _LOGGER.warning("Could not list Guesty webhooks: %s", err)
        existing = []

    for existing_webhook in existing:
        if existing_webhook.get("url") == webhook_url:
            guesty_webhook_id = existing_webhook.get("_id")
            _LOGGER.info(
                "Found existing Guesty webhook %s already pointing at: %s",
                guesty_webhook_id,
                existing_webhook.get("url"),
            )
            break

    if not guesty_webhook_id:
        try:
            created = await client.async_create_webhook(webhook_url, WEBHOOK_EVENTS)
        except GuestyApiError as err:
            _LOGGER.warning(
                "Could not create a Guesty webhook automatically (%s)", err
            )
            return None
        guesty_webhook_id = created.get("_id") if created else None
        _LOGGER.info(
            "Created new Guesty webhook %s - Guesty confirmed URL: %s (expected: %s)",
            guesty_webhook_id,
            created.get("url") if created else "(Guesty returned no body)",
            webhook_url,
        )

    if guesty_webhook_id:
        webhook_data["guesty_webhook_id"] = guesty_webhook_id
        await webhook_store.async_save(webhook_data)

    return guesty_webhook_id


def _make_webhook_handler(entry_id: str, webhook_store: Store):
    """Build the aiohttp handler for this config entry's webhook."""

    async def _handle(
        hass: HomeAssistant, webhook_id: str, request: Request
    ) -> Response | None:
        raw_body = await request.read()
        _LOGGER.info(
            "Received a Guesty webhook delivery (%d bytes) - validating", len(raw_body)
        )

        webhook_data = await webhook_store.async_load() or {}
        secret = webhook_data.get("secret")
        if not verify_svix_signature(
            secret or "",
            request.headers.get("svix-id", ""),
            request.headers.get("svix-timestamp", ""),
            request.headers.get("svix-signature", ""),
            raw_body,
        ):
            _LOGGER.warning("Rejected Guesty webhook delivery with an invalid signature")
            return Response(status=401)

        try:
            payload = json.loads(raw_body)
        except ValueError:
            _LOGGER.warning("Guesty webhook payload was not valid JSON")
            return Response(status=400)

        if not is_handled_event(payload):
            _LOGGER.debug(
                "Ignoring Guesty webhook payload that isn't reservation.new; "
                "top-level keys were: %s",
                sorted(payload.keys()),
            )
            return Response(status=200)

        reservation_id = extract_reservation_id(payload)
        if reservation_id is None:
            _LOGGER.warning(
                "Could not find a reservation ID in a Guesty webhook payload; "
                "top-level keys were: %s",
                sorted(payload.keys()),
            )
            return Response(status=200)

        entry_data = hass.data.get(DOMAIN, {}).get(entry_id)
        if entry_data is None:
            _LOGGER.warning(
                "Received a Guesty webhook but the config entry is no longer loaded"
            )
            return Response(status=410)

        # Guesty sends multiple webhook deliveries per event and recommends
        # re-fetching the reservation's current state by ID rather than
        # trusting the webhook payload's own (possibly stale/partial) data.
        client: GuestyApiClient = entry_data["client"]
        try:
            reservation = await client.async_get_reservation(reservation_id)
        except GuestyApiError as err:
            _LOGGER.warning(
                "Could not fetch reservation %s after a Guesty webhook: %s",
                reservation_id,
                err,
            )
            return Response(status=200)

        if reservation is None:
            _LOGGER.warning(
                "Guesty webhook referenced reservation %s but it could not be found",
                reservation_id,
            )
            return Response(status=200)

        # v1 /reservations doesn't embed custom fields - fetch separately,
        # same as the coordinator does for its own reservation enrichment.
        try:
            reservation["customFields"] = await client.async_get_reservation_custom_fields(
                reservation_id
            )
        except GuestyApiError as err:
            _LOGGER.debug(
                "Could not fetch custom fields for reservation %s: %s",
                reservation_id,
                err,
            )

        event_data = build_event_data(
            reservation,
            entry_data["listings"],
            entry_data["reservation_field_defs"],
            entry_data["listing_field_defs"],
        )
        hass.bus.async_fire(EVENT_RESERVATION_NEW, event_data)
        _LOGGER.info(
            "Fired %s for reservation %s (listing: %s)",
            EVENT_RESERVATION_NEW,
            reservation_id,
            event_data.get("listing_name") or event_data.get("listing_id"),
        )
        return Response(status=200)

    return _handle


def _build_custom_field_service_schema(reservation_field_defs: list[dict]) -> dict:
    """Build the service schema for guesty.set_reservation_custom_field,

    with a dropdown of this account's actual reservation custom field
    names - `custom_value: true` still allows typing a name or raw fieldId
    that isn't in the list (e.g. one added since the last reload).
    """
    field_names = sorted({field_label(f) for f in reservation_field_defs})
    return {
        "name": "Set reservation custom field",
        "description": (
            "Writes a value to a custom field on a Guesty reservation. Pick "
            "a known field name, or type a different name or raw fieldId."
        ),
        "fields": {
            "reservation_id": {
                "name": "Reservation ID",
                "description": (
                    "The Guesty reservationId to update "
                    "(e.g. from a guesty_reservation_new event)."
                ),
                "required": True,
                "example": "6a4611f64d5eacad15bd06e0",
                "selector": {"text": {}},
            },
            "field": {
                "name": "Field",
                "description": "The custom field's name (as configured in Guesty) or its fieldId.",
                "required": True,
                "example": "Reservation Access Code",
                "selector": {
                    "select": {
                        "options": field_names,
                        "custom_value": True,
                        "mode": "dropdown",
                    }
                },
            },
            "value": {
                "name": "Value",
                "description": "The value to write into the field.",
                "required": True,
                "example": "384712",
                "selector": {"text": {}},
            },
        },
    }


def _build_future_reservations_service_schema(listings_by_id: dict[str, dict]) -> dict:
    """Build the service schema for guesty.get_future_reservations, with a

    dropdown of this account's actual properties by name.
    """
    options = [
        {
            "value": listing_id,
            "label": listing.get("nickname") or listing.get("title") or listing_id,
        }
        for listing_id, listing in listings_by_id.items()
    ]
    return {
        "name": "Get future reservations",
        "description": (
            "Lists all future confirmed reservations for one property - e.g. "
            "to bulk-reprocess bookings after a property's lock setup changes."
        ),
        "fields": {
            "listing_id": {
                "name": "Property",
                "description": "The property to list future confirmed reservations for.",
                "required": True,
                "selector": {
                    "select": {
                        "options": options,
                        "custom_value": True,
                        "mode": "dropdown",
                    }
                },
            },
        },
    }


def _get_single_entry_data(hass: HomeAssistant) -> dict:
    """Return the first loaded Guesty config entry's data.

    All Guesty services assume a single connected account.
    """
    domain_data = hass.data.get(DOMAIN, {})
    if not domain_data:
        raise HomeAssistantError("No Guesty integration is currently loaded")
    return next(iter(domain_data.values()))


def _async_setup_service(hass: HomeAssistant) -> None:
    """Register the Guesty services, once."""
    if hass.services.has_service(DOMAIN, SERVICE_SET_RESERVATION_CUSTOM_FIELD):
        return

    async def _async_handle_set_custom_field(call: ServiceCall) -> None:
        reservation_id = call.data["reservation_id"]
        field = call.data["field"]
        # Guesty rejects the write if value is sent as a number rather than
        # a string (confirmed via testing) - always send a string
        # regardless of what type the caller passed in, so numeric access
        # codes etc. don't need to be manually quoted.
        value = str(call.data["value"])

        entry_data = _get_single_entry_data(hass)
        field_id = resolve_field_id(entry_data["reservation_field_defs"], field)
        if field_id is None:
            raise HomeAssistantError(f"Unknown Guesty reservation custom field: {field!r}")

        client: GuestyApiClient = entry_data["client"]
        try:
            await client.async_set_reservation_custom_field(reservation_id, field_id, value)
        except GuestyApiError as err:
            raise HomeAssistantError(f"Failed to update Guesty custom field: {err}") from err

    async def _async_handle_get_reservation(call: ServiceCall) -> dict:
        """Fetch a reservation's current, authoritative details on demand.

        Guesty recommends this pattern: receive the webhook as a pointer,
        then fetch the latest state for actual processing - this exposes
        that same fetch as a standalone action, e.g. to refresh a
        reservation's data again later (closer to check-in) rather than
        only acting on what the original webhook event contained.
        """
        reservation_id = call.data["reservation_id"]
        entry_data = _get_single_entry_data(hass)
        client: GuestyApiClient = entry_data["client"]

        try:
            reservation = await client.async_get_reservation(reservation_id)
        except GuestyApiError as err:
            raise HomeAssistantError(f"Failed to fetch Guesty reservation: {err}") from err

        try:
            reservation["customFields"] = await client.async_get_reservation_custom_fields(
                reservation_id
            )
        except GuestyApiError as err:
            _LOGGER.debug(
                "Could not fetch custom fields for reservation %s: %s", reservation_id, err
            )

        return build_event_data(
            reservation,
            entry_data["listings"],
            entry_data["reservation_field_defs"],
            entry_data["listing_field_defs"],
        )

    async def _async_handle_get_future_reservations(call: ServiceCall) -> dict:
        """List future confirmed reservations for one property.

        Lightweight identifying info only (not full custom fields per
        reservation) - meant for bulk reprocessing: loop over the returned
        reservation_ids and call get_reservation/set_reservation_custom_field
        individually for whichever ones need updating.
        """
        listing_id = call.data["listing_id"]
        entry_data = _get_single_entry_data(hass)
        client: GuestyApiClient = entry_data["client"]

        checkout_from = (dt_util.utcnow() - timedelta(days=1)).strftime(
            "%Y-%m-%dT00:00:00.000Z"
        )
        try:
            reservations = await client.async_search_reservations(
                checkout_from=checkout_from,
                status=RESERVATION_INCLUDED_STATUS,
                listing_id=listing_id,
                sort="checkIn",
            )
        except GuestyApiError as err:
            raise HomeAssistantError(f"Failed to fetch Guesty reservations: {err}") from err

        return {
            "reservations": [
                {
                    "reservation_id": r.get("_id"),
                    "confirmation_code": r.get("confirmationCode"),
                    "guest_name": (r.get("guest") or {}).get("fullName"),
                    "check_in": r.get("checkIn"),
                    "check_out": r.get("checkOut"),
                    "status": r.get("status"),
                }
                for r in reservations
            ]
        }

    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_RESERVATION_CUSTOM_FIELD,
        _async_handle_set_custom_field,
        schema=SET_CUSTOM_FIELD_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_GET_RESERVATION,
        _async_handle_get_reservation,
        schema=GET_RESERVATION_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_GET_FUTURE_RESERVATIONS,
        _async_handle_get_future_reservations,
        schema=GET_FUTURE_RESERVATIONS_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        entry_data = hass.data[DOMAIN].pop(entry.entry_id)
        webhook_id = entry_data.get("webhook_id")
        if webhook_id:
            webhook.async_unregister(hass, webhook_id)
        if not hass.data[DOMAIN]:
            hass.services.async_remove(DOMAIN, SERVICE_SET_RESERVATION_CUSTOM_FIELD)
            hass.services.async_remove(DOMAIN, SERVICE_GET_RESERVATION)
            hass.services.async_remove(DOMAIN, SERVICE_GET_FUTURE_RESERVATIONS)
    return unload_ok
