from __future__ import annotations

import argparse
import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from app.database import get_connection, init_db
from app.google_health_client import (
    HEALTH_BASE_URL,
    JST,
    credentials_from_env,
    data_type_candidates,
    extract_numeric_value,
    extract_text_value,
    fetch_steps,
    get_data_points,
    parse_data_point_timestamp,
    parse_end_timestamp,
    raw_sensor_rows_from_points,
    refresh_access_token,
    short_error,
)
from app.main import save_raw_sensor_data, save_raw_sleep_data
from app.schemas import RawSleepDataIn
from automation import DEFAULT_CONFIG, ParticipantConfig, load_config


# 引数と設定を読み込み、参加者ごとの過去データ補完を開始する。
def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill raw Google Health sensor/sleep data without anomaly detection or Slack notifications."
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--start", type=str, help="Start date, YYYY-MM-DD. Defaults to earliest saved data date.")
    parser.add_argument("--end", type=str, help="End date, YYYY-MM-DD. Defaults to today.")
    parser.add_argument("--participant", action="append", help="Participant ID to backfill. Repeatable.")
    parser.add_argument("--max-pages", type=int, default=200)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    init_db()
    config = load_config(args.config)
    requested = set(args.participant or [])
    participants = [
        participant
        for participant in config.participants
        if participant.source == "google_health" and (not requested or participant.participant_id in requested)
    ]
    if not participants:
        raise ValueError("No matching Google Health participants found.")

    total_raw_sensor = 0
    total_raw_sleep = 0
    for participant in participants:
        start = parse_date(args.start) if args.start else earliest_saved_date(participant)
        end = parse_date(args.end) if args.end else datetime.now(JST).date()
        if start is None:
            print(f"{participant.participant_id}: no existing date range found, skipped")
            continue
        if end < start:
            print(f"{participant.participant_id}: empty date range {start}..{end}, skipped")
            continue
        raw_sensor, raw_sleep = backfill_participant(
            participant,
            start,
            end,
            max_pages=args.max_pages,
            dry_run=args.dry_run,
        )
        total_raw_sensor += raw_sensor
        total_raw_sleep += raw_sleep

    print(
        f"raw backfill finished: {total_raw_sensor} raw sensor rows, "
        f"{total_raw_sleep} raw sleep rows"
    )


# 指定参加者の心拍・睡眠生データを日単位で遡って保存する。
def backfill_participant(
    participant: ParticipantConfig,
    start: date,
    end: date,
    max_pages: int,
    dry_run: bool,
) -> tuple[int, int]:
    credentials = credentials_from_env(participant.env_prefix)
    access_token = refresh_access_token(credentials)
    raw_sensor_count = 0
    raw_sleep_count = 0

    heart_by_time = fetch_raw_heart_points(
        access_token,
        participant.env_prefix,
        start,
        end,
        max_pages=max_pages,
    )
    sleep_rows = fetch_raw_sleep_rows(
        access_token,
        participant.participant_id,
        participant.env_prefix,
        start,
        end,
        max_pages=max_pages,
    )
    if not dry_run:
        for payload in raw_sensor_rows_from_points(participant.participant_id, {}, heart_by_time):
            save_raw_sensor_data(payload)
        for payload in sleep_rows:
            save_raw_sleep_data(payload)
    heart_row_count = len(raw_sensor_rows_from_points(participant.participant_id, {}, heart_by_time))
    raw_sensor_count += heart_row_count
    raw_sleep_count += len(sleep_rows)
    print(
        f"{participant.participant_id}: {start.isoformat()}..{end.isoformat()} "
        f"{heart_row_count} raw heart 1min, {len(sleep_rows)} raw sleep"
    )

    current = start
    while current <= end:
        try:
            steps_by_time = fetch_steps(access_token, current, participant.env_prefix)
        except Exception as exc:
            print(f"{participant.participant_id}: {current.isoformat()} raw step backfill failed: {exc}")
            current += timedelta(days=1)
            continue

        step_rows = raw_sensor_rows_from_points(participant.participant_id, steps_by_time, {})
        if not dry_run:
            for payload in step_rows:
                save_raw_sensor_data(payload)
        raw_sensor_count += len(step_rows)
        print(f"{participant.participant_id}: {current.isoformat()} {len(step_rows)} raw steps 1min")
        current += timedelta(days=1)
    return raw_sensor_count, raw_sleep_count


