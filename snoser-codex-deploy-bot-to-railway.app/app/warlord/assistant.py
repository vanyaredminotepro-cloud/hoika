"""
Module A: Assistant - answers player questions about WARLORD RP.
Read-only access to DB, no card generation, no DB writes.
"""
from __future__ import annotations
import logging
from typing import Any

from aiogram import Bot
from aiogram.types import Message

from .config import config, bot_username
from .db import Database
from .ai import AIClient
from . import knowledge
from .cards import build_country_card
from .security import (
    SecurityGuard, SecurityResult,
    is_reply_to_bot, should_respond_to_bot,
)

log = logging.getLogger(__name__)

class Assistant:
    """Module A - Player assistant for WARLORD RP questions."""

    def __init__(self, db: Database, ai: AIClient, security: SecurityGuard, bot: Bot):
        self.db = db
        self.ai = ai
        self.security = security
        self.bot = bot

    async def handle_message(self, message: Message) -> bool:
        """Main entry: check security, route to handler. Returns True if handled."""
        user_id = message.from_user.id
        username = message.from_user.username
        text = message.text or ""
        is_command = text.startswith("/")
        cmd = text.split()[0].lower() if is_command else None
        mentioned = f"@{bot_username}" in text if bot_username else False
        is_reply = is_reply_to_bot(message, getattr(self.bot, "id", None))

        # Гейт адресации: в группе отвечаем только на обращение к боту,
        # иначе ассистент писал бы на каждое сообщение в чате.
        if not should_respond_to_bot(message, getattr(self.bot, "id", None), bot_username):
            return False

        # Security check
        sec_result = await self.security.check_message(
            user_id, username, text,
            is_reply_to_bot=is_reply,
            is_command=is_command,
            command=cmd,
            mentioned_bot=mentioned
        )
        if not sec_result.allowed:
            if sec_result.reply_text:
                await message.answer(sec_result.reply_text)
            return True

        # Route commands
        if is_command:
            return await self._handle_command(message, cmd)

        # Regular question
        return await self._handle_question(message)

    async def _handle_command(self, message: Message, cmd: str) -> bool:
        """Handle bot commands for Module A."""
        if cmd == "/ai":
            # /ai <question> - direct question to assistant
            text = message.text
            if text:
                parts = text.split(maxsplit=1)
                if len(parts) > 1:
                    await self._answer_question(message, parts[1])
                    return True
            await message.answer("Использование: /ai <ваш вопрос по WARLORD RP>")
            return True

        if cmd == "/help":
            await message.answer(
                "🤖 <b>Warlord RP Assistant</b>\n\n"
                "Я отвечаю на вопросы по правилам, механике и карточкам WARLORD RP.\n\n"
                "<b>Команды:</b>\n"
                "/ai &lt;вопрос&gt; — задать вопрос ассистенту\n"
                "/help — эта справка\n\n"
                "<b>Примеры вопросов:</b>\n"
                "• «Расскажи про Паоницию»\n"
                "• «Сколько стоит завод?»\n"
                "• «Как работает мобилизация?»\n"
                "• «Что такое ОЯТ?»\n"
                "• «Правила колонизации»",
                parse_mode="HTML"
            )
            return True

        if cmd == "/status":
            if message.from_user.id == config.admin_id:
                import html as _html
                countries = await self.db.list_countries()
                factions = await self.db.list_factions()
                entities = await self.db.list_entities()
                questions = await self.db.list_questions()
                topics = await self.db.list_topics()
                txt = (
                    "📊 <b>Статус бота:</b>\n"
                    f"  🇺🇳 Стран: {len(countries)}\n"
                    f"  🎖 Группировок: {len(factions)}\n"
                    f"  🧩 Сущностей: {len(entities)}\n"
                    f"  ❓ Вопросов админу: {len(questions)}\n"
                    f"  🗂 Тем: {len(topics)}\n"
                    f"  🤖 Провайдер: {_html.escape(str(config.AI_PROVIDER))}\n"
                    f"  📈 Токенов за день: {self.ai._daily_tokens}"
                )
                await message.answer(txt, parse_mode="HTML")
            else:
                await message.answer("Команда доступна только админу.")
            return True

        if cmd == "/tokenreport":
            if message.from_user.id == config.admin_id:
                # Отчёт из token_usage с разрезом по моделям (п.13 ТЗ)
                await message.answer(await self.ai.daily_token_report())
            return True

        return False

    async def _answer_question(self, message: Message, question: str) -> None:
        """Answer a direct question using AI with context."""
        context = await self._build_context(question)
        answer = await self.ai.answer_question(question, context, user_id=message.from_user.id)
        await message.answer(answer)

    async def _handle_question(self, message: Message) -> bool:
        """Handle natural language question."""
        text = message.text or ""
        if not text.strip():
            return False

        # Check if asking about specific country
        country_name = self._extract_country_name(text)
        country = None
        if country_name:
            country = await self.db.get_country(country_name)
            if country:
                await self._send_country_info(message, country)
                return True
            # Check if unreg or not accepted
            entities = await self.db.list_entities("country")
            for e in entities:
                if e["name"].lower() == country_name.lower():
                    if e["status"] == "unreg":
                        await message.answer("Страна анрегнута, данных нет.")
                        return True
                    if e["status"] != "active":
                        await message.answer("Эта страна не принята в проект.")
                        return True

        # General question - use AI with DB + knowledge base context
        context = await self._build_context(text, country=country)
        answer = await self.ai.answer_question(text, context, user_id=message.from_user.id)
        await message.answer(answer)
        return True

    def _extract_country_name(self, text: str) -> str | None:
        """Extract country name from question like 'Расскажи про X'."""
        import re
        patterns = [
            r"(?:расскажи про|инфо про|что такое|кто таков|про)\s+([А-ЯЁA-Za-z0-9\s\-]{2,40})",
            r"страна\s+([А-ЯЁA-Za-z0-9\s\-]{2,40})",
            r"^([А-ЯЁA-Za-z0-9\s\-]{2,40})\s*—\s*что это",
        ]
        for pat in patterns:
            m = re.search(pat, text, re.IGNORECASE)
            if m:
                name = m.group(1).strip().strip('«»"\'')
                # Filter out common words
                if len(name) > 2 and name.lower() not in ("правил", "механик", "экономик", "войн", "правила"):
                    return name
        return None

    async def _build_context(
        self, question: str, country: dict[str, Any] | None = None
    ) -> str:
        """Build DB + knowledge-base context for the question.

        Раньше контекст наполнялся только по ключевым словам и для большего
        вопроса был пустым — ассистент отвечал
        «в базе нет информации». Теперь всегда
        добавляется общая справка из knowledge.py.
        """
        countries: list[dict[str, Any]] | None = None
        factions: list[dict[str, Any]] | None = None
        q = question.lower()
        if any(w in q for w in ("страна", "государств", "кто", "перечень", "список", "проект")):
            try:
                countries = await self.db.list_countries()
            except Exception:  # noqa: BLE001
                countries = None
        if any(w in q for w in ("группиров", "фракц", "чвк", "отряд", "кто", "перечень", "список")):
            try:
                factions = await self.db.list_factions()
            except Exception:  # noqa: BLE001
                factions = None
        return knowledge.build_context(
            question, country=country, countries=countries, factions=factions
        )

    async def _send_country_info(self, message: Message, country: dict[str, Any]) -> None:
        """Send formatted country info without LLM."""
        status = country.get("status", "active")
        if status == "unreg":
            await message.answer("Страна анрегнута, данных нет.")
            return
        if status != "active":
            await message.answer("Эта страна не принята в проект.")
            return

        text = build_country_card(country, "", "")
        # Truncate for chat message (no summary/notifications needed for info request)
        lines = text.split("\n")
        # Keep only key sections
        keep = []
        for line in lines:
            if any(kw in line for kw in ("ГОСУДАРСТВО", "Правитель", "Партия", "Идеология", "Цвет",
                                          "Население", "Казна", "Армия", "Налоги",
                                          "Промышленность", "Исследования", "ОЯТ",
                                          "Пехота", "Лёгкая", "Тяжёлая", "Артиллерия", "БПЛА", "Флот",
                                          "ГРУППИРОВКИ", "Стабильность", "Поддержка", "Счастье")):
                keep.append(line)
            if line.startswith("🏴") or line.startswith("•"):
                keep.append(line)
        out = "\n".join(keep[:30])
        await message.answer(out)

    # ----- Read-only API for Module B -----
    async def get_country_card_data(self, name: str) -> dict[str, Any] | None:
        """Module B can call this to get card data for reading."""
        return await self.db.get_country(name)

    async def get_faction_card_data(self, name: str) -> dict[str, Any] | None:
        return await self.db.get_faction(name)