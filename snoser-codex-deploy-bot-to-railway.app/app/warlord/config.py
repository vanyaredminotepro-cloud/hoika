"""
Warlord RP Bot Configuration
Only DeepSeek and Groq providers supported.
"""
from __future__ import annotations
import os
import re
from dataclasses import dataclass
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
LOG_DIR = BASE_DIR / "logs"

# Project root is two levels up: app/warlord -> app -> <root>
PROJECT_ROOT = BASE_DIR.parent.parent

def _resolve_db_path() -> Path:
    """Pick the populated DB. Explicit env var wins, then known locations."""
    env = os.getenv("WARLORD_DB_PATH", "").strip()
    if env:
        return Path(env)
    candidates = [
        PROJECT_ROOT / "warlord_data" / "warlord.sqlite3",
        PROJECT_ROOT / "data" / "warlord.sqlite3",
        DATA_DIR / "warlord.sqlite3",
    ]
    best, best_size = None, -1
    for c in candidates:
        try:
            if not c.exists():
                continue
            size = c.stat().st_size
        except OSError:
            continue
        if size > best_size:
            best, best_size = c, size
    return best or (DATA_DIR / "warlord.sqlite3")

DB_PATH = _resolve_db_path()

# Bot
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
TARGET_CHAT_ID = int(os.getenv("TARGET_CHAT_ID", "0"))
TARGET_TOPIC_ID = int(os.getenv("TARGET_TOPIC_ID", "0"))
FACTION_TOPIC_ID = int(os.getenv("FACTION_TOPIC_ID", "0"))

# Anketa source
ANKETA_CHAT_ID = int(os.getenv("ANKETA_CHAT_ID", "-1002446986804"))
ANKETA_TOPIC_ID = int(os.getenv("ANKETA_TOPIC_ID", "65885"))
# Вторая тема анкет: там тоже лежат шаблоны стран
ANKETA_TOPIC_ID_2 = int(os.getenv("ANKETA_TOPIC_ID_2", "151110"))

# Куда бот задаёт вопросы админу в ЛС (0 = fallback на ADMIN_ID/TARGET_CHAT_ID)
ADMIN_QUESTIONS_CHAT = int(os.getenv("ADMIN_QUESTIONS_CHAT", "0") or 0)

# Темы с новостями, которые нужно читать (используется как fallback,
# если темы не найдены в БД при discovery)
NEWS_TOPICS: dict[str, int] = {
    "новости стран": 65881,
    "войны стран": 83064,
    "расширения": 70799,
}

# Telethon userbot (needed to read forum history - bots cannot read history)
TG_API_ID = int(os.getenv("TG_API_ID", "0") or 0)
TG_API_HASH = os.getenv("TG_API_HASH", "")
SESSION_NAME = os.getenv("SESSION_NAME", "warlord_userbot")
TG_SESSION_STRING = os.getenv("TG_SESSION_STRING", "")

# AI Provider: "deepseek" or "groq"
AI_PROVIDER = os.getenv("AI_PROVIDER", "deepseek").lower()

# DeepSeek
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
DEEPSEEK_MODEL_FAST = os.getenv("DEEPSEEK_MODEL_FAST", "deepseek-chat")

# Groq
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_BASE_URL = os.getenv("GROQ_BASE_URL", "https://api.groq.com/openai/v1")
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
GROQ_MODEL_FAST = os.getenv("GROQ_MODEL_FAST", "llama-3.1-8b-instant")

# Scheduler
UPDATE_INTERVAL_HOURS = 2
MAX_NEWS_CHARS = 8000
MAX_MESSAGES_PER_TOPIC = 100
CARD_PUBLISH_INTERVAL_HOURS = 2

# Telegram Web App (Mini App) URL для встраивания внутри бота
WEB_APP_URL = os.getenv("WEB_APP_URL", "")

# Token economy
MAX_TOKENS_FAST = 300
MAX_TOKENS_FULL = 800
MAX_TOKENS_ANKETA = int(os.getenv("MAX_TOKENS_ANKETA", "1600"))
MAX_HISTORY_MESSAGES = 6
CACHE_TTL_HOURS = 24

