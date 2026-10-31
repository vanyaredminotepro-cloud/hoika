"""
Database layer for Warlord RP Bot - SQLite with async support.
"""
from __future__ import annotations
import json
import logging
import sqlite3
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite

from config import DB_PATH, LOG_DIR

log = logging.getLogger(__name__)

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA busy_timeout=30000;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS countries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    flag_file_id TEXT,
    ruler TEXT,
    party TEXT,
    ideology TEXT,
    color TEXT,
    population INTEGER DEFAULT 0,
    population_capacity INTEGER DEFAULT 0,
    budget INTEGER DEFAULT 0,
    income_daily INTEGER DEFAULT 1000,
    expenses_daily INTEGER DEFAULT 0,
    army_infantry INTEGER DEFAULT 0,
    army_light_vehicles INTEGER DEFAULT 0,
    army_heavy_wheeled INTEGER DEFAULT 0,
    army_artillery INTEGER DEFAULT 0,
    army_drones INTEGER DEFAULT 0,
    army_navy INTEGER DEFAULT 0,
    tax_level TEXT DEFAULT 'standard',
    stability INTEGER DEFAULT 70,
    war_support INTEGER DEFAULT 50,
    happiness INTEGER DEFAULT 50,
    resources_json TEXT DEFAULT '[]',
    settlements_json TEXT DEFAULT '[]',
    industry_json TEXT DEFAULT '[]',
    research_json TEXT DEFAULT '[]',
    card_message_id INTEGER,
    status TEXT DEFAULT 'active',
    updated_at TEXT,
    registered_at TEXT,
    last_activity_at TEXT
);

