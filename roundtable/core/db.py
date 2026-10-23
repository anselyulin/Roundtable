"""SQLite persistence. No chat-message field, history query, or logging hook."""

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import json
import sqlite3


SCHEMA = """
CREATE TABLE IF NOT EXISTS channels (
    channel_id TEXT PRIMARY KEY,
    guild_id TEXT NOT NULL,
    enabled INT NOT NULL DEFAULT 0 CHECK (enabled IN (0, 1)),
    paused INT NOT NULL DEFAULT 0 CHECK (paused IN (0, 1)),
    detection_enabled INT NOT NULL DEFAULT 1 CHECK (detection_enabled IN (0, 1)),
    notice_message_id TEXT,
    last_prompt_at TEXT,
    detection_cooldown_until TEXT
);
CREATE TABLE IF NOT EXISTS rounds (
    id INTEGER PRIMARY KEY,
    channel_id TEXT NOT NULL REFERENCES channels(channel_id),
    question TEXT NOT NULL,
    options_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('collecting', 'closed', 'cancelled')),
    public_message_id TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    deadline_at TEXT NOT NULL,
    outcome TEXT,
    owner_user_id TEXT,
    closed_at TEXT,
    outcome_at TEXT,
    reopened_from INTEGER REFERENCES rounds(id)
);
CREATE TABLE IF NOT EXISTS round_members (
    round_id INTEGER NOT NULL REFERENCES rounds(id),
    user_id TEXT NOT NULL,
    replied INT NOT NULL DEFAULT 0 CHECK (replied IN (0, 1)),
    reminded INT NOT NULL DEFAULT 0 CHECK (reminded IN (0, 1)),
    PRIMARY KEY (round_id, user_id)
);
CREATE TABLE IF NOT EXISTS responses (
    round_id INTEGER NOT NULL REFERENCES rounds(id),
    user_id TEXT NOT NULL,
    option_label TEXT NOT NULL,
    reason TEXT NOT NULL,
    show_name INT NOT NULL DEFAULT 0 CHECK (show_name IN (0, 1)),
    created_at TEXT NOT NULL,
    PRIMARY KEY (round_id, user_id),
    FOREIGN KEY (round_id, user_id) REFERENCES round_members(round_id, user_id)
);
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY,
    channel_id TEXT NOT NULL REFERENCES channels(channel_id),
    round_id INTEGER REFERENCES rounds(id),
    title TEXT NOT NULL,
    owner_user_id TEXT,
    due_at TEXT,
    status TEXT NOT NULL CHECK (status IN ('proposed', 'confirmed', 'done')),
    proposed_at TEXT NOT NULL,
    done_at TEXT,
    created_by TEXT NOT NULL,
    proposal_batch TEXT,
    proposal_message_id TEXT
);
CREATE TABLE IF NOT EXISTS member_prefs (
    channel_id TEXT NOT NULL REFERENCES channels(channel_id),
    user_id TEXT NOT NULL,
    preference_note TEXT NOT NULL,
    PRIMARY KEY (channel_id, user_id)
);
CREATE TABLE IF NOT EXISTS deadlines (
    id INTEGER PRIMARY KEY,
    channel_id TEXT NOT NULL REFERENCES channels(channel_id),
    title TEXT NOT NULL,
    due_at TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('assignment', 'milestone'))
);
CREATE TABLE IF NOT EXISTS metrics (
    channel_id TEXT NOT NULL REFERENCES channels(channel_id),
    name TEXT NOT NULL CHECK (name IN (
        'prompts_shown', 'prompts_accepted', 'prompts_dismissed',
        'clarify_uses', 'clarify_helpful', 'clarify_unhelpful',
        'tasks_done_on_time', 'tasks_done_late'
    )),
    count INT NOT NULL DEFAULT 0 CHECK (count >= 0),
    PRIMARY KEY (channel_id, name)
);
CREATE TABLE IF NOT EXISTS decision_prompts (
    id INTEGER PRIMARY KEY,
    channel_id TEXT NOT NULL REFERENCES channels(channel_id),
    question TEXT NOT NULL,
    options_json TEXT NOT NULL,
    public_message_id TEXT UNIQUE,
    status TEXT NOT NULL CHECK (status IN ('pending', 'accepted', 'dismissed', 'cancelled')),
    round_id INTEGER REFERENCES rounds(id),
    created_at TEXT NOT NULL,
    resolved_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS one_pending_decision_prompt
ON decision_prompts(channel_id) WHERE status = 'pending';
"""


