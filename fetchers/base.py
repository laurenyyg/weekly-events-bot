"""Base interface that all event fetchers must implement."""

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date
from typing import Callable, Optional

import requests

logger = logging.getLogger(__name__)

_DESCRIPTION_MAX_CHARS = 500


@dataclass
class Event:
    """Represents a single event."""
    title: str
    date: str           # ISO format: YYYY-MM-DD
    time: str           # e.g. "7:00 PM" or "TBD"
    location: str
    description: str
    url: str
    source: str         # Name of the fetcher that produced this event
    tags: list[str] = field(default_factory=list)
    pre_approved: bool = False  # source is on-topic by definition; skip keyword matching

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "date": self.date,
            "time": self.time,
            "location": self.location,
            "description": self.description,
            "url": self.url,
            "source": self.source,
            "tags": self.tags,
        }

    def __str__(self) -> str:
        return f"[{self.source}] {self.title} on {self.date} @ {self.location}"


def fill_descriptions(
    events: list[Event],
    parse_description: Callable[[str], str],
    headers: dict[str, str],
    source: str,
) -> None:
    """Fill each event's empty description from its detail page, in place.

    Listing pages often carry only titles, which leaves the LLM guessing. Each
    unique URL is fetched once (repeat screenings share a page). A page that
    fails to load or parse is logged and skipped; the event keeps "".

    Args:
        events:            Events to update.
        parse_description: Extracts description text from a detail page's HTML.
        headers:           HTTP headers for the detail-page requests.
        source:            Fetcher name, used in log messages.
    """
    descriptions: dict[str, str] = {}
    with requests.Session() as session:
        session.headers.update(headers)
        for url in {e.url for e in events if not e.description}:
            try:
                resp = session.get(url, timeout=10)
                resp.raise_for_status()
                descriptions[url] = parse_description(resp.text)[:_DESCRIPTION_MAX_CHARS]
            except Exception as exc:
                logger.warning("[%s] Could not fetch description from %s: %s", source, url, exc)

    for event in events:
        if not event.description:
            event.description = descriptions.get(event.url, "")
    found = sum(1 for e in events if e.description)
    logger.info("[%s] descriptions: %d / %d event(s)", source, found, len(events))


class BaseEventFetcher(ABC):
    """
    Abstract base class for all Vancouver event fetchers.

    Subclasses must implement:
      - name:          A human-readable identifier for the source (class attribute).
      - fetch_events:  Returns a list of Event objects for the given date range.
    """

    name: str = "UnnamedFetcher"

    @abstractmethod
    def fetch_events(self, start_date: date, end_date: date) -> list[Event]:
        """
        Fetch events between start_date and end_date (inclusive).

        Args:
            start_date: First day of the range (Monday of upcoming week).
            end_date:   Last day of the range (Sunday of upcoming week).

        Returns:
            A list of Event objects. May be empty but must not raise.

        Raises:
            Any exception is allowed to propagate; the orchestrator will catch it.
        """
        ...
