from __future__ import annotations

import fnmatch
import tomllib
from dataclasses import dataclass
from importlib import resources
from pathlib import Path


@dataclass(frozen=True)
class DenyList:
    exact: tuple[str, ...]
    patterns: tuple[str, ...]
    extensions: tuple[str, ...] = ()
    network_patterns: tuple[str, ...] = ()
    content_categories: dict[str, tuple[str, ...]] | None = None
    default_disabled_categories: tuple[str, ...] = ()

    def matches(self, value: str) -> bool:
        normalized = value.casefold()
        if normalized in self.exact:
            return True
        if any(fnmatch.fnmatch(normalized, pattern) for pattern in self.patterns):
            return True
        if any(normalized.endswith(extension) for extension in self.extensions):
            return True
        if any(f".{extension.removeprefix('.')}" in normalized for extension in self.extensions):
            return True
        return False

    def matches_network(self, value: str) -> bool:
        normalized = value.casefold()
        if any(fnmatch.fnmatch(normalized, pattern) for pattern in self.network_patterns):
            return True
        return False

    def matching_content_categories(
        self,
        value: str,
        enabled_categories: set[str] | None = None,
    ) -> set[str]:
        normalized = value.casefold()
        categories = self.content_categories or {}
        enabled = set(categories) if enabled_categories is None else enabled_categories
        return {
            name
            for name, patterns in categories.items()
            if name in enabled and any(fnmatch.fnmatch(normalized, pattern) for pattern in patterns)
        }

    def content_category_names(self) -> tuple[str, ...]:
        return tuple((self.content_categories or {}).keys())


def _read_config(name: str) -> dict:
    data = resources.files(__package__).joinpath("config", name).read_bytes()
    return tomllib.loads(data.decode("utf-8"))


def _casefold_tuple(raw: dict, key: str) -> tuple[str, ...]:
    return tuple(str(value).casefold() for value in raw.get(key, ()))


def load_keyboard_maps() -> dict[str, dict[str, str]]:
    raw = _read_config("keyboard_maps.toml")
    return {name: dict(profile["mapping"]) for name, profile in raw.get("maps", {}).items()}


def load_profiles() -> dict[str, list[str]]:
    raw = _read_config("profiles.toml")
    return {name: list(layout_ids) for name, layout_ids in raw.get("profiles", {}).items()}


def load_deny_list(path: str | Path | None = None) -> DenyList:
    raw = _read_config("denylist.toml")
    if path:
        user_raw = tomllib.loads(Path(path).read_text(encoding="utf-8"))
        raw["exact"] = [*raw.get("exact", []), *user_raw.get("exact", [])]
        raw["patterns"] = [*raw.get("patterns", []), *user_raw.get("patterns", [])]
        raw["extensions"] = [*raw.get("extensions", []), *user_raw.get("extensions", [])]
        raw["network_patterns"] = [*raw.get("network_patterns", []), *user_raw.get("network_patterns", [])]
        raw["content_categories"] = [
            *raw.get("content_categories", []),
            *user_raw.get("content_categories", []),
        ]
    categories: dict[str, list[str]] = {}
    default_disabled: list[str] = []
    for category in raw.get("content_categories", []):
        name = str(category.get("name", "")).casefold().strip()
        if not name:
            continue
        categories.setdefault(name, [])
        categories[name].extend(category.get("patterns", []))
        if category.get("disabled_by_default", False):
            default_disabled.append(name)
    return DenyList(
        exact=_casefold_tuple(raw, "exact"),
        patterns=_casefold_tuple(raw, "patterns"),
        extensions=_casefold_tuple(raw, "extensions"),
        network_patterns=_casefold_tuple(raw, "network_patterns"),
        content_categories={
            name: tuple(pattern.casefold() for pattern in patterns) for name, patterns in categories.items()
        },
        default_disabled_categories=tuple(default_disabled),
    )
