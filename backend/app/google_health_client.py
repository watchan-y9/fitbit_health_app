from __future__ import annotations

import os
import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from collections import defaultdict
from typing import Any

import httpx
from dotenv import load_dotenv

from .schemas import RawSensorDataIn, RawSleepDataIn, SensorDataIn, SleepDataIn, SleepStageDataIn


ROOT_DIR = Path(__file__).resolve().parents[2]
ENV_PATH = ROOT_DIR / ".env"
TOKEN_URL = "https://oauth2.googleapis.com/token"
HEALTH_BASE_URL = "https://health.googleapis.com/v4"
JST = timezone(timedelta(hours=9))


# Google Health APIの認証情報を保持する。
@dataclass(frozen=True)
class GoogleHealthCredentials:
    client_id: str
    client_secret: str
    refresh_token: str


# Google Healthから取得した各種データを一括で保持する。
@dataclass(frozen=True)
class GoogleHealthBatch:
    sensor_rows: list[SensorDataIn]
    raw_sensor_rows: list[RawSensorDataIn]
    raw_sleep_rows: list[RawSleepDataIn]
    sleep_rows: list[SleepDataIn]
    sleep_stage_rows: list[SleepStageDataIn]


# 環境変数からGoogle Healthの認証情報を組み立てる。
def credentials_from_env(env_prefix: str = "GOOGLE") -> GoogleHealthCredentials:
    load_dotenv(ENV_PATH)
    prefix = env_prefix.upper().rstrip("_")
    client_id = os.getenv(f"{prefix}_CLIENT_ID")
    client_secret = os.getenv(f"{prefix}_CLIENT_SECRET")
    refresh_token = os.getenv(f"{prefix}_REFRESH_TOKEN")

    if not client_id or not client_secret or not refresh_token:
        raise ValueError(
            f"Missing Google Health credentials. Set {prefix}_CLIENT_ID, "
            f"{prefix}_CLIENT_SECRET, and {prefix}_REFRESH_TOKEN in .env."
        )

    return GoogleHealthCredentials(
        client_id=client_id,
        client_secret=client_secret,
        refresh_token=refresh_token,
    )


# 指定日のGoogle Healthデータを30分単位の行へ変換する。
def fetch_google_health_sensor_rows(
    participant_id: str,
    env_prefix: str = "GOOGLE",
    target_date: date | None = None,
) -> list[SensorDataIn]:
    return fetch_google_health_batch(
        participant_id=participant_id,
        env_prefix=env_prefix,
        target_date=target_date,
        include_raw=False,
    ).sensor_rows


# 生データ点から保存用のセンサー行を作る。
def raw_sensor_rows_from_points(
    participant_id: str,
    steps_by_time: dict[datetime, int],
    heart_by_time: dict[datetime, float],
) -> list[RawSensorDataIn]:
    steps_by_minute: dict[datetime, int] = defaultdict(int)
    heart_by_minute: dict[datetime, list[float]] = defaultdict(list)
    for timestamp, steps in steps_by_time.items():
        steps_by_minute[floor_to_minute(timestamp)] += int(steps)
    for timestamp, heart_rate in heart_by_time.items():
        heart_by_minute[floor_to_minute(timestamp)].append(float(heart_rate))

    rows = [
        RawSensorDataIn(
            participant_id=participant_id,
            timestamp=timestamp,
            data_type="steps",
            value=float(steps),
            source="google_health_1min",
        )
        for timestamp, steps in sorted(steps_by_minute.items())
    ]
    rows.extend(
        RawSensorDataIn(
            participant_id=participant_id,
            timestamp=timestamp,
            data_type="heart_rate",
            value=round(sum(values) / len(values), 1),
            source="google_health_1min",
        )
        for timestamp, values in sorted(heart_by_minute.items())
        if values
    )
    return rows


