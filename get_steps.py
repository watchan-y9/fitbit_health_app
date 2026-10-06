import os
import json
from datetime import date, timedelta

import httpx
from dotenv import load_dotenv


load_dotenv()

CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
REFRESH_TOKEN = os.getenv("GOOGLE_REFRESH_TOKEN")

TOKEN_URL = "https://oauth2.googleapis.com/token"
HEALTH_BASE_URL = "https://health.googleapis.com/v4"


def get_access_token() -> stimar:
    if not CLIENT_ID or not CLIENT_SECRET or not REFRESH_TOKEN:
        raise ValueError(".env の GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET / GOOGLE_REFRESH_TOKEN を確認してください。")

    data = {
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "refresh_token": REFRESH_TOKEN,
        "grant_type": "refresh_token",
    }

    response = httpx.post(TOKEN_URL, data=data, timeout=30)

    if response.status_code != 200:
        print("access token取得に失敗しました。")
        print("status:", response.status_code)
        print(response.text)
        raise SystemExit

    return response.json()["access_token"]


def get_identity(access_token: str) -> dict:
    url = f"{HEALTH_BASE_URL}/users/me/identity"

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
    }

    response = httpx.get(url, headers=headers, timeout=30)

    if response.status_code != 200:
        print("identity取得に失敗しました。")
        print("status:", response.status_code)
        print(response.text)
        raise SystemExit

    return response.json()


def get_steps(access_token: str, target_date: date) -> dict:
    start = target_date.isoformat()
    end = (target_date + timedelta(days=1)).isoformat()

    filter_query = (
        f'steps.interval.civil_start_time >= "{start}" '
        f'AND steps.interval.civil_start_time < "{end}"'
    )

    url = f"{HEALTH_BASE_URL}/users/me/dataTypes/steps/dataPoints"

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
    }

    params = {
        "filter": filter_query,
        "pageSize": 500,
    }

    response = httpx.get(url, headers=headers, params=params, timeout=30)

    if response.status_code != 200:
        print("steps取得に失敗しました。")
        print("status:", response.status_code)
        print(response.text)
        raise SystemExit

    return response.json()


def summarize_steps(steps_response: dict) -> int:
    """
    stepsレスポンスから歩数合計を計算する。
    Google Health APIのレスポンス形式の違いに備えて、
    count / steps / value の候補を順番に見る。
    """
    total_steps = 0
    data_points = steps_response.get("dataPoints", [])

    print("\n=== Debug: extracted step values ===")

    for i, point in enumerate(data_points):
        steps_obj = point.get("steps", {})

        # 候補1: {"steps": {"count": 123}}
        value = steps_obj.get("count")

        # 候補2: {"steps": {"steps": "123"}}
        if value is None:
            value = steps_obj.get("steps")

        # 候補3: {"steps": {"value": 123}}
        if value is None:
            value = steps_obj.get("value")

        print(f"{i}: raw steps object = {steps_obj}, extracted value = {value}")

        if value is None:
            continue

        try:
            total_steps += int(value)
        except ValueError:
            print(f"数値に変換できませんでした: {value}")

    return total_steps


def main():
    target_date = date.today()

    access_token = get_access_token()

    identity = get_identity(access_token)
    print("=== Identity ===")
    print(json.dumps(identity, indent=2, ensure_ascii=False))

    steps_response = get_steps(access_token, target_date)
    print("\n=== Steps raw response ===")
    print(json.dumps(steps_response, indent=2, ensure_ascii=False))

    total_steps = summarize_steps(steps_response)
    print("\n=== Summary ===")
    print(f"対象日: {target_date}")
    print(f"歩数合計: {total_steps} 歩")


if __name__ == "__main__":
    main()