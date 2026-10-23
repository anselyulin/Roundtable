"""Open-ended rounds use member-authored answers, with no live API calls."""

from datetime import timedelta
from unittest.mock import AsyncMock

import discord
import pytest

from bot.commands import ERROR_TEXT
from bot.formatting import PAUSED_NOTICE
from bot.main import RoundtableClient
from bot.views import PrivateButton, private_id
from core.db import Database
from core.rounds import RoundError
from test_round_discord import context, modal_data
from test_rounds import T0


def answer_data(answer, reason="This fits our available time.", custom_id=""):
    data = modal_data(reason, custom_id)
    data["components"].insert(0, {"type": 1, "components": [
        {"type": 4, "custom_id": "rt:answer", "value": answer},
    ]})
    return data


async def save(ctx, record, answer, *, user_id=102, reason="This fits our available time.", named=False):
    interaction = ctx.make(user_id, answer_data(answer, reason))
    await ctx.ui.submit_modal(interaction, record.id, user_id, 0, named)
    return interaction


async def test_open_question_extracts_no_options_and_each_member_gets_empty_private_fields(context):
    ctx = context
    question = "How should we organize our meetings?"
    await ctx.ui.decide(ctx.make(), question)
    record = ctx.db.get_round(1)
    assert record.question == question and record.options == ()
    await save(ctx, record, "Weekly check-ins")
    for user_id in (102, 103):
        share = ctx.make(user_id)
        await ctx.ui.share(share, record.id)
        kwargs = share.followup.send.call_args.kwargs
        assert kwargs["ephemeral"]
        view = kwargs["view"]
        assert len(view.children) == 2
        assert not any(isinstance(item.item, discord.ui.Select) for item in view.children)
        button = view.children[0]
        assert button.item.label == "Add my answer" and not button.item.disabled
        interaction = ctx.make(user_id)
        await button.callback(interaction)
        interaction.response.defer.assert_not_awaited()
        modal = interaction.response.send_modal.call_args.args[0]
        assert [field.custom_id for field in modal.children] == ["rt:answer", "rt:reason"]
        assert [field.max_length for field in modal.children] == [80, 300]
        assert all(field.required for field in modal.children)
        if user_id == 102:
            assert modal.children[0].default == "Weekly check-ins"
        else:
            assert all(not field.default for field in modal.children)


async def test_answers_and_reasons_stay_private_until_close_and_edits_replace_own_response(context, caplog):
    ctx, record = context, context.begin(options=())
    ctx.service.provider.complete = AsyncMock()
    first = await save(ctx, record, "Weekly check-ins", reason="A private first reason.")
    assert first.followup.send.call_args.kwargs["ephemeral"]
    assert ctx.service.progress(record.id) == (1, 3)
    await save(ctx, record, "Async updates", reason="A private updated reason.", named=True)
    await save(ctx, record, "Weekly check-ins", user_id=103, reason="Another private reason.")
    rows = ctx.db.round_responses(record.id)
    assert [(row.user_id, row.option_label) for row in rows] == [("102", "Async updates"), ("103", "Weekly check-ins")]
    assert rows[0].show_name and rows[0].reason == "A private updated reason."
    assert ctx.service.progress(record.id) == (2, 3)
    assert ctx.db.get_round(record.id).options == ()
    ctx.service.provider.complete.assert_not_awaited()
    ctx.channel.send.assert_not_awaited()
    for call in ctx.channel.get_partial_message.return_value.edit.await_args_list:
        assert set(call.kwargs) == {"embed"}
        assert "private" not in str(call.kwargs["embed"].to_dict())
        assert "Async updates" not in str(call.kwargs["embed"].to_dict())
    assert "private" not in caplog.text and "Async updates" not in caplog.text


