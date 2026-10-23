from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
import socket

import discord
import pytest

from core.config import load_settings
from core.db import Database


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """An accidental live SDK/Discord connection must fail before consuming credits."""
    def forbidden(*args, **kwargs):
        raise AssertionError("Tests must not open network connections.")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)


@pytest.fixture
def db():
    database = Database(":memory:")
    yield database
    database.close()


@pytest.fixture
def settings():
    return load_settings(environ={"DISCORD_TOKEN": "test-token", "LLM_PROVIDER": "fake"})


@pytest.fixture
def interaction():
    guild = SimpleNamespace(id=100, owner_id=101)
    member = MagicMock(spec=discord.Member)
    member.id = 102
    member.guild = guild
    member.bot = False
    member.display_name = "Ana"
    member.send = AsyncMock()
    guild.members = [member]
    guild.chunked = True
    guild.chunk = AsyncMock()
    guild.get_member = lambda user_id: next((item for item in guild.members if item.id == user_id), None)
    channel = MagicMock(spec=discord.TextChannel)
    channel.id = 200
    channel.guild = guild
    channel.permissions_for.return_value = discord.Permissions(view_channel=True, manage_channels=True)
    notice = MagicMock(spec=discord.Message)
    notice.id = 300
    notice.pin = AsyncMock()
    channel.send = AsyncMock(return_value=notice)
    partial = MagicMock(spec=discord.PartialMessage)
    partial.edit = AsyncMock(return_value=notice)
    channel.get_partial_message.return_value = partial
    result = SimpleNamespace(
        guild=guild, guild_id=guild.id,
        channel=channel, channel_id=channel.id,
        user=member,
        response=SimpleNamespace(defer=AsyncMock(), is_done=lambda: False),
        followup=SimpleNamespace(send=AsyncMock()),
        delete_original_response=AsyncMock(),
        edit_original_response=AsyncMock(),
        message=notice,
        data={},
    )
    result.response.send_modal = AsyncMock()
    result.response.is_done = lambda: bool(result.response.defer.await_count or result.response.send_modal.await_count)
    return result
