#!/usr/bin/env python3
"""Проверяет /health задеплоенного бота и дописывает строку в health.log.

Строка лога: <UTC-время>	<HTTP-код>	<латентность>	<up|down>
Код возврата 1, если сервис не ответил 200.
"""

import os
import sys
import time
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_PATH = os.path.join(BASE_DIR, "health.log")
TIMEOUT = 120


def health_url() -> str:
    url = os.environ.get("BOT_HEALTH_URL", "").strip()
    if url:
        return url
    base = os.environ.get("BOT_URL", "").strip().rstrip("/")
    if base:
        return f"{base}/health"
    return ""


def check(url: str, timeout: int = TIMEOUT) -> dict:
    started = time.monotonic()
    request = Request(url, headers={"User-Agent": "tg5-health-check/1.0"})
    try:
        with urlopen(request, timeout=timeout) as response:
            response.read(256)
            code = response.status
    except HTTPError as exc:
        code = exc.code
    except URLError as exc:
        elapsed = round((time.monotonic() - started) * 1000)
        return {"code": 0, "ms": elapsed, "ok": False, "error": str(exc.reason)}

    elapsed = round((time.monotonic() - started) * 1000)
    return {"code": code, "ms": elapsed, "ok": code == 200}


def format_line(result: dict, now: datetime | None = None) -> str:
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
    state = "up" if result["ok"] else "down"
    return f"{stamp}\t{result['code']}\t{result['ms']} ms\t{state}\n"


def main() -> int:
    url = health_url()
    if not url:
        print("Не задан BOT_HEALTH_URL или BOT_URL", file=sys.stderr)
        return 1

    result = check(url)
    with open(LOG_PATH, "a", encoding="utf-8") as log:
        log.write(format_line(result))

    print(f"{url} -> HTTP {result['code']} за {result['ms']} ms")
    if not result["ok"]:
        print(
            f"health check failed: {result.get('error', 'unexpected status')}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
