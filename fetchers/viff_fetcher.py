"""
VIFF (Vancouver International Film Festival) event fetcher.

Scrapes upcoming screenings from https://viff.org/whats-on/ using
requests + BeautifulSoup.  One Event is created per screening instance —
a film showing at a specific date, time, and venue.  Images are ignored.
"""

import logging
import re
from datetime import date, datetime

import requests
from bs4 import BeautifulSoup

from .base import BaseEventFetcher, Event

logger = logging.getLogger(__name__)

_BASE_URL = "https://viff.org"
_EVENTS_URL = f"{_BASE_URL}/whats-on/"

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}


def _parse_viff_date(raw: str) -> str:
    """Parse a VIFF screening date like 'Sat Mar 21' to YYYY-MM-DD.

    The site omits the year.  We try the current year first; if the result is
    more than 60 days in the past, we roll forward to the next year.

    Args:
        raw: Text from the .c-event-instance__date <span>.

    Returns:
        ISO date string YYYY-MM-DD, or the original string on parse failure.
    """
    raw = re.sub(r"\s+", " ", raw).strip()
    # Strip day-of-week prefix: "Sat Mar 21" → "Mar 21"
    raw = re.sub(r"^(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s+", "", raw, flags=re.IGNORECASE)
    today = date.today()
    for year in (today.year, today.year + 1):
        try:
            d = datetime.strptime(f"{raw} {year}", "%b %d %Y").date()
            if (today - d).days <= 60:
                return d.strftime("%Y-%m-%d")
        except ValueError:
            continue
    return raw


def _parse_viff_time(raw: str) -> str:
    """Normalise a VIFF time string like '7:30 pm' to '7:30 PM'.

    Args:
        raw: Text from .c-event-instance__time.

    Returns:
        Normalised time string, or 'TBD' if unparseable.
    """
    m = re.search(r"(\d{1,2}:\d{2})\s*(am|pm)", raw.strip(), re.IGNORECASE)
    if m:
        return f"{m.group(1)} {m.group(2).upper()}"
    return "TBD"


def parse_viff_html(html: str, start_date: date, end_date: date) -> tuple[list[Event], int]:
    """Parse VIFF whats-on HTML and return in-range events plus raw screening count.

    Extracted for testability — accepts raw HTML, makes no network calls.

    Args:
        html:       Raw HTML of the VIFF whats-on page.
        start_date: First day of the desired date range.
        end_date:   Last day of the desired date range.

    Returns:
        A (events, raw_count) tuple where raw_count is the total number of
        screening instances parsed before date filtering.
    """
    soup = BeautifulSoup(html, "html.parser")
    events: list[Event] = []
    raw_count = 0

    for card in soup.find_all("div", class_="c-event-card"):
        # --- Film-level metadata ---
        title_tag = card.find("h3", class_="c-event-card__title")
        title_link = title_tag.find("a") if title_tag else None
        title = title_link.get_text(strip=True) if title_link else "Untitled"
        film_url = title_link["href"] if title_link and title_link.get("href") else _EVENTS_URL

        desc_tag = card.find("p", class_="c-event-card__excerpt")
        description = desc_tag.get_text(strip=True) if desc_tag else ""

        season_tag = card.find("div", class_="c-event-card__seasons")
        tags: list[str] = []
        if season_tag:
            for a in season_tag.find_all("a"):
                text = a.get_text(strip=True)
                if text:
                    tags.append(text)

        # --- One Event per screening instance ---
        for instance in card.find_all("div", class_="c-event-instance"):
            raw_count += 1

            date_tag = instance.find("div", class_="c-event-instance__date")
            date_span = date_tag.find("span") if date_tag else None
            raw_date = date_span.get_text(strip=True) if date_span else ""
            date_str = _parse_viff_date(raw_date)

            try:
                event_date = datetime.strptime(date_str, "%Y-%m-%d").date()
            except ValueError:
                continue
            if not (start_date <= event_date <= end_date):
                continue

            time_tag = instance.find("div", class_="c-event-instance__time")
            time_str = _parse_viff_time(time_tag.get_text() if time_tag else "")

            venue_tag = instance.find("span", class_="c-event-instance__venue")
            location = venue_tag.get_text(strip=True) if venue_tag else "VIFF Centre, Vancouver"

            # Prefer the booking link for this instance; fall back to the film page
            book_tag = instance.find("a", class_="c-event-instance__btn")
            url = book_tag["href"] if book_tag and book_tag.get("href") else film_url

            events.append(
                Event(
                    title=title,
                    date=date_str,
                    time=time_str,
                    location=location,
                    description=description,
                    url=url,
                    source="VIFF",
                    tags=tags,
                )
            )

    return events, raw_count


class ViffFetcher(BaseEventFetcher):
    """Fetches upcoming film screenings from viff.org/whats-on/."""

    name = "VIFF"

    def fetch_events(self, start_date: date, end_date: date) -> list[Event]:
        """Scrape VIFF whats-on events between start_date and end_date (inclusive).

        Makes a single HTTP GET request and parses the HTML.  One Event is
        returned per screening instance within the date window.

        Args:
            start_date: First day of the range.
            end_date:   Last day of the range.

        Returns:
            A list of Event objects, one per qualifying screening.
        """
        logger.info("[VIFF] Fetching events from %s", _EVENTS_URL)
        resp = requests.get(_EVENTS_URL, headers=_HEADERS, timeout=15)
        resp.raise_for_status()

        events, raw_count = parse_viff_html(resp.text, start_date, end_date)
        self.raw_count = raw_count

        logger.info(
            "[VIFF] raw=%d  in-range=%d  for %s → %s",
            raw_count, len(events), start_date, end_date,
        )
        return events
