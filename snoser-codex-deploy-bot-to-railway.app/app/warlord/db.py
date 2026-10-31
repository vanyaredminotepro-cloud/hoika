"""
SQLite-хранилище Warlord RP.

Схема строго соответствует разделу 6 ТЗ:
    countries, factions, country_factions, persons,
    topics, known_entities, pending_news, questions_log

Все методы асинхронные (aiosqlite). Сериализация JSON-полей выполняется
на уровне storage (данные наружу отдаются уже как dict/list).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

import aiosqlite

from .config import config

_SCHEMA = """
CREATE TABLE IF NOT EXISTS countries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    flag_file_id TEXT,
    ruler TEXT,
    ruler_user_id INTEGER,
    party TEXT,
    ideology TEXT,
    color TEXT,
    population INTEGER DEFAULT 0,
    population_capacity INTEGER DEFAULT 0,
    budget REAL DEFAULT 0,
    income_daily REAL DEFAULT 0,
    expenses_daily REAL DEFAULT 0,
    army_infantry INTEGER DEFAULT 0,
    army_light_vehicles INTEGER DEFAULT 0,
    army_heavy_wheeled INTEGER DEFAULT 0,
    army_artillery INTEGER DEFAULT 0,
    army_drones INTEGER DEFAULT 0,
    army_navy INTEGER DEFAULT 0,
    tax_level TEXT DEFAULT 'Средний',
    stability REAL DEFAULT 70,
    war_support REAL DEFAULT 50,
    happiness REAL DEFAULT 50,
    resources_json TEXT DEFAULT '[]',
    settlements_json TEXT DEFAULT '[]',
    industry_json TEXT DEFAULT '[]',
    research_json TEXT DEFAULT '[]',
    card_message_id INTEGER,
    updated_at TEXT,
    prev_population INTEGER DEFAULT 0,
    prev_budget REAL DEFAULT 0,
    prev_army_infantry INTEGER DEFAULT 0,
    prev_army_light_vehicles INTEGER DEFAULT 0,
    prev_army_heavy_wheeled INTEGER DEFAULT 0,
    prev_army_artillery INTEGER DEFAULT 0,
    prev_army_drones INTEGER DEFAULT 0,
    prev_army_navy INTEGER DEFAULT 0,
    prev_stability REAL DEFAULT 0,
    prev_war_support REAL DEFAULT 0,
    prev_happiness REAL DEFAULT 0,
    last_econ_at TEXT,
    summary TEXT,
    notifications_json TEXT DEFAULT '[]',
    status TEXT DEFAULT 'active',
    aliases_json TEXT DEFAULT '[]',
    registered_at TEXT,
    start_budget REAL DEFAULT 75000,
    card_fingerprint TEXT
);

CREATE TABLE IF NOT EXISTS factions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    prefix TEXT,
    full_name TEXT UNIQUE NOT NULL,
    emblem_file_id TEXT,
    commander TEXT,
    depends_on_country TEXT,
    base_country TEXT,
    relation_to_country TEXT DEFAULT 'нейтрал',
    strength INTEGER DEFAULT 5,
    equipment_json TEXT DEFAULT '[]',
    budget REAL DEFAULT 0,
    tasks TEXT,
    contracts_json TEXT DEFAULT '[]',
    wars_json TEXT DEFAULT '[]',
    reputation INTEGER DEFAULT 50,
    prev_reputation REAL DEFAULT 0,
    card_message_id INTEGER,
    updated_at TEXT,
    prev_budget REAL DEFAULT 0,
    prev_strength INTEGER DEFAULT 0,
    last_econ_at TEXT,
    card_fingerprint TEXT
);

CREATE TABLE IF NOT EXISTS economy_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    country_id INTEGER NOT NULL,
    event_date TEXT NOT NULL,
    kind TEXT NOT NULL,
    amount REAL NOT NULL,
    rate INTEGER DEFAULT 1,
    note TEXT DEFAULT '',
    UNIQUE (country_id, event_date, kind, amount, note)
);

CREATE TABLE IF NOT EXISTS researched_news (
    country_id INTEGER NOT NULL,
    chat_id INTEGER NOT NULL,
    thread_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    researched_at TEXT,
    PRIMARY KEY (country_id, chat_id, thread_id, message_id)
);
CREATE INDEX IF NOT EXISTS idx_researched_news_msg
    ON researched_news (chat_id, thread_id, message_id);

CREATE TABLE IF NOT EXISTS researched_faction_news (
    faction_id INTEGER NOT NULL,
    chat_id INTEGER NOT NULL,
    thread_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    researched_at TEXT,
    PRIMARY KEY (faction_id, chat_id, thread_id, message_id)
);
CREATE INDEX IF NOT EXISTS idx_researched_faction_news_msg
    ON researched_faction_news (chat_id, thread_id, message_id);

CREATE TABLE IF NOT EXISTS country_factions (
    country_id INTEGER NOT NULL,
    faction_id INTEGER NOT NULL,
    status TEXT DEFAULT 'базируется',
    PRIMARY KEY (country_id, faction_id)
);

CREATE TABLE IF NOT EXISTS persons (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    country TEXT,
    activity TEXT,
    job TEXT,
    money REAL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS topics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    thread_id INTEGER NOT NULL,
    purpose TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS known_entities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL,
    name TEXT NOT NULL,
    status TEXT DEFAULT 'active',
    note TEXT DEFAULT '',
    asked INTEGER DEFAULT 0,
    UNIQUE (type, name)
);

CREATE TABLE IF NOT EXISTS pending_news (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    country_name TEXT,
    text TEXT NOT NULL,
    added_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS questions_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    question TEXT NOT NULL,
    answer TEXT DEFAULT '',
    asked_at TEXT NOT NULL,
    telegram_message_id INTEGER
);

CREATE TABLE IF NOT EXISTS messages_log (
    chat_id INTEGER NOT NULL,
    thread_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    text TEXT DEFAULT '',
    added_at TEXT NOT NULL,
    PRIMARY KEY (chat_id, thread_id, message_id)
);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS ai_cache (
    cache_key TEXT PRIMARY KEY,
    result TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- Полный аудит изменений: БД -> apply_event -> validator -> БД.
-- Без него невозможно ни откатить показатель, ни восстановить население
-- после бага (см. БАГ 1 в ТЗ).
CREATE TABLE IF NOT EXISTS changes_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    country TEXT,
    field TEXT,
    old_value TEXT,
    new_value TEXT,
    reason TEXT,
    message_id INTEGER,
    timestamp TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_changes_country ON changes_log(country);
CREATE INDEX IF NOT EXISTS idx_changes_field ON changes_log(country, field);

-- Джейлбрейки и оффтоп. Отдельная от questions_log: сюда пишем даже когда
-- до LLM запрос не дошёл.
CREATE TABLE IF NOT EXISTS security_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    username TEXT,
    text TEXT,
    reason TEXT,
    timestamp TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_security_ts ON security_log(timestamp);

CREATE TABLE IF NOT EXISTS error_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    module TEXT,
    error TEXT,
    traceback TEXT,
    timestamp TEXT NOT NULL
);

-- Учёт токенов: отчёт админу раз в день (п.8 ТЗ).
CREATE TABLE IF NOT EXISTS token_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    model TEXT,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    cached INTEGER DEFAULT 0,
    timestamp TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_token_ts ON token_usage(timestamp);

-- Вопросы админу задаются ОДИН РАЗ, ответ запоминается (п.3, п.11 ТЗ).
CREATE TABLE IF NOT EXISTS admin_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    question TEXT,
    answer TEXT,
    asked INTEGER DEFAULT 0,
    timestamp TEXT NOT NULL
);
-- UNIQUE нужен для INSERT ... ON CONFLICT(question) в answer_admin_request:
-- без него SQLite ругается «ON CONFLICT clause does not match any ... constraint».
CREATE UNIQUE INDEX IF NOT EXISTS idx_admin_requests_q ON admin_requests(question);

-- Смена статусов активности: inactive/afk/neutral + возврат (п.6 ТЗ).
CREATE TABLE IF NOT EXISTS activity_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    country TEXT,
    old_status TEXT,
    new_status TEXT,
    reason TEXT,
    timestamp TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_activity_country ON activity_log(country);
