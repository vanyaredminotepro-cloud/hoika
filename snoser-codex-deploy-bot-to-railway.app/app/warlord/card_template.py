"""
Шаблоны карточки страны (разделы 2 и 3 ТЗ, модуль CARDS).

Здесь только представление: сбор строк из уже готовых данных. LLM в этом файле
нет вообще — «Итоги за 2 часа» приходят готовой строкой сверху.

Разметка — HTML (Telegram parse_mode="HTML"), потому что собственных шрифтов
у Telegram нет: «красиво и читаемо» здесь значит аккуратные заголовки, а не
кастомные шрифты. Все динамические строки (имя, правитель, итоги, списки)
прогоняются через esc(), иначе символы <, > и & ломают разбор разметки и
карточка либо не отправится, либо покажет «сырой» HTML.

Ключевые правила раздела 3, реализованные как функции:
  * показатель изменился   -> "(+7)", "(-150)", "(+2%)"
  * показатель не изменился -> скобок НЕТ вообще
  * показатель неизвестен   -> прочерк "-"
  * показатель заморожен    -> "(заморожено)"

Динамика считается по парам prev_* / текущее значение, которые уже ведёт
экономика, поэтому шаблон ничего не знает про историю и не ходит в БД.
"""
from __future__ import annotations

import html
import json
from datetime import datetime
from typing import Any, Iterable

from . import config

# Разделители секций карточки. Порядок жёсткий — раздел 2.
SEP = "━━━━━━━━━━━━━━━"

# Подписи статусов активности для шапки (раздел 2, раздел 8).
# Тексты должны дословно содержать причину заморозки: игроки ориентируются
# на них, а не на эмодзи, и «почему карточка не меняется» - частый вопрос.
STATUS_BANNERS: dict[str, str] = {
    "inactive": "СТРАНА ЗАМОРОЖЕНА: мало новостей, показатели не растут",
    "afk": "СТРАНА ЗАМОРОЖЕНА: нет новостей больше 7 дней",
    "neutral": "СТРАНА ЗАМОРОЖЕНА: нет новостей 2 часа",
    "data_error": "ДАННЫЕ СТРАНЫ ПОВРЕЖДЕНЫ: нужно заново заполнить анкету",
    "not_accepted": "АНКЕТА СТРАНЫ НЕ ПРИНЯТА",
    "not_registered": "АНКЕТА СТРАНЫ НЕ ПРИНЯТА",
    "anreg": "СТРАНА АНРЕГНУТА",
    "unreg": "СТРАНА АНРЕГНУТА",
}


def esc(value: Any) -> str:
    """Экранировать динамический текст для HTML-разметки Telegram.

    Без этого имя страны со знаком "&" или "<" ломает parse_mode="HTML":
    Telegram отвечает ошибкой и карточка не публикуется.
    """
    return html.escape(str(value if value is not None else ""), quote=True)


# ══════════════════════════════════════ ДИНАМИКА (раздел 3) ══════════════

def fmt_int(value: Any) -> str:
    """Целое с разделителем тысяч. None/unknown -> прочерк."""
    n = _as_float(value)
    if n is None:
        return "-"
    return f"{int(round(n)):,}".replace(",", " ")


def fmt_num(value: Any, digits: int = 1) -> str:
    """Дробное. None/unknown -> прочерк."""
    n = _as_float(value)
    if n is None:
        return "-"
    if abs(n - round(n)) < 1e-9:
        return fmt_int(n)
    return f"{n:,.{digits}f}".replace(",", " ").replace(".", ",")


def fmt_money(value: Any) -> str:
    """Казна. Целые — без дробной части, дробные — с запятой."""
    n = _as_float(value)
    if n is None:
        return "-"
    if abs(n - round(n)) < 1e-9:
        return f"{int(round(n)):,}".replace(",", " ")
    return f"{n:,.2f}".replace(",", " ").replace(".", ",")


def fmt_pct(value: Any) -> str:
    """Проценты. 70.0 -> «70%», 88.46 -> «88.5%»."""
    n = _as_float(value)
    if n is None:
        return "-"
    if abs(n - round(n)) < 1e-9:
        return f"{int(round(n))}%"
    return f"{n:.1f}".replace(".", ",") + "%"


