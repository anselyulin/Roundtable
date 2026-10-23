from datetime import timedelta
from unittest.mock import AsyncMock, patch
import asyncio

import pytest

from bot.commands import ERROR_TEXT
from bot.formatting import PAUSED_NOTICE, result_message
from bot.main import RoundtableClient
from bot.views import ShareView
from core.db import Database, utc_iso
from core.rounds import RoundError
from test_commands import http_error
from test_round_discord import context, submit
from test_rounds import T0, answer, service, start


async def test_core_end_early_freezes_partial_round_and_preserves_deadline(service):
    record = start(service)
    await answer(service, record)
    when = T0 + timedelta(minutes=5)
    closed = service.end_early(record.id, 200, 102, when)
    assert closed.status == "closed" and closed.closed_at == utc_iso(when)
    assert closed.deadline_at == record.deadline_at and closed.outcome_at is None
    assert not service.reminder_candidates(record.id, T0 + timedelta(hours=12))
    with pytest.raises(RoundError, match="closed"):
        await answer(service, record, "103", now=when)
    result = await service.prepare_result(record.id, when)
    assert result.deadline_missing and len(result.responses) == 1
    assert "Ended early. Proceeding with 1 of 3 views." in result_message(result, {})["content"]
    with pytest.raises(RoundError, match="closed"):
        service.end_early(record.id, 200, 102, when + timedelta(minutes=1))
    assert service.db.get_round(record.id).closed_at == utc_iso(when)


async def test_empty_round_can_end_without_provider_call(service):
    record = start(service)
    service.provider.complete = AsyncMock()
    service.end_early(record.id, 200, 102, T0)
    result = await service.prepare_result(record.id, T0)
    assert result.deadline_missing and not result.responses
    assert "Proceeding with 0 of 3 views." in result_message(result, {})["content"]
    service.provider.complete.assert_not_awaited()


@pytest.mark.parametrize("invalid", ["member", "paused", "other_channel", "missing", "cancelled"])
def test_end_early_core_guards(service, invalid):
    record = start(service)
    channel_id, user_id, round_id = 200, 102, record.id
    if invalid == "member":
        user_id = 103
    elif invalid == "paused":
        service.db.set_paused(200, True)
    elif invalid == "other_channel":
        service.db.ensure_channel(201, 100)
        service.db.enable_channel(201)
        channel_id = 201
    elif invalid == "missing":
        round_id = 999
    else:
        service.db.cancel_round(record.id)
    with pytest.raises(RoundError):
        service.end_early(round_id, channel_id, user_id, T0)
    assert service.db.get_round(record.id).closed_at is None


@pytest.mark.parametrize("actor", ["starter", "manager", "owner"])
async def test_end_button_authorizes_starter_or_channel_control_and_publishes_result(context, actor):
    ctx, record = context, context.begin()
    await submit(ctx, record, reason="Only this view is available yet.")
    ctx.channel.permissions_for.return_value.manage_channels = actor == "manager"
    user_id = 102 if actor == "starter" else 103
    if actor == "owner":
        ctx.guild.owner_id = 103
    view = ShareView(ctx.ui, record.id)
    assert view.is_persistent() and view.children[1].label == "End round early"
    interaction = ctx.make(user_id)
    await view.children[1].callback(interaction)
    interaction.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
    assert ctx.db.get_round(record.id).status == "closed"
    payload = ctx.channel.get_partial_message.return_value.edit.call_args.kwargs
    assert "Ended early. Proceeding with 1 of 3 views." in payload["content"]
    assert payload["content"].startswith("Roundtable (AI):")
    assert payload["embed"].fields[3].name == "What members said"
    assert payload["embed"].fields[3].value.startswith("**Campus**\n> ")  # Anonymous by default.
    assert [button.label for button in payload["view"].children] == ["Campus", "Local businesses", "None of these"]
    assert interaction.followup.send.call_args.kwargs["ephemeral"]
    await ctx.ui.log_outcome(ctx.make(), record.id, 1)
    assert ctx.db.get_round(record.id).outcome_at and ctx.service.log(200)[0].outcome == "Local businesses"


