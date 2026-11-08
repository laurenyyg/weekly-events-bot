"""
VIFF (Vancouver International Film Festival) event fetcher.

Scrapes upcoming screenings from https://viff.org/whats-on/ using
requests + BeautifulSoup.  One Event is created per screening instance —
a film showing at a specific date, time, and venue.  Images are ignored.

The listing is paginated, and each film card shows only its first few
screenings ("View N more dates"); the full list is on the film's own page.
"""

import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime

import requests
from bs4 import BeautifulSoup, Tag

from .base import BaseEventFetcher, Event

logger = logging.getLogger(__name__)

_BASE_URL = "https://viff.org"
_EVENTS_URL = f"{_BASE_URL}/whats-on/"
_MAX_PAGES = 10

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}


@dataclass
class ViffFilm:
    """Film-level metadata shared by all of a film's screenings."""
    title: str
    url: str
    description: str
    tags: list[str] = field(default_factory=list)
    listed_count: int = 0   # screenings shown on the listing card


@dataclass
class ViffListing:
    """One parsed page of the whats-on listing."""
    events: list[Event]
    raw_count: int
    card_count: int
    films_with_more_dates: list[ViffFilm]


def _parse_viff_date(raw: str) -> str:
    """Parse a VIFF screening date to YYYY-MM-DD.

    Listing cards use 'Sat Mar 21'; film pages use 'Saturday March 21'.
    The site omits the year.  We try the current year first; if the result is
    more than 60 days in the past, we roll forward to the next year.

    Args:
        raw: Date text from a listing card or a film page's date heading.

    Returns:
        ISO date string YYYY-MM-DD, or the original string on parse failure.
    """
    raw = re.sub(r"\s+", " ", raw).strip()
    # Strip day-of-week prefix: "Sat Mar 21" / "Saturday March 21" → "Mar 21" / "March 21"
    raw = re.sub(r"^(?:mon|tue|wed|thu|fri|sat|sun)[a-z]*\s+", "", raw, flags=re.IGNORECASE)
    today = date.today()
    for year in (today.year, today.year + 1):
        for fmt in ("%b %d %Y", "%B %d %Y"):
            try:
                d = datetime.strptime(f"{raw} {year}", fmt).date()
            except ValueError:
                continue
            if (today - d).days <= 60:
                return d.strftime("%Y-%m-%d")
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


def _instance_event(
    instance: Tag, film: ViffFilm, date_str: str, start_date: date, end_date: date
) -> Event | None:
    """Build an Event for one screening, or None if it is outside the range.

    Args:
        instance:   A div.c-event-instance element.
        film:       Metadata of the film being screened.
        date_str:   The screening date (YYYY-MM-DD if parseable).
        start_date: First day of the desired date range.
        end_date:   Last day of the desired date range.

    Returns:
        The Event, or None if the date is unparseable or out of range.
    """
    try:
        event_date = datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        return None
    if not (start_date <= event_date <= end_date):
        return None

    time_tag = instance.find("div", class_="c-event-instance__time")
    venue_tag = instance.find("span", class_="c-event-instance__venue")
    # Prefer the booking link for this instance; fall back to the film page
    book_tag = instance.find("a", class_="c-event-instance__btn")
    return Event(
        title=film.title,
        date=date_str,
        time=_parse_viff_time(time_tag.get_text() if time_tag else ""),
        location=venue_tag.get_text(strip=True) if venue_tag else "VIFF Centre, Vancouver",
        description=film.description,
        url=book_tag["href"] if book_tag and book_tag.get("href") else film.url,
        source="VIFF",
        tags=film.tags,
    )


