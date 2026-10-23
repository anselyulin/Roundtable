"""Section 12 rehearsal: real application handlers, synthetic Discord transport."""

import asyncio
from dataclasses import asdict
from datetime import timedelta
import json
from pathlib import Path
import re
import socket
import sqlite3
import subprocess
from textwrap import dedent
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord
from discord.components import _component_factory
import pytest

from bot.main import RoundtableClient
from bot.views import PrivateShareView, ShareView
from core import prompts
from core.db import Database
from test_commands import http_error
from test_round_discord import context, modal_data
from test_rounds import T0


ROOT = Path(__file__).resolve().parents[1]
ROLES = {"Ana": 102, "Ben": 103, "Chen": 104}


def script_table(name):
    source = (ROOT / "demo/script.md").read_text()
    section = source.split(f"<!-- demo-{name}:start -->", 1)[1].split(f"<!-- demo-{name}:end -->", 1)[0]
    return [tuple(part.strip() for part in line.strip().strip("|").split("|"))
            for line in section.splitlines() if any(line.startswith(f"| {role} |") for role in ROLES)]


def component_message(message_id, view):
    return SimpleNamespace(id=message_id, flags=discord.MessageFlags(),
                           components=[_component_factory(row) for row in view.to_components()])


@pytest.fixture
def rehearsal(context, monkeypatch):
    ctx = context
    ctx.now = [T0]
    ctx.client.detection.clock = lambda: ctx.now[0]
    ctx.client.detection.deadline_hours = ctx.service.deadline_hours = .05
    monkeypatch.setattr("bot.commands.now_utc", lambda: ctx.now[0])
    monkeypatch.setattr("core.rounds.now_utc", lambda: ctx.now[0])
    ctx.messages, ctx.public_events = {}, []

    async def send(content=None, **kwargs):
        message_id = 1000 + len(ctx.messages)
        message = SimpleNamespace(id=message_id, pin=AsyncMock())
        ctx.messages[message_id] = {"message": message, "content": content, **kwargs}
        ctx.public_events.append({"content": content, **kwargs})
        return message

    def partial(message_id):
        async def edit(**kwargs):
            ctx.messages[message_id].update(kwargs)
            ctx.public_events.append(dict(kwargs))
            return ctx.messages[message_id]["message"]
        return SimpleNamespace(edit=AsyncMock(side_effect=edit))

    ctx.channel.send.side_effect = send
    ctx.channel.get_partial_message.side_effect = partial
    original_make = ctx.make

    def make(user_id=102, data=None, message_id=None):
        interaction = original_make(user_id, data)
        if message_id is not None:
            interaction.message = ctx.messages[message_id]["message"]
        return interaction

    ctx.make = make
    return ctx


def visible_text(events):
    return "\n".join((event.get("content") or "") + json.dumps(event["embed"].to_dict() if event.get("embed") else {}) for event in events)


