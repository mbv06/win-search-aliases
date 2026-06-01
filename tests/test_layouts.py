import sys
from types import SimpleNamespace

import pytest

from win_search_aliases.layouts import (
    WINDOWS_KEYBOARD_PRELOAD_KEY,
    detect_installed_layout_identifiers,
    resolve_keyboard_map_names,
    select_supported_keyboard_maps,
)


class FakeRegistryKey:
    def __enter__(self) -> "FakeRegistryKey":
        return self

    def __exit__(self, *_args: object) -> None:
        return None


def test_select_supported_keyboard_maps_uses_installed_order_and_deduplicates() -> None:
    keyboard_maps = {"ru-jcuken": {}, "uk-jcuken": {}}  # type: ignore
    profiles = {
        "ru-jcuken": ["00000419"],
        "uk-jcuken": ["00000422"],
    }

    selected = select_supported_keyboard_maps(
        profiles,
        keyboard_maps,
        ["00000409", "00000419", "00000419", "00000422"],
    )

    assert selected == ["ru-jcuken", "uk-jcuken"]


def test_detect_installed_layout_identifiers_reads_keyboard_preload_in_registry_order(monkeypatch) -> None:
    values = {
        0: ("2", "00000422", 0),
        1: ("1", "00000419", 0),
        2: ("10", "00000409", 0),
    }

    def open_key(root: object, path: str) -> FakeRegistryKey:
        assert root == "HKEY_CURRENT_USER"
        assert path in (WINDOWS_KEYBOARD_PRELOAD_KEY, r"Keyboard Layout\Substitutes")
        if path == r"Keyboard Layout\Substitutes":
            raise OSError
        return FakeRegistryKey()

    def enum_value(_key: FakeRegistryKey, index: int) -> tuple[str, str, int]:
        if index not in values:
            raise OSError
        return values[index]

    fake_winreg = SimpleNamespace(
        HKEY_CURRENT_USER="HKEY_CURRENT_USER",
        OpenKey=open_key,
        EnumValue=enum_value,
    )

    monkeypatch.setitem(sys.modules, "winreg", fake_winreg)

    assert detect_installed_layout_identifiers() == ["00000419", "00000422", "00000409"]


def test_resolve_keyboard_map_names_accepts_profiles_explicit_maps_and_auto() -> None:
    keyboard_maps = {"ru-jcuken": {}, "uk-jcuken": {}}  # type: ignore
    profiles = {"ru-jcuken": ["00000419"], "uk-jcuken": ["00000422"]}

    selected = resolve_keyboard_map_names(
        ["00000422", "auto", "ru-jcuken"],
        keyboard_maps,
        profiles,
        installed_identifiers=["00000419"],
    )

    assert selected == ["uk-jcuken", "ru-jcuken"]


def test_resolve_keyboard_map_names_errors_when_auto_detects_nothing_supported() -> None:
    with pytest.raises(ValueError, match="No supported installed keyboard layouts"):
        resolve_keyboard_map_names(
            "auto",
            {"ru-jcuken": {}},
            {"ru-jcuken": ["00000419"]},
            installed_identifiers=["00000409"],
        )
