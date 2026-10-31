"""
Card templates and publishing logic for WARLORD RP.
Cards are formed from DB data, not LLM generation.
"""
from __future__ import annotations
import logging
from datetime import datetime, timezone
from typing import Any

from aiogram import Bot
from aiogram.types import BufferedInputFile

from config import config

log = logging.getLogger(__name__)

# ---------- Helpers ----------
def _num(v: Any) -> str:
    try:
        return f"{int(v):,}".replace(",", " ")
    except (TypeError, ValueError):
        return str(v)

def _delta(old: Any, new: Any) -> str:
    try:
        o = float(old or 0)
        n = float(new or 0)
        d = n - o
        if d > 0:
            return f" (+{_num(d)})"
        if d < 0:
            return f" ({_num(d)})"
        return ""
    except (TypeError, ValueError):
        return ""

def _pct(v: Any) -> str:
    try:
        return f"{int(v)}%"
    except (TypeError, ValueError):
        return str(v)

def _list_names(items: list[dict[str, Any]], limit: int = 8) -> str:
    if not items:
        return "—"
    names = []
    for it in items[:limit]:
        name = it.get("name") or it.get("type") or "?"
        val = it.get("value") or it.get("count") or it.get("level") or ""
        if val:
            names.append(f"{name} ({val})")
        else:
            names.append(name)
    if len(items) > limit:
        names.append("...")
    return ", ".join(names)

def _format_resources(resources: list[dict[str, Any]]) -> str:
    if not resources:
        return "—"
    lines = []
    for r in resources:
        name = r.get("name", "?")
        val = r.get("value") or r.get("count") or r.get("amount") or 0
        lines.append(f"• {name}: {_num(val)}")
    return "\n".join(lines)

def _format_industry(industry: list[dict[str, Any]]) -> str:
    if not industry:
        return "—"
    lines = []
    for ind in industry:
        name = ind.get("name") or ind.get("type") or "?"
        val = ind.get("value") or ind.get("count") or 1
        lines.append(f"• {name}: {val}")
    return "\n".join(lines)

def _format_settlements(settlements: list[dict[str, Any]]) -> str:
    if not settlements:
        return "—"
    lines = []
    for s in settlements:
        name = s.get("name", "?")
        typ = s.get("type") or s.get("kind") or "?"
        cap = s.get("value") or s.get("capacity") or s.get("population") or 0
        lines.append(f"• {name} ({typ}, вместимость {_num(cap)})")
    return "\n".join(lines)

def _format_research(research: list[dict[str, Any]]) -> str:
    if not research:
        return "Нет"
    lines = []
    for r in research:
        name = r.get("name", "?")
        time_left = r.get("time_left_hours") or r.get("hours_left") or r.get("value") or 0
        if time_left:
            lines.append(f"• {name} ({time_left} ч)")
        else:
            lines.append(f"• {name}")
    return "\n".join(lines)

def _format_equipment(eq: list[dict[str, Any]]) -> str:
    if not eq:
        return "—"
    return ", ".join(f"{e.get('name', '?')}: {e.get('count', 0)}" for e in eq)

def _format_contracts(contracts: list[dict[str, Any]]) -> str:
    if not contracts:
        return "Нет"
    lines = []
    for c in contracts:
        with_whom = c.get("with") or c.get("party") or "?"
        topic = c.get("topic") or c.get("task") or "?"
        payment = c.get("payment") or c.get("pay") or 0
        status = c.get("status") or "active"
        lines.append(f"• {with_whom}: {topic} ({_num(payment)} вирт-руб., {status})")
    return "\n".join(lines)

def _format_wars(wars: list[dict[str, Any]]) -> str:
    if not wars:
        return "Нет"
    lines = []
    for w in wars:
        enemy = w.get("enemy") or w.get("against") or "?"
        status = w.get("status") or "active"
        lines.append(f"• Против {enemy} ({status})")
    return "\n".join(lines)

