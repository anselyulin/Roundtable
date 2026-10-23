"""Persistent public views and restart-safe, member-bound private components."""

import re

import discord

from core.db import DecisionPrompt, RoundRecord
from core.clarify import FEEDBACK_TTL
from core.rounds import ANOTHER, NONE, DecisionPage
from core.tasks import PROPOSAL_LIFETIME, proposal_fingerprint
from datetime import datetime


PRIVATE_PATTERN = r"rt:round:(?P<round_id>[0-9]+):private:(?P<user_id>[0-9]+):(?P<selection>-?[0-9]+):(?P<named>[01]):(?P<action>select|toggle|reason|submit)"
MODAL_PATTERN = re.compile(PRIVATE_PATTERN + r"\Z")
BUTTON_PATTERN = PRIVATE_PATTERN.replace("select|toggle|reason|submit", "toggle|reason") + r"\Z"
SELECT_PATTERN = PRIVATE_PATTERN.replace("select|toggle|reason|submit", "select") + r"\Z"
REOPEN_PATTERN = r"rt:reopen:(?P<channel_id>[0-9]+):(?P<user_id>[0-9]+):(?P<page>[0-9]+):(?P<round_id>[0-9]+):(?P<action>page|select|vote|add|submit)"
REOPEN_MODAL_PATTERN = re.compile(REOPEN_PATTERN + r"\Z")
CLARIFY_FEEDBACK_PATTERN = r"rt:clarify:(?P<channel_id>[0-9]+):(?P<user_id>[0-9]+):(?P<token>[a-f0-9]{32}):(?P<helpful>[01])\Z"
TASK_MODAL_PATTERN = re.compile(r"rt:taskadd:(?P<channel_id>[0-9]+):(?P<user_id>[0-9]+):(?P<round_id>[0-9]+):submit\Z")
TASK_BUTTON_PATTERN = r"rt:task:(?P<channel_id>[0-9]+):(?P<batch>[a-f0-9]{16}):(?P<task_id>[0-9]+):(?P<owner_id>[0-9]+):(?P<action>confirm|swap|all)\Z"
TASK_SWAP_PATTERN = r"rt:taskswap:(?P<channel_id>[0-9]+):(?P<batch>[a-f0-9]{16}):(?P<task_id>[0-9]+):(?P<user_id>[0-9]+)\Z"
TASK_PAGE_PATTERN = r"rt:taskpage:(?P<channel_id>[0-9]+):(?P<user_id>[0-9]+):(?P<page>[0-9]+)\Z"
TASK_FLOW_PATTERN = r"rt:taskflow:(?P<channel_id>[0-9]+):(?P<round_id>[0-9]+)\Z"
TIMELINE_POST_PATTERN = r"rt:timeline:(?P<channel_id>[0-9]+):(?P<user_id>[0-9]+):(?P<token>[a-f0-9]{32})\Z"


def reopen_id(channel_id: int, user_id: int, page: int, round_id: int, action: str) -> str:
    return f"rt:reopen:{channel_id}:{user_id}:{page}:{round_id}:{action}"


def private_id(round_id: int, user_id: int, selection: int, named: bool, action: str) -> str:
    return f"rt:round:{round_id}:private:{user_id}:{selection}:{int(named)}:{action}"


class SafeView(discord.ui.View):
    async def on_error(self, interaction, error, item):
        await interaction.client.rounds_ui.fail(interaction)


class ShareView(SafeView):
    def __init__(self, controller, round_id: int):
        super().__init__(timeout=None)
        button = discord.ui.Button(label="Share my view", style=discord.ButtonStyle.primary, custom_id=f"rt:round:{round_id}:share")

        async def share(interaction):
            await controller.share(interaction, round_id)

        button.callback = share
        self.add_item(button)
        end = discord.ui.Button(label="End round early", style=discord.ButtonStyle.secondary, custom_id=f"rt:round:{round_id}:end")

        async def end_round(interaction):
            await controller.end_round(interaction, round_id)

        end.callback = end_round
        self.add_item(end)


