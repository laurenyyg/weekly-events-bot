"""
Luma event fetcher for lu.ma/vancouver.

Uses Luma's internal discovery API (reverse-engineered from the web app) to
fetch upcoming public events in Vancouver.  No API key required.
"""

import logging
from datetime import date, datetime
from zoneinfo import ZoneInfo

import requests

from .base import BaseEventFetcher, Event

logger = logging.getLogger(__name__)

_DISCOVER_URL = "https://api.lu.ma/discover/get-paginated-events"
_DETAIL_URL = "https://api.lu.ma/event/get"
_EVENT_BASE = "https://lu.ma"
_VANCOUVER_TZ = ZoneInfo("America/Vancouver")

_HEADERS = {
    "accept": "application/json, text/plain, */*",
    "accept-language": "en-US,en;q=0.9",
    "origin": "https://lu.ma",
    "referer": "https://lu.ma/vancouver",
    "x-luma-web-url": "https://lu.ma/vancouver",
    "user-agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0.0.0 Safari/537.36"
    ),
}

# Cities included in the digest. Online events are always included.
# "University Endowment Lands" is the official municipality name for UBC.
_ALLOWED_CITIES = {
    "Vancouver",
    "Burnaby",
    "Richmond",
    "University Endowment Lands",
}


def _is_in_coverage_area(event: dict) -> bool:
    """Return True if the event is in an allowed city or is online.

    Args:
        event: The event object from the Luma API.

    Returns:
        True if the event should be included, False if it should be skipped.
    """
    if event.get("location_type") == "online":
        return True
    geo: dict = event.get("geo_address_info") or {}
    city = geo.get("city", "")
    # If geo data is missing (e.g. stripped for datacenter IPs), pass through —
    # discover_page=vancouver already scopes the feed to the Vancouver region.
    if not city:
        return True
    return city in _ALLOWED_CITIES


def _extract_text_from_mirror(doc: dict) -> str:
    """Extract plain text from Luma's description_mirror rich-text document.

    Args:
        doc: The description_mirror dict returned by the event detail endpoint.

    Returns:
        A plain-text string with whitespace normalised.
    """
    if not doc or not isinstance(doc, dict):
        return ""

    parts: list[str] = []

    def _traverse(node: dict) -> None:
        if node.get("type") == "text":
            parts.append(node.get("text", ""))
        for child in node.get("content", []):
            _traverse(child)
        # Insert a space after block-level nodes so words don't run together.
        if node.get("type") in ("paragraph", "heading", "listItem", "blockquote"):
            parts.append(" ")

    _traverse(doc)
    return " ".join("".join(parts).split()).strip()


def _parse_location(event: dict) -> str:
    """Build a human-readable location string from an event dict.

    Args:
        event: The event object returned by the discovery or detail endpoint.

    Returns:
        A location string suitable for display in the Slack digest.
    """
    location_type = event.get("location_type", "")

    if location_type == "online":
        return "Online (Virtual)"

    geo: dict = event.get("geo_address_info") or {}
    mode = geo.get("mode", "")

    if mode == "shown":
        # Full or short address is available.
        return (
            geo.get("full_address")
            or geo.get("short_address")
            or geo.get("city_state", "Vancouver, BC")
        )

    if mode == "obfuscated":
        # Luma hides the exact address until the attendee RSVPs.
        city_state = geo.get("city_state") or geo.get("city", "Vancouver") + ", BC"
        return f"{city_state} (address shown after RSVP)"

    return "Vancouver, BC"


