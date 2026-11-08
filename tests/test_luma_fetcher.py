"""
Tests for LumaFetcher (iCal calendar feeds).

Uses an in-memory iCal payload mirroring Luma's feed format, so the parser
can be verified without network access. Run with:

    pytest tests/test_luma_fetcher.py -v
"""

from datetime import date, datetime, timezone
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest
import requests

from fetchers.luma_fetcher import LumaFetcher

START = date(2026, 11, 9)
END = date(2026, 11, 22)


def _vevent(uid: str, summary: str, dtstart: str, geo: str | None = "49.28;-123.12",
            location: str = "555 W Hastings St, Vancouver, BC", status: str | None = None) -> str:
    lines = [
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"SUMMARY:{summary}",
        f"DTSTART:{dtstart}",
        f"LOCATION:{location}",
        f"DESCRIPTION:Get up-to-date information at: https://luma.com/{uid}\\n\\nAddress:\\n"
        f"{location}\\n\\nA talk about {summary}.\\n\\nHosted by Someone",
    ]
    if geo:
        lines.append(f"GEO:{geo}")
    if status:
        lines.append(f"STATUS:{status}")
    lines.append("END:VEVENT")
    return "\r\n".join(lines)


def _feed(*vevents: str) -> bytes:
    body = "\r\n".join(["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Luma//EN", *vevents, "END:VCALENDAR"])
    return body.encode()


def _response(content: bytes) -> MagicMock:
    resp = MagicMock()
    resp.content = content
    resp.raise_for_status.return_value = None
    return resp


FEED = _feed(
    _vevent("ai-meetup", "Vancouver AI Meetup", "20261113T020000Z"),  # Nov 12 evening, Vancouver
    _vevent("seoul", "Meetup in Seoul", "20261112T100000Z", geo="37.50;127.03"),
    _vevent("postponed", "P O S T P O N E D", "20261118T020000Z"),
    _vevent("cancelled", "Data Night", "20261119T020000Z", status="CANCELLED"),
    _vevent("old", "Last Month's Meetup", "20261010T020000Z"),
    _vevent("hidden", "Founder Breakfast", "20261114T170000Z",
            location="https://luma.com/event/evt-xyz"),
)


@pytest.fixture
def fetched() -> tuple[LumaFetcher, list]:
    fetcher = LumaFetcher("BC + AI Events", "cal-abc")
    with patch("fetchers.luma_fetcher.requests.get", return_value=_response(FEED)) as get:
        events = fetcher.fetch_events(START, END)
    assert "id=cal-abc" in get.call_args.args[0]
    return fetcher, events


def test_keeps_only_in_area_active_events(fetched) -> None:
    _, events = fetched
    assert [e.title for e in events] == ["Vancouver AI Meetup", "Founder Breakfast"]


def test_raw_count_includes_filtered_events(fetched) -> None:
    fetcher, _ = fetched
    # In range: AI meetup, Seoul, postponed, cancelled, Founder Breakfast.
    assert fetcher.raw_count == 5
    assert fetcher.empty_ok


def test_event_fields_parsed(fetched) -> None:
    _, events = fetched
    meetup = events[0]
    local = datetime(2026, 11, 13, 2, tzinfo=timezone.utc).astimezone(ZoneInfo("America/Vancouver"))
    assert meetup.date == local.strftime("%Y-%m-%d")
    assert meetup.time == local.strftime("%I:%M %p").lstrip("0")
    assert meetup.url == "https://luma.com/ai-meetup"
    assert meetup.description == "A talk about Vancouver AI Meetup."
    assert meetup.source == "Luma: BC + AI Events"


def test_obfuscated_address_notes_rsvp(fetched) -> None:
    _, events = fetched
    assert events[1].location == "Vancouver, BC (address shown after RSVP)"


def test_quiet_calendar_is_not_breakage() -> None:
    fetcher = LumaFetcher("Quiet", "cal-q")
    feed = _feed(_vevent("old", "Last Month's Meetup", "20261010T020000Z"))
    with patch("fetchers.luma_fetcher.requests.get", return_value=_response(feed)):
        assert fetcher.fetch_events(START, END) == []
    assert fetcher.empty_ok


def test_empty_feed_is_flagged() -> None:
    fetcher = LumaFetcher("Empty", "cal-e")
    with patch("fetchers.luma_fetcher.requests.get", return_value=_response(_feed())):
        assert fetcher.fetch_events(START, END) == []
    assert not fetcher.empty_ok


def test_http_error_propagates() -> None:
    resp = MagicMock()
    resp.raise_for_status.side_effect = requests.HTTPError("404")
    with patch("fetchers.luma_fetcher.requests.get", return_value=resp):
        with pytest.raises(requests.HTTPError):
            LumaFetcher("Broken", "cal-b").fetch_events(START, END)
