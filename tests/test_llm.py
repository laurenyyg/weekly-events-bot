"""
Tests for the DeepSeek LLM layer in main.py.

The Anthropic SDK client is mocked, so no network access or API key is needed.
Run with:

    pytest tests/test_llm.py -v
"""

import json
from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import anthropic
import httpx
import pytest

import main
from fetchers import Event

_REQUEST = httpx.Request("POST", "https://api.deepseek.com/anthropic/v1/messages")


def _status_error(cls: type, code: int) -> anthropic.APIStatusError:
    return cls(f"Error code: {code}", response=httpx.Response(code, request=_REQUEST), body=None)


def _message(text: str, input_tokens: int = 100, cached: int = 0, output: int = 50,
             stop_reason: str | None = "end_turn") -> SimpleNamespace:
    return SimpleNamespace(
        model="deepseek-v4-pro",
        stop_reason=stop_reason,
        content=[SimpleNamespace(type="text", text=text)],
        usage=SimpleNamespace(
            input_tokens=input_tokens, cache_read_input_tokens=cached, output_tokens=output
        ),
    )


def _client(*outcomes) -> MagicMock:
    """Build a fake client whose successive stream() calls yield or raise `outcomes`."""
    client = MagicMock()
    streams = []
    for outcome in outcomes:
        stream = MagicMock()
        if isinstance(outcome, Exception):
            stream.__enter__.side_effect = outcome
        else:
            stream.__enter__.return_value.get_final_message.return_value = outcome
        streams.append(stream)
    client.messages.stream.side_effect = streams
    return client


def _event(title: str, description: str = "") -> Event:
    return Event(
        title=title, date="2026-11-14", time="7:00 PM", location="Vancouver",
        description=description, url="https://example.com", source="test", tags=[],
    )


# ---------------------------------------------------------------------------
# Pricing
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("iso, expected", [
    ("2026-11-08T04:00:00+00:00", True),   # Sunday — tech cron
    ("2026-11-08T08:05:00+00:00", True),   # Sunday — fun cron
    ("2026-11-09T02:00:00+00:00", False),  # Monday 02:00 — peak block 1
    ("2026-11-09T04:30:00+00:00", True),   # Monday 04:30 — gap between blocks
    ("2026-11-09T09:59:00+00:00", False),  # Monday 09:59 — peak block 2
    ("2026-11-09T10:00:00+00:00", True),   # Monday 10:00 — peak over
])
def test_is_off_peak(iso: str, expected: bool) -> None:
    assert main._is_off_peak(datetime.fromisoformat(iso)) is expected


def test_off_peak_uses_utc_not_local_time() -> None:
    # Sun 20:00 PST is Mon 04:00 UTC (off-peak gap); Sun 18:00 PST is Mon 02:00 UTC (peak).
    pst = timezone(main.timedelta(hours=-8))
    assert main._is_off_peak(datetime(2026, 11, 8, 20, 0, tzinfo=pst)) is True
    assert main._is_off_peak(datetime(2026, 11, 8, 18, 0, tzinfo=pst)) is False


def test_estimate_cost_usd() -> None:
    peak = main._estimate_cost_usd("deepseek-v4-pro", 1_000_000, 1_000_000, 1_000_000, off_peak=False)
    assert peak == pytest.approx(1.32 + 0.044 + 3.96)
    off_peak = main._estimate_cost_usd("deepseek-v4-pro", 1_000_000, 1_000_000, 1_000_000, off_peak=True)
    assert off_peak == pytest.approx(peak / 2)


# ---------------------------------------------------------------------------
# Retries
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("exc, expected", [
    (_status_error(anthropic.RateLimitError, 429), True),
    (_status_error(anthropic.InternalServerError, 500), True),
    (_status_error(anthropic.APIStatusError, 503), True),
    (_status_error(anthropic.APIStatusError, 200), True),   # error event mid-stream
    (_status_error(anthropic.BadRequestError, 400), False),
    (_status_error(anthropic.AuthenticationError, 401), False),
    (_status_error(anthropic.APIStatusError, 402), False),   # insufficient balance
    (anthropic.APIConnectionError(request=_REQUEST), True),
    (main.IncompleteStreamError(), True),
])
def test_is_retryable(exc: Exception, expected: bool) -> None:
    assert main._is_retryable(exc) is expected


