"""Device automation triggers for the Guesty integration.

Lets the Automation UI's trigger picker offer "New reservation" directly
for a Guesty property (or its "Reservation Info" child device), instead of
requiring the guesty_reservation_new event to be typed in manually via a
generic Event trigger. Also offered, unfiltered, on the bare "Guesty
Integration" device, since the underlying event is genuinely account-wide.
"""
from __future__ import annotations

import logging

import voluptuous as vol

from homeassistant.components.device_automation import DEVICE_TRIGGER_BASE_SCHEMA
from homeassistant.components.homeassistant.triggers import event as event_trigger
from homeassistant.const import CONF_DEVICE_ID, CONF_DOMAIN, CONF_PLATFORM, CONF_TYPE
from homeassistant.core import CALLBACK_TYPE, HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.trigger import TriggerActionType, TriggerInfo
from homeassistant.helpers.typing import ConfigType

from .const import (
    DOMAIN,
    EVENT_RESERVATION_NEW,
    INTEGRATION_DEVICE_ID_PREFIX,
    RESERVATION_DEVICE_ID_SUFFIX,
)

_LOGGER = logging.getLogger(__name__)
_LOGGER.warning("guesty.device_trigger module imported")

TRIGGER_TYPE_RESERVATION_NEW = "reservation_new"
TRIGGER_TYPES = {TRIGGER_TYPE_RESERVATION_NEW}

TRIGGER_SCHEMA = DEVICE_TRIGGER_BASE_SCHEMA.extend(
    {vol.Required(CONF_TYPE): vol.In(TRIGGER_TYPES)}
)

_RESERVATION_DEVICE_SUFFIX = f"_{RESERVATION_DEVICE_ID_SUFFIX}"
_INTEGRATION_DEVICE_PREFIX = f"{INTEGRATION_DEVICE_ID_PREFIX}_"


def _target_for_device(device: dr.DeviceEntry) -> tuple[bool, str | None]:
    """Resolve what a Guesty device's "New reservation" trigger should

    filter on. Returns (is_guesty_device, listing_id) - listing_id is None
    both when the device isn't a Guesty device at all, and for the bare
    "Guesty Integration" device, where None instead means "don't filter,
    match any property's reservations" (distinguished by is_guesty_device).
    """
    for domain, identifier in device.identifiers:
        if domain != DOMAIN:
            continue
        if identifier.startswith(_INTEGRATION_DEVICE_PREFIX):
            return True, None
        if identifier.endswith(_RESERVATION_DEVICE_SUFFIX):
            return True, identifier[: -len(_RESERVATION_DEVICE_SUFFIX)]
        return True, identifier
    return False, None


async def async_get_triggers(hass: HomeAssistant, device_id: str) -> list[dict]:
    """List the device triggers available for a Guesty device."""
    device_registry = dr.async_get(hass)
    device = device_registry.async_get(device_id)
    is_guesty_device = device is not None and _target_for_device(device)[0]
    _LOGGER.warning(
        "guesty.device_trigger.async_get_triggers called for device_id=%s "
        "found_device=%s identifiers=%s is_guesty_device=%s",
        device_id,
        device is not None,
        device.identifiers if device else None,
        is_guesty_device,
    )
    if not is_guesty_device:
        return []

    return [
        {
            CONF_PLATFORM: "device",
            CONF_DEVICE_ID: device_id,
            CONF_DOMAIN: DOMAIN,
            CONF_TYPE: TRIGGER_TYPE_RESERVATION_NEW,
        }
    ]


async def async_attach_trigger(
    hass: HomeAssistant,
    config: ConfigType,
    action: TriggerActionType,
    trigger_info: TriggerInfo,
) -> CALLBACK_TYPE:
    """Attach a device trigger by delegating to the underlying event trigger,

    filtered to this device's own listing_id so only reservations for the
    selected property fire the automation - or unfiltered, for any
    property, when the "Guesty Integration" device was picked instead.
    """
    config = TRIGGER_SCHEMA(config)

    device_registry = dr.async_get(hass)
    device = device_registry.async_get(config[CONF_DEVICE_ID])
    _, listing_id = _target_for_device(device) if device else (False, None)

    event_config_dict: dict = {
        event_trigger.CONF_PLATFORM: "event",
        event_trigger.CONF_EVENT_TYPE: EVENT_RESERVATION_NEW,
    }
    if listing_id:
        event_config_dict[event_trigger.CONF_EVENT_DATA] = {"listing_id": listing_id}

    event_config = event_trigger.TRIGGER_SCHEMA(event_config_dict)
    return await event_trigger.async_attach_trigger(
        hass, event_config, action, trigger_info, platform_type="device"
    )
