from __future__ import annotations

import re
from dataclasses import dataclass, field
from html import escape


@dataclass(frozen=True, slots=True)
class EconomyRules:
    base_income: int = 1000
    factory_income: int = 500
    port_income: int = 300
    soldier_upkeep: int = 5
    citizen_upkeep: int = 2
    tax_multipliers: dict[str, float] = field(
        default_factory=lambda: {
            "Пониженный": 0.5,
            "Средний": 1.0,
            "Повышенный": 1.5,
            "Чрезмерный": 2.0,
        }
    )

    def normalize_tax_level(self, value: str | None) -> str:
        raw = (value or "Средний").strip().lower()
        for label in self.tax_multipliers:
            if raw == label.lower():
                return label
        return "Средний"

    def daily_income(self, *, factories: int, ports: int, citizens: int, tax_level: str | None = None) -> int:
        tax_label = self.normalize_tax_level(tax_level)
        return int(
            self.base_income
            + max(0, factories) * self.factory_income
            + max(0, ports) * self.port_income
            + self.tax_multipliers[tax_label] * max(0, citizens)
        )

    def daily_expense(self, *, soldiers: int, citizens: int) -> int:
        return max(0, soldiers) * self.soldier_upkeep + max(0, citizens) * self.citizen_upkeep


@dataclass(frozen=True, slots=True)
class CountryCardInput:
    country: str
    ruler: str = "-"
    party: str = "-"
    ideology: str = "-"
    citizens: int | None = None
    previous_citizens: int | None = None
    capacity: int | None = None
    budget: int | None = None
    previous_budget: int | None = None
    soldiers: int | None = None
    previous_soldiers: int | None = None
    tax_level: str = "Средний"
    settlements: list[str] = field(default_factory=list)
    industry: list[str] = field(default_factory=list)
    researches: list[str] = field(default_factory=list)
    stability: int | None = None
    previous_stability: int | None = None
    war_support: int | None = None
    previous_war_support: int | None = None
    happiness: int | None = None
    previous_happiness: int | None = None
    events: list[str] = field(default_factory=list)
    violations: list[str] = field(default_factory=list)
    flag_present: bool = False
    channel_has_avatar: bool = False
    ruler_mention: str | None = None
    factories: int = 0
    ports: int = 0


