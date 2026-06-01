from win_search_aliases.config import DenyList, load_deny_list
from win_search_aliases.filters import (
    CYRILLIC_ONLY_NAME_REASON,
    AppCandidate,
    build_filter_report,
    filter_candidates,
    search_candidates,
)


def test_filter_excludes_obvious_non_app_entries() -> None:
    candidates = [
        AppCandidate("Google Chrome", "chrome"),
        AppCandidate("Chrome Manual.pdf", "file:///manual.pdf"),
        AppCandidate("Support Tool Help", "support-help"),
        AppCandidate("Readme", "C:/readme.txt"),
    ]

    assert filter_candidates(candidates, load_deny_list()) == [candidates[0]]


def test_filter_applies_deny_list_to_display_name_and_app_id() -> None:
    deny_list = DenyList(exact=("blocked app",), patterns=("*forbidden*",))
    candidates = [
        AppCandidate("Blocked App", "ok"),
        AppCandidate("Good App", "vendor.forbidden.app"),
        AppCandidate("Another App", "ok"),
    ]

    assert filter_candidates(candidates, deny_list) == [candidates[2]]


def test_toml_filters_can_be_disabled() -> None:
    candidates = [
        AppCandidate("Chrome Manual.pdf", "file:///manual.pdf", content_c1="https://example.test/manual.pdf"),
        AppCandidate("Google Chrome", "chrome"),
    ]

    assert filter_candidates(candidates, load_deny_list()) == [candidates[1]]
    assert filter_candidates(candidates, load_deny_list(), use_deny_list_filters=False) == candidates


def test_cyrillic_only_display_names_are_filtered_without_hiding_mixed_names() -> None:
    candidates = [
        AppCandidate("Калькулятор 2", "calc"),
        AppCandidate("Мой App", "mixed"),
        AppCandidate("12345", "digits"),
        AppCandidate("Google Chrome", "chrome"),
    ]

    assert filter_candidates(candidates) == candidates[1:]
    assert filter_candidates(candidates, exclude_cyrillic_only_names=False) == candidates

    report = build_filter_report(candidates)
    assert report.reason_counts[CYRILLIC_ONLY_NAME_REASON] == 1


def test_filter_excludes_forced_c1_links_even_when_default_c1_filter_is_disabled() -> None:
    candidates = [
        AppCandidate("Web Result", "web", content_c1="https://example.test/app"),
        AppCandidate("Share Result", "share", content_c1=r"\\server\share\app"),
        AppCandidate("Real App", "app", content_c1=r"C:\ProgramData\App.lnk"),
    ]

    assert filter_candidates(
        candidates,
        load_deny_list(),
        use_default_content_exclusions=False,
    ) == [candidates[2]]


def test_default_c1_exclusions_can_be_disabled() -> None:
    candidates = [
        AppCandidate("Some Steam Game", "game", content_c1="steam://rungameid/123"),
        AppCandidate("Normal App", "app", content_c1=r"C:\ProgramData\App.lnk"),
    ]

    assert filter_candidates(candidates, load_deny_list()) == [candidates[1]]
    assert (
        filter_candidates(
            candidates,
            load_deny_list(),
            use_default_content_exclusions=False,
        )
        == candidates
    )


def test_c1_exclusions_can_be_disabled_by_category() -> None:
    candidates = [
        AppCandidate("Some Steam Game", "game", content_c1="steam://rungameid/123"),
        AppCandidate("Normal App", "app", content_c1=r"C:\ProgramData\App.lnk"),
    ]
    deny_list = load_deny_list()

    assert filter_candidates(candidates, deny_list) == [candidates[1]]
    assert filter_candidates(
        candidates,
        deny_list,
        enabled_content_categories={"some-other-category"},
    ) == [candidates[0], candidates[1]]
    assert (
        filter_candidates(
            candidates,
            deny_list,
            enabled_content_categories=set(),
        )
        == candidates
    )


def test_filter_report_counts_ignored_entries_and_categories() -> None:
    candidates = [
        AppCandidate("Steam Game", "game", content_c1="steam://rungameid/123"),
        AppCandidate("Web Result", "web", content_c1="https://example.test/app"),
        AppCandidate("Manual.pdf", "manual", content_c1=r"C:\Manual.pdf"),
        AppCandidate("Normal App", "app", content_c1=r"C:\ProgramData\App.lnk"),
    ]

    report = build_filter_report(candidates, load_deny_list())

    assert report.total == 4
    assert report.eligible == 1
    assert report.ignored == 3
    assert report.reason_counts["c1:games"] == 1
    assert report.reason_counts["deny-list"] == 2


def test_search_prefers_display_name_matches_before_app_id_matches() -> None:
    candidates = [
        AppCandidate("Alpha", "contains-beta"),
        AppCandidate("Beta App", "beta-id"),
    ]

    assert search_candidates(candidates, "beta") == [candidates[1], candidates[0]]
