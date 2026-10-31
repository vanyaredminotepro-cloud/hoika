import json
from typing import Any

from app.core.rules_prompt import WARLORD_RULES

CARD_TEMPLATE = """🆔 {country_name}
{flag_line}
👑 Правитель: {ruler}
🏛 Партия: {party}
🎨 Идеология: {ideology}
🌍 Статус: {exists_line}
⭐ Контроль: {controlled_by}

👥 Население: {population} / {population_capacity}
💰 Казна: {budget}
📈 Доход/день: +{income_daily}
📉 Расходы/день: -{expenses_daily}
🎖 Армия: {army}
🧾 Налоги: {tax_level}%

🏘 Поселения: {settlements}
🏭 Промышленность: {industry}
🔬 Исследования: {research}

😊 Стабильность: {stability}
🫂 Поддержка войны: {war_support}
🎉 Счастье: {happiness}

🚧 Строится: {pending_buildings}
🔭 Исследуется: {pending_research}
🌍 Колонизация: {pending_colonization}

📝 Изменения: {changes}
📌 Сводка: {summary}
🔔 Уведомления: {notifications}
"""

_existence_map = {True: "Существует ✅", False: "Не существует ❌"}


def build_card_text(card: dict[str, Any]) -> str:
    flag = str(card.get("flag_file_id") or "").strip()
    flag_line = "🖼 Флаг загружен" if flag else "🖼 Флаг не загружен"
    exists = bool(card.get("exists", True))
    return CARD_TEMPLATE.format(
        country_name=card.get("country_name", "?"),
        flag_line=flag_line,
        ruler=card.get("ruler", "—") or "—",
        party=card.get("party", "—") or "—",
        ideology=card.get("ideology", "—") or "—",
        exists_line=_existence_map[exists],
        controlled_by=card.get("controlled_by", "—") or "—",
        population=card.get("population", 0),
        population_capacity=card.get("population_capacity", 0),
        budget=card.get("budget", 0),
        income_daily=card.get("income_daily", 0),
        expenses_daily=card.get("expenses_daily", 0),
        army=card.get("army", 0),
        tax_level=card.get("tax_level", 0),
        settlements=_fmt_list(card.get("settlements")),
        industry=_fmt_list(card.get("industry")),
        research=_fmt_list(card.get("research")),
        stability=card.get("stability", 0),
        war_support=card.get("war_support", 0),
        happiness=card.get("happiness", 0),
        pending_buildings=_fmt_list(card.get("pending_buildings")),
        pending_research=_fmt_list(card.get("pending_research")),
        pending_colonization=_fmt_list(card.get("pending_colonization")),
        changes=_fmt_changes(card.get("changes")),
        summary=card.get("summary", "") or "—",
        notifications=_fmt_list(card.get("notifications")),
    )


def _fmt_list(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, str):
        value = _safe_json(value)
    if not isinstance(value, list):
        return str(value) or "—"
    return ", ".join(str(v) for v in value) if value else "—"


def _fmt_changes(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, str):
        value = _safe_json(value)
    if isinstance(value, list):
        return "; ".join(str(v) for v in value) if value else "—"
    return str(value) or "—"


def _safe_json(raw: str) -> Any:
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return raw


SYSTEM_EXTRACTION_PROMPT = f"""Ты — автономный ИИ-механик военно-политической sim-игры WARLORD RP.
Твоя работа: читать сообщения из форум-разделов группы, следить за всеми странами и обновлять их карточки.

ОБЩИЕ ПРАВИЛА ВСЕЛЕННОЙ:
{WARLORD_RULES}

ЗАДАЧА:
1. Изучи сообщения из разделов «новости стран», «расширения», «войны стран».
2. Для КАЖДОЙ упомянутой страны определи, что изменилось: население, казна/бюджет, армия, поселения, промышленность, исследования, стабильность, поддержка войны, счастье, стройки, налоги, правитель (если сменился), существование страны.
3. Если в сообщениях нет конкретных данных для поля — НЕ выдумывай, оставь поле без изменений (не включай его в changes).
4. Если данных не хватает, чтобы точно посчитать изменение — не угадывай, а запиши вопрос в questions_for_admin.
5. Для каждой страны верни полную актуальную карточку (все поля, как они должны выглядеть после изменений), чтобы бот мог её сохранить.
6. Отвечай СТРОГО в формате JSON, без markdown-обёрток и без пояснений.

ФОРМАТ ОТВЕТА (JSON):
{{
  "cards": [
    {{
      "country_name": "Название страны",
      "flag_file_id": "старый file_id флага или пустая строка",
      "ruler": "Правитель",
      "party": "Правящая партия",
      "ideology": "Идеология",
      "exists": true,
      "controlled_by": "что контролирует",
      "population": 100,
      "population_capacity": 200,
      "budget": 100000,
      "income_daily": 1000,
      "expenses_daily": 800,
      "army": 500,
      "tax_level": 20,
      "settlements": ["название поселения"],
      "industry": ["отрасль промышленности"],
      "research": ["технологии/исследования"],
      "stability": 70,
      "war_support": 50,
      "happiness": 60,
      "pending_buildings": ["стройки"],
      "pending_research": ["исследования в процессе"],
      "pending_colonization": ["колонии в процессе"],
      "changes": ["краткое описание изменения человеческим языком"],
      "summary": "общая сводка одним-двумя предложениями",
      "notifications": ["важные события для лидера страны"]
    }}
  ],
  "questions_for_admin": [
    {{
      "country_name": "Название страны",
      "question": "Текст вопроса, на который админ должен ответить"
    }}
  ]
}}
"""


def build_user_prompt(messages: list[dict[str, Any]], current_cards: list[dict[str, Any]]) -> str:
    messages_text = "\n".join(
        f"[{m.get('topic', '?')}] {m.get('date', '')}: {m.get('text', '')}"
        for m in messages
    ) or "(пусто)"

    cards_text = (
        "Текущее состояние карточек стран (для контекста, что уже известно):\n"
        + json.dumps(current_cards, ensure_ascii=False, default=str)
    )

    return (
        "Раздел «Сообщения для анализа»:\n"
        f"{messages_text}\n\n"
        "########\n"
        f"{cards_text}\n\n"
        "Теперь верни JSON с обновлёнными карточками."
    )


def build_question_admin_prompt(question: str) -> str:
    return (
        "Ты — ИИ-механик WARLORD RP. Администратор ответил на твой вопрос.\n"
        f"Вопрос бота: {question}\n"
        "Ответ админа приходит отдельным сообщением. Просто жди ответа, ничего не рассылай."
    )