# Троттлинг AI-провайдера. Провайдер отвечает "Request waited too long for this
# API key's limit" при параллельных запросах, поэтому по умолчанию строго
# последовательно с паузой между вызовами.
AI_MAX_CONCURRENT = int(os.getenv("AI_MAX_CONCURRENT", "1"))
AI_MIN_INTERVAL = float(os.getenv("AI_MIN_INTERVAL", "3.0"))
AI_MAX_RETRIES = int(os.getenv("AI_MAX_RETRIES", "4"))
AI_TIMEOUT = float(os.getenv("AI_TIMEOUT", "60"))
# Общий бюджет времени на один AI-вызов (включая ретраи), чтобы недоступный
# провайдер не подвешивал весь цикл обновления.
AI_CALL_DEADLINE = float(os.getenv("AI_CALL_DEADLINE", "90"))

# Цикл обновления карточек
# Пока карточек мало, бот работает в быстром режиме (минуты), иначе — по часам.
FIRST_CYCLE_DELAY = float(os.getenv("FIRST_CYCLE_DELAY", "60"))
UPDATE_INTERVAL_MINUTES = float(os.getenv("UPDATE_INTERVAL_MINUTES", "5"))
CARDS_READY_THRESHOLD = int(os.getenv("CARDS_READY_THRESHOLD", "10"))

# Security
SECURITY_MUTE_THRESHOLD = 3
OFFTOPIC_MUTE_THRESHOLD = 5
MUTE_DURATION_HOURS = 1
OFFTOPIC_MUTE_DURATION_MINUTES = 30

# Activity (п.8 ТЗ): «мёртвая» страна определяется ДНЯМИ без постов, а не часами.
ACTIVITY_ACTIVE_MAX_DAYS = 3         # <=3 дней без постов -> active
ACTIVITY_INACTIVE_AFTER_DAYS = 7     # >3 и <=7 дней без постов -> inactive
ACTIVITY_AFK_AFTER_DAYS = 30         # >7 и <=30 дней -> afk
ACTIVITY_NEUTRAL_AFTER_DAYS = 30     # >30 дней -> neutral: обнуление, карточка скрыта

# АФК-штрафы (п.8, раздел 3 AFK)
AFK_DAILY_EXPENSES = True            # ежедневное списание содержания
AFK_STABILITY_FLOOR_PCT = 25         # при минусе 3 дня подряд стабильность < 25%
AFK_STABILITY_DROP_DAYS = 3          # сколько дней минуса нужно до штрафа
AFK_DESERTION_WEEKLY_PCT = 5         # -5% армии в неделю (по решению админа)
AFK_DESERTION_AUTO = False           # по умолчанию только по решению админа
AFK_RETURN_STABILITY_PENALTY = 10    # afk -> active: штраф стабильности -10%

# Старые (часовые) пороги. Используются толькоACTIVITY_MODEL="hours".
# Оставлены для отката: при ACTIVITY_MODEL="days" игнорируются.
ACTIVITY_MODEL = os.getenv("ACTIVITY_MODEL", "days").lower()  # "days" | "hours"
ACTIVITY_ACTIVE_WINDOW_H = 2       # legacy: есть новости за 2ч -> active
ACTIVITY_NEUTRAL_AFTER_H = 2       # legacy: нет новостей 2ч -> neutral
ACTIVITY_INACTIVE_MAX_NEWS = 3     # legacy: 1-3 новости за окно -> inactive
ACTIVITY_MIN_ARTICLES_FOR_ACTIVE = 2  # legacy: меньше статей - страна не «раскручена»

# Статусы стран (п.8 ТЗ). Каноническое имя — not_accepted.
# not_registered оставлен как синоним старого значения, чтобы не ломать
# уже записанные в БД строки.
ACTIVITY_STATUSES = ("active", "inactive", "afk", "neutral",
                     "data_error", "not_accepted")
# Псевдонимы, которые тоже означают «страна ещё не принята».
NOT_ACCEPTED_ALIASES = ("not_accepted", "not_registered")

# Статусы, при которых рост показателей замораживается.
FROZEN_STATUSES = ("inactive", "afk", "neutral", "anreg", "unreg",
                   "not_registered", "not_accepted", "data_error")

# Статусы, при которых карточку страны публиковать/обновлять нельзя.
# inactive/afk СЮДА НЕ входят: по п.13 их карточки сохраняются с пометкой,
# neutral — единственная «мёртвая» страна без публикации, плюс анрег/сломанные.
UNPUBLISHABLE_STATUSES = ("neutral", "data_error")

