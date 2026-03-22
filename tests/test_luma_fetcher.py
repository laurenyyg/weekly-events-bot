"""
Tests for LumaFetcher.

Uses mock JSON payloads (mirroring the real Luma API responses) so the parser
logic can be verified without any network access.  Run with:

    pytest tests/test_luma_fetcher.py -v
"""

from datetime import date
from unittest.mock import MagicMock, call, patch

import pytest

from fetchers.luma_fetcher import LumaFetcher, _extract_text_from_mirror

# ---------------------------------------------------------------------------
# Shared mock payloads
# ---------------------------------------------------------------------------

# Discovery endpoint response with three events:
#   1. Vancouver AI Meetup  — in area, full address visible
#   2. Chilliwack Market    — out of area, should be geo-filtered
#   3. UBC Workshop         — in area (University Endowment Lands), obfuscated addr
#   4. Online Webinar       — online, always included
#
# All start_at times are UTC; Vancouver is PDT (UTC-7) during late March 2026.
#   2026-03-24T02:00:00Z  =  Mon Mar 23 @ 7:00 PM PDT
#   2026-03-25T19:00:00Z  =  Tue Mar 25 @ 12:00 PM PDT
#   2026-03-26T01:30:00Z  =  Mon Mar 25 @ 6:30 PM PDT
#   2026-03-27T02:00:00Z  =  Wed Mar 26 @ 7:00 PM PDT

MOCK_DISCOVERY = {
    "entries": [
        {
            "api_id": "evt-aaa",
            "event": {
                "api_id": "evt-aaa",
                "name": "Vancouver AI Meetup",
                "start_at": "2026-03-24T02:00:00.000Z",
                "timezone": "America/Vancouver",
                "url": "van-ai-meetup",
                "location_type": "offline",
                "geo_address_info": {
                    "mode": "shown",
                    "city": "Vancouver",
                    "full_address": "555 W Hastings St, Vancouver, BC V6B 4N4, Canada",
                    "short_address": "555 W Hastings St, Vancouver",
                },
                "geo_address_visibility": "public",
            },
            "calendar": {"description_short": "Monthly AI meetup."},
        },
        {
            "api_id": "evt-bbb",
            "event": {
                "api_id": "evt-bbb",
                "name": "Chilliwack Farmers Market",
                "start_at": "2026-03-25T17:00:00.000Z",
                "timezone": "America/Vancouver",
                "url": "chilliwack-market",
                "location_type": "offline",
                "geo_address_info": {
                    "mode": "shown",
                    "city": "Chilliwack",
                    "full_address": "45905 Wellington Ave, Chilliwack, BC V2P 2C9, Canada",
                },
                "geo_address_visibility": "public",
            },
            "calendar": {},
        },
        {
            "api_id": "evt-ccc",
            "event": {
                "api_id": "evt-ccc",
                "name": "UBC Data Science Workshop",
                "start_at": "2026-03-26T01:30:00.000Z",
                "timezone": "America/Vancouver",
                "url": "ubc-ds-workshop",
                "location_type": "offline",
                "geo_address_info": {
                    "mode": "obfuscated",
                    "city": "University Endowment Lands",
                    "city_state": "University Endowment Lands, British Columbia",
                },
                "geo_address_visibility": "guests-only",
            },
            "calendar": {"description_short": "UBC data science series."},
        },
        {
            "api_id": "evt-ddd",
            "event": {
                "api_id": "evt-ddd",
                "name": "Online Tech Webinar",
                "start_at": "2026-03-27T02:00:00.000Z",
                "timezone": "America/Vancouver",
                "url": "online-webinar",
                "location_type": "online",
                "geo_address_info": None,
                "geo_address_visibility": "public",
            },
            "calendar": {},
        },
    ],
    "has_more": False,
    "next_cursor": None,
}

MOCK_DETAIL_WITH_DESCRIPTION = {
    "description_mirror": {
        "type": "doc",
        "content": [
            {
                "type": "paragraph",
                "content": [
                    {"type": "text", "text": "Join us for an exciting AI event "},
                    {"type": "text", "text": "in Vancouver.", "marks": [{"type": "bold"}]},
                ],
            },
            {
                "type": "paragraph",
                "content": [{"type": "text", "text": "All skill levels welcome."}],
            },
        ],
    }
}

MOCK_DETAIL_NO_DESCRIPTION: dict = {"description_mirror": None}


