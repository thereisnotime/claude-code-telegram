"""Filesystem-based Claude Code session discovery.

Discovers sessions from ~/.claude/projects/ by reading sessions-index.json
and scanning orphan .jsonl files. Also detects running sessions via PID files
in ~/.claude/sessions/.
"""

import json
import os
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import structlog

logger = structlog.get_logger(__name__)

CLAUDE_DIR = Path(os.path.expanduser("~/.claude"))
PROJECTS_DIR = CLAUDE_DIR / "projects"
SESSIONS_DIR = CLAUDE_DIR / "sessions"


@dataclass
class FSSessionEntry:
    """A single Claude Code session discovered from the filesystem."""

    session_id: str
    project_path: str
    project_slug: str
    first_prompt: str
    message_count: int
    created: str
    modified: str
    git_branch: str
    is_sidechain: bool
    custom_title: str
    jsonl_size_bytes: int
    session_dir_size_bytes: int
    total_size_bytes: int
    is_running: bool
    running_pid: int


def get_running_sessions(sessions_dir: Optional[Path] = None) -> Dict[str, int]:
    """Return {sessionId: pid} for currently running Claude sessions.

    Reads ~/.claude/sessions/*.json files. Each file is named {pid}.json
    and contains {"sessionId": "..."}.  Verifies PID is alive via os.kill(pid, 0).
    """
    sdir = sessions_dir or SESSIONS_DIR
    running: Dict[str, int] = {}
    if not sdir.is_dir():
        return running
    for pid_file in sdir.glob("*.json"):
        try:
            data = json.loads(pid_file.read_text())
            pid = int(pid_file.stem)
            try:
                os.kill(pid, 0)
            except OSError:
                continue
            sid = data.get("sessionId", "")
            if sid:
                running[sid] = pid
        except (OSError, ValueError, json.JSONDecodeError):
            continue
    return running


def _dir_size_bytes(path: Path) -> int:
    """Calculate directory size in bytes (recursive)."""
    total = 0
    try:
        for f in path.rglob("*"):
            if f.is_file():
                total += f.stat().st_size
    except OSError:
        pass
    return total


def _parse_jsonl_metadata(jsonl_path: Path) -> Dict[str, Any]:
    """Extract session metadata from a .jsonl file.

    Returns dict with keys: first_prompt, message_count, created, modified,
    git_branch, is_sidechain, project_path.
    """
    meta: Dict[str, Any] = {
        "first_prompt": "",
        "message_count": 0,
        "created": "",
        "modified": "",
        "git_branch": "",
        "is_sidechain": False,
        "project_path": "",
    }
    first_ts = ""
    last_ts = ""
    msg_count = 0

    try:
        with jsonl_path.open() as f:
            for line in f:
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue

                ts = d.get("timestamp", "")
                if ts and not first_ts:
                    first_ts = ts
                if ts:
                    last_ts = ts

                if d.get("type") == "user":
                    msg_count += 1
                    if not meta["first_prompt"]:
                        msg = d.get("message", "")
                        if isinstance(msg, dict):
                            for c in msg.get("content", []):
                                if isinstance(c, dict) and c.get("type") == "text":
                                    meta["first_prompt"] = c["text"][:200]
                                    break
                        elif isinstance(msg, str):
                            meta["first_prompt"] = msg[:200]
                    if not meta["git_branch"]:
                        meta["git_branch"] = d.get("gitBranch", "")
                    if not meta["project_path"]:
                        meta["project_path"] = d.get("cwd", "")
                    if d.get("isSidechain"):
                        meta["is_sidechain"] = True
                elif d.get("type") == "assistant":
                    msg_count += 1
    except OSError:
        pass

    meta["message_count"] = msg_count
    meta["created"] = first_ts
    meta["modified"] = last_ts
    return meta


def time_ago(ts_str: str) -> str:
    """Convert ISO timestamp to relative time string."""
    if not ts_str:
        return "?"
    try:
        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        elapsed = time.time() - dt.timestamp()
        if elapsed < 60:
            return f"{int(elapsed)}s ago"
        elif elapsed < 3600:
            return f"{int(elapsed / 60)}m ago"
        elif elapsed < 86400:
            return f"{elapsed / 3600:.1f}h ago"
        else:
            return f"{elapsed / 86400:.0f}d ago"
    except (ValueError, TypeError):
        return "?"


