from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    create_engine,
    event,
)
from sqlalchemy.engine import make_url
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker


class Base(DeclarativeBase):
    pass


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Circle(Base):
    __tablename__ = "circles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(12), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255), default="Школьная компания")
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Yekaterinburg")
    morning_time: Mapped[str] = mapped_column(String(5), default="07:00")
    school_start_time: Mapped[str] = mapped_column(String(5), default="08:30")
    last_prompt_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    members: Mapped[list["Member"]] = relationship(
        back_populates="circle", cascade="all, delete-orphan"
    )


class Member(Base):
    __tablename__ = "circle_members"
    __table_args__ = (UniqueConstraint("telegram_user_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    circle_id: Mapped[int] = mapped_column(
        ForeignKey("circles.id", ondelete="CASCADE"), index=True
    )
    telegram_user_id: Mapped[int] = mapped_column(BigInteger)
    private_chat_id: Mapped[int] = mapped_column(BigInteger)
    display_name: Mapped[str] = mapped_column(String(255))
    username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_owner: Mapped[bool] = mapped_column(Boolean, default=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    joined_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    circle: Mapped[Circle] = relationship(back_populates="members")
    attendances: Mapped[list["Attendance"]] = relationship(
        back_populates="member", cascade="all, delete-orphan"
    )


class Attendance(Base):
    __tablename__ = "circle_attendances"
    __table_args__ = (UniqueConstraint("member_id", "day"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    circle_id: Mapped[int] = mapped_column(
        ForeignKey("circles.id", ondelete="CASCADE"), index=True
    )
    member_id: Mapped[int] = mapped_column(
        ForeignKey("circle_members.id", ondelete="CASCADE"), index=True
    )
    day: Mapped[date] = mapped_column(Date, index=True)
    status: Mapped[str] = mapped_column(String(24))
    delay_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    arrival_time: Mapped[str | None] = mapped_column(String(5), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    member: Mapped[Member] = relationship(back_populates="attendances")
    reactions: Mapped[list["Reaction"]] = relationship(
        back_populates="attendance", cascade="all, delete-orphan"
    )


class Reaction(Base):
    __tablename__ = "circle_reactions"
    __table_args__ = (UniqueConstraint("attendance_id", "voter_member_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    attendance_id: Mapped[int] = mapped_column(
        ForeignKey("circle_attendances.id", ondelete="CASCADE"), index=True
    )
    voter_member_id: Mapped[int] = mapped_column(
        ForeignKey("circle_members.id", ondelete="CASCADE"), index=True
    )
    verdict: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    attendance: Mapped[Attendance] = relationship(back_populates="reactions")


def create_session_factory(database_url: str):
    kwargs = {"pool_pre_ping": True}
    if database_url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
        database_file = make_url(database_url).database
        if database_file and database_file != ":memory:":
            Path(database_file).parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(database_url, **kwargs)

    if database_url.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def configure_sqlite(dbapi_connection, _connection_record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.close()

    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)