@patch("main.time.sleep")
def test_retry_then_succeed_counts_retries(sleep: MagicMock) -> None:
    client = _client(_status_error(anthropic.APIStatusError, 503), _message("[]"))
    message, retries = main._stream_with_retry(client, model="deepseek-v4-pro")
    assert retries == 1
    assert message.stop_reason == "end_turn"
    sleep.assert_called_once_with(30)


@patch("main.time.sleep")
def test_permanent_error_is_not_retried(sleep: MagicMock) -> None:
    client = _client(_status_error(anthropic.APIStatusError, 402))
    with pytest.raises(anthropic.APIStatusError):
        main._stream_with_retry(client, model="deepseek-v4-pro")
    sleep.assert_not_called()


@patch("main.time.sleep")
def test_incomplete_stream_is_retried_then_raised(sleep: MagicMock) -> None:
    truncated = _message("[", stop_reason=None)
    client = _client(truncated, truncated, truncated)
    with pytest.raises(main.IncompleteStreamError):
        main._stream_with_retry(client, model="deepseek-v4-pro")
    assert sleep.call_count == 2


# ---------------------------------------------------------------------------
# filter_events_with_llm
# ---------------------------------------------------------------------------

@patch("main.anthropic.Anthropic")
def test_calls_use_deepseek_and_correct_thinking(anthropic_cls: MagicMock) -> None:
    matched, unmatched = _event("AI Meetup"), _event("Pottery Night")
    client = _client(
        _message(json.dumps([{"title": "AI Meetup", "summary": "s1"}]), output=40),
        _message(json.dumps([{"title": "Pottery Night", "summary": "s2"}]), cached=64, output=900),
    )
    anthropic_cls.return_value = client

    filtered, model_id, usage = main.filter_events_with_llm(
        [matched, unmatched], ["AI"], "tech", "key", "deepseek-v4-pro"
    )

    anthropic_cls.assert_called_once_with(
        api_key="key", base_url=main.DEEPSEEK_BASE_URL, max_retries=0
    )
    summary_kwargs, filter_kwargs = (c.kwargs for c in client.messages.stream.call_args_list)
    assert summary_kwargs["model"] == filter_kwargs["model"] == "deepseek-v4-pro"
    assert summary_kwargs["thinking"] == {"type": "disabled"}
    assert filter_kwargs["thinking"] == {"type": "enabled"}
    assert [fe.event.title for fe in filtered] == ["AI Meetup", "Pottery Night"]
    assert model_id == "deepseek-v4-pro"
    assert (usage.input_tokens, usage.cached_input_tokens, usage.output_tokens) == (200, 64, 940)
    assert usage.cost_usd > 0
    assert usage.warnings == []


@patch("main.anthropic.Anthropic")
def test_pre_approved_event_skips_keyword_filter(anthropic_cls: MagicMock) -> None:
    event = _event("Prompting Workshop")
    event.pre_approved = True
    client = _client(_message(json.dumps([{"title": "Prompting Workshop", "summary": "s"}])))
    anthropic_cls.return_value = client

    filtered, _, usage = main.filter_events_with_llm(
        [event], ["Startup"], "tech", "key", "deepseek-v4-pro"
    )

    assert client.messages.stream.call_count == 1
    assert client.messages.stream.call_args.kwargs["thinking"] == {"type": "disabled"}
    assert [fe.summary for fe in filtered] == ["s"]
    assert (usage.keyword_matched, usage.llm_judged) == (1, 0)


@patch("main.anthropic.Anthropic")
def test_silent_drops_become_warnings(anthropic_cls: MagicMock) -> None:
    events = [_event("AI Meetup"), _event("AI Workshop"), _event("Pottery Night")]
    anthropic_cls.return_value = _client(
        _message(json.dumps([{"title": "AI Meetup “Live”", "summary": "s"}])),
        _message("I could not find any relevant events."),
    )

    filtered, _, usage = main.filter_events_with_llm(
        events, ["AI"], "tech", "key", "deepseek-v4-pro"
    )

    assert filtered == []
    joined = "\n".join(usage.warnings)
    assert "matched no fetched event" in joined
    assert "covered 0 of 2" in joined
    assert "Filter call returned invalid JSON" in joined


