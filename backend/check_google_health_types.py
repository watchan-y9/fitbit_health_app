from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone

from app.google_health_client import HEALTH_BASE_URL, credentials_from_env, get_json, refresh_access_token


JST = timezone(timedelta(hours=9))


# 利用可能なGoogle Healthデータ型を取得して表示する。
def main() -> None:
    parser = argparse.ArgumentParser(description="Try Google Health data type ids.")
    parser.add_argument("--env-prefix", default="GOOGLE")
    parser.add_argument("--types", required=True, help="Comma separated data type ids to try.")
    args = parser.parse_args()

    token = refresh_access_token(credentials_from_env(args.env_prefix))
    target_date = datetime.now(JST).date()
    for data_type in [item.strip() for item in args.types.split(",") if item.strip()]:
        url = f"{HEALTH_BASE_URL}/users/me/dataTypes/{data_type}/dataPoints"
        try:
            payload = get_json(url, token, params={"pageSize": 5})
            count = len(payload.get("dataPoints", []))
            print(f"OK {data_type}: {count} sample point(s)")
            if count:
                print(payload["dataPoints"][0])
        except Exception as exc:
            print(f"NG {data_type}: {exc}")


if __name__ == "__main__":
    main()
