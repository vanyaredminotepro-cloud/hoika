"""
Validator: strict checks to prevent nonsense in country/faction cards.
All checks are hard rules - card is rejected if any fails.
"""
from __future__ import annotations
import logging
from dataclasses import dataclass
from typing import Any

from .config import (
    MIN_POPULATION, MAX_POPULATION_DROP_PCT,
    MIN_ARMY_START, MAX_ARMY_START, ARMY_POPULATION_RATIO,
    FROZEN_STATUSES, OJET_COUNTRY,
)
from .cards import normalize_tax_level

log = logging.getLogger(__name__)

@dataclass
class ValidationResult:
    valid: bool
    errors: list[str]
    warnings: list[str]

    def add_error(self, msg: str) -> None:
        self.errors.append(msg)
        self.valid = False

    def add_warning(self, msg: str) -> None:
        self.warnings.append(msg)

def _to_int(v: Any) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0

def _to_float(v: Any) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0

class CountryValidator:
    """Validates country card after LLM event application."""

    def __init__(self, old: dict[str, Any], new: dict[str, Any], events: list[dict[str, Any]]):
        self.old = old
        self.new = new
        self.events = events
        self.result = ValidationResult(valid=True, errors=[], warnings=[])

    def validate(self) -> ValidationResult:
        self._check_population()
        self._check_army()
        self._check_budget()
        self._check_buildings()
        self._check_unreg()
        self._check_mobilization()
        self._check_army_cap()
        self._check_frozen_growth()
        self._check_ojat()
        self._check_stats_source()
        self._check_stability_war_happiness()
        self._check_tax()
        self._check_general_consistency()
        return self.result

    def _check_population(self) -> None:
        pop = _to_int(self.new.get("population"))
        cap = _to_int(self.new.get("population_capacity"))
        old_pop = _to_int(self.old.get("population"))

        if pop < MIN_POPULATION:
            if self.new.get("status") == "active":
                self.result.add_error(f"Население {pop} < минимума {MIN_POPULATION} для активной страны")
            elif pop == 1:
                self.result.add_error("Население = 1 — баг, требуется восстановление")

        if old_pop > 0 and pop > 0:
            drop_pct = (old_pop - pop) / old_pop * 100
            if drop_pct > MAX_POPULATION_DROP_PCT:
                # Check if justified by events
                justified = any(e.get("type") in ("population", "unreg") for e in self.events)
                if not justified:
                    self.result.add_error(
                        f"Резкое падение населения {drop_pct:.0f}% без оснований в новостях"
                    )

        if pop > cap and cap > 0:
            self.result.add_warning(f"Население {pop} превышает вместимость {cap}")

    def _check_army(self) -> None:
        """Базовая проверка армии. Лимит 30% и мобилизация - в отдельных методах.

        Раньше здесь стоял лимит «вся армия <= 30% населения», где в сумму
        входили техника, дроны и флот. Это неверно: 30% от населения -
        ограничение на ЛЮДЕЙ (раздел 11, «25-50 на старте», «максимум 30%
        населения»), а 200 дронов на страну с 200 жителями - нормальная
        реальность проекта. Сейчас лимит считает только пехоту.
        """
        inf = _to_int(self.new.get("army_infantry"))
        total_army = sum(_to_int(self.new.get(f)) or 0 for f in (
            "army_infantry", "army_light_vehicles", "army_heavy_wheeled",
            "army_artillery", "army_drones", "army_navy"))

        if self.new.get("status") == "active":
            if total_army == 0:
                has_army_event = any(e.get("type") == "army" for e in self.events)
                if not has_army_event:
                    self.result.add_error(
                        "Армия = 0 у активной страны без поста о мобилизации")
            elif inf < MIN_ARMY_START:
                self.result.add_warning(
                    f"Пехота {inf} < рекомендуемых {MIN_ARMY_START}")

        for field in ("army_infantry", "army_light_vehicles",
                      "army_heavy_wheeled", "army_artillery",
                      "army_drones", "army_navy"):
            if (_to_int(self.new.get(field)) or 0) < 0:
                self.result.add_error(f"{field} отрицательное")

    def _check_budget(self) -> None:
        budget = _to_int(self.new.get("budget"))
        old_budget = _to_int(self.old.get("budget"))
        income = _to_int(self.new.get("income_daily"))
        expenses = _to_int(self.new.get("expenses_daily"))

        if budget < 0:
            self.result.add_warning(f"Отрицательный бюджет: {budget}")

        # Check growth without source
        if budget > old_budget:
            growth = budget - old_budget
            if growth > income * 3:  # More than 3 days income in one cycle
                justified = any(e.get("type") == "budget" and e.get("params", {}).get("delta", 0) > 0 for e in self.events)
                if not justified:
                    self.result.add_error(f"Рост казны {_delta_str(old_budget, budget)} без источника в новостях")

    def _check_buildings(self) -> None:
        """Здания (раздел 11): список из БД, без поста о стройке не добавлять.

        Появление новых построек без слов «стройка/построил/завод» в новостях -
        откат. Раньше эта проверка была заглушкой с неработающим импортом
        `from cards import ...`, который ронял валидатор целиком.
        """
        industry = self.new.get("industry_json") or []
        if not isinstance(industry, list):
            self.result.add_error("industry_json не является списком")
            return

        def _counts(raw: Any) -> dict[str, int]:
            out: dict[str, int] = {}
            for item in raw or []:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name") or item.get("type") or "").strip()
                if not name:
                    continue
                out[name] = out.get(name, 0) + (_to_int(
                    item.get("count", item.get("value"))) or 0)
            return out

        old_counts = _counts(self.old.get("industry_json"))
        new_counts = _counts(industry)
        grew = {name: new_counts[name] - old_counts.get(name, 0)
                for name in new_counts
                if new_counts[name] > old_counts.get(name, 0)}
        if not grew:
            return

        blob = " ".join(
            str(e.get("summary") or e.get("text") or "") for e in self.events
        ).lower()
        build_words = ("стро", "постро", "завод", "строительств", "открыт",
                       "введён", "введен", "сдач", "реконструкц", "цех",
                       "фабрик", "больниц", "школ", "университет", "шахт",
                       "верф", "площадк")
        if not any(word in blob for word in build_words):
            body = ", ".join(f"{n} +{c}" for n, c in sorted(grew.items()))
            self.result.add_error(
                f"Постройки появились без поста о стройке: {body}")

        # Мгновенное появление: за один цикл нельзя построить больше, чем
        # позволяет казна - грубая, но честная оценка.
        budget = _to_float(self.new.get("budget")) or 0.0
        cheapest = 5000.0
        total_new = sum(grew.values())
        if budget < cheapest * total_new:
            self.result.add_warning(
                f"Добавлено {total_new} построек при бюджете {budget:.0f} - "
                "возможно мгновенное появление")

    def _check_unreg(self) -> None:
        # Анрег (раздел 11): бюджет = 0, показатели обнулены.
        # Проверяем ВСЕ виды войск: раньше проверялась только пехота, и
        # анрегнутая страна с 200 БПЛА или флотом проходила валидацию.
        if self.new.get("status") in ("anreg", "unreg"):
            pop = _to_int(self.new.get("population"))
            bud = _to_float(self.new.get("budget"))
            army = {f: _to_int(self.new.get(f)) for f in (
                "army_infantry", "army_light_vehicles", "army_heavy_wheeled",
                "army_artillery", "army_drones", "army_navy")}
            dirty = {k: v for k, v in army.items() if v}
            if pop != 0 or abs(bud) > 0.01 or dirty:
                self.result.add_error(
                    "Анрегнутая страна должна иметь нулевые показатели "
                    f"(нас={pop}, бюджет={bud}, армия={dirty or 0})")

    def _check_mobilization(self) -> None:
        """Рост армии - только по посту о мобилизации (раздел 11).

        Проверяем ВСЕ виды войск, а не только пехоту: LLM раньше набирал, скажем,
        200 БПЛА, и это проходило, потому что infantry не менялась.
        """
        mob_event = any(
            e.get("type") == "army" and any(
                int(e.get("params", {}).get(key, 0) or 0) > 0
                for key in ("infantry", "light_vehicles", "heavy_wheeled",
                            "artillery", "drones", "navy"))
            for e in self.events
        )
        for label, field in (
            ("пехоты", "army_infantry"),
            ("лёгкой техники", "army_light_vehicles"),
            ("тяжёлой колёсной", "army_heavy_wheeled"),
            ("артиллерии", "army_artillery"),
            ("БПЛА", "army_drones"),
            ("флота", "army_navy"),
        ):
            old_val = _to_int(self.old.get(field))
            new_val = _to_int(self.new.get(field))
            if new_val > old_val and not mob_event:
                self.result.add_error(
                    f"Рост {label} без события мобилизации ({old_val} -> {new_val})")
            if new_val < 0:
                self.result.add_error(f"Отрицательное число {label}: {new_val}")
            if new_val == 0 and old_val > 0 and not self._has_loss_event():
                self.result.add_error(
                    f"Армия {label} обнулена без события потерь ({old_val} -> 0)")

    def _has_loss_event(self) -> bool:
        """Было ли в новостях осмысленное падение армии (потери, демобилизация)."""
        loss_words = ("потер", "погиб", "выбыл", "демобилиз", "дезерт",
                      "уничтож", "разгром", "ликвидир")
        for event in self.events:
            if event.get("type") != "army":
                continue
            for key in ("losses", "lost", "casualties"):
                if int(event.get("params", {}).get(key, 0) or 0) > 0:
                    return True
        blob = " ".join(
            str(e.get("summary") or e.get("text") or "").lower() for e in self.events
        )
        return any(word in blob for word in loss_words)

    def _check_army_cap(self) -> None:
        """Максимум 30% населения (раздел 11).

        Считаем ТОЛЬКО пехоту: это люди. Техника, дроны и флот - не люди, и
        суммировать их с населением нельзя, иначе любая страна с техникой
        улетает за лимит.
        """
        pop = _to_int(self.new.get("population"))
        inf = _to_int(self.new.get("army_infantry"))
        if pop > 0 and inf > pop * ARMY_POPULATION_RATIO:
            self.result.add_error(
                f"Пехота {inf} превышает {ARMY_POPULATION_RATIO:.0%} населения {pop}")

    def _check_frozen_growth(self) -> None:
        """Замороженная страна не растёт (раздел 8, раздел 13)."""
        status = str(self.new.get("status") or "active")
        if status not in FROZEN_STATUSES:
            return
        for field in ("population", "budget", "army_infantry",
                      "army_light_vehicles", "army_heavy_wheeled",
                      "army_artillery", "army_drones", "army_navy",
                      "income_daily"):
            old_val = _to_float(self.old.get(field))
            new_val = _to_float(self.new.get(field))
            if new_val > old_val + 0.01:
                self.result.add_error(
                    f"Рост {field} у замороженной страны ({status}): "
                    f"{old_val} -> {new_val}")

    def _check_ojat(self) -> None:
        """ОЯТ и ТХ-999 меняет только Паониция и только по упоминанию в новостях.

        Разделы 2, 4 и 13: ни одна другая страна не может получить ОЯТ, а Паониция
        - только если в посте есть явное «ОЯТ»/«ТХ-999».
        """
        new_ojat = _to_int(self.new.get("ojat")) or 0
        old_ojat = _to_int(self.old.get("ojat")) or 0
        new_tx = _to_int(self.new.get("tx999")) or 0
        old_tx = _to_int(self.old.get("tx999")) or 0
        if new_ojat == old_ojat and new_tx == old_tx:
            return

        name = str(self.new.get("name") or self.old.get("name") or "")
        if name != OJET_COUNTRY:
            self.result.add_error(
                f"ОЯТ/ТХ-999 может менять только {OJET_COUNTRY}, а не {name!r}")
            return

        blob = " ".join(
            str(e.get("summary") or e.get("text") or "") for e in self.events
        ).lower()
        if not ("оят" in blob or "тх-999" in blob or "тх999" in blob):
            self.result.add_error(
                "Изменение ОЯТ/ТХ-999 без упоминания в новостях")
        if new_tx > 0:
            # В разделе 5 ТХ-999 у Паониции засекречено. Ненулевое значение в
            # карточке означало бы, что бот раскрыл то, что скрыто от игроков.
            self.result.add_error("ТХ-999 засекречено и не может показываться в карточке")

    def _check_stats_source(self) -> None:
        """Для stats_source=manual цифры меняются только по явному посту.

        Раздел 4: LLM не имеет права менять население, армию, бюджет и вооружение
        без явного поста (мобилизация, потери, доход, стройка).
        Исключение: settlements/industry/research/resources — разрешены при
        наличии слов о стройке/построении/заводе.
        """
        if str(self.new.get("stats_source") or "") != "manual":
            return
        blob = " ".join(
            str(e.get("summary") or e.get("text") or "") for e in self.events
        ).lower()
        allowed = ("мобилизац", "набор", "призыв", "рекрутинг", "военный набор",
                   "потер", "погиб", "доход", "бюджет", "казна", "стройк",
                   "постро", "завод", "строительств", "заселение", "прирост",
                   "населен", "пересел", "производств", "налог", "расход",
                   "техник", "оруж", "флот", "дрон", "артиллер")
        build_allowed = any(word in blob for word in ("стро", "постро", "завод",
                            "строительств", "открыт", "введен", "введён", "сдач",
                            "реконструкц", "цех", "фабрик", "больниц", "школ",
                            "университет", "шахт", "верф", "площадк", "посел",
                            "город", "деревн", "посёл", "село", "хутор"))
        if any(word in blob for word in allowed):
            return
        # Для manual-стран: изменения population/budget/army без явного поста — ошибка.
        # Но settlements/industry/research/resources разрешаем, если есть слова о стройке.
        for field in ("population", "budget", "army_infantry",
                      "army_light_vehicles", "army_heavy_wheeled",
                      "army_artillery", "army_drones", "army_navy"):
            old_val = _to_float(self.old.get(field))
            new_val = _to_float(self.new.get(field))
            if abs(new_val - old_val) > 0.01:
                self.result.add_error(
                    f"stats_source=manual: изменение {field} без явного поста "
                    f"в новостях ({old_val} -> {new_val})")
        # settlements/industry/research/resources — разрешены только при build_allowed
        if build_allowed:
            return
        for field in ("settlements_json", "industry_json", "research_json", "resources_json"):
            old_val = self.old.get(field)
            new_val = self.new.get(field)
            if old_val != new_val:
                self.result.add_error(
                    f"stats_source=manual: изменение {field} без поста о стройке")

    def _check_stability_war_happiness(self) -> None:
        for field in ("stability", "war_support", "happiness"):
            val = _to_int(self.new.get(field))
            if val < 0 or val > 100:
                self.result.add_error(f"{field} вне диапазона 0-100: {val}")

    def _check_tax(self) -> None:
        """Уровень налога:english или русский (раздел 10, 11).

        Список уровней берём из cards.normalize_tax_level - раньше здесь был
        свой список только из четырёх английских слов, и русское «средний»,
        которое apply_event принимает, валидатор считал ошибкой.
        """
        tax = self.new.get("tax_level")
        if not tax:
            return
        if normalize_tax_level(tax) is None:
            self.result.add_error(f"Неверный налоговый уровень: {tax}")

    def _check_general_consistency(self) -> None:
        # Population capacity consistency
        pop = _to_int(self.new.get("population"))
        cap = _to_int(self.new.get("population_capacity"))
        if pop > 0 and cap == 0:
            self.result.add_warning("Есть население, но вместимость = 0")

        # Income/expenses consistency
        income = _to_int(self.new.get("income_daily"))
        expenses = _to_int(self.new.get("expenses_daily"))
        if income < 1000:
            self.result.add_warning(f"Подозрительно низкий доход: {income}")
        if expenses > income * 3:
            self.result.add_warning(f"Расходы {expenses} в 3 раза больше дохода {income}")

def _delta_str(old: int, new: int) -> str:
    d = new - old
    if d > 0:
        return f"+{d:,}".replace(",", " ")
    return f"{d:,}".replace(",", " ")

class FactionValidator:
    """Validates faction card."""

    def __init__(self, old: dict[str, Any], new: dict[str, Any]):
        self.old = old
        self.new = new
        self.result = ValidationResult(valid=True, errors=[], warnings=[])

    def validate(self) -> ValidationResult:
        strength = _to_int(self.new.get("strength"))
        budget = _to_int(self.new.get("budget"))

        if strength < 0:
            self.result.add_error(f"Отрицательная численность: {strength}")
        if strength == 0:
            self.result.add_warning("Численность группировки = 0")

        if budget < 0:
            self.result.add_error(f"Отрицательный бюджет: {budget}")

        old_str = _to_int(self.old.get("strength"))
        if strength > old_str:
            # Check if justified by recruitment event
            pass

        return self.result


def validate_country_card(old: dict[str, Any], new: dict[str, Any], events: list[dict[str, Any]]) -> ValidationResult:
    return CountryValidator(old, new, events).validate()

def validate_faction_card(old: dict[str, Any], new: dict[str, Any]) -> ValidationResult:
    return FactionValidator(old, new).validate()