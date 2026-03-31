"""Selective-concurrency update processor for PTB.

Regular updates (messages, commands) process sequentially -- one at a time
*per project thread*.  When project threads are enabled, different threads
get independent locks so they can run in parallel.

Priority callbacks (stop:*) bypass the queue and run immediately so they can
interrupt the currently-running handler.
"""

import asyncio
from collections import defaultdict
from typing import Any, Awaitable, Dict

from telegram import Update
from telegram.ext._baseupdateprocessor import BaseUpdateProcessor


def _thread_key(update: Update) -> str:
    """Derive a concurrency-partition key from the update.

    Messages inside a forum topic carry ``message_thread_id``; we combine
    it with ``chat_id`` so different topics in the same chat (or different
    chats) each get their own lock.  Updates without a thread id all share
    the ``"global"`` key, preserving the original one-at-a-time behaviour.
    """
    msg = update.effective_message
    if msg is not None:
        thread_id = getattr(msg, "message_thread_id", None)
        if isinstance(thread_id, int):
            chat_id = getattr(msg, "chat_id", None)
            if isinstance(chat_id, int):
                return f"{chat_id}:{thread_id}"
    return "global"


class StopAwareUpdateProcessor(BaseUpdateProcessor):
    """Update processor that lets priority callbacks bypass sequential processing.

    PTB calls ``process_update(update, coroutine)`` for every incoming update.
    The base class holds a semaphore (max 256) then calls our
    ``do_process_update()``.

    For priority callbacks (``stop:*``): we just ``await coroutine`` -- runs
    immediately.
    For everything else: we acquire a *per-thread* lock -- only one update
    per project thread runs at a time, but different threads can run in
    parallel.

    A stop callback arrives while a text handler holds the lock -> stop
    callback runs concurrently -> fires the ``asyncio.Event`` -> the watcher
    task inside ``execute_command()`` calls ``client.interrupt()`` -> Claude
    stops -> ``run_command()`` returns -> handler finishes -> lock released.
    """

    _PRIORITY_PREFIXES = ("stop:",)

    def __init__(self) -> None:
        # High limit so priority callbacks are never blocked by semaphore
        super().__init__(max_concurrent_updates=256)
        self._locks: Dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    @classmethod
    def _is_priority_callback(cls, update: object) -> bool:
        """Return True if the update is a priority callback query."""
        if not isinstance(update, Update):
            return False
        cb = update.callback_query
        return (
            cb is not None
            and cb.data is not None
            and cb.data.startswith(cls._PRIORITY_PREFIXES)
        )

    async def do_process_update(
        self,
        update: object,
        coroutine: Awaitable[Any],
    ) -> None:
        """Process an update, applying per-thread sequential lock."""
        if self._is_priority_callback(update):
            # Run immediately -- no sequential lock
            await coroutine
        else:
            # Derive key: different project threads get independent locks,
            # non-thread messages share "global".
            key = _thread_key(update) if isinstance(update, Update) else "global"
            async with self._locks[key]:
                await coroutine

    async def initialize(self) -> None:
        """Initialize the processor (no-op)."""

    async def shutdown(self) -> None:
        """Shutdown the processor (no-op)."""
