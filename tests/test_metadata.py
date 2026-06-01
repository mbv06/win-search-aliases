from win_search_aliases.aliases import custom_alias_group
from win_search_aliases.db import SOURCE_CUSTOM
from win_search_aliases.metadata import MetadataStore


def test_metadata_tracks_backups_and_groups(tmp_path) -> None:
    store = MetadataStore(tmp_path)
    db_path = tmp_path / "AppsIndex.db"
    backup_path = tmp_path / "backup.db"

    store.add_backup(backup_path, db_path, "unit-test")
    group = custom_alias_group("Google Chrome", "chrome", ["browser", "work-browser"])
    group.source = SOURCE_CUSTOM
    store.upsert_group(group)
    store.upsert_group(group)

    data = store.load()
    assert store.latest_backup() == backup_path
    assert len(data["groups"]) == 1
    assert data["groups"][0]["alias_type"] == "custom"
    assert data["groups"][0]["source"] == SOURCE_CUSTOM
    assert store.remove_backup(backup_path) == 1
    assert store.backups() == []
    assert store.remove_backup(backup_path) == 0

    store.remove_groups({"Google Chrome"})
    assert store.groups() == []
