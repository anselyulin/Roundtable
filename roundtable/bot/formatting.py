"""Discord-facing static wording. Public bot messages always carry the AI label."""

import re
from datetime import datetime

import discord

from core.db import DecisionEntry, DecisionPrompt, RoundRecord
from core.clarify import Clarification
from core.rounds import RoundError, RoundResult
from core.tasks import PilotStats, TaskBoard
from core.timeline import TimelinePreview

PITCH = "Every team has someone who quietly turns five opinions into one plan. Roundtable does that job neutrally, so nobody has to, and everybody's view counts."

PAUSED_NOTICE = "Roundtable (AI): Paused by a member. I'm not reading or posting until someone runs /rt-resume."
RESUMED_NOTICE = "Roundtable (AI): Resumed in this channel."
SETUP_REQUIRED = "Run /rt-setup in this channel first."
GENERIC_ERROR = "I couldn't do that, please try again"

TRANSPARENCY_NOTICE = (
    "Roundtable (AI): Transparency notice\n"
    "When automatic detection is enabled, I read only recent text messages "
    "in this channel, in a short in-memory window (at most 15 messages, no older than "
    "10 minutes), only to detect decisions. I ask before starting a round. "
    "Command-only mode reads no ordinary chat.\n"
    "I never read files, documents, assignment prompts, submissions, or grades. "
    "I ignore attachments and embeds entirely, and never write your team's deliverable.\n"
    "I store decisions, members' own round responses, tasks, deadlines, settings, "
    "and aggregate metric counters. I never store chat history or log message text. "
    "Round participation and task ownership require Discord identifiers; metric "
    "counters contain no member identifiers or content.\n"
    "Private clarify uses only the message you select; its answers are visible only "
    "to the asker, and using it is not announced to anyone else. Its text and answers "
    "are never stored; only aggregate use/feedback counters are saved. Reminders may mention "
    "members who have not yet replied to a round.\n"
    "Task preference notes stay private and are used only for owner suggestions. "
    "Timelines are proposals; /rt-stats shows aggregate channel numbers only.\n"
    "The team always makes the final call. Any member can run /rt-pause; while paused "
    "I read nothing and post nothing except a one-line confirmation. "
    "Run /rt-resume to resume, or /rt-help for commands."
)

HELP_TEXT = (
    PITCH + "\n\nAvailable now:\n"
    "/rt-setup — enable this channel and pin the transparency notice "
    "(Manage Channels permission or server owner).\n"
    "/rt-pause — any member can pause this channel.\n"
    "/rt-resume — any member can resume this channel.\n"
    "/rt-help — show this private help.\n"
    "/rt-decide question:<text> — start a private-first decision round.\n"
    "/rt-log — show the last 10 closed decisions privately.\n"
    "/re-open — browse past decisions, add an option or vote again.\n\n"
    "Right-click a message → Apps → Ask Roundtable privately for a private explanation. "
    "Rate it with Helpful / Not helpful.\n"
    "End round early — the round starter, server owner, or Manage Channels member can close a round using its button.\n\n"
    "Automatic detection offers Yes, run a round / No thanks; it never starts a round without a member's Yes.\n\n"
    "/rt-tasks add / list / assign / prefs / done — member-written tasks, confirmed owners, and workload counts.\n"
    "/rt-deadline add — save a manual assignment deadline (YYYY-MM-DD, UTC).\n"
    "/rt-timeline — preview a proposed timeline privately; optionally post it.\n"
    "/rt-stats — channel totals and rates only, with no names or message text.\n\n"
    "I never write your team's deliverable."
)


def safe(text: str) -> str:
    # Escape formatting and links without Discord's Python 3.14-deprecated regex
    # helper; disable all mentions independently in every outgoing API request.
    escaped = re.sub(r"([\\`*_~|\[\]])|^(\s*(?:>{1,3}|#{1,3}|[-+])(?=\s))", lambda match: "\\" + match[0], text, flags=re.MULTILINE)
    return discord.utils.escape_mentions(escaped)


def share_instructions(record: RoundRecord) -> str:
    if not record.options:
        return "No options were specified. Press Add my answer to enter your own answer and a reason. Only you can see this form."
    return "Choose an option, then Add my reason. Only you can see this form."


def starting_text(record: RoundRecord) -> str:
    deadline = int(datetime.fromisoformat(record.deadline_at).timestamp())
    text = (
        f'Roundtable (AI): Starting a private round on "{safe(record.question)}". '
        'Press "Share my view" and answer in one line, in any language you like. '
        f"Nobody sees others' answers until everyone has replied, <t:{deadline}:F>, "
        "or the starter, server owner, or a channel manager ends the round early."
    )
    if record.reopened_from is not None:
        text += f" Reopened from decision {record.reopened_from}."
    return text


def progress_embed(replied: int, total: int) -> discord.Embed:
    embed = discord.Embed()
    embed.set_footer(text=f"{replied} of {total} have shared a view")
    return embed


