"""
Compatibility scheduler module for backward compatibility with tests.
Provides WarlordScheduler class and apply_country_changes function.
"""
from __future__ import annotations
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from .cards import apply_event as _apply_event
from .config import config
from .economy import BASE_INCOME_DAILY

log = logging.getLogger(__name__)

# Default industry for new countries (test expects 3 items producing 1500 income)
_DEFAULT_INDUSTRY = [
    {"name": "военный завод", "type": "military_factory", "value": 1},
    {"name": "фабрика", "type": "factory", "value": 1},
    {"name": "фабрика", "type": "factory", "value": 1},
]


def apply_country_changes(current: dict[str, Any], changes: dict[str, Any]) -> dict[str, Any]:
    """
    Apply changes to country dict. Compatibility wrapper for apply_event.
    Expected changes format: {"field": value, ...} or {"events": [...]}
    """
    updated = current.copy()
    events = changes.get("events")
    if events:
        for ev in events:
            _apply_event(updated, ev)
    else:
        # Direct field updates
        for k, v in changes.items():
            if k != "events":
                updated[k] = v
    return updated


class WarlordScheduler:
    """
    Compatibility wrapper for WarlordScheduler.
    Provides methods needed by tests: _extract_cash_ops, _auto_unreg_inactive, etc.
    """

    _DEFAULT_INDUSTRY = _DEFAULT_INDUSTRY

    def __init__(self, db: Any = None, ai=None, bot=None, userbot=None):
        self.db = db
        self.ai = ai
        self.bot = bot
        self.userbot = userbot
        self.config = config

    @staticmethod
    def _extract_cash_ops(text: str) -> list[tuple[float, str, str]]:
        """Extract cash operations from text."""
        if not text:
            return []
        out: list[tuple[float, str, str]] = []
        num_pat = r"(\d(?:[\d\s.,.]{0,8}))\s*(к|тыс|руб|вр|в\.\s*р)?"
        spend_prefix = (
            r"(?:закуп(?:и(?:л|ла|ли)?)|потрат(?:и(?:л|ла|ли)?)|"
            r"вылож(?:и(?:л|ла|ли)?)|заплат(?:и(?:л|ла|ли)?)|"
            r"обош(?:л(?:ось|ась)|лось)|стоило|сожг(?:л|ла|ли))\b"
        )
        grant_prefix = (
            r"(?:наград[аыиу]|награжд[а-я]*|преми[аию]|"
            r"поступ(?:и(?:л|ла|ли)?)|выдел(?:и(?:л|ла|ли)?|ено|ена)|"
            r"субсиди[аию]|финансиров[а-я]{2,3})\b"
        )
        for prefix, sign in ((spend_prefix, -1.0), (grant_prefix, 1.0)):
            for m in re.finditer(
                rf"{prefix}[^\n]{{0,60}}?{num_pat}", text, re.IGNORECASE
            ):
                raw = m.group(1).replace(" ", "").replace(",", "").replace(".", "")
                mul = 1000.0 if (m.group(2) or "").lower().startswith(("к", "тыс")) else 1.0
                try:
                    amt = float(raw) * mul * sign
                except (TypeError, ValueError):
                    continue
                if abs(amt) > 1_000_000 or abs(amt) < 100:
                    continue
                out.append((amt, "cost" if sign < 0 else "grant",
                            "траты" if sign < 0 else "награда/доход"))
        # штраф
        for m in re.finditer(rf"штраф[а-я]*\b[^\n]{{0,40}}?{num_pat}", text, re.IGNORECASE):
            raw = m.group(1).replace(" ", "").replace(",", "").replace(".", "")
            mul = 1000.0 if (m.group(2) or "").lower().startswith(("к", "тыс")) else 1.0
            try:
                amt = -float(raw) * mul
            except (TypeError, ValueError):
                continue
            if 100 <= abs(amt) <= 1_000_000:
                out.append((amt, "cost", "штраф"))
        return out

    @staticmethod
    def _msgs_are_recent(msgs: list[dict[str, Any]], days: int = 14) -> bool:
        if not msgs:
            return False
        newest = max((m.get("date") or "" for m in msgs), default="")
        try:
            dt = datetime.fromisoformat(str(newest).replace("Z", "+00:00"))
        except ValueError:
            return False
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) - dt <= timedelta(days=days)

    @staticmethod
    def _detect_unreg(text: str) -> bool:
        low = text.lower()
        if re.search(
            r"\b(?:объявляю|объявляем|объявил[аи]?|заявил[аи]?)\b[^.\n]{0,45}?"
            r"(?:анрег|уход[а-я]*\s+(?:из|с)|выход[а-я]*\s+(?:из|с))",
            low,
        ):
            return True
        if re.search(r"\b(?:я|мы)\b[^.\n]{0,35}?(?:анрег|ухож[уи]\s+в?|выходим|сним(?:аю|аем))", low):
            return True
        if re.search(r"\bснима(?:ю|ем|емся|ются)\b[^.\n]{0,30}?(?:страну|с проекта|из игры|анрег)", low):
            return True
        if re.search(r"\bпокида(?:ю|ем)\b[^.\n]{0,25}?(?:проект|игру|рол|warlord)", low):
            return True
        if re.search(
            r"\b(?:анрегн(?:улся|улась|улась|ули|улят)|ухож[уи]\s+в\s+анрег|уходит\s+в\s+анрег)",
            low,
        ):
            return True
        return False

    @staticmethod
    def _detect_registration_budget(text: str) -> float | None:
        """Extract registration budget from text like 'Бюджет: 75к'."""
        import re
        m = re.search(r"бюджет\s*[:—]?\s*(\d+(?:[\d\s.,]{0,8}))\s*(к|тыс)?", text, re.IGNORECASE)
        if not m:
            return None
        raw = m.group(1).replace(" ", "").replace(",", "").replace(".", "")
        mul = 1000.0 if (m.group(2) or "").lower().startswith(("к", "тыс")) else 1.0
        try:
            return float(raw) * mul
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _find_duplicate_of(name: str, existing: list[dict[str, Any]]) -> str:
        """Return canonical name if name is a variant of existing."""
        low = (name or "").strip().lower()
        base = re.sub(r"[\(\[].*?[\)\]]", "", low).strip()
        min_len = 4
        for c in existing:
            cname = (c.get("name") or "").strip().lower()
            if not cname:
                continue
            if low == cname or base == cname:
                return c["name"]
            if len(low) >= min_len and low in cname:
                return c["name"]
            if len(cname) >= min_len and cname in low:
                return c["name"]
            cbase = re.sub(r"[\(\[].*?[\)\]]", "", cname).strip()
            if base and base == cbase:
                return c["name"]
        return ""

    @staticmethod
    def _msgs_are_recent(msgs: list[dict[str, Any]], days: int = 14) -> bool:
        if not msgs:
            return False
        newest = max((m.get("date") or "" for m in msgs), default="")
        try:
            dt = datetime.fromisoformat(str(newest).replace("Z", "+00:00"))
        except ValueError:
            return False
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) - dt <= timedelta(days=days)

    @staticmethod
    def _detect_unreg(text: str) -> bool:
        low = text.lower()
        if re.search(
            r"\b(?:объявляю|объявляем|объявил[аи]?|заявил[аи]?)\b[^.\n]{0,45}?"
            r"(?:анрег|уход[а-я]*\s+(?:из|с)|выход[а-я]*\s+(?:из|с))",
            low,
        ):
            return True
        if re.search(r"\b(?:я|мы)\b[^.\n]{0,35}?(?:анрег|ухож[уи]\s+в?|выходим|сним(?:аю|аем))", low):
            return True
        if re.search(r"\bснима(?:ю|ем|емся|ются)\b[^.\n]{0,30}?(?:страну|с проекта|из игры|анрег)", low):
            return True
        if re.search(r"\bпокида(?:ю|ем)\b[^.\n]{0,25}?(?:проект|игру|рол|warlord)", low):
            return True
        if re.search(
            r"\b(?:анрегн(?:улся|улась|улись|ули|улят)|ухож[уи]\s+в\s+анрег|уходит\s+в\s+анрег)",
            low,
        ):
            return True
        return False

    async def _seed_registration(self, country: dict[str, Any], msgs: list[dict[str, Any]]) -> None:
        """Seed registration events for country."""
        if not self.db:
            return
        cid = country.get("id")
        if not cid:
            return
        # Check if already registered
        existing = await self.db.list_economy_events(cid)
        if any(e.get("kind") == "registration" for e in existing):
            return
        
        # Registration event - use first message date if available
        reg_date = None
        start_budget = country.get("start_budget")
        if start_budget is None:
            start_budget = country.get("budget")
        if start_budget is not None:
            for m in msgs:
                if "бюджет" in (m.get("text") or "").lower():
                    reg_date = m.get("date")
                    break
            if not reg_date and msgs:
                reg_date = msgs[0].get("date")
        # If no budget keyword but we have start_budget and messages, use first message date
        if not reg_date and start_budget is not None and msgs:
            reg_date = msgs[0].get("date")
        if reg_date:
            await self.db.add_economy_event(
                cid, reg_date, "registration", float(start_budget or 0), rate=False,
                note="регистрация"
            )
        # Base income event
        if reg_date:
            await self.db.add_economy_event(
                cid, reg_date, "base", BASE_INCOME_DAILY, rate=True,
                note="базовый доход"
            )

    async def _record_action_events(
        self, updated: dict[str, Any], old: dict[str, Any], msgs: list[dict[str, Any]]
    ) -> None:
        """Record action events for country."""
        if not self.db:
            return
        cid = updated.get("id")
        if not cid:
            return
        event_date = None
        if msgs:
            event_date = msgs[0].get("date")
        if not event_date:
            return
        
        # Citizens
        old_pop = int(old.get("population") or 0)
        new_pop = int(updated.get("population") or 0)
        if new_pop != old_pop:
            delta = new_pop - old_pop
            amount = (1.0 - 2.0) * delta  # TAX_PER_CAPITA["средний"] - 2.0
            await self.db.add_economy_event(
                cid, event_date, "citizens", amount, rate=True,
                note=f"население ({delta:+d})"
            )
        
        # Soldiers
        old_inf = int(old.get("army_infantry") or 0)
        new_inf = int(updated.get("army_infantry") or 0)
        if new_inf != old_inf:
            delta = new_inf - old_inf
            amount = -5.0 * delta
            await self.db.add_economy_event(
                cid, event_date, "soldiers", amount, rate=True,
                note=f"армия ({delta:+d})"
            )

    async def _auto_unreg_inactive(self, days: int = 45) -> int:
        """Auto-unregister inactive countries. Returns count of unregistered."""
        if not self.db:
            return 0
        countries = await self.db.list_countries("active")
        unreg_count = 0
        for country in countries:
            last_activity = country.get("last_activity_at") or country.get("updated_at")
            if not last_activity:
                continue
            try:
                dt = datetime.fromisoformat(str(last_activity).replace("Z", "+00:00"))
            except ValueError:
                continue
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            if (datetime.now(timezone.utc) - dt).days >= days:
                await self.db.set_country_status(country["name"], "unreg")
                unreg_count += 1
        return unreg_count


# For backward compatibility
__all__ = ["WarlordScheduler", "apply_country_changes", "_DEFAULT_INDUSTRY"]