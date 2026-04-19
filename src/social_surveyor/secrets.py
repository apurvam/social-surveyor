"""Resolve secret *names* (from config YAML) to values.

Session 4: env-var lookup only — ``.env`` is loaded by the CLI entry
point via python-dotenv before anything resolves.

Session 5 adds an AWS SSM Parameter Store fallback for production.
Signature stays the same; only the resolver changes.
"""

from __future__ import annotations

import os


class SecretNotFoundError(RuntimeError):
    """Raised when a named secret can't be resolved anywhere."""


def resolve_secret(name: str) -> str:
    """Return the value for ``name`` or raise :class:`SecretNotFoundError`.

    Names are env-var-style (``OPENDATA_SLACK_WEBHOOK_IMMEDIATE``).
    The cheap guardrail of failing loud when the env var is missing
    catches the most common setup bug: "I copied the routing.yaml
    but forgot to put the webhook URL in .env."
    """
    value = os.environ.get(name)
    if value is None or value == "":
        raise SecretNotFoundError(
            f"secret {name!r} not found in environment "
            "(did you add it to .env? SSM fallback lands in Session 5)"
        )
    return value


__all__ = ["SecretNotFoundError", "resolve_secret"]
