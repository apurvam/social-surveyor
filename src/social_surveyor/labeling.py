"""Shared label-file read/write helpers.

Labels live at ``projects/<project>/evals/labeled.jsonl`` — one JSON
object per line, appended per decision so that a crash loses at most
one label. ``item_id`` follows the canonical ``{source}:{platform_id}``
form that the storage layer uses elsewhere.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError


class LabelEntry(BaseModel):
    """One decision recorded by the labeler.

    Labels are append-only. When multiple entries exist for the same
    ``item_id``, the one with the latest ``labeled_at`` is authoritative;
    earlier entries are retained in the file for audit but do not
    affect eval scoring. See :func:`resolve_effective_labels`.
    """

    model_config = ConfigDict(extra="forbid")

    item_id: str = Field(..., min_length=3)
    category: str = Field(..., min_length=1)
    urgency: int = Field(..., ge=0, le=10)
    note: str | None = None
    labeled_at: datetime


class LabelFileError(Exception):
    """Raised on malformed JSONL; the operator can inspect & fix manually."""


def labels_path(project: str, projects_root: Path | str = "projects") -> Path:
    return Path(projects_root) / project / "evals" / "labeled.jsonl"


def ensure_labels_file(project: str, projects_root: Path | str = "projects") -> Path:
    """Create the labels file + parent directory if missing; return the path."""
    p = labels_path(project, projects_root)
    p.parent.mkdir(parents=True, exist_ok=True)
    if not p.exists():
        p.touch()
    return p


def iter_label_entries(path: Path) -> list[LabelEntry]:
    """Read every label from ``path``. Missing file → empty list.

    Raises :class:`LabelFileError` on any malformed line so we don't
    silently corrupt the eval set.
    """
    if not path.exists():
        return []
    out: list[LabelEntry] = []
    with path.open("r", encoding="utf-8") as f:
        for lineno, raw in enumerate(f, start=1):
            line = raw.strip()
            if not line:
                continue
            try:
                out.append(LabelEntry.model_validate_json(line))
            except ValidationError as e:
                raise LabelFileError(f"{path}:{lineno}: invalid label entry — {e}") from e
            except json.JSONDecodeError as e:
                raise LabelFileError(f"{path}:{lineno}: malformed JSON — {e}") from e
    return out


def resolve_effective_labels(entries: list[LabelEntry]) -> dict[str, LabelEntry]:
    """Collapse an append-only label list to its latest-wins view.

    Groups by ``item_id`` and returns the entry with the maximum
    ``labeled_at`` per group. Earlier entries are discarded from the
    returned dict but remain in the underlying file (the raw list
    passed in is not mutated).

    This is the canonical helper for anyone asking "what is the
    current ground truth for this item?" — used by the eval harness,
    the label walkthrough queue builder, and the reconsider / disagreement
    queue builders.
    """
    latest: dict[str, LabelEntry] = {}
    for e in entries:
        prior = latest.get(e.item_id)
        if prior is None or e.labeled_at > prior.labeled_at:
            latest[e.item_id] = e
    return latest


def labeled_ids(path: Path) -> set[str]:
    """Return the set of ``item_id``s for which at least one label exists.

    Identical by construction to ``resolve_effective_labels(...).keys()``
    — the latest-wins collapse preserves the set of labeled ids, it
    just picks one canonical entry per id. Going through
    :func:`resolve_effective_labels` keeps the "one source of truth"
    invariant visible.
    """
    return set(resolve_effective_labels(iter_label_entries(path)).keys())


def count_labeled_ids(path: Path) -> int:
    """Count unique ``item_id``s (not lines — corrections append rather than overwrite)."""
    return len(labeled_ids(path))


def append_label(path: Path, entry: LabelEntry) -> None:
    """Append one label as a single JSONL line. Atomic on POSIX append."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        # Use model_dump_json for consistent ordering & date formatting.
        f.write(entry.model_dump_json() + "\n")


def make_entry(
    *,
    item_id: str,
    category: str,
    urgency: int,
    note: str | None,
) -> LabelEntry:
    """Build a LabelEntry with labeled_at set to now(UTC)."""
    return LabelEntry(
        item_id=item_id,
        category=category,
        urgency=urgency,
        note=note or None,
        labeled_at=datetime.now(UTC),
    )


def dump_label(entry: LabelEntry) -> dict[str, Any]:
    """For tests: round-trippable dict representation."""
    return json.loads(entry.model_dump_json())
