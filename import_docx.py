#!/usr/bin/env python3
"""Собирает расписание мероприятий из .docx в JSON.

Правила:
  * строки, целиком выделенные красным, считаются проведёнными и пропускаются;
  * чёрные строки попадают в расписание;
  * диапазон дат ("02.11 – 04.11") разбивается на отдельные мероприятия;
  * даты из --exclude пропускаются.

Пример:
    python3 import_docx.py "Программа мероприятий ВГТУ.docx" --year 2026
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import zipfile
import xml.etree.ElementTree as ET
from datetime import date
from pathlib import Path

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
NS = {"w": W}

DATE_RE = re.compile(r"(\d{2})\.(\d{2})")
TIME_RE = re.compile(r"(\d{1,2})[.:](\d{2})")
CHUNK_RE = re.compile(r"\s*(?=\d{2}\.\d{2}\s*[–—\-]\s+)")
CHUNK_DATA_RE = re.compile(r"^\s*(\d{2})\.(\d{2})\s*[–—\-]\s*(.*)$", re.S)
TEZIS_RE = re.compile(r"^(.*?)\s*\(тезисы?:\s*(.*)\)\s*$", re.I | re.S)
SPEAKER_RE = re.compile(r"[А-ЯЁ][а-яё\-]+(?:\s+[А-ЯЁ]\.\s*(?:[А-ЯЁ]\.)?)")


def q(tag: str) -> str:
    return f"{{{W}}}{tag}"


def cell_text(tc: ET.Element) -> str:
    parts = []
    for p in tc.iter(q("p")):
        text = "".join(t.text or "" for t in p.iter(q("t")))
        text = text.strip()
        if text:
            parts.append(text)
    return " ".join(parts).strip()


def is_red_row(tr: ET.Element) -> bool:
    red = 0
    other = 0
    for run in tr.iter(q("r")):
        text = "".join(t.text or "" for t in run.iter(q("t")))
        if not text.strip():
            continue
        color = None
        rpr = run.find(q("rPr"))
        if rpr is not None:
            color_el = rpr.find(q("color"))
            if color_el is not None:
                color = (color_el.get(q("val")) or "").upper()
        if color == "FF0000":
            red += len(text)
        else:
            other += len(text)
    return red > 0 and other == 0


def parse_speakers(text: str) -> list[str]:
    names = []
    for match in SPEAKER_RE.finditer(text):
        name = re.sub(r"\s+", " ", match.group(0)).strip()
        name = re.sub(r"([А-ЯЁ]\.)\s+(?=[А-ЯЁ]\.)", r"\1", name)
        names.append(name)
    return names


def split_note(text: str) -> tuple[str | None, str]:
    """Делит примечание на подзаголовок и тезисы."""
    text = re.sub(r"\s+", " ", text).strip().rstrip(".")
    match = TEZIS_RE.match(text)
    if not match:
        return None, text
    subtitle = match.group(1).strip().rstrip(".")
    details = match.group(2).strip().rstrip(".")
    return subtitle or None, details


def extract_rows(docx_path: Path) -> list[list[str]]:
    with zipfile.ZipFile(docx_path) as archive:
        xml = archive.read("word/document.xml")
    root = ET.fromstring(xml)
    body = root.find("w:body", NS)
    table = body.find("w:tbl", NS)
    if table is None:
        raise SystemExit("В документе не найдена таблица с расписанием")

    rows = []
    for tr in table.findall("w:tr", NS):
        if is_red_row(tr):
            continue
        rows.append([cell_text(tc) for tc in tr.findall("w:tc", NS)])
    return rows


def build_events(rows: list[list[str]], year: int, exclude: set[tuple[int, int]]) -> list[dict]:
    events: list[dict] = []
    same_day: dict[str, int] = {}

    for cells in rows[1:]:
        if len(cells) < 6:
            continue
        _, date_cell, title, note, place_cell, speakers_cell = cells[:6]
        dates = [(int(d), int(m)) for d, m in DATE_RE.findall(date_cell)]
        if not dates or not title:
            continue

        time_match = TIME_RE.search(place_cell)
        time_str = "18:00"
        place = place_cell.strip()
        if time_match:
            time_str = f"{int(time_match.group(1)):02d}:{time_match.group(2)}"
            place = place_cell[: time_match.start()].strip().rstrip(",")

        chunks: dict[tuple[int, int], str] = {}
        if len(dates) > 1:
            for chunk in CHUNK_RE.split(note):
                info = CHUNK_DATA_RE.match(chunk)
                if info:
                    chunks[(int(info.group(1)), int(info.group(2)))] = info.group(3)

        speakers = parse_speakers(speakers_cell)

        for day, month in dates:
            if (day, month) in exclude:
                continue
            try:
                event_date = date(year, month, day)
            except ValueError:
                print(f"Пропущена некорректная дата: {day:02d}.{month:02d}", file=sys.stderr)
                continue

            if len(dates) == 1:
                subtitle, details = None, split_note(note)[1]
            else:
                subtitle, details = split_note(chunks.get((day, month), ""))

            iso = event_date.isoformat()
            same_day[iso] = same_day.get(iso, 0) + 1
            suffix = "" if same_day[iso] == 1 else f"-{same_day[iso]}"

            event = {
                "id": f"{iso}{suffix}",
                "date": iso,
                "time": time_str,
                "title": title,
                "place": place or "ВГТУ",
                "speakers": speakers,
            }
            if subtitle:
                event["subtitle"] = subtitle
            if details:
                event["details"] = details
            events.append(event)

    events.sort(key=lambda e: (e["date"], e["time"], e["id"]))
    return events


def main() -> None:
    parser = argparse.ArgumentParser(description="Импорт расписания мероприятий из .docx")
    parser.add_argument("docx", help="путь к файлу программы мероприятий")
    parser.add_argument("--year", type=int, default=date.today().year, help="год программы")
    parser.add_argument(
        "--exclude",
        default="15.12,18.12",
        help="даты, которые нужно исключить, через запятую (ДД.ММ)",
    )
    parser.add_argument("--out", default="schedule.imported.json", help="куда сохранить JSON")
    args = parser.parse_args()

    exclude = set()
    for item in filter(None, (part.strip() for part in args.exclude.split(","))):
        match = re.fullmatch(r"(\d{1,2})\.(\d{1,2})", item)
        if not match:
            raise SystemExit(f"Не понял дату в --exclude: {item}")
        exclude.add((int(match.group(1)), int(match.group(2))))

    rows = extract_rows(Path(args.docx))
    events = build_events(rows, args.year, exclude)

    payload = {
        "program": "Образовательная программа по развитию инновационного предпринимательства среди студентов УО «ВГТУ»",
        "year": args.year,
        "events": events,
    }
    Path(args.out).write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(f"Собрано мероприятий: {len(events)}")
    for event in events:
        print(f"  {event['date']} {event['time']}  {event['title']}  [{event['id']}]")


if __name__ == "__main__":
    main()
