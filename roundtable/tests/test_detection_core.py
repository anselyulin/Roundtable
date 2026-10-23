from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
import asyncio
import json
import sqlite3

import pytest

from core import prompts
from core.db import Database, SCHEMA
from core.detection import DetectionService
from core.llm import FakeProvider, ProviderError
from core.rounds import RoundError
from test_rounds import T0


TEXTS = ("Which market should we choose?", "Campus is nearby.", "Local businesses have contacts.", "Let's compare both choices.")


@pytest.fixture
def detector(db, settings):
    db.ensure_channel(200, 100)
    db.enable_channel(200)
    now = [T0]
    service = DetectionService(db, FakeProvider(), settings, clock=lambda: now[0])
    return SimpleNamespace(service=service, db=db, now=now)


def metric(ctx, name):
    row = ctx.db.connection.execute("SELECT count FROM metrics WHERE channel_id='200' AND name=?", (name,)).fetchone()
    return row[0] if row else 0


def feed(ctx, *, authors=(102, 103, 102, 103), texts=TEXTS):
    check = None
    for author, text in zip(authors, texts):
        check = ctx.service.observe(200, author, text, ctx.now[0]) or check
    return check


async def shown(ctx):
    check = feed(ctx)
    candidate = await ctx.service.evaluate(check)
    prompt = ctx.service.reserve_prompt(check, candidate)
    ctx.service.mark_shown(prompt.id, 300)
    return ctx.db.get_decision_prompt(prompt.id)


async def test_detection_needs_four_messages_and_two_authors_and_only_sends_anonymous_recent_text(detector):
    ctx = detector
    ctx.service.provider.complete = AsyncMock(wraps=ctx.service.provider.complete)
    for text in TEXTS[:3]:
        assert ctx.service.observe(200, 102, text, T0) is None
    assert ctx.service.observe(200, 102, TEXTS[3], T0) is None
    check = ctx.service.observe(200, 103, "Which market: Campus or Local businesses?", T0)
    result = await ctx.service.evaluate(check)
    assert result.options == ("Campus", "Local businesses")
    ctx.service.provider.complete.assert_awaited_once()
    args = ctx.service.provider.complete.call_args.kwargs
    assert args["system"] == prompts.DETECT_DECISION
    assert args["user"].startswith("1. A: ") and "5. B: " in args["user"]
    assert "102" not in args["user"] and "103" not in args["user"]
    assert not ctx.db.connection.execute("SELECT 1 FROM decision_prompts").fetchone()


async def test_first_check_needs_four_messages_then_checks_new_messages_after_rate_limit(detector):
    ctx = detector
    ctx.service.provider.complete = AsyncMock(return_value='{"is_decision":false,"confidence":0,"question":"","options":[]}')
    first = feed(ctx)
    assert await ctx.service.evaluate(first) is None
    ctx.now[0] = T0 + timedelta(seconds=10)
    assert feed(ctx) is None
    ctx.now[0] = T0 + timedelta(seconds=45)
    second = ctx.service.observe(200, 102, TEXTS[0], ctx.now[0])
    assert second is not None
    assert await ctx.service.evaluate(second) is None
    ctx.now[0] = T0 + timedelta(seconds=90)
    third = ctx.service.observe(200, 103, TEXTS[3], ctx.now[0])
    assert third is not None
    await ctx.service.evaluate(third)
    assert ctx.service.provider.complete.await_count == 3


@pytest.mark.parametrize("state", ["unknown", "disabled", "paused", "detection_disabled", "command_only"])
def test_inactive_detection_keeps_no_window(detector, state):
    ctx = detector
    if state == "unknown":
        ctx.db.connection.execute("DELETE FROM channels")
    elif state == "disabled":
        ctx.db.connection.execute("UPDATE channels SET enabled=0")
    elif state == "paused":
        ctx.db.set_paused(200, True)
    elif state == "detection_disabled":
        ctx.db.connection.execute("UPDATE channels SET detection_enabled=0")
    else:
        ctx.service.mode = "command_only"
    assert feed(ctx) is None and not ctx.service.windows


def test_window_never_exceeds_fifteen_messages_or_ten_minutes(detector):
    ctx = detector
    for index in range(20):
        assert ctx.service.observe(200, 102, f"Message {index}", T0) is None
    window = ctx.service.windows["200"]
    assert len(window.messages) == 15 and window.messages[0].text == "Message 5"
    assert ctx.service.next_expiry() == T0 + timedelta(minutes=10)
    ctx.now[0] = T0 + timedelta(minutes=10)
    ctx.service.expire()
    assert not ctx.service.windows
    assert ctx.service.observe(200, 102, "Old text", T0) is None


