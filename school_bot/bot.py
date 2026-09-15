from __future__ import annotations

import html
import logging
import secrets
import string
from collections import defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatType, ParseMode
from telegram.error import BadRequest, Forbidden, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from school_bot.config import Settings, parse_clock
from school_bot.db import Attendance, Circle, Member, Reaction, create_session_factory
from school_bot.services import (
    calculate_arrival,
    credibility_percent,
    describe_status,
    status_counts,
)


logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"

HELP_TEXT = """<b>Школьная компания</b>

Каждый общается со мной только в личке. Первый человек создаёт компанию и отправляет двум друзьям ссылку. Все статусы и оценки синхронизируются между вашими отдельными диалогами.

/create — создать компанию
/join КОД — войти по коду
/today — отметить статус и увидеть всех
/verify — «пиздабол / не пиздабол»
/stats — общая статистика
/history — история за 7 дней
/members — участники
/invite — ссылка для приглашения
/settings — время рассылки
/setup 07:00 08:30 — изменить время (создатель)
/help — эта справка"""


def session_factory(application: Application):
    return application.bot_data["session_factory"]


def settings(application: Application) -> Settings:
    return application.bot_data["settings"]


def display_name(user) -> str:
    return user.full_name.strip() or (f"@{user.username}" if user.username else "Участник")


def start_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✨ Создать компанию", callback_data="home:create")],
            [InlineKeyboardButton("🔑 Войти по коду", callback_data="home:join")],
        ]
    )


def main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🏫 Отметиться", callback_data="nav:today"),
                InlineKeyboardButton("🔎 Проверить", callback_data="nav:verify"),
            ],
            [
                InlineKeyboardButton("📊 Статистика", callback_data="nav:stats"),
                InlineKeyboardButton("🗓 История", callback_data="nav:history"),
            ],
            [InlineKeyboardButton("👥 Участники и ссылка", callback_data="nav:members")],
        ]
    )


def status_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✅ Я приду в школу", callback_data="s:present")],
            [InlineKeyboardButton("⏰ Я опоздаю", callback_data="s:late")],
            [InlineKeyboardButton("🤔 Скорее всего не приду", callback_data="s:maybe")],
            [InlineKeyboardButton("❌ Я не приду", callback_data="s:absent")],
        ]
    )


def delay_keyboard(member_id: int) -> InlineKeyboardMarkup:
    choices = (5, 10, 15, 20, 30, 45, 60)
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    f"{minutes} мин", callback_data=f"l:{member_id}:{minutes}"
                )
                for minutes in choices[index : index + 3]
            ]
            for index in range(0, len(choices), 3)
        ]
    )


def reaction_keyboard(attendance_id: int, truth: int, lie: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    f"🤥 Пиздабол · {lie}", callback_data=f"r:{attendance_id}:lie"
                ),
                InlineKeyboardButton(
                    f"✅ Не пиздабол · {truth}", callback_data=f"r:{attendance_id}:truth"
                ),
            ]
        ]
    )


def get_member(db, telegram_user_id: int, active_only: bool = True) -> Member | None:
    query = select(Member).where(Member.telegram_user_id == telegram_user_id)
    if active_only:
        query = query.where(Member.active.is_(True))
    return db.scalar(query)


def circle_members(db, circle_id: int) -> list[Member]:
    return list(
        db.scalars(
            select(Member)
            .where(Member.circle_id == circle_id, Member.active.is_(True))
            .order_by(Member.joined_at)
        ).all()
    )


def create_code(db) -> str:
    for _ in range(30):
        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(6))
        if not db.scalar(select(Circle.id).where(Circle.code == code)):
            return code
    raise RuntimeError("Could not generate a unique invite code")


def local_day(circle: Circle):
    return datetime.now(ZoneInfo(circle.timezone)).date()


