"""Main entry point for Claude Code Telegram Bot."""

import argparse
import asyncio
import logging
import signal
import sys
from pathlib import Path
from typing import Any, Dict, Optional

import structlog

from src import __version__
from src.bot.core import ClaudeCodeBot
from src.claude import (
    ClaudeIntegration,
    SessionManager,
)
from src.claude.sdk_integration import ClaudeSDKManager
from src.config.features import FeatureFlags
from src.config.settings import Settings
from src.events.bus import EventBus
from src.events.handlers import AgentHandler
from src.events.middleware import EventSecurityMiddleware
from src.exceptions import ConfigurationError
from src.notifications.service import NotificationService
from src.projects import ProjectThreadManager, load_project_registry
from src.scheduler.scheduler import JobScheduler
from src.security.audit import AuditLogger, InMemoryAuditStorage
from src.security.auth import (
    AuthenticationManager,
    AuthProvider,
    InMemoryTokenStorage,
    TokenAuthProvider,
    WhitelistAuthProvider,
)
from src.security.rate_limiter import RateLimiter
from src.security.validators import SecurityValidator
from src.storage.facade import Storage
from src.storage.session_storage import SQLiteSessionStorage


def setup_logging(debug: bool = False) -> None:
    """Configure structured logging."""
    level = logging.DEBUG if debug else logging.INFO

    # Configure standard logging
    logging.basicConfig(
        level=level,
        format="%(message)s",
        stream=sys.stdout,
    )

    # Configure structlog
    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            structlog.stdlib.add_logger_name,
            structlog.stdlib.add_log_level,
            structlog.stdlib.PositionalArgumentsFormatter(),
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.UnicodeDecoder(),
            (
                structlog.processors.JSONRenderer()
                if not debug
                else structlog.dev.ConsoleRenderer()
            ),
        ],
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )


def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Claude Code Telegram Bot",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument(
        "--version", action="version", version=f"Claude Code Telegram Bot {__version__}"
    )

    parser.add_argument("--debug", action="store_true", help="Enable debug logging")

    parser.add_argument("--config-file", type=Path, help="Path to configuration file")

    parser.add_argument(
        "--env-file",
        type=Path,
        action="append",
        default=[],
        help="Additional .env file(s) to load. Can be repeated. Loaded in order, later files override earlier ones.",
    )

    return parser.parse_args()


async def create_application(config: Settings) -> Dict[str, Any]:
    """Create and configure the application components."""
    logger = structlog.get_logger()
    logger.info("Creating application components")

    features = FeatureFlags(config)

    # Initialize storage system
    storage = Storage(config.database_url, pool_size=config.db_pool_size)
    await storage.initialize()

    # Create security components
    providers: list[AuthProvider] = []

    # Add whitelist provider if users are configured
    if config.allowed_users:
        providers.append(WhitelistAuthProvider(config.allowed_users))

    # Add token provider if enabled
    if config.enable_token_auth:
        token_storage = InMemoryTokenStorage()  # TODO: Use database storage
        secret = (
            config.auth_token_secret.get_secret_value()
            if config.auth_token_secret
            else ""
        )
        providers.append(TokenAuthProvider(secret, token_storage))

    # Fall back to allowing all users in development mode
    if not providers and config.development_mode:
        logger.warning(
            "No auth providers configured"
            " - creating development-only allow-all provider"
        )
        providers.append(WhitelistAuthProvider([], allow_all_dev=True))
    elif not providers:
        raise ConfigurationError("No authentication providers configured")

    auth_manager = AuthenticationManager(providers)
    security_validator = SecurityValidator(
        config.approved_directory,
        disable_security_patterns=config.disable_security_patterns,
    )
    rate_limiter = RateLimiter(config)

    # Create audit storage and logger
    audit_storage = InMemoryAuditStorage()  # TODO: Use database storage in production
    audit_logger = AuditLogger(audit_storage)

    # Create Claude integration components with persistent storage
    session_storage = SQLiteSessionStorage(storage.db_manager)
    session_manager = SessionManager(config, session_storage)

    # Create Claude SDK manager and integration facade
    logger.info("Using Claude Python SDK integration")
    sdk_manager = ClaudeSDKManager(config, security_validator=security_validator)

    claude_integration = ClaudeIntegration(
        config=config,
        sdk_manager=sdk_manager,
        session_manager=session_manager,
    )

    # --- Event bus and agentic platform components ---
    event_bus = EventBus()

    # Event security middleware
    event_security = EventSecurityMiddleware(
        event_bus=event_bus,
        security_validator=security_validator,
        auth_manager=auth_manager,
    )
    event_security.register()

    # Agent handler — translates events into Claude executions
    agent_handler = AgentHandler(
        event_bus=event_bus,
        claude_integration=claude_integration,
        default_working_directory=config.approved_directory,
        default_user_id=config.allowed_users[0] if config.allowed_users else 0,
    )
    agent_handler.register()

    # Create bot with all dependencies
    dependencies = {
        "auth_manager": auth_manager,
        "security_validator": security_validator,
        "rate_limiter": rate_limiter,
        "audit_logger": audit_logger,
        "claude_integration": claude_integration,
        "storage": storage,
        "event_bus": event_bus,
        "project_registry": None,
        "project_threads_manager": None,
    }

    bot = ClaudeCodeBot(config, dependencies)

    # Notification service and scheduler need the bot's Telegram Bot instance,
    # which is only available after bot.initialize(). We store placeholders
    # and wire them up in run_application() after initialization.

    logger.info("Application components created successfully")

    return {
        "bot": bot,
        "claude_integration": claude_integration,
        "storage": storage,
        "config": config,
        "features": features,
        "event_bus": event_bus,
        "agent_handler": agent_handler,
        "auth_manager": auth_manager,
        "security_validator": security_validator,
    }


