import sqlite3
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parents[2]
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "app.db"


# SQLite接続を作成し、行を辞書形式で取得できるようにする。
def get_connection() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


# 必要なテーブルと追加カラムを作成してDBを初期化する。
def init_db() -> None:
    with get_connection() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS participants (
                participant_id TEXT PRIMARY KEY,
                display_name TEXT,
                sex TEXT,
                age INTEGER,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS sensor_data (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                participant_id TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                heart_rate REAL,
                max_heart_rate REAL,
                steps INTEGER,
                calories REAL,
                active_minutes REAL,
                UNIQUE(participant_id, timestamp),
                FOREIGN KEY(participant_id) REFERENCES participants(participant_id)
            );

            CREATE TABLE IF NOT EXISTS raw_sensor_data (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                participant_id TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                data_type TEXT NOT NULL,
                value REAL NOT NULL,
                source TEXT NOT NULL DEFAULT 'google_health',
                UNIQUE(participant_id, timestamp, data_type, source),
                FOREIGN KEY(participant_id) REFERENCES participants(participant_id)
            );

            CREATE TABLE IF NOT EXISTS raw_sleep_data (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                participant_id TEXT NOT NULL,
                start_time TEXT NOT NULL,
                end_time TEXT,
                data_type TEXT NOT NULL,
                stage TEXT,
                value REAL,
                raw_json TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'google_health',
                UNIQUE(participant_id, start_time, end_time, data_type, source),
                FOREIGN KEY(participant_id) REFERENCES participants(participant_id)
            );

            CREATE TABLE IF NOT EXISTS sleep_data (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                participant_id TEXT NOT NULL,
                start_time TEXT NOT NULL,
                end_time TEXT,
                sleep_minutes REAL,
                sleep_stage TEXT,
                source TEXT,
                UNIQUE(participant_id, start_time, source),
                FOREIGN KEY(participant_id) REFERENCES participants(participant_id)
            );

            CREATE TABLE IF NOT EXISTS sleep_stage_data (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                participant_id TEXT NOT NULL,
                sleep_start_time TEXT NOT NULL,
                start_time TEXT NOT NULL,
                end_time TEXT,
                stage TEXT NOT NULL,
                stage_minutes REAL,
                source TEXT,
                UNIQUE(participant_id, sleep_start_time, start_time, stage, source),
                FOREIGN KEY(participant_id) REFERENCES participants(participant_id)
            );

            CREATE TABLE IF NOT EXISTS anomaly_events (
                event_id TEXT PRIMARY KEY,
                participant_id TEXT NOT NULL,
                event_time TEXT NOT NULL,
                event_type TEXT NOT NULL,
                value REAL,
                baseline REAL,
                description TEXT NOT NULL,
                status TEXT NOT NULL,
                detected_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                FOREIGN KEY(participant_id) REFERENCES participants(participant_id)
            );

            CREATE TABLE IF NOT EXISTS demographic_baselines (
                baseline_id INTEGER PRIMARY KEY AUTOINCREMENT,
                metric TEXT NOT NULL,
                sex TEXT NOT NULL,
                age_min INTEGER NOT NULL,
                age_max INTEGER NOT NULL,
                weekday INTEGER,
                mean_value REAL NOT NULL,
                sd_value REAL,
                lower_bound REAL,
                upper_bound REAL,
                unit TEXT,
                source TEXT,
                UNIQUE(metric, sex, age_min, age_max, weekday, source)
            );

            CREATE TABLE IF NOT EXISTS anomaly_questions (
                question_id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL,
                participant_id TEXT NOT NULL,
                robot_id TEXT NOT NULL,
                asked_time TEXT NOT NULL,
                message TEXT NOT NULL,
                FOREIGN KEY(event_id) REFERENCES anomaly_events(event_id)
            );

            CREATE TABLE IF NOT EXISTS anomaly_responses (
                response_id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL,
                participant_id TEXT NOT NULL,
                robot_id TEXT NOT NULL,
                response_time TEXT NOT NULL,
                response_type TEXT NOT NULL,
                response_value TEXT NOT NULL,
                response_text TEXT,
                response_latency_sec REAL,
                FOREIGN KEY(event_id) REFERENCES anomaly_events(event_id)
            );

            CREATE INDEX IF NOT EXISTS idx_sensor_data_participant_time
                ON sensor_data(participant_id, timestamp);

            CREATE INDEX IF NOT EXISTS idx_raw_sensor_data_participant_time
                ON raw_sensor_data(participant_id, timestamp);

            CREATE INDEX IF NOT EXISTS idx_raw_sensor_data_type_time
                ON raw_sensor_data(data_type, timestamp);

            CREATE INDEX IF NOT EXISTS idx_raw_sleep_data_participant_start
                ON raw_sleep_data(participant_id, start_time);

            CREATE INDEX IF NOT EXISTS idx_sleep_data_participant_start
                ON sleep_data(participant_id, start_time);

            CREATE INDEX IF NOT EXISTS idx_sleep_stage_data_participant_start
                ON sleep_stage_data(participant_id, start_time);

            CREATE INDEX IF NOT EXISTS idx_anomaly_events_participant_time_status
                ON anomaly_events(participant_id, event_time, status);

            CREATE INDEX IF NOT EXISTS idx_demographic_baselines_lookup
                ON demographic_baselines(metric, sex, age_min, age_max, weekday);
            """
        )
        _ensure_column(conn, "participants", "sex", "TEXT")
        _ensure_column(conn, "participants", "age", "INTEGER")
        _ensure_column(conn, "sensor_data", "max_heart_rate", "REAL")


# テーブルに不足しているカラムだけを追加する。
def _ensure_column(conn: sqlite3.Connection, table: str, column: str, column_type: str) -> None:
    columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}")