def create_circle(db, user, chat_id: int, app_settings: Settings) -> tuple[Circle | None, str]:
    existing = get_member(db, user.id, active_only=False)
    if existing and existing.active:
        return db.get(Circle, existing.circle_id), "already"
    if existing:
        return None, "removed"

    circle = Circle(
        code=create_code(db),
        timezone=app_settings.default_timezone,
        morning_time=app_settings.default_morning_time,
        school_start_time=app_settings.default_school_start_time,
    )
    db.add(circle)
    db.flush()
    db.add(
        Member(
            circle_id=circle.id,
            telegram_user_id=user.id,
            private_chat_id=chat_id,
            display_name=display_name(user),
            username=user.username,
            is_owner=True,
        )
    )
    db.commit()
    return circle, "created"


def join_circle(
    db, code: str, user, chat_id: int, limit: int
) -> tuple[Circle | None, Member | None, str]:
    circle = db.scalar(select(Circle).where(Circle.code == code.upper().strip()))
    if not circle:
        return None, None, "bad_code"

    existing = get_member(db, user.id, active_only=False)
    if existing:
        if existing.circle_id != circle.id:
            return circle, existing, "other_circle"
        was_active = existing.active
        existing.active = True
        existing.private_chat_id = chat_id
        existing.display_name = display_name(user)
        existing.username = user.username
        db.commit()
        return circle, existing, "already" if was_active else "joined"

    count = db.scalar(
        select(func.count(Member.id)).where(
            Member.circle_id == circle.id, Member.active.is_(True)
        )
    )
    if (count or 0) >= limit:
        return circle, None, "full"
    member = Member(
        circle_id=circle.id,
        telegram_user_id=user.id,
        private_chat_id=chat_id,
        display_name=display_name(user),
        username=user.username,
    )
    db.add(member)
    db.commit()
    return circle, member, "joined"


def invite_text(circle: Circle, bot_username: str) -> str:
    link = f"https://t.me/{bot_username}?start={circle.code}"
    return (
        "🔗 <b>Приглашение в компанию</b>\n\n"
        f"Код: <code>{circle.code}</code>\n"
        f"Ссылка: {link}\n\n"
        "Отправь ссылку двум друзьям. Они будут общаться с ботом в своих личных чатах."
    )


def render_daily_summary(db, circle: Circle, day) -> str:
    members = circle_members(db, circle.id)
    records = db.scalars(
        select(Attendance).where(
            Attendance.circle_id == circle.id, Attendance.day == day
        )
    ).all()
    by_member = {record.member_id: record for record in records}
    lines = [f"🏫 <b>Сегодня · {day.strftime('%d.%m.%Y')}</b>", ""]
    for member in members:
        record = by_member.get(member.id)
        status = (
            describe_status(record.status, record.delay_minutes, record.arrival_time)
            if record
            else "➖ Ещё не отметил"
        )
        lines.append(f"<b>{html.escape(member.display_name)}</b> — {status}")
    lines.extend(["", "Выбери свой статус 👇"])
    return "\n".join(lines)


def verification_data(db, attendance_id: int):
    attendance = db.get(Attendance, attendance_id)
    if not attendance:
        return None
    author = db.get(Member, attendance.member_id)
    votes = db.execute(
        select(Reaction, Member)
        .join(Member, Reaction.voter_member_id == Member.id)
        .where(Reaction.attendance_id == attendance.id)
        .order_by(Reaction.updated_at)
    ).all()
    truth = sum(1 for vote, _ in votes if vote.verdict == "truth")
    lie = sum(1 for vote, _ in votes if vote.verdict == "lie")
    return attendance, author, votes, truth, lie


def render_verification_card(data) -> str:
    attendance, author, votes, _, _ = data
    lines = [
        f"<b>{html.escape(author.display_name)}</b>",
        describe_status(attendance.status, attendance.delay_minutes, attendance.arrival_time),
    ]
    if votes:
        lines.extend(["", "<b>Проверили:</b>"])
        for vote, voter in votes:
            verdict = "✅ не пиздабол" if vote.verdict == "truth" else "🤥 пиздабол"
            lines.append(f"• {html.escape(voter.display_name)} — {verdict}")
    else:
        lines.extend(["", "Пока никто не проверил."])
    return "\n".join(lines)


