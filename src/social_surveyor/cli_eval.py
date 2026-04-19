"""``social-surveyor eval`` — score the classifier against labeled.jsonl.

Version-exact-match semantics
-----------------------------
When invoked as ``eval --prompt-version v2``, the harness looks up
classifications where ``prompt_version = 'v2'`` exactly. It never
falls back to v1 or v3 rows. If a v2 classification is missing for a
labeled item, we classify now and save. This is what makes A/B-testing
prompts meaningful: the numbers you see are strictly attributable to
the version you asked about.

Cold start vs warm cache
------------------------
Warm-cache eval (all classifications cached for the target version)
hits zero API calls and returns in under a second — the whole point
of the iteration loop. Cold-start eval classifies ~150 items at
~2s/call, ~5 minutes. Progress is echoed per item so the wait isn't
silent.
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog
import typer
from anthropic import Anthropic

from .classifier import ClassificationError, Classifier, ClassifierInput
from .config import (
    CategoryConfig,
    ClassifierConfig,
    ConfigError,
    ProjectConfig,
    load_categories,
    load_classifier_config,
    load_project_config,
)
from .eval_metrics import (
    EvalPair,
    compute_metrics,
    stabilization_check,
    stop_criteria,
)
from .labeling import LabelEntry, iter_label_entries, labels_path
from .storage import Storage

log = structlog.get_logger("cli.eval")

# Heuristic default used when the project's taxonomy matches the opendata
# shape. A fork with different categories can override by ... not using
# these ids. The three ids below are the "alert to Slack" categories for
# opendata.
NON_ALERT_WORTHY_DEFAULT = {"off_topic", "neutral_discussion", "tutorial_or_marketing"}

# Haiku 4.5 public pricing (USD per million tokens) as of 2026-04.
# Used only for cost estimates in the eval summary; update when Anthropic
# changes pricing. Not load-bearing for correctness — if the prices drift
# a bit, the stop-criteria cost row might flip, but the raw token counts
# in api_usage are always accurate.
HAIKU_INPUT_USD_PER_MTOK = 1.00
HAIKU_OUTPUT_USD_PER_MTOK = 5.00


def run_eval(
    project: str,
    db_path: Path,
    projects_root: Path,
    *,
    prompt_version_override: str | None,
    verbose: bool,
    export_path: Path | None,
    client: Any | None = None,
    echo_fn: Any = typer.echo,
    progress_every: int = 5,
) -> dict[str, Any]:
    """Run the eval. Returns the full metrics bundle for programmatic use.

    Prints a human-readable summary to stdout via ``echo_fn``.
    """
    try:
        project_cfg = load_project_config(project, projects_root=projects_root)
        base_clf_cfg = load_classifier_config(project, projects_root=projects_root)
        categories = load_categories(project, projects_root=projects_root)
    except ConfigError as e:
        raise typer.BadParameter(str(e)) from None

    clf_cfg = _apply_version_override(base_clf_cfg, prompt_version_override)
    alert_worthy_ids = _infer_alert_worthy_ids(categories)

    labels_file = labels_path(project, projects_root=projects_root)
    effective_labels = _load_effective_labels(labels_file)
    if not effective_labels:
        raise typer.BadParameter(
            f"no labels at {labels_file}; run `social-surveyor label --project {project}`"
        )

    if not db_path.is_file():
        raise typer.BadParameter(f"no DB at {db_path} yet — run a poll first")

    with Storage(db_path) as db:
        harness = _EvalHarness(
            project_cfg=project_cfg,
            clf_cfg=clf_cfg,
            categories=categories,
            db=db,
            client=client,
        )
        pairs, run_cost = harness.build_pairs(
            effective_labels,
            echo_fn=echo_fn,
            progress_every=progress_every,
        )

        metrics = compute_metrics(pairs, categories, alert_worthy_ids)

        # Cost-per-classification uses tokens spent in THIS run, divided
        # by the number of items we classified now (not the warm-cache
        # items, which cost 0 this run).
        newly = run_cost["newly_classified"]
        cost_usd = _usd(run_cost["input_tokens"], run_cost["output_tokens"])
        per_call_cost = cost_usd / newly if newly else None
        criteria = stop_criteria(
            metrics,
            alert_worthy_ids,
            cost_per_classification_usd=per_call_cost,
        )

        # Previous run for the stabilization check: the most recent
        # different prompt_version that also has classifications for
        # these item_ids. Pragmatic: just pick the most-recently-
        # classified-at prior version; if we don't find one, stabilize
        # check returns None.
        prev_metrics = _prior_version_metrics(
            db=db,
            current_version=clf_cfg.prompt_version,
            effective_labels=effective_labels,
            categories=categories,
            alert_worthy_ids=alert_worthy_ids,
        )
        stabilization = stabilization_check(metrics, prev_metrics)

    _render_summary(
        metrics=metrics,
        criteria=criteria,
        stabilization=stabilization,
        clf_cfg=clf_cfg,
        run_cost=run_cost,
        verbose=verbose,
        pairs=pairs,
        categories=categories,
        alert_worthy_ids=alert_worthy_ids,
        echo_fn=echo_fn,
    )

    if export_path is not None:
        _export(
            path=export_path,
            clf_cfg=clf_cfg,
            metrics=metrics,
            criteria=criteria,
            stabilization=stabilization,
            run_cost=run_cost,
            pairs=pairs,
        )
        echo_fn(f"\nwrote eval export: {export_path}")

    return {
        "metrics": metrics,
        "criteria": criteria,
        "stabilization": stabilization,
        "run_cost": run_cost,
    }


# --- harness ------------------------------------------------------------


class _EvalHarness:
    def __init__(
        self,
        *,
        project_cfg: ProjectConfig,
        clf_cfg: ClassifierConfig,
        categories: CategoryConfig,
        db: Storage,
        client: Any | None,
    ) -> None:
        self.project_cfg = project_cfg
        self.clf_cfg = clf_cfg
        self.categories = categories
        self.db = db
        self._client = client
        self._classifier: Classifier | None = None

    def _ensure_classifier(self) -> Classifier:
        if self._classifier is None:
            real_client = self._client if self._client is not None else Anthropic()
            self._classifier = Classifier(
                self.project_cfg,
                self.clf_cfg,
                self.categories,
                client=real_client,
                storage=self.db,
            )
        return self._classifier

    def build_pairs(
        self,
        effective_labels: list[LabelEntry],
        *,
        echo_fn: Any,
        progress_every: int,
    ) -> tuple[list[EvalPair], dict[str, int]]:
        """Return ``(pairs, run_cost)`` — one EvalPair per label, and a
        tally of what this run spent vs. what the warm cache served.
        """
        pairs: list[EvalPair] = []
        cached = 0
        newly_classified = 0
        run_input_tokens = 0
        run_output_tokens = 0
        missing_in_db = 0
        failures = 0

        for i, label in enumerate(effective_labels, start=1):
            source, platform_id = _split_canonical(label.item_id)
            row = self.db.get_item_by_id(source, platform_id)
            if row is None:
                missing_in_db += 1
                pairs.append(
                    EvalPair(
                        item_id=label.item_id,
                        label_category=label.category,
                        label_urgency=label.urgency,
                        model_category=None,
                        model_urgency=None,
                        source=source,
                    )
                )
                continue

            existing = self.db.get_classification(
                label.item_id,
                self.clf_cfg.prompt_version,
            )
            if existing is not None:
                cached += 1
                pairs.append(
                    EvalPair(
                        item_id=label.item_id,
                        label_category=label.category,
                        label_urgency=label.urgency,
                        model_category=existing["category"],
                        model_urgency=int(existing["urgency"]),
                        source=source,
                        title=row.get("title") or "",
                        body=row.get("body") or "",
                    )
                )
                continue

            # Cold: classify now.
            classifier = self._ensure_classifier()
            ci = ClassifierInput.from_row(row)
            try:
                result = classifier.classify(ci)
                newly_classified += 1
                run_input_tokens += result.input_tokens
                run_output_tokens += result.output_tokens
                pairs.append(
                    EvalPair(
                        item_id=label.item_id,
                        label_category=label.category,
                        label_urgency=label.urgency,
                        model_category=result.category,
                        model_urgency=result.urgency,
                        source=source,
                        title=row.get("title") or "",
                        body=row.get("body") or "",
                    )
                )
            except ClassificationError as e:
                failures += 1
                log.warning(
                    "eval.classify_failed",
                    item_id=label.item_id,
                    error=str(e),
                )
                pairs.append(
                    EvalPair(
                        item_id=label.item_id,
                        label_category=label.category,
                        label_urgency=label.urgency,
                        model_category=None,
                        model_urgency=None,
                        source=source,
                    )
                )

            if progress_every and (i % progress_every == 0):
                echo_fn(
                    f"  classified {i}/{len(effective_labels)} "
                    f"(cached={cached}, new={newly_classified}, failed={failures})"
                )

        return pairs, {
            "cached": cached,
            "newly_classified": newly_classified,
            "input_tokens": run_input_tokens,
            "output_tokens": run_output_tokens,
            "missing_in_db": missing_in_db,
            "failures": failures,
        }


# --- label resolution ---------------------------------------------------


def _load_effective_labels(path: Path) -> list[LabelEntry]:
    """Apply latest-wins per item_id.

    Matches PLAN.md's "append-only labeled.jsonl with timestamp
    precedence" semantics. Duplicate item_ids are collapsed by picking
    the entry with the latest ``labeled_at``; earlier entries are
    retained in the file for audit but ignored by the eval harness.
    """
    entries = iter_label_entries(path)
    latest: dict[str, LabelEntry] = {}
    for e in entries:
        prior = latest.get(e.item_id)
        if prior is None or e.labeled_at > prior.labeled_at:
            latest[e.item_id] = e
    return list(latest.values())


def _apply_version_override(
    base: ClassifierConfig,
    override: str | None,
) -> ClassifierConfig:
    if override is None:
        return base
    return base.model_copy(update={"prompt_version": override})


def _infer_alert_worthy_ids(categories: CategoryConfig) -> set[str]:
    return {c.id for c in categories.categories if c.id not in NON_ALERT_WORTHY_DEFAULT}


def _split_canonical(item_id: str) -> tuple[str, str]:
    source, _, platform_id = item_id.partition(":")
    return source, platform_id


def _usd(input_tokens: int, output_tokens: int) -> float:
    return (
        input_tokens / 1_000_000 * HAIKU_INPUT_USD_PER_MTOK
        + output_tokens / 1_000_000 * HAIKU_OUTPUT_USD_PER_MTOK
    )


# --- prior version lookup for stabilization check -----------------------


def _prior_version_metrics(
    *,
    db: Storage,
    current_version: str,
    effective_labels: list[LabelEntry],
    categories: CategoryConfig,
    alert_worthy_ids: set[str],
) -> dict[str, Any] | None:
    """Find the most-recent prompt_version other than current that has
    classifications for at least half the labeled items, and compute
    metrics for it. Returns None when no such version exists.

    Half-coverage threshold is pragmatic: if the prior version only
    covers 10 of 149 items, its metrics are too noisy to drive the
    "stabilized across versions" check.
    """
    rows = db._conn.execute(
        """
        SELECT prompt_version, COUNT(*) AS n, MAX(classified_at) AS most_recent
        FROM classifications
        WHERE prompt_version != ?
        GROUP BY prompt_version
        ORDER BY most_recent DESC
        """,
        (current_version,),
    ).fetchall()
    if not rows:
        return None

    min_coverage = len(effective_labels) // 2
    for row in rows:
        prev_version = row["prompt_version"]
        if int(row["n"]) < min_coverage:
            continue
        pairs = _pairs_for_version(db, prev_version, effective_labels)
        if not pairs:
            continue
        return compute_metrics(pairs, categories, alert_worthy_ids)
    return None


def _pairs_for_version(
    db: Storage,
    prompt_version: str,
    effective_labels: list[LabelEntry],
) -> list[EvalPair]:
    """EvalPairs for ``prompt_version``, using cache-only lookups.

    If a labeled item has no classification under ``prompt_version``
    we skip it here — we don't re-classify for the stabilization
    comparison, because we only care about the numbers that *were*
    produced for that version.
    """
    pairs: list[EvalPair] = []
    for label in effective_labels:
        existing = db.get_classification(label.item_id, prompt_version)
        if existing is None:
            continue
        source, _ = _split_canonical(label.item_id)
        pairs.append(
            EvalPair(
                item_id=label.item_id,
                label_category=label.category,
                label_urgency=label.urgency,
                model_category=existing["category"],
                model_urgency=int(existing["urgency"]),
                source=source,
            )
        )
    return pairs


# --- rendering ----------------------------------------------------------


def _render_summary(
    *,
    metrics: dict[str, Any],
    criteria: list[dict[str, Any]],
    stabilization: dict[str, Any] | None,
    clf_cfg: ClassifierConfig,
    run_cost: dict[str, int],
    verbose: bool,
    pairs: list[EvalPair],
    categories: CategoryConfig,
    alert_worthy_ids: set[str],
    echo_fn: Any,
) -> None:
    lines: list[str] = []
    lines.append("")
    lines.append(f"Eval for prompt_version: {clf_cfg.prompt_version}")
    lines.append(f"Model: {clf_cfg.model}")
    lines.append(f"Labeled items: {metrics['total_labeled']}")
    lines.append(
        f"Classifications: {metrics['classified']} "
        f"({run_cost['cached']} cached, {run_cost['newly_classified']} newly classified)"
    )
    if metrics["classification_failures"]:
        lines.append(
            f"Classification failures: {metrics['classification_failures']} (excluded from metrics)"
        )
    run_usd = _usd(run_cost["input_tokens"], run_cost["output_tokens"])
    lines.append(
        f"Cost this run: ${run_usd:.4f} "
        f"({run_cost['input_tokens']:,} input + {run_cost['output_tokens']:,} output tokens)"
    )

    overall = metrics["overall_accuracy"]
    alert_worthy = metrics["alert_worthy_accuracy"]
    lines.append("")
    lines.append(
        f"Overall category accuracy:      {_pct(overall['accuracy'])} "
        f"({overall['matched']}/{overall['total']})"
    )
    lines.append(
        f"Alert-worthy category accuracy: {_pct(alert_worthy['accuracy'])} "
        f"({alert_worthy['matched']}/{alert_worthy['total']})   ← production signal"
    )

    lines.append("")
    lines.append("Per-category (P/R/F1):")
    per_cat = metrics["per_category"]
    # Widest category-id width for alignment.
    id_width = max((len(cid) for cid in per_cat), default=10)
    for cid, stats in per_cat.items():
        tag = f"  [{stats['variance_tag']}]" if stats["variance_tag"] else ""
        lines.append(
            f"  {cid.ljust(id_width)}  "
            f"{stats['precision']:.2f} / {stats['recall']:.2f} / {stats['f1']:.2f}"
            f"    (n={stats['n']}){tag}"
        )

    urgency = metrics["urgency"]
    lines.append("")
    lines.append("Urgency MAE:")
    lines.append(f"  overall:         {urgency['overall_mae']:.2f}")
    high_n = urgency.get("high_urgency_n", 0)
    lines.append(
        f"  high-urgency:    {urgency['high_urgency_mae']:.2f}    ← production signal (n={high_n})"
    )
    lines.append(f"  band accuracy:   {_pct(urgency['band_accuracy'])}")

    pr = metrics["alert_worthy_precision_recall"]
    lines.append("")
    lines.append(
        f"Alert-worthy precision: {pr['precision']:.2f}  "
        f"(model said urgency>=7 on {pr['n_model_alert']}; "
        f"{pr['n_both']} matched human >=7)"
    )
    lines.append(
        f"Alert-worthy recall:    {pr['recall']:.2f}  "
        f"(human said urgency>=7 on {pr['n_human_alert']})"
    )

    lines.append("")
    lines.append("Confusion matrix (alert-worthy only, rows=human, cols=model):")
    lines.extend(_render_confusion(metrics["confusion_matrix"]))

    lines.append("")
    lines.append("Stop criteria status:")
    for c in criteria:
        checkbox = "[x]" if c["met"] else "[ ]"
        detail = _fmt_criterion_detail(c)
        lines.append(f"  {checkbox} {c['name']} (currently {detail})")

    if stabilization is not None:
        lines.append("")
        verb = "stabilized" if stabilization["stable"] else "still moving"
        lines.append(
            f"Stabilization vs prior version: {verb} (tolerance=±{stabilization['tolerance']:.2f})"
        )
        for field, delta in stabilization["deltas"].items():
            lines.append(f"  {field}: Δ={delta:.3f}")

    if verbose:
        lines.append("")
        lines.append("Disagreements:")
        lines.extend(_render_disagreements(pairs, limit=20))

    for line in lines:
        echo_fn(line)


def _render_confusion(confusion: dict[str, Any]) -> list[str]:
    rows = confusion["rows"]
    cols = confusion["cols"]
    counts = confusion["counts"]
    # Max width for a column header or cell.
    row_label_width = max((len(r) for r in rows), default=10)
    col_widths = {c: max(len(c), 3) for c in cols}
    header = "  " + " " * row_label_width + "  " + "  ".join(c.rjust(col_widths[c]) for c in cols)
    out = [header]
    for r in rows:
        cells = "  ".join(str(counts[r][c]).rjust(col_widths[c]) for c in cols)
        out.append(f"  {r.ljust(row_label_width)}  {cells}")
    return out


def _fmt_criterion_detail(criterion: dict[str, Any]) -> str:
    if criterion["name"].startswith("Cost per"):
        return f"${criterion['current']:.4f}"
    if "worst_category" in criterion and criterion.get("worst_category"):
        return f"{criterion['worst_category']}: {criterion['current']:.2f}"
    return f"{criterion['current']:.2f}"


def _render_disagreements(pairs: list[EvalPair], *, limit: int) -> list[str]:
    disagreements = [
        p for p in pairs if p.model_category is not None and p.model_category != p.label_category
    ]
    if not disagreements:
        return ["  (none)"]
    shown = disagreements[:limit]
    out: list[str] = []
    for p in shown:
        out.append(
            f"  [{p.item_id}]  "
            f"human={p.label_category} u={p.label_urgency}  "
            f"model={p.model_category} u={p.model_urgency}"
        )
    if len(disagreements) > limit:
        out.append(f"  ... and {len(disagreements) - limit} more")
    return out


def _export(
    *,
    path: Path,
    clf_cfg: ClassifierConfig,
    metrics: dict[str, Any],
    criteria: list[dict[str, Any]],
    stabilization: dict[str, Any] | None,
    run_cost: dict[str, int],
    pairs: list[EvalPair],
) -> None:
    disagreements = []
    for p in pairs:
        if p.model_category is None:
            continue
        if p.model_category == p.label_category:
            continue
        # Cap body at 500 chars so the export is scannable for pattern-
        # matching review (the primary use case). Long HN comments get
        # the head truncated; the full stored body is still in SQLite.
        body_trunc = p.body[:500] + ("…" if len(p.body) > 500 else "")
        disagreements.append(
            {
                "item_id": p.item_id,
                "source": p.source,
                "title": p.title,
                "body": body_trunc,
                "human": {"category": p.label_category, "urgency": p.label_urgency},
                "model": {"category": p.model_category, "urgency": p.model_urgency},
            }
        )
    # Per-source breakdown is cheap and useful in exports even though
    # the terminal summary doesn't show it — JSON consumers can slice.
    per_source_counts: dict[str, int] = defaultdict(int)
    for p in pairs:
        per_source_counts[p.source] += 1

    data = {
        "prompt_version": clf_cfg.prompt_version,
        "model": clf_cfg.model,
        "generated_at": datetime.now(UTC).isoformat(),
        "metrics": metrics,
        "stop_criteria": criteria,
        "stabilization": stabilization,
        "run_cost": {
            **run_cost,
            "usd": _usd(run_cost["input_tokens"], run_cost["output_tokens"]),
        },
        "items_by_source": dict(per_source_counts),
        "disagreements": disagreements,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")


def _pct(value: float) -> str:
    return f"{value * 100:.1f}%"


__all__ = ["run_eval"]
