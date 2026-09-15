from sqlalchemy import text

from school_bot.db import Circle, Member, create_session_factory


def test_sqlite_file_and_private_circle_schema(tmp_path):
    database_file = tmp_path / "nested" / "school_bot.db"
    factory = create_session_factory(f"sqlite:///{database_file}")

    with factory() as session:
        circle = Circle(code="ABC234")
        session.add(circle)
        session.flush()
        session.add(
            Member(
                circle_id=circle.id,
                telegram_user_id=10,
                private_chat_id=10,
                display_name="Аня",
                is_owner=True,
            )
        )
        session.commit()
        foreign_keys = session.execute(text("PRAGMA foreign_keys")).scalar_one()
        journal_mode = session.execute(text("PRAGMA journal_mode")).scalar_one()

    assert database_file.exists()
    assert foreign_keys == 1
    assert journal_mode == "wal"

