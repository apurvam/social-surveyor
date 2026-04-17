from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError


class ConfigError(Exception):
    """Raised when a project's config is missing or fails validation.

    The message is user-facing — it's printed by the CLI on load failure,
    so it must be readable without a Python stack trace.
    """


class SourceConfig(BaseModel):
    """Base class for per-source config blocks.

    Subclasses declare source-specific fields. Lives here so session 2 can
    add ``HackerNewsSourceConfig``, ``GithubSourceConfig``, etc. without
    touching this module's public surface.
    """

    model_config = ConfigDict(extra="forbid")


class RedditSourceConfig(SourceConfig):
    subreddits: list[str] = Field(
        ..., min_length=1, description="Subreddits to poll (without the leading 'r/')."
    )
    queries: list[str] = Field(
        ..., min_length=1, description="Search queries applied to each subreddit."
    )
    limit_per_query: int = Field(
        default=100,
        ge=1,
        le=1000,
        description="Max results per (subreddit, query) pair on a single poll.",
    )
    time_filter: Literal["hour", "day", "week", "month", "year", "all"] = Field(
        default="week",
        description="Default Reddit search time filter for poll() and fallback for backfill().",
    )


class ProjectConfig(BaseModel):
    """Aggregated config for a single project.

    Each ``<source_type>`` field is optional — a project only needs to
    configure the sources it uses. Adding a new source type in a later
    session adds a new optional field here.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    reddit: RedditSourceConfig | None = None


def _load_yaml(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except yaml.YAMLError as e:
        raise ConfigError(f"{path}: invalid YAML ({e})") from e
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: expected a mapping at the top level, got {type(data).__name__}")
    return data


def _format_validation_error(path: Path, err: ValidationError) -> str:
    lines = [f"{path}: invalid config"]
    for e in err.errors():
        loc = ".".join(str(p) for p in e["loc"]) or "<root>"
        lines.append(f"  - {loc}: {e['msg']}")
    return "\n".join(lines)


def load_project_config(
    project: str,
    projects_root: Path | str = "projects",
) -> ProjectConfig:
    """Load and validate the config for a project.

    Reads every ``projects/<project>/sources/*.yaml`` file and merges
    them into a single :class:`ProjectConfig`. Raises :class:`ConfigError`
    with a human-readable message if the project directory is missing,
    a YAML file is malformed, or validation fails.
    """
    root = Path(projects_root)
    project_dir = root / project

    if not project_dir.is_dir():
        raise ConfigError(f"project '{project}' not found at {project_dir}")

    sources_dir = project_dir / "sources"
    if not sources_dir.is_dir():
        raise ConfigError(f"project '{project}' has no sources/ directory at {sources_dir}")

    data: dict[str, Any] = {"name": project}

    reddit_file = sources_dir / "reddit.yaml"
    if reddit_file.is_file():
        data["reddit"] = _load_yaml(reddit_file)

    try:
        return ProjectConfig.model_validate(data)
    except ValidationError as e:
        raise ConfigError(_format_validation_error(project_dir, e)) from e
