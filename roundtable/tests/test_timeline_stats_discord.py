import asyncio
from dataclasses import replace
from datetime import timedelta
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord
import pytest

from bot import formatting
from bot.commands import ERROR_TEXT
from bot.formatting import GENERIC_ERROR, PAUSED_NOTICE, RESUMED_NOTICE
from bot.main import RoundtableClient
from bot.views import TimelinePostButton
from core.db import Database, utc_iso
from core.rounds import RoundError
from test_commands import http_error
from test_round_discord import context
from test_rounds import T0


async def preview(ctx, *, deadline_days=10):
    ctx.client.timeline.add_deadline(200, "Team checkpoint", (T0 + timedelta(days=deadline_days)).date().isoformat(), T0)
    interaction = ctx.make()
    await ctx.client.tasks_ui.build_timeline(interaction)
    assert interaction.followup.send.call_args.kwargs["ephemeral"]
    return interaction.followup.send.call_args.kwargs


async def test_deadline_acknowledges_privately_and_stores_only_manual_date(context):
    ctx = context
    interaction = ctx.make()
    await ctx.client.tasks_ui.deadline_add(interaction, "Team checkpoint", "2026-12-31")
    interaction.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
    assert interaction.followup.send.call_args.kwargs["ephemeral"]
    deadline = ctx.db.nearest_deadline(200, T0.isoformat())
    assert deadline.kind == "assignment" and deadline.title == "Team checkpoint"
    assert deadline.due_at == "2026-12-31T23:59:59.999999+00:00"
    ctx.channel.send.assert_not_awaited()


async def test_timeline_is_private_until_explicit_post_and_warns_about_late_task(context):
    ctx = context
    ctx.client.tasks.add(200, 102, "Compare choices", (T0 + timedelta(days=11)).date().isoformat(), T0)
    payload = await preview(ctx)
    ctx.channel.send.assert_not_awaited()
    text = str(payload["embed"].to_dict())
    assert 'Heads up: "Compare choices" is due after the assignment deadline.' in text
    assert "Buffer" in text
    button = payload["view"].children[0]
    assert button.item.label == "Post to channel"
    wrong = ctx.make(103)
    await button.callback(wrong)
    assert wrong.followup.send.call_args.args[0] == ERROR_TEXT["task_private"]
    ctx.channel.send.assert_not_awaited()
    interaction = ctx.make()
    await button.callback(interaction)
    interaction.response.defer.assert_awaited_once_with(ephemeral=True, thinking=False)
    sent = ctx.channel.send.call_args
    assert sent.args[0].startswith("Roundtable (AI):")
    assert sent.kwargs["embed"].to_dict() == payload["embed"].to_dict()
    assert sent.kwargs["allowed_mentions"].to_dict() == {"parse": []}
    assert interaction.edit_original_response.call_args.kwargs["view"].children[0].item.disabled
    assert not ctx.client.timeline._previews
    assert ctx.db.connection.execute("SELECT count(*) FROM deadlines").fetchone()[0] == 1


async def test_repeated_or_racing_timeline_posts_send_only_once(context):
    ctx = context
    payload = await preview(ctx)
    button = payload["view"].children[0]
    await asyncio.gather(button.callback(ctx.make()), button.callback(ctx.make()))
    assert ctx.channel.send.await_count == 1


async def test_failed_timeline_send_can_retry_but_failed_edit_cannot_duplicate_post(context):
    ctx = context
    button = (await preview(ctx))["view"].children[0]
    ctx.channel.send.side_effect = http_error()
    failed = ctx.make()
    await button.callback(failed)
    assert failed.followup.send.call_args.args[0] == GENERIC_ERROR and ctx.client.timeline._previews
    ctx.channel.send.side_effect = None
    delivered = ctx.make()
    delivered.edit_original_response.side_effect = http_error()
    await button.callback(delivered)
    assert not ctx.client.timeline._previews
    posts = ctx.channel.send.await_count
    await button.callback(ctx.make())
    assert ctx.channel.send.await_count == posts