def _as_float(value: Any) -> float | None:
    """Мягкое приведение к float. Пустая строка/None/мусор -> None."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(" ", "").replace("%", "")
    if not text:
        return None
    text = text.replace(",", ".")
    # хвост вида "88.5%" уже срезан, но "1 000" мог остаться с мусором
    try:
        return float(text)
    except ValueError:
        return None


def delta(
    new: Any,
    old: Any,
    *,
    frozen: bool = False,
    pct: bool = False,
) -> str:
    """Скобки динамики по разделу 3. Пустая строка = скобок нет.

    frozen=True важнее всего остального: если показатель заморожен, динамика
    не показывается вообще, даже если значение случайно изменилось.
    """
    if frozen:
        return "(заморожено)"
    n_new, n_old = _as_float(new), _as_float(old)
    # Оба неизвестны — сравнивать нечего, скобок не пишем.
    if n_new is None and n_old is None:
        return ""
    # Один из них неизвестен: дельту посчитать нельзя, но показатель виден.
    if n_new is None or n_old is None:
        return ""
    diff = n_new - n_old
    if abs(diff) < 1e-9:
        return ""  # не изменилось — скобок нет

    if pct:
        if abs(n_old) < 1e-9:
            return f"({'+' if diff > 0 else ''}{int(round(diff))}%)"
        change = diff / abs(n_old) * 100.0
        sign = "+" if change > 0 else ""
        rounded = round(change, 1)
        body = f"{rounded:.1f}".rstrip("0").rstrip(".").replace(".", ",")
        return f"({sign}{body}%)"

    sign = "+" if diff > 0 else "-"
    amount = abs(diff)
    if abs(amount - round(amount)) < 1e-9:
        return f"({sign}{int(round(amount)):,})".replace(",", " ")
    return f"({sign}{amount:.1f})".replace(".", ",")


def metric(
    value: Any,
    old: Any | None = None,
    *,
    frozen: bool = False,
    fmt: str = "int",
    pct: bool = False,
    suffix: str = "",
    delta_override: str | None = None,
) -> str:
    """Собрать «значение + скобки динамики» одной строкой.

    delta_override позволяет подставить скобки, посчитанные не по prev_*
    (например, для ОЯТ, у которого нет пары prev-полей).
    """
    render = {"int": fmt_int, "num": fmt_num, "money": fmt_money, "pct": fmt_pct}[fmt]
    body = render(value)
    if suffix:
        body = f"{body} {suffix}"
    if delta_override is not None:
        return f"{body} {delta_override}".strip()
    if old is None:
        return body
    d = delta(value, old, frozen=frozen, pct=pct)
    return f"{body} {d}".strip() if d else body


def as_item(value: Any) -> dict[str, Any]:
    """Привести элемент списка к dict.

    ИИ и игроки пишут списки по-разному: «Фабрика», «Фабрика: 2»,
    {"name": "Фабрика", "value": 2}, '{"name": "Низкий"}'. Если такое не
    разобрать, игроки видят в списке городов сырой JSON, а количество
    теряется.
    """
    if isinstance(value, dict):
        return value
    text = str(value or "").strip()
    if not text:
        return {}
    if text.startswith("{"):
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                return parsed
        except (ValueError, TypeError):
            pass
    name, _, tail = text.partition(":")
    name = name.strip()
    digits = "".join(ch for ch in tail if ch.isdigit())
    return {"name": name, "count": int(digits) if digits else 0} if name else {}


def list_names(items: Iterable[Any], limit: int | None = None) -> str:
    """Список названий через запятую. Пусто -> «Нет»."""
    names: list[str] = []
    if isinstance(items, (str, bytes)) or isinstance(items, dict):
        items = [items]
    for item in items or []:
        data = as_item(item)
        name = str(data.get("name") or data.get("title") or "").strip()
        if not name:
            continue
        count = data.get("count", data.get("value"))
        try:
            count_n = int(count) if count not in (None, "") else None
        except (TypeError, ValueError):
            count_n = None
        name = esc(name)
        names.append(f"{name} ({fmt_int(count_n)})" if count_n else name)
    if not names:
        return "Нет"
    cap = limit or config.CARD_LIST_LIMIT
    if len(names) <= cap:
        return ", ".join(names)
    return ", ".join(names[:cap]) + f" и ещё {len(names) - cap}"


# ═══════════════════════════════ БЛОК ИЗМЕНЕНИЙ (раздел 10) ═══════════════

def _fmt_value(value: Any) -> str:
    if value is None or value == "":
        return "-"
    # Числа показываем аккуратно: 108364.4 -> «108 364,4», 35.0 -> «35».
    # Раньше сырые float'ы попадали в карточку как «108364.4 → 96364.0».
    if not isinstance(value, bool):
        n = _as_float(value)
        if n is not None:
            return fmt_num(n) if abs(n - round(n)) >= 1e-9 else fmt_int(n)
    return esc(value)


def _fmt_updated(value: Any) -> str:
    """Дата обновления карточки по-человечески: 2026-10-11T05:24:34+00:00 ->
    «11.10.2026 08:24» (локальное время сервера). Мусор отдаём как есть."""
    if not value:
        return ""
    try:
        dt = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return str(value)
    if dt.tzinfo is not None:
        dt = dt.astimezone()
    return dt.strftime("%d.%m.%Y %H:%M")


def build_change_block(
    changes: list[dict[str, Any]],
    *,
    updated_at: str | None = None,
) -> str:
    """Уровень 1 оповещения: «было -> стало» сверху карточки.

    changes: [{field, old_value, new_value, op}] где op in
             {"changed", "added", "removed"}.
    Возвращает "" если изменений нет — тогда блок не печатается вовсе.
    """
    if not changes:
        return ""
    added = [c for c in changes if c.get("op") == "added"]
    removed = [c for c in changes if c.get("op") == "removed"]
    changed = [c for c in changes
               if c.get("op") not in ("added", "removed")]

    lines: list[str] = []
    if updated_at:
        lines.append(f"<b>ОБНОВЛЕНО: {esc(_fmt_updated(updated_at))}</b>")
    if changed:
        # Заголовок блока - дословно как в разделе 10: без него непонятно,
        # к какому отрезку времени относится список.
        lines.append("<b>ИЗМЕНЕНИЯ ЗА ПРОШЕДШИЕ 2 ЧАСА</b>")
        for c in changed:
            lines.append(
                f"• {esc(c.get('field'))}: "
                f"{_fmt_value(c.get('old_value'))} → "
                f"{_fmt_value(c.get('new_value'))}")
    if added:
        lines.append("<b>ДОБАВЛЕНО</b>")
        for c in added:
            lines.append(f"• {esc(c.get('field'))}: {_fmt_value(c.get('new_value'))}")
    if removed:
        lines.append("<b>УБРАНО</b>")
        for c in removed:
            lines.append(f"• {esc(c.get('field'))}: {_fmt_value(c.get('old_value'))}")
    return "\n".join(lines)


# ═════════════════════════════ СЕКЦИИ КАРТОЧКИ (раздел 2) ═════════════════

def _section(title: str, body: str) -> str:
    """Секция с жирным заголовком. Пустое тело -> пустая строка."""
    body = (body or "").strip()
    if not body:
        return ""
    return f"<b>{title}</b>\n{body}"


def status_banner(country: dict[str, Any], days_ago: float | None = None) -> str:
    """Плашка статуса. Пустая строка для active — у активной страны плашки нет."""
    status = str(country.get("status") or "active")
    banner = STATUS_BANNERS.get(status)
    if not banner:
        return ""
    tail = ""
    if status in ("inactive", "afk"):
        # «(заморожено)» в плашке, а не только в показателях: игрок должен
        # видеть причину первым, до всех чисел.
        tail = " (заморожено)"
        if days_ago is not None:
            n = max(0, int(round(days_ago)))
            tail += f" ({n} дн. без постов)"
    return f"<blockquote><b>{banner}</b>{tail}</blockquote>"


def header_section(
    country: dict[str, Any],
    change_block: str = "",
    days_ago: float | None = None,
) -> str:
    """ШАПКА: блок изменений, флаг, название, статус, правитель."""
    out: list[str] = []
    if change_block:
        out.append(change_block)
        out.append("")

    name = str(country.get("name") or "-")
    out.append(f"<b>ГОСУДАРСТВО: {esc(name)}</b>")

    banner = status_banner(country, days_ago)
    if banner:
        out.append(banner)

    if not country.get("flag_file_id"):
        # Плашка обязательна: без неё нельзя понять, флаг не загружен или сломан.
        out.append("<i>Флаг не загружен</i>")

    for label, field in (
        ("Правитель", "ruler"),
        ("Партия", "party"),
        ("Идеология", "ideology"),
        ("Цвет", "color"),
    ):
        value = str(country.get(field) or "").strip()
        if value:
            out.append(f"<i>{label}:</i> {esc(value)}")
    return "\n".join(out)


def indicators_section(country: dict[str, Any], frozen: bool) -> str:
    """ГОСУДАРСТВЕННЫЕ ПОКАЗАТЕЛИ: население, казна, армия, налоги."""
    pop = metric(country.get("population"), country.get("prev_population"),
                 frozen=frozen, fmt="int")
    capacity = fmt_int(country.get("population_capacity"))
    cap_part = f" (Вместимость: {capacity})" if capacity != "-" else ""

    income = _as_float(country.get("income_daily")) or 0.0
    expenses = _as_float(country.get("expenses_daily")) or 0.0
    money = metric(country.get("budget"), country.get("prev_budget"),
                   frozen=frozen, fmt="money")
    money_line = f"Казна: {money} вирт-руб."
    if not frozen and (income or expenses):
        money_line += (f"  <i>(Доход: +{fmt_money(income)} / "
                       f"Расход: -{fmt_money(expenses)})</i>")

    army = metric(country.get("army_infantry"), country.get("prev_army_infantry"),
                  frozen=frozen, fmt="int")
    tax = esc(country.get("tax_level") or "-")

    body = "\n".join([
        f"Население: {pop}{cap_part}",
        money_line,
        f"Армия: {army} солдат",
        f"Налоги: {tax}",
    ])
    return _section("ГОСУДАРСТВЕННЫЕ ПОКАЗАТЕЛИ", body)


def infrastructure_section(country: dict[str, Any]) -> str:
    """ИНФРАСТРУКТУРА: пункты, промышленность, исследования, ОЯТ."""
    body = [
        f"Населённые пункты: {list_names(country.get('settlements_json') or [])}",
        f"Промышленность: {list_names(country.get('industry_json') or [])}",
    ]

    research = country.get("research_json") or []
    if research:
        parts: list[str] = []
        for item in research:
            data = as_item(item)
            name = str(data.get("name") or "").strip()
            if not name:
                continue
            name = esc(name)
            # Остаток важнее даты старта: игроки ждут «сколько ждать».
            hours = data.get("time_left_hours", data.get("hours"))
            when = str(data.get("at") or data.get("date") or "").strip()
            if hours not in (None, ""):
                parts.append(f"{name} ({fmt_int(hours)} ч)")
            elif when:
                parts.append(f"{name} ({esc(when)})")
            else:
                parts.append(name)
        research_body = ", ".join(parts) if parts else "Нет"
    else:
        research_body = "Нет"
    body.append(f"Исследования: {research_body}")

    # ОЯТ — только у страны из config.OJET_COUNTRY (раздел 2, раздел 13).
    if str(country.get("name") or "") == config.OJET_COUNTRY:
        body.append(f"{config.OJET_DISPLAY_NAME}: "
                    f"{fmt_int(country.get('ojat'))} ед.")
    return _section("ИНФРАСТРУКТУРА", "\n".join(body))


def military_section(country: dict[str, Any], frozen: bool) -> str:
    """ВОЕННЫЕ СИЛЫ: шесть видов войск с динамикой."""
    rows = (
        ("Пехота", "army_infantry", "prev_army_infantry"),
        ("Лёгкая техника", "army_light_vehicles", "prev_army_light_vehicles"),
        ("Тяжёлая колёсная", "army_heavy_wheeled", "prev_army_heavy_wheeled"),
        ("Артиллерия", "army_artillery", "prev_army_artillery"),
        ("БПЛА", "army_drones", "prev_army_drones"),
        ("Флот", "army_navy", "prev_army_navy"),
    )
    body = "\n".join(
        f"{label}: {metric(country.get(field), country.get(prev), frozen=frozen)}"
        for label, field, prev in rows
    )
    return _section("ВОЕННЫЕ СИЛЫ", body)


def factions_section(factions: list[dict[str, Any]] | None) -> str:
    """ГРУППИРОВКИ НА ТЕРРИТОРИИ: название — командир, численность, статус."""
    if not factions:
        return ""
    out: list[str] = []
    for f in factions:
        name = str(f.get("full_name") or f.get("name") or "").strip()
        if not name:
            continue
        parts = [f"<b>{esc(name)}</b>"]
        commander = str(f.get("commander") or "").strip()
        if commander:
            parts.append(esc(commander))
        parts.append(f"численность: {fmt_int(f.get('strength'))}")
        status = str(f.get("status") or "").strip()
        if status:
            parts.append(f"<i>{esc(status)}</i>")
        relation = str(f.get("relation_to_country") or "").strip()
        if relation:
            parts.append(f"<i>{esc(relation)}</i>")
        out.append("• " + ", ".join(parts))
    if not out:
        return ""
    return _section("ГРУППИРОВКИ НА ТЕРРИТОРИИ", "\n".join(out))


def social_section(country: dict[str, Any], frozen: bool) -> str:
    """ОБЩЕСТВЕННОЕ СОСТОЯНИЕ: стабильность, поддержка войны, счастье."""
    body = "\n".join([
        f"Стабильность: "
        f"{metric(country.get('stability'), country.get('prev_stability'), frozen=frozen, fmt='pct')}",
        f"Поддержка войны: "
        f"{metric(country.get('war_support'), country.get('prev_war_support'), frozen=frozen, fmt='pct')}",
        f"Счастье народа: "
        f"{metric(country.get('happiness'), country.get('prev_happiness'), frozen=frozen, fmt='pct')}",
    ])
    return _section("ОБЩЕСТВЕННОЕ СОСТОЯНИЕ", body)


def summary_section(summary: str) -> str:
    """ИТОГИ ЗА 2 ЧАСА. Единственное место, где в карточке есть LLM-текст."""
    text = (summary or "").strip()
    if not text:
        return ""
    # Жёстко режем: LLM любит выдавать простыни, а блок должен быть коротким.
    cap = config.CARD_SUMMARY_MAX_CHARS
    if len(text) > cap:
        text = text[:cap].rstrip() + "…"
    return _section("ИТОГИ ЗА 2 ЧАСА", f"<blockquote>{esc(text)}</blockquote>")


def notifications_section(notifications: Any) -> str:
    """УВЕДОМЛЕНИЯ. Пусто -> «Нарушений не выявлено» (раздел 2)."""
    items: list[str] = []
    if isinstance(notifications, str):
        text = notifications.strip()
        if text:
            items.append(esc(text))
    elif isinstance(notifications, dict):
        for key, value in notifications.items():
            body = str(value).strip()
            if body:
                items.append(f"<i>{esc(key)}:</i> {esc(body)}")
    elif isinstance(notifications, (list, tuple)):
        for item in notifications:
            if isinstance(item, dict):
                body = str(item.get("text") or item.get("body") or "").strip()
                label = str(item.get("title") or item.get("type") or "").strip()
                if body:
                    items.append(f"<i>{esc(label)}:</i> {esc(body)}" if label
                                 else esc(body))
            elif str(item).strip():
                items.append(esc(str(item).strip()))
    if not items:
        return _section("УВЕДОМЛЕНИЯ", "Нарушений не выявлено")
    return _section("УВЕДОМЛЕНИЯ", "\n".join(f"• {i}" for i in items))


# ═══════════════════════════ СБОРКА КАРТОЧКИ ЦЕЛИКОМ ════════════════════

def build_card(
    country: dict[str, Any],
    *,
    changes: list[dict[str, Any]] | None = None,
    summary: str = "",
    notifications: Any = None,
    days_ago: float | None = None,
    updated_at: str | None = None,
    factions: list[dict[str, Any]] | None = None,
    prev_country: str | None = None,
    next_country: str | None = None,
) -> str:
    """Собрать карточку страны целиком в порядке раздела 2.

    Порядок разделов менять нельзя: игроки уже знают карточку по виду, и
    перестановка блоков ломает привычку читать. Единственное, что может
    исчезнуть целиком, - блок изменений (нет изменений -> нет блока) и
    «ИТОГИ ЗА 2 ЧАСА» (нет новостей -> нет итогов).
    """
    status = str(country.get("status") or "active")
    frozen = status in config.FROZEN_STATUSES

    blocks: list[str] = [
        header_section(
            country,
            change_block=build_change_block(changes or [], updated_at=updated_at),
            days_ago=days_ago,
        ),
        indicators_section(country, frozen),
        infrastructure_section(country),
        military_section(country, frozen),
    ]

    # Группировки печатаются только если они есть: пустой раздел занимает
    # четыре строки и ничего не сообщает.
    if factions:
        factions_body = factions_section(factions)
        if factions_body:
            blocks.append(factions_body)

    blocks.append(social_section(country, frozen))

    summary_body = summary_section(summary)
    if summary_body:
        blocks.append(summary_body)
    blocks.append(notifications_section(notifications))

    # Навигация по карточкам: предыдущая / следующая страна.
    nav_parts: list[str] = []
    if prev_country:
        nav_parts.append(f"← Пред.: {esc(prev_country)}")
    if next_country:
        nav_parts.append(f"След.: {esc(next_country)} →")
    if nav_parts:
        blocks.append(
            f"<i>Навигация: {'   |   '.join(nav_parts)}</i>"
        )

    # Между блоками — тонкий разделитель: карточка длинная, без него разделы
    # сливаются в одну «простыню».
    sep = f"\n\n{SEP}\n\n"
    return sep.join(b for b in blocks if b)