# ---------- Country Card ----------
def build_country_card(country: dict[str, Any], summary: str = "", notifications: str = "") -> str:
    """Build country card text from DB data."""
    name = country.get("name", "—")
    ruler = country.get("ruler") or "—"
    party = country.get("party") or "—"
    ideology = country.get("ideology") or "—"
    color = country.get("color") or "—"

    pop = _num(country.get("population"))
    pop_cap = _num(country.get("population_capacity"))
    pop_d = _delta(country.get("prev_population"), country.get("population"))

    budget = _num(country.get("budget"))
    budget_d = _delta(country.get("prev_budget"), country.get("budget"))
    income = _num(country.get("income_daily"))
    expenses = _num(country.get("expenses_daily"))

    infantry = _num(country.get("army_infantry"))
    inf_d = _delta(country.get("prev_army_infantry"), country.get("army_infantry"))
    light = _num(country.get("army_light_vehicles"))
    heavy = _num(country.get("army_heavy_wheeled"))
    artillery = _num(country.get("army_artillery"))
    drones = _num(country.get("army_drones"))
    navy = _num(country.get("army_navy"))

    tax = country.get("tax_level") or "—"
    stability = _pct(country.get("stability"))
    war_sup = _pct(country.get("war_support"))
    happiness = _pct(country.get("happiness"))

    resources = _format_resources(country.get("resources_json", []))
    settlements = _format_settlements(country.get("settlements_json", []))
    industry = _format_industry(country.get("industry_json", []))
    research = _format_research(country.get("research_json", []))
    oyat = _num(country.get("resources_json", {}).get("ОЯТ") if isinstance(country.get("resources_json"), dict) else 0)

    factions = country.get("factions", [])
    faction_lines = []
    for f in factions:
        fn = f.get("full_name") or f.get("name") or "?"
        cmd = f.get("commander") or "—"
        stren = _num(f.get("strength"))
        rel = f.get("relation_to_country") or "neutral"
        faction_lines.append(f"• {fn} — {cmd}, {stren} чел., {rel}")

    notifications_block = notifications or "Нарушений не выявлено"

    lines = []
    lines.append(f"[🖼 Фото флага]")
    lines.append(f"ГОСУДАРСТВО: {name}")
    lines.append(f"👑 Правитель: {ruler}")
    lines.append(f"🏛 Партия: {party} | 👁 Идеология: {ideology}")
    lines.append(f"🎨 Цвет на карте: {color}")
    lines.append("")
    lines.append(f"📊 ГОСУДАРСТВЕННЫЕ ПОКАЗАТЕЛИ:")
    lines.append(f"👥 Население: {pop}{pop_d} (Вместимость: {pop_cap})")
    lines.append(f"💰 Казна: {budget}{budget_d} вирт-руб. *(Доход: +{income} / Расход: -{expenses})*")
    lines.append(f"🪖 Армия: {infantry}{inf_d} солдат")
    lines.append(f"📊 Налоги: {tax}")
    lines.append("")
    lines.append(f"🏭 ИНФРАСТРУКТУРА И ЭКОНОМИКА:")
    lines.append(f"• Населённые пункты:\n{settlements}")
    lines.append(f"• Промышленность:\n{industry}")
    lines.append(f"• Исследования: {research}")
    lines.append(f"• 🧪 ОЯТ: {oyat} ед.")
    lines.append("")
    lines.append(f"🪖 ВОЕННЫЕ СИЛЫ:")
    lines.append(f"• Пехота: {infantry}{inf_d}")
    lines.append(f"• Лёгкая техника: {light}")
    lines.append(f"• Тяжёлая колёсная: {heavy}")
    lines.append(f"• Артиллерия: {artillery}")
    lines.append(f"• БПЛА: {drones}")
    lines.append(f"• Флот: {navy}")
    lines.append("")
    if faction_lines:
        lines.append(f"🏴 ГРУППИРОВКИ НА ТЕРРИТОРИИ:")
        lines.extend(faction_lines)
        lines.append("")
    lines.append(f"⚖️ ОБЩЕСТВЕННОЕ СОСТОЯНИЕ:")
    lines.append(f"📉 Стабильность: {stability}")
    lines.append(f"⚔️ Поддержка войны: {war_sup}")
    lines.append(f"😊 Счастье народа: {happiness}")
    lines.append("")
    if summary:
        lines.append(f"📝 ИТОГИ ЗА 2 ЧАСА:")
        lines.append(f"• {summary}")
        lines.append("")
    lines.append(f"⚠️ УВЕДОМЛЕНИЯ:")
    lines.append(notifications_block)

    return "\n".join(lines)

