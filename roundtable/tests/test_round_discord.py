import asyncio
from dataclasses import replace
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
import json
import sqlite3

import discord
import pytest

from bot.formatting import GENERIC_ERROR, PAUSED_NOTICE
from bot import formatting
from bot.main import RoundtableClient
from bot.views import MODAL_PATTERN, OutcomeView, PrivateButton, PrivateSelect, PrivateShareView, private_id
from core import prompts
from core.db import Database
from core.rounds import PreparedQuestion
from core.rounds import PresentedReason, RoundError, RoundResult, Synthesis
from test_rounds import T0


@pytest.fixture
async def context(db, settings, interaction, monkeypatch):
    db.ensure_channel(200, 100)
    db.enable_channel(200)
    ana = interaction.user
    members = [ana]
    for user_id, name in [(103, "Ben"), (104, "Chen")]:
        member = MagicMock(spec=discord.Member)
        member.id, member.display_name, member.bot, member.guild = user_id, name, False, interaction.guild
        member.send = AsyncMock()
        members.append(member)
    interaction.guild.members = members
    client = RoundtableClient(settings, db)
    client.get_channel = lambda channel_id: interaction.channel if channel_id == 200 else None
    monkeypatch.setattr("bot.commands.now_utc", lambda: T0)
    monkeypatch.setattr("core.rounds.now_utc", lambda: T0)

    def make(user_id=102, data=None):
        response = SimpleNamespace(defer=AsyncMock(), send_modal=AsyncMock())
        response.is_done = lambda: bool(response.defer.await_count or response.send_modal.await_count)
        return SimpleNamespace(
            **{**vars(interaction), "user": interaction.guild.get_member(user_id), "client": client,
               "response": response, "followup": SimpleNamespace(send=AsyncMock()),
               "edit_original_response": AsyncMock(), "delete_original_response": AsyncMock(),
               "data": data if data is not None else {}},
        )

    def begin(options=("Campus", "Local businesses"), members=("102", "103", "104")):
        record = client.rounds.start(200, 102, PreparedQuestion("Which market: Campus or Local businesses?", options), members, T0)
        db.set_round_message(record.id, interaction.message.id)
        return db.get_round(record.id)

    result = SimpleNamespace(
        client=client, ui=client.rounds_ui, service=client.rounds, db=db,
        channel=interaction.channel, guild=interaction.guild, members=members,
        make=make, begin=begin, scheduler=client.scheduler,
    )
    yield result
    await client.close()


def modal_data(reason, custom_id=""):
    return {"custom_id": custom_id, "components": [{"type": 1, "components": [{"type": 4, "custom_id": "rt:reason", "value": reason}]}]}


async def submit(context, record, user_id=102, reason="Campus access is easier.", selection=0, named=False):
    interaction = context.make(user_id, modal_data(reason))
    await context.ui.submit_modal(interaction, record.id, user_id, selection, named)
    return interaction


async def test_decide_uses_chosen_deadline_and_rejects_bad_ones_before_model(context):
    ctx = context
    ctx.service.provider.complete = AsyncMock(wraps=ctx.service.provider.complete)
    await ctx.ui.decide(ctx.make(), "Which market: Campus or Local businesses?", "36h")
    record = ctx.db.get_round(1)
    deadline = datetime.fromisoformat(record.deadline_at)
    assert deadline - datetime.fromisoformat(record.created_at) == timedelta(hours=36)
    assert f"<t:{int(deadline.timestamp())}:F>" in ctx.channel.send.call_args.args[0]
    calls = ctx.service.provider.complete.await_count
    posts = ctx.channel.send.await_count
    for bad in ["", "soon", "2 fortnights", "1m", "31d"]:
        interaction = ctx.make()
        await ctx.ui.decide(interaction, "Which market: Campus or Local businesses?", bad)
        assert interaction.followup.send.call_args.args[0].startswith("Use a round length like 1h")
        assert interaction.followup.send.call_args.kwargs["ephemeral"]
    assert ctx.service.provider.complete.await_count == calls
    assert ctx.channel.send.await_count == posts
    assert ctx.db.get_round(2) is None


