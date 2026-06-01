from conftest import create_fixture_db

from win_search_aliases import operations as ops
from win_search_aliases.aliases import AliasGroup, AliasRecord, custom_alias_group
from win_search_aliases.db import (
    SOURCE_CUSTOM,
    SOURCE_GENERATED_AUTO,
    SOURCE_GENERATED_MANUAL,
    insert_alias_records,
    managed_rows,
)
from win_search_aliases.filters import AppCandidate
from win_search_aliases.metadata import MetadataStore


def generated_group(display_name: str, synonym: str) -> AliasGroup:
    return AliasGroup(
        display_name=display_name,
        app_id=display_name.casefold().replace(" ", "-"),
        alias_type="generated",
        aliases=[AliasRecord(display_name, synonym, "generated", "ru-jcuken", display_name.casefold())],
        keyboard_map="ru-jcuken",
    )


def test_scan_database_reports_counts(tmp_path) -> None:
    db_path = tmp_path / "AppsIndex.db"
    create_fixture_db(db_path)

    result = ops.scan_database(ops.AppSettings(db=db_path))

    assert result.report.total == 2
    assert result.report.eligible == 1
    assert result.report.ignored == 1


def test_build_generated_alias_plan_does_not_write_rows(monkeypatch, tmp_path) -> None:
    db_path = tmp_path / "AppsIndex.db"
    create_fixture_db(db_path)
    monkeypatch.setattr(
        ops, "load_keyboard_maps", lambda: {"ru-jcuken": {"c": "с", "h": "р", "r": "к", "o": "щ", "m": "ь", "e": "у"}}
    )
    monkeypatch.setattr(ops, "load_profiles", lambda: {"ru-jcuken": ["00000419"]})
    monkeypatch.setattr(ops, "resolve_keyboard_map_names", lambda *_args, **_kwargs: ["ru-jcuken"])

    plan = ops.build_generated_alias_plan(
        ops.AppSettings(db=db_path),
        ops.GenerationOptions(map_names=["ru-jcuken"]),
        [AppCandidate("Chrome", "chrome")],
        "unit-test",
        SOURCE_GENERATED_AUTO,
    )

    assert plan.total_aliases == 1
    assert plan.groups[0].aliases[0].synonym == "сркщьу"
    assert managed_rows(db_path) == []


def test_apply_alias_plan_can_skip_or_create_backup(monkeypatch, tmp_path) -> None:
    db_path = tmp_path / "AppsIndex.db"
    create_fixture_db(db_path)
    settings = ops.AppSettings(db=db_path, state_dir=tmp_path / "state")
    stops = []
    monkeypatch.setattr(ops, "stop_search_host", lambda: stops.append("stop"))

    first = ops.prepare_alias_plan(db_path, [custom_alias_group("Google Chrome", "chrome", ["browser"])], "custom")
    skipped = ops.apply_alias_plan(first, settings, create_backup_requested=False)
    second = ops.prepare_alias_plan(
        db_path, [custom_alias_group("Google Chrome", "chrome", ["work-browser"])], "custom"
    )
    backed_up = ops.apply_alias_plan(second, settings, create_backup_requested=True)

    assert skipped.backup is None
    assert backed_up.backup is not None
    assert skipped.inserted == 1
    assert backed_up.inserted == 1
    assert len(MetadataStore(tmp_path / "state").backups()) == 1
    assert stops == ["stop", "stop", "stop", "stop", "stop"]


def test_apply_alias_plan_append_merges_metadata_aliases(monkeypatch, tmp_path) -> None:
    db_path = tmp_path / "AppsIndex.db"
    create_fixture_db(db_path)
    settings = ops.AppSettings(db=db_path, state_dir=tmp_path / "state")
    monkeypatch.setattr(ops, "stop_search_host", lambda: None)

    first = ops.prepare_alias_plan(db_path, [custom_alias_group("Google Chrome", "chrome", ["browser"])], "custom")
    second = ops.prepare_alias_plan(
        db_path, [custom_alias_group("Google Chrome", "chrome", ["work-browser"])], "custom"
    )

    ops.apply_alias_plan(first, settings, create_backup_requested=False)
    result = ops.apply_alias_plan(second, settings, create_backup_requested=False)

    assert result.removed == 0
    assert result.inserted == 1
    assert [(row[0], row[1], row[3]) for row in managed_rows(db_path)] == [
        ("Google Chrome", "browser", SOURCE_CUSTOM),
        ("Google Chrome", "work-browser", SOURCE_CUSTOM),
    ]
    groups = MetadataStore(tmp_path / "state").groups()
    chrome = next(group for group in groups if group["display_name"] == "Google Chrome")
    assert [alias["synonym"] for alias in chrome["aliases"]] == ["browser", "work-browser"]