@patch("main.anthropic.Anthropic")
def test_summary_invalid_json_is_retried_once(anthropic_cls: MagicMock) -> None:
    client = _client(
        _message('[{"title": "AI Meetup", "summary": "an "unescaped" quote"}]', output=30),
        _message(json.dumps([{"title": "AI Meetup", "summary": "s"}]), output=20),
    )
    anthropic_cls.return_value = client

    filtered, _, usage = main.filter_events_with_llm(
        [_event("AI Meetup")], ["AI"], "tech", "key", "deepseek-v4-pro"
    )

    assert client.messages.stream.call_count == 2
    assert [fe.summary for fe in filtered] == ["s"]
    assert usage.output_tokens == 50
    assert usage.warnings == []


@patch("main.anthropic.Anthropic")
def test_summary_invalid_json_twice_is_flagged(anthropic_cls: MagicMock) -> None:
    client = _client(_message("not json"), _message("still not json"))
    anthropic_cls.return_value = client

    filtered, _, usage = main.filter_events_with_llm(
        [_event("AI Meetup")], ["AI"], "tech", "key", "deepseek-v4-pro"
    )

    assert client.messages.stream.call_count == 2
    assert filtered == []
    assert usage.warnings == [
        "Summary call returned invalid JSON 2 times — 1 keyword-matched event(s) "
        "are missing from Part A."
    ]


_ECHO = json.dumps([{"title": "AI Meetup", "venue": "Vancouver", "description": "d", "tags": []}])


@patch("main.anthropic.Anthropic")
def test_summary_echo_is_retried(anthropic_cls: MagicMock) -> None:
    client = _client(
        _message(_ECHO, output=30),
        _message(json.dumps([{"title": "AI Meetup", "summary": "s"}]), output=20),
    )
    anthropic_cls.return_value = client

    filtered, _, usage = main.filter_events_with_llm(
        [_event("AI Meetup")], ["AI"], "tech", "key", "deepseek-v4-pro"
    )

    assert client.messages.stream.call_count == 2
    assert [fe.summary for fe in filtered] == ["s"]
    assert usage.warnings == []


@patch("main.anthropic.Anthropic")
def test_summary_echo_twice_keeps_events_and_flags_them(anthropic_cls: MagicMock) -> None:
    client = _client(_message(_ECHO), _message(_ECHO))
    anthropic_cls.return_value = client

    filtered, _, usage = main.filter_events_with_llm(
        [_event("AI Meetup")], ["AI"], "tech", "key", "deepseek-v4-pro"
    )

    assert client.messages.stream.call_count == 2
    assert [fe.event.title for fe in filtered] == ["AI Meetup"]
    assert usage.warnings == ["1 approved event(s) came back with no summary: AI Meetup"]


def test_summary_prompt_ends_with_instructions() -> None:
    prompt = main.build_summary_prompt([_event("AI Meetup", "x" * 500)])
    assert prompt.startswith("Events:")
    assert prompt.rstrip().endswith("no extra text.")


# ---------------------------------------------------------------------------
# Parsing and title matching
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw", [
    '[{"title": "A", "summary": "s"}]',
    '```json\n[{"title": "A", "summary": "s"}]\n```',
    'Sure! Here are the [1] events:\n[{"title": "A", "summary": "s"}]\nEnjoy.',
    '{"events": [{"title": "A", "summary": "s"}]}',
])
def test_parse_llm_json_tolerates_wrapping(raw: str) -> None:
    assert main._parse_llm_json(raw) == [{"title": "A", "summary": "s"}]


@pytest.mark.parametrize("raw", ["", "No events matched.", "[1, 2, 3]", '[{"title": "A"'])
def test_parse_llm_json_rejects_non_arrays(raw: str) -> None:
    assert main._parse_llm_json(raw) is None