async def test_deadline_autocomplete_offers_presets_and_valid_typed_lengths(context):
    ctx = context

    async def shown(text):
        return [(choice.name, choice.value) for choice in await ctx.ui.deadline_choices(ctx.make(), text)]

    presets = [("1 hour", "1h"), ("6 hours", "6h"), ("1 day", "1d"), ("3 days", "3d"), ("1 week", "1w")]
    assert await shown("") == presets and await shown("nonsense") == presets
    assert await shown("day") == [("1 day", "1d"), ("3 days", "3d")]
    assert await shown("5") == [("5 hours", "5h"), ("5 days", "5d"), ("5 minutes", "5m")]
    assert await shown("1") == [("1 hour", "1h"), ("1 day", "1d"), ("1 week", "1w")]
    assert await shown("5 da") == [("5 days", "5d")]
    assert await shown("36h") == [("36 hours", "36h")]
    assert await shown("3 hrs") == [("3 hours", "3h")]
    assert await shown("10 min") == [("10 minutes", "10m")]
    assert await shown("31d") == [] and await shown("5x") == []
    parameters = {parameter.name: parameter for parameter in ctx.client.tree.get_command("rt-decide").parameters}
    assert parameters["question"].required
    assert not parameters["deadline"].required and parameters["deadline"].autocomplete


async def test_chosen_deadline_drives_halfway_reminders_and_scheduled_close(context):
    ctx = context
    await ctx.ui.decide(ctx.make(), "Which market: Campus or Local businesses?", "2h")
    record = ctx.db.get_round(1)
    await submit(ctx, record)
    await ctx.scheduler.tick(now=T0 + timedelta(minutes=59))
    assert all(member.send.await_count == 0 for member in ctx.members)
    await ctx.scheduler.tick(now=T0 + timedelta(hours=1))
    assert [member.send.await_count for member in ctx.members] == [0, 1, 1]
    await ctx.scheduler.tick(now=T0 + timedelta(hours=2))
    assert ctx.db.get_round(record.id).status == "closed"
    payload = ctx.channel.get_partial_message.return_value.edit.call_args.kwargs
    assert "Proceeding with 1 of 3 views." in payload["content"]
    late = await submit(ctx, record, 103)
    assert "closed" in late.followup.send.call_args.args[0].lower()
    assert ctx.service.progress(record.id) == (1, 3)


async def test_decide_defers_and_snapshots_all_visible_nonbot_members(context):
    ctx = context
    hidden = MagicMock(spec=discord.Member)
    hidden.id, hidden.bot = 105, False
    bot = MagicMock(spec=discord.Member)
    bot.id, bot.bot = 106, True
    ctx.guild.members.extend([hidden, bot])
    ctx.channel.permissions_for.side_effect = lambda member: discord.Permissions(view_channel=member.id != 105)
    ctx.guild.chunked = False
    interaction = ctx.make()
    original = ctx.service.provider.complete

    async def extract(**kwargs):
        interaction.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
        assert kwargs["user"] == "Which market: Campus or Local businesses?"
        return await original(**kwargs)

    ctx.service.provider.complete = extract
    await ctx.ui.decide(interaction, "Which market: Campus or Local businesses?")
    record = ctx.db.get_round(1)
    assert record.options == ("Campus", "Local businesses")
    assert {member.user_id for member in ctx.db.round_members(1)} == {"102", "103", "104"}
    ctx.guild.chunk.assert_awaited_once_with(cache=True)
    sent = ctx.channel.send.call_args
    assert sent.args[0].startswith("Roundtable (AI): Starting a private round")
    assert sent.kwargs["embed"].footer.text == "0 of 3 have shared a view"
    assert sent.kwargs["embed"].fields[0].name == "Views"
    assert sent.kwargs["embed"].fields[0].value == "\u26aa" * 3
    assert sent.kwargs["view"].is_persistent()
    assert interaction.followup.send.call_args.kwargs["ephemeral"]
    assert record.public_message_id == str(ctx.channel.send.return_value.id)
    assert all(member.send.await_count == 0 for member in ctx.members[:3])


