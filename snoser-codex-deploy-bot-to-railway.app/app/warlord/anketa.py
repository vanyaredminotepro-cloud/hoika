"""Deterministic parser for the numbered WARLORD RP anketa template.

The registration template is a fixed numbered list:
  1. Название страны
  2. Население
  3. Армия
  4. Территория
  5. Флаг (описание)
  6. Ссылка на флаг
  7. Правитель (ФИО)
  8. Два ПГТ и столица
  9. Стартовый бюджет («100к в.р.»)
 10. Цвет

Parsing it with regexes keeps country data correct even when the AI provider
is down — the previous behaviour silently left every country at
population=1, army=0.
"""
from __future__ import annotations

import re

_MULTIPLIERS = {
    "к": 1_000, "тыс": 1_000, "m": 1_000_000, "млн": 1_000_000,
    "мл": 1_000_000_000, "млрд": 1_000_000_000,
}


def _to_number(raw: str) -> float | None:
    """Parse '120', '100к', '1.5 млн', '100 000' into a number."""
    if not raw:
        return None
    text = raw.strip().replace("\u00a0", " ").replace(" ", "")
    text = text.replace(",", ".").replace("\u043b", "л")  # кириллическая л
    m = re.search(r"(\d+(?:\.\d+)?)", text)
    if not m:
        return None
    value = float(m.group(1))
    tail = text[m.end():].lower()
    for suffix, mult in _MULTIPLIERS.items():
        if suffix in tail:
            value *= mult
            break
    return value


def _field(text: str, number: int) -> str:
    """Return the value of numbered field N from an anketa."""
    pattern = rf"(?:^|\n)\s*{number}\s*[.)]\s*(.+)"
    m = re.search(pattern, text)
    if not m:
        return ""
    value = m.group(1).strip()
    # value ends at the next numbered field or a hard wrap
    nxt = re.search(r"\n\s*\d{1,2}\s*[.)]\s", value)
    if nxt:
        value = value[: nxt.start()].strip()
    return value.strip(" \t.;,")


def _is_plain_number(value: str) -> bool:
    """True for '120' but not for '120 солдат' or a whole sentence."""
    return bool(re.fullmatch(r"\d{1,9}", value.replace(" ", "").replace("\u00a0", "")))


_LABEL_WORDS = (
    "местоположение", "название", "двух", "пгт", "столица", "столицы",
    "указан", "выше", "другие", "города", "пгт1", "пгт2", "птг",
)


def _clean_settlements(value: str) -> list[str]:
    """'Столица Святоград, пгт. Наутички и пгт. Шума' -> ['Святоград', ...]."""
    # drop the template label glued to the first value: "... столицы: Паштва"
    value = re.sub(r"^.{0,80}?столиц[аы]:\s*", "", value, flags=re.IGNORECASE)
    parts = [p.strip() for p in re.split(r"[;,]| и ", value) if p.strip()]
    out: list[str] = []
    for part in parts:
        # "Испансия :пгт1:Тумачка" -> keep the tail after the last colon
        if ":" in part:
            head, _, tail = part.rpartition(":")
            if tail.strip() and not all(
                w in head.lower() for w in ("пгт", "птг", "столиц")
            ):
                part = tail.strip()
        low = part.lower()
        words = [w for w in re.split(r"[^\wа-яё]+", low) if w]
        if not words:
            continue
        # a part made only of template words carries no settlement name
        if all(w in _LABEL_WORDS for w in words):
            continue
        part = re.sub(r"^\d*\s*(пгт|птг)\s*\d*\s*[:.]?\s*", "", part, flags=re.IGNORECASE)
        part = re.sub(r"^столиц[аы]\s*[:.]?\s*", "", part, flags=re.IGNORECASE)
        part = part.strip(" .,:—-()")
        if part and len(part) <= 60 and not part.lower().startswith("указан"):
            out.append(part)
    return out[:5]


def parse_anketa_template(text: str) -> dict:
    """Extract anketa fields from the numbered template.

    Returns a dict with whatever could be parsed; empty when the message is
    not the numbered template at all.
    """
    if not text:
        return {}
    low = text.lower()
    if "шаблон анкеты" in low and not re.search(r"^\s*1\s*[.)]\s*\S", text, re.M):
        return {}  # пустой шаблон, который кто-то просто скинул в чат

    name = _field(text, 1)
    if not name or len(name) > 60 or name.lower().startswith(("название", "префикс")):
        return {}

    # The faction anketa reuses the same numbering but field 2 holds tasks and
    # field 3 the host country. Only bare numbers mean a country template,
    # otherwise we would invent countries named "Легион" or "UNC".
    # Порядок полей в шаблоне страны (п.12 ТЗ):
    #   1 Название | 2 Солдаты | 3 Граждане | 4 Территория | 5 Флаг
    #   6 Ссылка на канал | 7 Руководитель | 8 Столица + 2 ПГТ
    #   9 Бюджет | 10 Цвет
    # Раньше поля 2 и 3 были перепутаны местами, из-за чего население
    # записывалось из поля «Солдаты», а армия — из «Граждане».
    raw_soldiers = _field(text, 2)
    raw_citizens = _field(text, 3)
    if not _is_plain_number(raw_soldiers) or not _is_plain_number(raw_citizens):
        return {}

    data: dict = {"name": name.strip()}

    soldiers = _to_number(raw_soldiers)
    if soldiers and soldiers >= 0:
        data["army_infantry"] = int(soldiers)

    citizens = _to_number(raw_citizens)
    if citizens and citizens >= 1:
        data["population"] = int(citizens)

    territory = _field(text, 4)
    if territory and "Территория" not in territory:
        data["territory"] = territory

    # Флаг и ссылка на канал. В шаблоне это поля 5 и 6, но на практике игроки
    # часто оставляют поле 5 подписью («Флаг вашей страны:») и кладут ссылку
    # в 6-е, поэтому берём URL из первого подходящего поля.
    def _url(field: str) -> str:
        return field if field.lower().startswith(("http://", "https://", "t.me/")) else ""

    flag_url = _url(_field(text, 5)) or _url(_field(text, 6))
    if flag_url:
        data["flag_url"] = flag_url

    channel_url = _url(_field(text, 6))
    if channel_url:
        data["channel_url"] = channel_url

    ruler = _field(text, 7)
    if ruler and not ruler.lower().startswith(("правитель", "фио")):
        data["ruler"] = ruler

    settlements = _field(text, 8)
    if settlements:
        parts = _clean_settlements(settlements)
        if parts:
            data["settlements"] = parts

    budget = _to_number(_field(text, 9))
    if budget and budget > 0:
        data["start_budget"] = float(budget)

    color = _field(text, 10)
    if color and len(color) <= 40:
        data["color"] = color

    if len(data) < 2:
        return {}
    return data