def test_apply_alias_plan_can_replace_one_apps_custom_aliases(monkeypatch, tmp_path) -> None:
    db_path = tmp_path / "AppsIndex.db"
    create_fixture_db(db_path)
    settings = ops.AppSettings(db=db_path, state_dir=tmp_path / "state")
    monkeypatch.setattr(ops, "stop_search_host", lambda: None)
    insert_alias_records(db_path, [AliasRecord("Google Chrome", "old-browser", "custom")], source=SOURCE_CUSTOM)
    insert_alias_records(db_path, [AliasRecord("Other App", "other", "custom")], source=SOURCE_CUSTOM)

    plan = ops.build_custom_alias_plan(
        settings,
        AppCandidate("Google Chrome", "chrome"),
        ["browser"],
        replace_existing=True,
    )
    result = ops.apply_alias_plan(plan, settings, create_backup_requested=False)

    assert result.removed == 1
    assert result.inserted == 1
    assert [(row[0], row[1], row[3]) for row in managed_rows(db_path)] == [
        ("Google Chrome", "browser", SOURCE_CUSTOM),
        ("Other App", "other", SOURCE_CUSTOM),
    ]


def test_apply_alias_plan_does_not_track_skipped_auto_alias_in_metadata(monkeypatch, tmp_path) -> None:
    db_path = tmp_path / "AppsIndex.db"
    create_fixture_db(db_path)
    settings = ops.AppSettings(db=db_path, state_dir=tmp_path / "state")
    monkeypatch.setattr(ops, "stop_search_host", lambda: None)

    manual = ops.prepare_alias_plan(
        db_path,
        [generated_group("Google Chrome", "browser")],
        "generate-selected",
        SOURCE_GENERATED_MANUAL,
    )
    auto = ops.prepare_alias_plan(
        db_path,
        [generated_group("Google Chrome", "browser")],
        "auto",
        SOURCE_GENERATED_AUTO,
        replace_source=True,
    )

    ops.apply_alias_plan(manual, settings, create_backup_requested=False)
    result = ops.apply_alias_plan(auto, settings, create_backup_requested=False)

    assert result.inserted == 0
    assert [(row[0], row[1], row[3]) for row in managed_rows(db_path)] == [
        ("Google Chrome", "browser", SOURCE_GENERATED_MANUAL),
    ]
    groups = MetadataStore(tmp_path / "state").groups()
    assert [(group["display_name"], group["source"]) for group in groups] == [
        ("Google Chrome", SOURCE_GENERATED_MANUAL),
    ]


def test_apply_alias_plan_moves_metadata_to_higher_priority_source(monkeypatch, tmp_path) -> None:
    db_path = tmp_path / "AppsIndex.db"
    create_fixture_db(db_path)
    settings = ops.AppSettings(db=db_path, state_dir=tmp_path / "state")
    monkeypatch.setattr(ops, "stop_search_host", lambda: None)

    auto = ops.prepare_alias_plan(
        db_path,
        [generated_group("Google Chrome", "browser")],
        "auto",
        SOURCE_GENERATED_AUTO,
    )
    manual = ops.prepare_alias_plan(
        db_path,
        [generated_group("Google Chrome", "browser")],
        "generate-selected",
        SOURCE_GENERATED_MANUAL,
    )

    ops.apply_alias_plan(auto, settings, create_backup_requested=False)
    result = ops.apply_alias_plan(manual, settings, create_backup_requested=False)

    assert (result.removed, result.inserted) == (1, 1)
    assert [(row[0], row[1], row[3]) for row in managed_rows(db_path)] == [
        ("Google Chrome", "browser", SOURCE_GENERATED_MANUAL),
    ]
    groups = MetadataStore(tmp_path / "state").groups()
    assert [(group["display_name"], group["source"]) for group in groups] == [
        ("Google Chrome", SOURCE_GENERATED_MANUAL),
    ]


