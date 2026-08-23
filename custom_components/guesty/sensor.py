"""Sensor platform for Guesty reservations and property custom fields."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .coordinator import GuestyDataUpdateCoordinator, GuestyTasksCoordinator
from .custom_fields import field_label, field_slug
from .entity import GuestyDeviceEntity


def _guest_name(reservation: dict[str, Any]) -> Any:
    guest = reservation.get("guest") or {}
    return guest.get("fullName") or reservation.get("guestId")


def _nights(reservation: dict[str, Any]) -> Any:
    check_in = dt_util.parse_datetime(reservation["checkIn"])
    check_out = dt_util.parse_datetime(reservation["checkOut"])
    return (check_out.date() - check_in.date()).days


def _returning_guest(reservation: dict[str, Any]) -> Any:
    guest = reservation.get("guest") or {}
    returning = guest.get("returningGuest")
    if returning is None:
        return None
    return "yes" if returning else "no"


def _check_in_attributes(reservation: dict[str, Any]) -> dict[str, Any]:
    return {
        "stay_status": "current" if reservation.get("is_current") else "upcoming",
        "status": reservation.get("status"),
        "confirmation_code": reservation.get("confirmationCode"),
        "source": reservation.get("source")
        or (reservation.get("integration") or {}).get("platform"),
    }


@dataclass(frozen=True, kw_only=True)
class GuestyReservationSensorDescription(SensorEntityDescription):
    """Describes a sensor derived from a property's relevant reservation."""

    value_fn: Callable[[dict[str, Any]], Any]
    attributes_fn: Callable[[dict[str, Any]], dict[str, Any]] | None = None