# 歩数・心拍・睡眠をまとめてGoogle Healthから取得する。
def fetch_google_health_batch(
    participant_id: str,
    env_prefix: str = "GOOGLE",
    target_date: date | None = None,
    allow_unfiltered_backfill: bool = False,
    max_pages: int | None = None,
    include_raw: bool = True,
) -> GoogleHealthBatch:
    if target_date is None:
        target_date = datetime.now(JST).date()

    credentials = credentials_from_env(env_prefix)
    access_token = refresh_access_token(credentials)
    steps_by_time = fetch_steps(access_token, target_date, env_prefix)
    heart_by_time = fetch_heart_rate(
        access_token,
        target_date,
        env_prefix,
        allow_unfiltered_backfill=allow_unfiltered_backfill,
        max_pages=max_pages,
    )
    sleep_rows, sleep_stage_rows, raw_sleep_rows = fetch_sleep(
        access_token,
        target_date,
        participant_id,
        env_prefix,
        allow_unfiltered_backfill=allow_unfiltered_backfill,
        max_pages=max_pages,
        include_raw=include_raw,
    )
    if target_date == datetime.now(JST).date():
        previous_sleep_rows, previous_stage_rows, previous_raw_sleep_rows = fetch_sleep(
            access_token,
            target_date - timedelta(days=1),
            participant_id,
            env_prefix,
            allow_unfiltered_backfill=allow_unfiltered_backfill,
            max_pages=max_pages,
            include_raw=include_raw,
        )
        sleep_rows.extend(previous_sleep_rows)
        sleep_stage_rows.extend(previous_stage_rows)
        raw_sleep_rows.extend(previous_raw_sleep_rows)

    sensor_rows = sliding_sensor_rows(participant_id, target_date, steps_by_time, heart_by_time)
    raw_sensor_rows = raw_sensor_rows_from_points(participant_id, steps_by_time, heart_by_time) if include_raw else []
    return GoogleHealthBatch(
        sensor_rows=sensor_rows,
        raw_sensor_rows=raw_sensor_rows,
        raw_sleep_rows=raw_sleep_rows if include_raw else [],
        sleep_rows=sleep_rows,
        sleep_stage_rows=sleep_stage_rows,
    )


# 取得データを時間窓ごとのセンサー行へ集約する。
def sliding_sensor_rows(
    participant_id: str,
    target_date: date,
    steps_by_time: dict[datetime, int],
    heart_by_time: dict[datetime, float],
    window_minutes: int = 30,
    shift_minutes: int = 15,
) -> list[SensorDataIn]:
    rows = []
    current_time = datetime.now(timezone.utc)
    day_start = datetime.combine(target_date, datetime.min.time(), tzinfo=JST).astimezone(timezone.utc)
    windows_per_day = int(24 * 60 / shift_minutes)
    step_points = sorted(steps_by_time.items())
    heart_points = sorted(heart_by_time.items())
    for offset in range(windows_per_day):
        window_start = day_start + timedelta(minutes=offset * shift_minutes)
        window_end = window_start + timedelta(minutes=window_minutes)
        if window_end > current_time:
            continue
        steps_values = [
            int(steps)
            for timestamp, steps in step_points
            if window_start <= timestamp.astimezone(timezone.utc) < window_end
        ]
        heart_values = [
            float(heart_rate)
            for timestamp, heart_rate in heart_points
            if window_start <= timestamp.astimezone(timezone.utc) < window_end
        ]
        if not steps_values and not heart_values:
            continue
        steps = sum(steps_values) if steps_values else None
        heart_rate = round(sum(heart_values) / len(heart_values), 1) if heart_values else None
        max_heart_rate = max(heart_values) if heart_values else None
        rows.append(
            SensorDataIn(
                participant_id=participant_id,
                timestamp=window_start,
                heart_rate=heart_rate,
                max_heart_rate=max_heart_rate,
                steps=steps,
                calories=None,
                active_minutes=30.0 if (steps or 0) > 20 or (heart_rate or 0) >= 100 else 0.0,
            )
        )
    return rows


# リフレッシュトークンを使ってアクセストークンを更新する。
def refresh_access_token(credentials: GoogleHealthCredentials) -> str:
    response = httpx.post(
        TOKEN_URL,
        data={
            "client_id": credentials.client_id,
            "client_secret": credentials.client_secret,
            "refresh_token": credentials.refresh_token,
            "grant_type": "refresh_token",
        },
        timeout=30,
    )
    if response.status_code != 200:
        raise RuntimeError(f"Google token refresh failed: {response.status_code} {response.text}")
    return response.json()["access_token"]


