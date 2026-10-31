"""
aiogram 3 handlers for Warlord RP Bot.
"""
from __future__ import annotations
import logging
import math
import re
from aiogram import Router, F, Bot
from aiogram.filters import Command, CommandStart
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.types import (
    Message, ReplyKeyboardRemove, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
    KeyboardButton, ReplyKeyboardMarkup, WebAppInfo,
)
from aiogram.enums import ParseMode

from .config import config, ADMIN_ID, bot_username
from .db import Database
from .security import SecurityGuard, should_respond_to_bot, is_reply_to_bot as _reply_to_bot
from .assistant import Assistant
from .mechanic import Mechanic

log = logging.getLogger(__name__)
router = Router(name="warlord_main")

# Global instances (set in main.py)
db: Database | None = None
security: SecurityGuard | None = None
assistant: Assistant | None = None
mechanic: Mechanic | None = None
bot_instance: Bot | None = None

def setup_handlers(
    _db: Database,
    _security: SecurityGuard,
    _assistant: Assistant,
    _mechanic: Mechanic,
    _bot: Bot
) -> Router:
    """Initialize global instances and return router."""
    global db, security, assistant, mechanic, bot_instance
    db = _db
    security = _security
    assistant = _assistant
    mechanic = _mechanic
    bot_instance = _bot
    return router

# ---------- Helpers ----------
def is_admin(message: Message) -> bool:
    return message.from_user.id == ADMIN_ID

def is_reply_to_bot(message: Message) -> bool:
    """True только если ответ именно на сообщение бота (не на любое сообщение)."""
    return _reply_to_bot(message, bot_instance.id if bot_instance else None)

def extract_command(message: Message) -> str | None:
    text = message.text or ""
    if text.startswith("/"):
        return text.split()[0].lower()
    return None

def mentioned_bot(message: Message) -> bool:
    if not bot_username:
        return False
    return f"@{bot_username}" in (message.text or "")

# ---------- Handlers ----------
@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    await message.answer(
        "🤖 <b>Warlord RP Bot</b>\n\n"
        "Я — ассистент по WARLORD RP. Задавай вопросы по правилам, механике, странам и группировкам.\n\n"
        "Команды:\n"
        "/help — справка\n"
        "/ai &lt;ваш вопрос по WARLORD RP&gt; — спросить ассистента\n\n"
        "Админ-команды (только для админа):\n"
        "/admin — инлайн-панель управления\n"
        "/process — запустить цикл обновления\n"
        "/cards — переопубликовать карточки\n"
        "/newcountry &lt;country|faction&gt; &lt;JSON&gt; — создать страну/группировку\n"
        "/status — статус бота\n"
        "/tokenreport — отчёт по токенам",
        parse_mode=ParseMode.HTML
    )

@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await cmd_start(message)


@router.message(Command("webapp"))
async def cmd_webapp(message: Message) -> None:
    """Открыть веб-редактор стран как Telegram Web App (Mini App)."""
    if not config.WEB_APP_URL:
        await message.answer(
            "⚠️ Веб-приложение не настроено. Администратор должен задать "
            "WEB_APP_URL в переменных окружения.",
            parse_mode=ParseMode.HTML
        )
        return

    kb = ReplyKeyboardMarkup(
        keyboard=[[
            KeyboardButton(
                text="🌐 Открыть веб-редактор стран",
                web_app=WebAppInfo(url=config.WEB_APP_URL)
            )
        ]],
        resize_keyboard=True,
        one_time_keyboard=False,
        input_field_placeholder="Нажмите кнопку, чтобы открыть веб-редактор",
    )
    await message.answer(
        "📊 <b>Веб-редактор стран WARLORD RP</b>\n\n"
        "Нажмите кнопку ниже, чтобы открыть веб-приложение с полной статистикой "
        "стран и возможностью редактирования.\n\n"
        "Изменения сохраняются в общую базу данных и отображаются в карточках "
        "в следующем цикле обновления.",
        reply_markup=kb,
        parse_mode=ParseMode.HTML
    )


