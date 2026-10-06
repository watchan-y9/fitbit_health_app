from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from app.fitbit_client import fetch_fitbit_sensor_rows
from app.google_health_client import JST, fetch_google_health_batch, fetch_google_health_sensor_rows, parse_timestamp_value
from app.database import get_connection, init_db
from app.main import detect_anomalies, save_sensor_data, save_sleep_data, save_sleep_stage_data
from app.schemas import SensorDataIn


ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT_DIR / "config" / "automation.example.json"
INTERVAL_MINUTES = 15
PROGRAM_STARTED_AT = datetime.now(JST)


# 参加者ごとのデータ取得・基準値設定を保持する。
@dataclass(frozen=True)
class ParticipantConfig:
    participant_id: str
    source: str
    sex: str | None = None
    age: int | None = None
    csv_path: Path | None = None
    env_prefix: str = "FITBIT"
    detail_level: str = "30min"
    backfill_days: int = 7


# 自動処理全体の参加者一覧と実行間隔を保持する。
@dataclass(frozen=True)
class AutomationConfig:
    interval_minutes: int
    detect_after_import: bool
    participants: list[ParticipantConfig]


# JSON設定を読み込み、環境変数を補完して実行設定へ変換する。
def load_config(path: Path) -> AutomationConfig:
    raw = json.loads(path.read_text(encoding="utf-8"))
    requested_interval = int(raw.get("interval_minutes", INTERVAL_MINUTES))
    interval = INTERVAL_MINUTES
    if requested_interval != INTERVAL_MINUTES:
        print(
            f"interval_minutes={requested_interval} is not aligned with the research schedule. "
            f"Using {INTERVAL_MINUTES} minutes instead."
        )

    participants = []
    for entry in raw.get("participants", []):
        source = str(entry.get("source", "csv")).lower()
        csv_path = entry.get("csv_path")
        requested_detail_level = str(entry.get("detail_level", "30min"))
        detail_level = "30min"
        if requested_detail_level != detail_level:
            print(
                f"{entry['participant_id']}: detail_level={requested_detail_level} is not used. "
                f"Using {detail_level} instead."
            )
        participants.append(
            ParticipantConfig(
                participant_id=str(entry["participant_id"]),
                sex=entry.get("sex"),
                age=int(entry["age"]) if entry.get("age") is not None else None,
                source=source,
                csv_path=(ROOT_DIR / csv_path).resolve() if csv_path else None,
                env_prefix=str(entry.get("env_prefix", "FITBIT")),
                detail_level=detail_level,
                backfill_days=int(entry.get("backfill_days", 7)),
            )
        )

    if not participants:
        raise ValueError("No participants configured.")

    return AutomationConfig(
        interval_minutes=interval,
        detect_after_import=bool(raw.get("detect_after_import", True)),
        participants=participants,
    )


