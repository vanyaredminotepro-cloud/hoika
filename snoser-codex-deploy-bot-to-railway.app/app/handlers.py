"""
aiogram 3 handlers for Warlord RP Bot.
"""
from __future__ import annotations
import logging
import re
from aiogram import Router, F, Bot
from aiogram.filters import Command, CommandStart
from aiogram.types import Message, ReplyKeyboardRemove
from aiogram.enums import ParseMode

from config import config, ADMIN_ID
from db import Database
from security import SecurityGuard
from assistant import Assistant
from mechanic import Mechanic

log = logging.getLogger(__name__)
router = Router(name="warlord_main")

# Global instances (set in main.py)
db: Database | None = None
security: SecurityGuard | None = None
assistant: Assistant | None = None
mechanic: Mechanic | None = None
bot_instance: Bot | None = None

def setup_handlers(
    _db: Database,
    _security: SecurityGuard,
    _assistant: Assistant,
    _mechanic: Mechanic,
    _bot: Bot
) -> Router:
    """Initialize global instances and return router."""
    global db, security, assistant, mechanic, bot_instance
    db = _db
    security = _security
    assistant = _assistant
    mechanic = _mechanic
    bot_instance = _bot
    return router

# ---------- Helpers ----------
def is_admin(message: Message) -> bool:
    return message.from_user.id == ADMIN_ID

def is_reply_to_bot(message: Message) -> bool:
    if not message.reply_to_message:
        return False
    return message.reply_to_message.from_user.id == bot_instance.id if bot_instance else False

def extract_command(message: Message) -> str | None:
    text = message.text or ""
    if text.startswith("/"):
        return text.split()[0].lower()
    return None

def mentioned_bot(message: Message) -> bool:
    if not bot_instance or not bot_instance.username:
        return False
    return f"@{bot_instance.username}" in (message.text or "")

# ---------- Handlers ----------
@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    await message.answer(
        "🤖 <b>Warlord RP Bot</b>\n\n"
        "Я — ассистент по WARLORD RP. Задавай вопросы по правилам, механике, странам и группировкам.\n\n"
        "Команды:\n"
        "/help — справка\n"
        "/ai <вопрос> — спросить ассистента\n\n"
        "Админ-команды (только для админа):\n"
        "/process — запустить цикл обновления\n"
        "/cards — переопубликовать карточки\n"
        "/newcountry <country|faction> <JSON> — создать страну/группировку\n"
        "/status — статус бота\n"
        "/tokenreport — отчёт по токенам",
        parse_mode=ParseMode.HTML
    )

@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await cmd_start(message)

@router.message(Command("ai"))
async def cmd_ai(message: Message) -> None:
    """Direct question to assistant."""
    if not assistant:
        return
    text = message.text or ""
    parts = text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Использование: /ai <ваш вопрос по WARLORD RP>", parse_mode=ParseMode.HTML)
        return
    # Security check
    sec = await security.check_message(
        message.from_user.id, message.from_user.username, parts[1],
        is_command=True, command="/ai", mentioned_bot=False
    )
    if not sec.allowed:
        if sec.reply_text:
            await message.answer(sec.reply_text)
        return
    await assistant._answer_question(message, parts[1])

@router.message(Command("status"))
async def cmd_status(message: Message) -> None:
    if not assistant or not is_admin(message):
        return
    await assistant._handle_command(message, "/status")

@router.message(Command("tokenreport"))
async def cmd_tokenreport(message: Message) -> None:
    if not assistant or not is_admin(message):
        return
    await assistant._handle_command(message, "/tokenreport")

@router.message(Command("process"))
async def cmd_process(message: Message) -> None:
    if not mechanic or not is_admin(message):
        return
    await message.answer("🔄 Запускаю цикл обновления...")
    await mechanic.run_cycle()
    await message.answer("✅ Цикл завершён.")

@router.message(Command("cards"))
async def cmd_cards(message: Message) -> None:
    if not mechanic or not is_admin(message):
        return
    await message.answer("🃏 Переопубликовываю карточки...")
    await mechanic._publish_cards()
    await message.answer("✅ Карточки обновлены.")

@router.message(Command("newcountry"))
async def cmd_newcountry(message: Message) -> None:
    if not mechanic or not is_admin(message):
        return
    text = message.text or ""
    result = await mechanic.handle_admin_command(text)
    await message.answer(result)

@router.message(Command("unreg"))
async def cmd_unreg(message: Message) -> None:
    if not is_admin(message):
        return
    text = message.text or ""
    parts = text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Использование: /unreg <название страны или группировки>", parse_mode=ParseMode.HTML)
        return
    name = parts[1].strip()
    # TODO: implement unreg logic
    await message.answer(f"🗑 Анрег для {name} — в разработке.")

@router.message(Command("mute"))
async def cmd_mute(message: Message) -> None:
    if not is_admin(message):
        return
    parts = (message.text or "").split(maxsplit=2)
    if len(parts) < 2:
        await message.answer("Использование: /mute <user_id> [часы]")
        return
    try:
        uid = int(parts[1])
        hours = int(parts[2]) if len(parts) > 2 else 1
        security.mute_user(uid, hours)
        await message.answer(f"🔇 Пользователь {uid} замьючен на {hours} ч.")
    except ValueError:
        await message.answer("Неверный user_id или часы.")

