"""
Загрузка мануальной статистики (раздел 5 ТЗ, модуль CARDS).

Для стран из config.MANUAL_STATS_COUNTRIES стартовые и контрольные цифры
заданы руками. Правило раздела 4: для stats_source="manual" LLM не имеет
права менять население, армию, бюджет и вооружение без явного поста в новостях.
Этот файл — тот самый «пост», который авторизует начальные значения.

Что сюда попадает и что нет:
  * population, budget, army_*, stability, war_support, happiness и т.д.
    пишутся в колонки countries — они и есть карточка.
  * Полный исходный текст раздела 5 (вооружение, ракеты, дроны, спутники)
    сохраняется в manual_stats.payload_json как источник истины. Он НЕ рисуется
    в карточке: структура раздела 2 для вооружений не предусматривает строк,
    а выдумывать их нельзя. Payload используется проверками и ассистентом.

Запуск идемпотентен: apply(force=False) не трогает уже загруженные страны,
чтобы ручная правка админа не затиралась повторным сидом.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from . import config

log = logging.getLogger(__name__)


def _s(*pairs: tuple[str, int]) -> list[dict[str, Any]]:
    """Список построек/объектов: [("М воензавод", 1), ...] -> [{name,count}]."""
    return [{"name": name, "count": count} for name, count in pairs]


# ══════════════════════════ РАЗДЕЛ 5. МАНУАЛЬНАЯ СТАТИСТИКА ══════════════
#
# country: поля для countries
# raw:     исходный текст раздела 5 для manual_stats

MANUAL_STATS: dict[str, dict[str, Any]] = {
    "Пупсонская Федерация": {
        "country": {
            "population": 186, "army_infantry": 40,
            "budget": 9300.0, "income_daily": 2000.0, "expenses_daily": 0.0,
            "stability": 70.0, "happiness": 63.7, "war_support": 50.0,
            "population_capacity": 186,
            "tax_level": "Средний",
            "industry_json": _s(("Военный завод", 1), ("Верфь", 1),
                                ("Больница", 3), ("Школа", 3), ("Фабрика", 9)),
        },
        "raw": (
            "Граждане: 186 | Стабильность: 70% | Армия: 0 (нацгвардия 40)\n"
            "Счастье: 63.7% | Доход: 10606 (+2000 от газа, алюминия, меди)\n"
            "Бюджет: 9300\n"
            "Постройки: 1 воензавод, 1 верфь, 3 больницы, 3 школы, 9 фабрик\n"
            "Альянсы: СЭВ, UNC"
        ),
        "allies": ["СЭВ", "UNC"],
    },
    "Республика Новослободск": {
        "country": {
            "population": 310, "army_infantry": 170,
            "budget": 198000.0, "income_daily": 12000.0, "expenses_daily": 0.0,
            "stability": 98.0, "happiness": 88.5, "war_support": 1.6,
            "population_capacity": 310,
            "army_light_vehicles": 25,
            "army_heavy_wheeled": 20,
            "tax_level": "Высокий",
            "settlements_json": _s(("Город", 6), ("ПГТ", 6)),
        },
        "raw": (
            "Войска: ~170 (130 регулярных, 25 HMMWV, 20 резерв, 20 UNC)\n"
            "Граждане: ~310 (6 городов — 210, 6 ПГТ — 100)\n"
            "Противники: де-факто нет (де-юре враги ЛНОН)\n"
            "Союзники: ЛНОН, SIP, Р. Сербина\n"
            "Бюджет: ~198000 (19 зданий, +12000/день)\n"
            "Военный бюджет: засекречено\n"
            "Поддержка войны: 1.6% | Счастье: ~88.5% | Стабильность: ~98%\n"
            "Вооружение: пулемёты 88/130, автоматы 200/470, снайперки 50/270, "
            "пистолеты 230/320, ПЗРК 35/60, ПТРК 40/70\n"
            "Техника: вертолёты 10, транспортеры 60, бронеавтомобили 50, "
            "автобусы 5, грузовики 80+25+15\n"
            "Ракеты: ударные 70, баллистика 3, оборонные 38\n"
            "Дроны: камикадзе 150, разведка 50, морские 35\n"
            "Флот: 20 катеров"
        ),
        "allies": ["ЛНОН", "SIP", "Р. Сербина"],
        "enemies_declared": ["ЛНОН"],
    },
    "Соединённые Штаты Федерации": {
        "country": {
            "population": 580, "army_infantry": 530,
            "budget": 315000.0, "income_daily": 0.0, "expenses_daily": 20000.0,
            "stability": 83.0, "happiness": 80.0, "war_support": 82.0,
            "population_capacity": 580,
            "army_light_vehicles": 34,
            "army_heavy_wheeled": 50,
            "army_artillery": 100,
            "army_drones": 120,
            "army_navy": 65,
            "tax_level": "Высокий",
        },
        "raw": (
            "Солдаты: ~530 (285 резерв СШФ, 200 ВСФ, 45 Kraken)\n"
            "Граждане: 580\n"
            "Противники: Конфедерация, ПАО\n"
            "Союзники: ЛНОН +17 стран\n"
            "Бюджет: 315000 (-20000)\n"
            "Военный бюджет: засекречено\n"
            "Поддержка войны: 82% (+4%) | Счастье: 80% (+7%) | "
            "Стабильность: 83% (-4%)\n"
            "Вооружение: АК-12 600, РПК 200, СВД 70, Glock 168, АШ-12 240, AUG 150\n"
            "Техника: грузовики МАЗ-2 40, джипы с пулемётом 34, бронепикапы 50\n"
            "Артиллерия: ДБЛЦ 120, ZALP-2 29, зенитки 20, 2А65 100, L118 85\n"
            "БПЛА: М-ДОД 120, М-ИОД 110\n"
            "Флот: десантные 65, штурмовые 50\n"
            "Авиация: CHP-P 1, ВЫШ-2 1, U-6 1"
        ),
        "allies": ["ЛНОН"],
        "enemies_declared": ["Конфедерация Республик Гнева", "ПАО"],
    },
    "Дацберг": {
        "country": {
            "population": 100, "army_infantry": 30,
            "budget": 1000.0, "income_daily": 0.0, "expenses_daily": 0.0,
            "stability": 98.0, "happiness": 65.0, "war_support": 91.0,
            "population_capacity": 100,
            "army_light_vehicles": 20,
            "army_heavy_wheeled": 60,
            "army_artillery": 1,
            "tax_level": "Низкий",
        },
        "raw": (
            "Солдаты: 30 | Граждане: 100\n"
            "Противники: нет\n"
            "Союзники: Норденштадт, РНГ, Ф. Паштета, МЭС, Регио-Декала\n"
            "Бюджет: 1000 | Военный: неограничен\n"
            "Поддержка войны: 91% | Счастье: 65% | Стабильность: 98%\n"
            "Вооружение: 20 автоматов, 30 пулемётов, 6 снайперок, 100 пистолетов\n"
            "Техника: грузовики 35, джипы 20, бронепикапы 60\n"
            "Артиллерия: ракеты 2, ПВО 1, миномёты 1\n"
            "Другое: 14 спутников SECSlink, 2 спутника 2 рейха, "
            "1 обнаружение ракет, 2 SECS, лунные модули"
        ),
        "allies": ["Норденштадт", "РНГ", "Ф. Паштета", "МЭС", "Регио-Декала"],
    },
    "Югоречие": {
        "country": {
            "population": 90, "army_infantry": 35,
            "budget": 16900.0, "income_daily": 0.0, "expenses_daily": 0.0,
            "stability": 70.0, "happiness": 50.0, "war_support": 50.0,
            "population_capacity": 90,
            "tax_level": "Средний",
        },
        "raw": (
            "Солдаты: 35 | Граждане: 90\n"
            "Противники: нет | Союзники: весь ЛНОН\n"
            "Бюджет: 16900\n"
            "Поддержка войны: ~50% | Счастье: ~50% | Стабильность: ~70%\n"
            "Вооружение: 35 автоматов\n"
            "Другое: 1 прототип ТВ-81"
        ),
        "allies": ["ЛНОН"],
    },
    "Паониция": {
        "country": {
            "population": 315, "army_infantry": 215,
            "budget": 160000.0, "income_daily": 10000.0, "expenses_daily": 0.0,
            "stability": 88.0, "happiness": 75.0, "war_support": 87.0,
            "population_capacity": 315,
            "army_light_vehicles": 65,
            "army_heavy_wheeled": 70,
            "army_artillery": 70,
            "army_drones": 90,
            "army_navy": 62,
            "tax_level": "Высокий",
            # ОЯТ: единственная страна, где LLM может его менять (разделы 2, 13).
            # ТХ-999 засекречено - ненулевое значение означало бы вскрытие тайны.
            "ojat": 499,
            "tx999": 0,
        },
        "raw": (
            "Солдаты: ~215 (62 ПАО, 111 ВСП, 38 Fauzwaff, 14 Fraus Division)\n"
            "Граждане: 315\n"
            "Противники: Федерация, ЛНОН (налаживаются)\n"
            "Союзники: Олбония, Пупсонская Федерация\n"
            "Бюджет: 160000 (+10000)\n"
            "Военный бюджет: засекречено\n"
            "Поддержка войны: 87% (+2%) | Счастье: 75% (+3%) | "
            "Стабильность: 88% (+3%) | Поддержка правительства: 90% (+1%)\n"
            "Вооружение: 740 автоматов, 100 пулемётов, 50 снайперок, "
            "600 пистолеты\n"
            "Техника: грузовики 50, джипы 65, бронепикапы 70\n"
            "Артиллерия: ракеты 90, ПВО 50, миномёты 70\n"
            "БПЛА: разведка 90, «Икар» 95, «X-1» 90, «FS» 190\n"
            "Флот: десантные 62, мины 73, наводные дроны 38\n"
            "Другое: ОЯТ-Р, ОЯТ-Д, ОЯТ (>499 л), ТХ-999 — ЗАСЕКРЕЧЕНО"
        ),
        "allies": ["Олбония", "Пупсонская Федерация"],
        "enemies_declared": ["Соединённые Штаты Федерации", "ЛНОН"],
    },
    "Агесия": {
        "country": {
            "population": 183, "army_infantry": 45,
            "budget": 13921.0, "income_daily": 0.0, "expenses_daily": 9644.0,
            "stability": 80.0, "happiness": 85.0, "war_support": 60.0,
            "population_capacity": 183,
            "army_light_vehicles": 5,
            "army_artillery": 15,
            "tax_level": "Средний",
        },
        "raw": (
            "Солдаты: 45 | Граждане: 183\n"
            "Противники: нет\n"
            "Союзники: Югоречие, SIP, Антония, ZETA-3, С.Р.И., Р. Сербина, "
            "Регио-Декала, Линьфа, ЗХАД, Kraken, Нордстан, Гулания\n"
            "Бюджет: 13921 (-9644)\n"
            "Поддержка войны: 60% | Счастье: 85% | Стабильность: 80%\n"
            "Вооружение: 40 автоматов, 10 AUG-81K, 10 LR-TAC H, "
            "5 Browning M980-H5\n"
            "Техника: грузовики 3, M10098 5\n"
            "Артиллерия: 15 зениток, 3 комплекса Warrior"
        ),
        "allies": ["Югоречие", "SIP", "Антония", "ZETA-3", "С.Р.И.",
                   "Р. Сербина", "Регио-Декала", "Линьфа", "ЗХАД", "Kraken",
                   "Нордстан", "Гулания"],
    },
    "Супер Земля": {
        "country": {
            "population": 540, "army_infantry": 70,
            "budget": 0.0, "income_daily": 0.0, "expenses_daily": 0.0,
            "stability": 70.0, "happiness": 70.0, "war_support": 50.0,
            "population_capacity": 540,
            "army_light_vehicles": 0,
            "tax_level": "Средний",
            "industry_json": _s(("Завод", 13), ("Больница", 5), ("Школа", 4),
                                ("Шахта", 5), ("Университет", 1)),
        },
        "raw": (
            "Граждане: 540 | Военные: 70 | Техника: 0\n"
            "Постройки: 11+2 завода, 5 больниц, 4 школы, 5 шахт, 1 университет\n"
            "Примечание: в состав вошла Хельксария (отдельно не существует)."
        ),
        "note": "В состав вошла Хельксария, отдельно не существует.",
    },
    # ВНИМАНИЕ: в разделе 5 у РНГ НЕТ строки «Граждане» — только солдаты.
    # Население неизвестно, поэтому оставляем 0, а не выдумываем число.
    # Следствие: карточка РНГ не публикуется (раздел 11, population < 60) и
    # страна уходит в data_error с запросом анкеты админу. Это ровно то
    # поведение, которое требует ТЗ, а не ошибка сида.
    "Русское Национальное Государство": {
        "country": {
            "population": 0, "army_infantry": 300,
            "budget": 0.0, "income_daily": 0.0, "expenses_daily": 0.0,
            "stability": 70.0, "happiness": 70.0, "war_support": 92.0,
            "population_capacity": 0,
            "army_light_vehicles": 80,
            "army_heavy_wheeled": 300,
            "army_artillery": 110,
            "army_drones": 370,
            "army_navy": 60,
            "tax_level": "Высокий",
        },
        "raw": (
            "Статистика военной мощи РНГ (05.10.2026)\n"
            "Официальное кол-во солдат: 300. ВСРНГ 130, РОА 60, "
            "Боевые Отряды РНЕ 50, ВМФ 60\n"
            "Противники: Паониция\n"
            "Союзники: подконтрольные организации\n"
            "Военный бюджет: 265к\n"
            "Поддержка войны: 92%\n"
            "Стрелковое: 850 автоматов, 140 ручных пулемётов, 140 снайперских "
            "винтовок, 100 пистолетов, 200 РПГ, 100 ПЗРК, 300 мин пехотных, "
            "200 мин транспортных\n"
            "Техника: военные грузовики 50, багги 80, бронемашины 300, ПВО 60, "
            "миномёты 70 лёгких + 40 тяжёлых\n"
            "БПЛА: разведчики 50, камикадзе самолётные 130, FPV 190\n"
            "Флот: десантные катера и лодки 60, морские мины 80\n"
            "ВВС: боевые вертолёты 30, десантные 15\n"
            "Химоружие: засекречено"
        ),
        "allies": ["подконтрольные организации"],
        "enemies_declared": ["Паониция"],
    },
    "Республика Линьфа": {
        "country": {
            "population": 588, "army_infantry": 40,
            "budget": 428.0, "income_daily": 0.0, "expenses_daily": 0.0,
            "stability": 92.0, "happiness": 96.0, "war_support": 0.1,
            "population_capacity": 588,
            "tax_level": "Низкий",
        },
        "raw": (
            "Солдаты: 40 | Граждане: 588\n"
            "Противники: нет | Союзники: Альянс ЛНОН\n"
            "Бюджет: 428\n"
            "Военный бюджет: засекречено\n"
            "Поддержка войны: 0.1% | Счастье: 96% | Стабильность: 92%\n"
            "Вооружение: нет | Техника: нет | Артиллерия: нет | БПЛА: нет | "
            "Флот: нет | Авиация: нет"
        ),
        "allies": ["Альянс ЛНОН"],
    },
    "Шумадина и Поморавль": {
        "country": {
            "population": 521, "army_infantry": 362,
            "budget": 500000.0, "income_daily": 0.0, "expenses_daily": 0.0,
            "stability": 87.0, "happiness": 70.0, "war_support": 78.0,
            "population_capacity": 521,
            "army_light_vehicles": 25,
            "army_heavy_wheeled": 75,
            "army_artillery": 56,
            "army_drones": 55,
            "army_navy": 7,
            "tax_level": "Высокий",
        },
        "raw": (
            "Солдаты: 362 | Граждане: 521\n"
            "Противники: нет\n"
            "Союзники: Федерация, Агесия, Сербина, Новослободск, Гулания, "
            "Антония, ЛНОН\n"
            "Бюджет: 500000\n"
            "Военный бюджет: засекречено\n"
            "Поддержка войны: 78% | Счастье: 70% | Стабильность: 87%\n"
            "Вооружение: 250 автоматов, 45 пулемётов, 35 снайперок, "
            "200 пистолетов, 100 ПП\n"
            "Техника: грузовики 54, джипы с ПТРК 25, бронепикапы 75\n"
            "Артиллерия: 56, ПВО/ПРО 35 (20 ПВО + 15 ПРО), миномёты 16\n"
            "БПЛА: разведка 49, камикадзе 56, FPV 55\n"
            "Флот: десантные 7, мины засекречено\n"
            "Другое: ММоС 167, ТРоТ-7а 55, ТРоТ-8 34, ТРоТ-8а 17, "
            "AvL188 засекречено, PLx1 засекречено"
        ),
        "allies": ["Федерация", "Агесия", "Сербина", "Новослободск",
                   "Гулания", "Антония", "ЛНОН"],
    },

    # --- Missing DIRECTORY countries (added from current DB baseline) ---

    "Республика Белоярск": {
        "country": {
            "population": 73, "army_infantry": 11,
            "budget": 350151.5, "income_daily": 0.0, "expenses_daily": 0.0,
            "stability": 71.7, "happiness": 50.4, "war_support": 50.0,
            "population_capacity": 100,
            "tax_level": "Средний",
            "ruler": "Бабос II",
        },
        "raw": "Граждане: 73 | Солдаты: 11 | Бюджет: 350151",
        "ruler": "Бабос II",
    },

    "Нордстан": {
        "country": {
            "population": 87, "army_infantry": 40,
            "budget": 75000.0, "income_daily": 0.0, "expenses_daily": 0.0,
            "stability": 71.7, "happiness": 50.4, "war_support": 50.0,
            "population_capacity": 87,
            "tax_level": "Средний",
            "ruler": "Виктор Норд",
            "color": "Тёмно-зелёный",
        },
        "raw": "Граждане: 87 | Солдаты: 40 | Бюджет: 75000",
        "ruler": "Виктор Норд",
        "color": "Тёмно-зелёный",
    },

    "Республика Регио-Декала": {
        "country": {
            "population": 122, "army_infantry": 23,
            "budget": 313883.8, "income_daily": 0.0, "expenses_daily": 0.0,
            "stability": 82.6, "happiness": 53.0, "war_support": 50.0,
            "population_capacity": 360,
            "tax_level": "Средний",
            "ruler": "Генерал Мартиноз",
            "color": "Ярко синий",
        },
        "raw": "Граждане: 122 | Солдаты: 23 | Бюджет: 313884",
        "ruler": "Генерал Мартиноз",
        "color": "Ярко синий",
    },

    "Республика Сербина": {
        "country": {
            "population": 100, "army_infantry": 37,
            "budget": 67000.0, "income_daily": 0.0, "expenses_daily": 0.0,
            "stability": 71.7, "happiness": 50.4, "war_support": 50.0,
            "population_capacity": 100,
            "tax_level": "Средний",
        },
        "raw": "Граждане: 100 | Солдаты: 37 | Бюджет: 67000",
    },

    "Румия": {
        "country": {
            "population": 81, "army_infantry": 16,
            "budget": 483096.3, "income_daily": 0.0, "expenses_daily": 0.0,
            "stability": 100.0, "happiness": 68.1, "war_support": 50.0,
            "population_capacity": 240,
            "tax_level": "Средний",
            "ruler": "Император Алексей III",
            "color": "желтый",
        },
        "raw": "Граждане: 81 | Солдаты: 16 | Бюджет: 483096",
        "ruler": "Император Алексей III",
        "color": "желтый",
    },

    "N.I.C.I.L.U.S.": {
        "country": {
            "population": 62, "army_infantry": 12,
            "budget": 286609.6, "income_daily": 0.0, "expenses_daily": 0.0,
            "stability": 100.0, "happiness": 58.3, "war_support": 50.0,
            "population_capacity": 62,
            "tax_level": "Средний",
        },
        "raw": "Граждане: 62 | Солдаты: 12 | Бюджет: 286610",
    },

    "Конфедерация Республик Гнева": {
        "country": {
            "population": 122, "army_infantry": 23,
            "budget": 312884.5, "income_daily": 0.0, "expenses_daily": 0.0,
            "stability": 82.6, "happiness": 53.0, "war_support": 50.0,
            "population_capacity": 360,
            "tax_level": "Средний",
            "ruler": "Гневомир (лорд-диктатор, канцлер смерти)",
            "color": "темно-серый",
        },
        "raw": "Граждане: 122 | Солдаты: 23 | Бюджет: 312885",
        "ruler": "Гневомир (лорд-диктатор, канцлер смерти)",
        "color": "темно-серый",
    },

    "Олбония": {
        "country": {
            "population": 63, "army_infantry": 13,
            "budget": 206703.6, "income_daily": 0.0, "expenses_daily": 0.0,
            "stability": 100.0, "happiness": 58.3, "war_support": 50.0,
            "population_capacity": 63,
            "tax_level": "Средний",
        },
        "raw": "Граждане: 63 | Солдаты: 13 | Бюджет: 206704",
    },

    "Антония": {
        "country": {
            "population": 1, "army_infantry": 0,
            "budget": 253975.2, "income_daily": 0.0, "expenses_daily": 0.0,
            "stability": 100.0, "happiness": 53.1, "war_support": 54.4,
            "population_capacity": 1,
            "tax_level": "Средний",
            "ruler": "Лекси",
        },
        "raw": "Граждане: 1 | Солдаты: 0 | Бюджет: 253975",
        "ruler": "Лекси",
    },

    "ЗХАД — Западный Хавен-аль-Джихад": {
        "country": {
            "population": 62, "army_infantry": 12,
            "budget": 219166.3, "income_daily": 0.0, "expenses_daily": 0.0,
            "stability": 100.0, "happiness": 58.3, "war_support": 50.0,
            "population_capacity": 62,
            "tax_level": "Средний",
            "ruler": "Рейк Аль-Хаббат",
            "color": "Коричневый",
        },
        "raw": "Граждане: 62 | Солдаты: 12 | Бюджет: 219166",
        "ruler": "Рейк Аль-Хаббат",
        "color": "Коричневый",
    },

    "Гулания": {
        "country": {
            "population": 0, "army_infantry": 0,
            "budget": 76624.4, "income_daily": 0.0, "expenses_daily": 0.0,
            "stability": 71.7, "happiness": 50.4, "war_support": 50.0,
            "population_capacity": 0,
            "tax_level": "Средний",
        },
        "raw": "Граждане: 0 | Солдаты: 0 | Бюджет: 76624",
    },

    "Балтослободия": {
        "country": {
            "population": 60, "army_infantry": 11,
            "budget": 61428.0, "income_daily": 0.0, "expenses_daily": 0.0,
            "stability": 82.6, "happiness": 53.0, "war_support": 50.0,
            "population_capacity": 60,
            "tax_level": "Средний",
            "ruler": "Биллиер Климов",
            "color": "красный",
        },
        "raw": "Граждане: 60 | Солдаты: 11 | Бюджет: 61428",
        "ruler": "Биллиер Климов",
        "color": "красный",
    },

    "Союз Западных Штатов": {
        "country": {
            "population": 91, "army_infantry": 17,
            "budget": 313923.4, "income_daily": 0.0, "expenses_daily": 0.0,
            "stability": 82.6, "happiness": 53.0, "war_support": 50.0,
            "population_capacity": 270,
            "tax_level": "Средний",
            "ruler": "Джордж",
            "color": "3358A1",
        },
        "raw": "Граждане: 91 | Солдаты: 17 | Бюджет: 313923",
        "ruler": "Джордж",
        "color": "3358A1",
    },

}


def manual_stats_for(country: str) -> dict[str, Any] | None:
    """Данные раздела 5 для страны с учётом синонимов."""
    canon = config.normalize_name(country)
    return MANUAL_STATS.get(canon)


def manual_country_names() -> list[str]:
    return list(MANUAL_STATS.keys())


def _filled_from_anketa(row: dict[str, Any]) -> bool:
    """Анкета реально заполняла карточку, а не осталась значением по умолчанию.

    stats_source по умолчанию равен 'anketa' у ЛЮБОЙ строки countries, включая
    пустышки, которые создал enforce_directory. Если считать анкетой каждую
    такую строку, раздел 5 перестанет заполнять вообще ничего: все страны
    справочника приходят именно пустышками. Признак заполненной анкетой -
    accepted_at, который ставит только приём анкеты.
    """
    return bool(row.get("accepted_at"))


async def apply(db, force: bool = False) -> dict[str, list[str]]:
    """Загрузить мануальную статистику в БД.

    force=False (по умолчанию) — не перетирать уже загруженное: правка админа
    ценнее повторного сида из промпта.

    Отдельно важно не затирать анкетные страны. Раздел 5 — это ручные стартовые
    цифры для конкретного списка стран, а карточка, заведённая из анкеты,
    уже получила свои данные из раздела 4. Без этой проверки повторный запуск
    бота на существующей базе возвращал бы анкетные страны к цифрам промпта.
    """
    applied: list[str] = []
    skipped: list[str] = []
    for name, entry in MANUAL_STATS.items():
        country_fields = dict(entry["country"])
        existing = await db.get_country(name)
        if existing is None:
            # Страны может не быть в БД: создаём с правильным источником.
            await db.upsert_country({"name": name, **country_fields})
            existing = await db.get_country(name)

        if existing is not None and not existing.get("accepted_at"):
            # Мануальные страны приняты по определению: без отметки accepted_at
            # карточка получает ложный баннер «АНКЕТА СТРАНЫ НЕ ПРИНЯТА», а
            # _filled_from_anketa() считает заполненными только строки с ней.
            # Ставим её ДО возможного continue ниже, иначе на повторных
            # прогонах (only_if_absent) отметка так и не появится.
            await db._execute(
                "UPDATE countries SET accepted_at=? WHERE name=?",
                (datetime.now(timezone.utc).isoformat(), name),
            )

        if existing is not None and not force:
            source = str(existing.get("stats_source") or "")
            if source == "manual":
                # Уже помечена как manual: цифры могли уехать по новостям, не трогаем.
                saved = await db.save_manual_stats(name, entry, entry["raw"],
                                                   only_if_absent=True)
                if not saved:
                    skipped.append(name)
                    continue
            elif source == "anketa" and _filled_from_anketa(existing):
                # Данные из анкеты (раздел 4) - раздел 5 их не переписывает.
                skipped.append(name)
                log.info("seed_manual: %s пропущена, карточка из анкеты", name)
                continue

        if existing is not None:
            # Include top-level ruler/color from entry if present
            upsert_fields = {"name": name, **country_fields}
            if entry.get("ruler"):
                upsert_fields["ruler"] = entry["ruler"]
            if entry.get("color"):
                upsert_fields["color"] = entry["color"]
            # Set accepted_at for manual countries (protection from directory deletion)
            upsert_fields["accepted_at"] = datetime.now(timezone.utc).isoformat()
            await db.upsert_country(upsert_fields)
        # Колонки карточки — точечно, минуя upsert (см. db.set_card_fields).
        await db.set_card_fields(
            name,
            stats_source="manual",
            ojat=country_fields.get("ojat"),
            tx999=country_fields.get("tx999"),
        )
        await db.save_manual_stats(name, entry, entry["raw"])
        await db.log_change(name, "stats_source", existing.get("stats_source")
                            if existing else "", "manual",
                            "seed_manual: мануальная статистика (раздел 5)")
        applied.append(name)
        log.info("seed_manual: %s загружена", name)
    return {"applied": applied, "skipped": skipped}