# 指定日の歩数データを取得して時刻別にまとめる。
def fetch_steps(access_token: str, target_date: date, env_prefix: str) -> dict[datetime, int]:
    data_type = os.getenv(f"{env_prefix.upper()}_STEPS_DATA_TYPE", "steps")
    start = target_date.isoformat()
    end = (target_date + timedelta(days=1)).isoformat()
    filter_query = (
        f'{data_type}.interval.civil_start_time >= "{start}" '
        f'AND {data_type}.interval.civil_start_time < "{end}"'
    )
    payload = get_data_points(
        f"{HEALTH_BASE_URL}/users/me/dataTypes/{data_type}/dataPoints",
        access_token,
        params={"filter": filter_query, "pageSize": 1000},
    )

    values: dict[datetime, int] = {}
    for point in payload.get("dataPoints", []):
        timestamp = parse_data_point_timestamp(point, target_date, data_type)
        if timestamp is None:
            continue
        value = extract_numeric_value(point, data_type, ["count", "steps", "value"])
        if value is None:
            continue
        values[timestamp] = values.get(timestamp, 0) + int(value)
    return values


# 指定日の心拍データと生データ点を取得する。
def fetch_heart_rate(
    access_token: str,
    target_date: date,
    env_prefix: str,
    allow_unfiltered_backfill: bool = False,
    max_pages: int | None = None,
) -> dict[datetime, float]:
    payload = None
    data_type = None
    for candidate in data_type_candidates(env_prefix, "HEART_RATE", ["heart-rate", "heart_rate", "heartRate", "heart_rate_bpm"]):
        start = target_date.isoformat()
        end = (target_date + timedelta(days=1)).isoformat()
        filter_query = (
            f'{candidate}.interval.civil_start_time >= "{start}" '
            f'AND {candidate}.interval.civil_start_time < "{end}"'
        )
        try:
            payload = get_data_points(
                f"{HEALTH_BASE_URL}/users/me/dataTypes/{candidate}/dataPoints",
                access_token,
                params={"filter": filter_query, "pageSize": 1000},
                max_pages=max_pages or 5,
            )
            data_type = candidate
            break
        except RuntimeError as exc:
            if target_date != datetime.now(JST).date() and not allow_unfiltered_backfill:
                print(f"heart rate candidate '{candidate}': filter unavailable for backfill date {target_date}, skipped")
                continue
            try:
                payload = get_data_points(
                    f"{HEALTH_BASE_URL}/users/me/dataTypes/{candidate}/dataPoints",
                    access_token,
                    params={"pageSize": 1000},
                    max_pages=max_pages or (20 if allow_unfiltered_backfill else 1),
                )
                print(f"heart rate candidate '{candidate}': filter not supported, using local date filtering")
                data_type = candidate
                break
            except RuntimeError as fallback_exc:
                print(f"heart rate candidate '{candidate}' skipped: {short_error(fallback_exc)}")
    if payload is None or data_type is None:
        return {}

    values: dict[datetime, float] = {}
    for point in payload.get("dataPoints", []):
        timestamp = parse_data_point_timestamp(point, target_date, data_type)
        if timestamp is None:
            continue
        if timestamp.astimezone(JST).date() != target_date:
            continue
        value = extract_numeric_value(point, data_type, ["bpm", "heart_rate", "beatsPerMinute", "value"])
        if value is None:
            continue
        values[timestamp] = float(value)
    return values