CREATE TABLE IF NOT EXISTS mutes (
    user_id INTEGER PRIMARY KEY,
    until TEXT NOT NULL,
    reason TEXT DEFAULT '',
    timestamp TEXT
);
"""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _since_iso(minutes: int) -> str:
    """ISO-метка «N минут назад» — для запросов вида timestamp >= ?."""
    return (datetime.now() - timedelta(minutes=minutes)).isoformat(timespec="seconds")


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _loads(value: Any, default: Any) -> Any:
    if not value:
        return default
    if isinstance(value, (list, dict)):
        # уже разобранное значение (например, из _country_from_row)
        return value
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


_COUNTRY_JSON_FIELDS = (
    "resources_json",
    "settlements_json",
    "industry_json",
    "research_json",
    "notifications_json",
    "aliases_json",
)
_FACTION_JSON_FIELDS = ("equipment_json", "contracts_json", "wars_json")

# Колонки, добавляемые миграцией на существующие БД
_COUNTRY_ECON_COLUMNS: dict[str, str] = {
    "prev_population": "INTEGER DEFAULT 0",
    "prev_budget": "REAL DEFAULT 0",
    "prev_army_infantry": "INTEGER DEFAULT 0",
    "prev_army_light_vehicles": "INTEGER DEFAULT 0",
    "prev_army_heavy_wheeled": "INTEGER DEFAULT 0",
    "prev_army_artillery": "INTEGER DEFAULT 0",
    "prev_army_drones": "INTEGER DEFAULT 0",
    "prev_army_navy": "INTEGER DEFAULT 0",
    "prev_stability": "REAL DEFAULT 0",
    "prev_war_support": "REAL DEFAULT 0",
    "prev_happiness": "REAL DEFAULT 0",
    "last_econ_at": "TEXT",
    "summary": "TEXT",
    "notifications_json": "TEXT DEFAULT '[]'",
    "status": "TEXT DEFAULT 'active'",
    "aliases_json": "TEXT DEFAULT '[]'",
    "registered_at": "TEXT",
    "start_budget": "REAL DEFAULT 75000",
    "card_fingerprint": "TEXT",
    "last_activity_at": "TEXT",
    "card_published_at": "TEXT",
    # --- Модуль CARDS: разделы 4, 5, 6 -------------------------------------
    # Источник стартовых данных: "manual" (раздел 5) или "anketa" (раздел 6).
    "stats_source": "TEXT DEFAULT 'anketa'",
    # ОЯТ и ТХ-999 — только Паониция (разделы 2, 5, 13).
    "ojat": "INTEGER DEFAULT 0",
    "tx999": "INTEGER DEFAULT 0",
    # Столица и населённые пункты из анкеты (раздел 6).
    "capital": "TEXT",
    # Правитель: tg-id для личного уведомления (раздел 10, уровень 3).
    "ruler_chat_id": "INTEGER",
    # Ссылка на канал страны (раздел 6).
    "channel_url": "TEXT",
    # Регистрирована ли страна админом (п.8, not_accepted -> принята).
    "accepted_at": "TEXT",
}
_FACTION_ECON_COLUMNS: dict[str, str] = {
    "prev_budget": "REAL DEFAULT 0",
    "prev_strength": "INTEGER DEFAULT 0",
    "prev_reputation": "REAL DEFAULT 0",
    "last_econ_at": "TEXT",
    "card_fingerprint": "TEXT",
    "card_published_at": "TEXT",
}


class Database:
    def __init__(self, path: str | None = None) -> None:
        self.path = path or str(config.db_path)
        self._db: aiosqlite.Connection | None = None

    async def init(self) -> None:
        """Открыть соединение и создать схему.

        Повторный вызов (init/connect) ничего не делает: без этой проверки
        старый aiosqlite-поток и соединение оставались жить, а процесс
        не завершался. Идемпотентность важна для скриптов, которые зовут
        connect(), а потом init() (миграция на копии БД, тесты).
        """
        if self._db is not None:
            return
        import os
        os.makedirs(str(config.db_path.parent), exist_ok=True)
        self._db = await aiosqlite.connect(self.path)
        self._db.row_factory = aiosqlite.Row
        # WAL-режим + таймаут на блокировку — решает "database is locked"
        await self._db.execute("PRAGMA journal_mode=WAL;")
        await self._db.execute("PRAGMA busy_timeout=30000;")  # 30 сек ожидания
        await self._db.executescript(_SCHEMA)
        await self._migrate()
        await self._db.commit()

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    async def connect(self) -> None:
        """Alias for init() for backward compatibility."""
        await self.init()

    async def _ensure_column(self, table: str, column: str, ddl: str) -> None:
        rows = await self._fetchall(f"PRAGMA table_info({table})")
        if not any(r["name"] == column for r in rows):
            await self._execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")

    async def _migrate(self) -> None:
        for col, ddl in _COUNTRY_ECON_COLUMNS.items():
            await self._ensure_column("countries", col, ddl)
        for col, ddl in _FACTION_ECON_COLUMNS.items():
            await self._ensure_column("factions", col, ddl)
        await self._ensure_column("questions_log", "applied", "INTEGER DEFAULT 0")
        # last_post_at — по нему считается активность (п.6 ТЗ). last_activity_at
        # означает «бот что-то делал со страной», а не «страна писала в теме».
        await self._ensure_column("countries", "last_post_at", "TEXT")
        # TG-идентификатор правителя: позволяет /afk и /unafk только правителю
        # самой страны (или админу), а не любому желающему.
        await self._ensure_column("countries", "ruler_user_id", "INTEGER")
        # message_id в pending_news нужен для дедупа новостей: без него один и
        # тот же пост применится дважды (БАГ 2).
        await self._ensure_column("pending_news", "message_id", "INTEGER")
        # --- Модуль CARDS -----------------------------------------------------
        # Полный текст мануальной статистики (раздел 5) хранится отдельно от
        # колонок countries: он богаче, чем то, что показывает карточка, и нужен
        # как источник истины при проверках и для ручного вмешательства.
        await self._execute(
            """CREATE TABLE IF NOT EXISTS manual_stats (
                country TEXT PRIMARY KEY,
                payload_json TEXT NOT NULL DEFAULT '{}',
                raw_text TEXT DEFAULT '',
                seeded_at TEXT NOT NULL
            )"""
        )
        # Счётчики для ежедневного отчёта админу (раздел 10, уровень 4).
        await self._execute(
            """CREATE TABLE IF NOT EXISTS card_cycle_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ran_at TEXT NOT NULL,
                countries_updated INTEGER DEFAULT 0,
                changes_total INTEGER DEFAULT 0,
                rollbacks INTEGER DEFAULT 0,
                errors INTEGER DEFAULT 0,
                deleted TEXT DEFAULT ''
            )"""
        )
        await self._ensure_column("card_cycle_log", "deleted", "TEXT DEFAULT ''")
        # Замороженные админом значения (редактор стран): экономическая симуляция
        # не перезаписывает число, пока на него есть блокировка. Это чинит «числа
        # после редактора не сохраняются» — правка держится, а не стирается
        # следующим циклом.
        await self._execute(
            """CREATE TABLE IF NOT EXISTS country_admin_overrides (
                country_id INTEGER NOT NULL,
                field TEXT NOT NULL,
                value REAL NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (country_id, field)
            )"""
        )
        # Гистология ежедневных отчётов, чтобы не слать два отчёта за сутки.
        await self._execute(
            """CREATE TABLE IF NOT EXISTS card_reports (
                report_date TEXT PRIMARY KEY,
                sent_at TEXT NOT NULL
            )"""
        )

    # ------------------------------------------------------------------ helpers
    async def _fetchone(self, sql: str, params: tuple = ()) -> aiosqlite.Row | None:
        async with self._db.execute(sql, params) as cur:
            return await cur.fetchone()

    async def _fetchall(self, sql: str, params: tuple = ()) -> list[aiosqlite.Row]:
        async with self._db.execute(sql, params) as cur:
            return list(await cur.fetchall())

    async def fetchone(self, sql: str, params: tuple = ()) -> dict[str, Any] | None:
        row = await self._fetchone(sql, params)
        return dict(row) if row else None

    async def fetchall(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        rows = await self._fetchall(sql, params)
        return [dict(r) for r in rows]

    async def _execute(self, sql: str, params: tuple = ()) -> int:
        """Выполнить SQL. Возвращает число затронутых строк (нужно purge_orphans)."""
        cur = await self._db.execute(sql, params)
        await self._db.commit()
        return int(cur.rowcount or 0)

    @staticmethod
    def _country_from_row(row: aiosqlite.Row) -> dict[str, Any]:
        data = dict(row)
        for key in _COUNTRY_JSON_FIELDS:
            data[key] = _loads(data.get(key), [])
        return data

    @staticmethod
    def _faction_from_row(row: aiosqlite.Row) -> dict[str, Any]:
        data = dict(row)
        for key in _FACTION_JSON_FIELDS:
            data[key] = _loads(data.get(key), [])
        return data

    # ------------------------------------------------------------------ countries
    async def upsert_country(self, country: dict[str, Any]) -> int:
        """Создать или обновить страну по name. Возвращает id."""
        name = str(country.get("name") or "").strip()
        if not name:
            raise ValueError("country.name is required")

        # aliases_json: в ON CONFLICT стояло countries.aliases_json, из-за чего
        # алиасы нельзя было обновить никогда. Присваивать excluded нельзя —
        # частичный патч ({"name": ..., "industry_json": [...]}) стёр бы их.
        # Поэтому объединяем старые и новые значения здесь, в Python — до дампа
        # в JSON-строку, иначе в data["aliases_json"] уже лежит строка.
        merged_aliases: list[str] | None = None
        existing_row = await self._fetchone(
            "SELECT aliases_json FROM countries WHERE name=?", (name,)
        )
        if existing_row is not None:
            old_aliases = _loads(existing_row["aliases_json"], []) or []
            merged_aliases = list(old_aliases)
            for alias in (_loads(country.get("aliases_json"), []) or []):
                if alias and alias not in merged_aliases and alias != name:
                    merged_aliases.append(alias)

        data = dict(country)
        for key in _COUNTRY_JSON_FIELDS:
            if key == "aliases_json" and merged_aliases is not None:
                # именно JSON-строка: параметры собираются через str(data[...]),
                # и str(список) дал бы Python-repr вроде "[' alias']"
                data[key] = _dumps(merged_aliases)
            else:
                data[key] = _dumps(data.get(key) or [])

        data.setdefault("flag_file_id", None)
        data.setdefault("ruler", None)
        data.setdefault("party", None)
        data.setdefault("ideology", None)
        data.setdefault("color", None)
        data.setdefault("population", 0)
        data.setdefault("population_capacity", 0)
        data.setdefault("budget", 0.0)
        data.setdefault("income_daily", 0.0)
        data.setdefault("expenses_daily", 0.0)
        data.setdefault("army_infantry", 0)
        data.setdefault("army_light_vehicles", 0)
        data.setdefault("army_heavy_wheeled", 0)
        data.setdefault("army_artillery", 0)
        data.setdefault("army_drones", 0)
        data.setdefault("army_navy", 0)
        data.setdefault("tax_level", "Средний")
        data.setdefault("stability", 70.0)
        data.setdefault("war_support", 50.0)
        data.setdefault("happiness", 50.0)
        data.setdefault("card_message_id", None)
        data.setdefault("updated_at", None)
        data.setdefault("prev_population", 0)
        data.setdefault("prev_budget", 0.0)
        data.setdefault("prev_army_infantry", 0)
        data.setdefault("prev_army_light_vehicles", 0)
        data.setdefault("prev_army_heavy_wheeled", 0)
        data.setdefault("prev_army_artillery", 0)
        data.setdefault("prev_army_drones", 0)
        data.setdefault("prev_army_navy", 0)
        data.setdefault("prev_stability", 0.0)
        data.setdefault("prev_war_support", 0.0)
        data.setdefault("prev_happiness", 0.0)
        data.setdefault("last_econ_at", None)
        data.setdefault("summary", None)
        data.setdefault("notifications_json", [])
        data.setdefault("status", "active")
        data.setdefault("aliases_json", [])
        data.setdefault("registered_at", None)
        data.setdefault("start_budget", None)
        data.setdefault("last_activity_at", None)

        columns = (
            "name, flag_file_id, ruler, ruler_user_id, party, ideology, color, population, "
            "population_capacity, budget, income_daily, expenses_daily, "
            "army_infantry, army_light_vehicles, army_heavy_wheeled, army_artillery, "
            "army_drones, army_navy, tax_level, stability, war_support, happiness, "
            "resources_json, settlements_json, industry_json, research_json, "
            "prev_population, prev_budget, prev_army_infantry, prev_army_light_vehicles, "
            "prev_army_heavy_wheeled, prev_army_artillery, prev_army_drones, "
            "prev_army_navy, prev_stability, prev_war_support, prev_happiness, "
            "last_econ_at, summary, notifications_json, status, aliases_json, "
            "registered_at, start_budget, last_activity_at, "
            "card_message_id, updated_at, accepted_at"
        )
        placeholders = ", ".join("?" for _ in columns.split(", "))
        params = tuple(
            None if data.get(field.strip()) is None else str(data.get(field.strip()))
            for field in columns.split(",")
        )

        await self._db.execute(
            f"INSERT INTO countries ({columns}) VALUES ({placeholders}) "
            f"ON CONFLICT(name) DO UPDATE SET "
            f"flag_file_id=excluded.flag_file_id, "
            f"ruler=excluded.ruler, "
            f"ruler_user_id=COALESCE(excluded.ruler_user_id, countries.ruler_user_id), "
            f"party=excluded.party, ideology=excluded.ideology, "
            f"color=excluded.color, population=excluded.population, "
            f"population_capacity=excluded.population_capacity, budget=excluded.budget, "
            f"income_daily=excluded.income_daily, expenses_daily=excluded.expenses_daily, "
            f"army_infantry=excluded.army_infantry, "
            f"army_light_vehicles=excluded.army_light_vehicles, "
            f"army_heavy_wheeled=excluded.army_heavy_wheeled, "
            f"army_artillery=excluded.army_artillery, army_drones=excluded.army_drones, "
            f"army_navy=excluded.army_navy, tax_level=excluded.tax_level, "
            f"stability=excluded.stability, war_support=excluded.war_support, "
            f"happiness=excluded.happiness, resources_json=excluded.resources_json, "
            f"settlements_json=excluded.settlements_json, "
            f"industry_json=excluded.industry_json, research_json=excluded.research_json, "
            f"prev_population=excluded.prev_population, prev_budget=excluded.prev_budget, "
            f"prev_army_infantry=excluded.prev_army_infantry, "
            f"prev_army_light_vehicles=excluded.prev_army_light_vehicles, "
            f"prev_army_heavy_wheeled=excluded.prev_army_heavy_wheeled, "
            f"prev_army_artillery=excluded.prev_army_artillery, "
            f"prev_army_drones=excluded.prev_army_drones, "
            f"prev_army_navy=excluded.prev_army_navy, "
            f"prev_stability=excluded.prev_stability, "
            f"prev_war_support=excluded.prev_war_support, "
            f"prev_happiness=excluded.prev_happiness, "
            f"last_econ_at=excluded.last_econ_at, summary=excluded.summary, "
            f"notifications_json=excluded.notifications_json, "
            f"status=countries.status, "
            f"aliases_json=excluded.aliases_json, "
            f"registered_at=CASE WHEN excluded.registered_at IS NULL "
            f"THEN countries.registered_at ELSE excluded.registered_at END, "
            f"start_budget=CASE WHEN excluded.start_budget IS NULL "
            f"THEN countries.start_budget ELSE excluded.start_budget END, "
            f"last_activity_at=CASE WHEN excluded.last_activity_at IS NULL "
            f"THEN countries.last_activity_at ELSE excluded.last_activity_at END, "
            f"card_message_id=CASE WHEN excluded.card_message_id IS NULL "
            f"THEN countries.card_message_id ELSE excluded.card_message_id END, "
            f"updated_at=CASE WHEN excluded.updated_at IS NULL "
            f"THEN countries.updated_at ELSE excluded.updated_at END, "
            f"accepted_at=excluded.accepted_at",
            params,
        )
        await self._db.commit()

        row = await self._fetchone("SELECT id FROM countries WHERE name=?", (name,))
        return int(row["id"]) if row else 0

    async def get_country(self, name: str) -> dict[str, Any] | None:
        row = await self._fetchone("SELECT * FROM countries WHERE name=?", (name,))
        return self._country_from_row(row) if row else None

    async def get_country_by_id(self, country_id: int) -> dict[str, Any] | None:
        row = await self._fetchone("SELECT * FROM countries WHERE id=?", (country_id,))
        return self._country_from_row(row) if row else None

    async def get_country_by_ruler(self, user_id: int) -> dict[str, Any] | None:
        """Страна, которой управляет TG-пользователь (по ruler_user_id)."""
        row = await self._fetchone(
            "SELECT * FROM countries WHERE ruler_user_id=? LIMIT 1",
            (int(user_id),))
        return self._country_from_row(row) if row else None

    async def apply_country_debuff(
        self, country_id: int, *,
        budget: float = 0.0,
        stability: float = 0.0,
        happiness: float = 0.0,
        war_support: float = 0.0,
    ) -> None:
        """Одноразовый штраф стране за нарушение правил (джейлбрейк и т.п.).

        Стабильность/счастье/поддержка зажимаются в 1..100; казну нельзя
        выгнать в минус сверх имеющейся. Числа действуют как откатный
        дискомфорт: экономика не обнуляет их в следующем цикле.
        """
        c = await self.get_country_by_id(int(country_id))
        if not c:
            return

        def _num(value: Any, default: float = 0.0) -> float:
            try:
                return float(value)
            except (TypeError, ValueError):
                return default

        def _clamp_pct(value: Any, default: float) -> float:
            return max(1.0, min(100.0, round(_num(value, default) + 0.0, 1)))

        new_budget = max(0.0, round(_num(c.get("budget")) + budget, 1))
        new_stability = _clamp_pct(_num(c.get("stability"), 70.0) + stability, 70.0)
        new_happiness = _clamp_pct(_num(c.get("happiness"), 50.0) + happiness, 50.0)
        new_war_support = _clamp_pct(_num(c.get("war_support"), 50.0) + war_support, 50.0)
        await self._execute(
            "UPDATE countries SET budget=?, stability=?, happiness=?, war_support=? "
            "WHERE id=?",
            (new_budget, new_stability, new_happiness, new_war_support, int(country_id)),
        )

    async def list_countries(self, status: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM countries"
        params = ()
        if status:
            sql += " WHERE status=?"
            params = (status,)
        sql += " ORDER BY name"
        rows = await self._fetchall(sql, params)
        return [self._country_from_row(r) for r in rows]

    async def set_country_status(self, name: str, status: str) -> None:
        """Пометить страну статусом: active / unreg / archive."""
        await self._execute(
            "UPDATE countries SET status=? WHERE name=?", (status, name)
        )

    async def set_country_registration(
        self, name: str, registered_at: str, start_budget: float
    ) -> None:
        """Записать факт регистрации страны: дата вступления + стартовый бюджет."""
        await self._execute(
            "UPDATE countries SET registered_at=?, start_budget=? WHERE name=?",
            (registered_at, float(start_budget), name),
        )

    async def set_country_registered_at(self, name: str, registered_at: str) -> None:
        """Только обновить дату регистрации (перенос к более раннему факту)."""
        await self._execute(
            "UPDATE countries SET registered_at=? WHERE name=?", (registered_at, name)
        )

    async def update_country_last_activity(self, name: str, value: str) -> None:
        """Обновить дату последней реально замеченной новости страны."""
        if not name or not value:
            return
        await self._execute(
            "UPDATE countries SET last_activity_at=? WHERE name=? AND "
            "(last_activity_at IS NULL OR last_activity_at < ?)",
            (value, name, value),
        )

    # ------------------------------------------------------------------ экономические события (хроника)
    async def add_economy_event(
        self,
        country_id: int,
        event_date: str,
        kind: str,
        amount: float,
        rate: bool = True,
        note: str = "",
    ) -> None:
        """Добавить событие в хронику страны.

        rate=True — ставка ВР/день, начисляется с event_date и до «сейчас».
        rate=False — разовое абсолютное изменение казны (трата/награда/старт).
        UNIQUE(country_id, event_date, kind, amount, note) защищает от дублей.
        """
        await self._execute(
            "INSERT OR IGNORE INTO economy_events "
            "(country_id, event_date, kind, amount, rate, note) VALUES (?,?,?,?,?,?)",
            (country_id, event_date, kind, float(amount), 1 if rate else 0, note),
        )

    async def list_economy_events(self, country_id: int) -> list[dict[str, Any]]:
        rows = await self._fetchall(
            "SELECT * FROM economy_events WHERE country_id=? ORDER BY event_date",
            (country_id,),
        )
        return [dict(r) for r in rows]

    async def delete_economy_events(self, country_id: int) -> None:
        await self._execute(
            "DELETE FROM economy_events WHERE country_id=?", (country_id,)
        )

    async def delete_economy_event_ids(self, ids: list[int]) -> int:
        """Удалить события по их id (для дедупликации хроники)."""
        if not ids:
            return 0
        cur = await self._db.execute(
            f"DELETE FROM economy_events WHERE id IN ({','.join('?' * len(ids))})",
            tuple(int(i) for i in ids),
        )
        return int(cur.rowcount or 0)

    # ------------------------------------------------------------------ исследованные новости
    async def mark_news_researched(
        self, country_id: int, messages: list[dict[str, Any]]
    ) -> None:
        """Пометить сообщения как исследованные для страны и обновить last_post_at."""
        if not country_id or not messages:
            return
        stamp = _now()
        latest_date = None
        for m in messages:
            mid = m.get("id")
            if mid is None:
                continue
            await self._db.execute(
                "INSERT OR IGNORE INTO researched_news "
                "(country_id, chat_id, thread_id, message_id, researched_at) "
                "VALUES (?,?,?,?,?)",
                (country_id, int(m.get("chat_id") or 0),
                 int(m.get("thread_id") or 0), int(mid), stamp),
            )
            # Track latest message date for last_post_at
            mdate = m.get("date")
            if mdate:
                try:
                    dt = datetime.fromisoformat(mdate.replace("Z", "+00:00"))
                    if latest_date is None or dt > latest_date:
                        latest_date = dt
                except Exception:
                    pass
        # Update country's last_post_at with latest message date
        if latest_date is not None:
            await self._db.execute(
                "UPDATE countries SET last_post_at=? WHERE id=?",
                (latest_date.isoformat(timespec="seconds"), country_id),
            )
        await self._db.commit()

    async def researched_message_ids(
        self, country_ids: list[int], chat_id: int, thread_id: int
    ) -> set[int]:
        """Сообщения темы, которые уже исследованы хотя бы для одной страны."""
        if not country_ids:
            return set()
        placeholders = ", ".join("?" for _ in country_ids)
        rows = await self._fetchall(
            "SELECT DISTINCT message_id FROM researched_news "
            f"WHERE chat_id=? AND thread_id=? AND country_id IN ({placeholders})",
            (chat_id, thread_id, *country_ids),
        )
        return {int(r["message_id"]) for r in rows}

    async def mark_faction_news_researched(
        self, faction_id: int, messages: list[dict[str, Any]]
    ) -> None:
        """Пометить, что сообщения отработаны для группировки."""
        if not faction_id or not messages:
            return
        stamp = _now()
        for m in messages:
            mid = m.get("id")
            if mid is None:
                continue
            await self._db.execute(
                "INSERT OR IGNORE INTO researched_faction_news "
                "(faction_id, chat_id, thread_id, message_id, researched_at) "
                "VALUES (?,?,?,?,?)",
                (faction_id, int(m.get("chat_id") or 0),
                 int(m.get("thread_id") or 0), int(mid), stamp),
            )
        await self._db.commit()

    async def faction_researched_message_ids(
        self, faction_ids: list[int], chat_id: int, thread_id: int
    ) -> set[int]:
        """Сообщения темы, уже исследованные хотя бы для одной группировки."""
        if not faction_ids:
            return set()
        placeholders = ", ".join("?" for _ in faction_ids)
        rows = await self._fetchall(
            "SELECT DISTINCT message_id FROM researched_faction_news "
            f"WHERE chat_id=? AND thread_id=? AND faction_id IN ({placeholders})",
            (chat_id, thread_id, *faction_ids),
        )
        return {int(r["message_id"]) for r in rows}

    async def rename_country(self, old: str, new: str) -> None:
        """Переименовать страну, сохранив id и данные, добавив старое имя в алиасы.

        Если имя `new` уже занято - это один и тот же объект под двумя
        названиями, поэтому используется dedupe_country (взять большее), а НЕ
        merge_countries (сложить): переименование «Священная Румская Империя»
        в «Румия» не должно давать население 1 + 54.
        """
        old_row = await self._fetchone("SELECT * FROM countries WHERE name=?", (old,))
        if old_row is None:
            return
        new = (new or "").strip()
        if not new or new.lower() == old.lower():
            return
        existing = await self._fetchone("SELECT * FROM countries WHERE name=?", (new,))
        if existing is not None:
            await self.dedupe_country(new, old)
            return
        aliases = list(_loads(old_row["aliases_json"], []) or [])
        if old not in aliases:
            aliases.append(old)
        await self._execute(
            "UPDATE countries SET name=?, aliases_json=?, updated_at=? WHERE name=?",
            (new, _dumps(aliases), _now(), old),
        )

    async def merge_countries(self, canonical: str, drop: str) -> int:
        """Слить страну `drop` в страну `canonical`. Возвращает id канонической."""
        row_c = await self._fetchone("SELECT * FROM countries WHERE name=?", (canonical,))
        row_d = await self._fetchone("SELECT * FROM countries WHERE name=?", (drop,))
        if row_c is None or row_d is None or canonical.lower() == drop.lower():
            return int(row_c["id"]) if row_c else 0

        c = self._country_from_row(row_c)
        d = self._country_from_row(row_d)

        merged = dict(c)
        for key in _COUNTRY_JSON_FIELDS:
            vals = list(c.get(key) or []) + [
                x for x in (d.get(key) or []) if x not in (c.get(key) or [])
            ]
            merged[key] = vals
        merged["aliases_json"] = list(c["aliases_json"]) + [drop]
        scalar_defaults = {
            "ruler": "", "party": "", "ideology": "", "color": "", "flag_file_id": "",
            "tax_level": "", "summary": "",
        }
        for key, default in scalar_defaults.items():
            if not c.get(key) and d.get(key):
                merged[key] = d[key]
        if not c.get("card_message_id") and d.get("card_message_id"):
            merged["card_message_id"] = d["card_message_id"]
        if not c.get("updated_at") and d.get("updated_at"):
            merged["updated_at"] = d["updated_at"]
        for key in ("population", "population_capacity", "budget",
                    "army_infantry", "army_light_vehicles", "army_heavy_wheeled",
                    "army_artillery", "army_drones", "army_navy", "stability",
                    "war_support", "happiness", "income_daily", "expenses_daily"):
            try:
                cv, dv = float(c.get(key) or 0), float(d.get(key) or 0)
            except (TypeError, ValueError):
                continue
            merged[key] = cv + dv

        cid = int(row_c["id"])
        did = int(row_d["id"])
        # перебросить привязки группировок, хронику бюджета и исследованные
        # новости от сброшенной страны к канонической
        await self._execute(
            "UPDATE OR IGNORE country_factions SET country_id=? WHERE country_id=?",
            (cid, did),
        )
        await self._execute(
            "UPDATE OR IGNORE economy_events SET country_id=? WHERE country_id=?",
            (cid, did),
        )
        await self._execute(
            "UPDATE OR IGNORE researched_news SET country_id=? WHERE country_id=?",
            (cid, did),
        )
        await self._execute("DELETE FROM countries WHERE id=?", (did,))
        await self.upsert_country(merged)
        return cid

    async def dedupe_country(self, canonical: str, drop: str) -> int:
        """Слить ДУБЛЬ той же страны в каноническую запись.

        Отличие от `merge_countries`: числовые показатели не суммируются.
        Там объединяются две разные страны, здесь — две записи одной и той же
        страны под разными написаниями («Фаустланд» и «Фаустланд (Faustland)»),
        где население 60 + 1 = 61 было бы ерундой.
        """
        row_c = await self._fetchone("SELECT * FROM countries WHERE name=?", (canonical,))
        row_d = await self._fetchone("SELECT * FROM countries WHERE name=?", (drop,))
        if row_c is None or row_d is None or canonical.lower() == drop.lower():
            return int(row_c["id"]) if row_c else 0

        c = self._country_from_row(row_c)
        d = self._country_from_row(row_d)

        merged = dict(c)
        for key in _COUNTRY_JSON_FIELDS:
            vals = list(c.get(key) or []) + [
                x for x in (d.get(key) or []) if x not in (c.get(key) or [])
            ]
            merged[key] = vals
        merged["aliases_json"] = list(c["aliases_json"]) + [drop]

        for key in ("ruler", "party", "ideology", "color", "flag_file_id",
                    "tax_level", "summary"):
            if not (c.get(key) or "").strip() and (d.get(key) or "").strip():
                merged[key] = d[key]

        for key in ("population", "population_capacity", "budget",
                    "army_infantry", "army_light_vehicles", "army_heavy_wheeled",
                    "army_artillery", "army_drones", "army_navy",
                    "stability", "war_support", "happiness",
                    "income_daily", "expenses_daily"):
            # берём большее значение, а не сумму: это одна и та же страна
            merged[key] = max(float(c.get(key) or 0), float(d.get(key) or 0))

        if not c.get("card_message_id") and d.get("card_message_id"):
            merged["card_message_id"] = d["card_message_id"]
        for key in ("registered_at", "last_activity_at", "card_published_at"):
            if not c.get(key) and d.get(key):
                merged[key] = d[key]
        if (d.get("updated_at") or "") > (c.get("updated_at") or ""):
            merged["updated_at"] = d["updated_at"]

        cid, did = int(row_c["id"]), int(row_d["id"])
        await self._execute(
            "UPDATE OR IGNORE country_factions SET country_id=? WHERE country_id=?",
            (cid, did),
        )
        await self._execute(
            "UPDATE OR IGNORE economy_events SET country_id=? WHERE country_id=?",
            (cid, did),
        )
        await self._execute(
            "UPDATE OR IGNORE researched_news SET country_id=? WHERE country_id=?",
            (cid, did),
        )
        await self._execute("DELETE FROM countries WHERE id=?", (did,))
        await self.upsert_country(merged)
        return cid

    async def set_country_card_message(self, name: str, message_id: int) -> None:
        await self._execute(
            "UPDATE countries SET card_message_id=?, updated_at=? WHERE name=?",
            (message_id, _now(), name),
        )

    async def set_country_card_fingerprint(self, name: str, fingerprint: str) -> None:
        await self._execute(
            "UPDATE countries SET card_fingerprint=? WHERE name=?",
            (fingerprint, name),
        )

    async def set_country_card_published_at(self, name: str, published_at: str) -> None:
        await self._execute(
            "UPDATE countries SET card_published_at=? WHERE name=?",
            (published_at, name),
        )

    async def set_country_updated_at(self, name: str, updated_at: str) -> None:
        """Обновить мету времени правки страны (ЗОБНАВЛЕНО на карточке)."""
        await self._execute(
            "UPDATE countries SET updated_at=? WHERE name=?",
            (updated_at, name),
        )

    async def delete_country(self, name: str) -> None:
        """Удалить страну по имени вместе со всеми связанными записями.

        Раньше здесь чистились только economy_events, из-за чего после удаления
        оставались осиротевшие researched_news и country_factions.
        """
        row = await self._fetchone("SELECT id FROM countries WHERE name=?", (name,))
        if row is not None:
            await self.delete_country_by_id(int(row["id"]))
        else:
            await self._execute("DELETE FROM countries WHERE name=?", (name,))

    async def delete_country_by_id(self, country_id: int) -> None:
        await self.delete_economy_events(country_id)
        await self.delete_country_factions(country_id)
        await self._execute(
            "DELETE FROM researched_news WHERE country_id=?", (country_id,)
        )
        await self._execute("DELETE FROM countries WHERE id=?", (country_id,))

    async def set_country_flag(self, name: str, flag_file_id: str) -> None:
        """Сохранить bot file_id флага страны (для показа в карточке)."""
        await self._execute(
            "UPDATE countries SET flag_file_id=? WHERE name=?", (flag_file_id, name)
        )

    async def set_country_ruler_user(self, name: str, user_id: int | None) -> None:
        """Запомнить TG-идентификатор правителя страны (для прав на /afk)."""
        await self._execute(
            "UPDATE countries SET ruler_user_id=? WHERE name=?", (user_id, name)
        )

    # Числовые поля+страны, которые администратор может править из админ-панели.
    _ADMIN_EDITABLE_COUNTRY_FIELDS: frozenset[str] = frozenset({
        "population", "population_capacity", "army_infantry", "budget",
        "income_daily", "expenses_daily", "stability", "happiness", "war_support",
        "army_light_vehicles", "army_heavy_wheeled", "army_artillery",
        "army_drones", "army_navy",
    })

    # Текстовые поля страны, которые администратор может править построчно.
    _ADMIN_TEXT_COUNTRY_FIELDS: frozenset[str] = frozenset({
        "ruler", "party", "ideology", "color", "tax_level", "status",
    })

    async def set_country_value(self, country_id: int, field: str, value: float) -> None:
        """Точечно обновить одно числовое поле страны (белый список)."""
        if field not in self._ADMIN_EDITABLE_COUNTRY_FIELDS:
            raise ValueError(f"field not editable: {field}")
        await self._execute(
            f"UPDATE countries SET {field}=? WHERE id=?", (value, country_id)
        )

    async def set_country_text_field(
        self, country_id: int, field: str, value: str
    ) -> None:
        """Обновить текстовое поле страны (белый список)."""
        if field not in self._ADMIN_TEXT_COUNTRY_FIELDS:
            raise ValueError(f"field not text-editable: {field}")
        await self._execute(
            f"UPDATE countries SET {field}=? WHERE id=?",
            (str(value).strip(), country_id),
        )

    # Списки страны, которые администратор может править поэлементно.
    _ADMIN_LIST_COUNTRY_FIELDS: frozenset[str] = frozenset({
        "settlements_json", "industry_json", "research_json", "resources_json",
    })

    async def add_country_list_item(
        self, country_id: int, list_field: str, name: str,
        *,
        item_type: str | None = None, count: int = 0,
    ) -> int:
        """Добавить/прибавить элемент списка страны. Возвращает итоговое число.

        Одинаковые имена суммируются (поставил 2 фабрики → +2), разные —
        добавляются отдельными строками.
        """
        if list_field not in self._ADMIN_LIST_COUNTRY_FIELDS:
            raise ValueError(f"field not a list: {list_field}")
        c = await self.get_country_by_id(int(country_id))
        if not c:
            raise ValueError("country not found")
        try:
            items = json.loads(c.get(list_field) or "[]")
        except (TypeError, ValueError):
            items = []
        if not isinstance(items, list):
            items = []
        lname = str(name).strip()
        if not lname:
            raise ValueError("empty name")
        total = 0
        for it in items:
            if not isinstance(it, dict):
                continue
            iname = str(it.get("name") or "").strip().lower()
            if iname == lname.lower():
                try:
                    total += int(it.get("value", it.get("count", 0)) or 0)
                except (TypeError, ValueError):
                    total += 0
        total += max(int(count), 0)
        replaced = False
        for idx, it in enumerate(items):
            if isinstance(it, dict) and str(it.get("name") or "").strip().lower() == lname.lower():
                items[idx] = {
                    "name": it.get("name") or lname,
                    "type": item_type or it.get("type") or "",
                    "value": total,
                }
                replaced = True
                break
        if not replaced:
            items.append({"name": lname, "type": item_type or "", "value": max(int(count), 0)})
        await self._execute(
            f"UPDATE countries SET {list_field}=? WHERE id=?",
            (json.dumps(items, ensure_ascii=False), country_id),
        )
        return total

    async def remove_country_list_item(
        self, country_id: int, list_field: str, name: str, *,
        count: int = 0,
    ) -> int:
        """Убрать элемент списка страны (по имени). count>0 — убрать N штук,
        иначе удалить запись целиком. Возвращает удалённое количество."""
        if list_field not in self._ADMIN_LIST_COUNTRY_FIELDS:
            raise ValueError(f"field not a list: {list_field}")
        c = await self.get_country_by_id(int(country_id))
        if not c:
            raise ValueError("country not found")
        try:
            items = json.loads(c.get(list_field) or "[]")
        except (TypeError, ValueError):
            items = []
        if not isinstance(items, list):
            items = []
        lname = str(name).strip().lower()
        removed = 0
        out: list[Any] = []
        for it in items:
            if isinstance(it, dict) and str(it.get("name") or "").strip().lower() == lname:
                try:
                    have = int(it.get("value", it.get("count", 0)) or 0)
                except (TypeError, ValueError):
                    have = 0
                if count > 0:
                    left = max(have - count, 0)
                    removed += min(count, have)
                    if left > 0:
                        out.append({**it, "value": left})
                    continue
                removed += have
                continue
            out.append(it)
        await self._execute(
            f"UPDATE countries SET {list_field}=? WHERE id=?",
            (json.dumps(out, ensure_ascii=False), country_id),
        )
        return removed

    # ------------------------------------------------------------------
    # Заблокированные админом значения (редактор стран). Число, на которое
    # есть блокировка, экономическая симуляция не перезаписывает.
    # ------------------------------------------------------------------
    async def get_country_overrides(self, country_id: int) -> dict[str, float]:
        """Админские фиксации поля -> значение (пустой dict = блокировок нет)."""
        rows = await self._fetchall(
            "SELECT field, value FROM country_admin_overrides "
            "WHERE country_id=?", (country_id,))
        return {r["field"]: r["value"] for r in rows}

    async def set_country_override(self, country_id: int, field: str, value: float) -> None:
        """Зафиксировать число за админом (или сдвинуть существующую фиксацию)."""
        await self._execute(
            "INSERT INTO country_admin_overrides (country_id, field, value, updated_at) "
            "VALUES (?,?,?,?) "
            "ON CONFLICT(country_id, field) DO UPDATE SET "
            "value=excluded.value, updated_at=excluded.updated_at",
            (country_id, field, value,
             datetime.now(timezone.utc).isoformat(timespec="seconds")),
        )

    async def clear_country_override(self, country_id: int, field: str) -> None:
        """Снять блокировку с одного поля: симуляция снова им управляет."""
        await self._execute(
            "DELETE FROM country_admin_overrides WHERE country_id=? AND field=?",
            (country_id, field),
        )

    async def clear_country_overrides(self, country_id: int) -> None:
        """Снять все блокировки страны целиком."""
        await self._execute(
            "DELETE FROM country_admin_overrides WHERE country_id=?",
            (country_id,))

    async def set_all_country_overrides(self) -> int:
        """Зафиксировать текущие показатели ВСЕХ стран (⭐ всем).

        Для каждой страны все правки из белого списка кладутся в блокировки.
        Возвращает число записанных блокировок.
        """
        countries = await self.list_countries()
        total = 0
        for c in countries:
            cid = int(c["id"])
            for field in self._ADMIN_EDITABLE_COUNTRY_FIELDS:
                try:
                    fv = float(c.get(field) or 0)
                except (TypeError, ValueError):
                    continue
                await self.set_country_override(cid, field, fv)
                total += 1
        return total

    async def clear_all_country_overrides(self) -> int:
        """Снять блокировки со всех стран. Возвращает число удалённых записей."""
        return await self._execute("DELETE FROM country_admin_overrides")

    async def delete_country_factions(self, country_id: int) -> None:
        """Удалить все связи страны с группировками."""
        await self._execute(
            "DELETE FROM country_factions WHERE country_id=?", (country_id,)
        )

    async def delete_faction_countries(self, faction_id: int) -> None:
        """Удалить все связи группировки со странами."""
        await self._execute(
            "DELETE FROM country_factions WHERE faction_id=?", (faction_id,)
        )

    # ------------------------------------------------------------------ factions
    async def upsert_faction(self, faction: dict[str, Any]) -> int:
        name = str(faction.get("full_name") or "").strip()
        if not name:
            raise ValueError("faction.full_name is required")

        data = dict(faction)
        for key in _FACTION_JSON_FIELDS:
            data[key] = _dumps(data.get(key) or [])
        data.setdefault("prefix", None)
        data.setdefault("emblem_file_id", None)
        data.setdefault("commander", None)
        data.setdefault("depends_on_country", None)
        data.setdefault("base_country", None)
        data.setdefault("relation_to_country", "нейтрал")
        data.setdefault("strength", 5)
        data.setdefault("budget", 0.0)
        data.setdefault("tasks", None)
        data.setdefault("reputation", 50)
        data.setdefault("card_message_id", None)
        data.setdefault("updated_at", None)
        data.setdefault("prev_budget", None)
        data.setdefault("prev_strength", None)
        data.setdefault("prev_reputation", None)
        data.setdefault("last_econ_at", None)

        columns = (
            "prefix, full_name, emblem_file_id, commander, depends_on_country, "
            "base_country, relation_to_country, strength, equipment_json, budget, "
            "tasks, contracts_json, wars_json, reputation, "
            "prev_budget, prev_strength, prev_reputation, last_econ_at, "
            "card_message_id, updated_at"
        )
        placeholders = ", ".join("?" for _ in columns.split(", "))
        params = tuple(
            None if data.get(field.strip()) is None else str(data.get(field.strip()))
            for field in columns.split(",")
        )

        await self._db.execute(
            f"INSERT INTO factions ({columns}) VALUES ({placeholders}) "
            f"ON CONFLICT(full_name) DO UPDATE SET "
            f"prefix=excluded.prefix, emblem_file_id=excluded.emblem_file_id, "
            f"commander=excluded.commander, depends_on_country=excluded.depends_on_country, "
            f"base_country=excluded.base_country, "
            f"relation_to_country=excluded.relation_to_country, strength=excluded.strength, "
            f"equipment_json=excluded.equipment_json, budget=excluded.budget, "
            f"tasks=excluded.tasks, contracts_json=excluded.contracts_json, "
            f"wars_json=excluded.wars_json, reputation=excluded.reputation, "
            f"prev_budget=excluded.prev_budget, prev_strength=excluded.prev_strength, "
            f"prev_reputation=excluded.prev_reputation, last_econ_at=excluded.last_econ_at, "
            f"card_message_id=CASE WHEN excluded.card_message_id IS NULL "
            f"THEN factions.card_message_id ELSE excluded.card_message_id END, "
            f"updated_at=CASE WHEN excluded.updated_at IS NULL "
            f"THEN factions.updated_at ELSE excluded.updated_at END",
            params,
        )
        await self._db.commit()

        row = await self._fetchone("SELECT id FROM factions WHERE full_name=?", (name,))
        return int(row["id"]) if row else 0

    async def get_faction(self, full_name: str) -> dict[str, Any] | None:
        row = await self._fetchone("SELECT * FROM factions WHERE full_name=?", (full_name,))
        return self._faction_from_row(row) if row else None

    async def get_faction_by_id(self, faction_id: int) -> dict[str, Any] | None:
        row = await self._fetchone("SELECT * FROM factions WHERE id=?", (faction_id,))
        return self._faction_from_row(row) if row else None

    async def list_factions(self) -> list[dict[str, Any]]:
        rows = await self._fetchall("SELECT * FROM factions ORDER BY full_name")
        return [self._faction_from_row(r) for r in rows]

    async def set_faction_card_message(self, full_name: str, message_id: int) -> None:
        await self._execute(
            "UPDATE factions SET card_message_id=?, updated_at=? WHERE full_name=?",
            (message_id, _now(), full_name),
        )

    async def set_faction_card_fingerprint(self, full_name: str, fingerprint: str) -> None:
        await self._execute(
            "UPDATE factions SET card_fingerprint=? WHERE full_name=?",
            (fingerprint, full_name),
        )

    async def set_faction_card_published_at(
        self, full_name: str, published_at: str
    ) -> None:
        await self._execute(
            "UPDATE factions SET card_published_at=? WHERE full_name=?",
            (published_at, full_name),
        )

    async def delete_faction(self, full_name: str) -> None:
        await self._execute("DELETE FROM factions WHERE full_name=?", (full_name,))

    async def delete_faction_by_id(self, faction_id: int) -> None:
        await self.delete_faction_countries(faction_id)
        await self._execute(
            "DELETE FROM researched_faction_news WHERE faction_id=?", (faction_id,)
        )
        await self._execute("DELETE FROM factions WHERE id=?", (faction_id,))

    # ------------------------------------------------------------------ country_factions
    async def bind_faction_to_country(
        self, country_id: int, faction_id: int, status: str = "базируется"
    ) -> None:
        await self._execute(
            "INSERT INTO country_factions (country_id, faction_id, status) "
            "VALUES (?, ?, ?) ON CONFLICT(country_id, faction_id) "
            "DO UPDATE SET status=excluded.status",
            (country_id, faction_id, status),
        )

    async def list_country_factions(self, country_id: int) -> list[dict[str, Any]]:
        rows = await self._fetchall(
            "SELECT f.*, cf.status AS bind_status FROM factions f "
            "JOIN country_factions cf ON f.id = cf.faction_id "
            "WHERE cf.country_id = ?",
            (country_id,),
        )
        return [self._faction_from_row(r) for r in rows]

    async def list_faction_countries(self, faction_id: int) -> list[dict[str, Any]]:
        rows = await self._fetchall(
            "SELECT c.* FROM countries c "
            "JOIN country_factions cf ON c.id = cf.country_id "
            "WHERE cf.faction_id = ?",
            (faction_id,),
        )
        return [self._country_from_row(r) for r in rows]

    async def list_faction_bindings(self, faction_id: int) -> list[dict[str, Any]]:
        """Связи группировки со странами: country_id + статус (для мерджа)."""
        rows = await self._fetchall(
            "SELECT country_id, status FROM country_factions WHERE faction_id=?",
            (faction_id,),
        )
        return [dict(r) for r in rows]

    async def unbind_faction_from_country(self, country_id: int, faction_id: int) -> None:
        await self._execute(
            "DELETE FROM country_factions WHERE country_id=? AND faction_id=?",
            (country_id, faction_id),
        )

    # ------------------------------------------------------------------ persons
    async def add_person(self, person: dict[str, Any]) -> int:
        name = str(person.get("name") or "").strip()
        if not name:
            return 0
        await self._execute(
            "INSERT INTO persons (name, country, activity, job, money) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                name,
                person.get("country") or "",
                person.get("activity") or "",
                person.get("job") or "",
                float(person.get("money") or 0),
            ),
        )
        row = await self._fetchone(
            "SELECT id FROM persons WHERE name=? ORDER BY id DESC", (name,)
        )
        return int(row["id"]) if row else 0

    async def list_persons(self) -> list[dict[str, Any]]:
        rows = await self._fetchall("SELECT * FROM persons ORDER BY name")
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------ topics
    async def set_topic(self, chat_id: int, thread_id: int, purpose: str) -> None:
        row = await self._fetchone(
            "SELECT id FROM topics WHERE chat_id=? AND thread_id=?",
            (chat_id, thread_id),
        )
        if row:
            await self._execute(
                "UPDATE topics SET purpose=? WHERE id=?", (purpose, row["id"])
            )
        else:
            await self._execute(
                "INSERT INTO topics (chat_id, thread_id, purpose) VALUES (?, ?, ?)",
                (chat_id, thread_id, purpose),
            )

    async def get_topic(self, purpose: str) -> dict[str, Any] | None:
        row = await self._fetchone(
            "SELECT * FROM topics WHERE purpose=? ORDER BY id DESC LIMIT 1", (purpose,)
        )
        return dict(row) if row else None

    async def list_topics(self) -> list[dict[str, Any]]:
        rows = await self._fetchall("SELECT * FROM topics ORDER BY purpose")
        return [dict(r) for r in rows]

    async def get_topics(self, purpose: str) -> list[dict[str, Any]]:
        rows = await self._fetchall(
            "SELECT * FROM topics WHERE purpose=? ORDER BY id ASC", (purpose,)
        )
        return [dict(r) for r in rows]

    async def clear_topics(self) -> None:
        await self._execute("DELETE FROM topics")

    # ------------------------------------------------------------------ known_entities
    async def add_known_entity(self, ent_type: str, name: str, note: str = "") -> int:
        """Добавить известную сущность (страна/группировка/организация)."""
        name = (name or "").strip()
        if not name:
            return 0
        await self._db.execute(
            "INSERT INTO known_entities (type, name, status, note) "
            "VALUES (?, ?, 'active', ?) "
            "ON CONFLICT(type, name) DO NOTHING",
            (ent_type, name, note),
        )
        await self._db.commit()
        row = await self._fetchone(
            "SELECT id FROM known_entities WHERE type=? AND name=?", (ent_type, name)
        )
        return int(row["id"]) if row else 0

    async def get_entity(self, ent_type: str, name: str) -> dict[str, Any] | None:
        row = await self._fetchone(
            "SELECT * FROM known_entities WHERE type=? AND name=?", (ent_type, name)
        )
        return dict(row) if row else None

    async def delete_entity(self, ent_type: str | None = None, name: str | None = None) -> None:
        """Удалить сущности (по типу и/или имени). Без фильтров — опасная операция."""
        conds: list[str] = []
        params: list[Any] = []
        if ent_type:
            conds.append("type=?")
            params.append(ent_type)
        if name:
            conds.append("name=?")
            params.append(name)
        if not conds:
            return
        sql = "DELETE FROM known_entities WHERE " + " AND ".join(conds)
        await self._execute(sql, tuple(params))

    async def set_entity_type(self, ent_id: int, new_type: str) -> None:
        """Изменить тип известной сущности (напр. unknown -> country/faction)."""
        await self._execute(
            "UPDATE known_entities SET type=? WHERE id=?", (new_type, ent_id)
        )

    async def list_entities(
        self, ent_type: str | None = None, only_unasked: bool = False
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM known_entities"
        conds: list[str] = []
        params: list[Any] = []
        if ent_type:
            conds.append("type=?")
            params.append(ent_type)
        if only_unasked:
            conds.append("asked=0")
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY name"
        rows = await self._fetchall(sql, tuple(params))
        return [dict(r) for r in rows]

    async def mark_entity_asked(self, ent_id: int) -> None:
        await self._execute(
            "UPDATE known_entities SET asked=1 WHERE id=?", (ent_id,)
        )

    async def update_entity_status(self, ent_id: int, status: str) -> None:
        await self._execute(
            "UPDATE known_entities SET status=? WHERE id=?", (status, ent_id)
        )

    # ------------------------------------------------------------------ pending_news
    async def add_pending_news(
        self, country_name: str | None, text: str, added_at: str | None = None,
        message_id: int | None = None,
    ) -> None:
        await self._execute(
            "INSERT INTO pending_news (country_name, text, added_at, message_id) "
            "VALUES (?, ?, ?, ?)",
            (country_name, text, added_at or _now(), message_id),
        )

    async def list_pending_news(self, country_name: str | None = None) -> list[dict[str, Any]]:
        if country_name:
            rows = await self._fetchall(
                "SELECT * FROM pending_news WHERE country_name=? ORDER BY added_at",
                (country_name,),
            )
        else:
            rows = await self._fetchall("SELECT * FROM pending_news ORDER BY added_at")
        return [dict(r) for r in rows]

    async def clear_pending_news(self) -> None:
        await self._execute("DELETE FROM pending_news")

    async def clear_pending_news_for(self, country_name: str) -> None:
        await self._execute(
            "DELETE FROM pending_news WHERE country_name=?", (country_name,)
        )

    # ------------------------------------------------------------------ questions_log
    async def add_question(
        self, question: str, telegram_message_id: int | None = None
    ) -> int:
        await self._execute(
            "INSERT INTO questions_log (question, asked_at, telegram_message_id, answer) "
            "VALUES (?, ?, ?, '')",
            (question, _now(), telegram_message_id),
        )
        row = await self._fetchone("SELECT id FROM questions_log ORDER BY id DESC LIMIT 1")
        return int(row["id"]) if row else 0

    async def answer_question(self, question_id: int, answer: str) -> None:
        await self._execute(
            "UPDATE questions_log SET answer=? WHERE id=?", (answer, question_id)
        )

    async def get_question_by_tg_message(self, telegram_message_id: int) -> dict[str, Any] | None:
        row = await self._fetchone(
            "SELECT * FROM questions_log WHERE telegram_message_id=? LIMIT 1",
            (telegram_message_id,),
        )
        return dict(row) if row else None

    async def had_asked(self, question: str) -> bool:
        row = await self._fetchone(
            "SELECT id FROM questions_log WHERE question=? LIMIT 1", (question,)
        )
        return row is not None

    async def list_questions(self) -> list[dict[str, Any]]:
        rows = await self._fetchall("SELECT * FROM questions_log ORDER BY asked_at DESC")
        return [dict(r) for r in rows]

    async def list_unapplied_answers(self) -> list[dict[str, Any]]:
        """Вопросы, на которые админ уже ответил, но ответ ещё не применён к карточке."""
        rows = await self._fetchall(
            "SELECT * FROM questions_log "
            "WHERE answer != '' AND (applied IS NULL OR applied = 0) "
            "ORDER BY asked_at ASC"
        )
        return [dict(r) for r in rows]

    async def mark_questions_applied(self, ids: list[int]) -> None:
        if not ids:
            return
        placeholders = ",".join("?" for _ in ids)
        await self._execute(
            f"UPDATE questions_log SET applied=1 WHERE id IN ({placeholders})",
            tuple(ids),
        )

    # ------------------------------------------------------------------ messages_log
    async def mark_message_seen(
        self, chat_id: int, thread_id: int, message_id: int, text: str
    ) -> None:
        """Записать обработанное сообщение (для дедупликации в циклах)."""
        await self._db.execute(
            "INSERT OR IGNORE INTO messages_log (chat_id, thread_id, message_id, text, added_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (chat_id, thread_id, message_id, text[:2000], _now()),
        )
        await self._db.commit()

    async def has_message_seen(self, chat_id: int, thread_id: int, message_id: int) -> bool:
        row = await self._fetchone(
            "SELECT 1 FROM messages_log WHERE chat_id=? AND thread_id=? AND message_id=?",
            (chat_id, thread_id, message_id),
        )
        return row is not None

    async def purge_messages_log(self, older_than_hours: float = 24) -> None:
        cutoff = (datetime.now() - timedelta(hours=older_than_hours)).isoformat(timespec="seconds")
        await self._execute(
            "DELETE FROM messages_log WHERE added_at < ?", (cutoff,)
        )

    # ------------------------------------------------------------------ meta
    async def set_meta(self, key: str, value: str) -> None:
        await self._db.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        await self._db.commit()

    async def get_meta(self, key: str, default: str = "") -> str:
        try:
            row = await self._fetchone("SELECT value FROM meta WHERE key=?", (key,))
            return str(row["value"]) if row else default
        except aiosqlite.OperationalError:
            return default

    # ------------------------------------------------------------------ ai_cache
    async def get_ai_cache(self, cache_key: str) -> dict[str, Any] | None:
        try:
            row = await self._fetchone("SELECT result FROM ai_cache WHERE cache_key=?", (cache_key,))
            return _loads(row["result"], None) if row else None
        except aiosqlite.OperationalError:
            return None

    async def set_ai_cache(self, cache_key: str, result: dict[str, Any]) -> None:
        await self._db.execute(
            "INSERT INTO ai_cache (cache_key, result, created_at) VALUES (?, ?, ?) "
            "ON CONFLICT(cache_key) DO UPDATE SET result=excluded.result, created_at=excluded.created_at",
            (cache_key, _dumps(result), _now()),
        )
        await self._db.commit()

    # ------------------------------------------------------------------ purge_orphans
    async def purge_orphans(self) -> dict[str, int]:
        """Удалить записи, ссылающиеся на несуществующие страны.

        Ссылки бывают двух видов: по country_id (economy_events, researched_news,
        country_factions) и по имени (pending_news.country_name, persons.country,
        known_entities). После удаления стран оба вида остаются в БД и мешают
        ассистенту («страна X» находится по новостям удалённой страны).
        """
        removed: dict[str, int] = {}
        for table in ("economy_events", "researched_news", "country_factions"):
            cur = await self._execute(
                f"DELETE FROM {table} WHERE country_id NOT IN "
                "(SELECT id FROM countries)"
            )
            removed[table] = cur

        names = [
            str(r["name"])
            for r in await self._fetchall("SELECT name FROM countries")
        ]
        if not names:
            for table, col in (("pending_news", "country_name"),
                               ("persons", "country")):
                removed[table] = await self._execute(
                    f"DELETE FROM {table} WHERE {col} IS NOT NULL AND {col} != ''"
                )
            removed["known_entities"] = await self._execute(
                "DELETE FROM known_entities WHERE type='country'"
            )
            return removed

        # Страны, которых больше нет, удаляем только если они не синоним
        # (aliases) живой страны - иначе потеряем историю переименований.
        marks = ",".join("?" for _ in names)
        removed["pending_news"] = await self._execute(
            f"DELETE FROM pending_news WHERE country_name IS NOT NULL "
            f"AND country_name NOT IN ({marks}) "
            f"AND country_name NOT IN ("
            f"  SELECT name FROM countries, json_each(countries.aliases_json)"
            f")",
            tuple(names),
        )
        removed["persons"] = await self._execute(
            f"DELETE FROM persons WHERE country IS NOT NULL AND country NOT IN ({marks})",
            tuple(names),
        )
        removed["known_entities"] = await self._execute(
            f"DELETE FROM known_entities WHERE type='country' "
            f"AND name NOT IN ({marks}) "
            f"AND name NOT IN ("
            f"  SELECT name FROM countries, json_each(countries.aliases_json)"
            f")",
            tuple(names),
        )
        return removed
    # Единственный способ доказать, откуда взялась цифра в карточке, и
    # единственный источник для отката (п.9 ТЗ: «Всё в changes_log»).
    async def log_change(
        self,
        country: str,
        field: str,
        old_value: Any,
        new_value: Any,
        reason: str = "",
        message_id: int | None = None,
    ) -> None:
        await self._execute(
            "INSERT INTO changes_log (country, field, old_value, new_value, reason, "
            "message_id, timestamp) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                country,
                field,
                None if old_value is None else str(old_value),
                None if new_value is None else str(new_value),
                reason,
                message_id,
                _now(),
            ),
        )

    async def list_changes(self, country: str, field: str | None = None,
                           limit: int = 100) -> list[dict[str, Any]]:
        if field:
            rows = await self._fetchall(
                "SELECT * FROM changes_log WHERE country=? AND field=? "
                "ORDER BY timestamp DESC LIMIT ?",
                (country, field, limit),
            )
        else:
            rows = await self._fetchall(
                "SELECT * FROM changes_log WHERE country=? ORDER BY timestamp DESC LIMIT ?",
                (country, limit),
            )
        return [dict(r) for r in rows]

    # Последнее ненулевое значение поля: нужно для БАГ 1, чтобы восстановить
    # население, которое обнулилось.
    async def last_change_value(self, country: str, field: str) -> Any | None:
        rows = await self._fetchall(
            "SELECT new_value FROM changes_log WHERE country=? AND field=? "
            "AND new_value IS NOT NULL ORDER BY timestamp DESC LIMIT 5",
            (country, field),
        )
        for r in rows:
            try:
                val = float(r["new_value"])
            except (TypeError, ValueError):
                continue
            if val > 0:
                return r["new_value"]
        return None

    async def change_applied_for_message(self, country: str, message_id: int) -> bool:
        """Дедуп: не применять одно событие дважды по одному message_id (БАГ 2)."""
        row = await self._fetchone(
"SELECT 1 FROM changes_log WHERE country=? AND message_id=? LIMIT 1",
            (country, message_id),
        )
        return row is not None

    async def mobilization_applied_for_message(
        self, country: str, message_id: int
    ) -> bool:
        """Мобилизация из этого сообщения уже засчитана?

        Отдельный метод, а не change_applied_for_message: тот ищет ЛЮБУЮ
        запись changes_log с этим message_id, и пост, который уже изменил
        бюджет, считался бы «обработанным» - солдаты при этом могли бы
        начисляться повторно.
        """
        row = await self._fetchone(
            "SELECT 1 FROM changes_log WHERE country=? AND message_id=? "
            "AND reason LIKE 'mobilization:%' LIMIT 1",
            (country, message_id),
        )
        return row is not None

    # ------------------------------------------------------------------ security_log
    async def log_security(
        self, user_id: int | None, username: str | None, text: str, reason: str
    ) -> None:
        await self._execute(
            "INSERT INTO security_log (user_id, username, text, reason, timestamp) "
            "VALUES (?, ?, ?, ?, ?)",
            (user_id, username, text[:1000], reason, _now()),
        )

    async def count_security_since(self, reason: str, minutes: int) -> int:
        row = await self._fetchone(
            "SELECT COUNT(*) n FROM security_log WHERE reason=? AND timestamp >= ?",
            (reason, _since_iso(minutes)),
        )
        return int(row["n"]) if row else 0

    # ------------------------------------------------------------------ error_log
    async def log_error(self, module: str, error: str, tb: str = "") -> None:
        await self._execute(
            "INSERT INTO error_log (module, error, traceback, timestamp) "
            "VALUES (?, ?, ?, ?)",
            (module, error[:500], tb[:4000], _now()),
        )

    # ------------------------------------------------------------------ token_usage
    async def log_tokens(
        self, model: str, input_tokens: int, output_tokens: int, cached: bool = False
    ) -> None:
        await self._execute(
            "INSERT INTO token_usage (model, input_tokens, output_tokens, cached, "
            "timestamp) VALUES (?, ?, ?, ?, ?)",
            (model, int(input_tokens), int(output_tokens), int(bool(cached)), _now()),
        )

    # ------------------------------------------------------------------ mutes
    async def mute_user(self, user_id: int, until: str, reason: str = "") -> None:
        await self._execute(
            "INSERT INTO mutes (user_id, until, reason, timestamp) VALUES (?,?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET until=excluded.until, "
            "reason=excluded.reason, timestamp=excluded.timestamp",
            (int(user_id), until, reason, _now()),
        )

    async def unmute_user(self, user_id: int) -> bool:
        cur = await self._execute("DELETE FROM mutes WHERE user_id=?",
                                  (int(user_id),))
        return bool(cur)

    async def is_user_muted(self, user_id: int) -> bool:
        row = await self._fetchone("SELECT until FROM mutes WHERE user_id=?",
                                   (int(user_id),))
        if row is None:
            return False
        until = str(row["until"] or "").replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(until)
        except ValueError:
            await self._execute("DELETE FROM mutes WHERE user_id=?",
                                (int(user_id),))
            return False
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) < dt:
            return True
        # срок вышел - чистим, чтобы таблица не росла
        await self._execute("DELETE FROM mutes WHERE user_id=?", (int(user_id),))
        return False

    async def count_user_security(self, user_id: int, minutes: int = 60) -> int:
        """Сколько нарушенийsecurity от пользователя за последние N минут.

        Нужна для персонального мьюта: count_security_since считает по причине
        по всем пользователям сразу и для мьюта конкретного человека не годится.
        """
        row = await self._fetchone(
            "SELECT COUNT(*) n FROM security_log WHERE user_id=? AND timestamp >= ?",
            (int(user_id), _since_iso(minutes)),
        )
        return int(row["n"]) if row else 0

    async def tokens_by_model(self, minutes: int = 1440) -> list[aiosqlite.Row]:
        """Расход токенов по моделям за последние N минут (отчёт админу)."""
        return await self._fetchall(
            "SELECT model, SUM(input_tokens) AS input_tokens, "
            "SUM(output_tokens) AS output_tokens, COUNT(*) AS calls "
            "FROM token_usage WHERE timestamp >= ? GROUP BY model "
            "ORDER BY (SUM(input_tokens) + SUM(output_tokens)) DESC",
            (_since_iso(minutes),),
        )

    async def tokens_since(self, minutes: int = 1440) -> dict[str, Any]:
        row = await self._fetchone(
            "SELECT COALESCE(SUM(input_tokens),0) i, COALESCE(SUM(output_tokens),0) o, "
            "COUNT(*) c FROM token_usage WHERE timestamp >= ?",
            (_since_iso(minutes),),
        )
        if not row:
            return {"input": 0, "output": 0, "calls": 0}
        return {"input": int(row["i"]), "output": int(row["o"]), "calls": int(row["c"])}

    # ------------------------------------------------------------------ admin_requests
    async def ask_admin_once(self, question: str) -> bool:
        """Спросить админа ОДИН РАЗ. True, если вопрос новый (надо спросить)."""
        row = await self._fetchone(
            "SELECT id FROM admin_requests WHERE question=?", (question,)
        )
        if row is not None:
            return False
        await self._execute(
            "INSERT INTO admin_requests (question, answer, asked, timestamp) "
            "VALUES (?, '', 0, ?)",
            (question, _now()),
        )
        return True

    async def mark_admin_asked(self, question: str) -> None:
        await self._execute(
            "UPDATE admin_requests SET asked=1, timestamp=? WHERE question=?",
            (_now(), question),
        )

    async def answer_admin_request(self, question: str, answer: str) -> None:
        await self._execute(
            "INSERT INTO admin_requests (question, answer, asked, timestamp) "
            "VALUES (?, ?, 1, ?) ON CONFLICT(question) DO UPDATE SET "
            "answer=excluded.answer, asked=1",
            (question, answer, _now()),
        )

    async def get_admin_answer(self, question: str) -> str:
        row = await self._fetchone(
            "SELECT answer FROM admin_requests WHERE question=? AND answer != ''",
            (question,),
        )
        return str(row["answer"]) if row else ""

    # ------------------------------------------------------------------ activity_log
    async def log_activity(
        self, country: str, old_status: str, new_status: str, reason: str = ""
    ) -> None:
        await self._execute(
            "INSERT INTO activity_log (country, old_status, new_status, reason, "
            "timestamp) VALUES (?, ?, ?, ?, ?)",
            (country, old_status, new_status, reason, _now()),
        )

    async def set_last_post_at(self, name: str, when: str | None = None) -> None:
        await self._execute(
            "UPDATE countries SET last_post_at=? WHERE name=?", (when or _now(), name)
        )

    async def touch_activity(self, name: str) -> None:
        """Любой пост страны в теме новостей = активность."""
        stamp = _now()
        await self._execute(
            "UPDATE countries SET last_post_at=?, last_activity_at=? WHERE name=?",
            (stamp, stamp, name),
        )

    # ══════════════════════════════ Модуль CARDS (разделы 4, 5, 10) ══════

    async def set_stats_source(self, name: str, source: str) -> None:
        """Отметить источник стартовых данных: manual | anketa (раздел 4)."""
        if source not in ("manual", "anketa"):
            raise ValueError(f"stats_source must be 'manual' or 'anketa', got {source!r}")
        await self._execute(
            "UPDATE countries SET stats_source=? WHERE name=?", (source, name))

    # Колонки, которыми владеет только модуль CARDS. Держат их вне
    # upsert_country намеренно: тот переписывает колонки из своего белого
    # списка, и любая частичная правка задела бы stats_source и обнулила
    # ручной источник данных. Обновляем их точечно и только явно.
    _CARD_FIELDS = (
        "stats_source", "ojat", "tx999", "capital",
        "ruler_chat_id", "channel_url", "accepted_at",
    )

    async def set_card_fields(self, name: str, **fields: Any) -> list[str]:
        """Точечно обновить колонки карточки. Возвращает реально применённые.

        Ключи вне _CARD_FIELDS игнорируются: это защита от опечатки в имени
        поля, которая иначе тихо сделала бы NOTHING и потеряла правку.
        """
        applied: list[str] = []
        for key, value in fields.items():
            if key not in self._CARD_FIELDS or value is None:
                continue
            if key == "stats_source" and value not in ("manual", "anketa"):
                raise ValueError(
                    f"stats_source must be 'manual' or 'anketa', got {value!r}")
            await self._execute(
                f"UPDATE countries SET {key}=? WHERE name=?", (value, name))
            applied.append(key)
        return applied

    async def get_manual_stats(self, name: str) -> dict[str, Any] | None:
        row = await self._fetchone(
            "SELECT payload_json, raw_text, seeded_at FROM manual_stats WHERE country=?",
            (name,),
        )
        if not row:
            return None
        return {
            "country": name,
            "payload": _loads(row["payload_json"], {}),
            "raw_text": str(row["raw_text"] or ""),
            "seeded_at": str(row["seeded_at"] or ""),
        }

    async def save_manual_stats(
        self, name: str, payload: dict[str, Any], raw_text: str = "",
        only_if_absent: bool = False,
    ) -> bool:
        """Сохранить мануальную статистику (раздел 5).

        only_if_absent=True — не перетирать уже загруженную: ручные правки
        админа ценнее повторного сида из промпта.
        """
        if only_if_absent:
            existing = await self._fetchone(
                "SELECT 1 FROM manual_stats WHERE country=?", (name,))
            if existing:
                return False
        await self._execute(
            "INSERT INTO manual_stats (country, payload_json, raw_text, seeded_at) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(country) DO UPDATE SET "
            "payload_json=excluded.payload_json, raw_text=excluded.raw_text, "
            "seeded_at=excluded.seeded_at",
            (name, _dumps(payload), raw_text, _now()),
        )
        return True

    async def list_manual_stats_countries(self) -> list[str]:
        rows = await self._fetchall("SELECT country FROM manual_stats ORDER BY country")
        return [str(r["country"]) for r in rows]

    async def set_country_ruler_contact(
        self, name: str, chat_id: int | None, channel_url: str | None = None
    ) -> None:
        """Контакты правителя для личного уведомления (раздел 10, уровень 3)."""
        await self._execute(
            "UPDATE countries SET ruler_chat_id=?, "
            "channel_url=COALESCE(?, channel_url) WHERE name=?",
            (chat_id, channel_url, name),
        )

    async def set_country_accepted(self, name: str, accepted: bool = True) -> None:
        await self._execute(
            "UPDATE countries SET status=CASE WHEN ?=1 THEN 'active' ELSE status END, "
            "accepted_at=CASE WHEN ?=1 THEN ? ELSE accepted_at END WHERE name=?",
            (1 if accepted else 0, 1 if accepted else 0, _now(), name),
        )

    # --- Счётчики цикла и ежедневный отчёт (раздел 10) ---------------------
    async def log_card_cycle(
        self, countries_updated: int, changes_total: int,
        rollbacks: int, errors: int, deleted: list[str] | None = None,
    ) -> int:
        cur = await self._execute(
            "INSERT INTO card_cycle_log (ran_at, countries_updated, changes_total, "
            "rollbacks, errors, deleted) VALUES (?, ?, ?, ?, ?, ?)",
            (_now(), countries_updated, changes_total, rollbacks, errors,
             ", ".join(deleted or [])),
        )
        return int(cur or 0)

    async def card_cycles_since(self, hours: float = 24) -> list[aiosqlite.Row]:
        cutoff = _since_iso(int(hours * 60))
        return await self._fetchall(
            "SELECT * FROM card_cycle_log WHERE ran_at >= ? ORDER BY id", (cutoff,))

    async def card_cycle_totals(self, hours: float = 24) -> dict[str, int]:
        """Суммы за сутки для отчёта админу (раздел 10, уровень 4)."""
        rows = await self.card_cycles_since(hours)
        return {
            "cycles": len(rows),
            "countries_updated": sum(int(r["countries_updated"] or 0) for r in rows),
            "changes_total": sum(int(r["changes_total"] or 0) for r in rows),
            "rollbacks": sum(int(r["rollbacks"] or 0) for r in rows),
            "errors": sum(int(r["errors"] or 0) for r in rows),
        }

    async def card_report_sent_today(self, day: str) -> bool:
        row = await self._fetchone(
            "SELECT 1 FROM card_reports WHERE report_date=?", (day,))
        return row is not None

    async def mark_card_report_sent(self, day: str) -> None:
        await self._execute(
            "INSERT INTO card_reports (report_date, sent_at) VALUES (?, ?) "
            "ON CONFLICT(report_date) DO UPDATE SET sent_at=excluded.sent_at",
            (day, _now()),
        )


