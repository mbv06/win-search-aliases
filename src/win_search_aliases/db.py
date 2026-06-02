from __future__ import annotations

import logging
import os
import shutil
import sqlite3
import subprocess
import time
from contextlib import closing, suppress
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, NamedTuple, TypeVar

from .aliases import AliasGroup, AliasRecord
from .filters import AppCandidate
from .metadata import MetadataStore

AliasKey = tuple[str, str]
AliasRow = tuple[str, str, str]


class ManagedRow(NamedTuple):
    display_name: str
    synonym: str
    rank: int | None
    source: str


SOURCE_GENERATED_AUTO = "WinSearchAliasesAuto"
SOURCE_GENERATED_MANUAL = "WinSearchAliasesManual"
SOURCE_CUSTOM = "WinSearchAliasesCustom"
MANAGED_SOURCES = (
    SOURCE_GENERATED_AUTO,
    SOURCE_GENERATED_MANUAL,
    SOURCE_CUSTOM,
)
MANAGED_SOURCE_PRIORITY = {
    SOURCE_GENERATED_AUTO: 1,
    SOURCE_GENERATED_MANUAL: 2,
    SOURCE_CUSTOM: 3,
}
SOURCE_BY_KIND = {
    "auto": SOURCE_GENERATED_AUTO,
    "manual": SOURCE_GENERATED_MANUAL,
    "custom": SOURCE_CUSTOM,
}
DEFAULT_DB_RELATIVE = (
    Path("Packages") / "MicrosoftWindows.Client.CBS_cw5n1h2txyewy" / "LocalState" / "Search" / "AppsIndex.db"
)
logger = logging.getLogger(__name__)
T = TypeVar("T")
WRITE_RETRY_ATTEMPTS = 3
READ_RETRY_ATTEMPTS = 3
WRITE_SQLITE_TIMEOUT_SECONDS = 2.0
READ_SQLITE_TIMEOUT_SECONDS = 1.0
TRANSIENT_SQLITE_MESSAGES = (
    "database is locked",
    "database table is locked",
    "disk i/o error",
)
DB_ERRORS = (FileNotFoundError, RuntimeError, sqlite3.DatabaseError)


@dataclass(frozen=True)
class AliasWriteResult:
    inserted: int = 0
    removed: int = 0
    inserted_records: list[AliasRecord] = field(default_factory=list)
    removed_records: set[tuple[str, str, str]] = field(default_factory=set)


@dataclass
class _AliasWriteAccumulator:
    inserted_records: list[AliasRecord] = field(default_factory=list)
    removed: int = 0
    removed_records: set[AliasRow] = field(default_factory=set)

    def add_inserted(self, record: AliasRecord) -> None:
        self.inserted_records.append(record)

    def add_removed(self, row: AliasRow, count: int) -> None:
        self.removed += count
        if count:
            self.removed_records.add(row)

    def result(self) -> AliasWriteResult:
        return AliasWriteResult(
            inserted=len(self.inserted_records),
            removed=self.removed,
            inserted_records=self.inserted_records,
            removed_records=self.removed_records,
        )


def default_db_path() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")
    if not local_app_data:
        raise RuntimeError("LOCALAPPDATA is not set; pass --db explicitly.")
    return Path(local_app_data) / DEFAULT_DB_RELATIVE


def resolve_db_path(path: str | Path | None = None) -> Path:
    db_path = Path(path) if path else default_db_path()
    if not db_path.exists():
        raise FileNotFoundError(f"AppsIndex database not found: {db_path}")
    return db_path


def connect(db_path: str | Path, *, timeout_seconds: float = WRITE_SQLITE_TIMEOUT_SECONDS) -> sqlite3.Connection:
    conn = sqlite3.connect(Path(db_path), timeout=timeout_seconds)
    conn.execute(f"pragma busy_timeout = {int(timeout_seconds * 1000)}")
    return conn


def read_tiles(db_path: str | Path) -> list[AppCandidate]:
    return _with_sqlite_retry("read", lambda: _read_tiles_once(db_path), attempts=READ_RETRY_ATTEMPTS)


