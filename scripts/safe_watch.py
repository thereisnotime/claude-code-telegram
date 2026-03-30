#!/usr/bin/env python3
"""Smart restart wrapper for watchfiles.

Replaces raw `watchfiles` with debounce, syntax validation, health checks,
and git-based rollback. Prevents self-modification deadlocks when Claude
edits the bot's own source code.

This script must NOT import anything from src/ — it must survive even when
src/ is broken. Only stdlib + watchfiles (dev dependency) are used.

Usage:
    python scripts/safe_watch.py [options]
    poetry run python scripts/safe_watch.py [options]

Environment variables (all optional):
    SAFE_WATCH_DEBOUNCE         Debounce seconds (default: 5.0)
    SAFE_WATCH_HEALTH_TIMEOUT   Health check seconds (default: 15.0)
    SAFE_WATCH_ROLLBACK         Enable git rollback on crash (default: true)
    SAFE_WATCH_COMMAND          Bot command to run (default: claude-telegram-bot)
    SAFE_WATCH_MAX_RESTARTS     Max restarts in window (default: 3)
    SAFE_WATCH_RESTART_WINDOW   Restart window seconds (default: 60)
"""

from __future__ import annotations

import argparse
import logging
import os
import py_compile
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

# watchfiles is the only external dependency (already a dev dep)
try:
    from watchfiles import watch