def _mock_response(data: dict) -> MagicMock:
    """Build a mock requests.Response that returns *data* from .json()."""
    m = MagicMock()
    m.json.return_value = data
    m.raise_for_status.return_value = None
    return m


def _discovery_side_effect(url: str, **kwargs) -> MagicMock:
    """Route mock requests to the appropriate payload based on URL."""
    if "discover" in url:
        return _mock_response(MOCK_DISCOVERY)
    # Detail endpoint — return description for evt-aaa, nothing for others.
    params = kwargs.get("params", {})
    if params.get("event_api_id") == "evt-aaa":
        return _mock_response(MOCK_DETAIL_WITH_DESCRIPTION)
    return _mock_response(MOCK_DETAIL_NO_DESCRIPTION)


# ---------------------------------------------------------------------------
# Unit tests — pure parsing helpers
# ---------------------------------------------------------------------------

class TestExtractTextFromMirror:
    def test_simple_paragraph(self) -> None:
        doc = {
            "type": "doc",
            "content": [
                {
                    "type": "paragraph",
                    "content": [{"type": "text", "text": "Hello world."}],
                }
            ],
        }
        assert _extract_text_from_mirror(doc) == "Hello world."

    def test_multiple_paragraphs_joined(self) -> None:
        doc = {
            "type": "doc",
            "content": [
                {
                    "type": "paragraph",
                    "content": [{"type": "text", "text": "First paragraph."}],
                },
                {
                    "type": "paragraph",
                    "content": [{"type": "text", "text": "Second paragraph."}],
                },
            ],
        }
        result = _extract_text_from_mirror(doc)
        assert "First paragraph." in result
        assert "Second paragraph." in result

    def test_empty_doc_returns_empty_string(self) -> None:
        assert _extract_text_from_mirror({}) == ""
        assert _extract_text_from_mirror(None) == ""  # type: ignore[arg-type]

    def test_bold_marks_ignored_text_preserved(self) -> None:
        doc = {
            "type": "doc",
            "content": [
                {
                    "type": "paragraph",
                    "content": [
                        {"type": "text", "text": "plain "},
                        {"type": "text", "text": "bold", "marks": [{"type": "bold"}]},
                    ],
                }
            ],
        }
        assert "plain" in _extract_text_from_mirror(doc)
        assert "bold" in _extract_text_from_mirror(doc)


# ---------------------------------------------------------------------------
# Integration tests — LumaFetcher with mocked HTTP
# ---------------------------------------------------------------------------

