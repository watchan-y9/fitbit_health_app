# 健康データ異常検知・行動確認システム システム仕様書

## 1. 文書情報

| 項目 | 内容 |
|---|---|
| 文書版 | 1.0 |
| 作成日 | 2026-07-28 |
| 対象 | `fitbit_health_app` 現行実装 |
| 関連文書 | `docs/requirements_definition.md` |

本書は現行コードの外部仕様と主要な内部処理を示す。将来構想ではなく、実装済みの挙動を優先して記載する。

## 2. システム構成

| コンポーネント | 技術 | 責務 |
|---|---|---|
| REST API | Python、FastAPI、Pydantic | データ登録、イベント検出、質問・回答登録、CSV出力 |
| 定期処理 | Python | データ取得、DB登録、異常検知、Slack通知 |
| 検出エンジン | Python | 歩数・心拍・睡眠のルールベース判定 |
| データストア | SQLite `data/app.db` | 全研究データおよびSlack回答途中状態の保存 |
| Fitbitクライアント | Python、Fitbit Web API | OAuth更新、歩数・心拍取得 |
| Google Healthクライアント | Python、Google Health API | OAuth更新、歩数・心拍・睡眠取得 |
| Slack Bridge | Slack Bolt、Socket Mode | DM送信、二段階回答受信 |
| Kebbi Bridge | Python TCPクライアント | APIから質問取得、Kebbi読み上げ、回答登録 |
| Kebbi Androidアプリ | Kotlin、Jetpack Compose、Nuwa SDK | TCP JSONコマンド受付、ロボット制御 |

標準データフローは次のとおり。

```text
Fitbit / Google Health / CSV
            |
            v
  backend/automation.py  -- 15分周期
            |
            +--> SQLiteへ保存
            +--> anomaly_detection.py
            +--> anomaly_events
            +--> Slack通知
                       |
                       v
                 Slack参加者
                       |
                       v
              行動回答 + 感情回答
                       |
                       v
                    SQLite
                       |
                       v
              分析用CSVエクスポート
```

## 3. 実行・設定仕様

### 3.1 バックエンドAPI

```powershell
cd backend
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

- OpenAPI UI: `http://127.0.0.1:8000/docs`
- API起動時にDB初期化を実行する。
- DBパスはリポジトリ直下の`data/app.db`で固定されている。

### 3.2 定期処理

```powershell
python backend\automation.py --once
python backend\automation.py
python backend\automation.py --once --dry-run
python backend\automation.py --config config\automation.example.json
```

| 項目 | 仕様 |
|---|---|
| 周期 | 15分固定 |
| 起動後の通常実行 | 次の15分境界まで待機して繰り返す |
| 初回遡及 | 起動日と前日 |
| 通常遡及 | DB最新日付の前日から現在まで。ただし`backfill_days`内 |
| 検出 | `detect_after_import=true`かつ非dry-run時 |
| Slack通知 | 取込・検出後に実行 |
| dry-run | 外部取得・CSV読込件数を確認し、DB書込み・検出・通知を行わない |

設定例:

```json
{
  "interval_minutes": 15,
  "detect_after_import": true,
  "participants": [
    {
      "participant_id": "example_001",
      "sex": "female",
      "age": 20,
      "source": "google_health",
      "env_prefix": "GOOGLE",
      "detail_level": "30min",
      "backfill_days": 7
    }
  ]
}
```

`interval_minutes`は設定値にかかわらず15分、`detail_level`は30分へ補正される。`source`は`csv`、`fitbit`、`google_health`を受け付ける。

### 3.3 Slack

```powershell
python slack_bridge\anomaly_bot.py
python slack_bridge\anomaly_bot.py --notify-once
```

- 通常モードはSocket ModeでDM回答を常時受信する。
- `--notify-once`は参加者ごとに通知可能なイベントを最大1件送る。
- 単一ワークスペース設定と複数ワークスペース設定の双方をサポートする。
- 送信先は`slack_user_id`を優先し、未指定の場合は`slack_channel_id`を使う。

主要環境変数:

