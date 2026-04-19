"""`social-surveyor silence` — stop alerting on a specific item.

Distinct from `label --item-id <id>`:

- `silence` says "don't alert me about this one again." It's a filter on
  the router, not feedback to the classifier. Useful when a classifier
  call was reasonable but the item is noise for your workflow (already
  handled, wrong audience, spam-adjacent).
- `label` says "the classifier's category/urgency on this item was
  wrong; here's ground truth." It lands in the eval set and shapes
  future prompt iterations.

Silence is intentionally permanent — there is no `unsilence` subcommand
in the MVP. The recovery path for a mis-silence is documented in the
CLI help text and in the README's daily-workflow section.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import typer

from .storage import Storage


def run_silence(
    project: str,
    db_path: Path,
    *,
    item_id: str,
    echo_fn: Any = typer.echo,
) -> bool:
    """Silence ``item_id`` for ``project``. Returns True if newly silenced.

    Validates the item exists in the DB so a typo can't slip through
    quietly — a silence on a non-existent item would never do anything,
    but the user wouldn't know.
    """
    if not db_path.is_file():
        raise typer.BadParameter(f"no DB at {db_path} yet — run a poll first")

    src, _, platform_id = item_id.partition(":")
    if not src or not platform_id:
        raise typer.BadParameter(
            f"item_id {item_id!r} is not canonical ({{source}}:{{platform_id}})"
        )

    with Storage(db_path) as db:
        if db.get_item_by_id(src, platform_id) is None:
            raise typer.BadParameter(
                f"no item with id {item_id!r} in {db_path} — did you mean a different project?"
            )
        newly = db.silence_item(item_id)

    if newly:
        echo_fn(f"silenced {item_id} for project {project!r}")
    else:
        echo_fn(f"{item_id} was already silenced")
    echo_fn(
        f"to reverse: sqlite3 {db_path} \"DELETE FROM silenced_items WHERE item_id='{item_id}'\""
    )
    return newly


__all__ = ["run_silence"]
