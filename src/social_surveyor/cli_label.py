"""`social-surveyor label` — interactive labeling loop.

Reads `categories.yaml`, walks unlabeled items newest-first, and
appends one JSONL line per decision to `projects/<n>/evals/labeled.jsonl`.
Crash-safe (per-decision append), resume-default. Corrections land via
`label --item-id <id>` rather than a back-step — append-only with
latest-wins means you never destructively rewrite a prior label.
"""

from __future__ import annotations

import random
import time
from pathlib import Path
from typing import Any

import typer

from .config import CategoryConfig, load_categories
from .labeling import (
    LabelEntry,
    append_label,
    ensure_labels_file,
    iter_label_entries,
    labeled_ids,
    make_entry,
    resolve_effective_labels,
)
from .storage import Storage

# Keep the quit commands together so a reader can find them.
_QUIT = "q"
_SKIP = "s"


class _Session:
    """Mutable per-run state for the label loop.

    Tracks progress counters. Kept as a class so tests can drive the
    loop directly without going through Typer's prompt machinery.
    """

    def __init__(
        self,
        cfg: CategoryConfig,
        db: Storage,
        labels_file: Path,
        queue: list[tuple[str, str]],
    ) -> None:
        self.cfg = cfg
        self.db = db
        self.labels_file = labels_file
        self.queue = queue  # list of (source, platform_id)
        self.index = 0
        self.total = len(queue)
        self.skipped = 0
        self.labeled = 0
        self._start = time.monotonic()


def _build_queue(
    db: Storage,
    labels_file: Path,
    source: str | None,
    randomize: bool,
) -> list[tuple[str, str]]:
    already = labeled_ids(labels_file)
    all_ids = db.list_item_ids(source=source)
    queue: list[tuple[str, str]] = []
    for canonical in all_ids:
        if canonical in already:
            continue
        src, _, platform_id = canonical.partition(":")
        queue.append((src, platform_id))
    if randomize:
        random.shuffle(queue)
    return queue


def _build_reconsider_queue(
    labels_file: Path,
    *,
    category_filter: str | None,
    urgency_min: int | None,
    urgency_max: int | None,
    source_filter: str | None,
) -> list[tuple[str, str, LabelEntry]]:
    """Already-labeled items matching the filters, plus their effective
    (latest-wins) label.

    Used by ``label --reconsider`` to walk items through a sharpened
    taxonomy. Unlike ``_build_queue`` which excludes labeled items, this
    queue is specifically the labeled set — the point is to re-examine
    existing decisions against updated category definitions.

    The latest-wins logic means each item appears once even if it has
    been relabeled before; the user sees the current effective label
    and can append a new one (or keep).
    """
    latest_by_id = resolve_effective_labels(iter_label_entries(labels_file))

    queue: list[tuple[str, str, LabelEntry]] = []
    for item_id, label in latest_by_id.items():
        if category_filter is not None and label.category != category_filter:
            continue
        if urgency_min is not None and label.urgency < urgency_min:
            continue
        if urgency_max is not None and label.urgency > urgency_max:
            continue
        src, _, platform_id = item_id.partition(":")
        if source_filter is not None and src != source_filter:
            continue
        queue.append((src, platform_id, label))
    # Deterministic order: oldest-label-first is least surprising for a
    # review pass — users see items they labeled earliest (and where
    # their taxonomy calibration may have drifted most) first.
    queue.sort(key=lambda t: t[2].labeled_at)
    return queue


def _build_disagreement_queue(
    db: Storage,
    labels_file: Path,
    prompt_version: str,
    source: str | None,
) -> list[tuple[str, str]]:
    """Queue = labeled items whose classification (under ``prompt_version``)
    disagrees with the human category.

    Items without a classification under this version are excluded —
    ``--disagreements`` is specifically for re-examining items where
    both sides have already weighed in and disagreed. Classifying new
    items is the job of ``classify``.

    Applies latest-wins per item_id on the label side; anything the
    labeler touches in this loop appends a new entry that wins next
    time.
    """
    latest_by_id = resolve_effective_labels(iter_label_entries(labels_file))

    queue: list[tuple[str, str]] = []
    for item_id, label in latest_by_id.items():
        src, _, platform_id = item_id.partition(":")
        if source is not None and src != source:
            continue
        classification = db.get_classification(item_id, prompt_version)
        if classification is None:
            continue
        if classification["category"] == label.category:
            continue
        queue.append((src, platform_id))
    return queue


