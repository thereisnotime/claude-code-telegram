"""Telegram forum topic synchronization and project resolution."""

import asyncio
from dataclasses import dataclass
from time import monotonic
from typing import Awaitable, Callable, Dict, Optional, TypeVar

import structlog
from telegram import Bot
from telegram.error import TelegramError

from ..storage.models import ProjectThreadModel
from ..storage.repositories import ProjectThreadRepository
from ..utils.constants import DEFAULT_PROJECT_THREADS_SYNC_ACTION_INTERVAL_SECONDS
from .registry import ProjectDefinition, ProjectRegistry

logger = structlog.get_logger()
T = TypeVar("T")


class PrivateTopicsUnavailableError(RuntimeError):
    """Raised when private chat topics are unavailable/disabled."""


@dataclass
class TopicSyncResult:
    """Summary of a synchronization run."""

    created: int = 0
    reused: int = 0
    renamed: int = 0
    failed: int = 0
    deactivated: int = 0
    closed: int = 0
    reopened: int = 0


class ProjectThreadManager:
    """Maintains mapping between projects and Telegram forum topics."""

    def __init__(
        self,
        registry: ProjectRegistry,
        repository: ProjectThreadRepository,
        sync_action_interval_seconds: float = (
            DEFAULT_PROJECT_THREADS_SYNC_ACTION_INTERVAL_SECONDS
        ),
    ) -> None:
        self.registry = registry
        self.repository = repository
        self.sync_action_interval_seconds = max(0.0, sync_action_interval_seconds)
        self._sync_api_lock = asyncio.Lock()
        self._last_sync_api_call_at: Optional[float] = None
        # Pre-resolved topic name → thread_id from Telethon (set before sync)
        self._resolved_topics: Dict[str, int] = {}

    def set_resolved_topics(self, topics: Dict[str, int]) -> None:
        """Inject pre-resolved topic name → thread_id mapping.

        Call this *before* :meth:`sync_topics` with the output of
        :func:`topic_resolver.resolve_topics_if_enabled` so that the
        sync can reuse existing Telegram topics instead of creating
        duplicates.
        """
        self._resolved_topics = dict(topics)

    async def sync_topics(self, bot: Bot, chat_id: int) -> TopicSyncResult:
        """Create/reconcile topics for all enabled projects."""
        result = TopicSyncResult()

        enabled = self.registry.list_enabled()

        for project in enabled:
            try:
                existing = await self.repository.get_by_chat_project(
                    chat_id,
                    project.slug,
                )

                if existing:
                    handled = await self._sync_existing_mapping(
                        bot=bot,
                        project=project,
                        mapping=existing,
                        result=result,
                    )
                    if handled:
                        continue

                await self._create_and_map_topic(
                    bot=bot,
                    project=project,
                    chat_id=chat_id,
                    result=result,
                )

            except TelegramError as e:
                if self._is_private_topics_unavailable_error(e):
                    raise PrivateTopicsUnavailableError(
                        "Private chat topics are not enabled for this bot chat."
                    ) from e
                result.failed += 1
                logger.error(
                    "Failed to sync project topic",
                    project_slug=project.slug,
                    chat_id=chat_id,
                    error=str(e),
                )
            except Exception as e:
                result.failed += 1
                logger.error(
                    "Failed to sync project topic",
                    project_slug=project.slug,
                    chat_id=chat_id,
                    error=str(e),
                )

        stale_mappings = await self.repository.list_stale_active_mappings(
            chat_id=chat_id,
            active_project_slugs=list(self.registry.all_configured_slugs),
        )
        for stale in stale_mappings:
            try:
                await self._call_sync_api(
                    lambda: bot.close_forum_topic(
                        chat_id=stale.chat_id,
                        message_thread_id=stale.message_thread_id,
                    ),
                )
                result.closed += 1
            except TelegramError as e:
                if self._is_private_topics_unavailable_error(e):
                    raise PrivateTopicsUnavailableError(
                        "Private chat topics are not enabled for this bot chat."
                    ) from e
                # Private chats don't support close — just deactivate
                if "not a supergroup" in str(e).lower():
                    result.closed += 1
                else:
                    result.failed += 1
                    logger.warning(
                        "Could not close stale topic",
                        chat_id=stale.chat_id,
                        message_thread_id=stale.message_thread_id,
                        project_slug=stale.project_slug,
                        error=str(e),
                    )
            finally:
                await self.repository.set_active(
                    chat_id=stale.chat_id,
                    project_slug=stale.project_slug,
                    is_active=False,
                )
                result.deactivated += 1

        return result

    async def _call_sync_api(
        self,
        call: Callable[[], Awaitable[T]],
    ) -> T:
        """Call Telegram sync API with pacing."""
        async with self._sync_api_lock:
            await self._wait_for_sync_interval()
            self._last_sync_api_call_at = monotonic()
            return await call()

    async def _wait_for_sync_interval(self) -> None:
        """Wait until minimum sync action interval is satisfied."""
        if (
            self.sync_action_interval_seconds <= 0
            or self._last_sync_api_call_at is None
        ):
            return

        elapsed = monotonic() - self._last_sync_api_call_at
        wait_time = self.sync_action_interval_seconds - elapsed
        if wait_time > 0:
            await asyncio.sleep(wait_time)

    async def _sync_existing_mapping(
        self,
        bot: Bot,
        project: ProjectDefinition,
        mapping: ProjectThreadModel,
        result: TopicSyncResult,
    ) -> bool:
        """Sync an existing mapping. Returns True if handled without recreate."""
        chat_id = mapping.chat_id

        # If Telethon resolved a different thread_id for this project,
        # adopt the resolved one — the DB mapping is stale.
        resolved_thread_id = self._resolved_topics.get(project.name)
        if (
            resolved_thread_id is not None
            and resolved_thread_id != mapping.message_thread_id
        ):
            logger.info(
                "Updating stale thread ID from Telethon resolution",
                project_slug=project.slug,
                old_thread_id=mapping.message_thread_id,
                new_thread_id=resolved_thread_id,
                chat_id=chat_id,
            )
            await self.repository.upsert_mapping(
                project_slug=project.slug,
                chat_id=chat_id,
                message_thread_id=resolved_thread_id,
                topic_name=project.name,
                is_active=True,
            )
            result.reused += 1
            return True

        if not mapping.is_active:
            reopen_status = await self._reopen_topic_if_possible(bot, mapping)
            if reopen_status == "unusable":
                return False
            if reopen_status == "failed":
                result.failed += 1
                return True
            result.reopened += 1

        usable_status = await self._ensure_topic_usable(bot, mapping)
        if usable_status == "unusable":
            return False
        if usable_status == "failed":
            result.failed += 1
            return True

        topic_name = mapping.topic_name
        if mapping.topic_name != project.name:
            rename_status = await self._rename_topic(
                bot=bot,
                mapping=mapping,
                target_name=project.name,
            )
            if rename_status == "unusable":
                return False
            if rename_status == "failed":
                await self.repository.upsert_mapping(
                    project_slug=project.slug,
                    chat_id=chat_id,
                    message_thread_id=mapping.message_thread_id,
                    topic_name=mapping.topic_name,
                    is_active=True,
                )
                result.failed += 1
                result.reused += 1
                return True
            topic_name = project.name
            result.renamed += 1

        await self.repository.upsert_mapping(
            project_slug=project.slug,
            chat_id=chat_id,
            message_thread_id=mapping.message_thread_id,
            topic_name=topic_name,
            is_active=True,
        )
        result.reused += 1
        return True

    async def _create_and_map_topic(
        self,
        bot: Bot,
        project: ProjectDefinition,
        chat_id: int,
        result: TopicSyncResult,
    ) -> None:
        """Create a topic (or adopt a pre-resolved one) and persist mapping."""
        resolved_thread_id = self._resolved_topics.get(project.name)

        if resolved_thread_id is not None:
            # Adopt the existing Telegram topic instead of creating a new one
            logger.info(
                "Adopting pre-resolved topic",
                project_slug=project.slug,
                topic_name=project.name,
                message_thread_id=resolved_thread_id,
                chat_id=chat_id,
            )
            await self.repository.upsert_mapping(
                project_slug=project.slug,
                chat_id=chat_id,
                message_thread_id=resolved_thread_id,
                topic_name=project.name,
                is_active=True,
            )
            result.reused += 1
            return

        topic = await self._call_sync_api(
            lambda: bot.create_forum_topic(
                chat_id=chat_id,
                name=project.name,
            ),
        )

        await self.repository.upsert_mapping(
            project_slug=project.slug,
            chat_id=chat_id,
            message_thread_id=topic.message_thread_id,
            topic_name=project.name,
            is_active=True,
        )
        await self._send_topic_bootstrap_message(
            bot=bot,
            chat_id=chat_id,
            message_thread_id=topic.message_thread_id,
            project_name=project.name,
        )
        result.created += 1

    async def _ensure_topic_usable(self, bot: Bot, mapping: ProjectThreadModel) -> str:
        """Ensure mapped topic is usable. Returns ok|unusable|failed."""
        try:
            await self._call_sync_api(
                lambda: bot.reopen_forum_topic(
                    chat_id=mapping.chat_id,
                    message_thread_id=mapping.message_thread_id,
                ),
            )
            return "ok"
        except TelegramError as e:
            if self._is_topic_unusable_error(e):
                return "unusable"
            # Topic already open — Telegram returns "Topic_not_modified"
            if "not_modified" in str(e).lower():
                return "ok"
            # Private chats don't support reopen/close — topic is still usable
            if "not a supergroup" in str(e).lower():
                return "ok"
            logger.warning(
                "Could not verify topic usability",
                chat_id=mapping.chat_id,
                message_thread_id=mapping.message_thread_id,
                error=str(e),
            )
            return "failed"

    async def _reopen_topic_if_possible(
        self, bot: Bot, mapping: ProjectThreadModel
    ) -> str:
        """Reopen inactive topic. Returns reopened|unusable|failed."""
        try:
            await self._call_sync_api(
                lambda: bot.reopen_forum_topic(
                    chat_id=mapping.chat_id,
                    message_thread_id=mapping.message_thread_id,
                ),
            )
            return "reopened"
        except TelegramError as e:
            if self._is_topic_unusable_error(e):
                return "unusable"
            # Topic already open — Telegram returns "Topic_not_modified"
            if "not_modified" in str(e).lower():
                return "reopened"
            # Private chats don't support reopen/close — treat as reopened
            if "not a supergroup" in str(e).lower():
                return "reopened"
            logger.warning(
                "Could not reopen topic",
                chat_id=mapping.chat_id,
                message_thread_id=mapping.message_thread_id,
                error=str(e),
            )
            return "failed"

    async def resolve_project(
        self, chat_id: int, message_thread_id: int
    ) -> Optional[ProjectDefinition]:
        """Resolve mapped project for chat+thread."""
        mapping = await self.repository.get_by_chat_thread(chat_id, message_thread_id)
        if not mapping:
            logger.debug(
                "No DB mapping for chat+thread",
                chat_id=chat_id,
                message_thread_id=message_thread_id,
            )
            return None

        project = self.registry.get_by_slug(mapping.project_slug)
        if not project or not project.enabled:
            logger.debug(
                "Project not found or disabled",
                project_slug=mapping.project_slug,
                found=project is not None,
                enabled=getattr(project, "enabled", None),
            )
            return None

        return project

    @staticmethod
    def guidance_message(mode: str = "group") -> str:
        """Guidance text for strict routing rejections."""
        context_label = (
            "mapped project topic in this private chat"
            if mode == "private"
            else "mapped project forum topic"
        )
        return (
            "🚫 <b>Project Thread Required</b>\n\n"
            "This bot is configured for strict project threads.\n"
            f"Please send commands in a {context_label}.\n\n"
            "If topics are missing or stale, run <code>/sync_threads</code>."
        )

    @staticmethod
    def private_topics_unavailable_message() -> str:
        """User guidance when private chat topics are unavailable."""
        return (
            "❌ <b>Private Topics Unavailable</b>\n\n"
            "This bot requires topics in private chat, "
            "but topics are not available.\n\n"
            "Enable topics for this bot chat in Telegram, then run "
            "<code>/sync_threads</code>."
        )

    @staticmethod
    def _is_private_topics_unavailable_error(error: TelegramError) -> bool:
        """Return True for Telegram errors indicating topics are unavailable."""
        text = str(error).lower()
        markers = [
            "topics are not enabled",
            "topic_closed",
            "topic deleted",
            "forum topics are disabled",
            "direct messages topic",
            "chat is not a forum",
        ]
        return any(marker in text for marker in markers)

    async def _rename_topic(
        self,
        bot: Bot,
        mapping: ProjectThreadModel,
        target_name: str,
    ) -> str:
        """Rename forum topic. Returns renamed|unusable|failed."""
        try:
            await self._call_sync_api(
                lambda: bot.edit_forum_topic(
                    chat_id=mapping.chat_id,
                    message_thread_id=mapping.message_thread_id,
                    name=target_name,
                ),
            )
            return "renamed"
        except TelegramError as e:
            if self._is_topic_unusable_error(e):
                return "unusable"
            logger.warning(
                "Could not rename topic",
                chat_id=mapping.chat_id,
                message_thread_id=mapping.message_thread_id,
                target_name=target_name,
                error=str(e),
            )
            return "failed"

    async def _send_topic_bootstrap_message(
        self,
        bot: Bot,
        chat_id: int,
        message_thread_id: int,
        project_name: str,
    ) -> None:
        """Post a short message so newly created topics are visible in clients."""
        try:
            await self._call_sync_api(
                lambda: bot.send_message(
                    chat_id=chat_id,
                    message_thread_id=message_thread_id,
                    text=(
                        f"🧵 <b>{project_name}</b>\n\n"
                        "This project topic is ready. "
                        "Send messages here to work on this project."
                    ),
                    parse_mode="HTML",
                ),
            )
        except TelegramError as e:
            logger.warning(
                "Could not send topic bootstrap message",
                chat_id=chat_id,
                message_thread_id=message_thread_id,
                project_name=project_name,
                error=str(e),
            )

    @staticmethod
    def _is_topic_unusable_error(error: TelegramError) -> bool:
        """Return True when topic no longer exists or thread id is invalid."""
        text = str(error).lower()
        markers = [
            "topic deleted",
            "topic was deleted",
            "topic_closed",
            "topic closed",
            "message thread not found",
            "thread not found",
            "invalid message thread id",
            "forum topic not found",
        ]
        return any(marker in text for marker in markers)
