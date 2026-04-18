"""`social-surveyor triage` — per-query-group signal/noise decisions.

Walks through each configured query's recent items, prompts the
operator for a keep/drop/refine/skip decision, and writes a Markdown
report with YAML-diff suggestions at the end. The tool never auto-
rewrites source YAML — that's the operator's call after reviewing the
report.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import typer

from .storage import Storage

# Decision sentinel strings are stored verbatim in the report, so keep
# them short-and-readable rather than enum values.
KEEP = "keep"
DROP = "drop"
REFINE = "refine"
SKIP = "skip"

_PROMPT = "[k]eep / [d]rop / [r]efine / [s]kip / [v]iew more / [q]uit: "

_KEY_TO_DECISION = {"k": KEEP, "d": DROP, "r": REFINE, "s": SKIP}


@dataclass
class Decision:
    group_key: str
    decision: str
    item_count: int
    sample_titles: list[str] = field(default_factory=list)


def _is_unknown_bucket(group_key: str) -> bool:
    """The ``(unknown query)`` bucket maps back to no real source config."""
    return group_key == Storage._UNKNOWN_GROUP


def _parse_group_key(group_key: str) -> tuple[str, str | None, str]:
    """Split ``source:{suffix}`` into (source, subreddit?, query).

    Reddit's suffix is ``r/{subreddit}/{query}`` — we surface subreddit
    separately so the report can group DROP decisions by subreddit.
    """
    source, _, rest = group_key.partition(":")
    if source == "reddit" and rest.startswith("r/"):
        # rest = "r/{subreddit}/{query}"
        parts = rest.split("/", 2)
        if len(parts) == 3:
            _, subreddit, query = parts
            return source, subreddit, query
    return source, None, rest


def _render_group(
    group_key: str,
    items: list[dict[str, Any]],
    total: int,
    days: int,
    *,
    index: int,
    total_groups: int,
) -> str:
    per_day = total / max(1, days)
    lines: list[str] = [
        "",
        f"=== [{index}/{total_groups}] {group_key} ===",
        f"    total in window: {total}  ({per_day:.1f}/day over {days}d)",
        "",
    ]
    for i, item in enumerate(items, start=1):
        title = (item.get("title") or "(no title)").replace("\n", " ")
        body = (item.get("body") or "").replace("\n", " ").strip()
        preview = body[:120]
        if len(body) > 120:
            preview += "…"
        lines.append(f"  {i}. {title[:100]}")
        if preview:
            lines.append(f"       {preview}")
    lines.append("")
    return "\n".join(lines)


def run_triage(
    project: str,
    db_path: Path,
    projects_root: Path,
    *,
    source_filter: str | None,
    limit: int,
    window_days: int = 30,
    input_fn=input,
    echo_fn=typer.echo,
    now: datetime | None = None,
) -> Path:
    """Run the triage loop and write a Markdown report.

    Returns the path to the generated report file.
    """
    if not db_path.is_file():
        raise typer.BadParameter(f"no DB at {db_path} yet — run a poll first")

    now = now or datetime.now(UTC)
    window_start = now - timedelta(days=window_days)

    with Storage(db_path) as db:
        groups = db.count_items_by_group(since=window_start)

        relevant_groups = [
            (g, c) for g, c in groups if source_filter is None or g.startswith(f"{source_filter}:")
        ]
        if not relevant_groups:
            echo_fn(f"no groups found in the last {window_days} days; nothing to triage.")
            return _empty_report_path(project, projects_root, now)

        echo_fn(
            f"triaging {len(relevant_groups)} query groups for '{project}' "
            f"(window: last {window_days} days, sample limit: {limit})"
        )

        decisions: list[Decision] = []
        total_groups = len(relevant_groups)
        for idx, (group_key, total) in enumerate(relevant_groups, start=1):
            offset = 0
            while True:
                sample = db.list_items_in_group(group_key, limit=limit, offset=offset)
                if not sample and offset == 0:
                    # Edge case: count nonzero but items vanished. Skip.
                    break
                if not sample:
                    echo_fn("  (no more items in this group)")
                    break

                echo_fn(
                    _render_group(
                        group_key,
                        sample,
                        total,
                        window_days,
                        index=idx,
                        total_groups=total_groups,
                    )
                )
                raw = input_fn(_PROMPT).strip().lower()
                if raw == "q":
                    decisions.append(Decision(group_key=group_key, decision=SKIP, item_count=total))
                    return _write_report(project, projects_root, now, decisions, aborted=True)
                if raw == "v":
                    offset += limit
                    continue
                decision = _KEY_TO_DECISION.get(raw)
                if decision is None:
                    echo_fn(f"  unknown choice {raw!r}; expected one of: k d r s v q")
                    continue

                decisions.append(
                    Decision(
                        group_key=group_key,
                        decision=decision,
                        item_count=total,
                        sample_titles=[
                            (it.get("title") or "(no title)").strip() for it in sample[:5]
                        ],
                    )
                )
                break

        # All groups reviewed without quit — surface the natural end
        # so the operator isn't surprised by a silent exit.
        echo_fn(
            f"\nsession complete — {len(decisions)} decision(s) across "
            f"{total_groups} group(s). writing report..."
        )

    return _write_report(project, projects_root, now, decisions, aborted=False)


def _empty_report_path(project: str, projects_root: Path, now: datetime) -> Path:
    out = _report_path(project, projects_root, now)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        f"# Triage Report — {now:%Y-%m-%d %H:%M}\n\n"
        f"No groups found in the window for project `{project}`.\n"
    )
    return out


def _report_path(project: str, projects_root: Path, now: datetime) -> Path:
    return projects_root / project / f"triage_{now:%Y%m%d_%H%M}.md"


def _write_report(
    project: str,
    projects_root: Path,
    now: datetime,
    decisions: list[Decision],
    *,
    aborted: bool,
) -> Path:
    out = _report_path(project, projects_root, now)
    out.parent.mkdir(parents=True, exist_ok=True)

    lines: list[str] = [f"# Triage Report — {now:%Y-%m-%d %H:%M}", ""]
    if aborted:
        lines.append(
            "> Session quit before every group was reviewed; "
            "groups not shown were implicitly skipped."
        )
        lines.append("")

    lines.append("## Decisions")
    lines.append("")
    if not decisions:
        lines.append("_no decisions recorded_")
    else:
        for d in decisions:
            verb = {
                KEEP: "KEEP",
                DROP: "DROP",
                REFINE: "REFINE",
                SKIP: "SKIP",
            }[d.decision]
            lines.append(f"- **{d.group_key}** — {verb} ({d.item_count} items in window)")
    lines.append("")

    lines.extend(_suggested_yaml_changes(decisions))
    lines.extend(_refine_samples(decisions))

    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out


def _suggested_yaml_changes(decisions: list[Decision]) -> list[str]:
    """Emit per-source YAML-diff suggestions based on DROP decisions.

    The ``(unknown query)`` bucket is deliberately excluded here — it
    aggregates pre-``group_key`` items across every source, so there
    is no single YAML file the operator could edit in response.
    Decisions on that bucket still appear in the Decisions summary
    above; they just don't produce a config-edit recommendation.
    """
    drops_by_source: dict[str, list[Decision]] = {}
    refines_by_source: dict[str, list[Decision]] = {}
    for d in decisions:
        if d.decision not in {DROP, REFINE}:
            continue
        if _is_unknown_bucket(d.group_key):
            continue
        source, _, _ = d.group_key.partition(":")
        bucket = drops_by_source if d.decision == DROP else refines_by_source
        bucket.setdefault(source, []).append(d)

    if not drops_by_source and not refines_by_source:
        return []

    out: list[str] = ["## Suggested YAML changes", ""]

    for source, drops in drops_by_source.items():
        out.append(f"### `projects/<project>/sources/{source}.yaml` — DROP")
        out.append("")
        if source == "reddit":
            dropped_subs = sorted({_parse_group_key(d.group_key)[1] or "?" for d in drops})
            dropped_qs = sorted({_parse_group_key(d.group_key)[2] for d in drops})
            out.append("Consider removing these subreddit / query pairs:")
            out.append("```")
            for d in drops:
                _, sub, q = _parse_group_key(d.group_key)
                out.append(f"- r/{sub}  query: {q}")
            out.append("```")
            out.append(
                f"If a subreddit's entire query set is DROP, drop the subreddit "
                f"entry in `subreddits:` too. Subreddits flagged: {', '.join(dropped_subs)}. "
                f"Queries flagged: {', '.join(dropped_qs)}."
            )
        else:
            out.append("Remove the following queries:")
            out.append("```")
            for d in drops:
                _, _, q = _parse_group_key(d.group_key)
                out.append(f"- {q}")
            out.append("```")
        out.append("")

    for source, refines in refines_by_source.items():
        out.append(f"### `projects/<project>/sources/{source}.yaml` — REFINE")
        out.append("")
        out.append("The following queries need manual refinement (see samples below):")
        out.append("```")
        for d in refines:
            _, sub, q = _parse_group_key(d.group_key)
            if source == "reddit":
                out.append(f"- r/{sub}  query: {q}")
            else:
                out.append(f"- {q}")
        out.append("```")
        out.append(
            "For GitHub/X, consider negative qualifiers (e.g. `-is:retweet`, "
            "`NOT hiring`, `NOT tutorial`). For Reddit, consider per-subreddit "
            "narrowing or switching to `/new.rss` if queries don't match."
        )
        out.append("")

    return out


def _refine_samples(decisions: list[Decision]) -> list[str]:
    refines = [
        d for d in decisions if d.decision == REFINE and not _is_unknown_bucket(d.group_key)
    ]
    if not refines:
        return []
    out: list[str] = ["## REFINE — sample titles for context", ""]
    for d in refines:
        out.append(f"### {d.group_key}")
        out.append("")
        for title in d.sample_titles:
            clean = re.sub(r"\s+", " ", title).strip()[:160]
            out.append(f"- {clean}")
        out.append("")
    return out
