import asyncio
import json
import logging
import re
from datetime import datetime
from typing import Any

from aiogram import Bot
from telethon import TelegramClient

from app.config import config
from app.cards.prompts import build_card_text, build_user_prompt, SYSTEM_EXTRACTION_PROMPT
from app.cards.storage import CardStorage
from app.core.ai_service import ai_service

logger = logging.getLogger(__name__)

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


def _strip_json_fences(raw: str) -> str:
    match = _JSON_FENCE_RE.search(raw)
    if match:
        return match.group(1).strip()
    return raw.strip()


class CardEngine:
    def __init__(self, userbot: TelegramClient, bot: Bot, db: CardStorage) -> None:
        self.userbot = userbot
        self.bot = bot
        self.db = db
        self._cycle_running = False

    def _chat_entity(self) -> int | str:
        return config.cards_group_id

    def _cards_topic_id(self) -> int:
        return int(config.cards_topics.get("карточки стран", 126109))

    def _source_topics(self) -> list[tuple[str, int]]:
        return [
            (name, int(topic_id))
            for name, topic_id in config.cards_topics.items()
            if name != "карточки стран"
        ]

    async def scan_all_topics(self, since_message_id: int = 0) -> list[dict[str, Any]]:
        collected: list[dict[str, Any]] = []
        # Временной порог: не сканировать сообщения старше заданных часов
        from datetime import datetime, timezone, timedelta
        cutoff_time = datetime.now(timezone.utc) - timedelta(hours=config.cards_scan_hours_back)
        for topic_name, topic_id in self._source_topics():
            logger.info("Scanning topic %s (%s)...", topic_name, topic_id)
            try:
                async for msg in self.userbot.iter_messages(
                    self._chat_entity(),
                    reply_to=topic_id,
                    limit=config.cards_scan_limit,
                ):
                    if msg.id <= since_message_id:
                        continue
                    if msg.message is None or not msg.message.strip():
                        continue
                    # Фильтр по времени: пропускаем старые сообщения
                    if msg.date and msg.date.replace(tzinfo=timezone.utc) < cutoff_time:
                        continue
                    collected.append(
                        {
                            "topic": topic_name,
                            "message_id": int(msg.id),
                            "date": msg.date.isoformat() if msg.date else "",
                            "text": msg.message.strip(),
                        }
                    )
                    logger.info("  collected %s msg %s len=%s", topic_name, msg.id, len(msg.message))
                    # Общий лимит сообщений за цикл
                    if len(collected) >= config.cards_max_messages_per_cycle:
                        break
                if len(collected) >= config.cards_max_messages_per_cycle:
                    break
            except Exception as exc:  # pragma: no cover - network/API failures
                logger.error("Scan topic %s (%s) failed: %s", topic_name, topic_id, exc)
            logger.info("Topic %s (%s) done, collected so far: %s", topic_name, topic_id, len(collected))
        return collected

    async def extract_data(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        if not messages:
            return {"cards": [], "questions_for_admin": []}
        current_cards = await self.db.list_cards()
        user_prompt = build_user_prompt(messages, current_cards)
        prompt = (
            SYSTEM_EXTRACTION_PROMPT
            + "\n\n"
            + "ДОПОЛНИТЕЛЬНО: не перезаписывай поля, о которых в новых сообщениях ничего нет; "
            + "числовые изменения применяй, опираясь на прошлые значения из карточки.\n\n"
            + user_prompt
        )
        raw = await ai_service.generate_content(prompt, max_output_tokens=4000)
        if not raw:
            logger.warning("Card extraction returned empty response from AI.")
            return {"cards": [], "questions_for_admin": []}
        try:
            payload = json.loads(_strip_json_fences(raw))
        except (json.JSONDecodeError, TypeError) as exc:
            logger.error("Card extraction JSON parse failed: %s", exc)
            return {"cards": [], "questions_for_admin": []}
        if not isinstance(payload, dict):
            return {"cards": [], "questions_for_admin": []}
        return payload

    async def update_card(self, country: dict[str, Any]) -> None:
        name = str(country.get("country_name", "")).strip()
        if not name:
            return
        text = build_card_text(country)
        message_id = country.get("card_message_id")
        chat = self._chat_entity()
        thread = self._cards_topic_id()
        flag = str(country.get("flag_file_id") or "").strip()

        if message_id:
            try:
                await self.bot.edit_message_text(
                    chat_id=chat,
                    message_id=message_id,
                    text=text,
                    parse_mode="HTML",
                )
                return
            except Exception as exc:
                logger.info("Card text edit failed for %s (%s): %s", name, message_id, exc)
                try:
                    await self.bot.edit_message_caption(
                        chat_id=chat,
                        message_id=message_id,
                        caption=text,
                        parse_mode="HTML",
                    )
                    return
                except Exception as exc2:
                    logger.info("Card caption edit failed for %s (%s), resending: %s", name, message_id, exc2)

        try:
            if flag:
                sent = await self.bot.send_photo(
                    chat_id=chat,
                    photo=flag,
                    caption=text,
                    message_thread_id=thread,
                    parse_mode="HTML",
                )
            else:
                sent = await self.bot.send_message(
                    chat_id=chat,
                    text=text,
                    message_thread_id=thread,
                    parse_mode="HTML",
                )
        except Exception as exc:  # pragma: no cover - Telegram errors
            logger.error("Failed to send card for %s: %s", name, exc)
            return
        if sent:
            await self.db.set_card_message_pointer(name, int(sent.message_id))
            await self.db.upsert_card({**country, "card_message_id": int(sent.message_id)})

    async def ask_admin(self, questions: list[dict[str, str]]) -> list[int]:
        asked_ids: list[int] = []
        if not questions:
            return asked_ids
        existing = await self.db.list_unanswered_questions()
        seen: set[tuple[str, str]] = {
            (str(q.get("country_name", "")), str(q.get("question", "")).lower())
            for q in existing
        }
        for item in questions:
            country_name = str(item.get("country_name", "")).strip()
            question = str(item.get("question", "")).strip()
            if not question:
                continue
            if (country_name, question.lower()) in seen:
                continue
            seen.add((country_name, question.lower()))
            text = f"❓ Вопрос механика WARLORD RP\n\nСтрана: {country_name or '—'}\nВопрос: {question}\n\nОтветь на это сообщение, и я запомню."
            try:
                sent = await self.bot.send_message(config.admin_id, text)
                tg_id = int(sent.message_id) if sent else None
            except Exception as exc:  # pragma: no cover - Telegram errors
                logger.error("Failed to ask admin question: %s", exc)
                tg_id = None
            qid = await self.db.add_question(
                country_name,
                question,
                telegram_message_id=tg_id,
                asked_at=int(datetime.now().timestamp()),
            )
            asked_ids.append(qid)
        return asked_ids

    async def scheduler(self) -> None:
        logger.info("Card scheduler started. First cycle immediately.")
        try:
            interval = max(0.05, config.cards_update_interval_hours * 3600)
            logger.info("DEBUG: scheduler interval = %s", interval)
        except Exception as exc:
            logger.exception("Scheduler init failed: %s", exc)
            return
        while True:
            logger.info("Card cycle running at %s...", datetime.now().isoformat(timespec="seconds"))
            try:
                await self.run_cycle()
            except Exception as exc:  # pragma: no cover - defensive guard
                logger.exception("Card cycle failed: %s", exc)
            await asyncio.sleep(interval)

    async def run_cycle(self) -> None:
        if self._cycle_running:
            return
        self._cycle_running = True
        try:
            messages = await self.scan_all_topics()
            new_messages = [m for m in messages if not await self._is_scanned(m)]
            if not new_messages:
                logger.info("Card cycle: no new messages to process.")
                return
            for m in new_messages:
                await self.db.mark_message_processed(
                    int(config.cards_topics.get(str(m.get("topic")), 0)),
                    int(m["message_id"]),
                )
            parsed = await self.extract_data(new_messages)
            cards = parsed.get("cards") or []
            questions = parsed.get("questions_for_admin") or []
            updated: list[dict[str, Any]] = []
            for card in cards:
                if not isinstance(card, dict):
                    continue
                name = str(card.get("country_name", "")).strip()
                if not name:
                    continue
                existing = (await self.db.get_card(name)) or {}
                merged = await self._merge_card(existing, card)
                merged["last_updated"] = datetime.now().isoformat(timespec="seconds")
                await self.db.upsert_card(merged)
                updated.append(merged)
            for card in updated:
                await self.update_card(card)
            await self.ask_admin(questions)
            logger.info(
                "Card cycle done: %s cards updated, %s admin questions.",
                len(updated),
                len(questions),
            )
        finally:
            self._cycle_running = False

    async def _is_scanned(self, m: dict[str, Any]) -> bool:
        topic_id = int(config.cards_topics.get(str(m.get("topic")), 0))
        return await self.db.is_message_processed(topic_id, int(m["message_id"]))

    async def _merge_card(self, existing: dict[str, Any], update: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = dict(existing or {})
        for key, value in update.items():
            if key in ("country_name", "card_message_id"):
                continue
            if isinstance(value, list):
                result[key] = value
            elif value is not None and str(value) != "":
                result[key] = value
        if "changes" in update and isinstance(update.get("changes"), list):
            result["changes"] = update["changes"]
        if "notifications" in update and isinstance(update.get("notifications"), list):
            result["notifications"] = update["notifications"]
        return result

    async def initial_sync(self) -> None:
        logger.info("Card mechanic: running initial sync...")
        await self.run_cycle()