# Статусы, у которых карточка публикуется ДАЖЕ при малом населении —
# всегда с баннером статуса (АНРЕГНУТА / ЗАМОРОЖЕНА / анкета не принята).
STATUS_BANNERED = ("not_accepted", "not_registered", "unreg", "anreg",
                   "inactive", "afk")

# ═══════════════════════════════ МОДУЛЬ CARDS (разделы 2,3,9,10) ══════════
# "edit"    — редактируем существующее сообщение (по умолчанию)
# "replace" — удаляем старое и публикуем новое
CARD_MODE = os.getenv("CARD_MODE", "edit").lower()
# Сообщение старше этого возраста редактировать нельзя -> fallback на новое.
CARD_EDIT_MAX_AGE_H = int(os.getenv("CARD_EDIT_MAX_AGE_H", "48"))
# Максимальный возраст карточки, при котором она вообще считается живой.
CARD_STALE_AFTER_H = int(os.getenv("CARD_STALE_AFTER_H", "48"))
# Длина поля «Итоги за 2 часа» (литературный пересказ LLM).
CARD_SUMMARY_MAX_CHARS = int(os.getenv("CARD_SUMMARY_MAX_CHARS", "400"))
# Сколько элементов показывать в списках инфраструктуры до сворачивания.
CARD_LIST_LIMIT = int(os.getenv("CARD_LIST_LIMIT", "12"))
# Порог «существенного изменения» для уведомления правителю (п.10, уровень 3).
NOTIFY_RULERS = os.getenv("NOTIFY_RULERS", "true").lower() in ("1", "true", "yes", "on")
RULER_BUDGET_PCT = float(os.getenv("RULER_BUDGET_PCT", "10"))
RULER_ARMY_PCT = float(os.getenv("RULER_ARMY_PCT", "20"))
RULER_STABILITY_PCT = float(os.getenv("RULER_STABILITY_PCT", "5"))
# Время ежедневного отчёта админу (п.10, уровень 4), локальное время.
DAILY_REPORT_TIME = os.getenv("DAILY_REPORT_TIME", "09:00")
# Уведомления в тему карточек включаются отдельно от личных.
NOTIFY_TOPIC = os.getenv("NOTIFY_TOPIC", "true").lower() in ("1", "true", "yes", "on")

# ОЯТ и ТХ-999 — только Паониция (разделы 2 и 5).
# Больше ни одна страна не может их менять (п.13).
OJET_COUNTRY = os.getenv("OJET_COUNTRY", "Паониция")
# Единственная страна с ОЯТ в карточке.
OJET_DISPLAY_NAME = "ОЯТ"

# Проверки перед публикацией (раздел 11)
MIN_POPULATION = 60
MAX_POPULATION_DROP_PCT = 50
MIN_ARMY_START = 25
MAX_ARMY_START = 50
ARMY_POPULATION_RATIO = 1.0
# Сколько дней подряд отрицательного баланса дают штраф.
NEGATIVE_BUDGET_DAYS = 3
# Слова-признаки мобилизации (п.11, Army).
MOBILIZATION_MARKERS = (
    "мобилизац", "набор", "призыв", "рекрутинг", "военный набор",
    "призывн", "конскрипц", "добровольц",
)


@dataclass(frozen=True)
class ProviderConfig:
    name: str
    api_key: str
    base_url: str
    model_full: str
    model_fast: str

    @property
    def is_valid(self) -> bool:
        return bool(self.api_key)

PROVIDERS = {
    "deepseek": ProviderConfig(
        name="deepseek",
        api_key=DEEPSEEK_API_KEY,
        base_url=DEEPSEEK_BASE_URL,
        model_full=DEEPSEEK_MODEL,
        model_fast=DEEPSEEK_MODEL_FAST,
    ),
    "groq": ProviderConfig(
        name="groq",
        api_key=GROQ_API_KEY,
        base_url=GROQ_BASE_URL,
        model_full=GROQ_MODEL,
        model_fast=GROQ_MODEL_FAST,
    ),
}

def get_provider_config(name: str | None = None) -> ProviderConfig:
    name = (name or AI_PROVIDER).lower()
    if name not in PROVIDERS:
        raise ValueError(f"Unknown provider: {name}. Use 'deepseek' or 'groq'")
    cfg = PROVIDERS[name]
    if not cfg.is_valid:
        raise RuntimeError(f"Provider {name} not configured: missing API key")
    return cfg