def utc_iso(moment: datetime) -> str:
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("Timestamps must be timezone-aware.")
    return moment.astimezone(timezone.utc).isoformat()


def _discord_id(value: str | int) -> str:
    identifier = str(value)
    if not identifier.isdecimal() or int(identifier) <= 0:
        raise ValueError("Expected a positive Discord id.")
    return identifier


@dataclass(frozen=True)
class ChannelSettings:
    channel_id: str
    guild_id: str
    enabled: bool
    paused: bool
    detection_enabled: bool
    notice_message_id: str | None
    last_prompt_at: str | None
    detection_cooldown_until: str | None = None

    @property
    def active(self) -> bool:
        return self.enabled and not self.paused


@dataclass(frozen=True)
class RoundRecord:
    id: int
    channel_id: str
    question: str
    options: tuple[str, ...]
    status: str
    public_message_id: str | None
    created_by: str
    created_at: str
    deadline_at: str
    outcome: str | None
    owner_user_id: str | None
    closed_at: str | None
    outcome_at: str | None
    reopened_from: int | None = None

    @property
    def time_to_decision_seconds(self) -> float | None:
        if self.outcome_at is None:
            return None
        return (datetime.fromisoformat(self.outcome_at) - datetime.fromisoformat(self.created_at)).total_seconds()


@dataclass(frozen=True)
class ResponseRecord:
    round_id: int
    user_id: str
    option_label: str
    reason: str
    show_name: bool
    created_at: str


@dataclass(frozen=True)
class RoundMember:
    user_id: str
    replied: bool
    reminded: bool


@dataclass(frozen=True)
class DecisionEntry:
    id: int
    question: str
    outcome: str | None
    owner_user_id: str | None
    closed_at: str


@dataclass(frozen=True)
class DecisionPrompt:
    id: int
    channel_id: str
    question: str
    options: tuple[str, ...]
    public_message_id: str | None
    status: str
    round_id: int | None
    created_at: str
    resolved_at: str | None


@dataclass(frozen=True)
class TaskRecord:
    id: int
    channel_id: str
    round_id: int | None
    title: str
    owner_user_id: str | None
    due_at: str | None
    status: str
    proposed_at: str
    done_at: str | None
    created_by: str
    proposal_batch: str | None
    proposal_message_id: str | None


@dataclass(frozen=True)
class DeadlineRecord:
    id: int
    channel_id: str
    title: str
    due_at: str
    kind: str