# Google Healthから未加工の心拍データ点を取得する。
def fetch_raw_heart_points(
    access_token: str,
    env_prefix: str,
    start: date,
    end: date,
    max_pages: int,
) -> dict[datetime, float]:
    for candidate in data_type_candidates(env_prefix, "HEART_RATE", ["heart-rate", "heart_rate", "heartRate", "heart_rate_bpm"]):
        try:
            payload = get_data_points(
                f"{HEALTH_BASE_URL}/users/me/dataTypes/{candidate}/dataPoints",
                access_token,
                params={"pageSize": 1000},
                max_pages=max_pages,
            )
        except RuntimeError as exc:
            print(f"heart rate candidate '{candidate}' skipped: {short_error(exc)}")
            continue

        values: dict[datetime, float] = {}
        for point in payload.get("dataPoints", []):
            timestamp = parse_data_point_timestamp(point, start, candidate)
            if timestamp is None:
                continue
            day = timestamp.astimezone(JST).date()
            if day < start or day > end:
                continue
            value = extract_numeric_value(point, candidate, ["bpm", "heart_rate", "beatsPerMinute", "value"])
            if value is None:
                continue
            values[timestamp] = float(value)
        print(f"heart data type '{candidate}' returned {len(values)} raw points in range")
        return values
    return {}


# Google Healthから未加工の睡眠データを取得する。
def fetch_raw_sleep_rows(
    access_token: str,
    participant_id: str,
    env_prefix: str,
    start: date,
    end: date,
    max_pages: int,
) -> list[RawSleepDataIn]:
    for candidate in data_type_candidates(env_prefix, "SLEEP", ["sleep", "sleep_session", "sleepSession"]):
        try:
            payload = get_data_points(
                f"{HEALTH_BASE_URL}/users/me/dataTypes/{candidate}/dataPoints",
                access_token,
                params={"pageSize": 1000},
                max_pages=max_pages,
            )
        except RuntimeError as exc:
            print(f"sleep candidate '{candidate}' skipped: {short_error(exc)}")
            continue

        rows = []
        for point in payload.get("dataPoints", []):
            start_time = parse_data_point_timestamp(point, start, candidate)
            if start_time is None:
                continue
            day = start_time.astimezone(JST).date()
            if day < start or day > end:
                continue
            end_time = parse_end_timestamp(point, start, candidate)
            minutes = extract_numeric_value(point, candidate, ["minutes", "duration_minutes", "value"])
            if minutes is None and end_time is not None:
                minutes = (end_time - start_time).total_seconds() / 60
            rows.append(
                RawSleepDataIn(
                    participant_id=participant_id,
                    start_time=start_time,
                    end_time=end_time,
                    data_type=candidate,
                    stage=extract_text_value(point, candidate, ["stage", "sleep_stage", "type"]),
                    value=minutes,
                    raw_json=json.dumps(point, ensure_ascii=False, sort_keys=True),
                    source="google_health",
                )
            )
        print(f"{participant_id}: sleep data type '{candidate}' returned {len(rows)} rows in range")
        return rows
    return []


# 参加者について保存済みの最古データ日を調べる。
def earliest_saved_date(participant: ParticipantConfig) -> date | None:
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT min(value) AS value
            FROM (
                SELECT min(timestamp) AS value
                FROM sensor_data
                WHERE participant_id = ?
                UNION ALL
                SELECT min(start_time) AS value
                FROM sleep_data
                WHERE participant_id = ?
            )
            WHERE value IS NOT NULL
            """,
            (participant.participant_id, participant.participant_id),
        ).fetchone()
    if row is None or row["value"] is None:
        return None
    return datetime.fromisoformat(str(row["value"])).astimezone(JST).date()


# ISO形式の文字列を日付へ変換する。
def parse_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


if __name__ == "__main__":
    main()