@pytest.mark.parametrize("text", ["", "x" * 4001, "Assignment prompt: write our report", "Please write our deliverable", "Grade: 95"])
def test_forbidden_or_invalid_text_never_stays_in_window(detector, text):
    ctx = detector
    ctx.service.observe(200, 102, TEXTS[0], T0)
    assert ctx.service.observe(200, 103, text, T0) is None
    assert not ctx.service.windows


@pytest.mark.parametrize("overrides", [
    {"is_decision": False}, {"confidence": .74}, {"options": []},
    {"question": ""}, {"options": ["Invented", "Campus"]},
    {"question": "Write our report"},
])
async def test_low_confidence_non_decisions_and_unsupported_options_are_quiet(detector, overrides):
    ctx = detector
    payload = {"is_decision": True, "confidence": .95, "question": "Which market?", "options": ["Campus", "Local businesses"], **overrides}
    ctx.service.provider.complete = AsyncMock(return_value=json.dumps(payload))
    assert await ctx.service.evaluate(feed(ctx)) is None
    assert not metric(ctx, "prompts_shown")
    assert not ctx.db.connection.execute("SELECT 1 FROM decision_prompts").fetchone()


@pytest.mark.parametrize("invalid", ["INVALID_PRIVATE_OUTPUT", '{"is_decision":"yes"}', '{"is_decision":true,"confidence":2,"question":"Which?","options":[]}'])
async def test_invalid_detection_json_retries_once_without_logs(detector, invalid, caplog):
    ctx = detector
    valid = await FakeProvider().complete(system=prompts.DETECT_DECISION, user="which market")
    ctx.service.provider.complete = AsyncMock(side_effect=[invalid, valid])
    assert await ctx.service.evaluate(feed(ctx)) is not None
    assert ctx.service.provider.complete.await_count == 2
    assert "INVALID_PRIVATE_OUTPUT" not in caplog.text


async def test_provider_failure_is_quiet_and_not_retried(detector):
    ctx = detector
    ctx.service.provider.complete = AsyncMock(side_effect=ProviderError("Provider request failed."))
    assert await ctx.service.evaluate(feed(ctx)) is None
    assert ctx.service.provider.complete.await_count == 1


async def test_shown_accepted_counters_and_new_round_are_exactly_once(detector):
    ctx = detector
    prompt = await shown(ctx)
    assert metric(ctx, "prompts_shown") == 1 and not ctx.service.windows
    assert ctx.db.get_channel(200).last_prompt_at == T0.isoformat()
    assert not ctx.service.can_offer(200, T0 + timedelta(days=1))  # One pending prompt.
    with pytest.raises(RoundError):
        ctx.service.mark_shown(prompt.id, 300)
    record = ctx.service.accept(prompt.id, 200, 103, ("102", "103"))
    assert record.question == prompt.question and record.options == prompt.options
    assert record.created_by == "103" and record.status == "collecting"
    assert not ctx.db.round_responses(record.id)
    assert metric(ctx, "prompts_accepted") == 1
    with pytest.raises(RoundError):
        ctx.service.dismiss(prompt.id, 200)
    assert metric(ctx, "prompts_shown") == 1 and not metric(ctx, "prompts_dismissed")
    assert not ctx.service.can_offer(200, T0 + timedelta(minutes=14, seconds=59))
    assert ctx.service.can_offer(200, T0 + timedelta(minutes=15))


async def test_dismiss_keeps_only_normal_cooldown_and_counters_survive_restart(detector, settings):
    ctx = detector
    prompt = await shown(ctx)
    ctx.service.dismiss(prompt.id, 200)
    assert metric(ctx, "prompts_dismissed") == 1
    assert not ctx.db.connection.execute("SELECT 1 FROM rounds").fetchone()
    restarted = DetectionService(ctx.db, FakeProvider(), settings)
    assert not restarted.can_offer(200, T0 + timedelta(minutes=14, seconds=59))
    assert restarted.can_offer(200, T0 + timedelta(minutes=15))
    with pytest.raises(RoundError):
        restarted.accept(prompt.id, 200, 102, ("102",))
    assert metric(ctx, "prompts_dismissed") == 1 and not metric(ctx, "prompts_accepted")


