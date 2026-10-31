"""
Справка о стране из ВСЕХ таблиц (п.6 ТЗ) с кэшируемым пересказом истории.

Досье собирается только из БД, без LLM. Пересказ истории - один вызов LLM,
результат кладётся в countries.summary и переиспользуется, пока не изменится
хвост новостей (ключ кэша = подпись последних новостей + число записей).

ВНИМАНИЕ: модуль НЕ подключён ни к одной команде бота. Ассистент отвечает на
вопрос о стране своим собственным `_send_country_info()`, поэтому досье из
этого модуля в проде не показывается. Подключать его нужно явно (например,
в `/info <страна>`), иначе он молча мёртвый; учтите, что `get_summary()`
делает вызов LLM и пишет в countries.summary.
"""
from __future__ import annotations

import json
import logging
from typing import Any

log = logging.getLogger(__name__)

# Сколько последних новостей формируют «отпечаток» для инвалидации кэша
CACHE_TAIL_NEWS = 5
# Сколько событий экономики показывать в досье
ECONOMY_TAIL = 10
NEWS_TAIL = 8


def _loads(value: Any, default: Any) -> Any:
    """Разбор JSON-поля, которое может быть уже списком/словарём."""
    if value in (None, "", b""):
        return default
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _num(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _int(value: Any) -> int:
    """Мягкое приведение к int.

    В БД числовые колонки иногда содержат текст (испорченный anketa-парсер,
    правки руками). Справка не должна падать из-за одной плохой ячейки -
    показываем 0 и продолжаем.
    """
    try:
        return int(float(str(value).strip().replace(" ", "")))
    except (TypeError, ValueError):
        return 0


async def build_dossier(db, name: str) -> dict[str, Any] | None:
    """Собрать полное досье страны из всех таблиц.

    Возвращает None, если страны нет. Поля, которых нет в БД, не вызывают
    исключений - просто остаются пустыми.
    """
    row = await db._fetchone("SELECT * FROM countries WHERE name=?", (name,))
    if row is None:
        return None
    country = dict(row)
    cid = int(country["id"])

    # --- экономика: последние события -------------------------------------
    econ = [dict(r) for r in await db._fetchall(
        "SELECT event_date, kind, amount, rate, note FROM economy_events "
        "WHERE country_id=? ORDER BY event_date DESC, id DESC LIMIT ?",
        (cid, ECONOMY_TAIL))]

    # --- хроника: последние новости (текст лежит в pending_news) -----------
    news = [dict(r) for r in await db._fetchall(
        "SELECT p.added_at, p.text, p.message_id FROM pending_news p "
        "WHERE p.country_name=? ORDER BY p.added_at DESC, p.id DESC LIMIT ?",
        (name, NEWS_TAIL))]

    researched = int((await db._fetchone(
        "SELECT COUNT(*) c FROM researched_news WHERE country_id=?",
        (cid,)))["c"] or 0)

    # --- личности ----------------------------------------------------------
    persons = [dict(r) for r in await db._fetchall(
        "SELECT name, activity, job, money FROM persons WHERE country=? "
        "ORDER BY id DESC LIMIT 10", (name,))]

    # --- группировки -------------------------------------------------------
    factions = [dict(r) for r in await db._fetchall(
        "SELECT f.full_name, f.prefix, f.commander, f.strength, cf.status "
        "FROM country_factions cf JOIN factions f ON f.id=cf.faction_id "
        "WHERE cf.country_id=? ORDER BY f.full_name", (cid,))]

    # --- изменения показателей --------------------------------------------
    changes = [dict(r) for r in await db._fetchall(
        "SELECT field, old_value, new_value, reason, timestamp FROM changes_log "
        "WHERE country=? ORDER BY timestamp DESC, id DESC LIMIT 12", (name,))]

    # --- известные сущности ------------------------------------------------
    entities = [dict(r) for r in await db._fetchall(
        "SELECT name, type, status, note FROM known_entities "
        "WHERE note LIKE ? OR name IN (SELECT ruler FROM countries WHERE name=?)",
        (f"%{name}%", name))]

    settlements = _loads(country.get("settlements_json"), [])
    industry = _loads(country.get("industry_json"), [])
    research = _loads(country.get("research_json"), [])
    resources = _loads(country.get("resources_json"), [])

    return {
        "name": name,
        "status": country.get("status") or "unknown",
        "aliases": _loads(country.get("aliases_json"), []),
        "ruler": country.get("ruler") or "",
        "party": country.get("party") or "",
        "ideology": country.get("ideology") or "",
        "color": country.get("color") or "",
        "population": _int(country.get("population")),
        "population_capacity": _int(country.get("population_capacity")),
        "budget": _num(country.get("budget")),
        "income_daily": _num(country.get("income_daily")),
        "expenses_daily": _num(country.get("expenses_daily")),
        "army": {
            "пехота": _int(country.get("army_infantry")),
            "лёгкая": _int(country.get("army_light_vehicles")),
            "тяжёлая": _int(country.get("army_heavy_wheeled")),
            "артиллерия": _int(country.get("army_artillery")),
            "БПЛА": _int(country.get("army_drones")),
            "флот": _int(country.get("army_navy")),
        },
        "tax_level": _int(country.get("tax_level")),
        "stability": _int(country.get("stability")),
        "war_support": _int(country.get("war_support")),
        "happiness": _int(country.get("happiness")),
        "settlements": settlements,
        "industry": industry,
        "research": research,
        "resources": resources,
        "economy": econ,
        "news": news,
        "news_researched": researched,
        "persons": persons,
        "factions": factions,
        "changes": changes,
        "entities": entities,
        "last_post_at": country.get("last_post_at") or "",
        "summary": country.get("summary") or "",
    }


def _totals(d: dict[str, Any]) -> int:
    return sum(int(v) for v in d["army"].values())


def format_dossier(d: dict[str, Any]) -> str:
    """Человекочитаемое досье без LLM."""
    L: list[str] = [f"🏴 {d['name']}"]
    if d["aliases"]:
        L.append(f"прежние названия: {', '.join([str(x) for x in d['aliases']])}")
    L.append(f"статус: {d['status']}")
    if d["ruler"]:
        L.append(f"правитель: {d['ruler']}")
    if d["party"]:
        L.append(f"партия: {d['party']}")
    if d["ideology"]:
        L.append(f"идеология: {d['ideology']}")
    L.append(f"население: {d['population']}"
             + (f" / {d['population_capacity']}" if d["population_capacity"] else ""))
    L.append(f"казна: {int(d['budget'])} (доход {int(d['income_daily'])} / "
             f"расход {int(d['expenses_daily'])})")
    army = _totals(d)
    L.append(f"армия всего: {army} — " +
             ", ".join(f"{k} {v}" for k, v in d["army"].items() if v))
    L.append(f"налоги {d['tax_level']}% • стабильность {d['stability']} "
             f"• поддержка {d['war_support']} • счастье {d['happiness']}")
    if d["settlements"]:
        L.append("города: " + ", ".join([str(x) for x in d["settlements"]][:8]))
    if d["industry"]:
        L.append("промышленность: " + ", ".join([str(x) for x in d["industry"]][:8]))
    if d["research"]:
        L.append("исследования: " + ", ".join([str(x) for x in d["research"]][:8]))
    if d["factions"]:
        L.append("группировки: " + ", ".join(
            f"{f['full_name']} ({f['status'] or '—'})" for f in d["factions"]))
    if d["persons"]:
        L.append("личности: " + ", ".join(
            f"{p['name']} ({p['job'] or '—'})" for p in d["persons"][:6]))
    if d["economy"]:
        L.append("события экономики: " + "; ".join(
            f"{e['event_date']} {e['kind']} {e['amount']}"
            for e in d["economy"][:ECONOMY_TAIL]))
    if d["news"]:
        L.append(f"новостей обработано: {d['news_researched']}")
    if d["changes"]:
        L.append("последние изменения: " + "; ".join(
            f"{c['field']}: {c['old_value']}→{c['new_value']}"
            for c in d["changes"][:6]))
    return "\n".join(L)


def _fingerprint(d: dict[str, Any]) -> str:
    """Отпечаток содержимого для инвалидации кэша пересказа."""
    parts = [str(len(d["news"])), str(d["news_researched"]),
             str(len(d["economy"])), str(d["population"]),
             str(int(d["budget"]))]
    for n in d["news"][:CACHE_TAIL_NEWS]:
        parts.append(str(n.get("added_at") or ""))
        parts.append(str(n.get("text") or "")[:120])
    return "|".join(parts)


async def get_summary(db, ai, d: dict[str, Any],
                      force: bool = False) -> str:
    """Пересказ истории: 1 вызов LLM, результат кэшируется в countries.summary.

    Если ИИ недоступен или вернул мусор - отдаём текстовое досье,
    справка не должна зависеть от внешнего сервиса.
    """
    name = d["name"]
    cached = d.get("summary") or ""
    fingerprint = _fingerprint(d)
    if not force and cached and cached.startswith(fingerprint):
        return cached[len(fingerprint):].strip()

    prompt = (
        f"Страна: {name}. Правитель: {d['ruler'] or 'неизвестен'}. "
        f"Население: {d['population']}. Казна: {int(d['budget'])}.\n"
        f"Города: {', '.join([str(x) for x in d['settlements']][:8]) or '—'}\n"
        f"Группировки: {', '.join(f['full_name'] for f in d['factions']) or '—'}\n"
        f"События: " + "; ".join(
            f"{e['event_date']} {e['kind']} {e['amount']}" for e in d["economy"][:6]
        ) + "\n" + (
            "Новости:\n" + "\n".join(
                f"- {n['text']}" for n in d["news"][CACHE_TAIL_NEWS:]
            ) if d["news"] else "Новостей нет."
        ) + "\n\nНапиши 3-5 предложений о стране: кто она, чем занимается, "
          "что происходит сейчас. Без выдумки, только по фактам выше."
    )

    text = ""
    try:
        resp = await ai.chat(
            "Ты историк-аналитик игры. Отвечай кратко и только по фактам, "
            "которые даны. Не выдумывай ничего.",
            prompt,
            user_id=0,
        )
        text = str(getattr(resp, "text", "") or getattr(resp, "content", "") or "").strip()
    except Exception as exc:  # noqa: BLE001 - справка не должна падать
        log.warning("country_info: LLM недоступен для %s: %s", name, exc)
        return cached or format_dossier(d)

    if not text:
        return cached or format_dossier(d)

    await db._execute("UPDATE countries SET summary=? WHERE name=?",
                      (fingerprint + "\n" + text, name))
    return text
