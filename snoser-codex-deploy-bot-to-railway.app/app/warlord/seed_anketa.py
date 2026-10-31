"""
Стартовые данные из анкет (раздел 6 ТЗ, модуль CARDS).

Для стран из config.ANKETA_STATS_COUNTRIES берём данные из тем анкет
(ANKETA_CHAT_ID + ANKETA_TOPIC_ID). Требования раздела 6 к анкете:
  название, солдаты 25–50, граждане 60–120, территория, флаг, ссылка на канал,
  руководитель, столица + 2 ПГТ, бюджет 50k–100k, цвет (не серый/белый/чёрный).

Здесь две вещи:
  1. parse_anketa() — разбор текста анкеты в словарь полей. Работает на тексте
     из БД, сеть не трогает, поэтому тестируется обычными тестами.
  2. bootstrap() — создаёт/дополняет страну в БД для тех, кого ещё нет.

Чего этот модуль НЕ делает:
  * не выдумывает недостающие поля. Если в анкете нет бюджета - бюджет будет
    None, и страна получит data_error с запросом админу, а не правдоподобную
    выдумку. Раздел 11 запрещает публиковать население < 60, а раздел 13
    запрещает публиковать бред - выдумывать цифры хуже, чем не публиковать.
  * не перезаписывает страну, у которой stats_source уже manual.
"""
from __future__ import annotations

import logging
import re
from typing import Any

from . import config

log = logging.getLogger(__name__)

# Допустимые диапазоны из раздела 6.
SOLDIER_RANGE = (config.MIN_ARMY_START, config.MAX_ARMY_START)   # 25–50
CITIZEN_RANGE = (60, 120)
BUDGET_RANGE = (50_000, 100_000)

# Раздел 6: цвет не может быть серым, белым или чёрным.
FORBIDDEN_COLORS = {
    "серый", "серое", "белая", "белый", "белое", "чёрный", "чёрное",
    "черный", "черное", "grey", "gray", "white", "black",
}

# Синонимы названий стран в анкетах -> канонические (раздел 13 + RENAMES).
FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "population": ("граждане", "граждан", "население", "жители", "population"),
    "army": ("солдаты", "солдат", "армия", "войска", "военные", "army", "soldiers"),
    "budget": ("бюджет", "казна", "budget"),
    "ruler": ("правитель", "руководитель", "лидер", "глава", "ruler", "leader"),
    "capital": ("столица", "capital"),
    "color": ("цвет", "цвета", "цветов", "color"),
    "territory": ("территория", "территории", "территорий"),
    "settlements": ("населённые пункты", "населенные пункты", "нп", "города"),
    "ideology": ("идеология", "идеологии"),
    "party": ("партия", "партии"),
    "channel_url": ("канал", "ссылка на канал", "тгк", "tgk"),
    "flag": ("флаг", "флаги", "эмблема", "герб"),
}


def _find_value(text: str, keys: tuple[str, ...]) -> str | None:
    """Найти «Ключ: значение» по любому из синонимов. Регистр не важен."""
    for key in keys:
        # Ключ в начале строки, после него двоеточие (или тире/равно).
        pattern = rf"^\s*(?:[-*•]\s*)?{re.escape(key)}\s*[:\-—=]\s*(.+?)\s*$"
        match = re.search(pattern, text, re.IGNORECASE | re.MULTILINE)
        if match:
            return match.group(1).strip()
    return None


def _to_int(value: str | None) -> int | None:
    """«~310» / «310 чел.» / «1 200» -> int. None если не число."""
    if value is None:
        return None
    cleaned = re.sub(r"[^\d]", "", value)
    if not cleaned:
        return None
    try:
        return int(cleaned)
    except ValueError:
        return None


def _to_money(value: str | None) -> float | None:
    """«198000» / «198 000 руб» / «~50k» -> float."""
    if value is None:
        return None
    text = value.lower().replace(" ", "").replace("руб", "").replace("руб.", "")
    match = re.search(r"(\d+(?:[.,]\d+)?)\s*k", text)
    if match:
        try:
            return float(match.group(1).replace(",", ".")) * 1000
        except ValueError:
            return None
    match = re.search(r"\d[\d.,]*", text)
    if not match:
        return None
    try:
        return float(match.group(0).replace(" ", "").replace(",", "."))
    except ValueError:
        return None


