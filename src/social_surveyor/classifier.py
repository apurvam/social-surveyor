from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import structlog
from anthropic import (
    Anthropic,
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
)

from .config import CategoryConfig, ClassifierConfig, ProjectConfig
from .storage import Storage

log = structlog.get_logger("classifier")

# Hard ceiling on the body section of the user prompt. Anything past this
# is almost always boilerplate / signatures / quoted prior messages and
# adds tokens without adding signal. Tunable per source in later
# iteration.
BODY_CHAR_LIMIT = 2000


class ClassificationError(Exception):
    """Raised when an item could not be classified and should be flagged
    for manual review.

    The message is intended for logs — callers surface the ``item_id``
    separately.
    """


@dataclass(frozen=True)
class ClassifierInput:
    """The subset of an :class:`~social_surveyor.types.RawItem` the
    classifier needs, normalized for prompt assembly.

    Kept deliberately dict-free at the field level so the prompt
    assembly function has a stable, typed surface to test against.
    Construct via :meth:`from_row` when working with storage output.
    """

    item_id: str
    source: str
    author: str | None
    title: str
    body: str | None
    raw_json: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> ClassifierInput:
        return cls(
            item_id=f"{row['source']}:{row['platform_id']}",
            source=row["source"],
            author=row.get("author"),
            title=row.get("title") or "",
            body=row.get("body"),
            raw_json=row.get("raw_json") or {},
        )


@dataclass(frozen=True)
class Classification:
    item_id: str
    category: str
    urgency: int
    reasoning: str
    prompt_version: str
    model: str
    input_tokens: int
    output_tokens: int
    classified_at: datetime
    raw_response: dict[str, Any]


# --- prompt assembly (pure) ------------------------------------------------


def build_prompt(
    item: ClassifierInput,
    classifier_config: ClassifierConfig,
    categories: CategoryConfig,
) -> dict[str, Any]:
    """Return the kwargs for ``Anthropic.messages.create`` minus
    model/max_tokens/temperature.

    This function is pure: given the same inputs it produces the same
    output, and it performs no IO. The eval harness relies on that to
    A/B-test prompts by calling it directly in tests and diffing.

    The assistant turn is prefilled with ``{`` so the model is forced
    to continue valid JSON; callers prepend ``{`` to the response text
    before parsing.
    """
    system = _build_system_prompt(classifier_config, categories)
    user = _build_user_message(item)
    return {
        "system": system,
        "messages": [
            {"role": "user", "content": user},
            {"role": "assistant", "content": "{"},
        ],
    }


def _build_system_prompt(
    classifier_config: ClassifierConfig,
    categories: CategoryConfig,
) -> str:
    lines: list[str] = [
        "You are classifying social media posts for a technical product monitoring tool.",
        "",
        "The user's context:",
        classifier_config.icp_description.strip(),
        "",
        "Classify each post into exactly one of these categories:",
    ]
    for cat in categories.categories:
        lines.append(f"- {cat.id}: {cat.label} — {cat.description.strip()}")
    lines.extend(["", "Also assign an urgency score from 0 to 10:"])
    for band in categories.urgency_scale:
        lo, hi = band.range[0], band.range[1]
        lines.append(f"- {lo}-{hi}: {band.meaning}")

    if classifier_config.additional_instructions.strip():
        lines.append("")
        lines.append(classifier_config.additional_instructions.strip())

    if classifier_config.few_shot_examples:
        lines.extend(["", "Examples:"])
        for ex in classifier_config.few_shot_examples:
            lines.extend(
                [
                    "",
                    f"Title: {ex.title}",
                    f"Body: {ex.body}",
                    (
                        '{"category": "'
                        f"{ex.expected_category}"
                        '", "urgency": '
                        f"{ex.expected_urgency}"
                        ', "reasoning": "'
                        f"{ex.note or ex.expected_category}"
                        '"}'
                    ),
                ]
            )

    lines.extend(
        [
            "",
            "Respond with only a JSON object with exactly these fields:",
            '{"category": "<category_id>", "urgency": <0-10 integer>, '
            '"reasoning": "<one short sentence>"}',
            "Use one of the category ids listed above; do not invent new ones.",
        ]
    )
    return "\n".join(lines)


def _build_user_message(item: ClassifierInput) -> str:
    body = item.body or ""
    if len(body) > BODY_CHAR_LIMIT:
        body = body[:BODY_CHAR_LIMIT]

    # Source-specific framing. HN comments have a synthesized title
    # ("Comment by X on HN #Y") that carries zero classification signal;
    # presenting it as a normal title teaches Haiku that the header is
    # meaningful, and the model hedges. The branch below swaps in the
    # parent thread title (which IS signal-rich) and labels the shape
    # explicitly.
    #
    # Other sources (reddit posts, x tweets, github issues) use the
    # generic shape. GitHub comments have an analogous title-weak
    # problem but aren't in v3's scope.
    if _is_hn_comment(item):
        return _build_hn_comment_message(item, body)

    parts = [
        f"Source: {item.source}",
        f"Author: {item.author or '(unknown)'}",
        f"Title: {item.title}",
        "Body:",
        body,
    ]
    return "\n".join(parts)


