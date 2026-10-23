from datetime import datetime, timedelta, timezone
from dataclasses import replace
from unittest.mock import AsyncMock
import json

import pytest

from core import prompts
from core.db import Database, utc_iso
from core.llm import FakeProvider
from core.rounds import PreparedQuestion, RoundError, RoundService, duration_suggestions, parse_duration


T0 = datetime(2026, 10, 22, 16, tzinfo=timezone.utc)
MEMBERS = ("102", "103", "104")


@pytest.fixture
def service(db):
    db.ensure_channel(200, 100)
    db.enable_channel(200)
    return RoundService(db, FakeProvider())


def start(service, options=("Campus", "Local businesses"), members=MEMBERS):
    return service.start(200, 102, PreparedQuestion("Which market: Campus or Local businesses?", options), members, T0)


async def answer(service, record, user="102", selection=0, reason="We can reach campus students.", named=False, now=T0):
    await service.save_response(
        round_id=record.id, channel_id=200, user_id=user, selection=selection,
        reason=reason, show_name=named, now=now,
    )


async def test_question_extraction_only_uses_command_input(service):
    fake = service.provider
    fake.complete = AsyncMock(wraps=fake.complete)
    prepared = await service.prepare_question(200, "Which market: Campus or Local businesses?")
    assert prepared.options == ("Campus", "Local businesses")
    fake.complete.assert_awaited_once_with(system=prompts.EXTRACT_OPTIONS, user=prepared.question)
    no_options = await service.prepare_question(200, "Where should we focus?")
    assert no_options.options == ()


def test_start_snapshots_members_utc_deadline_and_status(service):
    record = start(service, members=MEMBERS + ("103",))
    assert record.status == "collecting"
    assert record.created_by == "102" and record.channel_id == "200"
    assert record.created_at == utc_iso(T0)
    assert record.deadline_at == utc_iso(T0 + timedelta(hours=24))
    assert record.outcome is None and record.outcome_at is None and record.closed_at is None
    assert [member.user_id for member in service.db.round_members(record.id)] == list(MEMBERS)
    assert service.progress(record.id) == (0, 3)


def test_three_minute_demo_deadline(service):
    service.deadline_hours = 0.05
    assert start(service).deadline_at == utc_iso(T0 + timedelta(minutes=3))


@pytest.mark.parametrize("text, hours", [
    ("1h", 1), ("36h", 36), ("2d", 48), ("1w", 168), ("90m", 1.5),
    ("1 hour", 1), ("3 Days", 72), ("1.5h", 1.5), ("5m", 5 / 60), ("30d", 720),
])
def test_parse_duration_accepts_relative_lengths(text, hours):
    assert parse_duration(text) == pytest.approx(hours)


@pytest.mark.parametrize("text", ["", "soon", "1", "h", "-1h", "0h", "4m", "31d", "5 weeks", "2026-10-30", "NaNh", "infh"])
def test_parse_duration_rejects_bad_or_out_of_range_lengths(text):
    with pytest.raises(RoundError) as error:
        parse_duration(text)
    assert error.value.code == "round_deadline"


@pytest.mark.parametrize("text", ["5", "1", "2.5", "45", "900", "5 da", "3 hrs", "10 min", "day"])
def test_every_duration_suggestion_is_accepted(text):
    assert all(parse_duration(value) for _, value in duration_suggestions(text))


@pytest.mark.parametrize("hours", [5 / 60, 36, 720])
def test_chosen_deadline_persists_and_controls_closure_after_restart(service, hours):
    record = service.start(200, 102, PreparedQuestion("Which market?", ()), MEMBERS, T0, deadline_hours=hours)
    deadline = T0 + timedelta(hours=hours)
    assert record.deadline_at == utc_iso(deadline)
    restored = Database(":memory:")
    restored.connection.deserialize(service.db.connection.serialize())
    try:
        restarted = RoundService(restored, FakeProvider())
        saved = restored.get_round(record.id)
        assert not restarted.ready(saved, deadline - timedelta(microseconds=1))
        assert restarted.ready(saved, deadline)
    finally:
        restored.close()


@pytest.mark.parametrize("hours", [0, -1, 721, float("nan"), float("inf")])
def test_start_rejects_invalid_override_without_creating_round(service, hours):
    with pytest.raises(RoundError, match="round_deadline"):
        service.start(200, 102, PreparedQuestion("Which market?", ()), MEMBERS, T0, deadline_hours=hours)
    assert not service.db.connection.execute("SELECT 1 FROM rounds").fetchone()


async def test_response_upsert_and_consent_default(service):
    record = start(service)
    await answer(service, record)
    first = service.db.round_responses(record.id)[0]
    assert not first.show_name and first.reason == "We can reach campus students."
    await answer(service, record, selection=1, reason="Local contacts may help.", named=True, now=T0 + timedelta(minutes=1))
    rows = service.db.round_responses(record.id)
    assert len(rows) == 1 and rows[0].option_label == "Local businesses"
    assert rows[0].show_name and rows[0].reason == "Local contacts may help."
    assert service.progress(record.id) == (1, 3)


