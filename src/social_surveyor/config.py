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
    reddit_username: str = Field(
        ...,
        min_length=1,
        description=(
            "Reddit handle of the person running social-surveyor. Used only to "
            "construct a polite User-Agent header (not for auth). Required — RSS "
            "polls with generic user agents get rate-limited aggressively."
        ),
    )
    min_seconds_between_requests: float = Field(
        default=2.0,
        ge=0.0,
        description=(
            "Client-side throttle between RSS requests. Reddit's unauthenticated "
            "limits are aggressive; 2s default keeps us well clear."
        ),
    )
    limit_per_query: int = Field(
        default=100,
        ge=1,
        le=100,
        description="Max results per (subreddit, query) pair; passed to RSS as ?limit=.",
    )
    time_filter: Literal["hour", "day", "week", "month", "year", "all"] = Field(
        default="week",
        description="Reddit search time filter; passed to RSS as ?t=.",
    )


class HackerNewsSourceConfig(SourceConfig):
    queries: list[str] = Field(..., min_length=1)
    tags: list[Literal["story", "comment"]] = Field(default_factory=lambda: ["story", "comment"])
    max_results_per_query: int = Field(default=50, ge=1, le=1000)


class GitHubQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    q: str
    type: Literal["issues", "prs", "both"] = Field(
        default="issues",
        description="Maps to GitHub's 'type:' qualifier. 'issues' excludes PRs.",
    )


class GitHubSourceConfig(SourceConfig):
    queries: list[GitHubQuery] = Field(..., min_length=1)
    orgs_watchlist: list[str] = Field(default_factory=list)
    max_results_per_query: int = Field(default=30, ge=1, le=100)
    # Belt-and-suspenders on top of GitHub's own rate limiter: how many
    # follow-up /comments calls we'll make per poll to resolve in:comments
    # matches into actual comment objects.
    max_comment_fetches_per_poll: int = Field(default=100, ge=1, le=1000)


class XQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(..., min_length=1)
    query: str = Field(..., min_length=1)


class XSourceConfig(SourceConfig):
    queries: list[XQuery] = Field(..., min_length=1)
    max_results_per_query: int = Field(
        default=100,
        ge=10,
        le=100,
        description="X Recent Search allows 10-100 per request.",
    )
    poll_interval_minutes: int = Field(
        default=10,
        ge=1,
        description="Informational; the scheduler is a session 5 concern.",
    )
    daily_read_cap: int = Field(
        default=500,
        ge=1,
        description="Hard daily ceiling on post reads. A query is skipped if running "
        "it could push today's total over this cap.",
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
    hackernews: HackerNewsSourceConfig | None = None
    github: GitHubSourceConfig | None = None
    x: XSourceConfig | None = None


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

    # YAML filename -> ProjectConfig field name.
    source_files = {
        "reddit.yaml": "reddit",
        "hackernews.yaml": "hackernews",
        "github.yaml": "github",
        "x.yaml": "x",
    }
    for filename, field_name in source_files.items():
        f = sources_dir / filename
        if f.is_file():
            data[field_name] = _load_yaml(f)

    try:
        return ProjectConfig.model_validate(data)
    except ValidationError as e:
        raise ConfigError(_format_validation_error(project_dir, e)) from e
