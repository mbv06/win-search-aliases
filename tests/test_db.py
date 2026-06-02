import sqlite3

from conftest import create_fixture_db

from win_search_aliases import db as db_mod
from win_search_aliases.aliases import AliasRecord
from win_search_aliases.config import load_deny_list
from win_search_aliases.db import (
    READ_SQLITE_TIMEOUT_SECONDS,
    SOURCE_CUSTOM,
    SOURCE_GENERATED_AUTO,
    SOURCE_GENERATED_MANUAL,
    connect,
    create_backup,
    insert_alias_records,
    managed_rows,
    read_tiles,
    remove_managed_alias_records,
    remove_managed_rows,
    replace_alias_records,
    restore_database,
)
from win_search_aliases.filters import filter_candidates
from win_search_aliases.metadata import MetadataStore


def test_read_tiles_and_counts_report_total_and_eligible(tmp_path) -> None:
    db_path = tmp_path / "AppsIndex.db"
    create_fixture_db(db_path)

    all_tiles = read_tiles(db_path)
    candidates = filter_candidates(all_tiles, load_deny_list())

    assert len(all_tiles) == 2
    assert len(candidates) == 1
    assert candidates[0].display_name == "Google Chrome"
    assert candidates[0].content_c1.endswith("Google Chrome.lnk")


def test_connect_applies_requested_busy_timeout(monkeypatch) -> None:
    seen = {}

    class FakeConnection:
        def execute(self, sql):
            seen["pragma"] = sql

    def fake_sqlite_connect(path, *, timeout):
        seen["path"] = path
        seen["timeout"] = timeout
        return FakeConnection()

    monkeypatch.setattr(db_mod.sqlite3, "connect", fake_sqlite_connect)

    conn = connect("AppsIndex.db", timeout_seconds=READ_SQLITE_TIMEOUT_SECONDS)

    assert conn is not None
    assert seen["timeout"] == READ_SQLITE_TIMEOUT_SECONDS
    assert seen["pragma"] == "pragma busy_timeout = 1000"


def test_read_tiles_retries_transient_locked_database(monkeypatch) -> None:
    attempts = []
    stopped = []

    class FakeCursor:
        def __init__(self, rows):
            self._rows = rows

        def fetchall(self):
            return self._rows

    class FakeConnection:
        def __init__(self, should_fail: bool) -> None:
            self.should_fail = should_fail
            self.closed = False

        def execute(self, sql, *_args, **_kwargs):
            if sql == "pragma table_info(tiles)" and self.should_fail:
                raise sqlite3.OperationalError("database is locked")
            if sql == "pragma table_info(tiles)":
                return FakeCursor([(0, "displayName"), (1, "appId"), (2, "cRank")])
            if "from sqlite_master" in sql:
                return FakeCursor([])
            return FakeCursor([("Google Chrome", "chrome", 1)])

        def close(self) -> None:
            self.closed = True

    def fake_connect(_path, **_kwargs):
        attempts.append(True)
        return FakeConnection(should_fail=len(attempts) == 1)

    monkeypatch.setattr(db_mod, "connect", fake_connect)
    monkeypatch.setattr(db_mod, "stop_search_host", lambda: stopped.append(True))
    monkeypatch.setattr(db_mod.time, "sleep", lambda _seconds: None)

    tiles = read_tiles("AppsIndex.db")

    assert [(tile.display_name, tile.app_id) for tile in tiles] == [("Google Chrome", "chrome")]
    assert len(attempts) == 2
    assert stopped == [True]


