from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from statistics import mean
from uuid import uuid4


JST = timezone(timedelta(hours=9))
MAX_HEART_RATE_AGE_INTERCEPT = 208.0
MAX_HEART_RATE_AGE_COEFFICIENT = 0.7
HEART_RATE_RESERVE_INTENSITY = 0.4
LEGACY_HEART_RATE_SLEEP_MULTIPLIER = 1.8
HEART_RATE_NEW_CRITERION_EFFECTIVE_AT = datetime(2026, 7, 22, tzinfo=JST)
SLEEP_HEART_RATE_FALLBACK_DAYS = 7

CHOICES_BY_EVENT_TYPE = {
    "step_increase_30min": ["移動していた", "散歩していた", "運動していた", "買い物をしていた", "その他"],
    "low_steps_30min": ["座って作業していた", "授業・会議だった", "休んでいた", "体調が悪かった", "その他"],
    "heart_rate_sleep_x2": ["運動していた", "急いで移動していた", "緊張・ストレスがあった", "食事をしていた", "その他"],
    "daily_steps_demographic_outlier": ["普段と違う予定だった", "体調が違った", "移動が多かった/少なかった", "その他"],
    "sleep_minutes_demographic_outlier": ["寝る時間がずれた", "予定があった", "体調が違った", "眠れなかった", "その他"],
}


# 検出した異常候補の内容を保持する。
@dataclass(frozen=True)
class CandidateEvent:
    event_id: str
    participant_id: str
    event_time: str
    event_type: str
    value: float | None
    baseline: float | None
    description: str
    status: str
    detected_at: str
    expires_at: str


# ISO形式の文字列をタイムゾーン付き日時へ変換する。
def parse_dt(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=JST)
    return parsed


# 日時を秒精度のISO形式へ変換する。
def to_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=JST)
    return dt.isoformat()


# 異常イベントの種類に応じた質問文を作る。
def event_message(event: dict) -> str:
    event_time = parse_dt(event["event_time"]).astimezone(JST)
    label = event_time.strftime("%Y年%m月%d日 %H:%M")
    event_type = event["event_type"]
    if event_type == "step_increase_30min":
        return f"{label}ごろ、30分前より歩数が大きく増えていました。そのとき何をしていましたか？"
    if event_type == "low_steps_30min":
        return f"{label}までの30分間、歩数が少ない状態でした。そのとき何をしていましたか？"
    if event_type == "heart_rate_sleep_x2":
        return f"{label}ごろ、睡眠中の平均心拍の2倍以上になっていました。そのとき何をしていましたか？"
    if event_type == "daily_steps_demographic_outlier":
        steps = event.get("value")
        baseline = event.get("baseline")
        if steps is not None and baseline is not None and float(steps) > float(baseline):
            return "今日はたくさん歩いたみたい。どんな1日だった？"
        return "今日、1日通して歩いた量が少なかったみたい。どんな1日だった？"
    if event_type == "sleep_minutes_demographic_outlier":
        return f"{event_time.strftime('%Y年%m月%d日')}の睡眠時間が、同じ性別・年齢階級・曜日の平均から1時間以上ずれていました。その日は何か普段と違いましたか？"
    return f"{label}ごろ、普段と違うデータ変化がありました。そのとき何をしていましたか？"


# イベント種別に応じた回答選択肢を返す。
def choices_for(event_type: str) -> list[str]:
    return CHOICES_BY_EVENT_TYPE.get(event_type, ["その他"])