# ---------- Faction Card ----------
def build_faction_card(faction: dict[str, Any], summary: str = "") -> str:
    """Build faction card text from DB data."""
    prefix = faction.get("prefix", "")
    full = faction.get("full_name", "—")
    commander = faction.get("commander") or "—"
    depends = faction.get("depends_on_country") or "независимая"
    base = faction.get("base_country") or "—"
    relation = faction.get("relation_to_country") or "neutral"
    strength = _num(faction.get("strength"))
    equipment = _format_equipment(faction.get("equipment_json", []))
    budget = _num(faction.get("budget"))
    tasks = faction.get("tasks") or "—"
    contracts = _format_contracts(faction.get("contracts_json", []))
    wars = _format_wars(faction.get("wars_json", []))
    rep = _pct(faction.get("reputation"))

    lines = []
    lines.append(f"[🖼 Эмблема]")
    lines.append(f"ГРУППИРОВКА: {prefix} {full}".strip())
    lines.append(f"🎖 Командир: {commander}")
    lines.append(f"🏛 Зависит от: {depends}")
    lines.append(f"📍 Базирование: {base}")
    lines.append(f"📊 Отношение к стране: {relation}")
    lines.append("")
    lines.append(f"👥 Численность: {strength}")
    lines.append(f"🚙 Техника: {equipment}")
    lines.append(f"💰 Бюджет: {budget} вирт-руб.")
    lines.append(f"🎯 Задачи: {tasks}")
    lines.append("")
    lines.append(f"📜 КОНТРАКТЫ:")
    lines.append(contracts)
    lines.append("")
    lines.append(f"⚔️ УЧАСТИЕ В ВОЙНАХ:")
    lines.append(wars)
    lines.append("")
    lines.append(f"🏆 Репутация: {rep}/100")
    lines.append("")
    if summary:
        lines.append(f"📝 ИСТОРИЯ:")
        lines.append(f"• {summary}")
        lines.append("")
    return "\n".join(lines)

# ---------- Publishing ----------
async def publish_or_edit(
    bot: Bot,
    chat_id: int,
    thread_id: int,
    text: str,
    message_id: int | None,
    photo: BufferedInputFile | None = None,
    caption: str | None = None
) -> int:
    """Publish new message or edit existing one. Returns message_id."""
    if message_id:
        try:
            if photo:
                await bot.edit_message_caption(
                    chat_id=chat_id,
                    message_id=message_id,
                    caption=text,
                    parse_mode=None
                )
            else:
                await bot.edit_message_text(
                    text=text,
                    chat_id=chat_id,
                    message_id=message_id,
                    parse_mode=None,
                    disable_web_page_preview=True
                )
            return message_id
        except Exception as e:
            log.warning("Edit failed for msg %s: %s. Re-sending.", message_id, e)
            # Fall through to send new
    try:
        if photo:
            msg = await bot.send_photo(
                chat_id=chat_id,
                photo=photo,
                caption=caption or text,
                message_thread_id=thread_id,
                parse_mode=None
            )
        else:
            msg = await bot.send_message(
                chat_id=chat_id,
                text=text,
                message_thread_id=thread_id,
                parse_mode=None,
                disable_web_page_preview=True
            )
        return msg.message_id
    except Exception as e:
        log.error("Failed to send message: %s", e)
        raise

async def publish_country_card(
    bot: Bot,
    country: dict[str, Any],
    thread_id: int,
    message_id: int | None,
    summary: str = "",
    notifications: str = ""
) -> int:
    text = build_country_card(country, summary, notifications)
    photo = None
    caption = None
    if country.get("flag_file_id"):
        try:
            # flag_file_id is Telegram file_id string
            photo = country["flag_file_id"]
            caption = text
            text = None
        except Exception:
            pass
    return await publish_or_edit(
        bot, config.target_chat_id, thread_id,
        text or "", message_id, photo=photo, caption=caption
    )

async def publish_faction_card(
    bot: Bot,
    faction: dict[str, Any],
    thread_id: int,
    message_id: int | None,
    summary: str = ""
) -> int:
    text = build_faction_card(faction, summary)
    photo = None
    caption = None
    if faction.get("emblem_file_id"):
        photo = faction["emblem_file_id"]
        caption = text
        text = None
    return await publish_or_edit(
        bot, config.target_chat_id, thread_id,
        text or "", message_id, photo=photo, caption=caption
    )

# ---------- Application of events ----------
BUILDING_COSTS = {
    "factory": 15000, "military_factory": 25000, "supply_factory": 25000,
    "shipyard": 45000, "research_center": 40000, "radar": 15000,
    "military_base": 12000, "checkpoint": 3000, "tower": 5000,
    "polygon": 8000, "bunker": 15000, "port": 35000, "hospital": 5000,
    "school": 5000, "village": 5000, "settlement": 10000,
    "town": 15000, "urban": 20000, "city": 24500, "road": 1000,
}

