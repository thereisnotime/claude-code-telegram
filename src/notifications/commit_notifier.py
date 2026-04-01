"""Notify Telegram when new git commits are pulled."""

import subprocess
from pathlib import Path
from typing import Dict, List, Optional

import structlog
from telegram import Bot
from telegram.error import TelegramError

from src.storage.repositories import ProjectThreadRepository

logger = structlog.get_logger(__name__)

MARKER_FILE = ".last_notified_commit"


async def check_and_notify_new_commits(
    bot: Bot,
    repo_dir: Path,
    chat_id: int,
    thread_repo: ProjectThreadRepository,
    news_slug: str = "news",
) -> None:
    """Compare HEAD against last-notified marker and send commit digest.

    Looks up the message_thread_id for the news topic from the DB
    (created by sync_topics from the project registry).

    Args:
        bot: Telegram Bot instance (already initialized).
        repo_dir: Path to the git repository root.
        chat_id: Telegram chat ID where the news topic lives.
        thread_repo: Repository to look up topic thread mappings.
        news_slug: Project slug for the news topic in the registry.
    """
    # Look up the news topic thread_id from the DB
    mapping = await thread_repo.get_by_chat_project(chat_id, news_slug)
    if not mapping:
        logger.warning(
            "News topic not found in DB, skipping commit notification",
            slug=news_slug,
            chat_id=chat_id,
        )
        return

    message_thread_id = mapping.message_thread_id

    marker_path = repo_dir / MARKER_FILE
    current_head = _git_rev_parse_head(repo_dir)
    if not current_head:
        logger.warning("Could not determine current HEAD, skipping commit notification")
        return

    last_notified = _read_marker(marker_path)

    if last_notified == current_head:
        logger.debug("No new commits since last notification", head=current_head[:12])
        return

    if last_notified is None:
        # First run — record current HEAD, don't spam history
        logger.info(
            "First run: recording current HEAD as baseline",
            head=current_head[:12],
        )
        _write_marker(marker_path, current_head)
        return

    # Get commit log between old and new HEAD
    commits = _git_log_between(repo_dir, last_notified, current_head)
    if not commits:
        logger.info(
            "No commits found between markers (force push or rebase?)",
            old=last_notified[:12],
            new=current_head[:12],
        )
        _write_marker(marker_path, current_head)
        return

    # Format and send
    message = _format_commit_message(commits, current_head)

    try:
        await bot.send_message(
            chat_id=chat_id,
            message_thread_id=message_thread_id,
            text=message,
            parse_mode="HTML",
        )
        logger.info(
            "Commit notification sent",
            chat_id=chat_id,
            thread_id=message_thread_id,
            commit_count=len(commits),
            head=current_head[:12],
        )
    except TelegramError as e:
        logger.error(
            "Failed to send commit notification",
            chat_id=chat_id,
            error=str(e),
        )

    # Update marker after send attempt (avoid retry spam)
    _write_marker(marker_path, current_head)


def _git_rev_parse_head(repo_dir: Path) -> Optional[str]:
    """Get current HEAD commit hash."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            cwd=repo_dir,
            timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception as e:
        logger.warning("git rev-parse HEAD failed", error=str(e))
    return None


def _git_log_between(
    repo_dir: Path, old_ref: str, new_ref: str
) -> List[Dict[str, str]]:
    """Get commit summaries between two refs.

    Returns list of dicts with 'hash', 'author', 'subject' keys.
    """
    try:
        # %H = full hash, %an = author name, %s = subject
        result = subprocess.run(
            [
                "git",
                "log",
                "--format=%H|%an|%s",
                f"{old_ref}..{new_ref}",
            ],
            capture_output=True,
            text=True,
            cwd=repo_dir,
            timeout=10,
        )
        if result.returncode != 0:
            return []

        commits: List[Dict[str, str]] = []
        for line in result.stdout.strip().splitlines():
            parts = line.split("|", 2)
            if len(parts) == 3:
                commits.append(
                    {
                        "hash": parts[0],
                        "author": parts[1],
                        "subject": parts[2],
                    }
                )
        return commits
    except Exception as e:
        logger.warning("git log failed", error=str(e))
        return []


def _format_commit_message(commits: List[Dict[str, str]], current_head: str) -> str:
    """Format commits into an HTML Telegram message."""
    count = len(commits)
    header = (
        f"\U0001f680 <b>New update</b> "
        f"({count} commit{'s' if count != 1 else ''})\n"
    )
    lines = [header]

    # Collect unique authors
    authors = sorted({c["author"] for c in commits})

    # Show up to 10 commits, truncate the rest
    display_commits = commits[:10]
    for c in display_commits:
        short_hash = c["hash"][:7]
        subject = _escape_html(c["subject"])
        lines.append(f"<code>{short_hash}</code> {subject}")

    if count > 10:
        lines.append(f"\n<i>\u2026 and {count - 10} more</i>")

    # Authors summary
    authors_str = ", ".join(_escape_html(a) for a in authors)
    lines.append(f"\n<b>By:</b> {authors_str}")
    lines.append(f"<code>HEAD \u2192 {current_head[:12]}</code>")

    return "\n".join(lines)


def _escape_html(text: str) -> str:
    """Minimal HTML escaping for Telegram."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _read_marker(path: Path) -> Optional[str]:
    """Read last-notified commit hash from marker file."""
    try:
        if path.exists():
            content = path.read_text().strip()
            if content:
                return content
    except Exception as e:
        logger.warning("Could not read commit marker", path=str(path), error=str(e))
    return None


def _write_marker(path: Path, commit_hash: str) -> None:
    """Write commit hash to marker file."""
    try:
        path.write_text(commit_hash + "\n")
    except Exception as e:
        logger.error("Could not write commit marker", path=str(path), error=str(e))
