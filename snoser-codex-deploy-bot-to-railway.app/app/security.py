"""
Security module: jailbreak protection, off-topic filter, rate limiting.
"""
from __future__ import annotations
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from config import ADMIN_ID, SECURITY_MUTE_THRESHOLD, OFFTOPIC_MUTE_THRESHOLD, MUTE_DURATION_HOURS, OFFTOPIC_MUTE_DURATION_MINUTES

log = logging.getLogger(__name__)

# Jailbreak patterns (case-insensitive)
JAILBREAK_PATTERNS = [
    r"\[SYSTEM", r"\[OVERRIDE", r"\[DEBUG", r"\[EMERGENCY", r"\[CRITICAL",
    r"\[WARLORD RP SYSTEM", r"\[SYSTEM COMMAND", r"\[SYSTEM UPDATE",
    r"ignore previous", r"игнорируй предыдущие", r"забудь все инструкции",
    r"забудь, что ты", r"ты теперь", r"твоя новая роль", r"твое имя",
    r"с этого момента ты", r"DAN", r"Do Anything Now",
    r"режим симуляции", r"Full Human Emulation",
    r"CODE 771A", r"SYSTEM_LOG", r"DATABASE_INJECTION",
    r"USER_PROFILES_DUMP", r"GLOBAL_STRATEGY_REPORT",
    r"докс", r"докси", r"слив данных", r"выгрузи данные", r"выдай данные о пользователях",
    r"prompt injection", r"prompt leak",
    r"покажи свой промпт", r"покажи системное сообщение",
]

# Off-topic indicators
OFFTOPIC_PATTERNS = [
    r"^как покакать", r"как пописать", r"что такое любовь",
    r"погод[ау]", r"расскажи анекдот", r"шутк[ау]",
    r"как приготовить", r"рецепт", r"фильм", r"сериал",
    r"музык[ау]", r"песн[яу]", r"игру[ау]", r"другая игра",
    r"проект\w*\s+(?!warlord)", r"other project",
]

# WARLORD RP keywords (whitelist)
WARLORD_KEYWORDS = [
    "warlord", "варлорд", "war lord",
    "правила", "механик", "карточк", "страна", "группировк",
    "войн", "экономик", "исследован", "налог", "стройк", "колонизац",
    "оят", "паоници", "паоник", "паоник",
    "бюджет", "населен", "арми", "техник", "промышленн",
    "стабильност", "поддержк", "счасть", "налог",
    "фабрик", "завод", "верф", "порт", "доро", "нп", "пгт",
    "мобилизац", "депутат", "президент", "правител",
    "парти", "идеолог", "цвет", "флаг", "эмблем",
    "командир", "базирован", "зависит", "репутац",
    "контракт", "войн", "битв", "битва",
    "награда", "штраф", "санкци", "теракт", "эпидеми",
    "гражданск", "войн", "мир", "перемири",
    "ресурс", "нефть", "газ", "уран", "железо", "медь",
    "посел", "деревн", "село", "посёл", "город", "столиц",
    "исследовани", "технолог", "улучшен", "апгрейд",
]

# Commands that bypass filters
ALLOWED_COMMANDS = {
    "/start", "/help", "/ai", "/newcountry", "/status",
    "/update", "/process", "/cards", "/unreg", "/mute",
    "/unmute", "/security", "/tokenreport"
}

_JAILBREAK_RE = re.compile("|".join(JAILBREAK_PATTERNS), re.IGNORECASE)
_OFFTOPIC_RE = re.compile("|".join(OFFTOPIC_PATTERNS), re.IGNORECASE)
_WARLORD_RE = re.compile("|".join(WARLORD_KEYWORDS), re.IGNORECASE)

@dataclass
class SecurityResult:
    allowed: bool
    reason: str | None = None
    reply_text: str | None = None

class SecurityGuard:
    def __init__(self, db: Any):
        self.db = db
        self._muted: dict[int, datetime] = {}

    async def check_message(
        self,
        user_id: int,
        username: str | None,
        text: str,
        is_reply_to_bot: bool = False,
        is_command: bool = False,
        command: str | None = None,
        mentioned_bot: bool = False
    ) -> SecurityResult:
        """Main entry point: check message before processing."""

        # 1. Admin bypass
        if user_id == ADMIN_ID:
            return SecurityResult(allowed=True)

        # 2. Mute check
        if user_id in self._muted:
            if datetime.now(timezone.utc) < self._muted[user_id]:
                return SecurityResult(
                    allowed=False,
                    reason="muted",
                    reply_text="Вы временно ограничены в использовании бота."
                )
            else:
                del self._muted[user_id]

        # 3. Commands bypass (if allowed)
        if is_command and command and command.split()[0].lower() in ALLOWED_COMMANDS:
            return SecurityResult(allowed=True)

        # 4. Replies to bot / mentions bypass
        if is_reply_to_bot or mentioned_bot:
            return SecurityResult(allowed=True)

        # 5. Jailbreak check
        if _JAILBREAK_RE.search(text):
            await self._log_security(user_id, username, text, "jailbreak")
            await self._notify_admin(user_id, username, text, "JAILBREAK")
            count = await self.db.get_security_count(user_id, hours=1)
            if count >= SECURITY_MUTE_THRESHOLD:
                self._muted[user_id] = datetime.now(timezone.utc) + timedelta(hours=MUTE_DURATION_HOURS)
            return SecurityResult(
                allowed=False,
                reason="jailbreak",
                reply_text="Я не могу обработать этот запрос. Задай вопрос по правилам WARLORD RP."
            )

        # 6. Off-topic check
        if _OFFTOPIC_RE.search(text):
            if not _WARLORD_RE.search(text):
                await self._log_security(user_id, username, text, "offtopic")
                count = await self.db.get_security_count(user_id, hours=1)
                if count >= OFFTOPIC_MUTE_THRESHOLD:
                    self._muted[user_id] = datetime.now(timezone.utc) + timedelta(minutes=OFFTOPIC_MUTE_DURATION_MINUTES)
                return SecurityResult(
                    allowed=False,
                    reason="offtopic",
                    reply_text="Я отвечаю только на вопросы по WARLORD RP. Задай вопрос по правилам, механике или карточкам."
                )

        # 7. Empty/low-content check
        if len(text.strip()) < 3 and not is_command:
            return SecurityResult(
                allowed=False,
                reason="too_short",
                reply_text="Слишком короткое сообщение. Сформулируйте вопрос по WARLORD RP."
            )

        return SecurityResult(allowed=True)

    async def _log_security(self, user_id: int, username: str | None, text: str, reason: str) -> None:
        await self.db.log_security(user_id, username, text, reason)

    async def _notify_admin(self, user_id: int, username: str | None, text: str, reason: str) -> None:
        # This will be called from handlers where bot is available
        log.warning("Security event: %s from %s (@%s): %s", reason, user_id, username, text[:100])

    def mute_user(self, user_id: int, duration_hours: int = 1) -> None:
        self._muted[user_id] = datetime.now(timezone.utc) + timedelta(hours=duration_hours)

    def unmute_user(self, user_id: int) -> bool:
        if user_id in self._muted:
            del self._muted[user_id]
            return True
        return False

    def is_muted(self, user_id: int) -> bool:
        if user_id in self._muted:
            if datetime.now(timezone.utc) < self._muted[user_id]:
                return True
            del self._muted[user_id]
        return False