def validate_color(color: str | None) -> str | None:
    """Раздел 6: серый/белый/чёрный запрещены. Нарушение -> None."""
    if not color:
        return None
    cleaned = color.strip()
    if cleaned.lower() in FORBIDDEN_COLORS:
        log.warning("seed_anketa: цвет %r запрещён разделом 6, игнорируем", cleaned)
        return None
    return cleaned


def _parse_settlements(value: str | None) -> list[dict[str, Any]]:
    """Столица + ПГТ -> [{name, count}].

    Разбирает «Ольборг, ПГТ: 2, Верхний: 1» так, чтобы «ПГТ: 2» не становилось
    двумя населёнными пунктами («ПГТ» и «2»). ПГТ - это обозначение типа, а не
    имя, поэтому из него делается одна запись с числом.
    """
    if not value:
        return []
    out: list[dict[str, Any]] = []
    for chunk in re.split(r"[,;]", value):
        name = chunk.strip()
        if not name:
            continue
        count = 0
        if ":" in name:
            name, raw_count = name.split(":", 1)
            name = name.strip()
            parsed = _to_int(raw_count)
            if parsed is not None:
                # «ПГТ: 2» -> это 2 посёлка городского типа.
                if re.fullmatch(r"(?i)пгт|пгт\.|пос[её]лок", name):
                    out.append({"name": "ПГТ", "count": parsed})
                    continue
                out.append({"name": name, "count": parsed})
                continue
        if name:
            out.append({"name": name, "count": count})
    return out


def parse_anketa(text: str, name_hint: str = "") -> dict[str, Any]:
    """Разобрать текст анкеты в поля страны раздела 6.

    Возвращает словарь. Отсутствующие поля равны None - их нельзя выдумывать.
    """
    body = text or ""
    data: dict[str, Any] = {
        "name": name_hint.strip() or "",
        "population": None,
        "army_infantry": None,
        "budget": None,
        "ruler": None,
        "capital": None,
        "color": None,
        "territory": None,
        "settlements_json": [],
        "ideology": None,
        "party": None,
        "channel_url": None,
        "flag_file_id": None,
    }

    # Название: из hint, иначе первая непустая строка без двоеточия.
    if not data["name"]:
        for line in body.splitlines():
            stripped = line.strip().lstrip("-*• ").strip()
            if stripped and ":" not in stripped and len(stripped) > 2:
                data["name"] = stripped
                break

    data["population"] = _to_int(_find_value(body, FIELD_ALIASES["population"]))
    data["army_infantry"] = _to_int(_find_value(body, FIELD_ALIASES["army"]))
    data["budget"] = _to_money(_find_value(body, FIELD_ALIASES["budget"]))
    data["ruler"] = _find_value(body, FIELD_ALIASES["ruler"])
    data["capital"] = _find_value(body, FIELD_ALIASES["capital"])
    data["color"] = validate_color(_find_value(body, FIELD_ALIASES["color"]))
    data["territory"] = _find_value(body, FIELD_ALIASES["territory"])
    data["ideology"] = _find_value(body, FIELD_ALIASES["ideology"])
    data["party"] = _find_value(body, FIELD_ALIASES["party"])
    data["channel_url"] = _find_value(body, FIELD_ALIASES["channel_url"])
    data["flag_file_id"] = _find_value(body, FIELD_ALIASES["flag"])

    data["name"] = config.normalize_name(data["name"]) or data["name"]
    data["settlements_json"] = _parse_settlements(
        _find_value(body, FIELD_ALIASES["settlements"]))
    if data["capital"] and not data["settlements_json"]:
        data["settlements_json"] = [{"name": data["capital"], "count": 0}]

    return data


