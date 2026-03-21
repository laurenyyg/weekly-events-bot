"""Base interface that all event fetchers must implement."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date
from typing import Optional


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