@pytest.mark.parametrize("selection,reason", [(-1, "A reason"), (8, "A reason"), (0, ""), (0, "  "), (0, "x" * 301)])
async def test_option_and_nonempty_max_300_reason_required(service, selection, reason):
    record = start(service)
    with pytest.raises(RoundError):
        await answer(service, record, selection=selection, reason=reason)
    assert service.db.round_responses(record.id) == ()
    assert service.progress(record.id) == (0, 3)


async def test_free_option_saves_explicit_answer_without_a_provider_call(service):
    record = start(service, options=())
    service.provider.complete = AsyncMock()
    await service.save_response(
        round_id=record.id, channel_id=200, user_id=102, selection=0,
        answer=" Campus ", reason="We can reach students.", show_name=False, now=T0,
    )
    row = service.db.round_responses(record.id)[0]
    assert row.option_label == "Campus" and row.reason == "We can reach students."
    assert service.choices(record) == ("Campus",)
    service.provider.complete.assert_not_awaited()


async def test_missing_free_option_fails_without_saving(service):
    record = start(service, options=())
    service.provider.complete = AsyncMock(return_value='{"option_label": null, "reason": "unclear"}')
    with pytest.raises(RoundError, match="input"):
        await answer(service, record, reason="unclear")
    assert not service.db.round_responses(record.id)
    service.provider.complete.assert_not_awaited()


async def test_early_close_only_when_everyone_replied(service):
    record = start(service)
    await answer(service, record)
    assert not service.ready(record, T0)
    with pytest.raises(RoundError, match="not_ready"):
        service.freeze_ready(record.id, T0)
    await answer(service, record, "103")
    await answer(service, record, "104", selection=1, reason="Local contacts are available.")
    assert service.ready(record, T0)
    closed = service.freeze_ready(record.id, T0 + timedelta(minutes=2))
    result = await service.prepare_result(record.id, T0 + timedelta(minutes=2))
    assert closed.status == "closed" and closed.closed_at == utc_iso(T0 + timedelta(minutes=2))
    assert result.counts == (("Campus", 2), ("Local businesses", 1))
    assert result.minority_option == "Local businesses" and not result.deadline_missing
    assert "Local contacts" in result.synthesis.steelman


async def test_deadline_closes_missing_views_and_prevents_late_edits(service):
    record = start(service)
    await answer(service, record)
    deadline = T0 + timedelta(days=1)
    assert service.ready(record, deadline)
    with pytest.raises(RoundError, match="closed"):
        await answer(service, record, "103", now=deadline)
    service.freeze_ready(record.id, deadline)
    result = await service.prepare_result(record.id, deadline)
    assert result.deadline_missing and len(result.responses) == 1 and result.total_members == 3
    assert service.close_prepared(result, deadline).status == "closed"
    with pytest.raises(RoundError, match="closed"):
        await answer(service, record, reason="changed", now=deadline)


async def test_no_replies_closes_without_llm_or_invented_dissent(service):
    record = start(service)
    service.provider.complete = AsyncMock()
    result = await service.prepare_result(record.id, T0 + timedelta(days=1))
    closed = service.close_prepared(result, T0 + timedelta(days=1))
    assert closed.status == "closed" and result.deadline_missing
    assert result.minority_option is None and not result.reasons
    service.provider.complete.assert_not_awaited()


async def test_synthesis_receives_only_stored_reasons_no_identity_or_chat(service):
    record = start(service, members=("102", "103"))
    await answer(service, record, reason="Campus access is easier.")
    await answer(service, record, "103", selection=1, reason="Local contacts are available.")
    original = service.provider.complete
    calls = []

    async def spy(**kwargs):
        calls.append(kwargs)
        return await original(**kwargs)

    service.provider.complete = spy
    result = await service.prepare_result(record.id, T0)
    payload = json.loads(next(call["user"] for call in calls if call["system"] == prompts.STEELMAN_MINORITY))
    assert set(payload) == {"minority_option", "reasons"}
    assert payload["reasons"] == [
        {"option_label": "Campus", "reason": "Campus access is easier."},
        {"option_label": "Local businesses", "reason": "Local contacts are available."},
    ]
    assert "102" not in json.dumps(payload) and "103" not in json.dumps(payload)
    assert "Campus access" in result.synthesis.trade_off and "Local contacts" in result.synthesis.trade_off
    assert result.synthesis.question and "recommend" not in result.synthesis.model_dump_json().lower()
    assert result.minority_option == "Campus"  # First option breaks a tie.


async def test_translated_reason_is_temporary_and_original_is_kept(service):
    record = start(service, members=("102",))
    await answer(service, record, reason="校园更方便。")
    original = service.provider.complete

    async def translate(**kwargs):
        if kwargs["system"] == prompts.PARSE_RESPONSE:
            return json.dumps({"option_label": "Campus", "reason": "Campus is more convenient."})
        return await original(**kwargs)

    service.provider.complete = translate
    result = await service.prepare_result(record.id, T0)
    assert result.reasons[0].english == "Campus is more convenient."
    assert result.reasons[0].response.reason == "校园更方便。"
    assert "Campus is more convenient." not in "\n".join(service.db.connection.iterdump())
    assert result.minority_option is None