async def test_late_dismissal_does_not_restart_normal_cooldown(detector):
    ctx = detector
    prompt = await shown(ctx)
    ctx.now[0] = T0 + timedelta(minutes=20)
    ctx.service.dismiss(prompt.id, 200)
    assert ctx.service.can_offer(200, ctx.now[0])
    with pytest.raises(RoundError):
        ctx.service.dismiss(prompt.id, 200)
    assert metric(ctx, "prompts_dismissed") == 1
    assert not ctx.db.connection.execute("SELECT 1 FROM rounds").fetchone()
    assert await ctx.service.evaluate(feed(ctx)) is not None


@pytest.mark.parametrize("minutes", [5, 15, 90])
async def test_legacy_dismissal_timer_does_not_override_configured_cooldown(detector, settings, minutes):
    ctx = detector
    prompt = await shown(ctx)
    ctx.service.dismiss(prompt.id, 200)
    ctx.db.connection.execute(
        "UPDATE channels SET detection_cooldown_until=? WHERE channel_id='200'",
        ((T0 + timedelta(minutes=60)).isoformat(),),
    )
    restarted = DetectionService(ctx.db, FakeProvider(), replace(settings, detection_cooldown_min=minutes))
    assert not restarted.can_offer(200, T0 + timedelta(minutes=minutes) - timedelta(seconds=1))
    assert restarted.can_offer(200, T0 + timedelta(minutes=minutes))


async def test_in_flight_check_cannot_publish_after_pause_resume_or_expiry(detector):
    ctx = detector
    check = feed(ctx)
    ctx.service.clear_channel(200)
    assert await ctx.service.evaluate(check) is None
    check = feed(ctx)
    ctx.now[0] += timedelta(minutes=10)
    assert await ctx.service.evaluate(check) is None


async def test_pause_during_invalid_result_prevents_the_retry_and_releases_snapshot(detector):
    ctx = detector
    check = feed(ctx)

    async def paused(**kwargs):
        ctx.db.set_paused(200, True)
        ctx.service.clear_channel(200)
        return "INVALID_PRIVATE_OUTPUT"

    ctx.service.provider.complete = AsyncMock(side_effect=paused)
    assert await ctx.service.evaluate(check) is None
    assert ctx.service.provider.complete.await_count == 1
    assert check.texts == () and check.payload == "" and not check.window.messages


async def test_in_flight_request_is_cancelled_at_the_source_window_expiry(detector):
    ctx = detector
    check = feed(ctx)
    ctx.now[0] = check.expires_at - timedelta(milliseconds=10)
    cancelled = []

    async def waiting(**kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(True)

    ctx.service.provider.complete = AsyncMock(side_effect=waiting)
    assert await asyncio.wait_for(ctx.service.evaluate(check), timeout=1) is None
    assert cancelled == [True] and ctx.service.provider.complete.await_count == 1
    assert check.texts == () and check.payload == ""
    assert not ctx.db.published_prompt_ids()


async def test_accept_rolls_back_prompt_round_and_counter_together(detector):
    ctx = detector
    prompt = await shown(ctx)
    ctx.db.connection.execute("""CREATE TRIGGER reject_counter BEFORE INSERT ON metrics
        WHEN NEW.name='prompts_accepted' BEGIN SELECT RAISE(ABORT,'Test failure'); END""")
    with pytest.raises(sqlite3.IntegrityError):
        ctx.service.accept(prompt.id, 200, 102, ("102",))
    assert ctx.db.get_decision_prompt(prompt.id).status == "pending"
    assert not ctx.db.connection.execute("SELECT 1 FROM rounds").fetchone()
    assert not metric(ctx, "prompts_accepted")


def test_detection_schema_upgrades_previous_database_in_memory(monkeypatch):
    connection = sqlite3.connect(":memory:")
    # Existing step-3 databases have neither cooldown field nor prompt table.
    legacy = SCHEMA.split("CREATE TABLE IF NOT EXISTS decision_prompts")[0].replace(
        "last_prompt_at TEXT,\n    detection_cooldown_until TEXT", "last_prompt_at TEXT",
    )
    connection.executescript(legacy)
    connection.execute("INSERT INTO channels(channel_id,guild_id,enabled) VALUES('200','100',1)")
    connection.commit()
    monkeypatch.setattr("core.db.sqlite3.connect", lambda path: connection)
    db = Database(":memory:")
    try:
        assert db.get_channel(200).active and db.get_channel(200).detection_cooldown_until is None
        Database(":memory:")
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()
        assert db.published_prompt_ids() == ()
    finally:
        db.close()