async def test_share_select_toggle_and_modal_are_private_and_ordered(context):
    ctx, record = context, context.begin()
    interaction = ctx.make()
    await ctx.ui.share(interaction, record.id)
    response = interaction.followup.send.call_args.kwargs
    assert response["ephemeral"]
    view = response["view"]
    assert view.timeout is None and view.children[1].item.disabled
    assert view.children[2].item.label == "Show my name: Off"
    assert not ctx.db.round_responses(record.id)
    ctx.channel.send.assert_not_awaited()

    select = ctx.make(data={"values": ["1"]})
    await ctx.ui.select_draft(select, record.id, 102, False)
    select.response.defer.assert_awaited_once_with(ephemeral=True, thinking=False)
    selected_view = select.edit_original_response.call_args.kwargs["view"]
    assert not selected_view.children[1].item.disabled
    assert ":1:0:reason" in selected_view.children[1].custom_id
    toggle = ctx.make()
    await selected_view.children[2].callback(toggle)
    toggled_view = toggle.edit_original_response.call_args.kwargs["view"]
    assert toggled_view.children[2].item.label == "Show my name: On"
    assert not ctx.db.round_responses(record.id)

    add = ctx.make()
    await toggled_view.children[1].callback(add)
    add.response.defer.assert_not_awaited()
    modal = add.response.send_modal.call_args.args[0]
    assert MODAL_PATTERN.fullmatch(modal.custom_id)
    assert modal.children[0].to_component_dict()["label"] == "Why? One line is fine"
    assert modal.children[0].required and modal.children[0].max_length == 300
    assert ":1:1:submit" in modal.custom_id


async def test_modal_saves_only_complete_submission_and_progress_counts(context):
    ctx, record = context, context.begin()
    interaction = await submit(ctx, record, reason="Private one-liner not yet public.")
    interaction.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
    assert interaction.followup.send.call_args.args[0] == "Saved. You can change your answer until the round closes."
    assert interaction.followup.send.call_args.kwargs["ephemeral"]
    row = ctx.db.round_responses(record.id)[0]
    assert row.reason == "Private one-liner not yet public." and not row.show_name
    edit = ctx.channel.get_partial_message.return_value.edit.call_args.kwargs
    assert set(edit) == {"embed"} and edit["embed"].footer.text == "1 of 3 have shared a view"
    assert edit["embed"].fields[0].value == "\U0001f7e2" + "\u26aa" * 2
    assert row.reason not in json.dumps(edit["embed"].to_dict())
    ctx.channel.send.assert_not_awaited()
    assert ctx.db.get_round(record.id).status == "collecting"


async def test_all_replies_close_once_with_full_result_and_outcomes(context):
    ctx, record = context, context.begin()
    partial = ctx.channel.get_partial_message.return_value

    async def check_visibility(**kwargs):
        if ctx.db.get_round(record.id).status == "collecting":
            assert set(kwargs) == {"embed"}
            assert "Campus access is easier." not in str(kwargs["embed"].to_dict())
        else:
            assert ctx.db.get_round(record.id).status == "closed"

    partial.edit.side_effect = check_visibility
    await submit(ctx, record, reason="Campus access is easier.")
    await submit(ctx, record, 103, reason="Campus contacts are available.", named=True)
    await submit(ctx, record, 104, reason="Local contacts are available.", selection=1)
    assert ctx.db.get_round(record.id).status == "closed"
    edit = ctx.channel.get_partial_message.return_value.edit.call_args.kwargs
    assert edit["content"].startswith("Roundtable (AI):")
    embed = edit["embed"]
    assert "2 lean toward Campus" in embed.description and "1 leans toward Local businesses" in embed.description
    assert [field.name.split(" · ")[0] for field in embed.fields[:3]] == ["Anonymous", "Ben", "Anonymous"]
    assert "Ana" not in json.dumps(embed.to_dict()) and "Chen" not in json.dumps(embed.to_dict())
    assert {field.name for field in embed.fields[-3:]} == {"Minority case", "Key trade-off", "Next question"}
    assert "Local contacts" in embed.fields[-3].value
    assert embed.footer.text == "The call is yours. Pick the outcome to log it."
    assert isinstance(edit["view"], OutcomeView) and edit["view"].is_persistent()
    assert [button.label for button in edit["view"].children] == ["Campus", "Local businesses", "None of these"]
    assert all(not button.disabled for button in edit["view"].children)
    previous_edits = ctx.channel.get_partial_message.return_value.edit.await_count
    await ctx.ui.maybe_close(record.id)
    assert ctx.channel.get_partial_message.return_value.edit.await_count == previous_edits
    assert all(member.send.await_count == 0 for member in ctx.members)  # Never DM submissions.