async def test_scripted_demo_setup_detection_private_round_result_log_tasks_timeline_and_stats(rehearsal, tmp_path, caplog):
    ctx = rehearsal
    # Start genuinely unconfigured; setup must pin before enabling the channel.
    ctx.db.connection.execute("DELETE FROM channels")
    await ctx.client.handlers.setup(ctx.make())
    state = ctx.db.get_channel(200)
    assert state.enabled and state.notice_message_id is not None
    ctx.messages[int(state.notice_message_id)]["message"].pin.assert_awaited_once()

    calls = []
    original = ctx.client.provider.complete

    async def provider(**kwargs):
        calls.append(kwargs)
        return await original(**kwargs)

    ctx.client.provider.complete = provider
    for role, text in script_table("chat"):
        await ctx.client.on_message(SimpleNamespace(
            channel=ctx.channel, guild=ctx.guild, author=ctx.guild.get_member(ROLES[role]),
            is_system=lambda: False, edited_at=None, attachments=[], embeds=[],
            content=text, created_at=ctx.now[0],
        ))
    assert len(ctx.db.published_prompt_ids()) == 1
    prompt = ctx.db.get_decision_prompt(ctx.db.published_prompt_ids()[0])
    offer = ctx.messages[int(prompt.public_message_id)]
    assert not ctx.db.connection.execute("SELECT 1 FROM rounds").fetchone()
    await offer["view"].children[0].callback(ctx.make(message_id=int(prompt.public_message_id)))
    record = ctx.db.get_round(1)
    assert record.status == "collecting" and ctx.service.progress(1) == (0, 3)
    assert record.deadline_at == (T0 + timedelta(minutes=3)).isoformat()
    starter_id = int(record.public_message_id)
    views = script_table("views")
    assert len(views) == 3

    async def share(role, option, reason):
        interaction = ctx.make(ROLES[role], message_id=starter_id)
        await ctx.messages[starter_id]["view"].children[0].callback(interaction)
        private = interaction.followup.send.call_args.kwargs
        assert private["ephemeral"]
        select = ctx.make(ROLES[role], {"values": [str(record.options.index(option))]})
        await private["view"].children[0].callback(select)
        selected = select.edit_original_response.call_args.kwargs["view"]
        assert selected.children[-1].item.label == "Show my name: Off"
        opening = ctx.make(ROLES[role])
        await selected.children[1].callback(opening)
        modal = opening.response.send_modal.call_args.args[0]
        submitted = ctx.make(ROLES[role], modal_data(reason, modal.custom_id))
        submitted.type = discord.InteractionType.modal_submit
        await ctx.client.on_interaction(submitted)
        assert submitted.followup.send.call_args.kwargs["ephemeral"]

    for index, row in enumerate(views[:2], 1):
        ctx.now[0] = T0 + timedelta(seconds=index * 10)
        await share(*row)
        assert ctx.messages[starter_id]["embed"].footer.text == f"{index} of 3 have shared a view"
        assert all(reason not in visible_text(ctx.public_events) for _, _, reason in views)
    # Exercise the optional failed-DM branch before the quiet member shares.
    ctx.members[2].send.side_effect = http_error()
    ctx.now[0] = T0 + timedelta(seconds=90)
    await ctx.scheduler.tick(now=ctx.now[0])
    assert [member.send.await_count for member in ctx.members] == [0, 0, 1]
    assert ctx.public_events[-1]["content"].startswith("Roundtable (AI): Still waiting on <@104>")
    assert all(reason not in visible_text(ctx.public_events) for _, _, reason in views)

    ctx.now[0] = T0 + timedelta(seconds=120)
    await share(*views[2])
    assert ctx.db.get_round(1).status == "closed"
    result = ctx.messages[starter_id]
    assert result["embed"].description.endswith("\n\n\U0001f7e2\U0001f7e2\u26aa  **2** \u00b7 Campus\n\U0001f7e2\u26aa\u26aa  **1** \u00b7 Local businesses\n3 of 3 shared a view")
    fields = result["embed"].fields
    assert [field.name for field in fields[:3]] == ["Summary", "Key trade-off", "Next question"]
    assert views[2][2] in fields[0].value
    said = "\n".join(field.value for field in fields[3:])
    assert all(f"> {reason}" in said for _, _, reason in views) and "\u2014" not in said  # Anonymous by default.
    assert [item.label for item in result["view"].children] == ["Campus", "Local businesses", "None of these"]
    synthesis_input = json.loads(next(call["user"] for call in calls if call["system"] == prompts.SUMMARIZE_REASONS))
    assert synthesis_input == {"reasons": [{"option_label": option, "reason": reason} for _, option, reason in views]}
    assert "recommend" not in " ".join(field.value for field in fields[:3]).lower()
    # The human chooses the less popular option. The bot never chooses an outcome.
    assert ctx.db.get_round(1).outcome is None
    await result["view"].children[1].callback(ctx.make(message_id=starter_id))
    assert ctx.db.get_round(1).outcome == "Local businesses" and ctx.db.get_round(1).outcome_at == ctx.now[0].isoformat()
    log = ctx.make()
    await ctx.ui.log(log)
    assert log.followup.send.call_args.kwargs["ephemeral"]
    assert "Local businesses" in str(log.followup.send.call_args.kwargs["embed"].to_dict())
    assert all(reason not in str(log.followup.send.call_args) for _, _, reason in views)

    # Post-scene checks: private clarify never changes the public channel.
    before = len(ctx.public_events)
    source_text = script_table("chat")[-1][1] + " PRIVATE_CLARIFY_SOURCE_72132"
    clarify = ctx.make(103)
    command = ctx.client.tree.get_command("Ask Roundtable privately", type=discord.AppCommandType.message)
    await command.callback(clarify, SimpleNamespace(guild=ctx.guild, channel=ctx.channel, content=source_text))
    assert len(ctx.public_events) == before
    answer = clarify.followup.send.call_args.kwargs
    assert answer["ephemeral"]
    await answer["view"].children[0].callback(ctx.make(103))
    assert len(ctx.public_events) == before

    for title in ("Arrange team review", "Compare interview availability"):
        await ctx.client.tasks_ui.add(ctx.make(), title)
    await ctx.client.tasks_ui.assign(ctx.make())
    proposed = ctx.public_events[-1]["view"]
    tasks = ctx.db.channel_tasks(200)
    assert all(task.status == "proposed" for task in tasks)
    await proposed.children[-1].callback(ctx.make(message_id=int(tasks[0].proposal_message_id)))
    assert all(task.status == "confirmed" for task in ctx.db.channel_tasks(200))
    listing = ctx.make()
    await ctx.client.tasks_ui.list_tasks(listing)
    assert listing.followup.send.call_args.kwargs["ephemeral"]
    assert "Ana 1, Ben 1, Chen 0" in str(listing.followup.send.call_args.kwargs["embed"].to_dict())
    await ctx.client.tasks_ui.done(ctx.make(104), tasks[0].id)
    await ctx.client.tasks_ui.deadline_add(ctx.make(), "Team checkpoint", (T0 + timedelta(days=7)).date().isoformat())
    timeline = ctx.make()
    before = len(ctx.public_events)
    await ctx.client.tasks_ui.build_timeline(timeline)
    preview = timeline.followup.send.call_args.kwargs
    assert preview["ephemeral"] and len(ctx.public_events) == before
    milestones = [field.name for field in preview["embed"].fields[:3]]
    assert len(milestones) == 3 and max(milestones)[:10] == (T0 + timedelta(days=6)).date().isoformat()
    await preview["view"].children[0].callback(ctx.make())
    assert len(ctx.public_events) == before + 1
    assert ctx.public_events[-1]["content"].startswith("Roundtable (AI):")

    stats = ctx.make()
    await ctx.client.tasks_ui.stats(stats)
    output = stats.followup.send.call_args.kwargs
    assert output["ephemeral"]
    values = {field.name: field.value for field in output["embed"].fields}
    assert values["Rounds started"] == values["Rounds closed"] == "1"
    assert values["Average share of members who replied per round"] == "100.0%"
    assert values["Median time from round start to logged outcome"] == "2.0 minutes"
    assert values["Prompt accept rate"] == values["Clarify helpful rate"] == "100.0%"
    assert values["Tasks done on time"] == "1" and values["Tasks done late"] == "0"
    assert all(value is None or type(value) in {int, float} for value in asdict(ctx.client.tasks.stats(200, ctx.now[0])).values())
    text = json.dumps(output["embed"].to_dict())
    assert all(value not in text for value in (*ROLES, "102", "103", "104", source_text, record.question, *(row[2] for row in views)))

    # Raw dialogue and selected-message text never enter SQLite or logs.
    snapshot = tmp_path / "acceptance-privacy.db"
    with sqlite3.connect(snapshot) as disk:
        ctx.db.connection.backup(disk)
    contents = snapshot.read_bytes()
    for raw in [row[1] for row in script_table("chat")] + [source_text]:
        assert raw.encode() not in contents and raw not in caplog.text
    assert ctx.client._connection._messages is None
    ctx.channel.history.assert_not_called()
    ctx.channel.fetch_message.assert_not_called()
    assert all(not event.get("content") or event["content"].startswith("Roundtable (AI):") for event in ctx.public_events)


