"""Pure metric computation for the classifier eval harness.

The renderer in :mod:`cli_eval` takes the dict produced here and turns
it into the terminal summary + JSON export. Keeping the metrics pure
(no IO, no storage, no API) means the eval harness's "change prompt,
run eval" cycle only depends on the speed of the Anthropic call and
the SQLite lookup — never on this module.

Alert-worthy semantics:
- ``alert_worthy_category_ids`` is the caller's choice. For opendata
  today that's ``{cost_complaint, self_host_intent, competitor_pain}``.
- Alert-worthy *accuracy* is category-based: of the items whose human
  label is an alert-worthy category, what fraction did the model
  classify into the same category?
- Alert-worthy *precision / recall* is urgency-based: model and human
  both assigning urgency >= ``ALERT_URGENCY_THRESHOLD``.

Small-n tagging on per-category F1 is deliberate; it's how the reader
knows a 0.55 F1 on n=10 items is noise, not signal.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Any

from .config import CategoryConfig

ALERT_URGENCY_THRESHOLD = 7

# Small-n thresholds for the per-category F1 variance tags. The
# justification is simple: at n<12 a single mislabeled item shifts F1
# by more than 10 points on that category, so the metric is noise.
SMALL_N_THRESHOLD = 15
HIGH_VARIANCE_THRESHOLD = 12


@dataclass(frozen=True)
class EvalPair:
    """One row of the eval: the human label paired with what the
    classifier said for this item (or ``None`` on classification failure).

    ``source`` is carried so we can stratify by source in a later
    iteration; not used in the current metrics but cheap to thread.
    ``title`` / ``body`` are optional and only populated when the
    caller wants them for the JSON export's disagreement entries —
    metrics computation itself ignores them.
    """

    item_id: str
    label_category: str
    label_urgency: int
    model_category: str | None
    model_urgency: int | None
    source: str = ""
    title: str = ""
    body: str = ""


def compute_metrics(
    pairs: list[EvalPair],
    categories: CategoryConfig,
    alert_worthy_category_ids: set[str],
) -> dict[str, Any]:
    """Compute the full metrics bundle.

    Returns a plain dict (JSON-serializable), rendered by cli_eval.
    Failures (classification returned no category) are counted
    separately and excluded from every per-category / accuracy
    computation — they're a real thing that happens at small eval
    sizes and silently dropping them would misattribute precision.
    """
    succeeded = [p for p in pairs if p.model_category is not None]
    failed = [p for p in pairs if p.model_category is None]

    category_ids = [c.id for c in categories.categories]

    overall = _overall_accuracy(succeeded)
    alert_worthy_acc = _alert_worthy_accuracy(succeeded, alert_worthy_category_ids)
    per_category = _per_category_prf1(succeeded, category_ids)
    urgency = _urgency_stats(succeeded, categories)
    alert_worthy_pr = _alert_worthy_precision_recall(succeeded)
    confusion = _confusion_matrix_3x3(succeeded, alert_worthy_category_ids)

    return {
        "total_labeled": len(pairs),
        "classified": len(succeeded),
        "classification_failures": len(failed),
        "failed_item_ids": [p.item_id for p in failed],
        "overall_accuracy": overall,
        "alert_worthy_accuracy": alert_worthy_acc,
        "per_category": per_category,
        "urgency": urgency,
        "alert_worthy_precision_recall": alert_worthy_pr,
        "confusion_matrix": confusion,
    }


def stop_criteria(
    metrics: dict[str, Any],
    alert_worthy_category_ids: set[str],
    *,
    cost_per_classification_usd: float | None = None,
) -> list[dict[str, Any]]:
    """Return a list of {name, target, current, met} for the stop gate.

    ``cost_per_classification_usd`` is optional because the harness can
    compute it separately from token counts and pass it in; passing
    ``None`` skips the cost-threshold row.
    """
    alert_pr = metrics["alert_worthy_precision_recall"]

    items: list[dict[str, Any]] = [
        {
            "name": "Alert-worthy precision >= 0.75",
            "target": 0.75,
            "current": alert_pr["precision"],
            "met": alert_pr["precision"] >= 0.75,
        },
        {
            "name": "Alert-worthy recall >= 0.75",
            "target": 0.75,
            "current": alert_pr["recall"],
            "met": alert_pr["recall"] >= 0.75,
        },
    ]

    # "No alert-worthy category below 0.75 F1" — identify the worst
    # offender so the user sees which category is dragging.
    worst_cat: str | None = None
    worst_f1 = 1.0
    for cat_id in alert_worthy_category_ids:
        stats = metrics["per_category"].get(cat_id)
        if stats is None or stats["n"] == 0:
            continue
        if stats["f1"] < worst_f1:
            worst_f1 = stats["f1"]
            worst_cat = cat_id
    items.append(
        {
            "name": "No alert-worthy category below 0.75 F1",
            "target": 0.75,
            "current": worst_f1 if worst_cat is not None else 1.0,
            "worst_category": worst_cat,
            "met": worst_cat is None or worst_f1 >= 0.75,
        }
    )

    if cost_per_classification_usd is not None:
        items.append(
            {
                "name": "Cost per classification < $0.001",
                "target": 0.001,
                "current": cost_per_classification_usd,
                # Lower is better — the only "lower-is-better" row.
                "met": cost_per_classification_usd < 0.001,
            }
        )

    return items


def stabilization_check(
    current: dict[str, Any],
    previous: dict[str, Any] | None,
    *,
    tolerance: float = 0.03,
) -> dict[str, Any] | None:
    """If we have a prior run's metrics, check whether the key headline
    numbers moved by less than ``tolerance``. Returns a dict
    summarizing; returns ``None`` when there's no prior run.

    "3 points" (tolerance=0.03) means different things on different
    scales: ±0.03 on a 0-1 ratio, ±0.3 on a 0-10 MAE. Each field's
    getter reports a delta already normalized to the 0-1 scale so a
    uniform tolerance applies.
    """
    if previous is None:
        return None

    # Each entry is (field_name, delta_getter). Delta getters return the
    # absolute difference already normalized to a 0-1 scale where
    # "tolerance" is interpretable as percentage points.
    def _scaled_mae_delta(m_a: dict[str, Any], m_b: dict[str, Any]) -> float:
        return abs(m_a["urgency"]["overall_mae"] - m_b["urgency"]["overall_mae"]) / 10.0

    fields = [
        (
            "overall_accuracy",
            lambda a, b: abs(a["overall_accuracy"]["accuracy"] - b["overall_accuracy"]["accuracy"]),
        ),
        (
            "alert_worthy_accuracy",
            lambda a, b: abs(
                a["alert_worthy_accuracy"]["accuracy"] - b["alert_worthy_accuracy"]["accuracy"]
            ),
        ),
        (
            "alert_worthy_precision",
            lambda a, b: abs(
                a["alert_worthy_precision_recall"]["precision"]
                - b["alert_worthy_precision_recall"]["precision"]
            ),
        ),
        (
            "alert_worthy_recall",
            lambda a, b: abs(
                a["alert_worthy_precision_recall"]["recall"]
                - b["alert_worthy_precision_recall"]["recall"]
            ),
        ),
        ("urgency_mae_scaled", _scaled_mae_delta),
    ]
    deltas: dict[str, float] = {}
    for name, compute in fields:
        try:
            deltas[name] = compute(current, previous)
        except (KeyError, TypeError):
            deltas[name] = float("nan")
    stable = all(d <= tolerance for d in deltas.values() if d == d)  # NaN excluded
    return {"stable": stable, "deltas": deltas, "tolerance": tolerance}


# --- per-metric helpers --------------------------------------------------


def _overall_accuracy(succeeded: list[EvalPair]) -> dict[str, Any]:
    matched = sum(1 for p in succeeded if p.model_category == p.label_category)
    total = len(succeeded)
    return {
        "matched": matched,
        "total": total,
        "accuracy": _safe_div(matched, total),
    }


def _alert_worthy_accuracy(
    succeeded: list[EvalPair],
    alert_worthy_ids: set[str],
) -> dict[str, Any]:
    subset = [p for p in succeeded if p.label_category in alert_worthy_ids]
    matched = sum(1 for p in subset if p.model_category == p.label_category)
    total = len(subset)
    return {
        "matched": matched,
        "total": total,
        "accuracy": _safe_div(matched, total),
    }


def _per_category_prf1(
    succeeded: list[EvalPair],
    category_ids: list[str],
) -> dict[str, dict[str, Any]]:
    tp: Counter[str] = Counter()
    fp: Counter[str] = Counter()
    fn: Counter[str] = Counter()
    support: Counter[str] = Counter()
    for p in succeeded:
        support[p.label_category] += 1
        if p.model_category == p.label_category:
            tp[p.label_category] += 1
        else:
            fp[p.model_category] += 1  # type: ignore[index]
            fn[p.label_category] += 1

    out: dict[str, dict[str, Any]] = {}
    for cid in category_ids:
        t = tp[cid]
        p_denom = t + fp[cid]
        r_denom = t + fn[cid]
        precision = _safe_div(t, p_denom)
        recall = _safe_div(t, r_denom)
        f1 = 0.0 if precision + recall == 0 else 2 * precision * recall / (precision + recall)
        n = support[cid]
        out[cid] = {
            "precision": precision,
            "recall": recall,
            "f1": f1,
            "n": n,
            "variance_tag": _variance_tag(n),
        }
    return out


def _variance_tag(n: int) -> str | None:
    if n >= SMALL_N_THRESHOLD:
        return None
    if n < HIGH_VARIANCE_THRESHOLD:
        return "small-n, high variance"
    return "small-n"


def _urgency_stats(
    succeeded: list[EvalPair],
    categories: CategoryConfig,
) -> dict[str, Any]:
    if not succeeded:
        return {
            "overall_mae": 0.0,
            "high_urgency_mae": 0.0,
            "band_accuracy": 0.0,
        }

    errors = [
        abs(p.model_urgency - p.label_urgency)  # type: ignore[operator]
        for p in succeeded
        if p.model_urgency is not None
    ]
    overall_mae = _mean(errors)

    high_errors = [
        abs(p.model_urgency - p.label_urgency)  # type: ignore[operator]
        for p in succeeded
        if p.model_urgency is not None and p.label_urgency >= ALERT_URGENCY_THRESHOLD
    ]
    high_mae = _mean(high_errors)

    band_matches = 0
    band_total = 0
    for p in succeeded:
        if p.model_urgency is None:
            continue
        band_total += 1
        if _same_band(p.model_urgency, p.label_urgency, categories):
            band_matches += 1
    band_acc = _safe_div(band_matches, band_total)

    return {
        "overall_mae": overall_mae,
        "high_urgency_mae": high_mae,
        "band_accuracy": band_acc,
        "high_urgency_n": len(high_errors),
    }


def _same_band(a: int, b: int, categories: CategoryConfig) -> bool:
    for band in categories.urgency_scale:
        lo, hi = band.range[0], band.range[1]
        if lo <= a <= hi and lo <= b <= hi:
            return True
    return False


def _alert_worthy_precision_recall(succeeded: list[EvalPair]) -> dict[str, Any]:
    human_alert = [p for p in succeeded if p.label_urgency >= ALERT_URGENCY_THRESHOLD]
    model_alert = [
        p
        for p in succeeded
        if p.model_urgency is not None and p.model_urgency >= ALERT_URGENCY_THRESHOLD
    ]
    both = [p for p in model_alert if p.label_urgency >= ALERT_URGENCY_THRESHOLD]
    return {
        "precision": _safe_div(len(both), len(model_alert)),
        "recall": _safe_div(len(both), len(human_alert)),
        "n_human_alert": len(human_alert),
        "n_model_alert": len(model_alert),
        "n_both": len(both),
    }


def _confusion_matrix_3x3(
    succeeded: list[EvalPair],
    alert_worthy_ids: set[str],
) -> dict[str, Any]:
    """Actual (rows) x predicted (cols), restricted to alert-worthy
    categories plus a single catch-all ``other`` column.

    The "rows are humans, columns are models" convention makes it easy
    to read at a glance: "of items the human called X, how did the
    model distribute them?" → read along the row.
    """
    alert_list = sorted(alert_worthy_ids)
    matrix: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for p in succeeded:
        if p.label_category not in alert_worthy_ids:
            continue
        col = p.model_category if p.model_category in alert_worthy_ids else "other"
        matrix[p.label_category][col] += 1

    # Materialize with deterministic row/column order and zeros filled in.
    cols = [*alert_list, "other"]
    rendered: dict[str, dict[str, int]] = {}
    for row in alert_list:
        rendered[row] = {c: int(matrix[row].get(c, 0)) for c in cols}
    return {"rows": alert_list, "cols": cols, "counts": rendered}


# --- tiny numeric helpers -----------------------------------------------


def _safe_div(n: float, d: float) -> float:
    return float(n) / float(d) if d else 0.0


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0