class OutcomeView(SafeView):
    def __init__(self, controller, record: RoundRecord, choices: tuple[str, ...]):
        super().__init__(timeout=None)
        for index, choice in enumerate(choices + (NONE,)):
            chosen = record.outcome is not None and choice == record.outcome
            button = discord.ui.Button(
                label=choice[:80], style=discord.ButtonStyle.success if chosen else discord.ButtonStyle.secondary,
                custom_id=f"rt:round:{record.id}:outcome:{index}", disabled=record.outcome is not None,
            )

            async def outcome(interaction, selected=index):
                await controller.log_outcome(interaction, record.id, selected)

            button.callback = outcome
            self.add_item(button)


class PrivateButton(discord.ui.DynamicItem[discord.ui.Button], template=BUTTON_PATTERN):
    def __init__(self, round_id: int, user_id: int, selection: int, named: bool, action: str):
        self.round_id, self.user_id = round_id, user_id
        self.selection, self.named, self.action = selection, named, action
        super().__init__(discord.ui.Button(
            label=f"Show my name: {'On' if named else 'Off'}" if action == "toggle" else "Add my reason",
            style=discord.ButtonStyle.secondary,
            custom_id=private_id(round_id, user_id, selection, named, action),
            disabled=action == "reason" and selection < 0, row=1,
        ))

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(int(match["round_id"]), int(match["user_id"]), int(match["selection"]), match["named"] == "1", match["action"])

    async def callback(self, interaction):
        controller = interaction.client.rounds_ui
        try:
            if self.action == "reason":
                await controller.open_reason(interaction, self.round_id, self.user_id, self.selection, self.named)
            elif self.action == "toggle":
                await controller.change_draft(interaction, self.round_id, self.user_id, self.selection, not self.named)
        except Exception:
            await controller.fail(interaction)


class PrivateSelect(discord.ui.DynamicItem[discord.ui.Select], template=SELECT_PATTERN):
    def __init__(self, record: RoundRecord, user_id: int, selection: int, named: bool):
        self.round_id, self.user_id, self.named = record.id, user_id, named
        super().__init__(discord.ui.Select(
            custom_id=private_id(record.id, user_id, selection, named, "select"),
            placeholder="Choose an option first",
            options=[discord.SelectOption(label=label, value=str(index), default=index == selection)
                     for index, label in enumerate(record.options or (ANOTHER,))], row=0,
        ))

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        # Restoring a control needs only its ids; authorization happens in callback
        # before any selected response or reason is read.
        obj = cls.__new__(cls)
        obj.round_id, obj.user_id, obj.named = int(match["round_id"]), int(match["user_id"]), match["named"] == "1"
        discord.ui.DynamicItem.__init__(obj, item)
        return obj

    async def callback(self, interaction):
        controller = interaction.client.rounds_ui
        try:
            await controller.select_draft(interaction, self.round_id, self.user_id, self.named)
        except Exception:
            await controller.fail(interaction)


class PrivateShareView(SafeView):
    def __init__(self, record: RoundRecord, user_id: int, selection: int = -1, named: bool = False):
        super().__init__(timeout=None)
        if record.options:
            self.add_item(PrivateSelect(record, user_id, selection, named))
        else:
            selection = 0
        reason = PrivateButton(record.id, user_id, selection, named, "reason")
        if not record.options:
            reason.item.label = "Add my answer"
        self.add_item(reason)
        self.add_item(PrivateButton(record.id, user_id, selection, named, "toggle"))


class ReasonModal(discord.ui.Modal):
    def __init__(self, round_id: int, user_id: int, selection: int, named: bool, original: str = "", *, answer: str | None = None):
        super().__init__(
            title="Share my view", timeout=None,
            custom_id=private_id(round_id, user_id, selection, named, "submit"),
        )
        if answer is not None:
            self.add_item(discord.ui.TextInput(
                label="Your answer", custom_id="rt:answer", required=True,
                max_length=80, default=answer or None, style=discord.TextStyle.short,
            ))
        self.add_item(discord.ui.TextInput(
            label="Why? One line is fine", custom_id="rt:reason", required=True,
            max_length=300, default=original or None, style=discord.TextStyle.short,
        ))

    async def on_submit(self, interaction):
        # The client's on_interaction routes modal submits in both a running
        # process and a fresh process, so the same submission is handled once.
        pass

    async def on_error(self, interaction, error):
        await interaction.client.rounds_ui.fail(interaction)


