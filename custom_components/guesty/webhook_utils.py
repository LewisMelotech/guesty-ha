"""Guesty webhook receiver.

Guesty delivers webhooks via Svix, signed with HMAC-SHA256. See
https://open-api-docs.guesty.com/docs/webhooks-reservations and Svix's own
docs for the signature scheme this implements.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import logging
from typing import Any

from .coordinator import extract_listing_id
from .custom_fields import field_label

_LOGGER = logging.getLogger(__name__)

# Only reservation.new is handled - reservation.updated is intentionally
# ignored (see parse_event_type).
_HANDLED_EVENT_TYPES = {"reservation.new"}


def verify_svix_signature(
    secret: str, svix_id: str, svix_timestamp: str, svix_signature: str, body: bytes
) -> bool:
    """Verify a Svix-delivered webhook's HMAC-SHA256 signature.

    `svix_signature` may contain multiple space-separated `v1,<base64sig>`
    values; a match against any of them is considered valid.
    """
    if not (secret and svix_id and svix_timestamp and svix_signature):
        return False

    try:
        signing_key = (
            base64.b64decode(secret[len("whsec_"):])
            if secret.startswith("whsec_")
            else secret.encode()
        )
    except (binascii.Error, ValueError):
        _LOGGER.warning("Stored Guesty webhook secret is malformed")
        return False

    signed_content = f"{svix_id}.{svix_timestamp}.".encode() + body
    expected = base64.b64encode(
        hmac.new(signing_key, signed_content, hashlib.sha256).digest()
    ).decode()

    for candidate in svix_signature.split():
        _, _, sig_value = candidate.partition(",")
        if sig_value and hmac.compare_digest(expected, sig_value):
            return True
    return False


def is_handled_event(payload: dict[str, Any]) -> bool:
    """Return True if this webhook payload is a `reservation.new` event.

    `reservation.updated` and any other event types are intentionally
    ignored - only new reservations trigger downstream automations.
    """
    event = payload.get("event") or payload.get("type") or payload.get("eventType")
    return str(event) in _HANDLED_EVENT_TYPES


def extract_reservation_id(payload: dict[str, Any]) -> str | None:
    """Pull the reservation ID out of a webhook payload, wherever it lives.

    Guesty sends multiple webhook deliveries per event and recommends
    treating the payload only as a pointer, then re-fetching the
    reservation's current state by ID rather than trusting the delivered
    body - so this only needs to locate an ID, not parse a full object.
    Tries common wrapper keys and ID field names defensively, since the
    envelope shape isn't documented with a concrete example.
    """
    for candidate in (payload, payload.get("data"), payload.get("reservation"), payload.get("payload")):
        if not isinstance(candidate, dict):
            continue
        for key in ("reservationId", "_id", "id"):
            if value := candidate.get(key):
                return value
    return None


def build_event_data(
    reservation: dict[str, Any],
    listings: dict[str, dict[str, Any]],
    reservation_field_defs: list[dict[str, Any]],
    listing_field_defs: list[dict[str, Any]],
) -> dict[str, Any]:
    """Turn a raw reservation payload into Home Assistant event data.

    Includes every reservation- and listing-level custom field by its
    human label (not just ones this integration knows about), so any
    automation can condition on whatever fields exist in the account.
    """
    listing_id = extract_listing_id(reservation)
    listing = listings.get(listing_id, {}) if listing_id else {}

    reservation_labels = {f["fieldId"]: field_label(f) for f in reservation_field_defs}
    listing_labels = {f["fieldId"]: field_label(f) for f in listing_field_defs}

    # Guesty's custom-fields endpoints don't cleanly separate by the field
    # definition's declared object type - a reservation's customFields can
    # include listing-level field values and vice versa. Only keep fields
    # actually known to belong to this object type, rather than falling
    # back to a raw (and misleading) fieldId for the rest.
    reservation_custom_fields = {
        reservation_labels[cf["fieldId"]]: cf.get("value")
        for cf in reservation.get("customFields") or []
        if cf["fieldId"] in reservation_labels
    }
    listing_custom_fields = {
        listing_labels[field_id]: value
        for field_id, value in (listing.get("custom_field_values") or {}).items()
        if field_id in listing_labels
    }

    return {
        "listing_id": listing_id,
        "listing_name": listing.get("nickname") or listing.get("title"),
        "reservation_id": reservation.get("_id") or reservation.get("reservationId"),
        "confirmation_code": reservation.get("confirmationCode"),
        "status": reservation.get("status"),
        "guest_id": reservation.get("guestId") or reservation.get("bookerId"),
        "check_in": reservation.get("checkIn"),
        "check_out": reservation.get("checkOut"),
        "source": reservation.get("source")
        or (reservation.get("integration") or {}).get("platform"),
        "reservation_custom_fields": reservation_custom_fields,
        "listing_custom_fields": listing_custom_fields,
    }