@router.message(F.web_app_data)
async def handle_web_app_data(message: Message) -> None:
    """Обработка данных, отправленных из Telegram Web App."""
    if not mechanic:
        return
    try:
        import json
        data = json.loads(message.web_app_data.data or "{}")
        action = data.get("action")

        if action == "edit_country":
            name = data.get("country")
            field = data.get("field")
            value = data.get("value")
            if not all([name, field, value is not None]):
                await message.answer("❌ Некорректные данные из веб-приложения.")
                return

            country = await db.get_country(name)
            if not country:
                await message.answer(f"❌ Страна «{name}» не найдена.")
                return

            # Числовые поля
            numeric_fields = (
                "population", "population_capacity", "budget",
                "income_daily", "expenses_daily",
                "army_infantry", "army_light_vehicles",
                "army_heavy_wheeled", "army_artillery",
                "army_drones", "army_navy",
                "stability", "happiness", "war_support",
            )
            if field in numeric_fields:
                new_val = float(value)
                if field in ("stability", "happiness", "war_support"):
                    new_val = min(100.0, max(0.0, new_val))
                else:
                    new_val = max(0.0, new_val)
                await db.set_country_value(int(country["id"]), field, new_val)
                await db.set_country_override(int(country["id"]), field, new_val)
                await message.answer(
                    f"✅ <b>{name}</b>: {field} = {new_val} (сохранено и заблокировано ⭐)",
                    parse_mode=ParseMode.HTML
                )
            elif field in ("ruler", "party", "ideology", "color", "tax_level", "status"):
                await db.set_country_value(int(country["id"]), field, str(value))
                await message.answer(
                    f"✅ <b>{name}</b>: {field} = {value}",
                    parse_mode=ParseMode.HTML
                )
            else:
                await message.answer(f"❌ Поле «{field}» не поддерживается для редактирования.")
        else:
            await message.answer(f"⚠️ Неизвестное действие из веб-приложения: {action}")

    except Exception as exc:  # noqa: BLE001
        log.exception("web_app_data handler failed: %s", exc)
        await message.answer("⚠️ Ошибка обработки данных из веб-приложения.")

@router.message(Command("ai"))
async def cmd_ai(message: Message) -> None:
    """Direct question to assistant."""
    if not assistant:
        return
    text = message.text or ""
    parts = text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Использование: /ai <ваш вопрос по WARLORD RP>", parse_mode=None)
        return
    # Security check
    sec = await security.check_message(
        message.from_user.id, message.from_user.username, parts[1],
        is_command=True, command="/ai", mentioned_bot=False
    )
    if not sec.allowed:
        if sec.reply_text:
            await message.answer(sec.reply_text)
        return
    try:
        await assistant._answer_question(message, parts[1])
    except Exception as exc:  # noqa: BLE001
        log.warning("AI question failed: %s: %s", type(exc).__name__, exc)
        await message.answer("⚠️ ИИ-модуль недоступен. Попробуйте позже.")

@router.message(Command("status"))
async def cmd_status(message: Message) -> None:
    if not assistant or not is_admin(message):
        return
    await assistant._handle_command(message, "/status")

@router.message(Command("tokenreport"))
async def cmd_tokenreport(message: Message) -> None:
    if not assistant or not is_admin(message):
        return
    await assistant._handle_command(message, "/tokenreport")

@router.message(Command("process"))
async def cmd_process(message: Message) -> None:
    if not mechanic or not is_admin(message):
        return
    await message.answer("🔄 Запускаю цикл обновления...")
    await mechanic.run_cycle()
    await message.answer("✅ Цикл завершён.")

