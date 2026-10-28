"""Canonical random portable object identities; no filesystem or provider access."""

from collections.abc import Collection
from uuid import UUID, uuid4


def portable_id(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("Portable identity must be a canonical UUIDv4")
    try:
        parsed = UUID(value)
    except ValueError:
        raise ValueError("Portable identity must be a canonical UUIDv4") from None
    if parsed.version != 4 or str(parsed) != value:
        raise ValueError("Portable identity must be a canonical UUIDv4")
    return value


def new_portable_id(occupied: Collection[str]) -> str:
    while True:
        value = str(uuid4())
        if value not in occupied:
            return value
