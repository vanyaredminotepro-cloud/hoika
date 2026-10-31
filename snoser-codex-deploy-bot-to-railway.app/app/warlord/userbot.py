"""
Telethon userbot client for reading forum history.

The aiogram bot cannot read message history of a forum, only messages it
receives live. Reading topics (news, anketa) requires a user session,
so a Telethon client is used for all history access.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from .config import config

log = logging.getLogger(__name__)


def get_userbot() -> Any | None:
    """Create a TelegramClient if credentials exist, else None."""
    api_id = getattr(config, "tg_api_id", 0)
    api_hash = getattr(config, "tg_api_hash", "")
    if not api_id or not api_hash:
        log.warning("Userbot: TG_API_ID/TG_API_HASH not set, history reading disabled")
        return None

    from telethon import TelegramClient

    session_string = getattr(config, "tg_session_string", "") or ""
    if session_string:
        from telethon.sessions import StringSession

        session: Any = StringSession(session_string)
    else:
        name = getattr(config, "session_name", "warlord_userbot") or "warlord_userbot"
        # Keep session files next to the database, not in the package dir
        session_dir = Path(getattr(config, "PROJECT_ROOT", ".")) / "warlord_data"
        session_dir.mkdir(parents=True, exist_ok=True)
        session = str(session_dir / name)

    client = TelegramClient(session, api_id=int(api_id), api_hash=api_hash)
    return client


async def connect_userbot() -> Any | None:
    """Connect and authorize the userbot. Returns None if unavailable."""
    client = get_userbot()
    if client is None:
        return None
    try:
        await client.start()
        me = await client.get_me()
        log.info("userbot authorized: %s", getattr(me, "first_name", me.username))
        return client
    except Exception as exc:  # noqa: BLE001
        log.error("Userbot connection failed: %s", exc)
        return None
