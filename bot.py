#!/usr/bin/env python3
"""Telegram-бот с напоминаниями о мероприятиях из schedule.json."""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import signal
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, time as dt_time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"
SCHEDULE_PATH = BASE_DIR / "schedule.json"
STATE_PATH = BASE_DIR / "state.json"
TICK_SECONDS = 20
EVENT_RETENTION_DAYS = 120

WEEKDAYS = (
    "понедельник",
    "вторник",
    "среда",
    "четверг",
    "пятница",
    "суббота",
    "воскресенье",
)


def log(message: str) -> None:
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {message}", flush=True)


def esc(value: object) -> str:
    return html.escape(str(value), quote=False)


def plural(number: int, one: str, few: str, many: str) -> str:
    if number % 10 == 1 and number % 100 != 11:
        return one
    if 2 <= number % 10 <= 4 and not 12 <= number % 100 <= 14:
        return few
    return many


def human_delta(delta: timedelta) -> str:
    minutes = max(0, int(delta.total_seconds() // 60))
    days, rest = divmod(minutes, 24 * 60)
    hours, mins = divmod(rest, 60)

    parts = []
    if days:
        parts.append(f"{days} {plural(days, 'день', 'дня', 'дней')}")
    if hours:
        parts.append(f"{hours} {plural(hours, 'час', 'часа', 'часов')}")
    if not days and not hours:
        if mins:
            parts.append(f"{mins} {plural(mins, 'минута', 'минуты', 'минут')}")
        else:
            return "меньше минуты"
    return " ".join(parts)


def load_env(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :]
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key.strip(), value)


def parse_hhmm(value: str) -> dt_time:
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", value.strip())
    if not match:
        raise SystemExit(f"Не понял время {value!r}, нужен формат ЧЧ:ММ")
    hour, minute = int(match.group(1)), int(match.group(2))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise SystemExit(f"Некорректное время: {value}")
    return dt_time(hour, minute)


def parse_int_list(value: str) -> tuple[int, ...]:
    result = []
    for part in re.split(r"[\s,;]+", value.strip()):
        if part:
            result.append(int(part))
    return tuple(dict.fromkeys(result))


def parse_float_list(value: str) -> tuple[float, ...]:
    result = []
    for part in re.split(r"[\s,;]+", value.strip()):
        if part:
            result.append(float(part))
    return tuple(dict.fromkeys(result))


@dataclass(frozen=True)
class Config:
    token: str
    chat_ids: tuple[int, ...]
    tz: ZoneInfo
    remind_days: tuple[int, ...]
    remind_at: dt_time
    remind_hours: tuple[float, ...]

    @classmethod
    def from_env(cls) -> "Config":
        try:
            tz = ZoneInfo(os.getenv("TIMEZONE", "Europe/Minsk"))
        except Exception as exc:
            raise SystemExit(f"Неизвестный часовой пояс: {exc}") from exc

        days = parse_int_list(os.getenv("REMIND_DAYS", "2,1"))
        if any(day < 0 for day in days):
            raise SystemExit("REMIND_DAYS не может содержать отрицательные значения")

        hours = parse_float_list(os.getenv("REMIND_HOURS", "2"))
        if any(hour <= 0 for hour in hours):
            raise SystemExit("REMIND_HOURS должен быть больше нуля")

        return cls(
            token=os.getenv("BOT_TOKEN", "").strip(),
            chat_ids=parse_int_list(os.getenv("ADMIN_CHAT_IDS", "")),
            tz=tz,
            remind_days=days,
            remind_at=parse_hhmm(os.getenv("REMIND_AT", "10:00")),
            remind_hours=hours,
        )


def load_events(config: Config) -> list[dict]:
    if not SCHEDULE_PATH.exists():
        raise SystemExit(f"Не найден {SCHEDULE_PATH.name}. Запустите import_docx.py или создайте файл вручную.")

    try:
        data = json.loads(SCHEDULE_PATH.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise SystemExit(f"Ошибка в schedule.json: {exc}") from exc

    raw_events = data.get("events", []) if isinstance(data, dict) else data
    events = []
    for raw in raw_events:
        try:
            day = date.fromisoformat(str(raw["date"]))
            hour, minute = (int(part) for part in str(raw.get("time", "18:00")).split(":")[:2])
            start = datetime(day.year, day.month, day.day, hour, minute, tzinfo=config.tz)
            title = str(raw["title"])
        except (KeyError, TypeError, ValueError) as exc:
            raise SystemExit(f"Ошибка в мероприятии {raw.get('id')!r}: {exc}") from exc

        events.append({**raw, "title": title, "_start": start})

    events.sort(key=lambda item: (item["_start"], item["id"]))
    return events


class SentState:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = threading.Lock()
        self.sent: set[str] = set()
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                self.sent = set(data.get("sent", []))
            except (OSError, ValueError):
                log("Не удалось прочитать state.json, начинаю с чистого листа")

    def has(self, key: str) -> bool:
        with self.lock:
            return key in self.sent

    def mark(self, key: str) -> None:
        with self.lock:
            if key in self.sent:
                return
            self.sent.add(key)
            self._save()

    def prune(self) -> None:
        cutoff = date.today() - timedelta(days=EVENT_RETENTION_DAYS)
        with self.lock:
            kept = set()
            for key in self.sent:
                match = re.match(r"(\d{4}-\d{2}-\d{2})", key)
                if match:
                    try:
                        if date.fromisoformat(match.group(1)) >= cutoff:
                            kept.add(key)
                    except ValueError:
                        kept.add(key)
                else:
                    kept.add(key)
            if kept != self.sent:
                self.sent = kept
                self._save()

    def _save(self) -> None:
        temp = self.path.with_suffix(".tmp")
        temp.write_text(
            json.dumps({"sent": sorted(self.sent)}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temp.replace(self.path)


def reminder_goals(event: dict, config: Config) -> list[tuple[datetime, str]]:
    start: datetime = event["_start"]
    goals: list[tuple[datetime, str]] = []
    seen: set[str] = set()

    for days in config.remind_days:
        due = datetime.combine(
            start.date() - timedelta(days=days),
            config.remind_at,
            tzinfo=config.tz,
        )
        rule = f"d{days}"
        if rule not in seen:
            goals.append((due, rule))
            seen.add(rule)

    for hours in config.remind_hours:
        due = start - timedelta(hours=hours)
        rule = f"h{hours:g}"
        if rule not in seen:
            goals.append((due, rule))
            seen.add(rule)

    goals.sort(key=lambda goal: (goal[0], goal[1]))
    return goals


def state_key(event: dict, rule: str, chat_id: int) -> str:
    return f"{event['id']}:{rule}:{chat_id}"


def format_event(event: dict, now: datetime | None = None, title: str | None = None) -> str:
    start: datetime = event["_start"]
    lines = [
        f"<b>{esc(title or event['title'])}</b>",
        f"{start:%d.%m.%Y}, {WEEKDAYS[start.weekday()]}, {start:%H:%M}",
    ]
    if event.get("place"):
        lines.append(f"Место: {esc(event['place'])}")
    if event.get("subtitle"):
        lines.extend(["", esc(event["subtitle"])])
    if event.get("details"):
        lines.append(f"Тезисы: {esc(event['details'])}")
    if event.get("note"):
        lines.append(esc(event["note"]))
    if event.get("speakers"):
        lines.append(f"Спикеры: {esc(', '.join(event['speakers']))}")
    if now is not None and start > now:
        lines.extend(["", f"До начала: {human_delta(start - now)}"])
    return "\n".join(lines)


def format_reminder(event: dict, now: datetime) -> str:
    return "<b>Напоминание о мероприятии</b>\n\n" + format_event(event, now=now)


def upcoming_events(events: list[dict], now: datetime) -> list[dict]:
    return [event for event in events if event["_start"] >= now]


def format_schedule(events: list[dict], now: datetime, limit: int = 20) -> str:
    upcoming = upcoming_events(events, now)
    if not upcoming:
        return "В расписании пока нет предстоящих мероприятий."

    lines = ["<b>Ближайшие мероприятия</b>"]
    for event in upcoming[:limit]:
        start: datetime = event["_start"]
        lines.extend(
            [
                "",
                f"<b>{start:%d.%m}, {WEEKDAYS[start.weekday()]}, {start:%H:%M}</b>",
                esc(event["title"]),
            ]
        )
        if event.get("subtitle"):
            lines.append(f"<i>{esc(event['subtitle'])}</i>")

    if len(upcoming) > limit:
        lines.extend(["", f"…и ещё {len(upcoming) - limit}. Полный список — в schedule.json."])
    return "\n".join(lines)


def help_text(chat_id: int) -> str:
    return (
        "<b>Напоминания о мероприятиях ВГТУ</b>\n\n"
        "Я присылаю напоминания админам заранее: за 2 дня, за 1 день и за 2 часа до начала.\n\n"
        "<b>Команды</b>\n"
        "/next — ближайшее мероприятие\n"
        "/schedule — все предстоящие\n"
        "/test — тестовое напоминание\n"
        "/status — состояние бота\n"
        "/id — chat_id для настройки\n\n"
        f"Ваш chat_id: <code>{chat_id}</code>"
    )


class Telegram:
    def __init__(self, token: str) -> None:
        self.token = token

    def call(self, method: str, payload: dict | None = None, timeout: int = 60):
        body = json.dumps(payload or {}, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            f"https://api.telegram.org/bot{self.token}/{method}",
            data=body,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                result = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            raise RuntimeError(f"{method}: HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"{method}: {exc.reason}") from exc
        if not result.get("ok"):
            raise RuntimeError(f"{method}: {result}")
        return result["result"]

    def send(self, chat_id: int, text: str) -> None:
        self.call(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
        )


class Bot:
    def __init__(self, config: Config, events: list[dict], state: SentState) -> None:
        self.config = config
        self.events = events
        self.state = state
        self.telegram = Telegram(config.token)
        self.started_at = datetime.now(config.tz)
        self.stop = threading.Event()

    def send_reminder(self, event: dict, now: datetime) -> None:
        text = format_reminder(event, now)
        for chat_id in self.config.chat_ids:
            key = state_key(event, "manual", chat_id)
            try:
                self.telegram.send(chat_id, text)
                self.state.mark(key)
            except Exception as exc:
                log(f"Не удалось отправить {chat_id}: {exc}")

    def deliver_due(self) -> None:
        if not self.config.chat_ids:
            return

        now = datetime.now(self.config.tz)
        for event in self.events:
            start: datetime = event["_start"]
            if start <= now:
                continue

            goals = reminder_goals(event, self.config)
            due = [goal for goal in goals if goal[0] <= now]
            if not due:
                continue

            stage = due[-1][1]
            for chat_id in self.config.chat_ids:
                for _, rule in due[:-1]:
                    self.state.mark(state_key(event, rule, chat_id))

            text = format_reminder(event, now)
            for chat_id in self.config.chat_ids:
                key = state_key(event, stage, chat_id)
                if self.state.has(key):
                    continue
                try:
                    self.telegram.send(chat_id, text)
                except Exception as exc:
                    log(f"Не отправлено {chat_id} ({event['id']}, {stage}): {exc}")
                    continue
                self.state.mark(key)
                log(f"Отправлено {chat_id}: {event['date']} {event['title']} ({stage})")

    def scheduler_loop(self) -> None:
        while not self.stop.is_set():
            try:
                self.deliver_due()
            except Exception as exc:
                log(f"Ошибка планировщика: {exc}")
            self.stop.wait(TICK_SECONDS)

    def handle_message(self, message: dict) -> None:
        text = (message.get("text") or "").strip()
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        if not text.startswith("/") or chat_id is None:
            return

        command = text.split()[0].split("@")[0].lower()
        now = datetime.now(self.config.tz)

        if command in ("/start", "/help"):
            reply = help_text(chat_id)
        elif command == "/id":
            reply = f"Ваш chat_id: <code>{chat_id}</code>"
        elif command == "/next":
            upcoming = upcoming_events(self.events, now)
            reply = (
                "<b>Ближайшее мероприятие</b>\n\n" + format_event(upcoming[0], now=now)
                if upcoming
                else "Предстоящих мероприятий нет."
            )
        elif command in ("/schedule", "/расписание"):
            reply = format_schedule(self.events, now)
        elif command == "/test":
            upcoming = upcoming_events(self.events, now)
            if not upcoming:
                reply = "Нечего показывать: предстоящих мероприятий нет."
            else:
                reply = "Тестовое сообщение.\n\n" + format_reminder(upcoming[0], now)
        elif command == "/status":
            reply = self.status_text(now)
        else:
            reply = "Не знаю такую команду. Список команд — /help"

        try:
            self.telegram.send(chat_id, reply)
        except Exception as exc:
            log(f"Не удалось ответить {chat_id}: {exc}")

    def status_text(self, now: datetime) -> str:
        upcoming = upcoming_events(self.events, now)
        nearest = (
            f"{upcoming[0]['_start']:%d.%m.%Y %H:%M} — {esc(upcoming[0]['title'])}"
            if upcoming
            else "нет"
        )
        days = ", ".join(str(day) for day in self.config.remind_days) or "—"
        hours = ", ".join(f"{hour:g}" for hour in self.config.remind_hours) or "—"
        admins = ", ".join(str(chat_id) for chat_id in self.config.chat_ids) or "не заданы"
        uptime = human_delta(now - self.started_at)
        return (
            "<b>Состояние бота</b>\n\n"
            f"Часовой пояс: {self.config.tz.key}\n"
            f"Админы: {esc(admins)}\n"
            f"Мероприятий в расписании: {len(self.events)}\n"
            f"Ближайшее: {nearest}\n"
            f"Напоминания: за {days} дн. в {self.config.remind_at:%H:%M}, "
            f"за {hours} ч.\n"
            f"Работаю: {uptime}"
        )

    def poll_updates(self) -> None:
        offset = None
        while not self.stop.is_set():
            payload: dict = {"timeout": 25, "allowed_updates": ["message"]}
            if offset is not None:
                payload["offset"] = offset
            try:
                updates = self.telegram.call("getUpdates", payload, timeout=35)
            except Exception as exc:
                log(f"getUpdates: {exc}")
                if "409" in str(exc):
                    log("Похоже, запущен второй экземпляр бота с этим же токеном.")
                self.stop.wait(5)
                continue

            for update in updates:
                offset = update["update_id"] + 1
                message = update.get("message")
                if message:
                    try:
                        self.handle_message(message)
                    except Exception as exc:
                        log(f"Ошибка обработки сообщения: {exc}")

    def run(self) -> None:
        me = self.telegram.call("getMe")
        log(f"Бот @{me.get('username')} на связи")
        if not self.config.chat_ids:
            log("ADMIN_CHAT_IDS пуст: уведомления никому не отправляются. Откройте бота и отправьте /id.")

        thread = threading.Thread(target=self.scheduler_loop, name="scheduler", daemon=True)
        thread.start()
        try:
            self.poll_updates()
        finally:
            self.stop.set()


def dry_run(config: Config, events: list[dict], days: int) -> None:
    now = datetime.now(config.tz)
    horizon = now + timedelta(days=days)
    upcoming = [event for event in events if now < event["_start"] <= horizon]

    print(f"Сейчас: {now:%d.%m.%Y %H:%M} ({config.tz.key})")
    print(
        f"Напоминания: за {', '.join(map(str, config.remind_days))} дн. "
        f"в {config.remind_at:%H:%M}; за {', '.join(f'{h:g}' for h in config.remind_hours)} ч."
    )
    print(f"Мероприятий в ближайшие {days} дн.: {len(upcoming)}\n")

    for event in upcoming:
        start: datetime = event["_start"]
        print(f"{start:%d.%m.%Y %H:%M}  {event['title']}  [{event['id']}]")
        for due, rule in reminder_goals(event, config):
            status = "уже наступило" if due <= now else "запланировано"
            print(f"    {due:%d.%m %H:%M}  {rule:>4}  {status}")
        print()

    if upcoming:
        print("Пример сообщения:\n")
        print(format_reminder(upcoming[0], now))


def main() -> None:
    parser = argparse.ArgumentParser(description="Напоминания о мероприятиях в Telegram")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="показать план напоминаний без запуска бота",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=14,
        help="горизонт для --dry-run в днях (по умолчанию 14)",
    )
    args = parser.parse_args()

    load_env(ENV_PATH)
    config = Config.from_env()
    events = load_events(config)
    state = SentState(STATE_PATH)
    state.prune()

    if args.dry_run:
        dry_run(config, events, args.days)
        return

    if not config.token:
        raise SystemExit(
            "Не задан BOT_TOKEN. Скопируйте .env.example в .env и вставьте токен от @BotFather."
        )

    bot = Bot(config, events, state)

    def stop_handler(signum, frame) -> None:
        log("Останавливаюсь")
        bot.stop.set()
        sys.exit(0)

    signal.signal(signal.SIGINT, stop_handler)
    signal.signal(signal.SIGTERM, stop_handler)

    try:
        bot.run()
    except KeyboardInterrupt:
        log("Останавливаюсь")
    except Exception as exc:
        raise SystemExit(f"Бот остановлен из-за ошибки: {exc}") from exc


if __name__ == "__main__":
    main()
