from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from anthropic import APIConnectionError, APIStatusError, APITimeoutError

from social_surveyor.classifier import (
    BODY_CHAR_LIMIT,
    Classification,
    ClassificationError,
    Classifier,
    ClassifierInput,
    build_prompt,
)
from social_surveyor.config import (
    CategoryConfig,
    ClassifierConfig,
    FewShotExample,
    ProjectConfig,
)
from social_surveyor.storage import Storage

# --- fixtures -----------------------------------------------------------


def _cats() -> CategoryConfig:
    """The opendata taxonomy, inline — keeps tests readable without
    touching disk and without coupling to projects/opendata/."""
    return CategoryConfig.model_validate(
        {
            "version": 1,
            "categories": [
                {"id": "cost_complaint", "label": "Cost", "description": "Cost pain."},
                {
                    "id": "self_host_intent",
                    "label": "Self-host",
                    "description": "Evaluating self-hosting.",
                },
                {
                    "id": "competitor_pain",
                    "label": "Competitor",
                    "description": "Complaining about competitors.",
                },
                {"id": "off_topic", "label": "Off", "description": "Not relevant."},
            ],
            "urgency_scale": [
                {"range": [0, 3], "meaning": "Irrelevant"},
                {"range": [4, 6], "meaning": "Relevant"},
                {"range": [7, 8], "meaning": "Good opportunity"},
                {"range": [9, 10], "meaning": "Urgent"},
            ],
        }
    )


def _cfg(
    *,
    prompt_version: str = "v1",
    few_shot: list[FewShotExample] | None = None,
    max_retries: int = 1,
) -> ClassifierConfig:
    return ClassifierConfig.model_validate(
        {
            "version": 1,
            "prompt_version": prompt_version,
            "icp_description": "A TSDB built on object storage; target is observability cost complainers.",
            "few_shot_examples": [ex.model_dump() for ex in (few_shot or [])],
            "model": "claude-haiku-4-5-20251001",
            "max_tokens": 500,
            "temperature": 0.0,
            "max_retries": max_retries,
            "backoff_seconds": 0.0,
        }
    )


def _project() -> ProjectConfig:
    return ProjectConfig(name="test")


def _item(
    *,
    item_id: str = "hackernews:41234567",
    title: str = "Why we moved off Datadog",
    body: str | None = "Three quarters at $80k and climbing, switching to self-hosted.",
    author: str | None = "alice",
) -> ClassifierInput:
    source, platform_id = item_id.split(":", 1)
    return ClassifierInput(
        item_id=item_id,
        source=source,
        author=author,
        title=title,
        body=body,
        raw_json={"id": platform_id},
    )


# --- fake Anthropic client ----------------------------------------------


@dataclass
class FakeUsage:
    input_tokens: int = 100
    output_tokens: int = 50


@dataclass
class FakeBlock:
    text: str


@dataclass
class FakeResponse:
    """Minimal stand-in for ``anthropic.types.Message`` that satisfies
    the attributes the classifier actually reads."""

    text: str
    input_tokens: int = 100
    output_tokens: int = 50

    @property
    def content(self) -> list[FakeBlock]:
        return [FakeBlock(text=self.text)]

    @property
    def usage(self) -> FakeUsage:
        return FakeUsage(self.input_tokens, self.output_tokens)

    def model_dump(self) -> dict[str, Any]:
        return {
            "content": [{"type": "text", "text": self.text}],
            "usage": {
                "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens,
            },
        }


