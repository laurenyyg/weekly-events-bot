"""
Luma event fetcher for community calendars.

Each instance reads one Luma calendar through its official iCal subscription
endpoint — no API key required and no bot-detection issues (unlike the
reverse-engineered discovery API). Calendar IDs live in config.json under
`tech_luma_calendars`.
"""

import logging
import re
from datetime import date, datetime
from zoneinfo import ZoneInfo

import requests
from icalendar import Calendar

from .base import BaseEventFetcher, Event

logger = logging.getLogger(__name__)

_ICAL_URL = "https://api2.luma.com/ics/get?entity=calendar&id={calendar_id}"
_VANCOUVER_TZ = ZoneInfo("America/Vancouver")

# Bounding box for Metro Vancouver (excludes Nanaimo, Victoria, Whistler, etc.)
_LAT_MIN, _LAT_MAX = 49.0, 49.45
_LON_MIN, _LON_MAX = -123.3, -122.2

# Hosts mark called-off events in the title ("P O S T P O N E D") as often as
# in the iCal STATUS field, so both are checked.
_CALLED_OFF_WORDS = ("POSTPONED", "CANCELLED", "CANCELED")


def _in_metro_vancouver(geo) -> bool:
    """Return True if a GEO value falls within the Metro Vancouver bounding box.

    Args:
        geo: The vGeo component from an iCal VEVENT, or None.

    Returns:
        True if in range or if geo data is absent or unparseable (pass-through).
    """
    if geo is None:
        return True
    try:
        lat = float(geo.latitude)
        lon = float(geo.longitude)
        return _LAT_MIN <= lat <= _LAT_MAX and _LON_MIN <= lon <= _LON_MAX
    except Exception:
        return True


def _is_called_off(vevent) -> bool:
    """Return True if a VEVENT is cancelled or postponed.

    Args:
        vevent: An icalendar VEVENT component.

    Returns:
        True if STATUS is CANCELLED or the title says postponed/cancelled.
    """
    if str(vevent.get("STATUS", "")).upper() == "CANCELLED":
        return True
    squashed = re.sub(r"\W", "", str(vevent.get("SUMMARY", ""))).upper()
    return any(word in squashed for word in _CALLED_OFF_WORDS)


def _extract_url(description: str) -> str:
    """Pull the canonical event URL out of the iCal DESCRIPTION field.

    Luma embeds it as the first line: "Get up-to-date information at: <url>"

    Args:
        description: Raw DESCRIPTION string from the VEVENT.

    Returns:
        The luma.com URL, or an empty string if not found.
    """
    m = re.search(r"https://luma\.com/\S+", description)
    return m.group(0) if m else ""


def _extract_description_body(description: str) -> str:
    """Strip the Luma boilerplate header and return just the event body text.

    The DESCRIPTION field has this structure:
        Get up-to-date information at: <url>

        Address:
        <address lines>

        <actual description>

        Hosted by <name>

    Args:
        description: Raw DESCRIPTION string from the VEVENT.

    Returns:
        The event body text with whitespace normalised.
    """
    # Drop everything up to and including the Address block.
    body = re.split(r"Address:\n.*?\n\n", description, maxsplit=1, flags=re.DOTALL)
    text = body[-1]  # everything after the address block (or full text if no match)

    # Drop trailing "Hosted by …" line.
    text = re.sub(r"\s*Hosted by .+$", "", text, flags=re.DOTALL)

    # Normalise Luma's escaped newlines and extra whitespace.
    text = text.replace("\\n", " ").strip()
    return " ".join(text.split())


def _parse_location(vevent) -> str:
    """Return a human-readable location string from a VEVENT.

    Falls back gracefully when the address is obfuscated (Luma replaces it
    with the event URL).

    Args:
        vevent: An icalendar VEVENT component.

    Returns:
        A location string for display.
    """
    loc = str(vevent.get("LOCATION", ""))
    if loc.startswith("https://"):
        return "Vancouver, BC (address shown after RSVP)"
    return loc or "Vancouver, BC"


