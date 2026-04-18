"""`social-surveyor setup` — interactive first-run credential wizard.

Captures Reddit username, GitHub token, X bearer token, Anthropic
API key, and optional Slack webhook placeholders. Writes secrets to
``.env`` (preserving unrelated existing keys). Updates Reddit's YAML
with the username.

Live validation:

- Reddit: fetch ``https://www.reddit.com/r/test/new.rss?limit=1`` with
  the constructed User-Agent. No cost, no auth — just confirms the
  User-Agent shape works.
- GitHub: ``GET /rate_limit`` with the token. Free, authenticated.
- X + Anthropic: syntactic-only. Every setup run making a paid call
  is the wrong pattern to build in; the first real poll/classify
  call is the authoritative live check.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import httpx
import structlog
import typer

from .config import ConfigError, load_project_config

log = structlog.get_logger(__name__)

# Tokens we capture. Order matters — this is the prompt sequence.
# "secret" determines whether we mask the default and hide input.
_SECRET_ENV_KEYS: tuple[tuple[str, str, bool], ...] = (
    ("GITHUB_TOKEN", "GitHub personal access token", True),
    ("X_BEARER_TOKEN", "X (Twitter) API v2 bearer token", True),
    ("ANTHROPIC_API_KEY", "Anthropic API key (needed in session 3)", True),
)


@dataclass
class SetupResult:
    """Summary returned by :func:`run_setup` for easy logging & tests."""

    reddit_username: str | None
    github_token_set: bool
    x_token_set: bool
    anthropic_key_set: bool
    slack_webhooks_set: list[str]
    validations: dict[str, str]  # source -> "ok" | "skipped" | "failed:<reason>"


def _mask(value: str) -> str:
    """Return first-4 + last-3 with elision in the middle.

    Short values (<= 7 chars) are shown as ``***`` so we don't
    accidentally reveal the whole thing.
    """
    if not value:
        return ""
    if len(value) <= 7:
        return "***"
    return f"{value[:4]}...{value[-3:]}"


def _read_env_file(path: Path) -> dict[str, str]:
    """Parse a minimal KEY=VALUE .env file. Preserves only the values.

    We don't try to round-trip comments because the setup wizard
    rewrites the file wholesale on save; we generate a fresh commented
    header and emit captured keys below it. Unrelated keys the user
    added by hand are preserved.
    """
    if not path.exists():
        return {}
    out: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip()
    return out


def _write_env_file(path: Path, values: dict[str, str]) -> None:
    """Write ``values`` to the .env file, preserving file-scoped comments
    we own (the Reddit RSS note) and any unrelated keys.

    Strategy: ignore the existing file's formatting. Emit a fixed
    header, then the session-captured keys, then any *other* keys we
    read in (user-added values we don't touch). Secrets are never
    printed in full by this function — the caller is responsible for
    not logging ``values``.
    """
    captured_keys = {k for k, _, _ in _SECRET_ENV_KEYS}
    slack_keys = sorted(k for k in values if k.startswith("SLACK_WEBHOOK_"))
    managed_keys = captured_keys | set(slack_keys)
    other_keys = [k for k in values if k not in managed_keys]

    lines: list[str] = [
        "# Session 2.5 note: Reddit is now RSS-based and needs NO credentials.",
        "# The operator's Reddit username is set per-project in",
        "# projects/<n>/sources/reddit.yaml (used to build a polite User-Agent),",
        "# not here.",
        "",
    ]
    for key, description, _ in _SECRET_ENV_KEYS:
        lines.append(f"# {description}.")
        lines.append(f"{key}={values.get(key, '')}")
        lines.append("")
    for slack_key in slack_keys:
        lines.append(f"{slack_key}={values.get(slack_key, '')}")
    if slack_keys:
        lines.append("")
    for key in other_keys:
        # Preserve unrelated keys the user added manually, without
        # stripping any formatting we can't see.
        lines.append(f"{key}={values[key]}")

    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def _validate_github(token: str) -> tuple[bool, str]:
    if not token:
        return False, "empty"
    try:
        resp = httpx.get(
            "https://api.github.com/rate_limit",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=10.0,
        )
    except httpx.HTTPError as e:
        return False, f"network error: {e}"
    if resp.status_code == 401:
        return False, "401 unauthorized (wrong token)"
    if resp.status_code >= 400:
        return False, f"HTTP {resp.status_code}"
    return True, "ok"


def _validate_reddit(username: str) -> tuple[bool, str]:
    if not username or username == "YOUR_REDDIT_USERNAME":
        return False, "placeholder username"
    ua = f"social-surveyor/setup (by /u/{username})"
    try:
        resp = httpx.get(
            "https://www.reddit.com/r/test/new.rss?limit=1",
            headers={"User-Agent": ua},
            timeout=10.0,
            follow_redirects=True,
        )
    except httpx.HTTPError as e:
        return False, f"network error: {e}"
    if resp.status_code == 403:
        return False, "403 (User-Agent rejected)"
    if resp.status_code >= 400:
        return False, f"HTTP {resp.status_code}"
    return True, "ok"


_X_BEARER_RE = re.compile(r"^AAAA[A-Za-z0-9%._~\-]+$")
_ANTHROPIC_KEY_RE = re.compile(r"^sk-ant-[A-Za-z0-9_-]+$")


def _validate_x_syntactic(token: str) -> tuple[bool, str]:
    if not token:
        return False, "empty"
    if len(token) < 80:
        return False, f"too short ({len(token)} chars); expected ~110"
    if not _X_BEARER_RE.match(token):
        return False, "does not match the v2 app-only bearer shape (should start with AAAA)"
    return True, "syntactic_ok (live check skipped by design)"


def _validate_anthropic_syntactic(key: str) -> tuple[bool, str]:
    if not key:
        return True, "skipped (empty; required for session 3)"
    if not _ANTHROPIC_KEY_RE.match(key):
        return False, "does not start with sk-ant- (check the paste)"
    return True, "syntactic_ok (live check skipped by design)"


def _prompt_secret(
    label: str,
    current: str,
    description: str,
    *,
    prompt_fn,
) -> str:
    """Prompt for a secret with masked default and Enter-to-keep.

    Returns the value (either the existing one or a newly entered
    string; empty string if user explicitly cleared).
    """
    display_default = _mask(current) if current else "not set"
    hint = f"{label} — {description}\n  current: [{display_default}]"
    entered = prompt_fn(f"{hint}\n  enter new value (press Enter to keep, or 'clear' to unset): ")
    if entered == "":
        return current
    if entered.lower() == "clear":
        return ""
    return entered


def _prompt_reddit_username(current: str, *, prompt_fn) -> str:
    hint = f"Reddit username — public, used to build a polite User-Agent\n  current: [{current or 'not set'}]"
    entered = prompt_fn(f"{hint}\n  enter username (press Enter to keep): ")
    return entered.strip() or current


def _update_reddit_yaml(project: str, username: str, projects_root: Path) -> None:
    """Rewrite the ``reddit_username`` value in ``reddit.yaml`` in place.

    Minimal edit: a line-level regex substitution so YAML comments and
    layout are preserved. Adds the line if missing.
    """
    path = projects_root / project / "sources" / "reddit.yaml"
    if not path.is_file():
        raise typer.BadParameter(
            f"no project found at projects/{project}/ — have you created it? (expected {path})"
        )
    text = path.read_text(encoding="utf-8")
    pattern = re.compile(r"^(reddit_username:)\s*.*$", re.MULTILINE)
    if pattern.search(text):
        new_text = pattern.sub(f"reddit_username: {username}", text, count=1)
    else:
        new_text = text.rstrip() + f"\nreddit_username: {username}\n"
    if new_text != text:
        path.write_text(new_text, encoding="utf-8")


def run_setup(
    project: str,
    projects_root: Path,
    env_path: Path,
    *,
    prompt_fn=typer.prompt,
    echo_fn=typer.echo,
) -> SetupResult:
    """Interactive wizard. Returns a :class:`SetupResult`.

    ``prompt_fn`` and ``echo_fn`` are injected so tests can script input
    without touching a real terminal. The default ``prompt_fn`` is
    :func:`typer.prompt`; the wizard calls it with ``default=""`` and a
    descriptive prompt string so masked defaults work uniformly.
    """

    # Simple input wrapper that accepts our own formatted prompt and
    # returns whatever the user types; always uses default="" so
    # Enter-to-keep is handled by _prompt_secret / _prompt_reddit_username.
    def _p(text: str) -> str:
        return prompt_fn(text, default="", show_default=False)

    # Step 1: load project config (catches a typo'd project name early).
    try:
        cfg = load_project_config(project, projects_root=projects_root)
    except ConfigError as e:
        raise typer.BadParameter(str(e)) from None

    echo_fn(f"social-surveyor setup — project '{project}'")
    echo_fn("Press Enter to keep the current value, or 'clear' to unset.\n")

    # --- Reddit username + YAML update -------------------------------
    current_username = (cfg.reddit.reddit_username if cfg.reddit else "") or ""
    new_username = _prompt_reddit_username(current_username, prompt_fn=_p)
    if new_username and new_username != "YOUR_REDDIT_USERNAME":
        _update_reddit_yaml(project, new_username, projects_root)

    # --- Secrets .env -------------------------------------------------
    env_values = _read_env_file(env_path)

    for key, desc, _secret in _SECRET_ENV_KEYS:
        env_values[key] = _prompt_secret(key, env_values.get(key, ""), desc, prompt_fn=_p)

    # --- Optional Slack webhooks (skippable) -------------------------
    slack_keys_set: list[str] = []
    for channel in ("IMMEDIATE", "DIGEST"):
        key = f"SLACK_WEBHOOK_{project.upper()}_{channel}"
        existing = env_values.get(key, "")
        entered = _prompt_secret(
            key,
            existing,
            f"Slack {channel.lower()} webhook (optional until session 4; Enter to skip)",
            prompt_fn=_p,
        )
        if entered:
            env_values[key] = entered
            slack_keys_set.append(key)
        elif key in env_values:
            # User cleared it explicitly.
            env_values.pop(key, None)

    _write_env_file(env_path, env_values)

    # --- Validation ---------------------------------------------------
    echo_fn("\nvalidating credentials (no paid calls)...")
    validations: dict[str, str] = {}

    if new_username and new_username != "YOUR_REDDIT_USERNAME":
        ok, reason = _validate_reddit(new_username)
        validations["reddit"] = "ok" if ok else f"failed:{reason}"
        log.info("setup.validate.reddit", ok=ok, reason=reason)
    else:
        validations["reddit"] = "skipped (no username)"

    gh = env_values.get("GITHUB_TOKEN", "")
    if gh:
        ok, reason = _validate_github(gh)
        validations["github"] = "ok" if ok else f"failed:{reason}"
        log.info("setup.validate.github", ok=ok, reason=reason)
    else:
        validations["github"] = "skipped (empty)"

    ok_x, reason_x = _validate_x_syntactic(env_values.get("X_BEARER_TOKEN", ""))
    validations["x"] = "ok" if ok_x else f"failed:{reason_x}"
    log.info("setup.validate.x", ok=ok_x, reason=reason_x, live_check_skipped=True)

    ok_a, reason_a = _validate_anthropic_syntactic(env_values.get("ANTHROPIC_API_KEY", ""))
    validations["anthropic"] = "ok" if ok_a else f"failed:{reason_a}"
    log.info("setup.validate.anthropic", ok=ok_a, reason=reason_a, live_check_skipped=True)

    # --- Summary ------------------------------------------------------
    echo_fn("\nsetup summary:")
    for source, status in validations.items():
        echo_fn(f"  {source:10s} {status}")
    echo_fn(f"\nwrote {env_path}")

    return SetupResult(
        reddit_username=new_username or None,
        github_token_set=bool(env_values.get("GITHUB_TOKEN")),
        x_token_set=bool(env_values.get("X_BEARER_TOKEN")),
        anthropic_key_set=bool(env_values.get("ANTHROPIC_API_KEY")),
        slack_webhooks_set=slack_keys_set,
        validations=validations,
    )
