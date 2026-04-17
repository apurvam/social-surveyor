from __future__ import annotations

from pathlib import Path

import pytest

from social_surveyor.config import ConfigError, load_project_config


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_load_valid_reddit_config(tmp_path: Path) -> None:
    _write(
        tmp_path / "demo" / "sources" / "reddit.yaml",
        """
subreddits: [devops, kubernetes]
queries: ["prometheus storage"]
limit_per_query: 50
time_filter: month
""",
    )

    cfg = load_project_config("demo", projects_root=tmp_path)

    assert cfg.name == "demo"
    assert cfg.reddit is not None
    assert cfg.reddit.subreddits == ["devops", "kubernetes"]
    assert cfg.reddit.queries == ["prometheus storage"]
    assert cfg.reddit.limit_per_query == 50
    assert cfg.reddit.time_filter == "month"


def test_project_with_no_sources_files_still_loads(tmp_path: Path) -> None:
    # sources/ exists but is empty — allowed; the project just has no
    # sources configured yet.
    (tmp_path / "demo" / "sources").mkdir(parents=True)

    cfg = load_project_config("demo", projects_root=tmp_path)

    assert cfg.name == "demo"
    assert cfg.reddit is None


def test_missing_project_dir_raises_readable_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as exc:
        load_project_config("does-not-exist", projects_root=tmp_path)

    msg = str(exc.value)
    assert "does-not-exist" in msg
    assert "not found" in msg


def test_missing_sources_dir_raises_readable_error(tmp_path: Path) -> None:
    (tmp_path / "demo").mkdir()

    with pytest.raises(ConfigError) as exc:
        load_project_config("demo", projects_root=tmp_path)

    assert "no sources/" in str(exc.value)


def test_invalid_yaml_raises_readable_error(tmp_path: Path) -> None:
    _write(
        tmp_path / "demo" / "sources" / "reddit.yaml",
        "subreddits: [devops\nqueries: ['unclosed",
    )

    with pytest.raises(ConfigError) as exc:
        load_project_config("demo", projects_root=tmp_path)

    assert "invalid YAML" in str(exc.value)


def test_invalid_schema_raises_readable_error(tmp_path: Path) -> None:
    _write(
        tmp_path / "demo" / "sources" / "reddit.yaml",
        """
subreddits: []
queries: []
""",
    )

    with pytest.raises(ConfigError) as exc:
        load_project_config("demo", projects_root=tmp_path)

    msg = str(exc.value)
    assert "invalid config" in msg
    assert "reddit.subreddits" in msg or "subreddits" in msg


def test_unknown_time_filter_is_rejected(tmp_path: Path) -> None:
    _write(
        tmp_path / "demo" / "sources" / "reddit.yaml",
        """
subreddits: [devops]
queries: ["x"]
time_filter: fortnight
""",
    )

    with pytest.raises(ConfigError) as exc:
        load_project_config("demo", projects_root=tmp_path)

    assert "time_filter" in str(exc.value)


def test_extra_keys_rejected(tmp_path: Path) -> None:
    _write(
        tmp_path / "demo" / "sources" / "reddit.yaml",
        """
subreddits: [devops]
queries: ["x"]
typo_field: 1
""",
    )

    with pytest.raises(ConfigError) as exc:
        load_project_config("demo", projects_root=tmp_path)

    assert "typo_field" in str(exc.value)
