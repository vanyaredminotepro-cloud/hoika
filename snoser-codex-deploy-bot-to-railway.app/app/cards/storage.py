import json
from typing import Any

import aiosqlite


class CardStorage:
    def __init__(self, path: str) -> None:
        self.path = path

    async def init(self) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS country_cards (
                    country_name TEXT PRIMARY KEY,
                    flag_file_id TEXT NOT NULL DEFAULT '',
                    ruler TEXT NOT NULL DEFAULT '',
                    party TEXT NOT NULL DEFAULT '',
                    ideology TEXT NOT NULL DEFAULT '',
                    "exists" INTEGER NOT NULL DEFAULT 1,
                    controlled_by TEXT NOT NULL DEFAULT '',
                    population INTEGER NOT NULL DEFAULT 0,
                    population_capacity INTEGER NOT NULL DEFAULT 0,
                    budget INTEGER NOT NULL DEFAULT 0,
                    income_daily INTEGER NOT NULL DEFAULT 0,
                    expenses_daily INTEGER NOT NULL DEFAULT 0,
                    army INTEGER NOT NULL DEFAULT 0,
                    tax_level INTEGER NOT NULL DEFAULT 0,
                    settlements TEXT NOT NULL DEFAULT '[]',
                    industry TEXT NOT NULL DEFAULT '[]',
                    research TEXT NOT NULL DEFAULT '[]',
                    stability INTEGER NOT NULL DEFAULT 0,
                    war_support INTEGER NOT NULL DEFAULT 0,
                    happiness INTEGER NOT NULL DEFAULT 0,
                    pending_buildings TEXT NOT NULL DEFAULT '[]',
                    pending_research TEXT NOT NULL DEFAULT '[]',
                    pending_colonization TEXT NOT NULL DEFAULT '[]',
                    card_message_id INTEGER,
                    last_updated TEXT NOT NULL DEFAULT '',
                    changes TEXT NOT NULL DEFAULT '[]',
                    summary TEXT NOT NULL DEFAULT '',
                    notifications TEXT NOT NULL DEFAULT '[]'
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS admin_questions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    country_name TEXT NOT NULL,
                    question TEXT NOT NULL,
                    answer TEXT,
                    answered_at INTEGER,
                    asked_at INTEGER NOT NULL DEFAULT 0,
                    telegram_message_id INTEGER
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS card_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL DEFAULT ''
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS scanned_posts (
                    topic_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL,
                    PRIMARY KEY (topic_id, message_id)
                )
                """
            )
            await db.commit()

    @staticmethod
    def _parse_json(raw: str, default: Any) -> Any:
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return default

    async def get_card(self, country_name: str) -> dict[str, Any] | None:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM country_cards WHERE country_name = ?", (country_name,)
            )
            row = await cursor.fetchone()
        if row is None:
            return None
        return self._card_from_row(row)

    @staticmethod
    def _card_from_row(row: aiosqlite.Row) -> dict[str, Any]:
        data = dict(row)
        for key in (
            "settlements",
            "industry",
            "research",
            "pending_buildings",
            "pending_research",
            "pending_colonization",
            "changes",
            "notifications",
        ):
            data[key] = CardStorage._parse_json(data.get(key) or "", [])
        return data

    async def list_cards(self) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            rows = await (await db.execute("SELECT * FROM country_cards ORDER BY country_name COLLATE NOCASE")).fetchall()
        return [self._card_from_row(row) for row in rows]

    async def upsert_card(self, card: dict[str, Any]) -> None:
        payload: dict[str, Any] = dict(card)
        name = str(payload.get("country_name", "")).strip()
        if not name:
            return
        json_fields = (
            "settlements",
            "industry",
            "research",
            "pending_buildings",
            "pending_research",
            "pending_colonization",
            "changes",
            "notifications",
        )
        for key in json_fields:
            value = payload.get(key)
            if isinstance(value, (list, dict)):
                payload[key] = json.dumps(value, ensure_ascii=False)
            elif value is None:
                payload[key] = "[]"

        scalar = {
            "country_name": name,
            "flag_file_id": str(payload.get("flag_file_id", "")),
            "ruler": str(payload.get("ruler", "")),
            "party": str(payload.get("party", "")),
            "ideology": str(payload.get("ideology", "")),
            "exists": 1 if payload.get("exists", True) else 0,
            "controlled_by": str(payload.get("controlled_by", "")),
            "population": int(payload.get("population", 0) or 0),
            "population_capacity": int(payload.get("population_capacity", 0) or 0),
            "budget": int(payload.get("budget", 0) or 0),
            "income_daily": int(payload.get("income_daily", 0) or 0),
            "expenses_daily": int(payload.get("expenses_daily", 0) or 0),
            "army": int(payload.get("army", 0) or 0),
            "tax_level": int(payload.get("tax_level", 0) or 0),
            "settlements": payload.get("settlements", "[]"),
            "industry": payload.get("industry", "[]"),
            "research": payload.get("research", "[]"),
            "stability": int(payload.get("stability", 0) or 0),
            "war_support": int(payload.get("war_support", 0) or 0),
            "happiness": int(payload.get("happiness", 0) or 0),
            "pending_buildings": payload.get("pending_buildings", "[]"),
            "pending_research": payload.get("pending_research", "[]"),
            "pending_colonization": payload.get("pending_colonization", "[]"),
            "card_message_id": payload.get("card_message_id"),
            "changes": payload.get("changes", "[]"),
            "summary": str(payload.get("summary", "")),
            "notifications": payload.get("notifications", "[]"),
            "last_updated": str(payload.get("last_updated", "")),
        }
        columns = list(scalar.keys())
        quoted = [f'"{c}"' for c in columns]
        placeholders = ", ".join(["?"] * len(columns))
        update_cols = ", ".join([f'"{c}" = excluded."{c}"' for c in columns if c != "country_name"])
        values = [scalar[c] for c in columns]
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                f"""
                INSERT INTO country_cards ({", ".join(quoted)})
                VALUES ({placeholders})
                ON CONFLICT(country_name) DO UPDATE SET {update_cols}
                """,
                values,
            )
            await db.commit()

    async def set_card_message_pointer(self, country_name: str, message_id: int | None) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "UPDATE country_cards SET card_message_id = ? WHERE country_name = ?",
                (message_id, country_name),
            )
            await db.commit()

    async def set_flag_file_id(self, country_name: str, flag_file_id: str) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "UPDATE country_cards SET flag_file_id = ? WHERE country_name = ?",
                (flag_file_id, country_name),
            )
            await db.commit()

    async def add_question(
        self,
        country_name: str,
        question: str,
        telegram_message_id: int | None = None,
        asked_at: int = 0,
    ) -> int:
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                "INSERT INTO admin_questions (country_name, question, telegram_message_id, asked_at) VALUES (?, ?, ?, ?)",
                (country_name, question, telegram_message_id, asked_at),
            )
            await db.commit()
            return int(cursor.lastrowid)

    async def list_unanswered_questions(self) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            rows = await (
                await db.execute(
                    "SELECT * FROM admin_questions WHERE answer IS NULL ORDER BY id ASC"
                )
            ).fetchall()
        return [dict(row) for row in rows]

    async def list_all_questions(self, limit: int = 50) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            rows = await (
                await db.execute(
                    "SELECT * FROM admin_questions ORDER BY id DESC LIMIT ?", (limit,)
                )
            ).fetchall()
        return [dict(row) for row in rows]

    async def answer_question(self, question_id: int, answer: str) -> bool:
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                "UPDATE admin_questions SET answer = ?, answered_at = strftime('%s','now') WHERE id = ?",
                (answer, question_id),
            )
            await db.commit()
            return int(cursor.rowcount or 0) > 0

    async def is_message_processed(self, topic_id: int, message_id: int) -> bool:
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                "SELECT 1 FROM scanned_posts WHERE topic_id = ? AND message_id = ?",
                (topic_id, message_id),
            )
            row = await cursor.fetchone()
        return row is not None

    async def mark_message_processed(self, topic_id: int, message_id: int) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT OR IGNORE INTO scanned_posts (topic_id, message_id) VALUES (?, ?)",
                (topic_id, message_id),
            )
            await db.commit()

    async def set_setting(self, key: str, value: str) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT INTO card_settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
            await db.commit()

    async def get_setting(self, key: str, default: str = "") -> str:
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute("SELECT value FROM card_settings WHERE key = ?", (key,))
            row = await cursor.fetchone()
        return str(row[0]) if row else default

    async def country_count(self) -> int:
        async with aiosqlite.connect(self.path) as db:
            row = await (await db.execute("SELECT COUNT(*) FROM country_cards")).fetchone()
        return int(row[0]) if row else 0