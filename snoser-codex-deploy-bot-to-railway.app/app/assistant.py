"""
Module A: Assistant - answers player questions about WARLORD RP.
Read-only access to DB, no card generation, no DB writes.
"""
from __future__ import annotations
import logging
from typing import Any

from aiogram import Bot
from aiogram.types import Message

from config import config
from db import Database
from ai import AIClient
from cards import build_country_card
from security import SecurityGuard, SecurityResult

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
        is_reply = message.reply_to_message is not None
        is_command = text.startswith("/")
        cmd = text.split()[0].lower() if is_command else None
        mentioned = f"@{self.bot.username}" in text if self.bot.username else False

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
                "/ai <вопрос> — задать вопрос ассистенту\n"
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
                countries = await self.db.list_countries()
                factions = await self.db.list_factions()
                entities = await self.db.list_entities()
                questions = await self.db.list_unanswered_questions()
                topics = await self.db.list_topics()
                txt = (
                    "📊 <b>Статус бота:</b>\n"
                    f"  🇺🇳 Стран: {len(countries)}\n"
                    f"  🎖 Группировок: {len(factions)}\n"
                    f"  🧩 Сущностей: {len(entities)}\n"
                    f"  ❓ Вопросов админу: {len(questions)}\n"
                    f"  🗂 Тем: {len(topics)}\n"
                    f"  🤖 Провайдер: {config.AI_PROVIDER}\n"
                    f"  📈 Токенов за день: {self.ai._daily_tokens}"
                )
                await message.answer(txt, parse_mode="HTML")
            else:
                await message.answer("Команда доступна только админу.")
            return True

        if cmd == "/tokenreport":
            if message.from_user.id == config.admin_id:
                await message.answer(self.ai.get_daily_token_report())
            return True

        return False

    async def _handle_question(self, message: Message) -> bool:
        """Handle natural language question."""
        text = message.text or ""
        if not text.strip():
            return False

        # Check if asking about specific country
        country_name = self._extract_country_name(text)
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

        # General question - use AI with DB context
        context = await self._build_context(text)
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

    async def _build_context(self, question: str) -> str:
        """Build relevant DB context for the question."""
        context_parts = []

        # Check for specific keywords and add relevant data
        q_lower = question.lower()

        if any(w in q_lower for w in ("страна", "страны", "государств")):
            countries = await self.db.list_countries("active")
            if countries:
                names = [c["name"] for c in countries[:15]]
                context_parts.append(f"Активные страны: {', '.join(names)}")

        if any(w in q_lower for w in ("группировк", "фракц", "чвк", "отряд")):
            factions = await self.db.list_factions()
            if factions:
                names = [f["full_name"] for f in factions[:10]]
                context_parts.append(f"Группировки: {', '.join(names)}")

        if any(w in q_lower for w in ("завод", "фабрик", "стройк", "постро", "здани")):
            from cards import BUILDING_COSTS, BUILDING_TIMES
            lines = [f"{k}: {v} вирт-руб., {BUILDING_TIMES.get(k, '?')} ч" for k, v in BUILDING_COSTS.items()]
            context_parts.append("Строительство:\n" + "\n".join(lines))

        if any(w in q_lower for w in ("налог", "наlogu")):
            context_parts.append("Налоговые уровни: low (0.5), standard (1.0), high (1.5), maximum (2.0). Смена не чаще 1 раза в 3 дня.")

        if any(w in q_lower for w in ("ояв", "ояй", "паониц")):
            context_parts.append("ОЯТ — вымышленный ресурс Паониции. Производство: 500 ед. + 4ч на иссл. центре. Хранение в бункере. Война: +10% урона по укреплениям, -5% стабильности. Торговля разрешена.")

        return "\n\n".join(context_parts)

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