class LumaFetcher(BaseEventFetcher):
    """Fetches events from one Luma community calendar's iCal feed."""

    def __init__(self, calendar_name: str, calendar_id: str) -> None:
        """
        Args:
            calendar_name: Display name, shown in Part B as "Luma: <name>".
            calendar_id:   Luma calendar ID ("cal-…"), found in the
                           calendar page's HTML.
        """
        self.name = f"Luma: {calendar_name}"
        self.calendar_id = calendar_id
        self.raw_count = 0
        # Set once the feed loads: a calendar with nothing in the window is
        # normal, so only an empty feed counts as breakage.
        self.empty_ok = False

    def fetch_events(self, start_date: date, end_date: date) -> list[Event]:
        """Fetch this calendar's Metro Vancouver events in the date range.

        Downloads the iCal feed, filters by date window, Metro Vancouver
        bounding box, and postponed/cancelled status.

        Args:
            start_date: First day of the range (inclusive).
            end_date:   Last day of the range (inclusive).

        Returns:
            A list of Event objects.

        Raises:
            requests.RequestException or ValueError: If the feed can't be
                downloaded or parsed. The orchestrator reports it in Part B.
        """
        start_boundary = datetime(
            start_date.year, start_date.month, start_date.day,
            0, 0, 0, tzinfo=_VANCOUVER_TZ,
        )
        end_boundary = datetime(
            end_date.year, end_date.month, end_date.day,
            23, 59, 59, tzinfo=_VANCOUVER_TZ,
        )

        resp = requests.get(_ICAL_URL.format(calendar_id=self.calendar_id), timeout=15)
        resp.raise_for_status()
        cal = Calendar.from_ical(resp.content)

        vevents = list(cal.walk("VEVENT"))
        self.empty_ok = bool(vevents)

        events: list[Event] = []
        raw = 0
        called_off = 0

        for component in vevents:
            dtstart = component.get("DTSTART")
            if dtstart is None:
                continue

            dt = dtstart.dt
            # iCal dates can be date-only (all-day) or datetime.
            if isinstance(dt, date) and not isinstance(dt, datetime):
                dt = datetime(dt.year, dt.month, dt.day, 0, 0, 0,
                              tzinfo=_VANCOUVER_TZ)
            elif dt.tzinfo is None:
                dt = dt.replace(tzinfo=_VANCOUVER_TZ)
            else:
                dt = dt.astimezone(_VANCOUVER_TZ)

            if not (start_boundary <= dt <= end_boundary):
                continue

            raw += 1

            if not _in_metro_vancouver(component.get("GEO")):
                logger.debug(
                    "[%s] Skipping out-of-area event: %s (geo=%s)",
                    self.name, component.get("SUMMARY"), component.get("GEO"),
                )
                continue

            if _is_called_off(component):
                called_off += 1
                logger.info("[%s] Skipping called-off event: %s", self.name, component.get("SUMMARY"))
                continue

            title = str(component.get("SUMMARY", "Untitled Event"))
            description_raw = str(component.get("DESCRIPTION", ""))

            date_str = dt.strftime("%Y-%m-%d")
            hour = dt.strftime("%I").lstrip("0") or "12"
            time_str = hour + dt.strftime(":%M %p")

            events.append(
                Event(
                    title=title,
                    date=date_str,
                    time=time_str,
                    location=_parse_location(component),
                    description=_extract_description_body(description_raw),
                    url=_extract_url(description_raw),
                    source=self.name,
                    tags=[],
                )
            )

        self.raw_count = raw
        logger.info(
            "[%s] feed=%d  in-range=%d  called-off=%d  kept=%d  for %s → %s",
            self.name, len(vevents), raw, called_off, len(events), start_date, end_date,
        )
        return events
