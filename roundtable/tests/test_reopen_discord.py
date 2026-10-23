from datetime import timedelta
from unittest.mock import AsyncMock, patch

import discord
import pytest

from bot.commands import ERROR_TEXT
from bot.formatting import HELP_TEXT, PAUSED_NOTICE
from bot.main import RoundtableClient
from bot.views import ReopenButton, ReopenSelect, reopen_id
from core.db import Database
from test_commands import http_error
from test_round_discord import context, submit
from test_rounds import T0


@pytest.fixture
async def archived(context):
    ctx = context
    record = ctx.begin(members=("102",))
    await ctx.service.save_response(
        round_id=record.id, channel_id=200, user_id=102, selection=0,
        reason="OLD_PRIVATE_REASON", show_name=False, now=T0,
    )
    ctx.service.freeze_ready(record.id, T0)
    return ctx.service.outcome(record.id, 200, 102, 0, T0)


async def details(ctx, record):
    opening = ctx.make()
    await ctx.ui.reopen(opening)
    view = opening.followup.send.call_args.kwargs["view"]
    selected = ctx.make(data={"values": [str(record.id)]})
    await view.children[0].callback(selected)
    return selected.edit_original_response.call_args.kwargs["view"]


def option_payload(round_id, option="Online customers"):
    return {
        "custom_id": reopen_id(200, 102, 0, round_id, "submit"),
        "components": [{"components": [{"custom_id": "rt:new_option", "value": option}]}],
    }


async def test_reopen_command_is_guild_only_and_help_lists_it(context):
    command = context.client.tree.get_command("re-open")
    assert command is not None and command.guild_only and command.parameters == []
    assert "/re-open" in HELP_TEXT


async def test_empty_history_is_private_and_has_no_select(context):
    ctx = context
    interaction = ctx.make()
    await ctx.ui.reopen(interaction)
    interaction.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
    kwargs = interaction.followup.send.call_args.kwargs
    assert kwargs["ephemeral"] and "No closed decisions" in kwargs["embed"].description
    assert all(button.item.disabled for button in kwargs["view"].children)
    ctx.channel.send.assert_not_awaited()


async def test_history_pages_go_forward_and_back_without_showing_reasons(context, archived, caplog):
    ctx = context
    for index in range(11):
        record = ctx.begin()
        ctx.service.freeze_ready(record.id, T0 + timedelta(days=1, minutes=index))
    interaction = ctx.make()
    await ctx.ui.reopen(interaction)
    kwargs = interaction.followup.send.call_args.kwargs
    assert len(kwargs["embed"].fields) == 10 and kwargs["embed"].footer.text == "Page 1 of 2"
    view = kwargs["view"]
    assert view.children[1].item.disabled and not view.children[2].item.disabled
    assert len(view.children[0].item.options) == 10
    next_page = ctx.make()
    await view.children[2].callback(next_page)
    next_page.response.defer.assert_awaited_once_with(ephemeral=True, thinking=False)
    second = next_page.edit_original_response.call_args.kwargs
    assert len(second["embed"].fields) == 2 and second["embed"].footer.text == "Page 2 of 2"
    assert second["view"].children[2].item.disabled
    assert "OLD_PRIVATE_REASON" not in str(kwargs["embed"].to_dict()) + str(second["embed"].to_dict()) + caplog.text
    previous = ctx.make()
    await second["view"].children[1].callback(previous)
    assert previous.edit_original_response.call_args.kwargs["embed"].footer.text == "Page 1 of 2"
    ctx.channel.send.assert_not_awaited()


async def test_select_detail_and_back_leave_history_unchanged(context, archived):
    ctx = context
    view = await details(ctx, archived)
    assert [button.item.label for button in view.children] == ["Vote again", "Add option & reopen", "Back to decisions"]
    back = ctx.make()
    await view.children[2].callback(back)
    assert back.edit_original_response.call_args.kwargs["embed"].title == "Roundtable decision log"
    assert ctx.db.get_round(archived.id) == archived
    ctx.channel.send.assert_not_awaited()