def result_message(result: RoundResult, names: dict[str, str]) -> dict:
    content = f'Roundtable (AI): Round on "{safe(result.round.question)}" is closed.'
    if result.deadline_missing:
        if result.round.closed_at and datetime.fromisoformat(result.round.closed_at) < datetime.fromisoformat(result.round.deadline_at):
            content += " Ended early."
        content += f" Proceeding with {len(result.responses)} of {result.total_members} views."
    spread = ", ".join(
        f"{count} {'leans' if count == 1 else 'lean'} toward {safe(option)}"
        for option, count in result.counts if count
    )
    embed = discord.Embed(description=spread + "." if spread else "No views were shared.")
    for item in result.reasons:
        response = item.response
        author = names.get(response.user_id, "Member") if response.show_name else "Anonymous"
        reason = safe(item.english)
        if item.english != response.reason:
            reason += f" ({safe(response.reason)})"
        # These are the member's own words, explicitly distinguished from synthesis.
        # Preserve every original/translated character even when escaping pushes
        # a reason beyond Discord's 1024-character field limit.
        label = f"{safe(author)} · {safe(response.option_label)}"
        for offset in range(0, len(reason), 1024):
            embed.add_field(name=label if offset == 0 else label + " (continued)", value=reason[offset:offset + 1024], inline=False)
    embed.add_field(name="Minority case", value=safe(result.synthesis.steelman) if result.minority_option else "No dissent raised.", inline=False)
    embed.add_field(name="Key trade-off", value=safe(result.synthesis.trade_off), inline=False)
    embed.add_field(name="Next question", value=safe(result.synthesis.question), inline=False)
    embed.set_footer(text="The call is yours. Pick the outcome to log it.")
    # Discord allows 6000 embed characters plus 2000 message characters. Move
    # synthesis into the message body when a long five-member result needs it.
    while len(embed) > 6000 and len(embed.fields) >= 3:
        field = embed.fields[-1]
        addition = f"\n\n{field.name}: {field.value}"
        if len(content + addition) > 2000:
            break
        content += addition
        embed.remove_field(-1)
    if len(embed) > 6000 or len(embed.fields) > 25 or len(content) > 2000:
        raise RoundError("capacity")
    return {"content": content, "embed": embed}


def decision_log(entries: tuple[DecisionEntry, ...], names: dict[str, str]) -> discord.Embed:
    embed = discord.Embed(title="Roundtable decision log")
    if not entries:
        embed.description = "No closed decisions yet. Start one with /rt-decide."
    for entry in entries:
        owner = safe(names.get(entry.owner_user_id, "Member")) if entry.owner_user_id else "Unclaimed"
        embed.add_field(
            name=f"Round {entry.id} · {entry.closed_at[:10]}",
            value=f"{safe(entry.question)}\nOutcome: {safe(entry.outcome) if entry.outcome else 'open outcome'}\nOwner: {owner}",
            inline=False,
        )
    return embed


def reopen_details(record: RoundRecord, choices: tuple[str, ...]) -> discord.Embed:
    embed = discord.Embed(title=f"Reopen decision {record.id}", description=safe(record.question))
    embed.add_field(name="Previous outcome", value=safe(record.outcome) if record.outcome else "open outcome", inline=False)
    if choices:
        chunk = ""
        for index, choice in enumerate(choices, 1):
            line = f"{index}. {safe(choice)}\n"
            if len(chunk + line) > 1024:
                embed.add_field(name="Options", value=chunk, inline=False)
                chunk = ""
            chunk += line
        embed.add_field(name="Options", value=chunk, inline=False)
    else:
        embed.add_field(name="Options", value="No previous options. Members can enter their own answers.", inline=False)
    embed.set_footer(text="Start a fresh private round. Everyone submits again; the previous decision stays in the log.")
    return embed


def clarification_embed(result: Clarification) -> discord.Embed:
    embed = discord.Embed(title="Private clarification")
    sections = [("What it likely means", safe(result.meaning))]
    if result.terms:
        sections.append(("Terms or idioms explained", "\n".join(
            f"{safe(item.term)}: {safe(item.explanation)}" for item in result.terms
        )))
    if result.other_readings:
        sections.append(("Other possible readings", "\n".join(f"• {safe(text)}" for text in result.other_readings)))
    sections.append(("If you want to check", safe(result.follow_up_question)))
    for title, value in sections:
        for offset in range(0, len(value), 1024):
            embed.add_field(name=title if offset == 0 else title + " (continued)", value=value[offset:offset + 1024], inline=False)
    embed.set_footer(text="Only you can see this. Feedback is optional and counts once per answer for 15 minutes.")
    if len(embed) > 6000:
        raise RoundError("capacity")
    return embed