def _read_tiles_once(db_path: str | Path) -> list[AppCandidate]:
    with closing(_connect_for_read(db_path)) as conn:
        columns = {row[1] for row in conn.execute("pragma table_info(tiles)").fetchall()}
        required = {"displayName", "appId"}
        missing = required - columns
        if missing:
            raise RuntimeError(f"tiles table is missing expected columns: {', '.join(sorted(missing))}")
        c_rank_expr = "t.cRank" if "cRank" in columns else "NULL as cRank"
        base_query = f"select displayName, appId, {c_rank_expr} from tiles as t"
        if _has_content_c1(conn):
            try:
                rows = conn.execute(
                    f"""
                    select t.displayName, t.appId, {c_rank_expr}, tc.c1
                    from tiles as t
                    left join tiles_content as tc on tc.id = t.rowid
                    """
                ).fetchall()
            except sqlite3.OperationalError:
                rows = conn.execute(base_query).fetchall()
        else:
            rows = conn.execute(base_query).fetchall()

    candidates = [
        AppCandidate(
            str(row[0] or ""),
            str(row[1] or ""),
            row[2],
            str(row[3] or "") if len(row) > 3 else "",
        )
        for row in rows
    ]
    return sorted(candidates, key=_rank_sort_key)


def insert_alias_groups(db_path: str | Path, groups: list[AliasGroup], source: str) -> AliasWriteResult:
    records = [record for group in groups for record in group.aliases]
    return insert_alias_records(db_path, records, source=source)


def _insert_new_records(conn: sqlite3.Connection, records: list[AliasRecord], source: str) -> AliasWriteResult:
    existing = _managed_alias_index(conn)
    result = _AliasWriteAccumulator()
    for record in records:
        key = _record_key(record)
        if not _prepare_alias_insert(conn, existing, key, source, result):
            continue
        _insert_alias_record(conn, record, source)
        result.add_inserted(record)
        existing.setdefault(key, []).append((record.display_name, record.synonym, source))
    return result.result()


def insert_alias_records(
    db_path: str | Path,
    records: list[AliasRecord],
    *,
    source: str,
) -> AliasWriteResult:
    return _write_transaction(db_path, lambda conn: _insert_new_records(conn, records, source))


def replace_alias_groups(db_path: str | Path, groups: list[AliasGroup], source: str) -> AliasWriteResult:
    records = [record for group in groups for record in group.aliases]
    return replace_alias_records(db_path, records, source=source)


def replace_alias_records(db_path: str | Path, records: list[AliasRecord], *, source: str) -> AliasWriteResult:
    source_sql, source_params = _managed_source_filter({source})

    def write(conn: sqlite3.Connection) -> AliasWriteResult:
        removed = conn.execute(
            f"select count(*) from synonyms where {source_sql}",
            source_params,
        ).fetchone()[0]
        removed_rows = conn.execute(
            f"select displayName, synonym, source from synonyms where {source_sql}",
            source_params,
        ).fetchall()
        conn.execute(f"delete from synonyms where {source_sql}", source_params)
        inserted = _insert_new_records(conn, records, source)
        return AliasWriteResult(
            inserted=inserted.inserted,
            removed=removed + inserted.removed,
            inserted_records=inserted.inserted_records,
            removed_records={(display_name, synonym, row_source) for display_name, synonym, row_source in removed_rows}
            | inserted.removed_records,
        )

    return _write_transaction(db_path, write)


def managed_rows(
    db_path: str | Path,
    sources: set[str] | None = None,
) -> list[ManagedRow]:
    return _with_sqlite_retry("read", lambda: _managed_rows_once(db_path, sources), attempts=READ_RETRY_ATTEMPTS)


def _managed_rows_once(
    db_path: str | Path,
    sources: set[str] | None = None,
) -> list[ManagedRow]:
    where_sql, params = _managed_source_filter(sources)
    with closing(_connect_for_read(db_path)) as conn:
        rows = conn.execute(
            f"""
            select displayName, synonym, rankPenalty, source
            from synonyms
            where {where_sql}
            order by displayName, synonym, source
            """,
            params,
        ).fetchall()
    return [ManagedRow(*row) for row in rows]


