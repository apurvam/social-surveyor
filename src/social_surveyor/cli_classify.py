"""``social-surveyor classify`` — drive the Classifier over the DB.

Three modes:

- ``--item-id``: classify exactly one item. Surfaces the full
  classification JSON to stdout. Useful when triaging a surprising
  eval disagreement.
- ``--limit N``: classify the first N items that have no classification
  under the active ``prompt_version``.
- neither: classify every item that has no classification under the
  active ``prompt_version``.

``--dry-run`` prints the assembled prompt without making any API
calls. ``--prompt-version`` overrides the version from
``classifier.yaml`` (Classifier stamps classifications with whatever
version it sees in its config, so we rebuild the config for the
override).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import structlog
import typer
from anthropic import Anthropic

from .classifier import (
    Classification,
    ClassificationError,
    Classifier,
    ClassifierInput,
    build_prompt,
)
from .config import (
    ClassifierConfig,
    ConfigError,
    load_categories,
    load_classifier_config,
    load_project_config,
)
from .storage import Storage

log = structlog.get_logger("cli.classify")


def run_classify(
    project: str,
    db_path: Path,
    projects_root: Path,
    *,
    item_id: str | None,
    limit: int | None,
    prompt_version_override: str | None,
    dry_run: bool,
    client: Any | None = None,
    echo_fn: Any = typer.echo,
) -> dict[str, int]:
    """Entry point invoked from ``cli.classify``.

    ``client`` is dependency-injected so tests can pass a fake Anthropic
    client without patching the module.
    """
    try:
        project_cfg = load_project_config(project, projects_root=projects_root)
        base_clf_cfg = load_classifier_config(project, projects_root=projects_root)
        categories = load_categories(project, projects_root=projects_root)
    except ConfigError as e:
        raise typer.BadParameter(str(e)) from None

    clf_cfg = _resolve_classifier_config(base_clf_cfg, prompt_version_override)

    if not db_path.is_file():
        raise typer.BadParameter(f"no DB at {db_path} yet — run a poll first")

    with Storage(db_path) as db:
        if item_id is not None:
            return _classify_one(
                item_id=item_id,
                db=db,
                project_cfg=project_cfg,
                clf_cfg=clf_cfg,
                categories=categories,
                client=client,
                dry_run=dry_run,
                echo_fn=echo_fn,
            )

        return _classify_batch(
            db=db,
            project_cfg=project_cfg,
            clf_cfg=clf_cfg,
            categories=categories,
            client=client,
            dry_run=dry_run,
            limit=limit,
            echo_fn=echo_fn,
        )


def _resolve_classifier_config(
    base: ClassifierConfig,
    override: str | None,
) -> ClassifierConfig:
    """Return the effective ClassifierConfig, swapping in the override
    prompt_version if given.

    The Classifier stamps classifications with ``cfg.prompt_version``;
    overriding it means we retrieve / persist under that version. We
    re-validate rather than patching the instance so the caller can
    trust the returned object is still fully validated.
    """
    if override is None:
        return base
    return base.model_copy(update={"prompt_version": override})


def _classify_one(
    *,
    item_id: str,
    db: Storage,
    project_cfg: Any,
    clf_cfg: ClassifierConfig,
    categories: Any,
    client: Any | None,
    dry_run: bool,
    echo_fn: Any,
) -> dict[str, int]:
    source, platform_id = _split_canonical(item_id)
    row = db.get_item_by_id(source, platform_id)
    if row is None:
        raise typer.BadParameter(f"no item {item_id!r} in DB")

    ci = ClassifierInput.from_row(row)

    if dry_run:
        _emit_dry_run(ci, clf_cfg, categories, echo_fn)
        return {"classified": 0, "failed": 0, "dry_run": 1}

    classifier = _make_classifier(project_cfg, clf_cfg, categories, db, client)
    try:
        result = classifier.classify(ci)
    except ClassificationError as e:
        echo_fn(f"classification failed: {e}")
        return {"classified": 0, "failed": 1, "dry_run": 0}

    echo_fn(json.dumps(_classification_to_dict(result), indent=2, default=str))
    return {"classified": 1, "failed": 0, "dry_run": 0}


def _classify_batch(
    *,
    db: Storage,
    project_cfg: Any,
    clf_cfg: ClassifierConfig,
    categories: Any,
    client: Any | None,
    dry_run: bool,
    limit: int | None,
    echo_fn: Any,
) -> dict[str, int]:
    rows = db.get_unclassified_items(clf_cfg.prompt_version, limit=limit)
    echo_fn(
        f"classifying {len(rows)} items under prompt_version={clf_cfg.prompt_version!r}"
        f"{' (dry-run)' if dry_run else ''}"
    )

    if dry_run:
        for row in rows:
            ci = ClassifierInput.from_row(row)
            _emit_dry_run(ci, clf_cfg, categories, echo_fn)
            echo_fn("---")
        return {"classified": 0, "failed": 0, "dry_run": len(rows)}

    classifier = _make_classifier(project_cfg, clf_cfg, categories, db, client)
    classified = 0
    failed = 0
    for i, row in enumerate(rows, start=1):
        ci = ClassifierInput.from_row(row)
        try:
            result = classifier.classify(ci)
            classified += 1
            # Short one-liner per item so progress is visible on stdout
            # without drowning the user in full JSON.
            echo_fn(f"[{i}/{len(rows)}] {ci.item_id} -> {result.category} u={result.urgency}")
        except ClassificationError as e:
            failed += 1
            echo_fn(f"[{i}/{len(rows)}] {ci.item_id} -> FAILED: {e}")
            log.warning("classify.item_failed", item_id=ci.item_id, error=str(e))

    echo_fn(f"done — classified={classified} failed={failed}")
    return {"classified": classified, "failed": failed, "dry_run": 0}


def _make_classifier(
    project_cfg: Any,
    clf_cfg: ClassifierConfig,
    categories: Any,
    db: Storage,
    client: Any | None,
) -> Classifier:
    real_client = client if client is not None else Anthropic()
    return Classifier(
        project_cfg,
        clf_cfg,
        categories,
        client=real_client,
        storage=db,
    )


def _emit_dry_run(
    ci: ClassifierInput,
    clf_cfg: ClassifierConfig,
    categories: Any,
    echo_fn: Any,
) -> None:
    prompt = build_prompt(ci, clf_cfg, categories)
    echo_fn(f"item_id: {ci.item_id}")
    echo_fn("=== SYSTEM ===")
    echo_fn(prompt["system"])
    echo_fn("=== USER ===")
    echo_fn(prompt["messages"][0]["content"])


def _split_canonical(item_id: str) -> tuple[str, str]:
    if ":" not in item_id:
        raise typer.BadParameter(
            f"item_id {item_id!r} must be in the form '<source>:<platform_id>'"
        )
    source, _, platform_id = item_id.partition(":")
    return source, platform_id


def _classification_to_dict(c: Classification) -> dict[str, Any]:
    return {
        "item_id": c.item_id,
        "category": c.category,
        "urgency": c.urgency,
        "reasoning": c.reasoning,
        "prompt_version": c.prompt_version,
        "model": c.model,
        "input_tokens": c.input_tokens,
        "output_tokens": c.output_tokens,
        "classified_at": c.classified_at.isoformat(),
    }


__all__ = ["run_classify"]