| 変数 | 用途 |
|---|---|
| `SLACK_BOT_TOKEN` | Bot token |
| `SLACK_APP_TOKEN` | Socket Mode app token |
| `SLACK_PARTICIPANT_MAP` | 参加者対応表 |
| `SLACK_BOT_ID` | DBへ保存する質問チャネル識別子 |
| `SLACK_WORKSPACES_CONFIG` | 複数ワークスペース設定 |

### 3.4 外部API認証

参加者ごとに`env_prefix`を変えることで認証情報を分離する。

```env
FITBIT_CLIENT_ID=
FITBIT_CLIENT_SECRET=
FITBIT_REFRESH_TOKEN=

GOOGLE_CLIENT_ID=
GOOGLE_CLIENT_SECRET=
GOOGLE_REFRESH_TOKEN=
```

例として`env_prefix=GOOGLE_EXAMPLE_002`の場合は、`GOOGLE_EXAMPLE_002_CLIENT_ID`等を参照する。

## 4. データ取得・集計仕様

### 4.1 Fitbit

- Fitbit OAuth 2.0 refresh tokenからaccess tokenを更新する。
- アクティビティintradayと心拍intradayを取得する。
- 日付と時刻をJSTの日時へ変換する。
- 30分単位に歩数、平均心拍、最大心拍、推定活動分を生成する。
- 更新されたrefresh tokenは`.env`の該当キーへ反映する。

### 4.2 Google Health

- Google OAuth refresh tokenからaccess tokenを取得する。
- 取得対象は歩数、心拍、睡眠区間、睡眠ステージである。
- 歩数と心拍から30分集計の`SensorDataIn`を生成する。
- 心拍は平均値と最大値を保持する。
- API環境差を考慮し、データ型候補を環境変数で上書きできる。

### 4.3 CSV

必要列と任意列:

| 列 | 必須 | 型 |
|---|---|---|
| `participant_id` | 任意。未指定時は設定値 | 文字列 |
| `timestamp` | 必須 | ISO 8601日時 |
| `heart_rate` | 任意 | 数値 |
| `max_heart_rate` | 任意 | 数値 |
| `steps` | 任意 | 整数 |
| `calories` | 任意 | 数値 |
| `active_minutes` | 任意 | 数値 |

文字コードはUTF-8 BOMの有無を許容する。

## 5. イベント検出仕様

### 5.1 共通仕様

- API実行時刻から3日と30分前までを検出対象の下限とする。
- 未完了30分枠を避けるため、現在時刻の30分前までのセンサーデータを使う。
- イベントの一意性は`participant_id + event_time + event_type`で論理判定する。
- 新規イベントIDは`evt_`とUUID由来12桁の16進文字列で構成する。
- 初期状態は`pending`、有効期限はイベント時刻の3日後とする。
- 日時文字列にタイムゾーンがない場合はJSTとして扱う。

### 5.2 イベント種別

| event_type | 判定 | value | baseline | イベント時刻 |
|---|---|---|---|---|
| `step_increase_30min` | 対象30分の歩数と30分前の非重複枠の歩数との差が2,000歩以上 | 歩数差 | 2,000 | 対象枠開始 |
| `low_steps_30min` | 30分歩数が200歩未満 | 30分歩数 | 200 | 対象枠開始 |
| `heart_rate_sleep_x2` | 覚醒中の30分最大心拍が心拍基準を超過 | 最大心拍 | 睡眠時平均心拍 | 対象枠時刻 |
| `daily_steps_demographic_outlier` | 22:00までの1日歩数が属性基準範囲外 | 1日歩数 | 属性平均 | 当日22:00 |
| `daily_steps_normal` | 22:00までの1日歩数が属性基準範囲内 | 1日歩数 | 属性平均 | 当日22:00 |
| `sleep_minutes_demographic_outlier` | 睡眠時間と属性・曜日平均の差が許容値以上 | 睡眠分 | 属性平均 | 睡眠終了後2回目の15分収集時刻 |
| `sleep_minutes_normal` | 睡眠時間と属性・曜日平均の差が許容値未満 | 睡眠分 | 属性平均 | 睡眠終了後2回目の15分収集時刻 |
| `heart_rate_step_increase_30min` | Slack通知選定時に高心拍と高歩数を結合した表示用種別 | 元イベント値 | 元イベント基準 | 結合区間開始 |
| `heart_rate_low_steps_30min` | Slack通知選定時に高心拍と低歩数を結合した表示用種別 | 元イベント値 | 元イベント基準 | 結合区間開始 |