except ImportError:
    print("ERROR: watchfiles not installed. Run: poetry install", file=sys.stderr)
    sys.exit(1)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [safe_watch] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("safe_watch")

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class SafeWatcher:
    """Supervises the bot process with validation and rollback."""

    def __init__(
        self,
        command: str,
        watch_dir: str = "src",
        debounce_s: float = 5.0,
        health_timeout_s: float = 15.0,
        enable_rollback: bool = True,
        max_restarts: int = 3,
        restart_window_s: float = 60.0,
        extra_args: Optional[list[str]] = None,
    ):
        self.command = command
        self.watch_dir = Path(PROJECT_ROOT / watch_dir)
        self.debounce_s = debounce_s
        self.health_timeout_s = health_timeout_s
        self.enable_rollback = enable_rollback
        self.max_restarts = max_restarts
        self.restart_window_s = restart_window_s
        self.extra_args = extra_args or []

        self.process: Optional[subprocess.Popen] = None
        self.last_good_commit: Optional[str] = None
        self.restart_times: list[float] = []
        self._shutting_down = False

    def start_bot(self) -> subprocess.Popen:
        """Start the bot as a subprocess."""
        cmd = ["poetry", "run", self.command] + self.extra_args
        log.info("Starting bot: %s", " ".join(cmd))
        proc = subprocess.Popen(
            cmd,
            cwd=PROJECT_ROOT,
            # Pass through stdin/stdout/stderr so logs are visible
            stdin=subprocess.DEVNULL,
        )
        self.process = proc
        return proc

    def stop_bot(self, timeout: float = 10.0) -> None:
        """Gracefully stop the bot process."""
        if not self.process or self.process.poll() is not None:
            return

        log.info("Sending SIGTERM to bot (pid %d)", self.process.pid)
        self.process.send_signal(signal.SIGTERM)

        try:
            self.process.wait(timeout=timeout)
            log.info("Bot stopped gracefully")
        except subprocess.TimeoutExpired:
            log.warning("Bot did not stop in %.0fs, sending SIGKILL", timeout)
            self.process.kill()
            self.process.wait(timeout=5)

    def validate_syntax(self, changed_files: set[Path]) -> bool:
        """Validate changed .py files with py_compile.

        Returns True if all files pass syntax check.
        """
        py_files = [f for f in changed_files if f.suffix == ".py" and f.exists()]
        if not py_files:
            return True

        errors = []
        for filepath in py_files:
            try:
                py_compile.compile(str(filepath), doraise=True)
            except py_compile.PyCompileError as e:
                errors.append((filepath, str(e)))

        if errors:
            log.error(
                "Syntax validation FAILED for %d file(s) — skipping restart:",
                len(errors),
            )
            for filepath, err in errors:
                log.error("  %s: %s", filepath.relative_to(PROJECT_ROOT), err)
            return False

        log.info("Syntax validation passed for %d file(s)", len(py_files))
        return True

    def health_check(self) -> bool:
        """Check if the bot process survives the health timeout.

        Returns True if the process is still running after the timeout (healthy).
        Returns False if it exited (crashed).
        """
        if not self.process:
            return False

        try:
            self.process.wait(timeout=self.health_timeout_s)
            # Process exited within timeout — it crashed
            exit_code = self.process.returncode
            log.error(
                "Bot crashed during health check (exit code %d)", exit_code
            )
            return False
        except subprocess.TimeoutExpired:
            # Still running — healthy
            log.info(
                "Health check passed (still running after %.0fs)",
                self.health_timeout_s,
            )
            return True

    def record_good_state(self) -> None:
        """Record the current git HEAD as the last known good state."""
        try:
            result = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                cwd=PROJECT_ROOT,
                timeout=5,
            )
            if result.returncode == 0:
                self.last_good_commit = result.stdout.strip()
                log.info("Recorded good state: %s", self.last_good_commit[:12])
        except Exception as e:
            log.warning("Could not record git state: %s", e)

    def rollback(self) -> bool:
        """Rollback src/ to last known good state.

        Stashes the broken changes first so they can be inspected later.
        Returns True if rollback succeeded.
        """
        if not self.enable_rollback:
            log.warning("Rollback disabled, not reverting")
            return False

        if not self.last_good_commit:
            log.error("No known good commit to rollback to")
            return False

        log.warning("Rolling back src/ to %s", self.last_good_commit[:12])

        try:
            # Stash broken changes so they can be inspected
            subprocess.run(
                [
                    "git",
                    "stash",
                    "push",
                    "-m",
                    f"safe_watch: auto-rollback broken state at {time.strftime('%H:%M:%S')}",
                    "--",
                    "src/",
                ],
                cwd=PROJECT_ROOT,
                timeout=10,
                capture_output=True,
            )

            # Restore src/ from last good commit
            subprocess.run(
                ["git", "checkout", self.last_good_commit, "--", "src/"],
                cwd=PROJECT_ROOT,
                timeout=10,
                check=True,
                capture_output=True,
            )

            # Unstage the checkout so the working tree is clean
            subprocess.run(
                ["git", "reset", "HEAD", "--", "src/"],
                cwd=PROJECT_ROOT,
                timeout=10,
                capture_output=True,
            )

            log.warning(
                "Rollback complete. Broken changes stashed — use `git stash list` to inspect."
            )
            return True

        except Exception as e:
            log.error("Rollback failed: %s", e)
            return False

    def check_restart_limit(self) -> bool:
        """Check if we've hit the restart rate limit.

        Returns True if restart is allowed, False if rate-limited.
        """
        now = time.time()
        # Prune old entries
        self.restart_times = [
            t for t in self.restart_times if now - t < self.restart_window_s
        ]

        if len(self.restart_times) >= self.max_restarts:
            log.error(
                "Restart rate limit hit (%d restarts in %.0fs window) — "
                "refusing to restart. Fix the code manually or increase the limit.",
                self.max_restarts,
                self.restart_window_s,
            )
            return False

        self.restart_times.append(now)
        return True

    def handle_changes(self, changed_files: set[Path]) -> None:
        """Handle detected file changes: validate, restart, health-check."""
        if self._shutting_down:
            return

        log.info(
            "Changes detected in %d file(s)",
            len(changed_files),
        )
        for f in sorted(changed_files):
            try:
                log.info("  %s", f.relative_to(PROJECT_ROOT))
            except ValueError:
                log.info("  %s", f)

        # Syntax validation
        if not self.validate_syntax(changed_files):
            log.warning("Keeping old process running due to syntax errors")
            return

        # Restart rate limit
        if not self.check_restart_limit():
            return

        # Record good state before restart
        self.record_good_state()

        # Stop old process
        self.stop_bot()

        # Start new process
        self.start_bot()

        # Health check
        if not self.health_check():
            log.error("New process failed health check")
            if self.rollback():
                log.info("Restarting with rolled-back code")
                self.start_bot()
                if not self.health_check():
                    log.error(
                        "Bot still failing after rollback — manual intervention needed"
                    )
            else:
                log.error("Rollback unavailable — manual intervention needed")

    def run(self) -> None:
        """Main loop: start bot, watch for changes."""
        # Handle signals for clean shutdown
        def on_signal(signum: int, frame: object) -> None:
            log.info("Received signal %d, shutting down", signum)
            self._shutting_down = True
            self.stop_bot()
            sys.exit(0)

        signal.signal(signal.SIGINT, on_signal)
        signal.signal(signal.SIGTERM, on_signal)

        # Initial start
        self.record_good_state()
        self.start_bot()

        log.info(
            "Watching %s (debounce=%.1fs, health_timeout=%.1fs, rollback=%s)",
            self.watch_dir.relative_to(PROJECT_ROOT),
            self.debounce_s,
            self.health_timeout_s,
            self.enable_rollback,
        )

        # watchfiles.watch() is a blocking iterator that yields sets of changes.
        # It has its own debounce (default 1600ms). We use a longer debounce
        # to let Claude finish multi-file writes.
        try:
            for changes in watch(
                self.watch_dir,
                # watchfiles debounce in ms — we use our own debounce on top
                debounce=int(self.debounce_s * 1000),
                # Only watch Python files
                watch_filter=lambda change, path: path.endswith(".py"),
                # Stop if the watcher itself should exit
                stop_event=None,
            ):
                changed_files = {Path(path) for _, path in changes}
                self.handle_changes(changed_files)

                # If bot died outside of our restart cycle, restart it
                if (
                    self.process
                    and self.process.poll() is not None
                    and not self._shutting_down
                ):
                    log.warning("Bot process died unexpectedly, restarting")
                    self.start_bot()

        except KeyboardInterrupt:
            pass
        finally:
            self.stop_bot()


