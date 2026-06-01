from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from .config import DenyList
from .utils import has_latin_letters


@dataclass(frozen=True)
class AppCandidate:
    display_name: str
    app_id: str
    c_rank: int | float | None = None
    content_c1: str = ""


DENY_LIST_REASON = "deny-list"
EMPTY_DISPLAY_NAME_REASON = "empty-display-name"
CYRILLIC_ONLY_NAME_REASON = "cyrillic-only-name"


@dataclass(frozen=True)
class FilterReport:
    total: int
    eligible: int
    ignored: int
    reason_counts: dict[str, int]


def is_eligible(
    candidate: AppCandidate,
    deny_list: DenyList | None = None,
    *,
    use_deny_list_filters: bool = True,
    use_default_content_exclusions: bool = True,
    enabled_content_categories: set[str] | None = None,
    exclude_cyrillic_only_names: bool = True,
) -> bool:
    return not candidate_filter_reasons(
        candidate,
        deny_list,
        use_deny_list_filters=use_deny_list_filters,
        use_default_content_exclusions=use_default_content_exclusions,
        enabled_content_categories=enabled_content_categories,
        exclude_cyrillic_only_names=exclude_cyrillic_only_names,
    )


def candidate_filter_reasons(
    candidate: AppCandidate,
    deny_list: DenyList | None = None,
    *,
    use_deny_list_filters: bool = True,
    use_default_content_exclusions: bool = True,
    enabled_content_categories: set[str] | None = None,
    exclude_cyrillic_only_names: bool = True,
) -> set[str]:
    fields = [candidate.display_name.strip(), candidate.app_id.strip()]
    if not fields[0]:
        return {EMPTY_DISPLAY_NAME_REASON}

    if exclude_cyrillic_only_names and is_cyrillic_only_name(fields[0]):
        return {CYRILLIC_ONLY_NAME_REASON}

    active_deny_list = deny_list if use_deny_list_filters else None
    if active_deny_list:
        for value in fields:
            if active_deny_list.matches(value):
                return {DENY_LIST_REASON}
        if active_deny_list.matches_network(candidate.content_c1):
            return {DENY_LIST_REASON}

    if active_deny_list and use_default_content_exclusions:
        categories = set()
        for value in [*fields, candidate.content_c1]:
            for category in active_deny_list.matching_content_categories(
                value,
                enabled_content_categories,
            ):
                categories.add(f"c1:{category}")
        if categories:
            return categories

    return set()


def filter_candidates(
    candidates: list[AppCandidate],
    deny_list: DenyList | None = None,
    *,
    use_deny_list_filters: bool = True,
    use_default_content_exclusions: bool = True,
    enabled_content_categories: set[str] | None = None,
    exclude_cyrillic_only_names: bool = True,
) -> list[AppCandidate]:
    return [
        candidate
        for candidate in candidates
        if is_eligible(
            candidate,
            deny_list,
            use_deny_list_filters=use_deny_list_filters,
            use_default_content_exclusions=use_default_content_exclusions,
            enabled_content_categories=enabled_content_categories,
            exclude_cyrillic_only_names=exclude_cyrillic_only_names,
        )
    ]


def build_filter_report(
    candidates: list[AppCandidate],
    deny_list: DenyList | None = None,
    *,
    use_deny_list_filters: bool = True,
    use_default_content_exclusions: bool = True,
    enabled_content_categories: set[str] | None = None,
    exclude_cyrillic_only_names: bool = True,
) -> FilterReport:
    reason_counts: Counter[str] = Counter()
    eligible = 0
    for candidate in candidates:
        reasons = candidate_filter_reasons(
            candidate,
            deny_list,
            use_deny_list_filters=use_deny_list_filters,
            use_default_content_exclusions=use_default_content_exclusions,
            enabled_content_categories=enabled_content_categories,
            exclude_cyrillic_only_names=exclude_cyrillic_only_names,
        )
        if reasons:
            reason_counts.update(reasons)
        else:
            eligible += 1
    return FilterReport(
        total=len(candidates),
        eligible=eligible,
        ignored=len(candidates) - eligible,
        reason_counts=dict(sorted(reason_counts.items())),
    )


def is_cyrillic_only_name(value: str) -> bool:
    if has_latin_letters(value):
        return False
    return any(_is_cyrillic_letter(char) for char in value)


def _is_cyrillic_letter(char: str) -> bool:
    return "\u0400" <= char <= "\u052f" or "\u2de0" <= char <= "\u2dff" or "\ua640" <= char <= "\ua69f"


def search_candidates(candidates: list[AppCandidate], query: str) -> list[AppCandidate]:
    normalized = query.casefold().strip()
    if not normalized:
        return candidates
    display_matches = [candidate for candidate in candidates if normalized in candidate.display_name.casefold()]
    display_names = {candidate.display_name for candidate in display_matches}
    app_id_matches = [
        candidate
        for candidate in candidates
        if candidate.display_name not in display_names and normalized in candidate.app_id.casefold()
    ]
    return [*display_matches, *app_id_matches]
