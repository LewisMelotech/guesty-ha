"""Shared device-linking mixin for Guesty entities across platforms."""
from __future__ import annotations

from typing import Any

from homeassistant.helpers.entity import DeviceInfo

from .const import (
    DOMAIN,
    INTEGRATION_DEVICE_ID_PREFIX,
    INTEGRATION_DEVICE_NAME,
    RESERVATION_DEVICE_ID_SUFFIX,
    RESERVATION_DEVICE_NAME_SUFFIX,
)


class GuestyDeviceEntity:
    """Mixin providing DeviceInfo for the property device, its

    "Reservation Info" child device, or the bare "Guesty Integration"
    device - shared across the sensor, image, and todo platforms.
    """

    def _init_device(self, listing_id: str, listing: dict[str, Any]) -> None:
        """Attach to the property device itself (e.g. "Daisy")."""
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, listing_id)},
            name=listing.get("nickname") or listing.get("title") or listing_id,
            manufacturer="Guesty",
            model=listing.get("propertyType"),
        )

    def _init_reservation_device(self, listing_id: str, listing: dict[str, Any]) -> None:
        """Attach to the "X: Reservation Info" device nested under the property."""
        property_name = listing.get("nickname") or listing.get("title") or listing_id
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{listing_id}_{RESERVATION_DEVICE_ID_SUFFIX}")},
            name=f"{property_name}: {RESERVATION_DEVICE_NAME_SUFFIX}",
            manufacturer="Guesty",
            via_device=(DOMAIN, listing_id),
        )

    def _init_integration_device(self, entry_id: str) -> None:
        """Attach to the bare, per-account "Guesty Integration" device."""
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{INTEGRATION_DEVICE_ID_PREFIX}_{entry_id}")},
            name=INTEGRATION_DEVICE_NAME,
            manufacturer="Guesty",
        )