async def test_restart_dispatches_serialized_public_and_private_controls_through_discord_store(context, settings):
    ctx, record = context, context.begin()
    old_private = PrivateShareView(record, 102)
    restored_db = Database(":memory:")
    restored_db.connection.deserialize(ctx.db.connection.serialize())
    restarted = RoundtableClient(settings, restored_db)
    restarted.get_channel = lambda channel_id: ctx.channel
    try:
        restarted.tree.sync = AsyncMock()
        with patch.object(restarted.scheduler.run, "start"):
            await restarted.setup_hook()
        store = restarted._connection._view_store

        async def dispatch(view, item, data=None):
            interaction = ctx.make(data=data or {})
            interaction.client = restarted
            interaction.message = component_message(int(record.public_message_id), view)
            store.dispatch_view(item.type.value, item.custom_id, interaction)
            pending = tuple(store._ViewStore__tasks)
            assert pending, "Stored controls must actually route through discord.py"
            await asyncio.gather(*pending)
            return interaction

        public = ShareView(ctx.ui, record.id)
        opened = await dispatch(public, public.children[0])
        assert opened.followup.send.call_args.kwargs["ephemeral"]
        selected = await dispatch(old_private, old_private.children[0], {"values": ["1"]})
        private = selected.edit_original_response.call_args.kwargs["view"]
        modal_open = await dispatch(private, private.children[1])
        modal = modal_open.response.send_modal.call_args.args[0]
        submitted = ctx.make(data=modal_data("Local contacts are available.", modal.custom_id))
        submitted.client, submitted.type = restarted, discord.InteractionType.modal_submit
        await restarted.on_interaction(submitted)
        assert restored_db.round_responses(record.id)[0].option_label == "Local businesses"
        assert restored_db.round_responses(record.id)[0].reason == "Local contacts are available."
    finally:
        await restarted.close()
        restored_db.close()