class WarlordAutonomyCore:
    """Deterministic Warlord RP mechanics and Telegram-ready render helpers."""

    economy = EconomyRules()
    non_rp_patterns: tuple[tuple[str, str], ...] = (
        (r"\b(захватил|победил|уничтожил)\b.{0,80}\b(без боя|мгновенно|сразу)\b", "PG: победа/захват без отыгрыша"),
        (r"\b(появил[аио]сь|получил[аи]?)\b.{0,60}\b(миллиард|деньг|солдат|танк|самолет)\b", "Ресурсы из воздуха"),
        (r"\b(лазер|робот|плазм|антиграв|ядерн|18\+|21\+)\b", "Запрещённый абсурд/контент"),
        (r"\b(рофл|мем|шутк|ахах|лол)\b", "Мемный/неролевой контент"),
    )

    @classmethod
    def moderation_violations(cls, text: str) -> list[str]:
        low = (text or "").strip().lower()
        violations = [reason for pattern, reason in cls.non_rp_patterns if re.search(pattern, low, flags=re.IGNORECASE)]
        if low and len([line for line in low.splitlines() if line.strip()]) == 1 and len(low.split()) <= 8:
            violations.append("Однострочный спам: не даёт бонусов")
        return violations

    @staticmethod
    def answer_rule(title: str, price_or_condition: str, time_or_limit: str, note: str) -> str:
        return (
            f"🎯 <b>{escape(title)}</b>\n"
            f"• <b>Цена/Условие:</b> {escape(price_or_condition)}\n"
            f"• <b>Время/Лимит:</b> {escape(time_or_limit)}\n"
            f"• <b>Примечание:</b> {escape(note)}"
        )

    @staticmethod
    def _fmt(value: int | None, suffix: str = "") -> str:
        return "-" if value is None else f"{value:,}".replace(",", " ") + suffix

    @staticmethod
    def _delta(current: int | None, previous: int | None, suffix: str = "") -> str:
        if current is None or previous is None:
            return ""
        delta = current - previous
        if delta == 0:
            return ""
        sign = "+" if delta > 0 else ""
        return f" ({sign}{delta}{suffix})"

    @staticmethod
    def _list_or_dash(items: list[str]) -> str:
        return ", ".join(escape(x) for x in items if x) or "-"

    @classmethod
    def flag_notice(cls, data: CountryCardInput) -> str | None:
        if data.flag_present:
            return None
        mention = data.ruler_mention or data.ruler or "правитель"
        if data.channel_has_avatar:
            return (
                f"⚠️ {mention}, для твоей страны {data.country} не прикреплена фотография флага. "
                "Отправь изображение/картинку флага или дай согласие использовать аватарку твоего канала!"
            )
        return (
            f"⚠️ {mention}, для твоей страны {data.country} не прикреплена фотография флага. "
            "Пожалуйста, пришли изображение/картинку флага!"
        )

    @classmethod
    def render_country_card(cls, data: CountryCardInput) -> str:
        tax = cls.economy.normalize_tax_level(data.tax_level)
        citizens = data.citizens or 0
        soldiers = data.soldiers or 0
        income = cls.economy.daily_income(factories=data.factories, ports=data.ports, citizens=citizens, tax_level=tax)
        expense = cls.economy.daily_expense(soldiers=soldiers, citizens=citizens)
        notifications = [*data.violations]
        notice = cls.flag_notice(data)
        if notice:
            notifications.insert(0, notice)
        if not notifications:
            notifications.append("Нарушений не выявлено")
        events = data.events or ["Существенных RP-событий за период не зафиксировано."]
        flag_line = "[Фото флага отсутствует]" if not data.flag_present else "[🖼 Фотография флага прикрепится медиа-файлом к посту]"
        return "\n".join([
            flag_line,
            f"<b>ГОСУДАРСТВО: {escape(data.country)}</b>",
            f"👑 <b>Правитель:</b> {escape(data.ruler or '-')}",
            f"🏛 <b>Партия:</b> {escape(data.party or '-')} | 👁 <b>Идеология:</b> {escape(data.ideology or '-')}",
            "",
            "📊 <b>ГОСУДАРСТВЕННЫЕ ПОКАЗАТЕЛИ:</b>",
            f"👥 <b>Население:</b> {cls._fmt(data.citizens)}{cls._delta(data.citizens, data.previous_citizens)} (Вместимость: {cls._fmt(data.capacity)})",
            f"💰 <b>Казна:</b> {cls._fmt(data.budget)} вирт-руб.{cls._delta(data.budget, data.previous_budget)} <i>(Доход: +{income} / Расход: -{expense})</i>",
            f"🪖 <b>Армия:</b> {cls._fmt(data.soldiers)} солдат{cls._delta(data.soldiers, data.previous_soldiers)}",
            f"📊 <b>Налоги:</b> {tax}",
            "",
            "🏭 <b>ИНФРАСТРУКТУРА И ЭКОНОМИКА:</b>",
            f"• <b>Населённые пункты:</b> {cls._list_or_dash(data.settlements)}",
            f"• <b>Промышленность:</b> {cls._list_or_dash(data.industry)}",
            f"• <b>Исследования:</b> {cls._list_or_dash(data.researches) if data.researches else 'Нет'}",
            "",
            "⚖️ <b>ОБЩЕСТВЕННОЕ СОСТОЯНИЕ:</b>",
            f"📉 <b>Стабильность:</b> {cls._fmt(data.stability, '%')}{cls._delta(data.stability, data.previous_stability, '%')}",
            f"⚔️ <b>Поддержка войны:</b> {cls._fmt(data.war_support, '%')}{cls._delta(data.war_support, data.previous_war_support, '%')}",
            f"😊 <b>Счастье народа:</b> {cls._fmt(data.happiness, '%')}{cls._delta(data.happiness, data.previous_happiness, '%')}",
            "",
            "📝 <b>ИТОГИ ЗА 2 ЧАСА:</b>",
            *(f"• {escape(event)}" for event in events),
            "",
            "⚠️ <b>УВЕДОМЛЕНИЯ И АВТО-ЗАПРОСЫ:</b>",
            *(escape(item) for item in notifications),
        ])
