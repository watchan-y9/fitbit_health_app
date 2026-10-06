from __future__ import annotations

import csv
import io
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse

from .anomaly_detection import choices_for, detect_candidates, event_message, parse_dt, to_iso
from .database import get_connection, init_db
from .schemas import AskedIn, DetectResponse, PendingResponse, RawSensorDataIn, RawSensorDataOut, RawSleepDataIn, RawSleepDataOut, ResponseIn, SensorDataIn, SensorDataOut, SleepDataIn, SleepDataOut, SleepStageDataIn, SleepStageDataOut


app = FastAPI(
    title="Fitbit-Kebbi Anomaly Research API",
    description="Fitbit/Google Health data, anomaly confirmation, and research CSV export API.",
    version="0.1.0",
)

DETECTION_LOOKBACK_DAYS = 3
STEP_COMPARISON_LOOKBACK_MINUTES = 30


# アプリ起動時にデータベースを初期化する。
@app.on_event("startup")
def startup() -> None:
    init_db()


# APIが稼働中であることを返す。
@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


# 集計済みセンサーデータを新規保存または更新する。
@app.post("/sensor-data", response_model=SensorDataOut)
def save_sensor_data(payload: SensorDataIn) -> dict:
    init_db()
    timestamp = to_iso(payload.timestamp)
    with get_connection() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO participants(participant_id, display_name)
            VALUES (?, ?)
            """,
            (payload.participant_id, payload.participant_id),
        )
        conn.execute(
            """
            INSERT INTO sensor_data(
                participant_id, timestamp, heart_rate, max_heart_rate, steps, calories, active_minutes
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(participant_id, timestamp) DO UPDATE SET
                heart_rate = excluded.heart_rate,
                max_heart_rate = excluded.max_heart_rate,
                steps = excluded.steps,
                calories = excluded.calories,
                active_minutes = excluded.active_minutes
            """,
            (
                payload.participant_id,
                timestamp,
                payload.heart_rate,
                payload.max_heart_rate,
                payload.steps,
                payload.calories,
                payload.active_minutes,
            ),
        )
        row = conn.execute(
            """
            SELECT id, participant_id, timestamp, heart_rate, max_heart_rate, steps, calories, active_minutes
            FROM sensor_data
            WHERE participant_id = ? AND timestamp = ?
            """,
            (payload.participant_id, timestamp),
        ).fetchone()
    return dict(row)


# 未加工センサーデータを重複なく保存する。
@app.post("/raw-sensor-data", response_model=RawSensorDataOut)
def save_raw_sensor_data(payload: RawSensorDataIn) -> dict:
    init_db()
    timestamp = to_iso(payload.timestamp)
    with get_connection() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO participants(participant_id, display_name)
            VALUES (?, ?)
            """,
            (payload.participant_id, payload.participant_id),
        )
        conn.execute(
            """
            INSERT INTO raw_sensor_data(participant_id, timestamp, data_type, value, source)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(participant_id, timestamp, data_type, source) DO UPDATE SET
                value = excluded.value
            """,
            (
                payload.participant_id,
                timestamp,
                payload.data_type,
                payload.value,
                payload.source,
            ),
        )
        row = conn.execute(
            """
            SELECT id, participant_id, timestamp, data_type, value, source
            FROM raw_sensor_data
            WHERE participant_id = ?
              AND timestamp = ?
              AND data_type = ?
              AND source = ?
            """,
            (payload.participant_id, timestamp, payload.data_type, payload.source),
        ).fetchone()
    return dict(row)