async def test_member_can_edit_own_submission_and_modal_prefills_only_their_reason(context):
    ctx, record = context, context.begin()
    await submit(ctx, record, reason="First private reason.")
    await submit(ctx, record, 103, reason="Different private reason.")
    await submit(ctx, record, reason="Changed private reason.", selection=1, named=True)
    assert len(ctx.db.round_responses(record.id)) == 2 and ctx.service.progress(record.id) == (2, 3)
    interaction = ctx.make()
    await ctx.ui.open_reason(interaction, record.id, 102, 1, True)
    modal = interaction.response.send_modal.call_args.args[0]
    assert modal.children[0].default == "Changed private reason."
    assert "Different private reason." not in str(modal.children[0].default)


async def test_no_options_share_accepts_an_explicit_answer(context):
    ctx, record = context, context.begin(options=())
    interaction = ctx.make()
    await ctx.ui.share(interaction, record.id)
    view = interaction.followup.send.call_args.kwargs["view"]
    assert len(view.children) == 2 and view.children[0].item.label == "Add my answer"
    assert not view.children[0].item.disabled
    assert "your own answer" in interaction.followup.send.call_args.args[0]
    data = modal_data("Students are nearby.")
    data["components"].insert(0, {"type": 1, "components": [{"type": 4, "custom_id": "rt:answer", "value": "Campus"}]})
    await ctx.ui.submit_modal(ctx.make(data=data), record.id, 102, 0, False)
    assert ctx.db.round_responses(record.id)[0].option_label == "Campus"


async def test_outcome_is_atomic_disables_buttons_and_log_is_private(context):
    ctx, record = context, context.begin(members=("102",))
    await submit(ctx, record)
    log = ctx.make()
    await ctx.ui.log(log)
    assert "open outcome" in log.followup.send.call_args.kwargs["embed"].fields[0].value
    assert "Campus access is easier." not in str(log.followup.send.call_args)
    outcome = ctx.make(103)
    await ctx.ui.log_outcome(outcome, record.id, 1)
    saved = ctx.db.get_round(record.id)
    assert saved.outcome == "Local businesses" and saved.owner_user_id == "103"
    assert saved.outcome_at is not None
    buttons = ctx.channel.get_partial_message.return_value.edit.call_args.kwargs["view"].children
    assert all(button.disabled for button in buttons)
    assert ctx.channel.send.call_args.args[0].startswith("Roundtable (AI): Outcome logged:")
    assert not ctx.channel.send.call_args.kwargs["view"].children[0].item.disabled
    posts = ctx.channel.send.await_count
    await ctx.ui.log_outcome(ctx.make(), record.id, 0)
    assert ctx.channel.send.await_count == posts and ctx.db.get_round(record.id).outcome == "Local businesses"
    log = ctx.make()
    await ctx.ui.log(log)
    kwargs = log.followup.send.call_args.kwargs
    assert kwargs["ephemeral"] and "Owner: Ben" in kwargs["embed"].fields[0].value
    assert "Campus access is easier." not in str(kwargs["embed"].to_dict())
    assert ctx.db.connection.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0


@pytest.mark.parametrize("action", ["decide", "share", "select", "toggle", "reason", "submit", "outcome", "log"])
async def test_pause_blocks_all_round_work_with_one_line_confirmation(context, action):
    ctx, record = context, context.begin()
    ctx.db.set_paused(200, True)
    ctx.service.provider.complete = AsyncMock()
    interaction = ctx.make(data=modal_data("PRIVATE_NOT_ACCEPTED"))
    calls = {
        "decide": lambda: ctx.ui.decide(interaction, "Which market?"),
        "share": lambda: ctx.ui.share(interaction, record.id),
        "select": lambda: ctx.ui.select_draft(interaction, record.id, 102, False),
        "toggle": lambda: ctx.ui.change_draft(interaction, record.id, 102, 0, True),
        "reason": lambda: ctx.ui.open_reason(interaction, record.id, 102, 0, False),
        "submit": lambda: ctx.ui.submit_modal(interaction, record.id, 102, 0, False),
        "outcome": lambda: ctx.ui.log_outcome(interaction, record.id, 0),
        "log": lambda: ctx.ui.log(interaction),
    }
    await calls[action]()
    assert interaction.followup.send.call_args.args[0] == PAUSED_NOTICE
    assert interaction.followup.send.call_args.kwargs["ephemeral"]
    interaction.response.send_modal.assert_not_awaited()
    ctx.channel.send.assert_not_awaited()
    ctx.channel.get_partial_message.return_value.edit.assert_not_awaited()
    ctx.service.provider.complete.assert_not_awaited()
    assert not ctx.db.round_responses(record.id)


