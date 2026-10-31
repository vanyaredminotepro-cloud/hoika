"""
Card publishing logic for WARLORD RP.

Вёрстка карточек живёт в card_template.py (разделы 2 и 3 ТЗ), этот модуль
отвечает за публикацию, применение событий и защитные ограничения на числа.

Правило модуля: LLM не может назначить цифру. Он может только попросить об
изменении, а apply_country_effects ограничивает размер изменения и проверяет
результат. Прямое присваивание числовых полей из ответа ИИ игнорируется.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramRetryAfter
from aiogram.types import BufferedInputFile

from . import card_template
from .config import config

log = logging.getLogger(__name__)

# ---------- Статусы стран (раздел 8 ТЗ) ----------
FROZEN_STATUSES = getattr(config, "FROZEN_STATUSES", ("inactive", "afk", "neutral"))
UNPUBLISHABLE_STATUSES = getattr(config, "UNPUBLISHABLE_STATUSES",
                                 ("neutral", "data_error"))
# Статусы с баннером: карточка публикуется даже при малом населении.
STATUS_BANNERED = getattr(config, "STATUS_BANNERED",
                          ("not_accepted", "not_registered", "unreg", "anreg",
                           "inactive", "afk"))

_STATUS_RU = {
    "active": "активна",
    "inactive": "мало новостей",
    "afk": "неактивна (AFK)",
    "neutral": "нейтральна, новостей нет",
    "anreg": "анрегнута",
    "unreg": "анрегнута",
    "not_registered": "не зарегистрирована",
    "data_error": "ошибка данных, нужна анкета",
}
_STATUS_BANNERS = {
    "inactive": "🧊 СТРАНА ЗАМОРОЖЕНА: мало новостей, показатели не растут",
    "afk": "🧊 СТРАНА ЗАМОРОЖЕНА: нет новостей больше 7 дней",
    "neutral": "🧊 СТРАНА ЗАМОРОЖЕНА: нет новостей 2 часа",
    "anreg": "⛔ СТРАНА АНРЕГНУТА",
    "unreg": "⛔ СТРАНА АНРЕГНУТА",
    "data_error": "⚠️ ДАННЫЕ СТРАНЫ ПОВРЕЖДЕНЫ: нужно заново заполнить анкету",
}
STATUS_BANNERS = _STATUS_BANNERS


class CardNotPublishable(Exception):
    """Карточку страны нельзя публиковать (статус или битое население)."""


def _status_ru(status: str) -> str:
    return _STATUS_RU.get(status, status)


def is_publishable(country: dict[str, Any]) -> bool:
    """Можно ли публиковать карточку страны (п.4/п.5).

    Нельзя только для neutral/data_error. Статусы с баннером
    (anreg/unreg/not_accepted/not_registered/inactive/afk) публикуются даже
    при населении меньше MIN_POPULATION — карточка несёт баннер статуса,
    а не «мусор».
    """
    status = str(country.get("status") or "active")
    if status in UNPUBLISHABLE_STATUSES:
        return False
    if status in STATUS_BANNERED:
        return True
    try:
        pop = int(float(country.get("population") or 0))
    except (TypeError, ValueError):
        pop = 0
    if pop < int(getattr(config, "MIN_POPULATION", 60)):
        return False
    return True


# ---------- Форматирование чисел для карточки ----------

def _num(v: Any) -> str:
    """Число с разделителем тысяч. None/мусор -> «—», а не «None»."""
    if v is None or isinstance(v, bool):
        return "—"
    if isinstance(v, (int, float)):
        n = float(v)
    else:
        text = str(v).strip().replace(" ", "").replace("%", "").replace(",", ".")
        try:
            n = float(text)
        except ValueError:
            return str(v) if str(v).strip() else "—"
    if abs(n - round(n)) < 1e-9:
        return f"{int(round(n)):,}".replace(",", " ")
    return f"{n:,.1f}".replace(",", " ").replace(".", ",")


def _delta(prev: Any, cur: Any) -> str:
    """Скобка динамики для списка изменений: «(+10)», «(-150)», «»."""
    try:
        old_n = float(prev or 0)
        new_n = float(cur or 0)
    except (TypeError, ValueError):
        return ""
    diff = new_n - old_n
    if abs(diff) < 1e-9:
        return ""
    sign = "+" if diff > 0 else "-"
    amount = abs(diff)
    if abs(amount - round(amount)) < 1e-9:
        return f"({sign}{int(round(amount)):,})".replace(",", " ")
    return f"({sign}{amount:.1f})".replace(".", ",")


def _pct(v: Any) -> str:
    """Проценты для старых строк карточки: «70%», мусор -> «—»."""
    if v is None or isinstance(v, bool):
        return "—"
    try:
        n = float(str(v).strip().replace("%", "").replace(",", "."))
    except (TypeError, ValueError):
        return "—"
    if abs(n - round(n)) < 1e-9:
        return f"{int(round(n))}%"
    return f"{n:.1f}".replace(".", ",") + "%"


def _as_item(value: Any) -> dict[str, Any]:
    """Привести элемент списка к dict.

    ИИ и игроки пишут списки по-разному: "Фабрика", "Фабрика: 2",
    {"name": "Фабрика", "value": 2}, '{"name": "Низкий"}'. Раньше такое
    попадало в карточку как есть, и игроки видели в списке городов сырой
    JSON - поэтому разбираем всё.
    """
    if isinstance(value, dict):
        return value
    text = str(value or "").strip()
    if not text:
        return {}
    if text.startswith("{"):
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                return parsed
        except (ValueError, TypeError):
            pass
    name, _, tail = text.partition(":")
    name = name.strip()
    count = 0
    tail = tail.strip()
    if tail:
        digits = "".join(ch for ch in tail if ch.isdigit())
        count = int(digits) if digits else 0
    return {"name": name, "count": count} if name else {}


def _items(value: Any) -> list[dict[str, Any]]:
    """Список элементов -> список dict'ов. Строка -> один элемент."""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [d for d in (_as_item(v) for v in value) if d]
    return [d for d in (_as_item(value),) if d]