def test_read_tiles_retries_locked_tiles_content_join(monkeypatch) -> None:
    attempts = []
    stopped = []

    class FakeCursor:
        def __init__(self, rows):
            self._rows = rows

        def fetchall(self):
            return self._rows

    class FakeConnection:
        def __init__(self, should_fail_join: bool) -> None:
            self.should_fail_join = should_fail_join

        def execute(self, sql, *_args, **_kwargs):
            if sql == "pragma table_info(tiles)":
                return FakeCursor([(0, "displayName"), (1, "appId"), (2, "cRank")])
            if "from sqlite_master" in sql:
                return FakeCursor([("tiles_content",)])
            if sql == "pragma table_info(tiles_content)":
                return FakeCursor([(0, "id"), (1, "c1")])
            if "left join tiles_content" in sql:
                if self.should_fail_join:
                    raise sqlite3.OperationalError("database is locked")
                return FakeCursor([("Google Chrome", "chrome", 1, "Programs/Google Chrome.lnk")])
            return FakeCursor([("Google Chrome", "chrome", 1)])

        def close(self) -> None:
            pass

    def fake_connect(_path, **_kwargs):
        attempts.append(True)
        return FakeConnection(should_fail_join=len(attempts) == 1)

    monkeypatch.setattr(db_mod, "connect", fake_connect)
    monkeypatch.setattr(db_mod, "stop_search_host", lambda: stopped.append(True))
    monkeypatch.setattr(db_mod.time, "sleep", lambda _seconds: None)

    tiles = read_tiles("AppsIndex.db")

    assert [(tile.display_name, tile.content_c1) for tile in tiles] == [
        ("Google Chrome", "Programs/Google Chrome.lnk")
    ]
    assert len(attempts) == 2
    assert stopped == [True]


def test_insert_alias_records_is_idempotent(tmp_path) -> None:
    db_path = tmp_path / "AppsIndex.db"
    create_fixture_db(db_path)
    record = AliasRecord("Google Chrome", "сркщьу", "generated", "ru-jcuken", "chrome")

    assert insert_alias_records(db_path, [record], source=SOURCE_GENERATED_AUTO).inserted == 1
    assert insert_alias_records(db_path, [record], source=SOURCE_GENERATED_AUTO).inserted == 0

    rows = managed_rows(db_path)
    assert len(rows) == 1
    assert rows[0][0:2] == ("Google Chrome", "сркщьу")
    assert rows[0][3] == SOURCE_GENERATED_AUTO


