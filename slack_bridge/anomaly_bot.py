from __future__ import annotations

import argparse
import json
import os
import sys
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv


ROOT_DIR = Path(__file__).resolve().parents[1]
BACKEND_DIR = ROOT_DIR / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.anomaly_detection import JST, event_message, parse_dt, to_iso  # noqa: E402
from app.database import get_connection, init_db  # noqa: E402


DEFAULT_MAP_PATH = ROOT_DIR / "config" / "slack_participants.json"
DEFAULT_WORKSPACES_PATH = ROOT_DIR / "config" / "slack_workspaces.json"
STEP_EVENT_TYPES = {"step_increase_30min", "low_steps_30min"}
INTERVAL_EVENT_TYPES = STEP_EVENT_TYPES | {"heart_rate_sleep_x2"}
SCHEDULED_EVENT_TYPES = {
    "sleep_minutes_demographic_outlier",
    "sleep_minutes_normal",
    "daily_steps_demographic_outlier",
    "daily_steps_normal",
}
QUESTION_COOLDOWN_HOURS = 2
SCHEDULED_EVENT_MAX_AGE_HOURS = 12
INTERVAL_EVENT_MAX_AGE_HOURS = 2
QUIET_START_HOUR_JST = 22
QUIET_END_HOUR_JST = 7
WAKE_NOTIFICATION_DELAY_MINUTES = 40
QUIET_HOURS_ALLOWED_EVENT_TYPES = {"daily_steps_demographic_outlier", "daily_steps_normal"}


# Slackアプリ1件分の接続設定を保持する。
@dataclass(frozen=True)
class SlackSettings:
    workspace_id: str
    bot_token: str
    app_token: str
    participant_map_path: Path
    bot_id: str


# Slack通知先となる参加者情報を保持する。
@dataclass(frozen=True)
class SlackParticipant:
    participant_id: str
    slack_channel_id: str | None = None
    slack_user_id: str | None = None
    enabled: bool = True


# 現在時刻をISO形式で返す。
def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat()


# 設定値を絶対パスへ解決する。
def resolve_config_path(value: str, default_path: Path | None = None) -> Path:
    map_value = value.strip() if value else str(default_path or DEFAULT_MAP_PATH)
    path = Path(map_value)
    if not path.is_absolute():
        path = ROOT_DIR / path
    return path


# 設定値を取得し、未指定なら環境変数から補完する。
def env_or_value(entry: dict[str, Any], value_key: str, env_key: str) -> str:
    value = str(entry.get(value_key, "")).strip()
    if value:
        return value
    env_name = str(entry.get(env_key, "")).strip()
    if env_name:
        return os.environ.get(env_name, "").strip()
    return ""


# 単一ワークスペース用のSlack設定を読み込む。
def load_settings(require_app_token: bool = False) -> SlackSettings | None:
    load_dotenv(ROOT_DIR / ".env")
    bot_token = os.environ.get("SLACK_BOT_TOKEN", "").strip()
    app_token = os.environ.get("SLACK_APP_TOKEN", "").strip()
    bot_id = os.environ.get("SLACK_BOT_ID", "slack_bot").strip() or "slack_bot"
    workspace_id = os.environ.get("SLACK_WORKSPACE_ID", "default").strip() or "default"
    map_path = resolve_config_path(os.environ.get("SLACK_PARTICIPANT_MAP", str(DEFAULT_MAP_PATH)))

    if not bot_token:
        return None
    if require_app_token and not app_token:
        raise RuntimeError("SLACK_APP_TOKEN is required to run the interactive Slack bot.")

    return SlackSettings(
        workspace_id=workspace_id,
        bot_token=bot_token,
        app_token=app_token,
        participant_map_path=map_path,
        bot_id=bot_id,
    )