def check_ranges(data: dict[str, Any]) -> list[str]:
    """Проверить попадание в диапазоны раздела 6. Вернём список претензий."""
    problems: list[str] = []
    pop = data.get("population")
    if pop is None:
        problems.append("население не указано")
    elif not (CITIZEN_RANGE[0] <= pop <= CITIZEN_RANGE[1]):
        problems.append(
            f"население {pop} вне диапазона {CITIZEN_RANGE[0]}-{CITIZEN_RANGE[1]}")
    army = data.get("army_infantry")
    if army is None:
        problems.append("число солдат не указано")
    elif not (SOLDIER_RANGE[0] <= army <= SOLDIER_RANGE[1]):
        problems.append(
            f"армия {army} вне диапазона {SOLDIER_RANGE[0]}-{SOLDIER_RANGE[1]}")
    budget = data.get("budget")
    if budget is None:
        problems.append("бюджет не указан")
    elif not (BUDGET_RANGE[0] <= budget <= BUDGET_RANGE[1]):
        # Не ошибка, а предупреждение: бюджет бывает и больше. Фиксируем, но не
        # блокируем - раздел 6 задаёт диапазон как ориентир для анкеты.
        log.info("seed_anketa: бюджет %s вне ориентира %s", budget, BUDGET_RANGE)
    if not data.get("color"):
        problems.append("цвет не указан или запрещён разделом 6")
    if not data.get("ruler"):
        problems.append("руководитель не указан")
    return problems


def _country_fields(data: dict[str, Any]) -> dict[str, Any]:
    """Подмножество полей для upsert_country. None-поля не пишем."""
    fields: dict[str, Any] = {"name": data["name"]}
    for key in ("population", "army_infantry", "ruler", "ideology", "party",
                "color", "settlements_json"):
        value = data.get(key)
        if value is None or value == []:
            continue
        fields[key] = value
    if data.get("budget") is not None:
        fields["budget"] = data["budget"]
    if data.get("population"):
        fields["population_capacity"] = data["population"]
    if data.get("channel_url"):
        fields["channel_url"] = data["channel_url"]
    if data.get("flag_file_id"):
        fields["flag_file_id"] = data["flag_file_id"]
    return fields


async def bootstrap(db, texts: dict[str, str] | None = None) -> dict[str, Any]:
    """Создать/дополнить страны раздела 6 из текстов анкет.

    texts: {название страны: текст анкеты}.

    texts=None означает «истории у меня нет»: в БД накопленных анкет не
    лежит (таблица parsed_news с сырыми текстами не существует), поэтому
    раньше этот случай молча проглатывал все страны и возвращал пустой отчёт.
    Теперь он честно предупреждает. Живая загрузка анкет из темы форума
    делается в Mechanic._load_anketa() на старте - там есть userbot.
    """
    created: list[str] = []
    updated: list[str] = []
    skipped: list[str] = []
    problems: dict[str, list[str]] = {}

    targets = list(config.ANKETA_STATS_COUNTRIES)
    if texts is None:
        log.warning(
            "seed_anketa: texts=None, накопленных анкет в БД нет — все страны "
            "раздела 6 пропущены. Для живого старта используйте "
            "Mechanic._load_anketa() (читает тему форума через userbot)."
        )
    texts = texts or {}

    for canon in targets:
        text = texts.get(canon)
        if not text:
            skipped.append(canon)
            continue
        data = parse_anketa(text, canon)
        issues = check_ranges(data)
        if issues:
            problems[canon] = issues
            log.warning("seed_anketa: %s — %s", canon, "; ".join(issues))

        existing = await db.get_country(canon)
        # manual-страну анкета не перебивает (раздел 4). Проверяем и сам факт
        # принадлежности к разделу 5: в БД stats_source может быть ещё не
        # проставлен, а страна уже входит в MANUAL_STATS_COUNTRIES.
        if existing and (existing.get("stats_source") == "manual"
                         or canon in config.MANUAL_STATS_COUNTRIES):
            skipped.append(canon)
            continue

        await db.upsert_country(_country_fields(data))
        await db.set_card_fields(
            canon,
            stats_source="anketa",
            capital=data.get("capital"),
            channel_url=data.get("channel_url"),
        )
        await db.log_change(
            canon, "stats_source",
            (existing or {}).get("stats_source") or "", "anketa",
            "seed_anketa: данные из анкеты (раздел 6)")
        (updated if existing else created).append(canon)

    return {"created": created, "updated": updated, "skipped": skipped,
            "problems": problems}