class TestLumaFetcher:
    START = date(2026, 3, 23)
    END = date(2026, 3, 29)

    @patch("fetchers.luma_fetcher.requests.get")
    def test_returns_only_in_area_events(self, mock_get: MagicMock) -> None:
        """Chilliwack event must be geo-filtered; three others returned."""
        mock_get.side_effect = _discovery_side_effect
        events = LumaFetcher().fetch_events(self.START, self.END)
        titles = [e.title for e in events]
        assert "Chilliwack Farmers Market" not in titles
        assert len(events) == 3

    @patch("fetchers.luma_fetcher.requests.get")
    def test_title_parsed_correctly(self, mock_get: MagicMock) -> None:
        mock_get.side_effect = _discovery_side_effect
        events = LumaFetcher().fetch_events(self.START, self.END)
        titles = [e.title for e in events]
        assert "Vancouver AI Meetup" in titles
        assert "UBC Data Science Workshop" in titles
        assert "Online Tech Webinar" in titles

    @patch("fetchers.luma_fetcher.requests.get")
    def test_utc_to_vancouver_date_conversion(self, mock_get: MagicMock) -> None:
        """2026-03-24T02:00Z == 2026-03-23 19:00 PDT (UTC-7)."""
        mock_get.side_effect = _discovery_side_effect
        events = LumaFetcher().fetch_events(self.START, self.END)
        ai_event = next(e for e in events if e.title == "Vancouver AI Meetup")
        assert ai_event.date == "2026-03-23"
        assert ai_event.time == "7:00 PM"

    @patch("fetchers.luma_fetcher.requests.get")
    def test_shown_address_used_as_location(self, mock_get: MagicMock) -> None:
        mock_get.side_effect = _discovery_side_effect
        events = LumaFetcher().fetch_events(self.START, self.END)
        ai_event = next(e for e in events if e.title == "Vancouver AI Meetup")
        assert ai_event.location == "555 W Hastings St, Vancouver, BC V6B 4N4, Canada"

    @patch("fetchers.luma_fetcher.requests.get")
    def test_obfuscated_address_notes_rsvp(self, mock_get: MagicMock) -> None:
        mock_get.side_effect = _discovery_side_effect
        events = LumaFetcher().fetch_events(self.START, self.END)
        ubc = next(e for e in events if "UBC" in e.title)
        assert "RSVP" in ubc.location
        assert "University Endowment Lands" in ubc.location

    @patch("fetchers.luma_fetcher.requests.get")
    def test_online_event_location(self, mock_get: MagicMock) -> None:
        mock_get.side_effect = _discovery_side_effect
        events = LumaFetcher().fetch_events(self.START, self.END)
        webinar = next(e for e in events if "Webinar" in e.title)
        assert webinar.location == "Online (Virtual)"

    @patch("fetchers.luma_fetcher.requests.get")
    def test_description_fetched_from_detail_endpoint(self, mock_get: MagicMock) -> None:
        mock_get.side_effect = _discovery_side_effect
        events = LumaFetcher().fetch_events(self.START, self.END)
        ai_event = next(e for e in events if e.title == "Vancouver AI Meetup")
        assert "Vancouver" in ai_event.description
        assert "AI" in ai_event.description or "exciting" in ai_event.description

    @patch("fetchers.luma_fetcher.requests.get")
    def test_calendar_description_fallback(self, mock_get: MagicMock) -> None:
        """Events with no detail description fall back to calendar.description_short."""
        mock_get.side_effect = _discovery_side_effect
        events = LumaFetcher().fetch_events(self.START, self.END)
        ubc = next(e for e in events if "UBC" in e.title)
        assert ubc.description == "UBC data science series."

    @patch("fetchers.luma_fetcher.requests.get")
    def test_url_constructed_correctly(self, mock_get: MagicMock) -> None:
        mock_get.side_effect = _discovery_side_effect
        events = LumaFetcher().fetch_events(self.START, self.END)
        ai_event = next(e for e in events if e.title == "Vancouver AI Meetup")
        assert ai_event.url == "https://lu.ma/van-ai-meetup"

    @patch("fetchers.luma_fetcher.requests.get")
    def test_source_is_luma(self, mock_get: MagicMock) -> None:
        mock_get.side_effect = _discovery_side_effect
        events = LumaFetcher().fetch_events(self.START, self.END)
        assert all(e.source == "Luma" for e in events)

    @patch("fetchers.luma_fetcher.requests.get")
    def test_raw_count_tracked(self, mock_get: MagicMock) -> None:
        """raw_count should include out-of-area events; len(events) should not."""
        mock_get.side_effect = _discovery_side_effect
        fetcher = LumaFetcher()
        events = fetcher.fetch_events(self.START, self.END)
        # 4 entries are in the date range; 1 is geo-filtered (Chilliwack)
        assert fetcher.raw_count == 4
        assert len(events) == 3

    @patch("fetchers.luma_fetcher.requests.get")
    def test_out_of_range_events_excluded(self, mock_get: MagicMock) -> None:
        """Requesting a window that excludes all mock events returns empty list."""
        mock_get.side_effect = _discovery_side_effect
        # Request a week entirely in the past relative to our mock data.
        events = LumaFetcher().fetch_events(date(2026, 3, 2), date(2026, 3, 8))
        assert events == []

    @patch("fetchers.luma_fetcher.requests.get")
    def test_no_crash_on_missing_geo(self, mock_get: MagicMock) -> None:
        """Events with null geo_address_info must not raise."""
        discovery = {
            "entries": [
                {
                    "api_id": "evt-nogeo",
                    "event": {
                        "api_id": "evt-nogeo",
                        "name": "Mystery Event",
                        "start_at": "2026-03-24T02:00:00.000Z",
                        "timezone": "America/Vancouver",
                        "url": "mystery",
                        "location_type": "offline",
                        "geo_address_info": None,
                        "geo_address_visibility": "public",
                    },
                    "calendar": {},
                }
            ],
            "has_more": False,
            "next_cursor": None,
        }
        mock_get.side_effect = lambda url, **kw: _mock_response(
            discovery if "discover" in url else MOCK_DETAIL_NO_DESCRIPTION
        )
        # city="" for null geo → not in _ALLOWED_CITIES → geo-filtered out, no crash
        events = LumaFetcher().fetch_events(self.START, self.END)
        assert isinstance(events, list)
