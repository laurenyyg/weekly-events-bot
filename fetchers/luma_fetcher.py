"""
Luma event fetcher for lu.ma/vancouver.

Uses Playwright (headless Chromium) to navigate to lu.ma/vancouver,
obtain Cloudflare clearance cookies, then calls Luma's internal discovery
API from within the browser's JS context so the requests inherit those
cookies and are not blocked.
"""

import logging
import os
from datetime import date, datetime
from zoneinfo import ZoneInfo

from playwright.sync_api import sync_playwright

from .base import BaseEventFetcher, Event

logger = logging.getLogger(__name__)

_VANCOUVER_TZ = ZoneInfo("America/Vancouver")
_EVENT_BASE = "https://lu.ma"

# Cities included in the digest. Online events are always included.
# "University Endowment Lands" is the official municipality name for UBC.
_ALLOWED_CITIES = {
    "Vancouver",
    "Burnaby",
    "Richmond",
    "University Endowment Lands",
    "North Vancouver",
    "West Vancouver",
    "Surrey",
    "Coquitlam",
    "New Westminster",
    "Port Coquitlam",
    "Port Moody",
    "Maple Ridge",
    "Langley",
    "Delta",
    "Pitt Meadows",
    "Lions Bay",
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
        return (
            geo.get("full_address")
            or geo.get("short_address")
            or geo.get("city_state", "Vancouver, BC")
        )

    if mode == "obfuscated":
        city_state = geo.get("city_state") or geo.get("city", "Vancouver") + ", BC"
        return f"{city_state} (address shown after RSVP)"

    return "Vancouver, BC"


class LumaFetcher(BaseEventFetcher):
    """Fetches events from lu.ma/vancouver via headless Chromium + Playwright."""

    name = "Luma"

    def fetch_events(self, start_date: date, end_date: date) -> list[Event]:
        """Fetch Vancouver Luma events between start_date and end_date (inclusive).

        Launches a headless browser, navigates to lu.ma/vancouver to obtain
        Cloudflare clearance, then paginates the discovery API from within the
        browser's JS context.

        Args:
            start_date: First day of the range (inclusive).
            end_date:   Last day of the range (inclusive).

        Returns:
            A list of Event objects. Never raises; logs errors instead.
        """
        start_boundary = datetime(
            start_date.year, start_date.month, start_date.day,
            0, 0, 0, tzinfo=_VANCOUVER_TZ,
        )
        end_boundary = datetime(
            end_date.year, end_date.month, end_date.day,
            23, 59, 59, tzinfo=_VANCOUVER_TZ,
        )

        try:
            return self._fetch_with_playwright(start_boundary, end_boundary, start_date, end_date)
        except Exception as exc:
            logger.error("[Luma] Playwright fetch failed: %s", exc)
            self.raw_count = 0
            return []

    def _fetch_with_playwright(
        self,
        start_boundary: datetime,
        end_boundary: datetime,
        start_date: date,
        end_date: date,
    ) -> list[Event]:
        """Run the Playwright browser session and return parsed events."""
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled"],
            )
            context = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/123.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1280, "height": 800},
                locale="en-US",
            )
            page = context.new_page()

            # Mask navigator.webdriver to avoid Cloudflare bot detection.
            page.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
            )

            logger.debug("[Luma] Navigating to lu.ma/vancouver to obtain CF clearance...")
            page.goto("https://lu.ma/vancouver", wait_until="networkidle", timeout=30000)

            events: list[Event] = []
            raw: int = 0
            cursor: str | None = None
            page_num: int = 0
            stop_paginating: bool = False

            while not stop_paginating:
                page_num += 1

                # Call the discovery API from within the browser's JS context
                # so Cloudflare clearance cookies are included automatically.
                data = page.evaluate(
                    """async (cursor) => {
                        const params = new URLSearchParams({
                            discover_page: 'vancouver',
                            pagination_limit: '50',
                        });
                        if (cursor) params.set('pagination_cursor', cursor);
                        const resp = await fetch(
                            'https://api.lu.ma/discover/get-paginated-events?' + params,
                            { headers: { 'accept': 'application/json' } }
                        );
                        return resp.json();
                    }""",
                    cursor,
                )

                entries: list[dict] = data.get("entries", [])
                has_more: bool = data.get("has_more", False)
                cursor = data.get("next_cursor")

                for entry in entries:
                    event_data: dict = entry.get("event", {})
                    start_at: str = event_data.get("start_at", "")
                    if not start_at:
                        continue

                    try:
                        dt_utc = datetime.fromisoformat(start_at.replace("Z", "+00:00"))
                        tz_name = event_data.get("timezone") or "America/Vancouver"
                        dt_local = dt_utc.astimezone(ZoneInfo(tz_name))
                    except Exception as exc:
                        logger.debug("[Luma] Could not parse datetime '%s': %s", start_at, exc)
                        continue

                    if dt_local > end_boundary:
                        stop_paginating = True
                        break

                    if dt_local < start_boundary:
                        continue

                    raw += 1

                    if not _is_in_coverage_area(event_data):
                        logger.debug(
                            "[Luma] Skipping out-of-area event: %s (%s)",
                            event_data.get("name"),
                            (event_data.get("geo_address_info") or {}).get("city", "unknown"),
                        )
                        continue

                    # Fetch event description via browser JS context.
                    api_id = event_data.get("api_id", "")
                    description = ""
                    if api_id:
                        try:
                            detail = page.evaluate(
                                """async (api_id) => {
                                    const resp = await fetch(
                                        'https://api.lu.ma/event/get?event_api_id=' + api_id + '&series_mode=next_only',
                                        { headers: { 'accept': 'application/json' } }
                                    );
                                    return resp.json();
                                }""",
                                api_id,
                            )
                            mirror = detail.get("description_mirror")
                            if mirror:
                                description = _extract_text_from_mirror(mirror)
                        except Exception as exc:
                            logger.debug("[Luma] Could not fetch description for %s: %s", api_id, exc)

                    if not description:
                        cal: dict = entry.get("calendar") or {}
                        description = cal.get("description_short") or ""

                    url_slug = event_data.get("url", "")
                    event_url = f"{_EVENT_BASE}/{url_slug}"
                    location = _parse_location(event_data)
                    date_str = dt_local.strftime("%Y-%m-%d")
                    hour = dt_local.strftime("%I").lstrip("0") or "12"
                    time_str = hour + dt_local.strftime(":%M %p")

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

                if not has_more or not cursor:
                    break

            browser.close()

        self.raw_count = raw
        logger.info(
            "[Luma] raw=%d  geo-filtered=%d  for %s → %s",
            raw, len(events), start_date, end_date,
        )
        return events