def _render_item(
    item: dict[str, Any],
    cfg: CategoryConfig,
    index: int,
    total: int,
) -> str:
    title = item.get("title") or "(no title)"
    author = item.get("author") or "(anonymous)"
    url = item.get("url") or "(no url)"
    created = item.get("created_at")
    created_str = created.isoformat() if hasattr(created, "isoformat") else str(created)
    body = item.get("body") or ""
    body_preview = body.strip()[:400]
    if len(body) > 400:
        body_preview += "…"

    lines: list[str] = []
    lines.append("")
    lines.append(f"[{index}/{total}]  {item['source']} — {created_str}")
    lines.append(f"Title:  {title}")
    lines.append(f"Author: {author}")
    lines.append(f"URL:    {url}")
    if body_preview:
        lines.append("")
        lines.append("Body:")
        for bline in body_preview.splitlines() or [body_preview]:
            lines.append(f"  {bline}")
    lines.append("")
    lines.append("Categories:")
    for i, cat in enumerate(cfg.categories, start=1):
        lines.append(f"  {i}) {cat.id}  —  {cat.label}")
    lines.append("")
    return "\n".join(lines)


def _render_item_with_current_label(
    item: dict[str, Any],
    label: LabelEntry,
    cfg: CategoryConfig,
    index: int,
    total: int,
) -> str:
    """Render shape used by --reconsider mode.

    Shows the current label prominently so the user can decide whether
    it still fits the (possibly updated) taxonomy, plus the full
    category list WITH descriptions so any taxonomy sharpening since
    the original label is visible.
    """
    title = item.get("title") or "(no title)"
    author = item.get("author") or "(anonymous)"
    url = item.get("url") or "(no url)"
    created = item.get("created_at")
    created_str = created.isoformat() if hasattr(created, "isoformat") else str(created)
    body = item.get("body") or ""
    body_preview = body.strip()[:400]
    if len(body) > 400:
        body_preview += "…"

    lines: list[str] = []
    lines.append("")
    lines.append(f"[{index}/{total}]  {item['source']} — {created_str}")
    lines.append(f"Title:  {title}")
    lines.append(f"Author: {author}")
    lines.append(f"URL:    {url}")
    if body_preview:
        lines.append("")
        lines.append("Body:")
        for bline in body_preview.splitlines() or [body_preview]:
            lines.append(f"  {bline}")
    lines.append("")
    lines.append(
        f"Current label: {label.category}  urgency={label.urgency}  "
        f"labeled_at={label.labeled_at.isoformat()}"
    )
    if label.note:
        lines.append(f"  note: {label.note}")
    lines.append("")
    lines.append("Categories (current definitions):")
    for i, cat in enumerate(cfg.categories, start=1):
        lines.append(f"  {i}) {cat.id}  —  {cat.label}")
        # Trim description to one line for the loop — the user can
        # consult categories.yaml for the full text when needed.
        desc = " ".join(cat.description.split())
        if desc:
            lines.append(f"       {desc}")
    lines.append("")
    return "\n".join(lines)


def _render_urgency_scale(cfg: CategoryConfig) -> str:
    lines = ["Urgency scale:"]
    for band in cfg.urgency_scale:
        lo, hi = band.range
        lines.append(f"  {lo}-{hi}: {band.meaning}")
    return "\n".join(lines)


def _resolve_category(raw: str, cfg: CategoryConfig) -> str | None:
    """Accept either a 1-based index or the literal category id."""
    raw = raw.strip().lower()
    if not raw:
        return None
    # Numeric shortcut.
    if raw.isdigit():
        idx = int(raw) - 1
        if 0 <= idx < len(cfg.categories):
            return cfg.categories[idx].id
        return None
    # Full id match.
    for cat in cfg.categories:
        if cat.id == raw:
            return cat.id
    return None


def _progress_line(session: _Session) -> str:
    elapsed = max(0.001, time.monotonic() - session._start)
    done = session.labeled + session.skipped
    per_item = elapsed / max(1, done)
    remaining_items = max(0, session.total - session.index)
    remaining_sec = int(per_item * remaining_items)
    mins, secs = divmod(remaining_sec, 60)
    return (
        f"progress: {session.labeled} labeled, {session.skipped} skipped, "
        f"{per_item:.1f}s/item, ~{mins}m{secs:02d}s remaining"
    )


