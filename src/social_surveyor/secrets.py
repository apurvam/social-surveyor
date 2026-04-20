"""Resolve secret *names* (from config YAML) to values.

Resolution order, per the Session 1 design note
("secret-reference pattern, day one, never revisited"):

1. ``os.environ[name]`` — the path local dev uses and what the
   production systemd unit sources via ``EnvironmentFile``. Stays the
   fast path so offline development doesn't need AWS credentials.
2. AWS SSM Parameter Store under
   ``$SOCIAL_SURVEYOR_SSM_PREFIX/<name>`` as a SecureString. Only
   attempted when the prefix env var is set (production) *and* the
   env lookup missed. The instance IAM role scopes the
   ``GetParameter`` permission to exactly this prefix.

The SSM fallback is additive: call sites keep the same
``resolve_secret(name)`` signature, and the environment variable path
continues to work unchanged. ``SOCIAL_SURVEYOR_SSM_PREFIX`` is how the
tool knows SSM is *available* — if it's unset, the resolver never
talks to AWS.

Caching: we cache by ``(prefix, name)`` for the process lifetime.
Secrets don't change mid-run; re-resolving on every webhook post would
add ~50ms per call against our tier. Call
:func:`clear_secret_cache` from long-lived tests that want a clean slate.
"""

from __future__ import annotations

import os
import threading
from typing import Any

import structlog

log = structlog.get_logger(__name__)

SSM_PREFIX_ENV = "SOCIAL_SURVEYOR_SSM_PREFIX"


class SecretNotFoundError(RuntimeError):
    """Raised when a named secret can't be resolved anywhere."""


# --- cache ------------------------------------------------------------------
# Per-process cache keyed on (prefix, name). Thread-safe because the
# scheduler runs jobs across several APScheduler threads and a transient
# cache miss under a race would produce duplicate SSM calls rather than
# incorrect values; the lock makes the cache deterministic anyway.

_cache_lock = threading.Lock()
_cache: dict[tuple[str, str], str] = {}


def clear_secret_cache() -> None:
    """Drop every cached resolution. Tests use this to simulate a
    fresh process when exercising env-then-SSM transitions."""
    with _cache_lock:
        _cache.clear()


# --- public API -------------------------------------------------------------


def resolve_secret(name: str) -> str:
    """Return the value for ``name`` or raise :class:`SecretNotFoundError`.

    Env wins when present — keeps the hot path local. SSM is the
    production fallback for names the systemd ``EnvironmentFile``
    didn't pre-load (historical ordering: the bootstrap
    ``social-surveyor-load-env`` pulls SSM into a file; once
    ``resolve_secret`` supports SSM directly, that helper is
    redundant but harmless).
    """
    value = os.environ.get(name)
    if value:
        return value

    prefix = os.environ.get(SSM_PREFIX_ENV, "").strip()
    if not prefix:
        raise SecretNotFoundError(
            f"secret {name!r} not found in environment; "
            f"set {name} in .env (local) or define {SSM_PREFIX_ENV} to enable SSM fallback"
        )

    cache_key = (prefix, name)
    with _cache_lock:
        cached = _cache.get(cache_key)
    if cached is not None:
        return cached

    try:
        fetched = _fetch_from_ssm(prefix, name)
    except _SSMCredentialError as e:
        # Prefix is configured but AWS creds aren't available. Treat
        # this as "fallback isn't wired"; don't crash the classify /
        # route job because of it. Surface in logs so the operator can
        # trace the reason.
        log.warning(
            "secrets.ssm_credentials_unavailable",
            name=name,
            prefix=prefix,
            error=str(e),
        )
        raise SecretNotFoundError(
            f"secret {name!r} not in env and SSM fallback unavailable: {e}"
        ) from e
    except _SSMParameterNotFoundError as e:
        raise SecretNotFoundError(
            f"secret {name!r} not in env and not found in SSM at {prefix}/{name}"
        ) from e

    with _cache_lock:
        _cache[cache_key] = fetched
    return fetched


# --- internal SSM helpers ---------------------------------------------------
# Wrapping boto3's exception types in module-local ones keeps the
# public API (SecretNotFoundError only) stable and makes the test
# doubles easy to write.


class _SSMCredentialError(RuntimeError):
    """Raised when AWS credentials are unavailable."""


class _SSMParameterNotFoundError(RuntimeError):
    """Raised when the SSM parameter doesn't exist at the path."""


def _fetch_from_ssm(prefix: str, name: str) -> str:
    """Fetch ``/<prefix>/<name>`` from SSM Parameter Store with decryption.

    ``prefix`` is the bare prefix stored in the env var; we accept
    both ``/social-surveyor/opendata`` and
    ``social-surveyor/opendata`` and normalize to the former. SSM
    parameter names must start with ``/``.
    """
    client = _make_ssm_client()
    normalized = prefix if prefix.startswith("/") else f"/{prefix}"
    path = f"{normalized.rstrip('/')}/{name}"

    # Imported inline so boto3 isn't loaded when SSM is never used.
    from botocore.exceptions import ClientError, NoCredentialsError, PartialCredentialsError

    try:
        resp = client.get_parameter(Name=path, WithDecryption=True)
    except (NoCredentialsError, PartialCredentialsError) as e:
        raise _SSMCredentialError(str(e)) from e
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code in {"ParameterNotFound", "ParameterVersionNotFound"}:
            raise _SSMParameterNotFoundError(path) from e
        # Anything else — access denied, throttling, KMS failure —
        # surfaces as credential-unavailable from the caller's point
        # of view, which triggers the "fallback not working" log and
        # a SecretNotFoundError at the caller boundary.
        raise _SSMCredentialError(f"{code or 'ClientError'}: {e}") from e

    parameter = resp.get("Parameter") or {}
    value = parameter.get("Value")
    if not isinstance(value, str) or value == "":
        raise _SSMParameterNotFoundError(path)
    return value


def _make_ssm_client() -> Any:
    """Build and cache a boto3 SSM client.

    Isolated behind a helper so tests can monkeypatch it with a double.
    Region is read from boto3's default resolution order (env /
    ~/.aws/config / instance metadata), which matches how the rest of
    the deploy config sources region.
    """
    global _ssm_client_singleton
    with _cache_lock:
        if _ssm_client_singleton is None:
            import boto3  # imported lazily so local dev doesn't pay the load cost

            _ssm_client_singleton = boto3.client("ssm")
        return _ssm_client_singleton


_ssm_client_singleton: Any = None


def _reset_ssm_client_for_tests() -> None:
    """Drop the cached SSM client — tests use this after monkeypatching
    :func:`_make_ssm_client`."""
    global _ssm_client_singleton
    with _cache_lock:
        _ssm_client_singleton = None


__all__ = [
    "SSM_PREFIX_ENV",
    "SecretNotFoundError",
    "clear_secret_cache",
    "resolve_secret",
]
