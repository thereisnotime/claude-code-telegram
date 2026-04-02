"""Project registry and Telegram thread management."""

from .registry import ProjectDefinition, ProjectRegistry, load_project_registry
from .thread_manager import (
    PrivateTopicsUnavailableError,
    ProjectThreadManager,
)
from .topic_resolver import resolve_topics_if_enabled

__all__ = [
    "ProjectDefinition",
    "ProjectRegistry",
    "load_project_registry",
    "ProjectThreadManager",
    "PrivateTopicsUnavailableError",
    "resolve_topics_if_enabled",
]
