"""
Module B: Mechanic - Scheduler for country/faction card updates.
Runs every 2 hours, processes news, updates cards, publishes/edits messages.
"""
from __future__ import annotations
import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from aiogram import Bot

from config import (
    config, UPDATE_INTERVAL_HOURS, MAX_MESSAGES_PER_TOPIC,
    ANKETA_CHAT_ID, ANKETA_TOPIC_ID,
    CARD_PUBLISH_INTERVAL_HOURS
)
from db import Database
from ai import AIClient
from cards import (
    build_country_card, build_faction_card,
    apply_event, publish_country_card, publish_faction_card
)
from validator import validate_country_card, validate_faction_card
from assistant import Assistant

log = logging.getLogger(__name__)

@dataclass
class NewsMessage:
    id: int
    chat_id: int
    thread_id: int
    text: str
    date: str
    country_name: str | None = None

class Mechanic:
    """Module B - Card update scheduler."""

    def __init__(self, db: Database, ai: AIClient, assistant: Assistant, bot: Bot):
        self.db = db
        self.ai = ai
        self.assistant = assistant
        self.bot = bot
        self._lock = asyncio.Lock()
        self._running = False
        self._news_topic_ids: list[int] = []
        self._last_scan: dict[str, datetime] = {}

    async def start(self) -> None:
        """Initial setup: discover topics, load anketa."""
        await self._discover_topics()
        await self._load_anketa()
        await self._reconcile_entities()
        log.info("Mechanic initialized")

    async def run_cycle(self) -> None:
        """Main 2-hour cycle."""
        if self._lock.locked():
            log.info("Cycle already running, skipping")
            return
        async with self._lock:
            self._running = True
            log.info("=== Starting update cycle ===")
            try:
                await self._process_news()
                await self._update_countries()
                await self._update_factions()
                await self._publish_cards()
                await self._check_inactive()
                await self._ask_admin_questions()
                log.info("=== Cycle completed ===")
            except Exception as e:
                log.exception("Cycle error: %s", e)
            finally:
                self._running = False

    # ---------- Discovery ----------
    async def _discover_topics(self) -> None:
        """Find news topics from target chat."""
        try:
            chat = await self.bot.get_chat(config.TARGET_CHAT_ID)
            # In a real implementation, you'd use get_forum_topic_list or similar
            # For now, we'll rely on configured topic IDs
            self._news_topic_ids = [
                config.TARGET_TOPIC_ID,
                config.FACTION_TOPIC_ID,
            ]
            # Also add any topics from DB
            topics = await self.db.list_topics()
            for t in topics:
                if t["thread_id"] not in self._news_topic_ids:
                    self._news_topic_ids.append(t["thread_id"])
            log.info("Discovered news topics: %s", self._news_topic_ids)
        except Exception as e:
            log.error("Failed to discover topics: %s", e)

    async def _load_anketa(self) -> None:
        """Load and parse all anketa from the anketa topic."""
        try:
            # This would use bot.get_forum_topic or similar
            # For now, we'll simulate - real implementation needs message iteration
            log.info("Anketa loading would happen here")
        except Exception as e:
            log.error("Anketa load failed: %s", e)

    async def _reconcile_entities(self) -> None:
        """Create missing countries/factions from known_entities."""
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

    # ---------- News Processing ----------
    async def _process_news(self) -> None:
        """Read new messages from news topics."""
        # In a real implementation, this would:
        # 1. Iterate messages in each news topic since last scan
        # 2. Classify by country/faction
        # 3. Store in pending_news
        # For now, we simulate with pending_news table
        pending = await self.db.get_pending_news(limit=MAX_MESSAGES_PER_TOPIC)
        log.info("Pending news to process: %d", len(pending))
        # News are already attributed by country_name in pending_news
        # Group by country
        self._news_by_country: dict[str, list[str]] = {}
        for item in pending:
            self._news_by_country.setdefault(item["country_name"], []).append(item["text"])

    async def _update_countries(self) -> None:
        """Process each country's news and update card."""
        for country_name, news_texts in self._news_by_country.items():
            country = await self.db.get_country(country_name)
            if not country:
                log.warning("Country not in DB: %s", country_name)
                continue
            if country.get("status") != "active":
                continue

            news = "\n\n".join(news_texts)
            if not news.strip():
                continue

            # Extract events via LLM
            try:
                raw = await self.ai.extract_events(country_name, country, news)
                events_data = self._parse_events_json(raw)
            except Exception as e:
                log.error("Event extraction failed for %s: %s", country_name, e)
                continue

            if not events_data.get("events"):
                log.info("No events for %s", country_name)
                continue

            # Apply events
            updated = country.copy()
            all_changes = []
            for ev in events_data["events"]:
                changes = apply_event(updated, ev, self.db)
                all_changes.extend(changes)

            # Validate
            validation = validate_country_card(country, updated, events_data["events"])
            if not validation.valid:
                log.warning("Validation failed for %s: %s", country_name, validation.errors)
                # Notify admin
                if config.ADMIN_ID:
                    try:
                        await self.bot.send_message(
                            config.ADMIN_ID,
                            f"⚠️ Валидация не прошла для {country_name}:\n" +
                            "\n".join(f"• {err}" for err in validation.errors)
                        )
                    except Exception:
                        pass
                # Keep old card
                continue

            # Save updated card
            updated["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            updated["prev_population"] = country.get("population")
            updated["prev_budget"] = country.get("budget")
            updated["prev_army_infantry"] = country.get("army_infantry")
            await self.db.upsert_country(updated)

            # Log changes
            for ch in all_changes:
                await self.db.log_change(country_name, "event", "", ch, "auto")

            # Generate summary
            summary = events_data.get("summary") or await self.ai.generate_summary(
                country_name, events_data["events"]
            )

            # Store for publishing
            country["_summary"] = summary
            country["_notifications"] = self._generate_notifications(updated, events_data["events"])

            # Clear processed news
            await self.db.clear_pending_news(country_name)
            log.info("Updated %s: %d events", country_name, len(events_data["events"]))

    def _parse_events_json(self, raw: str) -> dict[str, Any]:
        """Parse LLM response as JSON."""
        import json
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            # Try to extract JSON from markdown
            import re
            match = re.search(r"\{.*\}", raw, re.DOTALL)
            if match:
                try:
                    return json.loads(match.group())
                except json.JSONDecodeError:
                    pass
        return {"events": [], "summary": ""}

    def _generate_notifications(self, country: dict[str, Any], events: list[dict]) -> str:
        notes = []
        for e in events:
            if e.get("type") == "build":
                notes.append(f"🚧 Строительство: {e['params'].get('building')} x{e['params'].get('count',1)}")
            elif e.get("type") == "budget" and e.get("params",{}).get("delta",0) < -10000:
                notes.append(f"💸 Крупные расходы: {e['params'].get('reason','')}")
            elif e.get("type") == "unreg":
                notes.append("⚠️ Анрег страны")
        return "\n".join(notes) if notes else "Нарушений не выявлено"

    async def _update_factions(self) -> None:
        """Similar to countries but for factions."""
        factions = await self.db.list_factions()
        for faction in factions:
            # Check for news attributed to this faction
            # Simplified: skip if no news
            pass

    # ---------- Publishing ----------
    async def _publish_cards(self) -> None:
        """Publish/update all country and faction cards."""
        try:
            country_topic = await self.db.get_topic("страны")
            faction_topic = await self.db.get_topic("группировки")
            if not country_topic or not faction_topic:
                log.warning("Card topics not configured")
                return

            # Countries
            countries = await self.db.list_countries("active")
            for c in countries:
                msg_id = c.get("card_message_id")
                summary = c.get("_summary", "")
                notifications = c.get("_notifications", "")
                new_id = await publish_country_card(
                    self.bot, c, country_topic["thread_id"], msg_id,
                    summary, notifications
                )
                if new_id != msg_id:
                    await self.db.set_country_card_message(c["name"], new_id)

            # Factions
            factions = await self.db.list_factions()
            for f in factions:
                msg_id = f.get("card_message_id")
                summary = f.get("_summary", "")
                new_id = await publish_faction_card(
                    self.bot, f, faction_topic["thread_id"], msg_id, summary
                )
                if new_id != msg_id:
                    await self.db.set_faction_card_message(f["full_name"], new_id)

        except Exception as e:
            log.error("Publishing failed: %s", e)

    # ---------- Inactivity ----------
    async def _check_inactive(self) -> None:
        """Mark countries inactive after 3/7/30 days."""
        countries = await self.db.list_countries("active")
        now = datetime.now(timezone.utc)
        for c in countries:
            last = c.get("last_activity_at") or c.get("updated_at")
            if not last:
                continue
            try:
                dt = datetime.fromisoformat(last.replace("Z", "+00:00"))
            except ValueError:
                continue
            days = (now - dt).days
            if days > 30:
                if c.get("status") != "afk":
                    await self.db.set_country_status(c["name"], "afk")
                    log.info("%s marked AFK", c["name"])
            elif days > 7:
                if c.get("status") != "frozen":
                    await self.db.set_country_status(c["name"], "frozen")
                    log.info("%s frozen", c["name"])
            elif days > 3:
                if c.get("status") != "inactive":
                    await self.db.set_country_status(c["name"], "inactive")
                    log.info("%s inactive", c["name"])

    # ---------- Admin Questions ----------
    async def _ask_admin_questions(self) -> None:
        """Ask admin ONE TIME per missing data point."""
        # Missing country fields
        countries = await self.db.list_countries("active")
        for c in countries:
            missing = []
            if not c.get("flag_file_id"):
                missing.append("флаг")
            if not c.get("ruler"):
                missing.append("правитель")
            if not c.get("color"):
                missing.append("цвет")
            if not c.get("settlements_json"):
                missing.append("поселения")
            if missing and not await self._was_asked(f"country_{c['name']}_missing"):
                q = f"Страна «{c['name']}»: не указано ({', '.join(missing)}). Подскажи реплаем."
                await self.db.add_question(q)
                await self._mark_asked(f"country_{c['name']}_missing")
                if config.ADMIN_ID:
                    try:
                        await self.bot.send_message(config.ADMIN_ID, q)
                    except Exception:
                        pass

        # Missing faction fields
        factions = await self.db.list_factions()
        for f in factions:
            missing = []
            if not f.get("emblem_file_id"):
                missing.append("эмблема")
            if not f.get("commander"):
                missing.append("командир")
            if not f.get("base_country"):
                missing.append("страна базирования")
            if missing and not await self._was_asked(f"faction_{f['full_name']}_missing"):
                q = f"Группировка «{f['full_name']}»: не указано ({', '.join(missing)}). Подскажи реплаем."
                await self.db.add_question(q)
                await self._mark_asked(f"faction_{f['full_name']}_missing")
                if config.ADMIN_ID:
                    try:
                        await self.bot.send_message(config.ADMIN_ID, q)
                    except Exception:
                        pass

    async def _was_asked(self, key: str) -> bool:
        meta = await self.db.get_meta(f"asked_{key}")
        return meta == "1"

    async def _mark_asked(self, key: str) -> None:
        await self.db.set_meta(f"asked_{key}", "1")

    # ---------- Command handlers ----------
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
                import json
                typ = parts[1]
                data = json.loads(parts[2])
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

        return "Неизвестная команда."