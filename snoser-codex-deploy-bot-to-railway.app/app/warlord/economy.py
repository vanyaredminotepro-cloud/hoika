"""
Экономическая симуляция WARLORD RP (по официальному своду механик).

Бот САМ рассчитывает финансовые, демографические и общественные показатели
стран/группировок — детерминированно, без AI:

ДОХОД (в день):
  базовый доход ............ +1 000 ВР (всем по умолчанию)
  фабрика / гражданский завод +500 ВР за каждую
  порт/верфь ................ +300 ВР за каждый
  налоги .................... ставка * население
      Пониженный 0.5, Средний 1.0, Повышенный 1.5, Чрезмерный 2.0 ВР/чел/день

РАСХОД (в день):
  1 гражданин ................ 2 ВР
  1 пехотинец ................ 5 ВР
  1 ед. лёгкой техники ....... 20 ВР
  1 ед. тяжёлой колёсной ..... 50 ВР
  артиллерия / дроны / флот ... см. ARMY_UPKEEP

Стабильность: растёт при профиците, падает при дефиците и высоких налогах.
Банкротство (>3 дней минуса) роняет стабильность в критическую зону.
Числа заданы в «вирт-рублях» (ВР). Все формулы линейны и предсказуемы.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

# Налог с гражданина в день (ВР/чел), по уровню налогов (официальная шкала)
TAX_PER_CAPITA: dict[str, float] = {
    "пониженный": 0.5,
    "средний": 1.0,
    "повышенный": 1.5,
    "чрезмерный": 2.0,
}

# Содержание единицы армии в день (ВР) — официальные цифры (пехота/лёгкая/БРДМ)
ARMY_UPKEEP: dict[str, float] = {
    "army_infantry": 5,
    "army_light_vehicles": 20,
    "army_heavy_wheeled": 50,
    # вне официального прайса — обоснованные аналоги
    "army_artillery": 30,
    "army_drones": 15,
    "army_navy": 80,
}

# Базовый доход всем фракциям/странам в день (механика 1.1)
BASE_INCOME_DAILY = 1000.0
# Фабрика / гражданский завод
FACTORY_INCOME = 500.0
# Порт / верфь
PORT_INCOME = 300.0

# Доля «свободного» населения, которую страна может принять в день (к вместимости)
POPULATION_FILL_PER_DAY = 0.02
# Вместимость не может быть произвольной: максимум — в CAPACITY_MULTIPLE от
# текущего населения. Раньше AI мог написать population_capacity=25000, и
# «2% от свободного места» давало +500 человек в день.
CAPACITY_MULTIPLE = 3.0
MIN_CAPACITY = 100
# Вместимость от ИНФРАСТРУКТУРЫ: города и заводы дают рабочие места сверх
# базового потолка «население × CAPACITY_MULTIPLE». Без этого страна, построившая
# города/фабрики/порты, по-прежнему видит вместимость = 3×населения, и ассистент
# отвечает «нет инфраструктуры». Прирост населения всё равно ограничен
# MAX_GROWTH_PER_DAY/MAX_ABS_GROWTH_PER_DAY (50 чел/сутки), так что «вмíстимость»
# выше населения не даёт взрывного роста — только честный потолок.
INFRA_CAPACITY_PER_CITY = 20_000.0      # город / столица / мегаполис
INFRA_CAPACITY_PER_VILLAGE = 3_000.0    # деревня / село / посёлок / хутор
INFRA_CAPACITY_PER_SETTLEMENT = 7_000.0  # прочее поселение
INFRA_CAPACITY_BY_INDUSTRY = {
    "factory": 2_000.0,
    "civil_factory": 2_000.0,
    "military_factory": 1_000.0,
    "port": 3_000.0,
    "shipyard": 3_000.0,
}
# Жёсткий потолок прироста: не более 2% населения и не более 50 человек в сутки
MAX_GROWTH_PER_DAY = 0.02
MAX_ABS_GROWTH_PER_DAY = 50.0
# Естественная убыль при низкой стабильности (эмиграция), % в день
DEPOPULATION_PER_DAY = 0.01
# Сколько дней казны считается безопасным запасом
TREASURY_SAFETY_DAYS = 7.0
# Дней подряд минусового бюджета до кризисных последствий
BANKRUPT_DAYS = 3.0

# Падение стабильности в %/неделю при высоких налогах (механика 1.2)
STABILITY_TAX_PENALTY = {
    "пониженный": 0.0,
    "средний": 0.0,
    "повышенный": 5.0,
    "чрезмерный": 12.0,
}


def clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _num(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _items(value: Any) -> list[Any]:
    if not isinstance(value, list):
        return []
    return value


def _tax_rate(level: Any) -> float:
    return TAX_PER_CAPITA.get(str(level or "").strip().lower(), 1.0)


def _count_in(val: Any, keywords: tuple[str, ...]) -> int:
    """Сколько объектов в списке содержат одну из ключевых подстрок."""
    total = 0
    for item in _items(val):
        name = item if isinstance(item, str) else str((item or {}).get("name") or "")
        low = name.lower()
        if any(kw in low for kw in keywords):
            total += 1
    return total


def industry_income(industry: Any) -> float:
    """Дневной доход от промышленности: фабрики/заводы +500, порты/верфи +300."""
    factories = _count_in(industry, ("фабрик", "гражданский завод", "завод"))
    ports = _count_in(industry, ("порт", "верф"))
    return factories * FACTORY_INCOME + ports * PORT_INCOME


def count_factories(industry: Any) -> int:
    return _count_in(industry, ("фабрик", "гражданский завод", "завод"))


def count_ports(industry: Any) -> int:
    return _count_in(industry, ("порт", "верф"))


def _item_name(value: Any) -> str:
    """Имя элемента списка: строка, {"name": ...} или {"title": ...}."""
    if isinstance(value, dict):
        return str(value.get("name") or value.get("title") or "").strip().lower()
    return str(value or "").strip().lower()


def _item_value(value: Any) -> float:
    if isinstance(value, dict):
        return max(_num(value.get("value", value.get("count"))), 0.0)
    return 0.0


def infrastructure_capacity(country: dict[str, Any]) -> float:
    """Сколько граждан «вмещают» постройки страны сверх базового потолка.

    Города (населённые пункты) и промышленность (фабрики/заводы/порты) дают
    рабочие места и жильё: вместимость растёт с построенной инфраструктурой.
    """
    total = 0.0
    for item in _items(country.get("settlements_json")):
        name = _item_name(item)
        if not name:
            continue
        if any(kw in name for kw in ("город", "столиц", "мегаполис", "град")):
            total += INFRA_CAPACITY_PER_CITY
        elif any(kw in name for kw in ("дерев", "село", "сель", "посёл", "посел", "хутор", "пгт", "стан")):
            total += INFRA_CAPACITY_PER_VILLAGE
        else:
            total += INFRA_CAPACITY_PER_SETTLEMENT
    for item in _items(country.get("industry_json")):
        if not isinstance(item, dict):
            continue
        kind = str(item.get("type") or "").strip().lower()
        total += INFRA_CAPACITY_BY_INDUSTRY.get(kind, 0.0) * _item_value(item)
    return total


def _ensure_baseline_tech(country: dict[str, Any]) -> dict[str, Any]:
    """Дать стране минимальную базовую технику, если её совсем нет.

    Раздел 4: у каждой страны есть минимально базовая техника.
    Добавляем только если все поля техники = 0.
    """
    tech_keys = ("army_light_vehicles", "army_heavy_wheeled",
                 "army_artillery", "army_drones", "army_navy")
    has_any = any(_int(country.get(k)) > 0 for k in tech_keys)
    if has_any:
        return country

    pop = max(_int(country.get("population")), 0)
    if pop == 0:
        return country

    # Минимальная техника: масштабируется от населения, но не менее единицы
    out = dict(country)
    out["army_light_vehicles"] = max(1, pop // 500)
    out["army_heavy_wheeled"] = max(1, pop // 2000)
    out["army_artillery"] = max(1, pop // 1000)
    out["army_drones"] = max(1, pop // 1000)
    out["army_navy"] = max(1, pop // 5000)
    return out


def _utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def parse_event_date(value: Any) -> datetime | None:
    """ISO-дата события в aware UTC; нестроковое/битое значение → None."""
    if not isinstance(value, str):
        return None
    try:
        return _utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except (ValueError, TypeError):
        return None


# События, помеченные датой «чуть в будущее» (регистрация записывается
# за секунду до расчёта цикла), не должны обнулять казну: считаем их
# действующими с момента записи. Реально будущие (лори-даты из новостей
# не могут быть в будущем сильнее этого окна) — пропускаем.
_FUTURE_TOLERANCE = timedelta(hours=6)


def ledger_budget(
    start_budget: Any, events: Any, now: datetime | None = None
) -> float:
    """Бюджет страны по хронике событий (а не по текущему снимку).

    Бюджет = стартовый капитал +
            Σ (ставка ВР/день × число дней с даты события) +
            Σ разовых абсолютных изменений (траты/награды, уже со знаком).

    Поэтому каждая датированная новость «логически прибавляет/убирает»
    казну ровно за то время, что она действует.
    """
    now = _utc(now or datetime.now(timezone.utc))
    budget = _num(start_budget)
    for e in _items(events):
        event_at = parse_event_date(e.get("event_date"))
        if event_at is None or event_at > now + _FUTURE_TOLERANCE:
            continue
        days = (now - event_at).total_seconds() / 86400.0
        amount = _num(e.get("amount"))
        if e.get("rate"):
            budget += amount * days
        else:
            budget += amount
    return budget


def army_expenses(country: dict[str, Any]) -> float:
    total = 0.0
    for key, upkeep in ARMY_UPKEEP.items():
        total += _num(country.get(key)) * upkeep
    return total


# ---------------------------------------------------------------------------
# БОЕВОЙ ПОТЕНЦИАЛ (формулы, без AI)
#
# Армия не выставляется вручную: бот держит её равной тому, что страна
# реально может содержать. Считается сверху вниз:
#
#   1. demobilisable   = население * MOBILISATION_CEILING
#        доля населения, которая вообще может попасть в армию
#   2. readiness        = MOBILISATION_FLOOR
#                        + (1 - MOBILISATION_FLOOR)
#                        * (stability + war_support)/2 / 100
#        «хочешь воевать и можешь»: стабильность и военная поддержка
#   3. industry_cap     = суммарный вес предприятий отрасли
#        заводы/фабрики держат технику: без производства потолок ниже
#   4. target           = min(demobilisable * readiness, industry_cap)
#   5. дневная наборка  = min(target - army, RECRUIT_PER_DAY_LIMIT)
#        прирост ограничен, иначе после мобилизации за ночь появляется
#        сто тысяч солдат
#
# ТЕХНИКА (не пехота) жёстко ограничена тем же потолком target: если сумма
# всей армии превышает его, техника пропорционально списывается, чтобы
# содержание армии никогда не превышало возможности страны. Рост техники из
# новостей, превышающий потолок, при следующем срезе урезается до target.
# ---------------------------------------------------------------------------

# Доля населения, максимально годная к мобилизации (пассивная «строевая» армия).
# Это потолок ОБЫЧНОГО мира: страна содержит столько без мобилизационных постов.
MOBILISATION_CEILING = 0.25
# Жёсткий предел мобилизации — как в mobilization.py (config.ARMY_POPULATION_RATIO):
# заявленная/постовая армия может доходить до 100% населения, но не выше.
# Держим константу здесь, чтобы economy.py оставался автономным (без config).
MAX_MOBILISATION_RATIO = 1.0
# Минимальная готовность (даже при обвале стабильности кадровый резерв есть)
MOBILISATION_FLOOR = 0.4
# Военный завод «держит» столько единиц армии, сколько стоит техники
INDUSTRY_ARMYSUPPORT = {
    "military_factory": 1500,
    "factory": 300,
    "civil_factory": 300,
    "port": 200,
    "shipyard": 200,
}
# Потолок набора пехоты в сутки на страну
RECRUIT_PER_DAY_LIMIT = 400.0
# Доля дневного набора от целевой армии (плавный рост)
RECRUIT_RATE_PER_DAY = 0.05
# Стоимость набора одного пехотинца (обучение, форма, оружие)
RECRUIT_COST = 25.0
# Доля численности группировки, которую она может набрать за сутки
# из профицита кассы (минимум 1 боец) — рост плавный, как у стран.
FACTION_RECRUIT_RATE = 0.02


def industry_capacity(country: dict[str, Any]) -> float:
    """Сколько армии вообще может удержать производство страны."""
    total = 0.0
    for item in _items(country.get("industry_json")):
        if not isinstance(item, dict):
            continue
        value = max(_num(item.get("value")), 0.0)
        kind = str(item.get("type") or "").strip().lower()
        total += INDUSTRY_ARMYSUPPORT.get(kind, 0.0) * value
    return total


def declared_army_total(declared: Any) -> float | None:
    """Сумма заявленной (постовой) армии из payload раздела 5/анкеты.

    None — если заявленных army_-полей нет вовсе (тогда армия считается только
    формулой). 0.0 — если заявлена пустая армия (её и держим).
    """
    if not isinstance(declared, dict):
        return None
    keys = tuple(ARMY_UPKEEP)
    if not any(k in declared for k in keys):
        return None
    return sum(max(_num(declared.get(k)), 0.0) for k in keys)


def compute_army(
    country: dict[str, Any],
    days: float,
    *,
    industry_cap: float | None = None,
    spare_budget: float | None = None,
    declared: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Пересчитать армию страны по формулам выше.

    days — сколько суток прошло с прошлого среза.
    declared — заявленные (постовые/раздел 5) army_-поля. Это НЕ «цель, к
    которой надо медленно ползти», а реальная (зарегистрированная) численность:
    солдаты и техника уже существуют, поэтому карточка сразу показывает
    заявленные числа, а не подтягивает их пассивной формулой годами. Пехота
    ограничена пределом мобилизации (не более 100% населения); техника — нет,
    она ограничена бюджетом/промышленностью, а не числом граждан.

    Без заявки (declared is None) работает пассивная формула боевого
    потенциала: армия держится равной тому, что страна способна содержать.
    Возвращает army_* плюс справочные target/recruited.
    """
    pop = max(_int(country.get("population")), 0)
    stability = clamp(_num(country.get("stability", 70)), 0, 100)
    war_support = clamp(_num(country.get("war_support", 50)), 0, 100)
    happiness = clamp(_num(country.get("happiness", 50)), 0, 100)

    demobilisable = pop * MOBILISATION_CEILING
    readiness = MOBILISATION_FLOOR + (1.0 - MOBILISATION_FLOOR) * (
        (stability + war_support) / 2.0 / 100.0
    )
    # счастье чуть приглушает готовность: недовольные не идут в строй
    readiness *= 0.85 + 0.15 * (happiness / 100.0)

    by_population = demobilisable * readiness
    cap = industry_capacity(country) if industry_cap is None else industry_cap
    target = by_population if cap <= 0 else min(by_population, cap)

    # --- заявленная (постовая/раздел 5) армия — источник истины ---
    declared_map: dict[str, float] = {}
    if isinstance(declared, dict):
        for key in ARMY_UPKEEP:
            if key in declared:
                declared_map[key] = max(_num(declared.get(key)), 0.0)

    declared_infantry = declared_map.get("army_infantry")
    if declared_infantry is not None:
        # Зарегистрированные солдаты уже существуют: карточка показывает их
        # сразу, а не «доходит» формулой (раздел 5 фиксирует фактическую
        # численность). Население может быть ещё не введено (0) или ошибочно
        # ниже армии — урезать заявленную силу до населения мы НЕ должны,
        # иначе армия «сожмётся» относительно регистрации. Потолок мобилизации
        # применяется к известному населению (max(pop, армия) никогда не
        # занижает заявку), т.е. армия >= регистрации всегда.
        known_pop = max(pop, declared_infantry)
        target = min(declared_infantry, known_pop * MAX_MOBILISATION_RATIO)
        old_infantry = max(_num(country.get("army_infantry")), 0.0)
        tech_keys = tuple(k for k in ARMY_UPKEEP if k != "army_infantry")
        out: dict[str, Any] = {}
        for key in tech_keys:
            if key in declared_map:
                out[key] = int(round(declared_map[key]))
            else:
                out[key] = int(round(max(_num(country.get(key)), 0.0)))
        out["army_infantry"] = int(round(target))
        out["army_target"] = int(round(target))
        out["army_readiness"] = round(readiness, 3)
        out["recruited"] = int(round(target)) - int(round(old_infantry))
        return out

    army = sum(max(_num(country.get(key)), 0.0) for key in ARMY_UPKEEP)
    wanted = target - army
    step = min(
        abs(wanted),
        max(target * RECRUIT_RATE_PER_DAY * days, 1.0),
        RECRUIT_PER_DAY_LIMIT * days,
    )
    recruited = min(wanted, step) if wanted > 0 else -min(abs(wanted), step)

    # набор не может стоить больше, чем есть свободных денег
    if recruited > 0 and spare_budget is not None:
        affordable = max(spare_budget, 0.0) / RECRUIT_COST
        recruited = min(recruited, affordable)

    # армия не может развалиться в ноль: охрана границ
    floor = min(demobilisable * 0.05, RECRUIT_PER_DAY_LIMIT * days)
    infantry = max(_num(country.get("army_infantry")), 0.0)
    new_infantry = clamp(infantry + recruited, 0.0, max(target, floor))

    # --- техника не может превышать мобилизационный потолок страны ---
    # Армия держится равной тому, что страна способна содержать: при
    # превышении техника жестко списывается до target (население × готовность,
    # ограниченное промышленностью). Легитимные малые армии не трогаются.
    tech_keys = tuple(k for k in ARMY_UPKEEP if k != "army_infantry")
    tech_vals = {k: max(_num(country.get(k)), 0.0) for k in tech_keys}
    tech_total = sum(tech_vals.values())
    total_after = new_infantry + tech_total
    if total_after > target:
        if target <= new_infantry:
            new_infantry = max(int(round(target)), 0)
            tech_vals = {k: 0.0 for k in tech_keys}
        else:
            tech_cap = target - new_infantry
            if tech_total > 0:
                ratio = tech_cap / tech_total
                scaled = {k: round(v * ratio) for k, v in tech_vals.items()}
                tech_vals = {k: float(v) for k, v in scaled.items()}
                # остаток от округления компенсируем пехотой (технику не добавляем)
                remainder = tech_cap - sum(tech_vals.values())
                new_infantry = int(round(new_infantry + remainder))
            else:
                tech_vals = {k: 0.0 for k in tech_keys}

    return {
        **{k: int(round(v)) for k, v in tech_vals.items()},
        "army_infantry": int(round(new_infantry)),
        "army_target": int(round(target)),
        "army_readiness": round(readiness, 3),
        "recruited": int(round(new_infantry - infantry)),
    }



