# tg5 — VSTU events reminder bot

A Telegram bot that reminds the two administrators of the VSTU StartUp Space program about upcoming events, so a session is not missed in the middle of a busy week. The schedule lives in a JSON file, the bot sends the date, weekday, time, place, speakers and details in advance, and keeps track of what has already been sent.

## Features

- Reminders two days and one day before an event, twice a day at 10:00 and 22:00, plus two hours before the start
- Message contains the title, date, weekday, time, place, speakers and key points
- Schedule kept as structured JSON instead of a static document
- Import of a new `.docx` program file, including splitting date ranges into separate events
- Deduplication through `state.json`, so a restart does not repeat what was already delivered
- Freshness window that suppresses reminders that are hours overdue after a restart
- Two run modes: long polling for local work, webhook for hosting
- Keep-alive mechanism that keeps the free hosting tier awake
- Subscriber tracking and a test broadcast endpoint for checking delivery end to end

## Tech stack

| Layer | Technology |
| --- | --- |
| Language | Python 3.11 |
| Core | Python standard library, `zoneinfo` |
| Web layer | Flask |
| WSGI server | Gunicorn |
| Configuration | `.env` file and environment variables |
| HTTP client | requests |
| Tests | pytest |
| Automation | GitHub Actions |
| Hosting | Render, free tier |

## Getting started

### Requirements

- Python 3.11 or newer
- A bot token from [@BotFather](https://t.me/BotFather)

### Environment variables

| Variable | Required | Default | Description |
| --- | --- | --- | --- |
| `BOT_TOKEN` | yes | — | Token issued by BotFather |
| `ADMIN_CHAT_IDS` | yes | — | Comma-separated recipients; a group id also works |
| `TIMEZONE` | no | `Europe/Minsk` | Time zone of the schedule |
| `REMIND_DAYS` | no | `2,1` | Days before the event for the daily reminders |
| `REMIND_AT` | no | `10:00,22:00` | Times of day for those reminders |
| `REMIND_HOURS` | no | `2` | Hours before the start for the last reminder |
| `REMIND_WINDOW_MINUTES` | no | `360` | Reminders older than this window are skipped |
| `BOT_URL` | webhook mode | — | Public HTTPS URL of the deployed instance |
| `RENDER_EXTERNAL_URL` | no | — | Injected by Render automatically |

Create a `.env` file in the project root:

```
BOT_TOKEN=123456:ABCDEF...
ADMIN_CHAT_IDS=111111111,222222222
```

### Installation

```bash
git clone https://github.com/glcskl/tg5.git
cd tg5
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### Running

Local development with long polling:

```bash
python bot.py
```

Inspect the reminder plan without a token or a running bot:

```bash
python bot.py --dry-run --days 60
```

Webhook mode, which is what the hosting platform uses:

```bash
python setup_webhook.py
bash start.sh
```

`start.sh` runs Gunicorn with a single worker, so the scheduler is not duplicated across processes.

## Project structure

```
bot.py             core logic and long-polling entry point
web_app.py         Flask application serving the Telegram webhook
start.sh           Gunicorn command used by the hosting platform
setup_webhook.py   registers, inspects and removes the webhook
keep_alive.py      keep-alive pinger for the free hosting tier
health_check.py    pings /health and appends the result to health.log
import_docx.py     imports a .docx program file into schedule.json format
tests/             pytest suite for the bot, the schedule and the health probe
schedule.json      event schedule
render.yaml        Render service blueprint
Procfile           process definition for the hosting platform
```

## Schedule

`schedule.json` is the single source of truth. It currently holds 10 events from 02.11 to 02.12.2026. The final event on 02.12.2026 comes from a note in the source document, and its 18:00 start time is an assumption.

The following rows from the source document are intentionally excluded:

- 05.10 to 30.10, marked in red as already conducted;
- 15.12 and 18.12, which the notes say should not take place.

Each event uses the following fields:

```json
{
  "id": "2026-11-09",
  "date": "2026-11-09",
  "time": "18:00",
  "title": "Investment simulation",
  "place": "VSTU",
  "speakers": ["Matsuganova M.D.", "Tokar G.M."],
  "subtitle": "Short explanation",
  "details": "Key points of the session",
  "note": "Additional remark"
}
```

`id`, `date`, `time` and `title` are required, and `id` must be unique.

To rebuild the schedule from a new program file:

```bash
python import_docx.py "Program.docx" --year 2026 --out schedule.imported.json
```

The importer skips the red rows and the excluded dates, and splits ranges such as `02.11 – 04.11` into separate events. The result is written to `schedule.imported.json`; compare it with `schedule.json` and carry the changes over manually.

## Commands

| Command | Purpose |
| --- | --- |
| `/next` | Nearest upcoming event |
| `/schedule` | All upcoming events |
| `/test` | Sends a test reminder to the caller |
| `/status` | Time zone, recipients, next event, uptime |
| `/id` | Returns the caller's chat id |
| `/help` | Command reference |

## HTTP endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/` | Liveness text response |
| `GET` | `/health` | JSON status: time, event count, next event, admin and subscriber counts |
| `POST` | `/webhook/<token>` | Telegram update receiver; rejects a wrong token with 403 |
| `POST` | `/admin/broadcast/<token>` | Sends the next event reminder to every admin and subscriber |

## Deployment

`render.yaml` lets Render provision the web service straight from the blueprint.

1. In Render choose **New → Blueprint** and connect `glcskl/tg5`.
2. Fill in `BOT_TOKEN` and, if known already, `ADMIN_CHAT_IDS`.
3. After the first deploy, copy the service URL and add it to GitHub under **Settings → Secrets and variables → Actions → Variables** as `BOT_URL`.

Render passes `RENDER_EXTERNAL_URL` to the application, so the webhook is registered automatically on start.

Routine maintenance is handled by GitHub Actions:

| Workflow | Trigger | Purpose |
| --- | --- | --- |
| `Keep Alive` | every 5 minutes | pings the service so the free tier does not sleep |
| `Health log` | daily | checks `/health`, appends a line to `health.log` and commits it |
| `Redeploy` | manual | forces a redeploy through a Render deploy hook |
| `Tests` | push, pull request | runs the pytest suite |

For the manual redeploy, create a deploy hook in Render and store it as the `DEPLOY_HOOK_URL` secret.

## Tests and health log

```bash
pip install -r requirements.txt pytest
python -m pytest -q
python health_check.py
```

The suite covers reminder timing, delivery deduplication, restart behaviour, HTML escaping, subscriber tracking and the broadcast endpoint.

`health.log` is append-only: `<UTC time>\t<HTTP code>\t<latency>\t<up|down>`.

## Notes

- Telegram allows only one delivery method per bot. Do not run `python bot.py` with the same token while the hosted instance uses the webhook; remove the webhook first with `python setup_webhook.py delete`.
- The free hosting tier sleeps after 15 minutes without requests. The keep-alive workflow pings the service every 5 minutes, and the application also pings itself every 8 minutes.
- `state.json` and `subscribers.json` live on the instance and survive only until a restart. Recipients configured in `ADMIN_CHAT_IDS` are unaffected, and the freshness window prevents stale reminders from being sent after a restart.
- This project is personal and is not affiliated with the university. It is maintained on a best-effort basis.