def _list_names(items: Any, limit: int = 8, join: str = ", ") -> str:
    """Названия через запятую. Пусто -> «Нет»."""
    names: list[str] = []
    for item in _items(items):
        name = str(item.get("name") or item.get("title") or "").strip()
        if not name:
            continue
        count = item.get("count", item.get("value"))
        try:
            count_n = int(count) if count not in (None, "") else None
        except (TypeError, ValueError):
            count_n = None
        name = card_template.esc(name)
        if count_n:
            names.append(f"{name} ({count_n})")
        else:
            names.append(name)
    if not names:
        return "Нет"
    if len(names) <= limit:
        return join.join(names)
    return join.join(names[:limit]) + f" и ещё {len(names) - limit}"


def _format_settlements(settlements: Any) -> str:
    return _list_names(settlements)


def _format_industry(industry: Any) -> str:
    return _list_names(industry)


def _format_resources(resources: Any) -> str:
    return _list_names(resources)


def _format_research(research: Any) -> str:
    parts: list[str] = []
    for item in _items(research):
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        hours = item.get("time_left_hours", item.get("hours"))
        try:
            hours_n = int(hours) if hours not in (None, "") else None
        except (TypeError, ValueError):
            hours_n = None
        parts.append(f"{name} ({hours_n} ч)" if hours_n else name)
    return ", ".join(parts) if parts else "Нет"


def _format_equipment(eq: Any) -> str:
    return _list_names(eq)


def _format_contracts(contracts: Any) -> str:
    return _list_names(contracts, limit=5)


def _format_wars(wars: Any) -> str:
    return _list_names(wars, limit=5)


# ---------- Карточка страны (разделы 2 и 3) ----------

