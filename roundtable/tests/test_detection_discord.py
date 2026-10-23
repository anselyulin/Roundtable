from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import asyncio
import json
import sqlite3

import pytest

from bot.commands import ERROR_TEXT
from bot.formatting import PAUSED_NOTICE
from bot.main import RoundtableClient
from core.db import Database
from core.llm import FakeProvider
from test_commands import http_error
from test_detection_core import TEXTS, metric
from test_round_discord import context
from test_rounds import T0


@pytest.fixture
def automatic(context):
    ctx = context
    ctx.now = [T0]
    ctx.client.detection.clock = lambda: ctx.now[0]
    ctx.client.detection.provider.complete = AsyncMock(wraps=FakeProvider().complete)
    return ctx


def message(ctx, text=TEXTS[0], author=102):
    return SimpleNamespace(
        channel=ctx.channel, guild=ctx.guild, author=SimpleNamespace(id=author, bot=False),
        is_system=lambda: False, edited_at=None, attachments=[], embeds=[], content=text, created_at=ctx.now[0],
    )


async def show(ctx):
    for index, text in enumerate(TEXTS):
        await ctx.client.on_message(message(ctx, text, 102 + index % 2))
    return ctx.db.get_decision_prompt(ctx.db.published_prompt_ids()[-1])


async def test_auto_prompt_requires_four_messages_and_starts_no_round_without_yes(automatic):
    ctx = automatic
    for index, text in enumerate(TEXTS[:3]):
        await ctx.client.on_message(message(ctx, text, 102 + index % 2))
    ctx.client.detection.provider.complete.assert_not_awaited()
    await ctx.client.on_message(message(ctx, TEXTS[3], 103))
    ctx.client.detection.provider.complete.assert_awaited_once()
    sent = ctx.channel.send.call_args
    assert sent.args[0].startswith('Roundtable (AI): It sounds like you\'re deciding "Which market should we choose?"')
    assert "Campus" in sent.args[0] and "Local businesses" in sent.args[0]
    assert [button.label for button in sent.kwargs["view"].children] == ["Yes, run a round", "No thanks"]
    assert sent.kwargs["view"].is_persistent() and sent.kwargs["allowed_mentions"].to_dict() == {"parse": []}
    assert not ctx.db.connection.execute("SELECT 1 FROM rounds").fetchone()
    assert metric(ctx, "prompts_shown") == 1 and not metric(ctx, "prompts_accepted")
    ctx.channel.history.assert_not_called()
    ctx.channel.fetch_message.assert_not_called()


@pytest.mark.parametrize("condition", [
    "bot", "dm", "system", "attachments", "embeds", "edited", "paused", "disabled", "unknown", "detection_disabled", "foreign_guild", "old",
])
async def test_ineligible_messages_are_not_read_or_buffered(automatic, condition):
    ctx = automatic
    values = vars(message(ctx)).copy()
    reads = []
    if condition == "bot":
        values["author"].bot = True
    elif condition == "dm":
        values["guild"] = None
    elif condition == "system":
        values["is_system"] = lambda: True
    elif condition in {"attachments", "embeds"}:
        values[condition] = [object()]
    elif condition == "edited":
        values["edited_at"] = T0
    elif condition == "paused":
        ctx.db.set_paused(200, True)
    elif condition == "disabled":
        ctx.db.connection.execute("UPDATE channels SET enabled=0")
    elif condition == "unknown":
        ctx.db.connection.execute("DELETE FROM channels")
    elif condition == "detection_disabled":
        ctx.db.connection.execute("UPDATE channels SET detection_enabled=0")
    elif condition == "foreign_guild":
        values["guild"] = SimpleNamespace(id=999)
    else:
        values["created_at"] = T0 - timedelta(minutes=10)
    values.pop("content")

    class ProtectedMessage(SimpleNamespace):
        @property
        def content(self):
            reads.append(True)
            raise AssertionError("Ineligible content read")

    await ctx.client.on_message(ProtectedMessage(**values))
    assert not reads and not ctx.client.detection.windows
    ctx.client.detection.provider.complete.assert_not_awaited()
    ctx.channel.send.assert_not_awaited()