# 指定日の睡眠区間・ステージ・生データを取得する。
def fetch_sleep(
    access_token: str,
    target_date: date,
    participant_id: str,
    env_prefix: str,
    allow_unfiltered_backfill: bool = False,
    max_pages: int | None = None,
    include_raw: bool = True,
) -> tuple[list[SleepDataIn], list[SleepStageDataIn], list[RawSleepDataIn]]:
    payload = None
    data_type = None
    for candidate in data_type_candidates(env_prefix, "SLEEP", ["sleep", "sleep_session", "sleepSession"]):
        start = target_date.isoformat()
        end = (target_date + timedelta(days=1)).isoformat()
        filter_query = (
            f'{candidate}.interval.civil_start_time >= "{start}" '
            f'AND {candidate}.interval.civil_start_time < "{end}"'
        )
        try:
            payload = get_data_points(
                f"{HEALTH_BASE_URL}/users/me/dataTypes/{candidate}/dataPoints",
                access_token=access_token,
                params={"filter": filter_query, "pageSize": 1000},
                max_pages=max_pages or 5,
            )
            data_type = candidate
            break
        except RuntimeError:
            allowed_fallback_dates = {datetime.now(JST).date(), datetime.now(JST).date() - timedelta(days=1)}
            if target_date not in allowed_fallback_dates and not allow_unfiltered_backfill:
                print(f"sleep candidate '{candidate}': filter unavailable for backfill date {target_date}, skipped")
                continue
            try:
                payload = get_data_points(
                    f"{HEALTH_BASE_URL}/users/me/dataTypes/{candidate}/dataPoints",
                    access_token=access_token,
                    params={"pageSize": 1000},
                    max_pages=max_pages or 1,
                )
                print(f"sleep candidate '{candidate}': filter not supported, using local date filtering")
                data_type = candidate
                break
            except RuntimeError as fallback_exc:
                print(f"sleep candidate '{candidate}' skipped: {short_error(fallback_exc)}")
    if payload is None or data_type is None:
        return [], [], []

    rows = []
    stage_rows = []
    raw_rows = []
    for point in payload.get("dataPoints", []):
        start_time = parse_data_point_timestamp(point, target_date, data_type)
        if start_time is None:
            continue
        if start_time.astimezone(JST).date() != target_date:
            continue
        end_time = parse_end_timestamp(point, target_date, data_type)
        minutes = extract_numeric_value(point, data_type, ["minutes", "duration_minutes", "value"])
        if minutes is None and end_time is not None:
            minutes = (end_time - start_time).total_seconds() / 60
        stage = extract_text_value(point, data_type, ["stage", "sleep_stage", "type"])
        if include_raw:
            raw_rows.append(
                RawSleepDataIn(
                    participant_id=participant_id,
                    start_time=start_time,
                    end_time=end_time,
                    data_type=data_type,
                    stage=stage,
                    value=minutes,
                    raw_json=json.dumps(point, ensure_ascii=False, sort_keys=True),
                    source="google_health",
                )
            )
        rows.append(
            SleepDataIn(
                participant_id=participant_id,
                start_time=start_time,
                end_time=end_time,
                sleep_minutes=minutes,
                sleep_stage=stage,
                source="google_health",
            )
        )
        stage_rows.extend(parse_sleep_stages(point, participant_id, start_time, data_type))
    return rows, stage_rows, raw_rows


# 睡眠データ点からステージ区間を抽出する。
def parse_sleep_stages(
    point: dict[str, Any],
    participant_id: str,
    sleep_start_time: datetime,
    data_type: str,
) -> list[SleepStageDataIn]:
    rows = []
    for stage in data_object(point, data_type).get("stages", []) or []:
        start_time = parse_timestamp_value(stage.get("startTime") or stage.get("start_time"), sleep_start_time.date(), None)
        if start_time is None:
            continue
        end_time = parse_timestamp_value(stage.get("endTime") or stage.get("end_time"), sleep_start_time.date(), None)
        minutes = None
        if end_time is not None:
            minutes = (end_time - start_time).total_seconds() / 60
        stage_type = stage.get("type") or stage.get("stage")
        if stage_type is None:
            continue
        rows.append(
            SleepStageDataIn(
                participant_id=participant_id,
                sleep_start_time=sleep_start_time,
                start_time=start_time,
                end_time=end_time,
                stage=str(stage_type),
                stage_minutes=minutes,
                source="google_health",
            )
        )
    return rows


# 環境変数の候補を優先してデータ型名の一覧を作る。
def data_type_candidates(env_prefix: str, name: str, defaults: list[str]) -> list[str]:
    prefix = env_prefix.upper()
    raw = os.getenv(f"{prefix}_{name}_DATA_TYPES") or os.getenv(f"{prefix}_{name}_DATA_TYPE")
    if raw:
        return [item.strip() for item in raw.split(",") if item.strip()]
    return defaults


