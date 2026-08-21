"""Constants for the Guesty integration."""

DOMAIN = "guesty"

CONF_CLIENT_ID = "client_id"
CONF_CLIENT_SECRET = "client_secret"

# Guesty Open API OAuth2 client-credentials endpoint.
OAUTH_TOKEN_URL = "https://open-api.guesty.com/oauth2/token"
API_BASE_URL = "https://open-api.guesty.com/v1"

OAUTH_SCOPE = "open-api"

# Guesty issues bearer tokens valid for 24h. Refresh a bit early to avoid
# a request failing right as the token expires.
TOKEN_EXPIRY_BUFFER_SECONDS = 120

LISTINGS_PATH = "/listings"

# Guesty's newer /reservations-v3/search is still in beta and silently
# omits some channels (confirmed: Booking.com reservations never appear,
# for any filter). The older /reservations endpoint has full channel
# coverage, so that's what's used for the reservation list/search despite
# its more limited (filters-array) query syntax. The v3-based
# implementation is preserved in the `guesty [v3]` folder alongside this
# component for when v3 matures.
RESERVATIONS_PATH = "/reservations"

# Max page size accepted by /listings and /reservations.
API_PAGE_SIZE = 100

# Guesty's v1 /reservations filters only support a single value per $eq
# comparison (no comma-separated IN like v3's filter[status]=a,b).
RESERVATION_INCLUDED_STATUS = "confirmed"

# Reservation data gets its own child device (e.g. "Daisy: Reservation Info")
# nested under the property device, so property vs. stay-specific sensors
# are visually distinct.
RESERVATION_DEVICE_ID_SUFFIX = "reservation"
RESERVATION_DEVICE_NAME_SUFFIX = "Reservation Info"

# A bare, entity-less device representing the whole Guesty account (not a
# single property) - lets automations use the "New reservation" device
# trigger unfiltered, for any property, alongside the per-property ones.
INTEGRATION_DEVICE_ID_PREFIX = "integration"
INTEGRATION_DEVICE_NAME = "Guesty Integration"

DEFAULT_UPDATE_INTERVAL_MINUTES = 15

# How far back to fetch reservations, so the "Last check-out" sensor can
# still find the most recently completed stay even after a longer vacancy
# between bookings - not just the ~1 day that current-stay detection alone
# would need.
RESERVATION_LOOKBACK_DAYS = 45

# Fired on Home Assistant's event bus when Guesty's webhook delivers a
# reservation.new notification. reservation.updated is intentionally not
# handled. Automations can trigger on this directly
# (event_type: guesty_reservation_new).
EVENT_RESERVATION_NEW = f"{DOMAIN}_reservation_new"

WEBHOOK_SECRET_PATH = "/webhooks-v2/secret"
WEBHOOKS_PATH = "/webhooks"

# Only reservation.new is subscribed to on Guesty's side; see
# webhook_utils.is_handled_event for the receiving-end counterpart.
WEBHOOK_EVENTS = ["reservation.new"]

# Optional per-entry override (set via the Options Flow) for the externally
# reachable base URL to use when registering the webhook with Guesty - needed
# when Home Assistant sits behind a reverse proxy it doesn't know about.
CONF_WEBHOOK_BASE_URL = "webhook_base_url"

# Optional per-entry override (set via the Options Flow) for how often the
# reservations/tasks coordinators poll Guesty. Defaults to
# DEFAULT_UPDATE_INTERVAL_MINUTES.
CONF_UPDATE_INTERVAL_MINUTES = "update_interval_minutes"

SERVICE_SET_RESERVATION_CUSTOM_FIELD = "set_reservation_custom_field"
SERVICE_GET_RESERVATION = "get_reservation"
SERVICE_GET_FUTURE_RESERVATIONS = "get_future_reservations"

TASKS_PATH = "/tasks-open-api/tasks"

TASK_EXCLUDED_STATUSES = ["completed"]

# Guesty's tasks-open-api doesn't document a sort param, so open tasks are
# sorted by createdAt (descending) client-side and capped at this count -
# an account with an unusually large open-task backlog keeps only the most
# recently created ones rather than growing unbounded.
TASK_MAX_COUNT = 200
