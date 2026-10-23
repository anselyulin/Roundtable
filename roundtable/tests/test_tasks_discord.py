import asyncio
from datetime import timedelta
import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord
import pytest

from bot.commands import ERROR_TEXT
from bot.formatting import GENERIC_ERROR, PAUSED_NOTICE, RESUMED_NOTICE
from bot.main import RoundtableClient
from bot.views import TaskButton, TaskFlowButton, TaskPageButton, TaskSwapSelect, TASK_MODAL_PATTERN, TimelinePostButton
from core.db import Database
from core.llm import FakeProvider
from test_commands import http_error
from test_round_discord import context, submit
from test_rounds import T0


def add(ctx, title="Compare market choices", due=None):
    return ctx.client.tasks.add(200, 102, title, due, T0)


async def assign(ctx, titles=("Compare market choices", "Arrange team review")):
    for title in titles:
        add(ctx, title)
    interaction = ctx.make()
    await ctx.client.tasks_ui.assign(interaction)
    assert interaction.followup.send.call_args.args[0].startswith("Proposed owners")
    return ctx.channel.send.call_args.kwargs["view"]


async def test_registered_groups_have_exact_step_five_subcommands(context):
    ctx = context
    assert {command.name for command in ctx.client.tree.get_command("rt-tasks").commands} == {"add", "list", "assign", "prefs", "done"}
    assert {command.name for command in ctx.client.tree.get_command("rt-deadline").commands} == {"add"}
    # Serialization checks actual Discord application-command shapes and limits.
    for name in ("rt-tasks", "rt-deadline", "rt-timeline", "rt-stats"):
        payload = ctx.client.tree.get_command(name).to_dict(ctx.client.tree)
        assert payload["name"] == name
    assert [parameter.name for parameter in ctx.client.tree.get_command("rt-tasks").get_command("add").parameters] == ["title", "due"]


async def test_add_is_announced_publicly_and_preference_stays_private_without_llm(context):
    ctx = context
    ctx.client.tasks.provider.complete = AsyncMock()
    interaction = ctx.make()
    await ctx.client.tasks_ui.add(interaction, "Arrange team review", "2026-12-31")
    interaction.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
    assert ctx.db.get_task(1).status == "proposed" and ctx.db.get_task(1).owner_user_id is None
    post = ctx.channel.send.call_args
    assert post.args[0] == (
        "Roundtable (AI): New task 1: **Arrange team review** \u00b7 due 2026-12-31 UTC \u00b7 unowned. "
        "Use /rt-tasks assign for owner suggestions."
    )
    assert post.kwargs["allowed_mentions"].to_dict() == {"parse": []} and "Ana" not in post.args[0]
    interaction.delete_original_response.assert_awaited_once()
    interaction.followup.send.assert_not_awaited()
    ctx.channel.send.reset_mock()
    note = "PRIVATE_PREFERENCE_ONLY_18372"
    preference = ctx.make(103)
    await ctx.client.tasks_ui.prefs(preference, note)
    assert ctx.db.preference(200, 103) == note and ctx.db.preference(200, 102) == ""
    assert note not in str(preference.followup.send.call_args)
    ctx.client.tasks.provider.complete.assert_not_awaited()
    ctx.channel.send.assert_not_awaited()


async def test_assign_acknowledges_before_provider_then_posts_one_ai_labeled_proposal(context):
    ctx = context
    add(ctx)
    interaction = ctx.make()
    original = ctx.client.tasks.provider.complete

    async def complete(**kwargs):
        interaction.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
        response = json.loads(await original(**kwargs))
        response["assignments"][0]["reason"] = "PRIVATE_MODEL_REASON_48291"
        return json.dumps(response)

    ctx.client.tasks.provider.complete = complete
    await ctx.client.tasks_ui.assign(interaction)
    assert ctx.channel.send.await_count == 1
    sent = ctx.channel.send.call_args
    assert sent.args[0].startswith("Roundtable (AI):") and sent.kwargs["allowed_mentions"].to_dict() == {"parse": []}
    assert sent.kwargs["view"].is_persistent()
    assert [item.item.label for item in sent.kwargs["view"].children] == ["Confirm #1", "Swap #1", "Confirm all"]
    assert "Ana" in str(sent.kwargs["embed"].to_dict())
    assert "PRIVATE_MODEL_REASON_48291" not in str(sent) + "\n".join(ctx.db.connection.iterdump())
    assert ctx.db.get_task(1).status == "proposed"


