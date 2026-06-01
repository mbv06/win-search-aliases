from __future__ import annotations

import contextlib
from collections.abc import Iterable
from itertools import count

from .utils import dedupe_casefold

AUTO_KEYBOARD_MAP = "auto"

WINDOWS_KEYBOARD_PRELOAD_KEY = r"Keyboard Layout\Preload"


def detect_installed_layout_identifiers() -> list[str]:
    """Return installed Windows keyboard layout IDs in preference order."""
    return dedupe_casefold(_read_keyboard_layout_ids())


def select_supported_keyboard_maps(
    profiles: dict[str, list[str]],
    keyboard_maps: dict[str, dict[str, str]],
    installed_identifiers: Iterable[str] | None = None,
    *,
    _layout_index: dict[str, str] | None = None,
) -> list[str]:
    identifiers = (
        list(installed_identifiers) if installed_identifiers is not None else detect_installed_layout_identifiers()
    )
    layout_index = _layout_index or _keyboard_maps_by_layout_id(profiles)
    return dedupe_casefold(
        map_name
        for identifier in identifiers
        if (map_name := layout_index.get(identifier.casefold())) and map_name in keyboard_maps
    )


def resolve_keyboard_map_names(
    requested_maps: str | list[str] | None,
    keyboard_maps: dict[str, dict[str, str]],
    profiles: dict[str, list[str]],
    *,
    default_map: str = AUTO_KEYBOARD_MAP,
    installed_identifiers: Iterable[str] | None = None,
) -> list[str]:
    requested = _requested_map_names(requested_maps, default_map)
    layout_index = _keyboard_maps_by_layout_id(profiles)
    selected: list[str] = []

    for requested_name in requested:
        if requested_name.casefold() == AUTO_KEYBOARD_MAP:
            selected.extend(
                select_supported_keyboard_maps(
                    profiles, keyboard_maps, installed_identifiers, _layout_index=layout_index
                )
            )
        else:
            selected.append(_resolve_explicit_map_name(requested_name, keyboard_maps, layout_index))

    selected = dedupe_casefold(selected)
    if not selected:
        raise ValueError(
            "No supported installed keyboard layouts detected. "
            "Install a supported layout or pass --map ru-jcuken / --map uk-jcuken explicitly."
        )
    return selected


def _requested_map_names(requested_maps: str | list[str] | None, default_map: str) -> list[str]:
    if isinstance(requested_maps, str):
        requested_maps = [requested_maps]
    return requested_maps or [default_map]


def _resolve_explicit_map_name(
    requested_name: str,
    keyboard_maps: dict[str, dict[str, str]],
    layout_index: dict[str, str],
) -> str:
    if requested_name in keyboard_maps:
        return requested_name

    map_name = layout_index.get(requested_name.casefold())
    if map_name and map_name in keyboard_maps:
        return map_name

    raise ValueError(f"Unknown keyboard map: {requested_name}")


def _keyboard_maps_by_layout_id(profiles: dict[str, list[str]]) -> dict[str, str]:
    return {layout_id.casefold(): map_name for map_name, layout_ids in profiles.items() for layout_id in layout_ids}


def _read_keyboard_layout_ids() -> list[str]:
    try:
        import winreg
    except ImportError:
        return []

    substitutes: dict[str, str] = {}
    with contextlib.suppress(OSError):
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Keyboard Layout\Substitutes") as key:
            for index in count():
                try:
                    name, value, _ = winreg.EnumValue(key, index)
                    substitutes[name] = str(value)
                except OSError:
                    break

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, WINDOWS_KEYBOARD_PRELOAD_KEY) as key:
            values = []
            for index in count():
                try:
                    name, value, _ = winreg.EnumValue(key, index)
                except OSError:
                    break
                val_str = str(value)
                val_str = substitutes.get(val_str, val_str)
                values.append((name, val_str))
    except OSError:
        return []

    return [value for _, value in sorted(values, key=_registry_order_key)]


def _registry_order_key(item: tuple[str, str]) -> tuple[int, str]:
    name, _value = item
    try:
        return int(name), name
    except ValueError:
        return 1_000_000, name