async def test_vote_again_starts_fresh_round_without_model_calls(context, archived):
    ctx = context
    ctx.service.provider.complete = AsyncMock()
    view = await details(ctx, archived)
    ctx.channel.send.return_value.id = 301
    voting = ctx.make()
    await view.children[0].callback(voting)
    voting.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
    record = ctx.db.get_round(archived.id + 1)
    assert record.options == archived.options and record.reopened_from == archived.id
    assert record.public_message_id == "301" and record.outcome_at is None
    assert ctx.service.progress(record.id) == (0, 3)
    assert ctx.db.get_round(archived.id) == archived
    assert len(ctx.db.round_responses(archived.id)) == 1 and not ctx.db.round_responses(record.id)
    sent = ctx.channel.send.call_args
    assert sent.args[0].startswith("Roundtable (AI):")
    assert f"Reopened from decision {archived.id}" in sent.args[0]
    assert sent.kwargs["embed"].footer.text == "0 of 3 have shared a view"
    assert sent.kwargs["view"].is_persistent()
    assert sent.kwargs["allowed_mentions"].to_dict() == {"parse": []}
    assert voting.followup.send.call_args.kwargs["ephemeral"]
    ctx.service.provider.complete.assert_not_awaited()
    await view.children[0].callback(ctx.make())
    assert ctx.channel.send.await_count == 1 and ctx.db.get_round(record.id + 1) is None


async def test_add_option_modal_then_vote_close_and_log_preserves_original(context, archived, caplog):
    ctx = context
    view = await details(ctx, archived)
    adding = ctx.make()
    await view.children[1].callback(adding)
    adding.response.defer.assert_not_awaited()
    modal = adding.response.send_modal.call_args.args[0]
    assert modal.children[0].required and modal.children[0].max_length == 80
    assert "Online customers" not in modal.custom_id
    ctx.channel.send.return_value.id = 301
    submitted = ctx.make(data=option_payload(archived.id))
    submitted.type = discord.InteractionType.modal_submit
    await ctx.client.on_interaction(submitted)
    record = ctx.db.get_round(archived.id + 1)
    assert record.options == archived.options + ("Online customers",)
    assert ctx.db.get_round(archived.id) == archived
    assert submitted.followup.send.call_args.kwargs["ephemeral"]
    original = ctx.service.provider.complete
    ctx.service.provider.complete = AsyncMock(wraps=original)
    ctx.channel.send.return_value.id = 301
    for user_id in (102, 103, 104):
        interaction = ctx.make(user_id)
        interaction.message.id = 301
        await submit(ctx, record, user_id, reason="Our new contacts can help.", selection=2)
    assert ctx.db.get_round(record.id).status == "closed"
    assert "OLD_PRIVATE_REASON" not in str(ctx.service.provider.complete.call_args_list)
    payload = ctx.channel.get_partial_message.return_value.edit.call_args.kwargs
    assert "OLD_PRIVATE_REASON" not in str(payload["embed"].to_dict()) + caplog.text
    outcome = ctx.make(103)
    outcome.message.id = 301
    await ctx.ui.log_outcome(outcome, record.id, 2)
    saved = ctx.db.get_round(record.id)
    assert saved.outcome == "Online customers" and saved.outcome_at and saved.owner_user_id == "103"
    assert ctx.db.get_round(archived.id) == archived
    assert {entry.outcome for entry in ctx.service.log(200)} == {"Campus", "Online customers"}


@pytest.mark.parametrize("action", ["browse", "select", "add", "vote", "modal"])
async def test_other_members_cannot_use_someone_elses_reopen_controls(context, archived, action):
    class Unreadable(dict):
        def get(self, *args, **kwargs):
            raise AssertionError("Unauthorized payload read")

    ctx = context
    interaction = ctx.make(103, data=Unreadable())
    calls = {
        "browse": lambda: ctx.ui.browse_reopen(interaction, 200, 102, 0),
        "select": lambda: ctx.ui.choose_reopen(interaction, 200, 102, 0),
        "add": lambda: ctx.ui.open_reopen_option(interaction, 200, 102, 0, archived.id),
        "vote": lambda: ctx.ui.perform_reopen(interaction, 200, 102, archived.id),
        "modal": lambda: ctx.ui.perform_reopen(interaction, 200, 102, archived.id, modal=True),
    }
    await calls[action]()
    assert interaction.followup.send.call_args.args[0] == ERROR_TEXT["reopen_member"]
    interaction.response.send_modal.assert_not_awaited()
    ctx.channel.send.assert_not_awaited()


@pytest.mark.parametrize("action", ["command", "browse", "select", "add", "vote", "modal"])
async def test_pause_blocks_every_reopen_action(context, archived, action):
    class Unreadable(dict):
        def get(self, *args, **kwargs):
            raise AssertionError("Paused payload read")

    ctx = context
    ctx.db.set_paused(200, True)
    interaction = ctx.make(data=Unreadable())
    calls = {
        "command": lambda: ctx.ui.reopen(interaction),
        "browse": lambda: ctx.ui.browse_reopen(interaction, 200, 102, 0),
        "select": lambda: ctx.ui.choose_reopen(interaction, 200, 102, 0),
        "add": lambda: ctx.ui.open_reopen_option(interaction, 200, 102, 0, archived.id),
        "vote": lambda: ctx.ui.perform_reopen(interaction, 200, 102, archived.id),
        "modal": lambda: ctx.ui.perform_reopen(interaction, 200, 102, archived.id, modal=True),
    }
    await calls[action]()
    assert interaction.followup.send.call_args.args[0] == PAUSED_NOTICE
    interaction.response.send_modal.assert_not_awaited()
    interaction.edit_original_response.assert_not_awaited()
    ctx.channel.send.assert_not_awaited()
    assert ctx.db.get_round(archived.id + 1) is None