def discover_fs_sessions(
    projects_dir: Optional[Path] = None,
) -> List[FSSessionEntry]:
    """Discover all filesystem sessions from ~/.claude/projects/.

    Phase 1: Read sessions-index.json if it exists, iterate entries[].
    Phase 2: Glob *.jsonl files not already indexed (orphan/SDK sessions),
             parse metadata from JSONL content.

    Returns list of FSSessionEntry objects sorted by modified timestamp (newest first).
    """
    pdir = projects_dir or PROJECTS_DIR
    if not pdir.is_dir():
        return []

    running = get_running_sessions()
    sessions: List[FSSessionEntry] = []

    for proj_dir in sorted(pdir.iterdir()):
        if not proj_dir.is_dir():
            continue

        project_slug = proj_dir.name
        indexed_sids: set[str] = set()

        # Phase 1: sessions-index.json
        idx_file = proj_dir / "sessions-index.json"
        if idx_file.exists():
            try:
                data = json.loads(idx_file.read_text())
            except (OSError, json.JSONDecodeError):
                data = {}

            project_path = data.get("originalPath", "")

            for entry in data.get("entries", []):
                sid = entry.get("sessionId", "")
                if not sid:
                    continue

                indexed_sids.add(sid)

                jsonl_path = proj_dir / f"{sid}.jsonl"
                jsonl_size = 0
                if jsonl_path.exists():
                    try:
                        jsonl_size = jsonl_path.stat().st_size
                    except OSError:
                        pass

                session_subdir = proj_dir / sid
                subdir_size = (
                    _dir_size_bytes(session_subdir) if session_subdir.is_dir() else 0
                )

                is_running = sid in running
                running_pid = running.get(sid, 0)

                sessions.append(
                    FSSessionEntry(
                        session_id=sid,
                        project_path=project_path,
                        project_slug=project_slug,
                        first_prompt=entry.get("firstPrompt", ""),
                        message_count=entry.get("messageCount", 0),
                        created=entry.get("created", ""),
                        modified=entry.get("modified", ""),
                        git_branch=entry.get("gitBranch", ""),
                        is_sidechain=entry.get("isSidechain", False),
                        custom_title=entry.get("customTitle", ""),
                        jsonl_size_bytes=jsonl_size,
                        session_dir_size_bytes=subdir_size,
                        total_size_bytes=jsonl_size + subdir_size,
                        is_running=is_running,
                        running_pid=running_pid,
                    )
                )

        # Phase 2: Orphan .jsonl files not in index
        for jsonl_file in proj_dir.glob("*.jsonl"):
            sid = jsonl_file.stem
            if sid in indexed_sids:
                continue

            try:
                jsonl_size = jsonl_file.stat().st_size
            except OSError:
                jsonl_size = 0

            session_subdir = proj_dir / sid
            subdir_size = (
                _dir_size_bytes(session_subdir) if session_subdir.is_dir() else 0
            )

            meta = _parse_jsonl_metadata(jsonl_file)

            is_running = sid in running
            running_pid = running.get(sid, 0)

            sessions.append(
                FSSessionEntry(
                    session_id=sid,
                    project_path=meta.get("project_path", ""),
                    project_slug=project_slug,
                    first_prompt=meta.get("first_prompt", ""),
                    message_count=meta.get("message_count", 0),
                    created=meta.get("created", ""),
                    modified=meta.get("modified", ""),
                    git_branch=meta.get("git_branch", ""),
                    is_sidechain=meta.get("is_sidechain", False),
                    custom_title="",
                    jsonl_size_bytes=jsonl_size,
                    session_dir_size_bytes=subdir_size,
                    total_size_bytes=jsonl_size + subdir_size,
                    is_running=is_running,
                    running_pid=running_pid,
                )
            )

    # Sort by modified timestamp, newest first
    sessions.sort(key=lambda s: s.modified or "", reverse=True)
    return sessions
