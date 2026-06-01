from __future__ import annotations

from .aliases import AliasGroup
from .db import (
    SOURCE_BY_KIND,
    SOURCE_CUSTOM,
    SOURCE_GENERATED_AUTO,
    SOURCE_GENERATED_MANUAL,
    ManagedRow,
)

MANAGED_KIND_ORDER = ("auto", "manual", "custom")
KIND_BY_SOURCE = {source: kind for kind, source in SOURCE_BY_KIND.items()}


def alias_sources_by_display_name(rows: list[ManagedRow]) -> dict[str, set[str]]:
    sources: dict[str, set[str]] = {}
    for display_name, _synonym, _rank, source in rows:
        sources.setdefault(display_name, set()).add(source)
    return sources


def row_counts_by_kind(rows: list[ManagedRow]) -> dict[str, int]:
    counts = dict.fromkeys(MANAGED_KIND_ORDER, 0)
    for _display_name, _synonym, _rank, source in rows:
        kind = KIND_BY_SOURCE.get(source)
        if kind is not None:
            counts[kind] += 1
    return counts


def sources_from_kinds(kinds: list[str] | None) -> set[str] | None:
    if not kinds:
        return None
    return {SOURCE_BY_KIND[kind] for kind in kinds}


def source_for_groups(groups: list[AliasGroup]) -> str:
    explicit_sources = {group.source for group in groups if group.source}
    if len(explicit_sources) == 1:
        return explicit_sources.pop()
    if explicit_sources:
        raise ValueError("Alias groups with different sources must be applied separately.")
    if groups and all(group.alias_type == "custom" for group in groups):
        return SOURCE_CUSTOM
    raise ValueError("Generated alias groups need an explicit managed source.")


def primary_managed_source(sources: set[str]) -> str:
    return ordered_managed_sources(sources)[0]


def ordered_managed_sources(sources: set[str]) -> list[str]:
    order = [SOURCE_CUSTOM, SOURCE_GENERATED_MANUAL, SOURCE_GENERATED_AUTO]
    return [source for source in order if source in sources] + sorted(sources - set(order))