class FakeMessages:
    def __init__(self, responses: list[object]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if not self.responses:
            raise AssertionError("FakeMessages.create called more times than queued")
        item = self.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


@dataclass
class FakeClient:
    responses: list[object] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.messages = FakeMessages(self.responses)


def _body_after_prefill(
    category: str = "cost_complaint",
    urgency: int = 8,
    reasoning: str = "explicit dollar amount",
) -> str:
    """JSON body the model returns *after* the prefilled opening brace.

    The classifier prepends ``{`` before parsing, so this string must
    be the remainder.
    """
    return f'"category": "{category}", "urgency": {urgency}, "reasoning": "{reasoning}"}}'


def _fake_500() -> APIStatusError:
    """Construct an APIStatusError with status_code=500 without going
    through the real constructor, which wants an httpx Response."""
    e = APIStatusError.__new__(APIStatusError)
    e.status_code = 500
    return e


def _fake_400() -> APIStatusError:
    e = APIStatusError.__new__(APIStatusError)
    e.status_code = 400
    return e


def _fake_conn_error() -> APIConnectionError:
    return APIConnectionError.__new__(APIConnectionError)


def _fake_timeout() -> APITimeoutError:
    return APITimeoutError.__new__(APITimeoutError)


# --- prompt assembly ----------------------------------------------------


def test_build_prompt_is_deterministic() -> None:
    item = _item()
    a = build_prompt(item, _cfg(), _cats())
    b = build_prompt(item, _cfg(), _cats())
    assert a == b


def test_build_prompt_includes_every_category_id_and_label() -> None:
    system = build_prompt(_item(), _cfg(), _cats())["system"]
    for cat in _cats().categories:
        assert cat.id in system
        assert cat.label in system


def test_build_prompt_includes_urgency_scale() -> None:
    system = build_prompt(_item(), _cfg(), _cats())["system"]
    for band in _cats().urgency_scale:
        assert band.meaning in system
        assert f"{band.range[0]}-{band.range[1]}" in system


def test_build_prompt_includes_icp_description() -> None:
    cfg = _cfg()
    system = build_prompt(_item(), cfg, _cats())["system"]
    assert "TSDB built on object storage" in system


def test_build_prompt_renders_few_shot_examples_when_present() -> None:
    ex = FewShotExample(
        title="Datadog bill shock",
        body="Our $80k quarter forced a rethink.",
        expected_category="cost_complaint",
        expected_urgency=8,
        note="dollar amount + rethink",
    )
    cfg = _cfg(few_shot=[ex])
    system = build_prompt(_item(), cfg, _cats())["system"]
    assert "Datadog bill shock" in system
    assert '"category": "cost_complaint"' in system


def test_build_prompt_omits_examples_section_when_empty() -> None:
    system = build_prompt(_item(), _cfg(), _cats())["system"]
    assert "Examples:" not in system


def test_build_prompt_includes_additional_instructions_when_present() -> None:
    cfg = ClassifierConfig.model_validate(
        {
            "version": 1,
            "prompt_version": "v2",
            "icp_description": "test icp",
            "additional_instructions": (
                "Classification rules (apply in order):\n1. Rule one goes here."
            ),
            "few_shot_examples": [],
            "model": "claude-haiku-4-5-20251001",
            "max_tokens": 500,
            "temperature": 0.0,
            "max_retries": 1,
            "backoff_seconds": 0.0,
        }
    )
    system = build_prompt(_item(), cfg, _cats())["system"]
    # Rules should appear after the urgency scale and before the JSON
    # output instruction, so the classifier sees them while reading the
    # task description top-to-bottom.
    urgency_idx = system.index("Also assign an urgency score")
    rules_idx = system.index("Classification rules (apply in order):")
    output_idx = system.index("Respond with only a JSON object")
    assert urgency_idx < rules_idx < output_idx
    assert "Rule one goes here." in system


def test_build_prompt_omits_additional_instructions_when_empty() -> None:
    # The default _cfg() fixture leaves additional_instructions empty.
    system = build_prompt(_item(), _cfg(), _cats())["system"]
    assert "Classification rules (apply in order):" not in system


def test_build_prompt_truncates_long_body() -> None:
    long_body = "x" * (BODY_CHAR_LIMIT * 2)
    item = _item(body=long_body)
    user_content = build_prompt(item, _cfg(), _cats())["messages"][0]["content"]
    # Body line count: source + author + title + "Body:" header + body
    # -> body content is the final "block" after "Body:\n"; split and count.
    body_in_prompt = user_content.split("Body:\n", 1)[1]
    assert len(body_in_prompt) == BODY_CHAR_LIMIT


def test_build_prompt_hn_comment_uses_parent_title_and_drops_synthesized_one() -> None:
    """HN comments arrive with a synthesized title that carries no
    classification signal. The user message should label the item as a
    comment and swap in the parent thread's real title."""
    item = ClassifierInput(
        item_id="hackernews:47542069",
        source="hackernews",
        author="darkwater",
        title="Comment by darkwater on HN #47532339",
        body="We've been running Mimir for two years and the operational cost is killing us.",
        raw_json={
            "_tags": ["comment", "author_darkwater", "story_47532339"],
            "story_title": "OpenTelemetry profiles enters public alpha",
        },
    )
    user_content = build_prompt(item, _cfg(), _cats())["messages"][0]["content"]
    # Synthesized title is gone.
    assert "Comment by darkwater on HN #47532339" not in user_content
    # Parent thread title is present.
    assert "OpenTelemetry profiles enters public alpha" in user_content
    # Framing flag is present so the model weights the body.
    assert "comment on Hacker News" in user_content
    assert "primary signal" in user_content
    # Body is present.
    assert "Mimir for two years" in user_content


def test_build_prompt_hn_story_uses_generic_format() -> None:
    """HN stories (no 'comment' _tag) keep the generic title/body shape."""
    item = ClassifierInput(
        item_id="hackernews:47532339",
        source="hackernews",
        author="pg",
        title="OpenTelemetry profiles enters public alpha",
        body="Body of the post.",
        raw_json={"_tags": ["story", "author_pg"]},
    )
    user_content = build_prompt(item, _cfg(), _cats())["messages"][0]["content"]
    assert "Title: OpenTelemetry profiles enters public alpha" in user_content
    assert "comment on Hacker News" not in user_content


def test_build_prompt_hn_comment_missing_story_title_falls_back() -> None:
    item = ClassifierInput(
        item_id="hackernews:1",
        source="hackernews",
        author="alice",
        title="Comment by alice on HN #2",
        body="Body.",
        raw_json={"_tags": ["comment"]},  # no story_title
    )
    user_content = build_prompt(item, _cfg(), _cats())["messages"][0]["content"]
    assert "(unknown thread)" in user_content


def test_build_prompt_non_hackernews_source_uses_generic_format() -> None:
    """A reddit post with 'comment' in raw_json shouldn't accidentally
    trigger the HN-comment branch."""
    item = ClassifierInput(
        item_id="reddit:t3_abc",
        source="reddit",
        author="alice",
        title="Reddit post title",
        body="Body.",
        raw_json={"_tags": ["comment"]},  # present but ignored for non-HN
    )
    user_content = build_prompt(item, _cfg(), _cats())["messages"][0]["content"]
    assert "Title: Reddit post title" in user_content
    assert "comment on Hacker News" not in user_content


def test_build_prompt_handles_none_body_and_author() -> None:
    item = _item(body=None, author=None)
    content = build_prompt(item, _cfg(), _cats())["messages"][0]["content"]
    assert "(unknown)" in content  # author fallback
    # body is present but empty; no crash


def test_build_prompt_prefills_assistant_with_opening_brace() -> None:
    prompt = build_prompt(_item(), _cfg(), _cats())
    assert prompt["messages"][-1] == {"role": "assistant", "content": "{"}


# --- classify: happy path -----------------------------------------------


def _make_classifier(
    *,
    responses: list[object],
    storage: Storage | None = None,
    cfg: ClassifierConfig | None = None,
) -> tuple[Classifier, FakeClient]:
    client = FakeClient(responses)
    clf = Classifier(
        _project(),
        cfg or _cfg(),
        _cats(),
        client=client,
        storage=storage,
        sleep=lambda _s: None,
    )
    return clf, client


def test_classify_parses_prefilled_json_response(tmp_path: Path) -> None:
    with Storage(tmp_path / "t.db") as db:
        clf, client = _make_classifier(
            responses=[FakeResponse(_body_after_prefill(urgency=8))],
            storage=db,
        )
        result = clf.classify(_item())

        assert isinstance(result, Classification)
        assert result.category == "cost_complaint"
        assert result.urgency == 8
        assert result.reasoning == "explicit dollar amount"
        assert result.prompt_version == "v1"
        assert result.model == "claude-haiku-4-5-20251001"
        assert result.input_tokens == 100
        assert result.output_tokens == 50
        # Exactly one API call.
        assert len(client.messages.calls) == 1


def test_classify_persists_to_storage(tmp_path: Path) -> None:
    with Storage(tmp_path / "t.db") as db:
        clf, _ = _make_classifier(
            responses=[FakeResponse(_body_after_prefill())],
            storage=db,
        )
        clf.classify(_item())

        saved = db.get_classification("hackernews:41234567", "v1")
        assert saved is not None
        assert saved["category"] == "cost_complaint"
        assert saved["raw_response"]["usage"]["input_tokens"] == 100


def test_classify_logs_api_usage_with_token_counts(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    with Storage(tmp_path / "t.db") as db:
        clf, _ = _make_classifier(
            responses=[FakeResponse(_body_after_prefill(), input_tokens=1200, output_tokens=80)],
            storage=db,
        )
        clf.classify(_item())

        start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        assert db.sum_api_usage("anthropic", start) == 1
        assert db.sum_api_tokens("anthropic", start) == (1200, 80)
        # Logged under the prompt_version, so eval can attribute tokens
        # to specific prompts.
        assert db.api_usage_by_query("anthropic", start) == {"v1": 1}


def test_classify_without_storage_does_not_persist_or_log() -> None:
    clf, _ = _make_classifier(
        responses=[FakeResponse(_body_after_prefill())],
        storage=None,
    )
    result = clf.classify(_item())
    assert result.category == "cost_complaint"
    # No exception raised — the no-storage path is the --dry-run shape.


# --- classify: error paths ---------------------------------------------


def test_classify_reprompts_once_on_malformed_json_then_succeeds() -> None:
    # First response isn't valid JSON after prefill; second one is.
    clf, client = _make_classifier(
        responses=[
            FakeResponse("this is not json at all"),
            FakeResponse(_body_after_prefill(urgency=7)),
        ],
    )
    result = clf.classify(_item())
    assert result.urgency == 7
    assert len(client.messages.calls) == 2
    # The repair call's messages include the failed assistant turn so
    # the model sees its own output and can correct it.
    repair_msgs = client.messages.calls[1]["messages"]
    assert any(
        isinstance(m, dict) and "not valid JSON" in m.get("content", "") for m in repair_msgs
    )


def test_classify_raises_when_reprompt_also_malformed() -> None:
    clf, _ = _make_classifier(
        responses=[FakeResponse("garbage"), FakeResponse("still garbage")],
    )
    with pytest.raises(ClassificationError, match="invalid JSON twice"):
        clf.classify(_item())


def test_classify_raises_on_unknown_category() -> None:
    # Model returned a category id that isn't in the taxonomy.
    clf, _ = _make_classifier(
        responses=[FakeResponse(_body_after_prefill(category="not_a_real_category"))],
    )
    with pytest.raises(ClassificationError, match="not in the taxonomy"):
        clf.classify(_item())


def test_classify_clamps_urgency_above_10() -> None:
    clf, _ = _make_classifier(responses=[FakeResponse(_body_after_prefill(urgency=15))])
    result = clf.classify(_item())
    assert result.urgency == 10


def test_classify_clamps_urgency_below_0() -> None:
    clf, _ = _make_classifier(responses=[FakeResponse(_body_after_prefill(urgency=-3))])
    result = clf.classify(_item())
    assert result.urgency == 0


def test_classify_raises_on_non_integer_urgency() -> None:
    clf, _ = _make_classifier(
        responses=[
            FakeResponse('"category": "off_topic", "urgency": "high", "reasoning": "x"}'),
        ],
    )
    with pytest.raises(ClassificationError, match="not an integer"):
        clf.classify(_item())


# --- classify: retry logic ----------------------------------------------


def test_classify_retries_on_connection_error_and_succeeds() -> None:
    clf, client = _make_classifier(
        responses=[_fake_conn_error(), FakeResponse(_body_after_prefill())],
    )
    result = clf.classify(_item())
    assert result.category == "cost_complaint"
    assert len(client.messages.calls) == 2


def test_classify_retries_on_5xx() -> None:
    clf, client = _make_classifier(
        responses=[_fake_500(), FakeResponse(_body_after_prefill())],
    )
    result = clf.classify(_item())
    assert result.category == "cost_complaint"
    assert len(client.messages.calls) == 2


def test_classify_retries_on_timeout() -> None:
    clf, client = _make_classifier(
        responses=[_fake_timeout(), FakeResponse(_body_after_prefill())],
    )
    clf.classify(_item())
    assert len(client.messages.calls) == 2


def test_classify_does_not_retry_on_4xx() -> None:
    clf, client = _make_classifier(responses=[_fake_400()])
    with pytest.raises(APIStatusError):
        clf.classify(_item())
    # Exactly one call — 4xx surfaces immediately.
    assert len(client.messages.calls) == 1


def test_classify_raises_after_max_retries() -> None:
    # max_retries=1 means total attempts = 2. Two 500s exhausts it.
    clf, client = _make_classifier(
        responses=[_fake_500(), _fake_500()],
    )
    with pytest.raises(APIStatusError):
        clf.classify(_item())
    assert len(client.messages.calls) == 2


def test_classify_retries_respects_custom_max_retries() -> None:
    clf, client = _make_classifier(
        responses=[_fake_500(), _fake_500(), FakeResponse(_body_after_prefill())],
        cfg=_cfg(max_retries=2),
    )
    clf.classify(_item())
    assert len(client.messages.calls) == 3


# --- ClassifierInput.from_row ------------------------------------------


def test_classifier_input_from_row_builds_canonical_item_id() -> None:
    row = {
        "source": "hackernews",
        "platform_id": "41234567",
        "title": "T",
        "body": "B",
        "author": "alice",
        "raw_json": {"id": "41234567"},
    }
    ci = ClassifierInput.from_row(row)
    assert ci.item_id == "hackernews:41234567"
    assert ci.source == "hackernews"
    assert ci.author == "alice"


def test_classifier_input_from_row_tolerates_missing_optional_fields() -> None:
    row = {
        "source": "reddit",
        "platform_id": "abc",
        "title": "T",
    }
    ci = ClassifierInput.from_row(row)
    assert ci.body is None
    assert ci.author is None
    assert ci.raw_json == {}
