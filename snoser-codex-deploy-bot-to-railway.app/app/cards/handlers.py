import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import Message

from app.cards.storage import CardStorage
from app.config import config

logger = logging.getLogger(__name__)


def bind_card_handlers(db: CardStorage) -> Router:
    router = Router(name="cards")

    async def _is_question_reply(message: Message) -> bool:
        reply_to = message.reply_to_message
        if not reply_to or not reply_to.message_id or not message.text:
            return False
        questions = await db.list_unanswered_questions()
        return any(
            q.get("telegram_message_id") == reply_to.message_id for q in questions
        )

    @router.message(
        F.chat.type == "private",
        F.from_user.id == config.admin_id,
        F.reply_to_message,
        _is_question_reply,
    )
    async def admin_reply_to_question(message: Message) -> None:
        reply_to = message.reply_to_message
        questions = await db.list_unanswered_questions()
        target = next(
            (q for q in questions if q.get("telegram_message_id") == reply_to.message_id),
            None,
        )
        if target is None:
            return
        answer = message.text.strip()
        ok = await db.answer_question(int(target["id"]), answer[:2000])
        country = str(target.get("country_name") or "")
        if ok:
            await message.answer(
                f"✅ Ответ для «{country}» сохранён. "
                "Следующим циклом механик применит его к карточке."
            )
        else:
            await message.answer("⚠️ Не удалось сохранить ответ, попробуйте ещё раз.")

    @router.message(Command("cards"))
    async def cards_status(message: Message) -> None:
        if message.from_user and message.from_user.id != config.admin_id:
            return
        count = await db.country_count()
        unanswered = await db.list_unanswered_questions()
        lines = [
            "🗂 Карточки стран",
            f"Стран в базе: {count}",
            f"Нерешённых вопросов админу: {len(unanswered)}",
        ]
        if unanswered:
            lines.append("\nОткрой последний вопрос и ответь на него реплаем ✉️")
        await message.answer("\n".join(lines))

    return router