async def require_private(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if update.effective_chat and update.effective_chat.type == ChatType.PRIVATE:
        return True
    if update.effective_message:
        username = context.bot.username
        await update.effective_message.reply_text(
            "Я работаю только в личных сообщениях. Открой меня здесь:\n"
            f"https://t.me/{username}"
        )
    return False


async def notify_circle(
    application: Application,
    circle_id: int,
    text: str,
    exclude_user_id: int | None = None,
) -> None:
    with session_factory(application)() as db:
        recipients = [
            (member.telegram_user_id, member.private_chat_id)
            for member in circle_members(db, circle_id)
            if member.telegram_user_id != exclude_user_id
        ]
    for _, chat_id in recipients:
        try:
            await application.bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode=ParseMode.HTML,
                reply_markup=main_keyboard(),
            )
        except Forbidden:
            logger.info("Member %s blocked the bot", chat_id)
        except TelegramError:
            logger.exception("Could not notify private chat %s", chat_id)


async def show_home(chat_id: int, user_id: int, application: Application) -> None:
    with session_factory(application)() as db:
        member = get_member(db, user_id)
        if not member:
            await application.bot.send_message(
                chat_id=chat_id,
                text="Привет! Создай свою компанию или войди по приглашению друга.",
                reply_markup=start_keyboard(),
            )
            return
        circle = db.get(Circle, member.circle_id)
        members = circle_members(db, circle.id)
        text = (
            f"🏫 <b>{html.escape(circle.name)}</b>\n"
            f"Участников: <b>{len(members)}/{settings(application).max_members}</b>\n"
            f"Утренняя отметка: <b>{circle.morning_time}</b> ({html.escape(circle.timezone)})"
        )
    await application.bot.send_message(
        chat_id=chat_id, text=text, parse_mode=ParseMode.HTML, reply_markup=main_keyboard()
    )


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_private(update, context):
        return
    context.user_data.pop("awaiting_join_code", None)
    if context.args:
        await process_join(update, context, context.args[0])
        return
    await show_home(update.effective_chat.id, update.effective_user.id, context.application)


async def create_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_private(update, context):
        return
    with session_factory(context.application)() as db:
        circle, result = create_circle(
            db,
            update.effective_user,
            update.effective_chat.id,
            settings(context.application),
        )
    if result == "removed":
        await update.effective_message.reply_text(
            "Ты был удалён из прежней компании. Для возврата нужна её ссылка или код."
        )
        return
    if result == "already":
        await update.effective_message.reply_text("Ты уже состоишь в компании.")
        await show_home(update.effective_chat.id, update.effective_user.id, context.application)
        return
    await update.effective_message.reply_text(
        "✅ Компания создана!\n\n" + invite_text(circle, context.bot.username),
        parse_mode=ParseMode.HTML,
        reply_markup=main_keyboard(),
        disable_web_page_preview=True,
    )


async def process_join(update: Update, context: ContextTypes.DEFAULT_TYPE, code: str) -> None:
    if not await require_private(update, context):
        return
    with session_factory(context.application)() as db:
        circle, member, result = join_circle(
            db,
            code,
            update.effective_user,
            update.effective_chat.id,
            settings(context.application).max_members,
        )
    if result == "bad_code":
        await update.effective_message.reply_text("Такого кода нет. Проверь код или попроси новую ссылку.")
        return
    if result == "full":
        await update.effective_message.reply_text("В этой компании уже заняты все три места.")
        return
    if result == "other_circle":
        await update.effective_message.reply_text("Ты уже привязан к другой компании.")
        return
    if result == "already":
        await update.effective_message.reply_text("Ты уже состоишь в этой компании 👍")
        await show_home(update.effective_chat.id, update.effective_user.id, context.application)
        return

    await update.effective_message.reply_text(
        "✅ Ты присоединился! Все участники общаются со мной отдельно, "
        "но видят общие статусы и статистику.",
        reply_markup=main_keyboard(),
    )
    await notify_circle(
        context.application,
        circle.id,
        f"👋 <b>{html.escape(member.display_name)}</b> присоединился к компании.",
        exclude_user_id=member.telegram_user_id,
    )