async def _resume_interrupted_sessions(
    claude_integration: ClaudeIntegration,
    telegram_bot: Any,
    config: Settings,
    delay_seconds: float = 5.0,
) -> None:
    """Detect and resume sessions that were interrupted by a restart.

    Waits briefly for the bot to fully initialize, then checks for any
    sessions that had in_flight=TRUE when the process last died.
    """
    _logger = structlog.get_logger()

    await asyncio.sleep(delay_seconds)

    if not claude_integration.session_manager:
        return
    storage = claude_integration.session_manager.storage
    if not hasattr(storage, "get_interrupted_sessions"):
        return

    try:
        interrupted = await storage.get_interrupted_sessions()
    except Exception as e:
        _logger.warning("Could not query interrupted sessions", error=str(e))
        return

    if not interrupted:
        return

    _logger.info("Found interrupted sessions to resume", count=len(interrupted))

    from datetime import UTC, datetime, timedelta

    # Deduplicate: only resume the most recent session per user.
    # Clear stale duplicates from previous restart loops.
    seen_users: set[int] = set()
    deduplicated: list[dict[str, Any]] = []
    for s in sorted(
        interrupted,
        key=lambda x: x.get("in_flight_started_at") or datetime.min,
        reverse=True,
    ):
        uid = s["user_id"]
        if uid in seen_users:
            # Stale duplicate — clear it silently
            try:
                await storage.clear_in_flight(s["session_id"])
            except Exception:
                pass
            continue
        seen_users.add(uid)
        deduplicated.append(s)

    for session_info in deduplicated:
        session_id = session_info["session_id"]
        chat_id = session_info.get("chat_id")
        message_thread_id = session_info.get("message_thread_id")
        user_id = session_info["user_id"]
        original_prompt = session_info.get("in_flight_prompt") or ""
        working_dir = session_info.get("project_path", config.approved_directory)
        started_at = session_info.get("in_flight_started_at")

        # Skip stale sessions
        if started_at and isinstance(started_at, datetime):
            age = datetime.now(UTC) - started_at
            if age > timedelta(hours=config.session_timeout_hours):
                _logger.info(
                    "Skipping stale interrupted session",
                    session_id=session_id,
                    age_hours=age.total_seconds() / 3600,
                )
                await storage.clear_in_flight(session_id)
                continue

        if not chat_id:
            _logger.warning(
                "No chat_id for interrupted session, skipping",
                session_id=session_id,
            )
            await storage.clear_in_flight(session_id)
            continue

        # Notify user
        try:
            await telegram_bot.send_message(
                chat_id=chat_id,
                message_thread_id=message_thread_id,
                text="Bot restarted during your request. Resuming...",
            )
        except Exception as e:
            _logger.warning(
                "Failed to notify user of resume",
                chat_id=chat_id,
                error=str(e),
            )
            await storage.clear_in_flight(session_id)
            continue

        # Resume the Claude session
        resume_prompt = (
            "You were interrupted by a bot process restart mid-execution. "
            f"The user's original request was: {original_prompt}\n\n"
            "Continue where you left off. If you were writing code, "
            "verify the current state of the files and complete any unfinished work."
        )

        try:
            _logger.info(
                "Auto-resuming session",
                session_id=session_id,
                user_id=user_id,
                working_dir=working_dir,
            )
            response = await asyncio.wait_for(
                claude_integration.run_command(
                    prompt=resume_prompt,
                    working_directory=Path(working_dir),
                    user_id=user_id,
                    session_id=session_id,
                    chat_id=chat_id,
                    message_thread_id=message_thread_id,
                ),
                timeout=120.0,
            )

            _logger.info(
                "Auto-resume got response",
                session_id=session_id,
                content_length=len(response.content or ""),
                is_error=response.is_error,
            )

            # Send response back to the chat
            from src.bot.utils.formatting import ResponseFormatter

            formatter = ResponseFormatter(config)
            for msg in formatter.format_claude_response(response.content or ""):
                try:
                    await telegram_bot.send_message(
                        chat_id=chat_id,
                        message_thread_id=message_thread_id,
                        text=msg.text,
                        parse_mode=msg.parse_mode,
                    )
                except Exception:
                    # If HTML parse fails, send as plain text
                    await telegram_bot.send_message(
                        chat_id=chat_id,
                        message_thread_id=message_thread_id,
                        text=msg.text,
                    )

        except asyncio.TimeoutError:
            _logger.error("Auto-resume timed out", session_id=session_id)
            try:
                await telegram_bot.send_message(
                    chat_id=chat_id,
                    message_thread_id=message_thread_id,
                    text="Auto-resume timed out. Send your request again.",
                )
            except Exception:
                pass
        except Exception as e:
            _logger.error(
                "Failed to resume session",
                session_id=session_id,
                error=str(e),
                error_type=type(e).__name__,
            )
            try:
                await telegram_bot.send_message(
                    chat_id=chat_id,
                    message_thread_id=message_thread_id,
                    text=f"Auto-resume failed: {e}\nSend your request again.",
                )
            except Exception:
                pass
        finally:
            try:
                await storage.clear_in_flight(session_id)
            except Exception:
                pass

    _logger.info("Interrupted session resume complete")


