"""
Мобилизация (п.5 ТЗ, БАГ 2).

Правила:
  * набор в армию распознаётся и по ключевым словам, и по флажку от LLM;
  * лимит солдат — 100% населения страны
    (config.ARMY_POPULATION_RATIO);
  * одно и то же сообщение не должно дважды добавлять солдат -
    дедуп по message_id через db.change_applied_for_message();
  * все движения численности пишутся в changes_log.
"""
from __future__ import annotations

import logging
import re
from typing import Any

from . import config

log = logging.getLogger(__name__)

# Признаки мобилизации/набора в тексте новости
_MOBILIZATION_RE = re.compile(
    r"(мобилиз|набор\s+в\s+арм|призыв|рекрут|вербов|комплектован|"
    r"призывник|служат|в\s+армию\s+ид|взяли\s+в\s+арм|вступа(ют|л)\s+в\s+арм)",
    re.IGNORECASE,
)

# Фразы, из-за которых нельзя писать «призыв» - это уже другое действие
_DEMOBILIZATION_RE = re.compile(
    r"(демобилиз|дедлайн\s+призыва|распущен|отпускник|вернулись\s+из\s+арм)",
    re.IGNORECASE,
)


def is_mobilization(text: str) -> bool:
    """Похоже ли это на набор в армию?"""
    if not text:
        return False
    if _DEMOBILIZATION_RE.search(text):
        return False
    return bool(_MOBILIZATION_RE.search(text))


def _safe_int(value: Any) -> int:
    try:
        return int(float(str(value).strip().replace(" ", "")))
    except (TypeError, ValueError):
        return 0


def mobilization_limit(population: Any) -> int:
    """Максимум солдат, который страна может иметь при данном населении."""
    pop = _safe_int(population)
    if pop <= 0:
        return 0
    return int(pop * config.ARMY_POPULATION_RATIO)


async def enforce_mobilization(
    db,
    name: str,
    updated: dict[str, Any],
    news_text: str = "",
    ai_flag: bool = False,
    message_ids: list[int] | None = None,
) -> dict[str, Any]:
    """Ограничить рост армии 30% населения и записать в changes_log.

    Мутирует `updated` (поэтому возвращает тот же словарь) и возвращает
    словарь с отчётом: сколько хотели, сколько разрешили, что записали.
    """
    report = {
        "wanted": 0,
        "granted": 0,
        "clamped": False,
        "limit": 0,
        "skipped_duplicate": False,
    }

    wanted_raw = updated.get("army_infantry")
    try:
        wanted = int(float(str(wanted_raw).strip().replace(" ", "")))
    except (TypeError, ValueError):
        wanted = 0
    report["wanted"] = wanted

    # Это не мобилизация - просто не трогаем лимит, но всё равно логируем,
    # что число солдат изменилось (для прозрачности).
    if not (ai_flag or is_mobilization(news_text)):
        return report

    limit = mobilization_limit(updated.get("population"))
    report["limit"] = limit

    # Дубликат: это сообщение уже учитывали - не начисляем солдат второй раз.
    # Проверка не под лимитом: повторный пост может прийти с любым числом, и
    # повторное начисление - это всегда ошибка, а не «нечего ограничивать».
    for mid in (message_ids or []):
        if await db.mobilization_applied_for_message(name, int(mid)):
            report["skipped_duplicate"] = True
            # Возвращаем army к тому, что уже записано в БД (prev_* на начало
            # цикла), иначе число из повторного поста всё равно ушло бы в
            # карточку - просто без записи в журнал.
            current = updated.get("prev_army_infantry")
            current_int = _safe_int(current) if current is not None else wanted
            report["granted"] = current_int
            updated["army_infantry"] = current_int
            log.info("mobilization: %s — сообщение %s уже учтено, пропуск",
                     name, mid)
            return report

    granted = min(wanted, limit)
    report["granted"] = granted
    report["clamped"] = wanted > limit
    updated["army_infantry"] = granted

    # Население уменьшается на число мобилизованных - и независимо от того,
    # превысили ли мы лимит: солдаты-то прибыли в любом случае.
    old = _safe_int(updated.get("prev_army_infantry"))
    if granted > old:
        mobilized = granted - old
        pop = _safe_int(updated.get("population"))
        updated["population"] = max(0, pop - mobilized)
        mid = (message_ids or [None])[0]
        await db.log_change(name, "army_infantry", str(wanted), str(granted),
                            f"mobilization: лимит {limit} = 100% населения",
                            message_id=mid)
        await db.log_change(name, "population", str(pop),
                            str(updated["population"]),
                            f"mobilization: -{mobilized} человек в армию",
                            message_id=mid)
    elif report["clamped"]:
        await db.log_change(name, "army_infantry", str(wanted), str(granted),
                            f"mobilization: лимит {limit} = 100% населения",
                            message_id=(message_ids or [None])[0])

    if report["clamped"]:
        log.info("mobilization: %s — %d солдат хотели, лимит %d (100%% населения)",
                 name, wanted, limit)
    return report