@pytest.mark.parametrize("answer,reason", [
    ("", "A reason"), ("  ", "A reason"), ("x" * 81, "A reason"),
    ("None of these", "A reason"), (" another OPTION ", "A reason"),
    ("Weekly meetings", ""), ("Weekly meetings", "x" * 301),
    ("Write our report", "A reason"),
])
async def test_invalid_open_answers_or_reasons_never_save(context, answer, reason):
    ctx, record = context, context.begin(options=())
    ctx.service.provider.complete = AsyncMock()
    interaction = await save(ctx, record, answer, reason=reason)
    assert not ctx.db.round_responses(record.id) and ctx.service.progress(record.id) == (0, 3)
    assert interaction.followup.send.call_args.kwargs["ephemeral"]
    ctx.service.provider.complete.assert_not_awaited()


@pytest.mark.parametrize("malformed", ["missing", "duplicate", "extra", "wrong_type"])
async def test_open_modal_requires_exactly_one_answer_and_reason(context, malformed):
    ctx, record = context, context.begin(options=())
    data = answer_data("Weekly meetings")
    if malformed == "missing":
        data = modal_data("A reason")
    elif malformed == "duplicate":
        data["components"][1] = data["components"][0]
    elif malformed == "extra":
        data["components"].append({"components": [{"custom_id": "extra", "value": "ignored"}]})
    else:
        data["components"][0]["components"][0]["value"] = 42
    interaction = ctx.make(data=data)
    await ctx.ui.submit_modal(interaction, record.id, 102, 0, False)
    assert not ctx.db.round_responses(record.id)
    assert interaction.followup.send.call_args.args[0] == ERROR_TEXT["input"]


async def test_each_answer_appears_in_neutral_result_and_outcome_log(context):
    ctx, record = context, context.begin(options=())
    await save(ctx, record, "Weekly check-ins", reason="Regular meetings help us coordinate.")
    await save(ctx, record, "Async updates", user_id=103, reason="Written updates fit our schedules.")
    await save(ctx, record, "Weekly check-ins", user_id=104, reason="We can resolve questions together.")
    assert ctx.db.get_round(record.id).status == "closed"
    payload = ctx.channel.get_partial_message.return_value.edit.call_args.kwargs
    assert "**2** \u00b7 Weekly check-ins" in payload["embed"].description
    assert "**1** \u00b7 Async updates" in payload["embed"].description
    assert [button.label for button in payload["view"].children] == ["Weekly check-ins", "Async updates", "None of these"]
    groups = [field.value.split("\n")[0] for field in payload["embed"].fields[3:]]
    assert groups == ["**Weekly check-ins**", "**Async updates**"]  # Replies grouped by answer.
    assert "Written updates" in payload["embed"].fields[0].value
    await ctx.ui.log_outcome(ctx.make(), record.id, 1)
    logged = ctx.db.get_round(record.id)
    assert logged.outcome == "Async updates" and logged.outcome_at
    assert ctx.service.log(200)[0].outcome == "Async updates"


async def test_open_answer_at_deadline_is_rejected_and_existing_answer_closes(context, monkeypatch):
    ctx, record = context, context.begin(options=())
    await save(ctx, record, "Weekly check-ins")
    monkeypatch.setattr("core.rounds.now_utc", lambda: T0 + timedelta(days=1))
    monkeypatch.setattr("bot.commands.now_utc", lambda: T0 + timedelta(days=1))
    await save(ctx, record, "Too late", user_id=103)
    assert len(ctx.db.round_responses(record.id)) == 1
    await ctx.scheduler.tick(now=T0 + timedelta(days=1))
    assert ctx.db.get_round(record.id).status == "closed"
    assert "Proceeding with 1 of 3 views." in ctx.channel.get_partial_message.return_value.edit.call_args.kwargs["content"]