# Config object for easy import
class Config:
    # Bot
    BOT_TOKEN = BOT_TOKEN
    ADMIN_ID = ADMIN_ID
    TARGET_CHAT_ID = TARGET_CHAT_ID
    TARGET_TOPIC_ID = TARGET_TOPIC_ID
    FACTION_TOPIC_ID = FACTION_TOPIC_ID
    ANKETA_CHAT_ID = ANKETA_CHAT_ID
    ANKETA_TOPIC_ID = ANKETA_TOPIC_ID
    ANKETA_TOPIC_ID_2 = ANKETA_TOPIC_ID_2

    # Userbot (Telethon)
    TG_API_ID = TG_API_ID
    TG_API_HASH = TG_API_HASH
    SESSION_NAME = SESSION_NAME
    TG_SESSION_STRING = TG_SESSION_STRING
    session_name = SESSION_NAME
    tg_session_string = TG_SESSION_STRING
    tg_api_id = TG_API_ID
    tg_api_hash = TG_API_HASH

    # AI
    AI_PROVIDER = AI_PROVIDER
    DEEPSEEK_API_KEY = DEEPSEEK_API_KEY
    DEEPSEEK_BASE_URL = DEEPSEEK_BASE_URL
    DEEPSEEK_MODEL = DEEPSEEK_MODEL
    DEEPSEEK_MODEL_FAST = DEEPSEEK_MODEL_FAST
    GROQ_API_KEY = GROQ_API_KEY
    GROQ_BASE_URL = GROQ_BASE_URL
    GROQ_MODEL = GROQ_MODEL
    GROQ_MODEL_FAST = GROQ_MODEL_FAST
    
    # Scheduler
    UPDATE_INTERVAL_HOURS = UPDATE_INTERVAL_HOURS
    MAX_NEWS_CHARS = MAX_NEWS_CHARS
    MAX_MESSAGES_PER_TOPIC = MAX_MESSAGES_PER_TOPIC
    CARD_PUBLISH_INTERVAL_HOURS = CARD_PUBLISH_INTERVAL_HOURS

    # Нижний регистр — для кода, портированного из старого scheduler
    update_interval_hours = UPDATE_INTERVAL_HOURS
    max_messages_per_topic = MAX_MESSAGES_PER_TOPIC
    card_publish_interval_hours = CARD_PUBLISH_INTERVAL_HOURS
    admin_questions_chat = ADMIN_QUESTIONS_CHAT
    news_topics = NEWS_TOPICS
    
    # Token economy
    MAX_TOKENS_FAST = MAX_TOKENS_FAST
    MAX_TOKENS_FULL = MAX_TOKENS_FULL
    MAX_TOKENS_ANKETA = MAX_TOKENS_ANKETA
    MAX_HISTORY_MESSAGES = MAX_HISTORY_MESSAGES
    CACHE_TTL_HOURS = CACHE_TTL_HOURS
    AI_MAX_CONCURRENT = AI_MAX_CONCURRENT
    AI_MIN_INTERVAL = AI_MIN_INTERVAL
    AI_MAX_RETRIES = AI_MAX_RETRIES
    AI_TIMEOUT = AI_TIMEOUT
    AI_CALL_DEADLINE = AI_CALL_DEADLINE
    FIRST_CYCLE_DELAY = FIRST_CYCLE_DELAY
    UPDATE_INTERVAL_MINUTES = UPDATE_INTERVAL_MINUTES
    CARDS_READY_THRESHOLD = CARDS_READY_THRESHOLD
    
    # Security
    SECURITY_MUTE_THRESHOLD = SECURITY_MUTE_THRESHOLD
    OFFTOPIC_MUTE_THRESHOLD = OFFTOPIC_MUTE_THRESHOLD
    MUTE_DURATION_HOURS = MUTE_DURATION_HOURS
    OFFTOPIC_MUTE_DURATION_MINUTES = OFFTOPIC_MUTE_DURATION_MINUTES
    
    # Validation
    MIN_POPULATION = MIN_POPULATION
    MAX_POPULATION_DROP_PCT = MAX_POPULATION_DROP_PCT
    MIN_ARMY_START = MIN_ARMY_START
    MAX_ARMY_START = MAX_ARMY_START
    ARMY_POPULATION_RATIO = ARMY_POPULATION_RATIO

    # Activity (п.8 ТЗ). Дублируем на объект config: cards.py импортирует
    # именно объект, а activity.py - модуль, и оба должны видеть значения.
    ACTIVITY_MODEL = ACTIVITY_MODEL
    ACTIVITY_ACTIVE_MAX_DAYS = ACTIVITY_ACTIVE_MAX_DAYS
    ACTIVITY_INACTIVE_AFTER_DAYS = ACTIVITY_INACTIVE_AFTER_DAYS
    ACTIVITY_AFK_AFTER_DAYS = ACTIVITY_AFK_AFTER_DAYS
    ACTIVITY_NEUTRAL_AFTER_DAYS = ACTIVITY_NEUTRAL_AFTER_DAYS
    AFK_DAILY_EXPENSES = AFK_DAILY_EXPENSES
    AFK_STABILITY_FLOOR_PCT = AFK_STABILITY_FLOOR_PCT
    AFK_STABILITY_DROP_DAYS = AFK_STABILITY_DROP_DAYS
    AFK_DESERTION_WEEKLY_PCT = AFK_DESERTION_WEEKLY_PCT
    AFK_DESERTION_AUTO = AFK_DESERTION_AUTO
    AFK_RETURN_STABILITY_PENALTY = AFK_RETURN_STABILITY_PENALTY
    ACTIVITY_STATUSES = ACTIVITY_STATUSES
    NOT_ACCEPTED_ALIASES = NOT_ACCEPTED_ALIASES
    FROZEN_STATUSES = FROZEN_STATUSES
    UNPUBLISHABLE_STATUSES = UNPUBLISHABLE_STATUSES
    STATUS_BANNERED = STATUS_BANNERED
    # legacy
    ACTIVITY_ACTIVE_WINDOW_H = ACTIVITY_ACTIVE_WINDOW_H
    ACTIVITY_NEUTRAL_AFTER_H = ACTIVITY_NEUTRAL_AFTER_H
    ACTIVITY_INACTIVE_MAX_NEWS = ACTIVITY_INACTIVE_MAX_NEWS
    ACTIVITY_MIN_ARTICLES_FOR_ACTIVE = ACTIVITY_MIN_ARTICLES_FOR_ACTIVE

    # Cards (разделы 2, 3, 9, 10)
    CARD_MODE = CARD_MODE
    CARD_EDIT_MAX_AGE_H = CARD_EDIT_MAX_AGE_H
    CARD_STALE_AFTER_H = CARD_STALE_AFTER_H
    CARD_SUMMARY_MAX_CHARS = CARD_SUMMARY_MAX_CHARS
    CARD_LIST_LIMIT = CARD_LIST_LIMIT
    NOTIFY_RULERS = NOTIFY_RULERS
    RULER_BUDGET_PCT = RULER_BUDGET_PCT
    RULER_ARMY_PCT = RULER_ARMY_PCT
    RULER_STABILITY_PCT = RULER_STABILITY_PCT
    DAILY_REPORT_TIME = DAILY_REPORT_TIME
    NOTIFY_TOPIC = NOTIFY_TOPIC
    OJET_COUNTRY = OJET_COUNTRY
    OJET_DISPLAY_NAME = OJET_DISPLAY_NAME
    NEGATIVE_BUDGET_DAYS = NEGATIVE_BUDGET_DAYS
    MOBILIZATION_MARKERS = MOBILIZATION_MARKERS


    # Paths
    BASE_DIR = BASE_DIR
    PROJECT_ROOT = PROJECT_ROOT
    DATA_DIR = DATA_DIR
    DB_PATH = DB_PATH
    db_path = DB_PATH
    LOG_DIR = LOG_DIR

    # Lowercase aliases (convenience access, e.g. config.admin_id)
    bot_token = BOT_TOKEN
    admin_id = ADMIN_ID
    target_chat_id = TARGET_CHAT_ID
    target_topic_id = TARGET_TOPIC_ID
    faction_topic_id = FACTION_TOPIC_ID
    anketa_chat_id = ANKETA_CHAT_ID
    anketa_topic_id = ANKETA_TOPIC_ID
    ai_provider = AI_PROVIDER
    update_interval_hours = UPDATE_INTERVAL_HOURS

