from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from .aliases import DEFAULT_STOP_WORDS, AliasGroup, AliasRecord, custom_alias_group, generate_alias_group
from .config import DenyList, load_deny_list, load_keyboard_maps, load_profiles
from .db import (
    DB_ERRORS,
    SOURCE_CUSTOM,
    AliasWriteResult,
    ManagedRow,
    create_backup,
    insert_alias_groups,
    managed_rows,
    read_tiles,
    remove_managed_alias_records,
    remove_managed_rows,
    replace_alias_groups,
    resolve_db_path,
    restore_database,
    stop_search_host,
)
from .filters import AppCandidate, FilterReport, build_filter_report, filter_candidates
from .layouts import resolve_keyboard_map_names
from .managed_sources import source_for_groups, sources_from_kinds
from .metadata import MetadataStore

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AppSettings:
    db: str | Path | None = None
    state_dir: str | Path | None = None
    deny_list: str | Path | None = None
    use_deny_list_filters: bool = True
    use_default_content_exclusions: bool = True
    included_content_categories: list[str] | None = None
    exclude_cyrillic_only_names: bool = True


@dataclass(frozen=True)
class GenerationOptions:
    map_names: list[str] | None = None
    default_map: str = "uk-jcuken"
    include_full_name: bool = False
    min_token_length: int = 4
    stop_words: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ScanResult:
    db_path: Path
    report: FilterReport
    deny_list: DenyList
    enabled_content_categories: set[str]


@dataclass
class AliasPlan:
    db_path: Path
    groups: list[AliasGroup]
    reason: str
    source: str
    replace_source: bool = False
    replace_display_names: set[str] | None = None
    selected_maps: list[str] = field(default_factory=list)

    @property
    def total_aliases(self) -> int:
        return sum(len(group.aliases) for group in self.groups)


@dataclass(frozen=True)
class ApplyResult:
    backup: Path | None = None
    removed: int = 0
    inserted: int = 0


@dataclass(frozen=True)
class ManagedSummary:
    db_path: Path | None
    metadata_groups: list[dict]
    rows: list[ManagedRow]
    scan: ScanResult | None


@dataclass(frozen=True)
class BackupInfo:
    path: Path
    reason: str
    created_at: str
    status: str
    has_error: bool = False


@dataclass(frozen=True)
class DeleteBackupResult:
    path: Path
    removed_file: bool
    removed_metadata: int


def _resolve_filter_context(
    settings: AppSettings,
) -> tuple[Path, DenyList, set[str]]:
    """Shared setup for scan_database and eligible_candidates."""
    db_path = resolve_db_path(settings.db)
    deny_list = load_deny_list(settings.deny_list)
    enabled_categories = enabled_content_categories(settings, deny_list)
    return db_path, deny_list, enabled_categories


def _metadata(settings: AppSettings) -> MetadataStore:
    return MetadataStore(settings.state_dir)


def scan_database(settings: AppSettings) -> ScanResult:
    db_path, deny_list, enabled_categories = _resolve_filter_context(settings)
    report = build_filter_report(
        read_tiles(db_path),
        deny_list,
        use_deny_list_filters=settings.use_deny_list_filters,
        use_default_content_exclusions=settings.use_default_content_exclusions,
        enabled_content_categories=enabled_categories,
        exclude_cyrillic_only_names=settings.exclude_cyrillic_only_names,
    )
    logger.info(
        "Scan complete: total=%s eligible=%s ignored=%s",
        report.total,
        report.eligible,
        report.ignored,
    )
    return ScanResult(db_path, report, deny_list, enabled_categories)


def eligible_candidates(settings: AppSettings) -> list[AppCandidate]:
    db_path, deny_list, enabled_categories = _resolve_filter_context(settings)
    return filter_candidates(
        read_tiles(db_path),
        deny_list,
        use_deny_list_filters=settings.use_deny_list_filters,
        use_default_content_exclusions=settings.use_default_content_exclusions,
        enabled_content_categories=enabled_categories,
        exclude_cyrillic_only_names=settings.exclude_cyrillic_only_names,
    )