async def test_confirm_one_all_and_opposing_repeated_clicks_do_not_reassign(context):
    ctx = context
    view = await assign(ctx)
    confirm = view.children[0]
    await confirm.callback(ctx.make(104))
    assert ctx.db.get_task(1).status == "confirmed" and ctx.db.get_task(2).status == "proposed"
    edit_view = ctx.channel.get_partial_message.return_value.edit.call_args.kwargs["view"]
    assert edit_view.children[0].item.disabled and edit_view.children[1].item.disabled
    await confirm.callback(ctx.make())
    assert ctx.db.get_task(1).owner_user_id == "102"
    await edit_view.children[-1].callback(ctx.make())
    assert all(task.status == "confirmed" for task in ctx.db.channel_tasks(200))
    assert all(item.item.disabled for item in ctx.channel.get_partial_message.return_value.edit.call_args.kwargs["view"].children)


async def test_swap_uses_member_bound_private_user_select_and_still_requires_confirmation(context):
    ctx = context
    view = await assign(ctx, ("Compare choices",))
    interaction = ctx.make()
    await view.children[1].callback(interaction)
    private = interaction.followup.send.call_args.kwargs
    assert private["ephemeral"]
    selection = private["view"].children[0]
    assert isinstance(selection.item, discord.ui.UserSelect)
    wrong = ctx.make(103, {"values": ["104"]})
    await selection.callback(wrong)
    assert wrong.followup.send.call_args.args[0] == ERROR_TEXT["task_private"]
    assert ctx.db.get_task(1).owner_user_id == "102"
    selected = ctx.make(data={"values": ["104"]})
    await selection.callback(selected)
    assert ctx.db.get_task(1).status == "proposed" and ctx.db.get_task(1).owner_user_id == "104"
    assert selected.edit_original_response.call_args.kwargs["view"] is None
    # An old Confirm button must not silently confirm a different owner.
    old = ctx.make()
    await view.children[0].callback(old)
    assert old.followup.send.call_args.args[0] == ERROR_TEXT["task_stale"]
    refreshed = ctx.channel.get_partial_message.return_value.edit.call_args.kwargs["view"]
    stale_all = ctx.make(103)
    await view.children[-1].callback(stale_all)
    assert stale_all.followup.send.call_args.args[0] == ERROR_TEXT["task_stale"]
    assert ctx.db.get_task(1).status == "proposed"
    await refreshed.children[0].callback(ctx.make(103))
    assert ctx.db.get_task(1).status == "confirmed"


@pytest.mark.parametrize("owner", ["999", "104"])
async def test_swap_rejects_absent_or_hidden_owners(context, owner):
    ctx = context
    view = await assign(ctx, ("Compare choices",))
    open_menu = ctx.make()
    await view.children[1].callback(open_menu)
    selection = open_menu.followup.send.call_args.kwargs["view"].children[0]
    ctx.channel.permissions_for.side_effect = lambda member: discord.Permissions(view_channel=member.id != 104)
    selected = ctx.make(data={"values": [owner]})
    await selection.callback(selected)
    assert selected.followup.send.call_args.args[0] == ERROR_TEXT["task_members"]
    assert ctx.db.get_task(1).owner_user_id == "102"


async def test_workload_list_is_private_counts_open_and_done_and_supports_paging(context):
    ctx = context
    view = await assign(ctx, tuple(f"Planning task {i}" for i in range(10)))
    await view.children[-1].callback(ctx.make())
    done = ctx.make(104)
    await ctx.client.tasks_ui.done(done, 1)
    assert done.followup.send.call_args.args[0] == "Task 1 marked done."
    listing = ctx.make()
    await ctx.client.tasks_ui.list_tasks(listing)
    payload = listing.followup.send.call_args.kwargs
    assert payload["ephemeral"] and payload["embed"].description == "Open: 9 · Done: 1"
    text = str(payload["embed"].to_dict())
    assert "Ana 3, Ben 3, Chen 3" in text and "score" not in text.lower().replace("no performance scores", "")
    assert len(payload["embed"].fields) == 9
    page = ctx.make()
    await payload["view"].children[1].callback(page)
    assert "Page 2 of 2" in page.edit_original_response.call_args.kwargs["embed"].footer.text
    assert len(page.edit_original_response.call_args.kwargs["embed"].fields) == 3
    wrong = ctx.make(103)
    await payload["view"].children[1].callback(wrong)
    assert wrong.followup.send.call_args.args[0] == ERROR_TEXT["task_private"]


