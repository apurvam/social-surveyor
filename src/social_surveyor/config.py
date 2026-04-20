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
        default=6.0,
        ge=0.0,
        description=(
            "Client-side throttle between RSS requests. Live observation shows "
            "Reddit's unauthenticated bucket is ~100 requests per ~10 minutes, "
            "so 6s sustains indefinitely without 429s. The source also honors "
            "x-ratelimit-reset on any 429 that does slip through."
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


class Category(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(..., min_length=1, pattern=r"^[a-z][a-z0-9_]*$")
    label: str = Field(..., min_length=1)
    description: str = Field(..., min_length=1)


class UrgencyBand(BaseModel):
    model_config = ConfigDict(extra="forbid")
    range: list[int] = Field(..., min_length=2, max_length=2)
    meaning: str = Field(..., min_length=1)


class CategoryConfig(BaseModel):
    """Project-level category taxonomy and urgency scale.

    Read by the Session 2.75 labeler and (later) extended by
    Session 3's ``classifier.yaml``. Keep stable once labeling starts —
    renaming a category invalidates prior labels.
    """

    model_config = ConfigDict(extra="forbid")

    version: int = Field(default=1, ge=1)
    categories: list[Category] = Field(..., min_length=1)
    urgency_scale: list[UrgencyBand] = Field(..., min_length=1)


class FewShotExample(BaseModel):
    """One worked example fed to the classifier to pin down a category.

    ``expected_category`` must match a category id from the project's
    categories.yaml; the cross-file check lives in
    :func:`load_classifier_config` because a pydantic model can only see
    its own YAML.
    """

    model_config = ConfigDict(extra="forbid")

    title: str = Field(..., min_length=1)
    body: str = Field(..., min_length=1)
    expected_category: str = Field(..., min_length=1)
    expected_urgency: int = Field(..., ge=0, le=10)
    note: str = Field(default="")


class ClassifierConfig(BaseModel):
    """Per-project classifier configuration.

    Extends (never redefines) the taxonomy in ``categories.yaml`` — the
    ``categories_file`` field names the taxonomy file this classifier
    binds to, so renaming ``categories.yaml`` or splitting into
    private/public later stays a one-field change.

    ``prompt_version`` is the single most load-bearing field: it's
    stamped on every classification so the eval harness can A/B v1 vs
    v2 without re-classifying items. Bump it whenever a prompt-affecting
    field changes.
    """

    model_config = ConfigDict(extra="forbid")

    version: int = Field(default=1, ge=1)
    prompt_version: str = Field(..., min_length=1)
    categories_file: str = Field(
        default="categories.yaml",
        description="Path (relative to the project dir) of the taxonomy this "
        "classifier binds to. Must exist and parse as a CategoryConfig.",
    )
    icp_description: str = Field(
        ...,
        min_length=1,
        description="Free-text ICP context inlined into the system prompt.",
    )
    additional_instructions: str = Field(
        default="",
        description=(
            "Optional decision-rules block emitted in the system prompt "
            "between the urgency scale and few-shot examples. Use for "
            "prompt-version-specific heuristics (e.g. 'when in doubt "
            "between alert-worthy and neutral, prefer neutral'). Bump "
            "prompt_version when the content changes."
        ),
    )
    few_shot_examples: list[FewShotExample] = Field(default_factory=list)
    model: str = Field(..., min_length=1)
    max_tokens: int = Field(..., ge=1, le=8192)
    temperature: float = Field(..., ge=0.0, le=2.0)
    # Retries on transient API failures only (network errors, 5xx).
    # Malformed-JSON responses get one free re-prompt independently.
    max_retries: int = Field(default=1, ge=0, le=5)
    backoff_seconds: float = Field(default=2.0, ge=0.0)


class ImmediateConfig(BaseModel):
    """Routing rules for immediate Slack alerts."""

    model_config = ConfigDict(extra="forbid")

    threshold_urgency: int = Field(
        default=7,
        ge=0,
        le=10,
        description="Items with urgency >= this AND category in alert_worthy_categories alert.",
    )
    alert_worthy_categories: list[str] = Field(..., min_length=1)
    webhook_secret: str = Field(
        ...,
        min_length=1,
        description="Env var name holding the immediate-channel incoming webhook URL.",
    )
    max_item_age_hours: int = Field(
        default=72,
        ge=1,
        description=(
            "Skip immediate alerts for items whose created_at is older than this. "
            "Items still route to the digest. Set to a very large value "
            "(e.g. 87600 for ~10 years) to disable the cutoff."
        ),
    )


class DigestScheduleConfig(BaseModel):
    """Daily digest schedule (hour/minute/timezone)."""

    model_config = ConfigDict(extra="forbid")

    hour: int = Field(..., ge=0, le=23)
    minute: int = Field(..., ge=0, le=59)
    timezone: str = Field(default="UTC", min_length=1)


class DigestConfig(BaseModel):
    """Routing rules for the daily digest."""

    model_config = ConfigDict(extra="forbid")

    schedule: DigestScheduleConfig
    webhook_secret: str = Field(
        ...,
        min_length=1,
        description="Env var name holding the digest-channel incoming webhook URL.",
    )
    window_hours: int = Field(
        default=24,
        ge=1,
        description="Window of classifications to include in the daily digest.",
    )


class CostCapsConfig(BaseModel):
    """Hard daily ceilings. Session 4 parses these; full enforcement is
    a follow-on — X already has its own ``daily_read_cap`` today."""

    model_config = ConfigDict(extra="forbid")

    daily_haiku_tokens: int = Field(default=500_000, ge=0)
    daily_x_reads: int = Field(default=2_000, ge=0)


class RoutingConfig(BaseModel):
    """Top-level routing config (``projects/<n>/routing.yaml``)."""

    model_config = ConfigDict(extra="forbid")

    version: int = Field(default=1, ge=1)
    immediate: ImmediateConfig
    digest: DigestConfig
    cost_caps: CostCapsConfig = Field(default_factory=CostCapsConfig)


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


def load_categories(
    project: str,
    projects_root: Path | str = "projects",
) -> CategoryConfig:
    """Load the per-project category + urgency taxonomy.

    Raises :class:`ConfigError` with a human-readable message if the file
    is missing or fails validation.
    """
    root = Path(projects_root)
    path = root / project / "categories.yaml"
    if not path.is_file():
        raise ConfigError(
            f"project '{project}' has no categories.yaml at {path}; "
            f"run `social-surveyor setup --project {project}` or copy "
            f"projects/example/categories.yaml as a starting point"
        )
    data = _load_yaml(path)
    try:
        return CategoryConfig.model_validate(data)
    except ValidationError as e:
        raise ConfigError(_format_validation_error(path, e)) from e


def load_classifier_config(
    project: str,
    projects_root: Path | str = "projects",
) -> ClassifierConfig:
    """Load the per-project classifier configuration.

    Parses ``projects/<project>/classifier.yaml`` into a
    :class:`ClassifierConfig`, then resolves ``categories_file`` and
    verifies every ``few_shot_examples.expected_category`` references a
    category id that actually exists in the taxonomy. Cross-file
    validation intentionally lives here and not on the pydantic model,
    since a model only sees its own YAML.

    Raises :class:`ConfigError` with a human-readable message if the
    file is missing, fails schema validation, the referenced taxonomy
    is missing or invalid, or any example references an unknown
    category.
    """
    root = Path(projects_root)
    project_dir = root / project
    path = project_dir / "classifier.yaml"
    if not path.is_file():
        raise ConfigError(
            f"project '{project}' has no classifier.yaml at {path}; "
            f"copy projects/example/classifier.yaml as a starting point"
        )
    data = _load_yaml(path)
    try:
        cfg = ClassifierConfig.model_validate(data)
    except ValidationError as e:
        raise ConfigError(_format_validation_error(path, e)) from e

    categories_path = project_dir / cfg.categories_file
    if not categories_path.is_file():
        raise ConfigError(
            f"{path}: categories_file={cfg.categories_file!r} resolves to "
            f"{categories_path} which does not exist"
        )
    cats_data = _load_yaml(categories_path)
    try:
        cats = CategoryConfig.model_validate(cats_data)
    except ValidationError as e:
        raise ConfigError(_format_validation_error(categories_path, e)) from e

    valid_ids = {c.id for c in cats.categories}
    bad = [
        (i, ex.expected_category)
        for i, ex in enumerate(cfg.few_shot_examples)
        if ex.expected_category not in valid_ids
    ]
    if bad:
        valid_display = ", ".join(sorted(valid_ids))
        lines = [f"{path}: invalid few_shot_examples"]
        for i, cat in bad:
            lines.append(
                f"  - few_shot_examples.{i}.expected_category: "
                f"{cat!r} is not a category id in {cfg.categories_file} "
                f"(valid: {valid_display})"
            )
        raise ConfigError("\n".join(lines))

    return cfg


def load_routing_config(
    project: str,
    projects_root: Path | str = "projects",
) -> RoutingConfig:
    """Load ``projects/<project>/routing.yaml``.

    Cross-file validation (that ``alert_worthy_categories`` are real
    category ids) happens here so a typo fails loud at load rather
    than silently routing nothing to the immediate channel.
    """
    root = Path(projects_root)
    project_dir = root / project
    path = project_dir / "routing.yaml"
    if not path.is_file():
        raise ConfigError(
            f"project '{project}' has no routing.yaml at {path}; "
            f"copy projects/example/routing.yaml as a starting point"
        )
    data = _load_yaml(path)
    try:
        cfg = RoutingConfig.model_validate(data)
    except ValidationError as e:
        raise ConfigError(_format_validation_error(path, e)) from e

    # Validate alert_worthy_categories against the project's taxonomy.
    # A typo here would silently drop the item into the digest channel
    # instead of alerting, which is exactly the wrong kind of bug for
    # a routing config.
    categories_path = project_dir / "categories.yaml"
    if categories_path.is_file():
        cats_data = _load_yaml(categories_path)
        try:
            cats = CategoryConfig.model_validate(cats_data)
        except ValidationError:
            # Don't block routing-config load on an invalid categories
            # file — that gets flagged separately when the classifier
            # loads. We just skip the cross-check.
            return cfg
        valid_ids = {c.id for c in cats.categories}
        bad = [c for c in cfg.immediate.alert_worthy_categories if c not in valid_ids]
        if bad:
            valid_display = ", ".join(sorted(valid_ids))
            raise ConfigError(
                f"{path}: immediate.alert_worthy_categories references unknown "
                f"category ids: {sorted(bad)} (valid: {valid_display})"
            )
    return cfg