async def test_pause_discards_timeline_and_invalidates_old_private_preview(context):
    ctx = context
    button = (await preview(ctx))["view"].children[0]
    await ctx.client.handlers.pause(ctx.make())
    paused = ctx.make()
    await button.callback(paused)
    assert paused.followup.send.call_args.args[0] == PAUSED_NOTICE
    await ctx.client.handlers.resume(ctx.make())
    resumed = ctx.make()
    await button.callback(resumed)
    assert resumed.followup.send.call_args.args[0] == ERROR_TEXT["timeline_preview"]
    assert [call.args[0] for call in ctx.channel.send.await_args_list] == [PAUSED_NOTICE, RESUMED_NOTICE]  # Pause/resume notices only.


async def test_pause_resume_during_timeline_model_discards_result_and_prevents_retry(context):
    ctx = context
    ctx.client.timeline.add_deadline(200, "Team checkpoint", "2026-12-31", T0)
    started, finish = asyncio.Event(), asyncio.Event()

    async def slow(**kwargs):
        started.set()
        await finish.wait()
        return "INVALID_PRIVATE_RESPONSE"

    ctx.client.timeline.provider.complete = AsyncMock(side_effect=slow)
    interaction = ctx.make()
    job = asyncio.create_task(ctx.client.tasks_ui.build_timeline(interaction))
    await started.wait()
    await ctx.client.handlers.pause(ctx.make())
    await ctx.client.handlers.resume(ctx.make())
    finish.set()
    await job
    assert ctx.client.timeline.provider.complete.await_count == 1
    assert not ctx.client.timeline._previews
    assert [call.args[0] for call in ctx.channel.send.await_args_list] == [PAUSED_NOTICE, RESUMED_NOTICE]  # Pause/resume notices only.


async def test_timeline_preview_expires_after_restart_but_callback_gives_safe_private_reply(context, settings):
    ctx = context
    old = (await preview(ctx))["view"].children[0]
    restored = Database(":memory:")
    restored.connection.deserialize(ctx.db.connection.serialize())
    client = RoundtableClient(settings, restored)
    try:
        interaction = ctx.make()
        interaction.client = client
        match = TimelinePostButton.__discord_ui_compiled_template__.fullmatch(old.custom_id)
        rebuilt = await TimelinePostButton.from_custom_id(interaction, old.item, match)
        await rebuilt.callback(interaction)
        assert interaction.followup.send.call_args.args[0] == ERROR_TEXT["timeline_preview"]
        assert interaction.followup.send.call_args.kwargs["ephemeral"]
        ctx.channel.send.assert_not_awaited()
    finally:
        await client.close()
        restored.close()


async def test_timeline_dates_and_model_response_errors_are_safe_private_and_not_saved(context, caplog):
    ctx = context
    interaction = ctx.make()
    await ctx.client.tasks_ui.deadline_add(interaction, "Team checkpoint", "bad-date")
    assert interaction.followup.send.call_args.args[0] == ERROR_TEXT["date"]
    ctx.client.timeline.add_deadline(200, "Team checkpoint", "2026-12-31", T0)
    ctx.client.timeline.provider.complete = AsyncMock(return_value="PRIVATE_INVALID_RESPONSE")
    bad = ctx.make()
    await ctx.client.tasks_ui.build_timeline(bad)
    assert ctx.client.timeline.provider.complete.await_count == 2
    assert bad.followup.send.call_args.args[0] == GENERIC_ERROR
    assert "PRIVATE_INVALID_RESPONSE" not in "\n".join(ctx.db.connection.iterdump()) + caplog.text
    ctx.channel.send.assert_not_awaited()


