"""Helpers for turning Guesty's account-level custom field definitions into

Home Assistant entity names/keys, without hardcoding any specific field.
"""
from __future__ import annotations

import re
from typing import Any


def field_label(field: dict[str, Any]) -> str:
    """Pick the human-readable name for a custom field definition.

    Guesty's `/accounts/{id}/custom-fields` endpoint is inconsistent about
    whether `key` or `displayName` holds the human label (e.g. "Reservation
    Access Code") vs. the snake_case machine name (e.g.
    "reservation_access_code") - prefer whichever looks human-formatted.
    """
    key = field.get("key") or ""
    display_name = field.get("displayName") or ""

    if _looks_human_formatted(key):
        return key
    if _looks_human_formatted(display_name):
        return display_name
    return key or display_name or field.get("fieldId", "")


def _looks_human_formatted(value: str) -> bool:
    return bool(value) and (" " in value or value != value.lower())


def field_slug(label: str) -> str:
    """Turn a human-readable field label into a safe entity key fragment."""
    slug = re.sub(r"[^a-z0-9]+", "_", label.strip().lower())
    return slug.strip("_") or "field"


def normalize_field_name(name: str) -> str:
    """Normalize a field name for matching regardless of casing or whether

    it's written with underscores (e.g. `reservation_access_code`) or as a
    human label (e.g. `Reservation Access Code`).
    """
    return name.strip().lower().replace("_", " ")


def resolve_field_id(field_defs: list[dict[str, Any]], field: str) -> str | None:
    """Resolve a user-supplied field name/ID to a Guesty `fieldId`.

    Matches against each field's human label first (case/format
    insensitive); falls back to treating `field` as a raw fieldId.
    """
    normalized = normalize_field_name(field)
    for definition in field_defs:
        if normalize_field_name(field_label(definition)) == normalized:
            return definition["fieldId"]
    if any(definition.get("fieldId") == field for definition in field_defs):
        return field
    return None
