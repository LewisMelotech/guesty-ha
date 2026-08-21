"""DataUpdateCoordinator for Guesty reservations."""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import GuestyApiClient, GuestyApiError
from .const import (
    DEFAULT_UPDATE_INTERVAL_MINUTES,
    DOMAIN,
    RESERVATION_INCLUDED_STATUS,
    RESERVATION_LOOKBACK_DAYS,
    TASK_EXCLUDED_STATUSES,
    TASK_MAX_COUNT,
)

_LOGGER = logging.getLogger(__name__)


class GuestyDataUpdateCoordinator(DataUpdateCoordinator[dict[str, dict[str, Any]]]):
    """Fetches, per listing, the current/next reservation (enriched with

    guest details and custom field values), the listing's live cleaning
    status, the check-in of whatever reservation follows the relevant one
    (for computing turnaround time), and the checkout of the most recently
    completed stay. Each entry in `.data` is `{"reservation": ... | None,
    "cleaning_status": {...} | None, "next_check_in": "..." | None,
    "last_check_out": "..." | None}`.

    Also computes `account_summary` (a plain attribute, not part of
    `.data`) - portfolio-wide counts of check-ins/check-outs/same-day
    turnarounds today, used by sensors on the bare "Guesty Integration"
    device rather than any single property's.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        client: GuestyApiClient,
        listing_ids: list[str],
        update_interval_minutes: int = DEFAULT_UPDATE_INTERVAL_MINUTES,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(minutes=update_interval_minutes),
        )
        self._client = client
        self._listing_ids = listing_ids
        self.account_summary: dict[str, int] = {
            "check_ins_today": 0,
            "check_outs_today": 0,
            "same_day_turnarounds": 0,
        }

    async def _async_update_data(self) -> dict[str, dict[str, Any]]:
        """Fetch reservations and listings, and group/enrich per listing."""
        # A loose lower bound - the /reservations endpoint's $gt isn't
        # inclusive, so this is deliberately a little early; exact current-
        # vs-upcoming filtering happens in _pick_relevant against real "now".
        # Wider than current-stay detection alone would need, so the most
        # recently completed stay (for "last checkout") stays discoverable
        # across longer vacancies between bookings too.
        checkout_from = (
            dt_util.utcnow() - timedelta(days=RESERVATION_LOOKBACK_DAYS)
        ).strftime("%Y-%m-%dT00:00:00.000Z")

        try:
            reservations = await self._client.async_search_reservations(
                checkout_from=checkout_from,
                status=RESERVATION_INCLUDED_STATUS,
                sort="checkIn",
            )
        except GuestyApiError as err:
            raise UpdateFailed(f"Error fetching Guesty reservations: {err}") from err

        try:
            listings = await self._client.async_get_listings()
        except GuestyApiError as err:
            raise UpdateFailed(f"Error fetching Guesty listings: {err}") from err

        cleaning_status_by_listing = {
            listing["_id"]: listing.get("cleaningStatus")
            or (listing.get("pms") or {}).get("cleaningStatus")
            for listing in listings
        }

        by_listing: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for reservation in reservations:
            listing_id = extract_listing_id(reservation)
            if listing_id is None:
                _LOGGER.warning(
                    "Could not determine listing ID for reservation %s; "
                    "top-level keys were: %s",
                    reservation.get("_id"),
                    sorted(reservation.keys()),
                )
                continue
            by_listing[listing_id].append(reservation)

        now = dt_util.utcnow()
        self.account_summary = self._compute_account_summary(by_listing, now)

        result: dict[str, dict[str, Any]] = {}
        for listing_id in self._listing_ids:
            listing_reservations = by_listing.get(listing_id, [])
            reservation, is_current = self._pick_relevant(listing_reservations, now)

            next_check_in = None
            if reservation is not None:
                next_check_in = self._find_next_check_in(listing_reservations, reservation)
                reservation = dict(reservation)
                reservation["is_current"] = is_current
                await self._async_enrich_guest(reservation)
                await self._async_enrich_custom_fields(reservation)

            result[listing_id] = {
                "reservation": reservation,
                "cleaning_status": cleaning_status_by_listing.get(listing_id),
                "next_check_in": next_check_in,
                "last_check_out": self._find_last_check_out(listing_reservations, now),
            }

        return result

    @staticmethod
    def _pick_relevant(
        reservations: list[dict[str, Any]], now: datetime
    ) -> tuple[dict[str, Any] | None, bool]:
        """Return the current-stay reservation, or else the soonest upcoming one.

        Returns a (reservation, is_current) tuple; reservation is None if
        there is no current or upcoming stay.
        """
        upcoming: list[dict[str, Any]] = []
        for reservation in reservations:
            check_in = dt_util.parse_datetime(reservation["checkIn"])
            check_out = dt_util.parse_datetime(reservation["checkOut"])
            if check_in <= now < check_out:
                return reservation, True
            if check_in > now:
                upcoming.append(reservation)

        if not upcoming:
            return None, False

        return min(upcoming, key=lambda r: dt_util.parse_datetime(r["checkIn"])), False

    @staticmethod
    def _find_next_check_in(
        reservations: list[dict[str, Any]], after: dict[str, Any]
    ) -> str | None:
        """Find the check-in of the reservation immediately following

        `after`'s checkout - used to compute the property's turnaround
        time (the gap between one stay ending and the next beginning).
        """
        after_id = after.get("_id")
        after_check_out = dt_util.parse_datetime(after["checkOut"])

        following = [
            r
            for r in reservations
            if r.get("_id") != after_id
            and dt_util.parse_datetime(r["checkIn"]) >= after_check_out
        ]
        if not following:
            return None

        return min(following, key=lambda r: dt_util.parse_datetime(r["checkIn"]))["checkIn"]

    @staticmethod
    def _compute_account_summary(
        by_listing: dict[str, list[dict[str, Any]]], now: datetime
    ) -> dict[str, int]:
        """Count today's check-ins/check-outs/same-day turnarounds across

        every property. Scans every fetched reservation per listing (not
        just whichever one is "relevant" for that listing's own sensors),
        since a same-day turnover involves two distinct reservations for
        the same listing - one checking out, a different one checking in.
        """
        today = dt_util.as_local(now).date()
        check_in_listings: set[str] = set()
        check_out_listings: set[str] = set()

        for listing_id, reservations in by_listing.items():
            for reservation in reservations:
                check_in = dt_util.parse_datetime(reservation["checkIn"])
                check_out = dt_util.parse_datetime(reservation["checkOut"])
                if check_in is not None and dt_util.as_local(check_in).date() == today:
                    check_in_listings.add(listing_id)
                if check_out is not None and dt_util.as_local(check_out).date() == today:
                    check_out_listings.add(listing_id)

        return {
            "check_ins_today": len(check_in_listings),
            "check_outs_today": len(check_out_listings),
            "same_day_turnarounds": len(check_in_listings & check_out_listings),
        }

    @staticmethod
    def _find_last_check_out(
        reservations: list[dict[str, Any]], now: datetime
    ) -> str | None:
        """Find the checkout of the most recently completed stay - the

        newest checkOut that's already in the past, independent of whichever
        reservation is "relevant" (current/next upcoming), since after a
        vacancy that stay could be a different reservation entirely.
        """
        past = [
            r for r in reservations if dt_util.parse_datetime(r["checkOut"]) <= now
        ]
        if not past:
            return None

        return max(past, key=lambda r: dt_util.parse_datetime(r["checkOut"]))["checkOut"]

    async def _async_enrich_guest(self, reservation: dict[str, Any]) -> None:
        """Attach guest details (full name, returning-guest status)."""
        guest_id = reservation.get("guestId")
        if not guest_id:
            return

        try:
            reservation["guest"] = await self._client.async_get_guest(guest_id)
        except GuestyApiError as err:
            _LOGGER.debug("Could not fetch Guesty guest %s: %s", guest_id, err)

    async def _async_enrich_custom_fields(self, reservation: dict[str, Any]) -> None:
        """Attach custom field values, keyed by raw Guesty `fieldId`.

        Sensors map fieldId to a human label using the account's custom
        field definitions.
        """
        reservation_id = reservation.get("_id")
        if not reservation_id:
            return

        try:
            fields = await self._client.async_get_reservation_custom_fields(
                reservation_id
            )
        except GuestyApiError as err:
            _LOGGER.debug(
                "Could not fetch custom fields for reservation %s: %s",
                reservation_id,
                err,
            )
            fields = []

        reservation["custom_field_values"] = {
            cf["fieldId"]: cf.get("value") for cf in fields
        }


def extract_listing_id(reservation: dict[str, Any]) -> str | None:
    """The listing ID sits directly on the reservation via /reservations."""
    return reservation.get("listingId")


class GuestyTasksCoordinator(DataUpdateCoordinator[dict[str, dict[str, Any]]]):
    """Fetches open (non-completed) Guesty tasks, grouped per listing.

    Each entry in `.data` is `{"tasks": [...], "open_count": N,
    "due_today_count": N}`. Tasks are sorted by createdAt (descending) and
    capped at TASK_MAX_COUNT overall, since this endpoint doesn't support
    server-side sorting - an account with an unusually large open-task
    backlog keeps only the most recently created tasks rather than growing
    unbounded.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        client: GuestyApiClient,
        listing_ids: list[str],
        update_interval_minutes: int = DEFAULT_UPDATE_INTERVAL_MINUTES,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_tasks",
            update_interval=timedelta(minutes=update_interval_minutes),
        )
        self._client = client
        self._listing_ids = listing_ids

    async def _async_update_data(self) -> dict[str, dict[str, Any]]:
        try:
            tasks = await self._client.async_get_tasks(
                exclude_statuses=TASK_EXCLUDED_STATUSES
            )
        except GuestyApiError as err:
            raise UpdateFailed(f"Error fetching Guesty tasks: {err}") from err

        tasks.sort(key=lambda t: t.get("createdAt") or "", reverse=True)
        if len(tasks) > TASK_MAX_COUNT:
            _LOGGER.warning(
                "Guesty account has more than %d open tasks; keeping only "
                "the %d most recently created",
                TASK_MAX_COUNT,
                TASK_MAX_COUNT,
            )
            tasks = tasks[:TASK_MAX_COUNT]

        by_listing: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for task in tasks:
            listing_id = (task.get("listing") or {}).get("listingId")
            if listing_id:
                by_listing[listing_id].append(task)

        today = dt_util.now().date()
        result: dict[str, dict[str, Any]] = {}
        for listing_id in self._listing_ids:
            listing_tasks = by_listing.get(listing_id, [])
            due_today = sum(1 for t in listing_tasks if _is_due_today(t, today))
            result[listing_id] = {
                "tasks": listing_tasks,
                "open_count": len(listing_tasks),
                "due_today_count": due_today,
            }

        return result


def _is_due_today(task: dict[str, Any], today: Any) -> bool:
    due = (task.get("mustFinishBefore") or {}).get("date")
    if not due:
        return False
    parsed = dt_util.parse_datetime(due)
    if parsed is None:
        return False
    return dt_util.as_local(parsed).date() == today
