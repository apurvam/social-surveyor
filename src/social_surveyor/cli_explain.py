"""``social-surveyor explain`` — dump everything we know about one item.

Surfaces, in order:

1. Raw item content (title, body, author, url, source-specific raw_json)
2. Effective human label from labeled.jsonl, if any (latest-wins)
3. Every classification across all prompt_versions, newest-first
4. The exact prompt the most recent classification was built from,
   reconstructed from the current classifier config

The prompt reconstruction uses the *current* classifier config, so
if you've changed classifier.yaml since the classification was
produced the reconstructed prompt may differ. That's intentional:
``explain`` is a debugging tool for "why does my current config
produce this classification", not a forensic record of a past run.
The stored ``raw_response`` column has the actual past response.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import typer

from .classifier import ClassifierInput, build_prompt
from .config import (
    ConfigError,
    load_categories,
    load_classifier_config,
    load_project_config,
)
from .labeling import LabelEntry, iter_label_entries, labels_path
from .storage import Storage


def run_explain(
    project: str,
    db_path: Path,
    projects_root: Path,
    *,
    item_id: str,
    echo_fn: Any = typer.echo,
) -> None:
    try:
        load_project_config(project, projects_root=projects_root)
        clf_cfg = load_classifier_config(project, projects_root=projects_root)
        categories = load_categories(project, projects_root=projects_root)
    except ConfigError as e:
        raise typer.BadParameter(str(e)) from None

    if not db_path.is_file():
        raise typer.BadParameter(f"no DB at {db_path} yet — run a poll first")

    source, platform_id = _split_canonical(item_id)
    with Storage(db_path) as db:
        row = db.get_item_by_id(source, platform_id)
        if row is None:
            raise typer.BadParameter(f"no item {item_id!r} in DB")

        classifications = db.list_classifications(item_id)

    label = _effective_label(project, projects_root, item_id)

    echo_fn(f"=== item {item_id} ===")
    echo_fn(f"source:     {row['source']}")
    echo_fn(f"title:      {row.get('title')}")
    echo_fn(f"author:     {row.get('author')}")
    echo_fn(f"url:        {row.get('url')}")
    created = row.get("created_at")
    echo_fn(f"created_at: {created.isoformat() if hasattr(created, 'isoformat') else created}")
    body = row.get("body") or ""
    echo_fn("")
    echo_fn("body:")
    for line in (body or "(empty)").splitlines() or [body]:
        echo_fn(f"  {line}")

    raw_json = row.get("raw_json")
    if raw_json:
        echo_fn("")
        echo_fn("raw_json keys:")
        for k in sorted(raw_json.keys()):
            echo_fn(f"  - {k}")

    echo_fn("")
    echo_fn("=== effective human label ===")
    if label is None:
        echo_fn("(none)")
    else:
        echo_fn(f"category: {label.category}")
        echo_fn(f"urgency:  {label.urgency}")
        echo_fn(f"labeled_at: {label.labeled_at.isoformat()}")
        if label.note:
            echo_fn(f"note:     {label.note}")

    echo_fn("")
    echo_fn(f"=== classifications ({len(classifications)}) ===")
    if not classifications:
        echo_fn("(none)")
    else:
        for c in classifications:
            echo_fn(
                f"- {c['prompt_version']}  "
                f"category={c['category']} urgency={c['urgency']}  "
                f"model={c['model']}  "
                f"classified_at={c['classified_at'].isoformat()}"
            )
            if c.get("reasoning"):
                echo_fn(f"    reasoning: {c['reasoning']}")
            if c.get("input_tokens") is not None:
                echo_fn(f"    tokens: input={c['input_tokens']} output={c['output_tokens']}")

    echo_fn("")
    echo_fn(
        f"=== reconstructed prompt under current config (prompt_version={clf_cfg.prompt_version}) ==="
    )
    ci = ClassifierInput.from_row(row)
    prompt = build_prompt(ci, clf_cfg, categories)
    echo_fn("--- system ---")
    echo_fn(prompt["system"])
    echo_fn("--- user ---")
    echo_fn(prompt["messages"][0]["content"])

    if classifications:
        echo_fn("")
        echo_fn("=== most recent raw_response (JSON) ===")
        echo_fn(json.dumps(classifications[0].get("raw_response"), indent=2, default=str))


def _effective_label(
    project: str,
    projects_root: Path,
    item_id: str,
) -> LabelEntry | None:
    path = labels_path(project, projects_root=projects_root)
    entries = iter_label_entries(path)
    latest: LabelEntry | None = None
    for e in entries:
        if e.item_id != item_id:
            continue
        if latest is None or e.labeled_at > latest.labeled_at:
            latest = e
    return latest


def _split_canonical(item_id: str) -> tuple[str, str]:
    if ":" not in item_id:
        raise typer.BadParameter(
            f"item_id {item_id!r} must be in the form '<source>:<platform_id>'"
        )
    source, _, platform_id = item_id.partition(":")
    return source, platform_id


__all__ = ["run_explain"]
