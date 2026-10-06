# Fitbit-Kebbi anomaly confirmation MVP

このプロジェクトは、Fitbit / Google Health 由来のセンサーデータから「普段と違う身体・行動変化」を検出し、Kebbi が後から利用者に「そのとき何をしていましたか？」と確認し、研究分析用 CSV として保存するための MVP です。

## 推奨構成

```text
fitbit_health_app/
  backend/
    app/
      main.py                 # FastAPI の API 定義
      database.py             # SQLite 初期化
      schemas.py              # API 入出力モデル
      anomaly_detection.py    # ルールベース検出
    requirements.txt
    load_sample_csv.py
  sample_data/
    fitbit_sample.csv
  kebbi-app-main/             # zip から展開した Kebbi/Android 側の既存アプリ
  kebbi_bridge/
    research_prompt.py        # バックエンドと Kebbi ソケットサーバの橋渡し
  data/
    app.db                    # 実行時に自動生成
```

## 研究システムの流れ

1. `POST /sensor-data` でセンサーデータを保存する
2. `POST /detect-anomalies` で未確認アノマリーを検出する
3. Kebbi が人を検知したタイミングで `GET /participants/{participant_id}/pending-anomalies` を呼ぶ
4. 返ってきた質問文と選択肢を Kebbi が発話・表示する
5. 質問したら `POST /anomalies/{event_id}/asked` を呼ぶ
6. 回答されたら `POST /anomaly-responses` を呼ぶ
7. `GET /export/anomaly-analysis` で分析用 CSV を出力する

## 起動

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python load_sample_csv.py
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

API ドキュメントは `http://127.0.0.1:8000/docs` で確認できます。

## Kebbi 側の使い方

`kebbi-app-main` は、Kebbi 上で動く Android アプリです。アプリ内の開始ボタンでソケットサーバを起動すると、PC から TTS や表情制御の JSON コマンドを送れます。

研究MVPでは、まず PC 側のブリッジで「Kebbi が人を検知した」状態を模擬します。

```powershell
python kebbi_bridge\research_prompt.py --kebbi-host <KebbiのIPアドレス> --participant-id example_001
```

このスクリプトは、バックエンドから未確認アノマリーを 1 件取得し、Kebbi に発話させ、質問済みとして記録し、回答を保存します。Kebbi 実機が手元にないときは `--dummy` を付けると、ソケット送信なしでバックエンド部分だけ確認できます。

```powershell
python kebbi_bridge\research_prompt.py --kebbi-host 127.0.0.1 --participant-id example_001 --dummy
```

## 最初の検出ルール

- `heart_rate_spike`: 心拍数が 110 以上、または参加者平均 + 30 以上
- `high_steps`: 区間歩数が 3000 以上、または参加者平均の 2 倍以上
- `low_activity`: 歩数が 20 以下、活動分が 0、心拍数が高くない

閾値は仮置きです。研究の本番では参加者ごと、曜日ごと、時間帯ごとのベースラインに発展させるのが自然です。

## CSV の文字コード

`GET /export/anomaly-analysis` は、Windows 版 Excel で直接開いても日本語が崩れにくいように、UTF-8 BOM 付きの CSV を返します。分析ソフトで読み込む場合は、文字コードを `UTF-8` または `UTF-8 with BOM` として扱ってください。

## データ取得と検出の自動実行

Kebbi 連携を入れる前段階として、PC 側では `backend/automation.py` でセンサーデータの取り込みとアノマリー検出を定期実行できます。

標準設定は `config/automation.example.json` です。現在は `example_001` を Google Health API 連携として扱い、30 分に 1 回、歩数データを取得してからアノマリー検出を実行します。実行間隔は最低 15 分に制限しています。

1 回だけ試す場合:

```powershell
.\.venv\Scripts\python.exe backend\automation.py --once
```

定期実行する場合:

```powershell
.\.venv\Scripts\python.exe backend\automation.py
```

止めるときは `Ctrl + C` を押します。

まず読み込みだけ確認したい場合:

```powershell
.\.venv\Scripts\python.exe backend\automation.py --once --dry-run
```

CSV だけで動かしたい場合は、次のように CSV 用設定を指定します。