async def join_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_private(update, context):
        return
    if context.args:
        await process_join(update, context, context.args[0])
        return
    context.user_data["awaiting_join_code"] = True
    await update.effective_message.reply_text(
        "Пришли шестизначный код компании одним сообщением."
    )


async def text_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.user_data.pop("awaiting_join_code", False):
        return
    await process_join(update, context, update.effective_message.text)


async def send_today(chat_id: int, user_id: int, application: Application) -> None:
    with session_factory(application)() as db:
        member = get_member(db, user_id)
        if not member:
            await application.bot.send_message(chat_id, "Сначала создай компанию или войди по коду: /start")
            return
        circle = db.get(Circle, member.circle_id)
        text = render_daily_summary(db, circle, local_day(circle))
    await application.bot.send_message(
        chat_id, text, parse_mode=ParseMode.HTML, reply_markup=status_keyboard()
    )


async def today_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await require_private(update, context):
        await send_today(update.effective_chat.id, update.effective_user.id, context.application)


def upsert_attendance(
    db,
    circle: Circle,
    member: Member,
    status: str,
    delay_minutes: int | None = None,
    arrival_time: str | None = None,
) -> Attendance:
    day = local_day(circle)
    record = db.scalar(
        select(Attendance).where(Attendance.member_id == member.id, Attendance.day == day)
    )
    if not record:
        record = Attendance(circle_id=circle.id, member_id=member.id, day=day, status=status)
        db.add(record)
    record.status = status
    record.delay_minutes = delay_minutes
    record.arrival_time = arrival_time
    db.commit()
    return record


async def status_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not update.effective_user:
        return
    action = query.data.split(":", 1)[1]
    with session_factory(context.application)() as db:
        member = get_member(db, update.effective_user.id)
        if not member:
            await query.answer("Сначала /start", show_alert=True)
            return
        circle = db.get(Circle, member.circle_id)
        if action == "late":
            member_id = member.id
        else:
            record = upsert_attendance(db, circle, member, action)
            status_text = describe_status(record.status)
            summary = render_daily_summary(db, circle, record.day)
            circle_id = circle.id
            member_name = member.display_name

    if action == "late":
        await query.answer()
        await query.message.reply_text(
            "На сколько опоздаешь?",
            reply_markup=delay_keyboard(member_id),
        )
        return
    await query.answer("Сохранено ✅")
    try:
        await query.edit_message_text(
            summary, parse_mode=ParseMode.HTML, reply_markup=status_keyboard()
        )
    except BadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            raise
    await notify_circle(
        context.application,
        circle_id,
        f"🔔 <b>{html.escape(member_name)}</b> отметил: {status_text}",
        exclude_user_id=update.effective_user.id,
    )


async def late_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not update.effective_user:
        return
    _, requested_member_id, minutes_text = query.data.split(":")
    with session_factory(context.application)() as db:
        member = get_member(db, update.effective_user.id)
        if not member or member.id != int(requested_member_id):
            await query.answer("Это кнопки другого участника", show_alert=True)
            return
        circle = db.get(Circle, member.circle_id)
        minutes = int(minutes_text)
        arrival = calculate_arrival(circle.school_start_time, minutes)
        record = upsert_attendance(db, circle, member, "late", minutes, arrival)
        circle_id = circle.id
        member_name = member.display_name
    await query.answer("Сохранено ✅")
    await query.edit_message_text(
        f"✅ Опоздание на {minutes} мин. Будешь к {arrival}.",
    )
    await notify_circle(
        context.application,
        circle_id,
        f"🔔 <b>{html.escape(member_name)}</b> отметил: "
        f"{describe_status('late', minutes, arrival)}",
        exclude_user_id=update.effective_user.id,
    )
    await send_today(query.message.chat.id, update.effective_user.id, context.application)


