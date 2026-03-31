"""RAM-gated concurrent executor for parallel Claude SDK sessions.

When project threads are enabled, each thread can spawn its own Claude SDK
instance. This module provides backpressure: once system RAM usage exceeds
a configurable threshold (default 90 %), new requests are queued and executed
synchronously (one-at-a-time) until memory pressure drops.

A hard cap (``max_concurrent``) prevents runaway parallelism regardless of
available memory.
"""

import asyncio
from typing import Any, Awaitable, Callable, Dict

import psutil
import structlog

logger = structlog.get_logger()


class RAMGatedExecutor:
    """Manage concurrent Claude SDK executions with RAM-based backpressure.

    Parameters
    ----------
    ram_threshold_pct:
        Switch to synchronous (queued) execution once
        ``psutil.virtual_memory().percent`` reaches this value.
    max_concurrent:
        Hard upper bound on simultaneous SDK tasks, regardless of RAM.
    """

    def __init__(
        self,
        ram_threshold_pct: float = 90.0,
        max_concurrent: int = 5,
    ) -> None:
        self._ram_threshold = ram_threshold_pct
        self._max_concurrent = max(1, max_concurrent)
        self._semaphore = asyncio.Semaphore(self._max_concurrent)
        # Track active tasks keyed by a caller-supplied string
        # (e.g. "chat_id:thread_id" or "user_id:state_key").
        self._active: Dict[str, asyncio.Task[Any]] = {}
        self._lock = asyncio.Lock()
        # Serialisation lock used when RAM is above threshold
        self._sync_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Public helpers
    # ------------------------------------------------------------------

    def ram_available(self) -> bool:
        """Return True if current RAM usage is below the threshold."""
        return bool(psutil.virtual_memory().percent < self._ram_threshold)

    @property
    def active_count(self) -> int:
        """Number of currently running SDK tasks."""
        self._sweep_done()
        return len(self._active)

    @property
    def ram_threshold(self) -> float:
        return self._ram_threshold

    @property
    def max_concurrent(self) -> int:
        return self._max_concurrent

    def is_key_active(self, key: str) -> bool:
        """Return True if *key* already has an in-flight task."""
        task = self._active.get(key)
        return task is not None and not task.done()

    # ------------------------------------------------------------------
    # Core execution
    # ------------------------------------------------------------------

    async def submit(
        self,
        key: str,
        coro_fn: Callable[[], Awaitable[Any]],
    ) -> Any:
        """Execute *coro_fn* with RAM-gated concurrency control.

        * If RAM is below threshold **and** the hard cap hasn't been
          reached, the coroutine runs immediately (concurrent with
          other keys).
        * If RAM is **at or above** threshold, the coroutine is
          serialised behind ``_sync_lock`` so only one new request
          proceeds at a time — existing in-flight tasks keep running.
        * Requests with the **same key** are always serialised (a
          second call with the same key waits for the first to finish).
          This preserves per-thread session continuity.

        Returns whatever the coroutine returns.
        """
        # Housekeeping: clear finished tasks before checking
        self._sweep_done()

        # Per-key serialisation: if key is already running, wait for it.
        async with self._lock:
            existing = self._active.get(key)

        if existing is not None and not existing.done():
            logger.info(
                "ram_gated_executor.wait_existing",
                key=key,
                active=self.active_count,
            )
            await existing  # wait, then proceed below

        if self.ram_available():
            return await self._run_concurrent(key, coro_fn)
        else:
            logger.warning(
                "ram_gated_executor.ram_pressure",
                ram_pct=psutil.virtual_memory().percent,
                threshold=self._ram_threshold,
                active=self.active_count,
                key=key,
            )
            return await self._run_sync(key, coro_fn)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _run_concurrent(
        self,
        key: str,
        coro_fn: Callable[[], Awaitable[Any]],
    ) -> Any:
        """Run under the semaphore (up to max_concurrent in parallel)."""
        async with self._semaphore:
            return await self._tracked_run(key, coro_fn)

    async def _run_sync(
        self,
        key: str,
        coro_fn: Callable[[], Awaitable[Any]],
    ) -> Any:
        """Run behind a single lock — fully serialised."""
        async with self._sync_lock:
            # Re-check: RAM may have freed while we waited
            if self.ram_available():
                return await self._run_concurrent(key, coro_fn)
            return await self._tracked_run(key, coro_fn)

    async def _tracked_run(
        self,
        key: str,
        coro_fn: Callable[[], Awaitable[Any]],
    ) -> Any:
        """Execute coro_fn while registering it in ``_active``."""
        task: asyncio.Task[Any] = asyncio.current_task()  # type: ignore[assignment]
        self._active[key] = task
        try:
            logger.info(
                "ram_gated_executor.start",
                key=key,
                active=self.active_count,
                ram_pct=psutil.virtual_memory().percent,
            )
            result = await coro_fn()
            return result
        finally:
            self._active.pop(key, None)
            logger.info(
                "ram_gated_executor.done",
                key=key,
                remaining=self.active_count,
                ram_pct=psutil.virtual_memory().percent,
            )

    def _sweep_done(self) -> None:
        """Remove entries whose tasks have already completed.

        Called lazily from ``active_count`` so the dict doesn't
        accumulate stale references over time.
        """
        stale = [k for k, t in self._active.items() if t.done()]
        for k in stale:
            self._active.pop(k, None)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def cancel_all(self) -> int:
        """Cancel all active tasks. Returns the number cancelled."""
        cancelled = 0
        for key, task in list(self._active.items()):
            if not task.done():
                task.cancel()
                cancelled += 1
                logger.info("ram_gated_executor.cancelled", key=key)
        self._active.clear()
        return cancelled

    def status(self) -> Dict[str, Any]:
        """Return a snapshot dict for /status commands."""
        self._sweep_done()
        mem = psutil.virtual_memory()
        return {
            "active_tasks": self.active_count,
            "max_concurrent": self._max_concurrent,
            "ram_threshold_pct": self._ram_threshold,
            "ram_used_pct": mem.percent,
            "ram_available_gb": round(mem.available / (1024**3), 2),
            "mode": "concurrent" if self.ram_available() else "synchronous",
            "active_keys": list(self._active.keys()),
        }
