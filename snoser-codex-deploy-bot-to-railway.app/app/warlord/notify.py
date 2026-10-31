"""
Оповещения об обновлении карточек (раздел 10 ТЗ, модуль CARDS).

Четыре уровня, каждый отвечает за своё:

  1. внутри карточки   -> card_template.build_change_block()
  2. сообщение в теме  -> topic_cycle_summary()
  3. лично правителю   -> RulerNotifier.notify()
  4. отчёт админу за сутки -> daily_report()

Уровни 2-4 требуют бота. Функции принимают его первым параметром, а не импортируют
глобально, чтобы их можно было тестировать с заглушкой.

Критично (раздел 10): уведомление НЕ отправляется, если изменений не было.
Это проверяется в каждой точке отправки, а не только в логике вызова - иначе
пустое «🔄 Обновление завершено: 0 стран» будет спамить тему каждые 2 часа.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from . import config
from .config import config as cfg

log = logging.getLogger(__name__)


def _num(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _pct_change(old: Any, new: Any) -> float | None:
    """Изменение в процентах. None = сравнивать не с чем.

    Возвращать 100% при нулевом «до» нельзя: у только что созданной страны
    все prev_* равны нулю, и каждая страна получала бы «бюджет +100%,
    стабильность +100 п.п.» в первый же цикл. Это не изменение, а стартовое
    состояние, поэтому базы нет -> None -> порог не проверяется.
    """
    o, n = _num(old), _num(new)
    if abs(o) < 1e-9:
        return None
    return (n - o) / abs(o) * 100.0


# ══════════════════════════ УРОВЕНЬ 2. СООБЩЕНИЕ В ТЕМЕ ═══════════════════

def _countries_word(count: int) -> str:
    """«1 страна», «2 страны», «5 стран» - в отчёте это сразу бросается в глаза."""
    n = abs(count) % 100
    if 11 <= n <= 14:
        return "стран"
    n %= 10
    if n == 1:
        return "страна"
    if n in (2, 3, 4):
        return "страны"
    return "стран"


def _fmt_number(value: Any) -> str:
    """Аккуратные числа для сводки: 108364.4 -> «108 364,4», 35.0 -> «35».
    Не-числа показываем как есть."""
    if value is None or value == "":
        return "-"
    try:
        n = float(value)
    except (TypeError, ValueError):
        return str(value)
    if abs(n - round(n)) < 1e-9:
        return f"{int(round(n)):,}".replace(",", " ")
    return f"{n:,.1f}".replace(",", " ").replace(".", ",")


def build_topic_summary(
    updates: dict[str, list[dict[str, Any]]],
    next_run: datetime | None = None,
) -> str:
    """Текст сообщения в целевую тему после цикла.

    updates: {страна: [{field, old_value, new_value, op}]}
    Пустой dict -> "" (уведомление не отправляется, раздел 10).
    """
    if not updates:
        return ""
    lines = [f"🔄 Обновление карточек завершено: {len(updates)} {_countries_word(len(updates))}"]
    for name, changes in list(updates.items())[:10]:
        key_bits: list[str] = []
        for change in changes[:3]:
            field = str(change.get("field") or "?")
            old_v = _fmt_number(change.get("old_value"))
            new_v = _fmt_number(change.get("new_value"))
            if change.get("op") == "added":
                key_bits.append(f"{field}: + {new_v}")
            elif change.get("op") == "removed":
                key_bits.append(f"{field}: - {old_v}")
            else:
                key_bits.append(f"{field}: {old_v} → {new_v}")
        lines.append(f"📊 {name}: {'; '.join(key_bits)}")
    if len(updates) > 10:
        lines.append(f"   …и ещё {len(updates) - 10}")
    if next_run is not None:
        lines.append(
            f"⏰ Следующее обновление: {next_run.strftime('%d.%m.%Y %H:%M')}")
    return "\n".join(lines)


async def send_topic_summary(
    bot, updates: dict[str, list[dict[str, Any]]],
    next_run: datetime | None = None, *,
    chat_id: int | None = None, thread_id: int | None = None,
) -> bool:
    """Отправить уровень 2. False = отправлять нечего.

    chat_id/thread_id обязаны приходить от вызывающего: тему карточек ищет
    mechanic, и молчаливый фолбэк на TARGET_TOPIC_ID отправлял бы отчёт
    не туда (в общий чат или в топик новостей).
    """
    text = build_topic_summary(updates, next_run)
    if not text:
        return False
    target_chat = chat_id or config.TARGET_CHAT_ID
    target_thread = thread_id if thread_id is not None else config.TARGET_TOPIC_ID
    if not target_chat or not target_thread:
        log.warning("notify: нет чата/темы для отчёта — молчим")
        return False
    try:
        await bot.send_message(
            target_chat, text,
            message_thread_id=target_thread,
            disable_web_page_preview=True,
        )
        return True
    except Exception as exc:  # noqa: BLE001 - уведомление не должно ронять цикл
        log.warning("notify: не удалось отправить отчёт в тему: %s", exc)
        return False


# ══════════════════════ УРОВЕНЬ 3. ЛИЧНОЕ УВЕДОМЛЕНИЕ ПРАВИТЕЛЮ ══════════

def is_significant(old: dict[str, Any], new: dict[str, Any]) -> tuple[bool, list[str]]:
    """Существенное ли изменение (раздел 10)?

    Пороги: бюджет ±10%, армия ±20%, стабильность ±5%, смена статуса.
    Возвращает (да/нет, список ключевых изменений).
    """
    notes: list[str] = []
    old_status = str(old.get("status") or "active")
    new_status = str(new.get("status") or "active")
    if old_status != new_status:
        notes.append(f"статус: {old_status} → {new_status}")

    bud_pct = _pct_change(old.get("budget"), new.get("budget"))
    if bud_pct is not None and abs(bud_pct) >= cfg.RULER_BUDGET_PCT:
        notes.append(f"бюджет: {bud_pct:+.0f}%")

    army_pct = _pct_change(old.get("army_infantry"), new.get("army_infantry"))
    if army_pct is not None and abs(army_pct) >= cfg.RULER_ARMY_PCT:
        notes.append(f"армия: {army_pct:+.0f}%")

    stab_pct = _pct_change(old.get("stability"), new.get("stability"))
    if stab_pct is not None and abs(stab_pct) >= cfg.RULER_STABILITY_PCT:
        notes.append(f"стабильность: {stab_pct:+.0f} п.п.")

    return bool(notes), notes


def build_ruler_message(
    country: dict[str, Any], notes: list[str], link: str = "",
) -> str:
    """Текст личного уведомления правителю."""
    name = str(country.get("name") or "страна")
    body = "\n".join(f"• {n}" for n in notes)
    text = f"📊 Твоя страна {name} обновлена.\nКлючевые изменения:\n{body}"
    if link:
        text += f"\n\nПолная карточка: {link}"
    return text


class RulerNotifier:
    """Уровень 3. Копит уведомления за цикл и шлёт их пачкой в конце.

    Копилка нужна, потому что за один цикл страна может обновиться несколько
    раз, и писать правителю на каждое событие - это спам.

    Если у страны нет ruler_chat_id, уведомление адресуется админу: терять
    значимое изменение молча нельзя, но и писать в пустоту тоже.
    """

    def __init__(self, bot, admin_fallback: bool = True) -> None:
        self.bot = bot
        self.admin_fallback = admin_fallback
        self._queue: dict[tuple[int, str], dict[str, Any]] = {}

    def collect(
        self, old: dict[str, Any], new: dict[str, Any],
        link: str = "", chat_id: int | None = None,
    ) -> bool:
        """Заложить уведомление. False = несущественное, не кладём."""
        if not cfg.NOTIFY_RULERS:
            return False
        significant, notes = is_significant(old, new)
        if not significant:
            return False
        target = chat_id or new.get("ruler_chat_id") or old.get("ruler_chat_id")
        if not target and not self.admin_fallback:
            return False
        if not target:
            target = config.ADMIN_ID
        name = str(new.get("name") or old.get("name") or "")
        # Один чат может править несколькими странами - ключ включает страну.
        self._queue[(int(target), name)] = {
            "chat_id": int(target),
            "country": new,
            "notes": notes,
            "link": link,
        }
        return True

    @property
    def pending(self) -> int:
        return len(self._queue)

    async def flush(self) -> int:
        """Отправить накопленные уведомления. Возвращает число доставленных."""
        if not self._queue:
            return 0
        items = list(self._queue.values())
        self._queue.clear()
        sent = 0
        for item in items:
            text = build_ruler_message(item["country"], item["notes"], item["link"])
            try:
                await self.bot.send_message(item["chat_id"], text)
                sent += 1
            except Exception as exc:  # noqa: BLE001
                log.warning("notify: уведомление правителю %s не доставлено: %s",
                            item["country"].get("name"), exc)
        return sent


# ═══════════════════════ УРОВЕНЬ 4. ЕЖЕДНЕВНЫЙ ОТЧЁТ АДМИНУ ═════════════

def build_daily_report(
    totals: dict[str, int],
    inactive: list[str],
    afk: list[str],
    neutral: list[str],
    deleted: list[str] | None = None,
) -> str:
    """Текст отчёта за сутки (раздел 10, уровень 4)."""
    def _names(items: list[str], limit: int = 8) -> str:
        if not items:
            return "нет"
        shown = ", ".join(items[:limit])
        return shown + (f" и ещё {len(items) - limit}" if len(items) > limit else "")

    lines = [
        "📊 Отчёт за сутки:",
        f"• Обновлено карточек: {totals.get('countries_updated', 0)}",
        f"• Изменений: {totals.get('changes_total', 0)}",
        f"• Откатов: {totals.get('rollbacks', 0)}",
        f"• Ошибок: {totals.get('errors', 0)}",
        f"• Стран в inactive: {_names(inactive)}",
        f"• Стран в afk: {_names(afk)}",
        f"• Стран в neutral: {_names(neutral)}",
    ]
    if deleted:
        lines.append(f"• Удалено стран: {_names(deleted)}")
    return "\n".join(lines)


def report_due(now: datetime | None = None, last_sent: str = "") -> bool:
    """Пора ли слать отчёт. Время из config.DAILY_REPORT_TIME, не чаще раза в сутки."""
    now = now or datetime.now()
    try:
        hh, mm = (int(part) for part in cfg.DAILY_REPORT_TIME.split(":", 1))
    except (ValueError, AttributeError):
        hh, mm = 9, 0
    if now.hour < hh or (now.hour == hh and now.minute < mm):
        return False
    if last_sent:
        try:
            last = datetime.fromisoformat(last_sent)
        except ValueError:
            return True
        if (now - last).total_seconds() < 86400:
            return False
    return True


async def send_daily_report(
    db, bot, *, now: datetime | None = None, admin_id: int | None = None,
) -> str | None:
    """Отправить уровень 4. None = отправлять нечего."""
    now = now or datetime.now()
    today = now.date().isoformat()
    if await db.card_report_sent_today(today):
        return None

    totals = await db.card_cycle_totals(hours=24)

    async def _by_status(status: str) -> list[str]:
        rows = await db.list_countries(status=status)
        return [str(row.get("name") or "") for row in rows if row.get("name")]

    deleted: list[str] = []
    for row in await db.card_cycles_since(24):
        raw = str(row["deleted"] or "")
        deleted.extend(x.strip() for x in raw.split(",") if x.strip())

    text = build_daily_report(
        totals,
        inactive=await _by_status("inactive"),
        afk=await _by_status("afk"),
        neutral=await _by_status("neutral"),
        deleted=deleted,
    )
    try:
        await bot.send_message(admin_id or config.ADMIN_ID, text)
    except Exception as exc:  # noqa: BLE001
        log.warning("notify: суточный отчёт не отправлен: %s", exc)
        return None
    await db.mark_card_report_sent(today)
    return text


# ═════════════════════ ОПОВЕЩЕНИЕ ОБ АНОМАЛИЯХ (разделы 8, 11) ═══════════

async def alert_admin(
    db, bot, subject: str, details: list[str], *, ask_once: bool = True,
) -> bool:
    """Уведомить админа о проблеме, требующей решения.

    ask_once=True - не спамить: вопрос уходит один раз, ответ ждёт в
    admin_requests. Для повторяющихся проблем (population < 60 у 8 стран) это
    критично, иначе каждая проверка создаёт новый вопрос.
    """
    body = "\n".join(f"• {d}" for d in details) if details else "• -"
    text = f"⚠️ {subject}\n{body}"
    if ask_once:
        asked = await db.ask_admin_once(subject)
        if not asked:
            log.info("notify: вопрос админу уже задан, молчим: %s", subject)
            return False
    try:
        target = config.ADMIN_QUESTIONS_CHAT or config.ADMIN_ID
        await bot.send_message(target, text)
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("notify: уведомление админу не доставлено: %s", exc)
        return False
