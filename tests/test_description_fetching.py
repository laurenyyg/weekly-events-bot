"""Tests for detail-page description parsing and filling (no network calls)."""

from unittest.mock import MagicMock, patch

from fetchers import Event
from fetchers.base import fill_descriptions
from fetchers.chancentre_fetcher import parse_chancentre_description
from fetchers.cinematheque_fetcher import parse_cinematheque_description

_CINEMATHEQUE_PAGE = """<html><head>
<meta name="description" content="Personal integrity contends with empty stomach in Receptions.">
</head><body><p>Streaming December 1 through December 20</p></body></html>"""

_CHANCENTRE_PAGE = """<html><head></head><body>
<div class="layout-sidebar"><div class="layout-sidebar--content">
  <div class="mobile-event-tickets"><div class="vh30 desc">
    <p>Members get exclusive benefits like early access.</p>
  </div></div>
  <p class="p1">Allison Russell is a Grammy-winning artist.</p>
  <p>She co-founded Our Native Daughters.</p>
</div></div>
<footer><p>Sign up to receive all the latest news.</p></footer>
</body></html>"""


def _event(url: str, description: str = "") -> Event:
    return Event(
        title="T", date="2026-11-14", time="7:00 PM", location="Vancouver",
        description=description, url=url, source="test",
    )


def test_cinematheque_uses_meta_description() -> None:
    assert parse_cinematheque_description(_CINEMATHEQUE_PAGE) == (
        "Personal integrity contends with empty stomach in Receptions."
    )


def test_cinematheque_without_meta_returns_empty() -> None:
    assert parse_cinematheque_description("<html><body><p>Hi</p></body></html>") == ""


def test_chancentre_body_paragraphs_skip_promo_and_footer() -> None:
    assert parse_chancentre_description(_CHANCENTRE_PAGE) == (
        "Allison Russell is a Grammy-winning artist. She co-founded Our Native Daughters."
    )


def test_chancentre_description_in_bare_div() -> None:
    html = """<div class="layout-sidebar--content">
      <div class="section vh40">Early Music Vancouver The Magic of Monteverdi</div>
      <div class="mobile-event-tickets">Buy tickets Tue Nov 17</div>
      <div>Join Baroque masters William Christie and Paul Agnew.</div>
    </div>"""
    assert parse_chancentre_description(html) == "Join Baroque masters William Christie and Paul Agnew."


def test_chancentre_prefers_meta_description() -> None:
    html = '<html><head><meta name="description" content="Ta-Nehisi Coates comes to Vancouver."></head></html>'
    assert parse_chancentre_description(html) == "Ta-Nehisi Coates comes to Vancouver."


@patch("fetchers.base.requests.Session")
def test_fill_descriptions_fetches_each_url_once_and_survives_failures(session_cls: MagicMock) -> None:
    def get(url: str, timeout: int) -> MagicMock:
        if url == "https://bad":
            raise ConnectionError("boom")
        return MagicMock(text=_CINEMATHEQUE_PAGE)

    session = session_cls.return_value.__enter__.return_value
    session.get.side_effect = get
    events = [_event("https://film"), _event("https://film"), _event("https://bad"), _event("https://kept", "Kept.")]

    fill_descriptions(events, parse_cinematheque_description, {}, "test")

    assert session.get.call_count == 2
    assert events[0].description == events[1].description != ""
    assert events[2].description == ""
    assert events[3].description == "Kept."
