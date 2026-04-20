"""Lightweight checks against deploy/deploy.sh.

Not full integration — that requires AWS credentials + a live instance.
These cover the parts bash can verify without SSM: syntax, help output,
and the tag-validation error paths (which live in the dry-run-reachable
prefix of the script).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "deploy" / "deploy.sh"


def test_script_exists_and_is_executable() -> None:
    assert SCRIPT.is_file(), f"missing: {SCRIPT}"
    assert os.access(SCRIPT, os.X_OK), "deploy.sh must be executable"


def test_bash_syntax_is_valid() -> None:
    result = subprocess.run(
        ["bash", "-n", str(SCRIPT)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"bash -n failed:\n{result.stderr}"


def test_help_flag_prints_usage() -> None:
    result = subprocess.run(
        ["bash", str(SCRIPT), "--help"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "Usage:" in result.stdout
    assert "--dry-run" in result.stdout


def test_unknown_flag_fails() -> None:
    result = subprocess.run(
        ["bash", str(SCRIPT), "--bogus"],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "unknown flag" in result.stderr


@pytest.mark.skipif(shutil.which("git") is None, reason="git not available")
def test_missing_tag_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Tag that doesn't exist should error in pre-SSM validation, not
    reach the AWS call path."""
    # Run in a throwaway repo so we don't depend on the local tag set.
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "f").write_text("x")
    env = os.environ.copy()
    env["GIT_AUTHOR_NAME"] = env.get("GIT_AUTHOR_NAME", "t")
    env["GIT_AUTHOR_EMAIL"] = env.get("GIT_AUTHOR_EMAIL", "t@t")
    env["GIT_COMMITTER_NAME"] = env.get("GIT_COMMITTER_NAME", "t")
    env["GIT_COMMITTER_EMAIL"] = env.get("GIT_COMMITTER_EMAIL", "t@t")
    subprocess.run(["git", "-C", str(tmp_path), "add", "f"], check=True, env=env)
    subprocess.run(
        ["git", "-C", str(tmp_path), "commit", "-q", "-m", "init", "--no-verify"],
        check=True,
        env=env,
    )
    result = subprocess.run(
        ["bash", str(SCRIPT), "v99.99.99-nope"],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        env=env,
    )
    assert result.returncode != 0
    # Either "not found locally" or "not on origin" — both are pre-SSM
    # paths. On a fresh repo with no origin, it'll be the former.
    combined = result.stdout + result.stderr
    assert ("not found locally" in combined) or ("not on origin" in combined)


def test_dry_run_prints_remote_command(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Dry-run with a valid local tag prints the remote script without
    invoking AWS. Uses a throwaway repo + tag so the test is
    hermetic."""
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "f").write_text("x")
    env = os.environ.copy()
    env["GIT_AUTHOR_NAME"] = "t"
    env["GIT_AUTHOR_EMAIL"] = "t@t"
    env["GIT_COMMITTER_NAME"] = "t"
    env["GIT_COMMITTER_EMAIL"] = "t@t"
    subprocess.run(["git", "-C", str(tmp_path), "add", "f"], check=True, env=env)
    subprocess.run(
        ["git", "-C", str(tmp_path), "commit", "-q", "-m", "c", "--no-verify"],
        check=True,
        env=env,
    )
    subprocess.run(["git", "-C", str(tmp_path), "tag", "v0.0.1"], check=True, env=env)

    # Provide an instance id so resolution short-circuits without
    # needing pulumi state.
    env["SOCIAL_SURVEYOR_INSTANCE_ID"] = "i-ffffffffffffffff"

    result = subprocess.run(
        ["bash", str(SCRIPT), "v0.0.1", "--dry-run"],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        env=env,
    )
    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
    assert "would execute on i-ffffffffffffffff" in result.stdout
    assert "git checkout --detach v0.0.1" in result.stdout
    assert "social-surveyor@opendata" in result.stdout


def test_dry_run_respects_project_override(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "f").write_text("x")
    env = os.environ.copy()
    env.update(
        GIT_AUTHOR_NAME="t",
        GIT_AUTHOR_EMAIL="t@t",
        GIT_COMMITTER_NAME="t",
        GIT_COMMITTER_EMAIL="t@t",
    )
    subprocess.run(["git", "-C", str(tmp_path), "add", "f"], check=True, env=env)
    subprocess.run(
        ["git", "-C", str(tmp_path), "commit", "-q", "-m", "c", "--no-verify"],
        check=True,
        env=env,
    )
    subprocess.run(["git", "-C", str(tmp_path), "tag", "v0.0.1"], check=True, env=env)
    env["SOCIAL_SURVEYOR_INSTANCE_ID"] = "i-aaa"

    result = subprocess.run(
        ["bash", str(SCRIPT), "v0.0.1", "--dry-run", "--project", "other"],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        env=env,
    )
    assert result.returncode == 0
    assert "social-surveyor@other" in result.stdout


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-v"]))
