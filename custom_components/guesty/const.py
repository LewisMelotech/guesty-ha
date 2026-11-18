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

# Reservations v3. Guesty retires the legacy /reservations endpoints (and
# the reservation.new / reservation.updated webhooks) on 1 March 2027.
# GET RESERVATIONS_PATH?reservationIds=[<id>] fetches by ID (bare array, max
# 10 IDs); RESERVATIONS_SEARCH_PATH lists/filters with filter[...] params
# and returns {"results": [...], "pagination": {"hasMore": ...}}.
RESERVATIONS_PATH = "/reservations-v3"
RESERVATIONS_SEARCH_PATH = "/reservations-v3/search"

# Max page size accepted by /listings and /reservations-v3/search.
API_PAGE_SIZE = 100

# Sent as filter[status]; v3 accepts a comma-separated list meaning "any of".
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
# reservation.created.v2 notification. reservation.updated.v2 is
# intentionally not handled. Automations can trigger on this directly
# (event_type: guesty_reservation_new).
EVENT_RESERVATION_NEW = f"{DOMAIN}_reservation_new"

WEBHOOK_SECRET_PATH = "/webhooks-v2/secret"
WEBHOOKS_PATH = "/webhooks"

# Only reservation.created.v2 (the replacement for the legacy
# reservation.new) is subscribed to on Guesty's side. The subscription is
# PUT on every setup, so existing installs switch over on their next
# reload. See webhook_utils.is_handled_event for the receiving end.
WEBHOOK_EVENTS = ["reservation.created.v2"]

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