def test_titles_match_despite_cosmetic_changes() -> None:
    source = [_event('"Jazz Night" — Live 🎷'), _event("Casablanca  (1942)")]
    parsed = [
        {"title": "“Jazz Night” - Live", "summary": "a"},
        {"title": "casablanca (1942)", "summary": "b"},
        {"title": "Totally Different Event", "summary": "c"},
    ]
    matched, unknown = main._build_filtered_events(parsed, source)
    assert [fe.event for fe in matched] == source
    assert unknown == ["Totally Different Event"]


def test_title_typo_still_matches() -> None:
    real = _event("Chan Centre EXP: First Ladies of the Blues Starring Krystle Dos...")
    parsed = [{"title": "Chan Centre EXP: First Ladies of the Blues Starring Krystole Dos...",
               "summary": "s"}]
    matched, unknown = main._build_filtered_events(parsed, [real, _event("Kanal")])
    assert [fe.event for fe in matched] == [real]
    assert unknown == []


def test_dissimilar_title_not_fuzzy_matched() -> None:
    matched, unknown = main._build_filtered_events(
        [{"title": "Kanal II", "summary": "s"}], [_event("Kanal")]
    )
    assert matched == []
    assert unknown == ["Kanal II"]


def test_both_prompts_share_summary_length() -> None:
    events = [_event("Kanal")]
    assert main._SUMMARY_LENGTH in main.build_filter_prompt(events, ["Film"])
    assert main._SUMMARY_LENGTH in main.build_summary_prompt(events)


def test_repeat_screenings_each_get_their_own_event() -> None:
    first, second = _event("Vertigo"), _event("Vertigo")
    second.date = "2026-11-15"
    parsed = [{"title": "Vertigo", "summary": "a"}, {"title": "Vertigo", "summary": "b"}]
    matched, _ = main._build_filtered_events(parsed, [first, second])
    assert [fe.event.date for fe in matched] == ["2026-11-14", "2026-11-15"]


# ---------------------------------------------------------------------------
# Config errors
# ---------------------------------------------------------------------------

def test_config_errors_flags_claude_model_and_missing_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main, "DEEPSEEK_API_KEY", None)
    errors = main.config_errors("tech", "claude-opus-4-6", ["AI"])
    assert any("DEEPSEEK_API_KEY" in e for e in errors)
    assert any("Unsupported model 'claude-opus-4-6'" in e for e in errors)


def test_config_errors_empty_for_valid_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main, "DEEPSEEK_API_KEY", "key")
    assert main.config_errors("tech", "deepseek-v4-pro", ["AI"]) == []


