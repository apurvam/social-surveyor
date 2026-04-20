"""Tests for ``resolve_secret`` env-first + SSM fallback.

Coverage targets:

- Env hit — never touches SSM regardless of prefix config.
- SSM hit — only attempted when ``SOCIAL_SURVEYOR_SSM_PREFIX`` is set
  AND the env lookup missed.
- Credential error — logs and raises ``SecretNotFoundError``;
  classify/route jobs don't crash the whole process because of it.
- Parameter-not-found — raises ``SecretNotFoundError`` (the secret
  genuinely isn't anywhere).
- Cache — second call hits memo, not boto3.
- No prefix configured — no SSM call at all.
"""

from __future__ import annotations

from typing import Any

import pytest
from botocore.exceptions import NoCredentialsError

from social_surveyor import secrets as secrets_mod
from social_surveyor.secrets import (
    SSM_PREFIX_ENV,
    SecretNotFoundError,
    clear_secret_cache,
    resolve_secret,
)


class _FakeSSMClient:
    """Narrow stand-in for the boto3 ssm client. Records calls + raises
    programmable exceptions."""

    def __init__(self, *, responses: dict[str, Any] | None = None) -> None:
        self.responses = responses or {}
        self.calls: list[dict[str, Any]] = []

    def get_parameter(self, *, Name: str, WithDecryption: bool) -> Any:  # noqa: N803
        self.calls.append({"Name": Name, "WithDecryption": WithDecryption})
        if Name not in self.responses:
            # Mirror boto's ClientError for ParameterNotFound.
            from botocore.exceptions import ClientError

            raise ClientError(
                {
                    "Error": {"Code": "ParameterNotFound", "Message": "Parameter not found."},
                    "ResponseMetadata": {},
                },
                "GetParameter",
            )
        resp = self.responses[Name]
        if isinstance(resp, Exception):
            raise resp
        return {"Parameter": {"Name": Name, "Value": resp}}


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start every test with a clean cache + no SSM client singleton so
    monkeypatched factories take effect immediately."""
    clear_secret_cache()
    secrets_mod._reset_ssm_client_for_tests()
    monkeypatch.delenv(SSM_PREFIX_ENV, raising=False)


def _install_fake_ssm(
    monkeypatch: pytest.MonkeyPatch,
    client: _FakeSSMClient,
) -> _FakeSSMClient:
    monkeypatch.setattr(secrets_mod, "_make_ssm_client", lambda: client)
    return client


# --- env path --------------------------------------------------------------


def test_env_hit_short_circuits_ssm(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FOO", "bar")
    monkeypatch.setenv(SSM_PREFIX_ENV, "/social-surveyor/opendata")
    fake = _install_fake_ssm(monkeypatch, _FakeSSMClient(responses={"ignored": "x"}))
    assert resolve_secret("FOO") == "bar"
    assert fake.calls == []  # env hit means zero SSM calls


def test_empty_env_value_treated_as_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty string in env (e.g. `FOO=` in .env) shouldn't count as a hit
    — that's a common bug where a secret was set to blank and the caller
    gets a silent empty webhook URL instead of a clear error."""
    monkeypatch.setenv("FOO", "")
    # No SSM configured either → error path.
    with pytest.raises(SecretNotFoundError):
        resolve_secret("FOO")


def test_no_prefix_configured_no_ssm_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without the prefix env var, the resolver must not try SSM at all
    — local dev without AWS creds shouldn't pay any latency."""
    fake = _install_fake_ssm(monkeypatch, _FakeSSMClient())
    with pytest.raises(SecretNotFoundError, match=SSM_PREFIX_ENV):
        resolve_secret("MISSING")
    assert fake.calls == []


# --- SSM path --------------------------------------------------------------


def test_ssm_hit_when_env_misses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SSM_PREFIX_ENV, "/social-surveyor/opendata")
    fake = _install_fake_ssm(
        monkeypatch,
        _FakeSSMClient(
            responses={"/social-surveyor/opendata/WEBHOOK_X": "https://hooks.slack.test/INFRA"}
        ),
    )
    assert resolve_secret("WEBHOOK_X") == "https://hooks.slack.test/INFRA"
    assert len(fake.calls) == 1
    assert fake.calls[0]["Name"] == "/social-surveyor/opendata/WEBHOOK_X"
    assert fake.calls[0]["WithDecryption"] is True


def test_ssm_prefix_without_leading_slash_is_normalized(monkeypatch: pytest.MonkeyPatch) -> None:
    """Accept both `/social-surveyor/opendata` and `social-surveyor/opendata`."""
    monkeypatch.setenv(SSM_PREFIX_ENV, "social-surveyor/opendata")
    fake = _install_fake_ssm(
        monkeypatch,
        _FakeSSMClient(responses={"/social-surveyor/opendata/K": "v"}),
    )
    assert resolve_secret("K") == "v"
    assert fake.calls[0]["Name"] == "/social-surveyor/opendata/K"


def test_ssm_caches_second_call(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SSM_PREFIX_ENV, "/p/demo")
    fake = _install_fake_ssm(
        monkeypatch,
        _FakeSSMClient(responses={"/p/demo/K": "v"}),
    )
    assert resolve_secret("K") == "v"
    assert resolve_secret("K") == "v"
    assert len(fake.calls) == 1  # second call served from cache


def test_clear_cache_forces_refetch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SSM_PREFIX_ENV, "/p/demo")
    fake = _install_fake_ssm(
        monkeypatch,
        _FakeSSMClient(responses={"/p/demo/K": "v"}),
    )
    resolve_secret("K")
    clear_secret_cache()
    resolve_secret("K")
    assert len(fake.calls) == 2


# --- failure paths ---------------------------------------------------------


def test_ssm_parameter_not_found_raises_secret_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(SSM_PREFIX_ENV, "/p/demo")
    _install_fake_ssm(monkeypatch, _FakeSSMClient(responses={}))
    with pytest.raises(SecretNotFoundError, match="not in env and not found in SSM"):
        resolve_secret("MISSING")


def test_ssm_credentials_missing_raises_secret_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(SSM_PREFIX_ENV, "/p/demo")
    _install_fake_ssm(
        monkeypatch,
        _FakeSSMClient(responses={"/p/demo/K": NoCredentialsError()}),
    )
    with pytest.raises(SecretNotFoundError, match="SSM fallback unavailable"):
        resolve_secret("K")


def test_ssm_access_denied_maps_to_secret_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    """Access-denied is a deployment misconfiguration — IAM role drift,
    KMS key revoked, etc. Surface to the caller, don't swallow silently."""
    from botocore.exceptions import ClientError

    err = ClientError(
        {"Error": {"Code": "AccessDeniedException", "Message": "denied"}},
        "GetParameter",
    )
    monkeypatch.setenv(SSM_PREFIX_ENV, "/p/demo")
    _install_fake_ssm(monkeypatch, _FakeSSMClient(responses={"/p/demo/K": err}))
    with pytest.raises(SecretNotFoundError):
        resolve_secret("K")


def test_ssm_returns_empty_value_treated_as_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SSM_PREFIX_ENV, "/p/demo")
    _install_fake_ssm(monkeypatch, _FakeSSMClient(responses={"/p/demo/K": ""}))
    with pytest.raises(SecretNotFoundError):
        resolve_secret("K")
