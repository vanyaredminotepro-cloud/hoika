"""
Validator: strict checks to prevent nonsense in country/faction cards.
All checks are hard rules - card is rejected if any fails.
"""
from __future__ import annotations
import logging
from dataclasses import dataclass
from typing import Any

from config import (
    MIN_POPULATION, MAX_POPULATION_DROP_PCT,
    MIN_ARMY_START, MAX_ARMY_START, ARMY_POPULATION_RATIO
)

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
        inf = _to_int(self.new.get("army_infantry"))
        light = _to_int(self.new.get("army_light_vehicles"))
        heavy = _to_int(self.new.get("army_heavy_wheeled"))
        art = _to_int(self.new.get("army_artillery"))
        drones = _to_int(self.new.get("army_drones"))
        navy = _to_int(self.new.get("army_navy"))

        total_army = inf + light + heavy + art + drones + navy
        pop = _to_int(self.new.get("population"))

        if self.new.get("status") == "active":
            if total_army == 0:
                # Check if justified
                has_mobilization = any(e.get("type") == "army" for e in self.events)
                if not has_mobilization:
                    self.result.add_error("Армия = 0 у активной страны без поста о мобилизации/дислокации")
            elif total_army < MIN_ARMY_START:
                self.result.add_warning(f"Армия {total_army} < рекомендуемого минимума {MIN_ARMY_START}")

        if total_army > pop * ARMY_POPULATION_RATIO:
            self.result.add_error(
                f"Армия {total_army} превышает {ARMY_POPULATION_RATIO*100:.0f}% населения ({pop})"
            )

        for field, val in [("army_infantry", inf), ("army_light_vehicles", light),
                           ("army_heavy_wheeled", heavy), ("army_artillery", art),
                           ("army_drones", drones), ("army_navy", navy)]:
            if val < 0:
                self.result.add_error(f"{field} отрицательное: {val}")

        # Check growth without mobilization post
        old_inf = _to_int(self.old.get("army_infantry"))
        if inf > old_inf:
            has_mobilization = any(e.get("type") == "army" and e.get("params", {}).get("infantry", 0) > 0 for e in self.events)
            if not has_mobilization:
                self.result.add_error(f"Рост пехоты {old_inf}→{inf} без поста о мобилизации")

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
        industry = self.new.get("industry_json", [])
        if not isinstance(industry, list):
            self.result.add_error("industry_json не является списком")
            return

        from cards import BUILDING_TIMES, BUILDING_COSTS
        for item in industry:
            if not isinstance(item, dict):
                continue
            name = item.get("name") or item.get("type")
            count = _to_int(item.get("value") or item.get("count"))
            if not name or count <= 0:
                continue
            # Check cost consistency
            cost = BUILDING_COSTS.get(name, 0)
            if cost > 0:
                # Can't verify exact spending without history, just warn
                pass
            # No instant appearance check here - requires construction queue

    def _check_unreg(self) -> None:
        if self.new.get("status") == "unreg":
            pop = _to_int(self.new.get("population"))
            bud = _to_int(self.new.get("budget"))
            arm = _to_int(self.new.get("army_infantry"))
            if pop != 0 or bud != 0 or arm != 0:
                self.result.add_error("Анрегнутая страна должна иметь нулевые показатели")

    def _check_mobilization(self) -> None:
        # Army growth only with mobilization event
        old_inf = _to_int(self.old.get("army_infantry"))
        new_inf = _to_int(self.new.get("army_infantry"))
        if new_inf > old_inf:
            mob_event = any(
                e.get("type") == "army" and e.get("params", {}).get("infantry", 0) > 0
                for e in self.events
            )
            if not mob_event:
                self.result.add_error("Рост армии без события мобилизации")

    def _check_stability_war_happiness(self) -> None:
        for field in ("stability", "war_support", "happiness"):
            val = _to_int(self.new.get(field))
            if val < 0 or val > 100:
                self.result.add_error(f"{field} вне диапазона 0-100: {val}")

    def _check_tax(self) -> None:
        valid_levels = {"low", "standard", "high", "maximum"}
        tax = self.new.get("tax_level")
        if tax and tax not in valid_levels:
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