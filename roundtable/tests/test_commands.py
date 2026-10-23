import asyncio
from types import SimpleNamespace
from unittest.mock import ANY

import discord
import pytest

from bot.commands import ChannelCommands
from bot.formatting import HELP_TEXT, PAUSED_NOTICE, PITCH, RESUMED_NOTICE, SETUP_REQUIRED, TRANSPARENCY_NOTICE
from core.db import Database


def http_error(cls=discord.Forbidden, status=403):
    return cls(SimpleNamespace(status=status, reason="Test failure"), {"message": "omitted", "code": 0})


async def test_setup_defers_before_posting_and_pins_then_enables(db, interaction):
    handlers = ChannelCommands(db)

    async def send(*args, **kwargs):
        interaction.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
        assert not db.get_channel(200).enabled
        return notice

    async def pin(**kwargs):
        assert not db.get_channel(200).enabled

    notice = interaction.channel.send.return_value
    notice.pin.side_effect = pin
    interaction.channel.send.side_effect = send
    await handlers.setup(interaction)
    interaction.channel.send.assert_awaited_once_with(TRANSPARENCY_NOTICE, allowed_mentions=ANY)
    assert interaction.channel.send.call_args.kwargs["allowed_mentions"].to_dict() == {"parse": []}
    notice.pin.assert_awaited_once()
    state = db.get_channel(200)
    assert state.enabled and not state.paused and state.notice_message_id == "300"
    assert interaction.followup.send.call_args.kwargs["ephemeral"]


async def test_setup_requires_permissions_and_owner_bypasses_manage_channels(db, interaction):
    handlers = ChannelCommands(db)
    interaction.channel.permissions_for.return_value.manage_channels = False
    await handlers.setup(interaction)
    assert db.get_channel(200) is None
    interaction.channel.send.assert_not_awaited()
    assert "Manage Channels" in interaction.followup.send.call_args.args[0]
    interaction.user.id = interaction.guild.owner_id
    await handlers.setup(interaction)
    assert db.get_channel(200).enabled


async def test_setup_is_idempotent_and_reuses_notice(db, interaction):
    handlers = ChannelCommands(db)
    await handlers.setup(interaction)
    await handlers.setup(interaction)
    assert interaction.channel.send.await_count == 1
    interaction.channel.get_partial_message.assert_called_once_with(300)
    interaction.channel.get_partial_message.return_value.edit.assert_awaited_once()
    assert interaction.channel.send.return_value.pin.await_count == 2


async def test_deleted_notice_is_replaced(db, interaction):
    handlers = ChannelCommands(db)
    await handlers.setup(interaction)
    interaction.channel.get_partial_message.return_value.edit.side_effect = http_error(discord.NotFound, 404)
    interaction.channel.send.return_value.id = 301
    await handlers.setup(interaction)
    assert interaction.channel.send.await_count == 2
    assert db.get_channel(200).notice_message_id == "301"


async def test_failed_pin_leaves_disabled_and_retry_reuses_notice(db, interaction):
    handlers = ChannelCommands(db)
    notice = interaction.channel.send.return_value
    notice.pin.side_effect = http_error()
    await handlers.setup(interaction)
    assert not db.get_channel(200).enabled
    assert db.get_channel(200).notice_message_id == "300"
    assert "Pin Messages" in interaction.followup.send.call_args.args[0]
    notice.pin.side_effect = None
    await handlers.setup(interaction)
    assert db.get_channel(200).enabled
    assert interaction.channel.send.await_count == 1


async def test_send_failure_does_not_enable_channel(db, interaction):
    interaction.channel.send.side_effect = http_error()
    await ChannelCommands(db).setup(interaction)
    assert not db.get_channel(200).enabled
    assert db.get_channel(200).notice_message_id is None


async def test_any_member_can_pause_with_exact_anonymous_public_confirmation(db, interaction):
    handlers = ChannelCommands(db)
    await handlers.setup(interaction)
    interaction.channel.permissions_for.return_value.manage_channels = False
    interaction.channel.send.reset_mock()
    interaction.followup.send.reset_mock()

    async def send(*args, **kwargs):
        assert db.get_channel(200).paused  # Pause takes effect before confirmation.

    interaction.channel.send.side_effect = send
    await handlers.pause(interaction)
    assert not db.get_channel(200).active
    interaction.channel.send.assert_awaited_once_with(PAUSED_NOTICE, allowed_mentions=ANY)
    assert interaction.channel.send.call_args.kwargs["allowed_mentions"].to_dict() == {"parse": []}
    interaction.followup.send.assert_not_awaited()
    interaction.delete_original_response.assert_awaited_once()
    assert str(interaction.user.id) not in PAUSED_NOTICE


async def test_pause_post_failure_still_pauses_and_confirms_privately(db, interaction):
    handlers = ChannelCommands(db)
    await handlers.setup(interaction)
    interaction.channel.send.side_effect = http_error()
    await handlers.pause(interaction)
    assert db.get_channel(200).paused
    assert interaction.followup.send.call_args.args[0] == PAUSED_NOTICE
    assert interaction.followup.send.call_args.kwargs["ephemeral"]


@pytest.mark.parametrize("command", ["setup", "help"])
async def test_paused_commands_only_give_one_line_confirmation(db, interaction, command):
    handlers = ChannelCommands(db)
    await handlers.setup(interaction)
    await handlers.pause(interaction)
    interaction.channel.send.reset_mock()
    interaction.channel.get_partial_message.reset_mock()
    interaction.followup.send.reset_mock()
    await getattr(handlers, command)(interaction)
    interaction.channel.send.assert_not_awaited()
    interaction.channel.get_partial_message.assert_not_called()
    assert db.get_channel(200).paused
    interaction.followup.send.assert_awaited_once_with(PAUSED_NOTICE, ephemeral=True, allowed_mentions=ANY)
    assert interaction.followup.send.call_args.kwargs["allowed_mentions"].to_dict() == {"parse": []}
    assert "\n" not in PAUSED_NOTICE