async def test_private_body_is_not_read_for_wrong_member_or_paused_channel(context):
    class Unreadable(dict):
        def get(self, *args, **kwargs):
            raise AssertionError("Unauthorized private payload read")

    ctx, record = context, context.begin()
    wrong = ctx.make(data=Unreadable())
    await ctx.ui.submit_modal(wrong, record.id, 103, 0, False)
    ctx.db.set_paused(200, True)
    paused = ctx.make(data=Unreadable())
    await ctx.ui.submit_modal(paused, record.id, 102, 0, False)
    assert not ctx.db.round_responses(record.id)


async def test_deadline_blocks_existing_private_modal(context):
    ctx, record = context, context.begin()
    ctx.db.connection.execute("UPDATE rounds SET deadline_at = created_at WHERE id = ?", (record.id,))
    interaction = await submit(ctx, record)
    assert "deadline" in interaction.followup.send.call_args.args[0]
    assert not ctx.db.round_responses(record.id)


async def test_failed_initial_post_cancels_round_and_never_reminds(context):
    ctx = context
    ctx.channel.send.side_effect = discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "omitted")
    interaction = ctx.make()
    await ctx.ui.decide(interaction, "Which market: Campus or Local businesses?")
    assert ctx.db.get_round(1).status == "cancelled"
    assert ctx.db.active_round_ids() == ()
    assert interaction.followup.send.call_args.args[0] == GENERIC_ERROR


async def test_invalid_extraction_retry_leaves_no_round_and_no_public_post(context):
    ctx = context
    ctx.service.provider.complete = AsyncMock(return_value="PRIVATE_INVALID_JSON")
    interaction = ctx.make()
    await ctx.ui.decide(interaction, "Which market?")
    assert ctx.service.provider.complete.await_count == 2
    assert interaction.followup.send.call_args.args[0] == GENERIC_ERROR
    assert ctx.db.get_round(1) is None
    ctx.channel.send.assert_not_awaited()


async def test_pause_during_synthesis_stays_silent_and_recovers_after_resume(context):
    ctx, record = context, context.begin(members=("102",))
    await ctx.service.save_response(round_id=record.id, channel_id=200, user_id=102, selection=0, reason="Campus access is easier.", show_name=False, now=T0)
    original = ctx.service.provider.complete
    started, finish = asyncio.Event(), asyncio.Event()

    async def slow(**kwargs):
        started.set()
        await finish.wait()
        return await original(**kwargs)

    ctx.service.provider.complete = slow
    close = asyncio.create_task(ctx.ui.maybe_close(record.id))
    await started.wait()
    await ctx.client.handlers.pause(ctx.make())
    assert ctx.db.get_channel(200).paused
    finish.set()
    await close
    ctx.channel.get_partial_message.return_value.edit.assert_not_awaited()
    assert ctx.db.get_round(record.id).status == "closed" and record.id in ctx.ui.pending_results
    assert ctx.channel.send.await_count == 1  # Exact pause confirmation only.
    await ctx.client.handlers.resume(ctx.make())
    ctx.service.provider.complete = original
    await ctx.scheduler.tick(now=T0)
    assert record.id not in ctx.ui.pending_results
    assert "Round on" in ctx.channel.get_partial_message.return_value.edit.call_args.kwargs["content"]


