import asyncio
import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord
import pytest

from bot.commands import ERROR_TEXT
from bot.formatting import GENERIC_ERROR, PAUSED_NOTICE, clarification_embed
from bot.main import RoundtableClient
from bot.views import ClarifyFeedbackButton
from core.clarify import Clarification
from core.db import Database
from core.llm import FakeProvider
from test_clarify_core import metric
from test_commands import http_error
from test_round_discord import context


def selected(ctx, text="Can we compare the choices before deciding?"):
    class SelectedMessage:
        guild = ctx.guild
        channel = ctx.channel
        content = text

        def __getattr__(self, name):
            raise AssertionError("Only selected guild/channel/text may be read")

    return SelectedMessage()


async def ask(ctx):
    interaction = ctx.make()
    await ctx.client.clarify_ui.ask(interaction, selected(ctx))
    return interaction.followup.send.call_args.kwargs["view"]


async def test_context_menu_is_message_only_and_returns_private_explanation(context):
    ctx = context
    command = ctx.client.tree.get_command("Ask Roundtable privately", type=discord.AppCommandType.message)
    assert command is not None and command.guild_only
    assert ctx.client.tree.get_command("Ask Roundtable privately") is None
    interaction = ctx.make()
    original = ctx.client.clarify.provider.complete

    async def check(**kwargs):
        interaction.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
        assert kwargs["user"] == "Can we compare the choices before deciding?"
        return await original(**kwargs)

    ctx.client.clarify.provider.complete = AsyncMock(side_effect=check)
    await ctx.client.clarify_ui.ask(interaction, selected(ctx))
    kwargs = interaction.followup.send.call_args.kwargs
    assert kwargs["ephemeral"] and kwargs["allowed_mentions"].to_dict() == {"parse": []}
    assert [field.name for field in kwargs["embed"].fields] == ["What it likely means", "Other possible readings", "If you want to check"]
    assert [button.item.label for button in kwargs["view"].children] == ["Helpful", "Not helpful"]
    assert metric(ctx.db, "clarify_uses") == 1
    ctx.channel.send.assert_not_awaited()
    ctx.channel.history.assert_not_called()
    ctx.channel.fetch_message.assert_not_called()
    assert all(member.send.await_count == 0 for member in ctx.members)


@pytest.mark.parametrize("index,counter", [(0, "clarify_helpful"), (1, "clarify_unhelpful")])
async def test_feedback_is_private_and_duplicate_or_opposite_click_counts_once(context, index, counter):
    ctx = context
    view = await ask(ctx)
    first, second = ctx.make(), ctx.make()
    await asyncio.gather(view.children[index].callback(first), view.children[1 - index].callback(second))
    assert metric(ctx.db, counter) == 1
    assert metric(ctx.db, "clarify_helpful") + metric(ctx.db, "clarify_unhelpful") == 1
    disabled = first.edit_original_response.call_args.kwargs["view"]
    assert all(button.item.disabled for button in disabled.children)
    assert second.followup.send.call_args.kwargs["ephemeral"]
    ctx.channel.send.assert_not_awaited()


@pytest.mark.parametrize("state", ["paused", "setup", "dm", "hidden", "bot"])
async def test_unauthorized_or_paused_context_does_not_read_selected_message(context, state):
    class Unreadable:
        def __getattribute__(self, name):
            raise AssertionError("Selected message must remain unread")

    ctx = context
    interaction = ctx.make()
    if state == "paused":
        ctx.db.set_paused(200, True)
    elif state == "setup":
        ctx.db.connection.execute("UPDATE channels SET enabled=0")
    elif state == "dm":
        interaction.guild = None
    elif state == "hidden":
        ctx.channel.permissions_for.return_value.view_channel = False
    else:
        interaction.user.bot = True
    ctx.client.clarify.provider.complete = AsyncMock()
    await ctx.client.clarify_ui.ask(interaction, Unreadable())
    ctx.client.clarify.provider.complete.assert_not_awaited()
    assert not metric(ctx.db, "clarify_uses")
    ctx.channel.send.assert_not_awaited()


@pytest.mark.parametrize("source", ["foreign_channel", "foreign_guild", "attachments_only", "embeds_only", "empty", "forbidden"])
async def test_invalid_selected_messages_fail_without_model_calls(context, source):
    ctx = context
    message = selected(ctx)
    if source == "foreign_channel":
        message.channel = SimpleNamespace(id=201)
    elif source == "foreign_guild":
        message.guild = SimpleNamespace(id=999)
    elif source == "forbidden":
        message.content = "Assignment prompt: write our report"
    else:
        message.content = ""
    # Attachment/embed objects must never be traversed, even on mixed messages.
    ctx.client.clarify.provider.complete = AsyncMock()
    interaction = ctx.make()
    await ctx.client.clarify_ui.ask(interaction, message)
    ctx.client.clarify.provider.complete.assert_not_awaited()
    assert not metric(ctx.db, "clarify_uses") and interaction.followup.send.call_args.kwargs["ephemeral"]


async def test_pause_in_flight_prevents_answer_and_counter(context):
    ctx = context
    started, finish = asyncio.Event(), asyncio.Event()

    async def slow(**kwargs):
        started.set()
        await finish.wait()
        return await FakeProvider().complete(**kwargs)

    ctx.client.clarify.provider.complete = slow
    interaction = ctx.make()
    work = asyncio.create_task(ctx.client.clarify_ui.ask(interaction, selected(ctx)))
    await started.wait()
    ctx.db.set_paused(200, True)
    finish.set()
    await work
    assert interaction.followup.send.call_args.args[0] == PAUSED_NOTICE
    assert "embed" not in interaction.followup.send.call_args.kwargs
    assert not metric(ctx.db, "clarify_uses") and not ctx.client.clarify._feedback