def build_country_card(
    country: dict[str, Any],
    summary: str = "",
    notifications: Any = None,
    *,
    changes: list[dict[str, Any]] | None = None,
    days_ago: float | None = None,
    updated_at: str | None = None,
    factions: list[dict[str, Any]] | None = None,
    prev_country: str | None = None,
    next_country: str | None = None,
) -> str:
    """Собрать карточку страны по разделу 2 ТЗ.

    Вся вёрстка живёт в card_template.py: этот модуль отвечает за публикацию
    и применение событий, а не за текст. Держать тут вторую копию вёрстки
    нельзя - они расходятся, и правки правил ТЗ applied только к одной копии.
    """
    return card_template.build_card(
        country,
        changes=changes,
        summary=summary,
        notifications=notifications,
        days_ago=days_ago,
        updated_at=updated_at,
        factions=factions if factions is not None else country.get("factions") or [],
        prev_country=prev_country,
        next_country=next_country,
    )


# ---------- Карточка группировки ----------
def build_faction_card(faction: dict[str, Any], summary: str = "") -> str:
    """Build faction card text from DB data (HTML for Telegram)."""
    esc = card_template.esc
    prefix = str(faction.get("prefix") or "").strip()
    full = faction.get("full_name") or "—"
    # prefix уже встроен в full_name (например «ЧВК "Waffen"» имеет prefix «ЧВК») —
    # не дублируем его. Мусорные префиксы («—», «–») игнорируем совсем.
    title = full
    if prefix and prefix not in ("—", "–", "-"):
        norm = re.sub(r"[\s«»\"'“”`—–-]+", " ", full).strip().lower()
        if not norm.startswith(prefix.lower()):
            title = f"{prefix} {full}".strip()
    title = title.strip() or "—"
    commander = faction.get("commander") or "—"
    depends = faction.get("depends_on_country") or "независимая"
    base = faction.get("base_country") or "—"
    relation = faction.get("relation_to_country") or "neutral"
    strength = _num(faction.get("strength"))
    equipment = _format_equipment(faction.get("equipment_json", []))
    budget = _num(faction.get("budget"))
    tasks = faction.get("tasks") or "—"
    contracts = _format_contracts(faction.get("contracts_json", []))
    wars = _format_wars(faction.get("wars_json", []))
    rep = _pct(faction.get("reputation"))

    lines: list[str] = []
    lines.append(f"<b>ГРУППИРОВКА: {esc(title)}</b>")
    lines.append(f"<i>Командир:</i> {esc(commander)}")
    lines.append(f"<i>Зависит от:</i> {esc(depends)}")
    lines.append(f"<i>Базирование:</i> {esc(base)}")
    lines.append(f"<i>Отношение к стране:</i> {esc(relation)}")
    lines.append("")
    lines.append(f"Численность: {strength}")
    lines.append(f"Техника: {equipment}")
    lines.append(f"Бюджет: {budget} вирт-руб.")
    lines.append(f"Задачи: {esc(tasks)}")
    lines.append("")
    lines.append("<b>КОНТРАКТЫ</b>")
    lines.append(contracts)
    lines.append("")
    lines.append("<b>УЧАСТИЕ В ВОЙНАХ</b>")
    lines.append(wars)
    lines.append("")
    lines.append(f"Репутация: {rep}/100")
    if summary:
        lines.append("")
        lines.append("<b>ИСТОРИЯ</b>")
        lines.append(f"<blockquote>{esc(summary)}</blockquote>")
    return "\n".join(lines)


# ---------- Публикация ----------
# Telegram allows ~20 msgs/min per chat. Publishing 50+ cards in a tight
# loop trips flood control, so every send/edit is throttled and waits out
# any flood wait instead of firing another request.
_PUBLISH_MIN_INTERVAL = 3.0
_last_publish_at: float = 0.0

# Telegram ограничивает подпись под фото 1024 символами, а обычное сообщение —
# 4096. Карточки бывают длиннее подписи, поэтому такие публикуются текстом без
# флага (иначе send_photo падает с «message caption is too long»).
_CAPTION_LIMIT = 1024
_TEXT_LIMIT = 4096


async def _throttle() -> None:
    """Keep card publishing under Telegram's per-chat rate limit."""
    global _last_publish_at
    loop = asyncio.get_running_loop()
    wait = _PUBLISH_MIN_INTERVAL - (loop.time() - _last_publish_at)
    if wait > 0:
        await asyncio.sleep(wait)
    _last_publish_at = loop.time()