# 設定ファイルから全Slackワークスペースの設定を読み込む。
def load_workspace_settings(require_app_token: bool = False) -> list[SlackSettings]:
    load_dotenv(ROOT_DIR / ".env")
    config_path = resolve_config_path(os.environ.get("SLACK_WORKSPACES_CONFIG", str(DEFAULT_WORKSPACES_PATH)), DEFAULT_WORKSPACES_PATH)
    if not config_path.exists():
        legacy = load_settings(require_app_token=require_app_token)
        return [legacy] if legacy is not None else []

    raw = json.loads(config_path.read_text(encoding="utf-8"))
    settings = []
    for index, entry in enumerate(raw.get("workspaces", []), start=1):
        if not entry.get("enabled", True):
            continue
        workspace_id = str(entry.get("workspace_id", f"workspace_{index}")).strip() or f"workspace_{index}"
        bot_token = env_or_value(entry, "bot_token", "bot_token_env")
        app_token = env_or_value(entry, "app_token", "app_token_env")
        bot_id = str(entry.get("bot_id", f"slack_bot_{workspace_id}")).strip() or f"slack_bot_{workspace_id}"
        participant_map = resolve_config_path(str(entry.get("participant_map", DEFAULT_MAP_PATH)))
        if not bot_token:
            print(f"Slack workspace skipped: missing bot token for {workspace_id}")
            continue
        if require_app_token and not app_token:
            raise RuntimeError(f"SLACK_APP_TOKEN is required to run the interactive Slack bot for {workspace_id}.")
        settings.append(
            SlackSettings(
                workspace_id=workspace_id,
                bot_token=bot_token,
                app_token=app_token,
                participant_map_path=participant_map,
                bot_id=bot_id,
            )
        )
    return settings


# 設定ファイルからSlack通知対象の参加者を読み込む。
def load_participants(path: Path) -> list[SlackParticipant]:
    if not path.exists():
        print(f"Slack participant map not found, skipped: {path}")
        return []

    raw = json.loads(path.read_text(encoding="utf-8"))
    participants = []
    for entry in raw.get("participants", []):
        if not entry.get("enabled", True):
            continue
        participant_id = str(entry.get("participant_id", "")).strip()
        slack_channel_id = str(entry.get("slack_channel_id", "")).strip() or None
        slack_user_id = str(entry.get("slack_user_id", "")).strip() or None
        if not participant_id or not (slack_channel_id or slack_user_id):
            continue
        participants.append(SlackParticipant(participant_id, slack_channel_id, slack_user_id, True))
    return participants


# Slack連携で使用するテーブルを初期化する。
def ensure_slack_tables() -> None:
    init_db()
    with get_connection() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS slack_response_drafts (
                event_id TEXT PRIMARY KEY,
                participant_id TEXT NOT NULL,
                slack_workspace_id TEXT NOT NULL DEFAULT 'default',
                slack_channel_id TEXT NOT NULL,
                activity_text TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(event_id) REFERENCES anomaly_events(event_id)
            )
            """
        )
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(slack_response_drafts)")}
        if "slack_workspace_id" not in columns:
            conn.execute("ALTER TABLE slack_response_drafts ADD COLUMN slack_workspace_id TEXT NOT NULL DEFAULT 'default'")


# 参加者に通知可能な次の異常イベントを取得する。
def get_ready_anomaly(
    participant_id: str,
    stale_detected_at_cutoff: datetime | None = None,
    suppress_interval_detected_since: datetime | None = None,
) -> dict[str, Any] | None:
    init_db()
    if stale_detected_at_cutoff is not None:
        suppress_stale_interval_events(participant_id, stale_detected_at_cutoff)
    if suppress_interval_detected_since is not None:
        suppress_interval_events_detected_since(participant_id, suppress_interval_detected_since)
    suppress_old_scheduled_events(participant_id)
    suppress_previous_day_interval_events(participant_id)
    suppress_old_interval_events(participant_id)
    suppress_events_inside_question_cooldown(participant_id)
    if participant_is_sleeping_now(participant_id):
        return None

    now = datetime.now(timezone.utc).isoformat()
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM anomaly_events
            WHERE participant_id = ?
              AND status = 'pending'
              AND expires_at >= ?
            ORDER BY event_time ASC
            """,
            (participant_id, now),
        ).fetchall()

    pending = [dict(row) for row in rows]
    for event in ready_events(participant_id, pending):
        if not can_notify_now(participant_id, event):
            continue
        event["message"] = event_message(event)
        return event
    return None


# 保留イベントから現在通知できるものを選ぶ。
def ready_events(participant_id: str, pending: list[dict[str, Any]]) -> list[dict[str, Any]]:
    interval_ready = ready_interval_events(participant_id, pending)
    interval_ready = combine_heart_and_step_events(interval_ready)
    scheduled_ready = ready_scheduled_events(pending)
    return sorted(interval_ready + scheduled_ready, key=lambda event: parse_dt(event["event_time"]))


