from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class ParsedEdition:
    """Canonical edition metadata extracted from a provider title."""

    provider_id: str
    title: str
    edition_at: datetime

    def __post_init__(self) -> None:
        if self.edition_at.tzinfo is None or self.edition_at.utcoffset() is None:
            raise ValueError("edition_at must be timezone-aware")
