"""Event de-duplication: a Slack retry must never run a second evaluation.

Slack retries any event it did not see a 200 for within three seconds, marking
the retry with X-Slack-Retry-Num and re-sending the SAME event_id. Every entry
point checks here first.
"""

from __future__ import annotations

from bot.state import EventDedupe


def test_first_sighting_is_new_and_the_retry_is_not():
    seen = EventDedupe()
    assert seen.seen("Ev123") is False
    assert seen.seen("Ev123") is True
    assert seen.seen("Ev123") is True


def test_distinct_events_are_independent():
    seen = EventDedupe()
    assert seen.seen("EvA") is False
    assert seen.seen("EvB") is False
    assert seen.seen("EvA") is True


def test_missing_event_id_is_never_treated_as_a_retry():
    seen = EventDedupe()
    assert seen.seen(None) is False
    assert seen.seen("") is False
    assert len(seen) == 0


def test_the_cache_is_bounded_and_evicts_the_oldest():
    seen = EventDedupe(capacity=3)
    for event_id in ("E1", "E2", "E3"):
        assert seen.seen(event_id) is False
    assert len(seen) == 3
    assert seen.seen("E4") is False  # evicts E1
    assert len(seen) == 3
    assert seen.seen("E1") is False  # forgotten, so treated as new
    assert seen.seen("E4") is True


def test_a_repeat_refreshes_recency():
    seen = EventDedupe(capacity=2)
    seen.seen("E1")
    seen.seen("E2")
    assert seen.seen("E1") is True  # E1 becomes the most recent
    seen.seen("E3")  # evicts E2, not E1
    assert seen.seen("E1") is True
    assert seen.seen("E2") is False