# 同一区間の心拍上昇と歩数イベントを一つにまとめる。
def combine_heart_and_step_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    combined: list[dict[str, Any]] = []
    used_ids: set[str] = set()
    step_events = [
        event
        for event in events
        if event["event_type"] in STEP_EVENT_TYPES
    ]
    heart_events = [
        event
        for event in events
        if event["event_type"] == "heart_rate_sleep_x2"
    ]

    for heart in heart_events:
        if heart["event_id"] in used_ids:
            continue
        heart_time = parse_dt(heart["event_time"])
        matching_steps = [
            step
            for step in step_events
            if step["event_id"] not in used_ids and parse_dt(step["event_time"]) == heart_time
        ]
        if not matching_steps:
            continue

        selected_step = select_interval_representative(matching_steps, STEP_EVENT_TYPES)
        combo = dict(selected_step)
        combo["event_type"] = (
            "heart_rate_step_increase_30min"
            if selected_step["event_type"] == "step_increase_30min"
            else "heart_rate_low_steps_30min"
        )
        combo["heart_event_id"] = heart["event_id"]
        combo["step_event_id"] = selected_step["event_id"]
        combo["heart_value"] = heart.get("value")
        combo["step_value"] = selected_step.get("value")
        combo["event_end_time"] = to_iso(
            max(event_interval_end(heart), event_interval_end(selected_step))
        )
        combo["sequence_event_ids"] = sorted(
            set(
                list(heart.get("sequence_event_ids") or [heart["event_id"]])
                + list(selected_step.get("sequence_event_ids") or [selected_step["event_id"]])
            )
        )
        combined.append(combo)
        used_ids.update(combo["sequence_event_ids"])

    combined.extend(
        event
        for event in events
        if event["event_id"] not in used_ids
    )
    return combined


# イベントが現在通知可能か判定する。
def can_notify_now(participant_id: str, event: dict[str, Any]) -> bool:
    if not is_quiet_hours(participant_id):
        return True
    return event["event_type"] in QUIET_HOURS_ALLOWED_EVENT_TYPES


# 参加者が睡眠中などの通知抑制時間帯にいるか判定する。
def is_quiet_hours(participant_id: str, now: datetime | None = None) -> bool:
    current = (now or datetime.now(JST)).astimezone(JST)
    if current.hour >= QUIET_START_HOUR_JST:
        return True
    return current < notification_resume_time(participant_id, current)


# 参加者への通知を再開できる時刻を求める。
def notification_resume_time(participant_id: str, now: datetime | None = None) -> datetime:
    current = (now or datetime.now(JST)).astimezone(JST)
    wake_time = latest_wake_time_for_date(participant_id, current.date())
    if wake_time is not None:
        return wake_time + timedelta(minutes=WAKE_NOTIFICATION_DELAY_MINUTES)
    return current.replace(hour=QUIET_END_HOUR_JST, minute=0, second=0, microsecond=0)


# 指定日の最新の起床時刻を取得する。
def latest_wake_time_for_date(participant_id: str, target_date: Any) -> datetime | None:
    with get_connection() as conn:
        rows = [
            dict(row)
            for row in conn.execute(
                """
                SELECT end_time
                FROM sleep_data
                WHERE participant_id = ?
                  AND end_time IS NOT NULL
                ORDER BY end_time DESC
                """,
                (participant_id,),
            ).fetchall()
        ]

    wake_times = []
    for row in rows:
        if not row.get("end_time"):
            continue
        wake_time = parse_dt(row["end_time"]).astimezone(JST)
        if wake_time.date() == target_date:
            wake_times.append(wake_time)
    return max(wake_times) if wake_times else None


# 通知抑制時間帯に該当したイベントを抑制済みにする。
def suppress_quiet_hours_event(event: dict[str, Any]) -> None:
    event_ids = list(event.get("sequence_event_ids") or [event["event_id"]])
    with get_connection() as conn:
        conn.executemany(
            "UPDATE anomaly_events SET status = 'suppressed' WHERE event_id = ?",
            [(event_id,) for event_id in event_ids],
        )


# 指定時刻より古い区間イベントを抑制する。
def suppress_stale_interval_events(participant_id: str, cutoff: datetime) -> None:
    cutoff_iso = to_iso(cutoff)
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE anomaly_events
            SET status = 'suppressed'
            WHERE participant_id = ?
              AND status = 'pending'
              AND event_type IN ('step_increase_30min', 'low_steps_30min', 'heart_rate_sleep_x2')
              AND detected_at < ?
            """,
            (participant_id, cutoff_iso),
        )


# 指定検出時刻以降の区間イベントを抑制する。
def suppress_interval_events_detected_since(participant_id: str, detected_since: datetime) -> None:
    detected_since_iso = to_iso(detected_since)
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE anomaly_events
            SET status = 'suppressed'
            WHERE participant_id = ?
              AND status = 'pending'
              AND event_type IN ('step_increase_30min', 'low_steps_30min', 'heart_rate_sleep_x2')
              AND detected_at >= ?
            """,
            (participant_id, detected_since_iso),
        )