# 異常イベントの種類に応じた質問文を作る。
def event_message(event: dict) -> str:
    event_time = parse_dt(event["event_time"]).astimezone(JST)
    time_label = event_time.strftime("%H:%M")
    event_type = event["event_type"]

    if event_type == "heart_rate_step_increase_30min":
        return f"\u4eca\u65e5\u306e{time_label}\u3054\u308d\u3001\u305f\u304f\u3055\u3093\u6b69\u3044\u3066\u5fc3\u62cd\u304c\u4e0a\u304c\u3063\u3066\u3044\u305f\u306d\u3002\u4f55\u304c\u3042\u3063\u305f\u306e\uff1f"
    if event_type == "heart_rate_low_steps_30min":
        return f"\u4eca\u65e5\u306e{time_label}\u3054\u308d\u3001\u3042\u307e\u308a\u6b69\u3044\u3066\u3044\u306a\u3044\u306e\u306b\u5fc3\u62cd\u304c\u4e0a\u304c\u3063\u3066\u3044\u305f\u307f\u305f\u3044\u3002\u4f55\u304c\u3042\u3063\u305f\u306e\uff1f"
    if event_type == "step_increase_30min":
        return f"今日の{time_label}ごろ、たくさん歩いていたね！何があったの？"
    if event_type == "low_steps_30min":
        return f"今日の{time_label}ごろ、あまり歩いていなかったね。何があったの？"
    if event_type == "heart_rate_sleep_x2":
        return f"今日の{time_label}ごろ、心拍がかなり上がっていたみたい。何があったの？"
    if event_type == "daily_steps_demographic_outlier":
        steps = event.get("value")
        baseline = event.get("baseline")
        if steps is not None and baseline is not None and float(steps) > float(baseline):
            return "今日はたくさん歩いたみたい。どんな1日だった？"
        return "今日、1日通して歩いた量が少なかったみたい。どんな1日だった？"
    if event_type == "daily_steps_normal":
        return "今日はかなり歩いていたね。どんな1日だった？"
    if event_type == "sleep_minutes_demographic_outlier":
        sleep_minutes = event.get("value")
        baseline = event.get("baseline")
        if sleep_minutes is not None and baseline is not None and float(sleep_minutes) > float(baseline):
            return "今日の睡眠時間がいつもより長かったみたい。寝る前や起きた後に何があった？"
        return "今日の睡眠時間がいつもより短かったみたい。寝る前や起きた後に何があった？"
    if event_type == "sleep_minutes_normal":
        return "今日はよく眠れていたね。寝る前や起きた後に何かあった？"
    return f"今日の{time_label}ごろ、いつもと違う変化があったみたい。何があったの？"


# センサー・睡眠・基準値データから異常候補をまとめて検出する。
def detect_candidates(
    sensor_rows: list[dict],
    known_keys: set[tuple[str, str, str]],
    sleep_rows: list[dict] | None = None,
    sleep_stage_rows: list[dict] | None = None,
    participants: dict[str, dict] | None = None,
    demographic_baselines: list[dict] | None = None,
) -> list[CandidateEvent]:
    sleep_rows = sleep_rows or []
    sleep_stage_rows = sleep_stage_rows or []
    participants = participants or {}
    demographic_baselines = demographic_baselines or []

    candidates: list[CandidateEvent] = []
    now = datetime.now(JST)
    sensor_by_participant = group_by(sensor_rows, "participant_id")
    sleep_by_participant = group_by(sleep_rows, "participant_id")
    stage_by_participant = group_by(sleep_stage_rows, "participant_id")

    for participant_id, rows in sensor_by_participant.items():
        sorted_rows = sorted(rows, key=lambda row: parse_dt(row["timestamp"]))
        participant_sleep_rows = sleep_by_participant.get(participant_id, [])
        candidates.extend(detect_step_interval_events(participant_id, sorted_rows, participant_sleep_rows, known_keys, now))
        candidates.extend(
            detect_sleep_heart_rate_events(
                participant_id,
                sorted_rows,
                participant_sleep_rows,
                participants.get(participant_id),
                known_keys,
                now,
            )
        )
        candidates.extend(
            detect_demographic_step_events(
                participant_id,
                sorted_rows,
                participant_sleep_rows,
                participants.get(participant_id),
                demographic_baselines,
                known_keys,
                now,
            )
        )

    for participant_id, sleeps in sleep_by_participant.items():
        candidates.extend(
            detect_demographic_sleep_events(
                participant_id,
                sleeps,
                stage_by_participant.get(participant_id, []),
                participants.get(participant_id),
                demographic_baselines,
                known_keys,
                now,
            )
        )

    return [candidate for candidate in candidates if candidate.event_id]