@router.message(Command("cards"))
async def cmd_cards(message: Message) -> None:
    if not mechanic or not is_admin(message):
        return
    await message.answer("🃏 Переопубликовываю карточки...")
    try:
        await mechanic.publish_all_cards(force=True)
    except Exception as exc:  # noqa: BLE001
        log.exception("publish_all_cards failed")
        await message.answer(f"⚠️ Не удалось обновить карточки: {exc}")
        return
    await message.answer("✅ Карточки обновлены.")

@router.message(Command("cleanup"))
async def cmd_cleanup(message: Message) -> None:
    if not mechanic or not is_admin(message):
        return
    text = message.text or ""
    try:
        result = await mechanic.handle_admin_command(text)
    except Exception as exc:  # noqa: BLE001
        log.exception("cleanup failed")
        await message.answer(f"⚠️ Не удалось выполнить cleanup: {exc}")
        return
    await message.answer(result)

@router.message(Command("newcountry"))
async def cmd_newcountry(message: Message) -> None:
    if not mechanic or not is_admin(message):
        return
    text = message.text or ""
    result = await mechanic.handle_admin_command(text)
    await message.answer(result)

@router.message(Command("unreg"))
@router.message(Command("anreg"))
async def cmd_unreg(message: Message) -> None:
    if not mechanic or not is_admin(message):
        return
    result = await mechanic.handle_admin_command(message.text or "")
    await message.answer(result)

@router.message(Command("afk"))
async def cmd_afk(message: Message) -> None:
    """Перевести страну в АФК: только её правитель или админ."""
    if not mechanic:
        return
    arg = (message.text or "").split(maxsplit=1)
    if len(arg) < 2 or not arg[1].strip():
        await _afk_post(message, "Использование: /afk <название страны>",
                        delete=False)
        return
    name = arg[1].strip()
    country = await _find_country(name)
    if not country:
        await _afk_post(message, f"Страна «{name}» не найдена.", delete=True)
        return
    if not is_admin(message) and not _ruler_allows(country, message.from_user.id):
        await _afk_post(
            message,
            f"❌ Чужую страну переводить в АФК нельзя: это может только "
            f"правитель «{country['name']}» или админ.",
            delete=True,
        )
        return
    result = await mechanic.afk_country(country["name"])
    await _afk_post(message, result + "\nВыход из АФК: /unafk <название страны>",
                    delete=True)


@router.message(Command("active"))
@router.message(Command("unafk"))
async def cmd_active(message: Message) -> None:
    """Вернуть страну из АФК: только её правитель или админ."""
    if not mechanic:
        return
    arg = (message.text or "").split(maxsplit=1)
    if len(arg) < 2 or not arg[1].strip():
        await _afk_post(message, "Использование: /unafk <название страны>",
                        delete=False)
        return
    name = arg[1].strip()
    country = await _find_country(name)
    if not country:
        await _afk_post(message, f"Страна «{name}» не найдена.", delete=True)
        return
    if not is_admin(message) and not _ruler_allows(country, message.from_user.id):
        await _afk_post(
            message,
            f"❌ Вернуть из АФК можно только свою страну (правитель "
            f"«{country['name']}» или админ).",
            delete=True,
        )
        return
    result = await mechanic.activate_country(country["name"])
    await _afk_post(message, result, delete=True)


def _ruler_allows(country: dict, user_id: int) -> bool:
    """Только правитель страны (или админ) управляет её статусом."""
    try:
        ruler_id = int(country.get("ruler_user_id") or 0)
    except (TypeError, ValueError):
        ruler_id = 0
    return bool(ruler_id) and ruler_id == user_id


async def _find_country(name: str) -> dict | None:
    """Найти страну по точному имени или алиасу (СШФ → Соединённые Штаты...)."""
    found = await db.get_country(name)
    if found:
        return found
    needle = (name or "").strip().lower()
    if not needle:
        return None
    for c in await db.list_countries():
        names = {str(c["name"]).lower()}
        names.update(str(a).lower() for a in (c.get("aliases_json") or []))
        if needle in names:
            return c
    return None


