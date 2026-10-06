from __future__ import annotations

import argparse
import csv
from pathlib import Path

from app.database import get_connection, init_db


ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_CSV = ROOT_DIR / "sample_data" / "demographic_baselines_template.csv"


# 空文字を許容しながら文字列を小数へ変換する。
def optional_float(value: str | None) -> float | None:
    if value is None or value == "":
        return None
    return float(value)


# 空文字を許容しながら文字列を整数へ変換する。
def optional_int(value: str | None) -> int | None:
    if value is None or value == "":
        return None
    return int(value)


# CSVから人口統計別の基準値をデータベースへ登録する。
def main() -> None:
    parser = argparse.ArgumentParser(description="Load demographic baseline CSV into SQLite.")
    parser.add_argument("--csv", default=str(DEFAULT_CSV))
    args = parser.parse_args()

    init_db()
    path = Path(args.csv).resolve()
    loaded = 0
    with path.open(newline="", encoding="utf-8-sig") as f:
        with get_connection() as conn:
            for row in csv.DictReader(f):
                conn.execute(
                    """
                    INSERT INTO demographic_baselines(
                        metric, sex, age_min, age_max, weekday, mean_value,
                        sd_value, lower_bound, upper_bound, unit, source
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(metric, sex, age_min, age_max, weekday, source) DO UPDATE SET
                        mean_value = excluded.mean_value,
                        sd_value = excluded.sd_value,
                        lower_bound = excluded.lower_bound,
                        upper_bound = excluded.upper_bound,
                        unit = excluded.unit
                    """,
                    (
                        row["metric"],
                        row["sex"],
                        int(row["age_min"]),
                        int(row["age_max"]),
                        optional_int(row.get("weekday")),
                        float(row["mean_value"]),
                        optional_float(row.get("sd_value")),
                        optional_float(row.get("lower_bound")),
                        optional_float(row.get("upper_bound")),
                        row.get("unit"),
                        row.get("source") or "manual",
                    ),
                )
                loaded += 1
    print(f"loaded {loaded} baseline rows from {path}")


if __name__ == "__main__":
    main()
