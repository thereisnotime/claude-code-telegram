"""Tests for YAML project registry loading."""

from pathlib import Path

import pytest

from src.projects.registry import (
    ProjectDefinition,
    ProjectRegistry,
    load_project_registry,
)


def test_load_project_registry_valid(tmp_path: Path) -> None:
    approved = tmp_path / "projects"
    approved.mkdir()
    (approved / "app_one").mkdir()
    (approved / "app_two").mkdir()

    config_file = tmp_path / "projects.yaml"
    config_file.write_text(
        "projects:\n"
        "  - slug: app1\n"
        "    name: App One\n"
        "    path: app_one\n"
        "  - slug: app2\n"
        "    name: App Two\n"
        "    path: app_two\n"
        "    enabled: false\n",
        encoding="utf-8",
    )

    registry = load_project_registry(config_file, approved)

    assert len(registry.projects) == 2
    enabled = registry.list_enabled()
    assert len(enabled) == 1
    assert enabled[0].slug == "app1"


def test_load_project_registry_rejects_duplicate_slug(tmp_path: Path) -> None:
    approved = tmp_path / "projects"
    approved.mkdir()
    (approved / "app_one").mkdir()
    (approved / "app_two").mkdir()

    config_file = tmp_path / "projects.yaml"
    config_file.write_text(
        "projects:\n"
        "  - slug: app\n"
        "    name: App One\n"
        "    path: app_one\n"
        "  - slug: app\n"
        "    name: App Two\n"
        "    path: app_two\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError) as exc_info:
        load_project_registry(config_file, approved)

    assert "Duplicate project slug" in str(exc_info.value)


def test_load_project_registry_rejects_outside_approved_dir(tmp_path: Path) -> None:
    approved = tmp_path / "projects"
    approved.mkdir()

    config_file = tmp_path / "projects.yaml"
    config_file.write_text(
        "projects:\n" "  - slug: app\n" "    name: App\n" "    path: ../outside\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError) as exc_info:
        load_project_registry(config_file, approved)

    assert "outside approved directory" in str(exc_info.value)


def test_all_configured_slugs_includes_missing_directory(tmp_path: Path) -> None:
    """Projects with missing directories appear in all_configured_slugs."""
    approved = tmp_path / "projects"
    approved.mkdir()
    (approved / "app_one").mkdir()
    # app_two directory intentionally NOT created

    config_file = tmp_path / "projects.yaml"
    config_file.write_text(
        "projects:\n"
        "  - slug: app1\n"
        "    name: App One\n"
        "    path: app_one\n"
        "  - slug: app2\n"
        "    name: App Two\n"
        "    path: app_two\n",
        encoding="utf-8",
    )

    registry = load_project_registry(config_file, approved)

    # app2 is skipped from the project list (missing dir)
    assert len(registry.projects) == 1
    assert registry.projects[0].slug == "app1"

    # But app2 is still tracked in all_configured_slugs
    assert registry.all_configured_slugs == frozenset({"app1", "app2"})


def test_all_configured_slugs_excludes_disabled(tmp_path: Path) -> None:
    """Disabled projects are excluded from all_configured_slugs."""
    approved = tmp_path / "projects"
    approved.mkdir()
    (approved / "app_one").mkdir()
    (approved / "app_two").mkdir()

    config_file = tmp_path / "projects.yaml"
    config_file.write_text(
        "projects:\n"
        "  - slug: app1\n"
        "    name: App One\n"
        "    path: app_one\n"
        "  - slug: app2\n"
        "    name: App Two\n"
        "    path: app_two\n"
        "    enabled: false\n",
        encoding="utf-8",
    )

    registry = load_project_registry(config_file, approved)

    assert len(registry.projects) == 2
    assert registry.all_configured_slugs == frozenset({"app1"})


def test_all_configured_slugs_includes_notification_projects(
    tmp_path: Path,
) -> None:
    """Notification projects (no directory) appear in all_configured_slugs."""
    approved = tmp_path / "projects"
    approved.mkdir()
    (approved / "app_one").mkdir()

    config_file = tmp_path / "projects.yaml"
    config_file.write_text(
        "projects:\n"
        "  - slug: app1\n"
        "    name: App One\n"
        "    path: app_one\n"
        "  - slug: news\n"
        "    name: News\n"
        "    type: notification\n",
        encoding="utf-8",
    )

    registry = load_project_registry(config_file, approved)

    assert registry.all_configured_slugs == frozenset({"app1", "news"})


def test_registry_all_configured_slugs_defaults_to_enabled() -> None:
    """ProjectRegistry without explicit all_configured_slugs derives from projects."""
    projects = [
        ProjectDefinition(slug="a", name="A", enabled=True),
        ProjectDefinition(slug="b", name="B", enabled=False),
        ProjectDefinition(slug="c", name="C", enabled=True),
    ]
    registry = ProjectRegistry(projects)

    assert registry.all_configured_slugs == frozenset({"a", "c"})
