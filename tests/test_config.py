from school_bot.config import Settings


def test_default_database_is_local_sqlite(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "test-token")
    monkeypatch.delenv("DATABASE_PATH", raising=False)

    config = Settings.from_env()

    assert config.database_url == "sqlite:///school_bot.db"


def test_railway_volume_database_path(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "test-token")
    monkeypatch.setenv("DATABASE_PATH", "/data/school_bot.db")

    config = Settings.from_env()

    assert config.database_url == "sqlite:////data/school_bot.db"

