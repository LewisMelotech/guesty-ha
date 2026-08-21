"""Todo platform for Guesty open tasks."""
from __future__ import annotations

from typing import Any

from homeassistant.components.todo import (
    TodoItem,
    TodoItemStatus,
    TodoListEntity,
    TodoListEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .api import GuestyApiClient, GuestyApiError
from .const import DOMAIN
from .coordinator import GuestyTasksCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Guesty task lists from a config entry."""
    entry_data = hass.data[DOMAIN][entry.entry_id]
    coordinator: GuestyTasksCoordinator = entry_data["tasks_coordinator"]
    client: GuestyApiClient = entry_data["client"]
    listings: dict[str, dict[str, Any]] = entry_data["listings"]

    async_add_entities(
        GuestyTaskList(coordinator, client, listing_id, listing)
        for listing_id, listing in listings.items()
    )


class GuestyTaskList(CoordinatorEntity[GuestyTasksCoordinator], TodoListEntity):
    """A property's open Guesty tasks as a to-do list.

    Read-mostly: checking an item off marks it completed in Guesty (the
    only supported write). Only the base UPDATE_TODO_ITEM feature is
    granted (not the SET_DUE_DATE/SET_DESCRIPTION sub-features), so Home
    Assistant's UI shouldn't expose editing due dates or descriptions -
    just the completion checkbox. Creating, deleting, and reordering
    aren't supported either.
    """

    _attr_has_entity_name = True
    _attr_name = "Tasks"
    _attr_supported_features = TodoListEntityFeature.UPDATE_TODO_ITEM

    def __init__(
        self,
        coordinator: GuestyTasksCoordinator,
        client: GuestyApiClient,
        listing_id: str,
        listing: dict[str, Any],
    ) -> None:
        super().__init__(coordinator)
        self._client = client
        self._listing_id = listing_id
        self._attr_unique_id = f"{listing_id}_task_list"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, listing_id)},
            name=listing.get("nickname") or listing.get("title") or listing_id,
            manufacturer="Guesty",
            model=listing.get("propertyType"),
        )

    @property
    def todo_items(self) -> list[TodoItem] | None:
        tasks = (self.coordinator.data.get(self._listing_id) or {}).get("tasks") or []
        return [_to_todo_item(t) for t in tasks]

    async def async_update_todo_item(self, item: TodoItem) -> None:
        """Mark a task completed in Guesty - the only supported update.

        Un-checking an item isn't written back; the next coordinator
        refresh will restore its real (still-open) state from Guesty.
        """
        if item.status != TodoItemStatus.COMPLETED:
            return

        try:
            await self._client.async_complete_task(item.uid)
        except GuestyApiError as err:
            raise HomeAssistantError(f"Failed to complete Guesty task: {err}") from err

        await self.coordinator.async_request_refresh()


def _to_todo_item(task: dict[str, Any]) -> TodoItem:
    """Build a readable TodoItem - assignee/dates/Guesty ID go in the

    plain-text description, since HA's TodoItem has no structured "extra
    fields". Structured data for automations lives on the Open tasks
    sensor's attributes instead.
    """
    title = (task.get("taskTitle") or {}).get("children") or "Untitled task"
    assignee = (task.get("assignee") or {}).get("assigneeFullName")
    can_start_after = (task.get("canStartAfter") or {}).get("date")
    task_id = task.get("id")

    description_parts = []
    if assignee:
        description_parts.append(f"Assignee: {assignee}")
    if can_start_after:
        description_parts.append(f"Can start after: {can_start_after}")
    if task_id:
        description_parts.append(f"Guesty task ID: {task_id}")

    due = None
    must_finish_before = (task.get("mustFinishBefore") or {}).get("date")
    if must_finish_before:
        due = dt_util.parse_datetime(must_finish_before)

    return TodoItem(
        uid=task_id,
        summary=title,
        status=TodoItemStatus.NEEDS_ACTION,
        due=due,
        description=" · ".join(description_parts) or None,
    )
