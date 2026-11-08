"""
The Cinematheque event fetcher.

Scrapes the monthly calendar from https://thecinematheque.ca/films/calendar
using requests + BeautifulSoup.  The calendar renders as a day-by-day list
with screenings nested under each date.  Images are ignored.
"""

import logging
import re
from datetime import date, datetime

import requests
from bs4 import BeautifulSoup

from .base import BaseEventFetcher, Event, fill_descriptions

logger = logging.getLogger(__name__)

_BASE_URL = "https://thecinematheque.ca"
_CALENDAR_URL = f"{_BASE_URL}/films/calendar"
_DEFAULT_LOCATION = "The Cinematheque, 1131 Howe Street, Vancouver"

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}


def _parse_cinema_time(time_el) -> str:
    """Convert a Cinematheque <span class="time am|pm"> element to a display string.

    The element text is bare like '7:30'; its CSS class indicates am/pm.

    Args:
        time_el: BeautifulSoup Tag for the time span.

    Returns:
        Formatted time string such as '7:30 PM'.
    """
    raw = time_el.get_text(strip=True)
    period = "AM" if "am" in time_el.get("class", []) else "PM"
    m = re.match(r"(\d{1,2}):(\d{2})", raw)
    if not m:
        return raw
    return f"{m.group(1)}:{m.group(2)} {period}"


def parse_cinematheque_html(
    html: str, start_date: date, end_date: date
) -> tuple[list[Event], int]:
    """Parse Cinematheque calendar HTML and return in-range events plus raw count.

    Extracted for testability — accepts raw HTML, makes no network calls.

    The calendar is structured as:
        ol#eventCalendar > li (one per day)
            span.day > span.dow / .dom / .mon / .year
            ol.programs > li.programScreening
                span.details > a > span.time.am|pm
                a.programTitle

    Args:
        html:       Raw HTML of the Cinematheque calendar page.
        start_date: First day of the desired date range.
        end_date:   Last day of the desired date range.

    Returns:
        A (events, raw_count) tuple where raw_count is total screenings parsed
        before date filtering.
    """
    soup = BeautifulSoup(html, "html.parser")
    events: list[Event] = []
    raw_count = 0

    calendar = soup.find("ol", id="eventCalendar")
    if not calendar:
        return events, raw_count

    for day_li in calendar.find_all("li", recursive=False):
        day_span = day_li.find("span", class_="day")
        if not day_span:
            continue

        dom = day_span.find("span", class_="dom")
        mon = day_span.find("span", class_="mon")
        year = day_span.find("span", class_="year")
        if not (dom and mon and year):
            continue

        date_str_raw = (
            f"{mon.get_text(strip=True)} "
            f"{dom.get_text(strip=True)} "
            f"{year.get_text(strip=True)}"
        )
        try:
            event_date = datetime.strptime(date_str_raw, "%B %d %Y").date()
        except ValueError:
            continue

        date_str = event_date.strftime("%Y-%m-%d")
        in_range = start_date <= event_date <= end_date

        for screening in day_li.find_all("li", class_="programScreening"):
            raw_count += 1

            if not in_range:
                continue

            title_tag = screening.find("a", class_="programTitle")
            title = title_tag.get_text(strip=True) if title_tag else "Untitled"
            href = title_tag["href"] if title_tag and title_tag.get("href") else _CALENDAR_URL
            url = href if href.startswith("http") else _BASE_URL + href

            time_el = screening.find("span", class_="time")
            time_str = _parse_cinema_time(time_el) if time_el else "TBD"

            events.append(
                Event(
                    title=title,
                    date=date_str,
                    time=time_str,
                    location=_DEFAULT_LOCATION,
                    description="",
                    url=url,
                    source="Cinematheque",
                    tags=[],
                )
            )

    return events, raw_count


def parse_cinematheque_description(html: str) -> str:
    """Return a film page's one-sentence synopsis from its meta description.

    Args:
        html: Raw HTML of a thecinematheque.ca film page.

    Returns:
        The synopsis text, or "" if the page has none.
    """
    soup = BeautifulSoup(html, "html.parser")
    for attrs in ({"name": "description"}, {"property": "og:description"}):
        meta = soup.find("meta", attrs=attrs)
        if meta and meta.get("content", "").strip():
            return meta["content"].strip()
    return ""


class CinemathequeFetcher(BaseEventFetcher):
    """Fetches film screenings from thecinematheque.ca calendar."""

    name = "Cinematheque"

    def fetch_events(self, start_date: date, end_date: date) -> list[Event]:
        """Scrape Cinematheque calendar for screenings between start_date and end_date.

        Makes one HTTP GET to the calendar page and parses the day-by-day
        listing, then one GET per unique film page for its synopsis.
        Location is always The Cinematheque (1131 Howe Street).

        Args:
            start_date: First day of the range.
            end_date:   Last day of the range.

        Returns:
            A list of Event objects within the date range.
        """
        logger.info("[Cinematheque] Fetching calendar from %s", _CALENDAR_URL)
        resp = requests.get(_CALENDAR_URL, headers=_HEADERS, timeout=15)
        resp.raise_for_status()

        events, raw_count = parse_cinematheque_html(resp.text, start_date, end_date)
        self.raw_count = raw_count
        fill_descriptions(events, parse_cinematheque_description, _HEADERS, self.name)

        logger.info(
            "[Cinematheque] raw=%d  in-range=%d  for %s → %s",
            raw_count, len(events), start_date, end_date,
        )
        return events