async def test_any_member_done_counts_once_without_per_member_metrics(context):
    ctx = context
    view = await assign(ctx, ("Compare choices",))
    await view.children[0].callback(ctx.make())
    await asyncio.gather(ctx.client.tasks_ui.done(ctx.make(103), 1), ctx.client.tasks_ui.done(ctx.make(104), 1))
    assert ctx.db.get_task(1).done_at == T0.isoformat()
    assert dict(ctx.db.connection.execute("SELECT name,count FROM metrics")) == {"tasks_done_on_time": 1}


async def test_done_command_classifies_a_past_due_confirmed_task_as_late(context):
    ctx = context
    add(ctx, due=(T0 - timedelta(days=1)).date().isoformat())
    await ctx.client.tasks_ui.assign(ctx.make())
    view = ctx.channel.send.call_args.kwargs["view"]
    await view.children[0].callback(ctx.make())
    await ctx.client.tasks_ui.done(ctx.make(104), 1)
    stats = ctx.client.tasks.stats(200, T0)
    assert stats.tasks_done_late == 1 and stats.tasks_done_on_time == stats.tasks_overdue == 0


async def test_failed_proposal_send_reverts_unowned_and_does_not_confirm(context):
    ctx = context
    add(ctx)
    ctx.channel.send.side_effect = http_error()
    interaction = ctx.make()
    await ctx.client.tasks_ui.assign(interaction)
    assert interaction.followup.send.call_args.args[0] == GENERIC_ERROR
    task = ctx.db.get_task(1)
    assert task.owner_user_id is None and task.proposal_batch is None and task.status == "proposed"


async def test_failed_confirm_edit_retries_without_changing_state_and_pause_defers_edit(context):
    ctx = context
    view = await assign(ctx, ("Compare choices",))
    ctx.channel.get_partial_message.return_value.edit.side_effect = http_error()
    await view.children[0].callback(ctx.make())
    assert ctx.db.get_task(1).status == "confirmed" and ctx.client.tasks_ui.pending_edits
    ctx.db.set_paused(200, True)
    ctx.channel.get_partial_message.return_value.edit.reset_mock()
    await ctx.client.tasks_ui.reconcile(T0)
    ctx.channel.get_partial_message.return_value.edit.assert_not_awaited()
    ctx.db.set_paused(200, False)
    ctx.channel.get_partial_message.return_value.edit.side_effect = None
    await ctx.client.tasks_ui.reconcile(T0)
    assert not ctx.client.tasks_ui.pending_edits
    assert all(item.item.disabled for item in ctx.channel.get_partial_message.return_value.edit.call_args.kwargs["view"].children)


async def test_scheduler_expires_only_unconfirmed_proposals_at_twenty_four_hours(context):
    ctx = context
    view = await assign(ctx)
    await view.children[0].callback(ctx.make())
    await ctx.scheduler.tick(now=T0 + timedelta(hours=24))
    assert ctx.db.get_task(1).status == "confirmed" and ctx.db.get_task(1).owner_user_id == "102"
    assert ctx.db.get_task(2).status == "proposed" and ctx.db.get_task(2).owner_user_id is None
    stale = ctx.make()
    await view.children[2].callback(stale)
    assert stale.followup.send.call_args.args[0] == ERROR_TEXT["task_proposal"]


async def test_public_task_controls_bind_to_original_message_and_channel(context):
    ctx = context
    view = await assign(ctx, ("Compare choices",))
    wrong = ctx.make()
    wrong.message = SimpleNamespace(id=999)
    await view.children[0].callback(wrong)
    assert ctx.db.get_task(1).status == "proposed"
    wrong = ctx.make()
    wrong.channel_id = 201
    await view.children[0].callback(wrong)
    assert ctx.db.get_task(1).status == "proposed"