async def run_application(app: Dict[str, Any]) -> None:
    """Run the application with graceful shutdown handling."""
    logger = structlog.get_logger()
    bot: ClaudeCodeBot = app["bot"]
    claude_integration: ClaudeIntegration = app["claude_integration"]
    storage: Storage = app["storage"]
    config: Settings = app["config"]
    features: FeatureFlags = app["features"]
    event_bus: EventBus = app["event_bus"]

    notification_service: Optional[NotificationService] = None
    scheduler: Optional[JobScheduler] = None
    project_threads_manager: Optional[ProjectThreadManager] = None

    # Set up signal handlers for graceful shutdown
    shutdown_event = asyncio.Event()

    def signal_handler(signum: int, frame: Any) -> None:
        logger.info("Shutdown signal received", signal=signum)
        shutdown_event.set()

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    try:
        logger.info("Starting Claude Code Telegram Bot")

        # Initialize the bot first (creates the Telegram Application)
        await bot.initialize()

        if config.enable_project_threads:
            if not config.projects_config_path:
                raise ConfigurationError(
                    "Project thread mode enabled but required settings are missing"
                )
            registry = load_project_registry(
                config_path=config.projects_config_path,
                approved_directory=config.approved_directory,
            )
            project_threads_manager = ProjectThreadManager(
                registry=registry,
                repository=storage.project_threads,
                sync_action_interval_seconds=(
                    config.project_threads_sync_action_interval_seconds
                ),
            )

            bot.deps["project_registry"] = registry
            bot.deps["project_threads_manager"] = project_threads_manager

            if config.project_threads_mode == "group":
                if config.project_threads_chat_id is None:
                    raise ConfigurationError(
                        "Group thread mode requires PROJECT_THREADS_CHAT_ID"
                    )
                assert bot.app is not None
                sync_result = await project_threads_manager.sync_topics(
                    bot.app.bot,
                    chat_id=config.project_threads_chat_id,
                )
                logger.info(
                    "Project thread startup sync complete",
                    mode=config.project_threads_mode,
                    chat_id=config.project_threads_chat_id,
                    created=sync_result.created,
                    reused=sync_result.reused,
                    renamed=sync_result.renamed,
                    failed=sync_result.failed,
                    deactivated=sync_result.deactivated,
                )

        # Now wire up components that need the Telegram Bot instance
        assert bot.app is not None
        telegram_bot = bot.app.bot

        # Start event bus
        await event_bus.start()

        # Notification service
        notification_service = NotificationService(
            event_bus=event_bus,
            bot=telegram_bot,
            default_chat_ids=config.notification_chat_ids or [],
        )
        notification_service.register()
        await notification_service.start()

        # Collect concurrent tasks
        tasks: list[asyncio.Task[Any]] = []

        # Bot task — use start() which handles its own initialization check
        bot_task = asyncio.create_task(bot.start())
        tasks.append(bot_task)

        # API server (if enabled)
        if features.api_server_enabled:
            from src.api.server import run_api_server

            api_task = asyncio.create_task(
                run_api_server(event_bus, config, storage.db_manager)
            )
            tasks.append(api_task)
            logger.info("API server enabled", port=config.api_server_port)

        # Scheduler (if enabled)
        if features.scheduler_enabled:
            scheduler = JobScheduler(
                event_bus=event_bus,
                db_manager=storage.db_manager,
                default_working_directory=config.approved_directory,
            )
            await scheduler.start()
            logger.info("Job scheduler enabled")

        # Auto-resume sessions interrupted by a restart (fire-and-forget,
        # NOT added to tasks — completing this must not trigger shutdown).
        asyncio.create_task(
            _resume_interrupted_sessions(
                claude_integration=claude_integration,
                telegram_bot=telegram_bot,
                config=config,
                delay_seconds=5.0,
            )
        )

        # Shutdown task
        shutdown_task = asyncio.create_task(shutdown_event.wait())
        tasks.append(shutdown_task)

        # Wait for any task to complete or shutdown signal
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)

        # Check completed tasks for exceptions
        for task in done:
            if task.cancelled():
                continue
            exc = task.exception()
            if exc is not None:
                logger.error(
                    "Task failed",
                    task=task.get_name(),
                    error=str(exc),
                    error_type=type(exc).__name__,
                )

        # Cancel remaining tasks
        for task in pending:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    except Exception as e:
        logger.error("Application error", error=str(e))
        raise
    finally:
        # Ordered shutdown: scheduler -> API -> notification -> bot -> claude -> storage
        logger.info("Shutting down application")

        try:
            if scheduler:
                await scheduler.stop()
            if notification_service:
                await notification_service.stop()
            await event_bus.stop()
            await bot.stop()
            await claude_integration.shutdown()
            await storage.close()
        except Exception as e:
            logger.error("Error during shutdown", error=str(e))

        logger.info("Application shutdown complete")


async def main() -> None:
    """Main application entry point."""
    args = parse_args()
    setup_logging(debug=args.debug)

    logger = structlog.get_logger()
    logger.info("Starting Claude Code Telegram Bot", version=__version__)

    try:
        # Load configuration
        from src.config import FeatureFlags, load_config

        config = load_config(
            config_file=args.config_file,
            extra_env_files=args.env_file,
        )
        features = FeatureFlags(config)

        logger.info(
            "Configuration loaded",
            environment="production" if config.is_production else "development",
            enabled_features=features.get_enabled_features(),
            debug=config.debug,
        )

        # Initialize bot and Claude integration
        app = await create_application(config)
        await run_application(app)

    except ConfigurationError as e:
        logger.error("Configuration error", error=str(e))
        sys.exit(1)
    except Exception as e:
        logger.exception("Unexpected error", error=str(e))
        sys.exit(1)


def run() -> None:
    """Synchronous entry point for setuptools."""
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nShutdown requested by user")
        sys.exit(0)


if __name__ == "__main__":
    run()
