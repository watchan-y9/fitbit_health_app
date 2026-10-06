import csv
from pathlib import Path

from app.main import save_sensor_data
from app.schemas import SensorDataIn


# サンプルCSVを読み込み、センサーデータとしてAPIへ登録する。
def main() -> None:
    csv_path = Path(__file__).resolve().parents[1] / "sample_data" / "fitbit_sample.csv"
    with csv_path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            payload = SensorDataIn(
                participant_id=row["participant_id"],
                timestamp=row["timestamp"],
                heart_rate=float(row["heart_rate"]) if row["heart_rate"] else None,
                max_heart_rate=float(row["max_heart_rate"]) if row.get("max_heart_rate") else None,
                steps=int(row["steps"]) if row["steps"] else None,
                calories=float(row["calories"]) if row["calories"] else None,
                active_minutes=float(row["active_minutes"]) if row["active_minutes"] else None,
            )
            save_sensor_data(payload)
    print(f"loaded: {csv_path}")


if __name__ == "__main__":
    main()