async def send_verify(chat_id: int, user_id: int, application: Application) -> None:
    cards = []
    with session_factory(application)() as db:
        member = get_member(db, user_id)
        if not member:
            await application.bot.send_message(chat_id, "Сначала /start")
            return
        circle = db.get(Circle, member.circle_id)
        attendance_ids = db.scalars(
            select(Attendance.id)
            .where(Attendance.circle_id == circle.id, Attendance.day == local_day(circle))
            .order_by(Attendance.id)
        ).all()
        for attendance_id in attendance_ids:
            data = verification_data(db, attendance_id)
            cards.append((render_verification_card(data), attendance_id, data[3], data[4]))
    if not cards:
        await application.bot.send_message(chat_id, "Сегодня пока нечего проверять.")
        return
    await application.bot.send_message(
        chat_id,
        "🔎 <b>Проверка за сегодня</b>\nОценку можно изменить. Себя оценивать нельзя.",
        parse_mode=ParseMode.HTML,
    )
    for text, attendance_id, truth, lie in cards:
        await application.bot.send_message(
            chat_id,
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=reaction_keyboard(attendance_id, truth, lie),
        )


async def verify_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await require_private(update, context):
        await send_verify(update.effective_chat.id, update.effective_user.id, context.application)


async def reaction_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not update.effective_user:
        return
    _, attendance_id_text, verdict = query.data.split(":")
    attendance_id = int(attendance_id_text)
    with session_factory(context.application)() as db:
        voter = get_member(db, update.effective_user.id)
        attendance = db.get(Attendance, attendance_id)
        if not voter or not attendance or attendance.circle_id != voter.circle_id:
            await query.answer("Отметка недоступна", show_alert=True)
            return
        if voter.id == attendance.member_id:
            await query.answer("Себя оценивать нельзя 🙂", show_alert=True)
            return
        reaction = db.scalar(
            select(Reaction).where(
                Reaction.attendance_id == attendance.id,
                Reaction.voter_member_id == voter.id,
            )
        )
        if reaction:
            reaction.verdict = verdict
        else:
            db.add(
                Reaction(
                    attendance_id=attendance.id,
                    voter_member_id=voter.id,
                    verdict=verdict,
                )
            )
        db.commit()
        data = verification_data(db, attendance.id)
        text = render_verification_card(data)
        truth, lie = data[3], data[4]
        author_name = data[1].display_name
        voter_name = voter.display_name
        circle_id = voter.circle_id
    await query.answer("Оценка сохранена")
    await query.edit_message_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=reaction_keyboard(attendance_id, truth, lie),
    )
    verdict_text = "✅ не пиздабол" if verdict == "truth" else "🤥 пиздабол"
    await notify_circle(
        context.application,
        circle_id,
        f"🔎 <b>{html.escape(voter_name)}</b> оценил "
        f"<b>{html.escape(author_name)}</b>: {verdict_text}",
        exclude_user_id=update.effective_user.id,
    )


