"""Resolve existing Telegram forum topics via Telethon (MTProto).

When the bot starts with a fresh database, it has no record of topics
that were already created in a previous instance.  The Bot API does not
expose a "list forum topics" endpoint, but the MTProto client API does
(``channels.getForumTopics``).

This module temporarily connects a Telethon client using the same bot
token and fetches all existing topic names + IDs for a given chat.
When multiple topics share the same name the **largest** (newest) ID is
kept, so the bot reuses the most recent duplicate.

Feature-gated behind ``ENABLE_TOPIC_RESOLUTION`` (default ``false``).
Requires ``TELEGRAM_API_ID`` and ``TELEGRAM_API_HASH`` from
https://my.telegram.org.
"""

from __future__ import annotations

import asyncio
from typing import Dict, Optional

import structlog

logger = structlog.get_logger()


async def fetch_existing_topics(
    api_id: int,
    api_hash: str,
    bot_token: str,
    chat_id: int,
    *,
    session_name: str = "topic_resolver",
) -> Dict[str, int]:
    """Return ``{topic_name: message_thread_id}`` for all topics in *chat_id*.

    If multiple topics share the same name, the **largest** thread ID wins
    (newest topic).  The Telethon client is connected, used once, then
    disconnected — it is not kept alive.

    Returns an empty dict on any error so callers can fall back to
    create-if-missing behaviour.
    """
    try:
        from telethon import TelegramClient  # type: ignore[import-untyped]
        from telethon.tl.functions.channels import (  # type: ignore[import-untyped]
            GetForumTopicsRequest,
        )
    except ImportError:
        logger.warning(
            "telethon is not installed — topic resolution disabled. "
            "Install with: poetry install -E telethon"
        )
        return {}

    client: Optional[TelegramClient] = None
    try:
        client = TelegramClient(
            session_name,
            api_id,
            api_hash,
        )
        # Use system_lang_code to avoid locale warnings
        client.session.set_dc(2, "149.154.167.50", 443)
        await client.start(bot_token=bot_token)

        # Resolve the chat entity (works for supergroups, private chats, etc.)
        entity = await client.get_entity(chat_id)

        topics: Dict[str, int] = {}
        offset_date = 0
        offset_id = 0
        offset_topic = 0

        while True:
            result = await client(
                GetForumTopicsRequest(
                    channel=entity,
                    offset_date=offset_date,
                    offset_id=offset_id,
                    offset_topic=offset_topic,
                    limit=100,
                )
            )

            if not result.topics:
                break

            for topic in result.topics:
                name = getattr(topic, "title", None)
                tid = getattr(topic, "id", None)
                if name is None or tid is None:
                    continue

                existing_id = topics.get(name)
                if existing_id is None or tid > existing_id:
                    topics[name] = tid

            # Pagination: if we got fewer than requested, we're done
            if len(result.topics) < 100:
                break

            # Advance pagination cursors
            last = result.topics[-1]
            offset_date = getattr(last, "date", 0)
            offset_id = getattr(last, "id", 0)
            offset_topic = getattr(last, "id", 0)

        logger.info(
            "Resolved existing forum topics via Telethon",
            chat_id=chat_id,
            topic_count=len(topics),
        )
        return topics

    except Exception:
        logger.exception(
            "Failed to resolve forum topics via Telethon — "
            "sync will create topics normally",
            chat_id=chat_id,
        )
        return {}

    finally:
        if client is not None:
            try:
                await client.disconnect()
            except Exception:
                pass
        # Clean up session file (we don't need it persisted)
        _cleanup_session_file(session_name)


def _cleanup_session_file(session_name: str) -> None:
    """Remove the Telethon SQLite session file if it exists."""
    import pathlib

    for suffix in (".session", ".session-journal"):
        path = pathlib.Path(f"{session_name}{suffix}")
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


async def resolve_topics_if_enabled(
    *,
    enable_topic_resolution: bool,
    api_id: Optional[int],
    api_hash: Optional[str],
    bot_token: str,
    chat_id: int,
) -> Dict[str, int]:
    """High-level wrapper: resolve topics only when the feature is enabled.

    Returns ``{topic_name: message_thread_id}`` or empty dict.
    """
    if not enable_topic_resolution:
        return {}

    if not api_id or not api_hash:
        logger.warning(
            "Topic resolution enabled but TELEGRAM_API_ID / "
            "TELEGRAM_API_HASH not set — skipping"
        )
        return {}

    return await fetch_existing_topics(
        api_id=api_id,
        api_hash=api_hash,
        bot_token=bot_token,
        chat_id=chat_id,
    )