async def _call(fn, **kwargs):
    """Invoke a bot API method, honoring flood-wait responses."""
    for _ in range(4):
        try:
            return await fn(**kwargs)
        except TelegramRetryAfter as e:
            delay = getattr(e, "retry_after", 5) or 5
            log.warning("Flood control: sleeping %ss", delay)
            await asyncio.sleep(delay + 0.5)
    raise RuntimeError("Flood control retry limit exceeded")


async def publish_or_edit(
    bot: Bot,
    chat_id: int,
    thread_id: int,
    text: str,
    message_id: int | None,
    photo: BufferedInputFile | None = None,
    caption: str | None = None,
    reply_markup: Any | None = None,
) -> int:
    """Publish new message or edit existing one. Returns message_id.

    text   — тело карточки; photo — file_id флага/эмблемы; caption — подпись
    под фото (для карточек с флагом совпадает с text).
    reply_markup — inline-клавиатура (навигация и т.п.).
    """
    # Safety net: forum threads are small positive ints, chat ids are negative.
    # If a chat id ever lands in thread_id, Telegram silently posts to the
    # general topic and spams the chat — that must never happen.
    if not isinstance(thread_id, int) or thread_id <= 1:
        raise ValueError(
            f"refusing to publish without a forum topic: thread_id={thread_id!r}"
        )
    body = text or ""
    cap = caption if caption is not None else body
    # Подпись под фото не может быть длиннее 1024 символов. Если карточка
    # длиннее — выкладываем её текстом (лимит 4096): так текст сохраняется,
    # а не падает send_photo с «message caption is too long».
    if photo and len(cap) > _CAPTION_LIMIT:
        photo = None
    if len(body) > _TEXT_LIMIT:
        body = body[:_TEXT_LIMIT - 1].rstrip() + "…"

    if message_id:
        await _throttle()
        try:
            if photo:
                await _call(
                    bot.edit_message_caption,
                    chat_id=chat_id,
                    message_id=message_id,
                    caption=cap,
                    parse_mode="HTML",
                    reply_markup=reply_markup,
                )
            else:
                await _call(
                    bot.edit_message_text,
                    text=body,
                    chat_id=chat_id,
                    message_id=message_id,
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                    reply_markup=reply_markup,
                )
            return message_id
        except TelegramRetryAfter:
            # Rate limited: back off instead of immediately re-sending
            raise
        except Exception as e:
            log.warning("Edit failed for msg %s: %s. Re-sending.", message_id, e)
            # Fall through to send new

    await _throttle()
    if photo:
        msg = await _call(
            bot.send_photo,
            chat_id=chat_id,
            photo=photo,
            caption=cap,
            message_thread_id=thread_id,
            parse_mode="HTML",
            reply_markup=reply_markup,
        )
    else:
        msg = await _call(
            bot.send_message,
            chat_id=chat_id,
            text=body,
            message_thread_id=thread_id,
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=reply_markup,
        )
    # Перепечатали поверх битого/устаревшего сообщения — старую карточку
    # убираем, чтобы в теме не копились дубликаты (в т.ч. когда карточка
    # меняет тип: фото <-> текст).
    if message_id and msg.message_id != message_id:
        try:
            await _call(bot.delete_message, chat_id=chat_id, message_id=message_id)
        except Exception:  # noqa: BLE001 - удаление не критично
            pass
    return msg.message_id

