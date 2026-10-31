"""
Cleanup: приведение списка стран к канону (справочник + анрег + автономии).

Задача - убрать из БД страны, которых в каноне нет, не потеряв при этом
историю: переименования, старые новости, армию и уже опубликованные карточки.

Порядок работы со «странным» именем важен, потому что вариантов три, а цена
ошибки разная:

  1. Это синоним/старое название живой страны  -> СЛИТЬ (merge_duplicates).
     Берём бо́льшие значения, а не сумму: «Фаустланд» и «Фаустланд
     (Faustland)» — один и тот же объект, и 60+1=61 было бы выдумкой.
  2. Это страна вне канона, но с накопленной историей -> УДАЛИТЬ строку,
     предварительно перенеся накопленное на каноническую страну, если
     определить преемника можно.
  3. Мусор без истории -> удалить.

По умолчанию модуль НИЧЕГО НЕ УДАЛЯЕТ: `cleanup()` только строит отчёт
(`dry_run=True`). Удаление включается явно, потому что необратимо.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from . import config
from .config import (
    DIRECTORY, ANREG, AUTONOMIES, MANUAL_ONLY_AUTONOMIES,
    normalize_name, resolve_directory_name, _norm,
)

log = logging.getLogger(__name__)


def canonical_names() -> set[str]:
    """Множество канонических имён, которые обязаны существовать в БД."""
    return set(DIRECTORY) | set(ANREG) | set(AUTONOMIES) | set(MANUAL_ONLY_AUTONOMIES)


def protected_names() -> set[str]:
    """Имена, которые cleanup не имеет права удалять ни при каких условиях."""
    names = canonical_names()
    # Значения RENAMES - это канон, но проверим и их: если каноническое имя
    # куда-то переименовано, страна всё равно должна остаться.
    names.update(config.RENAMES.values())
    return names


@dataclass
class CleanupPlan:
    """Что cleanup собирается сделать. Ничего не выполняет."""

    keep: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    to_merge: list[tuple[str, str]] = field(default_factory=list)
    to_delete: list[str] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)

    @property
    def destructive(self) -> int:
        return len(self.to_merge) + len(self.to_delete)

    def summary(self) -> str:
        lines = [
            f"Оставить: {len(self.keep)}",
            f"Создать/восстановить: {len(self.missing)}",
            f"Слить в канон: {len(self.to_merge)}",
            f"Удалить: {len(self.to_delete)}",
        ]
        if self.unknown:
            lines.append(f"НЕОПОЗНАНО (требует решения админа): {len(self.unknown)}")
        return "\n".join(lines)

    def detail(self) -> str:
        lines: list[str] = []
        if self.missing:
            lines.append("Отсутствуют в БД: " + ", ".join(self.missing))
        if self.to_merge:
            lines.append("Слить (дубль -> канон):")
            lines += [f"  {dup} -> {canon}" for dup, canon in self.to_merge]
        if self.to_delete:
            lines.append("Удалить (нет в каноне): " + ", ".join(self.to_delete))
        if self.unknown:
            lines.append(
                "Требуют решения (похожи на страны, но не совпали): "
                + ", ".join(self.unknown)
            )
        return "\n".join(lines)


def _similar(name: str, candidates: list[str], threshold: float = 0.72) -> str | None:
    """Ближайшее каноническое имя по доле общих символов.

    Нужен, чтобы «Балтослободня» не удалилась молча, а попала в unknown и
    ждала решения админа. Порог 0.72 подбирался на реальных именах.
    """
    target = _norm(name)
    if not target:
        return None
    best: tuple[float, str] | None = None
    for candidate in candidates:
        key = _norm(candidate)
        # Кандидат, отличный от самой проверяемой строки: иначе любая
        # «Мусорлендия» идеально совпадает сама с собой и попадает в unknown
        # вместо того, чтобы быть удалённой.
        if not key or key == target:
            continue
        common = sum(1 for ch in set(target) & set(key))
        if not common:
            continue
        score = common / max(len(set(target)), len(set(key)))
        # Учитываем общий префикс: «Республика Х» ближе к «Республика Х»,
        # чем к «Союз Западных Штатов» с тем же количеством общих букв.
        prefix = 0
        for a, b in zip(target, key):
            if a != b:
                break
            prefix += 1
        score += min(prefix, 12) / 24.0
        if best is None or score > best[0]:
            best = (score, candidate)
    if best and best[0] >= threshold:
        return best[1]
    return None


async def build_plan(db) -> CleanupPlan:
    """Собрать план, ничего не меняя."""
    plan = CleanupPlan()
    canon = canonical_names()
    protect = protected_names()
    rows = await db._fetchall("SELECT name FROM countries")
    present = {str(r["name"]) for r in rows}

    plan.keep = sorted(present & protect)
    for name in sorted(set(DIRECTORY) - present):
        plan.missing.append(name)

    for name in sorted(present - protect):
        resolved = resolve_directory_name(name)
        if resolved and resolved != name and resolved in present:
            plan.to_merge.append((name, resolved))
            continue
        # Строка вне канона. Если её канон есть в БД - это дубль, который
        # resolve_directory_name не узнал (опечатка, другое написание).
        canon_of = normalize_name(name)
        if canon_of != name and canon_of in present:
            plan.to_merge.append((name, canon_of))
            continue
        # Ищем похожее и среди строк БД, и среди всего справочника. Если
        # опечатка в имени страны ещё не заведена в БД, это всё равно должно
        # попасть в unknown, а не в удаление: удалять «Балтослободня» молча
        # нельзя, это почти наверняка «Балтослободия».
        similar = _similar(name, sorted(present | set(DIRECTORY)))
        if similar:
            plan.unknown.append(f"{name} ~ {similar}")
        else:
            plan.to_delete.append(name)
    return plan


async def cleanup(db, *, dry_run: bool = True, bot=None) -> CleanupPlan:
    """Привести БД к канону.

    dry_run=True (по умолчанию) - только отчёт.
    bot - если передан, админу отправляется отчёт о том, что удалено.
    """
    plan = await build_plan(db)
    log.info("cleanup план:\n%s", plan.summary())

    if dry_run:
        return plan

    for dup, canon_name in plan.to_merge:
        log.info("cleanup: сливаю дубль %s -> %s", dup, canon_name)
        # dedupe_country, а не merge_countries: берём бо́льшие значения и не
        # складываем население. «ЗХАД» и «ЗХАД — Западный Хавен-аль-Джихад» —
        # один и тот же объект, 100+40 населения не существует.
        await db.dedupe_country(canon_name, dup)

    for name in plan.to_delete:
        log.warning("cleanup: удаляю страну вне канона: %s", name)
        await db.delete_country(name)

    # Подчистка висячих ссылок: researched_news/pending_news и т.п.
    await db.purge_orphans()

    if bot is not None and plan.destructive:
        try:
            await bot.send_message(
                config.ADMIN_ID,
                "🧹 Cleanup завершён\n"
                f"Слито: {len(plan.to_merge)}\n"
                f"Удалено: {len(plan.to_delete)}\n"
                + (f"Требуют решения: {', '.join(plan.unknown)}"
                   if plan.unknown else ""),
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("cleanup: отчёт админу не отправлен: %s", exc)
    return plan


def parse_manual_autonomy_rows(rows: list[dict[str, Any]]) -> list[str]:
    """Отделить manual-only автономии (Дацберг, Супер Земля) от стран.

    Они описаны в разделе 5, но в справочнике их нет: в таблице countries
    им не место, стартовые цифры живут в manual_stats. Cleanup их не трогает.
    """
    manual_only = {_norm(n) for n in MANUAL_ONLY_AUTONOMIES}
    return [str(r.get("name") or "") for r in rows
            if _norm(str(r.get("name") or "")) in manual_only]