async def test_reopen_checks_channel_and_pause_again_after_membership_fetch(context, archived):
    ctx = context
    wrong_channel = ctx.make(data=option_payload(archived.id))
    await ctx.ui.perform_reopen(wrong_channel, 201, 102, archived.id, modal=True)
    assert wrong_channel.followup.send.call_args.args[0] == ERROR_TEXT["reopen_member"]

    async def pause_during_fetch(*args):
        ctx.db.set_paused(200, True)
        return ("102", "103", "104")

    ctx.ui.visible_members = pause_during_fetch
    interaction = ctx.make()
    await ctx.ui.perform_reopen(interaction, 200, 102, archived.id)
    assert interaction.followup.send.call_args.args[0] == PAUSED_NOTICE
    assert ctx.db.get_round(archived.id + 1) is None
    ctx.channel.send.assert_not_awaited()


@pytest.mark.parametrize("data", [{"values": []}, {"values": ["not-an-id"]}, {"values": ["999"]}, {"values": ["1", "2"]}])
async def test_invalid_history_selections_fail_privately(context, archived, data):
    interaction = context.make(data=data)
    await context.ui.choose_reopen(interaction, 200, 102, 0)
    assert interaction.followup.send.call_args.args[0] == ERROR_TEXT["reopen"]
    interaction.edit_original_response.assert_not_awaited()


@pytest.mark.parametrize("data", [
    {"components": []}, {"components": [{"components": [{"custom_id": "wrong", "value": "Option"}]}]},
    {"components": [{"components": [{"custom_id": "rt:new_option", "value": 42}]}]},
])
async def test_malformed_option_modals_do_not_create_rounds(context, archived, data):
    interaction = context.make(data=data)
    await context.ui.perform_reopen(interaction, 200, 102, archived.id, modal=True)
    assert context.db.get_round(archived.id + 1) is None
    assert interaction.followup.send.call_args.kwargs["ephemeral"]


async def test_failed_public_post_cancels_child_and_allows_retry(context, archived):
    ctx = context
    ctx.channel.send.side_effect = http_error()
    await ctx.ui.perform_reopen(ctx.make(), 200, 102, archived.id)
    assert ctx.db.get_round(archived.id + 1).status == "cancelled"
    assert ctx.db.get_round(archived.id) == archived
    ctx.channel.send.side_effect = None
    await ctx.ui.perform_reopen(ctx.make(), 200, 102, archived.id)
    assert ctx.db.get_round(archived.id + 2).status == "collecting"


async def test_private_browser_buttons_and_option_modal_survive_restart(context, archived, settings):
    ctx = context
    opening = ctx.make()
    await ctx.ui.reopen(opening)
    old_select = opening.followup.send.call_args.kwargs["view"].children[0]
    restored_db = Database(":memory:")
    restored_db.connection.deserialize(ctx.db.connection.serialize())
    restarted = RoundtableClient(settings, restored_db)
    restarted.get_channel = lambda channel_id: ctx.channel
    try:
        restarted.tree.sync = AsyncMock()
        with patch.object(restarted.scheduler.run, "start"):
            await restarted.setup_hook()
        chosen = ctx.make(data={"values": [str(archived.id)]})
        chosen.client = restarted
        match = ReopenSelect.__discord_ui_compiled_template__.fullmatch(old_select.custom_id)
        select = await ReopenSelect.from_custom_id(chosen, old_select.item, match)
        await select.callback(chosen)
        add_button = chosen.edit_original_response.call_args.kwargs["view"].children[1]
        match = ReopenButton.__discord_ui_compiled_template__.fullmatch(add_button.custom_id)
        restored = await ReopenButton.from_custom_id(chosen, add_button.item, match)
        adding = ctx.make()
        adding.client = restarted
        await restored.callback(adding)
        assert adding.response.send_modal.await_count == 1
        modal = ctx.make(data=option_payload(archived.id))
        modal.client, modal.type = restarted, discord.InteractionType.modal_submit
        await restarted.on_interaction(modal)
        child = restored_db.get_round(archived.id + 1)
        assert child.options == archived.options + ("Online customers",)
        assert child.reopened_from == archived.id
        assert restored_db.get_round(archived.id) == archived
    finally:
        await restarted.close()
        restored_db.close()