# Runtime variable for bot username (set in main.py after get_me())
bot_username: str | None = None

config = Config()

# ================================================================== СПРАВОЧНИК СТРАН
#
# Единственный источник правды по составу проекта (п.3 ТЗ).
#
# ВАЖНО, как это работает на живых данных:
#   * RENAMES — страны, ПЕРЕИМЕНОВАННЫЕ по ходу проекта. Их старые названия
#     остаются в БД как синонимы, а сама страна сливается в каноническое имя
#     ВМЕСТЕ со всей историей, хроникой и армией.
#   * ANREG — страны, которых больше не существует. Помечаются анрег, карточка
#     не публикуется, история сохраняется.
#   * Всё остальное, чего нет в DIRECTORY и в RENAMES, считается лишним и
#     удаляется по решению админа.
#
# Канонические имена пишутся ровно так, как должны отображаться в карточке.

DIRECTORY: tuple[str, ...] = (
    "Соединённые Штаты Федерации",
    "Республика Белоярск",
    "Нордстан",
    "Республика Регио-Декала",
    "Республика Сербина",
    "Республика Новослободск",
    "Румия",
    "N.I.C.I.L.U.S.",
    "Конфедерация Республик Гнева",
    "Пупсонская Федерация",
    "Республика Линьфа",
    "Союз Западных Штатов",
    "Югоречие",
    "Агесия",
    "Олбония",
    "Паониция",
    "ЗХАД — Западный Хавен-аль-Джихад",
    "Русское Национальное Государство",
    "Антония",
    "Шумадина и Поморавль",
    "Гулания",
    "Балтослободия",
)

