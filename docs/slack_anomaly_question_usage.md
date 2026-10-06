# Slack anomaly bot usage

## 1. Create participant mapping

Copy the example file:

```powershell
Copy-Item config\slack_participants.example.json config\slack_participants.json
```

Edit `config/slack_participants.json`:

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

`participant_id` must match the participant IDs used by `backend/automation.py`.

## 2. Set Slack environment variables

Add these values to `.env`:

```env
SLACK_BOT_TOKEN=xoxb-...
SLACK_APP_TOKEN=xapp-...
SLACK_PARTICIPANT_MAP=config/slack_participants.json
SLACK_BOT_ID=slack_bot
```

For multiple Slack workspaces, create `config/slack_workspaces.json` from
`config/slack_workspaces.example.json` and set one token pair and one participant
map per workspace:

```env
SLACK_WORKSPACES_CONFIG=config/slack_workspaces.json
SLACK_BOT_TOKEN=xoxb-...
SLACK_APP_TOKEN=xapp-...
SLACK_BOT_TOKEN_WORKSPACE_B=xoxb-...
SLACK_APP_TOKEN_WORKSPACE_B=xapp-...
```

Each workspace needs its own Slack app installation. Keep participant IDs unique
across the participant maps unless the same database participant is intentionally
shared.

The Slack app should use Socket Mode. Required bot scopes are:

- `chat:write`
- `im:write`
- `im:history`

Subscribe to the `message.im` bot event. Interactivity is not required because this flow uses text replies, not buttons.

## 3. Run the Slack response bot

Keep this running while collecting responses:

```powershell
.\.venv\Scripts\python.exe slack_bridge\anomaly_bot.py
```

This process receives the participant's text reply, asks a follow-up about their feeling, and then saves both answers.

## 4. Run data acquisition

Run automation as usual:

```powershell
.\.venv\Scripts\python.exe backend\automation.py
```

Each cycle now runs in this order:

1. Import Fitbit / Google Health / CSV data.
2. Detect new anomalies from the latest imported data.
3. Ask Slack only for anomalies that satisfy the notification timing rules.

Automation runs immediately once at startup, then aligns subsequent acquisition to JST `:00` and `:30`.

At startup, Fitbit / Google Health data is backfilled from 00:00 on the previous JST date through the current date. For example, if the program starts at 14:00 on July 4, it imports from July 3 00:00 onward.

Sensor data is stored in 30-minute buckets. If the same participant and timestamp already exist in the database, the row is updated.

Notification timing rules:

- 30-minute step anomalies are held while consecutive anomaly data continues. When the next data point is not anomalous, Slack asks once using the start time of the anomaly run.
- Low-step anomalies are asked without waiting for a normal next data point when two consecutive 30-minute low-step data points are collected, meaning low steps continued for 60 minutes.
- If a high-step anomaly is followed by a low-step anomaly, Slack asks about the high-step anomaly immediately and collapses the following low-step anomaly.
- If high-step and low-step anomalies are mixed in one consecutive run, the high-step anomaly is used for the question.
- Heart-rate anomalies are also held while consecutive anomaly data continues. When the next data point is not anomalous, Slack asks once using the start time of the anomaly run.
- Step and heart-rate anomalies more than two hours older than the program startup time are suppressed, so backfilled historical interval anomalies do not trigger Slack messages.
- After a question is sent, the same question category is not sent again for 3 hours. Events detected during that 3-hour cooldown are suppressed permanently and are not asked after the cooldown ends.
- Sleep-duration questions are asked at the second 30-minute data collection time after the participant wakes up, even when sleep duration is within the baseline range. For example, if sleep ends at 06:20, the 06:30 collection is first and the 07:00 collection is second, so the question becomes ready at 07:00.
- Daily-step questions use the total steps through 22:00 and are asked at 22:00, even when the total is within the baseline range.
- Slack questions are not sent while the participant is currently recorded as sleeping. Pending questions remain waiting and can be sent after the participant wakes up, unless they become too old.

## 5. Manual notification test

To send one pending anomaly question per mapped participant:

```powershell
.\.venv\Scripts\python.exe slack_bridge\anomaly_bot.py --notify-once
```

If there are no pending anomalies, it prints a zero-count result.

## 6. Saved data

Slack responses are saved in the existing SQLite database:

- `anomaly_questions.message`: the first question sent to Slack
- `anomaly_responses.response_value`: the participant's answer to what happened
- `anomaly_responses.response_text`: the participant's answer to `そのときどんな気持ちだった？`

The CSV export also includes analysis-friendly columns:

- `activity_question`
- `activity_answer`
- `feeling_question`
- `feeling_answer`

Export it from:

```text
GET /export/anomaly-analysis
```