def run_label(
    project: str,
    db_path: Path,
    projects_root: Path,
    *,
    source: str | None,
    randomize: bool,
    disagreements_for_version: str | None = None,
    reconsider: bool = False,
    reconsider_category: str | None = None,
    reconsider_urgency_min: int | None = None,
    reconsider_urgency_max: int | None = None,
    input_fn=input,
    echo_fn=typer.echo,
) -> dict[str, int]:
    """Run the interactive label loop and return a result counter.

    Three modes:

    - default: walk unlabeled items, record labels.
    - ``disagreements_for_version``: walk labeled items whose
      classification under that prompt_version disagrees with the human
      label.
    - ``reconsider=True``: walk already-labeled items (filtered by
      category/urgency) to re-examine them against the current taxonomy.
      Default action per item is keep-current; relabel requires an
      explicit category choice.

    ``input_fn`` and ``echo_fn`` are dependency-injected so tests can
    feed scripted input without touching a real terminal.
    """
    cfg = load_categories(project, projects_root=projects_root)
    labels_file = ensure_labels_file(project, projects_root=projects_root)

    if not db_path.is_file():
        raise typer.BadParameter(f"no DB at {db_path} yet — run a poll first")

    if reconsider and disagreements_for_version is not None:
        raise typer.BadParameter("--reconsider and --disagreements are mutually exclusive")

    with Storage(db_path) as db:
        if reconsider:
            return _run_reconsider(
                cfg=cfg,
                db=db,
                labels_file=labels_file,
                project=project,
                category_filter=reconsider_category,
                urgency_min=reconsider_urgency_min,
                urgency_max=reconsider_urgency_max,
                source_filter=source,
                input_fn=input_fn,
                echo_fn=echo_fn,
            )

        if disagreements_for_version is not None:
            queue = _build_disagreement_queue(db, labels_file, disagreements_for_version, source)
            mode_desc = f"disagreements vs prompt_version={disagreements_for_version!r}"
        else:
            queue = _build_queue(db, labels_file, source, randomize)
            mode_desc = "random" if randomize else "newest-first"
        session = _Session(cfg, db, labels_file, queue)

        echo_fn(f"labeling {session.total} items for project '{project}' ({mode_desc} order)")
        echo_fn(_render_urgency_scale(cfg))

        while session.index < session.total:
            src, platform_id = queue[session.index]
            item = db.get_item_by_id(src, platform_id)
            if item is None:
                # Item vanished (deleted from DB between queue build and now).
                session.index += 1
                continue

            if disagreements_for_version is not None:
                _echo_disagreement_context(
                    db,
                    f"{src}:{platform_id}",
                    disagreements_for_version,
                    echo_fn,
                )
            echo_fn(_render_item(item, cfg, session.index + 1, session.total))
            raw_cat = input_fn("Category (number or id, s=skip, q=quit): ").strip()

            if raw_cat == _QUIT:
                break
            if raw_cat == _SKIP:
                session.skipped += 1
                session.index += 1
                continue

            cat_id = _resolve_category(raw_cat, cfg)
            if cat_id is None:
                echo_fn("unknown category; try again")
                continue

            urgency = _prompt_urgency(input_fn, echo_fn)
            if urgency is None:
                # User abandoned urgency prompt → treat as skip.
                continue
            note = input_fn("Note (optional, empty to skip): ").strip() or None

            entry = make_entry(
                item_id=f"{src}:{platform_id}",
                category=cat_id,
                urgency=urgency,
                note=note,
            )
            append_label(labels_file, entry)
            session.labeled += 1
            session.index += 1
            echo_fn(_progress_line(session))

    return {
        "total": session.total,
        "labeled": session.labeled,
        "skipped": session.skipped,
        "remaining": max(0, session.total - session.index),
    }