@pytest.mark.parametrize("invalid", [False, True])
async def test_failed_delivery_or_invalid_output_does_not_count_use(context, invalid):
    ctx = context
    interaction = ctx.make()
    if invalid:
        ctx.client.clarify.provider.complete = AsyncMock(return_value="PRIVATE_INVALID_OUTPUT")
    else:
        interaction.followup.send = AsyncMock(side_effect=[http_error(), None])
    await ctx.client.clarify_ui.ask(interaction, selected(ctx))
    assert interaction.followup.send.call_args.args[0] == GENERIC_ERROR
    assert not metric(ctx.db, "clarify_uses") and not ctx.client.clarify._feedback


@pytest.mark.parametrize("invalid", ["other_member", "other_channel", "paused"])
async def test_feedback_checks_owner_channel_and_pause(context, invalid):
    ctx = context
    view = await ask(ctx)
    interaction = ctx.make(103 if invalid == "other_member" else 102)
    if invalid == "other_channel":
        interaction.channel_id = 201
    elif invalid == "paused":
        ctx.db.set_paused(200, True)
    await view.children[0].callback(interaction)
    assert not metric(ctx.db, "clarify_helpful")
    interaction.edit_original_response.assert_not_awaited()
    assert interaction.followup.send.call_args.kwargs["ephemeral"]


async def test_feedback_after_restart_expires_safely_and_totals_survive(context, settings):
    ctx = context
    view = await ask(ctx)
    old_button = view.children[0]
    restarted = RoundtableClient(settings, ctx.db)
    try:
        restarted.tree.sync = AsyncMock()
        with patch.object(restarted.scheduler.run, "start"):
            await restarted.setup_hook()
        interaction = ctx.make()
        interaction.client = restarted
        match = ClarifyFeedbackButton.__discord_ui_compiled_template__.fullmatch(old_button.custom_id)
        restored = await ClarifyFeedbackButton.from_custom_id(interaction, old_button.item, match)
        await restored.callback(interaction)
        assert interaction.followup.send.call_args.args[0] == ERROR_TEXT["clarify_feedback"]
        assert metric(ctx.db, "clarify_uses") == 1 and not metric(ctx.db, "clarify_helpful")
    finally:
        await restarted.close()


async def test_failed_feedback_button_edit_does_not_allow_double_count(context):
    ctx = context
    view = await ask(ctx)
    interaction = ctx.make()
    interaction.edit_original_response.side_effect = http_error()
    await view.children[0].callback(interaction)
    assert metric(ctx.db, "clarify_helpful") == 1
    repeated = ctx.make()
    await view.children[1].callback(repeated)
    assert not metric(ctx.db, "clarify_unhelpful")
    assert repeated.followup.send.call_args.args[0] == ERROR_TEXT["clarify_feedback"]


async def test_foreign_message_text_is_not_read(context):
    ctx = context

    class ForeignMessage:
        guild = ctx.guild
        channel = SimpleNamespace(id=201)

        @property
        def content(self):
            raise AssertionError("Foreign content read")

    interaction = ctx.make()
    await ctx.client.clarify_ui.ask(interaction, ForeignMessage())
    assert interaction.followup.send.call_args.args[0] == ERROR_TEXT["clarify_input"]


async def test_feedback_token_expires_after_fifteen_minutes(context):
    ctx = context
    now = [0.0]
    ctx.client.clarify.clock = lambda: now[0]
    view = await ask(ctx)
    now[0] = 900
    interaction = ctx.make()
    await view.children[0].callback(interaction)
    assert not metric(ctx.db, "clarify_helpful")
    assert interaction.followup.send.call_args.args[0] == ERROR_TEXT["clarify_feedback"]


async def test_selected_text_answer_and_feedback_receipt_never_reach_db_file_or_logs(context, caplog, tmp_path):
    ctx = context
    message_text, answer_text = "PRIVATE_SELECTED_20931", "PRIVATE_ANSWER_98234"
    raw = json.dumps({"meaning": answer_text, "terms": [], "other_readings": [], "follow_up_question": "Could you explain?"})
    ctx.client.clarify.provider.complete = AsyncMock(return_value=raw)
    interaction = ctx.make()
    await ctx.client.clarify_ui.ask(interaction, selected(ctx, message_text))
    view = interaction.followup.send.call_args.kwargs["view"]
    token = view.children[0].token
    await view.children[0].callback(ctx.make())
    dump = "\n".join(ctx.db.connection.iterdump())
    snapshot = tmp_path / "clarify-privacy.db"
    with sqlite3.connect(snapshot) as disk:
        ctx.db.connection.backup(disk)
    for text in (message_text, answer_text, token):
        assert text not in dump + caplog.text and text.encode() not in snapshot.read_bytes()
    assert metric(ctx.db, "clarify_uses") == 1 and metric(ctx.db, "clarify_helpful") == 1
    assert {row["name"] for row in ctx.db.connection.execute("PRAGMA table_info(metrics)")} == {"channel_id", "name", "count"}
    ctx.channel.send.assert_not_awaited()


def test_clarify_format_includes_terms_and_handles_maximum_escaped_output():
    result = Clarification(
        meaning="*" * 600, terms=[{"term": "*" * 60, "explanation": "*" * 180} for _ in range(5)],
        other_readings=["*" * 200] * 3, follow_up_question="*" * 300,
    )
    embed = clarification_embed(result)
    assert "Terms or idioms explained" in {field.name for field in embed.fields}
    assert len(embed) <= 6000 and all(len(field.value) <= 1024 for field in embed.fields)