def _is_hn_comment(item: ClassifierInput) -> bool:
    """True if this HN item is a comment rather than a story.

    The Algolia HN source stashes a ``_tags`` list in ``raw_json`` with
    entries like ``['comment', 'author_X', 'story_Y']`` (or
    ``['story', ...]`` for posts). Detection is just membership.
    """
    if item.source != "hackernews":
        return False
    tags = item.raw_json.get("_tags") or []
    return "comment" in tags


def _build_hn_comment_message(item: ClassifierInput, body: str) -> str:
    """HN comment shape: parent thread title as context, synthesized
    title dropped, body flagged as the primary signal.

    ``story_title`` comes straight from Algolia's response and
    mirrors the parent post's headline.
    """
    story_title = item.raw_json.get("story_title") or "(unknown thread)"
    parts = [
        f"Source: hackernews (comment in thread titled: {story_title!r})",
        f"Author: {item.author or '(unknown)'}",
        "",
        (
            "[This is a comment on Hacker News. The thread title above is "
            "context; the comment body below is the primary signal for "
            "classification. Do not treat the comment as a standalone post.]"
        ),
        "",
        "Comment body:",
        body,
    ]
    return "\n".join(parts)


# --- classifier ------------------------------------------------------------


class Classifier:
    """Wraps an Anthropic client with the retry, parse, and persist rules
    specific to social-surveyor classifications.

    Passing ``storage=None`` disables persistence (used by ``--dry-run``
    paths that only want to build the prompt). Passing ``client=None``
    constructs an :class:`Anthropic` from the environment; tests inject
    a mock.
    """

    def __init__(
        self,
        project_config: ProjectConfig,
        classifier_config: ClassifierConfig,
        categories: CategoryConfig,
        *,
        client: Any | None = None,
        storage: Storage | None = None,
        sleep: Any | None = None,
    ) -> None:
        self.project = project_config
        self.cfg = classifier_config
        self.categories = categories
        self.client = client if client is not None else Anthropic()
        self.storage = storage
        # Injectable for tests so retries don't actually sleep.
        import time as _time

        self._sleep = sleep if sleep is not None else _time.sleep
        self._valid_category_ids = {c.id for c in categories.categories}

    def classify(self, item: ClassifierInput) -> Classification:
        """Classify one item. Raises :class:`ClassificationError` on
        unrecoverable errors (bad category, JSON malformed twice)."""
        prompt = build_prompt(item, self.cfg, self.categories)
        response = self._call_with_retry(prompt)

        parsed, final_response = self._parse_or_reprompt(prompt, response)
        category = parsed.get("category")
        urgency_raw = parsed.get("urgency")
        reasoning = str(parsed.get("reasoning") or "")

        if not isinstance(category, str) or category not in self._valid_category_ids:
            log.warning(
                "classifier.unknown_category",
                item_id=item.item_id,
                category=category,
                valid=sorted(self._valid_category_ids),
            )
            raise ClassificationError(
                f"classifier returned category {category!r} which is not in the taxonomy"
            )

        urgency = self._coerce_urgency(urgency_raw, item.item_id)

        usage = final_response.usage
        in_tok = int(getattr(usage, "input_tokens", 0) or 0)
        out_tok = int(getattr(usage, "output_tokens", 0) or 0)
        now = datetime.now(UTC)
        raw_dump = _response_to_dict(final_response)

        classification = Classification(
            item_id=item.item_id,
            category=category,
            urgency=urgency,
            reasoning=reasoning,
            prompt_version=self.cfg.prompt_version,
            model=self.cfg.model,
            input_tokens=in_tok,
            output_tokens=out_tok,
            classified_at=now,
            raw_response=raw_dump,
        )

        if self.storage is not None:
            self.storage.save_classification(
                item_id=classification.item_id,
                category=classification.category,
                urgency=classification.urgency,
                reasoning=classification.reasoning,
                prompt_version=classification.prompt_version,
                model=classification.model,
                input_tokens=classification.input_tokens,
                output_tokens=classification.output_tokens,
                classified_at=classification.classified_at,
                raw_response=classification.raw_response,
            )

        return classification

    # --- API call with retries ------------------------------------------

    def _call_with_retry(self, prompt_kwargs: dict[str, Any]) -> Any:
        """Invoke the Anthropic API with retry on transient failures.

        Per the plan: retry once on connection errors / timeouts / 5xx,
        using ``backoff_seconds``. 4xx (auth, rate limit, bad request)
        surfaces immediately — retrying those costs money without
        improving the odds.

        Every call (including failed retries) logs to ``api_usage`` if
        storage is attached, so cost tracking is accurate even when a
        classification ultimately fails.
        """
        last_exc: Exception | None = None
        attempts = self.cfg.max_retries + 1
        for attempt in range(attempts):
            try:
                response = self.client.messages.create(
                    model=self.cfg.model,
                    max_tokens=self.cfg.max_tokens,
                    temperature=self.cfg.temperature,
                    **prompt_kwargs,
                )
            except (APIConnectionError, APITimeoutError) as e:
                last_exc = e
                log.warning(
                    "classifier.transient_error",
                    attempt=attempt + 1,
                    attempts=attempts,
                    error=type(e).__name__,
                )
            except APIStatusError as e:
                if 500 <= e.status_code < 600:
                    last_exc = e
                    log.warning(
                        "classifier.server_error",
                        attempt=attempt + 1,
                        attempts=attempts,
                        status=e.status_code,
                    )
                else:
                    # 429s carry a retry-after header with the server's
                    # opinion on when to come back; surface it so the
                    # operator knows whether to re-run classify in 30s
                    # or 5min.
                    retry_after = _retry_after_seconds(e)
                    if e.status_code == 429:
                        log.warning(
                            "classifier.rate_limited",
                            status=e.status_code,
                            retry_after_seconds=retry_after,
                        )
                    raise
            else:
                self._record_usage(response)
                return response

            if attempt + 1 < attempts:
                self._sleep(self.cfg.backoff_seconds)

        assert last_exc is not None
        raise last_exc

    def _record_usage(self, response: Any) -> None:
        if self.storage is None:
            return
        usage = getattr(response, "usage", None)
        if usage is None:
            return
        self.storage.record_api_usage(
            source="anthropic",
            query_name=self.cfg.prompt_version,
            items_fetched=1,
            input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
            output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
        )

    # --- parsing / reprompt ---------------------------------------------

    def _parse_or_reprompt(
        self,
        prompt_kwargs: dict[str, Any],
        response: Any,
    ) -> tuple[dict[str, Any], Any]:
        """Parse the JSON response; on failure, re-prompt once then give up."""
        text = _first_text(response)
        try:
            return json.loads("{" + text), response
        except json.JSONDecodeError:
            log.warning(
                "classifier.malformed_json",
                snippet=text[:200],
            )

        # One re-prompt. We don't count this against max_retries; that
        # knob is for transient infra, this is a prompt-adherence issue.
        repair_messages = [
            *prompt_kwargs["messages"],
            {"role": "user", "content": "{" + text},
            {
                "role": "user",
                "content": (
                    "Your previous response was not valid JSON. "
                    "Respond with only a valid JSON object matching the "
                    'schema: {"category": "<id>", "urgency": <int>, '
                    '"reasoning": "<sentence>"}'
                ),
            },
            {"role": "assistant", "content": "{"},
        ]
        repair_kwargs = {"system": prompt_kwargs["system"], "messages": repair_messages}
        try:
            repair_response = self.client.messages.create(
                model=self.cfg.model,
                max_tokens=self.cfg.max_tokens,
                temperature=self.cfg.temperature,
                **repair_kwargs,
            )
        except (APIConnectionError, APITimeoutError, APIStatusError) as e:
            raise ClassificationError(f"repair call failed: {type(e).__name__}") from e

        self._record_usage(repair_response)
        repair_text = _first_text(repair_response)
        try:
            return json.loads("{" + repair_text), repair_response
        except json.JSONDecodeError as e:
            raise ClassificationError(
                f"model returned invalid JSON twice: {repair_text[:200]!r}"
            ) from e

    def _coerce_urgency(self, raw: Any, item_id: str) -> int:
        try:
            value = int(raw)
        except (TypeError, ValueError) as e:
            raise ClassificationError(f"urgency {raw!r} is not an integer") from e
        if value < 0 or value > 10:
            log.warning(
                "classifier.urgency_clamped",
                item_id=item_id,
                raw=value,
            )
            value = max(0, min(10, value))
        return value


