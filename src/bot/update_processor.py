"""Selective-concurrency update processor for PTB.

Regular updates (messages, commands) process sequentially -- one at a time
*per project thread*.  When project threads are enabled, different threads
get independent locks so they can run in parallel.

Priority callbacks (stop:*) bypass the queue and run immediately so they can
interrupt the currently-running handler.

Stale locks are periodically pruned to prevent unbounded memory growth when
the bot is added to many groups/topics over its lifetime.
"""

import asyncio
import time
from typing import Any, Awaitable, Dict, List, Tuple

import structlog
from telegram import Update
from telegram.ext._baseupdateprocessor import BaseUpdateProcessor

logger = structlog.get_logger()

# Locks idle for longer than this are eligible for pruning.
_LOCK_IDLE_SECONDS = 172800  # 48 hours
# Run the prune sweep at most once every this many seconds.
_PRUNE_INTERVAL_SECONDS = 300  # 5 minutes


def _thread_key(update: Update) -> str:
    """Derive a concurrency-partition key from the update.

    Messages inside a forum topic carry ``message_thread_id``; we combine
    it with ``chat_id`` so different topics in the same chat (or different
    chats) each get their own lock.

    Messages *without* a thread id (regular groups, DMs) partition by
    ``chat_id`` so different chats run in parallel while messages within
    the same chat stay serial.
    """
    msg = update.effective_message
    if msg is not None:
        chat_id = getattr(msg, "chat_id", None)
        if isinstance(chat_id, int):
            thread_id = getattr(msg, "message_thread_id", None)
            if isinstance(thread_id, int):
                return f"{chat_id}:{thread_id}"
            return str(chat_id)
    return "global"


class StopAwareUpdateProcessor(BaseUpdateProcessor):
    """Update processor that lets priority callbacks bypass sequential processing.

    PTB calls ``process_update(update, coroutine)`` for every incoming update.
    The base class holds a semaphore (max 256) then calls our
    ``do_process_update()``.

    For priority callbacks (``stop:*``): we just ``await coroutine`` -- runs
    immediately.
    For everything else: we acquire a *per-partition* lock -- only one update
    per project thread (or per chat) runs at a time, but different
    threads/chats can run in parallel.

    A stop callback arrives while a text handler holds the lock -> stop
    callback runs concurrently -> fires the ``asyncio.Event`` -> the watcher
    task inside ``execute_command()`` calls ``client.interrupt()`` -> Claude
    stops -> ``run_command()`` returns -> handler finishes -> lock released.

    **Memory management**: Each unique partition key creates an
    ``asyncio.Lock``.  To prevent unbounded growth when the bot is added to
    many groups/topics, locks that have been idle for over 1 hour are pruned
    every 5 minutes.
    """

    _PRIORITY_PREFIXES = ("stop:",)

    def __init__(self) -> None:
        # High limit so priority callbacks are never blocked by semaphore
        super().__init__(max_concurrent_updates=256)
        self._locks: Dict[str, asyncio.Lock] = {}
        # Track last-use time per key for pruning
        self._last_used: Dict[str, float] = {}
        self._last_prune: float = 0.0

    def _get_lock(self, key: str) -> asyncio.Lock:
        """Get or create a lock for *key*, recording access time."""
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        self._last_used[key] = time.monotonic()
        return lock

    def _maybe_prune(self) -> None:
        """Remove idle, unlocked entries if enough time has passed."""
        now = time.monotonic()
        if now - self._last_prune < _PRUNE_INTERVAL_SECONDS:
            return
        self._last_prune = now

        cutoff = now - _LOCK_IDLE_SECONDS
        stale: List[str] = [
            k
            for k, ts in self._last_used.items()
            if ts < cutoff and not self._locks.get(k, asyncio.Lock()).locked()
        ]
        for k in stale:
            self._locks.pop(k, None)
            self._last_used.pop(k, None)

        if stale:
            logger.info(
                "update_processor.pruned_locks",
                pruned=len(stale),
                remaining=len(self._locks),
            )

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
        """Process an update, applying per-partition sequential lock."""
        if self._is_priority_callback(update):
            # Run immediately -- no sequential lock
            await coroutine
        else:
            # Derive key: different project threads / chats get independent
            # locks; fallback "global" for unrecognised updates.
            key = _thread_key(update) if isinstance(update, Update) else "global"
            lock = self._get_lock(key)
            async with lock:
                await coroutine
            # Lightweight check after releasing — non-blocking
            self._maybe_prune()

    async def initialize(self) -> None:
        """Initialize the processor (no-op)."""

    async def shutdown(self) -> None:
        """Shutdown the processor (no-op)."""

    def lock_stats(self) -> Tuple[int, int]:
        """Return ``(total_locks, currently_held)`` for diagnostics."""
        held = sum(1 for lock in self._locks.values() if lock.locked())
        return len(self._locks), held