def _run_reconsider(
    *,
    cfg: CategoryConfig,
    db: Storage,
    labels_file: Path,
    project: str,
    category_filter: str | None,
    urgency_min: int | None,
    urgency_max: int | None,
    source_filter: str | None,
    input_fn: Any,
    echo_fn: Any,
) -> dict[str, int]:
    """Guided relabel walkthrough.

    Default action per item is keep-current (Enter). Relabel requires
    typing a category (number or id); the user is then prompted for a
    new urgency (current as default) and optional note. ``b``ack is
    intentionally omitted in this mode — the semantics are ambiguous
    (pop the last appended relabel? rewind to a kept item?). Users can
    quit and re-run with different filters to reach any item.
    """
    queue = _build_reconsider_queue(
        labels_file,
        category_filter=category_filter,
        urgency_min=urgency_min,
        urgency_max=urgency_max,
        source_filter=source_filter,
    )
    total = len(queue)

    filter_bits: list[str] = []
    if category_filter is not None:
        filter_bits.append(f"category={category_filter!r}")
    if urgency_min is not None:
        filter_bits.append(f"urgency>={urgency_min}")
    if urgency_max is not None:
        filter_bits.append(f"urgency<={urgency_max}")
    if source_filter is not None:
        filter_bits.append(f"source={source_filter!r}")
    filter_desc = ", ".join(filter_bits) if filter_bits else "all labeled items"
    echo_fn(
        f"reconsidering {total} labeled items for project '{project}' "
        f"({filter_desc}, oldest-label-first)"
    )
    echo_fn(_render_urgency_scale(cfg))

    kept = 0
    relabeled = 0
    skipped = 0

    index = 0
    while index < total:
        src, platform_id, current = queue[index]
        item = db.get_item_by_id(src, platform_id)
        if item is None:
            # Item vanished since the queue was built; count as skip.
            skipped += 1
            index += 1
            continue

        echo_fn(_render_item_with_current_label(item, current, cfg, index + 1, total))
        raw = input_fn("Keep current [Enter] / Relabel (number or id) / [s]kip / [q]uit: ").strip()

        if raw == _QUIT:
            break
        if raw == "" or raw == _SKIP:
            # Enter and s are equivalent — both mean "leave the current
            # label in place." Kept counter exists so tests and the
            # caller can tell active-keeps from timeouts.
            kept += 1
            index += 1
            continue

        cat_id = _resolve_category(raw, cfg)
        if cat_id is None:
            echo_fn("unknown category; try again")
            continue

        new_urgency = _prompt_urgency_with_default(input_fn, echo_fn, default=current.urgency)
        if new_urgency is None:
            # User bailed on the urgency prompt — treat as skip so the
            # existing label stays in place.
            skipped += 1
            index += 1
            continue
        note = input_fn("Note (optional, empty to skip): ").strip() or None

        entry = make_entry(
            item_id=f"{src}:{platform_id}",
            category=cat_id,
            urgency=new_urgency,
            note=note,
        )
        append_label(labels_file, entry)
        relabeled += 1
        index += 1
        echo_fn(f"  relabeled: {current.category} u={current.urgency} → {cat_id} u={new_urgency}")

    return {
        "total": total,
        # "labeled" key mirrors the standard flow so callers can use the
        # same result shape. In --reconsider mode this is a relabel
        # count; tests and the caller also see the finer-grained keys.
        "labeled": relabeled,
        "kept": kept,
        "skipped": skipped,
        "remaining": max(0, total - index),
    }


def _prompt_urgency_with_default(
    input_fn: Any,
    echo_fn: Any,
    *,
    default: int,
) -> int | None:
    """Urgency prompt with an Enter-to-accept default. ``s`` abandons."""
    for _ in range(5):
        raw = input_fn(f"Urgency (0-10, Enter={default}, s=skip item): ").strip()
        if raw == "":
            return default
        if raw == _SKIP:
            return None
        try:
            val = int(raw)
        except ValueError:
            echo_fn("urgency must be an integer 0-10")
            continue
        if 0 <= val <= 10:
            return val
        echo_fn("urgency out of range")
    echo_fn("too many bad urgency inputs; skipping item")
    return None


def _echo_disagreement_context(
    db: Storage,
    item_id: str,
    prompt_version: str,
    echo_fn: Any,
) -> None:
    """Show the current classifier prediction + prior human label so the
    operator can see what they're being asked to re-examine."""
    classification = db.get_classification(item_id, prompt_version)
    if classification is None:
        return
    echo_fn("")
    echo_fn(
        f"[classifier {prompt_version}] said: "
        f"{classification['category']} urgency={classification['urgency']}"
    )
    reasoning = classification.get("reasoning")
    if reasoning:
        echo_fn(f"  reasoning: {reasoning}")


def _prompt_urgency(input_fn, echo_fn) -> int | None:
    for _ in range(5):
        raw = input_fn("Urgency (0-10, s=skip item): ").strip()
        if raw == _SKIP:
            return None
        try:
            val = int(raw)
        except ValueError:
            echo_fn("urgency must be an integer 0-10")
            continue
        if 0 <= val <= 10:
            return val
        echo_fn("urgency out of range")
    echo_fn("too many bad urgency inputs; skipping item")
    return None