def remove_managed_rows(
    db_path: str | Path,
    display_names: set[str] | None = None,
    sources: set[str] | None = None,
) -> int:
    source_sql, source_params = _managed_source_filter(sources)

    def write(conn: sqlite3.Connection) -> int:
        if display_names:
            names = list(display_names)
            placeholders = ",".join("?" for _ in names)
            count = conn.execute(
                f"select count(*) from synonyms where {source_sql} and displayName in ({placeholders})",
                [*source_params, *names],
            ).fetchone()[0]
            conn.execute(
                f"delete from synonyms where {source_sql} and displayName in ({placeholders})",
                [*source_params, *names],
            )
            return count
        count = conn.execute(
            f"select count(*) from synonyms where {source_sql}",
            source_params,
        ).fetchone()[0]
        conn.execute(f"delete from synonyms where {source_sql}", source_params)
        return count

    return _write_transaction(db_path, write)


def remove_managed_alias_records(
    db_path: str | Path,
    records: set[tuple[str, str, str]],
) -> int:
    if not records:
        return 0

    def write(conn: sqlite3.Connection) -> int:
        removed = 0
        for display_name, synonym, source in sorted(records):
            removed += _delete_exact_alias_record(conn, display_name, synonym, source)
        return removed

    return _write_transaction(db_path, write)


def create_backup(db_path: str | Path, metadata: MetadataStore, reason: str) -> Path:
    source = Path(db_path)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    backup_dir = metadata.state_dir / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    target = backup_dir / f"AppsIndex-{timestamp}.db"
    shutil.copy2(source, target)
    metadata.add_backup(target, source, reason)
    logger.info("Backup created: path=%s reason=%s", target, reason)
    return target


def restore_database(
    db_path: str | Path,
    backup_path: str | Path,
    metadata: MetadataStore,
) -> Path:
    db = Path(db_path)
    backup = Path(backup_path)
    if not backup.exists():
        raise FileNotFoundError(f"Backup not found: {backup}")
    safety_backup = create_backup(db, metadata, "pre-restore")
    stop_search_host()
    shutil.copy2(backup, db)
    stop_search_host()
    return safety_backup


def stop_search_host() -> None:
    logger.debug("Stopping SearchHost")
    result = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-Command",
            (
                "$deadline = (Get-Date).AddSeconds(3); "
                "do { "
                "$processes = Get-Process -Name SearchHost -ErrorAction SilentlyContinue; "
                "if (-not $processes) { break }; "
                "$processes | Stop-Process -Force -ErrorAction SilentlyContinue; "
                "Start-Sleep -Milliseconds 100 "
                "} while ((Get-Date) -lt $deadline)"
            ),
        ],
        check=False,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    logger.debug("Stop SearchHost completed: returncode=%s", result.returncode)


def _begin_write(conn: sqlite3.Connection) -> None:
    conn.execute("begin immediate")


def _connect_for_read(db_path: str | Path) -> sqlite3.Connection:
    return connect(db_path, timeout_seconds=READ_SQLITE_TIMEOUT_SECONDS)


def _write_transaction(db_path: str | Path, write: Callable[[sqlite3.Connection], T]) -> T:
    return _with_sqlite_retry("write", lambda: _write_transaction_once(db_path, write), attempts=WRITE_RETRY_ATTEMPTS)


def _write_transaction_once(db_path: str | Path, write: Callable[[sqlite3.Connection], T]) -> T:
    with closing(connect(db_path)) as conn:
        _begin_write(conn)
        try:
            result = write(conn)
            conn.commit()
        except Exception:
            _rollback_quietly(conn)
            raise
        return result


def _with_sqlite_retry(operation: str, action: Callable[[], T], *, attempts: int) -> T:
    for attempt in range(attempts):
        logger.debug("SQLite %s attempt %s/%s", operation, attempt + 1, attempts)
        try:
            return action()
        except sqlite3.OperationalError as exc:
            if attempt == attempts - 1 or not _is_transient_sqlite_error(exc):
                raise
            logger.warning(
                "SQLite %s failed with a transient error; retrying after stopping SearchHost: %s",
                operation,
                exc,
            )
            stop_search_host()
            time.sleep(0.25 * (attempt + 1))
    raise RuntimeError(f"unreachable SQLite {operation} retry state")


