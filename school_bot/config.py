from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def parse_clock(value: str, variable_name: str) -> str:
    try:
        parsed = time.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{variable_name} must use HH:MM format") from exc
    if parsed.second or parsed.microsecond:
        raise ValueError(f"{variable_name} must use HH:MM format")
    return parsed.strftime("%H:%M")


def normalize_database_url(value: str) -> str:
    if value.startswith("postgres://"):
        return "postgresql+psycopg://" + value.removeprefix("postgres://")
    if value.startswith("postgresql://"):
        return "postgresql+psycopg://" + value.removeprefix("postgresql://")
    return value


@dataclass(frozen=True)
class Settings:
    bot_token: str
    database_url: str
    max_members: int
    default_timezone: str
    default_morning_time: str
    default_school_start_time: str

    @classmethod
    def from_env(cls) -> "Settings":
        token = os.getenv("BOT_TOKEN", "").strip()
        if not token:
            raise RuntimeError("BOT_TOKEN is not set")

        timezone_name = os.getenv("APP_TIMEZONE", "Europe/Moscow").strip()
        try:
            ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"Unknown APP_TIMEZONE: {timezone_name}") from exc

        max_members = int(os.getenv("MAX_MEMBERS", "3"))
        if max_members < 2:
            raise ValueError("MAX_MEMBERS must be at least 2")

        return cls(
            bot_token=token,
            database_url=normalize_database_url(
                os.getenv("DATABASE_URL", "sqlite:///school_bot.db").strip()
            ),
            max_members=max_members,
            default_timezone=timezone_name,
            default_morning_time=parse_clock(
                os.getenv("MORNING_TIME", "07:00"), "MORNING_TIME"
            ),
            default_school_start_time=parse_clock(
                os.getenv("SCHOOL_START_TIME", "08:30"), "SCHOOL_START_TIME"
            ),
        )

