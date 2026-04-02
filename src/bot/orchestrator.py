"""Message orchestrator — single entry point for all Telegram updates.

Routes messages based on agentic vs classic mode. In agentic mode, provides
a minimal conversational interface (3 commands, no inline keyboards). In
classic mode, delegates to existing full-featured handlers.
"""

import asyncio
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import structlog
from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from ..claude.concurrency import RAMGatedExecutor
from ..claude.sdk_integration import StreamUpdate
from ..config.settings import Settings
from ..projects import PrivateTopicsUnavailableError
from .utils.draft_streamer import DraftStreamer, generate_draft_id
from .utils.html_format import escape_html
from .utils.image_extractor import (
    ImageAttachment,
    should_send_as_photo,
    validate_image_path,
)

logger = structlog.get_logger()


def _format_size(size_bytes: int) -> str:
    """Format byte size to human-readable string."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    else:
        return f"{size_bytes / (1024 * 1024):.1f} MB"


_MEDIA_TYPE_MAP = {
    "png": "image/png",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
}

# Patterns that look like secrets/credentials in CLI arguments
_SECRET_PATTERNS: List[re.Pattern[str]] = [
    # API keys / tokens (sk-ant-..., sk-..., ghp_..., gho_..., github_pat_..., xoxb-...)
    re.compile(
        r"(sk-ant-api\d*-[A-Za-z0-9_-]{10})[A-Za-z0-9_-]*"
        r"|(sk-[A-Za-z0-9_-]{20})[A-Za-z0-9_-]*"
        r"|(ghp_[A-Za-z0-9]{5})[A-Za-z0-9]*"
        r"|(gho_[A-Za-z0-9]{5})[A-Za-z0-9]*"
        r"|(github_pat_[A-Za-z0-9_]{5})[A-Za-z0-9_]*"
        r"|(xoxb-[A-Za-z0-9]{5})[A-Za-z0-9-]*"
    ),
    # AWS access keys
    re.compile(r"(AKIA[0-9A-Z]{4})[0-9A-Z]{12}"),
    # Generic long hex/base64 tokens after common flags/env patterns
    re.compile(
        r"((?:--token|--secret|--password|--api-key|--apikey|--auth)"
        r"[= ]+)['\"]?[A-Za-z0-9+/_.:-]{8,}['\"]?"
    ),
    # Inline env assignments like KEY=value
    re.compile(
        r"((?:TOKEN|SECRET|PASSWORD|API_KEY|APIKEY|AUTH_TOKEN|PRIVATE_KEY"
        r"|ACCESS_KEY|CLIENT_SECRET|WEBHOOK_SECRET)"
        r"=)['\"]?[^\s'\"]{8,}['\"]?"
    ),
    # Bearer / Basic auth headers
    re.compile(r"(Bearer )[A-Za-z0-9+/_.:-]{8,}" r"|(Basic )[A-Za-z0-9+/=]{8,}"),
    # Connection strings with credentials  user:pass@host
    re.compile(r"://([^:]+:)[^@]{4,}(@)"),
]


def _redact_secrets(text: str) -> str:
    """Replace likely secrets/credentials with redacted placeholders."""
    result = text
    for pattern in _SECRET_PATTERNS:
        result = pattern.sub(
            lambda m: next((g + "***" for g in m.groups() if g is not None), "***"),
            result,
        )
    return result


# Tool name -> friendly emoji mapping for verbose output
_TOOL_ICONS: Dict[str, str] = {
    "Read": "\U0001f4d6",
    "Write": "\u270f\ufe0f",
    "Edit": "\u270f\ufe0f",
    "MultiEdit": "\u270f\ufe0f",
    "Bash": "\U0001f4bb",
    "Glob": "\U0001f50d",
    "Grep": "\U0001f50d",
    "LS": "\U0001f4c2",
    "Task": "\U0001f9e0",
    "TaskOutput": "\U0001f9e0",
    "WebFetch": "\U0001f310",
    "WebSearch": "\U0001f310",
    "NotebookRead": "\U0001f4d3",
    "NotebookEdit": "\U0001f4d3",
    "TodoRead": "\u2611\ufe0f",
    "TodoWrite": "\u2611\ufe0f",
    "AskUserQuestion": "\U0001f914",
    "EnterPlanMode": "\U0001f4cb",
    "ExitPlanMode": "\u2705",
    "Skill": "\u2699\ufe0f",
}


def _tool_icon(name: str) -> str:
    """Return emoji for a tool, with a default wrench."""
    return _TOOL_ICONS.get(name, "\U0001f527")


# ── Interactive tool call formatting (surface AskUserQuestion etc.) ──

_INTERACTIVE_TOOLS = frozenset(
    {"AskUserQuestion", "EnterPlanMode", "ExitPlanMode", "TodoWrite"}
)


def _format_interactive_tool(name: str, tool_input: Dict[str, Any]) -> Optional[str]:
    """Format an interactive tool call as an informational Telegram message.

    Returns HTML-formatted text, or None if the tool isn't interactive.
    """
    if name == "AskUserQuestion":
        questions = tool_input.get("questions", [])
        if not questions:
            return None
        lines: List[str] = ["\U0001f914 <b>Claude is asking:</b>\n"]
        for q in questions:
            lines.append(f"<b>{escape_html(q.get('question', ''))}</b>")
            for opt in q.get("options", []):
                label = escape_html(opt.get("label", ""))
                desc = escape_html(opt.get("description", ""))
                lines.append(f"  \u2022 {label} \u2014 {desc}")
            lines.append("")
        return "\n".join(lines).rstrip()

    if name == "EnterPlanMode":
        return (
            "\U0001f4cb <b>Claude entered plan mode</b> \u2014 "
            "exploring codebase and designing approach\u2026"
        )

    if name == "ExitPlanMode":
        return "\u2705 <b>Plan complete</b> \u2014 Claude is ready to implement."

    if name == "TodoWrite":
        todos = tool_input.get("todos", [])
        if not todos:
            return None
        status_icons = {
            "completed": "\u2705",
            "in_progress": "\U0001f504",
            "pending": "\u2b1c",
        }
        lines_td: List[str] = ["\U0001f4dd <b>Task list updated:</b>\n"]
        for t in todos:
            icon = status_icons.get(t.get("status", ""), "\u2b1c")
            lines_td.append(f"{icon} {escape_html(t.get('content', ''))}")
        return "\n".join(lines_td)

    return None


@dataclass
class ActiveRequest:
    """Tracks an in-flight Claude request so it can be interrupted."""

    user_id: int
    interrupt_event: asyncio.Event = field(default_factory=asyncio.Event)
    interrupted: bool = False
    progress_msg: Any = None  # telegram Message object


class MessageOrchestrator:
    """Routes messages based on mode. Single entry point for all Telegram updates."""

    def __init__(self, settings: Settings, deps: Dict[str, Any]):
        self.settings = settings
        self.deps = deps
        self._active_requests: Dict[str, ActiveRequest] = {}
        self._known_commands: frozenset[str] = frozenset()
        # RAM-gated parallel execution for project threads
        self._executor: Optional[RAMGatedExecutor] = None
        if getattr(settings, "enable_project_threads", False):
            self._executor = RAMGatedExecutor(
                ram_threshold_pct=getattr(settings, "ram_threshold_pct", 90.0),
                max_concurrent=getattr(settings, "max_concurrent_sdk", 5),
            )

    def _inject_deps(self, handler: Callable) -> Callable:  # type: ignore[type-arg]
        """Wrap handler to inject dependencies into context.bot_data."""

        async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            for key, value in self.deps.items():
                context.bot_data[key] = value
            context.bot_data["settings"] = self.settings
            assert context.user_data is not None
            context.user_data.pop("_thread_context", None)

            is_sync_bypass = handler.__name__ == "sync_threads"
            is_start_bypass = handler.__name__ in {"start_command", "agentic_start"}
            message_thread_id = self._extract_message_thread_id(update)
            should_enforce = self.settings.enable_project_threads

            if should_enforce:
                if self.settings.project_threads_mode == "private":
                    should_enforce = not is_sync_bypass and not (
                        is_start_bypass and message_thread_id is None
                    )
                else:
                    should_enforce = not is_sync_bypass

            if should_enforce:
                allowed = await self._apply_thread_routing_context(update, context)
                if not allowed:
                    return

            try:
                await handler(update, context)
            finally:
                if should_enforce:
                    self._persist_thread_state(context)

        return wrapped

    async def _apply_thread_routing_context(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> bool:
        """Enforce strict project-thread routing and load thread-local state."""
        manager = context.bot_data.get("project_threads_manager")
        if manager is None:
            await self._reject_for_thread_mode(
                update,
                "❌ <b>Project Thread Mode Misconfigured</b>\n\n"
                "Thread manager is not initialized.",
            )
            return False

        chat = update.effective_chat
        message = update.effective_message
        if not chat or not message:
            return False

        if self.settings.project_threads_mode == "group":
            if chat.id != self.settings.project_threads_chat_id:
                await self._reject_for_thread_mode(
                    update,
                    manager.guidance_message(mode=self.settings.project_threads_mode),
                )
                return False
        else:
            if getattr(chat, "type", "") != "private":
                await self._reject_for_thread_mode(
                    update,
                    manager.guidance_message(mode=self.settings.project_threads_mode),
                )
                return False

        message_thread_id = self._extract_message_thread_id(update)
        if not message_thread_id:
            logger.warning(
                "Thread routing rejected: no thread ID",
                chat_id=chat.id,
                chat_type=getattr(chat, "type", None),
            )
            await self._reject_for_thread_mode(
                update,
                manager.guidance_message(mode=self.settings.project_threads_mode),
            )
            return False

        project = await manager.resolve_project(chat.id, message_thread_id)
        if not project:
            logger.warning(
                "Thread routing rejected: no project for thread",
                chat_id=chat.id,
                message_thread_id=message_thread_id,
            )
            await self._reject_for_thread_mode(
                update,
                manager.guidance_message(mode=self.settings.project_threads_mode),
            )
            return False

        # Notification topics are read-only (no workspace directory)
        if project.absolute_path is None:
            await self._reject_for_thread_mode(
                update,
                f"\U0001f4e2 <b>{escape_html(project.name)}</b> "
                "is a notification-only topic.\n\n"
                "Please use a project topic to interact with the bot.",
            )
            return False

        assert context.user_data is not None
        state_key = f"{chat.id}:{message_thread_id}"
        thread_states = context.user_data.setdefault("thread_state", {})
        state = thread_states.get(state_key, {})

        project_root = project.absolute_path
        current_dir_raw = state.get("current_directory")
        current_dir = (
            Path(current_dir_raw).resolve() if current_dir_raw else project_root
        )
        if not self._is_within(current_dir, project_root) or not current_dir.is_dir():
            current_dir = project_root

        # NOTE: Do NOT write current_directory / claude_session_id to the
        # shared context.user_data top-level keys.  With parallel threads
        # those keys would race.  Instead, carry them inside
        # _thread_context and let the handler read from there.
        loaded_session_id = state.get("claude_session_id")
        context.user_data["_thread_context"] = {
            "chat_id": chat.id,
            "message_thread_id": message_thread_id,
            "state_key": state_key,
            "project_slug": project.slug,
            "project_root": str(project_root),
            "project_name": project.name,
            "current_directory": current_dir,
            "claude_session_id": loaded_session_id,
            # Snapshots of values at load time — used by _persist_thread_state
            # to detect whether the handler updated via _thread_context or via
            # the shared user_data keys (legacy path).
            "_loaded_session_id": loaded_session_id,
            "_loaded_current_directory": current_dir,
        }
        # Legacy compat: handlers that don't know about _thread_context
        # still read these.  Safe because _inject_deps holds the per-thread
        # update-processor lock, so only ONE handler per state_key is in
        # this section at a time.
        context.user_data["current_directory"] = current_dir
        context.user_data["claude_session_id"] = state.get("claude_session_id")
        return True

    def _persist_thread_state(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Persist thread-local state back into per-thread storage.

        Detects whether the handler updated values via the thread-safe
        ``_thread_context`` dict (new path) or via the shared
        ``context.user_data`` top-level keys (legacy path) and persists
        whichever changed.
        """
        assert context.user_data is not None
        thread_context = context.user_data.get("_thread_context")
        if not thread_context:
            return

        project_root = Path(thread_context["project_root"])

        # --- session_id ---
        tc_session = thread_context.get("claude_session_id")
        loaded_session = thread_context.get("_loaded_session_id")
        shared_session = context.user_data.get("claude_session_id")
        if tc_session != loaded_session:
            # Thread-aware handler updated _thread_context → prefer it
            session_id = tc_session
        elif shared_session != loaded_session:
            # Legacy handler updated the shared key → use it
            session_id = shared_session
        else:
            session_id = tc_session  # unchanged

        # --- current_directory ---
        tc_dir = thread_context.get("current_directory")
        loaded_dir = thread_context.get("_loaded_current_directory")
        shared_dir = context.user_data.get("current_directory")
        if tc_dir != loaded_dir:
            current_dir = tc_dir
        elif shared_dir != loaded_dir:
            current_dir = shared_dir
        else:
            current_dir = tc_dir

        if current_dir is None:
            current_dir = project_root
        if not isinstance(current_dir, Path):
            current_dir = Path(str(current_dir))
        current_dir = current_dir.resolve()
        if not self._is_within(current_dir, project_root) or not current_dir.is_dir():
            current_dir = project_root

        thread_states = context.user_data.setdefault("thread_state", {})
        thread_states[thread_context["state_key"]] = {
            "current_directory": str(current_dir),
            "claude_session_id": session_id,
            "project_slug": thread_context["project_slug"],
        }

    @staticmethod
    def _is_within(path: Path, root: Path) -> bool:
        """Return True if path is within root."""
        try:
            path.relative_to(root)
            return True
        except ValueError:
            return False

    @staticmethod
    def _extract_message_thread_id(update: Update) -> Optional[int]:
        """Extract topic/thread id from update message for forum/direct topics."""
        message = update.effective_message
        if not message:
            return None
        message_thread_id = getattr(message, "message_thread_id", None)
        if isinstance(message_thread_id, int) and message_thread_id > 0:
            logger.debug(
                "Extracted thread ID from message_thread_id",
                message_thread_id=message_thread_id,
            )
            return message_thread_id
        dm_topic = getattr(message, "direct_messages_topic", None)
        topic_id = getattr(dm_topic, "topic_id", None) if dm_topic else None
        if isinstance(topic_id, int) and topic_id > 0:
            logger.debug(
                "Extracted thread ID from direct_messages_topic",
                topic_id=topic_id,
            )
            return topic_id
        # Telegram omits message_thread_id for the General topic in forum
        # supergroups; its canonical thread ID is 1.
        chat = update.effective_chat
        if chat and getattr(chat, "is_forum", False):
            return 1
        logger.debug(
            "No thread ID found in update",
            has_message_thread_id=message_thread_id is not None,
            message_thread_id_value=message_thread_id,
            has_dm_topic=dm_topic is not None,
            dm_topic_id=topic_id,
            chat_type=getattr(update.effective_chat, "type", None),
            chat_id=getattr(update.effective_chat, "id", None),
            is_forum=getattr(update.effective_chat, "is_forum", None),
        )
        return None

    async def _reject_for_thread_mode(self, update: Update, message: str) -> None:
        """Send a guidance response when strict thread routing rejects an update."""
        query = update.callback_query
        if query:
            try:
                await query.answer()
            except Exception:
                pass
            msg = query.message
            if msg and hasattr(msg, "reply_text"):
                await msg.reply_text(message, parse_mode="HTML")  # type: ignore[union-attr]
            return

        if update.effective_message:
            await update.effective_message.reply_text(message, parse_mode="HTML")

    def register_handlers(self, app: Application) -> None:
        """Register handlers based on mode."""
        if self.settings.agentic_mode:
            self._register_agentic_handlers(app)
        else:
            self._register_classic_handlers(app)

    def _register_agentic_handlers(self, app: Application) -> None:
        """Register agentic handlers: commands + text/file/photo."""
        from .handlers import command

        # Commands
        handlers = [
            ("start", self.agentic_start),
            ("new", self.agentic_new),
            ("resume", self.agentic_resume),
            ("status", self.agentic_status),
            ("status_all", self.agentic_status_all),
            ("verbose", self.agentic_verbose),
            ("repo", self.agentic_repo),
            ("version", self.agentic_version),
            ("restart", command.restart_command),
            ("debug_show_config", command.debug_show_config),
        ]
        if self.settings.enable_project_threads:
            handlers.append(("sync_threads", command.sync_threads))

        self._known_commands = frozenset(cmd for cmd, _ in handlers)

        for cmd, handler in handlers:
            app.add_handler(CommandHandler(cmd, self._inject_deps(handler)))

        # Text messages -> Claude
        app.add_handler(
            MessageHandler(
                filters.TEXT & ~filters.COMMAND,
                self._inject_deps(self.agentic_text),
            ),
            group=10,
        )

        # Unknown slash commands -> Claude (passthrough in agentic mode).
        # Registered commands are handled by CommandHandlers in group 0;
        # exclude them at the filter level so the _inject_deps wrapper
        # (and its thread enforcement) never fires for known commands in
        # this group — preventing spurious "Project Thread Required"
        # rejections.
        _known_cmd_pattern = re.compile(
            r"^/("
            + "|".join(re.escape(c) for c in self._known_commands)
            + r")(@\S+)?(\s|$)",
            re.IGNORECASE,
        )
        app.add_handler(
            MessageHandler(
                filters.COMMAND & ~filters.Regex(_known_cmd_pattern),
                self._inject_deps(self._handle_unknown_command),
            ),
            group=10,
        )

        # File uploads -> Claude
        app.add_handler(
            MessageHandler(
                filters.Document.ALL, self._inject_deps(self.agentic_document)
            ),
            group=10,
        )

        # Photo uploads -> Claude
        app.add_handler(
            MessageHandler(filters.PHOTO, self._inject_deps(self.agentic_photo)),
            group=10,
        )

        # Voice messages -> transcribe -> Claude
        app.add_handler(
            MessageHandler(filters.VOICE, self._inject_deps(self.agentic_voice)),
            group=10,
        )

        # Stop button callback (must be before cd: handler)
        app.add_handler(
            CallbackQueryHandler(
                self._inject_deps(self._handle_stop_callback),
                pattern=r"^stop:",
            )
        )

        # Only cd: callbacks (for project selection), scoped by pattern
        app.add_handler(
            CallbackQueryHandler(
                self._inject_deps(self._agentic_callback),
                pattern=r"^cd:",
            )
        )

        logger.info("Agentic handlers registered")

    def _register_classic_handlers(self, app: Application) -> None:
        """Register full classic handler set (moved from core.py)."""
        from .handlers import callback, command, message

        handlers = [
            ("start", command.start_command),
            ("help", command.help_command),
            ("new", command.new_session),
            ("continue", command.continue_session),
            ("end", command.end_session),
            ("ls", command.list_files),
            ("cd", command.change_directory),
            ("pwd", command.print_working_directory),
            ("projects", command.show_projects),
            ("status", command.session_status),
            ("export", command.export_session),
            ("actions", command.quick_actions),
            ("git", command.git_command),
            ("restart", command.restart_command),
            ("debug_show_config", command.debug_show_config),
        ]
        if self.settings.enable_project_threads:
            handlers.append(("sync_threads", command.sync_threads))

        for cmd, handler in handlers:
            app.add_handler(CommandHandler(cmd, self._inject_deps(handler)))

        app.add_handler(
            MessageHandler(
                filters.TEXT & ~filters.COMMAND,
                self._inject_deps(message.handle_text_message),
            ),
            group=10,
        )
        app.add_handler(
            MessageHandler(
                filters.Document.ALL, self._inject_deps(message.handle_document)
            ),
            group=10,
        )
        app.add_handler(
            MessageHandler(filters.PHOTO, self._inject_deps(message.handle_photo)),
            group=10,
        )
        app.add_handler(
            MessageHandler(filters.VOICE, self._inject_deps(message.handle_voice)),
            group=10,
        )
        app.add_handler(
            CallbackQueryHandler(self._inject_deps(callback.handle_callback_query))
        )

        logger.info("Classic handlers registered (13 commands + full handler set)")

    async def get_bot_commands(self) -> list:  # type: ignore[type-arg]
        """Return bot commands appropriate for current mode."""
        if self.settings.agentic_mode:
            commands = [
                BotCommand("start", "Start the bot"),
                BotCommand("new", "Start a fresh session"),
                BotCommand("resume", "Resume a previous session"),
                BotCommand("status", "Show session status"),
                BotCommand("status_all", "Show all sessions (DB + FS)"),
                BotCommand("verbose", "Set output verbosity (0/1/2)"),
                BotCommand("repo", "List repos / switch workspace"),
                BotCommand("version", "Show version and recent commits"),
                BotCommand("restart", "Restart the bot"),
                BotCommand("sync_threads", "Sync project topics"),
                BotCommand("debug_show_config", "Show current bot config"),
            ]
            return commands
        else:
            commands = [
                BotCommand("start", "Start bot and show help"),
                BotCommand("help", "Show available commands"),
                BotCommand("new", "Clear context and start fresh session"),
                BotCommand("continue", "Explicitly continue last session"),
                BotCommand("end", "End current session and clear context"),
                BotCommand("ls", "List files in current directory"),
                BotCommand("cd", "Change directory (resumes project session)"),
                BotCommand("pwd", "Show current directory"),
                BotCommand("projects", "Show all projects"),
                BotCommand("status", "Show session status"),
                BotCommand("export", "Export current session"),
                BotCommand("actions", "Show quick actions"),
                BotCommand("git", "Git repository commands"),
                BotCommand("restart", "Restart the bot"),
                BotCommand("sync_threads", "Sync project topics"),
                BotCommand("debug_show_config", "Show current bot config"),
            ]
            return commands

    # --- Agentic handlers ---

    async def agentic_start(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Brief welcome, no buttons."""
        assert update.message is not None
        assert update.effective_user is not None
        assert context.user_data is not None
        user = update.effective_user
        sync_line = ""
        if (
            self.settings.enable_project_threads
            and self.settings.project_threads_mode == "private"
        ):
            if (
                not update.effective_chat
                or getattr(update.effective_chat, "type", "") != "private"
            ):
                await update.message.reply_text(
                    "🚫 <b>Private Topics Mode</b>\n\n"
                    "Use this bot in a private chat and run <code>/start</code> there.",
                    parse_mode="HTML",
                )
                return
            manager = context.bot_data.get("project_threads_manager")
            if manager:
                try:
                    # Pre-resolve existing topics via Telethon (if enabled)
                    if self.settings.enable_topic_resolution:
                        from src.projects.topic_resolver import (
                            resolve_topics_if_enabled,
                        )

                        resolved = await resolve_topics_if_enabled(
                            enable_topic_resolution=True,
                            api_id=self.settings.telegram_api_id,
                            api_hash=self.settings.telegram_api_hash,
                            bot_token=self.settings.telegram_token_str,
                            chat_id=update.effective_chat.id,
                        )
                        manager.set_resolved_topics(resolved)

                    result = await manager.sync_topics(
                        context.bot,
                        chat_id=update.effective_chat.id,
                    )
                    # Fire commit notifications after sync
                    registry = context.bot_data.get("project_registry")
                    storage = context.bot_data.get("storage")
                    if registry and storage:
                        from src.notifications.commit_notifier import (
                            fire_commit_notifications,
                        )

                        fire_commit_notifications(
                            bot=context.bot,
                            chat_id=update.effective_chat.id,
                            registry=registry,
                            thread_repo=storage.project_threads,
                        )
                    sync_line = (
                        "\n\n🧵 Topics synced"
                        f" (created {result.created}, reused {result.reused})."
                    )
                except PrivateTopicsUnavailableError:
                    await update.message.reply_text(
                        manager.private_topics_unavailable_message(),
                        parse_mode="HTML",
                    )
                    return
                except Exception:
                    sync_line = "\n\n🧵 Topic sync failed. Run /sync_threads to retry."
        current_dir = context.user_data.get(
            "current_directory", self.settings.approved_directory
        )
        dir_display = f"<code>{current_dir}/</code>"

        safe_name = escape_html(user.first_name)
        await update.message.reply_text(
            f"Hi {safe_name}! I'm your AI coding assistant.\n"
            f"Just tell me what you need — I can read, write, and run code.\n\n"
            f"Working in: {dir_display}\n"
            f"Commands: /new (reset) · /status"
            f"{sync_line}",
            parse_mode="HTML",
        )

    async def agentic_new(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Reset session, one-line confirmation."""
        assert update.message is not None
        assert context.user_data is not None
        thread_ctx = context.user_data.get("_thread_context")
        if thread_ctx:
            thread_ctx["claude_session_id"] = None
            thread_ctx["force_new_session"] = True
        else:
            context.user_data["claude_session_id"] = None
            context.user_data["force_new_session"] = True
        context.user_data["session_started"] = True

        await update.message.reply_text("Session reset. What's next?")

    async def agentic_resume(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Resume a previous session by ID prefix, or the most recent one."""
        assert update.message is not None
        assert update.effective_user is not None
        assert context.user_data is not None

        thread_ctx = context.user_data.get("_thread_context")
        if thread_ctx:
            current_dir = thread_ctx.get(
                "current_directory", self.settings.approved_directory
            )
        else:
            current_dir = context.user_data.get(
                "current_directory", self.settings.approved_directory
            )

        claude_integration = context.bot_data.get("claude_integration")
        if not claude_integration:
            await update.message.reply_text("Claude integration not available.")
            return

        sessions = await claude_integration.find_sessions_for_resume(
            update.effective_user.id, Path(str(current_dir))
        )

        # Parse argument: /resume, /resume list, /resume <prefix>
        arg = ""
        if update.message.text:
            parts = update.message.text.split(maxsplit=1)
            if len(parts) > 1:
                arg = parts[1].strip()

        # /resume list — show available sessions
        if arg.lower() == "list":
            if not sessions:
                await update.message.reply_text(
                    "No resumable sessions in this directory."
                )
                return
            lines = ["<b>Resumable sessions:</b>\n"]
            for s in sessions[:10]:
                age_seconds = time.time() - s.last_used.timestamp()
                if age_seconds < 3600:
                    age_str = f"{int(age_seconds / 60)}m ago"
                elif age_seconds < 86400:
                    age_str = f"{int(age_seconds / 3600)}h ago"
                else:
                    age_str = f"{int(age_seconds / 86400)}d ago"
                sid = s.session_id[:8]
                lines.append(
                    f"• <code>{sid}</code> — {s.message_count} msgs, "
                    f"${s.total_cost:.2f}, {age_str}"
                )
            lines.append(f"\nUse <code>/resume &lt;id&gt;</code> to resume.")
            await update.message.reply_text("\n".join(lines), parse_mode="HTML")
            return

        # /resume <prefix> — match by prefix
        if arg:
            prefix = arg.lower()
            matches = [s for s in sessions if s.session_id.lower().startswith(prefix)]
            if not matches:
                await update.message.reply_text(
                    f"No session matching <code>{escape_html(prefix)}</code>.\n"
                    "Use <code>/resume list</code> to see available sessions.",
                    parse_mode="HTML",
                )
                return
            if len(matches) > 1:
                abbrevs = ", ".join(f"<code>{s.session_id[:8]}</code>" for s in matches)
                await update.message.reply_text(
                    f"Ambiguous prefix — matches {len(matches)} sessions: {abbrevs}\n"
                    "Provide more characters.",
                    parse_mode="HTML",
                )
                return
            target = matches[0]
        else:
            # /resume (no args) — most recent
            if not sessions:
                await update.message.reply_text(
                    "No sessions to resume. Send a message to start one."
                )
                return
            target = sessions[0]

        # Set session and clear force_new
        if thread_ctx:
            thread_ctx["claude_session_id"] = target.session_id
            thread_ctx["force_new_session"] = False
        else:
            context.user_data["claude_session_id"] = target.session_id
            context.user_data["force_new_session"] = False

        sid = target.session_id[:8]
        await update.message.reply_text(
            f"Resumed session <code>{sid}</code> "
            f"({target.message_count} msgs, ${target.total_cost:.2f}). "
            "Send a message to continue.",
            parse_mode="HTML",
        )

    async def agentic_version(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Show version info: branch, SHA, and last 5 commits."""
        assert update.message is not None
        import subprocess

        # Use the bot's own installation directory (not approved_directory
        # which may be a parent like /home/user/).
        repo_dir = Path(__file__).resolve().parent.parent.parent
        git_dir = repo_dir / ".git"
        if not git_dir.exists():
            await update.message.reply_text(
                "ℹ️ No .git directory found — version info unavailable.",
            )
            return

        try:
            branch = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                capture_output=True,
                text=True,
                cwd=str(repo_dir),
                timeout=5,
            ).stdout.strip()

            sha = subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                capture_output=True,
                text=True,
                cwd=str(repo_dir),
                timeout=5,
            ).stdout.strip()

            log_output = subprocess.run(
                [
                    "git",
                    "log",
                    "--format=%h  %ad  %an%n      %s",
                    "--date=format:%Y-%m-%d %H:%M",
                    "-5",
                ],
                capture_output=True,
                text=True,
                cwd=str(repo_dir),
                timeout=5,
            ).stdout.strip()

            lines = [
                f"🏷 <b>Branch:</b> <code>{branch}</code>",
                f"🔖 <b>SHA:</b> <code>{sha}</code>",
                "",
                "<b>Last 5 commits:</b>",
                f"<pre>{log_output}</pre>",
            ]

            await update.message.reply_text("\n".join(lines), parse_mode="HTML")
        except Exception as e:
            await update.message.reply_text(f"⚠️ Failed to read git info: {e}")

    async def agentic_status(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Compact one-line status, no buttons."""
        assert update.message is not None
        assert update.effective_user is not None
        assert context.user_data is not None
        current_dir = context.user_data.get(
            "current_directory", self.settings.approved_directory
        )
        dir_display = str(current_dir)

        session_id = context.user_data.get("claude_session_id")
        session_status = "active" if session_id else "none"

        # Cost info
        cost_str = ""
        rate_limiter = context.bot_data.get("rate_limiter")
        if rate_limiter:
            try:
                user_status = rate_limiter.get_user_status(update.effective_user.id)
                cost_usage = user_status.get("cost_usage", {})
                current_cost = cost_usage.get("current", 0.0)
                cost_str = f" · Cost: ${current_cost:.2f}"
            except Exception:
                pass

        await update.message.reply_text(
            f"📂 {dir_display} · Session: {session_status}{cost_str}"
        )

    async def agentic_status_all(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Unified view of all sessions from DB and filesystem."""
        assert update.message is not None
        assert update.effective_user is not None

        from ..claude.fs_sessions import discover_fs_sessions

        # 1. Get all DB sessions (active + inactive)
        storage = context.bot_data.get("storage")
        db_sessions: list = []
        if storage:
            try:
                db_sessions = await storage.get_all_sessions_all_states()
            except Exception as e:
                logger.warning("Failed to load DB sessions", error=str(e))

        # 2. Discover filesystem sessions (sync disk I/O in thread)
        try:
            fs_sessions = await asyncio.to_thread(discover_fs_sessions)
        except Exception as e:
            logger.warning("Failed to discover FS sessions", error=str(e))
            fs_sessions = []

        # 3. Build the response
        db_count = len(db_sessions)
        fs_count = len(fs_sessions)
        running_count = sum(1 for fs in fs_sessions if fs.is_running)

        lines: list[str] = []
        lines.append(
            f"<b>All Sessions</b>  "
            f"({db_count} DB / {fs_count} FS / {running_count} running)\n"
        )

        # --- DB Sessions ---
        if db_sessions:
            lines.append("<b>DB Sessions:</b>")
            for s in db_sessions[:20]:
                icon = "\u25cf" if s.is_active else "\u25cb"
                sid_short = s.session_id[:8] if s.session_id else "?"
                path_short = (
                    s.project_path.replace("/home/usr200", "~")
                    if s.project_path
                    else "?"
                )
                active_label = "active" if s.is_active else "inactive"
                cost_str = f"${s.total_cost:.2f}" if s.total_cost else "$0.00"
                lines.append(
                    f"  {icon} <code>{escape_html(sid_short)}</code>"
                    f" {escape_html(path_short)}"
                    f" ({active_label}, {s.message_count} msgs, {cost_str})"
                )
            if db_count > 20:
                lines.append(f"  ... and {db_count - 20} more")
        else:
            lines.append("<b>DB Sessions:</b> none")

        lines.append("")

        # --- FS Sessions ---
        if fs_sessions:
            lines.append("<b>FS Sessions:</b>")
            db_sid_set = {s.session_id for s in db_sessions}
            for fs in fs_sessions[:30]:
                if fs.is_running:
                    icon = "\U0001f7e2"
                    run_label = f" [PID {fs.running_pid}]"
                else:
                    icon = "\u26aa"
                    run_label = ""

                sid_short = fs.session_id[:8]
                size_str = _format_size(fs.total_size_bytes)
                db_tag = " [DB]" if fs.session_id in db_sid_set else ""
                sidechain_tag = " (sidechain)" if fs.is_sidechain else ""

                label = fs.custom_title or (
                    fs.first_prompt[:40] if fs.first_prompt else sid_short
                )
                label = label.replace("\n", " ")

                lines.append(
                    f"  {icon} <code>{escape_html(sid_short)}</code>"
                    f" ({size_str}, {fs.message_count} msgs)"
                    f"{run_label}{db_tag}{sidechain_tag}"
                )
            if fs_count > 30:
                lines.append(f"  ... and {fs_count - 30} more")
        else:
            lines.append("<b>FS Sessions:</b> none")

        # --- Cross-reference ---
        db_sid_set = {s.session_id for s in db_sessions}
        fs_sid_set = {fs.session_id for fs in fs_sessions}
        db_only = db_sid_set - fs_sid_set
        fs_only = fs_sid_set - db_sid_set
        if db_only or fs_only:
            lines.append("")
            lines.append("<b>Cross-reference:</b>")
            if db_only:
                lines.append(f"  DB-only (no FS data): {len(db_only)}")
            if fs_only:
                lines.append(f"  FS-only (not in DB): {len(fs_only)}")

        response_text = "\n".join(lines)
        if len(response_text) > 4000:
            response_text = response_text[:3990] + "\n\n<i>(truncated)</i>"

        await update.message.reply_text(response_text, parse_mode="HTML")

    def _get_verbose_level(self, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Return effective verbose level: per-user override or global default."""
        assert context.user_data is not None
        user_override = context.user_data.get("verbose_level")
        if user_override is not None:
            return int(user_override)
        return self.settings.verbose_level

    async def agentic_verbose(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Set output verbosity: /verbose [0|1|2]."""
        assert update.message is not None
        assert context.user_data is not None
        args = update.message.text.split()[1:] if update.message.text else []
        if not args:
            current = self._get_verbose_level(context)
            labels = {0: "quiet", 1: "normal", 2: "detailed"}
            await update.message.reply_text(
                f"Verbosity: <b>{current}</b> ({labels.get(current, '?')})\n\n"
                "Usage: <code>/verbose 0|1|2</code>\n"
                "  0 = quiet (final response only)\n"
                "  1 = normal (tools + reasoning)\n"
                "  2 = detailed (tools with inputs + reasoning)",
                parse_mode="HTML",
            )
            return

        try:
            level = int(args[0])
            if level not in (0, 1, 2):
                raise ValueError
        except ValueError:
            await update.message.reply_text(
                "Please use: /verbose 0, /verbose 1, or /verbose 2"
            )
            return

        context.user_data["verbose_level"] = level
        labels = {0: "quiet", 1: "normal", 2: "detailed"}
        await update.message.reply_text(
            f"Verbosity set to <b>{level}</b> ({labels[level]})",
            parse_mode="HTML",
        )

    def _format_verbose_progress(
        self,
        activity_log: List[Dict[str, Any]],
        verbose_level: int,
        start_time: float,
    ) -> str:
        """Build the progress message text based on activity so far."""
        if not activity_log:
            return "Working..."

        elapsed = time.time() - start_time
        lines: List[str] = [f"Working... ({elapsed:.0f}s)\n"]

        for entry in activity_log[-15:]:  # Show last 15 entries max
            kind = entry.get("kind", "tool")
            if kind == "text":
                # Claude's intermediate reasoning/commentary
                snippet = entry.get("detail", "")
                if verbose_level >= 2:
                    lines.append(f"\U0001f4ac {snippet}")
                else:
                    # Level 1: one short line
                    lines.append(f"\U0001f4ac {snippet[:80]}")
            else:
                # Tool call
                icon = _tool_icon(entry["name"])
                if verbose_level >= 2 and entry.get("detail"):
                    lines.append(f"{icon} {entry['name']}: {entry['detail']}")
                else:
                    lines.append(f"{icon} {entry['name']}")

        if len(activity_log) > 15:
            lines.insert(1, f"... ({len(activity_log) - 15} earlier entries)\n")

        return "\n".join(lines)

    @staticmethod
    def _summarize_tool_input(tool_name: str, tool_input: Dict[str, Any]) -> str:
        """Return a short summary of tool input for verbose level 2."""
        if not tool_input:
            return ""
        if tool_name in ("Read", "Write", "Edit", "MultiEdit"):
            path = tool_input.get("file_path") or tool_input.get("path", "")
            if path:
                return str(path).rsplit("/", 1)[-1]
        if tool_name in ("Glob", "Grep"):
            pattern = tool_input.get("pattern", "")
            if pattern:
                return str(pattern)[:60]
        if tool_name == "Bash":
            cmd = tool_input.get("command", "")
            if cmd:
                return _redact_secrets(str(cmd)[:100])[:80]
        if tool_name in ("WebFetch", "WebSearch"):
            return str(tool_input.get("url", "") or tool_input.get("query", ""))[:60]
        if tool_name == "Task":
            desc = tool_input.get("description", "")
            if desc:
                return str(desc)[:60]
        if tool_name == "AskUserQuestion":
            questions = tool_input.get("questions", [])
            if questions:
                return str(questions[0].get("question", ""))[:60]
            return ""
        if tool_name == "TodoWrite":
            todos = tool_input.get("todos", [])
            in_prog = [t for t in todos if t.get("status") == "in_progress"]
            if in_prog:
                return str(in_prog[0].get("activeForm", ""))[:60]
            return f"{len(todos)} items"
        if tool_name in ("EnterPlanMode", "ExitPlanMode"):
            return ""
        # Generic: show first key's value
        for v in tool_input.values():
            if isinstance(v, str) and v:
                return v[:60]
        return ""

    @staticmethod
    def _start_typing_heartbeat(
        chat: Any,
        interval: float = 2.0,
    ) -> "asyncio.Task[None]":
        """Start a background typing indicator task.

        Sends typing every *interval* seconds, independently of
        stream events. Cancel the returned task in a ``finally``
        block.
        """

        async def _heartbeat() -> None:
            try:
                while True:
                    await asyncio.sleep(interval)
                    try:
                        await chat.send_action("typing")
                    except Exception:
                        pass
            except asyncio.CancelledError:
                pass

        return asyncio.create_task(_heartbeat())

    def _make_stream_callback(
        self,
        verbose_level: int,
        progress_msg: Any,
        tool_log: List[Dict[str, Any]],
        start_time: float,
        reply_markup: Optional[InlineKeyboardMarkup] = None,
        mcp_images: Optional[List[ImageAttachment]] = None,
        approved_directory: Optional[Path] = None,
        draft_streamer: Optional[DraftStreamer] = None,
        interrupt_event: Optional[asyncio.Event] = None,
        bot: Any = None,
        chat_id: Optional[int] = None,
        message_thread_id: Optional[int] = None,
    ) -> Optional[Callable[[StreamUpdate], Any]]:
        """Create a stream callback for verbose progress updates.

        When *mcp_images* is provided, the callback also intercepts
        ``send_image_to_user`` tool calls and collects validated
        :class:`ImageAttachment` objects for later Telegram delivery.

        When *draft_streamer* is provided, tool activity and assistant
        text are streamed to the user in real time via
        ``sendMessageDraft``.

        When *bot*, *chat_id* are provided, interactive tool calls
        (``AskUserQuestion``, ``EnterPlanMode``, ``ExitPlanMode``,
        ``TodoWrite``) are surfaced as ephemeral Telegram messages so
        the user sees what Claude is doing — closer to the CLI experience.

        Returns None when verbose_level is 0 **and** no MCP image
        collection or draft streaming is requested.
        Typing indicators are handled by a separate heartbeat task.
        """
        need_mcp_intercept = mcp_images is not None and approved_directory is not None
        can_send_interactive = bot is not None and chat_id is not None
        # Track whether any interactive tool messages were sent so callers
        # can add a brief delay before the final response (ordering fix).
        interactive_sent: List[bool] = []

        if (
            verbose_level == 0
            and not need_mcp_intercept
            and draft_streamer is None
            and not can_send_interactive
        ):
            return None

        last_edit_time = [0.0]  # mutable container for closure

        async def _on_stream(update_obj: StreamUpdate) -> None:
            # Stop all streaming activity after interrupt
            if interrupt_event is not None and interrupt_event.is_set():
                return

            # Intercept send_image_to_user MCP tool calls.
            # The SDK namespaces MCP tools as "mcp__<server>__<tool>",
            # so match both the bare name and the namespaced variant.
            if update_obj.tool_calls and need_mcp_intercept:
                for tc in update_obj.tool_calls:
                    tc_name = tc.get("name", "")
                    if tc_name == "send_image_to_user" or tc_name.endswith(
                        "__send_image_to_user"
                    ):
                        tc_input = tc.get("input", {})
                        file_path = tc_input.get("file_path", "")
                        caption = tc_input.get("caption", "")
                        assert approved_directory is not None
                        assert mcp_images is not None
                        img = validate_image_path(
                            file_path, approved_directory, caption
                        )
                        if img:
                            mcp_images.append(img)

            # Surface interactive tool calls (AskUserQuestion, plan mode, todos)
            if update_obj.tool_calls and can_send_interactive:
                for tc in update_obj.tool_calls:
                    tc_name = tc.get("name", "")
                    if tc_name in _INTERACTIVE_TOOLS:
                        formatted = _format_interactive_tool(
                            tc_name, tc.get("input", {})
                        )
                        if formatted:
                            try:
                                await bot.send_message(
                                    chat_id=chat_id,
                                    text=formatted,
                                    parse_mode="HTML",
                                    message_thread_id=message_thread_id,
                                )
                                interactive_sent.append(True)
                            except Exception as itm_err:
                                logger.warning(
                                    "Failed to send interactive tool msg",
                                    tool=tc_name,
                                    error=str(itm_err),
                                )

            # Capture tool calls
            if update_obj.tool_calls:
                for tc in update_obj.tool_calls:
                    name = tc.get("name", "unknown")
                    detail = self._summarize_tool_input(name, tc.get("input", {}))
                    if verbose_level >= 1:
                        tool_log.append(
                            {"kind": "tool", "name": name, "detail": detail}
                        )
                    if draft_streamer:
                        icon = _tool_icon(name)
                        line = (
                            f"{icon} {name}: {detail}" if detail else f"{icon} {name}"
                        )
                        await draft_streamer.append_tool(line)

            # Capture assistant text (reasoning / commentary)
            if update_obj.type == "assistant" and update_obj.content:
                text = update_obj.content.strip()
                if text:
                    first_line = text.split("\n", 1)[0].strip()
                    if first_line:
                        if verbose_level >= 1:
                            tool_log.append(
                                {"kind": "text", "detail": first_line[:120]}
                            )
                        if draft_streamer:
                            await draft_streamer.append_tool(
                                f"\U0001f4ac {first_line[:120]}"
                            )

            # Stream text to user via draft (prefer token deltas;
            # skip full assistant messages to avoid double-appending)
            if draft_streamer and update_obj.content:
                if update_obj.type == "stream_delta":
                    await draft_streamer.append_text(update_obj.content)

            # Throttle progress message edits to avoid Telegram rate limits
            if not draft_streamer and verbose_level >= 1:
                now = time.time()
                if (now - last_edit_time[0]) >= 2.0 and tool_log:
                    last_edit_time[0] = now
                    new_text = self._format_verbose_progress(
                        tool_log, verbose_level, start_time
                    )
                    try:
                        await progress_msg.edit_text(
                            new_text, reply_markup=reply_markup
                        )
                    except Exception:
                        pass

        _on_stream.interactive_sent = interactive_sent  # type: ignore[attr-defined]
        return _on_stream

    async def _send_images(
        self,
        update: Update,
        images: List[ImageAttachment],
        reply_to_message_id: Optional[int] = None,
        caption: Optional[str] = None,
        caption_parse_mode: Optional[str] = None,
    ) -> bool:
        """Send extracted images as a media group (album) or documents.

        If *caption* is provided and fits (≤1024 chars), it is attached to the
        photo / first album item so text + images appear as one message.

        Returns True if the caption was successfully embedded in the photo message.
        """
        assert update.message is not None
        photos: List[ImageAttachment] = []
        documents: List[ImageAttachment] = []
        for img in images:
            if should_send_as_photo(img.path):
                photos.append(img)
            else:
                documents.append(img)

        # Telegram caption limit
        use_caption = bool(
            caption and len(caption) <= 1024 and photos and not documents
        )
        caption_sent = False

        # Send raster photos as a single album (Telegram groups 2-10 items)
        if photos:
            try:
                if len(photos) == 1:
                    with open(photos[0].path, "rb") as f:
                        await update.message.reply_photo(
                            photo=f,
                            reply_to_message_id=reply_to_message_id,
                            caption=caption if use_caption else None,
                            parse_mode=caption_parse_mode if use_caption else None,
                        )
                    caption_sent = use_caption
                else:
                    media = []
                    file_handles = []
                    for idx, img in enumerate(photos[:10]):
                        fh = open(img.path, "rb")  # noqa: SIM115
                        file_handles.append(fh)
                        media.append(
                            InputMediaPhoto(
                                media=fh,
                                caption=caption if use_caption and idx == 0 else None,
                                parse_mode=(
                                    caption_parse_mode
                                    if use_caption and idx == 0
                                    else None
                                ),
                            )
                        )
                    try:
                        await update.message.chat.send_media_group(
                            media=media,
                            reply_to_message_id=reply_to_message_id,
                        )
                        caption_sent = use_caption
                    finally:
                        for fh in file_handles:
                            fh.close()
            except Exception as e:
                logger.warning("Failed to send photo album", error=str(e))

        # Send SVGs / large files as documents (one by one — can't mix in album)
        for img in documents:
            try:
                with open(img.path, "rb") as f:
                    await update.message.reply_document(
                        document=f,
                        filename=img.path.name,
                        reply_to_message_id=reply_to_message_id,
                    )
                await asyncio.sleep(0.5)
            except Exception as e:
                logger.warning(
                    "Failed to send document image",
                    path=str(img.path),
                    error=str(e),
                )

        return caption_sent

    def _request_key(self, user_id: int, context: ContextTypes.DEFAULT_TYPE) -> str:
        """Build the ``_active_requests`` key.

        In project-thread mode the key includes the thread state key so
        that different threads can run in parallel.  Otherwise it is just
        the stringified user ID (preserving the old one-at-a-time
        behaviour).
        """
        assert context.user_data is not None
        thread_ctx = context.user_data.get("_thread_context")
        if thread_ctx:
            return str(thread_ctx.get("state_key", str(user_id)))
        return str(user_id)

    async def agentic_text(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Direct Claude passthrough. Simple progress. No suggestions."""
        assert update.effective_user is not None
        assert update.message is not None
        assert context.user_data is not None
        user_id = update.effective_user.id
        message_text = update.message.text or ""
        req_key = self._request_key(user_id, context)

        logger.info(
            "Agentic text message",
            user_id=user_id,
            message_length=len(message_text),
            request_key=req_key,
        )

        # Rate limit check
        rate_limiter = context.bot_data.get("rate_limiter")
        if rate_limiter:
            allowed, limit_message = await rate_limiter.check_rate_limit(user_id, 0.001)
            if not allowed:
                await update.message.reply_text(f"⏱️ {limit_message}")
                return

        chat = update.message.chat
        msg_thread_id = getattr(update.message, "message_thread_id", None)
        await chat.send_action("typing")

        verbose_level = self._get_verbose_level(context)

        # Create Stop button and interrupt event
        interrupt_event = asyncio.Event()
        stop_kb = InlineKeyboardMarkup(
            [[InlineKeyboardButton("Stop", callback_data=f"stop:{user_id}")]]
        )
        progress_msg = await update.message.reply_text(
            "Working...", reply_markup=stop_kb
        )

        claude_integration = context.bot_data.get("claude_integration")
        if not claude_integration:
            await progress_msg.edit_text(
                "Claude integration not available. Check configuration.",
                reply_markup=None,
            )
            return

        # Register active request for stop callback — placed immediately
        # before the try/finally that pops it, so it never leaks.
        active_request = ActiveRequest(
            user_id=user_id,
            interrupt_event=interrupt_event,
            progress_msg=progress_msg,
        )
        self._active_requests[req_key] = active_request

        # Read current_directory and session_id from the thread-safe
        # _thread_context when running under project threads, falling
        # back to the shared user_data keys for non-thread mode.
        thread_ctx = context.user_data.get("_thread_context")
        if thread_ctx:
            current_dir = thread_ctx.get(
                "current_directory", self.settings.approved_directory
            )
            session_id = thread_ctx.get("claude_session_id")
        else:
            current_dir = context.user_data.get(
                "current_directory", self.settings.approved_directory
            )
            session_id = context.user_data.get("claude_session_id")

        # Check if /new was used — skip auto-resume for this first message.
        # Flag is only cleared after a successful run so retries keep the intent.
        if thread_ctx:
            force_new = bool(thread_ctx.get("force_new_session"))
        else:
            force_new = bool(context.user_data.get("force_new_session"))

        # --- Verbose progress tracking via stream callback ---
        tool_log: List[Dict[str, Any]] = []
        start_time = time.time()
        mcp_images: List[ImageAttachment] = []

        # Stream drafts (private chats only)
        draft_streamer: Optional[DraftStreamer] = None
        if self.settings.enable_stream_drafts and chat.type == "private":
            draft_streamer = DraftStreamer(
                bot=context.bot,
                chat_id=chat.id,
                draft_id=generate_draft_id(),
                message_thread_id=update.message.message_thread_id,
                throttle_interval=self.settings.stream_draft_interval,
            )

        on_stream = self._make_stream_callback(
            verbose_level,
            progress_msg,
            tool_log,
            start_time,
            reply_markup=stop_kb,
            mcp_images=mcp_images,
            approved_directory=self.settings.approved_directory,
            draft_streamer=draft_streamer,
            interrupt_event=interrupt_event,
            bot=context.bot,
            chat_id=chat.id,
            message_thread_id=msg_thread_id,
        )

        # Independent typing heartbeat — stays alive even with no stream events
        heartbeat = self._start_typing_heartbeat(chat)

        # --- The actual SDK call, optionally gated by the RAM executor ---
        async def _run_claude() -> Any:
            return await claude_integration.run_command(
                prompt=message_text,
                working_directory=current_dir,
                user_id=user_id,
                session_id=session_id,
                on_stream=on_stream,
                force_new=force_new,
                interrupt_event=interrupt_event,
                chat_id=chat.id,
                message_thread_id=msg_thread_id,
            )

        success = True
        try:
            if self._executor and self.settings.enable_project_threads:
                claude_response = await self._executor.submit(req_key, _run_claude)
            else:
                claude_response = await _run_claude()

            # New session created successfully — clear the one-shot flag
            if force_new:
                if thread_ctx:
                    thread_ctx["force_new_session"] = False
                else:
                    context.user_data["force_new_session"] = False

            # Write results back to the thread-safe _thread_context when
            # in project-thread mode so concurrent handlers don't clash.
            if thread_ctx:
                thread_ctx["claude_session_id"] = claude_response.session_id
            else:
                context.user_data["claude_session_id"] = claude_response.session_id

            # Track directory changes — write directly to thread_ctx when
            # in project-thread mode so the value doesn't race via user_data.
            from .handlers.message import _update_working_directory_from_claude_response

            _update_working_directory_from_claude_response(
                claude_response,
                context,
                self.settings,
                user_id,
                target=thread_ctx,
            )

            # Store interaction
            storage = context.bot_data.get("storage")
            if storage:
                try:
                    await storage.save_claude_interaction(
                        user_id=user_id,
                        session_id=claude_response.session_id,
                        prompt=message_text,
                        response=claude_response,
                        ip_address=None,
                    )
                except Exception as e:
                    logger.warning("Failed to log interaction", error=str(e))

            # Format response (no reply_markup — strip keyboards)
            from .utils.formatting import ResponseFormatter

            formatter = ResponseFormatter(self.settings)

            response_content = claude_response.content
            if claude_response.interrupted:
                response_content = (
                    response_content or ""
                ) + "\n\n_(Interrupted by user)_"
            elif claude_response.hit_turn_limit:
                response_content = (response_content or "") + (
                    "\n\n⚠️ _Response cut short — turn limit reached "
                    f"({claude_response.num_turns} turns). "
                    "Send a follow-up message to continue._"
                )

            formatted_messages = formatter.format_claude_response(response_content)

        except Exception as e:
            success = False
            logger.error("Claude integration failed", error=str(e), user_id=user_id)
            from .handlers.message import _format_error_message
            from .utils.formatting import FormattedMessage

            formatted_messages = [
                FormattedMessage(_format_error_message(e), parse_mode="HTML")
            ]
        finally:
            heartbeat.cancel()
            self._active_requests.pop(req_key, None)
            if draft_streamer:
                try:
                    await draft_streamer.flush()
                except Exception:
                    logger.debug("Draft flush failed in finally block", user_id=user_id)

        # Brief pause so interactive tool messages (plan/todo/question)
        # arrive before the final response in Telegram clients.
        if on_stream and getattr(on_stream, "interactive_sent", None):
            await asyncio.sleep(0.3)

        try:
            await progress_msg.delete()
        except Exception:
            logger.debug("Failed to delete progress message, ignoring")

        # Use MCP-collected images (from send_image_to_user tool calls)
        images: List[ImageAttachment] = mcp_images

        # Try to combine text + images in one message when possible
        caption_sent = False
        if images and len(formatted_messages) == 1:
            msg = formatted_messages[0]
            if msg.text and len(msg.text) <= 1024:
                try:
                    caption_sent = await self._send_images(
                        update,
                        images,
                        reply_to_message_id=update.message.message_id,
                        caption=msg.text,
                        caption_parse_mode=msg.parse_mode,
                    )
                except Exception as img_err:
                    logger.warning("Image+caption send failed", error=str(img_err))

        # Send text messages (skip if caption was already embedded in photos)
        if not caption_sent:
            for i, message in enumerate(formatted_messages):
                if not message.text or not message.text.strip():
                    continue
                try:
                    await update.message.reply_text(
                        message.text,
                        parse_mode=message.parse_mode,
                        reply_markup=None,  # No keyboards in agentic mode
                        reply_to_message_id=(
                            update.message.message_id if i == 0 else None
                        ),
                    )
                    if i < len(formatted_messages) - 1:
                        await asyncio.sleep(0.5)
                except Exception as send_err:
                    logger.warning(
                        "Failed to send HTML response, retrying as plain text",
                        error=str(send_err),
                        message_index=i,
                    )
                    try:
                        await update.message.reply_text(
                            message.text,
                            reply_markup=None,
                            reply_to_message_id=(
                                update.message.message_id if i == 0 else None
                            ),
                        )
                    except Exception as plain_err:
                        await update.message.reply_text(
                            f"Failed to deliver response "
                            f"(Telegram error: {str(plain_err)[:150]}). "
                            f"Please try again.",
                            reply_to_message_id=(
                                update.message.message_id if i == 0 else None
                            ),
                        )

            # Send images separately if caption wasn't used
            if images:
                try:
                    await self._send_images(
                        update,
                        images,
                        reply_to_message_id=update.message.message_id,
                    )
                except Exception as img_err:
                    logger.warning("Image send failed", error=str(img_err))

        # Audit log
        audit_logger = context.bot_data.get("audit_logger")
        if audit_logger:
            await audit_logger.log_command(
                user_id=user_id,
                command="text_message",
                args=[message_text[:100]],
                success=success,
            )

    async def agentic_document(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Process file upload -> Claude, minimal chrome."""
        assert update.effective_user is not None
        assert update.message is not None
        assert context.user_data is not None
        user_id = update.effective_user.id
        document = update.message.document
        assert document is not None

        logger.info(
            "Agentic document upload",
            user_id=user_id,
            filename=document.file_name,
        )

        # Security validation
        security_validator = context.bot_data.get("security_validator")
        if security_validator:
            valid, error = security_validator.validate_filename(document.file_name)
            if not valid:
                await update.message.reply_text(f"File rejected: {error}")
                return

        # Size check
        max_size = 10 * 1024 * 1024
        if document.file_size is not None and document.file_size > max_size:
            await update.message.reply_text(
                f"File too large ({document.file_size / 1024 / 1024:.1f}MB). Max: 10MB."
            )
            return

        chat = update.message.chat
        await chat.send_action("typing")
        progress_msg = await update.message.reply_text("Working...")

        # Try enhanced file handler, fall back to basic
        features = context.bot_data.get("features")
        file_handler = features.get_file_handler() if features else None
        prompt: Optional[str] = None

        if file_handler:
            try:
                processed_file = await file_handler.handle_document_upload(
                    document,
                    user_id,
                    update.message.caption or "Please review this file:",
                )
                prompt = processed_file.prompt
            except Exception:
                file_handler = None

        if not file_handler:
            file = await document.get_file()
            file_bytes = await file.download_as_bytearray()
            try:
                content = file_bytes.decode("utf-8")
                if len(content) > 50000:
                    content = content[:50000] + "\n... (truncated)"
                caption = update.message.caption or "Please review this file:"
                prompt = (
                    f"{caption}\n\n**File:** `{document.file_name}`\n\n"
                    f"```\n{content}\n```"
                )
            except UnicodeDecodeError:
                await progress_msg.edit_text(
                    "Unsupported file format. Must be text-based (UTF-8)."
                )
                return

        # Process with Claude
        claude_integration = context.bot_data.get("claude_integration")
        if not claude_integration:
            await progress_msg.edit_text(
                "Claude integration not available. Check configuration."
            )
            return

        # Thread-safe context reads (same pattern as agentic_text)
        thread_ctx = context.user_data.get("_thread_context")
        if thread_ctx:
            current_dir = thread_ctx.get(
                "current_directory", self.settings.approved_directory
            )
            session_id = thread_ctx.get("claude_session_id")
        else:
            current_dir = context.user_data.get(
                "current_directory", self.settings.approved_directory
            )
            session_id = context.user_data.get("claude_session_id")

        # Check if /new was used — skip auto-resume for this first message.
        # Flag is only cleared after a successful run so retries keep the intent.
        if thread_ctx:
            force_new = bool(thread_ctx.get("force_new_session"))
        else:
            force_new = bool(context.user_data.get("force_new_session"))

        verbose_level = self._get_verbose_level(context)
        tool_log: List[Dict[str, Any]] = []
        mcp_images_doc: List[ImageAttachment] = []
        on_stream = self._make_stream_callback(
            verbose_level,
            progress_msg,
            tool_log,
            time.time(),
            mcp_images=mcp_images_doc,
            approved_directory=self.settings.approved_directory,
        )

        heartbeat = self._start_typing_heartbeat(chat)
        try:
            claude_response = await claude_integration.run_command(
                prompt=prompt,
                working_directory=current_dir,
                user_id=user_id,
                session_id=session_id,
                on_stream=on_stream,
                force_new=force_new,
            )

            if force_new:
                if thread_ctx:
                    thread_ctx["force_new_session"] = False
                else:
                    context.user_data["force_new_session"] = False

            # Thread-safe context writes
            if thread_ctx:
                thread_ctx["claude_session_id"] = claude_response.session_id
            else:
                context.user_data["claude_session_id"] = claude_response.session_id

            from .handlers.message import _update_working_directory_from_claude_response

            _update_working_directory_from_claude_response(
                claude_response,
                context,
                self.settings,
                user_id,
                target=thread_ctx,
            )

            from .utils.formatting import ResponseFormatter

            formatter = ResponseFormatter(self.settings)
            formatted_messages = formatter.format_claude_response(
                claude_response.content
            )

            try:
                await progress_msg.delete()
            except Exception:
                logger.debug("Failed to delete progress message, ignoring")

            # Use MCP-collected images (from send_image_to_user tool calls)
            images: List[ImageAttachment] = mcp_images_doc

            caption_sent = False
            if images and len(formatted_messages) == 1:
                msg = formatted_messages[0]
                if msg.text and len(msg.text) <= 1024:
                    try:
                        caption_sent = await self._send_images(
                            update,
                            images,
                            reply_to_message_id=update.message.message_id,
                            caption=msg.text,
                            caption_parse_mode=msg.parse_mode,
                        )
                    except Exception as img_err:
                        logger.warning("Image+caption send failed", error=str(img_err))

            if not caption_sent:
                for i, message in enumerate(formatted_messages):
                    await update.message.reply_text(
                        message.text,
                        parse_mode=message.parse_mode,
                        reply_markup=None,
                        reply_to_message_id=(
                            update.message.message_id if i == 0 else None
                        ),
                    )
                    if i < len(formatted_messages) - 1:
                        await asyncio.sleep(0.5)

                if images:
                    try:
                        await self._send_images(
                            update,
                            images,
                            reply_to_message_id=update.message.message_id,
                        )
                    except Exception as img_err:
                        logger.warning("Image send failed", error=str(img_err))

        except Exception as e:
            from .handlers.message import _format_error_message

            await progress_msg.edit_text(_format_error_message(e), parse_mode="HTML")
            logger.error("Claude file processing failed", error=str(e), user_id=user_id)
        finally:
            heartbeat.cancel()

    async def agentic_photo(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Process photo -> Claude, minimal chrome."""
        assert update.effective_user is not None
        assert update.message is not None
        user_id = update.effective_user.id

        features = context.bot_data.get("features")
        image_handler = features.get_image_handler() if features else None

        if not image_handler:
            await update.message.reply_text("Photo processing is not available.")
            return

        chat = update.message.chat
        await chat.send_action("typing")
        progress_msg = await update.message.reply_text("Working...")

        try:
            photo = update.message.photo[-1]
            processed_image = await image_handler.process_image(
                photo, update.message.caption
            )
            fmt = processed_image.metadata.get("format", "png")
            images = [
                {
                    "data": processed_image.base64_data,
                    "media_type": _MEDIA_TYPE_MAP.get(fmt, "image/png"),
                }
            ]

            await self._handle_agentic_media_message(
                update=update,
                context=context,
                prompt=processed_image.prompt,
                progress_msg=progress_msg,
                user_id=user_id,
                chat=chat,
                images=images,
            )

        except Exception as e:
            from .handlers.message import _format_error_message

            await progress_msg.edit_text(_format_error_message(e), parse_mode="HTML")
            logger.error(
                "Claude photo processing failed", error=str(e), user_id=user_id
            )

    async def agentic_voice(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Transcribe voice message -> Claude, minimal chrome."""
        assert update.effective_user is not None
        assert update.message is not None
        user_id = update.effective_user.id

        features = context.bot_data.get("features")
        voice_handler = features.get_voice_handler() if features else None

        if not voice_handler:
            await update.message.reply_text(self._voice_unavailable_message())
            return

        chat = update.message.chat
        await chat.send_action("typing")
        progress_msg = await update.message.reply_text("Transcribing...")

        try:
            voice = update.message.voice
            processed_voice = await voice_handler.process_voice_message(
                voice, update.message.caption
            )

            await progress_msg.edit_text("Working...")
            await self._handle_agentic_media_message(
                update=update,
                context=context,
                prompt=processed_voice.prompt,
                progress_msg=progress_msg,
                user_id=user_id,
                chat=chat,
            )

        except Exception as e:
            from .handlers.message import _format_error_message

            await progress_msg.edit_text(_format_error_message(e), parse_mode="HTML")
            logger.error(
                "Claude voice processing failed", error=str(e), user_id=user_id
            )

    async def _handle_agentic_media_message(
        self,
        *,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        prompt: str,
        progress_msg: Any,
        user_id: int,
        chat: Any,
        images: Optional[List[Dict[str, str]]] = None,
    ) -> None:
        """Run a media-derived prompt through Claude and send responses."""
        assert update.message is not None
        assert context.user_data is not None
        claude_integration = context.bot_data.get("claude_integration")
        if not claude_integration:
            await progress_msg.edit_text(
                "Claude integration not available. Check configuration."
            )
            return

        # Thread-safe context reads (same pattern as agentic_text)
        thread_ctx = context.user_data.get("_thread_context")
        if thread_ctx:
            current_dir = thread_ctx.get(
                "current_directory", self.settings.approved_directory
            )
            session_id = thread_ctx.get("claude_session_id")
        else:
            current_dir = context.user_data.get(
                "current_directory", self.settings.approved_directory
            )
            session_id = context.user_data.get("claude_session_id")
        if thread_ctx:
            force_new = bool(thread_ctx.get("force_new_session"))
        else:
            force_new = bool(context.user_data.get("force_new_session"))

        verbose_level = self._get_verbose_level(context)
        tool_log: List[Dict[str, Any]] = []
        mcp_images_media: List[ImageAttachment] = []
        on_stream = self._make_stream_callback(
            verbose_level,
            progress_msg,
            tool_log,
            time.time(),
            mcp_images=mcp_images_media,
            approved_directory=self.settings.approved_directory,
        )

        heartbeat = self._start_typing_heartbeat(chat)
        try:
            claude_response = await claude_integration.run_command(
                prompt=prompt,
                working_directory=current_dir,
                user_id=user_id,
                session_id=session_id,
                on_stream=on_stream,
                force_new=force_new,
                images=images,
            )
        finally:
            heartbeat.cancel()

        if force_new:
            if thread_ctx:
                thread_ctx["force_new_session"] = False
            else:
                context.user_data["force_new_session"] = False

        # Thread-safe context writes
        if thread_ctx:
            thread_ctx["claude_session_id"] = claude_response.session_id
        else:
            context.user_data["claude_session_id"] = claude_response.session_id

        from .handlers.message import _update_working_directory_from_claude_response

        _update_working_directory_from_claude_response(
            claude_response,
            context,
            self.settings,
            user_id,
            target=thread_ctx,
        )

        from .utils.formatting import ResponseFormatter

        formatter = ResponseFormatter(self.settings)
        formatted_messages = formatter.format_claude_response(claude_response.content)

        try:
            await progress_msg.delete()
        except Exception:
            logger.debug("Failed to delete progress message, ignoring")

        # Use MCP-collected images (from send_image_to_user tool calls).
        mcp_images: List[ImageAttachment] = mcp_images_media

        caption_sent = False
        if mcp_images and len(formatted_messages) == 1:
            msg = formatted_messages[0]
            if msg.text and len(msg.text) <= 1024:
                try:
                    caption_sent = await self._send_images(
                        update,
                        mcp_images,
                        reply_to_message_id=update.message.message_id,
                        caption=msg.text,
                        caption_parse_mode=msg.parse_mode,
                    )
                except Exception as img_err:
                    logger.warning("Image+caption send failed", error=str(img_err))

        if not caption_sent:
            for i, message in enumerate(formatted_messages):
                if not message.text or not message.text.strip():
                    continue
                await update.message.reply_text(
                    message.text,
                    parse_mode=message.parse_mode,
                    reply_markup=None,
                    reply_to_message_id=(update.message.message_id if i == 0 else None),
                )
                if i < len(formatted_messages) - 1:
                    await asyncio.sleep(0.5)

            if mcp_images:
                try:
                    await self._send_images(
                        update,
                        mcp_images,
                        reply_to_message_id=update.message.message_id,
                    )
                except Exception as img_err:
                    logger.warning("Image send failed", error=str(img_err))

    async def _handle_unknown_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Forward unknown slash commands to Claude in agentic mode.

        Known commands are handled by their own CommandHandlers (group 0);
        this handler fires for *every* COMMAND message in group 10 but
        returns immediately when the command is registered, preventing
        double execution.
        """
        msg = update.effective_message
        if not msg or not msg.text:
            return
        cmd = msg.text.split()[0].lstrip("/").split("@")[0].lower()
        if cmd in self._known_commands:
            return  # let the registered CommandHandler take care of it
        # Forward unrecognised /commands to Claude as natural language
        await self.agentic_text(update, context)

    def _voice_unavailable_message(self) -> str:
        """Return provider-aware guidance when voice feature is unavailable."""
        if self.settings.voice_provider == "local":
            return (
                "Voice processing is not available. "
                "Ensure whisper.cpp is installed and the model file exists. "
                "Check WHISPER_CPP_BINARY_PATH and WHISPER_CPP_MODEL_PATH settings."
            )
        return (
            "Voice processing is not available. "
            f"Set {self.settings.voice_provider_api_key_env} "
            f"for {self.settings.voice_provider_display_name} and install "
            'voice extras with: pip install "claude-code-telegram[voice]"'
        )

    async def agentic_repo(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """List repos in workspace or switch to one.

        /repo          — list subdirectories with git indicators
        /repo <name>   — switch to that directory, resume session if available
        """
        assert update.message is not None
        assert update.effective_user is not None
        assert context.user_data is not None
        args = update.message.text.split()[1:] if update.message.text else []
        base = self.settings.approved_directory
        current_dir = context.user_data.get("current_directory", base)

        if args:
            # Switch to named repo
            target_name = args[0]
            target_path = base / target_name
            if not target_path.is_dir():
                await update.message.reply_text(
                    f"Directory not found: <code>{escape_html(target_name)}</code>",
                    parse_mode="HTML",
                )
                return

            context.user_data["current_directory"] = target_path

            # Try to find a resumable session
            claude_integration = context.bot_data.get("claude_integration")
            session_id = None
            if claude_integration:
                existing = await claude_integration._find_resumable_session(
                    update.effective_user.id, target_path
                )
                if existing:
                    session_id = existing.session_id
            context.user_data["claude_session_id"] = session_id

            is_git = (target_path / ".git").is_dir()
            git_badge = " (git)" if is_git else ""
            session_badge = " · session resumed" if session_id else ""

            await update.message.reply_text(
                f"Switched to <code>{escape_html(target_name)}/</code>"
                f"{git_badge}{session_badge}",
                parse_mode="HTML",
            )
            return

        # No args — list repos
        try:
            entries = sorted(
                [
                    d
                    for d in base.iterdir()
                    if d.is_dir() and not d.name.startswith(".")
                ],
                key=lambda d: d.name,
            )
        except OSError as e:
            await update.message.reply_text(f"Error reading workspace: {e}")
            return

        if not entries:
            await update.message.reply_text(
                f"No repos in <code>{escape_html(str(base))}</code>.\n"
                'Clone one by telling me, e.g. <i>"clone org/repo"</i>.',
                parse_mode="HTML",
            )
            return

        lines: List[str] = []
        keyboard_rows: List[list] = []  # type: ignore[type-arg]
        current_name = current_dir.name if current_dir != base else None

        for d in entries:
            is_git = (d / ".git").is_dir()
            icon = "\U0001f4e6" if is_git else "\U0001f4c1"
            marker = " \u25c0" if d.name == current_name else ""
            lines.append(f"{icon} <code>{escape_html(d.name)}/</code>{marker}")

        # Build inline keyboard (2 per row)
        for i in range(0, len(entries), 2):
            row = []
            for j in range(2):
                if i + j < len(entries):
                    name = entries[i + j].name
                    row.append(InlineKeyboardButton(name, callback_data=f"cd:{name}"))
            keyboard_rows.append(row)

        reply_markup = InlineKeyboardMarkup(keyboard_rows)

        await update.message.reply_text(
            "<b>Repos</b>\n\n" + "\n".join(lines),
            parse_mode="HTML",
            reply_markup=reply_markup,
        )

    async def _handle_stop_callback(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle stop: callbacks — interrupt a running Claude request."""
        query = update.callback_query
        assert query is not None
        assert query.data is not None
        target_user_id = int(query.data.split(":", 1)[1])

        # Only the requesting user can stop their own request
        if query.from_user.id != target_user_id:
            await query.answer(
                "Only the requesting user can stop this.", show_alert=True
            )
            return

        # Find any active request belonging to this user (keys may be
        # plain user_id or "chat:thread" when project threads are active).
        active: Optional[ActiveRequest] = None
        for key, req in self._active_requests.items():
            if req.user_id == target_user_id:
                active = req
                break
        if not active:
            await query.answer("Already completed.", show_alert=False)
            return
        if active.interrupted:
            await query.answer("Already stopping...", show_alert=False)
            return

        active.interrupt_event.set()
        active.interrupted = True
        await query.answer("Stopping...", show_alert=False)

        try:
            await active.progress_msg.edit_text("Stopping...", reply_markup=None)
        except Exception:
            pass

    async def _agentic_callback(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle cd: callbacks — switch directory and resume session if available."""
        query = update.callback_query
        assert query is not None
        assert query.data is not None
        assert context.user_data is not None
        await query.answer()

        data = query.data
        _, project_name = data.split(":", 1)

        base = self.settings.approved_directory
        new_path = base / project_name

        if not new_path.is_dir():
            await query.edit_message_text(
                f"Directory not found: <code>{escape_html(project_name)}</code>",
                parse_mode="HTML",
            )
            return

        context.user_data["current_directory"] = new_path

        # Look for a resumable session instead of always clearing
        claude_integration = context.bot_data.get("claude_integration")
        session_id = None
        if claude_integration:
            existing = await claude_integration._find_resumable_session(
                query.from_user.id, new_path
            )
            if existing:
                session_id = existing.session_id
        context.user_data["claude_session_id"] = session_id

        is_git = (new_path / ".git").is_dir()
        git_badge = " (git)" if is_git else ""
        session_badge = " · session resumed" if session_id else ""

        await query.edit_message_text(
            f"Switched to <code>{escape_html(project_name)}/</code>"
            f"{git_badge}{session_badge}",
            parse_mode="HTML",
        )

        # Audit log
        audit_logger = context.bot_data.get("audit_logger")
        if audit_logger:
            await audit_logger.log_command(
                user_id=query.from_user.id,
                command="cd",
                args=[project_name],
                success=True,
            )