# Внутренние автономии: не самостоятельные страны, но вопросы по ним допустимы.
AUTONOMIES: dict[str, str] = {
    "Автономия Сербии": "Республика Сербина",
}

# Переименования: старое имя -> каноническое. Страна сливается в каноническое
# имя со всей историей, хроникой, новостями и армией (объединение, не сложение:
# население берётся большее, а не сумма — это один и тот же объект).
RENAMES: dict[str, str] = {
    "Федерация МОС": "Соединённые Штаты Федерации",
    "МОС": "Соединённые Штаты Федерации",
    "Республика МОС": "Соединённые Штаты Федерации",
    "Республика Регио Декала": "Республика Регио-Декала",
    "КРГ (Конфедерация Республик Гнева)": "Конфедерация Республик Гнева",
    "КРГ": "Конфедерация Республик Гнева",
    "Пупсонская Республика": "Пупсонская Федерация",
    "Попсонская Федерация": "Пупсонская Федерация",
    "Попсонская Республика": "Пупсонская Федерация",
    "Республика Югоречие": "Югоречие",
    "Агесия (Агэсия)": "Агесия",
    "Белоярск": "Республика Белоярск",
    "Соединённые Западные Штаты": "Союз Западных Штатов",
    "Священная Румская Империя": "Румия",
    "Священная Оккупированная Зона Румии": "Румия",
    "С.О.З.Р": "Румия",
    "Руско́е Национальное государство": "Русское Национальное Государство",
    "Исламский Эмират Хавен-аль-Джихад": "ЗХАД — Западный Хавен-аль-Джихад",
    "ВНР (Восточная Новранская Республика)": "Шумадина и Поморавль",
    "Восточная Новранская Республика": "Шумадина и Поморавль",
    "Норденштадт": "Шумадина и Поморавль",
    # Короткие формы: в постах и анкетах страну называют сокращённо, и без
    # этих записей normalize_name() возвращал «ЗХАД» вместо канона, а карточка
    # создавалась как новая страна-двойник.
    "ЗХАД": "ЗХАД — Западный Хавен-аль-Джихад",
    "Западный Хавен-аль-Джихад": "ЗХАД — Западный Хавен-аль-Джихад",
    "СЗШ": "Союз Западных Штатов",
    "РНГ": "Русское Национальное Государство",
    "Шумадина": "Шумадина и Поморавль",
    "ВНР": "Шумадина и Поморавль",
    "Пустонская Федерация": "Пупсонская Федерация",
    "Норд": "Нордстан",
    "Сербина": "Республика Сербина",
    "Регио-Декала": "Республика Регио-Декала",
    "Новослободск": "Республика Новослободск",
}

# Анрег: страны не существуют. Помечаются, карточка не публикуется.
# СССХС НЕ путать с СЗШ (Союз Западных Штатов) — это разные страны.
# «Хавен-аль-Джихад» — отдельная страна эмирата, а не синоним ЗХАД
# («ЗХАД — Западный Хавен-аль-Джихад» в справочнике — другая страна).
# Держать одно имя и в RENAMES, и в ANREG нельзя: исход переименования
# зависел бы от того, кто проверил раньше, и данные одной страны текли бы
# в карточку другой.
ANREG: tuple[str, ...] = (
    "Республика Лунария",
    "ССШС (Союз Социалистических Штатов Сабзонии)",
    "ССШС",
    "Лунария",
    "Герцогство Хабарова",
    "Хавен-аль-Джихад",
)

