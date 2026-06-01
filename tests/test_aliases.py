from win_search_aliases.aliases import extract_tokens, generate_alias_group, map_text


def test_extract_tokens_ignores_short_numbers_and_non_latin() -> None:
    assert extract_tokens("Google Chrome 2024 Pro Яндекс") == ["google", "chrome"]


def test_layout_mapping_uses_configurable_map() -> None:
    mapping = {"c": "с", "h": "р", "r": "к", "o": "щ", "m": "ь", "e": "у"}
    assert map_text("chrome", mapping) == "сркщьу"


def test_alias_generation_defaults_to_token_aliases() -> None:
    mapping = {
        "g": "п",
        "o": "щ",
        "l": "д",
        "e": "у",
        "c": "с",
        "h": "р",
        "r": "к",
        "m": "ь",
    }
    group = generate_alias_group(
        "Google Chrome",
        "chrome-app",
        keyboard_map_name="test",
        keyboard_map=mapping,
    )

    aliases = {record.token: record.synonym for record in group.aliases}
    assert aliases == {"google": "пщщпду", "chrome": "сркщьу"}


def test_full_name_generation_is_optional() -> None:
    mapping = {"a": "ф", "p": "з"}
    without_full = generate_alias_group(
        "App",
        "app-id",
        keyboard_map_name="test",
        keyboard_map=mapping,
        min_token_length=1,
    )
    with_full = generate_alias_group(
        "App",
        "app-id",
        keyboard_map_name="test",
        keyboard_map=mapping,
        min_token_length=1,
        include_full_name=True,
    )

    assert [record.synonym for record in without_full.aliases] == ["фзз"]
    assert [record.synonym for record in with_full.aliases] == ["фзз"]
