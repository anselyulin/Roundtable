from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord

from test_rounds import T0
from test_round_discord import context, submit


def forbidden():
    return discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "omitted")


async def test_reminders_at_halfway_only_missing_once_and_no_public_post_on_success(context):
    ctx, record = context, context.begin()
    await submit(ctx, record)
    assert ctx.scheduler.run.seconds == 60
    await ctx.scheduler.tick(now=T0 + timedelta(hours=11))
    assert all(member.send.await_count == 0 for member in ctx.members)
    await ctx.scheduler.tick(now=T0 + timedelta(hours=12))
    assert [member.send.await_count for member in ctx.members] == [0, 1, 1]
    text = ctx.members[1].send.call_args.args[0]
    assert text.startswith("Gentle reminder:") and 'Press "Share my view" in the channel.' in text
    assert "Campus access is easier." not in text
    ctx.channel.send.assert_not_awaited()
    await ctx.scheduler.tick(now=T0 + timedelta(hours=13))
    assert [member.send.await_count for member in ctx.members] == [0, 1, 1]
    assert [member.reminded for member in ctx.db.round_members(record.id)] == [False, True, True]


async def test_forbidden_dm_falls_back_to_one_message_for_all_still_missing(context):
    ctx, record = context, context.begin()
    await submit(ctx, record)
    ctx.members[1].send.side_effect = forbidden()
    await ctx.scheduler.tick(now=T0 + timedelta(hours=12))
    assert [member.send.await_count for member in ctx.members] == [0, 1, 1]
    ctx.channel.send.assert_awaited_once()
    sent = ctx.channel.send.call_args
    assert sent.args[0].startswith("Roundtable (AI): Still waiting on <@103>, <@104>")
    assert "<@102>" not in sent.args[0]
    assert sent.kwargs["allowed_mentions"].to_dict()["users"] == [103, 104]
    await ctx.scheduler.tick(now=T0 + timedelta(hours=13))
    assert ctx.channel.send.await_count == 1 and ctx.members[1].send.await_count == 1


async def test_deadline_takes_precedence_over_reminders_and_reports_missing(context):
    ctx, record = context, context.begin()
    await submit(ctx, record)
    await ctx.scheduler.tick(now=T0 + timedelta(days=1))
    assert ctx.db.get_round(record.id).status == "closed"
    edit = ctx.channel.get_partial_message.return_value.edit.call_args.kwargs
    assert "Proceeding with 1 of 3 views." in edit["content"]
    assert all(member.send.await_count == 0 for member in ctx.members)


async def test_paused_scheduler_has_no_dm_no_llm_and_no_posts_or_edits(context):
    ctx, record = context, context.begin()
    ctx.db.set_paused(200, True)
    ctx.service.provider.complete = AsyncMock()
    await ctx.scheduler.tick(now=T0 + timedelta(hours=12))
    await ctx.scheduler.tick(now=T0 + timedelta(days=1))
    assert ctx.db.get_round(record.id).status == "collecting"
    assert all(member.send.await_count == 0 for member in ctx.members)
    ctx.service.provider.complete.assert_not_awaited()
    ctx.channel.send.assert_not_awaited()
    ctx.channel.get_partial_message.return_value.edit.assert_not_awaited()
    ctx.db.set_paused(200, False)
    await ctx.scheduler.tick(now=T0 + timedelta(days=1))
    assert ctx.db.get_round(record.id).status == "closed"


async def test_provider_failure_still_freezes_deadline_and_retries_pending_result(context):
    ctx, record = context, context.begin()
    await submit(ctx, record)
    original = ctx.service.provider.complete
    ctx.service.provider.complete = AsyncMock(return_value="INVALID_PRIVATE_OUTPUT")
    await ctx.scheduler.tick(now=T0 + timedelta(days=1))
    assert ctx.db.get_round(record.id).status == "closed"
    assert record.id in ctx.ui.pending_results
    assert ctx.db.get_round(record.id).closed_at is not None
    ctx.service.provider.complete = original
    await ctx.scheduler.tick(now=T0 + timedelta(days=1, minutes=1))
    assert record.id not in ctx.ui.pending_results
    assert "Proceeding with 1 of 3 views." in ctx.channel.get_partial_message.return_value.edit.call_args.kwargs["content"]


async def test_discord_publish_failure_retries_without_duplicate_messages(context):
    ctx, record = context, context.begin()
    partial = ctx.channel.get_partial_message.return_value
    partial.edit.side_effect = forbidden()
    await ctx.scheduler.tick(now=T0 + timedelta(days=1))
    assert ctx.db.get_round(record.id).status == "closed" and record.id in ctx.ui.pending_results
    ctx.channel.send.assert_not_awaited()
    partial.edit.side_effect = None
    await ctx.scheduler.tick(now=T0 + timedelta(days=1, minutes=1))
    assert record.id not in ctx.ui.pending_results
    ctx.channel.send.assert_not_awaited()


async def test_deleted_round_message_recreated_on_close_and_new_id_saved(context):
    ctx, record = context, context.begin()
    ctx.channel.get_partial_message.return_value.edit.side_effect = discord.NotFound(SimpleNamespace(status=404, reason="Not found"), "omitted")
    ctx.channel.send.return_value.id = 301
    await ctx.scheduler.tick(now=T0 + timedelta(days=1))
    ctx.channel.send.assert_awaited_once()
    assert ctx.db.get_round(record.id).public_message_id == "301"
    assert ctx.channel.send.call_args.kwargs["content"].startswith("Roundtable (AI):")


async def test_one_failed_channel_does_not_stop_other_rounds(context):
    ctx, first = context, context.begin()
    second = ctx.begin()
    original = ctx.ui.maybe_close

    async def fail_first(round_id, **kwargs):
        if round_id == first.id:
            raise RuntimeError("PRIVATE_EXCEPTION_DO_NOT_LOG")
        return await original(round_id, **kwargs)

    ctx.ui.maybe_close = fail_first
    await ctx.scheduler.tick(now=T0 + timedelta(days=1))
    assert ctx.db.get_round(first.id).status == "collecting"
    assert ctx.db.get_round(second.id).status == "closed"


async def test_saved_outcome_view_failure_retries_disable_without_relogging(context):
    ctx, record = context, context.begin(members=("102",))
    await submit(ctx, record)
    partial = ctx.channel.get_partial_message.return_value
    partial.edit.side_effect = forbidden()
    await ctx.ui.log_outcome(ctx.make(), record.id, 0)
    original_outcome_at = ctx.db.get_round(record.id).outcome_at
    assert original_outcome_at is not None and record.id in ctx.ui.pending_outcome_views
    posts = ctx.channel.send.await_count
    partial.edit.side_effect = None
    await ctx.scheduler.tick(now=T0 + timedelta(minutes=1))
    assert record.id not in ctx.ui.pending_outcome_views
    assert all(button.disabled for button in partial.edit.call_args.kwargs["view"].children)
    assert ctx.channel.send.await_count == posts
    assert ctx.db.get_round(record.id).outcome_at == original_outcome_at