# 古くなった定時質問イベントを抑制する。
def suppress_old_scheduled_events(participant_id: str) -> None:
    cutoff = datetime.now(JST) - timedelta(hours=SCHEDULED_EVENT_MAX_AGE_HOURS)
    cutoff_iso = to_iso(cutoff)
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE anomaly_events
            SET status = 'suppressed'
            WHERE participant_id = ?
              AND status = 'pending'
              AND event_type IN (
                  'sleep_minutes_demographic_outlier',
                  'sleep_minutes_normal',
                  'daily_steps_demographic_outlier',
                  'daily_steps_normal'
              )
              AND event_time < ?
            """,
            (participant_id, cutoff_iso),
        )


# 古くなった区間イベントを抑制する。
def suppress_old_interval_events(participant_id: str) -> None:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=INTERVAL_EVENT_MAX_AGE_HOURS, minutes=30)
    cutoff_iso = to_iso(cutoff)
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE anomaly_events
            SET status = 'suppressed'
            WHERE participant_id = ?
              AND status = 'pending'
              AND event_type IN ('step_increase_30min', 'low_steps_30min', 'heart_rate_sleep_x2')
              AND event_time < ?
            """,
            (participant_id, cutoff_iso),
        )


# 前日以前の区間イベントを抑制する。
def suppress_previous_day_interval_events(participant_id: str) -> None:
    today_start = datetime.now(JST).replace(hour=0, minute=0, second=0, microsecond=0)
    today_start_iso = to_iso(today_start.astimezone(timezone.utc))
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE anomaly_events
            SET status = 'suppressed'
            WHERE participant_id = ?
              AND status = 'pending'
              AND event_type IN ('step_increase_30min', 'low_steps_30min', 'heart_rate_sleep_x2')
              AND event_time < ?
            """,
            (participant_id, today_start_iso),
        )


# 最新の睡眠記録から参加者が現在睡眠中か判定する。
def participant_is_sleeping_now(participant_id: str) -> bool:
    current = datetime.now(timezone.utc).astimezone(JST)
    lookback = current - timedelta(days=2)
    with get_connection() as conn:
        rows = [
            dict(row)
            for row in conn.execute(
                """
                SELECT start_time, end_time
                FROM sleep_data
                WHERE participant_id = ?
                  AND start_time >= ?
                ORDER BY start_time DESC
                """,
                (participant_id, to_iso(lookback)),
            ).fetchall()
        ]
    for row in rows:
        if not row.get("start_time"):
            continue
        start = parse_dt(row["start_time"]).astimezone(JST)
        end = parse_dt(row["end_time"]).astimezone(JST) if row.get("end_time") else None
        if start <= current and (end is None or current <= end):
            return True
    return False


# 質問のクールダウン期間内にあるイベントを抑制する。
def suppress_events_inside_question_cooldown(participant_id: str) -> None:
    with get_connection() as conn:
        pending = [
            dict(row)
            for row in conn.execute(
                """
                SELECT event_id, event_type, event_time, detected_at
                FROM anomaly_events
                WHERE participant_id = ?
                  AND status = 'pending'
                """,
                (participant_id,),
            ).fetchall()
        ]
        asked = [
            dict(row)
            for row in conn.execute(
                """
                SELECT e.event_type, q.asked_time
                FROM anomaly_questions q
                JOIN anomaly_events e ON e.event_id = q.event_id
                WHERE q.participant_id = ?
                """,
                (participant_id,),
            ).fetchall()
        ]

        suppress_ids = []
        current = datetime.now(timezone.utc).astimezone()
        for event in pending:
            event_key = question_cooldown_key(event["event_type"])
            for question in asked:
                if question_cooldown_key(question["event_type"]) != event_key:
                    continue
                asked_at = parse_dt(question["asked_time"])
                cooldown_until = asked_at + timedelta(hours=QUESTION_COOLDOWN_HOURS)
                if asked_at <= current < cooldown_until:
                    suppress_ids.append((event["event_id"],))
                    break

        if suppress_ids:
            conn.executemany(
                "UPDATE anomaly_events SET status = 'suppressed' WHERE event_id = ?",
                suppress_ids,
            )


# イベント種別をクールダウン判定用の分類へ変換する。
def question_cooldown_key(event_type: str) -> str:
    if event_type.startswith("daily_steps_"):
        return "daily_steps"
    if event_type.startswith("sleep_minutes_"):
        return "sleep_minutes"
    if event_type == "heart_rate_low_steps_30min":
        return "low_steps_30min"
    if event_type == "heart_rate_step_increase_30min":
        return "step_increase_30min"
    return event_type


# 送信時刻に達した定時質問イベントを選ぶ。
def ready_scheduled_events(pending: list[dict[str, Any]]) -> list[dict[str, Any]]:
    current = datetime.now(timezone.utc).astimezone()
    ready = []
    for event in pending:
        if event["event_type"] not in SCHEDULED_EVENT_TYPES:
            continue
        if parse_dt(event["event_time"]) <= current:
            event = dict(event)
            event["sequence_event_ids"] = [event["event_id"]]
            ready.append(event)
    return ready


# 連続性を考慮して通知可能な区間イベントを選ぶ。
def ready_interval_events(participant_id: str, pending: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ready = []
    step_events = [event for event in pending if event["event_type"] in STEP_EVENT_TYPES]
    if step_events:
        event = ready_interval_sequence(participant_id, step_events, STEP_EVENT_TYPES)
        if event is not None:
            ready.append(event)

    for event in pending:
        if event["event_type"] != "heart_rate_sleep_x2":
            continue
        event = dict(event)
        event["event_end_time"] = to_iso(event_interval_end(event))
        event["sequence_event_ids"] = [event["event_id"]]
        ready.append(event)
    return ready


# 連続する区間イベントから通知対象を決める。
def ready_interval_sequence(
    participant_id: str,
    events: list[dict[str, Any]],
    event_types: set[str],
) -> dict[str, Any] | None:
    has_high_step = (
        event_types == STEP_EVENT_TYPES
        and any(event["event_type"] == "step_increase_30min" for event in events)
    )
    sequence_events = (
        [event for event in events if event["event_type"] == "step_increase_30min"]
        if has_high_step
        else events
    )
    events_by_time: dict[datetime, list[dict[str, Any]]] = {}
    for event in sequence_events:
        events_by_time.setdefault(parse_dt(event["event_time"]), []).append(event)

    start_time = min(events_by_time)
    sensor_times = sensor_timestamps_from(start_time, participant_id)
    if not sensor_times or sensor_times[0] != start_time:
        return None

    sequence: list[dict[str, Any]] = []
    ended = False
    ended_at_sensor_time: datetime | None = None
    seen_high_step = False
    for sensor_time in sensor_times:
        current_events = events_by_time.get(sensor_time)
        if current_events:
            sequence.extend(current_events)
            if any(event["event_type"] == "step_increase_30min" for event in current_events):
                seen_high_step = True
            if (
                event_types == STEP_EVENT_TYPES
                and not seen_high_step
                and low_step_sequence_minutes(sequence) >= 60
            ):
                ended = True
                break
            continue
        ended = True
        ended_at_sensor_time = sensor_time
        break

    if not ended or not sequence:
        return None

    if (
        event_types == STEP_EVENT_TYPES
        and not any(event["event_type"] == "step_increase_30min" for event in sequence)
        and low_step_sequence_minutes(sequence) < 60
    ):
        if ended_at_sensor_time is not None and sensor_row_is_low_steps(participant_id, ended_at_sensor_time):
            return None
        suppress_sequence_events(sequence)
        return None

    selected = select_interval_representative(sequence, event_types)
    selected = dict(selected)
    selected["event_time"] = to_iso(start_time)
    selected["event_end_time"] = to_iso(max(event_interval_end(event) for event in sequence))
    selected["sequence_event_ids"] = [event["event_id"] for event in sequence]
    return selected


# 指定時刻以降に存在するセンサーデータの時刻一覧を返す。
def sensor_timestamps_from(start_time: datetime, participant_id: str) -> list[datetime]:
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT timestamp
            FROM sensor_data
            WHERE participant_id = ?
              AND timestamp >= ?
            ORDER BY timestamp ASC
            """,
            (participant_id, to_iso(start_time)),
        ).fetchall()
    return [parse_dt(row["timestamp"]) for row in rows]