async def test_outcome_button_opens_member_written_linked_task_modal(context):
    ctx, record = context, context.begin(members=("102",))
    await submit(ctx, record)
    await ctx.ui.log_outcome(ctx.make(), record.id, 0)
    button = ctx.channel.send.call_args.kwargs["view"].children[0]
    assert isinstance(button, TaskFlowButton) and not button.item.disabled
    opening = ctx.make(103)
    await button.callback(opening)
    opening.response.defer.assert_not_awaited()
    modal = opening.response.send_modal.call_args.args[0]
    assert TASK_MODAL_PATTERN.fullmatch(modal.custom_id)
    assert modal.children[0].required and not modal.children[1].required
    submitted = ctx.make(103, {"custom_id": modal.custom_id, "components": [
        {"components": [{"custom_id": "rt:task_title", "value": "Arrange team review"}]},
        {"components": [{"custom_id": "rt:task_due", "value": ""}]},
    ]})
    submitted.type = discord.InteractionType.modal_submit
    await ctx.client.on_interaction(submitted)
    task = ctx.db.get_task(1)
    assert task.round_id == record.id and task.created_by == "103" and task.owner_user_id is None and task.status == "proposed"
    assert ctx.channel.send.call_args.args[0] == (
        "Roundtable (AI): New task 1: **Arrange team review** \u00b7 unowned. From decision: Campus. "
        "Use /rt-tasks assign for owner suggestions."
    )
    submitted.delete_original_response.assert_awaited_once()


async def test_private_task_modal_body_is_unread_when_paused_or_wrong_member(context):
    class Unreadable(dict):
        def get(self, *args):
            raise AssertionError("Private task body read")

    ctx = context
    wrong = ctx.make(data=Unreadable())
    await ctx.client.tasks_ui.submit_task(wrong, 200, 103, 1)
    ctx.db.set_paused(200, True)
    paused = ctx.make(data=Unreadable())
    await ctx.client.tasks_ui.submit_task(paused, 200, 102, 1)
    assert paused.followup.send.call_args.args[0] == PAUSED_NOTICE
    assert not ctx.db.channel_tasks(200)


async def test_pause_and_resume_during_assign_discards_output_and_blocks_retry(context):
    ctx = context
    add(ctx)
    started, finish = asyncio.Event(), asyncio.Event()

    async def slow(**kwargs):
        started.set()
        await finish.wait()
        return "PRIVATE_INVALID_RESULT"

    ctx.client.tasks.provider.complete = AsyncMock(side_effect=slow)
    interaction = ctx.make()
    job = asyncio.create_task(ctx.client.tasks_ui.assign(interaction))
    await started.wait()
    await ctx.client.handlers.pause(ctx.make())
    await ctx.client.handlers.resume(ctx.make())
    finish.set()
    await job
    assert ctx.client.tasks.provider.complete.await_count == 1
    assert ctx.db.get_task(1).owner_user_id is None
    assert [call.args[0] for call in ctx.channel.send.await_args_list] == [PAUSED_NOTICE, RESUMED_NOTICE]  # Pause/resume notices only.


@pytest.mark.parametrize("action", ["add", "prefs", "list", "assign", "done", "deadline", "timeline", "stats"])
async def test_paused_step_five_commands_do_nothing_except_private_confirmation(context, action):
    ctx = context
    ctx.db.set_paused(200, True)
    ctx.client.tasks.provider.complete = AsyncMock()
    interaction = ctx.make()
    ui = ctx.client.tasks_ui
    calls = {
        "add": lambda: ui.add(interaction, "Compare choices"), "prefs": lambda: ui.prefs(interaction, "Organizing"),
        "list": lambda: ui.list_tasks(interaction), "assign": lambda: ui.assign(interaction), "done": lambda: ui.done(interaction, 1),
        "deadline": lambda: ui.deadline_add(interaction, "Team checkpoint", "2099-01-01"),
        "timeline": lambda: ui.build_timeline(interaction), "stats": lambda: ui.stats(interaction),
    }
    await calls[action]()
    assert interaction.followup.send.call_args.args[0] == PAUSED_NOTICE
    assert interaction.followup.send.call_args.kwargs["ephemeral"]
    ctx.client.tasks.provider.complete.assert_not_awaited()
    ctx.channel.send.assert_not_awaited()
    assert not ctx.db.channel_tasks(200)


