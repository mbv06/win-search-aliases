from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import TypeVar

T = TypeVar("T")


def dedupe_casefold(items: Iterable[T], key: Callable[[T], str] | None = None) -> list[T]:
    """
    Remove duplicates from an iterable, ignoring case of a string key.
    The original order of the first occurrences is preserved.
    """
    seen: set[str] = set()
    result: list[T] = []
    for item in items:
        k = key(item).casefold() if key else str(item).casefold()
        if k not in seen:
            seen.add(k)
            result.append(item)
    return result


def has_latin_letters(text: str) -> bool:
    """Check if the text contains at least one Latin letter (a-z)."""
    return any("a" <= char <= "z" for char in text.casefold())


WINDOWS_ONLY_ERROR = "win-search-aliases is only supported on Windows."