async def publish_country_card(
    bot: Bot,
    country: dict[str, Any],
    thread_id: int,
    message_id: int | None,
    summary: str = "",
    notifications: Any = None,
    *,
    changes: list[dict[str, Any]] | None = None,
    days_ago: float | None = None,
    updated_at: str | None = None,
    prev_country: str | None = None,
    next_country: str | None = None,
    reply_markup: Any | None = None,
) -> int:
    # Публиковать нельзя: neutral (нет новостей 2ч), анрег, не зарегистрирована,
    # data_error и страны с населением < MIN_POPULATION (п.4, п.5 БАГ 1).
    # Если карточка уже была опубликована - обновляем её текст в БД не трогая,
    # но в Telegram ничего не шлём, чтобы не spamить канал мёртвыми странами.
    if not is_publishable(country):
        log.info("карточка %s не публикуется: статус=%s население=%s",
                 country.get("name"), country.get("status"),
                 country.get("population"))
        raise CardNotPublishable(str(country.get("status") or ""))

    text = build_country_card(
        country, summary, notifications,
        changes=changes, days_ago=days_ago, updated_at=updated_at,
        prev_country=prev_country, next_country=next_country,
    )
    photo = None
    caption = None
    if country.get("flag_file_id"):
        # flag_file_id is Telegram file_id string; подпись под фото = текст карточки
        photo = country["flag_file_id"]
        caption = text
    return await publish_or_edit(
        bot, config.target_chat_id, thread_id,
        text, message_id, photo=photo, caption=caption,
        reply_markup=reply_markup,
    )

async def publish_faction_card(
    bot: Bot,
    faction: dict[str, Any],
    thread_id: int,
    message_id: int | None,
    summary: str = ""
) -> int:
    text = build_faction_card(faction, summary)
    photo = None
    caption = None
    if faction.get("emblem_file_id"):
        photo = faction["emblem_file_id"]
        caption = text
    return await publish_or_edit(
        bot, config.target_chat_id, thread_id,
        text, message_id, photo=photo, caption=caption
    )


# ---------- Ограничения для ИИ (раздел 11) ----------

def apply_country_changes(country: dict[str, Any], changes: dict[str, Any]) -> dict[str, Any]:
    """Apply AI-returned field changes onto a country card (returns a copy).

    Fields listed in AI_ONLY_FIELDS are ignored here: they may only move via
    `effects` (clamped deltas) or through the bot's own formulas, otherwise the
    model can rewrite the entire economy from one post.
    """
    updated = dict(country)
    for key, value in (changes or {}).items():
        if key in AI_ONLY_FIELDS:
            continue
        if key == "industry_json":
            updated[key] = _items(value)
            continue
        updated[key] = value
    return updated


AI_ONLY_FIELDS = frozenset({
    "population", "population_capacity", "budget",
    "army_infantry", "army_light_vehicles", "army_heavy_wheeled",
    "army_artillery", "army_drones", "army_navy",
    "stability", "war_support", "happiness",
    "income_daily", "expenses_daily", "ojat", "tx999",
})

# Абсолютные потолки значений: даже через effects нельзя выйти за них.
EFFECT_LIMITS: dict[str, tuple[float, float]] = {
    "population": (0, 5_000_000),
    "population_capacity": (0, 15_000_000),
    "budget": (0, 10_000_000_000),
    "army_infantry": (0, 1_000_000),
    "army_light_vehicles": (0, 100_000),
    "army_heavy_wheeled": (0, 100_000),
    "army_artillery": (0, 50_000),
    "army_drones": (0, 50_000),
    "army_navy": (0, 10_000),
    "stability": (0, 100),
    "war_support": (0, 100),
    "happiness": (0, 100),
}

# Насколько одно событие может изменить показатель (раздел 11):
# живой пост даёт ощутимый, но не взрывной эффект.
EFFECT_CAPS: dict[str, float] = {
    "population": 0.15,      # +15% за событие
    "population_capacity": 0.15,
    "budget": 0.50,          # +50% за событие
    "army_infantry": 0.10,   # +10%: армия растёт только через мобилизацию
    "army_light_vehicles": 0.20,
    "army_heavy_wheeled": 0.20,
    "army_artillery": 0.20,
    "army_drones": 0.20,
    "army_navy": 0.20,
}

# Поля, где ноль допустим (страна может разоружиться).
ZERO_TARGET_KEYS = (
    "population", "budget", "army_infantry", "army_light_vehicles",
    "army_heavy_wheeled", "army_artillery", "army_drones", "army_navy",
)