class Database:
    def __init__(self, path: str | Path = "./roundtable.db") -> None:
        self.connection = sqlite3.connect(str(path))
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.executescript(SCHEMA)
        # Upgrade existing installations without changing historical decisions.
        columns = {row["name"] for row in self.connection.execute("PRAGMA table_info(rounds)")}
        with self.connection:
            if "reopened_from" not in columns:
                self.connection.execute("ALTER TABLE rounds ADD COLUMN reopened_from INTEGER REFERENCES rounds(id)")
            self.connection.execute(
                """CREATE UNIQUE INDEX IF NOT EXISTS one_active_reopening
                ON rounds(reopened_from) WHERE reopened_from IS NOT NULL AND status = 'collecting'"""
            )
            channel_columns = {row["name"] for row in self.connection.execute("PRAGMA table_info(channels)")}
            if "detection_cooldown_until" not in channel_columns:
                self.connection.execute("ALTER TABLE channels ADD COLUMN detection_cooldown_until TEXT")
            task_columns = {row["name"] for row in self.connection.execute("PRAGMA table_info(tasks)")}
            for column in ("proposal_batch", "proposal_message_id"):
                if column not in task_columns:
                    self.connection.execute(f"ALTER TABLE tasks ADD COLUMN {column} TEXT")

    def close(self) -> None:
        self.connection.close()

    def get_channel(self, channel_id: str | int) -> ChannelSettings | None:
        row = self.connection.execute(
            "SELECT * FROM channels WHERE channel_id = ?", (_discord_id(channel_id),)
        ).fetchone()
        if row is None:
            return None
        return ChannelSettings(
            channel_id=row["channel_id"], guild_id=row["guild_id"],
            enabled=bool(row["enabled"]), paused=bool(row["paused"]),
            detection_enabled=bool(row["detection_enabled"]),
            notice_message_id=row["notice_message_id"], last_prompt_at=row["last_prompt_at"],
            detection_cooldown_until=row["detection_cooldown_until"],
        )

    def ensure_channel(self, channel_id: str | int, guild_id: str | int) -> ChannelSettings:
        channel_id, guild_id = _discord_id(channel_id), _discord_id(guild_id)
        existing = self.get_channel(channel_id)
        if existing is not None and existing.guild_id != guild_id:
            raise ValueError("Channel belongs to a different guild.")
        with self.connection:
            self.connection.execute(
                "INSERT OR IGNORE INTO channels (channel_id, guild_id) VALUES (?, ?)",
                (channel_id, guild_id),
            )
        return self.get_channel(channel_id)

    def set_notice(self, channel_id: str | int, message_id: str | int) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE channels SET notice_message_id = ? WHERE channel_id = ?",
                (_discord_id(message_id), _discord_id(channel_id)),
            )

    def enable_channel(self, channel_id: str | int) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE channels SET enabled = 1 WHERE channel_id = ?",
                (_discord_id(channel_id),),
            )

    def set_paused(self, channel_id: str | int, paused: bool) -> bool:
        """No implicit setup: an unknown or disabled channel stays disabled."""
        with self.connection:
            result = self.connection.execute(
                "UPDATE channels SET paused = ? WHERE channel_id = ? AND enabled = 1",
                (int(paused), _discord_id(channel_id)),
            )
        return result.rowcount == 1

    def increment_metric(self, channel_id: str | int, name: str) -> None:
        # The schema's allowlist rejects unknown counters. No per-user receipts
        # or content are written, including for private clarification feedback.
        with self.connection:
            self._increment_metric(channel_id, name)

    def _increment_metric(self, channel_id: str | int, name: str) -> None:
        self.connection.execute(
            """INSERT INTO metrics (channel_id, name, count) VALUES (?, ?, 1)
            ON CONFLICT(channel_id, name) DO UPDATE SET count = count + 1""",
            (_discord_id(channel_id), name),
        )

    def create_round(
        self, *, channel_id: str, question: str, options: tuple[str, ...],
        created_by: str, created_at: str, deadline_at: str, member_ids: tuple[str, ...],
        reopened_from: int | None = None,
    ) -> RoundRecord:
        with self.connection:
            round_id = self._insert_round(
                channel_id=channel_id, question=question, options=options, created_by=created_by,
                created_at=created_at, deadline_at=deadline_at, member_ids=member_ids, reopened_from=reopened_from,
            )
        return self.get_round(round_id)

    def _insert_round(self, *, channel_id, question, options, created_by, created_at, deadline_at, member_ids, reopened_from=None) -> int:
        cursor = self.connection.execute(
            """INSERT INTO rounds
            (channel_id, question, options_json, status, created_by, created_at, deadline_at, reopened_from)
            VALUES (?, ?, ?, 'collecting', ?, ?, ?, ?)""",
            (_discord_id(channel_id), question, json.dumps(options, ensure_ascii=False),
             _discord_id(created_by), created_at, deadline_at, reopened_from),
        )
        round_id = cursor.lastrowid
        self.connection.executemany(
            "INSERT INTO round_members (round_id, user_id) VALUES (?, ?)",
            [(round_id, _discord_id(member)) for member in dict.fromkeys(member_ids)],
        )
        return round_id

    def get_round(self, round_id: int) -> RoundRecord | None:
        row = self.connection.execute("SELECT * FROM rounds WHERE id = ?", (round_id,)).fetchone()
        if row is None:
            return None
        values = dict(row)
        values["options"] = tuple(json.loads(values.pop("options_json")))
        return RoundRecord(**values)

    def round_members(self, round_id: int) -> tuple[RoundMember, ...]:
        return tuple(
            RoundMember(row["user_id"], bool(row["replied"]), bool(row["reminded"]))
            for row in self.connection.execute(
                "SELECT user_id, replied, reminded FROM round_members WHERE round_id = ? ORDER BY user_id", (round_id,),
            )
        )

    def round_responses(self, round_id: int) -> tuple[ResponseRecord, ...]:
        return tuple(
            ResponseRecord(**{**dict(row), "show_name": bool(row["show_name"])})
            for row in self.connection.execute(
                "SELECT * FROM responses WHERE round_id = ? ORDER BY created_at, user_id", (round_id,)
            )
        )

    def response_choices(self, round_id: int) -> tuple[str, ...]:
        # Reopening an open-ended decision needs labels, never member reasons.
        return tuple(dict.fromkeys(row[0] for row in self.connection.execute(
            "SELECT option_label FROM responses WHERE round_id = ? ORDER BY created_at, user_id", (round_id,),
        )))

    def set_round_message(self, round_id: int, message_id: str | int) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE rounds SET public_message_id = ? WHERE id = ?", (_discord_id(message_id), round_id),
            )

    def cancel_round(self, round_id: int) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE rounds SET status = 'cancelled' WHERE id = ? AND status = 'collecting'", (round_id,)
            )

    def upsert_response(
        self, *, round_id: int, user_id: str, option_label: str, reason: str,
        show_name: bool, now: str,
    ) -> bool:
        with self.connection:
            eligible = self.connection.execute(
                """SELECT 1 FROM rounds r JOIN round_members m ON m.round_id = r.id
                WHERE r.id = ? AND m.user_id = ? AND r.status = 'collecting' AND r.deadline_at > ?""",
                (round_id, _discord_id(user_id), now),
            ).fetchone()
            if not eligible:
                return False
            self.connection.execute(
                """INSERT INTO responses VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(round_id, user_id) DO UPDATE SET
                option_label = excluded.option_label, reason = excluded.reason,
                show_name = excluded.show_name, created_at = excluded.created_at""",
                (round_id, _discord_id(user_id), option_label, reason, int(show_name), now),
            )
            self.connection.execute(
                "UPDATE round_members SET replied = 1 WHERE round_id = ? AND user_id = ?", (round_id, _discord_id(user_id)),
            )
        return True

    def close_round(self, round_id: int, now: str) -> bool:
        with self.connection:
            result = self.connection.execute(
                "UPDATE rounds SET status = 'closed', closed_at = ? WHERE id = ? AND status = 'collecting'", (now, round_id),
            )
        return result.rowcount == 1

    def record_outcome(self, round_id: int, outcome: str, user_id: str, now: str) -> bool:
        with self.connection:
            result = self.connection.execute(
                """UPDATE rounds SET outcome = ?, outcome_at = ?, owner_user_id = ?
                WHERE id = ? AND status = 'closed' AND outcome IS NULL""", (outcome, now, _discord_id(user_id), round_id),
            )
        return result.rowcount == 1

    def mark_reminded(self, round_id: int, user_ids: tuple[str, ...]) -> None:
        with self.connection:
            self.connection.executemany(
                "UPDATE round_members SET reminded = 1 WHERE round_id = ? AND user_id = ? AND replied = 0",
                [(round_id, _discord_id(user)) for user in user_ids],
            )

    def restorable_round_ids(self) -> tuple[int, ...]:
        return tuple(row[0] for row in self.connection.execute(
            "SELECT id FROM rounds WHERE status != 'cancelled' AND public_message_id IS NOT NULL"
        ))

    def cancel_unpublished_rounds(self) -> None:
        # A crash between creating a round and posting its starter must not
        # produce reminders for a round nobody can open.
        with self.connection:
            self.connection.execute(
                "UPDATE rounds SET status = 'cancelled' WHERE status = 'collecting' AND public_message_id IS NULL"
            )

    def active_round_ids(self) -> tuple[int, ...]:
        return tuple(row[0] for row in self.connection.execute(
            """SELECT r.id FROM rounds r JOIN channels c ON c.channel_id = r.channel_id
            WHERE c.enabled = 1 AND c.paused = 0 AND r.status = 'collecting'"""
        ))

    def unlogged_closed_round_ids(self) -> tuple[int, ...]:
        return tuple(row[0] for row in self.connection.execute(
            "SELECT id FROM rounds WHERE status = 'closed' AND outcome IS NULL"
        ))

    def logged_round_ids(self) -> tuple[int, ...]:
        return tuple(row[0] for row in self.connection.execute(
            "SELECT id FROM rounds WHERE status = 'closed' AND outcome IS NOT NULL AND public_message_id IS NOT NULL"
        ))

    def decision_log(self, channel_id: str) -> tuple[DecisionEntry, ...]:
        return self.decision_history(channel_id, limit=10, offset=0)

    def decision_count(self, channel_id: str) -> int:
        return self.connection.execute(
            "SELECT count(*) FROM rounds WHERE channel_id = ? AND status = 'closed'", (_discord_id(channel_id),),
        ).fetchone()[0]

    def active_reopening(self, round_id: int) -> int | None:
        row = self.connection.execute(
            "SELECT id FROM rounds WHERE reopened_from = ? AND status = 'collecting'", (round_id,),
        ).fetchone()
        return row[0] if row else None

    def decision_history(self, channel_id: str, *, limit: int, offset: int) -> tuple[DecisionEntry, ...]:
        # Deliberately never query responses, participation, or round creators.
        return tuple(DecisionEntry(**dict(row)) for row in self.connection.execute(
            """SELECT id, question, outcome, owner_user_id, closed_at FROM rounds
            WHERE channel_id = ? AND status = 'closed' ORDER BY closed_at DESC, id DESC LIMIT ? OFFSET ?""",
            (_discord_id(channel_id), limit, offset),
        ))

    def get_decision_prompt(self, prompt_id: int) -> DecisionPrompt | None:
        row = self.connection.execute("SELECT * FROM decision_prompts WHERE id=?", (prompt_id,)).fetchone()
        if row is None:
            return None
        values = dict(row)
        values["options"] = tuple(json.loads(values.pop("options_json")))
        return DecisionPrompt(**values)

    def has_pending_prompt(self, channel_id: str | int) -> bool:
        return self.connection.execute(
            "SELECT 1 FROM decision_prompts WHERE channel_id=? AND status='pending'", (_discord_id(channel_id),),
        ).fetchone() is not None

    def create_decision_prompt(self, channel_id: str | int, question: str, options: tuple[str, ...], now: str) -> DecisionPrompt:
        with self.connection:
            cursor = self.connection.execute(
                "INSERT INTO decision_prompts (channel_id,question,options_json,status,created_at) VALUES (?,?,?,'pending',?)",
                (_discord_id(channel_id), question, json.dumps(options, ensure_ascii=False), now),
            )
        return self.get_decision_prompt(cursor.lastrowid)

    def mark_prompt_shown(self, prompt_id: int, message_id: str | int, now: str, cooldown_until: str) -> bool:
        with self.connection:
            changed = self.connection.execute(
                "UPDATE decision_prompts SET public_message_id=? WHERE id=? AND status='pending' AND public_message_id IS NULL",
                (_discord_id(message_id), prompt_id),
            ).rowcount
            if changed:
                prompt = self.get_decision_prompt(prompt_id)
                self.connection.execute(
                    "UPDATE channels SET last_prompt_at=?,detection_cooldown_until=? WHERE channel_id=?",
                    (now, cooldown_until, prompt.channel_id),
                )
                self._increment_metric(prompt.channel_id, "prompts_shown")
        return bool(changed)

    def cancel_unpublished_prompts(self, prompt_id: int | None = None) -> None:
        with self.connection:
            self.connection.execute(
                """UPDATE decision_prompts SET status='cancelled'
                WHERE status='pending' AND public_message_id IS NULL AND (? IS NULL OR id=?)""", (prompt_id, prompt_id),
            )

    def published_prompt_ids(self) -> tuple[int, ...]:
        return tuple(row[0] for row in self.connection.execute("SELECT id FROM decision_prompts WHERE public_message_id IS NOT NULL"))

    def dismiss_decision_prompt(self, prompt_id: int, now: str, cooldown_until: str) -> bool:
        with self.connection:
            changed = self.connection.execute(
                """UPDATE decision_prompts SET status='dismissed',resolved_at=?
                WHERE id=? AND status='pending' AND public_message_id IS NOT NULL""", (now, prompt_id),
            ).rowcount
            if changed:
                prompt = self.get_decision_prompt(prompt_id)
                self.connection.execute(
                    """UPDATE channels SET detection_cooldown_until=CASE
                    WHEN detection_cooldown_until>? THEN detection_cooldown_until ELSE ? END WHERE channel_id=?""",
                    (cooldown_until, cooldown_until, prompt.channel_id),
                )
                self._increment_metric(prompt.channel_id, "prompts_dismissed")
        return bool(changed)

    def accept_decision_prompt(self, prompt_id: int, creator_id: str | int, member_ids: tuple[str, ...], now: str, deadline_at: str) -> RoundRecord | None:
        with self.connection:
            changed = self.connection.execute(
                """UPDATE decision_prompts SET status='accepted',resolved_at=?
                WHERE id=? AND status='pending' AND public_message_id IS NOT NULL""", (now, prompt_id),
            ).rowcount
            if not changed:
                return None
            prompt = self.get_decision_prompt(prompt_id)
            round_id = self._insert_round(
                channel_id=prompt.channel_id, question=prompt.question, options=prompt.options,
                created_by=creator_id, member_ids=member_ids, created_at=now, deadline_at=deadline_at,
            )
            self.connection.execute("UPDATE decision_prompts SET round_id=? WHERE id=?", (round_id, prompt_id))
            self._increment_metric(prompt.channel_id, "prompts_accepted")
        return self.get_round(round_id)

    def add_task(self, channel_id: str | int, creator_id: str | int, title: str, due_at: str | None, now: str, round_id: int | None = None) -> TaskRecord:
        with self.connection:
            cursor = self.connection.execute(
                """INSERT INTO tasks(channel_id,round_id,title,due_at,status,proposed_at,created_by)
                VALUES(?,?,?,?,'proposed',?,?)""",
                (_discord_id(channel_id), round_id, title, due_at, now, _discord_id(creator_id)),
            )
        return self.get_task(cursor.lastrowid)

    def get_task(self, task_id: int) -> TaskRecord | None:
        row = self.connection.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        return TaskRecord(**dict(row)) if row else None

    def channel_tasks(self, channel_id: str | int) -> tuple[TaskRecord, ...]:
        return tuple(TaskRecord(**dict(row)) for row in self.connection.execute(
            "SELECT * FROM tasks WHERE channel_id=? ORDER BY id", (_discord_id(channel_id),),
        ))

    def task_batch(self, channel_id: str | int, batch: str) -> tuple[TaskRecord, ...]:
        return tuple(TaskRecord(**dict(row)) for row in self.connection.execute(
            "SELECT * FROM tasks WHERE channel_id=? AND proposal_batch=? ORDER BY id", (_discord_id(channel_id), batch),
        ))

    def publish_task_batch(self, channel_id: str | int, batch: str, message_id: str | int) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE tasks SET proposal_message_id=? WHERE channel_id=? AND proposal_batch=?",
                (_discord_id(message_id), _discord_id(channel_id), batch),
            )

    def save_task_proposals(self, tasks: tuple[TaskRecord, ...], assignments: tuple[tuple[int, str], ...], batch: str, now: str) -> None:
        with self.connection:
            if any(self.get_task(task.id) != task for task in tasks):
                raise ValueError("Stale task proposal.")
            self.connection.executemany(
                "UPDATE tasks SET owner_user_id=?,proposed_at=?,proposal_batch=?,proposal_message_id=NULL WHERE id=? AND status='proposed' AND owner_user_id IS NULL",
                [(owner, now, batch, task_id) for task_id, owner in assignments],
            )

    def confirm_tasks(self, channel_id: str | int, batch: str, task_ids: tuple[int, ...]) -> None:
        with self.connection:
            self.connection.executemany(
                "UPDATE tasks SET status='confirmed' WHERE id=? AND channel_id=? AND proposal_batch=? AND status='proposed' AND owner_user_id IS NOT NULL",
                [(task_id, _discord_id(channel_id), batch) for task_id in task_ids],
            )

    def swap_task_owner(self, task_id: int, channel_id: str | int, batch: str, owner_id: str) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE tasks SET owner_user_id=? WHERE id=? AND channel_id=? AND proposal_batch=? AND status='proposed' AND owner_user_id IS NOT NULL",
                (_discord_id(owner_id), task_id, _discord_id(channel_id), batch),
            )

    def cancel_task_batch(self, channel_id: str | int, batch: str) -> None:
        with self.connection:
            self.connection.execute(
                """UPDATE tasks SET owner_user_id=NULL,proposal_batch=NULL,proposal_message_id=NULL
                WHERE channel_id=? AND proposal_batch=? AND status='proposed' AND proposal_message_id IS NULL""",
                (_discord_id(channel_id), batch),
            )

    def cancel_unpublished_task_batches(self) -> None:
        with self.connection:
            self.connection.execute(
                """UPDATE tasks SET owner_user_id=NULL,proposal_batch=NULL
                WHERE status='proposed' AND proposal_batch IS NOT NULL AND proposal_message_id IS NULL"""
            )

    def published_task_batches(self) -> tuple[tuple[str, str, str], ...]:
        return tuple(tuple(row) for row in self.connection.execute(
            "SELECT DISTINCT channel_id,proposal_batch,proposal_message_id FROM tasks WHERE proposal_batch IS NOT NULL AND proposal_message_id IS NOT NULL"
        ))

    def expire_task_proposals(self, channel_id: str | int, cutoff: str) -> None:
        with self.connection:
            self.connection.execute(
                """UPDATE tasks SET owner_user_id=NULL WHERE channel_id=? AND status='proposed'
                AND owner_user_id IS NOT NULL AND proposed_at<=?""", (_discord_id(channel_id), cutoff),
            )

    def complete_task(self, task_id: int, channel_id: str | int, now: str) -> bool:
        # State change and aggregate counter must either both commit or both roll back.
        with self.connection:
            changed = self.connection.execute(
                "UPDATE tasks SET status='done',done_at=? WHERE id=? AND channel_id=? AND status='confirmed'",
                (now, task_id, _discord_id(channel_id)),
            ).rowcount
            if changed:
                task = self.get_task(task_id)
                name = "tasks_done_late" if task.due_at is not None and now > task.due_at else "tasks_done_on_time"
                self._increment_metric(channel_id, name)
        return bool(changed)

    def set_preference(self, channel_id: str | int, user_id: str | int, note: str) -> None:
        with self.connection:
            self.connection.execute(
                """INSERT INTO member_prefs VALUES(?,?,?) ON CONFLICT(channel_id,user_id)
                DO UPDATE SET preference_note=excluded.preference_note""", (_discord_id(channel_id), _discord_id(user_id), note),
            )

    def preference(self, channel_id: str | int, user_id: str | int) -> str:
        row = self.connection.execute(
            "SELECT preference_note FROM member_prefs WHERE channel_id=? AND user_id=?", (_discord_id(channel_id), _discord_id(user_id)),
        ).fetchone()
        return row[0] if row else ""

    def add_deadline(self, channel_id: str | int, title: str, due_at: str) -> DeadlineRecord:
        with self.connection:
            cursor = self.connection.execute(
                "INSERT INTO deadlines(channel_id,title,due_at,kind) VALUES(?,?,?,'assignment')", (_discord_id(channel_id), title, due_at),
            )
        return self.get_deadline(cursor.lastrowid)

    def get_deadline(self, deadline_id: int) -> DeadlineRecord | None:
        row = self.connection.execute("SELECT * FROM deadlines WHERE id=?", (deadline_id,)).fetchone()
        return DeadlineRecord(**dict(row)) if row else None

    def nearest_deadline(self, channel_id: str | int, now: str) -> DeadlineRecord | None:
        row = self.connection.execute(
            """SELECT * FROM deadlines WHERE channel_id=? AND kind='assignment' AND due_at>=?
            ORDER BY due_at,id LIMIT 1""", (_discord_id(channel_id), now),
        ).fetchone()
        return DeadlineRecord(**dict(row)) if row else None

    def pilot_numbers(self, channel_id: str | int, now: str) -> dict:
        # This boundary returns numbers only. Never select identities, questions,
        # task titles, reasons, preference notes, or any message text for stats.
        channel_id = _discord_id(channel_id)
        totals = self.connection.execute(
            """SELECT count(*) AS started,coalesce(sum(status='closed'),0) AS closed
            FROM rounds WHERE channel_id=? AND status!='cancelled'""", (channel_id,),
        ).fetchone()
        share = self.connection.execute(
            """SELECT avg(share) FROM (SELECT 1.0*sum(m.replied)/count(*) AS share
            FROM round_members m JOIN rounds r ON r.id=m.round_id
            WHERE r.channel_id=? AND r.status!='cancelled' GROUP BY r.id)""", (channel_id,),
        ).fetchone()[0]
        durations = tuple(row[0] for row in self.connection.execute(
            """SELECT (julianday(outcome_at)-julianday(created_at))*86400
            FROM rounds WHERE channel_id=? AND status='closed' AND outcome_at IS NOT NULL""", (channel_id,),
        ))
        counters = dict(self.connection.execute("SELECT name,count FROM metrics WHERE channel_id=?", (channel_id,)))
        overdue = self.connection.execute(
            "SELECT count(*) FROM tasks WHERE channel_id=? AND status='confirmed' AND due_at<?", (channel_id, now),
        ).fetchone()[0]
        return dict(started=totals["started"], closed=totals["closed"], share=share, durations=durations, counters=counters, overdue=overdue)
