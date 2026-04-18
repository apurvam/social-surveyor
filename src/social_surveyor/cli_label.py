"""`social-surveyor label` — interactive labeling loop.

Reads `categories.yaml`, walks unlabeled items newest-first, and
appends one JSONL line per decision to `projects/<n>/evals/labeled.jsonl`.
Crash-safe (per-decision append), resume-default, one-step back.
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
    labeled_ids,
    make_entry,
    pop_last_label,
)
from .storage import Storage

# Keep the quit commands together so a reader can find them.
_QUIT = "q"
_BACK = "b"
_SKIP = "s"


class _Session:
    """Mutable per-run state for the label loop.

    Tracks progress counters + the last item shown (for `b`ack).
    Kept as a class so tests can drive :meth:`process_one` directly
    without going through Typer's prompt machinery.
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
        # Stack of item_ids we've shown; supports a one-step `b`ack.
        self._last_shown: tuple[str, str] | None = None


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
    input_fn=input,
    echo_fn=typer.echo,
) -> dict[str, int]:
    """Run the interactive label loop and return a result counter.

    ``input_fn`` and ``echo_fn`` are dependency-injected so tests can
    feed scripted input without touching a real terminal.
    """
    cfg = load_categories(project, projects_root=projects_root)
    labels_file = ensure_labels_file(project, projects_root=projects_root)

    if not db_path.is_file():
        raise typer.BadParameter(
            f"no DB at {db_path} yet — run a poll first"
        )

    with Storage(db_path) as db:
        queue = _build_queue(db, labels_file, source, randomize)
        session = _Session(cfg, db, labels_file, queue)

        echo_fn(
            f"labeling {session.total} items for project '{project}' "
            f"({'random' if randomize else 'newest-first'} order)"
        )
        echo_fn(_render_urgency_scale(cfg))

        while session.index < session.total:
            src, platform_id = queue[session.index]
            item = db.get_item_by_id(src, platform_id)
            if item is None:
                # Item vanished (deleted from DB between queue build and now).
                session.index += 1
                continue

            echo_fn(_render_item(item, cfg, session.index + 1, session.total))
            raw_cat = input_fn("Category (number or id, s=skip, b=back, q=quit): ").strip()

            if raw_cat == _QUIT:
                break
            if raw_cat == _SKIP:
                session.skipped += 1
                session.index += 1
                session._last_shown = (src, platform_id)
                continue
            if raw_cat == _BACK:
                popped = pop_last_label(labels_file)
                if popped is None:
                    echo_fn("nothing to undo.")
                    continue
                # Drop into the previous item's slot.
                session.labeled = max(0, session.labeled - 1)
                session.index = max(0, session.index - 1)
                echo_fn(f"reverted: {popped.item_id}")
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
            session._last_shown = (src, platform_id)
            echo_fn(_progress_line(session))

    return {
        "total": session.total,
        "labeled": session.labeled,
        "skipped": session.skipped,
        "remaining": max(0, session.total - session.index),
    }


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


__all__ = ["LabelEntry", "run_label"]