def _rollback_quietly(conn: sqlite3.Connection) -> None:
    with suppress(sqlite3.Error):
        conn.rollback()


def _is_transient_sqlite_error(exc: sqlite3.OperationalError) -> bool:
    message = str(exc).casefold()
    return any(fragment in message for fragment in TRANSIENT_SQLITE_MESSAGES)


def _rank_sort_key(candidate: AppCandidate) -> tuple[int, float, str]:
    if candidate.c_rank is None:
        return 1, 0, candidate.display_name.casefold()
    try:
        rank = float(candidate.c_rank)
    except (TypeError, ValueError):
        rank = 0
    return 0, -rank, candidate.display_name.casefold()


def _has_content_c1(conn: sqlite3.Connection) -> bool:
    tables = {
        row[0] for row in conn.execute("select name from sqlite_master where type in ('table', 'view')").fetchall()
    }
    if "tiles_content" not in tables:
        return False
    columns = {row[1] for row in conn.execute("pragma table_info(tiles_content)").fetchall()}
    return {"id", "c1"}.issubset(columns)


def _managed_alias_index(conn: sqlite3.Connection) -> dict[AliasKey, list[AliasRow]]:
    source_sql, source_params = _managed_source_filter()
    rows = conn.execute(
        f"""
        select displayName, synonym, source
        from synonyms
        where {source_sql}
        """,
        source_params,
    ).fetchall()
    aliases: dict[AliasKey, list[AliasRow]] = {}
    for display_name, synonym, source in rows:
        row = (str(display_name or ""), str(synonym or ""), str(source or ""))
        aliases.setdefault(_alias_key(row[0], row[1]), []).append(row)
    return aliases


def _prepare_alias_insert(
    conn: sqlite3.Connection,
    existing: dict[AliasKey, list[AliasRow]],
    key: AliasKey,
    source: str,
    result: _AliasWriteAccumulator,
) -> bool:
    source_priority = _managed_source_priority(source)
    conflicts = existing.get(key, [])
    winning_priority = max([source_priority, *(_managed_source_priority(row[2]) for row in conflicts)])
    losing_conflicts = [row for row in conflicts if _managed_source_priority(row[2]) < winning_priority]
    for row in losing_conflicts:
        result.add_removed(row, _delete_exact_alias_record(conn, *row))
    if losing_conflicts:
        conflicts = [row for row in conflicts if row not in losing_conflicts]
        existing[key] = conflicts
    return source_priority >= winning_priority and all(row[2] != source for row in conflicts)


def _insert_alias_record(conn: sqlite3.Connection, record: AliasRecord, source: str) -> None:
    conn.execute(
        """
        insert into synonyms(displayName, rankPenalty, synonym, source)
        values (?, ?, ?, ?)
        """,
        (record.display_name, 1, record.synonym, source),
    )


def _record_key(record: AliasRecord) -> AliasKey:
    return _alias_key(record.display_name, record.synonym)


def _alias_key(display_name: str, synonym: str) -> AliasKey:
    return display_name.casefold(), synonym.casefold()


def _delete_exact_alias_record(conn: sqlite3.Connection, display_name: str, synonym: str, source: str) -> int:
    count = conn.execute(
        """
        select count(*)
        from synonyms
        where displayName = ? and synonym = ? and source = ?
        """,
        (display_name, synonym, source),
    ).fetchone()[0]
    conn.execute(
        """
        delete from synonyms
        where displayName = ? and synonym = ? and source = ?
        """,
        (display_name, synonym, source),
    )
    return count


def _managed_source_priority(source: str) -> int:
    return MANAGED_SOURCE_PRIORITY.get(source, 0)


def _managed_source_filter(sources: set[str] | None = None) -> tuple[str, list[str]]:
    selected = tuple(sources or MANAGED_SOURCES)
    placeholders = ",".join("?" for _ in selected)
    return f"source in ({placeholders})", list(selected)