async def test_command_only_does_not_inspect_any_message_field(context, settings):
    class Unreadable:
        def __getattribute__(self, name):
            raise AssertionError("No gateway field may be read")

    client = RoundtableClient(replace(settings, detection_mode="command_only"), context.db)
    try:
        client.detection.provider.complete = AsyncMock()
        await client.on_message(Unreadable())
        assert not client.detection.windows
        client.detection.provider.complete.assert_not_awaited()
        # /rt-decide remains available in command-only mode.
        interaction = context.make()
        client.rounds.provider = FakeProvider()
        await client.rounds_ui.decide(interaction, "Which market: Campus or Local businesses?")
        assert context.db.get_round(1).status == "collecting"
    finally:
        await client.close()


async def test_yes_uses_detected_options_and_current_members_without_extraction(automatic):
    ctx = automatic
    prompt = await show(ctx)
    old_view = ctx.channel.send.call_args.kwargs["view"]
    ctx.channel.send.return_value = SimpleNamespace(id=301)
    interaction = ctx.make(103)
    await old_view.children[0].callback(interaction)
    interaction.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
    record = ctx.db.get_round(1)
    assert record.question == prompt.question and record.options == prompt.options
    assert record.created_by == "103" and record.deadline_at == (T0 + timedelta(days=1)).isoformat()
    assert ctx.service.progress(record.id) == (0, 3) and record.public_message_id == "301"
    assert not ctx.db.round_responses(record.id)
    assert metric(ctx, "prompts_shown") == metric(ctx, "prompts_accepted") == 1
    assert not metric(ctx, "prompts_dismissed")
    assert ctx.client.detection.provider.complete.await_count == 1
    assert ctx.db.get_decision_prompt(prompt.id).round_id == record.id
    assert interaction.followup.send.call_args.kwargs["ephemeral"]
    assert all(button.disabled for button in ctx.channel.get_partial_message.return_value.edit.call_args.kwargs["view"].children)


async def test_no_disables_prompt_and_extends_cooldown_to_sixty_minutes(automatic):
    ctx = automatic
    prompt = await show(ctx)
    view = ctx.channel.send.call_args.kwargs["view"]
    await view.children[1].callback(ctx.make())
    edit = ctx.channel.get_partial_message.return_value.edit.call_args.kwargs
    assert edit["content"] == "Roundtable (AI): Okay, I'll stay quiet."
    assert all(button.disabled for button in edit["view"].children)
    assert metric(ctx, "prompts_dismissed") == 1 and not metric(ctx, "prompts_accepted")
    assert ctx.db.get_channel(200).detection_cooldown_until == (T0 + timedelta(minutes=60)).isoformat()
    assert not ctx.db.connection.execute("SELECT 1 FROM rounds").fetchone()
    ctx.now[0] = T0 + timedelta(minutes=59)
    for index, text in enumerate(TEXTS):
        await ctx.client.on_message(message(ctx, text, 102 + index % 2))
    assert ctx.channel.send.await_count == 1 and ctx.client.detection.provider.complete.await_count == 1
    ctx.now[0] += timedelta(minutes=1)
    ctx.channel.send.return_value = SimpleNamespace(id=301)
    for index, text in enumerate(TEXTS):
        await ctx.client.on_message(message(ctx, text, 102 + index % 2))
    assert metric(ctx, "prompts_shown") == 2


async def test_repeated_or_opposing_clicks_are_consumed_once(automatic):
    ctx = automatic
    prompt = await show(ctx)
    ctx.channel.send.return_value = SimpleNamespace(id=301)
    await asyncio.gather(
        ctx.client.detection_ui.respond(ctx.make(), prompt.id, True),
        ctx.client.detection_ui.respond(ctx.make(), prompt.id, False),
    )
    assert metric(ctx, "prompts_accepted") + metric(ctx, "prompts_dismissed") == 1
    assert ctx.db.connection.execute("SELECT count(*) FROM rounds").fetchone()[0] <= 1
    stale = ctx.make()
    await ctx.client.detection_ui.respond(stale, prompt.id, True)
    assert stale.followup.send.call_args.args[0] == ERROR_TEXT["detection_prompt"]