def parse_viff_html(html: str, start_date: date, end_date: date) -> ViffListing:
    """Parse one VIFF whats-on listing page.

    Extracted for testability — accepts raw HTML, makes no network calls.

    Args:
        html:       Raw HTML of a VIFF whats-on listing page.
        start_date: First day of the desired date range.
        end_date:   Last day of the desired date range.

    Returns:
        The page's in-range events, raw screening count, card count, and the
        films whose cards hide some screenings behind "View N more dates".
    """
    soup = BeautifulSoup(html, "html.parser")
    listing = ViffListing(events=[], raw_count=0, card_count=0, films_with_more_dates=[])

    for card in soup.find_all("div", class_="c-event-card"):
        listing.card_count += 1

        # --- Film-level metadata ---
        title_tag = card.find("h3", class_="c-event-card__title")
        title_link = title_tag.find("a") if title_tag else None
        desc_tag = card.find("p", class_="c-event-card__excerpt")
        season_tag = card.find("div", class_="c-event-card__seasons")
        film = ViffFilm(
            title=title_link.get_text(strip=True) if title_link else "Untitled",
            url=title_link["href"] if title_link and title_link.get("href") else _EVENTS_URL,
            description=desc_tag.get_text(strip=True) if desc_tag else "",
            tags=[t for a in season_tag.find_all("a") if (t := a.get_text(strip=True))]
            if season_tag else [],
        )

        # --- One Event per screening instance ---
        for instance in card.find_all("div", class_="c-event-instance"):
            film.listed_count += 1
            date_tag = instance.find("div", class_="c-event-instance__date")
            date_span = date_tag.find("span") if date_tag else None
            date_str = _parse_viff_date(date_span.get_text(strip=True) if date_span else "")
            event = _instance_event(instance, film, date_str, start_date, end_date)
            if event:
                listing.events.append(event)
        listing.raw_count += film.listed_count

        if any(a["href"].endswith("#book") for a in card.find_all("a", href=True)):
            listing.films_with_more_dates.append(film)

    return listing


def parse_viff_film_page(
    html: str, film: ViffFilm, start_date: date, end_date: date
) -> tuple[list[Event], int]:
    """Parse every screening from a VIFF film page.

    Screenings are grouped under date headings:
        div.c-event-instance__date-group > h4 ("Saturday November 07")
                                         > div.c-event-instance (time, venue, booking)

    Args:
        html:       Raw HTML of the film's page.
        film:       Metadata from the film's listing card.
        start_date: First day of the desired date range.
        end_date:   Last day of the desired date range.

    Returns:
        An (events, raw_count) tuple: in-range screenings and the total count.
    """
    soup = BeautifulSoup(html, "html.parser")
    events: list[Event] = []
    raw_count = 0
    for group in soup.find_all("div", class_="c-event-instance__date-group"):
        heading = group.find("h4")
        date_str = _parse_viff_date(heading.get_text(strip=True) if heading else "")
        for instance in group.find_all("div", class_="c-event-instance"):
            raw_count += 1
            event = _instance_event(instance, film, date_str, start_date, end_date)
            if event:
                events.append(event)
    return events, raw_count


class ViffFetcher(BaseEventFetcher):
    """Fetches upcoming film screenings from viff.org/whats-on/."""

    name = "VIFF"

    def fetch_events(self, start_date: date, end_date: date) -> list[Event]:
        """Scrape VIFF whats-on events between start_date and end_date (inclusive).

        Reads every listing page, then each film page whose card hides some
        screenings, replacing that film's card screenings with the full list.
        A failed listing page fails the fetcher; a failed film page is logged
        and the film keeps its card screenings.

        Args:
            start_date: First day of the range.
            end_date:   Last day of the range.

        Returns:
            A list of Event objects, one per qualifying screening.
        """
        events: list[Event] = []
        raw_count = 0
        films: dict[str, ViffFilm] = {}

        with requests.Session() as session:
            session.headers.update(_HEADERS)
            for page in range(1, _MAX_PAGES + 1):
                url = _EVENTS_URL if page == 1 else f"{_EVENTS_URL}page/{page}/"
                logger.info("[VIFF] Fetching events from %s", url)
                resp = session.get(url, timeout=15)
                if page > 1 and resp.status_code == 404:
                    break
                resp.raise_for_status()
                listing = parse_viff_html(resp.text, start_date, end_date)
                if not listing.card_count:
                    break
                events.extend(listing.events)
                raw_count += listing.raw_count
                films.update((f.url, f) for f in listing.films_with_more_dates)
            else:
                logger.warning("[VIFF] Stopped at the %d-page limit; later pages skipped.", _MAX_PAGES)

            for film in films.values():
                try:
                    resp = session.get(film.url, timeout=10)
                    resp.raise_for_status()
                    film_events, film_raw = parse_viff_film_page(resp.text, film, start_date, end_date)
                except Exception as exc:
                    logger.warning("[VIFF] Could not fetch all dates for '%s': %s", film.title, exc)
                    continue
                if not film_raw:
                    logger.warning("[VIFF] No screenings found on the page for '%s'.", film.title)
                    continue
                events = [e for e in events if e.title != film.title] + film_events
                raw_count += film_raw - film.listed_count

        self.raw_count = raw_count
        logger.info(
            "[VIFF] raw=%d  in-range=%d  (%d film page(s) for extra dates)  for %s → %s",
            raw_count, len(events), len(films), start_date, end_date,
        )
        return events