# 連続する歩数データから活動量の異常区間を検出する。
def detect_step_interval_events(
    participant_id: str,
    rows: list[dict],
    sleep_rows: list[dict],
    known_keys: set[tuple[str, str, str]],
    now: datetime,
) -> list[CandidateEvent]:
    candidates = []
    sleep_intervals = sleep_intervals_from_rows(sleep_rows)
    awake_rows = [row for row in rows if not step_bucket_overlaps_sleep(parse_dt(row["timestamp"]), sleep_intervals)]
    by_time = {parse_dt(row["timestamp"]): row for row in awake_rows}
    for current_time, row in sorted(by_time.items()):
        if row["steps"] is None:
            continue
        steps = int(row["steps"])
        comparison_time = current_time - timedelta(minutes=30)
        comparison_row = by_time.get(comparison_time)
        comparison_steps = (
            int(comparison_row["steps"])
            if comparison_row is not None and comparison_row["steps"] is not None
            else None
        )
        step_increase = steps - comparison_steps if comparison_steps is not None else None
        if step_increase is not None and step_increase >= 2000:
            candidates.append(
                candidate(
                    participant_id,
                    current_time,
                    "step_increase_30min",
                    float(step_increase),
                    2000.0,
                    (
                        f"現在の30分間は{steps}歩、直前の非重複30分間は"
                        f"{comparison_steps}歩で、差が{step_increase}歩でした"
                    ),
                    now,
                    known_keys,
                )
            )

        if steps < 200:
            candidates.append(
                candidate(
                    participant_id,
                    current_time,
                    "low_steps_30min",
                    float(steps),
                    200.0,
                    f"30分間の合計歩数が{steps}歩でした",
                    now,
                    known_keys,
                )
            )
    return candidates


# 睡眠中の心拍データから異常イベントを検出する。
def detect_sleep_heart_rate_events(
    participant_id: str,
    sensor_rows: list[dict],
    sleep_rows: list[dict],
    participant: dict | None,
    known_keys: set[tuple[str, str, str]],
    now: datetime,
) -> list[CandidateEvent]:
    if not participant or participant.get("age") is None:
        return []
    sleep_intervals = sleep_intervals_from_rows(sleep_rows)
    age = int(participant["age"])
    estimated_max_heart_rate = MAX_HEART_RATE_AGE_INTERCEPT - MAX_HEART_RATE_AGE_COEFFICIENT * age
    candidates = []
    for row in sensor_rows:
        detection_heart_rate = row.get("max_heart_rate")
        if detection_heart_rate is None:
            detection_heart_rate = row["heart_rate"]
        if detection_heart_rate is None:
            continue
        timestamp = parse_dt(row["timestamp"])
        if any(start <= timestamp <= end for start, end in sleep_intervals):
            continue
        sleep_hr_mean = sleep_heart_rate_baseline_for_timestamp(timestamp, sensor_rows, sleep_intervals)
        if sleep_hr_mean is None:
            continue
        if timestamp.astimezone(JST) < HEART_RATE_NEW_CRITERION_EFFECTIVE_AT:
            threshold = sleep_hr_mean * LEGACY_HEART_RATE_SLEEP_MULTIPLIER
            threshold_description = (
                f"睡眠中平均{sleep_hr_mean:.1f}の"
                f"{LEGACY_HEART_RATE_SLEEP_MULTIPLIER:.1f}倍の閾値{threshold:.1f}"
            )
        else:
            threshold = sleep_hr_mean + HEART_RATE_RESERVE_INTENSITY * (
                estimated_max_heart_rate - sleep_hr_mean
            )
            threshold_description = (
                f"安静時心拍数{sleep_hr_mean:.1f}と年齢{age}歳から算出した"
                f"40%心拍予備能閾値{threshold:.1f}"
            )
        heart_rate = float(detection_heart_rate)
        if heart_rate > threshold:
            candidates.append(
                candidate(
                    participant_id,
                    timestamp,
                    "heart_rate_sleep_x2",
                    heart_rate,
                    sleep_hr_mean,
                    (
                        f"30分間の最大心拍が{threshold_description}を超えました"
                    ),
                    now,
                    known_keys,
                )
            )
    return candidates


def sleep_heart_rate_baseline_for_timestamp(
    timestamp: datetime,
    sensor_rows: list[dict],
    sleep_intervals: list[tuple[datetime, datetime]],
) -> float | None:
    target_day = timestamp.astimezone(JST).date()
    same_day_rates = sleep_heart_rates_for_sleep_end_days(sensor_rows, sleep_intervals, {target_day})
    if same_day_rates:
        return mean(same_day_rates)

    fallback_days = {
        target_day - timedelta(days=offset)
        for offset in range(1, SLEEP_HEART_RATE_FALLBACK_DAYS + 1)
    }
    fallback_rates = sleep_heart_rates_for_sleep_end_days(sensor_rows, sleep_intervals, fallback_days)
    if fallback_rates:
        return mean(fallback_rates)
    return None


