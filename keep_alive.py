#!/usr/bin/env python3
"""Пингует /health, чтобы бесплатный сервис Render не засыпал."""

import argparse
import os
import time
from datetime import datetime

import requests


def log(message: str) -> None:
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {message}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Keep-alive для сервиса на Render")
    parser.add_argument(
        "--url",
        default=os.getenv("BOT_URL", "").rstrip("/"),
        help="URL сервиса, например https://tg5-schedule-bot.onrender.com",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=int(os.getenv("PING_INTERVAL_MINUTES", "8")),
        help="Интервал пинга в минутах",
    )
    args = parser.parse_args()

    if not args.url:
        raise SystemExit("Укажите --url или переменную окружения BOT_URL")
    if args.interval < 1:
        raise SystemExit("Интервал должен быть больше нуля")

    log(f"Пингую {args.url}/health каждые {args.interval} мин.")
    while True:
        try:
            response = requests.get(
                f"{args.url}/health",
                timeout=90,
                headers={"User-Agent": "tg5-keep-alive/1.0"},
            )
            log(f"HTTP {response.status_code}")
        except Exception as exc:
            log(f"Ошибка: {exc}")
        time.sleep(args.interval * 60)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("Остановлено")