RESERVATION_SENSOR_DESCRIPTIONS: tuple[GuestyReservationSensorDescription, ...] = (
    GuestyReservationSensorDescription(
        key="reservation_id",
        name="ID",
        icon="mdi:identifier",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda r: r.get("_id") or r.get("reservationId"),
    ),
    GuestyReservationSensorDescription(
        key="check_in",
        name="Check-in",
        device_class=SensorDeviceClass.TIMESTAMP,
        icon="mdi:calendar-arrow-right",
        value_fn=lambda r: dt_util.parse_datetime(r["checkIn"]),
        attributes_fn=_check_in_attributes,
    ),
    GuestyReservationSensorDescription(
        key="check_out",
        name="Check-out",
        device_class=SensorDeviceClass.TIMESTAMP,
        icon="mdi:calendar-arrow-left",
        value_fn=lambda r: dt_util.parse_datetime(r["checkOut"]),
    ),
    GuestyReservationSensorDescription(
        key="guest_name",
        name="Guest name",
        icon="mdi:account",
        value_fn=_guest_name,
    ),
    GuestyReservationSensorDescription(
        key="number_of_guests",
        name="Number of guests",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:account-group",
        value_fn=lambda r: r.get("guestsCount"),
    ),
    GuestyReservationSensorDescription(
        key="nights",
        name="Nights",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:weather-night",
        value_fn=_nights,
    ),
    GuestyReservationSensorDescription(
        key="returning_guest",
        name="Returning guest",
        icon="mdi:star",
        value_fn=_returning_guest,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Guesty sensors from a config entry."""
    entry_data = hass.data[DOMAIN][entry.entry_id]
    coordinator: GuestyDataUpdateCoordinator = entry_data["coordinator"]
    tasks_coordinator: GuestyTasksCoordinator = entry_data["tasks_coordinator"]
    listings: dict[str, dict[str, Any]] = entry_data["listings"]
    listing_field_defs: list[dict[str, Any]] = entry_data["listing_field_defs"]
    reservation_field_defs: list[dict[str, Any]] = entry_data["reservation_field_defs"]

    entities: list[SensorEntity] = [
        GuestyCheckInsTodaySensor(coordinator, entry.entry_id),
        GuestyCheckOutsTodaySensor(coordinator, entry.entry_id),
        GuestySameDayTurnaroundsSensor(coordinator, entry.entry_id),
        GuestyTotalOpenTasksSensor(tasks_coordinator, entry.entry_id),
        GuestyTotalTasksDueTodaySensor(tasks_coordinator, entry.entry_id),
    ]
    for listing_id, listing in listings.items():
        entities.append(GuestyListingIdSensor(listing_id, listing))
        entities.append(GuestyCleaningStatusSensor(coordinator, listing_id, listing))
        entities.append(GuestyTurnaroundSensor(coordinator, listing_id, listing))
        entities.append(GuestyLastCheckOutSensor(coordinator, listing_id, listing))
        entities.append(GuestyOpenTasksSensor(tasks_coordinator, listing_id, listing))
        entities.append(GuestyTasksDueTodaySensor(tasks_coordinator, listing_id, listing))
        for description in RESERVATION_SENSOR_DESCRIPTIONS:
            entities.append(
                GuestyReservationSensor(coordinator, listing_id, listing, description)
            )
        for field in reservation_field_defs:
            entities.append(
                GuestyReservationCustomFieldSensor(coordinator, listing_id, listing, field)
            )
        for field in listing_field_defs:
            entities.append(GuestyListingCustomFieldSensor(listing_id, listing, field))

    async_add_entities(entities)


class GuestyReservationSensor(
    GuestyDeviceEntity, CoordinatorEntity[GuestyDataUpdateCoordinator], SensorEntity
):
    """A single data point (check-in, guest name, ...) for a property's

    current or next reservation.
    """

    _attr_has_entity_name = True
    entity_description: GuestyReservationSensorDescription

    def __init__(
        self,
        coordinator: GuestyDataUpdateCoordinator,
        listing_id: str,
        listing: dict[str, Any],
        description: GuestyReservationSensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._listing_id = listing_id
        self._attr_unique_id = f"{listing_id}_{description.key}"
        self._init_reservation_device(listing_id, listing)

    @property
    def _reservation(self) -> dict[str, Any] | None:
        return (self.coordinator.data.get(self._listing_id) or {}).get("reservation")

    @property
    def native_value(self) -> Any:
        reservation = self._reservation
        if reservation is None:
            return None
        return self.entity_description.value_fn(reservation)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        reservation = self._reservation
        if reservation is None or self.entity_description.attributes_fn is None:
            return {}
        return self.entity_description.attributes_fn(reservation)


class GuestyReservationCustomFieldSensor(
    GuestyDeviceEntity, CoordinatorEntity[GuestyDataUpdateCoordinator], SensorEntity
):
    """A reservation-level custom field value (e.g. "Reservation Access

    Code"), auto-discovered from the Guesty account - no hardcoded field
    names, so this works for any account's own custom fields.
    """

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: GuestyDataUpdateCoordinator,
        listing_id: str,
        listing: dict[str, Any],
        field: dict[str, Any],
    ) -> None:
        super().__init__(coordinator)
        self._listing_id = listing_id
        self._field_id = field["fieldId"]
        label = field_label(field)
        self._attr_name = label
        self._attr_unique_id = f"{listing_id}_cf_{field_slug(label)}"
        self._init_reservation_device(listing_id, listing)

    @property
    def native_value(self) -> Any:
        reservation = (self.coordinator.data.get(self._listing_id) or {}).get("reservation")
        if reservation is None:
            return None
        return (reservation.get("custom_field_values") or {}).get(self._field_id)


# Guesty's raw cleaningStatus value is camelCase - HA's translation key
# schema requires lowercase snake_case ([a-z0-9-_]+), so the value is
# normalized before being returned as the entity's state.
_CLEANING_STATUS_VALUE_MAP = {
    "clean": "clean",
    "waitingForInspection": "waiting_for_inspection",
    "dirty": "dirty",
    "unknown": "unknown",
}


class GuestyCleaningStatusSensor(
    GuestyDeviceEntity, CoordinatorEntity[GuestyDataUpdateCoordinator], SensorEntity
):
    """The property's current housekeeping/cleaning status, polled alongside

    reservations since it changes throughout the day as cleaners work.
    Lives on the property device, not the reservation device, since it
    isn't tied to a specific stay.
    """

    _attr_has_entity_name = True
    _attr_name = "Cleaning status"
    _attr_icon = "mdi:broom"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_translation_key = "cleaning_status"
    # Confirmed values from a live account; if Guesty ever returns something
    # else, HA will log it as an unrecognized enum value rather than error.
    _attr_options = ["clean", "waiting_for_inspection", "dirty", "unknown"]

    def __init__(
        self,
        coordinator: GuestyDataUpdateCoordinator,
        listing_id: str,
        listing: dict[str, Any],
    ) -> None:
        super().__init__(coordinator)
        self._listing_id = listing_id
        self._attr_unique_id = f"{listing_id}_cleaning_status"
        self._init_device(listing_id, listing)

    @property
    def _cleaning_status(self) -> dict[str, Any]:
        return (self.coordinator.data.get(self._listing_id) or {}).get(
            "cleaning_status"
        ) or {}

    @property
    def native_value(self) -> Any:
        raw = self._cleaning_status.get("value")
        if raw is None:
            return None
        return _CLEANING_STATUS_VALUE_MAP.get(raw, raw)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        updated_at = self._cleaning_status.get("updatedAt")
        return {"updated_at": updated_at} if updated_at else {}


class GuestyTurnaroundSensor(
    GuestyDeviceEntity, CoordinatorEntity[GuestyDataUpdateCoordinator], SensorEntity
):
    """Hours between the relevant reservation's checkout and the next

    reservation's check-in - how much time is available to turn the
    property around (clean, maintain, etc.) before the next guest arrives.
    Lives on the property device since it spans two reservations, not one.
    `None` when there's no reservation after the relevant one to compute
    a gap against.
    """

    _attr_has_entity_name = True
    _attr_name = "Turnaround"
    _attr_icon = "mdi:autorenew"
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.HOURS
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 1

    def __init__(
        self,
        coordinator: GuestyDataUpdateCoordinator,
        listing_id: str,
        listing: dict[str, Any],
    ) -> None:
        super().__init__(coordinator)
        self._listing_id = listing_id
        self._attr_unique_id = f"{listing_id}_turnaround"
        self._init_device(listing_id, listing)

    @property
    def _data(self) -> dict[str, Any]:
        return self.coordinator.data.get(self._listing_id) or {}

    @property
    def _bounds(self) -> tuple[Any, Any] | None:
        reservation = self._data.get("reservation")
        next_check_in = self._data.get("next_check_in")
        if reservation is None or not next_check_in:
            return None
        check_out = dt_util.parse_datetime(reservation["checkOut"])
        check_in = dt_util.parse_datetime(next_check_in)
        if check_out is None or check_in is None:
            return None
        return check_out, check_in

    @property
    def native_value(self) -> Any:
        bounds = self._bounds
        if bounds is None:
            return None
        check_out, check_in = bounds
        return round((check_in - check_out).total_seconds() / 3600, 1)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        bounds = self._bounds
        if bounds is None:
            return {}
        check_out, check_in = bounds
        return {"check_out": check_out.isoformat(), "next_check_in": check_in.isoformat()}


class GuestyLastCheckOutSensor(
    GuestyDeviceEntity, CoordinatorEntity[GuestyDataUpdateCoordinator], SensorEntity
):
    """Checkout time of the property's most recently completed stay.

    Lives on the property device (not the reservation device) since it
    isn't tied to whichever reservation is currently "relevant" - after a
    vacancy, the last checkout belongs to a different, already-finished
    reservation. `None` if no completed stay is within the coordinator's
    lookback window.
    """

    _attr_has_entity_name = True
    _attr_name = "Last check-out"
    _attr_icon = "mdi:calendar-check"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(
        self,
        coordinator: GuestyDataUpdateCoordinator,
        listing_id: str,
        listing: dict[str, Any],
    ) -> None:
        super().__init__(coordinator)
        self._listing_id = listing_id
        self._attr_unique_id = f"{listing_id}_last_check_out"
        self._init_device(listing_id, listing)

    @property
    def native_value(self) -> Any:
        last_check_out = (self.coordinator.data.get(self._listing_id) or {}).get(
            "last_check_out"
        )
        if not last_check_out:
            return None
        return dt_util.parse_datetime(last_check_out)


class GuestyCheckInsTodaySensor(
    GuestyDeviceEntity, CoordinatorEntity[GuestyDataUpdateCoordinator], SensorEntity
):
    """Number of properties, across the whole account, with a guest

    checking in today. Lives on the bare "Guesty Integration" device
    rather than any single property's.
    """

    _attr_has_entity_name = True
    _attr_name = "Check-ins today"
    _attr_icon = "mdi:calendar-arrow-right"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: GuestyDataUpdateCoordinator, entry_id: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry_id}_check_ins_today"
        self._init_integration_device(entry_id)

    @property
    def native_value(self) -> Any:
        return self.coordinator.account_summary.get("check_ins_today", 0)


class GuestyCheckOutsTodaySensor(
    GuestyDeviceEntity, CoordinatorEntity[GuestyDataUpdateCoordinator], SensorEntity
):
    """Number of properties, across the whole account, with a guest

    checking out today.
    """

    _attr_has_entity_name = True
    _attr_name = "Check-outs today"
    _attr_icon = "mdi:calendar-arrow-left"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: GuestyDataUpdateCoordinator, entry_id: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry_id}_check_outs_today"
        self._init_integration_device(entry_id)

    @property
    def native_value(self) -> Any:
        return self.coordinator.account_summary.get("check_outs_today", 0)


class GuestySameDayTurnaroundsSensor(
    GuestyDeviceEntity, CoordinatorEntity[GuestyDataUpdateCoordinator], SensorEntity
):
    """Number of properties, across the whole account, checking one guest

    out and a different guest in on the same day.
    """

    _attr_has_entity_name = True
    _attr_name = "Same-day turnarounds"
    _attr_icon = "mdi:swap-horizontal-bold"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: GuestyDataUpdateCoordinator, entry_id: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry_id}_same_day_turnarounds"
        self._init_integration_device(entry_id)

    @property
    def native_value(self) -> Any:
        return self.coordinator.account_summary.get("same_day_turnarounds", 0)


class GuestyTotalOpenTasksSensor(
    GuestyDeviceEntity, CoordinatorEntity[GuestyTasksCoordinator], SensorEntity
):
    """Sum of open Guesty tasks across every property."""

    _attr_has_entity_name = True
    _attr_name = "Total open tasks"
    _attr_icon = "mdi:clipboard-list"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: GuestyTasksCoordinator, entry_id: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry_id}_total_open_tasks"
        self._init_integration_device(entry_id)

    @property
    def native_value(self) -> Any:
        return sum(v.get("open_count", 0) for v in self.coordinator.data.values())


class GuestyTotalTasksDueTodaySensor(
    GuestyDeviceEntity, CoordinatorEntity[GuestyTasksCoordinator], SensorEntity
):
    """Sum of open Guesty tasks due today across every property."""

    _attr_has_entity_name = True
    _attr_name = "Tasks due today"
    _attr_icon = "mdi:clipboard-alert"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: GuestyTasksCoordinator, entry_id: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry_id}_total_tasks_due_today"
        self._init_integration_device(entry_id)

    @property
    def native_value(self) -> Any:
        return sum(v.get("due_today_count", 0) for v in self.coordinator.data.values())


class GuestyOpenTasksSensor(
    GuestyDeviceEntity, CoordinatorEntity[GuestyTasksCoordinator], SensorEntity
):
    """Count of open (non-completed) Guesty tasks for this property.

    The full task list (title, status, assignee, dates, Guesty task ID) is
    exposed as an attribute for automation use - the readable checklist
    experience lives on the companion `todo` entity instead.
    """

    _attr_has_entity_name = True
    _attr_name = "Open tasks"
    _attr_icon = "mdi:clipboard-list"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self,
        coordinator: GuestyTasksCoordinator,
        listing_id: str,
        listing: dict[str, Any],
    ) -> None:
        super().__init__(coordinator)
        self._listing_id = listing_id
        self._attr_unique_id = f"{listing_id}_open_tasks"
        self._init_device(listing_id, listing)

    @property
    def _tasks(self) -> list[dict[str, Any]]:
        return (self.coordinator.data.get(self._listing_id) or {}).get("tasks") or []

    @property
    def native_value(self) -> Any:
        return (self.coordinator.data.get(self._listing_id) or {}).get("open_count", 0)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "tasks": [
                {
                    "task_id": t.get("id"),
                    "title": (t.get("taskTitle") or {}).get("children"),
                    "status": (t.get("status") or {}).get("status"),
                    "assignee": (t.get("assignee") or {}).get("assigneeFullName"),
                    "can_start_after": (t.get("canStartAfter") or {}).get("date"),
                    "must_finish_before": (t.get("mustFinishBefore") or {}).get("date"),
                }
                for t in self._tasks
            ]
        }


class GuestyTasksDueTodaySensor(
    GuestyDeviceEntity, CoordinatorEntity[GuestyTasksCoordinator], SensorEntity
):
    """Count of open Guesty tasks due (mustFinishBefore) today for this

    property.
    """

    _attr_has_entity_name = True
    _attr_name = "Tasks due today"
    _attr_icon = "mdi:clipboard-alert"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self,
        coordinator: GuestyTasksCoordinator,
        listing_id: str,
        listing: dict[str, Any],
    ) -> None:
        super().__init__(coordinator)
        self._listing_id = listing_id
        self._attr_unique_id = f"{listing_id}_tasks_due_today"
        self._init_device(listing_id, listing)

    @property
    def native_value(self) -> Any:
        return (self.coordinator.data.get(self._listing_id) or {}).get(
            "due_today_count", 0
        )


class GuestyListingIdSensor(GuestyDeviceEntity, SensorEntity):
    """The property's Guesty listing ID, exposed as a plain entity for

    easy reference in automations/templates without needing to inspect
    another entity's unique_id or wait for a webhook event.
    """

    _attr_has_entity_name = True
    _attr_name = "ID"
    _attr_icon = "mdi:identifier"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_should_poll = False

    def __init__(self, listing_id: str, listing: dict[str, Any]) -> None:
        self._attr_unique_id = f"{listing_id}_listing_id"
        self._attr_native_value = listing_id
        self._init_device(listing_id, listing)


class GuestyListingCustomFieldSensor(GuestyDeviceEntity, SensorEntity):
    """A listing-level custom field value (e.g. "Has Smart Lock"),

    auto-discovered from the Guesty account. Fetched once at setup rather
    than polled, since these change rarely.
    """

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_icon = "mdi:tag-outline"

    def __init__(
        self,
        listing_id: str,
        listing: dict[str, Any],
        field: dict[str, Any],
    ) -> None:
        field_id = field["fieldId"]
        label = field_label(field)
        self._attr_name = label
        self._attr_unique_id = f"{listing_id}_listing_cf_{field_slug(label)}"
        self._attr_native_value = (listing.get("custom_field_values") or {}).get(field_id)
        self._init_device(listing_id, listing)