def test_remove_managed_aliases_by_kind(monkeypatch, tmp_path) -> None:
    db_path = tmp_path / "AppsIndex.db"
    create_fixture_db(db_path)
    settings = ops.AppSettings(db=db_path, state_dir=tmp_path / "state")
    stops = []
    monkeypatch.setattr(ops, "stop_search_host", lambda: stops.append("stop"))
    insert_alias_records(db_path, [AliasRecord("Google Chrome", "browser", "custom")], source=SOURCE_CUSTOM)
    insert_alias_records(db_path, [AliasRecord("Google Chrome", "сркщьу", "generated")], source=SOURCE_GENERATED_AUTO)

    result = ops.remove_managed_aliases(settings, kinds=["custom"], create_backup_requested=False)

    assert result.removed == 1
    assert [row[3] for row in managed_rows(db_path)] == [SOURCE_GENERATED_AUTO]
    assert stops == ["stop", "stop"]


def test_remove_managed_alias_records_exact_preserves_unselected_aliases(monkeypatch, tmp_path) -> None:
    db_path = tmp_path / "AppsIndex.db"
    create_fixture_db(db_path)
    settings = ops.AppSettings(db=db_path, state_dir=tmp_path / "state")
    monkeypatch.setattr(ops, "stop_search_host", lambda: None)
    first = custom_alias_group("Google Chrome", "chrome", ["browser", "web"])
    second = custom_alias_group("MPC-HC", "mpc-hc", ["player"])
    first.source = SOURCE_CUSTOM
    second.source = SOURCE_CUSTOM
    insert_alias_records(db_path, first.aliases + second.aliases, source=SOURCE_CUSTOM)
    store = MetadataStore(tmp_path / "state")
    store.upsert_group(first)
    store.upsert_group(second)

    result = ops.remove_managed_alias_records_exact(
        settings,
        {("Google Chrome", "browser", SOURCE_CUSTOM)},
        create_backup_requested=False,
    )

    assert result.removed == 1
    assert [(row[0], row[1], row[3]) for row in managed_rows(db_path)] == [
        ("Google Chrome", "web", SOURCE_CUSTOM),
        ("MPC-HC", "player", SOURCE_CUSTOM),
    ]
    groups = store.groups()
    chrome = next(group for group in groups if group["display_name"] == "Google Chrome")
    assert [alias["synonym"] for alias in chrome["aliases"]] == ["web"]


def test_backup_infos_include_managed_row_status(tmp_path) -> None:
    db_path = tmp_path / "AppsIndex.db"
    create_fixture_db(db_path)
    store = MetadataStore(tmp_path / "state")
    store.add_backup(db_path, db_path, "unit-test")

    backups = ops.backup_infos(ops.AppSettings(state_dir=tmp_path / "state"))

    assert backups[0].path == db_path
    assert backups[0].reason == "unit-test"
    assert backups[0].status == "clean: no managed rows"


def test_delete_backup_removes_tracked_file_and_metadata(tmp_path) -> None:
    db_path = tmp_path / "AppsIndex.db"
    backup_path = tmp_path / "backup.db"
    backup_path.write_text("backup", encoding="utf-8")
    store = MetadataStore(tmp_path / "state")
    store.add_backup(backup_path, db_path, "unit-test")

    result = ops.delete_backup(ops.AppSettings(state_dir=tmp_path / "state"), backup_path)

    assert result.path == backup_path
    assert result.removed_file is True
    assert result.removed_metadata == 1
    assert not backup_path.exists()
    assert store.backups() == []


def test_delete_backup_rejects_untracked_file(tmp_path) -> None:
    backup_path = tmp_path / "backup.db"
    backup_path.write_text("backup", encoding="utf-8")

    try:
        ops.delete_backup(ops.AppSettings(state_dir=tmp_path / "state"), backup_path)
    except ValueError as exc:
        assert "Backup is not tracked" in str(exc)
    else:
        raise AssertionError("Untracked backups should not be deleted.")

    assert backup_path.exists()
