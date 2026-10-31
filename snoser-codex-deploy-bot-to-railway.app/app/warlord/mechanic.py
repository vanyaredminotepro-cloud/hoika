"""
Module B: Mechanic - scheduler for country/faction card updates.

Reads forum history through the Telethon userbot (bots cannot read history),
attributes messages to countries/factions, asks the AI for card changes and
publishes/updates the cards.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from aiogram import Bot
from aiogram.types import BufferedInputFile
from telethon.tl.functions.messages import GetForumTopicsRequest
from telethon.tl.types import InputChannel, Message

from .config import (
    config, UPDATE_INTERVAL_HOURS, MAX_MESSAGES_PER_TOPIC, MAX_NEWS_CHARS,
    ANKETA_CHAT_ID, ANKETA_TOPIC_ID, CARD_PUBLISH_INTERVAL_HOURS,
    faction_canonical_key, faction_strength_cap,
    FACTION_KNOWN_WARS,
)
from .db import Database
from .ai import AIClient, parse_json_response
from .economy import (
    BASE_INCOME_DAILY, FACTORY_INCOME, PORT_INCOME, TAX_PER_CAPITA,
    ARMY_UPKEEP,
    count_factories, count_ports,
    compute_country_economy, compute_faction_economy, _tax_rate,
    army_expenses, parse_event_date,
)
from .cards import (
    build_country_card, build_faction_card,
    apply_event, apply_country_changes, apply_faction_changes,
    apply_country_effects, AI_ONLY_FIELDS,
    publish_country_card, publish_faction_card, CardNotPublishable,
    collect_changes_since, normalize_tax_level,
)
from .card_template import as_item
from . import activity as _activity
from . import notify as _notify
from . import cleanup as _cleanup
from .validator import validate_country_card, validate_faction_card
from .assistant import Assistant
from .anketa import parse_anketa_template
from .mobilization import enforce_mobilization

log = logging.getLogger(__name__)

# Сколько подряд неудачных AI-вызовов терпим до остановки текущей работы.
# Нужен потому, что провайдер может лежать минутами, а каждый вызов стоит
# AI_CALL_DEADLINE секунд: без автомата цикл встал бы намертво.
AI_FAILURE_BUDGET = int(os.getenv("AI_FAILURE_BUDGET", "3"))

# Как часто пересканировать историю анкет в поисках флагов (полный обход
# истории дорогой: Telegram выдаёт flood wait на медиа).
FLAGS_SCAN_INTERVAL_MINUTES = int(os.getenv("FLAGS_SCAN_INTERVAL_MINUTES", "360"))

# Стартовая промышленность по правилам проекта: 1 военный завод + 2 фабрики.
_DEFAULT_INDUSTRY = [
    {"name": "Военный завод", "type": "military_factory", "value": 1},
    {"name": "Фабрика", "type": "factory", "value": 1},
    {"name": "Фабрика", "type": "factory", "value": 1},
]


def _anketa_get_field(flds: dict, *keys, default=None):
    for k in keys:
        v = flds.get(k)
        if v is not None and v != "":
            return v
    return default

def _anketa_to_int(v, default=0):
    try:
        return int(float(str(v).replace(",", "").replace(" ", "")))
    except Exception:
        return default

def _anketa_to_float(v, default=0.0):
    try:
        return float(str(v).replace(",", "").replace(" ", ""))
    except Exception:
        return default

def _num_sched(value, default=0.0):
    try:
        return float(value)
    except Exception:
        return default

def _anketa_merge(base: dict, parsed: dict) -> dict:
    """Наложить распарсенные поля на существующие, не обнуляя установленное."""
    out = dict(base)
    for k, v in parsed.items():
        if v is None:
            continue
        if isinstance(v, str) and not v.strip():
            continue
        if isinstance(v, (list, dict)) and not v:
            continue
        if isinstance(v, (int, float)) and v == 0:
            continue
        out[k] = v
    return out


def _is_topic_closed(exc: BaseException) -> bool:
    """Telegram отказывает TOPIC_CLOSED / TOPIC_DELETED."""
    text = str(exc).upper()
    return "TOPIC_CLOSED" in text or "TOPIC_DELETED" in text or "TOPIC_NOT_MODIFIED" in text


# Темы, которые Telegram принял закрытыми. Не тянемся
# долбиться в них подтёмных циклов по этой причине.
_CLOSED_TOPICS: dict[int, str] = {}


def _remember_closed_topic(thread_id: int | None, exc: BaseException | None = None) -> None:
    if thread_id:
        _CLOSED_TOPICS[int(thread_id)] = str(exc or "")


def _topic_is_closed(thread_id: int | None) -> bool:
    return bool(thread_id) and int(thread_id) in _CLOSED_TOPICS


# Поля, по которым оценивается «существенное изменение» (раздел 10).
_PREV_TRACKED = (
    "population", "budget", "stability", "war_support", "happiness",
    "army_infantry", "army_light_vehicles", "army_heavy_wheeled",
    "army_artillery", "army_drones", "army_navy",
)


def _prev_snapshot(country: dict[str, Any]) -> dict[str, Any]:
    """Состояние страны на начало цикла — из prev_* полей.

    Экономика уже сдвинула prev_* на текущие значения, поэтому «до» для
    уведомления правителю берём именно оттуда: это ровно то изменение,
    которое видно в скобках карточки. Если prev_* нет (страна только
    создана), возвращаем текущее состояние - тогда «изменения» пустые и
    уведомление не отправится, что правильно.
    """
    old = dict(country)
    for key in _PREV_TRACKED:
        prev = country.get(f"prev_{key}")
        if prev is not None:
            old[key] = prev
    return old


# Слова, которыми пост обязан подтвердить изменение (разделы 4 и 11).
# Событие для валидатора строится из ТЕКСТА новостей, а не из того, что AI
# сам написал в changes: иначе любое выдуманное AI число тут же «подтверждало»
# бы само себя и проверки ничего не ловили.
_VALIDATION_WORDS: dict[str, tuple[str, ...]] = {
    "population": ("населен", "жител", "граждан", "пересел", "эмигр",
                   "беженц", "умер", "погиб", "рожда", "прирост", "отток"),
    "budget": ("доход", "налог", "бюджет", "казна", "расход", "прибыл",
               "долг", "инвест", "финанс", "аукцион", "поставк", "импорт"),
    "army": ("мобилизац", "набор", "призыв", "рекрут", "армия", "войска",
             "солдат", "пехот", "техник", "дрон", "артиллер", "флот",
             "военн", "потер", "демобилиз", "дезерт", "уничтож"),
    "build": ("стро", "постро", "завод", "строительств", "открыт", "введен",
              "введён", "сдач", "реконструкц", "цех", "фабрик", "больниц",
              "школ", "университет", "шахт", "верф", "площадк"),
    "tax": ("налог",),
    "stability": ("стабиль", "восстан", "револ", "протест", "выбор",
                  "парламент", "оппозиц", "забастов", "мир", "договор"),
    "ojat": ("оят", "тх-999", "тх999"),
}

# Поле страны -> (событие валидатора, ключ params для армии).
_VALIDATION_FIELD_MAP: dict[str, tuple[str, str | None]] = {
    "population": ("population", None),
    "budget": ("budget", None),
    "army_infantry": ("army", "infantry"),
    "army_light_vehicles": ("army", "light_vehicles"),
    "army_heavy_wheeled": ("army", "heavy_wheeled"),
    "army_artillery": ("army", "artillery"),
    "army_drones": ("army", "drones"),
    "army_navy": ("army", "navy"),
    "industry_json": ("build", None),
    "buildings": ("build", None),
    "stability": ("stability", None),
    "war_support": ("stability", None),
    "happiness": ("stability", None),
    "tax_level": ("tax", None),
    "ojat": ("ojat", None),
    "tx999": ("ojat", None),
}


def _events_for_validation(
    news_text: str, result: dict[str, Any]
) -> list[dict[str, Any]]:
    """События для validator.py, подтверждённые текстом новостей.

    Поле, о котором пост не упомянул, события не порождает: валидатор тогда
    увидит изменение «без новости» и откатит карточку, как и требует раздел 11.
    """
    blob = f"{news_text or ''}\n{result.get('summary') or ''}".lower()
    changes = result.get("changes") or {}
    effects = result.get("effects") or {}
    events: list[dict[str, Any]] = []
    if not isinstance(changes, dict):
        changes = {}
    if not isinstance(effects, dict):
        effects = {}
    # Числа реально двигаются через effects (apply_country_changes игнорирует
    # защищённые поля в changes), поэтому события собираем из обоих блоков -
    # иначе подтверждённый новостью набор выглядел бы для валидатора как
    # «рост армии без события мобилизации» и откатывался бы.
    candidates: dict[str, Any] = {}
    for field, value in changes.items():
        candidates[field] = value
    for field, value in effects.items():
        candidates[field] = value
    for field, value in candidates.items():
        mapped = _VALIDATION_FIELD_MAP.get(field)
        if mapped is None:
            continue
        event_type, param = mapped
        if not any(word in blob for word in _VALIDATION_WORDS.get(event_type, ())):
            continue
        if isinstance(value, dict):
            delta: Any = value.get("delta", value.get("value", 0))
        else:
            delta = value
        params: dict[str, Any] = {param: delta} if param else {"delta": delta}
        events.append({"type": event_type, "params": params, "text": blob})
    return events


class Mechanic:
    """Module B - Card update scheduler."""

    def __init__(
        self,
        db: Database,
        ai: AIClient,
        assistant: Assistant,
        bot: Bot,
        userbot: Any | None = None,
    ):
        self.db = db
        self.ai = ai
        self.assistant = assistant
        self.bot = bot
        self.userbot = userbot
        self._lock = asyncio.Lock()
        self._running = False
        self._news_topic_ids: list[int] = []
        self._last_scan: dict[str, datetime] = {}
        self._sem = asyncio.Semaphore(2)
        self._seen_this_cycle: set[int] = set()
        self._backfilled: set[str] = set()  # страны, для которых уже искали историю
        self._ai_failure_streak = 0  # подряд идущие отказы AI-провайдера
        self._flags_last_scan: datetime | None = None  # последний скан флагов
        self._scan_budget = 400
        self._news_batch_size = 8
        # Счётчики цикла для отчёта админу (db.log_card_cycle). Раньше таблица
        # card_cycles никогда не заполнялась, и суточный отчёт показывал нули.
        self._cycle_countries = 0
        self._cycle_changes = 0
        self._cycle_rollbacks = 0
        self._cycle_errors = 0
        self._cycle_deleted: list[str] = []

    async def start(self) -> None:
        """Initial setup: discover topics, load anketa, run first economy pass.

        Anketa parsing talks to the AI provider, which can be down for minutes.
        It must never keep the bot from starting, so failures here are logged
        and the topic discovery/economy still run.
        """
        if not self.userbot:
            log.error(
                "Userbot not connected: нельзя читать историю форума. "
                "Проверь TG_API_ID/TG_API_HASH и файл сессии."
            )
        await self._discover_topics()
        try:
            await self._load_anketa()
        except Exception as exc:  # noqa: BLE001
            log.warning("anketa: пропущена на старте (провайдер недоступен): %s", exc)
        await self._reconcile_countries()
        await self._reconcile_factions()
        await self._merge_duplicate_factions()
        await self._apply_faction_known_facts()
        await self._repair_economy_v2()
        await self._run_economy()
        await self.db.set_meta("discovered", "1")

    async def run_cycle(self) -> None:
        """Main update cycle."""
        if self._lock.locked():
            log.info("Cycle already running, skipping")
            return
        async with self._lock:
            self._running = True
            self._ai_failure_streak = 0
            self._cycle_countries = 0
            self._cycle_changes = 0
            self._cycle_rollbacks = 0
            self._cycle_errors = 0
            self._cycle_deleted = []
            log.info("=== Starting update cycle ===")
            try:
                # Восстановление стран/группировок из known_entities + хронология
                await self._reconcile_countries()
                await self._reconcile_factions()
                # Склейка дублирующих карточек группировок (Ратич, Лингваффен,
                # Fauzwaff) + известные участия в войнах
                await self._merge_duplicate_factions()
                await self._apply_faction_known_facts()
                await self._backfill_unprocessed_countries()
                await self._backfill_registration_dates()
                await self._reseed_economy_from_registration()
                await self._repair_economy_v2()

                news_by_country, fresh, faction_news = await self._collect_news()
                if news_by_country:
                    await self._process_countries(news_by_country)
                    await self._process_factions(faction_news)
                # Ответы админа на вопросы без свежих новостей: применяем всегда
                await self._process_pending_answers_no_news()
                # Пересчёт активности сразу после свежих новостей: страна,
                # которая только что отписалась, не должна публиковаться как
                # «заморожена» из-за устаревшего last_post_at.
                try:
                    await _activity.check_activity(self.db)
                except Exception as exc:  # noqa: BLE001
                    log.warning("activity: пересчёт после новостей не удался: %s", exc)
                # Экономика считается ботом поверх любых AI-изменений
                await self._collect_flags()
                await self._run_economy()
                # АФК/анрег — РУЧНОЕ решение (тема объявлений + /afk, /unreg).
                # Авто-анрег по устаревшему last_activity_at убран: он ошибочно
                # выкидывал живые страны (у Линьфы свежий пост, но старая
                # last_activity_at) и удалял их карточки.
                # Единая публикация всех карточек (редко, с защитой от флуда)
                await self.publish_all_cards(force=False)
                await self._ask_admin_questions(fresh)
                log.info("=== Cycle completed ===")
            except Exception as e:
                log.exception("Cycle error: %s", e)
            finally:
                await self._save_cycle_stats()
                self._running = False

    async def _save_cycle_stats(self) -> None:
        """Записать итоги цикла в card_cycles (раздел 10, суточный отчёт).

        Отдельный метод, а не строчка в run_cycle: вызывается из finally, то
        есть даже после исключения - иначе в отчёте были бы сплошные нули.
        """
        if not any((self._cycle_countries, self._cycle_changes,
                    self._cycle_rollbacks, self._cycle_errors,
                    self._cycle_deleted)):
            return
        try:
            await self.db.log_card_cycle(
                self._cycle_countries, self._cycle_changes,
                self._cycle_rollbacks, self._cycle_errors,
                self._cycle_deleted or None,
            )
            log.info(
                "итоги цикла: страны=%d, изменений=%d, откатов=%d, ошибок=%d",
                self._cycle_countries, self._cycle_changes,
                self._cycle_rollbacks, self._cycle_errors,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("итоги цикла не записаны: %s", exc)

    # ================================================================== чтение новостей
    async def _read_topic_messages(
        self,
        chat_id: int,
        thread_id: int,
        limit: int = MAX_MESSAGES_PER_TOPIC,
        offset_id: int | None = None,
        min_date: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """Прочитать сообщения темы через userbot (свежие сверху).

        min_date — отсечь сообщения старше этой даты (включая дату).
        """
        if self.userbot is None:
            return []
        if thread_id is None:
            log.warning("read_topic: thread_id is None, skipping")
            return []
        if not isinstance(chat_id, int):
            log.warning("read_topic: invalid chat_id %r, skipping", chat_id)
            return []

        try:
            from telethon.tl.types import Message

            messages = []
            kwargs: dict[str, Any] = {"limit": limit}
            if isinstance(offset_id, int) and offset_id > 0:
                kwargs["offset_id"] = offset_id
            kwargs["reply_to"] = thread_id

            async for msg in self.userbot.iter_messages(chat_id, **kwargs):
                if not isinstance(msg, Message) or not msg.message:
                    continue
                if len(msg.message) <= 5:
                    continue
                if min_date is not None and msg.date is not None:
                    msg_dt = msg.date
                    if msg_dt.tzinfo is None:
                        msg_dt = msg_dt.replace(tzinfo=timezone.utc)
                    if msg_dt < min_date:
                        continue
                messages.append({
                    "id": msg.id,
                    "date": msg.date.isoformat() if msg.date else "",
                    "text": msg.message,
                    "sender_id": getattr(msg, "sender_id", ""),
                    "chat_id": chat_id,
                    "thread_id": thread_id or 0,
                })
            return messages
        except TypeError as exc:
            log.error(
                "read_topic %s/%s TypeError: %s | chat_id=%r thread_id=%r offset_id=%r",
                chat_id, thread_id, exc, chat_id, thread_id, offset_id,
            )
            return []
        except Exception as exc:  # noqa: BLE001
            log.error("read_topic %s/%s failed: %s", chat_id, thread_id, exc)
            return []

    async def _collect_news(
        self,
    ) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
        """Исследовать новости проекта от начала архива и до конца.

        У каждой новостной темы хранится курсор (meta scan:{chat}:{thread}).
        За цикл читается одна страница от свежих к старым, каждое сообщение
        атрибутируется странам ровно один раз (состояние в researched_news).
        Возвращает (news_by_country, fresh_messages, faction_news).
        """
        if self.userbot is None:
            return {}, [], {}

        chat_id = config.target_chat_id
        self._seen_this_cycle = set()
        countries = await self.db.list_countries()
        alias_map: dict[str, str] = {}
        for c in countries:
            if c.get("status", "active") == "unreg":
                continue
            alias_map[c["name"].lower()] = c["name"]
            for a in c.get("aliases_json") or []:
                alias_map[str(a).strip().lower()] = c["name"]

        factions = await self.db.list_factions()
        faction_alias: dict[str, str] = {}
        for f in factions:
            fn = (f.get("full_name") or "").strip()
            if not fn:
                continue
            faction_alias[fn.lower()] = fn
            prefix = str(f.get("prefix") or "").strip().lower()
            if len(prefix) >= 3:
                faction_alias[prefix] = fn

        news_by_country: dict[str, list[dict[str, Any]]] = {}
        faction_news: dict[str, list[dict[str, Any]]] = {}
        fresh_messages: list[dict[str, Any]] = []
        last_activity: dict[str, str] = {}
        work_budget = self._scan_budget or 400

        topics = await self.db.list_topics()
        news_topic_ids = [
            t["thread_id"] for t in topics
            if t["purpose"] in ("новости", "расширения", "объявления", "анкеты")
            or not t["purpose"]
        ]
        if not news_topic_ids:
            news_topic_ids = list(self._news_topic_ids)

        for thread_id in news_topic_ids:
            if thread_id is None or work_budget <= 0:
                continue
            scan_key = f"scan:{chat_id}:{thread_id}"
            raw = await self.db.get_meta(scan_key)
            try:
                # 0 — backfill ещё не начат, >0 — id, ниже которого читаем
                # историю, <0 — история прочитана до конца.
                cursor = int(raw) if raw not in (None, "") else 0
            except (ValueError, TypeError):
                cursor = 0

            # СВЕЖАЯ страница читается ВСЕГДА. Раньше читали только вниз по
            # истории по курсору, и посты, появившиеся после первого прохода
            # (их id ВЫШЕ курсора), не попадали в атрибуцию: страна писала
            # новости, а статус оставался «afk».
            fresh_page = await self._read_topic_messages(
                chat_id, thread_id, limit=MAX_MESSAGES_PER_TOPIC,
            )
            # Догрузка старой истории — отдельно, чтобы не мешать свежим постам.
            backfill_page: list[dict[str, Any]] = []
            if cursor > 0:
                backfill_page = await self._read_topic_messages(
                    chat_id, thread_id, limit=MAX_MESSAGES_PER_TOPIC,
                    offset_id=cursor,
                )

            if not fresh_page and not backfill_page:
                if cursor >= 0:
                    await self.db.set_meta(scan_key, "-1")
                continue

            by_id: dict[int, dict[str, Any]] = {}
            for m in backfill_page + fresh_page:
                if isinstance(m.get("id"), int):
                    by_id[m["id"]] = m
            messages = sorted(
                by_id.values(), key=lambda m: m.get("id") or 0, reverse=True)

            this_country_ids = [c["id"] for c in countries if c.get("id")]
            already_c = await self.db.researched_message_ids(
                this_country_ids, chat_id, thread_id
            )
            this_faction_ids = [f["id"] for f in factions if f.get("id")]
            already_f = await self.db.faction_researched_message_ids(
                this_faction_ids, chat_id, thread_id
            )

            batch_done = True
            for m in messages:  # от свежих к старым
                if work_budget < 1:
                    batch_done = False
                    break
                text_low = (m.get("text") or "").lower()
                matched = [
                    canonical for key, canonical in alias_map.items()
                    if key and (key in text_low or f"_{key}_" in text_low)
                ]
                matched_f = [
                    fn for key, fn in faction_alias.items()
                    if key and key in text_low
                ]
                for canonical in dict.fromkeys(matched):
                    mdate = m.get("date") or ""
                    if mdate > last_activity.get(canonical, ""):
                        last_activity[canonical] = mdate
                if not matched and not matched_f:
                    await self._note_new(chat_id, thread_id, m, fresh_messages)
                    continue

                needs = [
                    c for c in dict.fromkeys(matched)
                    if m["id"] not in already_c
                    and m not in news_by_country.get(c, [])
                ]
                needs_f = [
                    f for f in dict.fromkeys(matched_f)
                    if m["id"] not in already_f
                    and m not in faction_news.get(f, [])
                ]
                if not needs and not needs_f:
                    continue
                if any(
                    len(news_by_country.get(c, [])) >= self._news_batch_size
                    for c in needs
                ) or any(
                    len(faction_news.get(f, [])) >= self._news_batch_size
                    for f in needs_f
                ) or work_budget < len(needs) + len(needs_f):
                    batch_done = False
                    break
                for canonical in needs:
                    news_by_country.setdefault(canonical, []).append(m)
                    work_budget -= 1
                for full in needs_f:
                    faction_news.setdefault(full, []).append(m)
                    work_budget -= 1
                await self._note_new(chat_id, thread_id, m, fresh_messages)

            if batch_done:
                if backfill_page:
                    # Продолжаем спуск по истории.
                    next_cursor = min(m["id"] for m in backfill_page) - 1
                    await self.db.set_meta(scan_key, str(max(next_cursor, 0)))
                elif cursor == 0 and fresh_page:
                    # Backfill не начат: стартуем сразу под свежей страницей.
                    next_cursor = min(m["id"] for m in fresh_page) - 1
                    await self.db.set_meta(scan_key, str(max(next_cursor, 0)))
                elif cursor > 0:
                    # Старше курсора уже ничего нет — история прочитана.
                    await self.db.set_meta(scan_key, "-1")
            log.info("тема %s: свежих %d, история %d, остаток работ %d",
                     thread_id, len(fresh_page), len(backfill_page), work_budget)

        for canonical, mdate in last_activity.items():
            if mdate:
                await self.db.update_country_last_activity(canonical, mdate)

        pending_total = sum(len(v) for v in news_by_country.values())
        pending_f = sum(len(v) for v in faction_news.values())
        if pending_total or pending_f:
            log.info("исследование: атрибутировано стран=%d группировок=%d",
                     pending_total, pending_f)
        return news_by_country, fresh_messages, faction_news

    async def _note_new(
        self,
        chat_id: int,
        thread_id: int,
        m: dict[str, Any],
        fresh_messages: list[dict[str, Any]],
    ) -> None:
        """Первое появление сообщения за цикл: dedup + очередь детекции."""
        mid = m.get("id")
        if mid in self._seen_this_cycle:
            return
        self._seen_this_cycle.add(mid)
        if not await self.db.has_message_seen(chat_id, thread_id, int(mid)):
            await self.db.mark_message_seen(
                chat_id, thread_id, int(mid), (m.get("text") or "")[:2000]
            )
            fresh_messages.append(m)

    # ================================================================== обработка стран
    async def _process_countries(self, news_by_country: dict[str, list[dict[str, Any]]]) -> None:
        tasks = []
        for name, msgs in news_by_country.items():
            ordered = sorted(msgs, key=lambda m: (m.get("date") or "", m.get("id") or 0))
            tasks.append(self._process_country(name, ordered[: self._news_batch_size]))
        if not tasks:
            log.info("свежих новостей нет, карточки стран не трогаю")
            return
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for r in results:
            if isinstance(r, Exception):
                log.error("ошибка обработки страны: %s", r)

    async def _process_country(self, name: str, msgs: list[dict[str, Any]]) -> None:
        async with self._sem:
            current = await self.db.get_country(name)
            if current is None:
                current = {"name": name}
            news_text = "\n\n".join(
                f"[{m['date']}] {m['text'][:1200]}" for m in msgs[:10]
            )
            # id сообщений - для дедупа мобилизации (БАГ 2)
            news_ids = [m.get("message_id") for m in msgs[:10]]

            async def _mark_done(cid: int | None) -> None:
                if cid and msgs:
                    await self.db.mark_news_researched(cid, msgs)

            if not current.get("industry_json") and not current.get("updated_at"):
                current["industry_json"] = list(_DEFAULT_INDUSTRY)

            # Анрег — ТОЛЬКО вручную (/unreg или заявление в теме АФК).
            # Раньше здесь по regex из новостей ставился статус unreg и
            # удалялась карточка; ложные срабатывания на военных новостях
            # выкидывали живые страны. Оставляем только запись в лог.
            if self._msgs_are_recent(msgs, days=14) and self._detect_unreg(news_text):
                log.info("страна %s: новость похожа на анрег — ждём ручного /unreg", name)

            # Переименование: только по недавним сообщениям
            new_name = ""
            if self._msgs_are_recent(msgs, days=30):
                new_name = self._detect_rename(name, news_text)
            if new_name and new_name.lower() != name.lower():
                all_countries = await self.db.list_countries()
                all_low = {c["name"].strip().lower() for c in all_countries}
                for c in all_countries:
                    for a in (c.get("aliases_json") or []):
                        all_low.add(str(a).strip().lower())
                if new_name.strip().lower() in all_low:
                    log.warning(
                        "игнорирую переименование %s -> %s: имя занято другой страной",
                        name, new_name,
                    )
                else:
                    await self.db.rename_country(name, new_name)
                    await self.db.add_known_entity("country", new_name)
                    log.info("страна переименована: %s -> %s", name, new_name)
                    name = new_name
                    current = await self.db.get_country(name) or {"name": new_name}

            answers, answer_ids = await self._collect_admin_answers(name)
            if answers:
                news_text += "\n\n[ОТВЕТЫ АДМИНИСТРАТОРА]\n" + answers

            if self._ai_failure_streak >= AI_FAILURE_BUDGET:
                log.warning(
                    "AI недоступен (%d ошибок подряд), страна %s пропущена "
                    "до следующего цикла", self._ai_failure_streak, name,
                )
                return
            try:
                raw = await self.ai.build_country_update(name, current, news_text)
                result = self._parse_events_json(raw)
            except Exception as exc:  # noqa: BLE001
                self._ai_failure_streak += 1
                log.error("AI для %s не отвечает: %s: %s", name,
                          type(exc).__name__, exc or "(empty)")
                return
            self._ai_failure_streak = 0

            updated = apply_country_changes(current, result.get("changes") or {})
            effects = result.get("effects") or {}
            if effects:
                updated = apply_country_effects(updated, effects)

            # Применить списки из AI: settlements, industry, research
            lists = result.get("lists") or {}
            if lists:
                if "settlements" in lists:
                    updated["settlements_json"] = self._merge_settlements(
                        current.get("settlements_json") or [], lists["settlements"]
                    )
                if "industry" in lists:
                    updated["industry_json"] = self._merge_industry(
                        current.get("industry_json") or [], lists["industry"]
                    )
                if "research" in lists:
                    updated["research_json"] = self._merge_research(
                        current.get("research_json") or [], lists["research"]
                    )
                if "resources" in lists:
                    updated["resources_json"] = self._merge_resources(
                        current.get("resources_json") or [], lists["resources"]
                    )

            # Сохранить эффекты как economy_events для корректного расчёта бюджета
            # в следующем цикле экономики. Используем дату последнего сообщения как
            # event_date, чтобы разовые изменения не терялись.
            if effects and news_ids:
                last_msg_date = None
                for mid in news_ids:
                    if mid:
                        try:
                            # Получаем дату сообщения из БД или используем текущую
                            # Здесь упрощённо: используем сейчас
                            last_msg_date = datetime.now(timezone.utc).isoformat(timespec="seconds")
                            break
                        except Exception:
                            pass
                event_date = last_msg_date or datetime.now(timezone.utc).isoformat(timespec="seconds")
                for field, delta in effects.items():
                    try:
                        delta_val = float(delta)
                    except (TypeError, ValueError):
                        continue
                    if abs(delta_val) < 1e-9:
                        continue
                    kind = self._effect_to_event_kind(field)
                    if kind:
                        await self.db.add_economy_event(
                            current["id"], event_date, kind, delta_val, rate=False,
                            note=f"AI: {field} {delta_val:+.1f}"
                        )

            # БАГ 2: мобилизация ограничена 30% населения, одно сообщение не
            # должно дважды начислить солдат. Проверяем до записи в БД.
            try:
                mob = await enforce_mobilization(
                    self.db, name, updated,
                    news_text=news_text,
                    ai_flag=bool(result.get("mobilization")),
                    message_ids=[m for m in (news_ids or []) if m],
                )
                if mob.get("clamped") or mob.get("skipped_duplicate"):
                    log.info("карточка %s: мобилизация %s", name, mob)
            except Exception as exc:  # noqa: BLE001
                log.warning("проверка мобилизации %s не удалась: %s", name, exc)

            # Раздел 11: валидация с откатом. Раньше validate_country_card
            # импортировался и НЕ вызывался - все проверки были мёртвым кодом,
            # и AI мог расписать что угодно без последствий.
            report = validate_country_card(
                current, updated, _events_for_validation(news_text, result)
            )
            if not report.valid:
                self._cycle_rollbacks += 1
                self._cycle_errors += len(report.errors)
                log.error(
                    "страна %s: AI-изменения откачены (%s): %s",
                    name, len(report.errors), "; ".join(report.errors[:5]),
                )
                for err in report.errors[:10]:
                    await self.db.log_change(
                        name, "ai_validation", "", "",
                        f"rollback: {err}",
                        message_id=(news_ids or [None])[0] if news_ids else None,
                    )
                reverted = dict(current)
                if result.get("summary"):
                    reverted["summary"] = result["summary"]
                if result.get("notifications"):
                    reverted["notifications_json"] = result["notifications"]
                reverted["updated_at"] = datetime.now(timezone.utc).isoformat(
                    timespec="seconds")
                await self.db.upsert_country(reverted)
                # Новости намеренно НЕ помечаем обработанными: в следующем
                # цикле AI попробует снова, уже с теми же новостями.
                return

            if result.get("summary"):
                updated["summary"] = result["summary"]
            if result.get("notifications"):
                updated["notifications_json"] = result["notifications"]
            updated["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            await self.db.upsert_country(updated)
            self._cycle_countries += 1
            if effects:
                log.info(
                    "карточка %s: применены эффекты новостей %s",
                    name, {k: round(updated.get(k, 0) - current.get(k, 0), 1)
                           for k in effects if isinstance(updated.get(k), (int, float))},
                )
            # Прозрачность: каждое изменение показателей - в changes_log (п.11)
            await self._log_numeric_changes(name, current, updated, news_ids)
            if answer_ids:
                await self.db.mark_questions_applied(answer_ids)
            await _mark_done(updated.get("id") or current.get("id"))
            log.info("карточка %s: AI-изменения сохранены (%d сообщений истории)",
                     name, len(msgs))

            for q in (result.get("questions_for_admin") or [])[:3]:
                await self._schedule_question(f"По {name}: {q}")

    async def _log_numeric_changes(self, name: str, before: dict[str, Any],
                                   after: dict[str, Any],
                                   message_ids: list[Any] | None = None) -> None:
        """Записать в changes_log все изменившиеся числовые показатели.

        Мобилизация уже пишет army_infantry/population - повторно не пишем.
        """
        mid = None
        for m in (message_ids or []):
            if m:
                mid = int(m)
                break
        for field in ("population", "budget", "army_infantry",
                      "army_light_vehicles", "army_heavy_wheeled",
                      "army_artillery", "army_drones", "army_navy",
                      "stability", "war_support", "happiness", "tax_level"):
            old = before.get(field)
            new = after.get(field)
            if not isinstance(old, (int, float)) or not isinstance(new, (int, float)):
                continue
            if abs(float(new) - float(old)) < 1e-9:
                continue
            if field in ("army_infantry", "population") and mid is not None:
                # Эти поля пишет enforce_mobilization, чтобы не было двух записей
                # на одно и то же событие.
                if await self.db.change_applied_for_message(name, mid):
                    continue
            try:
                await self.db.log_change(name, field, str(old), str(new),
                                         "новости", message_id=mid)
                self._cycle_changes += 1
            except Exception as exc:  # noqa: BLE001
                log.debug("changes_log %s/%s: %s", name, field, exc)

    def _parse_events_json(self, raw: str) -> dict[str, Any]:
        data = parse_json_response(raw)
        if not data:
            return {"changes": {}, "summary": "", "questions_for_admin": []}
        return data

    # ------------------------------------------------------------------
    # Вспомогательные методы для объединения списков из AI
    # ------------------------------------------------------------------
    @staticmethod
    def _merge_settlements(old: list[Any], new: list[Any]) -> list[dict[str, Any]]:
        """Объединить старые и новые поселения, избегая дублей по имени."""
        merged = {}
        for item in old:
            d = as_item(item)
            name = str(d.get("name") or "").strip()
            if name:
                merged[name.lower()] = d
        for item in new:
            d = as_item(item)
            name = str(d.get("name") or "").strip()
            if name:
                merged[name.lower()] = d
        return list(merged.values())

    @staticmethod
    def _merge_industry(old: list[Any], new: list[Any]) -> list[dict[str, Any]]:
        """Объединить промышленность, суммируя value/count по name+type."""
        merged = {}
        for item in old:
            d = as_item(item)
            name = str(d.get("name") or d.get("type") or "").strip().lower()
            if not name:
                continue
            key = (name, str(d.get("type") or "").strip().lower())
            val = int(d.get("value") or d.get("count") or 0)
            if key not in merged:
                merged[key] = {"name": d.get("name"), "type": d.get("type"), "value": val}
            else:
                merged[key]["value"] = merged[key].get("value", 0) + val
        for item in new:
            d = as_item(item)
            name = str(d.get("name") or d.get("type") or "").strip().lower()
            if not name:
                continue
            key = (name, str(d.get("type") or "").strip().lower())
            val = int(d.get("value") or d.get("count") or 0)
            if key not in merged:
                merged[key] = {"name": d.get("name"), "type": d.get("type"), "value": val}
            else:
                merged[key]["value"] = merged[key].get("value", 0) + val
        return list(merged.values())

    @staticmethod
    def _merge_research(old: list[Any], new: list[Any]) -> list[dict[str, Any]]:
        """Объединить исследования по имени."""
        merged = {}
        for item in old:
            d = as_item(item)
            name = str(d.get("name") or "").strip()
            if name:
                merged[name.lower()] = d
        for item in new:
            d = as_item(item)
            name = str(d.get("name") or "").strip()
            if name:
                merged[name.lower()] = d
        return list(merged.values())

    @staticmethod
    def _merge_resources(old: list[Any], new: list[Any]) -> list[dict[str, Any]]:
        """Объединить ресурсы по имени."""
        merged = {}
        for item in old:
            d = as_item(item)
            name = str(d.get("name") or "").strip()
            if name:
                merged[name.lower()] = d
        for item in new:
            d = as_item(item)
            name = str(d.get("name") or "").strip()
            if name:
                merged[name.lower()] = d
        return list(merged.values())

    def _effect_to_event_kind(self, field: str) -> str | None:
        """Сопоставить поле эффекта с kind события в economy_events."""
        mapping = {
            "population": "citizens",
            "budget": "budget",
            "army_infantry": "soldiers",
            "army_light_vehicles": "light_vehicles",
            "army_heavy_wheeled": "heavy_wheeled",
            "army_artillery": "artillery",
            "army_drones": "drones",
            "army_navy": "navy",
            "stability": "stability",
            "war_support": "war_support",
            "happiness": "happiness",
        }
        return mapping.get(field)

    @staticmethod
    def _msgs_are_recent(msgs: list[dict[str, Any]], days: int = 14) -> bool:
        """Есть ли среди сообщений свежее `days` дней (по дате)."""
        if not msgs:
            return False
        newest = max((m.get("date") or "" for m in msgs), default="")
        try:
            dt = datetime.fromisoformat(str(newest).replace("Z", "+00:00"))
        except ValueError:
            return False
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) - dt <= timedelta(days=days)

    @staticmethod
    def _detect_unreg(text: str) -> bool:
        """Признаки ЯВНОГО объявления об анреге страны в новостях."""
        low = text.lower()
        if re.search(
            r"\b(?:объявляю|объявляем|объявил[аи]?|заявил[аи]?)\b[^.\n]{0,45}?"
            r"(?:анрег|уход[а-я]*\s+(?:из|с)|выход[а-я]*\s+(?:из|с))",
            low,
        ):
            return True
        if re.search(r"\b(?:я|мы)\b[^.\n]{0,35}?(?:анрег|ухож[уи]\s+в?|выходим|сним(?:аю|аем))", low):
            return True
        if re.search(r"\bснима(?:ю|ем|емся|ются)\b[^.\n]{0,30}?(?:страну|с проекта|из игры|анрег)", low):
            return True
        if re.search(r"\bпокида(?:ю|ем)\b[^.\n]{0,25}?(?:проект|игру|рол|warlord)", low):
            return True
        if re.search(
            r"\b(?:анрегн(?:улся|улась|ули|улят)|ухож[уи]\s+в\s+анрег|уходит\s+в\s+анрег)",
            low,
        ):
            return True
        return False

    async def _collect_admin_answers(
        self, entity_name: str, faction: bool = False
    ) -> tuple[str, list[int]]:
        """Собрать отвеченные админом вопросы по стране/группировке."""
        entity_name = (entity_name or "").strip()
        if not entity_name:
            return "", []
        low = entity_name.lower()
        relevant = []
        for q in await self.db.list_unapplied_answers():
            qtext = q.get("question") or ""
            qlow = qtext.lower()
            if low not in qlow:
                continue
            if "в новостях встречено" in qlow or "существует ли это" in qlow:
                continue
            answer = (q.get("answer") or "").strip()
            if not answer:
                continue
            relevant.append((qtext, answer, int(q["id"])))
        if not relevant:
            return "", []
        block = "\n".join(
            f"- ВОПРОС: {qt}\n  ОТВЕТ АДМИНА: {an}" for qt, an, _ in relevant
        )
        ids = [i for _, _, i in relevant]
        log.info("ответы админа для %s: %d применятся к карточке", entity_name, len(ids))
        return block, ids

    @staticmethod
    def _detect_rename(name: str, text: str) -> str:
        """Определить новое название страны из сообщений о переименовании.

        Строго: принимаем имя только если оно в кавычках ИЛИ стоит рядом с
        маркером переименования, и прежнее имя упомянуто вблизи фразы.
        """
        old = (name or "").strip().lower()
        if len(old) < 3:
            return ""
        low = text.lower()
        if not any(w in low for w in (
            "переименова", "переименовыв", "теперь назыв", "новое назван",
            "отныне", "впредь назыв", "будет назыв"
        )):
            return ""
        if old not in low:
            return ""

        candidates: list[tuple[str, int]] = []

        def _add(cand: str, pos: int) -> None:
            if cand:
                candidates.append((cand, pos))

        qpat = (
            r"(?:переименова|переименовыв|теперь назыв|\sназыв|\sзовётся|"
            r"\sзовёмся|новое назван|отныне|\sименуется|новое имя)"
            r"[^\n«\"]{0,60}?[«\"]([^»\"]{2,60})[»\"]"
        )
        for m in re.finditer(qpat, text, re.IGNORECASE):
            _add(m.group(1), m.start())

        if not candidates:
            upat = (
                r"переименован[аыо]?\s+(?:в|на)\s+"
                r"([А-ЯЁ][а-яёA-Za-z\- ]{1,40})"
            )
            for m in re.finditer(upat, text, re.IGNORECASE):
                _add(m.group(1), m.start())

        if not candidates:
            npat = (
                r"(?:теперь|отныне|впредь)\s+(?:называется|называемся|зовётся|зовёмся)\s+"
                r"([А-ЯЁ][а-яёA-Za-z\- ]{2,40})"
            )
            for m in re.finditer(npat, text, re.IGNORECASE):
                _add(m.group(1), m.start())

        banned = (
            "структур", "преобразу", "будет", "теперь", "является", "станов",
            "переименов", "состоит", "объедин", "называет", "имеет в виду",
            "так ", "это ", "наша ", "наше ", "сюда", "после", "все", "вся",
        )
        for cand, pos in candidates:
            cand = cand.strip().strip("»\"").strip()
            cl = cand.lower()
            if not cand or cl == old:
                continue
            if len(cand) < 2 or len(cand) > 60:
                continue
            if any(b in cl for b in banned):
                continue
            if re.search(r"[.,!?;:]", cand):
                continue
            if cand.startswith(("в ", "на ", "и ", "а ", "это ", "котор", "что ")):
                continue
            window = low[max(0, pos - 300): pos + 300]
            if old not in window:
                continue
            return cand
        return ""

    # ================================================================== обработка группировок
    async def _process_factions(
        self, news_by_faction: dict[str, list[dict[str, Any]]]
    ) -> None:
        factions = await self.db.list_factions()
        if not news_by_faction or not factions:
            return
        by_name = {f["full_name"].strip(): f for f in factions}
        tasks = []
        for full_name, msgs in news_by_faction.items():
            faction = by_name.get(full_name)
            if not faction:
                continue
            ordered = sorted(msgs, key=lambda m: (m.get("date") or "", m.get("id") or 0))
            tasks.append(self._process_faction(faction, ordered[: self._news_batch_size]))
        if not tasks:
            return
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for r in results:
            if isinstance(r, Exception):
                log.error("ошибка обработки группировки: %s", r)

    async def _process_faction(
        self, faction: dict[str, Any], msgs: list[dict[str, Any]]
    ) -> None:
        async with self._sem:
            news_text = "\n\n".join(
                f"[{m['date']}] {m['text'][:1200]}" for m in msgs[:10]
            )
            answers, answer_ids = await self._collect_admin_answers(
                faction.get("full_name") or "", faction=True
            )
            if answers:
                news_text += "\n\n[ОТВЕТЫ АДМИНИСТРАТОРА]\n" + answers
            if self._ai_failure_streak >= AI_FAILURE_BUDGET:
                log.warning(
                    "AI недоступен (%d ошибок подряд), группировка %s пропущена "
                    "до следующего цикла", self._ai_failure_streak,
                    faction.get("full_name") or "",
                )
                return
            try:
                raw = await self.ai.build_faction_update(
                    faction["full_name"], faction, news_text
                )
                result = self._parse_events_json(raw)
            except Exception as exc:  # noqa: BLE001
                self._ai_failure_streak += 1
                log.error("AI для группировки %s: %s", faction.get("full_name"), exc)
                return

            updated = apply_faction_changes(faction, result.get("changes") or {})
            report = validate_faction_card(faction, updated)
            if not report.valid:
                self._cycle_rollbacks += 1
                self._cycle_errors += len(report.errors)
                log.error(
                    "группировка %s: AI-изменения откачены (%s): %s",
                    faction.get("full_name"), len(report.errors),
                    "; ".join(report.errors[:5]),
                )
                for err in report.errors[:10]:
                    await self.db.log_change(
                        faction.get("full_name") or "", "ai_validation", "", "",
                        f"rollback: {err}",
                    )
                return  # новости не помечаем: попробуем в следующем цикле
            updated["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            await self.db.upsert_faction(updated)
            self._cycle_countries += 1
            if answer_ids:
                await self.db.mark_questions_applied(answer_ids)
            if faction.get("id") and msgs:
                await self.db.mark_faction_news_researched(faction["id"], msgs)
            log.info("карточка группировки %s: AI-изменения сохранены (%d сообщений)",
                     faction.get("full_name"), len(msgs))

            for q in (result.get("questions_for_admin") or [])[:3]:
                await self._schedule_question(
                    f"По группировке {faction.get('full_name')}: {q}"
                )

    async def _process_pending_answers_no_news(self) -> None:
        """Применить ответы админа к карточкам без новых новостей."""
        answered = 0
        for country in await self.db.list_countries():
            if country.get("status", "active") == "unreg":
                continue
            _, ids = await self._collect_admin_answers(country.get("name") or "")
            if not ids:
                continue
            try:
                await self._process_country(country.get("name") or "", [])
                answered += 1
            except Exception as exc:  # noqa: BLE001
                log.error("ответы для страны %s: %s", country.get("name"), exc)
        for faction in await self.db.list_factions():
            _, ids = await self._collect_admin_answers(faction.get("full_name") or "")
            if not ids:
                continue
            try:
                await self._process_faction(faction, [])
                answered += 1
            except Exception as exc:  # noqa: BLE001
                log.error("ответы для группировки %s: %s",
                          faction.get("full_name"), exc)
        if answered:
            log.info("ответы админа: применялись к %d карточкам без новостей", answered)

    # ================================================================== Discovery
    async def _discover_topics(self) -> None:
        """Получить темы форума и записать их предназначение."""
        topics = await self.db.list_topics()
        self._news_topic_ids = [
            t["thread_id"] for t in topics
            if t["purpose"] in ("новости", "расширения", "объявления")
        ]
        if self.userbot is None:
            log.warning("discover: нет userbot, темы только из БД: %s",
                        self._news_topic_ids)
            return

        try:
            channel = await self.userbot.get_entity(config.target_chat_id)
        except Exception as exc:  # noqa: BLE001
            log.error("discover: не могу получить группу: %s", exc)
            return

        try:
            input_channel = (
                InputChannel(channel.id, channel.access_hash)
                if channel.access_hash else channel
            )
            offset = 0
            found: list[tuple[int, str]] = []
            while True:
                result = await self.userbot(GetForumTopicsRequest(
                    peer=input_channel,
                    offset_date=None,
                    offset_id=0,
                    offset_topic=offset,
                    limit=100,
                ))
                for topic in result.topics:
                    found.append((topic.id, topic.title or ""))
                offset += len(result.topics)
                if offset >= result.count or not result.topics:
                    break

            for topic_id, title in found:
                purpose = self._classify_topic(title)
                if purpose:
                    await self.db.set_topic(config.target_chat_id, topic_id, purpose)
            log.info("discover: распознано тем: %d", len(await self.db.list_topics()))
        except Exception as exc:  # noqa: BLE001
            log.error("discover: ошибка GetForumTopicsRequest: %s", exc)

    @staticmethod
    def _classify_topic(title: str) -> str:
        """Определить назначение темы по её названию."""
        low = (title or "").strip().lower()
        if not low:
            return ""
        if "новост" in low:
            return "новости"
        if "анкет" in low:
            return "анкеты"
        if "карточ" in low and ("стран" in low or "государ" in low):
            return "карточки стран"
        if "карточ" in low and ("группиров" in low or "фракц" in low or "чвк" in low):
            return "карточки группировок"
        if "расширен" in low:
            return "расширения"
        if "объявлен" in low:
            return "объявления"
        return ""

    async def _load_anketa(self) -> None:
        """Прочитать анкеты стран и группировок из тем анкет.

        Шаблон анкеты фиксированный, поэтому страны разбираются детерминированно
        (app.anketa) — это не зависит от AI-провайдера. Раньше данные приходили
        только от AI, и при его недоступности все страны оставались с
        населением 1 и армией 0.
        """
        if self.userbot is None:
            log.warning("anketa: нет userbot, пропускаю чтение анкет")
            return

        chat_id = config.ANKETA_CHAT_ID
        topic_ids = [config.ANKETA_TOPIC_ID]
        extra = config.ANKETA_TOPIC_ID_2
        if extra and extra != config.ANKETA_TOPIC_ID:
            topic_ids.append(extra)

        messages: list[dict[str, Any]] = []
        # Отсекаем анкеты до 28.09.2026 — старые регистрации не валидны
        anketa_cutoff = datetime(2026, 9, 28, tzinfo=timezone.utc)
        for topic in topic_ids:
            if topic is None:
                continue
            got = await self._read_topic_messages(
                chat_id, topic, limit=MAX_MESSAGES_PER_TOPIC,
                min_date=anketa_cutoff,
            )
            log.info("anketa: тема %s прочитана: %d сообщений", topic, len(got))
            messages.extend(got)
        if not messages:
            log.info("anketa: сообщений не найдено")
            return

        known = {c["name"].lower() for c in await self.db.list_countries()}
        known |= {f["full_name"].lower() for f in await self.db.list_factions()}
        created = 0
        updated = 0
        skipped = 0
        failures = 0
        ai_fail_budget = AI_FAILURE_BUDGET

        for m in sorted(messages, key=lambda x: x.get("id") or 0):
            text = m.get("text") or ""
            if len(text) < 20:
                continue

            # 1) детерминированный разбор шаблона — без AI
            data = parse_anketa_template(text)
            if data:
                name = data["name"]
                sender = m.get("sender_id") or m.get("from_user_id")
                if self._name_already_known(name, known):
                    if await self._fill_country_from_anketa(
                        name, data, sender_id=sender
                    ):
                        updated += 1
                    else:
                        skipped += 1
                    continue
                try:
                    anketa_country = self._anketa_to_country(data)
                    sender = m.get("sender_id") or m.get("from_user_id")
                    if sender:
                        anketa_country["ruler_user_id"] = int(sender)
                    await self.db.upsert_country(anketa_country)
                    known.add(name.lower())
                    created += 1
                    log.info("anketa: создана страна %s (население=%s, армия=%s) — без AI",
                             name, data.get("population"), data.get("army_infantry"))
                except Exception as exc:  # noqa: BLE001
                    log.warning("anketa: сохранение %s не удалось: %s", name, exc)
                continue

            # 2) всё остальное (группировки, вольные анкеты) — через AI
            if not self._looks_like_anketa(text):
                continue
            if failures >= ai_fail_budget:
                log.warning(
                    "anketa: AI недоступен (%d ошибок подряд), остальные анкеты "
                    "пропущены до следующего цикла", failures
                )
                break
            peek = self._peek_anketa_name(text)
            if peek and self._name_already_known(peek, known):
                skipped += 1
                continue
            try:
                data = await self.ai.extract_anketa(text)
            except Exception as exc:  # noqa: BLE001
                failures += 1
                log.warning("anketa: разбор не удался: %s: %s",
                            type(exc).__name__, exc or "(empty)")
                continue
            failures = 0
            if not data:
                continue
            name = str(data.get("name") or data.get("full_name") or "").strip()
            if not name or name.lower() in known:
                skipped += 1
                continue
            is_faction = bool(data.get("full_name")) and not data.get("name")
            try:
                if is_faction:
                    if faction_canonical_key(name) in self._faction_merge_keys():
                        skipped += 1
                        continue
                    await self.db.upsert_faction(data)
                else:
                    anketa_country = data
                    sender = m.get("sender_id") or m.get("from_user_id")
                    if sender:
                        anketa_country = {**data, "ruler_user_id": int(sender)}
                    await self.db.upsert_country(anketa_country)
                known.add(name.lower())
                created += 1
                log.info("anketa: создана %s: %s",
                         "группировка" if is_faction else "страна", name)
            except Exception as exc:  # noqa: BLE001
                log.warning("anketa: сохранение %s не удалось: %s", name, exc)
        log.info("anketa: создано: %d, дополнено: %d, пропущено: %d",
                 created, updated, skipped)

    @staticmethod
    def _anketa_to_country(data: dict[str, Any]) -> dict[str, Any]:
        """Map parsed anketa fields onto a countries row."""
        now_iso = datetime.now(timezone.utc).isoformat()
        country: dict[str, Any] = {
            "name": data["name"],
            "status": "active",
            "stats_source": "anketa",
            "accepted_at": now_iso,
        }
        if data.get("population"):
            country["population"] = int(data["population"])
            country["population_capacity"] = max(
                int(data["population"]), int(country["population"]) * 3, 100)
        if data.get("army_infantry") is not None:
            country["army_infantry"] = int(data["army_infantry"])
        if data.get("ruler"):
            country["ruler"] = data["ruler"]
        if data.get("color"):
            country["color"] = data["color"]
        if data.get("settlements"):
            country["settlements_json"] = data["settlements"]
        if data.get("start_budget"):
            country["start_budget"] = float(data["start_budget"])
            country["budget"] = float(data["start_budget"])
        if not country.get("industry_json"):
            country["industry_json"] = list(_DEFAULT_INDUSTRY)
        return country

    async def _fill_country_from_anketa(
        self, name: str, data: dict[str, Any], sender_id: int | None = None
    ) -> bool:
        """Fill placeholder values of an existing country from its anketa.

        Only touches fields that are still placeholders (population 1, empty
        ruler, no settlements) so real card data is never overwritten.
        """
        country = await self.db.get_country(name)
        if not country:
            for c in await self.db.list_countries():
                if self._name_already_known(name, {c["name"].lower()}):
                    country = c
                    break
        if not country:
            return False

        patch: dict[str, Any] = {"name": country["name"]}
        changed = False
        if int(country.get("population") or 0) <= 1 and data.get("population"):
            patch["population"] = int(data["population"])
            patch["population_capacity"] = max(int(data["population"]) * 3, 100)
            changed = True
        if not int(country.get("army_infantry") or 0) and data.get("army_infantry"):
            patch["army_infantry"] = int(data["army_infantry"])
            changed = True
        if not (country.get("ruler") or "").strip() and data.get("ruler"):
            patch["ruler"] = data["ruler"]
            changed = True
        if not (country.get("ruler_user_id") or 0) and sender_id:
            patch["ruler_user_id"] = int(sender_id)
            changed = True
        if not country.get("settlements_json") and data.get("settlements"):
            patch["settlements_json"] = data["settlements"]
            changed = True
        if not (country.get("color") or "").strip() and data.get("color"):
            patch["color"] = data["color"]
            changed = True
        if not changed:
            return False
        await self.db.upsert_country(patch)
        log.info("anketa: страна %s дополнена из анкеты (%s)",
                 country["name"],
                 ", ".join(f"{k}={v}" for k, v in patch.items() if k != "name")
                 or "без изменений")
        return True

    _ANKETA_NAME_PATTERNS = (
        r"(?:новая\s+)?(?:страна|государство|королевство|республика)\s*[:—\-]?\s*"
        r"[«\"']?([^\n«\"':—]{2,60})[»\"']?",
        r"(?:новая\s+)?(?:группировка|организация|фракция)\s*[:—\-]?\s*"
        r"[«\"']?([^\n«\"':—]{2,60})[»\"']?",
        r"название\s*[:—\-]?\s*[«\"']?([^\n«\"':—]{2,60})[»\"']?",
    )

    @staticmethod
    def _name_already_known(peeked: str, known: set[str]) -> bool:
        """Is the peeked anketa name already a known country/faction?

        Compares loosely: the peeked string is a raw copy from the message and
        may keep quotes or parentheses that the stored canonical name lacks.
        """
        low = peeked.strip().lower()
        if not low:
            return False
        if low in known:
            return True
        return any(low in name or name in low for name in known if len(name) >= 4)

    @classmethod
    def _peek_anketa_name(cls, text: str) -> str:
        """Best-effort country/faction name from an anketa, without calling AI."""
        for pattern in cls._ANKETA_NAME_PATTERNS:
            m = re.search(pattern, text, re.IGNORECASE)
            if not m:
                continue
            candidate = m.group(1).strip(" \t\u00a0«»\"'()-–—.,:;*_[]")
            # отбрасываем мусорные подписи вроде «название» / «анкета»
            if len(candidate) < 2 or candidate.lower() in ("анкета", "регистрация"):
                continue
            return candidate
        return ""

    @staticmethod
    def _looks_like_anketa(text: str) -> bool:
        """Похоже ли сообщение на анкету регистрации."""
        low = text.lower()
        markers = ("анкета", "регистрация", "новое государство", "вступаю в страну",
                   "правитель", "население", "бюджет", "столица")
        hits = sum(1 for m in markers if m in low)
        return hits >= 2

    async def _reconcile_entities(self) -> None:
        """Создать недостающие страны/группировки из known_entities."""
        entities = await self.db.list_entities("country")
        for e in entities:
            if e["status"] == "unknown":
                country = await self.db.get_country(e["name"])
                if not country:
                    await self.db.upsert_country({
                        "name": e["name"],
                        "status": "pending",
                        "industry_json": [],
                    })
                    log.info("Created pending country from entity: %s", e["name"])

    # ================================================================== публикация
    async def _resolve_card_topics(self) -> tuple[int | None, int | None]:
        """Найти темы карточек по назначению (устойчиво к вариантам названий)."""
        topics = await self.db.list_topics()

        async def _pick(keys: list[str], must: list[str]) -> int | None:
            for k in keys:
                t = await self.db.get_topic(k)
                if t:
                    return t["thread_id"]
            for t in topics:
                p = (t.get("purpose") or "").lower()
                if all(m in p for m in must):
                    return t["thread_id"]
            return None

        country_thread = await _pick(["карточки стран", "страны"], ["стран", "карточ"])
        faction_thread = await _pick(
            ["карточки группировок", "группировки"], ["группиров"]
        )
        return country_thread, faction_thread

    async def _publish_cards(self) -> None:
        """Совместимость: команда /cards внутри handle_admin_command.

        Раньше здесь жил второй, отдельный publisher: он печатал карточки без
        блока изменений, без activity и без уведомлений, из-за чего карточки
        после ручной команды выглядели беднее, чем после цикла. Теперь это
        тонкая обёртка над единственным каноническим publish_all_cards().
        """
        await self.publish_all_cards(force=True)

    # ================================================================== вопросы админу
    async def _detect_entities(
        self, fresh_messages: list[dict[str, Any]], known_names: set[str]
    ) -> list[tuple[str, str]]:
        """AI-детекция новых сущностей в свежих новостях."""
        texts = [m["text"] for m in fresh_messages if m.get("text")]
        if not texts:
            return []
        found: list[tuple[str, str]] = []
        for i in range(0, len(texts), 15):
            chunk = texts[i:i + 15]
            try:
                results = await self.ai.extract_entities(chunk)
            except Exception as exc:  # noqa: BLE001
                log.warning("детекция сущностей (чанк %d) не удалась: %s", i, exc)
                continue
            for etype, name in results:
                name = str(name or "").strip()
                low = name.lower()
                if not (2 <= len(name) <= 60):
                    continue
                if low in known_names or any(low in k for k in known_names):
                    continue
                if re.fullmatch(
                    r"[А-ЯЁа-яёA-Za-z0-9 .,\-'/«»():–]{2,60}", name
                ) is None:
                    continue
                found.append((name, etype))
        return found

    async def _ask_admin_questions(
        self, fresh_messages: list[dict[str, Any]] | None = None
    ) -> None:
        """По разу задать админу осмысленные вопросы."""
        if not config.admin_id:
            return
        fresh_messages = fresh_messages or []
        known_names = {c["name"].lower() for c in await self.db.list_countries()}
        known_names |= {f["full_name"].lower() for f in await self.db.list_factions()}

        for name, etype in await self._detect_entities(fresh_messages, known_names):
            if await self.db.get_entity("unknown", name):
                continue
            await self.db.add_known_entity("unknown", name, "указано в новостях")
            log.info("новая сущность в новостях: %s (%s)", name, etype)

        asked = 0
        for ent in await self.db.list_entities(ent_type="unknown", only_unasked=True):
            if asked >= 2:
                break
            question = (
                f"В новостях встречено «{ent['name']}». Существует ли это "
                f"страна/группировка в WARLORD RP? Ответь реплаем: "
                f"«страна», «группировка» или «нет»."
            )
            await self._ask_once(entity=ent, question=question)
            asked += 1

        for country in (await self.db.list_countries())[:2]:
            missing = self._missing_country_fields(country)
            if not missing:
                continue
            question = (
                f"Страна «{country['name']}»: не указано ({', '.join(missing)}). "
                f"Подскажи, чтобы карточка была точной. Ответ — реплаем."
            )
            await self._schedule_plain_question(question)

        for faction in (await self.db.list_factions())[:2]:
            missing = self._missing_faction_fields(faction)
            if not missing:
                continue
            question = (
                f"Группировка «{faction['full_name']}»: не указано ({', '.join(missing)}). "
                f"Подскажи реплаем."
            )
            await self._schedule_plain_question(question)

    @staticmethod
    def _missing_country_fields(country: dict[str, Any]) -> list[str]:
        missing: list[str] = []
        if not country.get("ruler"):
            missing.append("правитель")
        if country.get("population") is None:
            missing.append("население")
        if not country.get("budget"):
            missing.append("бюджет")
        if not country.get("resources_json"):
            missing.append("ресурсы")
        return missing

    @staticmethod
    def _missing_faction_fields(faction: dict[str, Any]) -> list[str]:
        missing: list[str] = []
        if not faction.get("base_country"):
            missing.append("страна базирования")
        if not faction.get("depends_on_country"):
            missing.append("подчинение")
        return missing

    async def _schedule_plain_question(self, question: str) -> None:
        """Задать вопрос без привязки к сущности."""
        if await self.db.had_asked(question):
            return
        try:
            msg = await self.bot.send_message(config.admin_id, question, parse_mode=None)
            await self.db.add_question(question, telegram_message_id=msg.message_id)
            log.info("вопрос: %s", question)
        except Exception as exc:  # noqa: BLE001
            log.error("вопрос не отправлен: %s", exc)

    async def _ask_once(self, entity: dict[str, Any], question: str) -> None:
        """Отправить вопрос и пометить, что уже спрашивали."""
        try:
            msg = await self.bot.send_message(config.admin_id, question, parse_mode=None)
            await self.db.add_question(question, telegram_message_id=msg.message_id)
            await self.db.mark_entity_asked(entity["id"])
            log.info("вопрос админу отправлен: %s", question)
        except Exception as exc:  # noqa: BLE001
            log.error("не смог отправить вопрос админу: %s", exc)

    async def _schedule_question(self, question: str) -> None:
        """Задать админу один вопрос (если ещё не задавался)."""
        if not config.admin_id:
            return
        if await self.db.had_asked(question):
            return
        try:
            msg = await self.bot.send_message(config.admin_id, question, parse_mode=None)
            await self.db.add_question(question, telegram_message_id=msg.message_id)
            log.info("вопрос: %s", question)
        except Exception as exc:  # noqa: BLE001
            log.error("вопрос не отправлен: %s", exc)

    # ================================================================== команды админа
    async def handle_admin_command(self, message_text: str) -> str:
        """Handle admin commands like /process, /newcountry."""
        parts = message_text.split(maxsplit=2)
        cmd = parts[0].lower()

        if cmd == "/process":
            await self.run_cycle()
            return "✅ Цикл запущен вручную."

        if cmd == "/newcountry":
            if len(parts) < 3:
                return "Использование: /newcountry <country|faction> <JSON>"
            try:
                data = json.loads(parts[2])
                typ = parts[1]
                if typ == "country":
                    await self.db.upsert_country(data)
                    return f"✅ Страна {data.get('name')} создана."
                elif typ == "faction":
                    await self.db.upsert_faction(data)
                    return f"✅ Группировка {data.get('full_name')} создана."
                return "Тип: country или faction"
            except Exception as e:
                return f"❌ Ошибка: {e}"

        if cmd == "/cards":
            await self._publish_cards()
            return "✅ Карточки переопубликованы."

        if cmd == "/cleanup":
            # dry_run по умолчанию: cleanup удаляет и сливает страны, поэтому
            # включать его в автозапуск нельзя. Применение - только по команде.
            apply_now = len(parts) > 1 and parts[1].strip().lower() == "apply"
            plan = await _cleanup.cleanup(self.db, dry_run=not apply_now,
                                          bot=self.bot)
            if apply_now:
                return ("✅ Cleanup применён.\n" + plan.summary()
                        + "\n\n⚠️ Теперь нажми /cards, чтобы карточки "
                            "синхронизировались с БД.")
            return ("🧹 План cleanup (ничего не изменено):\n" + plan.summary()
                    + "\n\nПрименить: /cleanup apply")

        if cmd in ("/unreg", "/anreg"):
            arg = message_text.split(maxsplit=1)
            if len(arg) < 2 or not arg[1].strip():
                return "Использование: /unreg <название страны>"
            return await self.unreg_country(arg[1].strip())

        if cmd == "/afk":
            arg = message_text.split(maxsplit=1)
            if len(arg) < 2 or not arg[1].strip():
                return "Использование: /afk <название страны>"
            return await self.afk_country(arg[1].strip())

        if cmd == "/active":
            arg = message_text.split(maxsplit=1)
            if len(arg) < 2 or not arg[1].strip():
                return "Использование: /active <название страны>"
            return await self.activate_country(arg[1].strip())

        return "Неизвестная команда."

    async def afk_country(self, name: str) -> str:
        """Пометить страну как АФК вручную (статы НЕ обнуляются)."""
        name = (name or "").strip()
        if not name:
            return "Укажите название страны."
        country = await self.db.get_country(name)
        if not country:
            return f"Страна «{name}» не найдена."
        old = country.get("status") or ""
        await self.db.set_country_status(country["name"], "afk")
        await self.db.log_change(country["name"], "status", old, "afk",
                                 "админ: ручной АФК")
        log.info("афк: %s (%s -> afk)", country["name"], old)
        return f"💤 Страна «{country['name']}» переведена в АФК."

    async def activate_country(self, name: str) -> str:
        """Вернуть страну из АФК/анрега/непринятых в active вручную."""
        name = (name or "").strip()
        if not name:
            return "Укажите название страны."
        country = await self.db.get_country(name)
        if not country:
            return f"Страна «{name}» не найдена."
        old = country.get("status") or ""
        await self.db.set_country_accepted(country["name"], True)
        await self.db.log_change(country["name"], "status", old, "active",
                                 "админ: возврат в игру")
        log.info("active: %s (%s -> active)", country["name"], old)
        return f"✅ Страна «{country['name']}» снова активна."

    async def unreg_country(self, name: str) -> str:
        """Анрег страны: отключить карточку, удалить сообщение."""
        name = (name or "").strip()
        if not name:
            return "Укажите название страны."
        country = await self.db.get_country(name)
        if not country:
            return f"Страна «{name}» не найдена."

        await self.db.set_country_status(country["name"], "unreg")
        log.info("анрег: %s", country["name"])

        msg_id = country.get("card_message_id")
        if msg_id:
            try:
                await self.bot.delete_message(config.target_chat_id, msg_id)
            except Exception as exc:  # noqa: BLE001
                log.warning("анрег: сообщение не удалено: %s", exc)
            await self.db.set_country_card_message(country["name"], None)

        await self.db.set_meta(f"dropped:{country['name']}", "1")
        return f"✅ Страна «{country['name']}» анрегнута, карточка отключена."

    _INACTIVE_UNREG_DAYS = 45

    # Подписи, которые явно НЕ являются флагом страны (карты/коллажи/скрины)
    _FLAG_SKIP_WORDS = (
        "карта", "границ", "территория отмечена", "изображение карты",
        "скрин", "коллаж", "соотношение", "map", "картинка-схема",
    )
    # Имя страны должны встретиться в первых N символах подписи анкеты
    _FLAG_NAME_SLICE = 300

    _PLACEHOLDER_NAMES: frozenset[str] = frozenset(
        {"страна", "государство", "группировка", "организация", "империя", "королевство"}
    )

    @staticmethod
    def _is_placeholder_anketa(name: str, fields: dict[str, Any], ent_type: str) -> bool:
        """Отсечь анкеты-шаблоны с плейсхолдерами (Страна / Правитель / ПГТ1)."""
        low = name.lower().strip()
        if ent_type == "country" and low in Mechanic._PLACEHOLDER_NAMES:
            return True

        ruler = str(fields.get("ruler") or fields.get("правитель") or "").strip().lower()
        party = str(fields.get("party") or fields.get("партия") or "").strip().lower()
        ideology = str(fields.get("ideology") or fields.get("идеология") or "").strip().lower()
        if ruler == "правитель" and party in ("", "партия") and ideology in ("", "идеология"):
            return True

        settlements = fields.get("settlements") or fields.get("поселения") or []
        if isinstance(settlements, dict):
            settlements = (
                [settlements.get("capital")]
                + list(settlements.get("pgt") or [])
            )
        for s in settlements:
            s_name = s.get("name") if isinstance(s, dict) else s
            if re.fullmatch(r"(?i)пгт\s*\d+", str(s_name).strip()):
                return True
        return False

    async def _declared_stats(self, country: dict[str, Any]) -> dict[str, Any] | None:
        """Заявленные (постовые) цифры страны — источник истины для экономики.

        Два случая:
        - stats_source="manual" — источник истины payload раздела 5 из
          manual_stats (ключ "country");
        - stats_source="anketa" — регистрация из анкеты: сами числа уже лежат в
          строке БД, возвращаем их как «заявленные», чтобы армия не сжималась
          пассивной формулой ниже регистрации.

        Для остальных (AI/новости) заявки нет — армия считается формулой
        боевого потенциала.
        """
        src = str(country.get("stats_source") or "").lower()
        if src == "manual":
            name = str(country.get("name") or "")
            if not name:
                return None
            try:
                stats = await self.db.get_manual_stats(name)
            except Exception as exc:  # noqa: BLE001
                log.warning("не удалось прочитать manual_stats для %s: %s", name, exc)
                return None
            payload = (stats or {}).get("payload") or {}
            declared = payload.get("country")
            return declared if isinstance(declared, dict) else None
        if src == "anketa":
            declared: dict[str, Any] = {}
            for key in ARMY_UPKEEP:
                value = country.get(key)
                if value not in (None, "", 0):
                    declared[key] = value
            declared["population"] = country.get("population")
            return declared or None
        return None

    async def _run_economy(self) -> None:
        """Пересчитать экономику стран/группировок за прошедшие часы.

        Сохраняет снимок «до» в prev_* колонки, применяет симуляцию
        (economy.py) и перезаписывает карточку данными среза.
        """
        now = datetime.now(timezone.utc)

        for country in await self.db.list_countries():
            hours = config.update_interval_hours
            last_dt: datetime | None = None
            if last := country.get("last_econ_at"):
                try:
                    last_dt = datetime.fromisoformat(str(last))
                    if last_dt.tzinfo is None:
                        last_dt = last_dt.replace(tzinfo=timezone.utc)
                    hours = (now - last_dt).total_seconds() / 3600.0
                except ValueError:
                    pass
            hours = max(hours, 0.5)

            if not country.get("registered_at"):
                await self._seed_registration(country)  # старт хроники для новичков
                country = await self.db.get_country(country["name"]) or country

            for key in (
                "prev_population", "prev_budget", "prev_army_infantry",
                "prev_army_light_vehicles", "prev_army_heavy_wheeled",
                "prev_army_artillery", "prev_army_drones", "prev_army_navy",
                "prev_stability", "prev_war_support", "prev_happiness",
            ):
                country[key] = country.get(key[len("prev_"):])
            events = (
                await self.db.list_economy_events(country["id"])
                if country.get("id") else []
            )
            declared = await self._declared_stats(country)
            econ = compute_country_economy(
                country, hours, events=events, now=now, one_off_from=last_dt,
                declared=declared,
            )
            # Админ-редактор: зафиксированные числа не стираются симуляцией.
            # Правка, на которую админ нажал +/− в панели, применяется к строке
            # сразу, а здесь защищается от перезаписи до снятия блокировки.
            overrides = (
                await self.db.get_country_overrides(country["id"])
                if country.get("id") else {}
            )
            if overrides:
                changed = {k: v for k, v in overrides.items() if econ.get(k) != v}
                if changed:
                    log.info("страна %s: применены админские блокировки %s",
                             country.get("name"), sorted(changed))
                econ.update(overrides)
            country.update({k: v for k, v in econ.items() if k != "deltas"})
            country["last_econ_at"] = now.isoformat(timespec="seconds")
            await self.db.upsert_country(country)

        factions = await self.db.list_factions()
        for faction in factions:
            hours = config.update_interval_hours
            if last := faction.get("last_econ_at"):
                try:
                    last_dt = datetime.fromisoformat(str(last))
                    if last_dt.tzinfo is None:
                        last_dt = last_dt.replace(tzinfo=timezone.utc)
                    hours = (now - last_dt).total_seconds() / 3600.0
                except ValueError:
                    pass
            hours = max(hours, 0.5)

            faction["prev_budget"] = faction.get("budget")
            faction["prev_strength"] = faction.get("strength")
            faction["prev_reputation"] = faction.get("reputation")
            econ = compute_faction_economy(faction, hours)
            faction.update({k: v for k, v in econ.items() if k != "deltas"})
            faction["last_econ_at"] = now.isoformat(timespec="seconds")
            await self.db.upsert_faction(faction)

        await self._bind_factions_to_countries()
        log.info("экономика: пересчитано стран=%d группировок=%d",
                 len(await self.db.list_countries()), len(factions))

    async def _bind_factions_to_countries(self) -> None:
        """Автосвязка группировок со странами по полям base_country/depends_on_country."""
        countries = await self.db.list_countries()
        country_by_name = {c["name"].strip().lower(): c["id"] for c in countries}
        for faction in await self.db.list_factions():
            fid = faction.get("id")
            if not fid:
                continue
            base = str(faction.get("base_country") or "").strip().lower()
            depends = str(faction.get("depends_on_country") or "").strip().lower()
            for cname, status in ((base, "базируется"), (depends, "контракт")):
                cid = country_by_name.get(cname)
                if cid:
                    await self.db.bind_faction_to_country(cid, fid, status)

    async def _attached_factions(self, country: dict[str, Any]) -> list[dict[str, Any]]:
        """Список группировок, привязанных к стране (для блока на карточке)."""
        country_id = country.get("id")
        if not country_id:
            return []
        try:
            return await self.db.list_country_factions(country_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("не удалось загрузить группировки для %s: %s",
                        country.get("name"), exc)
            return []

    async def _reconcile_countries(self) -> int:
        """Создать карточки для стран из known_entities, которых ещё нет в БД.

        Варианты названий, уже покрытые алиасами существующих стран
        (полное имя ↔ краткое, «республика X» ↔ «X»), НЕ создаются
        повторно — имя добавляется в алиасы канонической страны.
        Сокращения-аббревиатуры (≤3 симв.) без данных не создаются.
        Явно удалённые/слитые имена (в meta dropped_countries) не воскрешаются.
        """
        countries = await self.db.list_countries()
        name_set = {c["name"].strip().lower() for c in countries}
        alias_pool: set[str] = set()
        for c in countries:
            for a in c.get("aliases_json") or []:
                alias_pool.add(str(a).strip().lower())
        # явно дропнутые имена — не воскрешать никогда
        dropped_raw = await self.db.get_meta("dropped_countries") or ""
        dropped = {n.strip().lower() for n in dropped_raw.split(",") if n.strip()}

        created = 0
        for ent in await self.db.list_entities(ent_type="country"):
            name = (ent.get("name") or "").strip()
            if not name:
                continue
            low = name.lower()
            if low in name_set or low in alias_pool or low in dropped:
                continue
            if low in Mechanic._PLACEHOLDER_NAMES or len(name) > 120:
                continue
            # аббревиатуры/мусор без данных (напр. «MOS») — карточку не делаем
            if len(re.sub(r"[^а-яa-z0-9]", "", low)) <= 3:
                continue
            # вариант уже существующей страны → сделать алиасом, не плодить дубль
            if dup := self._find_duplicate_of(name, countries):
                current = await self.db.get_country(dup)
                if current is not None:
                    aliases = list(current.get("aliases_json") or [])
                    if name not in aliases:
                        aliases.append(name)
                    await self.db.upsert_country({**current, "aliases_json": aliases})
                # пометить как дропнутое и удалить из known_entities
                await self._mark_dropped(name)
                await self.db.delete_entity("country", name)
                log.info("reconcile: %s → алиас к %s (дроп)", name, dup)
                continue
            await self.db.upsert_country({
                "name": name,
                "industry_json": list(Mechanic._DEFAULT_INDUSTRY),
            })
            name_set.add(low)
            created += 1
            log.info("reconcile: создана страна из known_entities: %s", name)
        if created:
            log.info("reconcile: создано стран: %d", created)
        return created

    async def _mark_dropped(self, name: str) -> None:
        """Добавить имя в meta dropped_countries, чтобы reconcile его не воскрешал."""
        raw = await self.db.get_meta("dropped_countries") or ""
        dropped = {n.strip().lower() for n in raw.split(",") if n.strip()}
        dropped.add(name.strip().lower())
        await self.db.set_meta("dropped_countries", ",".join(sorted(dropped)))

    async def _delete_country_card_message(self, name: str) -> None:
        """Удалить карточку страны из темы и сбросить message_id/fingerprint."""
        country = await self.db.get_country(name)
        if not country or not country.get("card_message_id"):
            return
        topic = await self.db.get_topic("карточки стран")
        if not topic:
            return
        try:
            await self.bot.delete_message(
                config.target_chat_id, int(country["card_message_id"])
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("не удалил карточку %s: %s", name, exc)
        await self.db.set_country_card_message(name, None)
        await self.db.set_country_card_fingerprint(name, "")

    @staticmethod
    def _find_duplicate_of(name: str, existing: list[dict[str, Any]]) -> str:
        """Вернуть каноническое имя, вариантом которого является `name`."""
        low = (name or "").strip().lower()
        base = re.sub(r"[\(\[].*?[\)\]]", "", low).strip()  # без скобочной приписки
        min_len = 4
        for c in existing:
            cname = (c.get("name") or "").strip().lower()
            if not cname:
                continue
            if low == cname or base == cname:
                return c["name"]
            if len(low) >= min_len and low in cname:
                return c["name"]   # «Белоярск» ⊆ «республика Белоярск»
            if len(cname) >= min_len and cname in low:
                return c["name"]   # «Фаустланд» ⊆ «Фаустланд (Faustland)»
            cbase = re.sub(r"[\(\[].*?[\)\]]", "", cname).strip()
            if len(base) >= min_len and base == cbase:
                return c["name"]
        return ""

    async def _reconcile_factions(self) -> int:
        """Создать карточки группировок из known_entities, которых нет в БД."""
        factions = await self.db.list_factions()
        existing = {f["full_name"].strip().lower() for f in factions}
        existing_keys = {faction_canonical_key(f["full_name"]) for f in factions if faction_canonical_key(f["full_name"])}
        created = 0
        for ent in await self.db.list_entities(ent_type="faction"):
            name = (ent.get("name") or "").strip()
            if not name or name.lower() in existing or len(name) > 120:
                continue
            if len(re.sub(r"[^а-яa-z0-9]", "", name.lower())) <= 2:
                continue
            mkey = faction_canonical_key(name)
            # дубликат уже есть (например, «Фаузвафф» при живом «ЧВК Fauzwaff»)?
            # не создаём вторую карточку, её всё равно склеили бы обратно
            if mkey and mkey in existing_keys:
                log.info("reconcile: пропущен дубль группировки %s (ключ %s уже есть)", name, mkey)
                continue
            await self.db.upsert_faction({"full_name": name})
            existing.add(name.lower())
            if mkey:
                existing_keys.add(mkey)
            created += 1
            log.info("reconcile: создана группировка из known_entities: %s", name)
        if created:
            log.info("reconcile: создано группировок: %d", created)
        return created

    async def _faction_merge_keys(self) -> set[str]:
        """Канонические ключи склейки группировок (по полному имени записей в БД)."""
        keys: set[str] = set()
        for f in await self.db.list_factions():
            key = faction_canonical_key(f.get("full_name") or "")
            if key:
                keys.add(key)
        return keys

    async def _apply_faction_known_facts(self) -> None:
        """Дополнить wars_json фактами, которые не выводятся из новостей.

        Например, участие Fauzwaff в войне с Хавен-аль-Джихадом (ИЭХД).
        Идемпотентно: всегда (FACTION_KNOWN_WARS) добавляем только новые войны.
        """
        if not FACTION_KNOWN_WARS:
            return
        for faction in await self.db.list_factions():
            key = faction_canonical_key(faction.get("full_name") or "")
            known = FACTION_KNOWN_WARS.get(key)
            if not known:
                continue
            existing = [w for w in (faction.get("wars_json") or []) if isinstance(w, dict)]
            have = {str(w.get("name") or "").strip().lower() for w in existing if w.get("name")}
            missing = [w for w in known if str(w.get("name") or "").strip().lower() not in have]
            if not missing:
                continue
            updated = dict(faction)
            updated["wars_json"] = existing + missing
            updated["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            await self.db.upsert_faction(updated)
            log.info("группировка %s: добавлены войны %s",
                     faction.get("full_name"), missing)

    async def _merge_duplicate_factions(self) -> int:
        """Склеить дублирующие карточки группировок по ключу без префикса.

        Например «ДШРГ "Ратич"» + «МВО "Ратич"» → остаётся запись с самым
        большим бюджетом, численность складывается (с потолком по ключу,
        например Ратич — 230). Связи со странами переносятся; лишние карточки
        Telegram удаляются. Возвращает число склеек.
        """
        factions = await self.db.list_factions()
        groups: dict[str, list[dict[str, Any]]] = {}
        for f in factions:
            key = faction_canonical_key(f.get("full_name") or "")
            if key and len(factions) > 1:
                groups.setdefault(key, []).append(f)
        merged = 0
        for key, members in groups.items():
            if len(members) < 2:
                continue
            keeper = max(members, key=lambda f: float(f.get("budget") or 0.0))
            others = [f for f in members if f["id"] != keeper["id"]]
            strength = sum(int(f.get("strength") or 0) for f in members)
            cap = faction_strength_cap(keeper.get("full_name") or "")
            if cap is not None:
                strength = min(strength, cap)
            budget = max(float(f.get("budget") or 0.0) for f in members)
            wars: list[dict[str, Any]] = []
            seen: set[str] = set()
            for f in sorted(members, key=lambda m: m.get("id") or 0):
                for w in (f.get("wars_json") or []):
                    if not isinstance(w, dict):
                        continue
                    wn = str(w.get("name") or "").strip()
                    if wn and wn.lower() not in seen:
                        seen.add(wn.lower())
                        wars.append(w)
            for w in (FACTION_KNOWN_WARS.get(key) or []):
                wn = str(w.get("name") or "").strip()
                if wn and wn.lower() not in seen:
                    seen.add(wn.lower())
                    wars.append(w)
            commander = keeper.get("commander") or next(
                (f.get("commander") for f in others if f.get("commander")), None)
            base = keeper.get("base_country") or next(
                (f.get("base_country") for f in others if f.get("base_country")), None)
            depends = keeper.get("depends_on_country") or next(
                (f.get("depends_on_country") for f in others if f.get("depends_on_country")), None)

            updated = dict(keeper)
            updated["strength"] = strength
            updated["budget"] = budget
            updated["wars_json"] = wars
            if commander:
                updated["commander"] = commander
            if base:
                updated["base_country"] = base
            if depends:
                updated["depends_on_country"] = depends
            updated["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            await self.db.upsert_faction(updated)
            keeper_id = int(keeper["id"])
            for other in others:
                for binding in await self.db.list_faction_bindings(int(other["id"])):
                    await self.db.bind_faction_to_country(
                        int(binding["country_id"]), keeper_id,
                        binding.get("status") or "базируется",
                    )
                card_msg = other.get("card_message_id")
                if card_msg:
                    try:
                        await self.bot.delete_message(config.target_chat_id, int(card_msg))
                    except Exception as exc:  # noqa: BLE001
                        log.info("группировка %s: не удалось удалить карточку %s: %s",
                                 other.get("full_name"), card_msg, exc)
                await self.db.delete_faction_by_id(int(other["id"]))
                log.info("группировки: склеена %s -> %s (солдаты %s, бюджет %s)",
                         other.get("full_name"), keeper.get("full_name"), strength, budget)
            merged += 1
        if merged:
            log.info("группировки: склеено дублей: %d", merged)
        return merged

    async def _auto_unreg_inactive(self) -> None:
        """ОТКЛЮЧЕНО: анрег — ручное решение админа (тема объявлений + /unreg).

        Раньше функция выгоняла страны без активности за 45 дней, опираясь на
        last_activity_at. Это ошибочно удаляло живые страны с устаревшей
        last_activity_at (например, Линьфа: последний пост свежий, а поле
        не обновлялось) и вместе с ними — их карточки. Оставляем как no-op,
        чтобы случайный вызов не навредил.
        """
        return

    async def _collect_flags(self, max_flags: int = 25) -> None:
        """Собрать флаги стран из фото в темах анкет.

        Сообщения с фото и текстом, где встречается название/алиас страны,
        скачиваются через userbot, пере-загружаются через бота (чтобы получить
        bot file_id) и сохраняются в countries.flag_file_id.
        """
        countries = await self.db.list_countries()
        missing = [c for c in countries if not c.get("flag_file_id")]
        if not missing:
            return
        # Full history scan is expensive (flood waits on media) and countries
        # that have no flag photo in the topic never resolve, so do it rarely.
        if self._flags_last_scan is not None:
            waited = datetime.now(timezone.utc) - self._flags_last_scan
            if waited < timedelta(minutes=FLAGS_SCAN_INTERVAL_MINUTES):
                log.info("флаги: повторный скан не раньше чем через %d мин (ждём: %d стран)",
                         FLAGS_SCAN_INTERVAL_MINUTES, len(missing))
                return
        self._flags_last_scan = datetime.now(timezone.utc)
        alias_map: dict[str, str] = {}
        for c in countries:
            alias_map[c["name"].strip().lower()] = c["name"]
            for a in c.get("aliases_json") or []:
                alias_map[str(a).strip().lower()] = c["name"]

        topics = await self.db.get_topics("анкеты")
        if not topics:
            if config.anketa_chat_id is not None and config.anketa_topic_id is not None:
                topics = [{"chat_id": config.anketa_chat_id, "thread_id": config.anketa_topic_id}]

        uploaded = 0
        for t in topics:
            chat_id = t.get("chat_id")
            thread_id = t.get("thread_id")
            if thread_id is None or not isinstance(chat_id, int):
                continue
            try:
                async for msg in self.userbot.iter_messages(
                    chat_id, reply_to=thread_id, limit=80
                ):
                    if uploaded >= max_flags:
                        return
                    if not isinstance(msg, Message) or not getattr(msg, "photo", None):
                        continue
                    text = (msg.message or "").strip().lower()
                    if not text:
                        continue
                    # «карта мира», «границы», «соотношение» и т.п. — НЕ флаг
                    if any(w in text for w in self._FLAG_SKIP_WORDS):
                        continue
                    canonical = ""
                    for key in sorted(alias_map, key=len, reverse=True):
                        # имя должно встретиться в начале подписи/анкеты,
                        # а не в списке стран под картой
                        if key and key in text[: self._FLAG_NAME_SLICE]:
                            canonical = alias_map[key]
                            break
                    if not canonical:
                        continue
                    cur = await self.db.get_country(canonical)
                    if not cur or cur.get("flag_file_id"):
                        continue
                    data = await msg.download_media(file=bytes)
                    if not data:
                        continue
                    file_id = await self._upload_photo_get_file_id(data)
                    if file_id:
                        await self.db.set_country_flag(canonical, file_id)
                        uploaded += 1
                        log.info("флаг %s: сохранён (%s)", canonical, file_id[:20])
            except Exception as exc:  # noqa: BLE001
                log.warning("флаги %s/%s: %s", chat_id, thread_id, exc)
        log.info("флаги: собрано %d из %d без флага", uploaded, len(missing))

    async def _upload_photo_get_file_id(self, data: bytes) -> str:
        """Пере-загрузить фото через бота, вернуть bot file_id."""
        scratch = config.admin_questions_chat or config.admin_id or config.target_chat_id
        if not scratch:
            return ""
        try:
            sent = await self.bot.send_photo(scratch, BufferedInputFile(data, filename="flag.png"))
            fid = sent.photo[-1].file_id
            try:
                await self.bot.delete_message(scratch, sent.message_id)
            except Exception:  # noqa: BLE001
                pass
            return fid
        except Exception as exc:  # noqa: BLE001
            log.warning("флаг: не удалось загрузить фото: %s", exc)
            return ""

    async def _backfill_unprocessed_countries(self) -> None:
        """Для стран без AI-обработки (updated_at IS NULL) один раз прогнать
        карточку по их ВСЕЙ новостной истории.

        Благодаря этому «новая» (в т.ч. восстановленная после переименования
        или созданная по ответу админа) страна сразу получает промышленность,
        ресурсы и население из своих же новостей, а не стартовую заглушку.
        """
        chat_id = config.target_chat_id
        topics = await self.db.list_topics()
        news_topic_ids = [
            t["thread_id"] for t in topics
            if t["purpose"] in ("новости", "расширения") or not t["purpose"]
        ]
        if not news_topic_ids:
            news_topic_ids = list(config.news_topics.values())

        for country in await self.db.list_countries():
            name = country["name"]
            if name in self._backfilled:
                continue
            if country.get("updated_at"):
                self._backfilled.add(name)
                continue
            if country.get("status", "active") == "unreg":
                self._backfilled.add(name)
                continue
            keys = [name.lower()] + [str(a).lower() for a in (country.get("aliases_json") or [])]

            picked: list[tuple[int, dict[str, Any]]] = []
            for thread_id in news_topic_ids:
                if thread_id is None:
                    continue
                for m in await self._read_topic_messages(chat_id, thread_id, limit=300):
                    try:
                        dt = datetime.fromisoformat(
                            (m.get("date") or "").replace("Z", "+00:00"))
                    except ValueError:
                        continue
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    low = (m["text"] or "").lower()
                    if any(k and (k in low or f"_{k}_" in low) for k in keys):
                        picked.append((thread_id, m))
                    if len(picked) >= 15:
                        break
                if len(picked) >= 15:
                    break

            self._backfilled.add(name)
            if not picked:
                continue
            picked.sort(key=lambda x: x[1].get("date") or "")
            msgs = [m for _, m in picked[:10]]
            await self._process_country(name, msgs)
            for thread_id, m in picked:
                if not await self.db.has_message_seen(chat_id, thread_id, m["id"]):
                    await self.db.mark_message_seen(chat_id, thread_id, m["id"], m["text"])
            log.info("backfill: страна %s обработана (%d сообщений)",
                     name, len(picked))

    # ── контекст карточки и оповещения (раздел 10) ──

    def _card_link(self, thread_id: int, message_id: int | None) -> str:
        """Ссылка на сообщение с карточкой для уведомления правителю."""
        chat_id = int(getattr(config, "target_chat_id", 0) or 0)
        if not chat_id or not message_id:
            return ""
        return f"https://t.me/c{abs(chat_id)}/{thread_id}/{message_id}"

    @staticmethod
    def _notifications_text(raw: Any) -> list[str]:
        """notifications_json -> список строк для секции УВЕДОМЛЕНИЯ.

        Раньше здесь был json.loads прямо в цикле публикации, и при мусоре в
        JSON-колонке весь цикл падал целиком. Теперь мусор просто игнорируется.
        """
        if not raw:
            return []
        data: Any = raw
        if isinstance(raw, (str, bytes)):
            try:
                data = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                text = str(raw).strip()
                return [text] if text else []
        if isinstance(data, list):
            out: list[str] = []
            for item in data:
                if isinstance(item, dict):
                    body = str(item.get("text") or item.get("body") or "").strip()
                    label = str(item.get("title") or item.get("type") or "").strip()
                    if body:
                        out.append(f"{label}: {body}" if label else body)
                elif str(item).strip():
                    out.append(str(item).strip())
            return out
        text = str(data).strip()
        return [text] if text else []

    async def _card_context(self, country: dict[str, Any]) -> dict[str, Any]:
        """Собрать данные для карточки: итоги, уведомления, блок изменений.

        Источник изменений - changes_log, а не сравнение prev_*/текущее: в
        журнале есть причина и время, и по нему видно, что именно изменилось
        за цикл (раздел 10).
        """
        now = datetime.now(timezone.utc)
        window = float(getattr(config, "card_publish_interval_hours", 2) or 2)
        since = (now - timedelta(hours=window)).isoformat(timespec="seconds")
        name = str(country.get("name") or "")
        try:
            rows = await self.db.list_changes(name, limit=100)
        except Exception as exc:  # noqa: BLE001 - блок изменений не критичен
            log.debug("changes_log %s: %s", name, exc)
            rows = []
        return {
            "summary": str(country.get("summary") or ""),
            "notifications": self._notifications_text(country.get("notifications_json")),
            "changes": collect_changes_since(rows, since=since),
            "days_ago": _activity.days_since_post(
                _activity._parse_ts(country.get("last_post_at")), now),
            "updated_at": str(country.get("last_post_at") or "") or None,
        }

    # ---------- админ: ручное редактирование цифр стран ----------
    _ADMIN_FIELD_LABELS: dict[str, str] = {
        "population": "население",
        "population_capacity": "лимит населения",
        "army_infantry": "пехота",
        "army_light_vehicles": "лёгкая техника",
        "army_heavy_wheeled": "тяжёлая техника",
        "army_artillery": "артеллерия",
        "army_drones": "БПЛА",
        "army_navy": "флот",
        "budget": "бюджет",
        "income_daily": "доход/день",
        "expenses_daily": "расходы/день",
        "stability": "стабильность",
        "happiness": "счастье",
        "war_support": "поддержка войны",
    }

    _ADMIN_TEXT_LABELS: dict[str, str] = {
        "ruler": "правитель",
        "party": "партия",
        "ideology": "идеология",
        "color": "цвет",
        "tax_level": "налоги",
        "status": "статус",
    }

    _ADMIN_LIST_LABELS: dict[str, str] = {
        "settlements_json": "населённые пункты",
        "industry_json": "промышленность",
        "research_json": "исследования",
        "resources_json": "ресурсы",
    }

    async def admin_set_text(
        self, country_id: int, field: str, value: str
    ) -> str:
        """Задать текстовое поле страны из админ-панели. Возвращает текст ответа."""
        country = await self.db.get_country_by_id(int(country_id))
        if not country:
            return "❌ Страна не найдена."
        name = str(country.get("name") or "?")
        label = self._ADMIN_TEXT_LABELS.get(field, field)
        value = str(value or "").strip()
        if field == "tax_level":
            normalized = normalize_tax_level(value)
            if normalized is None:
                return (f"❌ Неверный уровень налогов: {value}. "
                        f"Один из: Пониженный, Средний, Повышенный, Чрезмерный.")
            value = normalized
        if field == "status" and value not in ("active", "unreg", "anreg", "archive"):
            return "❌ Статус: active / unreg / archive."
        empty_ok = field != "status"
        if not value and not empty_ok:
            return f"❌ {label}: пустое значение недопустимо."
        old = str(country.get(field) or "")
        await self.db.set_country_text_field(int(country_id), field, value)
        await self.db.log_change(name, field, old, value, "админ-правка")
        await self.db.set_country_updated_at(
            name, datetime.now(timezone.utc).isoformat(timespec="seconds"))
        return f"✅ <b>{name}</b>: {label}: «{old or '—'}» → «{value or '—'}»."

    async def admin_add_list_item(
        self, country_id: int, list_field: str, name: str, count: int = 0,
    ) -> str:
        """Добавить элемент списка страны (город/завод/ресурс…)."""
        country = await self.db.get_country_by_id(int(country_id))
        if not country:
            return "❌ Страна не найдена."
        cname = str(country.get("name") or "?")
        label = self._ADMIN_LIST_LABELS.get(list_field, list_field)
        try:
            total = await self.db.add_country_list_item(
                int(country_id), list_field, name, count=max(int(count), 0))
        except ValueError as exc:
            return f"❌ Не удалось добавить: {exc}"
        extra = f" ×{total}" if total else ""
        await self.db.log_change(
            cname, list_field, "", f"+{name}{extra}", "админ-правка")
        await self.db.set_country_updated_at(
            cname, datetime.now(timezone.utc).isoformat(timespec="seconds"))
        return f"✅ <b>{cname}</b>: {label}: +{name}{extra}."

    async def admin_remove_list_item(
        self, country_id: int, list_field: str, name: str, count: int = 0,
    ) -> str:
        """Убрать элемент списка страны (целиком или частично)."""
        country = await self.db.get_country_by_id(int(country_id))
        if not country:
            return "❌ Страна не найдена."
        cname = str(country.get("name") or "?")
        label = self._ADMIN_LIST_LABELS.get(list_field, list_field)
        try:
            removed = await self.db.remove_country_list_item(
                int(country_id), list_field, name, count=max(int(count), 0))
        except ValueError as exc:
            return f"❌ Не удалось убрать: {exc}"
        if removed <= 0:
            return f"⚠️ <b>{cname}</b>: «{name}» не найдено среди {label}."
        await self.db.log_change(
            cname, list_field, name, f"−{name} ({removed})",
            "админ-правка")
        await self.db.set_country_updated_at(
            cname, datetime.now(timezone.utc).isoformat(timespec="seconds"))
        return f"✅ <b>{cname}</b>: {label}: −{name} ({removed})."

    async def admin_edit_country(
        self, country_id: int, field: str, delta: float
    ) -> str:
        """Изменить одну цифру страны из админ-панели. Возвращает текст ответа."""
        country = await self.db.get_country_by_id(int(country_id))
        if not country:
            return "❌ Страна не найдена."
        name = str(country.get("name") or "?")
        label = self._ADMIN_FIELD_LABELS.get(field, field)
        try:
            current = float(country.get(field) or 0)
        except (TypeError, ValueError) as exc:  # noqa: BLE001
            return f"❌ {name}: поле {label} не число ({exc})."
        new = current + float(delta)
        if field in ("stability", "happiness", "war_support"):
            new = min(100.0, max(0.0, new))
        else:
            new = max(0.0, new)
        formatted = (
            f"{current:,.0f}" if field == "budget" else f"{current:.1f}"
            if current != int(current) else f"{int(current)}"
        )
        new_formatted = (
            f"{new:,.0f}" if field == "budget" else f"{new:.1f}"
            if new != int(new) else f"{int(new)}"
        )
        await self.db.set_country_value(int(country_id), field, new)
        await self.db.set_country_override(int(country_id), field, new)
        return (f"✅ <b>{name}</b>: {label}: {formatted} → {new_formatted}."
                f" ⭐ зафиксировано до снятия блокировки.")

    async def admin_approve_flag(self, country_id: int, file_id: str) -> str:
        """Привязать загруженный админом флаг к стране."""
        country = await self.db.get_country_by_id(int(country_id))
        if not country:
            return "❌ Страна не найдена."
        await self.db.set_country_flag(str(country["name"]), file_id)
        return f"✅ <b>{country['name']}</b>: флаг обновлён."

    async def admin_country_snapshot(
        self, country: dict[str, Any],
        overrides: dict[str, float] | None = None,
    ) -> str:
        """Текст сводки страны для меню действий админ-панели."""
        def fmt(value) -> str:
            try:
                v = float(value or 0)
            except (TypeError, ValueError):
                return "0"
            return f"{v:,.0f}" if v >= 1000 else f"{int(v)}"
        name = str(country.get("name") or "?")
        status = str(country.get("status") or "?")
        locked = "⭐" if overrides else ""
        overrides = overrides or {}

        def line(label: str, field: str, suffix: str = "") -> str:
            # для зафиксированного поля показываем значение блокировки —
            # именно её симуляция будет ставить в карточку каждый цикл
            value = overrides.get(field, country.get(field))
            mark = " ⭐" if field in overrides else ""
            return f"{label}: {fmt(value)}{suffix}{mark}"

        rows = [
            f"<b>{name}</b> (<i>{status}</i>){locked}",
            line("Население", "population"),
            line("Лимит", "population_capacity"),
            line("Армия", "army_infantry"),
            line("Бюджет", "budget"),
            line("Доход/день", "income_daily"),
            line("Расходы/день", "expenses_daily"),
            line("Стабильность", "stability", "%"),
            line("Счастье", "happiness", "%"),
            line("Поддержка войны", "war_support", "%"),
        ]
        if overrides:
            rows.append("⭐ — число зафиксировано админом, симуляция его не трогает")
        return "\n".join(rows)

    async def republish_country_card(self, name: str) -> str:
        """Переиздать карточку одной страны (без ожидания цикла)."""
        country = await self.db.get_country(name)
        if not country:
            return "❌ Страна не найдена."
        thread, _ = await self._resolve_card_topics()
        if not thread:
            return "❌ Тема карточек стран не найдена."
        # Навигация: 찾ём соседние страны по алфавиту
        all_countries = await self.db.list_countries()
        countries_sorted = sorted(all_countries, key=lambda c: str(c.get("name") or ""))
        idx = next((i for i, c in enumerate(countries_sorted) if c.get("name") == name), -1)
        prev_country = countries_sorted[idx - 1]["name"] if idx > 0 else None
        next_country = countries_sorted[idx + 1]["name"] if 0 <= idx < len(countries_sorted) - 1 else None
        ctx = await self._card_context(country)
        try:
            msg_id = await publish_country_card(
                self.bot, country, thread, country.get("card_message_id"),
                ctx["summary"], ctx["notifications"],
                changes=ctx["changes"], days_ago=ctx["days_ago"],
                updated_at=ctx["updated_at"],
                prev_country=prev_country, next_country=next_country,
            )
        except CardNotPublishable as skip:
            return f"⚠️ {name}: карточка не публикуется ({skip})."
        if msg_id is None:
            await self.db.set_country_card_message(name, None)
            await self.db.set_country_card_fingerprint(name, "")
            return "⚠️ Карточка не найдена в теме — сообщение сброшено, будет создана заново."
        now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
        await self.db.set_country_card_message(name, msg_id)
        await self.db.set_country_card_fingerprint(
            name,
            hashlib.sha256(build_country_card(country).encode("utf-8")).hexdigest()[:16],
        )
        await self.db.set_country_card_published_at(name, now_iso)
        # Сохранить страну в БД, чтобы зафиксировать отпечаток и время публикации,
        # и обновить updated_at — это защищает от отката при следующем цикле экономики.
        country["card_message_id"] = msg_id
        country["card_fingerprint"] = hashlib.sha256(build_country_card(country).encode("utf-8")).hexdigest()[:16]
        country["card_published_at"] = now_iso
        country["updated_at"] = now_iso
        await self.db.upsert_country(country)
        return f"✅ <b>{name}</b>: карточка переиздана и сохранена в БД."

    async def _notify_cycle(
        self,
        updates: dict[str, list[dict[str, Any]]],
        notifier: "_notify.RulerNotifier",
        country_thread: int | None,
    ) -> None:
        """Уровни 2-4 оповещений (раздел 10). Пустые уведомления не шлём."""
        chat_id = int(getattr(config, "target_chat_id", 0) or 0)
        if updates and country_thread and getattr(config, "NOTIFY_TOPIC", True):
            window = float(getattr(config, "card_publish_interval_hours", 2) or 2)
            await _notify.send_topic_summary(
                self.bot, updates,
                datetime.now(timezone.utc) + timedelta(hours=window),
                chat_id=chat_id, thread_id=country_thread,
            )
        if notifier.pending:
            sent = await notifier.flush()
            log.info("уведомлений правителям отправлено: %d", sent)
        if _notify.report_due():
            if await _notify.send_daily_report(self.db, self.bot):
                log.info("суточный отчёт админу отправлен")

    async def publish_all_cards(self, force: bool = False) -> None:
        """Опубликовать/обновить карточки всех известных стран и группировок.

        Каждая карточка печатается заново (edit) только когда изменился
        fingerprint текста — тогда флаги, промышленность, армия и казна
        появляются на уже выложенных карточках. Дебаунс защищает от флуда.
        Неактивные (анрег) страны пропускаются, их карточки удаляются.
        """
        country_thread, faction_thread = await self._resolve_card_topics()
        country_topic = {"thread_id": country_thread} if country_thread else None
        faction_topic = {"thread_id": faction_thread} if faction_thread else None
        if not (country_topic and faction_topic):
            log.warning(
                "темы карточек не найдены (страны=%s, группировки=%s) — публикация пропущена",
                country_thread, faction_thread,
            )
            return

        # Изменения за цикл: из них строятся блок в карточке, отчёт в тему
        # (уровень 2) и уведомления правителям (уровень 3).
        cycle_updates: dict[str, list[dict[str, Any]]] = {}
        notifier = _notify.RulerNotifier(self.bot)

        countries = await self.db.list_countries()
        # Навигация: сортируем по имени, чтобы prev/next были стабильными
        countries_sorted = sorted(countries, key=lambda c: str(c.get("name") or ""))
        total = len(countries_sorted)
        for idx, country in enumerate(countries_sorted):
            if country.get("status", "active") == "unreg":
                # анрегнутая страна: карточку из темы убираем раз и навсегда
                if country.get("card_message_id"):
                    await self._delete_country_card_message(country.get("name"))
                continue
            if not country_topic:
                break
            try:
                prev_country = countries_sorted[idx - 1]["name"] if idx > 0 else None
                next_country = countries_sorted[idx + 1]["name"] if idx < total - 1 else None

                # Групировки нужны ДО расчёта текста: карточка печатает их
                # секцией, и fingerprint должен совпадать с тем, что уйдёт.
                country["bound_factions"] = await self._attached_factions(country)
                ctx = await self._card_context(country)
            except Exception as exc:  # noqa: BLE001
                log.warning("карточка страны %s: не собраны данные (%s)",
                            country.get("name"), exc)
                continue
            text = build_country_card(
                country, ctx["summary"], ctx["notifications"],
                changes=ctx["changes"], days_ago=ctx["days_ago"],
                updated_at=ctx["updated_at"],
                prev_country=prev_country, next_country=next_country,
            )
            fingerprint = hashlib.sha256(
                (text + "|flag=" + (country.get("flag_file_id") or "")).encode("utf-8")
            ).hexdigest()[:16]
            if fingerprint == country.get("card_fingerprint"):
                continue  # текст не менялся — не дёргаем Telegram
            # Дебаунс: перепечатываем не чаще, чем раз в N часов (у казны,
            # которая растёт каждый цикл, текст меняется постоянно) —
            # иначе упрёмся во флуд-контроль Telegram.
            published_at = country.get("card_published_at")
            if not force and published_at:
                try:
                    last_dt = datetime.fromisoformat(str(published_at))
                    if last_dt.tzinfo is None:
                        last_dt = last_dt.replace(tzinfo=timezone.utc)
                    elapsed_h = (datetime.now(timezone.utc) - last_dt).total_seconds() / 3600.0
                except ValueError:
                    elapsed_h = float("inf")
                if elapsed_h < config.card_publish_interval_hours:
                    continue
            try:
                # ВАЖНО: publish_country_card(bot, country, thread_id, message_id).
                # Раньше сюда передавался id чата, и карточки уходили в общий чат.
                try:
                    msg_id = await publish_country_card(
                        self.bot, country, country_topic["thread_id"],
                        country.get("card_message_id"),
                        ctx["summary"], ctx["notifications"],
                        changes=ctx["changes"], days_ago=ctx["days_ago"],
                        updated_at=ctx["updated_at"],
                        prev_country=prev_country, next_country=next_country,
                    )
                except CardNotPublishable as skip:
                    # Страна неактивна/анрег/битая - карточку не трогаем вообще
                    log.info("страна %s: публикация пропущена (%s)",
                             country.get("name"), skip)
                    continue
                if msg_id is None:
                    # сообщение не найдено — чистим БД и повторим в следующем цикле
                    await self.db.set_country_card_message(country["name"], None)
                    await self.db.set_country_card_fingerprint(country["name"], "")
                    log.info("страна %s: сообщение не найдено, сброшен card_message_id", country.get("name"))
                elif msg_id:
                    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
                    await self.db.set_country_card_message(country["name"], msg_id)
                    await self.db.set_country_card_fingerprint(country["name"], fingerprint)
                    await self.db.set_country_card_published_at(country["name"], now_iso)
                    if ctx["changes"]:
                        cycle_updates[country["name"]] = ctx["changes"]
                    # Уровень 3: правителю — только про существенное, иначе
                    # он получит 20 сообщений за цикл и перестанет читать.
                    if notifier.collect(
                        _prev_snapshot(country), country,
                        link=self._card_link(country_topic["thread_id"], msg_id),
                    ):
                        log.debug("страна %s: изменение существенное", country["name"])
                    await asyncio.sleep(0.7)
            except Exception as exc:  # noqa: BLE001
                if _is_topic_closed(exc):
                    # тема закрыта/удалена — не долбимся в неё каждый цикл
                    _remember_closed_topic(country_topic["thread_id"], exc)
                    await self.db.set_country_card_message(country["name"], None)
                    await self.db.set_country_card_fingerprint(country["name"], "")
                    continue
                log.error("карточка страны %s: %s", country.get("name"), exc)
        log.info("карточки стран: проверено %d, обновлено с изменениями %d",
                 len(countries), len(cycle_updates))

        if faction_topic:
            if _topic_is_closed(faction_topic["thread_id"]):
                log.info("карточки группировок: тема %s закрыта, публикация пропущена",
                         faction_topic["thread_id"])
                return
            for faction in await self.db.list_factions():
                fingerprint = hashlib.sha256(
                    build_faction_card(faction).encode("utf-8")
                ).hexdigest()[:16]
                if fingerprint == faction.get("card_fingerprint"):
                    continue
                faction_published_at = faction.get("card_published_at")
                if not force and faction_published_at:
                    try:
                        last_dt = datetime.fromisoformat(str(faction_published_at))
                        if last_dt.tzinfo is None:
                            last_dt = last_dt.replace(tzinfo=timezone.utc)
                        elapsed_h = (datetime.now(timezone.utc) - last_dt).total_seconds() / 3600.0
                    except ValueError:
                        elapsed_h = float("inf")
                    if elapsed_h < config.card_publish_interval_hours:
                        continue
                try:
                    # ВАЖНО: publish_faction_card(bot, faction, thread_id, message_id)
                    msg_id = await publish_faction_card(
                        self.bot, faction, faction_topic["thread_id"],
                        faction.get("card_message_id"),
                    )
                    if msg_id is None:
                        await self.db.set_faction_card_message(faction["full_name"], None)
                        await self.db.set_faction_card_fingerprint(faction["full_name"], "")
                        log.info("группировка %s: сообщение не найдено, сброшен card_message_id", faction.get("full_name"))
                    elif msg_id:
                        now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
                        await self.db.set_faction_card_message(
                            faction["full_name"], msg_id)
                        await self.db.set_faction_card_fingerprint(
                            faction["full_name"], fingerprint)
                        await self.db.set_faction_card_published_at(
                            faction["full_name"], now_iso)
                        await asyncio.sleep(0.7)
                except Exception as exc:  # noqa: BLE001
                    if _is_topic_closed(exc):
                        _remember_closed_topic(faction_topic["thread_id"], exc)
                        await self.db.set_faction_card_message(faction["full_name"], None)
                        await self.db.set_faction_card_fingerprint(faction["full_name"], "")
                        continue
                    log.error("карточка группировки %s: %s",
                              faction.get("full_name"), exc)
            log.info("карточки группировок: проверено %d",
                     len(await self.db.list_factions()))

        # Уровни 2-4 (раздел 10). Вызываем в конце: к этому моменту известно,
        # что именно реально обновилось, а не что запрашивалось.
        await self._notify_cycle(cycle_updates, notifier, country_thread)

    async def count_published_cards(self) -> int:
        """Число карточек стран/группировок, у которых уже есть сообщение."""
        countries = await self.db.list_countries()
        factions = await self.db.list_factions()
        return sum(1 for c in countries if c.get("card_message_id")) + \
            sum(1 for f in factions if f.get("card_message_id"))

    @staticmethod
    def _parse_dt(value: str) -> datetime:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return datetime.now(timezone.utc) - timedelta(days=365)

    @staticmethod
    def _is_registration_text(text: str) -> bool:
        low = text.lower()
        if any(w in low for w in ("анкета", "регистраци", "новое государство", "вступаю в страну")):
            return True
        if re.search(r"бюджет\s*[:—]?\s*\d", low):
            return True
        return False

    @staticmethod
    def _detect_registration_budget(text: str) -> float | None:
        """Извлечь стартовый бюджет из анкеты / сообщения о регистрации."""
        if not text:
            return None
        low = text.lower()
        # ближайшие к «бюджет» числа: «бюджет 75к», «бюджет 75000», «бюджет — 60000 ВР»
        m = re.search(
            r"бюджет[^:—\n]{0,25}?:?\s*[«\"']?[-—]?\s*(\d[\d\s.,]{1,8})\s*(тыс|к|руб|рублей|вр)?",
            low,
        )
        if not m:
            return None
        raw = m.group(1).replace(" ", "").replace(",", "")
        multiplier = 1000.0 if (m.group(2) or "").startswith(("тыс", "к")) else 1.0
        try:
            return float(raw) * multiplier
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _last_msg_date(msgs: list[dict[str, Any]]) -> str:
        """Самая поздняя (лексикографически) дата из списка сообщений."""
        now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
        if not msgs:
            return now_iso
        last = msgs[-1].get("date")
        for m in reversed(msgs):
            d = m.get("date") or ""
            if d > (last or ""):
                last = d
        return last or now_iso

    async def _backfill_registration_dates(self) -> None:
        """Разовый пересчёт дат регистрации: для стран, сидированных «сегодня»
        (временная дата из _seed_registration), найти самое раннее упоминание в
        архиве новостей/анкет и проставить его как registered_at.

        Флаг в meta registration_dates_backfilled не даёт пересчитывать повторно.
        """
        if await self.db.get_meta("registration_dates_backfilled") == "1":
            return
        chat_id = config.target_chat_id
        today_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")

        countries = await self.db.list_countries()
        alias_map: dict[str, str] = {}
        for c in countries:
            alias_map[c["name"].lower()] = c["name"]
            for a in c.get("aliases_json") or []:
                alias_map[str(a).strip().lower()] = c["name"]

        topics = await self.db.list_topics()
        topic_ids = [
            t["thread_id"] for t in topics
            if t["purpose"] in ("новости", "расширения", "анкеты")
            or not t["purpose"]
        ]
        if not topic_ids:
            topic_ids = list(config.news_topics.values())

        # Самая ранняя дата упоминания по каждой стране (каноническое имя).
        earliest: dict[str, str] = {}
        for thread_id in topic_ids:
            if thread_id is None:
                continue
            cursor: int | None = None
            while True:
                msgs = await self._read_topic_messages(
                    chat_id, thread_id, limit=config.max_messages_per_topic,
                    offset_id=cursor,
                )
                if not msgs:
                    break
                for m in msgs:
                    mdate = m.get("date") or ""
                    if not mdate:
                        continue
                    text_low = (m.get("text") or "").lower()
                    for key, canonical in alias_map.items():
                        if key and (key in text_low or f"_{key}_" in text_low):
                            if canonical not in earliest or mdate < earliest[canonical]:
                                earliest[canonical] = mdate
                ids = [int(m["id"]) for m in msgs if m.get("id")]
                if not ids:
                    break
                cursor = min(ids) - 1
                if cursor <= 0:
                    break

        fixed = 0
        for c in countries:
            name = c.get("name") or ""
            if not name:
                continue
            cur_reg = c.get("registered_at") or ""
            # Только страны с «временной» датой (сид сегодня) либо без даты.
            seeded_today = cur_reg[:10] == today_iso[:10] or not cur_reg
            best = earliest.get(name)
            if not best:
                continue
            if not seeded_today and cur_reg and best >= cur_reg:
                continue
            await self.db.set_country_registered_at(name, best)
            fixed += 1
            log.info("дата регистрации %s: %s (было %s)", name, best[:10], (cur_reg or "—")[:10])

        await self.db.set_meta("registration_dates_backfilled", "1")
        log.info("регистрационные даты пересчитаны: обновлено %d стран", fixed)

    async def _reseed_economy_from_registration(self) -> None:
        """Разово пересобрать стартовые события хроники с даты регистрации.

        Проблема: события economy_events сидируются на «сегодня» (день создания
        страны), а backfill_registration_dates переносит registered_at в прошлое
        (июль). Из-за этого ledger_budget считает доходы только с даты события,
        и казна выглядит так, будто страна зарегистрировалась «вчера».

        Здесь мы удаляем все события страны и пересоздаём их с event_date =
        registered_at, а также заполняем industry_json дефолтной промышленностью,
        если она пуста. Флаг в meta econ_seed_reseeded не даёт пересобрать повторно.
        """
        if await self.db.get_meta("econ_seed_reseeded") == "1":
            return
        fixed = 0
        for country in await self.db.list_countries():
            name = country.get("name") or ""
            reg = country.get("registered_at")
            cid = country.get("id")
            if not reg or not cid:
                continue
            # Дефолтная промышленность (1 воензавод + 2 фабрики), если пуста
            if not country.get("industry_json"):
                country["industry_json"] = list(Mechanic._DEFAULT_INDUSTRY)
                await self.db.upsert_country({"name": name, "industry_json": list(Mechanic._DEFAULT_INDUSTRY)})
            await self.db.delete_economy_events(cid)
            await self._seed_registration(country, msgs=[], force_event_date=reg)
            fixed += 1
            log.info("экономика %s: стартовые события пересобраны с %s", name, (reg or "")[:10])
        await self.db.set_meta("econ_seed_reseeded", "1")
        log.info("экономика: стартовые события пересобраны у %d стран (дата регистрации)", fixed)

    _SEED_EVENT_NOTES = (
        "регистрация", "базовый доход",
        "фабрики (старт", "военные заводы (старт",
        "население (старт", "армия (старт",
    )

    async def _repair_economy_v2(self) -> int:
        """Одноразовое исправление казны/армии live-БД (econ_v2).

        Раньше economy_events сидировались повторно (по 2–4 стартовых набора
        на страну), из-за чего казна «раздувалась» (ledger суммировал все
        наборы), а часть стран не имела событий вовсе. Плюс техника могла
        превышать мобилизационный потолок. Здесь:

          1. оставляем по одному стартовому набору на страну;
          2. до-сидируем страны без хроники (дата события = registered_at);
          3. жёстко урезаем армию до потолка и нормализуем вместимость;
          4. пересчитываем казну накопительно от текущего потока:
             старт + (доход−расход)×дней + разовые операции с регистрации;
          5. сбрасываем prev_*, чтобы первая карточка не показывала ложный скачок.

        Флаг meta econ_v2_repaired не даёт запускаться повторно.
        """
        if await self.db.get_meta("econ_v2_repaired") == "1":
            return 0
        now = datetime.now(timezone.utc)
        seed_notes = self._SEED_EVENT_NOTES
        fixed = 0
        for c in await self.db.list_countries():
            name = c.get("name") or ""
            cid = c.get("id")
            if not cid:
                continue
            events = await self.db.list_economy_events(cid)

            # 1) дедуп стартовых наборов
            groups: dict[str, list[dict[str, Any]]] = {}
            for e in events:
                note = str(e.get("note") or "")
                if any(s in note for s in seed_notes):
                    groups.setdefault(str(e.get("event_date")), []).append(e)
            if len(groups) > 1:
                keep = self._canonical_seed_date(
                    list(groups.keys()), c.get("registered_at")
                )
                drop = [
                    e["id"] for d, evs in groups.items()
                    if d != keep for e in evs if e.get("id")
                ]
                if drop:
                    await self.db.delete_economy_event_ids(drop)
                    fixed += 1
                events = await self.db.list_economy_events(cid)

            # 2) до-сидровка стран без хроники
            if not any(e.get("kind") == "registration" for e in events):
                await self.db.delete_economy_events(cid)
                await self._seed_registration(
                    c, msgs=[], force_event_date=c.get("registered_at")
                )
                events = await self.db.list_economy_events(cid)
                fixed += 1

            # 3) армия до потолка, нормализация вместимости (hours=0 — только
            #    кламп/пересчёт, время не «течёт»: население/стабильность не трогаем)
            econ = compute_country_economy(
                c, 0.0, events=events, now=now,
                declared=await self._declared_stats(c),
            )

            # 4) казна накопительно от текущего потока с даты регистрации
            reg_dt = parse_event_date(c.get("registered_at")) or now
            days = max((now - reg_dt).total_seconds() / 86400.0, 0.0)
            one_offs = 0.0
            for e in events:
                if e.get("rate") or str(e.get("kind") or "").lower() == "registration":
                    continue
                ea = parse_event_date(e.get("event_date"))
                if ea is not None and reg_dt < ea <= now:
                    one_offs += float(e.get("amount") or 0.0)
            income = float(econ.get("income_daily") or 0.0)
            expenses = army_expenses(econ) + float(econ.get("population") or 0.0) * 2.0
            start_budget = _num_sched(c.get("start_budget"))
            if start_budget < 5000 or start_budget > 1_000_000:
                start_budget = _num_sched(c.get("budget"), 75_000.0)
            budget = start_budget + (income - expenses) * days + one_offs

            # 5) prev_* = новые значения, чтобы первая карточка без ложного скачка
            updated: dict[str, Any] = {"name": name, **econ, "budget": round(budget, 1)}
            for key in (
                "population", "budget", "army_infantry", "army_light_vehicles",
                "army_heavy_wheeled", "army_artillery", "army_drones", "army_navy",
                "stability", "war_support", "happiness",
            ):
                updated[f"prev_{key}"] = updated.get(key)
            updated["last_econ_at"] = now.isoformat(timespec="seconds")
            await self.db.upsert_country(updated)

        await self.db.set_meta("econ_v2_repaired", "1")
        log.info("экономика v2: обойдено %d стран (строевая/казна), флаг установлен",
                 len(await self.db.list_countries()))
        return fixed

    @staticmethod
    def _canonical_seed_date(dates: list[str], registered_at: Any) -> str:
        """Какая дата стартового набора канонична (ближайшая к registered_at)."""
        if not dates:
            return ""
        target = parse_event_date(registered_at)
        if target is None:
            return min(dates)
        valid = [(d, p) for d in dates if (p := parse_event_date(d)) is not None]
        if not valid:
            return min(dates)
        return min(valid, key=lambda dp: abs((dp[1] - target).total_seconds()))[0]

    async def _seed_registration(
        self,
        country: dict[str, Any],
        msgs: list[dict[str, Any]] | None = None,
        *,
        force_event_date: str | None = None,
        start_budget_override: float | None = None,
    ) -> None:
        """Записать стартовые события хроники для страны, если ещё не зарегистрирована.
        
        Ищет дату регистрации в новостях (анкету/объявление), иначе — самое раннее упоминание.
        Добавляет дефолтную промышленность (2 гражданские фабрики + 1 военный завод) в хронику.
        force_event_date — принудительно зафиксировать стартовые события на эту дату
        (для пересборки хроники под backfill-дату регистрации).
        """
        if country.get("registered_at") and not force_event_date:
            return
        msgs = msgs or []
        cid = country.get("id")
        if not cid:
            name = country.get("name", "")
            loaded = await self.db.get_country(name)
            cid = loaded["id"] if loaded else None
            if not cid:
                return

        # Ищем сообщение о регистрации (анкета/объявление/бюджет)
        reg_msg = next((m for m in msgs if self._is_registration_text(m.get("text") or "")), None)
        
        # Дата регистрации: анкета → первое упоминание → крайний случай now()
        if force_event_date:
            event_date = force_event_date
        elif reg_msg:
            event_date = reg_msg.get("date")
        elif msgs:
            # самое старое сообщение страны
            event_date = min((m.get("date") for m in msgs if m.get("date")), default=None)
        else:
            event_date = None

        if not event_date:
            event_date = datetime.now(timezone.utc).isoformat(timespec="seconds")

        text_for_budget = (reg_msg or {}).get("text") or ""
        detected = self._detect_registration_budget(text_for_budget)
        start_budget = (
            start_budget_override
            if start_budget_override is not None
            else (detected or (_num_sched(country.get("budget")) or 75_000.0))
        )
        if (
            not isinstance(start_budget, (int, float))
            or (isinstance(start_budget, float) and math.isnan(start_budget))
            or start_budget < 0
            or start_budget > 1_000_000
        ):
            start_budget = 75_000.0

        # стартовый капитал
        await self.db.add_economy_event(cid, event_date, "registration", start_budget, rate=False, note="регистрация")
        # базовый доход
        await self.db.add_economy_event(cid, event_date, "base", BASE_INCOME_DAILY, rate=True, note="базовый доход")
        
        # дефолтная промышленность: 2 гражданские фабрики (+1000 ВР/день) + 1 военный завод
        # если в industry_json уже есть — используем то, что есть
        industry = country.get("industry_json") or []
        factory_count = count_factories(industry)
        port_count = count_ports(industry)
        # гражданские фабрики для дохода: минимум 2
        default_factories = max(factory_count, 2)
        # военные заводы НЕ дают дохода — они производят технику
        default_military = sum(1 for item in industry if isinstance(item, dict) and "военн" in str(item.get("name", "")).lower())
        default_military = max(default_military, 1)  # минимум 1 в.завод
        
        if default_factories:
            await self.db.add_economy_event(
                cid, event_date, "factory", FACTORY_INCOME * default_factories, rate=True,
                note=f"фабрики (старт {default_factories})",
            )
        # в.заводы в хронику не добавляем как доход — они только производят технику
        if default_military:
            await self.db.add_economy_event(
                cid, event_date, "military_factory", 0, rate=True,
                note=f"военные заводы (старт {default_military})",
            )

        # стартовое население (net вклад в бюджет)
        tax_level = (country.get("tax_level") or "").lower()
        tax = 1.0
        for k, v in TAX_PER_CAPITA.items():
            if k in tax_level:
                tax = v
                break
        pop = int(country.get("population") or 0)
        if pop:
            citizens_rate = (tax - 2.0) * float(pop)
            await self.db.add_economy_event(
                cid, event_date, "citizens", citizens_rate, rate=True,
                note=f"население (старт {pop})",
            )
        soldiers = int(country.get("army_infantry") or 0)
        if soldiers:
            await self.db.add_economy_event(
                cid, event_date, "soldiers", -5.0 * soldiers, rate=True,
                note=f"армия (старт {soldiers})",
            )
        await self.db.set_country_registration(
            country.get("name", ""), event_date, float(start_budget)
        )
        log.info("хроника: страна %s зарегистрирована %s (старт %d ВР, фабрик %d, в.заводов %d)",
                 country.get("name"), event_date, int(start_budget), default_factories, default_military)

    @staticmethod
    def _extract_cash_ops(text: str) -> list[tuple[float, str, str]]:
        """Детерминированно извлечь разовые операции (траты/награды) из текста."""
        if not text:
            return []
        out: list[tuple[float, str, str]] = []
        num_pat = r"(\d(?:[\d\s.,.]{0,8}))\s*(к|тыс|руб|вр|в\.\s*р)?"
        # траты: «закупили/потратили/выложили/... N(к) (ВР)»
        spend_prefix = (
            r"(?:закуп(?:и(?:л|ла|ли)?)|потрат(?:и(?:л|ла|ли)?)|"
            r"вылож(?:и(?:л|ла|ли)?)|заплат(?:и(?:л|ла|ли)?)|"
            r"обош(?:л(?:ось|ась)|лось)|стоило|сожг(?:л|ла|ли))\b"
        )
        # награды/доход: «награда 5к ВР», «получили 10000», «выделено 30к»
        grant_prefix = (
            r"(?:наград[аыиу]|награжд[а-я]*|преми[аию]|"
            r"поступ(?:и(?:л|ла|ли)?)|выдел(?:и(?:л|ла|ли)?|ено|ена)|"
            r"субсиди[аию]|финасиров[а-я]{2,3})\b"
        )
        for prefix, sign in ((spend_prefix, -1.0), (grant_prefix, 1.0)):
            for m in re.finditer(
                rf"{prefix}[^\n]{{0,60}}?{num_pat}", text, re.IGNORECASE
            ):
                raw = m.group(1).replace(" ", "").replace(",", "").replace(".", "")
                mul = 1000.0 if (m.group(2) or "").lower().startswith(("к", "тыс")) else 1.0
                try:
                    amt = float(raw) * mul * sign
                except (TypeError, ValueError):
                    continue
                if abs(amt) > 1_000_000 or abs(amt) < 100:
                    continue
                out.append((amt, "cost" if sign < 0 else "grant",
                            "траты" if sign < 0 else "награда/доход"))
        # штраф
        for m in re.finditer(rf"штраф[а-я]*\b[^\n]{{0,40}}?{num_pat}", text, re.IGNORECASE):
            raw = m.group(1).replace(" ", "").replace(",", "").replace(".", "")
            mul = 1000.0 if (m.group(2) or "").lower().startswith(("к", "тыс")) else 1.0
            try:
                amt = -float(raw) * mul
            except (TypeError, ValueError):
                continue
            if 100 <= abs(amt) <= 1_000_000:
                out.append((amt, "cost", "штраф"))
        return out

    async def _record_action_events(
        self,
        country: dict[str, Any],
        old: dict[str, Any],
        msgs: list[dict[str, Any]],
    ) -> None:
        """Записать в хронику бюджета последствия изменений из последних новостей.

        Для каждого типа действий (фабрики, порты, население, армия, траты)
        создаётся датированное событие со ставкой ВР/день или абсолютной суммой.
        UNIQUE(country_id, event_date, kind, amount, note) гарантирует
        отсутствие повторных начислений (дедуп по сообщениям уже есть).
        """
        cid = country.get("id")
        if not cid:
            return
        event_date = self._last_msg_date(msgs)

        # --- промышленность (фабрики/заводы ±500 ВР/день, порты ±300 ВР/день) ---
        def factories(industry: Any) -> int:
            return count_factories(industry)
        def ports(industry: Any) -> int:
            return count_ports(industry)

        old_f, old_p = factories(old.get("industry_json")), ports(old.get("industry_json"))
        new_f, new_p = factories(country.get("industry_json")), ports(country.get("industry_json"))
        for delta, kind, amount, label in (
            (new_f - old_f, "factory",  FACTORY_INCOME, "фабрика"),
            (new_p - old_p, "port",     PORT_INCOME,    "порт"),
        ):
            if delta == 0:
                continue
            for i in range(abs(delta)):
                rate = amount if delta > 0 else -amount
                await self.db.add_economy_event(
                    cid, event_date, kind, rate, rate=True,
                    note=f"{label} #{i+1}",
                )

        # --- население (net: налог - содержание × число изменённых граждан) ---
        def _tax_rate(name: Any) -> float:
            low = (name or "").lower()
            for k, v in TAX_PER_CAPITA.items():
                if k in low:
                    return v
            return 1.0
        tax_rate = _tax_rate(country.get("tax_level"))
        delta_pop = (int(country.get("population") or 0) - int(old.get("population") or 0))
        # защита от глюков AI: лимит дельты населения за цикл
        if delta_pop > 50000:
            log.warning("%s: подозрительный рост населения %d, лимитирую до 50000", country.get("name"), delta_pop)
            delta_pop = 50000
        elif delta_pop < -50000:
            log.warning("%s: подозрительное падение населения %d, лимитирую до -50000", country.get("name"), delta_pop)
            delta_pop = -50000
        if delta_pop:
            amount = (tax_rate - 2.0) * float(delta_pop)
            await self.db.add_economy_event(
                cid, event_date, "citizens", amount, rate=True,
                note=f"население {delta_pop:+d}",
            )
        # --- налог (если ставка изменилась: скорректировать вклад на текущее население) ---
        if (country.get("tax_level") or "").lower() != (old.get("tax_level") or "").lower():
            pop_at_now = int(country.get("population") or 0)
            adj = (_tax_rate(country.get("tax_level")) - _tax_rate(old.get("tax_level"))) * float(pop_at_now)
            if adj:
                await self.db.add_economy_event(
                    cid, event_date, "citizens", adj, rate=True,
                    note="изменение налоговой ставки",
                )
        # --- армия: пехота (±5 ВР/день) ---
        ds = (int(country.get("army_infantry") or 0) - int(old.get("army_infantry") or 0))
        # защита от глюков AI
        if ds > 50000:
            log.warning("%s: подозрительный рост армии %d, лимитирую до 50000", country.get("name"), ds)
            ds = 50000
        elif ds < -50000:
            log.warning("%s: подозрительное падение армии %d, лимитирую до -50000", country.get("name"), ds)
            ds = -50000
        if ds:
            await self.db.add_economy_event(
                cid, event_date, "soldiers", -5.0 * ds, rate=True,
                note=f"армия {ds:+d}",
            )
        # --- разовые операции из текста новостей ---
        for m in msgs:
            for amount, kind, label in self._extract_cash_ops(m.get("text") or ""):
                if kind in ("cost", "grant"):
                    await self.db.add_economy_event(
                        cid, m.get("date") or event_date, kind, amount, rate=False,
                        note=label,
                    )
        log.debug("хроника %s: записаны события за %s", country.get("name"), event_date)