# 指定時刻のセンサーデータが低歩数か判定する。
def sensor_row_is_low_steps(participant_id: str, timestamp: datetime) -> bool:
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT steps
            FROM sensor_data
            WHERE participant_id = ?
              AND timestamp = ?
            """,
            (participant_id, to_iso(timestamp)),
        ).fetchone()
    if row is None or row["steps"] is None:
        return False
    return int(row["steps"]) < 200


# イベント区間の終了時刻を返す。
def event_interval_end(event: dict[str, Any]) -> datetime:
    if event.get("event_end_time"):
        return parse_dt(event["event_end_time"])
    return parse_dt(event["event_time"]) + timedelta(minutes=30)


# 連続イベント群から通知を代表するイベントを選ぶ。
def select_interval_representative(
    sequence: list[dict[str, Any]],
    event_types: set[str],
) -> dict[str, Any]:
    if event_types == STEP_EVENT_TYPES:
        high_steps = [event for event in sequence if event["event_type"] == "step_increase_30min"]
        if high_steps:
            return sorted(high_steps, key=lambda event: parse_dt(event["event_time"]))[0]
    return sorted(sequence, key=lambda event: parse_dt(event["event_time"]))[0]


# イベント列に含まれる指定種別の件数を数える。
def count_events(sequence: list[dict[str, Any]], event_type: str) -> int:
    return sum(1 for event in sequence if event["event_type"] == event_type)


# 連続イベント列が表す低歩数時間を分単位で返す。
def low_step_sequence_minutes(sequence: list[dict[str, Any]]) -> float:
    low_steps = [event for event in sequence if event["event_type"] == "low_steps_30min"]
    if not low_steps:
        return 0.0
    start = min(parse_dt(event["event_time"]) for event in low_steps)
    end = max(event_interval_end(event) for event in low_steps)
    return (end - start).total_seconds() / 60


# 代表以外の連続イベントを抑制済みにする。
def suppress_sequence_events(sequence: list[dict[str, Any]]) -> None:
    with get_connection() as conn:
        conn.executemany(
            "UPDATE anomaly_events SET status = 'suppressed' WHERE event_id = ?",
            [(event["event_id"],) for event in sequence],
        )


# 異常イベントをSlackで質問済みとして記録する。
def mark_asked(event: dict[str, Any], bot_id: str, asked_time: str) -> None:
    init_db()
    sequence_event_ids = list(event.get("sequence_event_ids") or [event["event_id"]])
    collapsed_ids = [event_id for event_id in sequence_event_ids if event_id != event["event_id"]]
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO anomaly_questions(event_id, participant_id, robot_id, asked_time, message)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                event["event_id"],
                event["participant_id"],
                bot_id,
                to_iso(parse_dt(asked_time)),
                event_message(event),
            ),
        )
        conn.execute("UPDATE anomaly_events SET status = 'asked' WHERE event_id = ?", (event["event_id"],))
        if collapsed_ids:
            conn.executemany(
                "UPDATE anomaly_events SET status = 'collapsed' WHERE event_id = ?",
                [(event_id,) for event_id in collapsed_ids],
            )


# Slack上の回答を異常イベントへ紐付けて保存する。
def save_anomaly_response(
    event_id: str,
    participant_id: str,
    bot_id: str,
    activity_text: str,
    feeling_text: str,
    response_time: str | None = None,
) -> None:
    init_db()
    saved_at = to_iso(parse_dt(response_time or now_iso()))
    with get_connection() as conn:
        event = conn.execute(
            "SELECT * FROM anomaly_events WHERE event_id = ? AND participant_id = ?",
            (event_id, participant_id),
        ).fetchone()
        if event is None:
            raise ValueError(f"event not found: {event_id}")

        question = conn.execute(
            """
            SELECT asked_time
            FROM anomaly_questions
            WHERE event_id = ?
            ORDER BY asked_time DESC
            LIMIT 1
            """,
            (event_id,),
        ).fetchone()
        latency = None
        if question is not None:
            latency = (parse_dt(saved_at) - parse_dt(question["asked_time"])).total_seconds()

        conn.execute(
            """
            INSERT INTO anomaly_responses(
                event_id, participant_id, robot_id, response_time,
                response_type, response_value, response_text, response_latency_sec
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                participant_id,
                bot_id,
                saved_at,
                "text",
                activity_text,
                feeling_text,
                latency,
            ),
        )
        conn.execute("UPDATE anomaly_events SET status = 'answered' WHERE event_id = ?", (event_id,))
        conn.execute("DELETE FROM slack_response_drafts WHERE event_id = ?", (event_id,))