async def test_outcome_timestamp_owner_and_time_to_decision_once(service):
    record = start(service, members=("102",))
    await answer(service, record)
    service.freeze_ready(record.id, T0 + timedelta(minutes=2))
    assert service.log(200)[0].outcome is None
    result = service.outcome(record.id, 200, "103", 1, T0 + timedelta(minutes=7))
    assert result.outcome == "Local businesses" and result.owner_user_id == "103"
    assert result.outcome_at == utc_iso(T0 + timedelta(minutes=7))
    assert result.time_to_decision_seconds == 420
    with pytest.raises(RoundError, match="outcome"):
        service.outcome(record.id, 200, "104", 0, T0 + timedelta(minutes=8))
    assert service.db.get_round(record.id).owner_user_id == "103"


async def test_none_of_these_outcome_and_unclosed_outcome_rejected(service):
    record = start(service)
    with pytest.raises(RoundError, match="outcome"):
        service.outcome(record.id, 200, "102", 0, T0)
    service.freeze_ready(record.id, T0 + timedelta(days=1))
    assert service.outcome(record.id, 200, "102", 2, T0 + timedelta(days=1)).outcome == "None of these"


async def test_log_limits_to_last_ten_channel_decisions_no_responses(service):
    for index in range(12):
        record = start(service)
        service.freeze_ready(record.id, T0 + timedelta(days=1, minutes=index))
    entries = service.log(200)
    assert len(entries) == 10 and [entry.id for entry in entries] == list(range(12, 2, -1))
    assert set(vars(entries[0])) == {"id", "question", "outcome", "owner_user_id", "closed_at"}
    assert service.db.decision_log("201") == ()


@pytest.mark.parametrize("state", ["unknown", "paused"])
async def test_setup_and_pause_guard_all_round_operations(db, state):
    service = RoundService(db, FakeProvider())
    service.provider.complete = AsyncMock()
    if state == "paused":
        db.ensure_channel(200, 100)
        db.enable_channel(200)
        db.set_paused(200, True)
    with pytest.raises(RoundError, match="setup|paused"):
        await service.prepare_question(200, "Which market?")
    service.provider.complete.assert_not_awaited()
    with pytest.raises(RoundError):
        service.log(200)


async def test_foreign_channel_and_nonparticipant_cannot_submit(service):
    record = start(service)
    service.db.ensure_channel(201, 100)
    service.db.enable_channel(201)
    with pytest.raises(RoundError, match="round"):
        service.get(record.id, 201, "102")
    with pytest.raises(RoundError, match="member"):
        await answer(service, record, "999")
    assert not service.db.round_responses(record.id)


def test_halfway_reminders_only_once_and_only_missing(service):
    record = start(service)
    assert service.reminder_candidates(record.id, T0 + timedelta(hours=11)) == ()
    assert service.reminder_candidates(record.id, T0 + timedelta(hours=12)) == MEMBERS
    service.db.mark_reminded(record.id, ("102",))
    assert service.reminder_candidates(record.id, T0 + timedelta(hours=13)) == ("103", "104")
    assert service.reminder_candidates(record.id, T0 + timedelta(hours=24)) == ()


@pytest.mark.parametrize("question", ["Assignment prompt: write our report", "Please write my assignment", "Draft our deliverable", "Grade: 95"])
async def test_forbidden_inputs_refused_before_provider_or_persistence(service, question):
    service.provider.complete = AsyncMock()
    with pytest.raises(RoundError, match="boundary"):
        await service.prepare_question(200, question)
    service.provider.complete.assert_not_awaited()
    assert service.db.active_round_ids() == ()


async def test_invalid_and_recommending_synthesis_retry_then_fail_quietly(service, caplog):
    record = start(service, members=("102",))
    await answer(service, record)
    original = service.provider.complete
    failures = 0

    async def unsafe(**kwargs):
        nonlocal failures
        if kwargs["system"] == prompts.STEELMAN_MINORITY:
            failures += 1
            return json.dumps({"steelman": "I recommend Campus", "trade_off": "private-invalid-output", "question": "Choose?"})
        return await original(**kwargs)

    service.provider.complete = unsafe
    with pytest.raises(RoundError, match="provider"):
        await service.prepare_result(record.id, T0)
    assert failures == 2
    assert "private-invalid-output" not in caplog.text


async def test_stale_synthesis_cannot_close_changed_response(service):
    record = start(service, members=("102",))
    await answer(service, record)
    prepared = await service.prepare_result(record.id, T0)
    await answer(service, record, reason="A changed priority.", now=T0 + timedelta(seconds=1))
    with pytest.raises(RoundError, match="stale"):
        service.close_prepared(prepared, T0 + timedelta(seconds=1))
    assert service.db.get_round(record.id).status == "collecting"