@pytest.mark.parametrize("invalid", ["low_confidence", "not_decision", "invalid_json", "error"])
async def test_automatic_failures_and_low_confidence_post_nothing(automatic, invalid, caplog):
    ctx = automatic
    result = {"is_decision": invalid != "not_decision", "confidence": .74 if invalid == "low_confidence" else .95,
              "question": "Which market?", "options": ["Campus", "Local businesses"]}
    if invalid == "error":
        ctx.client.detection.provider.complete.side_effect = RuntimeError("PRIVATE_PROVIDER_ERROR")
    else:
        ctx.client.detection.provider.complete.side_effect = None
        ctx.client.detection.provider.complete.return_value = "PRIVATE_INVALID_JSON" if invalid == "invalid_json" else json.dumps(result)
    for index, text in enumerate(TEXTS):
        await ctx.client.on_message(message(ctx, text, 102 + index % 2))
    ctx.channel.send.assert_not_awaited()
    assert not ctx.db.published_prompt_ids() and not metric(ctx, "prompts_shown")
    assert "PRIVATE_PROVIDER_ERROR" not in caplog.text and "PRIVATE_INVALID_JSON" not in caplog.text


async def test_pause_clears_window_and_discards_in_flight_check_even_after_resume(automatic):
    ctx = automatic
    started, finish = asyncio.Event(), asyncio.Event()

    async def slow(**kwargs):
        started.set()
        await finish.wait()
        return await FakeProvider().complete(**kwargs)

    ctx.client.detection.provider.complete = slow
    for index, text in enumerate(TEXTS[:3]):
        await ctx.client.on_message(message(ctx, text, 102 + index % 2))
    running = asyncio.create_task(ctx.client.on_message(message(ctx, TEXTS[3], 103)))
    await started.wait()
    await ctx.client.handlers.pause(ctx.make())
    assert not ctx.client.detection.windows
    await ctx.client.handlers.resume(ctx.make())
    finish.set()
    await running
    assert ctx.channel.send.await_count == 1  # Pause confirmation only.
    assert not metric(ctx, "prompts_shown") and not ctx.db.published_prompt_ids()


@pytest.mark.parametrize("invalid", ["paused", "wrong_message", "foreign_channel", "bot", "hidden"])
async def test_prompt_actions_check_pause_member_access_and_message_binding(automatic, invalid):
    ctx = automatic
    prompt = await show(ctx)
    interaction = ctx.make()
    if invalid == "paused":
        ctx.db.set_paused(200, True)
    elif invalid == "wrong_message":
        interaction.message.id = 999
    elif invalid == "foreign_channel":
        ctx.db.ensure_channel(201, 100)
        ctx.db.enable_channel(201)
        interaction.channel.id = 201
    elif invalid == "bot":
        interaction.user.bot = True
    else:
        ctx.channel.permissions_for.return_value.view_channel = False
    await ctx.client.detection_ui.respond(interaction, prompt.id, False)
    assert ctx.db.get_decision_prompt(prompt.id).status == "pending"
    assert not metric(ctx, "prompts_dismissed")


async def test_pause_during_yes_membership_fetch_does_not_consume_prompt(automatic):
    ctx = automatic
    prompt = await show(ctx)

    async def pause(*args):
        ctx.db.set_paused(200, True)
        ctx.client.detection_ui.clear_channel(200)
        return ("102", "103", "104")

    ctx.ui.visible_members = pause
    interaction = ctx.make()
    await ctx.client.detection_ui.respond(interaction, prompt.id, True)
    assert interaction.followup.send.call_args.args[0] == PAUSED_NOTICE
    assert ctx.db.get_decision_prompt(prompt.id).status == "pending" and not metric(ctx, "prompts_accepted")
    assert not ctx.db.connection.execute("SELECT 1 FROM rounds").fetchone()


async def test_failed_prompt_post_does_not_increment_shown(automatic):
    ctx = automatic
    ctx.channel.send.side_effect = http_error()
    for index, text in enumerate(TEXTS):
        await ctx.client.on_message(message(ctx, text, 102 + index % 2))
    assert not metric(ctx, "prompts_shown")
    assert not ctx.db.has_pending_prompt(200)
    assert ctx.db.get_decision_prompt(1).status == "cancelled"


