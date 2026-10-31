"""
Warlord RP Bot - Main entry point.
Two modules: Assistant (Module A) and Mechanic (Module B).
"""
from __future__ import annotations
import asyncio
import logging
import signal
import sys
from typing import Any

from aiogram import Bot, Dispatcher
from aiogram.enums import ParseMode
from aiogram.types import (
    BotCommand,
    BotCommandScopeDefault,
    BotCommandScopeAllChatAdministrators,
)

from .config import (
    config, BOT_TOKEN, ADMIN_ID, UPDATE_INTERVAL_HOURS,
    FIRST_CYCLE_DELAY, UPDATE_INTERVAL_MINUTES, CARDS_READY_THRESHOLD,
    anreg_unknown_entities,
)
from .db import Database
from .ai import AIClient
from .security import SecurityGuard
from .assistant import Assistant
from .mechanic import Mechanic
from .userbot import connect_userbot
from .handlers import router, setup_handlers, bot_username
from .logger import setup_logging
from .activity import check_activity
from .directory import enforce_directory

setup_logging()
log = logging.getLogger(__name__)

class WarlordBot:
    def __init__(self):
        self.bot: Bot | None = None
        self.dp: Dispatcher | None = None
        self.db: Database | None = None
        self.ai: AIClient | None = None
        self.security: SecurityGuard | None = None
        self.assistant: Assistant | None = None
        self.mechanic: Mechanic | None = None
        self.userbot: Any | None = None
        self._scheduler_task: asyncio.Task | None = None
        self._shutdown = asyncio.Event()

    async def start(self) -> None:
        """Initialize all components and start bot."""
        log.info("Starting Warlord RP Bot...")

        # Validate config
        if not BOT_TOKEN:
            raise RuntimeError("BOT_TOKEN not set in environment")
        if not ADMIN_ID:
            raise RuntimeError("ADMIN_ID not set in environment")

        # Initialize DB
        self.db = Database()
        await self.db.connect()

        # Справочник стран (п.3 ТЗ). apply=True здесь намеренно: без этого в БД
        # остаются дубли, ассистент и карточки работают с мусорными именами.
        # Сначала логируем план, потом применяем - так в логах видно, что удалено.
        try:
            plan = await enforce_directory(self.db, apply=False)
            log.warning("Справочник стран (план): %s", plan.summary())
            await enforce_directory(self.db, apply=True)
        except Exception as e:
            log.exception("Не удалось привести БД к справочнику: %s", e)

        # Пересчёт активности и население (п.4, п.5 БАГ 1)
        activity_report: dict = {}
        try:
            activity_report = await check_activity(self.db)
        except Exception as e:
            log.exception("Проверка активности не удалась: %s", e)

        # ANREG-имена вне справочника: на старте видно, какие страны канон
        # помечает anreg, хотя справочник их не знает (сверка канона глазами).
        try:
            unknown_anreg = anreg_unknown_entities()
            if unknown_anreg:
                log.warning(
                    "ANREG вне справочника (%d): %s - если такая страна появится "
                    "в БД, enforce_directory поставит ей anreg молча",
                    len(unknown_anreg), ", ".join(unknown_anreg))
        except Exception as e:
            log.exception("Проверка списка ANREG не удалась: %s", e)

        # Initialize AI
        self.ai = AIClient()  # Uses config.AI_PROVIDER
        # Постоянный учёт токенов в token_usage (п.13 ТЗ)
        self.ai.attach_db(self.db)

        # Initialize security
        self.security = SecurityGuard(self.db)

        # Initialize bot and dispatcher
        # No global parse_mode: dynamic content (LLM answers, cards) is sent
        # as plain text. HTML is enabled explicitly only on static messages.
        self.bot = Bot(token=BOT_TOKEN)

        # Get bot username for mention detection
        me = await self.bot.get_me()
        global bot_username
        bot_username = me.username

        self.dp = Dispatcher()

        # Initialize modules
        self.assistant = Assistant(self.db, self.ai, self.security, self.bot)

        # Userbot (Telethon) is required to read forum history - bots cannot.
        self.userbot = await connect_userbot()

        self.mechanic = Mechanic(
            self.db, self.ai, self.assistant, self.bot, self.userbot
        )

        # Setup handlers
        setup_handlers(self.db, self.security, self.assistant, self.mechanic, self.bot)
        self.dp.include_router(router)

        # Initialize mechanic
        await self.mechanic.start()

        # Меню команд: игрокам показываются только их команды; административные
        # (/process, /cards, /status, ...) видны и доступны лишь администраторам.
        try:
            await self._setup_commands()
        except Exception as e:
            log.warning("Не удалось зарегистрировать меню команд: %s", e)

        # Send startup message to admin
        try:
            await self.bot.send_message(
                ADMIN_ID,
                "🚀 <b>Warlord RP Bot запущен</b>\n"
                f"Провайдер: {config.AI_PROVIDER}\n"
                f"Интервал циклов: {UPDATE_INTERVAL_HOURS} ч.",
                parse_mode=ParseMode.HTML
            )
            # Отчёт по странам, которым нужна анкета (БАГ 1)
            broken = activity_report.get("data_error") or []
            if broken:
                await self.bot.send_message(
                    ADMIN_ID,
                    "⚠️ <b>Нужна анкета</b>\n"
                    "У следующих стран население ниже минимума "
                    f"({config.MIN_POPULATION}), показатели заморожены:\n"
                    + "\n".join("• " + n for n in broken),
                    parse_mode=ParseMode.HTML,
                )
            changed = activity_report.get("changed") or []
            if changed:
                await self.bot.send_message(
                    ADMIN_ID,
                    "🧊 <b>Активность пересчитана</b>\n"
                    + "\n".join(f"• {n}: {a} → {b}" for n, a, b in changed[:20]),
                    parse_mode=ParseMode.HTML,
                )
        except Exception as e:
            log.warning("Failed to send startup message: %s", e)

        # Start scheduler
        self._scheduler_task = asyncio.create_task(self._scheduler_loop())

        log.info("Bot started successfully")

    async def _setup_commands(self) -> None:
        """Зарегистрировать команды в меню Telegram.

        Игрокам (default scope) видны только их команды; административные
        (/process, /cards, /status, /tokenreport, /security, /cleanup,
        /newcountry, /unreg, /anreg, /mute, /unmute) — только администраторам.
        """
        player_commands = [
            BotCommand(command="start", description="Запуск бота"),
            BotCommand(command="help", description="Помощь и правила"),
            BotCommand(command="ai", description="Вопрос ассистенту по WARLORD"),
            BotCommand(command="afk", description="Перевести свою страну в АФК"),
            BotCommand(command="unafk", description="Вернуть страну из АФК"),
            BotCommand(command="webapp", description="Веб-редактор стран (Mini App)"),
        ]
        admin_commands = [
            BotCommand(command="process", description="Запустить цикл обновления"),
            BotCommand(command="cards", description="Переопубликовать карточки"),
            BotCommand(command="status", description="Статус бота"),
            BotCommand(command="tokenreport", description="Отчёт по токенам"),
            BotCommand(command="security", description="Лог безопасности"),
            BotCommand(command="cleanup", description="План очистки БД"),
            BotCommand(command="newcountry", description="Создать страну/группировку"),
            BotCommand(command="unreg", description="Анрег страны"),
            BotCommand(command="mute", description="Замьютить пользователя"),
            BotCommand(command="unmute", description="Размьютить пользователя"),
        ]
        await self.bot.set_my_commands(
            player_commands, scope=BotCommandScopeDefault())
        await self.bot.set_my_commands(
            player_commands + admin_commands,
            scope=BotCommandScopeAllChatAdministrators(),
        )

    async def _scheduler_loop(self) -> None:
        """Run the mechanic cycle: fast while cards are being built, then slow.

        The first cycle starts shortly after boot so cards actually get built
        instead of waiting a full UPDATE_INTERVAL_HOURS.
        """
        first = True
        while not self._shutdown.is_set():
            try:
                if first:
                    delay = FIRST_CYCLE_DELAY
                    first = False
                else:
                    published = 0
                    try:
                        published = await self.mechanic.count_published_cards()
                    except Exception:  # noqa: BLE001
                        published = 0
                    if published < CARDS_READY_THRESHOLD:
                        delay = UPDATE_INTERVAL_MINUTES * 60
                    else:
                        delay = UPDATE_INTERVAL_HOURS * 3600
                    log.info("Next cycle in %.0fs (published=%d)", delay, published)

                await asyncio.sleep(delay)
                if self._shutdown.is_set():
                    break
                log.info("Scheduled cycle triggered")
                # Перед обработкой новостей - свежая сверка справочника и
                # активности: за время простоя могли появиться дубли/новые страны.
                try:
                    await enforce_directory(self.db, apply=True)
                    await check_activity(self.db)
                except Exception as e:  # noqa: BLE001
                    log.warning("Плановая сверка справочника не удалась: %s", e)
                await self.mechanic.run_cycle()
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.exception("Scheduler error: %s", e)
                await asyncio.sleep(60)  # Back off on error

    async def run(self) -> None:
        """Run bot polling."""
        if not self.dp or not self.bot:
            raise RuntimeError("Bot not initialized")

        try:
            await self.dp.start_polling(self.bot)
        finally:
            await self.shutdown()

    async def shutdown(self) -> None:
        """Graceful shutdown."""
        log.info("Shutting down...")
        self._shutdown.set()

        if self._scheduler_task:
            self._scheduler_task.cancel()
            try:
                await self._scheduler_task
            except asyncio.CancelledError:
                pass

        if self.ai:
            await self.ai.close()

        if self.userbot:
            try:
                await self.userbot.disconnect()
            except Exception:  # noqa: BLE001
                pass

        if self.dp:
            # Если старт упал до polling, stop_polling() бросает
            # RuntimeError("Polling is not started") и маскирует исходную ошибку.
            try:
                await self.dp.stop_polling()
            except Exception:  # noqa: BLE001
                pass

        if self.bot:
            await self.bot.session.close()

        if self.db:
            await self.db.close()

        log.info("Shutdown complete")


async def main() -> None:
    bot = WarlordBot()
    try:
        await bot.start()
        await bot.run()
    except KeyboardInterrupt:
        log.info("Interrupted by user")
    except Exception as e:
        log.exception("Fatal error: %s", e)
        sys.exit(1)
    finally:
        await bot.shutdown()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass