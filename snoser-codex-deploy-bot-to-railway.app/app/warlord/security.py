"""
Security module: jailbreak protection, off-topic filter, rate limiting.
"""
from __future__ import annotations
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .config import ADMIN_ID, SECURITY_MUTE_THRESHOLD, OFFTOPIC_MUTE_THRESHOLD, MUTE_DURATION_HOURS, OFFTOPIC_MUTE_DURATION_MINUTES

log = logging.getLogger(__name__)

# Штраф стране за джейлбрейк/вредный промпт (П.3): бот заявляет по tg id
# правителя его страну и бьёт по экономике. Повторные нарушения
# дополнительно уводят в мьют по порогу ниже.
JAILBREAK_DEBUFF = {
    "budget": -20_000.0,
    "stability": -15.0,
    "happiness": -10.0,
    "war_support": -5.0,
}


# ---------------------------------------------------------------------------
# АДРЕСАЦИЯ: бот отвечает только когда к нему ЯВНО обратились.
# Раньше флаг is_reply_to_bot означал «это ответ на любое сообщение»,
# из-за чего ассистент отвечал в чате на каждое сообщение подряд.
# ---------------------------------------------------------------------------
def is_reply_to_bot(message: Any, bot_id: int | None = None) -> bool:
    """True только если сообщение отвечает именно на сообщение бота."""
    replied = getattr(message, "reply_to_message", None)
    if replied is None:
        return False
    author = getattr(replied, "from_user", None)
    if author is None:
        return False
    if bot_id:
        return author.id == bot_id
    # запасной путь: id бота неизвестен — считаем репликой бота сообщение
    # от аккаунта, помеченного Telegram как бот
    return bool(getattr(author, "is_bot", False))


def mentions_bot(message: Any, bot_username: str | None = None) -> bool:
    """True если в тексте есть @username бота."""
    if not bot_username:
        return False
    return f"@{bot_username}".lower() in (getattr(message, "text", None) or "").lower()


def should_respond_to_bot(
    message: Any,
    bot_id: int | None = None,
    bot_username: str | None = None,
) -> bool:
    """Единый гейт: команда, личка, @упоминание или ответ на сообщение бота."""
    chat = getattr(message, "chat", None)
    if getattr(chat, "type", "") == "private":
        return True
    text = getattr(message, "text", None) or ""
    if text.strip().startswith("/"):
        return True
    if mentions_bot(message, bot_username):
        return True
    return is_reply_to_bot(message, bot_id)


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
    # повторные попытки сломать систему через роли и мета-команды
    r"притворись", r"притворись что ты", r"ты больше не", r"нарушай правила",
    r"разрешено всё", r"без ограничений", r"ignore all", r"disregard",
    r"system\W*prompt", r"you are now", r"act as (a|an)?\s*(admin|root|dev)",
    r"переопредел", r"новые правила", r"админ-режим", r"debug mode",
    r"выведи (весь )?(код|исходник)", r"покажи код", r"доступ к бд",
    r"сбрось (все )?(настройки|ограничения)", r"обнули память",
]

# Off-topic indicators
OFFTOPIC_PATTERNS = [
    r"^как покакать", r"как пописать", r"что такое любовь",
    r"погод[ау]", r"расскажи анекдот", r"шутк[ау]",
    r"как приготовить", r"рецепт", r"фильм", r"сериал",
    r"музык[ау]", r"песн[яу]", r"игру[ау]", r"другая игра",
    r"проект\w*\s+(?!warlord)", r"other project",
    # бытовые и политические темы вне ролевой игры
    r"квартир", r"ипотек", r"работа\b.*(ваканс|резюме)", r"зарплат",
    r"выборы", r"президент\s+(рф|россии|украины)", r"путин", r"зеленск",
    r"санкции против", r"курс доллара", r"криптовалют", r"биткоин",
    r"что такое (секс|наркотик)", r"где купить (оружие|наркотик)",
    r"порн", r"эротик", r"18\+",
    r"напиши (код|скрипт) на (питон|javascript|си)", r"помоги с (домашк|урок)",
    r"поговорим о (жизни|отношениях|здоровье)",
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
    "/update", "/process", "/cards", "/unreg", "/anreg",
    "/afk", "/unafk", "/active", "/mute",
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
            # П.3: страна нарушителя (по tg id правителя) получает дебафф.
            # Сообщение в бот так и не доходит до AI — токены не тратятся.
            try:
                country = await self.db.get_country_by_ruler(user_id)
                if country:
                    await self.db.apply_country_debuff(
                        int(country["id"]), **JAILBREAK_DEBUFF)
                    await self._log_security(
                        user_id, username, text,
                        f"jailbreak_debuff:{country.get('name', '')[:40]}")
            except Exception as exc:  # noqa: BLE001
                log.warning("дебафф за джейлбрейк не применён к %s: %s",
                            user_id, exc)
            # Раньше здесь был вызов db.get_security_count(), которого в БД нет:
            # первое же jailbreak-сообщение роняло проверку с AttributeError.
            # Считаем нарушения конкретного пользователя.
            count = await self.db.count_user_security(user_id, minutes=60)
            if count >= SECURITY_MUTE_THRESHOLD:
                await self.mute_user(
                    user_id, duration_hours=MUTE_DURATION_HOURS)
            return SecurityResult(
                allowed=False,
                reason="jailbreak",
                reply_text="Я не могу обработать этот запрос. Задай вопрос по правилам WARLORD RP."
            )

        # 6. Off-topic check
        if _OFFTOPIC_RE.search(text):
            if not _WARLORD_RE.search(text):
                await self._log_security(user_id, username, text, "offtopic")
                count = await self.db.count_user_security(user_id, minutes=60)
                if count >= OFFTOPIC_MUTE_THRESHOLD:
                    await self.mute_user(
                        user_id,
                        duration_hours=OFFTOPIC_MUTE_DURATION_MINUTES / 60.0,
                    )
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

    async def mute_user(self, user_id: int, duration_hours: float = 1) -> None:
        """Заглушить пользователя. Сохраняем в БД, иначе после перезапуска
        бота мьют слетает и нарушитель снова проходит."""
        until = datetime.now(timezone.utc) + timedelta(hours=duration_hours)
        self._muted[user_id] = until
        try:
            await self.db.mute_user(user_id, until.isoformat(timespec="seconds"))
        except Exception as exc:  # noqa: BLE001
            log.warning("не удалось сохранить мьют %s: %s", user_id, exc)

    async def unmute_user(self, user_id: int) -> bool:
        existed = self._muted.pop(user_id, None) is not None
        try:
            existed = await self.db.unmute_user(user_id) or existed
        except Exception as exc:  # noqa: BLE001
            log.warning("не удалось снять мьют %s: %s", user_id, exc)
        return existed

    async def is_muted(self, user_id: int) -> bool:
        if user_id in self._muted:
            if datetime.now(timezone.utc) < self._muted[user_id]:
                return True
            del self._muted[user_id]
        # подхватываем мьют, поставленный до перезапуска
        try:
            if await self.db.is_user_muted(user_id):
                return True
        except Exception:  # noqa: BLE001
            pass
        return False