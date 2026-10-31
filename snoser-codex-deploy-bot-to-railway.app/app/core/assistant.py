import asyncio
import json
import logging
from urllib import request as urlrequest

from app.config import config
from app.core.ai_service import ai_service
from app.core.rules_prompt import WARLORD_RULES

logger = logging.getLogger(__name__)

HELP_TEXT = (
    "Я — ассистент проекта WARLORD RP. Спроси меня о правилах, механике, "
    "экономике, войне, строительстве или исследованиях, например:\n"
    "· /ai можно ли строить танки?\n"
    "· /ai какие налоги бывают?\n"
    "· /ai как провести мобилизацию?\n"
    "· /ai сколько стоит фабрика?"
)

_CYRILLIC = "абвгдеёжзийклмнопрстуфхцчшщъыьэюя"

_OFFTOPIC_RESPONSE = (
    "Я отвечаю только на вопросы о проекте WARLORD RP — его правилах и механике. "
    "Вопросы не по теме я обсуждать не могу."
)


def _is_russian(text: str) -> bool:
    """Эвристика: ответ считается русским, если кириллицы ≥ латиницы."""
    lower = (text or "").lower()
    cyr = sum(1 for ch in lower if ch in _CYRILLIC)
    lat = sum(1 for ch in lower if "a" <= ch <= "z")
    return cyr >= lat and cyr > 0


def _build_system_prompt(rules: str) -> str:
    return (
        "Ты — официальный ИИ-ассистент военно-политической RP-игры WARLORD RP.\n"
        "ЖЁСТКИЕ ПРАВИЛА ОТВЕТОВ:\n"
        "1. Отвечай СТРОГО на русском языке. Ответы на английском или любом другом языке ЗАПРЕЩЕНЫ.\n"
        "2. Обсуждай ТОЛЬКО WARLORD RP и его правила/механики. Если вопрос не касается WARLORD RP "
        "(приветствие, личный разговор, флуд, шутки, другие игры, общие темы) — вежливо откажись "
        "отвечать, напиши, что обсуждаешь только WARLORD RP.\n"
        "3. Отвечай ТОЛЬКО по приведённому ниже своду правил, ничего не выдумывай. "
        "Если в правилах ответа нет — так и скажи.\n"
        "4. Отвечай кратко (до 300-350 символов), по делу, официальным строгим стилем.\n"
        "5. Не используй Markdown-символы и HTML-теги.\n\n"
        "ОФИЦИАЛЬНЫЙ СВОД ПРАВИЛ И МЕХАНИК WARLORD RP:\n\n"
        f"{rules}"
    )


class RPAssistant:
    def __init__(self) -> None:
        self.system_prompt = _build_system_prompt(WARLORD_RULES)

    async def _llm_answer(self, question: str, force_russian: bool = False) -> str | None:
        content = self.system_prompt
        if force_russian:
            content += (
                "\n\nВАЖНО: твой предыдущий ответ был не на русском языке. "
                "Перепиши ответ заново СТРОГО на русском."
            )

        if ai_service.available:
            generated = await ai_service.generate_content(
                f"{content}\n\nВопрос игрока: {question}\nОтвет:",
                max_output_tokens=2048,
            )
            if generated:
                return generated

        if config.openai_api_key:
            payload = json.dumps(
                {
                    "model": config.openai_model,
                    "messages": [
                        {"role": "system", "content": content},
                        {"role": "user", "content": question},
                    ],
                    "temperature": 0.2,
                    "max_tokens": 2000,
                }
            ).encode("utf-8")
            try:
                req = urlrequest.Request(
                    "https://api.openai.com/v1/chat/completions",
                    data=payload,
                    headers={"Authorization": f"Bearer {config.openai_api_key}", "Content-Type": "application/json"},
                    method="POST",
                )
                data = await asyncio.to_thread(lambda: json.loads(urlrequest.urlopen(req, timeout=20).read().decode("utf-8")))
                content = str(data["choices"][0]["message"]["content"]).strip()
                if content:
                    return content
            except Exception as exc:  # pragma: no cover - network/API failure
                logger.error("OpenAI assistant request failed: %s", exc)
        return None

    async def answer(self, question: str) -> str:
        question = (question or "").strip()
        if not question:
            return HELP_TEXT

        if (
            ai_service.quota_paused
            and not config.openai_api_key
            and not config.groq_api_key
            and not config.openrouter_api_key
            and not config.deepseek_api_key
            and not config.mistral_api_key
            and not config.cloudflare_account_id
        ):
            return (
                "⛔ Исчерпан дневной лимит запросов к ИИ (Free Tier Gemini: 20/день). "
                "Чтобы ассистент работал без ограничений, задайте бесплатный DEEPSEEK_API_KEY "
                "или CLOUDFLARE_API_TOKEN. Попробуйте позже."
            )

        reply = await self._llm_answer(question)
        if not reply:
            return "⛔ Ассистент сейчас недоступен. Попробуйте позже."
        if _is_russian(reply):
            return reply

        retried = await self._llm_answer(question, force_russian=True)
        if retried and _is_russian(retried):
            return retried
        return "⚠️ Прошу прощения, но я отвечаю только на русском языке. Повторите ваш вопрос."