# 未加工睡眠データを重複なく保存する。
@app.post("/raw-sleep-data", response_model=RawSleepDataOut)
def save_raw_sleep_data(payload: RawSleepDataIn) -> dict:
    init_db()
    start_time = to_iso(payload.start_time)
    end_time = to_iso(payload.end_time) if payload.end_time else None
    with get_connection() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO participants(participant_id, display_name)
            VALUES (?, ?)
            """,
            (payload.participant_id, payload.participant_id),
        )
        conn.execute(
            """
            INSERT INTO raw_sleep_data(
                participant_id, start_time, end_time, data_type, stage, value, raw_json, source
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(participant_id, start_time, end_time, data_type, source) DO UPDATE SET
                stage = excluded.stage,
                value = excluded.value,
                raw_json = excluded.raw_json
            """,
            (
                payload.participant_id,
                start_time,
                end_time,
                payload.data_type,
                payload.stage,
                payload.value,
                payload.raw_json,
                payload.source,
            ),
        )
        row = conn.execute(
            """
            SELECT id, participant_id, start_time, end_time, data_type, stage, value, raw_json, source
            FROM raw_sleep_data
            WHERE participant_id = ?
              AND start_time = ?
              AND (end_time IS ? OR end_time = ?)
              AND data_type = ?
              AND source = ?
            """,
            (payload.participant_id, start_time, end_time, end_time, payload.data_type, payload.source),
        ).fetchone()
    return dict(row)


# 睡眠区間データを新規保存または更新する。
@app.post("/sleep-data", response_model=SleepDataOut)
def save_sleep_data(payload: SleepDataIn) -> dict:
    init_db()
    start_time = to_iso(payload.start_time)
    end_time = to_iso(payload.end_time) if payload.end_time else None
    with get_connection() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO participants(participant_id, display_name)
            VALUES (?, ?)
            """,
            (payload.participant_id, payload.participant_id),
        )
        conn.execute(
            """
            INSERT INTO sleep_data(participant_id, start_time, end_time, sleep_minutes, sleep_stage, source)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(participant_id, start_time, source) DO UPDATE SET
                end_time = excluded.end_time,
                sleep_minutes = excluded.sleep_minutes,
                sleep_stage = excluded.sleep_stage
            """,
            (
                payload.participant_id,
                start_time,
                end_time,
                payload.sleep_minutes,
                payload.sleep_stage,
                payload.source,
            ),
        )
        row = conn.execute(
            """
            SELECT id, participant_id, start_time, end_time, sleep_minutes, sleep_stage, source
            FROM sleep_data
            WHERE participant_id = ? AND start_time = ? AND source = ?
            """,
            (payload.participant_id, start_time, payload.source),
        ).fetchone()
    return dict(row)


