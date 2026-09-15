from __future__ import annotations

import html
import logging
from collections import defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ChatMemberStatus, ChatType, ParseMode
from telegram.error import BadRequest, Forbidden, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

from school_bot.config import Settings, parse_clock
from school_bot.db import Attendance, GroupConfig, Member, Reaction, create_session_factory
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


HELP_TEXT = """<b>Что умеет бот</b>

/join — занять место участника
/today — отметить сегодняшний статус и увидеть всех
/verify — проверить обещания: «пиздабол / не пиздабол»
/stats — общая статистика
/history — история за 7 дней
/members — список участников
/remove — освободить место (ответом на сообщение, для админа)
/settings — время рассылки и начала школы
/setup 07:00 08:30 — настроить группу (для админа)
/help — эта справка

Каждое утро бот сам пришлёт карточку. Все действия делаются кнопками."""


def session_factory(application: Application):
    return application.bot_data["session_factory"]


def settings(application: Application) -> Settings:
    return application.bot_data["settings"]


def is_group(update: Update) -> bool:
    chat = update.effective_chat
    return bool(chat and chat.type in {ChatType.GROUP, ChatType.SUPERGROUP})


def display_name(user) -> str:
    return user.full_name.strip() or (f"@{user.username}" if user.username else "Участник")


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
    rows = []
    for index in range(0, len(choices), 3):
        rows.append(
            [
                InlineKeyboardButton(
                    f"{minutes} мин", callback_data=f"l:{member_id}:{minutes}"
                )
                for minutes in choices[index : index + 3]
            ]
        )
    return InlineKeyboardMarkup(rows)


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


def get_member(db, chat_id: int, telegram_user_id: int) -> Member | None:
    return db.scalar(
        select(Member).where(
            Member.chat_id == chat_id,
            Member.telegram_user_id == telegram_user_id,
            Member.active.is_(True),
        )
    )


def register_member(db, chat_id: int, user, limit: int) -> tuple[Member | None, str]:
    member = db.scalar(
        select(Member).where(
            Member.chat_id == chat_id, Member.telegram_user_id == user.id
        )
    )
    if member:
        member.display_name = display_name(user)
        member.username = user.username
        member.active = True
        db.commit()
        return member, "existing"

    count = db.scalar(
        select(func.count(Member.id)).where(
            Member.chat_id == chat_id, Member.active.is_(True)
        )
    )
    if (count or 0) >= limit:
        return None, "full"

    member = Member(
        chat_id=chat_id,
        telegram_user_id=user.id,
        display_name=display_name(user),
        username=user.username,
    )
    db.add(member)
    db.commit()
    return member, "created"


def local_day(config: GroupConfig):
    return datetime.now(ZoneInfo(config.timezone)).date()


def render_daily_summary(db, config: GroupConfig, day) -> str:
    members = db.scalars(
        select(Member)
        .where(Member.chat_id == config.chat_id, Member.active.is_(True))
        .order_by(Member.joined_at)
    ).all()
    records = db.scalars(
        select(Attendance).where(
            Attendance.chat_id == config.chat_id, Attendance.day == day
        )
    ).all()
    by_member = {record.member_id: record for record in records}

    lines = [f"🏫 <b>Школа · {day.strftime('%d.%m.%Y')}</b>", ""]
    if not members:
        lines.append("Пока никто не зарегистрирован. Нажмите /join.")
    for member in members:
        record = by_member.get(member.id)
        current = (
            describe_status(record.status, record.delay_minutes, record.arrival_time)
            if record
            else "➖ Ещё не отметил"
        )
        lines.append(f"<b>{html.escape(member.display_name)}</b> — {current}")
    lines.extend(["", "Выбери свой статус кнопкой ниже 👇"])
    return "\n".join(lines)


def get_verification_data(db, attendance_id: int):
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
        describe_status(
            attendance.status, attendance.delay_minutes, attendance.arrival_time
        ),
    ]
    if votes:
        lines.append("")
        lines.append("<b>Проверили:</b>")
        for vote, voter in votes:
            verdict = "✅ не пиздабол" if vote.verdict == "truth" else "🤥 пиздабол"
            lines.append(f"• {html.escape(voter.display_name)} — {verdict}")
    else:
        lines.extend(["", "Пока никто не проверил."])
    return "\n".join(lines)