async def _afk_post(message: Message, text: str, delete: bool) -> None:
    """Ответить на /afk: в общем форуме удалить команду и написать результат."""
    if delete and message.chat.type in ("supergroup", "channel"):
        try:
            await message.delete()
        except Exception as exc:  # noqa: BLE001 - удаление не критично
            log.debug("не удалось удалить сообщение %s: %s", message.message_id, exc)
    try:
        await message.answer(text)
    except Exception as exc:  # noqa: BLE001
        log.warning("не удалось ответить на /afk: %s", exc)

@router.message(Command("mute"))
async def cmd_mute(message: Message) -> None:
    if not is_admin(message):
        return
    parts = (message.text or "").split(maxsplit=2)
    if len(parts) < 2:
        await message.answer("Использование: /mute <user_id> [часы]")
        return
    try:
        uid = int(parts[1])
        hours = int(parts[2]) if len(parts) > 2 else 1
        await security.mute_user(uid, hours)
        await message.answer(f"🔇 Пользователь {uid} замьючен на {hours} ч.")
    except ValueError:
        await message.answer("Неверный user_id или часы.")

@router.message(Command("unmute"))
async def cmd_unmute(message: Message) -> None:
    if not is_admin(message):
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Использование: /unmute <user_id>")
        return
    try:
        uid = int(parts[1])
        if await security.unmute_user(uid):
            await message.answer(f"🔊 Пользователь {uid} размьючен.")
        else:
            await message.answer("Пользователь не в муте.")
    except ValueError:
        await message.answer("Неверный user_id.")

@router.message(Command("security"))
async def cmd_security(message: Message) -> None:
    if not is_admin(message):
        return
    rows = await db.fetchall(
        "SELECT user_id, username, reason, timestamp, text FROM security_log ORDER BY id DESC LIMIT 20"
    )
    if not rows:
        await message.answer("Лог безопасности пуст.")
        return
    lines = ["🛡 <b>Security Log (последние 20):</b>"]
    for r in rows:
        lines.append(
            f"• <code>{r['user_id']}</code> @{r['username'] or '?'} — {r['reason']} — {r['timestamp'][:19]}"
        )
    await message.answer("\n".join(lines), parse_mode=ParseMode.HTML)

# ---------- Inline admin panel ----------

def _admin_panel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Цикл обновления", callback_data="admin:process")],
        [InlineKeyboardButton(text="Переопубликовать карточки",
                              callback_data="admin:cards")],
        [
            InlineKeyboardButton(text="Статус", callback_data="admin:status"),
            InlineKeyboardButton(text="Токены", callback_data="admin:tokenreport"),
        ],
        [InlineKeyboardButton(text="Лог безопасности",
                              callback_data="admin:security")],
        [InlineKeyboardButton(text="Редактор стран",
                              callback_data="admin:edit:0")],
        [
            InlineKeyboardButton(text="⭐ всем",
                                 callback_data="admin:star_all:1"),
            InlineKeyboardButton(text="Снять ⭐ со всех",
                                 callback_data="admin:unstar_all:1"),
        ],
        [InlineKeyboardButton(text="Очистка (план)", callback_data="admin:cleanup")],
    ])


# ---------- Админ: редактор стран ----------
ADMIN_PAGE_SIZE = 8
# id страны, которой админ сейчас пришлёт фото флага (по id админа)
_ADMIN_FLAG_PENDING: dict[int, int] = {}
# приращения для кнопок −/+ (население и армия имеют второй шаг для быстрого роста)
_FIELD_INCREMENTS = {
    "budget": (10000, 2500),
    "income_daily": (1000, 250),
    "expenses_daily": (1000, 250),
    "population": (50, 10),
    "population_capacity": (50, 10),
    "army_infantry": (25, 5),
    "army_light_vehicles": (10, 2),
    "army_heavy_wheeled": (10, 2),
    "army_artillery": (10, 2),
    "army_drones": (10, 2),
    "army_navy": (5, 1),
    "stability": (5, 1),
    "happiness": (5, 1),
    "war_support": (5, 1),
}


