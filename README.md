# Guesty for Home Assistant

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)
[![GitHub release](https://img.shields.io/github/v/release/LewisMelotech/guesty-ha)](https://github.com/LewisMelotech/guesty-ha/releases)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

A Home Assistant custom integration for [Guesty](https://www.guesty.com/), a property-management platform for short-term rentals. Each Guesty listing becomes a device, with live reservation, cleaning-status, task, and turnaround data as sensors underneath it - updated both on a poll interval and instantly via Guesty webhooks.

## Features

- One device per Guesty property, plus a nested "Reservation Info" device for stay-specific sensors
- Check-in, check-out, guest name, guest count, nights, and returning-guest status for the current or next reservation
- Cleaning status, turnaround time (gap between checkout and the next check-in), and last checkout time
- Custom fields - both listing- and reservation-level - are auto-discovered from your Guesty account, not hardcoded
- Live updates via a Guesty webhook (`reservation.new`), auto-registered and kept in sync by the integration - no manual webhook setup in Guesty's dashboard
- Guesty Tasks support: a per-property `todo` list, plus "Open tasks" and "Tasks due today" count sensors, with two-way sync for marking tasks complete
- An account-wide "Guesty Integration" device with portfolio-level sensors: check-ins today, check-outs today, same-day turnarounds, and task totals across every property
- A `guesty_reservation_new` event, plus native Home Assistant **device triggers** ("New reservation") selectable directly in the Automation UI - either scoped to one property or account-wide
- Services to fetch a reservation's current data on demand, list a property's future reservations, and write values back to Guesty custom fields
- Configurable polling interval (default 15 minutes)

## Requirements

- A Guesty account with [Open API](https://open-api-docs.guesty.com/) access (a client ID and client secret)
- Home Assistant 2024.4.0 or newer

## Installation

### HACS (recommended)

1. In Home Assistant, go to **HACS → Integrations → ⋮ → Custom repositories**
2. Add `https://github.com/LewisMelotech/guesty-ha` with category **Integration**
3. Find and install **Guesty**, then restart Home Assistant

### Manual

Copy `custom_components/guesty` into your Home Assistant config's `custom_components/` folder, then restart.

## Configuration

Go to **Settings → Devices & services → Add Integration → Guesty** and enter:

- **Client ID** / **Client Secret** - from your Guesty Open API application
- **Webhook base URL** *(optional)* - only needed if Home Assistant sits behind a reverse proxy it doesn't already know its own external URL through
- **Polling interval** *(optional, default 15 minutes)* - how often reservations and tasks are re-fetched; both editable later via the integration's **Configure** option

The integration automatically creates (and keeps up to date) a Guesty-side webhook subscription so `reservation.new` events reach Home Assistant instantly, without waiting for the next poll.

## Entities

**Property device** (e.g. "Daisy")
- Cleaning status
- Turnaround
- Last check-out
- Open tasks / Tasks due today (count sensors)
- Todo list (that property's task backlog)
- Listing-level custom fields

**"`<Property>`: Reservation Info" device**
- Check-in / Check-out
- Guest name, Number of guests, Nights, Returning guest
- Reservation-level custom fields

**"Guesty Integration" device** (account-wide, not tied to one property)
- Check-ins today
- Check-outs today
- Same-day turnarounds
- Total open tasks
- Tasks due today

## Automations

### Event

`guesty_reservation_new` fires on the Home Assistant event bus for every new confirmed reservation, across all properties, with event data:

```
listing_id, listing_name, reservation_id, confirmation_code, status,
guest_id, check_in, check_out, source,
reservation_custom_fields, listing_custom_fields
```

### Device triggers

"New reservation" is available directly in the Automation UI's **Add Trigger → Device** picker - no need to type the event type manually:

- On a property (or its Reservation Info device): fires only for that property's new reservations
- On the **Guesty Integration** device: fires for any property

## Services

| Service | Description |
|---|---|
| `guesty.set_reservation_custom_field` | Write a value to a custom field on a Guesty reservation |
| `guesty.get_reservation` | Fetch a reservation's current, authoritative data on demand |
| `guesty.get_future_reservations` | List a property's future confirmed reservations (e.g. for bulk-reprocessing after a lock or field change) |

## License

[MIT](LICENSE)
