"""Tests for the VIFF fetcher: listing pages, film pages, and pagination (no network calls)."""

from datetime import date
from unittest.mock import MagicMock, patch

import pytest

from fetchers import viff_fetcher
from fetchers.viff_fetcher import ViffFetcher, ViffFilm, parse_viff_film_page, parse_viff_html

_RANGE = (date(2026, 11, 9), date(2026, 11, 22))


class _FixedDate(date):
    @classmethod
    def today(cls) -> date:
        return cls(2026, 11, 7)


@pytest.fixture(autouse=True)
def _fixed_today():
    with patch.object(viff_fetcher, "date", _FixedDate):
        yield


def _instance(day: str, time: str, booking: str) -> str:
    return f"""<div class="c-event-instance">
      <div class="c-event-instance__time">{time}</div>
      <div class="c-event-instance__date"><span>{day}</span></div>
      <span class="c-event-instance__venue">VIFF Centre - VIFF Cinema</span>
      <a class="c-event-instance__btn" href="https://viff.org/book/{booking}">Book</a>
    </div>"""


def _card(title: str, slug: str, instances: str, more_dates: bool = False) -> str:
    more = f'<a href="https://viff.org/whats-on/{slug}/#book">View 3 more dates</a>' if more_dates else ""
    return f"""<div class="c-event-card">
      <h3 class="c-event-card__title"><a href="https://viff.org/whats-on/{slug}/">{title}</a></h3>
      <p class="c-event-card__excerpt">About {title}.</p>
      <div class="c-event-card__seasons"><a>Pantheon</a></div>
      {instances}{more}
    </div>"""


_FILM_PAGE = """<section class="c-event__instances">
  <div class="c-event-instance__date-group"><h4>Monday November 09</h4>
    <div class="c-event-instance"><div class="c-event-instance__time">2:30 pm</div>
      <a class="c-event-instance__btn" href="https://viff.org/book/a">Book</a></div>
  </div>
  <div class="c-event-instance__date-group"><h4>Saturday November 21</h4>
    <div class="c-event-instance"><div class="c-event-instance__time">1:00 pm</div>
      <a class="c-event-instance__btn" href="https://viff.org/book/b">Book</a></div>
    <div class="c-event-instance"><div class="c-event-instance__time">7:45 pm</div>
      <a class="c-event-instance__btn" href="https://viff.org/book/c">Book</a></div>
  </div>
  <div class="c-event-instance__date-group"><h4>Monday November 30</h4>
    <div class="c-event-instance"><div class="c-event-instance__time">6:00 pm</div></div>
  </div>
</section>"""


def test_listing_page_parses_cards_and_flags_hidden_dates() -> None:
    html = _card("Sunless", "sunless", _instance("Sun Nov 15", "11:00 am", "s1")) + _card(
        "Raise the Red Lantern", "red-lantern", _instance("Mon Nov 09", "2:30 pm", "r1"), more_dates=True
    )
    listing = parse_viff_html(html, *_RANGE)

    assert listing.card_count == 2 and listing.raw_count == 2
    sunless = listing.events[0]
    assert (sunless.date, sunless.time, sunless.url) == ("2026-11-15", "11:00 AM", "https://viff.org/book/s1")
    assert sunless.description == "About Sunless." and sunless.tags == ["Pantheon"]
    assert [f.title for f in listing.films_with_more_dates] == ["Raise the Red Lantern"]


def test_film_page_reads_every_date_group() -> None:
    film = ViffFilm(title="Raise the Red Lantern", url="https://viff.org/whats-on/red-lantern/",
                    description="About it.", tags=[])
    events, raw = parse_viff_film_page(_FILM_PAGE, film, *_RANGE)

    assert raw == 4
    assert [(e.date, e.time) for e in events] == [
        ("2026-11-09", "2:30 PM"), ("2026-11-21", "1:00 PM"), ("2026-11-21", "7:45 PM"),
    ]
    assert events[-1].description == "About it."


def _response(text: str, status: int = 200) -> MagicMock:
    return MagicMock(text=text, status_code=status)


@patch("fetchers.viff_fetcher.requests.Session")
def test_fetch_reads_all_pages_and_replaces_card_dates(session_cls: MagicMock) -> None:
    pages = {
        viff_fetcher._EVENTS_URL: _card("Sunless", "sunless", _instance("Sun Nov 15", "11:00 am", "s1")),
        f"{viff_fetcher._EVENTS_URL}page/2/": _card(
            "Raise the Red Lantern", "red-lantern", _instance("Mon Nov 09", "2:30 pm", "a"), more_dates=True
        ),
        f"{viff_fetcher._EVENTS_URL}page/3/": "<html></html>",
        "https://viff.org/whats-on/red-lantern/": _FILM_PAGE,
    }
    session = session_cls.return_value.__enter__.return_value
    session.get.side_effect = lambda url, timeout: _response(pages[url])

    fetcher = ViffFetcher()
    events = fetcher.fetch_events(*_RANGE)

    assert sorted((e.title, e.date, e.time) for e in events) == [
        ("Raise the Red Lantern", "2026-11-09", "2:30 PM"),
        ("Raise the Red Lantern", "2026-11-21", "1:00 PM"),
        ("Raise the Red Lantern", "2026-11-21", "7:45 PM"),
        ("Sunless", "2026-11-15", "11:00 AM"),
    ]
    assert fetcher.raw_count == 5


@patch("fetchers.viff_fetcher.requests.Session")
def test_failed_film_page_keeps_card_dates(session_cls: MagicMock) -> None:
    listing = _card("Raise the Red Lantern", "red-lantern", _instance("Mon Nov 09", "2:30 pm", "a"), more_dates=True)

    def get(url: str, timeout: int) -> MagicMock:
        if url == viff_fetcher._EVENTS_URL:
            return _response(listing)
        if url.endswith("page/2/"):
            return _response("", status=404)
        raise ConnectionError("boom")

    session_cls.return_value.__enter__.return_value.get.side_effect = get
    events = ViffFetcher().fetch_events(*_RANGE)
    assert [(e.title, e.date) for e in events] == [("Raise the Red Lantern", "2026-11-09")]