def compute_country_economy(
    country: dict[str, Any],
    hours: float,
    events: Any = None,
    now: datetime | None = None,
    one_off_from: datetime | None = None,
    declared: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Рассчитать новый экономический срез страны за `hours` часов.

    Казна (budget) накапливается от ТЕКУЩЕГО чистого потока: budget +=
    (доход − расход) × дни + разовые операции хроники, датированные после
    `one_off_from`. Так казна на карточке всегда согласована с показываемыми
    доходами/расходами. Отдельные постройки/траты/награды из новостей приходят
    как события rate=False и учитываются разовой суммой.

    Возвращает dict с обновлёнными полями и ключом `deltas`.
    НЕ сохраняет в БД, НЕ трогает prev-поля (их хранит scheduler).
    """
    days = max(hours, 0.0) / 24.0

    pop = _int(country.get("population"))
    cap = _int(country.get("population_capacity"))
    base_cap = max(pop * CAPACITY_MULTIPLE, MIN_CAPACITY)
    # постройки расширяют вместимость сверх базового потолка
    ceiling = int(base_cap + max(infrastructure_capacity(country), 0.0))
    if cap <= 0:
        cap = ceiling
    else:
        # обрезаем абсурдную вместимость: больше, чем реально дают население
        # и построенная инфраструктура, страна физически не примет (и не должна
        # расти на +500 в день)
        cap = int(clamp(cap, max(pop, 1), ceiling))
    tax_level = country.get("tax_level", "Средний")

    # --- доходы (механика 1.1) — ставки для карточки ---
    tax_income = pop * _tax_rate(tax_level)
    industry_income_ = industry_income(country.get("industry_json", []))
    income_daily = BASE_INCOME_DAILY + tax_income + industry_income_

    # --- расходы (механика 1.1) ---
    expenses_daily = army_expenses(country) + pop * 2.0

    # --- казна ---
    # Казна накапливается от ТЕКУЩЕГО чистого потока (чтобы цифры на карточке
    # всегда сходились: budget_delta == (income - expenses) * days + разовые
    # операции). Разовые операции из хроники (rate=False), произошедшие после
    # прошлого среза (one_off_from), добавляются отдельными суммами.
    old_budget = _num(country.get("budget"))
    events_now = now or datetime.now(timezone.utc)
    net_flow = income_daily - expenses_daily
    one_off = 0.0
    if one_off_from is not None:
        for e in _items(events):
            if e.get("rate") or str(e.get("kind") or "").strip().lower() == "registration":
                continue
            event_at = parse_event_date(e.get("event_date"))
            if event_at is not None and one_off_from < event_at <= events_now:
                one_off += _num(e.get("amount"))
    budget = old_budget + net_flow * days + one_off

    # --- население: медленный рост к вместимости, с жёстким потолком ---
    stability_now = clamp(_num(country.get("stability", 70)), 0, 100)
    raw_growth = (cap - pop) * POPULATION_FILL_PER_DAY * days
    # убыль при низкой стабильности и чрезмерных налогах
    drain = 0.0
    if stability_now < 40:
        drain = pop * DEPOPULATION_PER_DAY * (40 - stability_now) / 40 * days
    net_growth = raw_growth - drain
    growth_cap = min(
        pop * MAX_GROWTH_PER_DAY * days,
        MAX_ABS_GROWTH_PER_DAY * days,
    )
    pop_growth = int(round(clamp(net_growth, -growth_cap, growth_cap)))
    new_pop = _int(clamp(pop + pop_growth, 0, cap))

    # Заявленное (раздел 5) население — нижняя граница: ребаланс/дрейф не
    # должны «съедать» зарегистрированных граждан. Карточка не показывает
    # население меньше зарегистрированного (пока в самой заявке оно есть).
    if isinstance(declared, dict):
        declared_pop = _int(declared.get("population"))
        if declared_pop > 0:
            cap = max(cap, declared_pop)
            new_pop = max(new_pop, min(declared_pop, cap))

    # --- стабильность / счастье / военная поддержка ---
    stability = _num(country.get("stability", 70))
    war_support = _num(country.get("war_support", 50))
    happiness = _num(country.get("happiness", 50))

    weekly_tax_penalty = STABILITY_TAX_PENALTY.get(
        str(tax_level).strip().lower(), 0.0
    )
    stability += -weekly_tax_penalty / 7.0 * days

    if net_flow >= 0:
        stability += +2.0 * days           # профицит = здоровая казна
    else:
        stability += -3.0 * days           # дефицит давит
    if budget <= 0 and days > 0:
        stability += -15.0 * days          # в минусе — плохо (банкротство)

    happiness_drift = {"чрезмерный": -4.0, "повышенный": -1.5}.get(
        str(tax_level).strip().lower(), +0.5
    )
    happiness += happiness_drift * days
    war_support += (50.0 - war_support) * 0.05 * days  # медленно к нейтралу

    # --- армия: набор/демобилизация по формулам боевого потенциала ---
    # Базовая техника: если у страны нет ни единой единицы техники, даём
    # минимальный комплект на основе населения (раздел 4: у каждой страны есть
    # базовая техника). Это чинит «у каждой страны есть минимально базовая техника».
    country_for_army = dict(country)
    country_for_army["population"] = new_pop
    country_for_army["stability"] = stability
    country_for_army["war_support"] = war_support
    country_for_army["happiness"] = happiness
    country_for_army = _ensure_baseline_tech(country_for_army)
    army_state = compute_army(
        country_for_army,
        days,
        spare_budget=max(budget, 0.0),
        declared=declared,
    )

    # после урезания техники расходы и казна считаются по актуальной армии,
    # чтобы карточка всегда показывала согласованные цифры
    expenses_daily = army_expenses({**country, **army_state}) + new_pop * 2.0
    budget = old_budget + (income_daily - expenses_daily) * days + one_off

    return {
        "income_daily": round(income_daily, 1),
        "expenses_daily": round(expenses_daily, 1),
        "budget": round(budget, 1),
        "population": new_pop,
        "population_capacity": cap,
        "stability": round(clamp(stability, 0, 100), 1),
        "war_support": round(clamp(war_support, 0, 100), 1),
        "happiness": round(clamp(happiness, 0, 100), 1),
        **army_state,
        "deltas": {
            "population": new_pop - pop,
            "budget": round(budget - old_budget, 1),
            "income_daily": round(income_daily - _num(country.get("income_daily")), 1),
            "expenses_daily": round(expenses_daily - _num(country.get("expenses_daily")), 1),
            "army_infantry": army_state.get("recruited", 0),
        },
    }


def compute_faction_economy(faction: dict[str, Any], hours: float) -> dict[str, Any]:
    """Срез группировки: получает базовый доход, тратит на содержание.

    Набор войск: профицит кассы конвертируется в новых бойцов (новичок
    стоит RECRUIT_COST), поэтому «наборали войска» в постах отражается в
    численности, а группировки с численностью 0 начинают сами набирать.
    Рост за цикл ограничен долей текущей силы (FACTION_RECRUIT_RATE),
    чтобы цифры росли плавно, а не прыгали.
    """
    days = max(hours, 0.0) / 24.0

    old_budget = _num(faction.get("budget"))
    strength = _int(faction.get("strength") or faction.get("army_infantry"))
    income = BASE_INCOME_DAILY
    expenses = strength * ARMY_UPKEEP["army_infantry"]
    net_daily = income - expenses
    # защита от мгновенного слива казны в минус одним дефицитом
    net_flow = max(net_daily, -old_budget * 0.1)

    recruit_cap = max(int(round(strength * FACTION_RECRUIT_RATE)), 1) * days
    affordable = max(net_flow, 0.0) * days / RECRUIT_COST
    recruits = int(min(recruit_cap, affordable))
    new_strength = strength + recruits
    new_budget = old_budget + net_flow * days - recruits * RECRUIT_COST

    reputation = _num(faction.get("reputation", 50))
    new_reputation = reputation + (50.0 - reputation) * 0.05 * days

    return {
        "budget": round(new_budget, 1),
        "strength": new_strength,
        "reputation": round(clamp(new_reputation, 0, 100), 1),
        "deltas": {
            "budget": round(new_budget - old_budget, 1),
            "strength": recruits,
            "reputation": round(new_reputation - reputation, 1),
        },
    }