CREATE TABLE IF NOT EXISTS factions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    prefix TEXT NOT NULL,
    full_name TEXT UNIQUE NOT NULL,
    emblem_file_id TEXT,
    commander TEXT,
    depends_on_country TEXT,
    base_country TEXT,
    relation_to_country TEXT DEFAULT 'neutral',
    strength INTEGER DEFAULT 0,
    equipment_json TEXT DEFAULT '[]',
    budget INTEGER DEFAULT 0,
    tasks TEXT,
    contracts_json TEXT DEFAULT '[]',
    wars_json TEXT DEFAULT '[]',
    reputation INTEGER DEFAULT 50,
    card_message_id INTEGER,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS country_factions (
    country_id INTEGER NOT NULL,
    faction_id INTEGER NOT NULL,
    status TEXT DEFAULT 'stationed',
    PRIMARY KEY (country_id, faction_id),
    FOREIGN KEY (country_id) REFERENCES countries(id) ON DELETE CASCADE,
    FOREIGN KEY (faction_id) REFERENCES factions(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS persons (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    country TEXT,
    activity TEXT,
    job TEXT,
    money INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS topics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    thread_id INTEGER NOT NULL,
    purpose TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS known_entities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL,
    name TEXT NOT NULL,
    status TEXT DEFAULT 'unknown',
    note TEXT,
    asked INTEGER DEFAULT 0,
    UNIQUE(type, name)
);

CREATE TABLE IF NOT EXISTS pending_news (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    country_name TEXT NOT NULL,
    text TEXT NOT NULL,
    added_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS questions_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    question TEXT NOT NULL,
    answer TEXT DEFAULT '',
    asked_at TEXT NOT NULL,
    applied INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS security_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    username TEXT,
    text TEXT NOT NULL,
    reason TEXT NOT NULL,
    timestamp TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS changes_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    country TEXT NOT NULL,
    field TEXT NOT NULL,
    old_value TEXT,
    new_value TEXT,
    reason TEXT,
    timestamp TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE INDEX IF NOT EXISTS idx_pending_news_country ON pending_news(country_name);
CREATE INDEX IF NOT EXISTS idx_security_log_user ON security_log(user_id);
CREATE INDEX IF NOT EXISTS idx_changes_log_country ON changes_log(country);
CREATE INDEX IF NOT EXISTS idx_known_entities_type ON known_entities(type);
"""

def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def _json_loads(val: str | None) -> list[Any]:
    if not val:
        return []
    try:
        return json.loads(val)
    except json.JSONDecodeError:
        return []

def _json_dumps(val: Any) -> str:
    return json.dumps(val, ensure_ascii=False)

class Database:
    def __init__(self, path: Path | None = None):
        self.path = path or DB_PATH
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.executescript(SCHEMA)
        await self._conn.commit()
        log.info("Database connected: %s", self.path)

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if not self._conn:
            raise RuntimeError("Database not connected")
        return self._conn

    # ---------- Countries ----------
    async def upsert_country(self, data: dict[str, Any]) -> int:
        name = data.get("name")
        if not name:
            raise ValueError("Country name required")
        now = _now()
        fields = [
            "name", "flag_file_id", "ruler", "party", "ideology", "color",
            "population", "population_capacity", "budget", "income_daily", "expenses_daily",
            "army_infantry", "army_light_vehicles", "army_heavy_wheeled",
            "army_artillery", "army_drones", "army_navy",
            "tax_level", "stability", "war_support", "happiness",
            "resources_json", "settlements_json", "industry_json", "research_json",
            "card_message_id", "status", "updated_at"
        ]
        vals = []
        for f in fields:
            v = data.get(f)
            if isinstance(v, (list, dict)):
                v = _json_dumps(v)
            vals.append(v)
        vals.append(now)
        placeholders = ",".join("?" * len(fields))
        sql = f"""
            INSERT INTO countries ({",".join(fields)}, updated_at)
            VALUES ({placeholders}, ?)
            ON CONFLICT(name) DO UPDATE SET
            {", ".join(f"{f}=excluded.{f}" for f in fields)},
            updated_at=excluded.updated_at
        """
        await self.conn.execute(sql, vals)
        await self.conn.commit()
        row = await self.conn.execute("SELECT id FROM countries WHERE name=?", (name,))
        r = await row.fetchone()
        return r["id"] if r else 0

    async def get_country(self, name: str) -> dict[str, Any] | None:
        row = await self.conn.execute("SELECT * FROM countries WHERE name=?", (name,))
        r = await row.fetchone()
        if not r:
            return None
        d = dict(r)
        for k in ("resources_json", "settlements_json", "industry_json", "research_json"):
            d[k] = _json_loads(d.get(k))
        return d

    async def list_countries(self, status: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM countries"
        params = ()
        if status:
            sql += " WHERE status=?"
            params = (status,)
        sql += " ORDER BY name"
        rows = await self.conn.execute(sql, params)
        out = []
        for r in await rows.fetchall():
            d = dict(r)
            for k in ("resources_json", "settlements_json", "industry_json", "research_json"):
                d[k] = _json_loads(d.get(k))
            out.append(d)
        return out

    async def set_country_status(self, name: str, status: str) -> None:
        await self.conn.execute(
            "UPDATE countries SET status=?, updated_at=? WHERE name=?",
            (status, _now(), name)
        )
        await self.conn.commit()

    async def set_country_card_message(self, name: str, msg_id: int | None) -> None:
        await self.conn.execute(
            "UPDATE countries SET card_message_id=? WHERE name=?",
            (msg_id, name)
        )
        await self.conn.commit()

    # ---------- Factions ----------
    async def upsert_faction(self, data: dict[str, Any]) -> int:
        name = data.get("full_name")
        if not name:
            raise ValueError("Faction full_name required")
        now = _now()
        fields = [
            "prefix", "full_name", "emblem_file_id", "commander",
            "depends_on_country", "base_country", "relation_to_country",
            "strength", "equipment_json", "budget", "tasks",
            "contracts_json", "wars_json", "reputation", "card_message_id"
        ]
        vals = []
        for f in fields:
            v = data.get(f)
            if isinstance(v, (list, dict)):
                v = _json_dumps(v)
            vals.append(v)
        vals.append(now)
        placeholders = ",".join("?" * len(fields))
        sql = f"""
            INSERT INTO factions ({",".join(fields)}, updated_at)
            VALUES ({placeholders}, ?)
            ON CONFLICT(full_name) DO UPDATE SET
            {", ".join(f"{f}=excluded.{f}" for f in fields)},
            updated_at=excluded.updated_at
        """
        await self.conn.execute(sql, vals)
        await self.conn.commit()
        row = await self.conn.execute("SELECT id FROM factions WHERE full_name=?", (name,))
        r = await row.fetchone()
        return r["id"] if r else 0

    async def get_faction(self, full_name: str) -> dict[str, Any] | None:
        row = await self.conn.execute("SELECT * FROM factions WHERE full_name=?", (full_name,))
        r = await row.fetchone()
        if not r:
            return None
        d = dict(r)
        for k in ("equipment_json", "contracts_json", "wars_json"):
            d[k] = _json_loads(d.get(k))
        return d

    async def list_factions(self) -> list[dict[str, Any]]:
        rows = await self.conn.execute("SELECT * FROM factions ORDER BY full_name")
        out = []
        for r in await rows.fetchall():
            d = dict(r)
            for k in ("equipment_json", "contracts_json", "wars_json"):
                d[k] = _json_loads(d.get(k))
            out.append(d)
        return out

    async def set_faction_card_message(self, full_name: str, msg_id: int | None) -> None:
        await self.conn.execute(
            "UPDATE factions SET card_message_id=? WHERE full_name=?",
            (msg_id, full_name)
        )
        await self.conn.commit()

    # ---------- Country-Faction links ----------
    async def link_country_faction(self, country_id: int, faction_id: int, status: str = "stationed") -> None:
        await self.conn.execute(
            "INSERT OR REPLACE INTO country_factions (country_id, faction_id, status) VALUES (?, ?, ?)",
            (country_id, faction_id, status)
        )
        await self.conn.commit()

    async def get_country_factions(self, country_id: int) -> list[dict[str, Any]]:
        rows = await self.conn.execute(
            """SELECT f.* FROM factions f
               JOIN country_factions cf ON f.id = cf.faction_id
               WHERE cf.country_id = ?""",
            (country_id,)
        )
        out = []
        for r in await rows.fetchall():
            d = dict(r)
            for k in ("equipment_json", "contracts_json", "wars_json"):
                d[k] = _json_loads(d.get(k))
            out.append(d)
        return out

    # ---------- Persons ----------
    async def upsert_person(self, data: dict[str, Any]) -> int:
        await self.conn.execute(
            """INSERT INTO persons (name, country, activity, job, money)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(name) DO UPDATE SET
               country=excluded.country, activity=excluded.activity,
               job=excluded.job, money=excluded.money""",
            (data.get("name"), data.get("country"), data.get("activity"),
             data.get("job"), data.get("money", 0))
        )
        await self.conn.commit()
        return 1

    # ---------- Topics ----------
    async def upsert_topic(self, chat_id: int, thread_id: int, purpose: str) -> None:
        await self.conn.execute(
            """INSERT INTO topics (chat_id, thread_id, purpose) VALUES (?, ?, ?)
               ON CONFLICT(chat_id, thread_id) DO UPDATE SET purpose=excluded.purpose""",
            (chat_id, thread_id, purpose)
        )
        await self.conn.commit()

    async def get_topic(self, purpose: str) -> dict[str, Any] | None:
        row = await self.conn.execute("SELECT * FROM topics WHERE purpose=?", (purpose,))
        r = await row.fetchone()
        return dict(r) if r else None

    async def list_topics(self) -> list[dict[str, Any]]:
        rows = await self.conn.execute("SELECT * FROM topics")
        return [dict(r) for r in await rows.fetchall()]

    # ---------- Known Entities ----------
    async def add_known_entity(self, ent_type: str, name: str, note: str = "") -> int:
        await self.conn.execute(
            """INSERT OR IGNORE INTO known_entities (type, name, status, note)
               VALUES (?, ?, 'unknown', ?)""",
            (ent_type, name, note)
        )
        await self.conn.commit()
        row = await self.conn.execute("SELECT id FROM known_entities WHERE type=? AND name=?", (ent_type, name))
        r = await row.fetchone()
        return r["id"] if r else 0

    async def get_entity(self, ent_type: str, name: str) -> dict[str, Any] | None:
        row = await self.conn.execute("SELECT * FROM known_entities WHERE type=? AND name=?", (ent_type, name))
        r = await row.fetchone()
        return dict(r) if r else None

    async def set_entity_type(self, ent_id: int, new_type: str) -> None:
        await self.conn.execute(
            "UPDATE known_entities SET type=?, status=?, asked=1 WHERE id=?",
            (new_type, new_type, ent_id)
        )
        await self.conn.commit()

    async def list_entities(self, ent_type: str | None = None, only_unasked: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM known_entities"
        params: list[Any] = []
        if ent_type:
            sql += " WHERE type=?"
            params.append(ent_type)
        if only_unasked:
            sql += (" AND" if ent_type else " WHERE") + " asked=0"
        sql += " ORDER BY id"
        rows = await self.conn.execute(sql, params)
        return [dict(r) for r in await rows.fetchall()]

    async def mark_entity_asked(self, ent_id: int) -> None:
        await self.conn.execute("UPDATE known_entities SET asked=1 WHERE id=?", (ent_id,))
        await self.conn.commit()

    async def delete_entity(self, ent_type: str, name: str) -> None:
        await self.conn.execute("DELETE FROM known_entities WHERE type=? AND name=?", (ent_type, name))
        await self.conn.commit()

    # ---------- Pending News ----------
    async def add_pending_news(self, country_name: str, text: str) -> int:
        await self.conn.execute(
            "INSERT INTO pending_news (country_name, text, added_at) VALUES (?, ?, ?)",
            (country_name, text, _now())
        )
        await self.conn.commit()
        row = await self.conn.execute("SELECT last_insert_rowid()")
        r = await row.fetchone()
        return r[0] if r else 0

    async def get_pending_news(self, country_name: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        if country_name:
            sql = "SELECT * FROM pending_news WHERE country_name=? ORDER BY added_at DESC LIMIT ?"
            params = (country_name, limit)
        else:
            sql = "SELECT * FROM pending_news ORDER BY added_at DESC LIMIT ?"
            params = (limit,)
        rows = await self.conn.execute(sql, params)
        return [dict(r) for r in await rows.fetchall()]

    async def clear_pending_news(self, country_name: str | None = None) -> None:
        if country_name:
            await self.conn.execute("DELETE FROM pending_news WHERE country_name=?", (country_name,))
        else:
            await self.conn.execute("DELETE FROM pending_news")
        await self.conn.commit()

    # ---------- Questions ----------
    async def add_question(self, question: str) -> int:
        await self.conn.execute(
            "INSERT INTO questions_log (question, asked_at) VALUES (?, ?)",
            (question, _now())
        )
        await self.conn.commit()
        row = await self.conn.execute("SELECT last_insert_rowid()")
        r = await row.fetchone()
        return r[0] if r else 0

    async def answer_question(self, qid: int, answer: str) -> None:
        await self.conn.execute(
            "UPDATE questions_log SET answer=? WHERE id=?",
            (answer, qid)
        )
        await self.conn.commit()

    async def get_question(self, qid: int) -> dict[str, Any] | None:
        row = await self.conn.execute("SELECT * FROM questions_log WHERE id=?", (qid,))
        r = await row.fetchone()
        return dict(r) if r else None

    async def list_unanswered_questions(self) -> list[dict[str, Any]]:
        rows = await self.conn.execute(
            "SELECT * FROM questions_log WHERE answer='' AND applied=0 ORDER BY asked_at"
        )
        return [dict(r) for r in await rows.fetchall()]

    async def list_unapplied_answers(self) -> list[dict[str, Any]]:
        rows = await self.conn.execute(
            "SELECT * FROM questions_log WHERE answer!='' AND (applied IS NULL OR applied=0) ORDER BY asked_at"
        )
        return [dict(r) for r in await rows.fetchall()]

    async def mark_questions_applied(self, ids: list[int]) -> None:
        if not ids:
            return
        placeholders = ",".join("?" * len(ids))
        await self.conn.execute(
            f"UPDATE questions_log SET applied=1 WHERE id IN ({placeholders})",
            tuple(ids)
        )
        await self.conn.commit()

    # ---------- Security Log ----------
    async def log_security(self, user_id: int, username: str | None, text: str, reason: str) -> None:
        await self.conn.execute(
            "INSERT INTO security_log (user_id, username, text, reason, timestamp) VALUES (?, ?, ?, ?, ?)",
            (user_id, username, text[:500], reason, _now())
        )
        await self.conn.commit()

    async def get_security_count(self, user_id: int, hours: int = 1) -> int:
        since = datetime.now(timezone.utc).timestamp() - hours * 3600
        row = await self.conn.execute(
            "SELECT COUNT(*) FROM security_log WHERE user_id=? AND timestamp > ?",
            (user_id, datetime.fromtimestamp(since, tz=timezone.utc).isoformat())
        )
        r = await row.fetchone()
        return r[0] if r else 0

    # ---------- Changes Log ----------
    async def log_change(self, country: str, field: str, old: Any, new: Any, reason: str) -> None:
        await self.conn.execute(
            "INSERT INTO changes_log (country, field, old_value, new_value, reason, timestamp) VALUES (?, ?, ?, ?, ?, ?)",
            (country, field, str(old) if old is not None else "", str(new) if new is not None else "", reason, _now())
        )
        await self.conn.commit()

    # ---------- Meta ----------
    async def set_meta(self, key: str, value: str) -> None:
        await self.conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value)
        )
        await self.conn.commit()

    async def get_meta(self, key: str) -> str | None:
        row = await self.conn.execute("SELECT value FROM meta WHERE key=?", (key,))
        r = await row.fetchone()
        return r[0] if r else None

    # ---------- Utility ----------
    async def execute(self, sql: str, params: tuple = ()) -> None:
        await self.conn.execute(sql, params)
        await self.conn.commit()

    async def fetchone(self, sql: str, params: tuple = ()) -> dict[str, Any] | None:
        row = await self.conn.execute(sql, params)
        r = await row.fetchone()
        return dict(r) if r else None

    async def fetchall(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        rows = await self.conn.execute(sql, params)
        return [dict(r) for r in await rows.fetchall()]