async def test_resume_is_available_to_any_member_and_is_channel_scoped(db, interaction):
    handlers = ChannelCommands(db)
    await handlers.setup(interaction)
    await handlers.pause(interaction)
    db.ensure_channel(201, 100)
    db.enable_channel(201)
    db.set_paused(201, True)
    interaction.channel.permissions_for.return_value.manage_channels = False
    interaction.channel.send.reset_mock()
    await handlers.resume(interaction)
    assert db.get_channel(200).active
    assert db.get_channel(201).paused
    assert interaction.followup.send.call_args.args[0] == RESUMED_NOTICE
    assert interaction.followup.send.call_args.kwargs["ephemeral"]
    interaction.channel.send.assert_not_awaited()


@pytest.mark.parametrize("command", ["pause", "resume"])
async def test_controls_do_not_enable_unconfigured_channel(db, interaction, command):
    await getattr(ChannelCommands(db), command)(interaction)
    assert db.get_channel(200) is None
    interaction.channel.send.assert_not_awaited()
    assert interaction.followup.send.call_args.args[0] == SETUP_REQUIRED
    assert interaction.followup.send.call_args.kwargs["ephemeral"]


async def test_help_is_ephemeral_starts_with_pitch_and_lists_only_available_commands(db, interaction):
    await ChannelCommands(db).help(interaction)
    text = interaction.followup.send.call_args.args[0]
    assert text == HELP_TEXT and text.startswith(PITCH)
    assert all(command in text for command in ["/rt-setup", "/rt-pause", "/rt-resume", "/rt-help"])
    assert "/rt-decide" in text and "/rt-log" in text
    assert all(command in text for command in ("/rt-tasks", "/rt-deadline", "/rt-timeline", "/rt-stats"))
    assert interaction.followup.send.call_args.kwargs["ephemeral"]
    interaction.channel.send.assert_not_awaited()
    assert db.get_channel(200) is None


@pytest.mark.parametrize("command", ["setup", "pause", "resume", "help"])
async def test_bot_users_are_ignored(db, interaction, command):
    interaction.user.bot = True
    await getattr(ChannelCommands(db), command)(interaction)
    interaction.response.defer.assert_not_awaited()
    interaction.followup.send.assert_not_awaited()
    interaction.channel.send.assert_not_awaited()
    assert db.get_channel(200) is None


@pytest.mark.parametrize("invalid", ["dm", "foreign_member", "foreign_channel", "hidden_channel"])
async def test_only_channel_guild_members_with_access_are_handled(db, interaction, invalid):
    if invalid == "dm":
        interaction.guild = None
    elif invalid == "foreign_member":
        interaction.user.guild = SimpleNamespace(id=999)
    elif invalid == "foreign_channel":
        interaction.channel.guild = SimpleNamespace(id=999)
    else:
        interaction.channel.permissions_for.return_value.view_channel = False
    await ChannelCommands(db).setup(interaction)
    assert db.get_channel(200) is None
    interaction.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
    interaction.channel.send.assert_not_awaited()
    assert interaction.followup.send.call_args.kwargs["ephemeral"]


async def test_pause_waits_for_setup_then_prevents_later_setup_posts(db, interaction):
    handlers = ChannelCommands(db)
    pin_started, finish_pin = asyncio.Event(), asyncio.Event()

    async def pin(**kwargs):
        pin_started.set()
        await finish_pin.wait()

    interaction.channel.send.return_value.pin.side_effect = pin
    setup = asyncio.create_task(handlers.setup(interaction))
    await pin_started.wait()
    pause = asyncio.create_task(handlers.pause(interaction))
    await asyncio.sleep(0)
    finish_pin.set()
    await asyncio.gather(setup, pause)
    assert db.get_channel(200).paused
    assert [call.args[0] for call in interaction.channel.send.await_args_list] == [TRANSPARENCY_NOTICE, PAUSED_NOTICE]
    await handlers.setup(interaction)
    assert interaction.channel.send.await_count == 2


async def test_paused_state_and_notice_survive_reconstruction(db, interaction):
    handlers = ChannelCommands(db)
    await handlers.setup(interaction)
    await handlers.pause(interaction)
    restored = Database(":memory:")
    try:
        restored.connection.deserialize(db.connection.serialize())
        fresh_handlers = ChannelCommands(restored)
        assert restored.get_channel(200).notice_message_id == "300"
        assert restored.get_channel(200).paused
        interaction.channel.send.reset_mock()
        await fresh_handlers.setup(interaction)
        interaction.channel.send.assert_not_awaited()
        await fresh_handlers.resume(interaction)
        assert restored.get_channel(200).active
    finally:
        restored.close()


def test_notice_covers_all_required_topics_and_public_labels():
    assert len(TRANSPARENCY_NOTICE) < 2000
    assert TRANSPARENCY_NOTICE.startswith("Roundtable (AI):")
    assert PAUSED_NOTICE.startswith("Roundtable (AI):")
    for phrase in ["15 messages", "10 minutes", "detect decisions", "files", "documents", "grades", "decisions", "tasks", "deadlines", "Private clarify", "only to the asker", "Reminders may mention", "/rt-pause"]:
        assert phrase in TRANSPARENCY_NOTICE
