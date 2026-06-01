from __future__ import annotations

import contextlib
import json
import os
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Generator

from .aliases import AliasGroup
from .utils import dedupe_casefold


def default_state_dir() -> Path:
    root = os.environ.get("LOCALAPPDATA")
    if root:
        return Path(root) / "win-search-aliases"
    return Path.home() / ".win-search-aliases"


class MetadataStore:
    def __init__(self, state_dir: str | Path | None = None) -> None:
        self.state_dir = Path(state_dir) if state_dir else default_state_dir()
        self.path = self.state_dir / "metadata.json"

    def load(self) -> dict:
        if not self.path.exists():
            return {"version": 1, "backups": [], "groups": []}
        return json.loads(self.path.read_text(encoding="utf-8"))

    def save(self, data: dict) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    @contextlib.contextmanager
    def _mutate(self) -> Generator[dict, None, None]:
        data = self.load()
        yield data
        self.save(data)

    def add_backup(self, backup_path: Path, db_path: Path, reason: str) -> None:
        with self._mutate() as data:
            data.setdefault("backups", []).append(
                {
                    "path": str(backup_path),
                    "db_path": str(db_path),
                    "reason": reason,
                    "created_at": now_iso(),
                }
            )

    def latest_backup(self) -> Path | None:
        backups = self.backups()
        if not backups:
            return None
        return Path(backups[-1]["path"])

    def backups(self) -> list[dict]:
        return list(self.load().get("backups", []))

    def remove_backup(self, backup_path: Path) -> int:
        target = str(backup_path)
        removed = 0
        with self._mutate() as data:
            backups = data.get("backups", [])
            kept = [backup for backup in backups if str(backup.get("path", "")) != target]
            removed = len(backups) - len(kept)
            data["backups"] = kept
        return removed

    def upsert_group(self, group: AliasGroup, *, merge_aliases: bool = False) -> None:
        self.upsert_groups([group], merge_aliases=merge_aliases)

    def upsert_groups(self, groups: list[AliasGroup], *, merge_aliases: bool = False) -> None:
        with self._mutate() as data:
            existing = data.setdefault("groups", [])
            for group in groups:
                key = _group_key(group)
                serialized = asdict(group) | {"updated_at": now_iso(), "key": key}
                for index, item in enumerate(existing):
                    if item.get("key") == key:
                        if merge_aliases:
                            serialized["aliases"] = _merge_aliases(item.get("aliases", []), serialized["aliases"])
                            if "created_at" in item:
                                serialized["created_at"] = item["created_at"]
                        existing[index] = serialized
                        break
                else:
                    serialized["created_at"] = now_iso()
                    existing.append(serialized)

    def groups(self) -> list[dict]:
        return list(self.load().get("groups", []))

    def remove_groups(
        self,
        display_names: set[str] | None = None,
        sources: set[str] | None = None,
    ) -> None:
        with self._mutate() as data:
            if display_names is None and sources is None:
                data["groups"] = []
            else:
                names = {name.casefold() for name in display_names or set()}
                data["groups"] = [
                    group for group in data.get("groups", []) if not _matches_remove_filter(group, names, sources)
                ]

    def remove_alias_records(self, records: set[tuple[str, str, str]]) -> None:
        if not records:
            return
        remove_keys = {
            (display_name.casefold(), synonym.casefold(), source) for display_name, synonym, source in records
        }
        with self._mutate() as data:
            groups = []
            for group in data.get("groups", []):
                display_name = group.get("display_name", "").casefold()
                source = group.get("source") or ""
                aliases = [
                    alias
                    for alias in group.get("aliases", [])
                    if (display_name, str(alias.get("synonym", "")).casefold(), source) not in remove_keys
                ]
                if aliases:
                    group["aliases"] = aliases
                    group["updated_at"] = now_iso()
                    groups.append(group)
            data["groups"] = groups


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _group_key(group: AliasGroup) -> str:
    return "|".join(
        [
            group.display_name.casefold(),
            group.app_id.casefold(),
            group.alias_type,
            group.keyboard_map or "",
            group.source or "",
        ]
    )


def _matches_remove_filter(
    group: dict,
    display_names: set[str],
    sources: set[str] | None,
) -> bool:
    name_matches = not display_names or group.get("display_name", "").casefold() in display_names
    source_matches = sources is None or group.get("source") in sources
    return name_matches and source_matches


def _merge_aliases(existing_aliases: list[dict], new_aliases: list[dict]) -> list[dict]:
    return dedupe_casefold([*existing_aliases, *new_aliases], key=lambda a: str(a.get("synonym", "")))