async def is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    chat = update.effective_chat
    user = update.effective_user
    if not chat or not user:
        return False
    try:
        membership = await context.bot.get_chat_member(chat.id, user.id)
    except TelegramError:
        return False
    return membership.status in {ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER}


async def require_group(update: Update) -> bool:
    if is_group(update):
        return True
    if update.effective_message:
        await update.effective_message.reply_text(
            "Добавь меня в общую Telegram-группу и используй команды там."
        )
    return False


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_group(update):
        await update.effective_message.reply_text(
            "Привет! Я веду посещаемость маленькой школьной компании.\n\n"
            "Добавь меня в вашу общую группу, затем админ группы должен отправить "
            "/setup 07:00 08:30."
        )
        return
    await join_command(update, context)


async def setup_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_group(update):
        return
    if not await is_admin(update, context):
        await update.effective_message.reply_text("Настраивать бота может админ группы.")
        return

    app_settings = settings(context.application)
    morning = app_settings.default_morning_time
    school_start = app_settings.default_school_start_time
    try:
        if context.args:
            morning = parse_clock(context.args[0], "время рассылки")
        if len(context.args) > 1:
            school_start = parse_clock(context.args[1], "время начала школы")
        if len(context.args) > 2:
            raise ValueError
    except ValueError:
        await update.effective_message.reply_text(
            "Формат: /setup 07:00 08:30\n"
            "Первое — утренняя рассылка, второе — начало школы."
        )
        return

    chat = update.effective_chat
    with session_factory(context.application)() as db:
        config = db.get(GroupConfig, chat.id)
        if not config:
            config = GroupConfig(
                chat_id=chat.id,
                title=chat.title or "Школьная группа",
                timezone=app_settings.default_timezone,
                morning_time=morning,
                school_start_time=school_start,
            )
            db.add(config)
        else:
            config.title = chat.title or config.title
            config.timezone = app_settings.default_timezone
            config.morning_time = morning
            config.school_start_time = school_start
        db.commit()
        _, join_result = register_member(
            db, chat.id, update.effective_user, app_settings.max_members
        )

    join_note = (
        "\nТы также зарегистрирован как участник."
        if join_result == "created"
        else ""
    )
    await update.effective_message.reply_text(
        "✅ <b>Группа настроена</b>\n"
        f"Утренняя карточка: <b>{morning}</b>\n"
        f"Начало школы: <b>{school_start}</b>\n"
        f"Часовой пояс: <b>{html.escape(app_settings.default_timezone)}</b>\n\n"
        f"Теперь остальные участники нажимают /join.{join_note}",
        parse_mode=ParseMode.HTML,
    )


async def join_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_group(update):
        return
    chat = update.effective_chat
    with session_factory(context.application)() as db:
        config = db.get(GroupConfig, chat.id)
        if not config:
            await update.effective_message.reply_text(
                "Сначала админ группы должен выполнить /setup 07:00 08:30."
            )
            return
        member, result = register_member(
            db, chat.id, update.effective_user, settings(context.application).max_members
        )

    if result == "full":
        await update.effective_message.reply_text(
            "Все места уже заняты. Лимит меняется переменной MAX_MEMBERS в Railway."
        )
    elif result == "created":
        await update.effective_message.reply_text(
            f"✅ {html.escape(member.display_name)}, ты в списке!",
            parse_mode=ParseMode.HTML,
        )
    else:
        await update.effective_message.reply_text("Ты уже в списке 👍")


async def members_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_group(update):
        return
    chat = update.effective_chat
    with session_factory(context.application)() as db:
        members = db.scalars(
            select(Member)
            .where(Member.chat_id == chat.id, Member.active.is_(True))
            .order_by(Member.joined_at)
        ).all()
    limit = settings(context.application).max_members
    lines = [f"👥 <b>Участники ({len(members)}/{limit})</b>"]
    lines.extend(f"{number}. {html.escape(m.display_name)}" for number, m in enumerate(members, 1))
    if len(members) < limit:
        lines.append("\nСвободный участник может нажать /join.")
    await update.effective_message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def remove_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_group(update):
        return
    if not await is_admin(update, context):
        await update.effective_message.reply_text("Освобождать места может админ группы.")
        return
    replied = update.effective_message.reply_to_message
    if not replied or not replied.from_user:
        await update.effective_message.reply_text(
            "Ответь командой /remove на сообщение участника, которого нужно убрать."
        )
        return
    with session_factory(context.application)() as db:
        member = get_member(db, update.effective_chat.id, replied.from_user.id)
        if not member:
            await update.effective_message.reply_text("Этот человек не занимает место.")
            return
        removed_name = member.display_name
        member.active = False
        db.commit()
    await update.effective_message.reply_text(
        f"Место {html.escape(removed_name)} освобождено. История сохранена.",
        parse_mode=ParseMode.HTML,
    )