def _admin_country_field_label(field: str) -> str:
    labels = {
        "population": "население",
        "population_capacity": "лимит населения",
        "army_infantry": "пехота",
        "army_light_vehicles": "лёгкая техника",
        "army_heavy_wheeled": "тяжёлая техника",
        "army_artillery": "артиллерия",
        "army_drones": "БПЛА",
        "army_navy": "флот",
        "budget": "бюджет",
        "income_daily": "доход/день",
        "expenses_daily": "расходы/день",
        "stability": "стабильность",
        "happiness": "счастье",
        "war_support": "поддержка войны",
    }
    return labels.get(field, field)


async def _admin_countries_list(page: int) -> tuple[str, InlineKeyboardMarkup]:
    countries = await db.list_countries()
    total = len(countries)
    pages = max(1, math.ceil(total / ADMIN_PAGE_SIZE))
    page = min(max(0, page), pages - 1)
    chunk = countries[page * ADMIN_PAGE_SIZE:(page + 1) * ADMIN_PAGE_SIZE]
    lines = [
        f"<b>Редактор стран</b> — всего {total}, страница {page + 1}/{pages}",
        "Флаг + имя + армия. Кнопка открывает меню правок.",
    ]
    rows: list[list[InlineKeyboardButton]] = []
    for c in chunk:
        flag = "🏳️" if c.get("flag_file_id") else "▫️"
        army = int(c.get("army_infantry") or 0)
        lines.append(f"{flag} <b>{c['name']}</b> · армия {army}")
        rows.append([InlineKeyboardButton(
            text=f"{flag} {c['name']} ({army})",
            callback_data=f"admin:co:{page}:{c['id']}")])
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="‹", callback_data=f"admin:edit:{page - 1}"))
    nav.append(InlineKeyboardButton(text="↩ В панель", callback_data="admin:panel"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton(text="›", callback_data=f"admin:edit:{page + 1}"))
    rows.append(nav)
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


def _admin_country_menu_kb(country_id: int, page: int) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for field, (big, small) in _FIELD_INCREMENTS.items():
        rows.append([
            InlineKeyboardButton(text="−", callback_data=f"admin:op:{page}:{country_id}:{field}:{-big}"),
            InlineKeyboardButton(text="−", callback_data=f"admin:op:{page}:{country_id}:{field}:{-small}"),
            InlineKeyboardButton(text=_admin_country_field_label(field), callback_data="admin:noop"),
            InlineKeyboardButton(text="+", callback_data=f"admin:op:{page}:{country_id}:{field}:{small}"),
            InlineKeyboardButton(text="+", callback_data=f"admin:op:{page}:{country_id}:{field}:{big}"),
        ])
    rows.append([
        InlineKeyboardButton(text="Флаг (загрузить фото)", callback_data=f"admin:op:{page}:{country_id}:flag:1"),
        InlineKeyboardButton(text="Переиздать карточку", callback_data=f"admin:op:{page}:{country_id}:publish:1"),
    ])
    rows.append([
        InlineKeyboardButton(text="↩ Назад", callback_data=f"admin:edit:{page}"),
        InlineKeyboardButton(text="Снять ⭐", callback_data=f"admin:op:{page}:{country_id}:unlock:1"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _admin_status_text() -> str:
    countries = await db.list_countries()
    factions = await db.list_factions()
    entities = await db.list_entities()
    questions = await db.list_questions()
    topics = await db.list_topics()
    tokens = assistant.ai._daily_tokens if assistant and assistant.ai else 0
    return (
        "<b>Статус бота</b>\n"
        f"Стран: {len(countries)}\n"
        f"Группировок: {len(factions)}\n"
        f"Сущностей: {len(entities)}\n"
        f"Вопросов админу: {len(questions)}\n"
        f"Тем на форуме: {len(topics)}\n"
        f"Провайдер ИИ: {config.AI_PROVIDER}\n"
        f"Токенов за день: {tokens}"
    )


async def _admin_security_text() -> str:
    rows = await db.fetchall(
        "SELECT user_id, username, reason, timestamp FROM security_log "
        "ORDER BY id DESC LIMIT 20"
    )
    if not rows:
        return "Лог безопасности пуст."
    lines = ["<b>Лог безопасности (последние 20)</b>"]
    for r in rows:
        lines.append(
            f"• <code>{r['user_id']}</code> @{r['username'] or '?'} — "
            f"{r['reason']} — {r['timestamp'][:19]}"
        )
    return "\n".join(lines)


@router.message(Command("admin"))
async def cmd_admin(message: Message) -> None:
    if not is_admin(message):
        return
    await message.answer(
        "<b>Админ-панель WARLORD RP</b>\nВыберите действие:",
        reply_markup=_admin_panel_kb(),
        parse_mode=ParseMode.HTML,
    )


@router.callback_query(F.data.startswith("admin:"))
async def admin_panel_callback(cb: CallbackQuery) -> None:
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("Недостаточно прав.", show_alert=True)
        return
    data = cb.data or ""
    action = data.split(":", 1)[1]
    await cb.answer()  # закрыть «часики» до тяжёлой операции
    if not mechanic:
        await cb.message.answer("Механик не инициализирован.")
        return
    try:
        # --- Редактор стран: вернуть основную панель ---
        if action == "panel":
            await cb.message.edit_text(
                "<b>Админ-панель WARLORD RP</b>\nВыберите действие:",
                reply_markup=_admin_panel_kb(), parse_mode=ParseMode.HTML,
            )
            return
        if action == "noop":
            return
        # --- Редактор стран: ⭐ всем / снять ⭐ со всех ---
        if action.startswith("star_all:") or action.startswith("unstar_all:"):
            if action.startswith("star_all:"):
                n = await db.set_all_country_overrides()
                result = (f"✅ Зафиксированы показатели всех стран "
                          f"({n} блокировок). Симуляция заморожена до снятия.")
            else:
                n = await db.clear_all_country_overrides()
                result = (f"✅ Сняты блокировки со всех стран "
                          f"({n} записей). Симуляция снова управляет числами.")
            await cb.message.edit_text(
                "<b>Админ-панель WARLORD RP</b>\n" + result,
                reply_markup=_admin_panel_kb(), parse_mode=ParseMode.HTML,
            )
            return
        # --- Редактор стран: страница списка ---
        if action.startswith("edit:"):
            _, page_s = action.split(":", 1)
            page = int(page_s) if page_s.isdigit() else 0
            text, kb = await _admin_countries_list(page)
            await cb.message.edit_text(text, reply_markup=kb,
                                       parse_mode=ParseMode.HTML)
            return
        # --- Редактор стран: меню одной страны ---
        if action.startswith("co:"):
            _, page_s, cid_s = action.split(":")
            country = await db.get_country_by_id(int(cid_s))
            if not country:
                text = "❌ Страна не найдена."
                kb = _admin_panel_kb()
            else:
                overrides = await db.get_country_overrides(int(cid_s))
                text = await mechanic.admin_country_snapshot(country, overrides)
                kb = _admin_country_menu_kb(int(cid_s), int(page_s or 0))
            await cb.message.edit_text(text, reply_markup=kb,
                                       parse_mode=ParseMode.HTML)
            return
        # --- Редактор стран: операции над страной ---
        if action.startswith("op:"):
            parts = action.split(":")
            page, cid = int(parts[1]), int(parts[2])
            field, arg = parts[3], (parts[4] if len(parts) > 4 else "")
            result = ""
            if field == "flag":
                _ADMIN_FLAG_PENDING[cb.from_user.id] = cid
                await cb.message.answer(
                    "Отправьте фото флага одним сообщением — привяжу его к стране.")
                return
            if field == "publish":
                country = await db.get_country_by_id(cid)
                if not country:
                    result = "❌ Страна не найдена."
                else:
                    result = await mechanic.republish_country_card(
                        str(country["name"]))
            elif field == "unlock":
                await db.clear_country_overrides(cid)
                result = "✅ Блокировки сняты — симуляция снова управляет числами."
            else:
                try:
                    result = await mechanic.admin_edit_country(
                        cid, field, float(arg))
                except (TypeError, ValueError) as exc:  # noqa: BLE001
                    result = f"❌ Неверное значение: {exc}"
            country = await db.get_country_by_id(cid)
            if not country:
                await cb.message.answer(result, parse_mode=ParseMode.HTML)
                return
            overrides = await db.get_country_overrides(cid)
            snapshot = await mechanic.admin_country_snapshot(country, overrides)
            kb = _admin_country_menu_kb(cid, page)
            await cb.message.edit_text(
                snapshot + "\n\n" + result, reply_markup=kb,
                parse_mode=ParseMode.HTML,
            )
            return
        # --- исходные действия панели ---
        if action == "process":
            await cb.message.answer("Запускаю цикл обновления...")
            await mechanic.run_cycle()
            text = "Цикл завершён."
        elif action == "cards":
            await cb.message.answer("Переопубликовываю карточки...")
            await mechanic.publish_all_cards(force=True)
            text = "Карточки обновлены."
        elif action == "status":
            text = await _admin_status_text()
        elif action == "tokenreport":
            text = (await assistant.ai.daily_token_report()) if assistant else \
                "Ассистент недоступен."
        elif action == "security":
            text = await _admin_security_text()
        elif action == "cleanup":
            text = await mechanic.handle_admin_command("/cleanup")
        else:
            text = "Неизвестное действие."
    except Exception as exc:  # noqa: BLE001
        log.exception("admin panel action %s failed", action)
        text = f"Ошибка: {exc}"
    await cb.message.answer(text, parse_mode=ParseMode.HTML,
                            disable_web_page_preview=True)


@router.message(F.photo, F.from_user.id == ADMIN_ID)
async def admin_flag_photo(message: Message) -> None:
    """Принять фото флага, если админ только что выбрал «Флаг» для страны."""
    country_id = _ADMIN_FLAG_PENDING.pop(message.from_user.id, None)
    if not country_id or not mechanic:
        return
    photo = message.photo[-1]
    text = await mechanic.admin_approve_flag(country_id, photo.file_id)
    await message.answer(text, parse_mode=ParseMode.HTML)


# ---------- Admin reply handler ----------
# Фильтр по админу обязателен: без него этот хендлер перехватывал ЛЮБОЙ ответ
# на сообщение и «съедал» апдейт, поэтому обычные игроки, отвечая реплаем на
# карточку бота, никогда не доходили до ассистента (ответ по реплаю «не работал»).
@router.message(F.reply_to_message, F.from_user.id == ADMIN_ID)
async def admin_reply(message: Message) -> None:
    """Handle admin replies to bot questions."""
    if not message.text:
        return
    if not message.reply_to_message:
        return

    replied_id = message.reply_to_message.message_id
    # Find question by telegram message id (stored in questions_log)
    question = await db.fetchone(
        "SELECT * FROM questions_log WHERE telegram_message_id=? AND answer=''",
        (replied_id,)
    )
    if not question:
        # Ответ админа не на наш вопрос (например, обычный диалог реплаем с
        # ботом). Не глотаем апдейт — отдаём дальше ассистенту.
        raise SkipHandler

    answer_text = message.text.strip()
    await db.answer_question(question["id"], answer_text)
    await db.mark_questions_applied([question["id"]])

    # Apply answer if it's a country/faction field
    qtext = question["question"]
    applied = False

    # Match country missing field: "Страна «X»: не указано (поля)..."
    m = re.search(r"Страна\s*[«\"]([^»\"]+)[»\"]", qtext)
    if m:
        cname = m.group(1).strip()
        country = await db.get_country(cname)
        if country:
            # Try to parse which field
            missing_match = re.search(r"не указано\s*\(([^)]+)\)", qtext)
            if missing_match:
                fields = [f.strip() for f in missing_match.group(1).split(",")]
                updates = {}
                for field in fields:
                    field = field.lower()
                    if "флаг" in field and answer_text:
                        updates["flag_file_id"] = answer_text
                    elif "правител" in field and answer_text:
                        updates["ruler"] = answer_text
                    elif "цвет" in field and answer_text:
                        updates["color"] = answer_text
                    elif "посел" in field and answer_text:
                        # parse settlements
                        pass
                if updates:
                    await db.upsert_country({**country, **updates})
                    applied = True

    # Match faction missing field
    m = re.search(r"Группировка\s*[«\"]([^»\"]+)[»\"]", qtext)
    if m and not applied:
        fname = m.group(1).strip()
        faction = await db.get_faction(fname)
        if faction:
            updates = {}
            if "эмблем" in qtext.lower() and answer_text:
                updates["emblem_file_id"] = answer_text
            elif "командир" in qtext.lower() and answer_text:
                updates["commander"] = answer_text
            elif "базирован" in qtext.lower() and answer_text:
                updates["base_country"] = answer_text
            if updates:
                await db.upsert_faction({**faction, **updates})
                applied = True

    if applied:
        await message.answer("✅ Данные сохранены и применены к карточке.")
    else:
        await message.answer("✅ Ответ принят. Будет применён в следующем цикле.")

# ---------- Main message handler ----------
@router.message(F.text)
async def handle_message(message: Message) -> None:
    """Main message handler - routes to assistant."""
    if not assistant or not security:
        return

    # Skip if already handled by command handlers
    if message.text and message.text.startswith("/"):
        return

    user_id = message.from_user.id
    username = message.from_user.username
    text = message.text or ""
    cmd = extract_command(message)
    mentioned = mentioned_bot(message)
    is_reply = is_reply_to_bot(message)

    # ЖЁСТКИЙ ГЕЙТ: в групповом чате бот молчит, пока к нему не обратились
    # (упоминание @bot, ответ на его сообщение или команда). Раньше флаг
    # is_reply означал «ответ на любое сообщение», и ассистент отвечал всем.
    if not should_respond_to_bot(
        message,
        bot_instance.id if bot_instance else None,
        bot_username,
    ):
        return

    # Security check
    sec = await security.check_message(
        user_id, username, text,
        is_reply_to_bot=is_reply,
        is_command=bool(cmd),
        command=cmd,
        mentioned_bot=mentioned
    )
    if not sec.allowed:
        if sec.reply_text:
            await message.answer(sec.reply_text)
        return

    # Delegate to assistant. AI can be rate-limited or down; the user must still
    # get an answer instead of an unhandled exception in the update handler.
    try:
        await assistant.handle_message(message)
    except Exception as exc:  # noqa: BLE001
        log.warning("assistant failed for %s: %s: %s", user_id, type(exc).__name__, exc)
        try:
            await message.answer(
                "⚠️ ИИ-модуль сейчас недоступен (лимит запросов или сбой провайдера). "
                "Попробуйте через минуту."
            )
        except Exception:  # noqa: BLE001
            pass

# ---------- New country registration from anketa ----------
@router.message(F.chat.id == config.ANKETA_CHAT_ID, F.message_thread_id == config.ANKETA_TOPIC_ID)
async def handle_anketa(message: Message) -> None:
    """Parse new country/faction anketa from dedicated topic."""
    if not message.text:
        return
    # This would be processed by mechanic's anketa loader
    # For now, just log
    log.info("New anketa message in topic: %s", message.text[:100])