def enabled_content_categories(settings: AppSettings, deny_list: DenyList) -> set[str]:
    if not settings.use_deny_list_filters:
        return set()
    validate_content_categories(settings, deny_list)
    category_names = set(deny_list.content_category_names())
    if not settings.use_default_content_exclusions:
        return set()
    return category_names - set(_included_categories(settings, deny_list))


def validate_content_categories(settings: AppSettings, deny_list: DenyList) -> None:
    known = set(deny_list.content_category_names())
    unknown = sorted(set(_included_categories(settings, deny_list)) - known)
    if unknown:
        raise ValueError(
            "Unknown c1 categories: " + ", ".join(unknown) + f". Known categories: {', '.join(sorted(known))}"
        )


def filter_category_counts(settings: AppSettings) -> dict[str, int]:
    deny_list = load_deny_list(None)
    try:
        db_path = resolve_db_path(settings.db)
        report = build_filter_report(
            read_tiles(db_path),
            deny_list,
            use_deny_list_filters=True,
            use_default_content_exclusions=True,
            enabled_content_categories=set(deny_list.content_category_names()),
            exclude_cyrillic_only_names=False,
        )
    except DB_ERRORS:
        return dict.fromkeys(deny_list.content_category_names(), 0)
    return {category: report.reason_counts.get(f"c1:{category}", 0) for category in deny_list.content_category_names()}


def build_generated_alias_plan(
    settings: AppSettings,
    options: GenerationOptions,
    candidates: list[AppCandidate],
    reason: str,
    source: str,
    *,
    replace_source: bool = False,
) -> AliasPlan:
    db_path = resolve_db_path(settings.db)
    maps = load_keyboard_maps()
    profiles = load_profiles()
    selected_maps = resolve_keyboard_map_names(
        options.map_names,
        maps,
        profiles,
        default_map=options.default_map,
    )
    stop_words = set(DEFAULT_STOP_WORDS) | set(options.stop_words)
    groups = [
        generate_alias_group(
            candidate.display_name,
            candidate.app_id,
            keyboard_map_name=map_name,
            keyboard_map=maps[map_name],
            min_token_length=options.min_token_length,
            include_full_name=options.include_full_name,
            stop_words=stop_words,
        )
        for candidate in candidates
        for map_name in selected_maps
    ]
    plan = prepare_alias_plan(
        db_path,
        [group for group in groups if group.aliases],
        reason,
        source,
        replace_source=replace_source,
        selected_maps=selected_maps,
    )
    logger.info(
        "Generated alias plan ready: groups=%s aliases=%s maps=%s replace_source=%s",
        len(plan.groups),
        plan.total_aliases,
        ",".join(plan.selected_maps),
        plan.replace_source,
    )
    return plan


def build_custom_alias_plan(
    settings: AppSettings,
    candidate: AppCandidate,
    aliases: list[str],
    *,
    replace_existing: bool = False,
) -> AliasPlan:
    db_path = resolve_db_path(settings.db)
    group = custom_alias_group(candidate.display_name, candidate.app_id, aliases)
    plan = prepare_alias_plan(
        db_path,
        [group],
        "add-custom",
        SOURCE_CUSTOM,
        replace_display_names={candidate.display_name} if replace_existing else None,
    )
    logger.info("Custom alias plan ready: app=%s aliases=%s", candidate.display_name, plan.total_aliases)
    return plan


def prepare_alias_plan(
    db_path: Path,
    groups: list[AliasGroup],
    reason: str,
    source: str | None = None,
    *,
    replace_source: bool = False,
    replace_display_names: set[str] | None = None,
    selected_maps: list[str] | None = None,
) -> AliasPlan:
    selected_source = source or source_for_groups(groups)
    for group in groups:
        group.source = selected_source
    return AliasPlan(
        db_path=db_path,
        groups=groups,
        reason=reason,
        source=selected_source,
        replace_source=replace_source,
        replace_display_names=replace_display_names,
        selected_maps=list(selected_maps or []),
    )