async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_group(update):
        return
    with session_factory(context.application)() as db:
        config = db.get(GroupConfig, update.effective_chat.id)
        if not config:
            await update.effective_message.reply_text("Бот ещё не настроен: /setup 07:00 08:30")
            return
        text = (
            "⚙️ <b>Настройки</b>\n"
            f"Утренняя карточка: <b>{config.morning_time}</b>\n"
            f"Начало школы: <b>{config.school_start_time}</b>\n"
            f"Часовой пояс: <b>{html.escape(config.timezone)}</b>\n"
            f"Лимит участников: <b>{settings(context.application).max_members}</b>\n\n"
            "Изменить время: /setup 07:00 08:30"
        )
    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML)


async def today_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_group(update):
        return
    chat_id = update.effective_chat.id
    with session_factory(context.application)() as db:
        config = db.get(GroupConfig, chat_id)
        if not config:
            await update.effective_message.reply_text("Сначала настройте бота: /setup 07:00 08:30")
            return
        day = local_day(config)
        text = render_daily_summary(db, config, day)
    await update.effective_message.reply_text(
        text, parse_mode=ParseMode.HTML, reply_markup=status_keyboard()
    )


async def verify_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_group(update):
        return
    chat_id = update.effective_chat.id
    cards = []
    with session_factory(context.application)() as db:
        config = db.get(GroupConfig, chat_id)
        if not config:
            await update.effective_message.reply_text("Сначала настройте бота: /setup 07:00 08:30")
            return
        day = local_day(config)
        attendance_ids = db.scalars(
            select(Attendance.id)
            .where(Attendance.chat_id == chat_id, Attendance.day == day)
            .order_by(Attendance.id)
        ).all()
        for attendance_id in attendance_ids:
            data = get_verification_data(db, attendance_id)
            if data:
                cards.append((render_verification_card(data), attendance_id, data[3], data[4]))

    if not cards:
        await update.effective_message.reply_text("Сегодня пока нечего проверять.")
        return
    await update.effective_message.reply_text(
        "🔎 <b>Проверка обещаний за сегодня</b>\n"
        "Каждый может поставить или поменять одну оценку. Себя оценивать нельзя.",
        parse_mode=ParseMode.HTML,
    )
    for text, attendance_id, truth, lie in cards:
        await update.effective_message.reply_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=reaction_keyboard(attendance_id, truth, lie),
        )


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_group(update):
        return
    chat_id = update.effective_chat.id
    with session_factory(context.application)() as db:
        members = db.scalars(
            select(Member)
            .where(Member.chat_id == chat_id, Member.active.is_(True))
            .order_by(Member.joined_at)
        ).all()
        if not members:
            await update.effective_message.reply_text("Статистики пока нет.")
            return
        blocks = ["📊 <b>Общая статистика</b>"]
        for member in members:
            statuses = db.scalars(
                select(Attendance.status).where(Attendance.member_id == member.id)
            ).all()
            counts = status_counts(statuses)
            received_votes = db.scalars(
                select(Reaction.verdict)
                .join(Attendance, Reaction.attendance_id == Attendance.id)
                .where(Attendance.member_id == member.id)
            ).all()
            truth = sum(1 for vote in received_votes if vote == "truth")
            lie = sum(1 for vote in received_votes if vote == "lie")
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
    await update.effective_message.reply_text("\n".join(blocks), parse_mode=ParseMode.HTML)