async def send_stats(chat_id: int, user_id: int, application: Application) -> None:
    with session_factory(application)() as db:
        current = get_member(db, user_id)
        if not current:
            await application.bot.send_message(chat_id, "Сначала /start")
            return
        blocks = ["📊 <b>Общая статистика</b>"]
        for member in circle_members(db, current.circle_id):
            statuses = list(
                db.scalars(select(Attendance.status).where(Attendance.member_id == member.id)).all()
            )
            counts = status_counts(statuses)
            votes = list(
                db.scalars(
                    select(Reaction.verdict)
                    .join(Attendance, Reaction.attendance_id == Attendance.id)
                    .where(Attendance.member_id == member.id)
                ).all()
            )
            truth = votes.count("truth")
            lie = votes.count("lie")
            credibility = credibility_percent(truth, lie)
            score = "нет оценок" if credibility is None else f"{credibility}%"
            blocks.append(
                "\n"
                f"<b>{html.escape(member.display_name)}</b>\n"
                f"Отметок: {len(statuses)} · Пришёл: {counts['present']} · "
                f"Опоздал: {counts['late']}\n"
                f"Скорее не придёт: {counts['maybe']} · Прогулов: {counts['absent']}\n"
                f"Не пиздабол: {truth} · Пиздабол: {lie} · Честность: <b>{score}</b>"
            )
    await application.bot.send_message(
        chat_id, "\n".join(blocks), parse_mode=ParseMode.HTML, reply_markup=main_keyboard()
    )


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await require_private(update, context):
        await send_stats(update.effective_chat.id, update.effective_user.id, context.application)


async def send_history(chat_id: int, user_id: int, application: Application) -> None:
    with session_factory(application)() as db:
        current = get_member(db, user_id)
        if not current:
            await application.bot.send_message(chat_id, "Сначала /start")
            return
        circle = db.get(Circle, current.circle_id)
        today = local_day(circle)
        rows = db.execute(
            select(Attendance, Member)
            .join(Member, Attendance.member_id == Member.id)
            .where(
                Attendance.circle_id == circle.id,
                Attendance.day >= today - timedelta(days=6),
                Attendance.day <= today,
            )
            .order_by(Attendance.day.desc(), Member.joined_at)
        ).all()
        grouped = defaultdict(list)
        for attendance, member in rows:
            grouped[attendance.day].append((attendance, member))
        lines = ["🗓 <b>История за 7 дней</b>"]
        for day in sorted(grouped, reverse=True):
            lines.append(f"\n<b>{day.strftime('%d.%m.%Y')}</b>")
            for attendance, member in grouped[day]:
                lines.append(
                    f"• {html.escape(member.display_name)} — "
                    f"{describe_status(attendance.status, attendance.delay_minutes, attendance.arrival_time)}"
                )
        if not rows:
            lines.append("\nПока нет отметок.")
    await application.bot.send_message(
        chat_id, "\n".join(lines), parse_mode=ParseMode.HTML, reply_markup=main_keyboard()
    )


async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await require_private(update, context):
        await send_history(update.effective_chat.id, update.effective_user.id, context.application)


async def send_members(chat_id: int, user_id: int, application: Application) -> None:
    with session_factory(application)() as db:
        current = get_member(db, user_id)
        if not current:
            await application.bot.send_message(chat_id, "Сначала /start")
            return
        circle = db.get(Circle, current.circle_id)
        members = circle_members(db, circle.id)
        lines = [f"👥 <b>Участники ({len(members)}/{settings(application).max_members})</b>"]
        lines.extend(
            f"{index}. {html.escape(member.display_name)}"
            + (" 👑" if member.is_owner else "")
            for index, member in enumerate(members, 1)
        )
        lines.extend(["", invite_text(circle, application.bot.username)])
        markup = None
        if current.is_owner:
            removable = [member for member in members if member.id != current.id]
            if removable:
                markup = InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                f"Убрать {member.display_name}", callback_data=f"m:remove:{member.id}"
                            )
                        ]
                        for member in removable
                    ]
                )
    await application.bot.send_message(
        chat_id,
        "\n".join(lines),
        parse_mode=ParseMode.HTML,
        reply_markup=markup or main_keyboard(),
        disable_web_page_preview=True,
    )


async def members_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await require_private(update, context):
        await send_members(update.effective_chat.id, update.effective_user.id, context.application)