async def test_failed_round_post_consumes_acceptance_once_and_cancels_round(automatic):
    ctx = automatic
    prompt = await show(ctx)
    ctx.channel.send.side_effect = http_error()
    await ctx.client.detection_ui.respond(ctx.make(), prompt.id, True)
    assert ctx.db.get_round(1).status == "cancelled"
    assert metric(ctx, "prompts_accepted") == 1
    assert ctx.db.get_decision_prompt(prompt.id).status == "accepted"
    assert "could not be posted" in ctx.channel.get_partial_message.return_value.edit.call_args.kwargs["content"]


async def test_failed_prompt_edit_reconciles_without_recounting_and_respects_pause(automatic):
    ctx = automatic
    prompt = await show(ctx)
    ctx.channel.get_partial_message.return_value.edit.side_effect = http_error()
    await ctx.client.detection_ui.respond(ctx.make(), prompt.id, False)
    assert prompt.id in ctx.client.detection_ui.pending_edits and metric(ctx, "prompts_dismissed") == 1
    ctx.db.set_paused(200, True)
    ctx.channel.get_partial_message.return_value.edit.reset_mock()
    await ctx.client.detection_ui.reconcile()
    ctx.channel.get_partial_message.return_value.edit.assert_not_awaited()
    ctx.db.set_paused(200, False)
    ctx.channel.get_partial_message.return_value.edit.side_effect = None
    await ctx.client.detection_ui.reconcile()
    assert prompt.id not in ctx.client.detection_ui.pending_edits and metric(ctx, "prompts_dismissed") == 1


@pytest.mark.parametrize("accepted", [True, False])
async def test_pending_yes_no_controls_survive_restart(automatic, settings, accepted):
    ctx = automatic
    prompt = await show(ctx)
    restored_db = Database(":memory:")
    restored_db.connection.deserialize(ctx.db.connection.serialize())
    restarted = RoundtableClient(settings, restored_db)
    restarted.detection.clock = lambda: T0
    restarted.get_channel = lambda channel_id: ctx.channel
    try:
        restarted.tree.sync = AsyncMock()
        with patch.object(restarted.scheduler.run, "start"):
            await restarted.setup_hook()
        assert not restarted.detection.windows
        buttons = [button for view in restarted.persistent_views for button in view.children]
        target = next(button for button in buttons if button.custom_id == f"rt:detect:{prompt.id}:{'yes' if accepted else 'no'}")
        ctx.channel.send.return_value = SimpleNamespace(id=301)
        await target.callback(ctx.make())
        assert restored_db.get_decision_prompt(prompt.id).status == ("accepted" if accepted else "dismissed")
        assert restored_db.get_channel(200).detection_cooldown_until
    finally:
        await restarted.close()
        restored_db.close()


async def test_timer_expires_idle_window_without_new_chat(automatic):
    ctx = automatic
    await ctx.client.on_message(message(ctx))
    assert ctx.client.detection_ui._expiry is not None and ctx.client.detection.windows
    ctx.now[0] += timedelta(minutes=10)
    ctx.client.detection_ui.expire_windows()
    assert not ctx.client.detection.windows and ctx.client.detection_ui._expiry is None


async def test_raw_chat_and_author_ids_never_reach_sqlite_files_or_logs(automatic, tmp_path, caplog):
    ctx = automatic
    sentinel = "RAW_CHAT_NEVER_PERSIST_938742"
    for index, text in enumerate(TEXTS):
        await ctx.client.on_message(message(ctx, f"{text} {sentinel}", 987654321012345670 + index % 2))
    assert metric(ctx, "prompts_shown") == 1
    dump = "\n".join(ctx.db.connection.iterdump())
    snapshot = tmp_path / "detection-privacy.db"
    with sqlite3.connect(snapshot) as disk:
        ctx.db.connection.backup(disk)
    assert sentinel not in dump + caplog.text and sentinel.encode() not in snapshot.read_bytes()
    assert "987654321012345670" not in dump and "987654321012345671" not in dump
    assert not ctx.client.detection.windows
    assert ctx.client._connection._messages is None