async def test_stats_query_and_output_have_no_names_ids_message_text_or_member_breakdown(context):
    ctx = context
    record = ctx.begin()
    await ctx.service.save_response(round_id=record.id, channel_id=200, user_id=102, selection=0, reason="PRIVATE_REASON_SENTINEL_3198", show_name=True, now=T0)
    ctx.db.close_round(record.id, utc_iso(T0 + timedelta(minutes=3)))
    ctx.db.record_outcome(record.id, "PRIVATE_OUTCOME_SENTINEL_7251", "103", utc_iso(T0 + timedelta(minutes=10)))
    ctx.client.tasks.add(200, 102, "PRIVATE_TASK_SENTINEL_8712", None, T0)
    ctx.client.tasks.prefs(200, 102, "PRIVATE_PREF_SENTINEL_4972")
    for name in ("prompts_shown", "prompts_accepted", "clarify_uses", "clarify_helpful"):
        ctx.db.increment_metric(200, name)
    ctx.client.tasks.provider.complete = AsyncMock()
    statements = []
    ctx.db.connection.set_trace_callback(statements.append)
    interaction = ctx.make()
    with patch.object(ctx.guild, "get_member", side_effect=AssertionError("Stats must not look up identities")):
        await ctx.client.tasks_ui.stats(interaction)
    interaction.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
    payload = interaction.followup.send.call_args.kwargs
    assert payload["ephemeral"] and set(payload) == {"embed", "ephemeral", "allowed_mentions"}
    text = json.dumps(payload["embed"].to_dict())
    for forbidden in ("Ana", "Ben", "Chen", "102", "103", "104", "PRIVATE_", record.question, "user_id", "owner", "created_by"):
        assert forbidden not in text
    assert len(payload["embed"].fields) == 13
    values = {field.name: field.value for field in payload["embed"].fields}
    assert values["Rounds started"] == values["Rounds closed"] == "1"
    assert values["Average share of members who replied per round"] == "33.3%"
    assert values["Median time from round start to logged outcome"] == "10.0 minutes"
    assert values["Prompt accept rate"] == values["Clarify helpful rate"] == "100.0%"
    selects = " ".join(statement for statement in statements if statement.lstrip().upper().startswith("SELECT") and "FROM channels" not in statement)
    for forbidden in ("question", "title", "reason", "user_id", "preference_note"):
        assert forbidden not in selects
    ctx.client.tasks.provider.complete.assert_not_awaited()
    ctx.channel.send.assert_not_awaited()


async def test_stats_empty_denominators_and_live_overdue_counts(context):
    ctx = context
    interaction = ctx.make()
    await ctx.client.tasks_ui.stats(interaction)
    values = {field.name: field.value for field in interaction.followup.send.call_args.kwargs["embed"].fields}
    assert values["Prompt accept rate"] == values["Clarify helpful rate"] == values["Median time from round start to logged outcome"] == "N/A"
    for status in ("proposed", "confirmed", "done"):
        task = ctx.client.tasks.add(200, 102, "Compare choices", None, T0)
        ctx.db.connection.execute("UPDATE tasks SET due_at=?,status=? WHERE id=?", (utc_iso(T0 - timedelta(seconds=1)), status, task.id))
    interaction = ctx.make()
    await ctx.client.tasks_ui.stats(interaction)
    values = {field.name: field.value for field in interaction.followup.send.call_args.kwargs["embed"].fields}
    assert values["Confirmed tasks currently overdue"] == "1"


@pytest.mark.parametrize("state", ["unknown", "disabled", "wrong_guild", "hidden", "bot"])
async def test_new_commands_require_setup_and_current_channel_access(context, state):
    ctx = context
    interaction = ctx.make()
    if state == "unknown":
        ctx.db.connection.execute("DELETE FROM channels")
    elif state == "disabled":
        ctx.db.connection.execute("UPDATE channels SET enabled=0")
    elif state == "wrong_guild":
        interaction.guild = SimpleNamespace(id=999)
    elif state == "hidden":
        ctx.channel.permissions_for.return_value = discord.Permissions.none()
    else:
        interaction.user.bot = True
    ctx.client.tasks.provider.complete = AsyncMock()
    await ctx.client.tasks_ui.stats(interaction)
    await ctx.client.tasks_ui.add(interaction, "Compare choices")
    await ctx.client.tasks_ui.build_timeline(interaction)
    assert not ctx.db.channel_tasks(200)
    ctx.channel.send.assert_not_awaited()
    ctx.client.tasks.provider.complete.assert_not_awaited()


async def test_new_public_formatters_escape_mentions_and_enforce_size(context):
    ctx = context
    ctx.client.timeline.add_deadline(200, "@everyone **Checkpoint**", "2026-12-31", T0)
    proposal = await ctx.client.timeline.build(200, T0)
    proposal = replace(proposal, milestones=(("@everyone **Review**", T0.date().isoformat()),) + proposal.milestones)
    embed = formatting.timeline_embed(proposal)
    assert "@everyone" not in str(embed.to_dict())
    assert "\\*\\*Review\\*\\*" in str(embed.fields[0].value)
    with pytest.raises(RoundError, match="capacity"):
        formatting.timeline_embed(replace(proposal, late_tasks=("x" * 200,) * 30))