# チャンネルに保存された回答途中の下書きを探す。
def find_draft(slack_workspace_id: str, slack_channel_id: str) -> dict[str, Any] | None:
    ensure_slack_tables()
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT *
            FROM slack_response_drafts
            WHERE slack_workspace_id = ?
              AND slack_channel_id = ?
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            (slack_workspace_id, slack_channel_id),
        ).fetchone()
    return dict(row) if row is not None else None


# 自由記述待ちの回答下書きを作成する。
def create_draft(
    event_id: str,
    participant_id: str,
    slack_workspace_id: str,
    slack_channel_id: str,
    activity_text: str,
) -> None:
    ensure_slack_tables()
    now = now_iso()
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO slack_response_drafts(
                event_id, participant_id, slack_workspace_id, slack_channel_id,
                activity_text, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(event_id) DO UPDATE SET
                slack_workspace_id = excluded.slack_workspace_id,
                slack_channel_id = excluded.slack_channel_id,
                activity_text = excluded.activity_text,
                updated_at = excluded.updated_at
            """,
            (event_id, participant_id, slack_workspace_id, slack_channel_id, activity_text, now, now),
        )


# 参加者の直近の未回答イベントを取得する。
def find_latest_unanswered_event(participant_id: str) -> dict[str, Any] | None:
    init_db()
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT e.*
            FROM anomaly_events e
            JOIN anomaly_questions q ON q.event_id = e.event_id
            WHERE e.participant_id = ?
              AND e.status = 'asked'
              AND NOT EXISTS (
                  SELECT 1 FROM anomaly_responses r WHERE r.event_id = e.event_id
              )
            ORDER BY q.asked_time DESC, e.event_time DESC
            LIMIT 1
            """,
            (participant_id,),
        ).fetchone()
    return dict(row) if row is not None else None