async def test_answer_payload_is_unread_when_paused_or_for_another_member(context):
    class Unreadable(dict):
        def get(self, *args, **kwargs):
            raise AssertionError("Private payload must not be read")

    ctx, record = context, context.begin(options=())
    wrong = ctx.make(data=Unreadable())
    await ctx.ui.submit_modal(wrong, record.id, 103, 0, False)
    ctx.db.set_paused(200, True)
    paused = ctx.make(data=Unreadable())
    await ctx.ui.submit_modal(paused, record.id, 102, 0, False)
    assert paused.followup.send.call_args.args[0] == PAUSED_NOTICE
    assert not ctx.db.round_responses(record.id)


async def test_open_answer_controls_and_modals_survive_restart(context, settings):
    ctx, record = context, context.begin(options=())
    await save(ctx, record, "Weekly check-ins")
    restored_db = Database(":memory:")
    restored_db.connection.deserialize(ctx.db.connection.serialize())
    restarted = RoundtableClient(settings, restored_db)
    restarted.get_channel = lambda channel_id: ctx.channel
    try:
        reopened = ctx.make()
        reopened.client = restarted
        await restarted.rounds_ui.share(reopened, record.id)
        button = reopened.followup.send.call_args.kwargs["view"].children[0]
        match = PrivateButton.__discord_ui_compiled_template__.fullmatch(button.custom_id)
        restored = await PrivateButton.from_custom_id(reopened, button.item, match)
        await restored.callback(reopened)
        modal = reopened.response.send_modal.call_args.args[0]
        assert modal.children[0].default == "Weekly check-ins"
        modal_submit = ctx.make(data=answer_data("Async updates", custom_id=private_id(record.id, 102, 0, False, "submit")))
        modal_submit.client, modal_submit.type = restarted, discord.InteractionType.modal_submit
        await restarted.on_interaction(modal_submit)
        assert restored_db.round_responses(record.id)[0].option_label == "Async updates"
        assert ctx.db.round_responses(record.id)[0].option_label == "Weekly check-ins"
    finally:
        await restarted.close()
        restored_db.close()


async def test_fixed_options_cannot_be_overridden_with_free_text(context):
    ctx, record = context, context.begin()
    with pytest.raises(RoundError, match="input"):
        await ctx.service.save_response(
            round_id=record.id, channel_id=200, user_id=102, selection=0,
            answer="Override", reason="A reason", show_name=False, now=T0,
        )
    assert not ctx.db.round_responses(record.id)


async def test_open_answer_name_consent_toggle_keeps_form_enabled(context):
    ctx, record = context, context.begin(options=())
    share = ctx.make()
    await ctx.ui.share(share, record.id)
    view = share.followup.send.call_args.kwargs["view"]
    assert view.children[1].item.label == "Show my name: Off"
    toggled = ctx.make()
    await view.children[1].callback(toggled)
    replacement = toggled.edit_original_response.call_args.kwargs["view"]
    assert replacement.children[1].item.label == "Show my name: On"
    assert replacement.children[0].item.label == "Add my answer"
    assert not replacement.children[0].item.disabled
    opening = ctx.make()
    await replacement.children[0].callback(opening)
    modal = opening.response.send_modal.call_args.args[0]
    assert ":0:1:submit" in modal.custom_id
    assert len(modal.children) == 2 and not ctx.db.round_responses(record.id)


async def test_five_members_can_submit_distinct_maximum_length_answers(context):
    ctx = context
    record = ctx.begin(options=(), members=("102", "103", "104", "105", "106"))
    for index, user_id in enumerate((102, 103, 104, 105, 106)):
        await ctx.service.save_response(
            round_id=record.id, channel_id=200, user_id=user_id, selection=0,
            answer=f"{index}" + "案" * 79, reason="We have time for this approach.",
            show_name=False, now=T0,
        )
    await ctx.ui.maybe_close(record.id)
    assert ctx.db.get_round(record.id).status == "closed"
    payload = ctx.channel.get_partial_message.return_value.edit.call_args.kwargs
    assert len(payload["view"].children) == 6
    assert [len(row["components"]) for row in payload["view"].to_components()] == [5, 1]