async def test_restart_restores_public_views_private_controls_and_modal(context, settings):
    ctx, record = context, context.begin()
    old_private = PrivateShareView(record, 102, 1, True)
    restored_db = Database(":memory:")
    restored_db.connection.deserialize(ctx.db.connection.serialize())
    restarted = RoundtableClient(settings, restored_db)
    restarted.get_channel = lambda channel_id: ctx.channel
    try:
        restarted.tree.sync = AsyncMock()
        with patch.object(restarted.scheduler.run, "start"):
            await restarted.setup_hook()
        public_ids = {item.custom_id for view in restarted.persistent_views for item in view.children}
        assert f"rt:round:{record.id}:share" in public_ids
        assert len(restarted._connection._view_store._dynamic_items) == 10
        interaction = ctx.make()
        interaction.client = restarted
        button = old_private.children[2]
        match = PrivateButton.__discord_ui_compiled_template__.fullmatch(button.custom_id)
        rebuilt = await PrivateButton.from_custom_id(interaction, button.item, match)
        await rebuilt.callback(interaction)
        view = interaction.edit_original_response.call_args.kwargs["view"]
        assert view.children[2].item.label == "Show my name: Off"

        select_item = old_private.children[0]
        select_interaction = ctx.make(data={"values": ["0"]})
        select_interaction.client = restarted
        match = PrivateSelect.__discord_ui_compiled_template__.fullmatch(select_item.custom_id)
        restored_select = await PrivateSelect.from_custom_id(select_interaction, select_item.item, match)
        await restored_select.callback(select_interaction)
        assert ":0:1:reason" in select_interaction.edit_original_response.call_args.kwargs["view"].children[1].custom_id

        modal = ctx.make(data=modal_data("Local contacts are available.", private_id(record.id, 102, 1, True, "submit")))
        modal.client, modal.type = restarted, discord.InteractionType.modal_submit
        await restarted.on_interaction(modal)
        response = restored_db.round_responses(record.id)[0]
        assert response.option_label == "Local businesses" and response.show_name
        assert response.reason == "Local contacts are available."
    finally:
        await restarted.close()
        restored_db.close()


async def test_restart_recovers_closed_result_and_outcome_buttons(context, settings):
    ctx, record = context, context.begin(members=("102",))
    await ctx.service.save_response(round_id=record.id, channel_id=200, user_id=102, selection=0, reason="Campus access is easier.", show_name=False, now=T0)
    ctx.service.freeze_ready(record.id, T0)
    restored_db = Database(":memory:")
    restored_db.connection.deserialize(ctx.db.connection.serialize())
    restarted = RoundtableClient(settings, restored_db)
    restarted.get_channel = lambda channel_id: ctx.channel
    try:
        restarted.rounds_ui.register_views()
        assert record.id in restarted.rounds_ui.pending_results
        await restarted.scheduler.tick(now=T0)
        assert record.id not in restarted.rounds_ui.pending_results
        view = ctx.channel.get_partial_message.return_value.edit.call_args.kwargs["view"]
        interaction = ctx.make(103)
        interaction.client = restarted
        await view.children[0].callback(interaction)
        assert restored_db.get_round(record.id).outcome == "Campus"
        assert restored_db.get_round(record.id).owner_user_id == "103"
    finally:
        await restarted.close()
        restored_db.close()


async def test_restart_reconciles_disabled_outcome_buttons_without_new_posts(context, settings):
    ctx, record = context, context.begin(members=("102",))
    await submit(ctx, record)
    await ctx.ui.log_outcome(ctx.make(), record.id, 0)
    saved_at = ctx.db.get_round(record.id).outcome_at
    restored_db = Database(":memory:")
    restored_db.connection.deserialize(ctx.db.connection.serialize())
    restarted = RoundtableClient(settings, restored_db)
    restarted.get_channel = lambda channel_id: ctx.channel
    try:
        assert record.id in restarted.rounds_ui.pending_outcome_views
        ctx.channel.send.reset_mock()
        await restarted.scheduler.tick(now=T0 + timedelta(minutes=1))
        assert record.id not in restarted.rounds_ui.pending_outcome_views
        assert all(button.disabled for button in ctx.channel.get_partial_message.return_value.edit.call_args.kwargs["view"].children)
        assert restored_db.get_round(record.id).outcome_at == saved_at
        ctx.channel.send.assert_not_awaited()
    finally:
        await restarted.close()
        restored_db.close()