# Событие, которое ничего не меняет, - это не событие.
MIN_EFFECT_SIZE = {
    "population": 1,
    "budget": 1.0,
    "army_infantry": 1,
    "army_light_vehicles": 1,
    "army_heavy_wheeled": 1,
    "army_artillery": 1,
    "army_drones": 1,
    "army_navy": 1,
    "stability": 1,
    "war_support": 1,
    "happiness": 1,
}


def _to_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def apply_country_effects(country: dict[str, Any], effects: dict[str, Any]) -> dict[str, Any]:
    """Применить числовые эффекты события с жёсткими ограничениями.

    Эффекты - это дельты («+20 солдат»), а не готовые значения. Каждая дельта
    ограничена долей от текущего значения, итог зажат в EFFECT_LIMITS. Это
    единственное место, где ИИ двигает цифры, поэтому проверки живут здесь.
    """
    out = dict(country)
    for key, raw_delta in (effects or {}).items():
        if key not in EFFECT_LIMITS:
            continue  # неизвестный ключ - тихо игнорируем
        try:
            delta = float(raw_delta)
        except (TypeError, ValueError):
            continue
        if abs(delta) < 1e-9:
            continue
        current = _to_float(out.get(key))

        cap_share = EFFECT_CAPS.get(key)
        if cap_share is not None:
            # Убывание не ограничиваем: потери бывают любые.
            if delta > 0:
                cap = abs(current) * cap_share
                if key == "budget" and cap < 1.0:
                    cap = 1.0
                if cap > 0:
                    delta = min(delta, cap)

        new_value = current + delta
        low, high = EFFECT_LIMITS[key]
        new_value = max(low, min(high, new_value))

        if key == "population":
            new_value = int(round(new_value))
        elif key in ZERO_TARGET_KEYS:
            new_value = max(0, int(round(new_value)))
        else:
            new_value = round(new_value, 2)
        out[key] = new_value
    return out


def apply_faction_changes(faction: dict[str, Any], changes: dict[str, Any]) -> dict[str, Any]:
    """Apply AI-returned changes to a faction, ignoring numeric abuse."""
    out = dict(faction)
    for key, value in (changes or {}).items():
        if key in ("strength", "budget"):
            try:
                out[key] = max(0.0, float(value))
            except (TypeError, ValueError):
                continue
            continue
        out[key] = value
    return out


# ---------- Здания ----------
BUILDING_COSTS = {
    "factory": 15000,
    "military_factory": 30000,
    "supply_factory": 20000,
    "shipyard": 25000,
    "port": 12000,
    "research_center": 40000,
    "radar": 18000,
    "military_base": 50000,
    "checkpoint": 5000,
    "tower": 7000,
    "bunker": 9000,
    "polygon": 20000,
    "hospital": 16000,
    "school": 14000,
    "village": 3000,
    "settlement": 5000,
    "town": 8000,
    "urban": 12000,
    "city": 20000,
}
BUILDING_TIMES = {
    "factory": 24,
    "military_factory": 48,
    "supply_factory": 36,
    "shipyard": 40,
    "port": 24,
    "research_center": 72,
    "radar": 24,
    "military_base": 60,
    "checkpoint": 8,
    "tower": 12,
    "bunker": 16,
    "polygon": 24,
    "hospital": 36,
    "school": 24,
    "village": 12,
    "settlement": 16,
    "town": 24,
    "urban": 36,
    "city": 48,
}
BUILDING_INCOME = {"factory": 500, "port": 300}

# Налоговые уровни приходят и по-русски (так они лежат в БД), и по-английски
# (так их просит ИИ). Раньше принимался только английский, и налог молча
# не менялся.
_TAX_LEVELS = {
    "low": "Низкий",
    "standard": "Стандартный",
    "high": "Высокий",
    "maximum": "Максимальный",
    "низкий": "Низкий",
    "средний": "Стандартный",
    "стандартный": "Стандартный",
    "высокий": "Высокий",
    "максимальный": "Максимальный",
}

# Канонический уровень налога -> русская подпись в карточке.
TAX_NAMES: dict[str, str] = {
    key: name for key, name in _TAX_LEVELS.items() if key.isascii()
}