@pytest.mark.parametrize("invalid", ["ordinary_member", "paused", "wrong_message", "bot", "hidden"])
async def test_end_button_cannot_bypass_permissions_pause_or_message_binding(context, invalid):
    ctx, record = context, context.begin()
    interaction = ctx.make(103 if invalid == "ordinary_member" else 102)
    if invalid == "ordinary_member":
        ctx.channel.permissions_for.return_value.manage_channels = False
    elif invalid == "paused":
        ctx.db.set_paused(200, True)
    elif invalid == "wrong_message":
        interaction.message.id = 999
    elif invalid == "hidden":
        ctx.channel.permissions_for.return_value.view_channel = False
    else:
        interaction.user.bot = True
    await ctx.ui.end_round(interaction, record.id)
    assert ctx.db.get_round(record.id).status == "collecting"
    ctx.channel.get_partial_message.return_value.edit.assert_not_awaited()
    ctx.channel.send.assert_not_awaited()


async def test_simultaneous_end_clicks_publish_once_and_never_change_closure(context):
    ctx, record = context, context.begin()
    await submit(ctx, record)
    await asyncio.gather(ctx.ui.end_round(ctx.make(), record.id), ctx.ui.end_round(ctx.make(), record.id))
    payloads = ctx.channel.get_partial_message.return_value.edit.await_args_list
    assert sum("content" in call.kwargs for call in payloads) == 1
    assert ctx.db.get_round(record.id).closed_at == utc_iso(T0)


@pytest.mark.parametrize("failure", ["provider", "discord"])
async def test_end_freezes_immediately_and_scheduler_retries_failed_publication(context, failure):
    ctx, record = context, context.begin()
    await submit(ctx, record)
    provider = ctx.service.provider.complete
    if failure == "provider":
        ctx.service.provider.complete = AsyncMock(return_value="PRIVATE_INVALID_OUTPUT")
    else:
        ctx.channel.get_partial_message.return_value.edit.side_effect = http_error()
    await ctx.ui.end_round(ctx.make(), record.id)
    assert ctx.db.get_round(record.id).status == "closed" and record.id in ctx.ui.pending_results
    assert ctx.db.get_round(record.id).outcome_at is None
    ctx.service.provider.complete = provider
    ctx.channel.get_partial_message.return_value.edit.side_effect = None
    await ctx.scheduler.tick(now=T0 + timedelta(minutes=1))
    assert record.id not in ctx.ui.pending_results
    assert "Ended early" in ctx.channel.get_partial_message.return_value.edit.call_args.kwargs["content"]


async def test_old_round_controls_upgrade_after_restart_and_wait_while_paused(context, settings):
    ctx, record = context, context.begin()
    ctx.db.set_paused(200, True)
    restarted = RoundtableClient(settings, ctx.db)
    restarted.get_channel = lambda channel_id: ctx.channel
    try:
        restarted.tree.sync = AsyncMock()
        with patch.object(restarted.scheduler.run, "start"):
            await restarted.setup_hook()
        assert record.id in restarted.rounds_ui.pending_share_views
        await restarted.scheduler.tick(now=T0)
        ctx.channel.get_partial_message.return_value.edit.assert_not_awaited()
        ctx.db.set_paused(200, False)
        await restarted.scheduler.tick(now=T0)
        payload = ctx.channel.get_partial_message.return_value.edit.call_args.kwargs
        assert any(button.label == "End round early" for button in payload["view"].children)
        assert payload["content"].startswith("Roundtable (AI):")
        assert "end it early" in payload["embed"].description and payload["embed"].footer.text == "0 of 3 have shared a view"
        assert record.id not in restarted.rounds_ui.pending_share_views
        interaction = ctx.make()
        interaction.client = restarted
        await payload["view"].children[1].callback(interaction)
        assert ctx.db.get_round(record.id).status == "closed"
    finally:
        await restarted.close()


async def test_failed_early_result_recovers_after_restart_without_changing_closed_at(context, settings):
    ctx, record = context, context.begin()
    await submit(ctx, record)
    ctx.service.provider.complete = AsyncMock(return_value="PRIVATE_INVALID_OUTPUT")
    await ctx.ui.end_round(ctx.make(), record.id)
    closed_at = ctx.db.get_round(record.id).closed_at
    restored_db = Database(":memory:")
    restored_db.connection.deserialize(ctx.db.connection.serialize())
    restarted = RoundtableClient(settings, restored_db)
    restarted.get_channel = lambda channel_id: ctx.channel
    try:
        await restarted.scheduler.tick(now=T0 + timedelta(minutes=1))
        assert restored_db.get_round(record.id).closed_at == closed_at
        assert "Ended early" in ctx.channel.get_partial_message.return_value.edit.call_args.kwargs["content"]
    finally:
        await restarted.close()
        restored_db.close()