def sleep_heart_rates_for_sleep_end_days(
    sensor_rows: list[dict],
    sleep_intervals: list[tuple[datetime, datetime]],
    target_days: set[date],
) -> list[float]:
    rates = []
    target_intervals = [
        (start, end)
        for start, end in sleep_intervals
        if end.astimezone(JST).date() in target_days
    ]
    if not target_intervals:
        return rates

    for row in sensor_rows:
        if row["heart_rate"] is None:
            continue
        timestamp = parse_dt(row["timestamp"])
        if any(start <= timestamp <= end for start, end in target_intervals):
            rates.append(float(row["heart_rate"]))
    return rates


# 人口統計基準と比較して歩数の異常を検出する。
def detect_demographic_step_events(
    participant_id: str,
    rows: list[dict],
    sleep_rows: list[dict],
    participant: dict | None,
    baselines: list[dict],
    known_keys: set[tuple[str, str, str]],
    now: datetime,
) -> list[CandidateEvent]:
    if not participant:
        return []
    sleep_intervals = sleep_intervals_from_rows(sleep_rows)
    daily_steps: dict[date, int] = {}
    for row in rows:
        if row["steps"] is None:
            continue
        timestamp = parse_dt(row["timestamp"])
        if not is_primary_30min_window(timestamp):
            continue
        if step_bucket_overlaps_sleep(timestamp, sleep_intervals):
            continue
        timestamp = timestamp.astimezone(JST)
        day = timestamp.date()
        if timestamp.time() > time(hour=22):
            continue
        daily_steps[day] = daily_steps.get(day, 0) + int(row["steps"])

    candidates = []
    for day, total_steps in daily_steps.items():
        event_time = datetime.combine(day, time(hour=22), tzinfo=JST)
        if event_time > now:
            continue
        baseline = find_baseline(baselines, "steps", participant, weekday=None)
        if not baseline:
            continue
        lower, upper = bounds_from_baseline(baseline)
        event_type = "daily_steps_demographic_outlier"
        description = f"1???{total_steps}??????????????????"
        if lower <= total_steps <= upper:
            event_type = "daily_steps_normal"
            description = f"1???{total_steps}??????????????????"
        candidates.append(
            candidate(
                participant_id,
                event_time,
                event_type,
                float(total_steps),
                float(baseline["mean_value"]),
                description,
                now,
                known_keys,
            )
        )
    return candidates


# 人口統計基準と比較して睡眠時間の異常を検出する。
def detect_demographic_sleep_events(
    participant_id: str,
    sleep_rows: list[dict],
    sleep_stage_rows: list[dict],
    participant: dict | None,
    baselines: list[dict],
    known_keys: set[tuple[str, str, str]],
    now: datetime,
) -> list[CandidateEvent]:
    if not participant:
        return []
    candidates = []
    for row in sleep_rows:
        if row["sleep_minutes"] is None:
            continue
        start = parse_dt(row["start_time"]).astimezone(JST)
        end = parse_dt(row["end_time"]).astimezone(JST) if row.get("end_time") else start
        weekday = start.weekday()
        baseline = find_baseline(baselines, "sleep_minutes", participant, weekday=weekday)
        if not baseline:
            continue
        sleep_minutes = float(row["sleep_minutes"])
        mean_value = float(baseline["mean_value"])
        tolerance = float(baseline.get("sd_value") or 60)
        event_time = second_collection_time_after(end)
        if event_time > now:
            continue
        event_type = "sleep_minutes_demographic_outlier"
        description = f"????{sleep_minutes:.0f}????????????????{tolerance:.0f}??????????"
        if abs(sleep_minutes - mean_value) < tolerance:
            event_type = "sleep_minutes_normal"
            description = f"????{sleep_minutes:.0f}?????????????????????"
        if known_event_on_day(known_keys, participant_id, end.date(), event_type):
            continue
        candidates.append(
            candidate(
                participant_id,
                event_time,
                event_type,
                sleep_minutes,
                mean_value,
                description,
                now,
                known_keys,
            )
        )
    return candidates