@router.message(Command("unmute"))
async def cmd_unmute(message: Message) -> None:
    if not is_admin(message):
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Использование: /unmute <user_id>")
        return
    try:
        uid = int(parts[1])
        if security.unmute_user(uid):
            await message.answer(f"🔊 Пользователь {uid} размьючен.")
        else:
            await message.answer("Пользователь не в муте.")
    except ValueError:
        await message.answer("Неверный user_id.")

@router.message(Command("security"))
async def cmd_security(message: Message) -> None:
    if not is_admin(message):
        return
    rows = await db.fetchall(
        "SELECT user_id, username, reason, timestamp, text FROM security_log ORDER BY id DESC LIMIT 20"
    )
    if not rows:
        await message.answer("Лог безопасности пуст.")
        return
    lines = ["🛡 <b>Security Log (последние 20):</b>"]
    for r in rows:
        lines.append(
            f"• <code>{r['user_id']}</code> @{r['username'] or '?'} — {r['reason']} — {r['timestamp'][:19]}"
        )
    await message.answer("\n".join(lines), parse_mode=ParseMode.HTML)

# ---------- Admin reply handler ----------
@router.message(F.reply_to_message)
async def admin_reply(message: Message) -> None:
    """Handle admin replies to bot questions."""
    if not is_admin(message):
        return
    if not message.text:
        return

    replied_id = message.reply_to_message.message_id
    # Find question by telegram message id (stored in questions_log)
    question = await db.fetchone(
        "SELECT * FROM questions_log WHERE telegram_message_id=? AND answer=''",
        (replied_id,)
    )
    if not question:
        return

    answer_text = message.text.strip()
    await db.answer_question(question["id"], answer_text)
    await db.mark_questions_applied([question["id"]])

    # Apply answer if it's a country/faction field
    qtext = question["question"]
    applied = False

    # Match country missing field: "Страна «X»: не указано (поля)..."
    m = re.search(r"Страна\s*[«\"]([^»\"]+)[»\"]", qtext)
    if m:
        cname = m.group(1).strip()
        country = await db.get_country(cname)
        if country:
            # Try to parse which field
            missing_match = re.search(r"не указано\s*\(([^)]+)\)", qtext)
            if missing_match:
                fields = [f.strip() for f in missing_match.group(1).split(",")]
                updates = {}
                for field in fields:
                    field = field.lower()
                    if "флаг" in field and answer_text:
                        updates["flag_file_id"] = answer_text
                    elif "правител" in field and answer_text:
                        updates["ruler"] = answer_text
                    elif "цвет" in field and answer_text:
                        updates["color"] = answer_text
                    elif "посел" in field and answer_text:
                        # parse settlements
                        pass
                if updates:
                    await db.upsert_country({**country, **updates})
                    applied = True

    # Match faction missing field
    m = re.search(r"Группировка\s*[«\"]([^»\"]+)[»\"]", qtext)
    if m and not applied:
        fname = m.group(1).strip()
        faction = await db.get_faction(fname)
        if faction:
            updates = {}
            if "эмблем" in qtext.lower() and answer_text:
                updates["emblem_file_id"] = answer_text
            elif "командир" in qtext.lower() and answer_text:
                updates["commander"] = answer_text
            elif "базирован" in qtext.lower() and answer_text:
                updates["base_country"] = answer_text
            if updates:
                await db.upsert_faction({**faction, **updates})
                applied = True

    if applied:
        await message.answer("✅ Данные сохранены и применены к карточке.")
    else:
        await message.answer("✅ Ответ принят. Будет применён в следующем цикле.")

# ---------- Main message handler ----------
@router.message(F.text)
async def handle_message(message: Message) -> None:
    """Main message handler - routes to assistant."""
    if not assistant or not security:
        return

    # Skip if already handled by command handlers
    if message.text and message.text.startswith("/"):
        return

    user_id = message.from_user.id
    username = message.from_user.username
    text = message.text or ""
    is_reply = message.reply_to_message is not None
    cmd = extract_command(message)
    mentioned = mentioned_bot(message)

    # Security check
    sec = await security.check_message(
        user_id, username, text,
        is_reply_to_bot=is_reply,
        is_command=bool(cmd),
        command=cmd,
        mentioned_bot=mentioned
    )
    if not sec.allowed:
        if sec.reply_text:
            await message.answer(sec.reply_text)
        return

    # Delegate to assistant
    await assistant.handle_message(message)

# ---------- New country registration from anketa ----------
@router.message(F.chat.id == config.ANKETA_CHAT_ID, F.message_thread_id == config.ANKETA_TOPIC_ID)
async def handle_anketa(message: Message) -> None:
    """Parse new country/faction anketa from dedicated topic."""
    if not message.text:
        return
    # This would be processed by mechanic's anketa loader
    # For now, just log
    log.info("New anketa message in topic: %s", message.text[:100])