import json
from datetime import date, datetime, time as dt_time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import bot

TZ = ZoneInfo("Europe/Minsk")


def make_config(**overrides) -> bot.Config:
    values = {
        "token": "test",
        "chat_ids": (1, 2),
        "tz": TZ,
        "remind_days": (2, 1),
        "remind_times": (dt_time(10, 0), dt_time(22, 0)),
        "remind_hours": (2.0,),
        "remind_window_minutes": 360,
    }
    values.update(overrides)
    return bot.Config(**values)


def make_event(day: str, title: str = "Тестовое мероприятие", at: str = "18:00") -> dict:
    hour, minute = (int(part) for part in at.split(":"))
    start = datetime.fromisoformat(day).replace(hour=hour, minute=minute, tzinfo=TZ)
    return {
        "id": day,
        "date": day,
        "time": at,
        "title": title,
        "place": "ВГТУ",
        "_start": start,
    }


class FakeTelegram:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    def send(self, chat_id: int, text: str) -> None:
        self.sent.append((chat_id, text))


def make_bot(tmp_path: Path, events: list[dict], config: bot.Config):
    state = bot.SentState(tmp_path / "state.json")
    service = bot.Bot(config, events, state)
    telegram = FakeTelegram()
    service.telegram = telegram
    return service, telegram, state


def test_goals_include_each_time_of_day():
    goals = bot.reminder_goals(make_event("2026-11-09"), make_config())

    assert [(goal[0].strftime("%d.%m %H:%M"), goal[1]) for goal in goals] == [
        ("07.11 10:00", "d2@10:00"),
        ("07.11 22:00", "d2@22:00"),
        ("08.11 10:00", "d1@10:00"),
        ("08.11 22:00", "d1@22:00"),
        ("09.11 16:00", "h2"),
    ]


def test_sends_only_latest_fresh_reminder(tmp_path):
    service, telegram, state = make_bot(
        tmp_path, [make_event("2026-11-02")], make_config()
    )
    now = datetime(2026, 11, 1, 12, 0, tzinfo=TZ)

    service.deliver_due(now)

    assert len(telegram.sent) == 2
    assert all("Напоминание о мероприятии" in text for _, text in telegram.sent)
    assert state.has("2026-11-02:d1@10:00:1")
    assert state.has("2026-11-02:d2@22:00:2")


def test_repeated_run_does_not_duplicate(tmp_path):
    service, telegram, _ = make_bot(
        tmp_path, [make_event("2026-11-02")], make_config()
    )
    now = datetime(2026, 11, 1, 12, 0, tzinfo=TZ)

    service.deliver_due(now)
    service.deliver_due(now)

    assert len(telegram.sent) == 2


def test_evening_reminder_fires(tmp_path):
    service, telegram, _ = make_bot(
        tmp_path, [make_event("2026-11-02")], make_config()
    )

    service.deliver_due(datetime(2026, 11, 1, 22, 0, tzinfo=TZ))

    assert len(telegram.sent) == 2
    assert "До начала" in telegram.sent[0][1]


def test_stale_reminders_are_suppressed(tmp_path):
    service, telegram, _ = make_bot(
        tmp_path, [make_event("2026-11-02")], make_config()
    )

    service.deliver_due(datetime(2026, 11, 2, 12, 0, tzinfo=TZ))
    assert telegram.sent == []

    service.deliver_due(datetime(2026, 11, 2, 16, 0, tzinfo=TZ))
    assert len(telegram.sent) == 2


def test_state_survives_restart(tmp_path):
    now = datetime(2026, 11, 1, 12, 0, tzinfo=TZ)
    service, telegram, _ = make_bot(
        tmp_path, [make_event("2026-11-02")], make_config()
    )
    service.deliver_due(now)
    assert len(telegram.sent) == 2

    service2, telegram2, _ = make_bot(
        tmp_path, [make_event("2026-11-02")], make_config()
    )
    service2.deliver_due(now)

    assert telegram2.sent == []


def test_schedule_file_is_valid():
    data = json.loads(Path(bot.SCHEDULE_PATH).read_text(encoding="utf-8"))
    events = data["events"]
    ids = [event["id"] for event in events]

    assert len(ids) == len(set(ids))
    for event in events:
        date.fromisoformat(event["date"])
        hour, minute = (int(part) for part in event["time"].split(":"))
        assert 0 <= hour <= 23
        assert 0 <= minute <= 59
        assert event["title"]


def test_message_is_html_escaped():
    event = make_event("2026-11-09", title="<b>Опасный & заголовок</b>")

    text = bot.format_reminder(event, event["_start"] - timedelta(hours=2))

    assert "&lt;b&gt;Опасный &amp; заголовок&lt;/b&gt;" in text
    assert "<b>Опасный" not in text


def test_human_delta():
    assert bot.human_delta(timedelta(days=2)) == "2 дня"
    assert bot.human_delta(timedelta(hours=1, minutes=30)) == "1 час 30 минут"


def test_start_records_subscriber(tmp_path):
    subscribers = bot.Subscribers(tmp_path / "subscribers.json")
    service, telegram, _ = make_bot(tmp_path, [make_event("2026-11-02")], make_config())
    service.subscribers = subscribers

    service.handle_message(
        {"chat": {"id": 42, "first_name": "Глеб", "username": "glcskl"}, "text": "/start"}
    )

    assert subscribers.ids() == [42]
    assert len(telegram.sent) == 1


def test_subscribers_persist_without_duplicates(tmp_path):
    path = tmp_path / "subscribers.json"
    first = bot.Subscribers(path)
    first.add({"id": 7, "first_name": "Анна"})
    first.add({"id": 7, "first_name": "Анна"})

    second = bot.Subscribers(path)
    assert second.ids() == [7]


def test_broadcast_reaches_admins_and_subscribers(tmp_path):
    subscribers = bot.Subscribers(tmp_path / "subscribers.json")
    subscribers.add({"id": 7, "first_name": "Анна"})
    subscribers.add({"id": 1, "first_name": "Глеб", "username": "glcskl"})
    service, telegram, _ = make_bot(tmp_path, [make_event("2026-11-02")], make_config())
    service.subscribers = subscribers

    result = service.broadcast_next(now=datetime(2026, 11, 1, 20, 0, tzinfo=TZ))

    assert result["event"] == "2026-11-02"
    assert result["targets"] == [1, 2, 7]
    assert result["sent"] == [1, 2, 7]
    assert len(telegram.sent) == 3
    assert all("Тестовый прогон" in text for _, text in telegram.sent)
