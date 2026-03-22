"""
Chan Centre for the Performing Arts event fetcher.

Scrapes upcoming events from https://chancentre.com/events/ using
requests + BeautifulSoup.  Only text data is extracted (titles, dates,
times, locations, URLs) — all images are ignored.
"""

import logging
import re
from datetime import date, datetime

import requests
from bs4 import BeautifulSoup

from .base import BaseEventFetcher, Event

logger = logging.getLogger(__name__)

_BASE_URL = "https://chancentre.com"
_EVENTS_URL = f"{_BASE_URL}/events/"

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}


def _parse_meta_date(raw: str) -> tuple[str, str]:
    """Parse the event-item--meta-date string from chancentre.com.

    The site uses two formats:
      - Single date+time: "Sat Mar 21 / 2026 / 7:30pm"
      - Date range:       "Mar 19 – Mar 22 / 2026"

    Args:
        raw: The raw text from the .event-item--meta-date div.

    Returns:
        A (date_str, time_str) tuple. date_str is YYYY-MM-DD (start date for
        ranges). time_str is "H:MM AM/PM" or "TBD".
    """
    raw = re.sub(r"\s+", " ", raw).strip()
    parts = [p.strip() for p in raw.split("/")]

    year = ""
    for p in parts:
        if re.match(r"^\d{4}$", p):
            year = p
            break

    # Extract time if present
    time_str = "TBD"
    time_match = re.search(r"(\d{1,2}:\d{2}\s*(?:am|pm))", raw, re.IGNORECASE)
    if time_match:
        t = time_match.group(1).upper()
        t = re.sub(r"(\d)(AM|PM)", r"\1 \2", t)
        time_str = t

    # Extract start date — handle ranges like "Mar 19 – Mar 22"
    date_part = parts[0]
    # If range, take only the portion before the dash
    range_match = re.search(r"^(.+?)\s*[–—-]\s*.+$", date_part)
    if range_match:
        date_part = range_match.group(1).strip()

    # Strip day-of-week prefix: "Sat Mar 21" → "Mar 21"
    date_part = re.sub(r"^(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s+", "", date_part, flags=re.IGNORECASE)
    date_part = date_part.strip()

    date_str = date_part  # fallback
    if year:
        for fmt in ("%b %d %Y", "%B %d %Y"):
            try:
                date_str = datetime.strptime(f"{date_part} {year}", fmt).strftime("%Y-%m-%d")
                break
            except ValueError:
                continue

    return date_str, time_str


def _is_in_date_range(date_str: str, start_date: date, end_date: date) -> bool:
    """Return True if date_str (YYYY-MM-DD) falls within [start_date, end_date].

    Args:
        date_str:   ISO-format date string.
        start_date: Start of the range (inclusive).
        end_date:   End of the range (inclusive).

    Returns:
        True if the event falls within the window, False otherwise.
    """
    try:
        event_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        return start_date <= event_date <= end_date
    except ValueError:
        return False


def parse_chancentre_html(html: str, start_date: date, end_date: date) -> tuple[list[Event], int]:
    """Parse Chan Centre events HTML and return in-range events plus raw count.

    This function is extracted for testability — it accepts raw HTML rather
    than making any network calls.

    Args:
        html:       Raw HTML of the Chan Centre events page.
        start_date: First day of the desired date range.
        end_date:   Last day of the desired date range.

    Returns:
        A (events, raw_count) tuple where events contains only those within
        the date range and raw_count is the total number of events parsed
        before date filtering.
    """
    soup = BeautifulSoup(html, "html.parser")
    events: list[Event] = []
    raw_count = 0

    # Chan Centre renders each event as <div class="event-item ...">
    items = soup.find_all("div", class_="event-item")

    for item in items:
        # --- Date / Time ---
        date_tag = item.find(class_="event-item--meta-date")
        raw_meta = date_tag.get_text(strip=True) if date_tag else ""
        date_str, time_str = _parse_meta_date(raw_meta)

        raw_count += 1

        if not _is_in_date_range(date_str, start_date, end_date):
            continue

        # --- URL ---
        link_tag = item.find("a", class_="event-item--meta")
        url = link_tag["href"] if link_tag else _EVENTS_URL

        # --- Title (lead + title combined) ---
        lead_tag = item.find(class_="event-item--lead")
        title_tag = item.find(class_="event-item--title")
        lead = lead_tag.get_text(strip=True) if lead_tag else ""
        title_text = title_tag.get_text(strip=True) if title_tag else "Untitled Event"
        title = f"{lead}: {title_text}" if lead else title_text

        # --- Location ---
        loc_tag = item.find(class_="event-item--location")
        location = loc_tag.get_text(strip=True) if loc_tag else "Chan Centre for the Performing Arts, Vancouver, BC"

        events.append(
            Event(
                title=title,
                date=date_str,
                time=time_str,
                location=location,
                description="",
                url=url,
                source="ChanCentre",
                tags=[],
            )
        )

    return events, raw_count


class ChanCentreFetcher(BaseEventFetcher):
    """Fetches upcoming events from chancentre.com/events/."""

    name = "ChanCentre"

    def fetch_events(self, start_date: date, end_date: date) -> list[Event]:
        """Scrape Chan Centre events between start_date and end_date (inclusive).

        Makes a single HTTP GET request to the events listing page and parses
        the HTML with BeautifulSoup.  Only text content is extracted; all
        images are ignored.

        Args:
            start_date: First day of the range (Monday of the upcoming week).
            end_date:   Last day of the range (Sunday of the upcoming week).

        Returns:
            A list of Event objects within the date range.
        """
        logger.info("[ChanCentre] Fetching events from %s", _EVENTS_URL)
        resp = requests.get(_EVENTS_URL, headers=_HEADERS, timeout=15)
        resp.raise_for_status()

        events, raw_count = parse_chancentre_html(resp.text, start_date, end_date)
        self.raw_count = raw_count

        logger.info(
            "[ChanCentre] raw=%d  in-range=%d  for %s → %s",
            raw_count, len(events), start_date, end_date,
        )
        return events