# 睡眠ステージデータを新規保存または更新する。
@app.post("/sleep-stage-data", response_model=SleepStageDataOut)
def save_sleep_stage_data(payload: SleepStageDataIn) -> dict:
    init_db()
    sleep_start_time = to_iso(payload.sleep_start_time)
    start_time = to_iso(payload.start_time)
    end_time = to_iso(payload.end_time) if payload.end_time else None
    with get_connection() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO participants(participant_id, display_name)
            VALUES (?, ?)
            """,
            (payload.participant_id, payload.participant_id),
        )
        conn.execute(
            """
            INSERT INTO sleep_stage_data(
                participant_id, sleep_start_time, start_time, end_time,
                stage, stage_minutes, source
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(participant_id, sleep_start_time, start_time, stage, source) DO UPDATE SET
                end_time = excluded.end_time,
                stage_minutes = excluded.stage_minutes
            """,
            (
                payload.participant_id,
                sleep_start_time,
                start_time,
                end_time,
                payload.stage,
                payload.stage_minutes,
                payload.source,
            ),
        )
        row = conn.execute(
            """
            SELECT id, participant_id, sleep_start_time, start_time, end_time,
                   stage, stage_minutes, source
            FROM sleep_stage_data
            WHERE participant_id = ?
              AND sleep_start_time = ?
              AND start_time = ?
              AND stage = ?
              AND source = ?
            """,
            (payload.participant_id, sleep_start_time, start_time, payload.stage, payload.source),
        ).fetchone()
    return dict(row)


# 保存データから異常候補を検出してイベントとして登録する。
@app.post("/detect-anomalies", response_model=DetectResponse)
def detect_anomalies() -> dict:
    init_db()
    current_time = datetime.now(timezone.utc)
    detection_cutoff = current_time - timedelta(
        days=DETECTION_LOOKBACK_DAYS,
        minutes=STEP_COMPARISON_LOOKBACK_MINUTES,
    )
    cutoff_iso = detection_cutoff.isoformat()
    completed_window_cutoff_iso = (
        current_time - timedelta(minutes=STEP_COMPARISON_LOOKBACK_MINUTES)
    ).isoformat()
    with get_connection() as conn:
        rows = [
            dict(row)
            for row in conn.execute(
                """
                SELECT *
                FROM sensor_data
                WHERE datetime(timestamp) >= datetime(?)
                  AND datetime(timestamp) <= datetime(?)
                """,
                (cutoff_iso, completed_window_cutoff_iso),
            ).fetchall()
        ]
        sleep_rows = [
            dict(row)
            for row in conn.execute(
                """
                SELECT *
                FROM sleep_data
                WHERE end_time IS NULL
                   OR datetime(end_time) >= datetime(?)
                """,
                (cutoff_iso,),
            ).fetchall()
        ]
        sleep_stage_rows = [
            dict(row)
            for row in conn.execute(
                """
                SELECT *
                FROM sleep_stage_data
                WHERE end_time IS NULL
                   OR datetime(end_time) >= datetime(?)
                """,
                (cutoff_iso,),
            ).fetchall()
        ]
        participants = {
            row["participant_id"]: dict(row)
            for row in conn.execute("SELECT participant_id, sex, age FROM participants").fetchall()
        }
        demographic_baselines = [
            dict(row) for row in conn.execute("SELECT * FROM demographic_baselines").fetchall()
        ]
        known_keys = {
            (row["participant_id"], row["event_time"], row["event_type"])
            for row in conn.execute("SELECT participant_id, event_time, event_type FROM anomaly_events")
        }
        candidates = detect_candidates(
            rows,
            known_keys,
            sleep_rows=sleep_rows,
            sleep_stage_rows=sleep_stage_rows,
            participants=participants,
            demographic_baselines=demographic_baselines,
        )
        for event in candidates:
            conn.execute(
                """
                INSERT INTO anomaly_events(
                    event_id, participant_id, event_time, event_type, value, baseline,
                    description, status, detected_at, expires_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.event_id,
                    event.participant_id,
                    event.event_time,
                    event.event_type,
                    event.value,
                    event.baseline,
                    event.description,
                    event.status,
                    event.detected_at,
                    event.expires_at,
                ),
            )
        events = [
            {
                "event_id": event.event_id,
                "participant_id": event.participant_id,
                "event_time": event.event_time,
                "event_type": event.event_type,
                "value": event.value,
                "baseline": event.baseline,
                "description": event.description,
                "status": event.status,
            }
            for event in candidates
        ]
    return {"detected_count": len(events), "events": events}