def _parse_bool(value: str) -> bool:
    return value.lower() in ("true", "1", "yes")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Smart restart wrapper for the Telegram bot",
    )
    parser.add_argument(
        "--debounce",
        type=float,
        default=float(os.environ.get("SAFE_WATCH_DEBOUNCE", "5.0")),
        help="Debounce seconds before restart (default: 5.0)",
    )
    parser.add_argument(
        "--health-timeout",
        type=float,
        default=float(os.environ.get("SAFE_WATCH_HEALTH_TIMEOUT", "15.0")),
        help="Seconds to wait for health check (default: 15.0)",
    )
    parser.add_argument(
        "--no-rollback",
        action="store_true",
        default=not _parse_bool(os.environ.get("SAFE_WATCH_ROLLBACK", "true")),
        help="Disable git rollback on crash",
    )
    parser.add_argument(
        "--max-restarts",
        type=int,
        default=int(os.environ.get("SAFE_WATCH_MAX_RESTARTS", "3")),
        help="Max restarts in window (default: 3)",
    )
    parser.add_argument(
        "--restart-window",
        type=float,
        default=float(os.environ.get("SAFE_WATCH_RESTART_WINDOW", "60")),
        help="Restart window seconds (default: 60)",
    )
    parser.add_argument(
        "--command",
        default=os.environ.get("SAFE_WATCH_COMMAND", "claude-telegram-bot"),
        help="Bot command to run (default: claude-telegram-bot)",
    )
    parser.add_argument(
        "extra_args",
        nargs="*",
        help="Extra arguments passed to the bot command",
    )

    args = parser.parse_args()

    watcher = SafeWatcher(
        command=args.command,
        debounce_s=args.debounce,
        health_timeout_s=args.health_timeout,
        enable_rollback=not args.no_rollback,
        max_restarts=args.max_restarts,
        restart_window_s=args.restart_window,
        extra_args=args.extra_args,
    )
    watcher.run()


if __name__ == "__main__":
    main()