# Пары, которые нельзя путать. Используется в промптах ассистента, чтобы он
# не отвечал «ССШС это и есть СЗШ».
CONFUSABLE_PAIRS: tuple[tuple[str, str], ...] = (
    ("ССШС (Союз Социалистических Штатов Сабзонии)", "Союз Западных Штатов"),
    ("МОС", "Соединённые Штаты Федерации"),
    ("Норденштадт", "Шумадина и Поморавль"),
    ("Попсонская", "Пупсонская Федерация"),
)

# Быстрый доступ по нормализованному имени
def _norm(name: str) -> str:
    """Нормализация для сравнения имён: регистр, дефисы, «ё», лишние пробелы."""
    s = (name or "").strip().lower().replace("\u0451", "е")
    s = s.replace("\u2014", "-").replace("\u2013", "-").replace("\u2010", "-")
    s = "".join(ch for ch in s if ch.isalnum() or ch.isspace())
    return " ".join(s.split())


DIRECTORY_NORM: dict[str, str] = {_norm(n): n for n in DIRECTORY}
AUTONOMIES_NORM: dict[str, str] = {_norm(k): v for k, v in AUTONOMIES.items()}
ANREG_NORM: set[str] = {_norm(n) for n in ANREG}
RENAMES_NORM: dict[str, str] = {_norm(k): v for k, v in RENAMES.items()}


def resolve_directory_name(name: str) -> str | None:
    """Каноническое имя справочника для `name` либо None, если страны нет.

    Учитывает переименования: «Федерация МОС» -> «Соединённые Штаты Федерации».
    """
    key = _norm(name)
    if key in DIRECTORY_NORM:
        return DIRECTORY_NORM[key]
    if key in RENAMES_NORM:
        return RENAMES_NORM[key]
    return None


def is_known_country(name: str) -> bool:
    """Страна из справочника (в т.ч. под старым названием)."""
    return resolve_directory_name(name) is not None


def is_anreg(name: str) -> bool:
    return _norm(name) in ANREG_NORM


def anreg_unknown_entities() -> list[str]:
    """ANREG-имена, которых нет ни в DIRECTORY, ни в RENAMES/AUTONOMIES.

    Такое имя проверка справочника никогда не создаст, но `enforce_directory`
    тихо поставит `anreg` любой стране с таким именем, если она появится в БД
    (например, её завёл ИИ или ручной ввод). Печатаем список на старте, чтобы
    канон сверялся глазами, а страна не исчезала из карточек молча.
    """
    known = (set(DIRECTORY) | set(AUTONOMIES) | set(RENAMES)
             | set(RENAMES.values()) | set(MANUAL_STATS_COUNTRIES)
             | set(ANKETA_STATS_COUNTRIES) | set(MANUAL_ONLY_AUTONOMIES))
    return [n for n in ANREG if n not in known]


# ═══════════════════════════ ИСТОЧНИКИ ДАННЫХ (разделы 4, 5, 6) ═══════════
#
# stats_source в таблице countries:
#   "manual" — стартовые и контрольные цифры руками (раздел 5, seed_manual.py)
#   "anketa" — стартовые цифры из анкеты (раздел 6, seed_anketa.py)
#
# Для manual LLM не имеет права менять население/армию/бюджет/вооружение без
# явного поста в новостях. Для anketa старт из анкеты, дальше — по новостям.

# Раздел 5: страны с мануальной статистикой.
MANUAL_STATS_COUNTRIES: tuple[str, ...] = (
    "Пупсонская Федерация",
    "Республика Новослободск",
    "Соединённые Штаты Федерации",
    "Дацберг",
    "Югоречие",
    "Паониция",
    "Агесия",
    "Супер Земля",
    "Русское Национальное Государство",
    "Республика Линьфа",
    "Шумадина и Поморавль",
)

