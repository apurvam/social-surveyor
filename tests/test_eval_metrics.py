from __future__ import annotations

from social_surveyor.config import CategoryConfig
from social_surveyor.eval_metrics import (
    EvalPair,
    compute_metrics,
    stabilization_check,
    stop_criteria,
)


def _cats() -> CategoryConfig:
    """The opendata taxonomy, inlined."""
    return CategoryConfig.model_validate(
        {
            "version": 1,
            "categories": [
                {"id": "cost_complaint", "label": "Cost", "description": "Cost."},
                {
                    "id": "self_host_intent",
                    "label": "Self-host",
                    "description": "Self-host.",
                },
                {
                    "id": "competitor_pain",
                    "label": "Competitor",
                    "description": "Pain.",
                },
                {
                    "id": "neutral_discussion",
                    "label": "Neutral",
                    "description": "Neutral.",
                },
                {
                    "id": "tutorial_or_marketing",
                    "label": "Tutorial",
                    "description": "Tutorial.",
                },
                {"id": "off_topic", "label": "Off", "description": "Off."},
            ],
            "urgency_scale": [
                {"range": [0, 3], "meaning": "Irrelevant"},
                {"range": [4, 6], "meaning": "Relevant"},
                {"range": [7, 8], "meaning": "Good"},
                {"range": [9, 10], "meaning": "Urgent"},
            ],
        }
    )


_ALERT_WORTHY = {"cost_complaint", "self_host_intent", "competitor_pain"}


def _pair(
    label_cat: str,
    label_u: int,
    model_cat: str | None,
    model_u: int | None,
    *,
    item_id: str = "hackernews:1",
    source: str = "hackernews",
) -> EvalPair:
    return EvalPair(
        item_id=item_id,
        label_category=label_cat,
        label_urgency=label_u,
        model_category=model_cat,
        model_urgency=model_u,
        source=source,
    )


# --- compute_metrics ------------------------------------------------------


def test_empty_pairs_does_not_divide_by_zero() -> None:
    m = compute_metrics([], _cats(), _ALERT_WORTHY)
    assert m["overall_accuracy"]["accuracy"] == 0.0
    assert m["urgency"]["overall_mae"] == 0.0
    assert m["alert_worthy_precision_recall"]["precision"] == 0.0


def test_all_correct() -> None:
    pairs = [
        _pair("cost_complaint", 8, "cost_complaint", 8),
        _pair("off_topic", 1, "off_topic", 1),
    ]
    m = compute_metrics(pairs, _cats(), _ALERT_WORTHY)
    assert m["overall_accuracy"]["accuracy"] == 1.0
    assert m["per_category"]["cost_complaint"]["f1"] == 1.0
    assert m["urgency"]["overall_mae"] == 0.0


def test_all_disagree() -> None:
    pairs = [
        _pair("cost_complaint", 8, "off_topic", 0),
        _pair("self_host_intent", 7, "off_topic", 0),
    ]
    m = compute_metrics(pairs, _cats(), _ALERT_WORTHY)
    assert m["overall_accuracy"]["accuracy"] == 0.0
    # cost_complaint has label support but no true positives → P/R/F1 all 0
    cc = m["per_category"]["cost_complaint"]
    assert cc["precision"] == 0.0
    assert cc["recall"] == 0.0
    assert cc["f1"] == 0.0
    assert cc["n"] == 1
    # off_topic has no label support → n=0, recall is 0, precision is 0
    # (no TP because we require label_cat==model_cat==c).
    assert m["per_category"]["off_topic"]["n"] == 0


def test_classification_failures_are_excluded_from_metrics() -> None:
    pairs = [
        _pair("cost_complaint", 8, "cost_complaint", 8),
        _pair("self_host_intent", 7, None, None),  # failure
        _pair("off_topic", 1, "off_topic", 1),
    ]
    m = compute_metrics(pairs, _cats(), _ALERT_WORTHY)
    # 3 labeled, 2 classified, 1 failure.
    assert m["total_labeled"] == 3
    assert m["classified"] == 2
    assert m["classification_failures"] == 1
    # Accuracy is over classified only (2/2), not 2/3.
    assert m["overall_accuracy"]["total"] == 2
    assert m["overall_accuracy"]["accuracy"] == 1.0


def test_alert_worthy_accuracy_filters_on_human_label() -> None:
    # 3 alert-worthy-labeled items, model gets 2 right.
    pairs = [
        _pair("cost_complaint", 8, "cost_complaint", 8),
        _pair("cost_complaint", 8, "off_topic", 0),
        _pair("self_host_intent", 7, "self_host_intent", 7),
        # Off-topic items don't factor into alert-worthy accuracy.
        _pair("off_topic", 1, "cost_complaint", 8),
    ]
    m = compute_metrics(pairs, _cats(), _ALERT_WORTHY)
    alert = m["alert_worthy_accuracy"]
    assert alert["total"] == 3
    assert alert["matched"] == 2
    assert abs(alert["accuracy"] - 2 / 3) < 1e-9


def test_per_category_prf1_standard_case() -> None:
    # cost_complaint: 4 labeled, model got 3 right, 1 wrong.
    # model also labeled 1 off_topic item as cost_complaint → FP.
    pairs = [
        _pair("cost_complaint", 8, "cost_complaint", 8, item_id="a"),
        _pair("cost_complaint", 8, "cost_complaint", 8, item_id="b"),
        _pair("cost_complaint", 8, "cost_complaint", 8, item_id="c"),
        _pair("cost_complaint", 8, "off_topic", 0, item_id="d"),
        _pair("off_topic", 1, "cost_complaint", 8, item_id="e"),
    ]
    m = compute_metrics(pairs, _cats(), _ALERT_WORTHY)
    cc = m["per_category"]["cost_complaint"]
    # TP=3, FP=1, FN=1.
    assert cc["n"] == 4
    assert abs(cc["precision"] - 3 / 4) < 1e-9
    assert abs(cc["recall"] - 3 / 4) < 1e-9
    assert abs(cc["f1"] - 0.75) < 1e-9


def test_variance_tags() -> None:
    # 14 items in cost_complaint → n<15 but >=12 → "small-n"
    # 10 items in competitor_pain → n<12 → "small-n, high variance"
    # 20 items in off_topic → no tag (None)
    pairs = []
    for i in range(14):
        pairs.append(
            _pair(
                "cost_complaint",
                8,
                "cost_complaint",
                8,
                item_id=f"cc:{i}",
            )
        )
    for i in range(10):
        pairs.append(
            _pair(
                "competitor_pain",
                8,
                "competitor_pain",
                8,
                item_id=f"cp:{i}",
            )
        )
    for i in range(20):
        pairs.append(_pair("off_topic", 1, "off_topic", 1, item_id=f"ot:{i}"))
    m = compute_metrics(pairs, _cats(), _ALERT_WORTHY)
    assert m["per_category"]["cost_complaint"]["variance_tag"] == "small-n"
    assert m["per_category"]["competitor_pain"]["variance_tag"] == "small-n, high variance"
    assert m["per_category"]["off_topic"]["variance_tag"] is None


# --- urgency --------------------------------------------------------------


def test_urgency_mae_overall_vs_high_urgency() -> None:
    # Two items at label=8 (high-urgency), each off by 2 → high MAE=2.
    # One item at label=2, model=2 → overall MAE is (2+2+0)/3 = ~1.33.
    pairs = [
        _pair("cost_complaint", 8, "cost_complaint", 6),
        _pair("cost_complaint", 8, "cost_complaint", 10),
        _pair("off_topic", 2, "off_topic", 2),
    ]
    m = compute_metrics(pairs, _cats(), _ALERT_WORTHY)
    u = m["urgency"]
    assert abs(u["overall_mae"] - (2 + 2 + 0) / 3) < 1e-9
    assert abs(u["high_urgency_mae"] - 2.0) < 1e-9
    assert u["high_urgency_n"] == 2


def test_urgency_band_accuracy() -> None:
    # Bands: 0-3, 4-6, 7-8, 9-10.
    pairs = [
        _pair("cost_complaint", 8, "cost_complaint", 7),  # both band 7-8 ✓
        _pair("cost_complaint", 2, "cost_complaint", 5),  # 0-3 vs 4-6 ✗
        _pair("off_topic", 10, "off_topic", 9),  # both 9-10 ✓
    ]
    m = compute_metrics(pairs, _cats(), _ALERT_WORTHY)
    assert abs(m["urgency"]["band_accuracy"] - 2 / 3) < 1e-9


def test_alert_worthy_precision_recall_urgency_based() -> None:
    # Human says urgency>=7 on 3 items; model says on 4.
    # Of model's 4, 2 also have human>=7 → precision 0.5.
    # Of human's 3, 2 match → recall 2/3.
    pairs = [
        _pair("cost_complaint", 8, "cost_complaint", 8),  # both ≥7
        _pair("self_host_intent", 7, "self_host_intent", 9),  # both ≥7
        _pair("competitor_pain", 9, "competitor_pain", 5),  # human≥7, model<7
        _pair("off_topic", 2, "off_topic", 7),  # model≥7, human<7
        _pair("off_topic", 1, "off_topic", 7),  # model≥7, human<7
        _pair("off_topic", 1, "off_topic", 3),  # neither
    ]
    m = compute_metrics(pairs, _cats(), _ALERT_WORTHY)
    pr = m["alert_worthy_precision_recall"]
    assert pr["n_human_alert"] == 3
    assert pr["n_model_alert"] == 4
    assert pr["n_both"] == 2
    assert abs(pr["precision"] - 0.5) < 1e-9
    assert abs(pr["recall"] - 2 / 3) < 1e-9


# --- confusion matrix -----------------------------------------------------


def test_confusion_matrix_shape_and_counts() -> None:
    pairs = [
        # cost → cost, cost, self
        _pair("cost_complaint", 8, "cost_complaint", 8, item_id="a"),
        _pair("cost_complaint", 8, "cost_complaint", 8, item_id="b"),
        _pair("cost_complaint", 8, "self_host_intent", 7, item_id="c"),
        # self → self, competitor, off_topic (→"other")
        _pair("self_host_intent", 7, "self_host_intent", 7, item_id="d"),
        _pair("self_host_intent", 7, "competitor_pain", 8, item_id="e"),
        _pair("self_host_intent", 7, "off_topic", 0, item_id="f"),
        # Non-alert-worthy rows are excluded.
        _pair("off_topic", 1, "cost_complaint", 8, item_id="g"),
    ]
    m = compute_metrics(pairs, _cats(), _ALERT_WORTHY)
    cm = m["confusion_matrix"]
    assert cm["rows"] == ["competitor_pain", "cost_complaint", "self_host_intent"]
    assert cm["cols"] == [
        "competitor_pain",
        "cost_complaint",
        "self_host_intent",
        "other",
    ]
    counts = cm["counts"]
    assert counts["cost_complaint"] == {
        "competitor_pain": 0,
        "cost_complaint": 2,
        "self_host_intent": 1,
        "other": 0,
    }
    assert counts["self_host_intent"] == {
        "competitor_pain": 1,
        "cost_complaint": 0,
        "self_host_intent": 1,
        "other": 1,
    }
    # competitor_pain row has no labeled items → all zeros.
    assert counts["competitor_pain"] == {
        "competitor_pain": 0,
        "cost_complaint": 0,
        "self_host_intent": 0,
        "other": 0,
    }


# --- stop_criteria --------------------------------------------------------


def test_stop_criteria_all_met() -> None:
    m = {
        "per_category": {
            "cost_complaint": {"f1": 0.80, "n": 20},
            "self_host_intent": {"f1": 0.82, "n": 20},
            "competitor_pain": {"f1": 0.76, "n": 20},
        },
        "alert_worthy_precision_recall": {"precision": 0.80, "recall": 0.78},
    }
    criteria = stop_criteria(m, _ALERT_WORTHY, cost_per_classification_usd=0.0005)
    assert all(c["met"] for c in criteria)


def test_stop_criteria_identifies_worst_category() -> None:
    m = {
        "per_category": {
            "cost_complaint": {"f1": 0.90, "n": 20},
            "self_host_intent": {"f1": 0.55, "n": 10},
            "competitor_pain": {"f1": 0.80, "n": 15},
        },
        "alert_worthy_precision_recall": {"precision": 0.80, "recall": 0.80},
    }
    criteria = stop_criteria(m, _ALERT_WORTHY)
    worst_row = next(c for c in criteria if c["name"].startswith("No alert-worthy category"))
    assert worst_row["worst_category"] == "self_host_intent"
    assert worst_row["met"] is False


def test_stop_criteria_ignores_zero_n_categories() -> None:
    # If a category has no labeled items its F1 is meaningless and
    # shouldn't drag the "no category below 0.75" check.
    m = {
        "per_category": {
            "cost_complaint": {"f1": 0.90, "n": 20},
            "self_host_intent": {"f1": 0.0, "n": 0},  # no data
            "competitor_pain": {"f1": 0.80, "n": 15},
        },
        "alert_worthy_precision_recall": {"precision": 0.80, "recall": 0.80},
    }
    criteria = stop_criteria(m, _ALERT_WORTHY)
    worst_row = next(c for c in criteria if c["name"].startswith("No alert-worthy category"))
    assert worst_row["met"] is True


def test_stop_criteria_omits_cost_row_when_unspecified() -> None:
    m = {
        "per_category": {
            "cost_complaint": {"f1": 0.80, "n": 20},
            "self_host_intent": {"f1": 0.80, "n": 20},
            "competitor_pain": {"f1": 0.80, "n": 20},
        },
        "alert_worthy_precision_recall": {"precision": 0.80, "recall": 0.80},
    }
    criteria = stop_criteria(m, _ALERT_WORTHY)
    assert not any(c["name"].startswith("Cost per") for c in criteria)


# --- stabilization_check ---------------------------------------------------


def test_stabilization_returns_none_without_previous() -> None:
    assert stabilization_check({}, None) is None


def test_stabilization_reports_stable_when_deltas_under_tolerance() -> None:
    current = _metrics_shell(acc=0.80, alert_acc=0.70, p=0.75, r=0.73, mae=1.4)
    previous = _metrics_shell(acc=0.79, alert_acc=0.69, p=0.76, r=0.72, mae=1.5)
    result = stabilization_check(current, previous, tolerance=0.03)
    assert result is not None
    assert result["stable"] is True


def test_stabilization_reports_moving_when_any_delta_exceeds_tolerance() -> None:
    current = _metrics_shell(acc=0.80, alert_acc=0.70, p=0.75, r=0.73, mae=1.4)
    previous = _metrics_shell(acc=0.60, alert_acc=0.69, p=0.76, r=0.72, mae=1.5)
    result = stabilization_check(current, previous, tolerance=0.03)
    assert result is not None
    assert result["stable"] is False
    assert result["deltas"]["overall_accuracy"] > 0.03


def _metrics_shell(
    *,
    acc: float,
    alert_acc: float,
    p: float,
    r: float,
    mae: float,
) -> dict:
    return {
        "overall_accuracy": {"accuracy": acc},
        "alert_worthy_accuracy": {"accuracy": alert_acc},
        "alert_worthy_precision_recall": {"precision": p, "recall": r},
        "urgency": {"overall_mae": mae},
    }
