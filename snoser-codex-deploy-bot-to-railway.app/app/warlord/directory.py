"""
Приведение БД в соответствие со справочником стран (п.3, п.11 ТЗ).

Запускается при старте. По умолчанию — ТОЛЬКО ОТЧЁТ (apply=False): сначала
админ смотрит, что собирается удалить, и только потом подтверждает.

Четыре действия:

1. ПЕРЕИМЕНОВАНИЕ -> страна сливается в каноническое имя со всей историей,
   хроникой, новостями и армией. Числа НЕ складываются: это один и тот же
   объект, записанный дважды (см. db.dedupe_country).
2. АНРЕГ -> страна помечается status='anreg', карточка не публикуется, история
   остаётся.
3. ЛИШНЯЯ -> страны, которых нет ни в справочнике, ни в переименованиях:
   удаляется вместе со своими группировками, новостями и хроникой.
4. ОТСУТСТВУЕТ -> страны справочника, которых ещё нет в БД: создаются
   заготовками со статусом 'not_registered' (карточка не публикуется, доход не
   начисляется), чтобы ассистент их узнавал и отвечал «не зарегистрирована».

Инвариант: ничего не удаляется, пока apply=False.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable

from .config import (
    ANREG, AUTONOMIES, DIRECTORY, RENAMES,
    _norm, resolve_directory_name,
)

log = logging.getLogger(__name__)

# Статусы, при которых карточка не публикуется и показатели не растут.
INACTIVE_STATUSES = ("inactive", "afk", "neutral", "data_error", "not_registered")


@dataclass
class DirectoryReport:
    """Что именно enforce_directory собирается сделать."""

    renamed: list[tuple[str, str]] = field(default_factory=list)
    merged: list[tuple[str, str]] = field(default_factory=list)   # (old, canonical)
    anreg: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    created: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)  # не опознано, оставлено как есть

    @property
    def destructive(self) -> int:
        return len(self.deleted)

    def summary(self) -> str:
        return (
            f"переименовать: {len(self.renamed)}, слить: {len(self.merged)}, "
            f"анрег: {len(self.anreg)}, УДАЛИТЬ: {len(self.deleted)}, "
            f"создать: {len(self.created)}, оставить: {len(self.kept)}"
        )

    def detail(self) -> str:
        out: list[str] = []
        if self.merged:
            out.append("СЛИТЬ (история и армия сохраняются):")
            out += [f"  {o} -> {c}" for o, c in self.merged]
        if self.renamed:
            out.append("ПЕРЕИМЕНОВАТЬ:")
            out += [f"  {o} -> {c}" for o, c in self.renamed]
        if self.anreg:
            out.append("АНРЕГ (история остаётся, карточка не публикуется):")
            out += [f"  {n}" for n in self.anreg]
        if self.deleted:
            out.append("УДАЛИТЬ:")
            out += [f"  {n}" for n in self.deleted]
        if self.created:
            out.append("СОЗДАТЬ заготовку:")
            out += [f"  {n}" for n in self.created]
        if self.unknown:
            out.append("НЕ ОПОЗНАНО (оставлено как есть):")
            out += [f"  {n}" for n in self.unknown]
        return "\n".join(out)


def _norm_set(names) -> set[str]:
    return {_norm(n) for n in names}


async def plan_directory(db) -> DirectoryReport:
    """Разобрать текущее состояние БД и вернуть план действий. Ничего не меняет."""
    rep = DirectoryReport()
    countries = await db.list_countries()
    by_name = {c["name"]: c for c in countries}
    existing = {_norm(c["name"]) for c in countries}
    anreg_norms = _norm_set(ANREG)

    # Защита: страны с принятой анкетой (accepted_at) или ручной статистикой
    # не должны удаляться справочником
    protected_norms = set()
    for c in countries:
        if c.get("accepted_at") or c.get("stats_source") == "manual":
            protected_norms.add(_norm(c["name"]))

    # Страны, которые надо перевести на каноническое имя: старое -> каноническое.
    # Если на одно каноническое имя претендуют несколько записей, переименованием
    # может быть только ПЕРВАЯ (самая полная), остальные обязаны слиться: иначе
    # rename_country увидит уже существующее имя и вызовет merge_countries,
    # который СУММИРУЕТ показатели (Румия 1 + 1 + 54 = население 56 из воздуха).
    pending: list[tuple[str, str]] = []
    for c in countries:
        name = c["name"]
        key = _norm(name)

        # 1. Анрег важнее переименования: несуществующая страна не должна
        #    «вливаться» в живую страну.
        if key in anreg_norms:
            rep.anreg.append(name)
            continue

        # 2. Каноническое имя из справочника (в т.ч. через переименование)
        canonical = resolve_directory_name(name)
        if canonical is None:
            # Защита: принятые анкеты и мануальные страны не удаляем
            if key in protected_norms:
                rep.kept.append(name)
            else:
                rep.deleted.append(name)
            continue

        if _norm(canonical) == key:
            rep.kept.append(name)
        else:
            pending.append((name, canonical))

    # Самая полная запись становится канонической, остальные сливаются в неё.
    def richness(pair: tuple[str, str]) -> tuple:
        c = by_name.get(pair[0], {})
        return (
            1 if (c.get("ruler") or "").strip() else 0,
            1 if c.get("registered_at") else 0,
            float(c.get("population") or 0),
            float(c.get("army_infantry") or 0),
        )

    by_target: dict[str, list[tuple[str, str]]] = {}
    for old, canonical in pending:
        by_target.setdefault(_norm(canonical), []).append((old, canonical))

    for group in by_target.values():
        group.sort(key=richness, reverse=True)
        first_old, first_canon = group[0]
        if _norm(first_canon) in existing:
            # каноническая запись уже есть в БД - это слияние, не переименование
            rep.merged.append((first_old, first_canon))
        else:
            rep.renamed.append((first_old, first_canon))
        for old, canonical in group[1:]:
            rep.merged.append((old, canonical))

    # 3. Страны справочника, которых нет в БД
    deleted_norms = {_norm(n) for n in rep.deleted}
    covered = {resolve_directory_name(c["name"]) for c in countries
               if _norm(c["name"]) not in deleted_norms}
    covered_norm = {_norm(c) for c in covered if c}
    for _, target in rep.renamed + rep.merged:
        covered_norm.add(_norm(target))
    for name in DIRECTORY:
        if _norm(name) not in covered_norm:
            rep.created.append(name)

    rep.renamed.sort()
    rep.merged.sort()
    return rep


async def enforce_directory(
    db,
    *,
    apply: bool = False,
    protect_names: set[str] | None = None,
    on_delete: Callable[[str], None] | None = None,
) -> DirectoryReport:
    """Синхронизировать страны со справочником.

    apply=False (по умолчанию) -> только план, БД не трогаем.
    protect_names -> эти страны удалять нельзя даже если их нет в справочнике
    (страховка от потери данных: например, countries, созданные вручную).
    """
    rep = await plan_directory(db)
    protect = {_norm(n) for n in (protect_names or set())}

    blocked = [n for n in rep.deleted if _norm(n) in protect]
    if blocked:
        rep.deleted = [n for n in rep.deleted if _norm(n) not in protect]
        rep.unknown.extend(blocked)
        log.warning("directory: %d стран защищены от удаления", len(blocked))

    if not apply:
        log.info("directory (DRY-RUN): %s", rep.summary())
        return rep

    countries = await db.list_countries()
    dir_norms = {_norm(n) for n in DIRECTORY}

    # Существующие страны справочника: если у них есть население, но статус
    # непубликуемый (neutral/not_registered/not_accepted) — делаем active, чтобы
    # карточки публиковались.
    for c in countries:
        name = c["name"]
        if _norm(name) in dir_norms:
            pop = int(c.get("population") or 0)
            status = c.get("status") or ""
            if pop > 0 and status in ("neutral", "not_registered", "not_accepted"):
                await db.set_country_status(name, "active")
                await db.log_change(name, "status", status, "active",
                                   "directory: страна справочника с населением -> active")
                log.info("directory: %s статус %s -> active (pop=%d)", name, status, pop)

    # --- 1. Переименования и слияния. Каноническое имя должно существовать
    #        к моменту слияния, поэтому сначала создаём недостающие.
    for name in rep.created:
        await db.upsert_country(
            {"name": name, "population": 0, "status": "not_registered",
             "summary": "Страна из справочника, данных пока нет"}
        )
        log.info("directory: создана заготовка %s", name)

    for old, canonical in rep.renamed:
        await db.rename_country(old, canonical)
        await db.log_change(canonical, "name", old, canonical, "переименование по справочнику")
        log.info("directory: переименовано %s -> %s", old, canonical)

    for old, canonical in rep.merged:
        before = next((c for c in await db.list_countries() if c["name"] == old), {})
        await db.dedupe_country(canonical, old)
        await db.log_change(
            canonical, "name", old, canonical, "слито со старым названием по справочнику"
        )
        log.info(
            "directory: слито %s -> %s (нас=%s, арм=%s сохранены)",
            old, canonical, before.get("population"), before.get("army_infantry"),
        )

    # --- 2. Анрег: помечаем, ничего не удаляем.
    for name in rep.anreg:
        await db.set_country_status(name, "anreg")
        await db.log_change(name, "status", "active", "anreg", "анрег по справочнику")
        log.info("directory: анрег %s", name)

    # --- 3. Лишние: удаляем вместе со связанными записями.
    for name in rep.deleted:
        await db.delete_country(name)
        await db.delete_entity("country", name)
        if on_delete:
            on_delete(name)
        log.warning("directory: УДАЛЕНА страна %s", name)

    # --- 4. Подчистка: новости, хроника, личности и сущности удалённых стран.
    purged = await db.purge_orphans()
    total = sum(purged.values())
    if total:
        log.warning("directory: удалено осиротевших записей: %s", purged)

    log.warning("directory: применено. %s", rep.summary())
    return rep