def test_suite_network_guard_prevents_live_api_connections():
    with socket.socket() as connection:
        with pytest.raises(AssertionError, match="must not open network"):
            connection.connect(("127.0.0.1", 443))


@pytest.mark.parametrize("recommendation", ["Pick Campus.", "Campus is the best option.", "We should go with Campus."])
@pytest.mark.parametrize("field", ["summary", "trade_off", "question"])
async def test_recommending_result_is_never_published_and_recovers_with_neutral_output(context, recommendation, field, caplog):
    ctx, record = context, context.begin(members=("102", "103"))
    for user_id, selection in ((102, 0), (103, 1)):
        await ctx.service.save_response(round_id=record.id, channel_id=200, user_id=user_id, selection=selection,
                                        reason="Campus access is easier." if selection == 0 else "Local contacts are available.", show_name=False, now=T0)
    original = ctx.client.provider.complete
    attempts = []

    async def recommending(**kwargs):
        if kwargs["system"] == prompts.SUMMARIZE_REASONS:
            attempts.append(True)
            return json.dumps({"summary": "The stated priority deserves consideration.", "trade_off": "The team can weigh access against local contacts.", "question": "Which stated priority matters most?", field: recommendation})
        return await original(**kwargs)

    ctx.client.provider.complete = recommending
    await ctx.ui.maybe_close(record.id)
    assert attempts == [True, True]
    assert record.id in ctx.ui.pending_results
    ctx.channel.get_partial_message.return_value.edit.assert_not_awaited()
    ctx.channel.send.assert_not_awaited()
    assert recommendation not in "\n".join(ctx.db.connection.iterdump()) + caplog.text
    ctx.client.provider.complete = original
    await ctx.scheduler.tick(now=T0)
    assert record.id not in ctx.ui.pending_results
    assert ctx.channel.get_partial_message.return_value.edit.call_args.kwargs["embed"].fields[2].name == "Next question"


@pytest.mark.parametrize("operation", ["extract", "clarify", "parse", "synthesis"])
async def test_rapid_pause_resume_discards_valid_in_flight_round_and_clarify_output(context, operation):
    ctx = context
    target = {"extract": prompts.EXTRACT_OPTIONS, "clarify": prompts.CLARIFY_MESSAGE,
              "parse": prompts.PARSE_RESPONSE, "synthesis": prompts.SUMMARIZE_REASONS}[operation]
    record = None
    if operation in {"parse", "synthesis"}:
        record = ctx.begin(members=("102",))
        await ctx.service.save_response(round_id=record.id, channel_id=200, user_id=102, selection=0, reason="Campus access is easier.", show_name=False, now=T0)
    original = ctx.client.provider.complete

    async def paused(**kwargs):
        result = await original(**kwargs)
        if kwargs["system"] == target:
            ctx.db.set_paused(200, True)
            ctx.client.clear_channel_work(200)
            ctx.db.set_paused(200, False)
        return result

    ctx.client.provider.complete = paused
    if operation == "extract":
        await ctx.ui.decide(ctx.make(), "Which market: Campus or Local businesses?")
        assert ctx.db.get_round(1) is None
    elif operation == "clarify":
        interaction = ctx.make()
        await ctx.client.clarify_ui.ask(interaction, SimpleNamespace(guild=ctx.guild, channel=ctx.channel, content="Can we compare choices?"))
        assert "embed" not in interaction.followup.send.call_args.kwargs
        assert ctx.client.tasks.stats(200, T0).clarify_uses == 0
    else:
        await ctx.ui.maybe_close(record.id)
        assert record.id in ctx.ui.pending_results
    ctx.channel.send.assert_not_awaited()
    ctx.channel.get_partial_message.return_value.edit.assert_not_awaited()


