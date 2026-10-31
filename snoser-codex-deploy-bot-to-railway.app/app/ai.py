"""
AI Client with DeepSeek/Groq support, token economy, caching, and prompt management.
"""
from __future__ import annotations
import hashlib
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

import aiohttp

from config import (
    get_provider_config, MAX_TOKENS_FAST, MAX_TOKENS_FULL,
    MAX_HISTORY_MESSAGES, CACHE_TTL_HOURS, MAX_NEWS_CHARS
)

log = logging.getLogger(__name__)

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

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if not self.session or self.session.closed:
            timeout = aiohttp.ClientTimeout(total=60)
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

        for attempt in range(3):
            try:
                async with session.post(url, json=payload, headers=headers) as resp:
                    if resp.status == 429:
                        wait = min(2 ** attempt * 5, 60)
                        log.warning("Rate limited, waiting %ds", wait)
                        import asyncio
                        await asyncio.sleep(wait)
                        continue
                    if resp.status != 200:
                        text = await resp.text()
                        raise RuntimeError(f"API {resp.status}: {text}")
                    data = await resp.json()
                    break
            except aiohttp.ClientError as e:
                if attempt == 2:
                    raise
                import asyncio
                await asyncio.sleep(2 ** attempt)
        else:
            raise RuntimeError("Max retries exceeded")

        choice = data["choices"][0]
        content = choice["message"]["content"] or ""
        usage = data.get("usage", {})
        tokens = usage.get("total_tokens", self._estimate_tokens(content))

        self._daily_tokens += tokens

        # Cache successful response
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
            "Формат: {\"events\": [{\"type\": \"build|population|budget|army|stability|tax|research|resource|unreg|rename\", "
            "\"params\": {...}}], \"summary\": \"краткий итог за период (1-2 строки)\"}\n"
            "Типы событий:\n"
            "- build: {\"building\": \"factory|military_factory|port|...\", \"count\": 1}\n"
            "- population: {\"delta\": +N}\n"
            "- budget: {\"delta\": +N|-N, \"reason\": \"строка\"}\n"
            "- army: {\"infantry\": +N, \"light_vehicles\": +N, ...}\n"
            "- stability: {\"delta\": +N|-N, \"reason\": \"строка\"}\n"
            "- tax: {\"level\": \"low|standard|high|maximum\"}\n"
            "- research: {\"name\": \"название\", \"time_left_hours\": N}\n"
            "- resource: {\"name\": \"ОЯТ\", \"delta\": +N}\n"
            "- unreg: {}\n"
            "- rename: {\"new_name\": \"...\"}\n"
            "Только события, прямо подтверждённые новостями. Если новостей нет — верни пустой events."
        )
        user = (
            f"СТРАНА: {country_name}\n"
            f"ТЕКУЩАЯ КАРТОЧКА (JSON):\n{json.dumps(current_card, ensure_ascii=False, default=str)}\n\n"
            f"НОВОСТИ:\n{news_text[:MAX_NEWS_CHARS]}"
        )
        return (await self.chat(system, user, use_fast=use_fast, json_mode=True, max_tokens=MAX_TOKENS_FULL)).content

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
        resp = await self.chat(system, user, use_fast=True, json_mode=True, max_tokens=MAX_TOKENS_FAST)
        try:
            return json.loads(resp.content)
        except json.JSONDecodeError:
            log.error("Failed to parse anketa JSON: %s", resp.content)
            return {}

    async def extract_entities(
        self,
        texts: list[str]
    ) -> list[tuple[str, str]]:
        """Detect new entities (countries/factions/persons) from news."""
        system = (
            "Найди в текстах новые сущности: страны, группировки, личности. "
            "Верни JSON массив: [{\"type\": \"country|faction|person\", \"name\": \"...\"}]"
        )
        user = "ТЕКСТЫ:\n" + "\n---\n".join(texts[:15])
        resp = await self.chat(system, user, use_fast=True, json_mode=True, max_tokens=MAX_TOKENS_FAST)
        try:
            data = json.loads(resp.content)
            return [(d["type"], d["name"]) for d in data if d.get("type") and d.get("name")]
        except (json.JSONDecodeError, KeyError):
            return []

    async def generate_summary(
        self,
        country_name: str,
        events: list[dict[str, Any]],
        max_tokens: int = 200
    ) -> str:
        """Generate literary summary for card."""
        system = (
            "Напиши краткий литературный пересказ событий за 2 часа для карточки страны. "
            "Максимум 3 короткие строки, деловой стиль, без воды. На русском."
        )
        user = f"Страна: {country_name}\nСобытия: {json.dumps(events, ensure_ascii=False)}"
        resp = await self.chat(system, user, use_fast=True, max_tokens=max_tokens)
        return resp.content

    async def answer_question(
        self,
        question: str,
        context: str = "",
        user_id: int = 0
    ) -> str:
        """Answer player question about WARLORD RP rules/mechanics/countries."""
        system = (
            "Ты — справочник по WARLORD RP. Отвечай кратко, точно, по правилам. "
            "Максимум 300 токенов. Если не знаешь — честно скажи. Не выдумывай."
        )
        user = f"Контекст (БД):\n{context}\n\nВопрос: {question}"
        resp = await self.chat(system, user, user_id=user_id, use_fast=True, max_tokens=MAX_TOKENS_FAST)
        return resp.content

    def get_daily_token_report(self) -> str:
        return f"Токенов за день: {self._daily_tokens} (провайдер: {self.provider.name})"

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()