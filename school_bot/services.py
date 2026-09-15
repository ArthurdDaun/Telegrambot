from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta


STATUS_LABELS = {
    "present": "✅ Приду в школу",
    "late": "⏰ Опоздаю",
    "maybe": "🤔 Скорее всего не приду",
    "absent": "❌ Не приду",
}


def calculate_arrival(school_start: str, delay_minutes: int) -> str:
    start = datetime.strptime(school_start, "%H:%M")
    return (start + timedelta(minutes=delay_minutes)).strftime("%H:%M")


def describe_status(
    status: str, delay_minutes: int | None = None, arrival_time: str | None = None
) -> str:
    if status == "late":
        return f"⏰ Опоздаю на {delay_minutes} мин, буду к {arrival_time}"
    return STATUS_LABELS.get(status, "❔ Неизвестно")


def credibility_percent(truth_votes: int, lie_votes: int) -> int | None:
    total = truth_votes + lie_votes
    if not total:
        return None
    return round(truth_votes * 100 / total)


def status_counts(statuses: Iterable[str]) -> dict[str, int]:
    result = {name: 0 for name in STATUS_LABELS}
    for status in statuses:
        if status in result:
            result[status] += 1
    return result