class ReopenButton(discord.ui.DynamicItem[discord.ui.Button], template=REOPEN_PATTERN.replace("page|select|vote|add|submit", "page|vote|add") + r"\Z"):
    def __init__(self, channel_id: int, user_id: int, page: int, round_id: int, action: str, *, label: str, disabled: bool = False):
        self.channel_id, self.user_id, self.page, self.round_id, self.action = channel_id, user_id, page, round_id, action
        super().__init__(discord.ui.Button(
            label=label, disabled=disabled, row=1, style=discord.ButtonStyle.secondary,
            custom_id=reopen_id(channel_id, user_id, page, round_id, action),
        ))

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(int(match["channel_id"]), int(match["user_id"]), int(match["page"]), int(match["round_id"]), match["action"], label=item.label, disabled=item.disabled)

    async def callback(self, interaction):
        controller = interaction.client.rounds_ui
        try:
            if self.action == "page":
                await controller.browse_reopen(interaction, self.channel_id, self.user_id, self.page)
            elif self.action == "add":
                await controller.open_reopen_option(interaction, self.channel_id, self.user_id, self.page, self.round_id)
            else:
                await controller.perform_reopen(interaction, self.channel_id, self.user_id, self.round_id)
        except Exception:
            await controller.fail(interaction)


class ReopenSelect(discord.ui.DynamicItem[discord.ui.Select], template=REOPEN_PATTERN.replace("page|select|vote|add|submit", "select") + r"\Z"):
    def __init__(self, channel_id: int, user_id: int, history: DecisionPage):
        self.channel_id, self.user_id, self.page = channel_id, user_id, history.page
        super().__init__(discord.ui.Select(
            custom_id=reopen_id(channel_id, user_id, history.page, 0, "select"),
            placeholder="Choose a previous decision", row=0,
            options=[discord.SelectOption(
                label=f"Round {entry.id}: {entry.question}"[:100], value=str(entry.id),
                description=f"{entry.closed_at[:10]} · {entry.outcome or 'open outcome'}"[:100],
            ) for entry in history.entries],
        ))

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        obj = cls.__new__(cls)
        obj.channel_id, obj.user_id, obj.page = int(match["channel_id"]), int(match["user_id"]), int(match["page"])
        discord.ui.DynamicItem.__init__(obj, item)
        return obj

    async def callback(self, interaction):
        try:
            await interaction.client.rounds_ui.choose_reopen(interaction, self.channel_id, self.user_id, self.page)
        except Exception:
            await interaction.client.rounds_ui.fail(interaction)


class ReopenHistoryView(SafeView):
    def __init__(self, channel_id: int, user_id: int, history: DecisionPage):
        super().__init__(timeout=None)
        if history.entries:
            self.add_item(ReopenSelect(channel_id, user_id, history))
        self.add_item(ReopenButton(channel_id, user_id, max(0, history.page - 1), 0, "page", label="Previous", disabled=history.page == 0))
        self.add_item(ReopenButton(channel_id, user_id, history.page + 1, 0, "page", label="Next", disabled=history.page + 1 >= history.total_pages))


class ReopenDecisionView(SafeView):
    def __init__(self, channel_id: int, user_id: int, page: int, round_id: int):
        super().__init__(timeout=None)
        self.add_item(ReopenButton(channel_id, user_id, page, round_id, "vote", label="Vote again"))
        self.add_item(ReopenButton(channel_id, user_id, page, round_id, "add", label="Add option & reopen"))
        self.add_item(ReopenButton(channel_id, user_id, page, 0, "page", label="Back to decisions"))


class ReopenOptionModal(discord.ui.Modal):
    def __init__(self, channel_id: int, user_id: int, page: int, round_id: int):
        super().__init__(title="Add option and reopen", timeout=None, custom_id=reopen_id(channel_id, user_id, page, round_id, "submit"))
        self.add_item(discord.ui.TextInput(label="New option", custom_id="rt:new_option", required=True, max_length=80, style=discord.TextStyle.short))

    async def on_submit(self, interaction):
        # The gateway route also handles modals opened before a process restart.
        pass

    async def on_error(self, interaction, error):
        await interaction.client.rounds_ui.fail(interaction)


