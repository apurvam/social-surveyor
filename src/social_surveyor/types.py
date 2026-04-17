from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class RawItem:
    """Common shape every source produces.

    ``created_at`` is always UTC. ``raw_json`` is the untransformed payload
    from the platform, preserved so later pipeline stages (classification,
    enrichment) can look at fields we didn't eagerly extract.
    """

    source: str
    platform_id: str
    url: str
    title: str
    body: str | None
    author: str | None
    created_at: datetime
    raw_json: dict[str, Any] = field(default_factory=dict)