### 5.3 歩数判定

- `sensor_data.steps`は30分区間の合計値として扱う。
- 睡眠区間と対象30分枠が少しでも重なる場合、その枠を除外する。
- 日次歩数はJSTの00:00～22:00、分が00または30の主要枠だけを合計する。
- 日次基準値の検索では曜日を指定せず、性別・年齢階級が一致する汎用基準を使う。

### 5.4 心拍判定

覚醒中の心拍のみを対象とする。判定値は`max_heart_rate`を優先し、ない場合は`heart_rate`を使う。

2026-07-22 00:00 JST以降:

```text
推定最大心拍 = 208 - 0.7 × 年齢
閾値 = 睡眠時平均心拍 + 0.4 × (推定最大心拍 - 睡眠時平均心拍)
判定 = 30分最大心拍 > 閾値
```

2026-07-22より前の過去データ:

```text
閾値 = 睡眠時平均心拍 × 1.8
```

睡眠時平均心拍は、対象日の睡眠終了日に属する睡眠区間の心拍を優先し、存在しない場合は過去7日を使う。年齢または睡眠時心拍がなければ判定しない。

### 5.5 属性基準

基準CSV:

```csv
metric,sex,age_min,age_max,weekday,mean_value,sd_value,lower_bound,upper_bound,unit,source
```

- `metric`: `steps`または`sleep_minutes`
- `weekday`: Python方式の0=月曜～6=日曜。空欄は全曜日共通
- 性別が完全一致し、年齢が`age_min <= age <= age_max`の行を使う。
- 睡眠は曜日一致を優先し、なければ曜日空欄へフォールバックする。
- 下限・上限が未指定の場合は`mean_value ± sd_value`とする。
- 睡眠の許容値は`sd_value`、未指定時は60分とする。

## 6. Slack通知・対話仕様

### 6.1 通知制御定数

| 項目 | 値 |
|---|---|
| 質問クールダウン | 2時間 |
| 日次・睡眠イベントの最大経過時間 | 12時間 |
| 区間イベントの最大経過時間 | 2時間 |
| 標準静穏時間 | 22:00～07:00 JST |
| 起床後通知待機 | 40分 |
| 静穏時間中に許可する種別 | 日次歩数の異常・通常イベント |

参加者設定に静穏時間の上書きがある場合はそれを優先する。睡眠区間内と判定した場合も通知しない。

### 6.2 通知対象選定

1. `pending`イベントを取得する。
2. 期限切れ、古いイベント、前日の区間イベントを抑制する。
3. 睡眠中、静穏時間、クールダウン対象を除外または抑制する。
4. 同時刻帯の高心拍と歩数イベントを、通知用の複合イベントへまとめる。
5. 日次・睡眠イベントまたは完了した区間イベントを通知候補とする。
6. 時刻順で最初の通知可能イベントを1件選ぶ。

歩数区間は連続性を確認してから通知する。高歩数を含む連続列は最初の高歩数を代表とする。低歩数のみの場合は原則60分以上継続した列を質問対象とし、それ未満は抑制する。代表以外は`collapsed`、通知不要と判断したイベントは`suppressed`へ変更する。

### 6.3 送信

1. `slack_user_id`があれば`conversations.open`でDMチャンネルを得る。
2. イベント種別・時間帯から生成した質問文を`chat.postMessage`で送る。
3. 成功後に`anomaly_questions`へ記録し、代表イベントを`asked`へ変更する。
4. 送信失敗時はDB状態を変更せず、エラーをログ出力する。

### 6.4 回答

```text
Bot: イベントに応じた「何があったの？」という質問
参加者: 自由記述（行動）
Bot: 「そのときどんな気持ちだった？」等
参加者: 自由記述（感情）
Bot: 「ありがとう、記録したよ。」
```

- 一つ目の回答は`slack_response_drafts.activity_text`へ一時保存する。
- 日次歩数イベントの感情質問は「1日を通して、どんな気持ちだった？」とする。
- 二つ目の回答時に`anomaly_responses`へ`response_type=text`で保存する。
- `response_value`に行動、`response_text`に感情を格納する。
- 回答時刻と最新質問時刻の差を秒で`response_latency_sec`へ保存する。
- 保存後にイベントを`answered`へ変更し、下書きを削除する。

