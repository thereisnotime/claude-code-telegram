"""Persistent session storage implementation.

Replaces the in-memory session storage with SQLite persistence.
"""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import structlog

from ..claude.session import ClaudeSession, SessionStorage
from .database import DatabaseManager
from .models import SessionModel

logger = structlog.get_logger()


class SQLiteSessionStorage(SessionStorage):
    """SQLite-based session storage."""

    def __init__(self, db_manager: DatabaseManager):
        """Initialize with database manager."""
        self.db_manager = db_manager

    async def _ensure_user_exists(
        self, user_id: int, username: Optional[str] = None
    ) -> None:
        """Ensure user exists in database before creating session."""
        async with self.db_manager.get_connection() as conn:
            # Check if user exists
            cursor = await conn.execute(
                "SELECT user_id FROM users WHERE user_id = ?", (user_id,)
            )
            user_exists = await cursor.fetchone()

            if not user_exists:
                # Create user record
                now = datetime.now(UTC)
                await conn.execute(
                    """
                    INSERT INTO users
                    (user_id, telegram_username, first_seen, last_active, is_allowed)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        user_id,
                        username,
                        now,
                        now,
                        True,
                    ),  # Allow user by default for now
                )
                await conn.commit()

                logger.info(
                    "Created user record for session",
                    user_id=user_id,
                    username=username,
                )

    async def save_session(self, session: ClaudeSession) -> None:
        """Save session to database."""
        # Ensure user exists before creating session
        await self._ensure_user_exists(session.user_id)

        session_model = SessionModel(
            session_id=session.session_id,
            user_id=session.user_id,
            project_path=str(session.project_path),
            created_at=session.created_at,
            last_used=session.last_used,
            total_cost=session.total_cost,
            total_turns=session.total_turns,
            message_count=session.message_count,
        )

        async with self.db_manager.get_connection() as conn:
            # Try to update first
            cursor = await conn.execute(
                """
                UPDATE sessions
                SET last_used = ?, total_cost = ?, total_turns = ?,
                    message_count = ?, chat_id = COALESCE(?, chat_id),
                    message_thread_id = COALESCE(?, message_thread_id)
                WHERE session_id = ?
            """,
                (
                    session_model.last_used,
                    session_model.total_cost,
                    session_model.total_turns,
                    session_model.message_count,
                    session.chat_id,
                    session.message_thread_id,
                    session_model.session_id,
                ),
            )

            # If no rows were updated, insert new record
            if cursor.rowcount == 0:
                await conn.execute(
                    """
                    INSERT INTO sessions
                    (session_id, user_id, project_path, created_at, last_used,
                     total_cost, total_turns, message_count, chat_id,
                     message_thread_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                    (
                        session_model.session_id,
                        session_model.user_id,
                        session_model.project_path,
                        session_model.created_at,
                        session_model.last_used,
                        session_model.total_cost,
                        session_model.total_turns,
                        session_model.message_count,
                        session.chat_id,
                        session.message_thread_id,
                    ),
                )

            await conn.commit()

        logger.debug(
            "Session saved to database",
            session_id=session.session_id,
            user_id=session.user_id,
        )

    async def load_session(
        self, session_id: str, user_id: int
    ) -> Optional[ClaudeSession]:
        """Load session from database, filtered by user ownership."""
        async with self.db_manager.get_connection() as conn:
            cursor = await conn.execute(
                "SELECT * FROM sessions WHERE session_id = ? AND user_id = ?",
                (session_id, user_id),
            )
            row = await cursor.fetchone()

            if not row:
                return None

            session_model = SessionModel.from_row(row)

            claude_session = self._model_to_session(session_model)

            logger.debug(
                "Session loaded from database",
                session_id=session_id,
                user_id=claude_session.user_id,
            )

            return claude_session

    async def delete_session(self, session_id: str) -> None:
        """Delete session from database."""
        async with self.db_manager.get_connection() as conn:
            await conn.execute(
                "UPDATE sessions SET is_active = FALSE WHERE session_id = ?",
                (session_id,),
            )
            await conn.commit()

        logger.debug("Session marked as inactive", session_id=session_id)

    @staticmethod
    def _model_to_session(model: SessionModel) -> ClaudeSession:
        """Convert a SessionModel to a ClaudeSession."""
        return ClaudeSession(
            session_id=model.session_id,
            user_id=model.user_id,
            project_path=Path(model.project_path),
            created_at=model.created_at,
            last_used=model.last_used,
            total_cost=model.total_cost,
            total_turns=model.total_turns,
            message_count=model.message_count,
            tools_used=[],  # Tools are tracked separately in tool_usage table
            chat_id=model.chat_id,
            message_thread_id=model.message_thread_id,
        )

    async def get_user_sessions(self, user_id: int) -> List[ClaudeSession]:
        """Get all active sessions for a user."""
        async with self.db_manager.get_connection() as conn:
            cursor = await conn.execute(
                """
                SELECT * FROM sessions
                WHERE user_id = ? AND is_active = TRUE
                ORDER BY last_used DESC
            """,
                (user_id,),
            )
            rows = await cursor.fetchall()
            return [self._model_to_session(SessionModel.from_row(row)) for row in rows]

    async def get_all_sessions(self) -> List[ClaudeSession]:
        """Get all active sessions."""
        async with self.db_manager.get_connection() as conn:
            cursor = await conn.execute(
                "SELECT * FROM sessions WHERE is_active = TRUE ORDER BY last_used DESC"
            )
            rows = await cursor.fetchall()
            return [self._model_to_session(SessionModel.from_row(row)) for row in rows]

    async def mark_in_flight(
        self,
        session_id: str,
        chat_id: int,
        message_thread_id: Optional[int],
        prompt: str,
    ) -> None:
        """Mark a session as having an active in-flight request."""
        async with self.db_manager.get_connection() as conn:
            await conn.execute(
                """
                UPDATE sessions
                SET in_flight = TRUE, chat_id = ?, message_thread_id = ?,
                    in_flight_prompt = ?, in_flight_started_at = ?
                WHERE session_id = ?
            """,
                (
                    chat_id,
                    message_thread_id,
                    prompt[:500],  # Truncate to avoid bloating DB
                    datetime.now(UTC),
                    session_id,
                ),
            )
            await conn.commit()

        logger.debug("Session marked in-flight", session_id=session_id)

    async def clear_in_flight(self, session_id: str) -> None:
        """Clear the in-flight flag after execution completes."""
        async with self.db_manager.get_connection() as conn:
            await conn.execute(
                """
                UPDATE sessions
                SET in_flight = FALSE, in_flight_prompt = NULL,
                    in_flight_started_at = NULL
                WHERE session_id = ?
            """,
                (session_id,),
            )
            await conn.commit()

        logger.debug("Session in-flight cleared", session_id=session_id)

    async def get_interrupted_sessions(self) -> List[Dict[str, Any]]:
        """Get all sessions that were interrupted mid-flight."""
        async with self.db_manager.get_connection() as conn:
            cursor = await conn.execute(
                """
                SELECT session_id, user_id, chat_id, message_thread_id,
                       project_path, in_flight_prompt, in_flight_started_at
                FROM sessions
                WHERE in_flight = TRUE AND is_active = TRUE
            """
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def cleanup_expired_sessions(self, timeout_hours: int) -> int:
        """Mark expired sessions as inactive."""
        async with self.db_manager.get_connection() as conn:
            cursor = await conn.execute(
                """
                UPDATE sessions
                SET is_active = FALSE
                WHERE last_used < datetime('now', '-' || ? || ' hours')
                  AND is_active = TRUE
            """,
                (timeout_hours,),
            )
            await conn.commit()

            affected = cursor.rowcount
            logger.info(
                "Cleaned up expired sessions",
                count=affected,
                timeout_hours=timeout_hours,
            )
            return affected
