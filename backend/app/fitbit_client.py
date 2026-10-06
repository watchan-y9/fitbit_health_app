from __future__ import annotations

import base64
import os
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv

from .schemas import SensorDataIn


ROOT_DIR = Path(__file__).resolve().parents[2]
ENV_PATH = ROOT_DIR / ".env"
TOKEN_URL = "https://api.fitbit.com/oauth2/token"
FITBIT_API_BASE = "https://api.fitbit.com"
JST = timezone(timedelta(hours=9))


# Fitbit APIの認証情報を保持する。
@dataclass(frozen=True)
class FitbitCredentials:
    client_id: str
    client_secret: str
    refresh_token: str
    refresh_token_env_name: str


# 環境変数からFitbitの認証情報を組み立てる。
def credentials_from_env(env_prefix: str = "FITBIT") -> FitbitCredentials:
    load_dotenv(ENV_PATH)
    prefix = env_prefix.upper().rstrip("_")
    client_id = os.getenv(f"{prefix}_CLIENT_ID") or os.getenv("FITBIT_CLIENT_ID")
    client_secret = os.getenv(f"{prefix}_CLIENT_SECRET") or os.getenv("FITBIT_CLIENT_SECRET")
    refresh_token_env_name = f"{prefix}_REFRESH_TOKEN"
    refresh_token = os.getenv(refresh_token_env_name) or os.getenv("FITBIT_REFRESH_TOKEN")

    if not client_id or not client_secret or not refresh_token:
        raise ValueError(
            f"Missing Fitbit credentials. Set {prefix}_CLIENT_ID, "
            f"{prefix}_CLIENT_SECRET, and {prefix}_REFRESH_TOKEN in .env."
        )

    return FitbitCredentials(
        client_id=client_id,
        client_secret=client_secret,
        refresh_token=refresh_token,
        refresh_token_env_name=refresh_token_env_name,
    )


# 指定日の歩数・心拍数を取得し、30分単位のセンサーデータにまとめる。
def fetch_fitbit_sensor_rows(
    participant_id: str,
    env_prefix: str = "FITBIT",
    target_date: date | None = None,
    detail_level: str = "30min",
) -> list[SensorDataIn]:
    if target_date is None:
        target_date = datetime.now(JST).date()

    credentials = credentials_from_env(env_prefix)
    access_token, new_refresh_token = refresh_access_token(credentials)
    if new_refresh_token and new_refresh_token != credentials.refresh_token:
        update_env_value(credentials.refresh_token_env_name, new_refresh_token)

    steps = fetch_activity_intraday(access_token, "steps", target_date, detail_level)
    calories = fetch_activity_intraday(access_token, "calories", target_date, detail_level)
    heart_rate = fetch_heart_intraday(access_token, target_date, detail_level)

    timestamps = sorted(set(steps) | set(calories) | set(heart_rate))
    rows = []
    for timestamp in timestamps:
        step_value = steps.get(timestamp)
        heart_value = heart_rate.get(timestamp)
        calorie_value = calories.get(timestamp)
        active_minutes = estimate_active_minutes(step_value, heart_value, detail_level)
        rows.append(
            SensorDataIn(
                participant_id=participant_id,
                timestamp=timestamp,
                heart_rate=heart_value,
                steps=step_value,
                calories=calorie_value,
                active_minutes=active_minutes,
            )
        )
    return rows


# リフレッシュトークンを使ってアクセストークンを更新する。
def refresh_access_token(credentials: FitbitCredentials) -> tuple[str, str | None]:
    basic = base64.b64encode(
        f"{credentials.client_id}:{credentials.client_secret}".encode("utf-8")
    ).decode("ascii")
    response = httpx.post(
        TOKEN_URL,
        data={"grant_type": "refresh_token", "refresh_token": credentials.refresh_token},
        headers={
            "Authorization": f"Basic {basic}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        timeout=30,
    )
    if response.status_code != 200:
        raise RuntimeError(f"Fitbit token refresh failed: {response.status_code} {response.text}")
    payload = response.json()
    return payload["access_token"], payload.get("refresh_token")


# Fitbitから日内の歩数データを取得する。
def fetch_activity_intraday(
    access_token: str,
    resource: str,
    target_date: date,
    detail_level: str,
) -> dict[datetime, int | float]:
    url = (
        f"{FITBIT_API_BASE}/1/user/-/activities/{resource}/date/"
        f"{target_date.isoformat()}/1d/{detail_level}.json"
    )
    payload = get_json(url, access_token)
    dataset = payload.get(f"activities-{resource}-intraday", {}).get("dataset", [])
    return parse_intraday_dataset(target_date, dataset)


# Fitbitから日内の心拍数データを取得する。
def fetch_heart_intraday(
    access_token: str,
    target_date: date,
    detail_level: str,
) -> dict[datetime, int | float]:
    url = (
        f"{FITBIT_API_BASE}/1/user/-/activities/heart/date/"
        f"{target_date.isoformat()}/1d/{detail_level}.json"
    )
    payload = get_json(url, access_token)
    dataset = payload.get("activities-heart-intraday", {}).get("dataset", [])
    return parse_intraday_dataset(target_date, dataset)


# 認証付きGETリクエストを送り、JSONレスポンスを返す。
def get_json(url: str, access_token: str) -> dict[str, Any]:
    response = httpx.get(
        url,
        headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
        timeout=30,
    )
    if response.status_code == 429:
        reset = response.headers.get("fitbit-rate-limit-reset", "unknown")
        raise RuntimeError(f"Fitbit rate limit reached. Try again after {reset} seconds.")
    if response.status_code != 200:
        raise RuntimeError(f"Fitbit API request failed: {response.status_code} {response.text}")
    return response.json()


# 日内データセットの時刻と値を辞書へ変換する。
def parse_intraday_dataset(target_date: date, dataset: list[dict[str, Any]]) -> dict[datetime, int | float]:
    values = {}
    for point in dataset:
        timestamp = datetime.combine(
            target_date,
            time.fromisoformat(point["time"]),
            tzinfo=JST,
        )
        values[timestamp] = point.get("value")
    return values


# 歩数と心拍数から活動時間を概算する。
def estimate_active_minutes(
    steps: int | float | None,
    heart_rate: int | float | None,
    detail_level: str,
) -> float:
    minutes = int(detail_level.replace("min", "")) if detail_level.endswith("min") else 1
    if (steps is not None and steps > 20) or (heart_rate is not None and heart_rate >= 100):
        return float(minutes)
    return 0.0


# .env内の指定値を更新し、未定義なら追記する。
def update_env_value(name: str, value: str) -> None:
    lines = []
    found = False
    if ENV_PATH.exists():
        lines = ENV_PATH.read_text(encoding="utf-8").splitlines()

    updated = []
    for line in lines:
        if line.startswith(f"{name}="):
            updated.append(f"{name}={value}")
            found = True
        else:
            updated.append(line)
    if not found:
        updated.append(f"{name}={value}")

    ENV_PATH.write_text("\n".join(updated) + "\n", encoding="utf-8")