# --- helpers ---------------------------------------------------------------


def _first_text(response: Any) -> str:
    """Extract the text of the first content block.

    Anthropic's ``Message.content`` is a list of blocks; for our
    prompts it's always exactly one ``text`` block. Written defensively
    because mocks often pass a minimal stand-in.
    """
    content = getattr(response, "content", None)
    if not content:
        return ""
    first = content[0]
    text = getattr(first, "text", None)
    if text is None and isinstance(first, Mapping):
        text = first.get("text")
    return str(text or "")


def _retry_after_seconds(err: APIStatusError) -> float | None:
    """Extract a ``retry-after`` value from an anthropic APIStatusError.

    The header may be an integer number of seconds or an HTTP-date; we
    only parse the integer form since that's what Anthropic sends in
    practice. Missing/unparseable header returns None (caller logs it
    as-is).
    """
    response = getattr(err, "response", None)
    headers = getattr(response, "headers", None) if response is not None else None
    if headers is None:
        return None
    value = headers.get("retry-after") or headers.get("Retry-After")
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _response_to_dict(response: Any) -> dict[str, Any]:
    """Best-effort JSON-serializable snapshot of an Anthropic response.

    The Anthropic SDK's ``Message`` is pydantic-like and exposes
    ``model_dump``; tests can pass any object that implements it, or a
    plain dict.
    """
    if isinstance(response, Mapping):
        return dict(response)
    dump = getattr(response, "model_dump", None)
    if callable(dump):
        return dict(dump())
    # Last resort — keep the raw_response column non-null but truthful.
    return {"_repr": repr(response)}