async def invite_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_private(update, context):
        return
    with session_factory(context.application)() as db:
        member = get_member(db, update.effective_user.id)
        if not member:
            await update.effective_message.reply_text("Сначала /start")
            return
        circle = db.get(Circle, member.circle_id)
        text = invite_text(circle, context.bot.username)
    await update.effective_message.reply_text(
        text, parse_mode=ParseMode.HTML, disable_web_page_preview=True
    )


async def remove_member_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    _, _, target_id_text = query.data.split(":")
    with session_factory(context.application)() as db:
        owner = get_member(db, update.effective_user.id)
        target = db.get(Member, int(target_id_text))
        if (
            not owner
            or not owner.is_owner
            or not target
            or target.circle_id != owner.circle_id
            or target.id == owner.id
        ):
            await query.answer("Недостаточно прав", show_alert=True)
            return
        target.active = False
        target_name = target.display_name
        target_chat_id = target.private_chat_id
        circle_id = owner.circle_id
        db.commit()
    await query.answer("Участник удалён")
    await query.edit_message_text(f"Место {html.escape(target_name)} освобождено.", parse_mode=ParseMode.HTML)
    try:
        await context.bot.send_message(
            target_chat_id, "Ты удалён из компании. Чтобы вернуться, понадобится новая ссылка."
        )
    except TelegramError:
        pass
    await notify_circle(
        context.application,
        circle_id,
        f"👋 <b>{html.escape(target_name)}</b> удалён из компании.",
        exclude_user_id=update.effective_user.id,
    )


async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_private(update, context):
        return
    with session_factory(context.application)() as db:
        member = get_member(db, update.effective_user.id)
        if not member:
            await update.effective_message.reply_text("Сначала /start")
            return
        circle = db.get(Circle, member.circle_id)
        text = (
            "⚙️ <b>Настройки компании</b>\n"
            f"Утренняя карточка: <b>{circle.morning_time}</b>\n"
            f"Начало школы: <b>{circle.school_start_time}</b>\n"
            f"Часовой пояс: <b>{html.escape(circle.timezone)}</b>"
        )
        if member.is_owner:
            text += "\n\nИзменить: /setup 07:00 08:30"
    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML)


async def setup_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_private(update, context):
        return
    if len(context.args) != 2:
        await update.effective_message.reply_text(
            "Формат: /setup 07:00 08:30\nПервое — рассылка, второе — начало школы."
        )
        return
    try:
        morning = parse_clock(context.args[0], "время рассылки")
        school_start = parse_clock(context.args[1], "время школы")
    except ValueError:
        await update.effective_message.reply_text("Используй время в формате ЧЧ:ММ.")
        return
    with session_factory(context.application)() as db:
        member = get_member(db, update.effective_user.id)
        if not member or not member.is_owner:
            await update.effective_message.reply_text("Менять время может создатель компании.")
            return
        circle = db.get(Circle, member.circle_id)
        circle.morning_time = morning
        circle.school_start_time = school_start
        circle.timezone = settings(context.application).default_timezone
        db.commit()
    await update.effective_message.reply_text(
        f"✅ Рассылка: {morning}, начало школы: {school_start}, "
        f"часовой пояс {settings(context.application).default_timezone}."
    )


async def nav_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    action = query.data.split(":", 1)[1]
    chat_id = query.message.chat.id
    user_id = update.effective_user.id
    actions = {
        "today": send_today,
        "verify": send_verify,
        "stats": send_stats,
        "history": send_history,
        "members": send_members,
    }
    await actions[action](chat_id, user_id, context.application)


async def home_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    action = query.data.split(":", 1)[1]
    await query.answer()
    if action == "create":
        await create_command(update, context)
    else:
        context.user_data["awaiting_join_code"] = True
        await query.message.reply_text("Пришли шестизначный код компании одним сообщением.")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        HELP_TEXT, parse_mode=ParseMode.HTML, reply_markup=main_keyboard()
    )


