# Slack anomaly question design

## Goal

Replace the current Kebbi-based question step with a Slack chatbot while keeping the existing anomaly detection, SQLite storage, and CSV export flow.

The current backend already exposes the right media-neutral API:

1. `POST /detect-anomalies` creates `anomaly_events`.
2. `GET /participants/{participant_id}/pending-anomalies` returns one pending anomaly with `message`.
3. `POST /anomalies/{event_id}/asked` records that the question was sent.
4. `POST /anomaly-responses` records the participant response.

Kebbi should therefore be treated as only one question channel. Slack becomes another channel.

## ZIP files to reuse

Use the following files from `IoT-Integration.zip` as references or starting points:

- `IoT-Integration/Fitbit-Switchbot/slack-bot/app-sensor.py`
  - Reuse the Slack Bolt + Socket Mode structure.
  - Reuse the `user_id_map` idea, but move it to a config file instead of hard-coding.
  - Reuse the DM message event handling pattern.
- `IoT-Integration/Fitbit-Switchbot/requirements-slack.txt`
  - Add Slack dependencies from here: `slack-bolt`, `slack-sdk`, and optionally `schedule`.
- `IoT-Integration/Fitbit-Switchbot/Dockerfile.slack`
  - Reuse the container layout if the Slack bot is run as a separate service.
- `IoT-Integration/Fitbit-Switchbot/Fitbit_Fetch.py`
  - Treat as Fitbit ingestion reference only.
  - The current project already has `backend/app/fitbit_client.py`, `backend/app/google_health_client.py`, and `backend/automation.py`, so anomaly detection should continue to use the current SQLite pipeline rather than introducing InfluxDB.

Important: the ZIP Slack files contain token-like values in source code. Do not copy those values. Store Slack tokens only in `.env`, and rotate any token that has been committed or shared.

## Proposed architecture

Add a new Slack bridge process:

```text
backend/automation.py
  -> imports Fitbit / Google Health / CSV data
  -> calls detect_anomalies()
  -> anomaly_events are stored in SQLite

slack_bridge/anomaly_bot.py
  -> checks pending anomalies for configured participants
  -> sends Slack DM with a text question
  -> marks event as asked after successful Slack send
  -> receives text replies
  -> writes responses to the same SQLite tables used by /anomaly-responses

backend/app/main.py
  -> remains the source of truth for anomaly events, questions, and responses
```

Run the Slack bot separately from FastAPI. This matches the ZIP implementation and keeps Slack credentials out of the API server. The implemented bridge uses the backend database helpers directly, so FastAPI does not need to be running for automation-triggered Slack questions or response recording.

## Slack app mode

Use Slack Socket Mode first.

Reasons:

- The ZIP Slack bot already uses `SocketModeHandler`.
- Local development does not need a public HTTPS endpoint.
- A bot token (`xoxb-...`) and app-level token (`xapp-...`) are enough.

Required Slack app features:

- Socket Mode: enabled
- Bot token scopes:
  - `chat:write`
  - `im:write`
  - `im:history`
  - `channels:read` only if channel lookup is needed
- Event subscriptions through Socket Mode:
  - `message.im`
- Event subscriptions:
  - enabled for DM replies

## Participant mapping

Create a config file such as `config/slack_participants.example.json`:

```json
{
  "participants": [
    {
      "participant_id": "example_001",
      "slack_channel_id": "D0000000000",
      "enabled": true
    }
  ]
}
```

This replaces the hard-coded `user_id_map` in the ZIP code. `participant_id` must match the IDs used by `anomaly_events`.

For production, keep the real mapping in an untracked config file, for example `config/slack_participants.json`.

## Environment variables

Add these to `.env.example`:

```env
SLACK_BOT_TOKEN=
SLACK_APP_TOKEN=
SLACK_PARTICIPANT_MAP=config/slack_participants.json
SLACK_BOT_ID=slack_bot
```

`SLACK_BOT_ID` can be sent to the existing `robot_id` field. Later, the backend can rename this concept to `channel_id` or `medium_id`, but no schema change is required for the MVP.

## Message flow

### 1. Send anomaly question

For each enabled participant:

1. Call `GET /participants/{participant_id}/pending-anomalies`.
2. If `event` is `null`, do nothing.
3. Send a Slack DM to `slack_channel_id`.
4. Send `event.message` as a plain text DM.
5. After Slack send succeeds, call `POST /anomalies/{event_id}/asked`.

Do not mark the event as asked before Slack send succeeds.

### 2. Save text response

When the participant replies:

1. Treat the first reply as what they were doing.
2. Ask: `そのときどんな気持ちだった？`
3. Treat the next reply as their feeling.
4. Store the same payload shape that `POST /anomaly-responses` uses:

```json
{
  "event_id": "...",
  "participant_id": "example_001",
  "robot_id": "slack_bot",
  "response_time": "2026-07-03T12:00:00+09:00",
  "response_type": "text",
  "response_value": "what they were doing",
  "response_text": "how they felt"
}
```

5. Send a short thank-you message in the DM.

The first reply is stored temporarily in SQLite until the feeling reply arrives, so restarting the Slack bot does not lose the two-step flow.

## Slack Block Kit shape

Use a compact DM message:

```text
{event.message}
```

## Scheduling strategy

MVP recommendation:

- Keep `backend/automation.py` as the data import and detection scheduler.
- Run `slack_bridge/anomaly_bot.py` continuously to receive text replies.
- `backend/automation.py` calls the Slack notifier once per acquisition cycle before detecting the newest anomalies.

Because Slack notification runs before the current cycle's detection step, a newly detected anomaly is asked at the next acquisition timing. Slack outages do not stop Fitbit import or anomaly detection.

Later improvement:

- Add a backend table for notification attempts and a generic notification worker.
- If same-cycle notification is desired later, move the Slack notifier call after `detect_anomalies()`.

## Data storage

For the MVP, keep all research data in the current SQLite tables:

- `anomaly_events`
- `anomaly_questions`
- `anomaly_responses`

Do not write Slack answers to InfluxDB. The ZIP Slack bot writes labels to InfluxDB because that older system used Influx as its research store. This project already exports research CSV from SQLite, so using SQLite keeps analysis consistent.

The existing `robot_id` field can store `slack_bot` for now:

```text
anomaly_questions.robot_id = "slack_bot"
anomaly_responses.robot_id = "slack_bot"
```

## Files to add or change

Recommended MVP changes:

- Add `slack_bridge/anomaly_bot.py`
  - Based on the structure of ZIP `slack-bot/app-sensor.py`.
  - Remove InfluxDB logic.
  - Replace device alert endpoints with backend polling.
  - Save answers through the FastAPI endpoints.
- Add `config/slack_participants.example.json`
  - Participant-to-Slack DM mapping.
- Update `backend/requirements.txt`
  - Add `slack-bolt` and `slack-sdk`.
  - Add `schedule` only if using the same scheduler style as the ZIP bot.
- Update `.env.example`
  - Add Slack settings.
- Optional: add `Dockerfile.slack`
  - Based on the ZIP `Dockerfile.slack`, adjusted to this repo layout.

## Error handling

Handle these cases explicitly:

- No Slack mapping for participant: log and skip.
- Slack send fails: do not call `/asked`; leave anomaly as `pending`.
- Backend unavailable: retry on the next poll.
- Duplicate polling: keep an in-memory `sent_event_ids` set or rely on marking as `asked` after successful send.
- Bot restart after sending but before `/asked`: possible duplicate DM. This is acceptable for MVP, but can be fixed later with a notification-attempt table.
- Slack action received for already answered event: backend currently accepts another response. Later, add a guard if duplicate answers should be rejected.

## Security notes

- Never commit `.env` or Slack tokens.
- Rotate any token found in the ZIP source files before reuse.
- Keep real participant-to-Slack mapping out of version control.
- If switching from Socket Mode to HTTP Request URL later, verify Slack signatures on every request.

## Migration from Kebbi

Keep `kebbi_bridge/research_prompt.py` for manual fallback during development.

The new default flow should be:

```powershell
# terminal 1
cd backend
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000

# terminal 2
.\.venv\Scripts\python.exe backend\automation.py

# terminal 3
.\.venv\Scripts\python.exe slack_bridge\anomaly_bot.py
```

Once Slack is validated, update README wording from "Fitbit-Kebbi" to a more neutral name such as "Fitbit anomaly confirmation MVP" and describe Kebbi and Slack as supported question channels.
