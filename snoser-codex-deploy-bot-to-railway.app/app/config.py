"""
Warlord RP Bot Configuration
Only DeepSeek and Groq providers supported.
"""
from __future__ import annotations
import os
from dataclasses import dataclass
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "warlord.sqlite3"
LOG_DIR = BASE_DIR / "logs"

# Bot
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
TARGET_CHAT_ID = int(os.getenv("TARGET_CHAT_ID", "0"))
TARGET_TOPIC_ID = int(os.getenv("TARGET_TOPIC_ID", "0"))
FACTION_TOPIC_ID = int(os.getenv("FACTION_TOPIC_ID", "0"))

# Anketa source
ANKETA_CHAT_ID = int(os.getenv("ANKETA_CHAT_ID", "-1002446986804"))
ANKETA_TOPIC_ID = int(os.getenv("ANKETA_TOPIC_ID", "65885"))

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

# Token economy
MAX_TOKENS_FAST = 300
MAX_TOKENS_FULL = 800
MAX_HISTORY_MESSAGES = 6
CACHE_TTL_HOURS = 24

# Security
SECURITY_MUTE_THRESHOLD = 3
OFFTOPIC_MUTE_THRESHOLD = 5
MUTE_DURATION_HOURS = 1
OFFTOPIC_MUTE_DURATION_MINUTES = 30

# Validation thresholds
MIN_POPULATION = 60
MAX_POPULATION_DROP_PCT = 50
MIN_ARMY_START = 25
MAX_ARMY_START = 50
ARMY_POPULATION_RATIO = 0.3

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
    
    # Token economy
    MAX_TOKENS_FAST = MAX_TOKENS_FAST
    MAX_TOKENS_FULL = MAX_TOKENS_FULL
    MAX_HISTORY_MESSAGES = MAX_HISTORY_MESSAGES
    CACHE_TTL_HOURS = CACHE_TTL_HOURS
    
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
    
    # Paths
    BASE_DIR = BASE_DIR
    DATA_DIR = DATA_DIR
    DB_PATH = DB_PATH
    LOG_DIR = LOG_DIR

config = Config()