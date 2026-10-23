from unittest.mock import AsyncMock

import pytest

from core import prompts
from core.clarify import Clarification, ClarifyService
from core.llm import FakeProvider, ProviderError
from core.rounds import RoundError


@pytest.fixture
def clarify(db):
    db.ensure_channel(200, 100)
    db.enable_channel(200)
    return ClarifyService(db, FakeProvider())


def metric(db, name):
    row = db.connection.execute("SELECT count FROM metrics WHERE channel_id='200' AND name=?", (name,)).fetchone()
    return row[0] if row else 0


async def test_clarify_only_supplies_selected_text_and_does_not_persist_it(clarify, caplog):
    selected = "SELECTED_PRIVATE_MESSAGE_78128"
    clarify.provider.complete = AsyncMock(wraps=clarify.provider.complete)
    result = await clarify.explain(200, selected)
    assert isinstance(result, Clarification)
    clarify.provider.complete.assert_awaited_once_with(system=prompts.CLARIFY_MESSAGE, user=selected)
    assert not metric(clarify.db, "clarify_uses")  # Count only after delivery.
    dump = "\n".join(clarify.db.connection.iterdump())
    assert selected not in dump + caplog.text
    assert result.meaning not in dump + caplog.text
    assert selected not in str(vars(clarify))


@pytest.mark.parametrize("text,code", [
    ("", "input"), ("  ", "input"), ("x" * 4001, "input"),
    ("Assignment prompt: write about strategy", "boundary"),
    ("Please write our report", "boundary"), ("Grade: 95", "boundary"),
])
async def test_invalid_or_forbidden_clarify_inputs_never_reach_provider(clarify, text, code):
    clarify.provider.complete = AsyncMock()
    with pytest.raises(RoundError, match=code):
        await clarify.explain(200, text)
    clarify.provider.complete.assert_not_awaited()
    assert not metric(clarify.db, "clarify_uses")


@pytest.mark.parametrize("state", ["unknown", "paused"])
async def test_clarify_and_feedback_require_active_setup(clarify, state):
    token = clarify.new_feedback(200)
    if state == "unknown":
        channel = 201
    else:
        clarify.db.set_paused(200, True)
        channel = 200
    clarify.provider.complete = AsyncMock()
    with pytest.raises(RoundError):
        await clarify.explain(channel, "A selected message")
    with pytest.raises(RoundError):
        clarify.record_feedback(channel, token, helpful=True)
    with pytest.raises(RoundError):
        clarify.record_use(channel)
    clarify.provider.complete.assert_not_awaited()


@pytest.mark.parametrize("invalid", ["INVALID_PRIVATE_JSON", "{}", '{"meaning":"","terms":[],"other_readings":[],"follow_up_question":"What?"}'])
async def test_invalid_clarify_json_or_schema_retries_once(clarify, invalid):
    valid = await FakeProvider().complete(system=prompts.CLARIFY_MESSAGE, user="fixture")
    clarify.provider.complete = AsyncMock(side_effect=[invalid, valid])
    assert await clarify.explain(200, "A selected message")
    assert clarify.provider.complete.await_count == 2


async def test_second_invalid_result_and_provider_error_fail_safely(clarify, caplog):
    clarify.provider.complete = AsyncMock(return_value="PRIVATE_INVALID_OUTPUT")
    with pytest.raises(RoundError, match="provider"):
        await clarify.explain(200, "PRIVATE_MESSAGE")
    assert clarify.provider.complete.await_count == 2
    clarify.provider.complete = AsyncMock(side_effect=ProviderError("Provider request failed."))
    with pytest.raises(RoundError, match="provider"):
        await clarify.explain(200, "PRIVATE_MESSAGE")
    assert clarify.provider.complete.await_count == 1
    assert "PRIVATE_MESSAGE" not in caplog.text and "PRIVATE_INVALID_OUTPUT" not in caplog.text


@pytest.mark.parametrize("helpful,counter", [(True, "clarify_helpful"), (False, "clarify_unhelpful")])
def test_feedback_counts_once_and_metrics_contain_no_user_receipts(clarify, helpful, counter):
    token = clarify.new_feedback(200)
    clarify.record_use(200)
    clarify.record_feedback(200, token, helpful=helpful)
    assert metric(clarify.db, "clarify_uses") == 1 and metric(clarify.db, counter) == 1
    with pytest.raises(RoundError, match="clarify_feedback"):
        clarify.record_feedback(200, token, helpful=not helpful)
    assert sum(row[0] for row in clarify.db.connection.execute("SELECT count FROM metrics")) == 2
    assert token not in "\n".join(clarify.db.connection.iterdump())
    assert not clarify._feedback


def test_feedback_expiry_wrong_channel_restart_and_memory_bound(clarify, monkeypatch):
    now = [0.0]
    clarify.clock = lambda: now[0]
    token = clarify.new_feedback(200)
    clarify.db.ensure_channel(201, 100)
    clarify.db.enable_channel(201)
    with pytest.raises(RoundError, match="clarify_feedback"):
        clarify.record_feedback(201, token, helpful=True)
    now[0] = 900
    with pytest.raises(RoundError, match="clarify_feedback"):
        clarify.record_feedback(200, token, helpful=True)
    monkeypatch.setattr("core.clarify.MAX_PENDING_FEEDBACK", 2)
    tokens = [clarify.new_feedback(200) for _ in range(3)]
    assert len(clarify._feedback) == 2 and tokens[0] not in clarify._feedback and token not in clarify._feedback
    restarted = ClarifyService(clarify.db, FakeProvider())
    with pytest.raises(RoundError, match="clarify_feedback"):
        restarted.record_feedback(200, tokens[-1], helpful=True)
    assert not metric(clarify.db, "clarify_helpful")


async def test_pause_during_provider_call_discards_clarification(clarify):
    async def pause(**kwargs):
        clarify.db.set_paused(200, True)
        return await FakeProvider().complete(**kwargs)

    clarify.provider.complete = pause
    with pytest.raises(RoundError, match="paused"):
        await clarify.explain(200, "Selected message")
    assert not metric(clarify.db, "clarify_uses") and not clarify._feedback
