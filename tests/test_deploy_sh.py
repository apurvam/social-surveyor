"""Lightweight checks against deploy/deploy.sh.

Not full integration — that requires AWS credentials + a live instance.
These cover the parts bash can verify without SSM: syntax, help output,
and the ref-resolution error paths (which live in the dry-run-reachable
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

BASE_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@t",
}


def _init_repo_with_commit(path: Path) -> str:
    """Create a git repo at ``path`` with one commit. Return the HEAD SHA."""
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
    sha = subprocess.check_output(
        ["git", "-C", str(path), "rev-parse", "HEAD"], env=env, text=True
    ).strip()
    return sha


def _env_with_base(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = os.environ.copy()
    env.update(BASE_ENV)
    if extra:
        env.update(extra)
    return env


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
    # Help should advertise the no-arg/branch/tag/sha entrypoints.
    assert "origin/main" in result.stdout
    assert "branch tip" in result.stdout


def test_unknown_flag_fails() -> None:
    result = subprocess.run(
        ["bash", str(SCRIPT), "--bogus"],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "unknown flag" in result.stderr


@pytest.mark.skipif(shutil.which("git") is None, reason="git not available")
def test_missing_ref_errors(tmp_path: Path) -> None:
    """Ref that doesn't resolve should error in pre-SSM validation,
    not reach the AWS call path."""
    _init_repo_with_commit(tmp_path)
    result = subprocess.run(
        ["bash", str(SCRIPT), "does-not-exist-xyz"],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        env=_env_with_base(),
    )
    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "not a tag, a branch on origin, or a known commit SHA" in combined


def test_dry_run_with_tag(tmp_path: Path) -> None:
    """Dry-run with a valid local tag prints the remote script using
    the tag's resolved SHA."""
    _init_repo_with_commit(tmp_path)
    env = _env_with_base({"SOCIAL_SURVEYOR_INSTANCE_ID": "i-ffffffffffffffff"})
    subprocess.run(["git", "-C", str(tmp_path), "tag", "v0.0.1"], check=True, env=env)
    sha = subprocess.check_output(
        ["git", "-C", str(tmp_path), "rev-parse", "v0.0.1^{commit}"], env=env, text=True
    ).strip()

    result = subprocess.run(
        ["bash", str(SCRIPT), "v0.0.1", "--dry-run"],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        env=env,
    )
    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
    assert "would execute on i-ffffffffffffffff" in result.stdout
    assert f"git checkout --detach {sha}" in result.stdout
    assert "ref:      v0.0.1 (tag)" in result.stdout
    assert "social-surveyor@opendata" in result.stdout


def test_dry_run_with_branch(tmp_path: Path) -> None:
    """Branch on origin should resolve to origin/<branch>'s SHA.

    We fake origin by configuring it to the same repo — git treats
    that as a valid remote and we can fetch back our own refs.
    """
    sha = _init_repo_with_commit(tmp_path)
    env = _env_with_base({"SOCIAL_SURVEYOR_INSTANCE_ID": "i-ffffffffffffffff"})
    # Create a bare clone to act as origin.
    bare = tmp_path.parent / (tmp_path.name + "-origin.git")
    subprocess.run(["git", "clone", "--bare", "-q", str(tmp_path), str(bare)], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "remote", "add", "origin", str(bare)],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(tmp_path), "fetch", "--quiet", "origin"],
        check=True,
        env=env,
    )
    # Determine the default branch name the clone used (git init may
    # produce 'master' or 'main' depending on version/config).
    head_ref = subprocess.check_output(
        ["git", "-C", str(tmp_path), "symbolic-ref", "HEAD"], env=env, text=True
    ).strip()
    branch = head_ref.rsplit("/", 1)[-1]

    result = subprocess.run(
        ["bash", str(SCRIPT), branch, "--dry-run"],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        env=env,
    )
    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
    assert f"ref:      {branch} (branch)" in result.stdout
    assert f"git checkout --detach {sha}" in result.stdout


def test_dry_run_with_commit_sha(tmp_path: Path) -> None:
    sha = _init_repo_with_commit(tmp_path)
    env = _env_with_base({"SOCIAL_SURVEYOR_INSTANCE_ID": "i-aaa"})

    result = subprocess.run(
        ["bash", str(SCRIPT), sha, "--dry-run"],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        env=env,
    )
    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
    assert "(commit)" in result.stdout
    assert f"git checkout --detach {sha}" in result.stdout


def test_dry_run_default_resolves_main(tmp_path: Path) -> None:
    """No argument → script picks origin/main. Prove that on a repo
    whose default branch is main; skip cleanly when the init default is
    different (some envs produce master)."""
    _init_repo_with_commit(tmp_path)
    env = _env_with_base({"SOCIAL_SURVEYOR_INSTANCE_ID": "i-bbb"})
    # Ensure branch is named main; harmless if already.
    subprocess.run(["git", "-C", str(tmp_path), "branch", "-M", "main"], check=True, env=env)
    bare = tmp_path.parent / (tmp_path.name + "-origin.git")
    subprocess.run(["git", "clone", "--bare", "-q", str(tmp_path), str(bare)], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "remote", "add", "origin", str(bare)],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(tmp_path), "fetch", "--quiet", "origin"],
        check=True,
        env=env,
    )

    result = subprocess.run(
        ["bash", str(SCRIPT), "--dry-run"],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        env=env,
    )
    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
    assert "ref:      main (branch)" in result.stdout


def test_dry_run_respects_project_override(tmp_path: Path) -> None:
    _init_repo_with_commit(tmp_path)
    env = _env_with_base({"SOCIAL_SURVEYOR_INSTANCE_ID": "i-aaa"})
    subprocess.run(["git", "-C", str(tmp_path), "tag", "v0.0.1"], check=True, env=env)

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
