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
          url: str = "https://luma.com/abc?tab=guest-list", end: str | None = None,
          venue: str | None = None) -> dict:
    return {"id": f"{name}|{start}", "name": name, "startDateTime": start,
            "endDateTime": end or start, "location": location, "venue": venue,
            "description": description, "descriptionSummary": "Summary.", "url": url}


def _page(events: list[dict], last: bool = True) -> MagicMock:
    resp = MagicMock()
    resp.json.return_value = {"events": events, "totalCount": len(events), "lastPage": last}
    resp.raise_for_status.return_value = None
    return resp


def _fetch(*remote_pages: MagicMock, vancouver: tuple[MagicMock, ...] = ()
           ) -> tuple[CerebralValleyFetcher, list, MagicMock]:
    """Run the fetcher; remote pages are served first, then Vancouver pages (default: empty)."""
    fetcher = CerebralValleyFetcher()
    with patch("fetchers.cerebral_valley_fetcher.requests.Session") as session_cls:
        get = session_cls.return_value.__enter__.return_value.get
        get.side_effect = list(remote_pages) + list(vancouver or (_page([]),))
        events = fetcher.fetch_events(START, END)
    return fetcher, events, get


def test_local_time_conversion_and_fields() -> None:
    _, events, get = _fetch(_page([_item("AI Roundtable", _utc_for_local(9, 11))]))
    e = events[0]
    assert (e.date, e.time) == ("2026-11-09", "11:00 AM")
    assert e.location == "Online"
    assert e.url == "https://luma.com/abc"
    assert e.source == "Cerebral Valley (remote + Vancouver)"
    assert e.pre_approved
    locations = [c.kwargs["params"]["locations"] for c in get.call_args_list]
    assert locations == ["REMOTE", "Vancouver"]
    assert get.call_args.kwargs["params"]["startDateTime"].endswith("Z")


def test_start_hour_boundaries() -> None:
    _, events, _ = _fetch(_page([
        _item("Too early", _utc_for_local(10, 9, 59)),
        _item("Opens", _utc_for_local(10, 10, 0)),
        _item("Closes", _utc_for_local(10, 20, 0)),
        _item("Too late", _utc_for_local(10, 20, 1)),
    ]))
    assert [e.title for e in events] == ["Opens", "Closes"]


def test_long_remote_event_skips_start_hour_rule() -> None:
    _, events, _ = _fetch(_page([
        _item("NASA Space Apps", _utc_for_local(14, 7), end=_utc_for_local(15, 14)),
        _item("Short early workshop", _utc_for_local(14, 7), end=_utc_for_local(14, 9)),
    ]))
    assert [e.title for e in events] == ["NASA Space Apps"]


def test_vancouver_events_kept_at_any_hour_with_venue() -> None:
    _, events, _ = _fetch(_page([]), vancouver=(_page([
        _item("AI Skills Workshop", _utc_for_local(11, 16, 30), location="Vancouver, Canada",
              venue="177 E 3rd Ave"),
        _item("Late Builders Night", _utc_for_local(12, 21), location="Vancouver, Canada"),
        _item("PDX AI Meetup", _utc_for_local(12, 18), location="Vancouver, WA"),
    ]),))
    assert [(e.title, e.location) for e in events] == [
        ("AI Skills Workshop", "177 E 3rd Ave"),
        ("Late Builders Night", "Vancouver, Canada"),
    ]
    assert all(e.pre_approved for e in events)


def test_event_in_both_pulls_kept_once() -> None:
    item = _item("Hybrid Hack", _utc_for_local(12, 12))
    _, events, _ = _fetch(_page([item]), vancouver=(_page([item]),))
    assert [e.title for e in events] == ["Hybrid Hack"]


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
    assert [c.kwargs["params"]["offset"] for c in get.call_args_list] == [0, 100, 0]


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
