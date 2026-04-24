"""Lightweight checks against deploy/label-prod.sh.

Covers the parts bash can verify without hitting SSM/S3/git remotes:
syntax, help output, preflight errors, and the dry-run summary.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "deploy" / "label-prod.sh"

BASE_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@t",
}


def _init_repo(path: Path) -> None:
    env = os.environ.copy()
    env.update(BASE_ENV)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    (path / "f").write_text("x")
    subprocess.run(["git", "-C", str(path), "add", "f"], check=True, env=env)
    subprocess.run(
        ["git", "-C", str(path), "commit", "-q", "-m", "c", "--no-verify"],
        check=True,
        env=env,
    )


def _env(extra: dict[str, str] | None = None) -> dict[str, str]:
    e = os.environ.copy()
    e.update(BASE_ENV)
    if extra:
        e.update(extra)
    return e


def test_script_exists_and_is_executable() -> None:
    assert SCRIPT.is_file(), f"missing: {SCRIPT}"
    assert os.access(SCRIPT, os.X_OK), "label-prod.sh must be executable"


def test_bash_syntax_is_valid() -> None:
    result = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True)
    assert result.returncode == 0, f"bash -n failed:\n{result.stderr}"


def test_help_flag_prints_usage() -> None:
    result = subprocess.run(["bash", str(SCRIPT), "--help"], capture_output=True, text=True)
    assert result.returncode == 0
    assert "Usage:" in result.stdout
    assert "--project <name>" in result.stdout
    assert "--dry-run" in result.stdout


def test_missing_project_errors() -> None:
    result = subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True)
    assert result.returncode != 0
    assert "--project is required" in result.stderr


def test_unknown_flag_fails() -> None:
    result = subprocess.run(["bash", str(SCRIPT), "--bogus"], capture_output=True, text=True)
    assert result.returncode != 0
    assert "unknown flag" in result.stderr


def test_unknown_project_errors(tmp_path: Path) -> None:
    """Project dir must exist under projects/ — otherwise bail before
    touching SSM or S3."""
    _init_repo(tmp_path)
    result = subprocess.run(
        ["bash", str(SCRIPT), "--project", "does-not-exist", "--dry-run"],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        env=_env({"SOCIAL_SURVEYOR_INSTANCE_ID": "i-aaa"}),
    )
    assert result.returncode != 0
    assert "project dir not found" in result.stderr


def test_dry_run_prints_plan(tmp_path: Path) -> None:
    """Dry-run lists every step the script would execute — so the
    operator can eyeball what's about to happen before the real run
    touches SSM, S3, or git."""
    _init_repo(tmp_path)
    (tmp_path / "projects" / "opendata-brand").mkdir(parents=True)

    result = subprocess.run(
        ["bash", str(SCRIPT), "--project", "opendata-brand", "--dry-run"],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        env=_env({"SOCIAL_SURVEYOR_INSTANCE_ID": "i-ffffffffffffffff"}),
    )
    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
    out = result.stdout
    # Target summary
    assert "project:   opendata-brand" in out
    assert "instance:  i-ffffffffffffffff" in out
    assert "remote db: /var/lib/social-surveyor/opendata-brand/opendata-brand.db" in out
    # Step list — presence, not exact wording, so small phrasing edits
    # don't break the test.
    assert "ensure S3 staging bucket" in out
    assert "presigned PUT URL" in out
    assert "SSM send-command" in out
    assert "aws s3 cp" in out
    assert "aws s3 rm" in out
    assert "uv run social-surveyor label --project opendata-brand" in out
    # Fixed branch, no timestamp — sessions accumulate on one rolling PR.
    assert "labels/opendata-brand (fixed" in out
    assert "labels/opendata-brand-2" not in out  # catches a stray old-stamp regression
    assert "checkout labels/opendata-brand" in out
    assert "commit on labels/opendata-brand" in out


def test_dry_run_respects_bucket_override(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    (tmp_path / "projects" / "opendata-brand").mkdir(parents=True)

    result = subprocess.run(
        [
            "bash",
            str(SCRIPT),
            "--project",
            "opendata-brand",
            "--bucket",
            "my-scratch-bucket",
            "--dry-run",
        ],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        env=_env({"SOCIAL_SURVEYOR_INSTANCE_ID": "i-aaa"}),
    )
    assert result.returncode == 0
    assert "s3://my-scratch-bucket/" in result.stdout


def test_dry_run_rejects_dirty_tree(tmp_path: Path) -> None:
    """Default rejects an uncommitted change — the script is about to
    make one of its own, and we don't want to stir two into the same
    branch."""
    _init_repo(tmp_path)
    (tmp_path / "projects" / "opendata-brand").mkdir(parents=True)
    (tmp_path / "dirty").write_text("dirt")  # untracked file

    result = subprocess.run(
        ["bash", str(SCRIPT), "--project", "opendata-brand", "--dry-run"],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        env=_env({"SOCIAL_SURVEYOR_INSTANCE_ID": "i-aaa"}),
    )
    assert result.returncode != 0
    assert "working tree is dirty" in result.stderr


def test_dry_run_dirty_flag_allows_dirty_tree(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    (tmp_path / "projects" / "opendata-brand").mkdir(parents=True)
    (tmp_path / "dirty").write_text("dirt")

    result = subprocess.run(
        [
            "bash",
            str(SCRIPT),
            "--project",
            "opendata-brand",
            "--dirty",
            "--dry-run",
        ],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        env=_env({"SOCIAL_SURVEYOR_INSTANCE_ID": "i-aaa"}),
    )
    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
    assert "opendata-brand" in result.stdout


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-v"]))