# 参加者の次の未回答異常イベントを返す。
@app.get("/participants/{participant_id}/pending-anomalies", response_model=PendingResponse)
def get_pending_anomaly(participant_id: str) -> dict:
    init_db()
    now = datetime.now(timezone.utc).isoformat()
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT *
            FROM anomaly_events
            WHERE participant_id = ?
              AND status = 'pending'
              AND expires_at >= ?
            ORDER BY event_time ASC
            LIMIT 1
            """,
            (participant_id, now),
        ).fetchone()
    if row is None:
        return {"event": None}

    event = dict(row)
    return {
        "event": {
            "event_id": event["event_id"],
            "participant_id": event["participant_id"],
            "event_time": event["event_time"],
            "event_type": event["event_type"],
            "message": event_message(event),
            "choices": choices_for(event["event_type"]),
        }
    }


# 異常イベントを質問送信済みとして記録する。
@app.post("/anomalies/{event_id}/asked")
def mark_asked(event_id: str, payload: AskedIn) -> dict:
    init_db()
    asked_time = to_iso(payload.asked_time)
    with get_connection() as conn:
        event = conn.execute("SELECT * FROM anomaly_events WHERE event_id = ?", (event_id,)).fetchone()
        if event is None:
            raise HTTPException(status_code=404, detail="event not found")
        event_dict = dict(event)
        message = event_message(event_dict)
        conn.execute(
            """
            INSERT INTO anomaly_questions(event_id, participant_id, robot_id, asked_time, message)
            VALUES (?, ?, ?, ?, ?)
            """,
            (event_id, event_dict["participant_id"], payload.robot_id, asked_time, message),
        )
        conn.execute("UPDATE anomaly_events SET status = 'asked' WHERE event_id = ?", (event_id,))
    return {"status": "asked", "event_id": event_id}


# 異常イベントへの回答を保存してイベントを回答済みにする。
@app.post("/anomaly-responses")
def save_response(payload: ResponseIn) -> dict:
    init_db()
    response_time = to_iso(payload.response_time)
    with get_connection() as conn:
        event = conn.execute(
            "SELECT * FROM anomaly_events WHERE event_id = ? AND participant_id = ?",
            (payload.event_id, payload.participant_id),
        ).fetchone()
        if event is None:
            raise HTTPException(status_code=404, detail="event not found")

        question = conn.execute(
            """
            SELECT asked_time
            FROM anomaly_questions
            WHERE event_id = ?
            ORDER BY asked_time DESC
            LIMIT 1
            """,
            (payload.event_id,),
        ).fetchone()
        latency = None
        if question is not None:
            latency = (parse_dt(response_time) - parse_dt(question["asked_time"])).total_seconds()

        conn.execute(
            """
            INSERT INTO anomaly_responses(
                event_id, participant_id, robot_id, response_time,
                response_type, response_value, response_text, response_latency_sec
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                payload.event_id,
                payload.participant_id,
                payload.robot_id,
                response_time,
                payload.response_type,
                payload.response_value,
                payload.response_text,
                latency,
            ),
        )
        conn.execute("UPDATE anomaly_events SET status = 'answered' WHERE event_id = ?", (payload.event_id,))
    return {"status": "answered", "event_id": payload.event_id}


# 異常イベントと回答の分析用CSVを出力する。
@app.get("/export/anomaly-analysis")
def export_anomaly_analysis() -> StreamingResponse:
    init_db()
    output = io.StringIO()
    fieldnames = [
        "event_id",
        "participant_id",
        "event_time",
        "event_type",
        "value",
        "baseline",
        "description",
        "asked_time",
        "activity_question",
        "activity_answer",
        "feeling_question",
        "feeling_answer",
        "response_time",
        "response_value",
        "response_text",
        "status",
    ]
    writer = csv.DictWriter(output, fieldnames=fieldnames)
    writer.writeheader()

    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT
                e.event_id,
                e.participant_id,
                e.event_time,
                e.event_type,
                e.value,
                e.baseline,
                e.description,
                q.asked_time,
                q.message AS activity_question,
                r.response_value AS activity_answer,
                CASE
                    WHEN r.response_text IS NOT NULL AND r.response_text != ''
                    THEN CASE
                        WHEN e.event_type IN ('daily_steps_demographic_outlier', 'daily_steps_normal')
                        THEN '1日を通して、どんな気持ちだった？'
                        ELSE 'そのときどんな気持ちだった？'
                    END
                    ELSE NULL
                END AS feeling_question,
                r.response_text AS feeling_answer,
                r.response_time,
                r.response_value,
                r.response_text,
                e.status
            FROM anomaly_events e
            LEFT JOIN anomaly_questions q ON q.event_id = e.event_id
            LEFT JOIN anomaly_responses r ON r.event_id = e.event_id
            ORDER BY e.event_time ASC
            """
        ).fetchall()
        for row in rows:
            writer.writerow(dict(row))

    output.seek(0)
    headers = {"Content-Disposition": 'attachment; filename="anomaly_analysis.csv"'}
    csv_bytes = output.getvalue().encode("utf-8-sig")
    return StreamingResponse(iter([csv_bytes]), media_type="text/csv; charset=utf-8", headers=headers)
