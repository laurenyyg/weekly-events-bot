"""
Tests for splitting long Slack digests into several messages.

Run with:

    pytest tests/test_slack_split.py -v
"""

import json
from datetime import date
from unittest.mock import MagicMock, patch

import pytest
import requests

import main
from fetchers import Event

TITLE = "Vancouver Events (Tech): Nov 09 – Nov 22, 2026"


def _filtered(n: int) -> list[main.FilteredEvent]:
    return [
        main.FilteredEvent(
            event=Event(
                title=f"Event {i:03d}", date="2026-11-14", time="7:00 PM",
                location="Vancouver", description="d", url=f"https://example.com/{i}",
                source="test", tags=[],
            ),
            summary=f"About event {i}.",
        )
        for i in range(n)
    ]


def _payload(n_events: int, profile: str = "tech", with_issues: bool = False) -> dict:
    results = [main.FetcherResult(name="Broken", success=True, events=[], raw_count=0)] if with_issues else []
    return main.format_slack_message(
        _filtered(n_events), results, date(2026, 11, 9), date(2026, 11, 22),
        "deepseek-v4-pro", ["AI"], profile, main.LLMUsage(model_id="deepseek-v4-pro"), None,
    )


def _event_titles(messages: list[dict]) -> list[str]:
    return [
        b["text"]["text"].split("|")[1].split(">")[0]
        for m in messages for b in m["blocks"]
        if b["type"] == "section" and b["text"]["text"].startswith("*<")
    ]


def test_small_digest_is_one_unchanged_message() -> None:
    payload = _payload(5)
    assert main.split_slack_payload(payload) == [payload]


@pytest.mark.parametrize("profile", ["tech", "fun", "future-profile"])
def test_long_digest_splits_under_limit_for_any_profile(profile: str) -> None:
    payload = _payload(120, profile)
    messages = main.split_slack_payload(payload)
    assert len(messages) == 3
    assert all(len(m["blocks"]) <= main._SLACK_MAX_BLOCKS for m in messages)
    assert _event_titles(messages) == [f"Event {i:03d}" for i in range(120)]


def test_follow_up_messages_start_with_cont_header() -> None:
    first, *rest = main.split_slack_payload(_payload(60, with_issues=True))
    assert first["blocks"][0]["text"]["text"] == TITLE
    assert first["text"].startswith(TITLE) and "issue(s)" in first["text"]
    for message in rest:
        assert message["blocks"][0] == {
            "type": "header", "text": {"type": "plain_text", "text": f"(cont.) {TITLE}", "emoji": True},
        }
        assert message["text"] == f"(cont.) {TITLE}"


def test_banner_first_and_part_b_footer_kept_together_last() -> None:
    messages = main.split_slack_payload(_payload(60, with_issues=True))
    as_text = [json.dumps(m, ensure_ascii=False) for m in messages]
    assert "issue(s) this run — see Part B" in as_text[0]
    assert all("issue(s) this run" not in t for t in as_text[1:])
    last = messages[-1]["blocks"]
    assert any("Part B — System Diagnostics" in json.dumps(b, ensure_ascii=False) for b in last)
    assert any("URGENT" in json.dumps(b) for b in last)
    assert last[-1]["type"] == "context"  # footer
    assert all("Part B — System Diagnostics" not in t for t in as_text[:-1])


def test_tail_moves_to_new_message_when_it_does_not_fit() -> None:
    payload = _payload(46)  # 49 blocks before Part B, so the 4-block tail can't follow
    messages = main.split_slack_payload(payload)
    assert [len(m["blocks"]) for m in messages] == [49, 5]
    assert messages[1]["blocks"][1]["type"] == "divider"
    assert _event_titles(messages) == [f"Event {i:03d}" for i in range(46)]


def _ok() -> MagicMock:
    resp = MagicMock(text="ok")
    resp.raise_for_status.return_value = None
    return resp


def test_post_sends_each_part_in_order() -> None:
    with patch("main.requests.post", return_value=_ok()) as post, patch("main.time.sleep") as sleep:
        main.post_to_slack(_payload(120), "https://hooks.example")
    sent = [c.kwargs["json"] for c in post.call_args_list]
    assert sent == main.split_slack_payload(_payload(120))
    assert sleep.call_count == len(sent) - 1


def test_post_failure_reports_how_many_parts_went_out() -> None:
    bad = MagicMock()
    bad.raise_for_status.side_effect = requests.HTTPError("400 invalid_blocks")
    with patch("main.requests.post", side_effect=[_ok(), bad]), patch("main.time.sleep"):
        with pytest.raises(RuntimeError, match=r"message 2 of 3 \(1 already posted\)"):
            main.post_to_slack(_payload(120), "https://hooks.example")
