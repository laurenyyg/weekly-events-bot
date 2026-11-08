"""
Cerebral Valley fetcher for remote AI events.

Reads the public JSON API behind cerebralvalley.ai/events (no key needed).
Cerebral Valley has no Vancouver location, so only remote events are pulled,
and only those starting at a reasonable Vancouver hour are kept. Every event
is AI-related, so kept events are marked pre_approved and skip keyword
matching.
"""

import logging
from datetime import date, datetime, time, timedelta, timezone
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import requests

from .base import BaseEventFetcher, Event

logger = logging.getLogger(__name__)

_API_URL = "https://api.cerebralvalley.ai/v1/public/event/pull"
_VANCOUVER_TZ = ZoneInfo("America/Vancouver")
_PAGE_SIZE = 100
_MAX_PAGES = 20
_DESCRIPTION_MAX_CHARS = 500

# Keep events starting between 10:00 and 20:00 Vancouver time, inclusive.
_EARLIEST = 10 * 60
_LATEST = 20 * 60

# The REMOTE filter also returns some "Other" events, which have turned out
# to be in-person events elsewhere.
_EXCLUDED_LOCATIONS = {"Other"}

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept": "application/json",
}


def _parse_utc(value: str) -> datetime:
    """Parse the API's UTC timestamp ("YYYY-MM-DD HH:MM:SS") as an aware datetime."""
    return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)


def _strip_query(url: str) -> str:
    """Drop the query string and fragment (e.g. "?tab=guest-list") from a URL."""
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def _iso_utc(dt: datetime) -> str:
    """Format an aware datetime as the API's ISO UTC string."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


class CerebralValleyFetcher(BaseEventFetcher):
    """Fetches remote AI events from Cerebral Valley."""

    name = "Cerebral Valley (remote)"

    def __init__(self) -> None:
        self.raw_count = 0
        self.empty_ok = False

    def _pull(self, start: datetime, end: datetime) -> list[dict]:
        """Return every remote event the API lists for [start, end), across pages."""
        events: list[dict] = []
        with requests.Session() as session:
            session.headers.update(_HEADERS)
            for page in range(_MAX_PAGES):
                resp = session.get(_API_URL, params={
                    "approved": "true",
                    "locations": "REMOTE",
                    "startDateTime": _iso_utc(start),
                    "endDateTime": _iso_utc(end),
                    "limit": _PAGE_SIZE,
                    "offset": page * _PAGE_SIZE,
                }, timeout=15)
                resp.raise_for_status()
                data = resp.json()
                if not isinstance(data.get("events"), list):
                    raise ValueError("Cerebral Valley response has no 'events' list")
                events.extend(data["events"])
                if data.get("lastPage", True) or not data["events"]:
                    break
        return events

    def fetch_events(self, start_date: date, end_date: date) -> list[Event]:
        """Fetch remote events starting in the date range at 10:00–20:00 Vancouver time.

        Args:
            start_date: First day of the range (inclusive).
            end_date:   Last day of the range (inclusive).

        Returns:
            A list of Event objects, all marked pre_approved.

        Raises:
            requests.RequestException or ValueError: If the API call fails or
                returns an unexpected shape. The orchestrator reports it in Part B.
        """
        start = datetime.combine(start_date, time.min, tzinfo=_VANCOUVER_TZ)
        end = datetime.combine(end_date + timedelta(days=1), time.min, tzinfo=_VANCOUVER_TZ)

        listed = self._pull(start, end)
        self.empty_ok = True

        events: list[Event] = []
        raw = 0
        for item in listed:
            try:
                starts = _parse_utc(item["startDateTime"]).astimezone(_VANCOUVER_TZ)
            except (KeyError, TypeError, ValueError):
                logger.warning("[%s] Skipping event with bad start time: %s", self.name, item.get("name"))
                continue
            if not (start <= starts < end):
                continue  # ongoing events that began before the window
            raw += 1

            if item.get("location") in _EXCLUDED_LOCATIONS:
                logger.info("[%s] Skipping non-remote event: %s", self.name, item.get("name"))
                continue
            minutes = starts.hour * 60 + starts.minute
            if not (_EARLIEST <= minutes <= _LATEST):
                continue

            description = item.get("description") or item.get("descriptionSummary") or ""
            hour = starts.strftime("%I").lstrip("0") or "12"
            events.append(
                Event(
                    title=str(item.get("name", "Untitled Event")),
                    date=starts.strftime("%Y-%m-%d"),
                    time=hour + starts.strftime(":%M %p"),
                    location="Online",
                    description=" ".join(description.split())[:_DESCRIPTION_MAX_CHARS],
                    url=_strip_query(str(item.get("url", ""))),
                    source=self.name,
                    tags=[],
                    pre_approved=True,
                )
            )

        self.raw_count = raw
        logger.info(
            "[%s] listed=%d  in-range=%d  kept=%d  for %s → %s",
            self.name, len(listed), raw, len(events), start_date, end_date,
        )
        return events