def detection_prompt(prompt: DecisionPrompt, record: RoundRecord | None = None) -> str:
    if prompt.status == "dismissed":
        return "Roundtable (AI): Okay, I'll stay quiet."
    if prompt.status == "accepted":
        if record is not None and record.status == "cancelled":
            return "Roundtable (AI): The round could not be posted. Use /rt-decide to try again."
        return "Roundtable (AI): Round started. Use Share my view in the round below."
    return (
        f'Roundtable (AI): It sounds like you\'re deciding "{safe(prompt.question)}" '
        f"between {', '.join(safe(option) for option in prompt.options)}. "
        "Want me to run a quick private round so everyone's view is heard?"
    )


def bounded_embed(embed: discord.Embed) -> discord.Embed:
    if len(embed) > 6000 or len(embed.fields) > 25 or any(len(field.value) > 1024 or len(field.name) > 256 for field in embed.fields):
        raise RoundError("capacity")
    return embed


def task_board(board: TaskBoard, names: dict[str, str]) -> discord.Embed:
    embed = discord.Embed(title="Task board", description=f"Open: {board.open_count} · Done: {board.done_count}")
    if not board.tasks:
        embed.add_field(name="Tasks", value="No tasks yet. Add one with /rt-tasks add.", inline=False)
    for task in board.tasks:
        owner = safe(names.get(task.owner_user_id, "Unavailable member")) if task.owner_user_id else "Unowned"
        if task.status == "proposed" and task.owner_user_id:
            owner += " (suggested; unconfirmed)"
        due = task.due_at[:10] + " UTC" if task.due_at else "No due date"
        embed.add_field(name=f"Task {task.id} · {task.status}", value=f"{safe(task.title)}\n{owner} · {due}", inline=False)
    workload = ", ".join(f"{safe(names.get(member, 'Member'))} {count}" for member, count in board.workload) or "No current members."
    if board.unavailable_count:
        workload += f" · Unavailable members: {board.unavailable_count} open tasks"
    for offset in range(0, len(workload), 1024):
        embed.add_field(name="Confirmed open tasks per member" if offset == 0 else "Workload (continued)", value=workload[offset:offset + 1024], inline=False)
    embed.set_footer(text=f"Page {board.page + 1} of {board.total_pages} · Counts only; no performance scores.")
    return bounded_embed(embed)


def task_proposals(tasks, names: dict[str, str]) -> discord.Embed:
    embed = discord.Embed(title="Proposed task owners", description="Suggestions only. Confirm each task or Confirm all. Swap changes a suggestion; it does not confirm it. Unconfirmed suggestions expire after 24 hours.")
    if not tasks:
        embed.description = "These proposals are no longer available. Use /rt-tasks list or /rt-tasks assign."
    for task in tasks:
        owner = safe(names.get(task.owner_user_id, "Unavailable member")) if task.owner_user_id else "Unowned (proposal expired)"
        due = task.due_at[:10] + " UTC" if task.due_at else "No due date"
        embed.add_field(name=f"Task {task.id} · {task.status}", value=f"{safe(task.title)}\n{owner} · {due}", inline=False)
    return bounded_embed(embed)


def timeline_embed(preview: TimelinePreview) -> discord.Embed:
    embed = discord.Embed(title="Proposed backward timeline", description=f"{safe(preview.deadline.title)} · {preview.deadline.due_at[:10]} UTC\nSuggestions only; dates and tasks are not changed.")
    for title, day in preview.milestones:
        embed.add_field(name=day + " UTC", value=safe(title), inline=False)
    embed.add_field(name="Buffer", value="At least one full calendar day before the assignment deadline.", inline=False)
    for title in preview.late_tasks:
        embed.add_field(name="Due-date check", value=f'Heads up: "{safe(title)}" is due after the assignment deadline.', inline=False)
    return bounded_embed(embed)


def pilot_stats(stats: PilotStats) -> discord.Embed:
    # Accept only the numeric core projection, never names, members, rounds,
    # tasks, messages, or a guild object. There is no identity lookup here.
    def rate(value):
        return "N/A" if value is None else f"{value:.1%}"

    duration = "N/A" if stats.median_decision_seconds is None else f"{stats.median_decision_seconds / 60:.1f} minutes"
    embed = discord.Embed(title="Roundtable pilot stats", description="Current channel since setup · aggregate numbers only")
    for name, value in (
        ("Rounds started", str(stats.rounds_started)), ("Rounds closed", str(stats.rounds_closed)),
        ("Average share of members who replied per round", rate(stats.average_reply_share)),
        ("Median time from round start to logged outcome", duration),
        ("Prompts shown", str(stats.prompts_shown)), ("Prompts accepted", str(stats.prompts_accepted)),
        ("Prompts dismissed", str(stats.prompts_dismissed)), ("Prompt accept rate", rate(stats.accept_rate)),
        ("Clarify uses", str(stats.clarify_uses)), ("Clarify helpful rate", rate(stats.helpful_rate)),
        ("Tasks done on time", str(stats.tasks_done_on_time)), ("Tasks done late", str(stats.tasks_done_late)),
        ("Confirmed tasks currently overdue", str(stats.tasks_overdue)),
    ):
        embed.add_field(name=name, value=value, inline=False)
    return embed