async def test_round_privacy_file_and_logs_allow_only_explicit_responses(context, tmp_path, caplog):
    ctx, record = context, context.begin()
    chat = "CHAT_HISTORY_MUST_NOT_PERSIST_009813"
    await ctx.client.on_message(SimpleNamespace(content=chat, attachments=[chat], embeds=[chat]))
    await submit(ctx, record, reason="An explicitly submitted private one-liner.")
    dump = "\n".join(ctx.db.connection.iterdump())
    assert chat not in dump
    assert "An explicitly submitted private one-liner." in dump
    ctx.db.connection.commit()
    path = tmp_path / "round-privacy.db"
    with sqlite3.connect(path) as snapshot:
        ctx.db.connection.backup(snapshot)
    assert chat.encode() not in path.read_bytes()
    assert chat not in caplog.text and "An explicitly submitted private one-liner." not in caplog.text


async def test_result_keeps_english_and_original_and_fits_five_long_reasons(context):
    ctx, record = context, context.begin(members=("102", "103", "104", "105", "106"))
    for user_id in (102, 103, 104, 105, 106):
        await ctx.service.save_response(
            round_id=record.id, channel_id=200, user_id=user_id, selection=0,
            reason="原" * 300, show_name=False, now=T0,
        )
    prepared = await ctx.service.prepare_result(record.id, T0)
    long_result = replace(
        prepared, minority_option="Campus",
        reasons=tuple(PresentedReason(item.response, "English " * 75) for item in prepared.reasons),
        synthesis=Synthesis(steelman="S" * 700, trade_off="T" * 300, question="Q" * 300),
    )
    payload = formatting.result_message(long_result, {})
    embed = payload["embed"]
    assert len(embed) <= 6000 and len(payload["content"]) <= 2000
    assert all(len(field.value) <= 1024 for field in embed.fields)
    assert "English " * 75 in str(embed.to_dict())
    assert "(" + "原" * 300 + ")" in str(embed.to_dict())
    assert "Key trade-off" in str(embed.to_dict()) + payload["content"]
    assert "Next question" in str(embed.to_dict()) + payload["content"]


async def test_startup_cancels_orphan_without_a_public_message(context):
    ctx = context
    orphan = ctx.service.start(200, 102, PreparedQuestion("Which market?", ()), ("102", "103"), T0)
    ctx.client.tree.sync = AsyncMock()
    with patch.object(ctx.scheduler.run, "start"):
        await ctx.client.setup_hook()
    assert ctx.db.get_round(orphan.id).status == "cancelled"
    assert ctx.db.active_round_ids() == ()


def test_public_text_escapes_mentions_markdown_and_links():
    value = formatting.safe("@everyone **text** [document](https://example.org/file) <@123456789012345678>")
    assert "@everyone" not in value and "<@123456789012345678>" not in value
    assert "\\*\\*text\\*\\*" in value and "\\[document\\]" in value


@pytest.mark.parametrize("replied, total, expected", [
    (0, 0, ""), (0, 2, "\u26aa\u26aa"), (2, 2, "\U0001f7e2\U0001f7e2"),
    (3, 10, "\U0001f7e2" * 3 + "\u26aa" * 7),
    (1, 40, "\u25b0" + "\u25b1" * 9), (39, 40, "\u25b0" * 9 + "\u25b1"),
    (40, 40, "\u25b0" * 10), (0, 40, "\u25b1" * 10),
])
def test_progress_dots_fill_by_count_and_switch_to_bar_for_large_teams(replied, total, expected):
    assert formatting.progress_dots(replied, total) == expected
    embed = formatting.progress_embed(replied, total)
    assert embed.fields[0].name == "Views"
    assert embed.fields[0].value == (expected or "\u2014")
    assert embed.footer.text == f"{replied} of {total} have shared a view"


async def test_editing_submission_keeps_progress_visual_counted_once(context):
    ctx, record = context, context.begin()
    await submit(ctx, record)
    await submit(ctx, record, 103)
    await submit(ctx, record, selection=1, reason="A changed private reason.")
    embed = ctx.channel.get_partial_message.return_value.edit.call_args.kwargs["embed"]
    assert embed.fields[0].value == "\U0001f7e2\U0001f7e2\u26aa"
    assert embed.footer.text == "2 of 3 have shared a view"
    payload = json.dumps(embed.to_dict())
    for private in ("Ana", "Ben", "Chen", "102", "103", "104", "Campus", "Local businesses", "A changed private reason."):
        assert private not in payload