async def test_task_buttons_and_user_select_restore_after_restart(context, settings):
    ctx = context
    view = await assign(ctx, ("Compare choices",))
    menu_interaction = ctx.make()
    await view.children[1].callback(menu_interaction)
    old_select = menu_interaction.followup.send.call_args.kwargs["view"].children[0]
    restored = Database(":memory:")
    restored.connection.deserialize(ctx.db.connection.serialize())
    client = RoundtableClient(settings, restored)
    client.get_channel = lambda channel_id: ctx.channel
    try:
        client.tree.sync = AsyncMock()
        with patch.object(client.scheduler.run, "start"):
            await client.setup_hook()
        registered = set(client._connection._view_store._dynamic_items.values())
        assert {TaskButton, TaskSwapSelect, TaskFlowButton, TaskPageButton, TimelinePostButton} <= registered
        interaction = ctx.make(data={"values": ["104"]})
        interaction.client = client
        match = TaskSwapSelect.__discord_ui_compiled_template__.fullmatch(old_select.custom_id)
        restored_select = await TaskSwapSelect.from_custom_id(interaction, old_select.item, match)
        await restored_select.callback(interaction)
        assert restored.get_task(1).owner_user_id == "104"
        refreshed = ctx.channel.get_partial_message.return_value.edit.call_args.kwargs["view"].children[0]
        match = TaskButton.__discord_ui_compiled_template__.fullmatch(refreshed.custom_id)
        rebuilt = await TaskButton.from_custom_id(interaction, refreshed.item, match)
        await rebuilt.callback(interaction)
        assert restored.get_task(1).status == "confirmed"
        done = ctx.make(103)
        done.client = client
        await client.tasks_ui.done(done, 1)
        assert restored.get_task(1).status == "done" and client.tasks.stats(200, T0).tasks_done_on_time == 1
    finally:
        await client.close()
        restored.close()


async def test_linked_task_modal_opened_before_restart_can_be_submitted(context, settings):
    ctx, record = context, context.begin(members=("102",))
    await submit(ctx, record)
    await ctx.ui.log_outcome(ctx.make(), record.id, 0)
    opening = ctx.make()
    await ctx.channel.send.call_args.kwargs["view"].children[0].callback(opening)
    modal = opening.response.send_modal.call_args.args[0]
    restored = Database(":memory:")
    restored.connection.deserialize(ctx.db.connection.serialize())
    client = RoundtableClient(settings, restored)
    try:
        submitted = ctx.make(data={"custom_id": modal.custom_id, "components": [
            {"components": [{"custom_id": "rt:task_title", "value": "Compare choices"}]},
            {"components": [{"custom_id": "rt:task_due", "value": "2026-12-31"}]},
        ]})
        submitted.client, submitted.type = client, discord.InteractionType.modal_submit
        await client.on_interaction(submitted)
        assert restored.get_task(1).round_id == record.id and restored.get_task(1).title == "Compare choices"
        assert ctx.channel.send.call_args.args[0].startswith("Roundtable (AI): New task 1: **Compare choices**")
    finally:
        await client.close()
        restored.close()


async def test_step_five_inputs_and_private_model_output_do_not_leak_to_logs_or_chat_history(context, tmp_path, caplog):
    ctx = context
    sentinel = "ORDINARY_CHAT_NEVER_SAVED_541983"
    await ctx.client.on_message(SimpleNamespace(content=sentinel))
    note = "PRIVATE_PREF_FOR_MODEL_84371"
    await ctx.client.tasks_ui.prefs(ctx.make(), note)
    await assign(ctx, ("Compare choices",))
    assert note not in str(ctx.channel.send.call_args)
    assert sentinel not in "\n".join(ctx.db.connection.iterdump()) + caplog.text
    assert note not in caplog.text
    snapshot = tmp_path / "step-five-privacy.db"
    with sqlite3.connect(snapshot) as disk:
        ctx.db.connection.backup(disk)
    assert sentinel.encode() not in snapshot.read_bytes()
    ctx.channel.history.assert_not_called()
    ctx.channel.fetch_message.assert_not_called()


async def test_task_announcement_failure_falls_back_to_private_reply(context):
    ctx = context
    ctx.channel.send.side_effect = discord.Forbidden(SimpleNamespace(status=403, reason="No access"), {"message": "", "code": 0})
    interaction = ctx.make()
    await ctx.client.tasks_ui.add(interaction, "Arrange team review")
    assert ctx.db.get_task(1) is not None
    assert interaction.followup.send.call_args.args[0].startswith("Roundtable (AI): New task 1:")
    assert interaction.followup.send.call_args.kwargs["ephemeral"]
