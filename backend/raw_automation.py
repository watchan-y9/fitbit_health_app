from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

from app.google_health_client import JST, fetch_google_health_batch
from app.main import save_raw_sensor_data, save_raw_sleep_data
from automation import DEFAULT_CONFIG, load_config


# 全参加者のGoogle Health生データ取得を1回実行する。
def run_once(config_path: Path, max_pages: int, dry_run: bool = False) -> dict[str, int]:
    config = load_config(config_path)
    started_at = datetime.now(JST)
    print(f"[{started_at.isoformat(timespec='seconds')}] raw automation cycle started")
    raw_sensor_total = 0
    raw_sleep_total = 0
    for participant in config.participants:
        if participant.source != "google_health":
            continue
        try:
            batch = fetch_google_health_batch(
                participant_id=participant.participant_id,
                env_prefix=participant.env_prefix,
                target_date=started_at.date(),
                max_pages=max_pages,
                include_raw=True,
            )
        except Exception as exc:
            print(f"{participant.participant_id}: raw Google Health import failed: {exc}")
            continue

        if not dry_run:
            for payload in batch.raw_sensor_rows:
                save_raw_sensor_data(payload)
            for payload in batch.raw_sleep_rows:
                save_raw_sleep_data(payload)
        raw_sensor_total += len(batch.raw_sensor_rows)
        raw_sleep_total += len(batch.raw_sleep_rows)
        print(
            f"{participant.participant_id}: imported "
            f"{len(batch.raw_sensor_rows)} raw sensor, {len(batch.raw_sleep_rows)} raw sleep rows"
        )
    finished_at = datetime.now(JST)
    print(f"[{finished_at.isoformat(timespec='seconds')}] raw automation cycle finished")
    return {"raw_sensor_rows": raw_sensor_total, "raw_sleep_rows": raw_sleep_total}


# 15分ごとに生データ取得を繰り返す。
def run_forever(config_path: Path, max_pages: int, dry_run: bool = False) -> None:
    print("Starting raw automation on JST quarter-hour boundaries.")
    while True:
        sleep_seconds = seconds_until_next_quarter_hour()
        next_run = datetime.now(JST) + timedelta(seconds=sleep_seconds)
        print(f"Sleeping until {next_run.strftime('%Y-%m-%d %H:%M:%S %Z')}. Press Ctrl+C to stop.")
        time.sleep(sleep_seconds)
        run_once(config_path, max_pages=max_pages, dry_run=dry_run)


# 次の毎時0分、15分、30分、45分までの待機秒数を返す。
def seconds_until_next_quarter_hour(now: datetime | None = None) -> float:
    current = (now or datetime.now(JST)).astimezone(JST)
    interval_minutes = 15
    next_minute = ((current.minute // interval_minutes) + 1) * interval_minutes
    if next_minute < 60:
        next_time = current.replace(minute=next_minute, second=0, microsecond=0)
    else:
        next_hour = current + timedelta(hours=1)
        next_time = next_hour.replace(minute=0, second=0, microsecond=0)
    return max((next_time - current).total_seconds(), 0.0)


# コマンドライン引数を解析して生データ取得を開始する。
def main() -> None:
    parser = argparse.ArgumentParser(description="Continuously import raw Google Health data without anomaly detection.")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="Path to automation JSON config.")
    parser.add_argument("--max-pages", type=int, default=20)
    parser.add_argument("--once", action="store_true", help="Run one cycle and exit.")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    config_path = Path(args.config).resolve()
    if str(config_path.parent.parent) not in sys.path:
        sys.path.insert(0, str(config_path.parent.parent))
    if args.once:
        run_once(config_path, max_pages=args.max_pages, dry_run=args.dry_run)
    else:
        run_forever(config_path, max_pages=args.max_pages, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
