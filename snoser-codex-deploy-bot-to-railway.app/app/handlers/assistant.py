import html
import logging
import re

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import Message

from app.core.assistant import RPAssistant

logger = logging.getLogger(__name__)

_MAX_MESSAGE_LEN = 4000


def _split_long_text(text: str) -> list[str]:
    if len(text) <= _MAX_MESSAGE_LEN:
        return [text]
    chunks: list[str] = []
    current = ""
    for line in text.splitlines(keepends=True):
        if len(current) + len(line) > _MAX_MESSAGE_LEN:
            if current:
                chunks.append(current.rstrip())
            current = ""
        if len(line) > _MAX_MESSAGE_LEN:
            for i in range(0, len(line), _MAX_MESSAGE_LEN):
                chunks.append(line[i : i + _MAX_MESSAGE_LEN].rstrip())
            current = ""
        else:
            current += line
            if len(current) >= _MAX_MESSAGE_LEN:
                chunks.append(current.rstrip())
                current = ""
    if current:
        chunks.append(current.rstrip())
    return chunks


HELP_TEXT = (
    "Напиши вопрос одним из способов:\n"
    "· /ai можно ли строить танки?\n"
    "· ответь (реплай) на любое моё сообщение с вопросом\n"
    "· напиши моим @username и после него вопрос\n"
    "· /ai какие технологии разрешены?\n"
    "· /ai как заработать деньги?\n"
    "· /ai как провести мобилизацию?\n"
    "· /ai что даёт фабрика?"
)


_MENTION_RE = re.compile(r"\s*@[A-Za-z0-9_]{5,32}\b", re.IGNORECASE)
_QUESTION_PREFIXES = (
    "можно ",
    "можно ли",
    "как ",
    "какая ",
    "какие ",
    "какой ",
    "разреш",
    "запрещ",
    "сколько",
    "кто ",
    "что такое",
    "что да",
    "что нужно",
    "что лучше",
    "что даёт",
    "почему",
    "зачем",
    "когда ",
    "есть ли",
)


def _text_looks_like_question(text: str | None) -> bool:
    text = (text or "").strip()
    if not text or len(text) > 400:
        return False
    if text.startswith("/") or "\n" in text or "#" in text:
        return False
    low = text.lower()
    if "?" in low:
        return True
    return low.startswith(_QUESTION_PREFIXES)


def _looks_like_question(message: Message) -> bool:
    return _text_looks_like_question(message.text)


def bind_assistant_handlers(assistant: RPAssistant, bot: Bot) -> Router:
    router = Router(name="assistant")
    cached_username: dict[int, str] = {}

    async def _bot_username(message: Message) -> str:
        bot_id = id(bot)
        if bot_id in cached_username:
            return cached_username[bot_id]
        try:
            me = await bot.get_me()
            username = me.username or ""
        except Exception:  # pragma: no cover - network failure
            username = ""
        cached_username[bot_id] = username
        return username

    async def _reply(message: Message, question: str) -> None:
        try:
            answer = await assistant.answer(question)
        except Exception as exc:  # pragma: no cover - defensive guard
            logger.exception("Assistant failed for question: %s", question)
            answer = "⛔ Не удалось подготовить ответ. Попробуйте позже. Ошибка: " + str(exc)
        for chunk in _split_long_text(answer):
            await message.answer(html.escape(chunk), parse_mode="HTML")

    @router.message(Command("ai"))
    async def ai_command(message: Message) -> None:
        question = (message.text or "").partition(" ")[2].strip()
        if not question:
            await message.answer(HELP_TEXT)
            return
        await _reply(message, question)

    @router.message(F.chat.type == "private", F.text, _looks_like_question)
    async def ai_private_question(message: Message) -> None:
        await _reply(message, message.text.strip())

    @router.message(F.reply_to_message, F.text)
    async def ai_reply_question(message: Message) -> None:
        """Реплай на сообщение бота = вопрос ассистенту."""
        replied = message.reply_to_message
        if not replied or replied.from_user is None or not replied.from_user.is_bot:
            return False
        question = _MENTION_RE.sub("", (message.text or "")).strip()
        if not question:
            return False
        await _reply(message, question)
        return True

    @router.message(F.text)
    async def ai_mention_question(message: Message) -> None:
        """Упоминание @username бота в любом чате = вопрос ассистенту."""
        text = message.text or ""
        username = await _bot_username(message)
        if not username:
            return False
        if f"@{username.lower()}" not in text.lower():
            return False
        question = _MENTION_RE.sub(" ", text).strip()
        if not question:
            await message.answer(HELP_TEXT)
            return True
        await _reply(message, question)
        return True

    return router