@pytest.mark.parametrize("prepared_kind", ["question", "result"])
async def test_pause_invalidates_prepared_round_data_before_it_can_be_published(context, prepared_kind):
    ctx = context
    if prepared_kind == "question":
        prepared = await ctx.service.prepare_question(200, "Which market: Campus or Local businesses?")
    else:
        record = ctx.begin(members=("102",))
        await ctx.service.save_response(round_id=record.id, channel_id=200, user_id=102, selection=0, reason="Campus access is easier.", show_name=False, now=T0)
        prepared = await ctx.service.prepare_result(record.id, T0)
    ctx.db.set_paused(200, True)
    ctx.client.clear_channel_work(200)
    ctx.db.set_paused(200, False)
    from core.rounds import RoundError
    with pytest.raises(RoundError, match="stale"):
        if prepared_kind == "question":
            ctx.service.start(200, 102, prepared, ("102",), T0)
        else:
            ctx.service.close_prepared(prepared, T0)


async def test_clarify_pause_after_model_return_before_publication_discards_answer(context):
    ctx = context
    original = ctx.client.clarify.explain

    async def paused(*args):
        result = await original(*args)
        ctx.db.set_paused(200, True)
        ctx.client.clear_channel_work(200)
        ctx.db.set_paused(200, False)
        return result

    ctx.client.clarify.explain = paused
    interaction = ctx.make()
    await ctx.client.clarify_ui.ask(interaction, SimpleNamespace(guild=ctx.guild, channel=ctx.channel, content="Can we compare choices?"))
    assert "embed" not in interaction.followup.send.call_args.kwargs
    assert ctx.client.tasks.stats(200, T0).clarify_uses == 0
    ctx.channel.send.assert_not_awaited()


@pytest.mark.parametrize("document", ["README.md", "demo/script.md"])
def test_setup_and_demo_instructions_have_valid_shell_syntax_and_local_links(document):
    text = (ROOT / document).read_text()
    for snippet in re.findall(r"```sh\n(.*?)```", text, flags=re.S):
        # Parse only: never install, launch the bot, load .env, or call a network.
        result = subprocess.run(["/bin/sh", "-n"], input=dedent(snippet), text=True, capture_output=True)
        assert result.returncode == 0, result.stderr
    for target in re.findall(r"\]\(([^\s)]+)\)", text):
        if not target.startswith(("http:", "https:", "#")):
            assert (ROOT / document).parent.joinpath(target.split("#", 1)[0]).exists()


@pytest.mark.parametrize("operation", ["extract", "clarify", "parse", "synthesis"])
async def test_pause_prevents_new_round_and_clarify_json_attempts(context, operation):
    ctx = context
    target = {"extract": prompts.EXTRACT_OPTIONS, "clarify": prompts.CLARIFY_MESSAGE,
              "parse": prompts.PARSE_RESPONSE, "synthesis": prompts.SUMMARIZE_REASONS}[operation]
    record = None
    if operation in {"parse", "synthesis"}:
        record = ctx.begin(members=("102",))
        await ctx.service.save_response(round_id=record.id, channel_id=200, user_id=102, selection=0, reason="Campus access is easier.", show_name=False, now=T0)
    original = ctx.client.provider.complete
    attempts = []

    async def paused(**kwargs):
        if kwargs["system"] == target:
            attempts.append(True)
            ctx.db.set_paused(200, True)
            ctx.client.clear_channel_work(200)
            return "INVALID_PRIVATE_OUTPUT"
        return await original(**kwargs)

    ctx.client.provider.complete = paused
    if operation == "extract":
        await ctx.ui.decide(ctx.make(), "Which market: Campus or Local businesses?")
    elif operation == "clarify":
        await ctx.client.clarify_ui.ask(ctx.make(), SimpleNamespace(guild=ctx.guild, channel=ctx.channel, content="Can we compare choices?"))
    else:
        await ctx.ui.maybe_close(record.id)
    assert attempts == [True], "A JSON retry must not call the provider after pause"
    ctx.channel.send.assert_not_awaited()
    ctx.channel.get_partial_message.return_value.edit.assert_not_awaited()
