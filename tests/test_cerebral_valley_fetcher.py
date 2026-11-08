"""
Tests for CerebralValleyFetcher.

Uses mock API payloads mirroring api.cerebralvalley.ai/v1/public/event/pull,
so the parser can be verified without network access. Run with:

    pytest tests/test_cerebral_valley_fetcher.py -v
"""

from datetime import date, datetime, timezone
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest
import requests

from fetchers.cerebral_valley_fetcher import CerebralValleyFetcher

START = date(2026, 11, 9)
END = date(2026, 11, 22)
VANCOUVER = ZoneInfo("America/Vancouver")


def _utc_for_local(day: int, hour: int, minute: int = 0) -> str:
    """API timestamp (UTC) for a given Vancouver local time in November 2026."""
    local = datetime(2026, 11, day, hour, minute, tzinfo=VANCOUVER)
    return local.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _item(name: str, start: str, location: str = "Remote", description: str = "About it.",
          url: str = "https://luma.com/abc?tab=guest-list") -> dict:
    return {"name": name, "startDateTime": start, "endDateTime": start, "location": location,
            "description": description, "descriptionSummary": "Summary.", "url": url}


def _page(events: list[dict], last: bool = True) -> MagicMock:
    resp = MagicMock()
    resp.json.return_value = {"events": events, "totalCount": len(events), "lastPage": last}
    resp.raise_for_status.return_value = None
    return resp


def _fetch(*pages: MagicMock) -> tuple[CerebralValleyFetcher, list, MagicMock]:
    fetcher = CerebralValleyFetcher()
    with patch("fetchers.cerebral_valley_fetcher.requests.Session") as session_cls:
        get = session_cls.return_value.__enter__.return_value.get
        get.side_effect = list(pages)
        events = fetcher.fetch_events(START, END)
    return fetcher, events, get


def test_local_time_conversion_and_fields() -> None:
    _, events, get = _fetch(_page([_item("AI Roundtable", _utc_for_local(9, 11))]))
    e = events[0]
    assert (e.date, e.time) == ("2026-11-09", "11:00 AM")
    assert e.location == "Online"
    assert e.url == "https://luma.com/abc"
    assert e.source == "Cerebral Valley (remote)"
    assert e.pre_approved
    params = get.call_args.kwargs["params"]
    assert params["locations"] == "REMOTE"
    assert params["startDateTime"].endswith("Z")


def test_start_hour_boundaries() -> None:
    _, events, _ = _fetch(_page([
        _item("Too early", _utc_for_local(10, 9, 59)),
        _item("Opens", _utc_for_local(10, 10, 0)),
        _item("Closes", _utc_for_local(10, 20, 0)),
        _item("Too late", _utc_for_local(10, 20, 1)),
    ]))
    assert [e.title for e in events] == ["Opens", "Closes"]


def test_ongoing_and_other_location_dropped() -> None:
    fetcher, events, _ = _fetch(_page([
        _item("Started last week", _utc_for_local(2, 12)),
        _item("AI Builders Vienna", _utc_for_local(12, 12), location="Other"),
        _item("Kept", _utc_for_local(12, 13)),
    ]))
    assert [e.title for e in events] == ["Kept"]
    assert fetcher.raw_count == 2  # Vienna counts as in range before the location rule
    assert fetcher.empty_ok


def test_pages_until_last_page() -> None:
    _, events, get = _fetch(
        _page([_item("First", _utc_for_local(11, 12))], last=False),
        _page([_item("Second", _utc_for_local(12, 12))], last=True),
    )
    assert [e.title for e in events] == ["First", "Second"]
    assert [c.kwargs["params"]["offset"] for c in get.call_args_list] == [0, 100]


def test_description_capped_with_summary_fallback() -> None:
    _, events, _ = _fetch(_page([
        _item("Long", _utc_for_local(11, 12), description="word " * 300),
        _item("Blank", _utc_for_local(11, 13), description=""),
    ]))
    assert len(events[0].description) == 500
    assert events[1].description == "Summary."


def test_empty_window_is_not_breakage() -> None:
    fetcher, events, _ = _fetch(_page([]))
    assert events == []
    assert fetcher.empty_ok


def test_http_error_propagates() -> None:
    resp = MagicMock()
    resp.raise_for_status.side_effect = requests.HTTPError("503")
    with pytest.raises(requests.HTTPError):
        _fetch(resp)
