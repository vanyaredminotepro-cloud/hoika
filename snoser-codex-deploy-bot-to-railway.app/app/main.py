"""
Warlord RP Bot - Main entry point.
Two modules: Assistant (Module A) and Mechanic (Module B).
"""
from __future__ import annotations
import asyncio
import logging
import signal
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from config import config, BOT_TOKEN, ADMIN_ID, UPDATE_INTERVAL_HOURS
from db import Database
from ai import AIClient
from security import SecurityGuard
from assistant import Assistant
from mechanic import Mechanic
from handlers import router, setup_handlers
from logger import setup_logging

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

        # Initialize AI
        self.ai = AIClient()  # Uses config.AI_PROVIDER

        # Initialize security
        self.security = SecurityGuard(self.db)

        # Initialize bot and dispatcher
        self.bot = Bot(
            token=BOT_TOKEN,
            default=DefaultBotProperties(parse_mode=ParseMode.HTML)
        )
        self.dp = Dispatcher()

        # Initialize modules
        self.assistant = Assistant(self.db, self.ai, self.security, self.bot)
        self.mechanic = Mechanic(self.db, self.ai, self.assistant, self.bot)

        # Setup handlers
        setup_handlers(self.db, self.security, self.assistant, self.mechanic, self.bot)
        self.dp.include_router(router)

        # Initialize mechanic
        await self.mechanic.start()

        # Send startup message to admin
        try:
            await self.bot.send_message(
                ADMIN_ID,
                "🚀 <b>Warlord RP Bot запущен</b>\n"
                f"Провайдер: {config.AI_PROVIDER}\n"
                f"Интервал циклов: {UPDATE_INTERVAL_HOURS} ч."
            )
        except Exception as e:
            log.warning("Failed to send startup message: %s", e)

        # Start scheduler
        self._scheduler_task = asyncio.create_task(self._scheduler_loop())

        log.info("Bot started successfully")

    async def _scheduler_loop(self) -> None:
        """Run mechanic cycle every UPDATE_INTERVAL_HOURS."""
        while not self._shutdown.is_set():
            try:
                await asyncio.sleep(UPDATE_INTERVAL_HOURS * 3600)
                if self._shutdown.is_set():
                    break
                log.info("Scheduled cycle triggered")
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

        if self.dp:
            await self.dp.stop_polling()

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