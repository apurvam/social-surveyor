"""`social-surveyor stats` — one-screen DB summary.

Kept as a separate module so the triage and label CLIs can reuse the
same query-grouping helpers without pulling in the main cli.py.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import typer

from .labeling import count_labeled_ids, labels_path
from .storage import Storage


def _fmt_size(path: Path) -> str:
    try:
        n = path.stat().st_size
    except FileNotFoundError:
        return "0 B"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}".replace(".0 ", " ")
        n /= 1024
    return f"{n:.1f} TB"


def run_stats(project: str, db_path: Path, projects_root: Path) -> str:
    """Return the stats screen as a single string.

    Side-effect-free so tests can call it directly and assert on the
    output; :func:`stats_command` is the CLI wrapper.
    """
    if not db_path.is_file():
        raise typer.BadParameter(
            f"no DB at {db_path} yet — run `social-surveyor poll --project {project}` first"
        )

    now = datetime.now(UTC)
    since_1d = now - timedelta(days=1)
    since_7d = now - timedelta(days=7)

    with Storage(db_path) as db:
        by_source_all = db.count_items_by_window()
        by_source_1d = db.count_items_by_window(since=since_1d)
        by_source_7d = db.count_items_by_window(since=since_7d)
        groups = db.count_items_by_group(since=since_7d)
        all_ids = db.list_item_ids()

    labels_file = labels_path(project, projects_root)
    labeled_count = count_labeled_ids(labels_file)
    total_items = sum(by_source_all.values())
    unlabeled_count = max(0, total_items - labeled_count)

    lines: list[str] = []
    lines.append(f"Project: {project}")
    lines.append(f"Database: {db_path} ({_fmt_size(db_path)})")
    lines.append("")
    lines.append("Items:")
    lines.append(f"  {'source':<14}{'total':>8}{'last 24h':>12}{'last 7d':>10}")
    for source in sorted(by_source_all):
        lines.append(
            f"  {source:<14}"
            f"{by_source_all.get(source, 0):>8}"
            f"{by_source_1d.get(source, 0):>12}"
            f"{by_source_7d.get(source, 0):>10}"
        )
    lines.append(f"  {'-' * 44}")
    lines.append(
        f"  {'TOTAL':<14}"
        f"{total_items:>8}"
        f"{sum(by_source_1d.values()):>12}"
        f"{sum(by_source_7d.values()):>10}"
    )
    lines.append("")
    lines.append("Queries (top 10 by volume, last 7d):")
    top = groups[:10]
    max_label = max((len(g) for g, _ in top), default=20)
    max_label = max(max_label, 20)
    for group_key, count in top:
        lines.append(f"  {group_key:<{max_label + 2}}{count:>6}")
    if not top:
        lines.append("  (no items in the last 7 days)")
    lines.append("")
    lines.append("Labels:")
    lines.append(f"  {'labeled':>10}{'unlabeled':>14}")
    lines.append(f"  {labeled_count:>10}{unlabeled_count:>14}")
    if labels_file.exists():
        lines.append(f"  file: {labels_file}")
    else:
        lines.append(f"  (no labels file yet — run `social-surveyor label --project {project}`)")
    # Hint on the unknown bucket if it's the top group
    if top and top[0][0] == Storage._UNKNOWN_GROUP:
        lines.append("")
        lines.append(
            "Note: (unknown query) is the top bucket. These are pre-group_key "
            "items from earlier polls; they age out as new items come in."
        )
    _ = all_ids  # for future inspection; not printed

    return "\n".join(lines)
