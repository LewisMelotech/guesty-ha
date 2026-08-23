"""Image platform showing each property's Guesty listing photo."""
from __future__ import annotations

from typing import Any

from homeassistant.components.image import ImageEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .entity import GuestyDeviceEntity


def _listing_picture_url(listing: dict[str, Any]) -> str | None:
    """Best-effort extraction of a listing's cover photo URL.

    Guesty's exact payload shape for pictures isn't guaranteed stable (see
    the general note about not trusting documented shapes at face value),
    so this tries the couple of shapes seen in practice rather than
    assuming one - a singular "picture" object, or the first entry of a
    "pictures" array. Returns None if neither is present.
    """
    picture = listing.get("picture")
    if isinstance(picture, dict):
        url = picture.get("thumbnail") or picture.get("regular") or picture.get("original")
        if url:
            return url

    pictures = listing.get("pictures")
    if isinstance(pictures, list) and pictures and isinstance(pictures[0], dict):
        first = pictures[0]
        return first.get("thumbnail") or first.get("regular") or first.get("original")

    return None


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Guesty listing photo entities, one per property that has one."""
    entry_data = hass.data[DOMAIN][entry.entry_id]
    listings: dict[str, dict[str, Any]] = entry_data["listings"]

    entities = []
    for listing_id, listing in listings.items():
        picture_url = _listing_picture_url(listing)
        if picture_url:
            entities.append(GuestyListingPhoto(hass, listing_id, listing, picture_url))

    async_add_entities(entities)


class GuestyListingPhoto(GuestyDeviceEntity, ImageEntity):
    """The property's cover photo, fetched once at setup since listing

    photos change rarely.
    """

    _attr_has_entity_name = True
    _attr_name = "Photo"

    def __init__(
        self,
        hass: HomeAssistant,
        listing_id: str,
        listing: dict[str, Any],
        picture_url: str,
    ) -> None:
        super().__init__(hass)
        self._attr_unique_id = f"{listing_id}_photo"
        self._attr_image_url = picture_url
        self._attr_image_last_updated = dt_util.utcnow()
        self._init_device(listing_id, listing)
