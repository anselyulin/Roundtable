from datetime import datetime, timedelta, timezone
import sqlite3

import pytest

from core.db import SCHEMA, utc_iso


EXPECTED = {
    "channels": "channel_id guild_id enabled paused detection_enabled notice_message_id last_prompt_at detection_cooldown_until",
    "rounds": "id channel_id question options_json status public_message_id created_by created_at deadline_at outcome owner_user_id closed_at outcome_at reopened_from",
    "round_members": "round_id user_id replied reminded",
    "responses": "round_id user_id option_label reason show_name created_at",
    "tasks": "id channel_id round_id title owner_user_id due_at status proposed_at done_at created_by proposal_batch proposal_message_id",
    "member_prefs": "channel_id user_id preference_note",
    "deadlines": "id channel_id title due_at kind",
    "metrics": "channel_id name count",
    "decision_prompts": "id channel_id question options_json public_message_id status round_id created_at resolved_at",
}


def test_complete_schema_idempotent_and_all_discord_ids_text(db):
    db.connection.executescript(SCHEMA)
    tables = {row[0] for row in db.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert tables == set(EXPECTED)
    discord_ids = {"channel_id", "guild_id", "user_id", "public_message_id", "notice_message_id", "created_by", "owner_user_id", "proposal_message_id"}
    for table, fields in EXPECTED.items():
        columns = db.connection.execute(f"PRAGMA table_info({table})").fetchall()
        assert [row["name"] for row in columns] == fields.split()
        for column in columns:
            if column["name"] in discord_ids or column["name"].endswith("_at"):
                assert column["type"] == "TEXT"
    assert db.connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_default_state_requires_explicit_setup(db):
    assert db.get_channel(200) is None
    assert not db.set_paused(200, True)
    state = db.ensure_channel(200, 100)
    assert not state.enabled and not state.paused and state.detection_enabled
    assert state.notice_message_id is None and state.last_prompt_at is None
    assert not state.active
    assert not db.set_paused(200, False)


def test_settings_and_notice_updates_preserve_pause_and_other_channels(db):
    db.ensure_channel(200, 100)
    db.ensure_channel(201, 100)
    db.set_notice(200, 300)
    db.enable_channel(200)
    assert db.get_channel(200).active
    assert db.set_paused(200, True)
    db.ensure_channel(200, 100)
    db.enable_channel(200)
    assert db.get_channel(200).paused
    assert db.get_channel(200).notice_message_id == "300"
    assert not db.get_channel(201).active
    assert db.set_paused(200, False)
    assert db.get_channel(200).active


def test_ids_remain_exact_text_and_guild_cannot_change(db):
    large_id = "1234567890123456789"
    db.ensure_channel(large_id, "987654321012345678")
    row = db.connection.execute("SELECT channel_id, typeof(channel_id) FROM channels").fetchone()
    assert tuple(row) == (large_id, "text")
    with pytest.raises(ValueError, match="different guild"):
        db.ensure_channel(large_id, 999)
    with pytest.raises(ValueError):
        db.ensure_channel("not a channel id", 100)


def test_metrics_allow_only_aggregate_named_counters(db):
    db.ensure_channel(200, 100)
    fields = db.connection.execute("PRAGMA table_info(metrics)").fetchall()
    assert [row["name"] for row in fields if row["pk"]] == ["channel_id", "name"]
    counters = [
        "prompts_shown", "prompts_accepted", "prompts_dismissed", "clarify_uses",
        "clarify_helpful", "clarify_unhelpful", "tasks_done_on_time", "tasks_done_late",
    ]
    for name in counters:
        db.connection.execute("INSERT INTO metrics (channel_id, name) VALUES ('200', ?)", (name,))
    assert all(row[0] == 0 for row in db.connection.execute("SELECT count FROM metrics"))
    for name, count in [("unknown", 1), ("prompts_shown", -1)]:
        with pytest.raises(sqlite3.IntegrityError):
            db.connection.execute("INSERT OR REPLACE INTO metrics VALUES ('200', ?, ?)", (name, count))


def test_outcome_and_done_timestamps_exist_and_are_nullable(db):
    for table, field in [("rounds", "outcome_at"), ("tasks", "done_at")]:
        column = next(row for row in db.connection.execute(f"PRAGMA table_info({table})") if row["name"] == field)
        assert column["notnull"] == 0
        assert column["type"] == "TEXT"


def test_utc_iso_rejects_naive_datetimes():
    assert utc_iso(datetime(2026, 10, 22, 9, tzinfo=timezone(timedelta(hours=-7)))) == "2026-10-22T16:00:00+00:00"
    with pytest.raises(ValueError, match="timezone-aware"):
        utc_iso(datetime(2026, 10, 22))