# 同じ参加者・日付・種別のイベントが既にあるか確認する。
def known_event_on_day(
    known_keys: set[tuple[str, str, str]],
    participant_id: str,
    day: date,
    event_type: str,
) -> bool:
    for known_participant_id, event_time, known_event_type in known_keys:
        if known_participant_id != participant_id or known_event_type != event_type:
            continue
        if parse_dt(event_time).astimezone(JST).date() == day:
            return True
    return False


# 指定時刻以降で2回目となる定時収集時刻を求める。
def second_collection_time_after(value: datetime) -> datetime:
    value = value.astimezone(JST)
    boundary = value.replace(second=0, microsecond=0)
    if value.second or value.microsecond:
        boundary += timedelta(minutes=1)

    next_quarter_minute = ((boundary.minute + 14) // 15) * 15
    if next_quarter_minute < 60:
        first_collection = boundary.replace(minute=next_quarter_minute)
    else:
        first_collection = boundary.replace(minute=0) + timedelta(hours=1)
    return first_collection + timedelta(minutes=15)


# 指標と参加者属性に最も合う基準値を選ぶ。
def find_baseline(baselines: list[dict], metric: str, participant: dict, weekday: int | None) -> dict | None:
    sex = participant.get("sex")
    age = participant.get("age")
    if sex is None or age is None:
        return None
    age = int(age)
    exact_weekday_matches = []
    fallback_matches = []
    for baseline in baselines:
        if baseline["metric"] != metric:
            continue
        if baseline["sex"] != sex:
            continue
        if not (int(baseline["age_min"]) <= age <= int(baseline["age_max"])):
            continue
        baseline_weekday = baseline.get("weekday")
        if baseline_weekday is None:
            fallback_matches.append(baseline)
            continue
        if weekday is not None and int(baseline_weekday) == weekday:
            exact_weekday_matches.append(baseline)
    if exact_weekday_matches:
        return exact_weekday_matches[0]
    return fallback_matches[0] if fallback_matches else None


# 基準値から異常判定に使う下限と上限を計算する。
def bounds_from_baseline(baseline: dict) -> tuple[float, float]:
    mean_value = float(baseline["mean_value"])
    sd_value = baseline.get("sd_value")
    lower = baseline.get("lower_bound")
    upper = baseline.get("upper_bound")
    if lower is None:
        lower = mean_value - float(sd_value or 0)
    if upper is None:
        upper = mean_value + float(sd_value or 0)
    return float(lower), float(upper)


# 行データを指定キーの値ごとにまとめる。
def group_by(rows: list[dict], key: str) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        grouped.setdefault(row[key], []).append(row)
    return grouped


# 許容範囲内で対象時刻に最も近い行を返す。
def nearest_row(rows_by_time: dict[datetime, dict], target: datetime, tolerance: timedelta) -> dict | None:
    if not rows_by_time:
        return None
    nearest_time = min(rows_by_time, key=lambda item: abs(item - target))
    if abs(nearest_time - target) <= tolerance:
        return rows_by_time[nearest_time]
    return None


# 指定区間内の歩数を合計する。
def sum_steps_between(rows: list[dict], start: datetime, end: datetime) -> int | None:
    values = [
        int(row["steps"])
        for row in rows
        if row["steps"] is not None and start < parse_dt(row["timestamp"]) <= end
    ]
    if not values:
        return None
    return sum(values)


# 睡眠行から開始・終了時刻の区間一覧を作る。
def sleep_intervals_from_rows(sleep_rows: list[dict]) -> list[tuple[datetime, datetime]]:
    return [
        (parse_dt(row["start_time"]), parse_dt(row["end_time"]))
        for row in sleep_rows
        if row.get("start_time") and row.get("end_time")
    ]


# 歩数集計区間が睡眠区間と重なるか判定する。
def step_bucket_overlaps_sleep(
    timestamp: datetime,
    sleep_intervals: list[tuple[datetime, datetime]],
    bucket_minutes: int = 30,
) -> bool:
    bucket_start = timestamp
    bucket_end = bucket_start + timedelta(minutes=bucket_minutes)
    return any(start < bucket_end and end > bucket_start for start, end in sleep_intervals)


# 時刻が主要な30分収集枠に該当するか判定する。
def is_primary_30min_window(timestamp: datetime) -> bool:
    local = timestamp.astimezone(JST)
    return local.minute in (0, 30) and local.second == 0 and local.microsecond == 0


# 値と基準範囲から異常候補を生成する。
def candidate(
    participant_id: str,
    event_time: datetime,
    event_type: str,
    value: float | None,
    baseline: float | None,
    description: str,
    now: datetime,
    known_keys: set[tuple[str, str, str]],
) -> CandidateEvent:
    event_time_iso = to_iso(event_time)
    if (participant_id, event_time_iso, event_type) in known_keys:
        return CandidateEvent("", participant_id, event_time_iso, event_type, value, baseline, description, "", "", "")
    return CandidateEvent(
        event_id=f"evt_{uuid4().hex[:12]}",
        participant_id=participant_id,
        event_time=event_time_iso,
        event_type=event_type,
        value=value,
        baseline=baseline,
        description=description,
        status="pending",
        detected_at=to_iso(now),
        expires_at=to_iso(event_time + timedelta(days=3)),
    )


# 異常イベントの種類に応じた質問文を作る。
def event_message(event: dict) -> str:
    event_type = event["event_type"]
    time_range_label = event_time_range_label(event)
    date_prefix = event_date_prefix(event)

    if event_type == "heart_rate_step_increase_30min":
        return f"{date_prefix}{time_range_label}ごろ、たくさん歩いて心拍が上がっていたね。何があったの？"
    if event_type == "heart_rate_low_steps_30min":
        return f"{date_prefix}{time_range_label}ごろ、あまり歩いていないのに心拍が上がっていたみたい。何があったの？"
    if event_type == "step_increase_30min":
        return f"{date_prefix}{time_range_label}ごろ、たくさん歩いていたね！何があったの？"
    if event_type == "low_steps_30min":
        return f"{date_prefix}{time_range_label}ごろ、あまり歩いていなかったね。何があったの？"
    if event_type == "heart_rate_sleep_x2":
        return f"{date_prefix}{time_range_label}ごろ、心拍がかなり上がっていたみたい。何があったの？"
    if event_type == "daily_steps_demographic_outlier":
        steps = event.get("value")
        baseline = event.get("baseline")
        if steps is not None and baseline is not None and float(steps) > float(baseline):
            return "今日はたくさん歩いたみたい。どんな1日だった？"
        return "今日、1日通して歩いた量が少なかったみたい。どんな1日だった？"
    if event_type == "daily_steps_normal":
        return "今日はほど良く歩いていたね。どんな1日だった？"
    if event_type == "sleep_minutes_demographic_outlier":
        sleep_minutes = event.get("value")
        baseline = event.get("baseline")
        if sleep_minutes is not None and baseline is not None and float(sleep_minutes) > float(baseline):
            return "今日の睡眠時間がいつもより長かったみたい。寝る前や起きた後に何があった？"
        return "今日の睡眠時間がいつもより短かったみたい。寝る前や起きた後に何があった？"
    if event_type == "sleep_minutes_normal":
        return "今日はよく眠れていたね。寝る前や起きた後に何かあった？"

    return f"{date_prefix}{time_range_label}ごろ、いつもと違う変化があったみたい。何があったの？"


# イベントの開始・終了時刻を表示用文字列にする。
def event_time_range_label(event: dict) -> str:
    start_value = event.get("display_start_time") or event["event_time"]
    start = parse_dt(start_value).astimezone(JST)
    end_value = event.get("display_end_time") or event.get("event_end_time")
    end = parse_dt(end_value).astimezone(JST) if end_value else start + timedelta(minutes=30)
    return f"{start.strftime('%H:%M')}~{end.strftime('%H:%M')}"


# イベント日が基準日と異なる場合の日付接頭辞を作る。
def event_date_prefix(event: dict, reference: datetime | None = None) -> str:
    event_date = parse_dt(event["event_time"]).astimezone(JST).date()
    current_date = (reference or datetime.now(JST)).astimezone(JST).date()
    if event_date == current_date:
        return "今日の"
    if event_date == current_date - timedelta(days=1):
        return "昨日の"
    return f"{event_date.month}/{event_date.day}の"