## 7. REST API仕様

### 7.1 エンドポイント一覧

| Method | Path | 概要 | 成功 |
|---|---|---|---|
| GET | `/health` | 稼働確認 | `{"status":"ok"}` |
| POST | `/sensor-data` | 30分集計センサーデータのupsert | 保存行 |
| POST | `/raw-sensor-data` | 未加工センサーデータのupsert | 保存行 |
| POST | `/raw-sleep-data` | 未加工睡眠データのupsert | 保存行 |
| POST | `/sleep-data` | 睡眠区間のupsert | 保存行 |
| POST | `/sleep-stage-data` | 睡眠ステージのupsert | 保存行 |
| POST | `/detect-anomalies` | 保存済みデータからイベント生成 | 件数とイベント配列 |
| GET | `/participants/{participant_id}/pending-anomalies` | 有効な最古の保留イベントを1件取得 | イベントまたはnull |
| POST | `/anomalies/{event_id}/asked` | 質問済み登録 | 状態とevent_id |
| POST | `/anomaly-responses` | 回答登録 | 状態とevent_id |
| GET | `/export/anomaly-analysis` | 分析用CSV出力 | CSVダウンロード |

### 7.2 主要リクエスト

センサーデータ:

```json
{
  "participant_id": "example_001",
  "timestamp": "2026-07-28T10:00:00+09:00",
  "heart_rate": 78.2,
  "max_heart_rate": 112.0,
  "steps": 850,
  "calories": 42.5,
  "active_minutes": 12.0
}
```

質問済み:

```json
{
  "robot_id": "slack_bot",
  "asked_time": "2026-07-28T10:35:00+09:00"
}
```

回答:

```json
{
  "event_id": "evt_012345abcdef",
  "participant_id": "example_001",
  "robot_id": "slack_bot",
  "response_time": "2026-07-28T10:38:00+09:00",
  "response_type": "text",
  "response_value": "駅まで急いで歩いていた",
  "response_text": "少し焦っていた"
}
```

### 7.3 エラー

- Pydantic検証エラー: HTTP 422
- 質問済みまたは回答登録時にイベントがない: HTTP 404、`event not found`
- 外部API、Slack、DB例外の統一エラー形式は未実装である。
- API認証、レート制限、冪等性キーは未実装である。

### 7.4 CSV出力列

`event_id, participant_id, event_time, event_type, value, baseline, description, asked_time, activity_question, activity_answer, feeling_question, feeling_answer, response_time, response_value, response_text, status`

- イベントを基点に質問・回答をLEFT JOINする。
- `event_time`昇順で出力する。
- UTF-8 BOM付き、ファイル名は`anomaly_analysis.csv`とする。
- 同一イベントに質問または回答が複数ある場合、組合せ分の行が出力される。

## 8. DB仕様

### 8.1 テーブル一覧

| テーブル | 主キー | 用途 | 主な一意制約 |
|---|---|---|---|
| `participants` | `participant_id` | 参加者属性 | 参加者ID |
| `sensor_data` | `id` | 30分集計センサー | 参加者ID＋時刻 |
| `raw_sensor_data` | `id` | 未加工センサー | 参加者＋時刻＋型＋取得元 |
| `raw_sleep_data` | `id` | 未加工睡眠JSON | 参加者＋開始＋終了＋型＋取得元 |
| `sleep_data` | `id` | 睡眠区間 | 参加者＋開始＋取得元 |
| `sleep_stage_data` | `id` | 睡眠ステージ | 参加者＋睡眠開始＋開始＋ステージ＋取得元 |
| `demographic_baselines` | `baseline_id` | 属性別基準値 | 指標＋性別＋年齢範囲＋曜日＋出典 |
| `anomaly_events` | `event_id` | 検出イベント | event_id |
| `anomaly_questions` | `question_id` | 質問送信履歴 | なし |
| `anomaly_responses` | `response_id` | 回答履歴 | なし |
| `slack_response_drafts` | 実装定義 | Slack回答途中状態 | event_id |