# イベントからSlackへ送る質問文を作る。
def question_text(event: dict[str, Any]) -> str:
    return event["message"]


# 選択回答後に送る体調確認の質問文を作る。
def feeling_question_text(event: dict[str, Any]) -> str:
    if event.get("event_type", "").startswith("daily_steps_"):
        return "1日を通して、どんな気持ちだった？"
    return "そのときどんな気持ちだった？"


# 参加者のDMチャンネルIDを取得する。
def resolve_dm_channel_id(client: Any, participant: SlackParticipant) -> str:
    if participant.slack_user_id:
        response = client.conversations_open(users=participant.slack_user_id)
        channel = response.get("channel", {})
        channel_id = channel.get("id")
        if not channel_id:
            raise RuntimeError(f"Slack DM channel could not be opened for {participant.participant_id}.")
        return str(channel_id)
    if participant.slack_channel_id:
        return participant.slack_channel_id
    raise RuntimeError(f"Slack destination is not configured for {participant.participant_id}.")


# 異常イベントの質問をSlackへ送信して送信済みにする。
def send_question(client: Any, participant: SlackParticipant, event: dict[str, Any], bot_id: str) -> None:
    channel_id = resolve_dm_channel_id(client, participant)
    client.chat_postMessage(
        channel=channel_id,
        text=question_text(event),
    )
    mark_asked(event, bot_id=bot_id, asked_time=now_iso())


# 全参加者を調べ、保留中の異常質問をSlackへ通知する。
def notify_pending_anomalies(
    participant_ids: set[str] | None = None,
    stale_detected_at_cutoff: datetime | None = None,
    suppress_interval_detected_since: datetime | None = None,
) -> dict[str, int]:
    workspaces = load_workspace_settings(require_app_token=False)
    if not workspaces:
        return {"sent": 0, "skipped": 0, "errors": 0}

    try:
        from slack_sdk import WebClient
    except ImportError:
        print("Slack notification skipped: slack_sdk is not installed.")
        return {"sent": 0, "skipped": 0, "errors": 1}

    result = {"sent": 0, "skipped": 0, "errors": 0}
    for settings in workspaces:
        participants = load_participants(settings.participant_map_path)
        if participant_ids is not None:
            participants = [p for p in participants if p.participant_id in participant_ids]

        client = WebClient(token=settings.bot_token)
        for participant in participants:
            event = get_ready_anomaly(
                participant.participant_id,
                stale_detected_at_cutoff=stale_detected_at_cutoff,
                suppress_interval_detected_since=suppress_interval_detected_since,
            )
            if event is None:
                result["skipped"] += 1
                continue
            try:
                send_question(client, participant, event, settings.bot_id)
                result["sent"] += 1
                print(
                    f"Slack anomaly question sent: {settings.workspace_id} "
                    f"{participant.participant_id} {event['event_id']}"
                )
            except Exception as exc:
                result["errors"] += 1
                print(
                    f"Slack anomaly question failed for {settings.workspace_id} "
                    f"{participant.participant_id}: {exc}"
                )
    return result