# Раздел 6: страны, старт которых берётся из анкеты.
# Названия приведены к каноническим из DIRECTORY.
ANKETA_STATS_COUNTRIES: tuple[str, ...] = (
    "Республика Белоярск",
    "Нордстан",
    "Республика Регио-Декала",
    "Республика Сербина",
    "Румия",
    "N.I.C.I.L.U.S.",
    "Конфедерация Республик Гнева",
    "Союз Западных Штатов",
    "Олбония",
    "ЗХАД — Западный Хавен-аль-Джихад",
    "Антония",
    "Гулания",
    "Балтослободия",
)

# «Лимфа» — неправильное написание. Канонически — «Линьфа» (раздел 13).
SPELLING_FIXES: dict[str, str] = {
    "лимфа": "Республика Линьфа",
    "лимфа ": "Республика Линьфа",
    "республика лимфа": "Республика Линьфа",
    "буддийская социалистическая республика линьфы": "Республика Линьфа",
}

# Дацберг и Супер Земля описаны в разделе 5, но не входят в DIRECTORY:
# это автономия/объединение, а не самостоятельная страна справочника.
MANUAL_ONLY_AUTONOMIES: tuple[str, ...] = ("Дацберг", "Супер Земля")


def normalize_name(name: str) -> str:
    """Привести произвольное название к каноническому имени страны.

    Порядок: исправление известных опечаток -> справочник -> переименования.
    """
    raw = (name or "").strip()
    fixed = SPELLING_FIXES.get(raw.lower())
    if fixed:
        return fixed
    return resolve_directory_name(raw) or raw


def stats_source_for(name: str) -> str:
    """Какой источник стартовых данных у страны: manual или anketa."""
    canon = normalize_name(name)
    if canon in MANUAL_STATS_COUNTRIES:
        return "manual"
    return "anketa"


# ------------------------------------------------------------------ группировки

# Префиксы, которые не различают группировки: «ДШРГ "Ратич"» и
# «МВО "Ратич"» — это одна и та же группировка, просто записанная по-разному.
_FACTION_PREFIX_RE = re.compile(
    r'^(?:дшрг|мво|чвк|чдкз|цсн|осн|го|пдсн|спн|вс|частный\s+бизнес|'
    r'акционерное\s+общество|ао|фгк|гильдия|—|–|-)\s*',
    re.IGNORECASE,
)

# Потолок численности для смерешённой группировки (по ключу-имени без префикса).
# Ратич: две записи (5 и 350) → 230 солдат по решению владельца.
FACTION_STRENGTH_CAPS: dict[str, int] = {
    "ратич": 230,
}

# Ключи группировок, которые на самом деле одна и та же группировка:
# Айсер и Лингваффен — это Фаузвафф (тот же командир Айсер, та же база Паониция,
# «подчинение»). Владетель просил склеить их численность с Fauzwaff.
FACTION_MERGE_ALIASES: dict[str, str] = {
    "айсер": "fauzwaff",
    "лингваффен": "fauzwaff",
}


def faction_merge_key(name: str) -> str:
    """Ключ, по которому склеиваются дублирующие карточки группировок.

    Снимаем префиксы (ДШРГ/МВО/ЧВК/…), кавычки и регистр:
    «ДШРГ "Ратич"» и «МВО "Ратич"» → «ратич»; «ЧВК "Лингваффен"» и
    «ЧВК Лингваффен» → «лингваффен»; «Fauzwaff» и «ЧВК Fauzwaff» → «fauzwaff».
    Отличающиеся имена (например, «Айсер») остаются отдельными ключами.
    """
    text = (name or "").strip()
    # кавычки всех распространённых видов
    text = re.sub(r'[«»"“”\'`]+', " ", text)
    text = _FACTION_PREFIX_RE.sub("", text)
    text = re.sub(r"\s+", " ", text).strip().lower()
    return text


def faction_canonical_key(name: str) -> str:
    """Канонический ключ склейки с учётом алиасов (Айсер/Лингваффен → Fauzwaff)."""
    return FACTION_MERGE_ALIASES.get(faction_merge_key(name), faction_merge_key(name))


def faction_strength_cap(name: str) -> int | None:
    return FACTION_STRENGTH_CAPS.get(faction_merge_key(name))


# Известные факты о группировках, не выводимые из новостей: ключ merge-key →
# список участий в войнах (добавляются к wars_json при синхронизации).
FACTION_KNOWN_WARS: dict[str, list[dict[str, str]]] = {
    "fauzwaff": [{"name": "Война с Хавен-аль-Джихадом (ИЭХД)"}],
}


