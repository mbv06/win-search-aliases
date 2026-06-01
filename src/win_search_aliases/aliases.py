from __future__ import annotations

import re
from dataclasses import dataclass, field

from .utils import dedupe_casefold, has_latin_letters

DEFAULT_STOP_WORDS = frozenset({"and", "for", "the", "with", "from"})
TOKEN_RE = re.compile(r"[A-Za-z0-9]+")


@dataclass(frozen=True)
class AliasRecord:
    display_name: str
    synonym: str
    alias_type: str
    keyboard_map: str | None = None
    token: str | None = None


@dataclass
class AliasGroup:
    display_name: str
    app_id: str
    alias_type: str
    aliases: list[AliasRecord] = field(default_factory=list)
    keyboard_map: str | None = None
    source: str | None = None


def extract_tokens(
    display_name: str,
    *,
    min_length: int = 4,
    stop_words: set[str] | frozenset[str] = DEFAULT_STOP_WORDS,
) -> list[str]:
    tokens: list[str] = []
    for match in TOKEN_RE.finditer(display_name):
        token = match.group(0).casefold()
        if token in stop_words:
            continue
        if len(token) < min_length or token.isdigit():
            continue
        if not has_latin_letters(token):
            continue
        tokens.append(token)
    return dedupe_casefold(tokens)


def map_text(text: str, keyboard_map: dict[str, str]) -> str:
    chars: list[str] = []
    for char in text.casefold():
        chars.append(keyboard_map.get(char, char))
    return "".join(chars)


def generate_alias_group(
    display_name: str,
    app_id: str,
    *,
    keyboard_map_name: str,
    keyboard_map: dict[str, str],
    min_token_length: int = 4,
    include_full_name: bool = False,
    stop_words: set[str] | frozenset[str] = DEFAULT_STOP_WORDS,
) -> AliasGroup:
    group = AliasGroup(
        display_name=display_name,
        app_id=app_id,
        alias_type="generated",
        keyboard_map=keyboard_map_name,
    )
    seen: set[str] = set()
    for token in extract_tokens(display_name, min_length=min_token_length, stop_words=stop_words):
        alias = map_text(token, keyboard_map)
        if alias != token and alias not in seen:
            seen.add(alias)
            group.aliases.append(AliasRecord(display_name, alias, "generated", keyboard_map_name, token))

    if include_full_name:
        full_name = display_name.casefold().strip()
        if has_latin_letters(full_name):
            alias = map_text(full_name, keyboard_map)
            if alias != full_name and alias not in seen:
                group.aliases.append(AliasRecord(display_name, alias, "generated", keyboard_map_name, full_name))
    return group


def custom_alias_group(display_name: str, app_id: str, aliases: list[str]) -> AliasGroup:
    group = AliasGroup(display_name=display_name, app_id=app_id, alias_type="custom")
    valid_aliases = [a.strip() for a in aliases if a.strip()]
    for alias in dedupe_casefold(valid_aliases):
        group.aliases.append(AliasRecord(display_name, alias, "custom"))
    return group