class ClarifyFeedbackButton(discord.ui.DynamicItem[discord.ui.Button], template=CLARIFY_FEEDBACK_PATTERN):
    def __init__(self, channel_id: int, user_id: int, token: str, helpful: bool, *, disabled: bool = False):
        self.channel_id, self.user_id, self.token, self.helpful = channel_id, user_id, token, helpful
        super().__init__(discord.ui.Button(
            label="Helpful" if helpful else "Not helpful", disabled=disabled,
            style=discord.ButtonStyle.secondary,
            custom_id=f"rt:clarify:{channel_id}:{user_id}:{token}:{int(helpful)}",
        ))

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(int(match["channel_id"]), int(match["user_id"]), match["token"], match["helpful"] == "1", disabled=item.disabled)

    async def callback(self, interaction):
        await interaction.client.clarify_ui.feedback(interaction, self.channel_id, self.user_id, self.token, self.helpful)


class ClarifyFeedbackView(SafeView):
    def __init__(self, channel_id: int, user_id: int, token: str, *, disabled: bool = False):
        # The view contains ids/nonce only, never selected text or model output.
        super().__init__(timeout=FEEDBACK_TTL)
        for helpful in (True, False):
            self.add_item(ClarifyFeedbackButton(channel_id, user_id, token, helpful, disabled=disabled))


class DetectionPromptView(SafeView):
    def __init__(self, controller, prompt: DecisionPrompt):
        super().__init__(timeout=None)
        for accepted, label in ((True, "Yes, run a round"), (False, "No thanks")):
            button = discord.ui.Button(
                label=label, style=discord.ButtonStyle.success if accepted else discord.ButtonStyle.secondary,
                custom_id=f"rt:detect:{prompt.id}:{'yes' if accepted else 'no'}", disabled=prompt.status != "pending",
            )

            async def respond(interaction, choice=accepted):
                await controller.respond(interaction, prompt.id, choice)

            button.callback = respond
            self.add_item(button)


class TaskButton(discord.ui.DynamicItem[discord.ui.Button], template=TASK_BUTTON_PATTERN):
    def __init__(self, channel_id, batch, task_id, owner_id, action, *, disabled=False):
        self.channel_id, self.batch, self.task_id, self.owner_id, self.action = channel_id, batch, task_id, str(owner_id), action
        label = "Confirm all" if action == "all" else f"{'Confirm' if action == 'confirm' else 'Swap'} #{task_id}"
        super().__init__(discord.ui.Button(
            label=label, disabled=disabled, style=discord.ButtonStyle.success if action != "swap" else discord.ButtonStyle.secondary,
            custom_id=f"rt:task:{channel_id}:{batch}:{task_id}:{owner_id}:{action}",
        ))

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(int(match["channel_id"]), match["batch"], int(match["task_id"]), match["owner_id"], match["action"], disabled=item.disabled)

    async def callback(self, interaction):
        await interaction.client.tasks_ui.proposal_action(interaction, self.channel_id, self.batch, self.task_id, self.owner_id, self.action)


class TaskProposalView(SafeView):
    def __init__(self, channel_id, batch, tasks, now):
        super().__init__(timeout=None)
        pending = False
        for task in tasks:
            active = task.status == "proposed" and task.owner_user_id is not None and datetime.fromisoformat(task.proposed_at) + PROPOSAL_LIFETIME > now
            pending = pending or active
            for action in ("confirm", "swap"):
                self.add_item(TaskButton(channel_id, batch, task.id, task.owner_user_id or "0", action, disabled=not active))
        self.add_item(TaskButton(channel_id, batch, 0, proposal_fingerprint(tasks), "all", disabled=not pending))


class TaskSwapSelect(discord.ui.DynamicItem[discord.ui.UserSelect], template=TASK_SWAP_PATTERN):
    def __init__(self, channel_id, batch, task_id, user_id):
        self.channel_id, self.batch, self.task_id, self.user_id = channel_id, batch, task_id, user_id
        super().__init__(discord.ui.UserSelect(
            placeholder="Choose a proposed owner", min_values=1, max_values=1,
            custom_id=f"rt:taskswap:{channel_id}:{batch}:{task_id}:{user_id}",
        ))

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(int(match["channel_id"]), match["batch"], int(match["task_id"]), int(match["user_id"]))

    async def callback(self, interaction):
        await interaction.client.tasks_ui.swap_selected(interaction, self.channel_id, self.batch, self.task_id, self.user_id)


