from types import SimpleNamespace

from school_bot.bot import create_circle, join_circle
from school_bot.config import Settings
from school_bot.db import create_session_factory


def user(user_id: int, name: str):
    return SimpleNamespace(id=user_id, full_name=name, username=None)


def test_three_people_join_one_private_circle():
    factory = create_session_factory("sqlite:///:memory:")
    config = Settings(
        bot_token="test",
        database_url="sqlite:///:memory:",
        max_members=3,
        default_timezone="Asia/Yekaterinburg",
        default_morning_time="07:00",
        default_school_start_time="08:30",
    )

    with factory() as session:
        circle, result = create_circle(session, user(1, "Один"), 101, config)
        assert result == "created"
        assert join_circle(session, circle.code, user(2, "Два"), 102, 3)[2] == "joined"
        assert join_circle(session, circle.code, user(3, "Три"), 103, 3)[2] == "joined"
        assert join_circle(session, circle.code, user(4, "Четыре"), 104, 3)[2] == "full"