# 日時を直前の30分境界へ切り下げる。
def floor_to_30_minutes(value: datetime) -> datetime:
    minute = (value.minute // 30) * 30
    return value.replace(minute=minute, second=0, microsecond=0)


# 日時を分単位へ切り下げる。
def floor_to_minute(value: datetime) -> datetime:
    return value.replace(second=0, microsecond=0)


# 例外メッセージをログ向けの短い文字列にする。
def short_error(exc: Exception) -> str:
    text = str(exc).replace("\n", " ")
    return text[:220] + ("..." if len(text) > 220 else "")


# 認証とクエリ付きGETリクエストを送り、JSONを返す。
def get_json(url: str, access_token: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    response = httpx.get(
        url,
        headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
        params=params,
        timeout=30,
    )
    if response.status_code != 200:
        raise RuntimeError(f"Google Health API request failed: {response.status_code} {response.text}")
    return response.json()


# 指定データ型の全ページからデータ点を収集する。
def get_data_points(
    url: str,
    access_token: str,
    params: dict[str, Any] | None = None,
    max_pages: int = 5,
) -> dict[str, Any]:
    all_points = []
    next_page_token = None
    base_params = dict(params or {})
    pages = 0
    while True:
        pages += 1
        request_params = dict(base_params)
        if next_page_token:
            request_params["pageToken"] = next_page_token
        payload = get_json(url, access_token, request_params)
        all_points.extend(payload.get("dataPoints", []))
        next_page_token = payload.get("nextPageToken")
        if not next_page_token or pages >= max_pages:
            if next_page_token:
                print(f"Google Health API response reached max_pages={max_pages}; remaining points were not fetched")
            payload["dataPoints"] = all_points
            return payload


# データ点の開始時刻を解析する。
def parse_data_point_timestamp(point: dict[str, Any], fallback_date: date, data_type: str) -> datetime | None:
    data_obj = data_object(point, data_type)
    interval = data_obj.get("interval", {})
    value = (
        interval.get("civil_start_time")
        or interval.get("startTime")
        or interval.get("start_time")
        or data_obj.get("sampleTime", {}).get("physicalTime")
        or data_obj.get("startTime")
        or data_obj.get("start_time")
        or point.get("sampleTime", {}).get("physicalTime")
        or point.get("startTime")
        or point.get("start_time")
    )
    return parse_timestamp_value(value, fallback_date, point.get("time"))


# データ点の終了時刻を解析する。
def parse_end_timestamp(point: dict[str, Any], fallback_date: date, data_type: str) -> datetime | None:
    data_obj = data_object(point, data_type)
    interval = data_obj.get("interval", {})
    value = (
        interval.get("civil_end_time")
        or interval.get("endTime")
        or interval.get("end_time")
        or data_obj.get("endTime")
        or data_obj.get("end_time")
        or point.get("endTime")
        or point.get("end_time")
    )
    return parse_timestamp_value(value, fallback_date, None)


# 各種表現の日時値をタイムゾーン付き日時へ変換する。
def parse_timestamp_value(value: Any, fallback_date: date, time_value: Any) -> datetime | None:
    if value:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=JST)
            return parsed
        except ValueError:
            pass

    if time_value:
        try:
            return datetime.fromisoformat(f"{fallback_date.isoformat()}T{time_value}+09:00")
        except ValueError:
            return None
    return None


# データ点から候補キーに対応する数値を取り出す。
def extract_numeric_value(point: dict[str, Any], data_type: str, keys: list[str]) -> float | None:
    data_obj = data_object(point, data_type)
    value = None
    for key in keys:
        value = data_obj.get(key)
        if value is not None:
            break
        value = point.get(key)
        if value is not None:
            break
    if value is None:
        value = point.get("value")
    if value is None:
        return None
    return float(value)


# データ点から候補キーに対応する文字列を取り出す。
def extract_text_value(point: dict[str, Any], data_type: str, keys: list[str]) -> str | None:
    data_obj = data_object(point, data_type)
    for key in keys:
        value = data_obj.get(key)
        if value is not None:
            return str(value)
    value = point.get("type") or point.get("stage")
    return str(value) if value is not None else None


# データ型に対応する入れ子のデータ本体を返す。
def data_object(point: dict[str, Any], data_type: str) -> dict[str, Any]:
    if data_type in point and isinstance(point[data_type], dict):
        return point[data_type]
    aliases = {
        "heart-rate": "heartRate",
        "sleep-session": "sleepSession",
        "sleep_session": "sleepSession",
    }
    alias = aliases.get(data_type)
    if alias and isinstance(point.get(alias), dict):
        return point[alias]
    return {}
