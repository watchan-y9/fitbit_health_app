from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


# 集計済みセンサーデータの入力項目を定義する。
class SensorDataIn(BaseModel):
    participant_id: str = Field(..., examples=["example_001"])
    timestamp: datetime
    heart_rate: Optional[float] = None
    max_heart_rate: Optional[float] = None
    steps: Optional[int] = None
    calories: Optional[float] = None
    active_minutes: Optional[float] = None


# 保存済みセンサーデータの応答項目を定義する。
class SensorDataOut(SensorDataIn):
    id: int


# 未加工センサーデータの入力項目を定義する。
class RawSensorDataIn(BaseModel):
    participant_id: str = Field(..., examples=["example_001"])
    timestamp: datetime
    data_type: str = Field(..., examples=["heart_rate"])
    value: float
    source: str = "google_health"


# 保存済み未加工センサーデータの応答項目を定義する。
class RawSensorDataOut(RawSensorDataIn):
    id: int


# 未加工睡眠データの入力項目を定義する。
class RawSleepDataIn(BaseModel):
    participant_id: str = Field(..., examples=["example_001"])
    start_time: datetime
    end_time: Optional[datetime] = None
    data_type: str = "sleep"
    stage: Optional[str] = None
    value: Optional[float] = None
    raw_json: str
    source: str = "google_health"


# 保存済み未加工睡眠データの応答項目を定義する。
class RawSleepDataOut(RawSleepDataIn):
    id: int


# 睡眠区間データの入力項目を定義する。
class SleepDataIn(BaseModel):
    participant_id: str = Field(..., examples=["example_001"])
    start_time: datetime
    end_time: Optional[datetime] = None
    sleep_minutes: Optional[float] = None
    sleep_stage: Optional[str] = None
    source: str = "google_health"


# 保存済み睡眠区間データの応答項目を定義する。
class SleepDataOut(SleepDataIn):
    id: int


# 睡眠ステージデータの入力項目を定義する。
class SleepStageDataIn(BaseModel):
    participant_id: str = Field(..., examples=["example_001"])
    sleep_start_time: datetime
    start_time: datetime
    end_time: Optional[datetime] = None
    stage: str
    stage_minutes: Optional[float] = None
    source: str = "google_health"


# 保存済み睡眠ステージデータの応答項目を定義する。
class SleepStageDataOut(SleepStageDataIn):
    id: int


# 異常イベントの応答項目を定義する。
class AnomalyEventOut(BaseModel):
    event_id: str
    participant_id: str
    event_time: datetime
    event_type: str
    value: Optional[float]
    baseline: Optional[float]
    description: str
    status: str


# 異常検知結果の応答項目を定義する。
class DetectResponse(BaseModel):
    detected_count: int
    events: list[AnomalyEventOut]


# 未回答の異常イベントの応答項目を定義する。
class PendingAnomalyOut(BaseModel):
    event_id: str
    participant_id: str
    event_time: datetime
    event_type: str
    message: str
    choices: list[str]


# 未回答イベント取得APIの応答項目を定義する。
class PendingResponse(BaseModel):
    event: Optional[PendingAnomalyOut]


# 質問送信済み状態の入力項目を定義する。
class AskedIn(BaseModel):
    robot_id: str = "kebbi_001"
    asked_time: datetime


# 異常に対する回答の入力項目を定義する。
class ResponseIn(BaseModel):
    event_id: str
    participant_id: str
    robot_id: str = "kebbi_001"
    response_time: datetime
    response_type: str = "tap"
    response_value: str
    response_text: str = ""