# Slackメッセージの送信元に対応する参加者を特定する。
def participant_for_message(
    settings: SlackSettings,
    slack_channel_id: str,
    slack_user_id: str | None = None,
) -> SlackParticipant | None:
    for participant in load_participants(settings.participant_map_path):
        if participant.slack_channel_id == slack_channel_id:
            return participant
        if slack_user_id and participant.slack_user_id == slack_user_id:
            return participant
    return None


# Slackのメッセージ・アクション処理を登録する。
def register_handlers(slack_app: Any, settings: SlackSettings) -> None:
    @slack_app.event("message")
    def handle_dm_message(body: dict[str, Any], client: Any, logger: Any) -> None:
        event = body.get("event", {})
        if event.get("channel_type") != "im" or event.get("bot_id"):
            return

        channel = event.get("channel")
        user = event.get("user")
        text = str(event.get("text", "")).strip()
        if not channel or not text:
            return

        participant = participant_for_message(settings, channel, user)
        if participant is None:
            return

        draft = find_draft(settings.workspace_id, channel)
        if draft is not None:
            try:
                save_anomaly_response(
                    event_id=draft["event_id"],
                    participant_id=draft["participant_id"],
                    bot_id=settings.bot_id,
                    activity_text=draft["activity_text"],
                    feeling_text=text,
                )
                client.chat_postMessage(channel=channel, text="ありがとう、記録したよ。")
            except Exception as exc:
                logger.error(f"Failed to save anomaly response: {exc}")
                client.chat_postMessage(channel=channel, text="ごめん、記録に失敗したみたい。少し待ってもう一度送って。")
            return

        asked_event = find_latest_unanswered_event(participant.participant_id)
        if asked_event is None:
            return

        try:
            create_draft(
                event_id=asked_event["event_id"],
                participant_id=participant.participant_id,
                slack_workspace_id=settings.workspace_id,
                slack_channel_id=channel,
                activity_text=text,
            )
            client.chat_postMessage(channel=channel, text=feeling_question_text(asked_event))
        except Exception as exc:
            logger.error(f"Failed to create Slack response draft: {exc}")
            client.chat_postMessage(channel=channel, text="ごめん、記録に失敗したみたい。少し待ってもう一度送って。")


# 各ワークスペースのSocket Mode Botを起動する。
def run_socket_mode_bot() -> None:
    workspaces = load_workspace_settings(require_app_token=True)
    if not workspaces:
        raise RuntimeError("SLACK_BOT_TOKEN is required to run the Slack bot.")

    try:
        from slack_bolt import App
        from slack_bolt.adapter.socket_mode import SocketModeHandler
    except ImportError as exc:
        raise RuntimeError("slack-bolt is not installed. Install backend/requirements.txt first.") from exc

    ensure_slack_tables()
    handlers = []
    for settings in workspaces:
        slack_app = App(token=settings.bot_token)
        register_handlers(slack_app, settings)
        handler = SocketModeHandler(slack_app, settings.app_token)
        handlers.append((settings, handler))

    if len(handlers) == 1:
        settings, handler = handlers[0]
        print(f"Starting Slack anomaly bot in Socket Mode: {settings.workspace_id}")
        handler.start()
        return

    try:
        for settings, handler in handlers:
            handler.connect()
            print(f"Starting Slack anomaly bot in Socket Mode: {settings.workspace_id}")
        threading.Event().wait()
    finally:
        for _settings, handler in handlers:
            handler.close()


# 実行モードに応じて通知処理またはBotを開始する。
def main() -> None:
    parser = argparse.ArgumentParser(description="Slack bot for anomaly confirmation.")
    parser.add_argument("--notify-once", action="store_true", help="Send one pending anomaly question per mapped participant and exit.")
    args = parser.parse_args()

    if args.notify_once:
        result = notify_pending_anomalies()
        print(f"Slack notification result: {result}")
        return

    run_socket_mode_bot()


if __name__ == "__main__":
    main()
