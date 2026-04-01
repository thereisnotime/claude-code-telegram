"""YAML-backed project registry for thread mode."""

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

import structlog
import yaml  # type: ignore[import-untyped]

logger = structlog.get_logger(__name__)

PROJECT_TYPE_WORKSPACE = "workspace"
PROJECT_TYPE_NOTIFICATION = "notification"
VALID_PROJECT_TYPES = {PROJECT_TYPE_WORKSPACE, PROJECT_TYPE_NOTIFICATION}


@dataclass(frozen=True)
class ProjectDefinition:
    """Project entry from YAML configuration."""

    slug: str
    name: str
    relative_path: Optional[Path] = None
    absolute_path: Optional[Path] = None
    enabled: bool = True
    project_type: str = PROJECT_TYPE_WORKSPACE


class ProjectRegistry:
    """In-memory validated project registry."""

    def __init__(self, projects: List[ProjectDefinition]) -> None:
        self._projects = projects
        self._by_slug: Dict[str, ProjectDefinition] = {p.slug: p for p in projects}

    @property
    def projects(self) -> List[ProjectDefinition]:
        """Return all projects."""
        return list(self._projects)

    def list_enabled(self) -> List[ProjectDefinition]:
        """Return enabled projects only."""
        return [p for p in self._projects if p.enabled]

    def list_workspaces(self) -> List[ProjectDefinition]:
        """Return enabled workspace projects only."""
        return [
            p
            for p in self._projects
            if p.enabled and p.project_type == PROJECT_TYPE_WORKSPACE
        ]

    def list_by_type(self, project_type: str) -> List[ProjectDefinition]:
        """Return enabled projects of a specific type."""
        return [
            p for p in self._projects if p.enabled and p.project_type == project_type
        ]

    def get_by_slug(self, slug: str) -> Optional[ProjectDefinition]:
        """Get project by slug."""
        return self._by_slug.get(slug)


def load_project_registry(
    config_path: Path, approved_directory: Path
) -> ProjectRegistry:
    """Load and validate project definitions from YAML."""
    if not config_path.exists():
        raise ValueError(f"Projects config file does not exist: {config_path}")

    with open(config_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    if not isinstance(data, dict):
        raise ValueError("Projects config must be a YAML object")

    raw_projects = data.get("projects")
    if not isinstance(raw_projects, list):
        raise ValueError("Projects config must contain a 'projects' list")
    if not raw_projects:
        logger.warning("Projects config contains an empty 'projects' list")
        return ProjectRegistry([])

    approved_root = approved_directory.resolve()
    seen_slugs: set[str] = set()
    seen_names: set[str] = set()
    seen_rel_paths: set[str] = set()
    projects: List[ProjectDefinition] = []

    for idx, raw in enumerate(raw_projects):
        if not isinstance(raw, dict):
            raise ValueError(f"Project entry at index {idx} must be an object")

        slug = str(raw.get("slug", "")).strip()
        name = str(raw.get("name", "")).strip()
        enabled = bool(raw.get("enabled", True))
        raw_type = str(raw.get("type", PROJECT_TYPE_WORKSPACE)).strip()

        if not slug:
            raise ValueError(f"Project entry at index {idx} is missing 'slug'")
        if not name:
            raise ValueError(f"Project '{slug}' is missing 'name'")
        if raw_type not in VALID_PROJECT_TYPES:
            raise ValueError(
                f"Project '{slug}' has invalid type '{raw_type}', "
                f"must be one of {sorted(VALID_PROJECT_TYPES)}"
            )

        if slug in seen_slugs:
            raise ValueError(f"Duplicate project slug: {slug}")
        if name in seen_names:
            raise ValueError(f"Duplicate project name: {name}")
        seen_slugs.add(slug)
        seen_names.add(name)

        # Notification projects don't need a directory path
        if raw_type == PROJECT_TYPE_NOTIFICATION:
            projects.append(
                ProjectDefinition(
                    slug=slug,
                    name=name,
                    enabled=enabled,
                    project_type=raw_type,
                )
            )
            continue

        # Workspace projects require a valid directory
        rel_path_raw = str(raw.get("path", "")).strip()
        if not rel_path_raw:
            raise ValueError(f"Project '{slug}' is missing 'path'")

        rel_path = Path(rel_path_raw)
        if rel_path.is_absolute():
            raise ValueError(f"Project '{slug}' path must be relative: {rel_path_raw}")

        absolute_path = (approved_root / rel_path).resolve()

        try:
            absolute_path.relative_to(approved_root)
        except ValueError as e:
            raise ValueError(
                f"Project '{slug}' path outside approved " f"directory: {rel_path_raw}"
            ) from e

        if not absolute_path.exists() or not absolute_path.is_dir():
            logger.warning(
                "Skipping project with missing directory",
                slug=slug,
                path=str(absolute_path),
            )
            continue

        rel_path_norm = str(rel_path)
        if rel_path_norm in seen_rel_paths:
            raise ValueError(f"Duplicate project path: {rel_path_norm}")
        seen_rel_paths.add(rel_path_norm)

        projects.append(
            ProjectDefinition(
                slug=slug,
                name=name,
                relative_path=rel_path,
                absolute_path=absolute_path,
                enabled=enabled,
                project_type=raw_type,
            )
        )

    return ProjectRegistry(projects)