BUILDING_TIMES = {
    "factory": 6, "military_factory": 8, "supply_factory": 8,
    "shipyard": 16, "research_center": 48, "radar": 15,
    "military_base": 24, "checkpoint": 2, "tower": 4,
    "polygon": 10, "bunker": 48, "port": 12, "hospital": 6,
    "school": 6, "village": 8, "settlement": 12, "town": 16,
    "urban": 24, "city": 30, "road": 6,
}

BUILDING_INCOME = {"factory": 500, "port": 300}

def apply_event(country: dict[str, Any], event: dict[str, Any], db: Any = None) -> list[str]:
    """Apply a single event to country dict. Returns list of change descriptions for logging."""
    changes = []
    etype = event.get("type")
    params = event.get("params", {})

    if etype == "build":
        building = params.get("building")
        count = int(params.get("count", 1))
        cost = BUILDING_COSTS.get(building, 0) * count
        if country.get("budget", 0) < cost:
            changes.append(f"Недостаточно средств для {building} x{count}")
            return changes
        country["budget"] = country.get("budget", 0) - cost
        industry = country.get("industry_json", [])
        found = False
        for item in industry:
            if item.get("name") == building or item.get("type") == building:
                item["value"] = item.get("value", 0) + count
                found = True
                break
        if not found:
            industry.append({"name": building, "type": building, "value": count})
        country["industry_json"] = industry
        if building in BUILDING_INCOME:
            country["income_daily"] = country.get("income_daily", 1000) + BUILDING_INCOME[building] * count
        changes.append(f"Построено: {building} x{count} (-{_num(cost)} вирт-руб.)")

    elif etype == "population":
        delta = int(params.get("delta", 0))
        old = country.get("population", 0)
        new = max(0, old + delta)
        country["population"] = new
        changes.append(f"Население: {_num(old)} → {_num(new)} ({_delta(old, new)})")

    elif etype == "budget":
        delta = int(params.get("delta", 0))
        reason = params.get("reason", "")
        old = country.get("budget", 0)
        new = old + delta
        country["budget"] = new
        changes.append(f"Бюджет: {_num(old)} → {_num(new)} ({_delta(old, new)}) [{reason}]")

    elif etype == "army":
        old_inf = country.get("army_infantry", 0)
        inf_delta = int(params.get("infantry", 0))
        country["army_infantry"] = max(0, old_inf + inf_delta)
        changes.append(f"Пехота: {_num(old_inf)} → {_num(country['army_infantry'])} ({_delta(old_inf, country['army_infantry'])})")
        for k in ("light_vehicles", "heavy_wheeled", "artillery", "drones", "navy"):
            if k in params:
                old = country.get(f"army_{k}", 0)
                delta = int(params[k])
                country[f"army_{k}"] = max(0, old + delta)
                changes.append(f"{k}: {_num(old)} → {_num(country[f'army_{k}'])} ({_delta(old, country[f'army_{k}'])})")

    elif etype == "stability":
        delta = int(params.get("delta", 0))
        reason = params.get("reason", "")
        old = country.get("stability", 70)
        new = max(0, min(100, old + delta))
        country["stability"] = new
        changes.append(f"Стабильность: {old}% → {new}% ({_delta(old, new)}) [{reason}]")

    elif etype == "tax":
        level = params.get("level")
        if level in ("low", "standard", "high", "maximum"):
            country["tax_level"] = level
            changes.append(f"Налоговый уровень: {level}")

    elif etype == "research":
        name = params.get("name")
        time_left = int(params.get("time_left_hours", 0))
        research = country.get("research_json", [])
        found = False
        for r in research:
            if r.get("name") == name:
                r["time_left_hours"] = time_left
                found = True
                break
        if not found:
            research.append({"name": name, "time_left_hours": time_left})
        country["research_json"] = research
        changes.append(f"Исследование: {name} ({time_left} ч)")

    elif etype == "resource":
        name = params.get("name")
        delta = int(params.get("delta", 0))
        resources = country.get("resources_json", [])
        found = False
        for r in resources:
            if r.get("name") == name:
                r["value"] = r.get("value", 0) + delta
                found = True
                break
        if not found:
            resources.append({"name": name, "value": delta})
        country["resources_json"] = resources
        changes.append(f"Ресурс {name}: {_delta(0, delta)}")

    elif etype == "unreg":
        country["status"] = "unreg"
        country["population"] = 0
        country["budget"] = 0
        country["army_infantry"] = 0
        changes.append("Страна анрегнута: показатели обнулены")

    elif etype == "rename":
        new_name = params.get("new_name")
        if new_name:
            country["name"] = new_name
            changes.append(f"Переименована в: {new_name}")

    return changes