def _prepare_mutation(
    settings: AppSettings,
    reason: str,
    *,
    create_backup_requested: bool = True,
) -> tuple[Path, MetadataStore, Path | None]:
    db_path = resolve_db_path(settings.db)
    metadata = _metadata(settings)
    backup = None
    if create_backup_requested:
        stop_search_host()
        backup = create_backup(db_path, metadata, reason)
    stop_search_host()
    return db_path, metadata, backup


def apply_alias_plan(
    plan: AliasPlan,
    settings: AppSettings,
    *,
    create_backup_requested: bool = True,
) -> ApplyResult:
    if not plan.groups and not plan.replace_source:
        logger.info("Skipping empty alias plan: source=%s", plan.source)
        return ApplyResult()
    logger.info(
        "Applying alias plan: source=%s groups=%s aliases=%s replace_source=%s backup_requested=%s",
        plan.source,
        len(plan.groups),
        plan.total_aliases,
        plan.replace_source,
        create_backup_requested,
    )
    _db_path, metadata, backup = _prepare_mutation(
        settings,
        plan.reason,
        create_backup_requested=create_backup_requested,
    )
    if plan.replace_source:
        write_result = replace_alias_groups(plan.db_path, plan.groups, plan.source)
        metadata.remove_groups(sources={plan.source})
    elif plan.replace_display_names:
        removed = remove_managed_rows(plan.db_path, plan.replace_display_names, {plan.source})
        write_result = insert_alias_groups(plan.db_path, plan.groups, source=plan.source)
        write_result = AliasWriteResult(
            inserted=write_result.inserted,
            removed=removed + write_result.removed,
            inserted_records=write_result.inserted_records,
            removed_records=write_result.removed_records,
        )
        metadata.remove_groups(plan.replace_display_names, {plan.source})
    else:
        write_result = insert_alias_groups(plan.db_path, plan.groups, source=plan.source)
    if write_result.removed_records:
        metadata.remove_alias_records(write_result.removed_records)
    inserted_groups = _groups_for_inserted_records(plan.groups, write_result.inserted_records)
    metadata.upsert_groups(
        inserted_groups,
        merge_aliases=not plan.replace_source and plan.replace_display_names is None,
    )
    stop_search_host()
    logger.info(
        "Alias plan applied: removed=%s inserted=%s backup=%s",
        write_result.removed,
        write_result.inserted,
        backup,
    )
    return ApplyResult(backup=backup, removed=write_result.removed, inserted=write_result.inserted)


def managed_alias_rows(
    settings: AppSettings,
    kinds: list[str] | None = None,
) -> list[ManagedRow]:
    return managed_rows(resolve_db_path(settings.db), sources_from_kinds(kinds))


def remove_managed_aliases(
    settings: AppSettings,
    *,
    display_names: set[str] | None = None,
    kinds: list[str] | None = None,
    create_backup_requested: bool = True,
) -> ApplyResult:
    logger.info(
        "Removing managed aliases: display_names=%s kinds=%s backup_requested=%s",
        display_names,
        kinds,
        create_backup_requested,
    )
    db_path, metadata, backup = _prepare_mutation(
        settings,
        "remove-managed",
        create_backup_requested=create_backup_requested,
    )
    sources = sources_from_kinds(kinds)
    removed = remove_managed_rows(db_path, display_names, sources)
    metadata.remove_groups(display_names, sources)
    stop_search_host()
    logger.info("Managed aliases removed: removed=%s backup=%s", removed, backup)
    return ApplyResult(backup=backup, removed=removed, inserted=0)