@patch("main.requests.post")
def test_config_error_is_posted_to_slack_before_exit(
    post: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(main, "DEEPSEEK_API_KEY", "key")
    monkeypatch.setitem(main._CONFIG, "tech_model", "claude-opus-4-6")
    monkeypatch.setenv("TECH_WEBHOOK_URL", "https://hooks.slack.com/x")
    monkeypatch.setattr(main.sys, "argv", ["main.py", "--profile", "tech"])
    with pytest.raises(SystemExit):
        main.main()
    payload = post.call_args.kwargs["json"]
    assert "CONFIG ERROR" in payload["text"]
    assert "claude-opus-4-6" in json.dumps(payload)


# ---------------------------------------------------------------------------
# Balance
# ---------------------------------------------------------------------------

@patch("main.requests.get")
def test_fetch_balance_prefers_usd(get: MagicMock) -> None:
    get.return_value.json.return_value = {
        "is_available": True,
        "balance_infos": [
            {"currency": "CNY", "total_balance": "10.00"},
            {"currency": "USD", "total_balance": "4.87"},
        ],
    }
    balance = main.fetch_deepseek_balance("key")
    assert (balance.currency, balance.total, balance.is_low) == ("USD", 4.87, False)


@patch("main.requests.get", side_effect=main.requests.ConnectionError("down"))
def test_fetch_balance_failure_returns_none(_get: MagicMock) -> None:
    assert main.fetch_deepseek_balance("key") is None


# ---------------------------------------------------------------------------
# Slack rendering
# ---------------------------------------------------------------------------

def _render(usage: main.LLMUsage, balance: main.DeepSeekBalance | None = None) -> dict:
    return main.format_slack_message(
        [], [], date(2026, 11, 9), date(2026, 11, 22), "deepseek-v4-pro", ["AI"], "fun",
        usage, balance,
    )


def test_footer_and_warnings_rendered() -> None:
    usage = main.LLMUsage(
        model_id="deepseek-v4-pro", input_tokens=1000, cached_input_tokens=200,
        output_tokens=800, cost_usd=0.0012, off_peak=True, warnings=["Something dropped"],
    )
    payload = _render(usage, main.DeepSeekBalance("USD", 4.87, True))
    text = json.dumps(payload, ensure_ascii=False)
    assert "Tokens: 2,000 (in 1,000 · cached 200 · out 800)" in text
    assert "Est. cost: $0.0012 (off-peak)" in text
    assert "Balance: $4.87" in text
    assert ":warning: *LLM* — Something dropped" in text
    assert "1 issue(s) this run — see Part B" in text
    assert payload["text"].endswith("⚠️ 1 issue(s), see Part B")


def test_clean_run_has_no_banner() -> None:
    payload = _render(main.LLMUsage(model_id="deepseek-v4-pro", cost_usd=0.003))
    text = json.dumps(payload, ensure_ascii=False)
    assert "issue(s)" not in text
    assert payload["text"] == "Vancouver Events (Fun): Nov 09 – Nov 22, 2026"


def test_prompts_include_venue() -> None:
    clowns = _event("My Family and Other Clowns")
    clowns.location = "The Cinematheque, 1131 Howe Street, Vancouver"
    for prompt in (main.build_filter_prompt([clowns], ["Theatre"]), main.build_summary_prompt([clowns])):
        assert '"venue": "The Cinematheque, 1131 Howe Street, Vancouver"' in prompt
        assert main._GROUNDING_RULES in prompt


def test_venue_does_not_trigger_keyword_match() -> None:
    event = _event("UBC Bands")
    event.location = "Chan Shun Concert Hall"
    assert not main._matches_any_keyword(event, ["Concert"])


@pytest.mark.parametrize("text", [
    "Join us for a painting class", "Coaches are available", "Fairways & Friends",
    "Win a beautiful hair clip", "Send an email to RSVP",
])
def test_keyword_inside_another_word_does_not_match(text: str) -> None:
    assert not main._matches_any_keyword(_event("Social", text), ["AI"])


@pytest.mark.parametrize("text, keyword", [
    ("An AI-assisted CTF", "AI"),
    ("AI, ML and more", "AI"),
    ("Pitch night for Startups", "Startup"),
    ("intro to machine learning", "Machine Learning"),
])
def test_keyword_matches_whole_word(text: str, keyword: str) -> None:
    assert main._matches_any_keyword(_event("Event", text), [keyword])


def test_part_b_shows_keyword_and_llm_split() -> None:
    usage = main.LLMUsage(model_id="deepseek-v4-pro", keyword_matched=1, llm_judged=12)
    text = json.dumps(_render(usage), ensure_ascii=False)
    assert "*Auto-included:* 1 · *LLM-judged:* 12  →  *LLM approved:* 0" in text


def _render_fetcher(events: list[Event]) -> str:
    result = main.FetcherResult(name="Cinematheque", success=True, events=events, raw_count=len(events))
    payload = main.format_slack_message(
        [], [result], date(2026, 11, 9), date(2026, 11, 22), "deepseek-v4-pro", ["AI"], "fun",
        main.LLMUsage(model_id="deepseek-v4-pro"), None,
    )
    return json.dumps(payload, ensure_ascii=False)


def test_quiet_calendar_is_not_an_issue() -> None:
    result = main.FetcherResult(name="Luma: Quiet", success=True, events=[], raw_count=3, empty_ok=True)
    payload = main.format_slack_message(
        [], [result], date(2026, 11, 9), date(2026, 11, 22), "deepseek-v4-pro", ["AI"], "tech",
        main.LLMUsage(model_id="deepseek-v4-pro"), None,
    )
    text = json.dumps(payload, ensure_ascii=False)
    assert ":white_circle: *Luma: Quiet* — 0 in-area event(s) this window" in text
    assert "URGENT" not in text
    assert "issue(s)" not in text


def test_blank_summary_renders_without_empty_italics() -> None:
    blocks = _part_a_texts([main.FilteredEvent(event=_event("FV + AI"), summary="")])
    assert blocks[0].endswith("Vancouver")
    assert "__" not in blocks[0]


def test_tech_footer_has_calendar_discovery_reminder() -> None:
    payload = main.format_slack_message(
        [], [], date(2026, 11, 9), date(2026, 11, 22), "deepseek-v4-pro", ["AI"], "tech",
        main.LLMUsage(model_id="deepseek-v4-pro"), None,
    )
    text = json.dumps(payload, ensure_ascii=False)
    count = len(main._CONFIG["tech_luma_calendars"])
    assert f"Events come from the {count} Luma calendars" in text
    assert "Cerebral Valley's remote AI events" in text
    assert "To discover more" in text


def test_mostly_missing_descriptions_flagged() -> None:
    text = _render_fetcher([_event("A"), _event("B"), _event("C", "A Dutch drama.")])
    assert ":warning: *Cinematheque* — 2 of 3 event(s) have no description" in text


def test_few_missing_descriptions_not_flagged() -> None:
    text = _render_fetcher([_event("A"), _event("B", "x"), _event("C", "y")])
    assert "have no description" not in text


def _showing(title: str, day: str, time: str, location: str = "VIFF Cinema") -> main.FilteredEvent:
    event = _event(title)
    event.date, event.time, event.location = day, time, location
    return main.FilteredEvent(event=event, summary=f"About {title} on {day}.")


def _part_a_texts(filtered: list[main.FilteredEvent]) -> list[str]:
    payload = main.format_slack_message(
        filtered, [], date(2026, 11, 9), date(2026, 11, 22), "deepseek-v4-pro", ["Film"], "fun",
    )
    return [
        b["text"]["text"] for b in payload["blocks"]
        if b["type"] == "section" and b["text"]["text"].startswith("*<")
    ]


def test_part_a_sorted_by_date_then_time_with_tbd_last() -> None:
    texts = _part_a_texts([
        _showing("Late", "2026-11-21", "8:10 PM"),
        _showing("Unknown", "2026-11-15", "TBD"),
        _showing("Noon", "2026-11-15", "12:00 PM"),
        _showing("Morning", "2026-11-15", "11:00 AM"),
    ])
    assert [t.split("|")[1].split(">")[0] for t in texts] == ["Morning", "Noon", "Unknown", "Late"]


def test_repeat_showings_merged_into_one_entry() -> None:
    texts = _part_a_texts([
        _showing("Yellow Letters", "2026-11-22", "1:20 PM", "Lochmaddy Studio Theatre"),
        _showing("Kanal", "2026-11-22", "3:00 PM"),
        _showing("Yellow Letters", "2026-11-21", "5:00 PM"),
    ])
    assert len(texts) == 2
    merged = texts[0]
    assert merged.startswith("*<https://example.com|Yellow Letters>*")
    assert merged.index("2026-11-21 (Saturday)") < merged.index("2026-11-22 (Sunday)")
    assert "Lochmaddy Studio Theatre" in merged
    assert merged.count("_About Yellow Letters") == 1
    assert "About Yellow Letters on 2026-11-21" in merged


@pytest.mark.parametrize(("day", "time", "kept"), [
    ("2026-11-13", "6:00 PM", True),
    ("2026-11-13", "5:59 PM", False),
    ("2026-11-13", "12:30 AM", False),
    ("2026-11-13", "TBD", False),
    ("2026-11-14", "TBD", True),
    ("2026-11-12", "8:00 PM", False),
])
def test_weekend_filter(day: str, time: str, kept: bool) -> None:
    event = _event("X")
    event.date, event.time = day, time
    assert main._passes_weekend_filter(event) is kept


def test_cost_alarm_and_low_balance_flagged() -> None:
    usage = main.LLMUsage(model_id="deepseek-v4-pro", cost_usd=main._COST_ALARM_USD + 0.01)
    text = json.dumps(_render(usage, main.DeepSeekBalance("USD", 0.42, True)), ensure_ascii=False)
    assert ":rotating_light: *Cost*" in text
    assert ":warning: *Balance* — DeepSeek balance $0.42 is below" in text
    assert "2 issue(s) this run" in text