def test_remove_managed_rows_only_removes_tool_source(tmp_path) -> None:
    db_path = tmp_path / "AppsIndex.db"
    create_fixture_db(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute(
        "insert into synonyms(displayName, rankPenalty, synonym, source) values (?, ?, ?, ?)",
        ("Google Chrome", 1, "browser", "OtherSource"),
    )
    conn.commit()
    conn.close()
    insert_alias_records(
        db_path,
        [AliasRecord("Google Chrome", "browser", "custom")],
        source=SOURCE_CUSTOM,
    )

    assert remove_managed_rows(db_path) == 1

    conn = sqlite3.connect(db_path)
    remaining = conn.execute("select source from synonyms").fetchall()
    conn.close()
    assert remaining == [("OtherSource",)]


def test_remove_managed_rows_can_target_one_source_kind(tmp_path) -> None:
    db_path = tmp_path / "AppsIndex.db"
    create_fixture_db(db_path)
    insert_alias_records(
        db_path, [AliasRecord("Google Chrome", "auto-browser", "generated")], source=SOURCE_GENERATED_AUTO
    )
    insert_alias_records(
        db_path, [AliasRecord("Google Chrome", "manual-browser", "generated")], source=SOURCE_GENERATED_MANUAL
    )
    insert_alias_records(db_path, [AliasRecord("Google Chrome", "custom-browser", "custom")], source=SOURCE_CUSTOM)

    assert remove_managed_rows(db_path, sources={SOURCE_GENERATED_AUTO}) == 1

    rows = managed_rows(db_path)
    assert [row[3] for row in rows] == [SOURCE_CUSTOM, SOURCE_GENERATED_MANUAL]


def test_remove_managed_alias_records_removes_only_exact_rows(tmp_path) -> None:
    db_path = tmp_path / "AppsIndex.db"
    create_fixture_db(db_path)
    insert_alias_records(
        db_path,
        [
            AliasRecord("Google Chrome", "browser", "custom"),
            AliasRecord("Google Chrome", "web", "custom"),
            AliasRecord("MPC-HC", "player", "custom"),
        ],
        source=SOURCE_CUSTOM,
    )

    removed = remove_managed_alias_records(db_path, {("Google Chrome", "browser", SOURCE_CUSTOM)})

    assert removed == 1
    assert [(row[0], row[1], row[3]) for row in managed_rows(db_path)] == [
        ("Google Chrome", "web", SOURCE_CUSTOM),
        ("MPC-HC", "player", SOURCE_CUSTOM),
    ]


def test_managed_rows_closes_connection(monkeypatch) -> None:
    closed = []

    class FakeCursor:
        def fetchall(self):
            return []

    class FakeConnection:
        def execute(self, *_args, **_kwargs):
            return FakeCursor()

        def close(self):
            closed.append(True)

    monkeypatch.setattr("win_search_aliases.db.connect", lambda _path, **_kwargs: FakeConnection())

    assert managed_rows("AppsIndex.db") == []
    assert closed == [True]


def test_managed_rows_retries_transient_locked_database(monkeypatch) -> None:
    attempts = []
    stopped = []

    class FakeCursor:
        def fetchall(self):
            return [("Google Chrome", "browser", 1, SOURCE_CUSTOM)]

    class FakeConnection:
        def __init__(self, should_fail: bool) -> None:
            self.should_fail = should_fail

        def execute(self, *_args, **_kwargs):
            if self.should_fail:
                raise sqlite3.OperationalError("database is locked")
            return FakeCursor()

        def close(self) -> None:
            pass

    def fake_connect(_path, **_kwargs):
        attempts.append(True)
        return FakeConnection(should_fail=len(attempts) == 1)

    monkeypatch.setattr(db_mod, "connect", fake_connect)
    monkeypatch.setattr(db_mod, "stop_search_host", lambda: stopped.append(True))
    monkeypatch.setattr(db_mod.time, "sleep", lambda _seconds: None)

    rows = managed_rows("AppsIndex.db")

    assert rows == [("Google Chrome", "browser", 1, SOURCE_CUSTOM)]
    assert len(attempts) == 2
    assert stopped == [True]


def test_replace_auto_source_leaves_manual_and_custom_rows(tmp_path) -> None:
    db_path = tmp_path / "AppsIndex.db"
    create_fixture_db(db_path)
    insert_alias_records(
        db_path, [AliasRecord("Google Chrome", "auto-browser", "generated")], source=SOURCE_GENERATED_AUTO
    )
    insert_alias_records(
        db_path, [AliasRecord("Google Chrome", "manual-browser", "generated")], source=SOURCE_GENERATED_MANUAL
    )
    insert_alias_records(db_path, [AliasRecord("Google Chrome", "custom-browser", "custom")], source=SOURCE_CUSTOM)

    result = replace_alias_records(
        db_path,
        [AliasRecord("Google Chrome", "сркщьу", "generated", "ru-jcuken", "chrome")],
        source=SOURCE_GENERATED_AUTO,
    )

    assert (result.removed, result.inserted) == (1, 1)
    rows = managed_rows(db_path)
    assert [row[3] for row in rows] == [
        SOURCE_CUSTOM,
        SOURCE_GENERATED_MANUAL,
        SOURCE_GENERATED_AUTO,
    ]


def test_auto_skips_existing_manual_alias(tmp_path) -> None:
    db_path = tmp_path / "AppsIndex.db"
    create_fixture_db(db_path)
    record = AliasRecord("Google Chrome", "browser", "generated")

    assert insert_alias_records(db_path, [record], source=SOURCE_GENERATED_MANUAL).inserted == 1
    assert insert_alias_records(db_path, [record], source=SOURCE_GENERATED_AUTO).inserted == 0

    assert [(row[0], row[1], row[3]) for row in managed_rows(db_path)] == [
        ("Google Chrome", "browser", SOURCE_GENERATED_MANUAL),
    ]


def test_manual_replaces_existing_auto_alias(tmp_path) -> None:
    db_path = tmp_path / "AppsIndex.db"
    create_fixture_db(db_path)
    record = AliasRecord("Google Chrome", "browser", "generated")

    assert insert_alias_records(db_path, [record], source=SOURCE_GENERATED_AUTO).inserted == 1
    assert insert_alias_records(db_path, [record], source=SOURCE_GENERATED_MANUAL).inserted == 1

    assert [(row[0], row[1], row[3]) for row in managed_rows(db_path)] == [
        ("Google Chrome", "browser", SOURCE_GENERATED_MANUAL),
    ]


def test_managed_alias_conflicts_are_case_insensitive(tmp_path) -> None:
    db_path = tmp_path / "AppsIndex.db"
    create_fixture_db(db_path)

    assert (
        insert_alias_records(
            db_path,
            [AliasRecord("Google Chrome", "Browser", "generated")],
            source=SOURCE_GENERATED_AUTO,
        ).inserted
        == 1
    )
    assert (
        insert_alias_records(
            db_path,
            [AliasRecord("Google Chrome", "browser", "generated")],
            source=SOURCE_GENERATED_MANUAL,
        ).inserted
        == 1
    )

    assert [(row[0], row[1], row[3]) for row in managed_rows(db_path)] == [
        ("Google Chrome", "browser", SOURCE_GENERATED_MANUAL),
    ]


def test_custom_replaces_generated_alias(tmp_path) -> None:
    db_path = tmp_path / "AppsIndex.db"
    create_fixture_db(db_path)
    record = AliasRecord("Google Chrome", "browser", "custom")

    assert insert_alias_records(db_path, [record], source=SOURCE_GENERATED_MANUAL).inserted == 1
    assert insert_alias_records(db_path, [record], source=SOURCE_CUSTOM).inserted == 1

    assert [(row[0], row[1], row[3]) for row in managed_rows(db_path)] == [
        ("Google Chrome", "browser", SOURCE_CUSTOM),
    ]


def test_auto_replacement_preserves_manual_conflict(tmp_path) -> None:
    db_path = tmp_path / "AppsIndex.db"
    create_fixture_db(db_path)

    insert_alias_records(db_path, [AliasRecord("Google Chrome", "old-auto", "generated")], source=SOURCE_GENERATED_AUTO)
    insert_alias_records(
        db_path, [AliasRecord("Google Chrome", "browser", "generated")], source=SOURCE_GENERATED_MANUAL
    )

    result = replace_alias_records(
        db_path,
        [
            AliasRecord("Google Chrome", "browser", "generated"),
            AliasRecord("Google Chrome", "new-auto", "generated"),
        ],
        source=SOURCE_GENERATED_AUTO,
    )

    assert (result.removed, result.inserted) == (1, 1)
    assert [(row[0], row[1], row[3]) for row in managed_rows(db_path)] == [
        ("Google Chrome", "browser", SOURCE_GENERATED_MANUAL),
        ("Google Chrome", "new-auto", SOURCE_GENERATED_AUTO),
    ]


def test_replace_alias_records_can_clear_source(tmp_path) -> None:
    db_path = tmp_path / "AppsIndex.db"
    create_fixture_db(db_path)
    record = AliasRecord("Google Chrome", "browser", "custom")
    insert_alias_records(db_path, [record], source=SOURCE_GENERATED_AUTO)

    result = replace_alias_records(db_path, [], source=SOURCE_GENERATED_AUTO)

    assert (result.removed, result.inserted) == (1, 0)
    assert managed_rows(db_path) == []


def test_replace_alias_records_retries_transient_disk_io_error(monkeypatch) -> None:
    attempts = []
    stopped = []

    class FakeCursor:
        def fetchone(self):
            return (2,)

        def fetchall(self):
            return []

    class FakeConnection:
        def __init__(self, should_fail: bool) -> None:
            self.should_fail = should_fail
            self.closed = False

        def execute(self, sql, *_args, **_kwargs):
            if sql == "begin immediate":
                return FakeCursor()
            if self.should_fail:
                self.should_fail = False
                raise sqlite3.OperationalError("disk I/O error")
            return FakeCursor()

        def commit(self) -> None:
            pass

        def rollback(self) -> None:
            pass

        def close(self) -> None:
            self.closed = True

    def fake_connect(_path):
        attempts.append(True)
        return FakeConnection(should_fail=len(attempts) == 1)

    monkeypatch.setattr(db_mod, "connect", fake_connect)
    monkeypatch.setattr(db_mod, "stop_search_host", lambda: stopped.append(True))
    monkeypatch.setattr(db_mod.time, "sleep", lambda _seconds: None)

    result = replace_alias_records("AppsIndex.db", [], source=SOURCE_GENERATED_AUTO)

    assert (result.removed, result.inserted) == (2, 0)
    assert len(attempts) == 2
    assert stopped == [True]


def test_backup_and_restore_database(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "AppsIndex.db"
    db_path.write_text("current", encoding="utf-8")
    metadata = MetadataStore(tmp_path / "state")
    backup = create_backup(db_path, metadata, "test")
    db_path.write_text("changed", encoding="utf-8")

    monkeypatch.setattr("win_search_aliases.db.stop_search_host", lambda: None)
    safety = restore_database(db_path, backup, metadata)

    assert safety is not None
    assert db_path.read_text(encoding="utf-8") == "current"
    assert len(metadata.load()["backups"]) == 2
