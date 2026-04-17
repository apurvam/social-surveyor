from __future__ import annotations

from abc import ABC, abstractmethod

from ..types import RawItem


class Source(ABC):
    """Common interface every platform source implements.

    ``name`` is the canonical source identifier stored in ``items.source``
    and surfaced in the CLI (``--source reddit``, ``--source hackernews``).
    """

    name: str

    @abstractmethod
    def fetch(self, since_id: str | None = None) -> list[RawItem]:
        """Fetch the latest matching items from the platform.

        ``since_id`` is a platform-native incremental cursor. Sources that
        don't support it (e.g. Reddit search) may ignore the argument and
        rely on :meth:`Storage.upsert_item` for dedupe.
        """

    @abstractmethod
    def backfill(self, days: int) -> list[RawItem]:
        """Fetch items created within the last ``days`` days."""
