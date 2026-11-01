#!/usr/bin/env python3
"""Управление Telegram webhook для задеплоенного бота.

Примеры:
    python3 setup_webhook.py setup https://tg5-schedule-bot.onrender.com
    python3 setup_webhook.py info
    python3 setup_webhook.py delete
"""

from __future__ import annotations

import os
import sys

import requests

import bot as core

core.load_env(core.ENV_PATH)
TOKEN = os.getenv("BOT_TOKEN", "").strip()
BOT_URL = (os.getenv("BOT_URL") or os.getenv("RENDER_EXTERNAL_URL") or "").rstrip("/")
API = f"https://api.telegram.org/bot{TOKEN}/"


def call(method: str, payload: dict | None = None) -> dict:
    response = requests.post(API + method, json=payload or {}, timeout=20)
    try:
        data = response.json()
    except ValueError as exc:
        raise RuntimeError(f"{method}: некорректный ответ Telegram") from exc
    if not data.get("ok"):
        raise RuntimeError(f"{method}: {data.get('description', 'неизвестная ошибка')}")
    return data.get("result") or {}


def setup(url: str) -> None:
    url = url.rstrip("/")
    call(
        "setWebhook",
        {
            "url": f"{url}/webhook/{TOKEN}",
            "allowed_updates": ["message"],
            "drop_pending_updates": True,
        },
    )
    print("Webhook установлен:", url + "/webhook/<token>")


def info() -> None:
    data = call("getWebhookInfo")
    print("URL:", data.get("url") or "не задан")
    print("Ожидает обновлений:", data.get("pending_update_count", 0))
    print("Последняя ошибка:", data.get("last_error_message") or "нет")


def delete() -> None:
    call("deleteWebhook", {"drop_pending_updates": False})
    print("Webhook удалён. Теперь можно запускать bot.py в режиме long polling.")


def main() -> int:
    if not TOKEN:
        print("Не задан BOT_TOKEN (.env или переменная окружения).", file=sys.stderr)
        return 1

    command = sys.argv[1] if len(sys.argv) > 1 else "setup"
    url = sys.argv[2] if len(sys.argv) > 2 else BOT_URL

    try:
        if command == "setup":
            if not url:
                print(
                    "Укажите URL: python3 setup_webhook.py setup https://...",
                    file=sys.stderr,
                )
                return 1
            setup(url)
        elif command == "info":
            info()
        elif command == "delete":
            delete()
        else:
            print(
                f"Неизвестная команда: {command}. Доступно: setup, info, delete.",
                file=sys.stderr,
            )
            return 1
    except Exception as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
