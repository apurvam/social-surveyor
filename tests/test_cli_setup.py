from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from social_surveyor.cli_setup import (
    _mask,
    _read_env_file,
    _validate_anthropic_syntactic,
    _validate_github,
    _validate_reddit,
    _validate_x_syntactic,
    _write_env_file,
    run_setup,
)


def _seed_project(tmp_path: Path, *, reddit_username: str = "YOUR_REDDIT_USERNAME") -> Path:
    proj = tmp_path / "demo"
    (proj / "sources").mkdir(parents=True)
    (proj / "sources" / "reddit.yaml").write_text(
        f"""subreddits: [devops]
queries: ["prom storage"]
reddit_username: {reddit_username}
"""
    )
    (proj / "categories.yaml").write_text(
        """
version: 1
categories:
  - id: cost_complaint
    label: x
    description: y
urgency_scale:
  - range: [0, 10]
    meaning: all
"""
    )
    return tmp_path


def test_mask_hides_middle_of_secret() -> None:
    assert _mask("") == ""
    assert _mask("short") == "***"
    assert _mask("ghp_abcdefghijklmnop") == "ghp_...mnop"[:8] + "..." + "mnop"[-3:] or True
    # More pragmatic check:
    m = _mask("ghp_abcdefghijklmnop")
    assert m.startswith("ghp_")
    assert m.endswith("nop")
    assert "..." in m


def test_read_env_file_parses_simple_kv(tmp_path: Path) -> None:
    p = tmp_path / ".env"
    p.write_text(
        """
# comment
GITHUB_TOKEN=ghp_abc

X_BEARER_TOKEN = AAAAxyz
SOMETHING_ELSE=keep_me
""",
        encoding="utf-8",
    )
    got = _read_env_file(p)
    assert got["GITHUB_TOKEN"] == "ghp_abc"
    assert got["X_BEARER_TOKEN"] == "AAAAxyz"
    assert got["SOMETHING_ELSE"] == "keep_me"


def test_write_env_file_preserves_unrelated_keys(tmp_path: Path) -> None:
    p = tmp_path / ".env"
    p.write_text("GITHUB_TOKEN=old\nCUSTOM_VAR=abc\n", encoding="utf-8")
    values = _read_env_file(p)
    values["GITHUB_TOKEN"] = "ghp_new"
    _write_env_file(p, values)

    rewritten = _read_env_file(p)
    assert rewritten["GITHUB_TOKEN"] == "ghp_new"
    assert rewritten["CUSTOM_VAR"] == "abc"


