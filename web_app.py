#!/usr/bin/env python3
"""Flask-приложение для Render: Telegram webhook и планировщик напоминаний."""

from __future__ import annotations

import os
import secrets
import threading
import time
from datetime import datetime

import requests
from flask import Flask, abort, request

import bot as core

core.load_env(core.ENV_PATH)

config = core.Config.from_env()
if not config.token:
    raise RuntimeError(
        "Не задан BOT_TOKEN. Добавьте переменную окружения BOT_TOKEN в Render или в .env."
    )

events = core.load_events(config)
state = core.SentState(core.STATE_PATH)
state.prune()
service = core.Bot(config, events, state)

app = Flask(__name__)

BASE_URL = (os.getenv("RENDER_EXTERNAL_URL") or os.getenv("BOT_URL") or "").rstrip("/")
SELF_PING_SECONDS = 8 * 60
WEBHOOK_RETRIES = 6
WEBHOOK_RETRY_SECONDS = 20


def register_webhook() -> None:
    if not BASE_URL:
        core.log("RENDER_EXTERNAL_URL/BOT_URL не задан — webhook не регистрирую")
        return

    payload = {
        "url": f"{BASE_URL}/webhook/{config.token}",
        "allowed_updates": ["message"],
        "drop_pending_updates": False,
    }
    for attempt in range(1, WEBHOOK_RETRIES + 1):
        try:
            service.telegram.call("setWebhook", payload)
        except Exception as exc:
            core.log(f"setWebhook, попытка {attempt}/{WEBHOOK_RETRIES}: {exc}")
            if attempt < WEBHOOK_RETRIES:
                time.sleep(WEBHOOK_RETRY_SECONDS)
            continue
        core.log(f"Webhook зарегистрирован: {BASE_URL}/webhook/<token>")
        return

    core.log("Не удалось зарегистрировать webhook")


def background_worker() -> None:
    time.sleep(15)
    register_webhook()

    while True:
        time.sleep(SELF_PING_SECONDS)
        if not BASE_URL:
            continue
        try:
            response = requests.get(
                f"{BASE_URL}/health",
                timeout=90,
                headers={"User-Agent": "tg5-self-ping/1.0"},
            )
            if response.status_code != 200:
                core.log(f"Self-ping: HTTP {response.status_code}")
        except Exception as exc:
            core.log(f"Self-ping: {exc}")


threading.Thread(target=service.scheduler_loop, name="scheduler", daemon=True).start()
threading.Thread(target=background_worker, name="webhook-self-ping", daemon=True).start()


@app.get("/")
def index():
    return "Bot is running."


@app.get("/health")
def health():
    now = datetime.now(config.tz)
    upcoming = core.upcoming_events(events, now)
    return {
        "status": "ok",
        "service": "tg5-schedule-bot",
        "time": now.isoformat(),
        "events": len(events),
        "next": upcoming[0]["id"] if upcoming else None,
        "admins": len(config.chat_ids),
    }


@app.post("/webhook/<token>")
def telegram_webhook(token: str):
    if not secrets.compare_digest(token, config.token):
        abort(403)

    update = request.get_json(silent=True) or {}
    message = update.get("message")
    if message:
        try:
            service.handle_message(message)
        except Exception as exc:
            core.log(f"Ошибка обработки update: {exc}")
    return "", 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