async def send_morning_prompt(application: Application, circle: Circle) -> None:
    with session_factory(application)() as db:
        stored_circle = db.get(Circle, circle.id)
        if not stored_circle:
            return
        day = local_day(stored_circle)
        text = "☀️ <b>Доброе утро!</b>\nПора отметиться.\n\n" + render_daily_summary(
            db, stored_circle, day
        )
        recipients = [member.private_chat_id for member in circle_members(db, stored_circle.id)]
    for chat_id in recipients:
        try:
            await application.bot.send_message(
                chat_id,
                text,
                parse_mode=ParseMode.HTML,
                reply_markup=status_keyboard(),
            )
        except Forbidden:
            logger.info("Member %s blocked morning messages", chat_id)
        except TelegramError:
            logger.exception("Could not send morning message to %s", chat_id)
    with session_factory(application)() as db:
        stored_circle = db.get(Circle, circle.id)
        if stored_circle:
            stored_circle.last_prompt_date = day
            db.commit()


async def morning_tick(context: ContextTypes.DEFAULT_TYPE) -> None:
    with session_factory(context.application)() as db:
        circles = list(db.scalars(select(Circle)).all())
    for circle in circles:
        now = datetime.now(ZoneInfo(circle.timezone))
        if now.strftime("%H:%M") >= circle.morning_time and circle.last_prompt_date != now.date():
            try:
                await send_morning_prompt(context.application, circle)
            except TelegramError:
                logger.exception("Could not send morning prompt for circle %s", circle.id)


async def post_init(application: Application) -> None:
    await application.bot.set_my_commands(
        [
            BotCommand("start", "открыть главное меню"),
            BotCommand("today", "отметиться и увидеть всех"),
            BotCommand("verify", "проверить обещания"),
            BotCommand("stats", "общая статистика"),
            BotCommand("history", "история за 7 дней"),
            BotCommand("members", "участники и приглашение"),
            BotCommand("invite", "получить ссылку для друзей"),
            BotCommand("settings", "настройки времени"),
            BotCommand("help", "помощь"),
        ]
    )
    application.job_queue.run_repeating(morning_tick, interval=60, first=3, name="morning")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    error = context.error
    logger.error(
        "Unhandled update error",
        exc_info=(type(error), error, error.__traceback__) if error else None,
    )


def build_application(app_settings: Settings) -> Application:
    application = Application.builder().token(app_settings.bot_token).post_init(post_init).build()
    application.bot_data["settings"] = app_settings
    application.bot_data["session_factory"] = create_session_factory(app_settings.database_url)

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("create", create_command))
    application.add_handler(CommandHandler("join", join_command))
    application.add_handler(CommandHandler("today", today_command))
    application.add_handler(CommandHandler("verify", verify_command))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(CommandHandler("history", history_command))
    application.add_handler(CommandHandler("members", members_command))
    application.add_handler(CommandHandler("invite", invite_command))
    application.add_handler(CommandHandler("settings", settings_command))
    application.add_handler(CommandHandler("setup", setup_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CallbackQueryHandler(home_callback, pattern=r"^home:"))
    application.add_handler(CallbackQueryHandler(nav_callback, pattern=r"^nav:"))
    application.add_handler(CallbackQueryHandler(status_callback, pattern=r"^s:"))
    application.add_handler(CallbackQueryHandler(late_callback, pattern=r"^l:"))
    application.add_handler(CallbackQueryHandler(reaction_callback, pattern=r"^r:"))
    application.add_handler(CallbackQueryHandler(remove_member_callback, pattern=r"^m:remove:"))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_message))
    application.add_error_handler(error_handler)
    return application


def main() -> None:
    app_settings = Settings.from_env()
    logger.info("Starting private school-circle bot")
    build_application(app_settings).run_polling(drop_pending_updates=False)