def normalize_tax_level(level: Any) -> str | None:
    """«Средний» / «standard» / « STANDARD » -> 'standard'. None если неизвестно.

    Единственный источник правды об уровнях налога: им пользуются и карточка,
    и валидатор. Раньше у каждого был свой список, и валидатор отклонял
    русские значения, которые apply_event принимал.
    """
    return _TAX_LEVEL_BY_RU.get(str(level or "").strip().lower())


_TAX_LEVEL_BY_RU = {
    "low": "low",
    "низкий": "low",
    "standard": "standard",
    "средний": "standard",
    "стандартный": "standard",
    "high": "high",
    "высокий": "high",
    "maximum": "maximum",
    "максимальный": "maximum",
}


def _tax_name(level: Any) -> str:
    raw = str(level or "").strip()
    return _TAX_LEVELS.get(raw.lower(), raw)


# ---------- Применение события из ответа ИИ ----------

def apply_event(country: dict[str, Any], event: dict[str, Any], db: Any = None) -> list[str]:
    """Применить одно событие к карточке страны. Возвращает список изменений.

    События берутся из extract_events(): build, population, budget, army,
    stability, tax, research, resource, unreg, rename. Каждое событие
    проверяется: неизвестный тип и нечисловые значения не ломают карточку,
    а просто игнорируются.
    """
    changes: list[str] = []
    if not isinstance(event, dict):
        return changes

    etype = str(event.get("type") or "").strip()
    params = event.get("params") or {}
    if not isinstance(params, dict):
        params = {}

    if etype == "build":
        building = str(params.get("building") or params.get("name") or "").strip()
        try:
            count = int(params.get("count", 1) or 1)
        except (TypeError, ValueError):
            count = 1
        if building and count > 0:
            industry = _items(country.get("industry_json"))
            for item in industry:
                if str(item.get("name") or "").strip().lower() == building.lower():
                    item["count"] = int(item.get("count") or 0) + count
                    break
            else:
                industry.append({"name": building, "count": count})
            country["industry_json"] = industry
            changes.append(f"Построено: {building} x{count}")

    elif etype == "population":
        old = int(_to_float(country.get("population")))
        try:
            delta = int(params.get("delta", 0) or 0)
        except (TypeError, ValueError):
            delta = 0
        country["population"] = max(0, old + delta)
        if delta:
            changes.append(
                f"Население: {_num(old)} → {_num(country['population'])} "
                f"{_delta(old, country['population'])}")

    elif etype == "budget":
        old = _to_float(country.get("budget"))
        try:
            delta = float(params.get("delta", 0) or 0)
        except (TypeError, ValueError):
            delta = 0.0
        country["budget"] = max(0.0, old + delta)
        reason = str(params.get("reason") or "").strip()
        if delta:
            changes.append(
                f"Казна: {_num(old)} → {_num(country['budget'])} "
                f"{_delta(old, country['budget'])}"
                + (f" [{reason}]" if reason else ""))

    elif etype == "army":
        old_inf = country.get("army_infantry", 0)
        inf_delta = int(params.get("infantry", 0) or 0)
        country["army_infantry"] = max(0, int(old_inf or 0) + inf_delta)
        if inf_delta:
            changes.append(f"Пехота: {_num(old_inf)} → {_num(country['army_infantry'])} {_delta(old_inf, country['army_infantry'])}")
        for k in ("light_vehicles", "heavy_wheeled", "artillery", "drones", "navy"):
            if k in params:
                old = country.get(f"army_{k}", 0)
                delta = int(params[k] or 0)
                country[f"army_{k}"] = max(0, int(old or 0) + delta)
                if delta:
                    field = f"army_{k}"
                    new_val = country[field]
                    changes.append(f"{k}: {_num(old)} → {_num(new_val)} {_delta(old, new_val)}")

    elif etype == "stability":
        delta = int(params.get("delta", 0) or 0)
        reason = str(params.get("reason") or "")
        old = _to_float(country.get("stability", 70))
        new = max(0.0, min(100.0, old + delta))
        country["stability"] = new
        if delta:
            changes.append(f"Стабильность: {old:g}% → {new:g}% {_delta(old, new)} [{reason}]")

    elif etype == "tax":
        level = params.get("level")
        name = _tax_name(level)
        if name:
            country["tax_level"] = name
            changes.append(f"Налоговый уровень: {name}")

    elif etype == "research":
        name = str(params.get("name") or "").strip()
        try:
            time_left = int(params.get("time_left_hours", 0) or 0)
        except (TypeError, ValueError):
            time_left = 0
        if name:
            research = _items(country.get("research_json"))
            research.append({
                "name": name,
                "time_left_hours": max(0, time_left),
                "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            })
            country["research_json"] = research
            changes.append(f"Исследование: {name} ({time_left} ч)")

    elif etype == "resource":
        name = str(params.get("name") or "").strip()
        try:
            delta = int(params.get("delta", 0) or 0)
        except (TypeError, ValueError):
            delta = 0
        if name and delta:
            resources = _items(country.get("resources_json"))
            for item in resources:
                if str(item.get("name") or "").strip().lower() == name.lower():
                    old = int(item.get("count") or 0)
                    item["count"] = max(0, old + delta)
                    changes.append(f"Ресурс {name}: {_num(old)} → {_num(item['count'])}")
                    break
            else:
                resources.append({"name": name, "count": max(0, delta)})
                changes.append(f"Ресурс {name}: +{delta}")
            country["resources_json"] = resources

    elif etype == "unreg":
        # Анрег — ручное решение админа (/unreg). Событие от AI статус НЕ меняет.
        log.info("apply_event: unreg-событие проигнорировано (только вручную)")

    elif etype == "rename":
        new_name = str(params.get("new_name") or "").strip()
        if new_name and new_name.lower() != str(country.get("name") or "").lower():
            changes.append(f"Переименование предложено: {new_name}")

    else:
        log.debug("apply_event: неизвестный тип события %r", etype)

    return changes


# ---------- Блок изменений для карточки (раздел 10) ----------

# Поля, которые имеет смысл показывать в блоке изменений. Доход и расход
# сюда не входят: они производные от казны и только шумят в блоке.
CARD_TRACKED_FIELDS: tuple[tuple[str, str], ...] = (
    ("Население", "population"),
    ("Казна", "budget"),
    ("Армия", "army_infantry"),
    ("Лёгкая техника", "army_light_vehicles"),
    ("Тяжёлая колёсная", "army_heavy_wheeled"),
    ("Артиллерия", "army_artillery"),
    ("БПЛА", "army_drones"),
    ("Флот", "army_navy"),
    ("Стабильность", "stability"),
    ("Поддержка войны", "war_support"),
    ("Счастье народа", "happiness"),
)

# Карта техническое_имя -> подпись. Именно в таком направлении: в журнале
# лежат английские названия полей, а в карточке и в отчёте должны быть русские.
_FIELD_LABELS: dict[str, str] = {field: label for label, field in CARD_TRACKED_FIELDS}


def collect_changes_since(
    rows: list[dict[str, Any]], since: str = "",
) -> list[dict[str, Any]]:
    """Собрать блок изменений из журнала changes_log.

    Источник - именно журнал, а не сравнение prev_*/текущее значение: в
    журнале лежит причина и старый post id, и по нему видно, что именно
    изменилось за цикл. rows приходят от db.list_changes() (свежие сверху),
    поэтому останавливаемся на первой записи старше `since`.
    """
    out: list[dict[str, Any]] = []
    for row in rows or []:
        stamp = str(row.get("timestamp") or "")
        if since and stamp and stamp <= since:
            break
        field = str(row.get("field") or "")
        old_value = row.get("old_value")
        new_value = row.get("new_value")
        if str(old_value) == str(new_value):
            continue
        out.append({
            "field": _FIELD_LABELS.get(field, field),
            "old_value": old_value,
            "new_value": new_value,
            "op": "changed",
        })
    out.reverse()
    return out