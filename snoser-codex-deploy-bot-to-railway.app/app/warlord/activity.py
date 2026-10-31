"""
Активность стран (раздел 8 ТЗ, модуль CARDS).

Модель построена на ДНЯХ без постов, а не на часах:

    active       <= 3 дней без постов     обычный рост
    inactive      3 < d <= 7               рост заморожен
    afk           7 < d <= 30             заморозка + ежедневные расходы
    neutral      d > 30                   обнуление, карточка не публикуется
    data_error    население < 60 и нечего восстановить
    not_accepted  страна ещё не принята (никогда не постила)

Отличие от прежней (часовой) модели: neutral больше не означает «молчит 2 часа».
Пауза в пару часов — это нормальный перерыв между постами, а не смерть страны.
Молчание теперь обрабатывает цикл публикации (раздел 7.5: «Если новостей по
стране нет — карточка не перерисовывается»), а не статус.

Возврат в active (раздел 8, раздел 5):
    inactive -> active   рост просто возобновляется
    afk      -> active   рост + штраф стабильности -10%
    neutral   -> active   страна с нуля, ТОЛЬКО по решению админа
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from . import config

log = logging.getLogger(__name__)

# Служебные статусы: активность их не пересчитывает.
# data_error здесь быть НЕ должен: это наш собственный вердикт о population < 60,
# и после починки населения страна обязана выйти из него. Иначе она застревает
# в data_error навсегда и больше не проверяется.
LIFECYCLE_STATUSES = ("anreg", "unreg")

# Псевдонимы «страна не принята»: в БД встречаются оба написания.
NOT_ACCEPTED = set(config.NOT_ACCEPTED_ALIASES)


def _parse_ts(value: Any) -> datetime | None:
    """Разобрать дату из SQLite (ISO-строка) или из datetime. TZ -> naive UTC."""
    if isinstance(value, datetime):
        # Если уже datetime — приводим к naive UTC
        if value.tzinfo is None:
            return value
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text)
    except (ValueError, TypeError):
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(text, fmt)
                break
            except (ValueError, TypeError):
                continue
        else:
            return None
    # Всегда возвращаем naive UTC
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def days_since_post(last_post: datetime | None, now: datetime | None = None) -> float | None:
    """Сколько дней прошло с последнего поста. None -> постов не было."""
    if last_post is None:
        return None
    if now is None:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
    elif now.tzinfo is not None:
        now = now.astimezone(timezone.utc).replace(tzinfo=None)
    return (now - last_post).total_seconds() / 86400.0


def status_for_days(days: float | None) -> str:
    """Чистая функция: дни без постов -> статус активности (раздел 8)."""
    if days is None:
        return "not_accepted"
    if days <= config.ACTIVITY_ACTIVE_MAX_DAYS:
        return "active"
    if days <= config.ACTIVITY_INACTIVE_AFTER_DAYS:
        return "inactive"
    if days <= config.ACTIVITY_AFK_AFTER_DAYS:
        return "afk"
    return "neutral"


# Совместимость с прежним вызовом compute_status(news_count, last_post, now):
# число новостей в новой модели не влияет на статус, но аргумент остаётся,
# чтобы старые вызовы и тесты не падали.
def compute_status(
    news_count: int = 0,
    last_post: datetime | None = None,
    now: datetime | None = None,
) -> str:
    """Статус по времени последнего поста. news_count оставлен для совместимости."""
    return status_for_days(days_since_post(last_post, now))


def is_frozen(status: str) -> bool:
    """Рост показателей заморожен?"""
    return status in config.FROZEN_STATUSES


def is_publishable_status(status: str) -> bool:
    """Можно ли публиковать карточку в этом статусе.

    inactive/afk — МОЖНО: по разделу 13 их карточки сохраняются с пометкой.
    neutral — нельзя: страна обнулена и не публикуется.
    """
    return status not in config.UNPUBLISHABLE_STATUSES


def can_publish(status: str, population: int | None = None) -> bool:
    """Публикуема ли карточка: статус допустим; население < минимума
    блокирует только «живые» статусы — забанеренные (anreg/unreg/...) и
    замороженные публикуются с баннером статуса при любом населении."""
    if not is_publishable_status(status):
        return False
    if population is not None and population < config.MIN_POPULATION:
        if str(status or "active") not in config.STATUS_BANNERED:
            return False
    return True


def recover_population(changes: list[dict[str, Any]]) -> int | None:
    """Найти последнее правдоподобное население в changes_log (раздел 11).

    Нельзя брать просто последнее записанное значение: именно оно и было
    испорчено. Типичная запись о поломке — «было 80, стало 1», поэтому
    хорошее число может лежать и в new_value, и в old_value. list_changes
    отдаёт историю от свежих записей к старым, значит первое правдоподобное
    значение и есть последнее корректное.
    """
    for row in changes:  # от свежих к старым
        field = str(row.get("field") or "").lower()
        if field not in ("population", "население"):
            continue
        for key in ("new_value", "old_value"):
            raw = row.get(key)
            if raw is None:
                continue
            try:
                val = int(float(str(raw).replace(" ", "").strip()))
            except (TypeError, ValueError):
                continue
            if val >= config.MIN_POPULATION:
                return val
    return None


def _afk_drift(country: dict[str, Any], days_afk: float,
               charge_days: float | None = None) -> list[tuple[str, Any]]:
    """Что происходит с показателями за время афка (раздел 8, AFK).

    Возвращает список (поле, новое_значение). Ничего не пишет в БД — вызывающий
    код применяет и логирует, чтобы все изменения шли через changes_log.

    days_afk — полные дни афка (для порогов), charge_days — сколько дней
    ЕЩЁ не списано. Разделение обязательно: check_activity зовёт эту функцию
    каждый цикл, и если списывать за все дни молчания каждый раз, страна,
    молчащая неделю, потеряет содержание семь раз за одну неделю и обанкротится
    за сутки. charge_days считается по журналу (см. _afk_charge_days), поэтому
    повторный вызов ничего не списывает.

    Доход у замороженной страны НЕ начисляется, а содержание остаётся, поэтому
    списываются только расходы: раньше тут был (income - expenses), и страна
    молчала, получая при этом зарплату.
    """
    out: list[tuple[str, Any]] = []
    charge_days = days_afk if charge_days is None else charge_days
    if days_afk <= config.ACTIVITY_INACTIVE_AFTER_DAYS or charge_days <= 0:
        return out
    # Меньше часа накопленного простоя списывать нечем: цикл ходит каждые
    # 2 часа, и иначе changes_log засорялся бы дробными изменениями в копейки.
    if charge_days < 1.0 / 24.0:
        return out

    # Расходы содержания: income не начисляется, expenses остаются.
    if config.AFK_DAILY_EXPENSES:
        expenses = float(country.get("expenses_daily") or 0.0)
        budget = float(country.get("budget") or 0.0)
        if expenses:
            out.append(("budget", budget - expenses * charge_days))

        # При минусе 3 дня подряд стабильность падает под 25%.
        projected = budget - expenses * charge_days
        if projected < 0 and days_afk >= config.AFK_STABILITY_DROP_DAYS:
            stability = float(country.get("stability") or 0.0)
            if stability > config.AFK_STABILITY_FLOOR_PCT:
                out.append(("stability", float(config.AFK_STABILITY_FLOOR_PCT)))

    # Дезертирство: -5% армии в полную неделю молчания. Считаем от
    # charge_days, а не от days_afk: полная неделя списывается один раз.
    if config.AFK_DESERTION_AUTO:
        weeks = charge_days // 7
        if weeks >= 1:
            factor = (1.0 - config.AFK_DESERTION_WEEKLY_PCT / 100.0) ** weeks
            for field in ("army_infantry", "army_light_vehicles",
                          "army_heavy_wheeled", "army_artillery",
                          "army_drones", "army_navy"):
                current = int(country.get(field) or 0)
                if current > 0:
                    out.append((field, int(current * factor)))
    return out


async def _afk_charge_days(db, name: str, days_afk: float,
                           now: datetime | None = None) -> float:
    """Сколько дней афка ещё не списано.

    Источник - последняя запись changes_log с меткой афк-штрафа. Так списание
    идемпотентно: повторный вызов в том же цикле или через час не спишет
    ничего, а после простоя бота доберёт пропущенные дни.
    """
    if days_afk <= 0:
        return 0.0
    row = await db._fetchone(
        "SELECT MAX(timestamp) t FROM changes_log WHERE country=? "
        "AND reason LIKE 'activity: афк-штраф%'", (name,))
    stamp = row["t"] if row else None
    last = _parse_ts(stamp) if stamp else None
    if last is None:
        return days_afk  # ни разу не списывали - списываем весь простой
    already = days_since_post(last, now)
    if already is None:
        return 0.0
    return max(0.0, min(days_afk, already))


def _apply_return_penalty(
    country: dict[str, Any], old_status: str, new_status: str,
) -> dict[str, Any] | None:
    """Штраф за возврат из afk (раздел 8, раздел 5). None — штрафа нет."""
    if new_status != "active":
        return None
    if old_status != "afk":
        return None
    stability = float(country.get("stability") or 0.0)
    return {
        "stability": max(0.0, stability - config.AFK_RETURN_STABILITY_PENALTY),
    }


async def check_activity(db, now: datetime | None = None) -> dict[str, Any]:
    """Пересчитать статус активности для всех стран. Вызывается каждый цикл.

    Возвращает сводку: кто сменил статус, кто заморожен, кому нужна анкета,
    кого обнулили, какие штрафы применены.
    """
    now = now or datetime.now()
    changed: list[tuple[str, str, str]] = []
    frozen: list[str] = []
    needs_admin: list[str] = []
    repaired: list[tuple[str, int]] = []
    drifted: list[tuple[str, str]] = []
    zeroed: list[str] = []
    needs_decision: list[str] = []

    rows = await db._fetchall("SELECT * FROM countries ORDER BY name")
    for row in rows:
        data = dict(row)
        name = str(data.get("name") or "")
        if not name:
            continue
        cid = int(data["id"])
        status = str(data.get("status") or "not_accepted")

        # Анрег/распущена — это решение админа, активность его не трогает.
        if status in LIFECYCLE_STATUSES:
            continue
        # not_accepted -> active переводит только админ, не мы.
        if status in NOT_ACCEPTED:
            frozen.append(name)
            continue

        # --- последний пост -------------------------------------------------
        # Свежесть страны берём по САМОМУ ПОЗДНЕМУ сигналу: отметка об
        # исследованной новости, last_post_at и last_activity_at (дата самого
        # сообщения-новости). Иначе свежий пост, ещё не перерисованный в
        # карточку, не спасал страну от ложного «afk».
        last_row = await db._fetchone(
            "SELECT MAX(researched_at) t FROM researched_news WHERE country_id=?",
            (cid,))
        candidates = [
            _parse_ts(last_row["t"]) if last_row and last_row["t"] else None,
            _parse_ts(data.get("last_post_at")),
            _parse_ts(data.get("last_activity_at")),
        ]
        last_post = max((d for d in candidates if d is not None), default=None)
        if last_post is not None:
            await db._execute(
                "UPDATE countries SET last_post_at=? WHERE id=?",
                (last_post.isoformat(timespec="seconds"), cid))

        days = days_since_post(last_post, now)
        # Авто-заморозку по времени последнего поста УБРАЛИ: страна не должна
        # замерзать/обнуляться сама. Статусы afk/inactive/neutral/anreg
        # назначаются вручную (команды /afk, /active, /unreg). Здесь только
        # пересчитываем last_post_at и (ниже) чиним население. status_for_days()
        # остаётся чистой функцией для отчётов/тестов, но статус из неё не берём.
        new_status = status

        # --- раздел 11: население -------------------------------------------
        population = int(data.get("population") or 0)
        # У neutral всё обнулено намеренно (раздел 8), восстанавливать
        # население из журнала нельзя: иначе каждый цикл страна оживает и
        # снова умирает, а changes_log растёт мусорными записями.
        if population < config.MIN_POPULATION and status != "neutral":
            changes = await db.list_changes(name, "population", limit=50)
            fixed_val = recover_population(changes)
            if fixed_val:
                await db._execute("UPDATE countries SET population=? WHERE id=?",
                                  (fixed_val, cid))
                await db.log_change(name, "population", str(population),
                                    str(fixed_val), "activity: восстановлено")
                repaired.append((name, fixed_val))
                population = fixed_val
            else:
                # Восстановить нечем: карточка не публикуется, админу уходит
                # запрос на заполнение анкеты.
                new_status = "data_error"
                needs_admin.append(name)

        # Восстановление из data_error: население починили — возвращаем active.
        if status == "data_error" and population >= config.MIN_POPULATION:
            new_status = "active"

        # --- АФК-дрейф: расходы, штраф стабильности, дезертирство -----------
        if new_status == "afk":
            charge_days = await _afk_charge_days(db, name, days or 0.0, now)
            for field, value in _afk_drift(data, days or 0.0, charge_days):
                await db._execute(
                    f"UPDATE countries SET {field}=? WHERE id=?", (value, cid))
                await db.log_change(name, field, str(data.get(field)), str(value),
                                    "activity: афк-штраф")
                drifted.append((name, field))

        # --- neutral: всё обнуляется (раздел 8) -----------------------------
        if new_status == "neutral":
            for field in ("population", "population_capacity", "budget",
                          "income_daily", "army_infantry", "army_light_vehicles",
                          "army_heavy_wheeled", "army_artillery", "army_drones",
                          "army_navy"):
                if data.get(field):
                    await db._execute(
                        f"UPDATE countries SET {field}=0 WHERE id=?", (cid,))
                    await db.log_change(name, field, str(data.get(field)), "0",
                                        "activity: нейтрализация")
            zeroed.append(name)

        # --- смена статуса ---------------------------------------------------
        if new_status != status:
            # neutral -> active только по решению админа: не возвращаем сами.
            if status == "neutral" and new_status == "active":
                needs_decision.append(name)
                continue
            penalty = _apply_return_penalty(data, status, new_status)
            if penalty:
                for field, value in penalty.items():
                    await db._execute(
                        f"UPDATE countries SET {field}=? WHERE id=?", (value, cid))
                    await db.log_change(name, field, str(data.get(field)), str(value),
                                        "activity: штраф за возврат")

            await db._execute("UPDATE countries SET status=? WHERE id=?",
                              (new_status, cid))
            await db.log_activity(name, status, new_status,
                                  f"пересчёт активности: {days:.1f} дн. без постов"
                                  if days is not None else "пересчёт активности")
            await db.log_change(name, "status", status, new_status,
                                "activity: пересчёт активности")
            changed.append((name, status, new_status))

        if is_frozen(new_status):
            frozen.append(name)

    summary = {
        "changed": changed,
        "frozen": frozen,
        "data_error": needs_admin,
        "repaired_population": repaired,
        "afk_drift": drifted,
        "neutralized": zeroed,
        "needs_admin_decision": needs_decision,
        "total": len(rows),
    }
    if changed:
        log.info("activity: сменили статус: %s", changed)
    if repaired:
        log.info("activity: восстановлено население: %s", repaired)
    if drifted:
        log.info("activity: афк-штрафы: %s", drifted)
    if zeroed:
        log.info("activity: нейтрализованы: %s", zeroed)
    if needs_admin:
        log.warning("activity: население < %s, нужна анкета: %s",
                    config.MIN_POPULATION, needs_admin)
    if needs_decision:
        log.warning("activity: neutral -> active только по решению админа: %s",
                    needs_decision)
    return summary