class TaskSwapView(SafeView):
    def __init__(self, channel_id, batch, task_id, user_id):
        super().__init__(timeout=None)
        self.add_item(TaskSwapSelect(channel_id, batch, task_id, user_id))


class TaskPageButton(discord.ui.DynamicItem[discord.ui.Button], template=TASK_PAGE_PATTERN):
    def __init__(self, channel_id, user_id, page, *, label, disabled=False):
        self.channel_id, self.user_id, self.page = channel_id, user_id, page
        super().__init__(discord.ui.Button(label=label, disabled=disabled, custom_id=f"rt:taskpage:{channel_id}:{user_id}:{page}"))

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(int(match["channel_id"]), int(match["user_id"]), int(match["page"]), label=item.label, disabled=item.disabled)

    async def callback(self, interaction):
        await interaction.client.tasks_ui.show_board(interaction, self.page, user_id=self.user_id, channel_id=self.channel_id, edit=True)


class TaskBoardView(SafeView):
    def __init__(self, channel_id, user_id, board):
        super().__init__(timeout=None)
        self.add_item(TaskPageButton(channel_id, user_id, max(0, board.page - 1), label="Previous", disabled=board.page == 0))
        self.add_item(TaskPageButton(channel_id, user_id, board.page + 1, label="Next", disabled=board.page + 1 >= board.total_pages))


class TaskFlowButton(discord.ui.DynamicItem[discord.ui.Button], template=TASK_FLOW_PATTERN):
    def __init__(self, channel_id, round_id):
        self.channel_id, self.round_id = channel_id, round_id
        super().__init__(discord.ui.Button(label="Turn into tasks", style=discord.ButtonStyle.primary, custom_id=f"rt:taskflow:{channel_id}:{round_id}"))

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(int(match["channel_id"]), int(match["round_id"]))

    async def callback(self, interaction):
        await interaction.client.tasks_ui.open_task(interaction, self.channel_id, self.round_id)


class TaskFlowView(SafeView):
    def __init__(self, channel_id, round_id):
        super().__init__(timeout=None)
        self.add_item(TaskFlowButton(channel_id, round_id))


class TaskModal(discord.ui.Modal):
    def __init__(self, channel_id, user_id, round_id):
        super().__init__(title="Add a task for this decision", timeout=None, custom_id=f"rt:taskadd:{channel_id}:{user_id}:{round_id}:submit")
        self.add_item(discord.ui.TextInput(label="Task title (your own words)", custom_id="rt:task_title", max_length=200, required=True))
        self.add_item(discord.ui.TextInput(label="Due date: YYYY-MM-DD, UTC (optional)", custom_id="rt:task_due", max_length=10, required=False))

    async def on_submit(self, interaction):
        pass  # Routed once through main.on_interaction, including after restart.

    async def on_error(self, interaction, error):
        await interaction.client.rounds_ui.fail(interaction)


class TimelinePostButton(discord.ui.DynamicItem[discord.ui.Button], template=TIMELINE_POST_PATTERN):
    def __init__(self, channel_id, user_id, token, *, disabled=False):
        self.channel_id, self.user_id, self.token = channel_id, user_id, token
        super().__init__(discord.ui.Button(label="Post to channel", disabled=disabled, style=discord.ButtonStyle.primary, custom_id=f"rt:timeline:{channel_id}:{user_id}:{token}"))

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(int(match["channel_id"]), int(match["user_id"]), match["token"], disabled=item.disabled)

    async def callback(self, interaction):
        await interaction.client.tasks_ui.post_timeline(interaction, self.channel_id, self.user_id, self.token)


class TimelinePreviewView(SafeView):
    def __init__(self, channel_id, user_id, token, *, disabled=False):
        super().__init__(timeout=None)
        self.add_item(TimelinePostButton(channel_id, user_id, token, disabled=disabled))