### 8.2 イベント状態

| 状態 | 意味 |
|---|---|
| `pending` | 検出済み・未通知 |
| `asked` | 質問送信済み・未回答 |
| `answered` | 回答保存済み |
| `suppressed` | 通知条件により質問対象外 |
| `collapsed` | 連続イベントの代表イベントへ集約済み |

現行APIは状態遷移の厳密な検証を行わない。たとえば回答APIは`pending`イベントにも回答を登録でき、同一イベントへの複数回答も許容する。

### 8.3 外部キーと削除

- センサー、睡眠、イベントは`participants`を参照する。
- 質問・回答は`anomaly_events`を参照する。
- SQLite接続ごとに`PRAGMA foreign_keys=ON`を設定する。
- CASCADE削除は定義されていない。
- 参加者・イベントを削除する業務機能はない。

## 9. Kebbi仕様

### 9.1 Bridge

`kebbi_bridge/research_prompt.py`は次の手順を実行する。

1. バックエンドの保留イベント取得APIを呼ぶ。
2. イベントがあればKebbi TCPクライアントへ質問文を送る。
3. 質問済みAPIを呼ぶ。
4. 利用者の回答を受け取り、回答APIへ保存する。
5. `--dummy`指定時は実機通信を行わず検証する。

### 9.2 Androidソケットサーバー

- 既定ポートはアプリ画面で設定し、1024～65535を許容する。
- 改行区切りのJSONコマンドをTCPで受け付ける。
- 基本要求は`id`、`category`、`action`、`params`、`timestamp`で構成する。
- 応答は`id`、`success`、`message`、`data`、`timestamp`で構成する。
- Nuwa SDKを使い、TTS、音声認識、顔、モーション、モーター、質問画面等を制御する。
- TCP通信の認証と暗号化は実装されていないため、信頼できるローカルネットワーク内でのみ使用する。

## 10. ログ・障害処理

| 処理 | 現行動作 |
|---|---|
| Fitbit/Google Health取得失敗 | 参加者単位で例外を捕捉し、ログ出力後に次参加者へ進む |
| 未対応取得元 | スキップしてログ出力 |
| Slack SDK未導入 | 通知をスキップしエラー件数を返す |
| Slack送信失敗 | イベントを`pending`に残し、エラー件数を加算 |
| Slack回答下書き保存失敗 | 参加者へ再試行を案内 |
| DB障害 | 一部処理では上位へ例外送出。統一リトライなし |
| Kebbi通信失敗 | Bridge実行がエラー。自動再試行なし |

ログは標準出力中心であり、ファイルローテーション、構造化ログ、監視通知は未実装である。

## 11. セキュリティ仕様

- APIには認証・認可がないため、既定では`127.0.0.1`で起動する。
- OAuth/Slackトークンは`.env`または環境変数から読む。
- 実Slack設定はサンプルとは別の非公開ファイルにする。
- SlackはSocket Modeを使い、受信HTTPエンドポイントを公開しない。
- Kebbi TCPサーバーには認証・暗号化がない。
- SQLite DBには暗号化機能を適用していない。

本番または学外ネットワークへ公開する場合は、リバースプロキシによるTLS、API認証、アクセス制御、秘密情報管理、DB暗号化または端末暗号化を別途実装する。

## 12. 既知の制約・技術的注意事項

1. `anomaly_detection.py`には`event_message`が複数回定義されており、Python上は最後の定義のみが有効である。
2. 一部イベントの`description`生成文字列に文字化けが残っている。
3. APIの保留イベント取得は単純な最古1件であり、Slack Bridgeの高度な通知選定とは挙動が異なる。
4. Slack BridgeはAPIを経由せずSQLiteヘルパーを直接使用する。
5. SQLiteへ複数プロセスが同時書込みするため、高負荷時にはロック競合の可能性がある。
6. 質問と回答に一意制約がなく、API再送で重複履歴を作成できる。
7. イベント期限切れを専用状態へ更新する処理はなく、取得・通知時に除外する。
8. CSV出力は質問・回答の複数行を集約しない。
9. 現行DBパスは設定変更できない。
10. 自動テスト一式はリポジトリ内に確認できず、受入条件の自動回帰試験は未整備である。
