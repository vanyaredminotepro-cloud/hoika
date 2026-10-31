import asyncio
import json
import logging
import time
from urllib import request as urlrequest
from app.config import config

logger = logging.getLogger(__name__)

try:
    from google import genai
    from google.genai import types
except ImportError:  # pragma: no cover - depends on optional google-genai package
    genai = None
    types = None

_GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
_OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
_DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
_MISTRAL_URL = "https://api.mistral.ai/v1/chat/completions"


class AIService:
    QUOTA_COOLDOWN_SECONDS = 300

    def __init__(self) -> None:
        self.api_key = config.gemini_api_key or ""
        self.model = config.gemini_model or "gemini-3.6-flash"
        self.client = None
        self._quota_paused_until = 0.0
        if genai is None:
            logger.warning("google-genai is not installed; AI formatting is disabled.")
        elif not self.api_key:
            logger.warning("GEMINI_API_KEY is not set; AI formatting is disabled.")
        else:
            try:
                self.client = genai.Client(api_key=self.api_key)
            except Exception as exc:  # pragma: no cover
                logger.error("Failed to initialize Gemini client: %s", exc)

    @property
    def available(self) -> bool:
        return (
            self.client is not None
            or bool(config.groq_api_key)
            or bool(config.openrouter_api_key)
            or bool(config.deepseek_api_key)
            or bool(config.mistral_api_key)
            or bool(config.cloudflare_account_id and config.cloudflare_api_token)
        )

    @property
    def quota_paused(self) -> bool:
        return time.monotonic() < self._quota_paused_until

    def _set_quota_paused(self, retry_seconds: float = 45.0) -> None:
        pause = max(self.QUOTA_COOLDOWN_SECONDS, float(retry_seconds) + 5)
        self._quota_paused_until = time.monotonic() + pause

    @staticmethod
    def _is_quota_error(exc: Exception) -> bool:
        status = str(getattr(exc, "code", ""))
        message = str(exc)
        if "429" in status or "RESOURCE_EXHAUSTED" in message:
            return True
        return "quota" in message.lower() and "rate" in message.lower()

    async def _chat_completion(self, url: str, api_key: str, model: str, prompt: str, max_output_tokens: int) -> str:
        if not api_key:
            return ""
        payload = json.dumps(
            {
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.4,
                "max_tokens": max_output_tokens,
            }
        ).encode("utf-8")
        try:
            req = urlrequest.Request(
                url,
                data=payload,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                method="POST",
            )
            data = await asyncio.to_thread(lambda: json.loads(urlrequest.urlopen(req, timeout=30).read().decode("utf-8")))
            content = str(data["choices"][0]["message"]["content"]).strip()
            return content or ""
        except Exception as exc:  # pragma: no cover - network/API failure
            body = ""
            if isinstance(exc, urlrequest.HTTPError):
                try:
                    body = exc.read().decode("utf-8", errors="replace")[:500]
                except Exception:
                    body = ""
            logger.error("Chat completion failed (%s): %s %s", url, exc, body)
            return ""

    async def _groq_chat(self, prompt: str, max_output_tokens: int) -> str:
        return await self._chat_completion(
            _GROQ_URL, config.groq_api_key, config.groq_model, prompt, max_output_tokens
        )

    async def _openrouter_chat(self, prompt: str, max_output_tokens: int) -> str:
        return await self._chat_completion(
            _OPENROUTER_URL,
            config.openrouter_api_key,
            config.openrouter_model,
            prompt,
            max_output_tokens,
        )

    async def _deepseek_chat(self, prompt: str, max_output_tokens: int) -> str:
        return await self._chat_completion(
            _DEEPSEEK_URL,
            config.deepseek_api_key,
            config.deepseek_model,
            prompt,
            max_output_tokens,
        )

    async def _mistral_chat(self, prompt: str, max_output_tokens: int) -> str:
        return await self._chat_completion(
            _MISTRAL_URL,
            config.mistral_api_key,
            config.mistral_model,
            prompt,
            max_output_tokens,
        )

    async def _cloudflare_chat(self, prompt: str, max_output_tokens: int) -> str:
        if not config.cloudflare_account_id or not config.cloudflare_api_token:
            return ""
        url = f"https://api.cloudflare.com/client/v4/accounts/{config.cloudflare_account_id}/ai/run/{config.cloudflare_model}"
        payload = json.dumps(
            {
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": max_output_tokens,
            }
        ).encode("utf-8")
        try:
            req = urlrequest.Request(
                url,
                data=payload,
                headers={"Authorization": f"Bearer {config.cloudflare_api_token}", "Content-Type": "application/json"},
                method="POST",
            )
            data = await asyncio.to_thread(lambda: json.loads(urlrequest.urlopen(req, timeout=45).read().decode("utf-8")))
            result = data.get("result") or {}
            content = str(result.get("response") or "").strip()
            return content or ""
        except Exception as exc:  # pragma: no cover - network/API failure
            body = ""
            if isinstance(exc, urlrequest.HTTPError):
                try:
                    body = exc.read().decode("utf-8", errors="replace")[:500]
                except Exception:
                    body = ""
            logger.error("Cloudflare AI request failed: %s %s", exc, body)
            return ""

    async def generate_content(self, prompt: str, max_output_tokens: int = 1000) -> str:
        # 1) Gemini (if available and not paused)
        if self.client is not None and not self.quota_paused:
            for attempt in range(3):
                try:
                    loop = asyncio.get_running_loop()
                    response = await loop.run_in_executor(
                        None,
                        lambda: self.client.models.generate_content(
                            model=self.model,
                            contents=prompt,
                            config=types.GenerateContentConfig(
                                temperature=0.4,
                                max_output_tokens=max_output_tokens,
                            ),
                        ),
                    )
                    if response and getattr(response, "text", None):
                        return response.text.strip()
                except Exception as exc:
                    if self._is_quota_error(exc):
                        retry_delay = 0.0
                        try:
                            error_obj = exc.args[0]
                            payload = error_obj.get("error", {}) if isinstance(error_obj, dict) else error_obj
                            for item in (payload.get("details") or []):
                                if "RetryInfo" in str(item.get("@type", "")):
                                    delay = str(item.get("retryDelay", "")).strip().replace("s", "")
                                    if delay.replace(".", "", 1).isdigit():
                                        retry_delay = float(delay)
                        except Exception:
                            retry_delay = 0.0
                        logger.error(
                            "Gemini quota exceeded (attempt %s/3): %s", attempt + 1, exc
                        )
                        self._set_quota_paused(retry_delay or 45.0)
                        break
                    logger.error("Gemini API error (attempt %s/3): %s", attempt + 1, exc)
                    await asyncio.sleep(2)

        # 2) Cloudflare Workers AI (free 10k neurons/day, без гео-блокировок)
        cloudflare = await self._cloudflare_chat(prompt, max_output_tokens)
        if cloudflare:
            return cloudflare

        # 3) Mistral fallback
        mistral = await self._mistral_chat(prompt, max_output_tokens)
        if mistral:
            return mistral

        # 4) DeepSeek fallback (works in most regions, cheap/free credits)
        deepseek = await self._deepseek_chat(prompt, max_output_tokens)
        if deepseek:
            return deepseek

        # 5) OpenRouter fallback (free models, geo-blocked in some regions)
        openrouter = await self._openrouter_chat(prompt, max_output_tokens)
        if openrouter:
            return openrouter

        # 6) Groq fallback (geo-blocked in some regions)
        groq = await self._groq_chat(prompt, max_output_tokens)
        if groq:
            return groq
        return ""

    async def format_news(self, raw_title: str, raw_text: str, source_url: str = "") -> str:
        """Переписывает и стилизует новость под RP-формат с добавлением плейсхолдеров эмодзи."""
        if not self.available:
            return f"<b>{raw_title}</b>\n\n{raw_text}"

        available_tags = ", ".join([f"{{{k}}}" for k in config.custom_emoji_catalog.keys()])

        prompt = f"""
Ты — главный редактор новостного агентства в военно-политической RP-игре.
Перепиши следующую новость в строгом, официальном геополитическом стиле.

ТРЕБОВАНИЯ К ОФОРМЛЕНИЮ:
1. Заголовок должен быть лаконичным и выразительным.
2. Используй плейсхолдеры кастомных эмодзи в фигурных скобках перед важными блоками и заголовком.
   Доступные теги эмодзи: {available_tags}
3. Выделяй важные названия, цифры и ключевые факты жирным шрифтом (используй HTML-теги <b>текст</b>).
4. Текст должен быть структурированным (2-3 коротких абзаца).
5. Не используй Markdown-символы (* или _), используй ТОЛЬКО HTML-теги: <b>, <i>, <code>.

Исходная новость:
Заголовок: {raw_title}
Текст: {raw_text}
"""
        generated = await self.generate_content(prompt)
        if generated:
            return generated
        return f"<b>{{IMPORTANT}} {raw_title}</b>\n\n{raw_text}"


ai_service = AIService()