async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_group(update):
        return
    chat_id = update.effective_chat.id
    with session_factory(context.application)() as db:
        config = db.get(GroupConfig, chat_id)
        if not config:
            await update.effective_message.reply_text("Сначала настройте бота: /setup 07:00 08:30")
            return
        today = local_day(config)
        start = today - timedelta(days=6)
        rows = db.execute(
            select(Attendance, Member)
            .join(Member, Attendance.member_id == Member.id)
            .where(
                Attendance.chat_id == chat_id,
                Attendance.day >= start,
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
                status = describe_status(
                    attendance.status,
                    attendance.delay_minutes,
                    attendance.arrival_time,
                )
                lines.append(f"• {html.escape(member.display_name)} — {status}")
    if not rows:
        lines.append("\nПока нет отметок.")
    await update.effective_message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(HELP_TEXT, parse_mode=ParseMode.HTML)


def upsert_attendance(
    db,
    chat_id: int,
    member: Member,
    day,
    status: str,
    delay_minutes: int | None = None,
    arrival_time: str | None = None,
) -> Attendance:
    record = db.scalar(
        select(Attendance).where(
            Attendance.member_id == member.id, Attendance.day == day
        )
    )
    if not record:
        record = Attendance(chat_id=chat_id, member_id=member.id, day=day, status=status)
        db.add(record)
    record.status = status
    record.delay_minutes = delay_minutes
    record.arrival_time = arrival_time
    db.commit()
    return record


async def refresh_saved_prompt(
    application: Application, chat_id: int, day, skip_message_id: int | None = None
) -> None:
    with session_factory(application)() as db:
        config = db.get(GroupConfig, chat_id)
        if not config or config.last_prompt_date != day or not config.prompt_message_id:
            return
        if config.prompt_message_id == skip_message_id:
            return
        text = render_daily_summary(db, config, day)
        message_id = config.prompt_message_id
    try:
        await application.bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            parse_mode=ParseMode.HTML,
            reply_markup=status_keyboard(),
        )
    except BadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            logger.info("Could not refresh daily prompt: %s", exc)


async def status_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.message or not update.effective_user:
        return
    chat_id = query.message.chat.id
    app_settings = settings(context.application)

    with session_factory(context.application)() as db:
        config = db.get(GroupConfig, chat_id)
        if not config:
            await query.answer("Сначала нужен /setup", show_alert=True)
            return
        member, result = register_member(db, chat_id, update.effective_user, app_settings.max_members)
        if result == "full" or not member:
            await query.answer("Ты не в списке участников", show_alert=True)
            return
        day = local_day(config)
        action = query.data.split(":", 1)[1]
        if action == "late":
            member_id = member.id
        else:
            upsert_attendance(db, chat_id, member, day, action)
            text = render_daily_summary(db, config, day)

    if action == "late":
        await query.answer("Выбери, на сколько опоздаешь")
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"⏰ {html.escape(display_name(update.effective_user))}, на сколько опоздаешь?",
            parse_mode=ParseMode.HTML,
            reply_markup=delay_keyboard(member_id),
        )
        return

    await query.answer("Сохранено ✅")
    try:
        await query.edit_message_text(
            text=text, parse_mode=ParseMode.HTML, reply_markup=status_keyboard()
        )
    except BadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            logger.info("Could not edit status card: %s", exc)
    await refresh_saved_prompt(
        context.application, chat_id, day, skip_message_id=query.message.message_id
    )


async def late_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.message or not update.effective_user:
        return
    _, requested_member_id, minutes_text = query.data.split(":")
    chat_id = query.message.chat.id
    with session_factory(context.application)() as db:
        config = db.get(GroupConfig, chat_id)
        member = get_member(db, chat_id, update.effective_user.id)
        if not config or not member or member.id != int(requested_member_id):
            await query.answer("Эти кнопки предназначены другому участнику", show_alert=True)
            return
        day = local_day(config)
        minutes = int(minutes_text)
        arrival = calculate_arrival(config.school_start_time, minutes)
        upsert_attendance(db, chat_id, member, day, "late", minutes, arrival)
        summary = render_daily_summary(db, config, day)

    await query.answer("Опоздание сохранено ✅")
    await query.edit_message_text(
        f"✅ {html.escape(member.display_name)}: опоздание на {minutes} мин, "
        f"будет к {arrival}.",
        parse_mode=ParseMode.HTML,
    )
    await refresh_saved_prompt(context.application, chat_id, day)
    await context.bot.send_message(
        chat_id=chat_id,
        text=summary,
        parse_mode=ParseMode.HTML,
        reply_markup=status_keyboard(),
    )