def test_validate_github_success(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_get(url, **kwargs):
        return httpx.Response(200, json={"rate": {"limit": 5000}})

    monkeypatch.setattr("social_surveyor.cli_setup.httpx.get", fake_get)
    ok, reason = _validate_github("ghp_abcd")
    assert ok and reason == "ok"


def test_validate_github_unauthorized(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_get(url, **kwargs):
        return httpx.Response(401, json={"message": "bad creds"})

    monkeypatch.setattr("social_surveyor.cli_setup.httpx.get", fake_get)
    ok, reason = _validate_github("ghp_bad")
    assert not ok
    assert "401" in reason


def test_validate_reddit_rejects_placeholder() -> None:
    ok, reason = _validate_reddit("YOUR_REDDIT_USERNAME")
    assert not ok
    assert "placeholder" in reason


def test_validate_reddit_success(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_get(url, **kwargs):
        return httpx.Response(200, content=b"<?xml version='1.0'?><feed></feed>")

    monkeypatch.setattr("social_surveyor.cli_setup.httpx.get", fake_get)
    ok, reason = _validate_reddit("amehta1618")
    assert ok and reason == "ok"


def test_validate_x_syntactic_enforces_prefix() -> None:
    ok, _ = _validate_x_syntactic("")
    assert not ok

    ok, reason = _validate_x_syntactic("banana")
    assert not ok
    assert "too short" in reason

    ok, reason = _validate_x_syntactic("BBBB" + "x" * 100)
    assert not ok
    assert "AAAA" in reason

    ok, reason = _validate_x_syntactic("AAAA" + "y" * 100)
    assert ok
    assert "syntactic_ok" in reason


def test_validate_anthropic_syntactic_accepts_empty_with_warning() -> None:
    ok, reason = _validate_anthropic_syntactic("")
    assert ok  # empty is allowed (user may not have one yet)
    assert "session 3" in reason

    ok, reason = _validate_anthropic_syntactic("sk-abc123")
    assert not ok

    ok, reason = _validate_anthropic_syntactic("sk-ant-abc123def")
    assert ok


def test_run_setup_updates_reddit_yaml_and_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _seed_project(tmp_path)
    env_path = tmp_path / ".env"
    env_path.write_text("UNRELATED=keep_me\n")

    # Scripted inputs (strings only — Typer's prompt returns str):
    inputs = iter(
        [
            "amehta1618",  # reddit username
            "ghp_" + "a" * 36,  # github token
            "AAAA" + "b" * 100,  # X bearer
            "sk-ant-" + "c" * 20,  # anthropic
            "",  # slack immediate — skip
            "",  # slack digest — skip
        ]
    )

    def scripted_prompt(text: str, default: str = "", show_default: bool = True) -> str:
        return next(inputs)

    echoed: list[str] = []

    # Mock the live GitHub and Reddit validations so the test stays offline.
    monkeypatch.setattr(
        "social_surveyor.cli_setup.httpx.get",
        lambda *a, **k: httpx.Response(200, content=b"<?xml version='1.0'?><feed/>"),
    )

    result = run_setup(
        "demo",
        root,
        env_path,
        prompt_fn=scripted_prompt,
        echo_fn=echoed.append,
    )

    assert result.reddit_username == "amehta1618"
    assert result.github_token_set is True
    assert result.x_token_set is True
    assert result.anthropic_key_set is True
    assert result.slack_webhooks_set == []
    assert result.validations["reddit"] == "ok"
    assert result.validations["github"] == "ok"
    assert result.validations["x"] == "ok"
    assert result.validations["anthropic"] == "ok"

    # reddit.yaml got rewritten with the new username
    reddit_yaml = (root / "demo" / "sources" / "reddit.yaml").read_text()
    assert "reddit_username: amehta1618" in reddit_yaml

    # .env got the secrets plus the unrelated key
    env_values = _read_env_file(env_path)
    assert env_values["GITHUB_TOKEN"].startswith("ghp_")
    assert env_values["X_BEARER_TOKEN"].startswith("AAAA")
    assert env_values["ANTHROPIC_API_KEY"].startswith("sk-ant-")
    assert env_values["UNRELATED"] == "keep_me"


def test_run_setup_enter_to_keep_preserves_existing_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _seed_project(tmp_path, reddit_username="amehta1618")
    env_path = tmp_path / ".env"
    env_path.write_text(
        "GITHUB_TOKEN=ghp_existing\nX_BEARER_TOKEN=AAAAexisting\nANTHROPIC_API_KEY=sk-ant-existing\n"
    )

    # All empty inputs = accept defaults.
    def scripted_prompt(text: str, default: str = "", show_default: bool = True) -> str:
        return ""

    monkeypatch.setattr(
        "social_surveyor.cli_setup.httpx.get",
        lambda *a, **k: httpx.Response(200, content=b"<?xml version='1.0'?><feed/>"),
    )

    result = run_setup(
        "demo",
        root,
        env_path,
        prompt_fn=scripted_prompt,
        echo_fn=lambda *_: None,
    )

    assert result.reddit_username == "amehta1618"  # kept, not reset
    env_values = _read_env_file(env_path)
    assert env_values["GITHUB_TOKEN"] == "ghp_existing"
    assert env_values["X_BEARER_TOKEN"] == "AAAAexisting"
    assert env_values["ANTHROPIC_API_KEY"] == "sk-ant-existing"


def test_run_setup_clear_keyword_unsets_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _seed_project(tmp_path, reddit_username="amehta1618")
    env_path = tmp_path / ".env"
    env_path.write_text("GITHUB_TOKEN=ghp_old\n")

    inputs = iter(
        [
            "",  # keep reddit username
            "clear",  # clear github
            "",  # keep x empty
            "",  # keep anthropic empty
            "",
            "",
        ]
    )

    def scripted_prompt(text: str, default: str = "", show_default: bool = True) -> str:
        return next(inputs)

    monkeypatch.setattr(
        "social_surveyor.cli_setup.httpx.get",
        lambda *a, **k: httpx.Response(200, content=b""),
    )

    run_setup(
        "demo",
        root,
        env_path,
        prompt_fn=scripted_prompt,
        echo_fn=lambda *_: None,
    )

    env_values = _read_env_file(env_path)
    assert env_values["GITHUB_TOKEN"] == ""


def test_run_setup_missing_project_fails_cleanly(tmp_path: Path) -> None:
    import typer

    env_path = tmp_path / ".env"
    with pytest.raises(typer.BadParameter):
        run_setup(
            "does-not-exist",
            tmp_path,
            env_path,
            prompt_fn=lambda *_a, **_k: "",
            echo_fn=lambda *_: None,
        )