def run_label_item(
    project: str,
    db_path: Path,
    projects_root: Path,
    *,
    item_id: str,
    category: str | None,
    urgency: int | None,
    note: str | None,
    input_fn: Any = input,
    echo_fn: Any = typer.echo,
) -> LabelEntry:
    """Label a single item by canonical ``item_id``, bypassing the walkthrough.

    Two paths:

    - **Fully specified** (``category`` and ``urgency`` both given): non-
      interactive. Validate and append. Used from Slack copy-paste
      commands where the operator already decided what the label should be.
    - **Partial / none given**: interactive. Prompt for whatever is
      missing, mirroring the walkthrough's prompt shapes so muscle
      memory carries over. If the item already has an effective label,
      show it first and ask whether to replace; a `n`/`N`/empty reply
      exits without appending.

    Validation is front-loaded: missing DB row, unknown category, and
    urgency-out-of-range all fail with ``typer.BadParameter`` so the
    CLI can convert to a clean exit-1. Reason: this command is expected
    to run inside tight copy-paste loops from Slack — silent failures
    there would mean "I thought I corrected it" while the eval set
    stays stale.
    """
    cfg = load_categories(project, projects_root=projects_root)
    labels_file = ensure_labels_file(project, projects_root=projects_root)

    if not db_path.is_file():
        raise typer.BadParameter(f"no DB at {db_path} yet — run a poll first")

    src, _, platform_id = item_id.partition(":")
    if not src or not platform_id:
        raise typer.BadParameter(
            f"item_id {item_id!r} is not canonical ({{source}}:{{platform_id}})"
        )

    if category is not None:
        resolved = _resolve_category(category, cfg)
        if resolved is None:
            valid = ", ".join(c.id for c in cfg.categories)
            raise typer.BadParameter(f"unknown category {category!r}; valid: {valid}")
        category = resolved

    if urgency is not None and not (0 <= urgency <= 10):
        raise typer.BadParameter(f"urgency must be in 0..10, got {urgency}")

    with Storage(db_path) as db:
        item = db.get_item_by_id(src, platform_id)
        if item is None:
            raise typer.BadParameter(
                f"no item with id {item_id!r} in {db_path} — "
                "did you mean a different project, or need to poll first?"
            )

        existing = resolve_effective_labels(iter_label_entries(labels_file)).get(item_id)

        # Non-interactive fast path: everything specified, nothing to ask.
        if category is not None and urgency is not None:
            entry = make_entry(
                item_id=item_id,
                category=category,
                urgency=urgency,
                note=note,
            )
            append_label(labels_file, entry)
            if existing is not None:
                echo_fn(
                    f"relabeled {item_id}: "
                    f"{existing.category} u={existing.urgency} → "
                    f"{entry.category} u={entry.urgency}"
                )
            else:
                echo_fn(f"labeled {item_id}: {entry.category} u={entry.urgency}")
            return entry

        # Interactive path: confirm replace if already labeled, then prompt
        # for whatever the caller didn't pass in.
        echo_fn(_render_item(item, cfg, index=1, total=1))
        if existing is not None:
            echo_fn(
                f"Current label: {existing.category}  urgency={existing.urgency}  "
                f"labeled_at={existing.labeled_at.isoformat()}"
            )
            if existing.note:
                echo_fn(f"  note: {existing.note}")
            raw = input_fn("Replace with new label? [y/N]: ").strip().lower()
            if raw not in {"y", "yes"}:
                echo_fn("keeping current label; no changes written.")
                # Sentinel return: callers don't need the entry, only the CLI
                # echoes whether anything changed. Raising here would turn
                # a benign "I changed my mind" into an error exit.
                return existing

        if category is None:
            while True:
                raw_cat = input_fn("Category (number or id, q=quit): ").strip()
                if raw_cat == _QUIT:
                    raise typer.Exit(code=0)
                resolved = _resolve_category(raw_cat, cfg)
                if resolved is not None:
                    category = resolved
                    break
                echo_fn("unknown category; try again")

        if urgency is None:
            echo_fn(_render_urgency_scale(cfg))
            prompted = _prompt_urgency(input_fn, echo_fn)
            if prompted is None:
                raise typer.Exit(code=0)
            urgency = prompted

        if note is None:
            note = input_fn("Note (optional, empty to skip): ").strip() or None

        entry = make_entry(
            item_id=item_id,
            category=category,
            urgency=urgency,
            note=note,
        )
        append_label(labels_file, entry)
        if existing is not None:
            echo_fn(
                f"relabeled {item_id}: "
                f"{existing.category} u={existing.urgency} → "
                f"{entry.category} u={entry.urgency}"
            )
        else:
            echo_fn(f"labeled {item_id}: {entry.category} u={entry.urgency}")
        return entry


__all__ = ["LabelEntry", "run_label", "run_label_item"]