```powershell
.\.venv\Scripts\python.exe backend\automation.py --config config\automation.csv.example.json
```

3 人に増やす場合は、`config/automation.example.json` の `participants` に `example_001`, `example_002`, `example_003` を追加し、それぞれの Google OAuth 情報を `.env` に入れます。

```env
GOOGLE_CLIENT_ID=...
GOOGLE_CLIENT_SECRET=...
GOOGLE_REFRESH_TOKEN=...

GOOGLE_EXAMPLE_002_CLIENT_ID=...
GOOGLE_EXAMPLE_002_CLIENT_SECRET=...
GOOGLE_EXAMPLE_002_REFRESH_TOKEN=...
```

参加者ごとに `env_prefix` を `GOOGLE_EXAMPLE_002` のように分けると、別々の認証情報を使えます。

Google Health API 連携で現在使用するデータは、`steps`, `heart_rate`, `sleep` です。歩数と心拍は `sensor_data` に保存し、睡眠セッションは `sleep_data`、睡眠の深さは `sleep_stage_data` に保存します。`active_minutes` は歩数と心拍から簡易推定しています。

`backend/automation.py` は、起動時に DB の最新保存時刻を確認し、前回取得地点から現在までの期間を日付単位で穴埋め取得します。標準では最大 7 日前までさかのぼります。

```json
"backfill_days": 7
```

PCを止めていた時間があっても、次回起動時に Google Health API 側に残っているデータをまとめて取り込みます。

Google Health API 側のデータ型 ID が環境によって違う場合は、`.env` で以下を変更してください。

```env
GOOGLE_STEPS_DATA_TYPE=steps
GOOGLE_HEART_RATE_DATA_TYPES=heart-rate,heart_rate,heartRate,heart_rate_bpm
GOOGLE_SLEEP_DATA_TYPES=sleep,sleep_session,sleepSession
```

心拍や睡眠の権限がまだ有効でない場合、そのデータだけスキップして、取得できるデータは保存します。

## アノマリー検出基準

現在の `anomaly_events` は、以下の基準で作成します。

- `step_increase_30min`: 30 分前の歩数との差が `+3000` 歩以上
- `low_steps_30min`: 直近 30 分間の合計歩数が `200` 歩未満
- `heart_rate_sleep_x2`: 日常生活中の心拍が、その人の睡眠中平均心拍の 2 倍を超えた
- `daily_steps_demographic_outlier`: 1 日歩数が、性別・年齢階級別の平均 ± 標準偏差の範囲外
- `sleep_minutes_demographic_outlier`: 睡眠時間が、性別・年齢階級・曜日別の平均から 1 時間以上ずれた

参加者の性別・年齢は `config/automation.example.json` に設定します。

```json
{
  "participant_id": "example_001",
  "sex": "female",
  "age": 20
}
```

公的平均値は `demographic_baselines` テーブルに保存します。まずテンプレートを実データに置き換えてから読み込んでください。

```powershell
.\.venv\Scripts\python.exe backend\load_demographic_baselines.py --csv sample_data\demographic_baselines_template.csv
```

CSV の列は以下です。

```csv
metric,sex,age_min,age_max,weekday,mean_value,sd_value,lower_bound,upper_bound,unit,source
```

`weekday` は Python 形式で、`0=月曜`, `1=火曜`, ..., `6=日曜` です。歩数のように曜日を使わない平均値では空欄にします。

`sample_data/demographic_baselines_template.csv` の値は形式確認用の仮値です。厚労省の国民健康・栄養調査、社会生活基本調査などから取得した正式な値に置き換えてください。

## Android 側で次に足すべきもの

現状の `kebbi-app-main` は「外部から Kebbi を制御するサーバ」としてかなり使えます。一方、研究用には次の 2 点を追加すると完成度が上がります。

- 人感センサー検知時に、Android アプリ自身が `GET /participants/{participant_id}/pending-anomalies` を呼ぶ
- 返ってきた `choices` を Kebbi 画面にボタン表示し、押された値を `POST /anomaly-responses` で送る

MVP の最短ルートは、まず `kebbi_bridge/research_prompt.py` で研究ログの流れを確認し、その後 Android 側に同じ API 呼び出しを移植することです。