def remove_managed_alias_records_exact(
    settings: AppSettings,
    records: set[tuple[str, str, str]],
    *,
    create_backup_requested: bool = True,
) -> ApplyResult:
    logger.info(
        "Removing exact managed alias records: count=%s backup_requested=%s", len(records), create_backup_requested
    )
    db_path, metadata, backup = _prepare_mutation(
        settings,
        "remove-managed",
        create_backup_requested=create_backup_requested,
    )
    removed = remove_managed_alias_records(db_path, records)
    metadata.remove_alias_records(records)
    stop_search_host()
    logger.info("Exact managed alias records removed: removed=%s backup=%s", removed, backup)
    return ApplyResult(backup=backup, removed=removed, inserted=0)


def managed_summary(settings: AppSettings) -> ManagedSummary:
    db_path: Path | None
    rows: list[ManagedRow]
    scan: ScanResult | None
    try:
        db_path = resolve_db_path(settings.db)
        rows = managed_rows(db_path)
        scan = scan_database(settings)
    except DB_ERRORS:
        db_path = None
        rows = []
        scan = None
    return ManagedSummary(
        db_path=db_path,
        metadata_groups=_metadata(settings).groups(),
        rows=rows,
        scan=scan,
    )


def backup_infos(settings: AppSettings) -> list[BackupInfo]:
    return [_backup_info(item) for item in _metadata(settings).backups()]


def latest_backup(settings: AppSettings) -> Path | None:
    return _metadata(settings).latest_backup()


def restore_backup(settings: AppSettings, backup_path: str | Path) -> ApplyResult:
    db_path = resolve_db_path(settings.db)
    logger.info("Restoring backup: db=%s backup=%s", db_path, backup_path)
    safety = restore_database(db_path, backup_path, _metadata(settings))
    logger.info("Backup restored: safety_backup=%s", safety)
    return ApplyResult(backup=safety)


def delete_backup(settings: AppSettings, backup_path: str | Path) -> DeleteBackupResult:
    path = Path(backup_path)
    metadata = _metadata(settings)
    removed_metadata = metadata.remove_backup(path)
    if removed_metadata == 0:
        raise ValueError(f"Backup is not tracked: {path}")
    removed_file = False
    if path.exists():
        path.unlink()
        removed_file = True
    logger.info("Backup deleted: path=%s removed_file=%s removed_metadata=%s", path, removed_file, removed_metadata)
    return DeleteBackupResult(path=path, removed_file=removed_file, removed_metadata=removed_metadata)


def _groups_for_inserted_records(groups: list[AliasGroup], records: list[AliasRecord]) -> list[AliasGroup]:
    inserted_ids = {id(record) for record in records}
    inserted_groups: list[AliasGroup] = []
    for group in groups:
        aliases = [alias for alias in group.aliases if id(alias) in inserted_ids]
        if aliases:
            inserted_groups.append(
                AliasGroup(
                    display_name=group.display_name,
                    app_id=group.app_id,
                    alias_type=group.alias_type,
                    aliases=aliases,
                    keyboard_map=group.keyboard_map,
                    source=group.source,
                )
            )
    return inserted_groups


def _backup_info(item: dict) -> BackupInfo:
    path = Path(item["path"])
    status, has_error = backup_managed_rows_status(path)
    return BackupInfo(
        path=path,
        reason=item.get("reason") or "unknown",
        created_at=item.get("created_at") or "unknown time",
        status=status,
        has_error=has_error,
    )


def backup_managed_rows_status(path: Path) -> tuple[str, bool]:
    if not path.exists():
        return "missing file", True
    try:
        count = len(managed_rows(path))
    except sqlite3.DatabaseError:
        return "cannot read managed rows", True
    if count == 0:
        return "clean: no managed rows", False
    return f"modified: managed rows: {count}", False


def _included_categories(settings: AppSettings, deny_list: DenyList) -> list[str]:
    included = settings.included_content_categories
    if included is None:
        included = list(deny_list.default_disabled_categories)
    return [category.casefold() for category in included]