# CSVのセンサーデータをAPIへ一括登録する。
def import_csv(participant: ParticipantConfig, dry_run: bool = False) -> int:
    if participant.csv_path is None:
        raise ValueError(f"{participant.participant_id}: csv_path is required.")
    if not participant.csv_path.exists():
        print(f"{participant.participant_id}: CSV not found, skipped: {participant.csv_path}")
        return 0

    imported = 0
    with participant.csv_path.open(newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            row_participant_id = row.get("participant_id") or participant.participant_id
            if row_participant_id != participant.participant_id:
                continue
            payload = SensorDataIn(
                participant_id=participant.participant_id,
                timestamp=row["timestamp"],
                heart_rate=_optional_float(row.get("heart_rate")),
                max_heart_rate=_optional_float(row.get("max_heart_rate")),
                steps=_optional_int(row.get("steps")),
                calories=_optional_float(row.get("calories")),
                active_minutes=_optional_float(row.get("active_minutes")),
            )
            if not dry_run:
                save_sensor_data(payload)
            imported += 1

    print(f"{participant.participant_id}: imported {imported} rows from {participant.csv_path.name}")
    return imported


# Fitbitから対象期間のデータを取得してAPIへ登録する。
def import_fitbit(participant: ParticipantConfig, dry_run: bool = False, initial_backfill: bool = False) -> int:
    if initial_backfill:
        total = 0
        for target_date in startup_backfill_dates():
            rows = fetch_fitbit_sensor_rows(
                participant_id=participant.participant_id,
                env_prefix=participant.env_prefix,
                target_date=target_date,
                detail_level=participant.detail_level,
            )
            for payload in rows:
                if not dry_run:
                    save_sensor_data(payload)
            total += len(rows)
            print(
                f"{participant.participant_id}: imported {len(rows)} rows "
                f"from Fitbit ({participant.detail_level}) for {target_date.isoformat()}"
            )
        return total

    rows = fetch_fitbit_sensor_rows(
        participant_id=participant.participant_id,
        env_prefix=participant.env_prefix,
        detail_level=participant.detail_level,
    )
    for payload in rows:
        if not dry_run:
            save_sensor_data(payload)
    print(
        f"{participant.participant_id}: imported {len(rows)} rows "
        f"from Fitbit ({participant.detail_level})"
    )
    return len(rows)


# Google Healthから対象期間のデータを取得してAPIへ登録する。
def import_google_health(participant: ParticipantConfig, dry_run: bool = False, initial_backfill: bool = False) -> int:
    if not dry_run:
        ensure_participant_profile(participant)
    dates = startup_backfill_dates() if initial_backfill else backfill_dates(participant)
    imported = 0
    for target_date in dates:
        batch = fetch_google_health_batch(
            participant_id=participant.participant_id,
            env_prefix=participant.env_prefix,
            target_date=target_date,
            include_raw=False,
        )
        for payload in batch.sensor_rows:
            if not dry_run:
                save_sensor_data(payload)
        for payload in batch.sleep_rows:
            if not dry_run:
                save_sleep_data(payload)
        for payload in batch.sleep_stage_rows:
            if not dry_run:
                save_sleep_stage_data(payload)
        imported += (
            len(batch.sensor_rows)
            + len(batch.sleep_rows)
            + len(batch.sleep_stage_rows)
        )
        print(
            f"{participant.participant_id}: {target_date.isoformat()} imported "
            f"{len(batch.sensor_rows)} sensor, {len(batch.sleep_rows)} sleep, "
            f"{len(batch.sleep_stage_rows)} sleep-stage rows"
        )
    print(
        f"{participant.participant_id}: imported {imported} total rows from Google Health "
        f"for {len(dates)} day(s)"
    )
    return imported


# 参加者の属性情報と個人基準値をデータベースへ反映する。
def ensure_participant_profile(participant: ParticipantConfig) -> None:
    init_db()
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO participants(participant_id, display_name, sex, age)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(participant_id) DO UPDATE SET
                sex = COALESCE(excluded.sex, participants.sex),
                age = COALESCE(excluded.age, participants.age)
            """,
            (participant.participant_id, participant.participant_id, participant.sex, participant.age),
        )


# 初回起動時に遡って取得する日付一覧を作る。
def startup_backfill_dates() -> list[date]:
    today = PROGRAM_STARTED_AT.astimezone(JST).date()
    return [today - timedelta(days=1), today]


# 参加者設定に基づいて通常の取得対象日を決める。
def backfill_dates(participant: ParticipantConfig) -> list[date]:
    init_db()
    today = datetime.now(JST).date()
    earliest = today - timedelta(days=max(participant.backfill_days, 1))
    with get_connection() as conn:
        latest_sensor = conn.execute(
            "SELECT max(timestamp) AS value FROM sensor_data WHERE participant_id = ?",
            (participant.participant_id,),
        ).fetchone()["value"]
        latest_sleep = conn.execute(
            "SELECT max(start_time) AS value FROM sleep_data WHERE participant_id = ?",
            (participant.participant_id,),
        ).fetchone()["value"]

    latest_dates = []
    for value in (latest_sensor, latest_sleep):
        parsed = parse_timestamp_value(value, today, None) if value else None
        if parsed:
            latest_dates.append(parsed.astimezone(JST).date())

    start_date = min(latest_dates) if latest_dates else earliest
    start_date = max(start_date - timedelta(days=1), earliest)
    days = (today - start_date).days
    return [start_date + timedelta(days=offset) for offset in range(days + 1)]


# 全参加者の取得・異常検知・Slack通知を1回実行する。
def run_once(config: AutomationConfig, dry_run: bool = False, initial_backfill: bool = False) -> dict[str, Any]:
    cycle_started_at = datetime.now(JST)
    print(f"[{cycle_started_at.isoformat(timespec='seconds')}] automation cycle started")

    imported_total = 0
    for participant in config.participants:
        if participant.source == "csv":
            imported_total += import_csv(participant, dry_run=dry_run)
        elif participant.source == "fitbit":
            try:
                imported_total += import_fitbit(participant, dry_run=dry_run, initial_backfill=initial_backfill)
            except Exception as exc:
                print(f"{participant.participant_id}: Fitbit import failed: {exc}")
        elif participant.source == "google_health":
            try:
                imported_total += import_google_health(participant, dry_run=dry_run, initial_backfill=initial_backfill)
            except Exception as exc:
                print(f"{participant.participant_id}: Google Health import failed: {exc}")
        else:
            print(f"{participant.participant_id}: unsupported source '{participant.source}', skipped")

    detected = {"detected_count": 0, "events": []}
    if config.detect_after_import and not dry_run:
        detected = detect_anomalies()
        print(f"detected_count: {detected['detected_count']}")
    elif dry_run:
        print("dry-run: anomaly detection was not written to the database")

    slack_notifications = {"sent": 0, "skipped": 0, "errors": 0}
    if not dry_run:
        slack_notifications = notify_pending_slack_questions(
            {participant.participant_id for participant in config.participants},
            stale_detected_at_cutoff=PROGRAM_STARTED_AT - timedelta(hours=2),
            suppress_interval_detected_since=cycle_started_at if initial_backfill else None,
        )
        if any(slack_notifications.values()):
            print(f"slack_notifications: {slack_notifications}")

    finished_at = datetime.now().isoformat(timespec="seconds")
    print(f"[{finished_at}] automation cycle finished")
    return {"imported_rows": imported_total, "slack_notifications": slack_notifications, "detected": detected}


# 未通知の異常を参加者ごとのSlackワークスペースへ送る。
def notify_pending_slack_questions(
    participant_ids: set[str],
    stale_detected_at_cutoff: datetime | None = None,
    suppress_interval_detected_since: datetime | None = None,
) -> dict[str, int]:
    root = str(ROOT_DIR)
    if root not in sys.path:
        sys.path.insert(0, root)

    try:
        from slack_bridge.anomaly_bot import notify_pending_anomalies
    except Exception as exc:
        print(f"Slack notification skipped: {exc}")
        return {"sent": 0, "skipped": 0, "errors": 1}

    try:
        return notify_pending_anomalies(
            participant_ids=participant_ids,
            stale_detected_at_cutoff=stale_detected_at_cutoff,
            suppress_interval_detected_since=suppress_interval_detected_since,
        )
    except Exception as exc:
        print(f"Slack notification failed: {exc}")
        return {"sent": 0, "skipped": 0, "errors": 1}


# 指定間隔で自動処理を繰り返す。
def run_forever(config: AutomationConfig, dry_run: bool = False) -> None:
    print(
        f"Starting automation every {config.interval_minutes} minutes on JST quarter-hour boundaries "
        f"for {len(config.participants)} participant(s)."
    )
    run_once(config, dry_run=dry_run, initial_backfill=True)
    while True:
        sleep_seconds = seconds_until_next_quarter_hour()
        next_run = datetime.now(JST) + timedelta(seconds=sleep_seconds)
        print(f"Sleeping until {next_run.strftime('%Y-%m-%d %H:%M:%S %Z')}. Press Ctrl+C to stop.")
        time.sleep(sleep_seconds)
        run_once(config, dry_run=dry_run, initial_backfill=False)


# 次の毎時0分、15分、30分、45分までの待機秒数を返す。
def seconds_until_next_quarter_hour(now: datetime | None = None) -> float:
    current = (now or datetime.now(JST)).astimezone(JST)
    next_minute = ((current.minute // INTERVAL_MINUTES) + 1) * INTERVAL_MINUTES
    if next_minute < 60:
        next_time = current.replace(minute=next_minute, second=0, microsecond=0)
    else:
        next_hour = current + timedelta(hours=1)
        next_time = next_hour.replace(minute=0, second=0, microsecond=0)
    return max((next_time - current).total_seconds(), 0.0)


# 空文字を許容しながら文字列を小数へ変換する。
def _optional_float(value: str | None) -> float | None:
    if value is None or value == "":
        return None
    return float(value)


# 空文字を許容しながら文字列を整数へ変換する。
def _optional_int(value: str | None) -> int | None:
    if value is None or value == "":
        return None
    return int(float(value))


# コマンドライン引数を解析して自動処理を開始する。
def main() -> None:
    parser = argparse.ArgumentParser(description="Automatically import sensor CSVs and detect anomalies.")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="Path to automation JSON config.")
    parser.add_argument("--once", action="store_true", help="Run one cycle and exit.")
    parser.add_argument("--dry-run", action="store_true", help="Read configured sources without writing to DB.")
    args = parser.parse_args()

    config = load_config(Path(args.config).resolve())
    if args.once:
        run_once(config, dry_run=args.dry_run, initial_backfill=True)
    else:
        run_forever(config, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
