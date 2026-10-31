"""
AI Client with DeepSeek/Groq support, token economy, caching, and prompt management.
"""
from __future__ import annotations
import asyncio
import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

import aiohttp

from .config import (
    get_provider_config, MAX_TOKENS_FAST, MAX_TOKENS_FULL, MAX_TOKENS_ANKETA,
    MAX_HISTORY_MESSAGES, CACHE_TTL_HOURS, MAX_NEWS_CHARS,
    AI_MAX_CONCURRENT, AI_MIN_INTERVAL, AI_MAX_RETRIES, AI_TIMEOUT,
    AI_CALL_DEADLINE,
)

log = logging.getLogger(__name__)


def _extract_content(data: dict[str, Any]) -> str:
    """Pull text out of an OpenAI-compatible completion payload.

    Handles the usual string content, multi-part content blocks and
    reasoning-only responses (content empty but reasoning_content filled).
    """
    try:
        message = data["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        return ""

    content = message.get("content")
    if isinstance(content, list):
        parts = [
            b.get("text", "")
            for b in content
            if isinstance(b, dict) and b.get("type") in ("text", "output_text")
        ]
        content = "".join(parts)
    elif not isinstance(content, str):
        content = "" if content is None else str(content)

    if content.strip():
        return content

    reasoning = message.get("reasoning_content")
    if isinstance(reasoning, str) and reasoning.strip():
        return reasoning
    return content


def _as_dict(data: Any) -> dict[str, Any]:
    """Keep only the dict contract: unwrap JSON arrays, drop everything else."""
    if isinstance(data, dict):
        return data
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                return item
    return {}


def parse_json_response(raw: str) -> dict[str, Any]:
    """Parse a JSON object from an LLM reply.

    Models often wrap JSON in ```json fences or add prose around it,
    and can truncate it. Tries fences first, then a brace scan.
    """
    if not raw:
        return {}
    text = raw.strip()

    # 1) fenced block
    m = re.search(r"```(?:json)?\s*(.+?)```", text, re.DOTALL)
    if m:
        text = m.group(1).strip()

    # 2) plain object
    try:
        return _as_dict(json.loads(text))
    except json.JSONDecodeError:
        pass

    # 3) brace-balanced scan from the first '{'
    start = text.find("{")
    if start == -1:
        return {}
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                candidate = text[start:i + 1]
                try:
                    return _as_dict(json.loads(candidate))
                except json.JSONDecodeError:
                    break
    return {}

@dataclass
class AIResponse:
    content: str
    tokens_used: int
    model: str
    cached: bool = False

@dataclass
class TokenUsage:
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int

class TokenCache:
    """Simple in-memory cache with TTL."""
    def __init__(self, ttl_hours: int = CACHE_TTL_HOURS):
        self._cache: dict[str, tuple[Any, float]] = {}
        self._ttl = ttl_hours * 3600

    def _key(self, prompt: str, model: str) -> str:
        return hashlib.sha256(f"{model}:{prompt}".encode()).hexdigest()[:32]

    def get(self, prompt: str, model: str) -> Any | None:
        key = self._key(prompt, model)
        if key in self._cache:
            value, timestamp = self._cache[key]
            if time.time() - timestamp < self._ttl:
                return value
            del self._cache[key]
        return None

    def set(self, prompt: str, model: str, value: Any) -> None:
        self._cache[self._key(prompt, model)] = (value, time.time())

    def clear_expired(self) -> None:
        now = time.time()
        expired = [k for k, (_, ts) in self._cache.items() if now - ts >= self._ttl]
        for k in expired:
            del self._cache[k]

class AIClient:
    """Unified client for DeepSeek and Groq with token economy."""

    def __init__(self, provider_name: str | None = None):
        self.provider = get_provider_config(provider_name)
        self.session: aiohttp.ClientSession | None = None
        self.cache = TokenCache()
        self._daily_tokens = 0
        self._day_start = datetime.now(timezone.utc).date()
        self._history: dict[int, list[dict[str, str]]] = {}
        # The provider throttles hard: one in-flight request per client plus a
        # minimum gap between calls keeps a 50-country cycle from getting 429.
        self._sem = asyncio.Semaphore(AI_MAX_CONCURRENT)
        self._last_request_at = 0.0

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if not self.session or self.session.closed:
            timeout = aiohttp.ClientTimeout(total=AI_TIMEOUT)
            self.session = aiohttp.ClientSession(timeout=timeout)
        return self.session

    async def close(self) -> None:
        if self.session and not self.session.closed:
            await self.session.close()

    def _check_day_rollover(self) -> None:
        today = datetime.now(timezone.utc).date()
        if today != self._day_start:
            self._day_start = today
            self._daily_tokens = 0
            log.info("Daily token counter reset")

    def _estimate_tokens(self, text: str) -> int:
        # Rough estimate: ~1.3 tokens per word for Russian/English mix
        return max(1, int(len(text.split()) * 1.3))

    def _trim_history(self, user_id: int) -> list[dict[str, str]]:
        hist = self._history.get(user_id, [])
        if len(hist) > MAX_HISTORY_MESSAGES * 2:
            hist = hist[-MAX_HISTORY_MESSAGES * 2:]
        return hist

    def add_to_history(self, user_id: int, role: str, content: str) -> None:
        if user_id not in self._history:
            self._history[user_id] = []
        self._history[user_id].append({"role": role, "content": content})
        self._history[user_id] = self._trim_history(user_id)

    def clear_history(self, user_id: int) -> None:
        if user_id in self._history:
            del self._history[user_id]

    async def _request(
        self,
        messages: list[dict[str, str]],
        model: str,
        max_tokens: int,
        temperature: float = 0.3,
        response_format: dict | None = None
    ) -> AIResponse:
        self._check_day_rollover()
        session = await self._ensure_session()

        payload = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": False,
        }
        if response_format:
            payload["response_format"] = response_format

        # Check cache for non-streaming, deterministic requests
        cache_key = hashlib.sha256(
            f"{model}:{json.dumps(messages, sort_keys=True)}:{max_tokens}:{temperature}".encode()
        ).hexdigest()[:32]

        cached = self.cache.get(cache_key, model)
        if cached:
            log.debug("Cache hit for model %s", model)
            return AIResponse(content=cached["content"], tokens_used=cached["tokens_used"],
                              model=model, cached=True)

        headers = {
            "Authorization": f"Bearer {self.provider.api_key}",
            "Content-Type": "application/json",
        }

        url = f"{self.provider.base_url.rstrip('/')}/chat/completions"
        retryable = (429, 500, 502, 503, 504)
        # Hard wall-clock budget for this call (all attempts). The provider can
        # stay down for minutes; one call must never stall a whole cycle.
        deadline = time.monotonic() + AI_CALL_DEADLINE

        async def _post() -> tuple[int, Any, str]:
            """One HTTP attempt, respecting the client rate limit."""
            remaining = deadline - time.monotonic()
            if remaining <= 5:
                return 0, None, "AI deadline exhausted"
            attempt_timeout = min(AI_TIMEOUT, remaining)
            async with self._sem:
                gap = time.monotonic() - self._last_request_at
                if gap < AI_MIN_INTERVAL:
                    await asyncio.sleep(AI_MIN_INTERVAL - gap)
                try:
                    self._last_request_at = time.monotonic()
                    timeout = aiohttp.ClientTimeout(total=attempt_timeout)
                    async with session.post(
                        url, json=payload, headers=headers, timeout=timeout
                    ) as resp:
                        if resp.status == 200:
                            return resp.status, await resp.json(), ""
                        return resp.status, None, await resp.text()
                except aiohttp.ClientError as exc:
                    return 0, None, str(exc)
                except (asyncio.TimeoutError, TimeoutError) as exc:
                    # aiohttp surfaces hangs as a bare TimeoutError, which is not
                    # an aiohttp.ClientError; treat it as a retryable failure
                    return 0, None, f"timeout after {attempt_timeout:.0f}s: {exc or ''}"

        data: Any = None
        last_error = ""
        for attempt in range(AI_MAX_RETRIES):
            status, data, text = await _post()
            if status == 200 and data is not None:
                break
            last_error = text or f"HTTP {status}"
            if status == 200:
                last_error = "empty body"
            if status and status not in retryable and status != 0:
                raise RuntimeError(f"API {status}: {last_error}")
            if attempt == AI_MAX_RETRIES - 1:
                break
            if time.monotonic() >= deadline:
                break
            wait = min(2 ** attempt * 4, 15)
            log.warning(
                "AI request failed (%s), retry %d/%d in %ds",
                last_error[:80], attempt + 1, AI_MAX_RETRIES, wait,
            )
            await asyncio.sleep(wait)
        else:  # pragma: no cover - defensive
            raise RuntimeError(f"Max retries exceeded: {last_error}")

        if data is None:
            raise RuntimeError(f"AI request failed after retries: {last_error}")

        content = _extract_content(data)
        usage = data.get("usage", {}) or {}
        tokens = usage.get("total_tokens") or self._estimate_tokens(content)

        # The provider intermittently returns an empty body (empty content or a
        # reasoning-only completion). Retry a couple of times before giving up so
        # callers do not see a blank parse failure.
        blank_retries = 0
        while not content.strip() and blank_retries < 2:
            blank_retries += 1
            log.warning(
                "Empty completion from %s (attempt %d/2), retrying", model, blank_retries
            )
            await asyncio.sleep(1.5 * blank_retries)
            status, data, _ = await _post()
            if status != 200 or data is None:
                break
            content = _extract_content(data)
            usage = data.get("usage", {}) or {}
            tokens = usage.get("total_tokens") or self._estimate_tokens(content)

        self._daily_tokens += tokens
        # Постоянный учёт расхода токенов (п.13 ТЗ): счётчик в памяти живёт
        # только до перезапуска, а нужен отчёт за сутки и разрез по моделям.
        await self._log_token_usage(model, usage, tokens)

        # Only cache non-empty responses, otherwise blanks become sticky.
        if content.strip():
            self.cache.set(cache_key, model, {"content": content, "tokens_used": tokens})

        return AIResponse(content=content.strip(), tokens_used=tokens, model=model, cached=False)

    # ---------- Public Methods ----------

    async def chat(
        self,
        system: str,
        user: str,
        user_id: int = 0,
        model: str | None = None,
        max_tokens: int | None = None,
        temperature: float = 0.3,
        use_fast: bool = True,
        json_mode: bool = False,
        reasoning: bool = False
    ) -> AIResponse:
        """Main chat method with history."""
        model = model or (self.provider.model_fast if use_fast else self.provider.model_full)
        max_tokens = max_tokens or (MAX_TOKENS_FAST if use_fast else MAX_TOKENS_FULL)

        messages = [
            {"role": "system", "content": system},
            *self._trim_history(user_id),
            {"role": "user", "content": user},
        ]

        resp_fmt = {"type": "json_object"} if json_mode else None

        resp = await self._request(messages, model, max_tokens, temperature, resp_fmt)

        # Update history
        if user_id:
            self.add_to_history(user_id, "user", user)
            self.add_to_history(user_id, "assistant", resp.content)

        return resp

    async def _chat_json(
        self,
        system: str,
        user: str,
        max_tokens: int | None = None,
        use_fast: bool = True
    ) -> tuple[dict[str, Any], str]:
        """Ask for JSON and guarantee a dict result.

        The provider sometimes answers with a reasoning preamble instead of the
        object. On a failed parse we re-ask once, explicitly showing the bad
        answer and demanding JSON only.
        """
        resp = await self.chat(
            system, user, use_fast=use_fast, json_mode=True, max_tokens=max_tokens
        )
        data = parse_json_response(resp.content)
        if data:
            return data, resp.content

        log.warning("Non-JSON answer, retrying with correction: %s", resp.content[:200])
        retry_system = (
            system
            + "\n\nКРИТИЧНО: твой предыдущий ответ НЕ был валидным JSON. "
            "Не пиши рассуждений, не используй markdown-блоки, не добавляй пояснений. "
            "Ответь ТОЛЬКО одним объектом JSON, начиная с { и заканчивая }."
        )
        retry_user = (
            f"{user}\n\nТвой предыдущий ответ (невалидный):\n{resp.content[:500]}"
        )
        retry = await self.chat(
            retry_system, retry_user, use_fast=use_fast, json_mode=True, max_tokens=max_tokens
        )
        data = parse_json_response(retry.content)
        if not data:
            log.warning("Retry also failed to parse: %s", retry.content[:300])
        return data, retry.content

    async def extract_events(
        self,
        country_name: str,
        current_card: dict[str, Any],
        news_text: str,
        use_fast: bool = True
    ) -> dict[str, Any]:
        """Extract structured events from news for a country."""
        system = (
            "Ты — аналитик WARLORD RP. Извлеки события из новостей в строгом JSON.\n"
            "НЕ считай, НЕ выдумывай, НЕ меняй поля, которых нет в новостях.\n"
            "Формат: {\"events\": [{\"type\": \"build|population|budget|army|stability|tax|research|resource|rename\", "
            "\"params\": {...}}], \"summary\": \"краткий грамотный итог за период (1-2 строки, без эмодзи)\"}\n"
            "Типы событий:\n"
            "- build: {\"building\": \"factory|military_factory|port|...\", \"count\": 1}\n"
            "- population: {\"delta\": +N}\n"
            "- budget: {\"delta\": +N|-N, \"reason\": \"строка\"}\n"
            "- army: {\"infantry\": +N, \"light_vehicles\": +N, ...}\n"
            "- stability: {\"delta\": +N|-N, \"reason\": \"строка\"}\n"
            "- tax: {\"level\": \"low|standard|high|maximum\"}\n"
            "- research: {\"name\": \"название\", \"time_left_hours\": N}\n"
            "- resource: {\"name\": \"ОЯТ\", \"delta\": +N}\n"
            "- rename: {\"new_name\": \"...\"}\n"
            "Только события, прямо подтверждённые новостями. Если новостей нет — верни пустой events."
        )
        user = (
            f"СТРАНА: {country_name}\n"
            f"ТЕКУЩАЯ КАРТОЧКА (JSON):\n{json.dumps(current_card, ensure_ascii=False, default=str)}\n\n"
            f"НОВОСТИ:\n{news_text[:MAX_NEWS_CHARS]}"
        )
        data, _ = await self._chat_json(system, user, MAX_TOKENS_FULL, use_fast)
        return data

    async def extract_anketa(
        self,
        text: str,
        is_faction: bool = False
    ) -> dict[str, Any]:
        """Parse anketa text into structured data."""
        if is_faction:
            system = (
                "Распарсь анкету ГРУППИРОВКИ в JSON. Поля:\n"
                "prefix, full_name, commander, depends_on_country, base_country, "
                "strength, budget, tasks, equipment (list). "
                "Численность 5-10, бюджет 20-40к. Верни только JSON."
            )
        else:
            system = (
                "Распарсь анкету СТРАНЫ в JSON. Поля:\n"
                "name, ruler, population (60-120), army_infantry (25-50), "
                "budget (50000-100000), settlements (capital + 2 ПГТ), "
                "flag_file_id (string or null), color (not gray/white/black), "
                "party, ideology. Верни только JSON."
            )
        user = f"ТЕКСТ АНКЕТЫ:\n{text}"
        data, _ = await self._chat_json(system, user, MAX_TOKENS_ANKETA, True)
        return data

    async def extract_entities(
        self,
        texts: list[str]
    ) -> list[tuple[str, str]]:
        """Detect new entities (countries/factions/persons) from news."""
        system = (
            "Найди в текстах новые сущности: страны, группировки, личности. "
            "Верни JSON объект {\"entities\": [{\"type\": \"country|faction|person\", "
            "\"name\": \"...\"}]}. Верни только JSON."
        )
        user = "ТЕКСТЫ:\n" + "\n---\n".join(texts[:15])
        data, _ = await self._chat_json(system, user, MAX_TOKENS_FULL, True)
        items = data.get("entities")
        if not isinstance(items, list):
            items = data if isinstance(data, list) else []
        return [
            (str(d.get("type") or ""), str(d.get("name") or ""))
            for d in items
            if isinstance(d, dict) and d.get("type") and d.get("name")
        ]

    async def generate_summary(
        self,
        country_name: str,
        events: list[dict[str, Any]],
        max_tokens: int = 200
    ) -> str:
        """Generate literary summary for card."""
        system = (
            "Напиши краткий деловой итог событий за последние 2 часа для карточки страны. "
            "Одно-два законченных предложения грамотным русским языком, без вводных слов, "
            "без смайликов и без воды. Не выдумывай факты — только то, что есть в событиях."
        )
        user = f"Страна: {country_name}\nСобытия: {json.dumps(events, ensure_ascii=False)}"
        resp = await self.chat(system, user, use_fast=True, max_tokens=max_tokens)
        return resp.content

    async def build_country_update(
        self,
        country_name: str,
        old_card: dict[str, Any],
        news: str,
        use_fast: bool = False
    ) -> str:
        """Update country card from news. Returns raw JSON string.

        Full card text is rendered locally by the template, so the model
        only returns the changed fields.
        """
        system = (
            "Ты — аналитик карточек WARLORD RP. По новостям актуализируй карточку страны.\n"
            "⚠️ ЖЁСТКОЕ ПРАВИЛО: ОЯТ (отработанное ядерное топливо) и ядерные материалы РАЗРЕШЕНЫ — "
            "их можно производить, хранить, перерабатывать, торговать, использовать в энергетике. "
            "ХИМИЧЕСКИЕ ГАЗЫ (иприт, хлор, фосген) РАЗРЕШЕНЫ к производству и применению. "
            "ЗАПРЕЩЕНЫ только: биологическое оружие, лазерное/плазменное/антиграв оружие, "
            "боевые роботы, ТЯО, космос.\n"
            "Требования:\n"
            "- Правь только факты, подтверждённые новостями; остальное оставь как было.\n"
            "- `changes` — ТОЛЬКО изменённые поля (числа — числа, строки — строки).\n"
            "- `effects` — числа ПО СМЫСЛУ новости, со знаком (минус = потеря).\n"
            "  Разрешённые ключи: army_infantry, army_light_vehicles, army_heavy_wheeled,\n"
            "  army_artillery, army_drones, army_navy, population, budget,\n"
            "  stability, war_support, happiness.\n"
            "  Ориентиры по величине (для страны с населением ~100):\n"
            "    мобилизация/призыв        → army_infantry +20..+60, war_support +5..+15,\n"
            "                               happiness -5..-15\n"
            "    демобилизация/расслабление → army_infantry -20..-60, war_support -10\n"
            "    война/нападение/оборона    → army_infantry +15..+40, stability -5..-20,\n"
            "                               war_support +10..+25, budget -10000..-30000\n"
            "    переговоры/мирный договор  → stability +5..+15, war_support -10..-20\n"
            "    налоги повышены            → budget +10000..+30000, happiness -5..-15\n"
            "    налоги снижены             → budget -5000..-15000, happiness +5..+15\n"
            "    стройка/завод/линия        → population +5..+20, budget -10000..-30000\n"
            "    урожай/приток людей       → population +5..+20\n"
            "    голод/эпидемия/потоп      → population -5..-30, happiness -10..-25,\n"
            "                               stability -10..-25\n"
            "    революция/мятеж           → stability -15..-40, war_support -10..-20\n"
            "    выборы/стабилизация        → stability +10..+25, happiness +5..+15\n"
            "    техника/техника получена   → соответствующее army_* +1..+10\n"
            "  В `changes` НЕ трогай: army_*, population, budget, income_daily,\n"
            "  expenses_daily, population_capacity, stability, war_support, happiness —\n"
            "  эти числа меняются только через `effects`.\n"
            "- Базовые показатели (доходы, содержание армии, вместимость) считает бот по формулам.\n"
            "- `summary` — краткий литературный пересказ событий периода (1–3 коротких строки).\n"
            "- `notifications` — массив коротких уведомлений, до 3.\n"
            "- Каждый элемент lists: resources/settlements/industry/research имеет структуру "
            '{"name": "...", "type": "...", "value": число или ключ}.\n'
            "- `questions_for_admin` — не более 3 вопросов, только если критично.\n"
            'Ответ — строго JSON {"changes": {...}, "effects": {...}, "summary": "...", '
            '"notifications": [...], "questions_for_admin": [...]} без markdown-обёртки.'
        )
        user = (
            f"СТРАНА: {country_name}\n\n"
            f"ТЕКУЩАЯ КАРТОЧКА (JSON):\n{json.dumps(old_card, ensure_ascii=False, default=str)}\n\n"
            f"НОВОСТИ:\n{news[:MAX_NEWS_CHARS]}"
        )
        data, raw = await self._chat_json(system, user, MAX_TOKENS_FULL, use_fast)
        return raw if data else ""

    async def build_faction_update(
        self,
        faction_name: str,
        old_card: dict[str, Any],
        news: str,
        use_fast: bool = False
    ) -> str:
        """Update faction card from news. Returns raw JSON string."""
        system = (
            "Ты — аналитик карточек WARLORD RP (группировки).\n"
            "⚠️ ЖЁСТКОЕ ПРАВИЛО: ОЯТ и ядерные материалы РАЗРЕШЕНЫ — производить, хранить, "
            "перерабатывать, торговать. ХИМИЧЕСКИЕ ГАЗЫ РАЗРЕШЕНЫ. "
            "ЗАПРЕЩЕНЫ только: биологическое оружие, лазерное/плазменное/антиграв оружие, "
            "боевые роботы, ТЯО, космос.\n"
            "Требования:\n"
            "- Правь только подтверждённые новостями факты.\n"
            "- `changes` — ТОЛЬКО изменившиеся поля.\n"
            "- Бюджет (budget) — НЕ менять: его считает бот сам. Валюта — вирт-рубли (ВР).\n"
            "- `summary` — краткие итоги периода (1–3 строки).\n"
            "- Каждый элемент lists: equipment/contracts/wars имеет структуру "
            '{"name": "...", "value": число или ключ}.\n'
            "- `questions_for_admin` — не более 3, только критичные.\n"
            'Ответ — строго JSON {"changes": {...}, "summary": "...", '
            '"questions_for_admin": [...]} без markdown-обёртки.'
        )
        user = (
            f"ГРУППИРОВКА: {faction_name}\n\n"
            f"ТЕКУЩАЯ КАРТОЧКА (JSON):\n{json.dumps(old_card, ensure_ascii=False, default=str)}\n\n"
            f"НОВОСТИ:\n{news[:MAX_NEWS_CHARS]}"
        )
        data, raw = await self._chat_json(system, user, MAX_TOKENS_FULL, use_fast)
        return raw if data else ""

    async def answer_question(
        self,
        question: str,
        context: str = "",
        user_id: int = 0
    ) -> str:
        """Answer player question about WARLORD RP rules/mechanics/countries."""
        system = (
            "Ты — грамотный помощник по военно-политической игре (ВПИ) WARLORD RP. "
            "Отвечай на русском языке литературно и по существу: правильная грамматика, "
            "законченные предложения, без канцелярита и без смайликов. "
            "Опирайся только на факты из контекста; если данных нет — честно скажи об этом и не выдумывай. "
            "Длинный ответ разбивай на короткие абзацы или список. Объём — до 300 токенов."
        )
        user = f"Контекст (БД):\n{context}\n\nВопрос: {question}"
        resp = await self.chat(system, user, user_id=user_id, use_fast=True, max_tokens=MAX_TOKENS_FAST)
        return resp.content

    def get_daily_token_report(self) -> str:
        return f"Токенов за день: {self._daily_tokens} (провайдер: {self.provider.name})"

    async def _log_token_usage(self, model: str, usage: dict, tokens: int) -> None:
        """Записать расход в token_usage, если к AIClient подключена БД.

        Ошибка логирования не должна ломать обработку новостей, поэтому
        всё глотаем.
        """
        db = getattr(self, "db", None)
        if db is None:
            return
        try:
            await db.log_tokens(
                str(model),
                int(usage.get("prompt_tokens") or 0),
                int(usage.get("completion_tokens") or 0),
            )
        except Exception as exc:  # noqa: BLE001
            log.debug("token_usage не записан: %s", exc)

    def attach_db(self, db) -> None:
        """Подключить БД для учёта токенов (вызывается из main)."""
        self.db = db

    async def daily_token_report(self) -> str:
        """Отчёт за сутки из БД с разрезом по моделям."""
        db = getattr(self, "db", None)
        if db is None:
            return self.get_daily_token_report()
        try:
            rows = await db.tokens_by_model(minutes=1440)
        except Exception as exc:  # noqa: BLE001
            log.debug("token_usage отчёт не получен: %s", exc)
            return self.get_daily_token_report()
        if not rows:
            return "Токенов за 24 часа: 0"
        total = 0
        by_model: dict[str, int] = {}
        for r in rows:
            data = dict(r)
            model = str(data.get("model") or "?")
            n = int(data.get("input_tokens") or 0) + int(data.get("output_tokens") or 0)
            by_model[model] = by_model.get(model, 0) + n
            total += n
        lines = [f"📊 Токенов за 24 часа: {total} "
                 f"(провайдер: {self.provider.name})"]
        for m, n in sorted(by_model.items(), key=lambda kv: -kv[1]):
            lines.append(f"• {m}: {n}")
        return "\n".join(lines)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()