def _fetch_event_description(api_id: str) -> str:
    """Fetch a description for a single event from the Luma detail endpoint.

    Args:
        api_id: The event's api_id (e.g. "evt-abc123").

    Returns:
        Plain-text description, or an empty string on failure.
    """
    try:
        resp = requests.get(
            _DETAIL_URL,
            params={"event_api_id": api_id, "series_mode": "next_only"},
            headers=_HEADERS,
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        mirror = data.get("description_mirror")
        if mirror:
            return _extract_text_from_mirror(mirror)
    except Exception as exc:  # noqa: BLE001
        logger.debug("[Luma] Could not fetch description for %s: %s", api_id, exc)
    return ""


class LumaFetcher(BaseEventFetcher):
    """Fetches events from lu.ma/vancouver via Luma's public discovery API."""

    name = "Luma"

    def fetch_events(self, start_date: date, end_date: date) -> list[Event]:
        """Fetch Vancouver Luma events between start_date and end_date (inclusive).

        Paginates through the Luma discovery feed for Vancouver and stops as
        soon as event start times exceed end_date.  For each event that falls
        within the window, a second API call retrieves the event description.

        Args:
            start_date: First day of the range (Monday of the upcoming week).
            end_date:   Last day of the range (Sunday of the upcoming week).

        Returns:
            A list of Event objects.  Never raises; logs errors instead.
        """
        start_boundary = datetime(
            start_date.year, start_date.month, start_date.day,
            0, 0, 0, tzinfo=_VANCOUVER_TZ,
        )
        end_boundary = datetime(
            end_date.year, end_date.month, end_date.day,
            23, 59, 59, tzinfo=_VANCOUVER_TZ,
        )

        events: list[Event] = []
        raw: int = 0          # events in date range before geo filter
        cursor: str | None = None
        page = 0

        while True:
            page += 1
            params: dict[str, str] = {
                "discover_page": "vancouver",
                "pagination_limit": "50",
            }
            if cursor:
                params["pagination_cursor"] = cursor

            try:
                resp = requests.get(
                    _DISCOVER_URL, params=params, headers=_HEADERS, timeout=15,
                )
                resp.raise_for_status()
                data = resp.json()
            except Exception as exc:  # noqa: BLE001
                logger.error("[Luma] Discovery page %d failed: %s", page, exc)
                break

            entries: list[dict] = data.get("entries", [])
            has_more: bool = data.get("has_more", False)
            cursor = data.get("next_cursor")

            stop_paginating = False
            for entry in entries:
                event_data: dict = entry.get("event", {})
                start_at: str = event_data.get("start_at", "")
                if not start_at:
                    continue

                try:
                    dt_utc = datetime.fromisoformat(start_at.replace("Z", "+00:00"))
                    tz_name = event_data.get("timezone") or "America/Vancouver"
                    dt_local = dt_utc.astimezone(ZoneInfo(tz_name))
                except Exception as exc:  # noqa: BLE001
                    logger.debug("[Luma] Could not parse datetime '%s': %s", start_at, exc)
                    continue

                if dt_local > end_boundary:
                    # Events are chronological — nothing further is in range.
                    stop_paginating = True
                    break

                if dt_local < start_boundary:
                    continue

                raw += 1  # in-range before geo filter

                if not _is_in_coverage_area(event_data):
                    logger.debug(
                        "[Luma] Skipping out-of-area event: %s (%s)",
                        event_data.get("name"),
                        (event_data.get("geo_address_info") or {}).get("city", "unknown"),
                    )
                    continue

                # ---- Map to our Event dataclass ----
                url_slug = event_data.get("url", "")
                event_url = f"{_EVENT_BASE}/{url_slug}"
                location = _parse_location(event_data)
                date_str = dt_local.strftime("%Y-%m-%d")
                # strftime hour without leading zero (%-I on POSIX)
                hour = dt_local.strftime("%I").lstrip("0") or "12"
                time_str = hour + dt_local.strftime(":%M %p")

                api_id = event_data.get("api_id", "")
                description = _fetch_event_description(api_id) if api_id else ""

                # Fall back to the calendar's short description when the event
                # itself has no body text (common for recurring/simple events).
                if not description:
                    cal: dict = entry.get("calendar") or {}
                    description = cal.get("description_short") or ""

                events.append(
                    Event(
                        title=event_data.get("name", "Untitled Event"),
                        date=date_str,
                        time=time_str,
                        location=location,
                        description=description,
                        url=event_url,
                        source=self.name,
                        tags=[],
                    )
                )

            if stop_paginating or not has_more or not cursor:
                break

        self.raw_count = raw
        logger.info(
            "[Luma] raw=%d  geo-filtered=%d  for %s → %s",
            raw, len(events), start_date, end_date,
        )
        return events