async def reaction_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.message or not update.effective_user:
        return
    _, attendance_id_text, verdict = query.data.split(":")
    attendance_id = int(attendance_id_text)
    chat_id = query.message.chat.id

    with session_factory(context.application)() as db:
        attendance = db.get(Attendance, attendance_id)
        if not attendance or attendance.chat_id != chat_id:
            await query.answer("Эта отметка уже недоступна", show_alert=True)
            return
        voter = get_member(db, chat_id, update.effective_user.id)
        if not voter:
            await query.answer("Сначала зарегистрируйся: /join", show_alert=True)
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
        if not reaction:
            reaction = Reaction(
                attendance_id=attendance.id,
                voter_member_id=voter.id,
                verdict=verdict,
            )
            db.add(reaction)
        else:
            reaction.verdict = verdict
        db.commit()
        data = get_verification_data(db, attendance.id)
        text = render_verification_card(data)
        truth, lie = data[3], data[4]

    await query.answer("Оценка сохранена")
    try:
        await query.edit_message_text(
            text=text,
            parse_mode=ParseMode.HTML,
            reply_markup=reaction_keyboard(attendance_id, truth, lie),
        )
    except BadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            raise


async def send_daily_prompt(application: Application, chat_id: int) -> None:
    with session_factory(application)() as db:
        config = db.get(GroupConfig, chat_id)
        if not config:
            return
        day = local_day(config)
        text = render_daily_summary(db, config, day)
    message = await application.bot.send_message(
        chat_id=chat_id,
        text="☀️ <b>Доброе утро!</b>\nПора отметиться.\n\n" + text,
        parse_mode=ParseMode.HTML,
        reply_markup=status_keyboard(),
    )
    with session_factory(application)() as db:
        config = db.get(GroupConfig, chat_id)
        if config:
            config.last_prompt_date = day
            config.prompt_message_id = message.message_id
            db.commit()


async def morning_tick(context: ContextTypes.DEFAULT_TYPE) -> None:
    with session_factory(context.application)() as db:
        configs = list(db.scalars(select(GroupConfig)).all())
    for config in configs:
        now = datetime.now(ZoneInfo(config.timezone))
        if now.strftime("%H:%M") < config.morning_time:
            continue
        if config.last_prompt_date == now.date():
            continue
        try:
            await send_daily_prompt(context.application, config.chat_id)
        except (Forbidden, BadRequest) as exc:
            logger.warning("Cannot send morning prompt to %s: %s", config.chat_id, exc)
        except TelegramError:
            logger.exception("Telegram error while sending morning prompt to %s", config.chat_id)


async def post_init(application: Application) -> None:
    await application.bot.set_my_commands(
        [
            BotCommand("today", "отметиться и увидеть сегодняшний статус"),
            BotCommand("verify", "проверить обещания за сегодня"),
            BotCommand("stats", "общая статистика"),
            BotCommand("history", "история за 7 дней"),
            BotCommand("members", "список участников"),
            BotCommand("join", "зарегистрироваться"),
            BotCommand("remove", "освободить место участника"),
            BotCommand("settings", "посмотреть настройки"),
            BotCommand("setup", "настроить группу и время"),
            BotCommand("help", "помощь"),
        ]
    )
    application.job_queue.run_repeating(morning_tick, interval=60, first=3, name="morning")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Unhandled update error", exc_info=context.error)


def build_application(app_settings: Settings) -> Application:
    application = (
        Application.builder().token(app_settings.bot_token).post_init(post_init).build()
    )
    application.bot_data["settings"] = app_settings
    application.bot_data["session_factory"] = create_session_factory(
        app_settings.database_url
    )

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("setup", setup_command))
    application.add_handler(CommandHandler("join", join_command))
    application.add_handler(CommandHandler("members", members_command))
    application.add_handler(CommandHandler("remove", remove_command))
    application.add_handler(CommandHandler("settings", settings_command))
    application.add_handler(CommandHandler("today", today_command))
    application.add_handler(CommandHandler("verify", verify_command))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(CommandHandler("history", history_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CallbackQueryHandler(status_callback, pattern=r"^s:"))
    application.add_handler(CallbackQueryHandler(late_callback, pattern=r"^l:"))
    application.add_handler(CallbackQueryHandler(reaction_callback, pattern=r"^r:"))
    application.add_error_handler(error_handler)
    return application


def main() -> None:
    app_settings = Settings.from_env()
    logger.info("Starting school attendance bot")
    build_application(app_settings).run_polling(drop_pending_updates=False)
