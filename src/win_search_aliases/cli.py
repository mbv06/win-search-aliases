from __future__ import annotations

import argparse
import contextlib
import io
import logging
import sys
from collections.abc import Callable
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as package_version
from pathlib import Path

from . import log_config
from . import operations as ops
from .config import load_deny_list
from .db import (
    DB_ERRORS,
    SOURCE_BY_KIND,
    SOURCE_CUSTOM,
    SOURCE_GENERATED_AUTO,
    SOURCE_GENERATED_MANUAL,
    ManagedRow,
    managed_rows,
    resolve_db_path,
)
from .filters import (
    CYRILLIC_ONLY_NAME_REASON,
    DENY_LIST_REASON,
    EMPTY_DISPLAY_NAME_REASON,
    AppCandidate,
    search_candidates,
)
from .layouts import AUTO_KEYBOARD_MAP
from .managed_sources import (
    alias_sources_by_display_name,
    ordered_managed_sources,
    primary_managed_source,
    row_counts_by_kind,
)
from .metadata import MetadataStore
from .ui import tui
from .ui.console import color, danger, heading, info, muted, success, warning
from .ui.tui import confirm, prompt_custom_alias, select_apps, select_one_app
from .utils import WINDOWS_ONLY_ERROR

_INTERACTIVE_EXIT = 240
logger = logging.getLogger(__name__)
APP_ERRORS = DB_ERRORS + (ValueError,)


@dataclass(frozen=True)
class InteractiveCommand:
    label: str
    handler: Callable[[], int]
    view_output: bool = False


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    try:
        if not argv:
            log_config.configure_logging(app_mode="console")
            _ensure_windows()
            logger.info("Starting interactive CLI")
            return cmd_interactive(argparse.Namespace(db=None, state_dir=None, verbose=False))
        parser = build_parser()
        args = parser.parse_args(argv)
        log_path = log_config.configure_logging(args.state_dir, verbose=args.verbose, app_mode="console")
        logger.debug("CLI logging to %s", log_path)
        _ensure_windows()
        func = getattr(args, "func", None)
        logger.info("Running command: %s", func.__name__ if func else "unknown")
        return args.func(args)
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
        print(f"\n{muted('Interrupted.')}")
        return 130
    except APP_ERRORS as exc:
        logger.exception("Command failed")
        print(danger(f"error: {exc}"), file=sys.stderr)
        return 1


def _ensure_windows() -> None:
    if sys.platform != "win32":
        raise RuntimeError(WINDOWS_ONLY_ERROR)


def metadata_version() -> str:
    try:
        return package_version("win-search-aliases")
    except PackageNotFoundError:
        return "unknown"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="win-search-aliases")
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {metadata_version()}",
    )
    parser.add_argument("--verbose", action="store_true", help="Write debug logs to stderr")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", help="Path to AppsIndex.db")
    common.add_argument("--state-dir", help="Metadata and backup directory")
    common.add_argument(
        "--verbose",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Write debug logs to stderr",
    )
    subparsers = parser.add_subparsers(required=True)

    interactive = subparsers.add_parser("interactive", parents=[common], help="Open the interactive main menu")
    interactive.set_defaults(func=cmd_interactive)

    scan = subparsers.add_parser("scan", parents=[common], help="Scan indexed entries and eligible app candidates")
    scan.add_argument("--deny-list", help="Extra TOML deny-list file")
    add_filter_args(scan)
    scan.set_defaults(func=cmd_scan)

    generate_all = subparsers.add_parser(
        "generate-all", parents=[common], help="Generate aliases for all eligible apps"
    )
    add_generation_args(generate_all)
    generate_all.set_defaults(func=cmd_generate_all)

    auto = subparsers.add_parser("auto", parents=[common], help="Automatically generate aliases for all eligible apps")
    add_generation_args(auto, default_map=AUTO_KEYBOARD_MAP)
    auto.set_defaults(func=cmd_auto)

    generate_selected = subparsers.add_parser(
        "generate-selected", parents=[common], help="Generate aliases for selected apps"
    )
    add_generation_args(generate_selected)
    generate_selected.add_argument("--app", action="append", help="App search text; repeatable")
    generate_selected.set_defaults(func=cmd_generate_selected)

    custom = subparsers.add_parser("add-custom", parents=[common], help="Add custom synonyms for one selected app")
    custom.add_argument("--app", help="App search text")
    custom.add_argument("--alias", dest="aliases", action="append", help="Custom alias")
    custom.add_argument("--deny-list", help="Extra TOML deny-list file")
    add_filter_args(custom)
    custom.add_argument("--dry-run", action="store_true")
    custom.add_argument("-y", "--yes", action="store_true")
    custom.set_defaults(func=cmd_add_custom)

    listed = subparsers.add_parser("list-managed", parents=[common], help="List aliases managed by this tool")
    listed.add_argument("--kind", choices=sorted(SOURCE_BY_KIND), action="append", help="Managed source kind")
    listed.set_defaults(func=cmd_list_managed)

    remove = subparsers.add_parser("remove-managed", parents=[common], help="Remove aliases managed by this tool")
    remove.add_argument("--app", action="append", help="Exact displayName to remove; repeatable")
    remove.add_argument("--kind", choices=sorted(SOURCE_BY_KIND), action="append", help="Managed source kind")
    remove.add_argument("--dry-run", action="store_true")
    remove.add_argument("-y", "--yes", action="store_true")
    remove.set_defaults(func=cmd_remove_managed)

    restore = subparsers.add_parser("restore-backup", parents=[common], help="Restore AppsIndex.db from a backup")
    restore.add_argument("--backup", help="Backup path")
    restore.add_argument("--latest", action="store_true", help="Use latest tracked backup")
    restore.add_argument("--dry-run", action="store_true")
    restore.add_argument("-y", "--yes", action="store_true")
    restore.set_defaults(func=cmd_restore_backup)
    return parser


def add_generation_args(parser: argparse.ArgumentParser, *, default_map: str = "uk-jcuken") -> None:
    parser.set_defaults(default_map=default_map)
    parser.add_argument(
        "--map",
        action="append",
        help=(f"Keyboard map, profile, or 'auto'. Repeatable. Default: {default_map}."),
    )
    parser.add_argument("--include-full-name", action="store_true")
    parser.add_argument("--min-token-length", type=int, default=4)
    parser.add_argument("--stop-word", action="append", default=[])
    parser.add_argument("--deny-list", help="Extra TOML deny-list file")
    add_filter_args(parser)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("-y", "--yes", action="store_true")


def add_filter_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--no-toml-filters",
        dest="toml_filters",
        action="store_false",
        default=True,
        help="Disable all TOML deny-list filters, including exact, pattern, extension, and category rules.",
    )
    parser.add_argument(
        "--include-c1-category",
        action="append",
        default=None,
        metavar="CATEGORY",
        help="Do not exclude a category from default content filtering. Repeatable; adds to bundled defaults.",
    )
    parser.add_argument(
        "--no-c1-default-exclusions",
        dest="default_c1_exclusions",
        action="store_false",
        default=True,
        help=(
            "Disable default category exclusions for game launcher and "
            "autogenerated entries. URL and UNC link filtering stays enabled."
        ),
    )
    parser.add_argument(
        "--include-cyrillic-apps",
        dest="exclude_cyrillic_only",
        action="store_false",
        default=True,
        help="Include apps whose display name has Cyrillic letters.",
    )


def _settings(args: argparse.Namespace) -> ops.AppSettings:
    return ops.AppSettings(
        db=getattr(args, "db", None),
        state_dir=getattr(args, "state_dir", None),
        deny_list=getattr(args, "deny_list", None),
        use_deny_list_filters=_use_toml_filters(args),
        use_default_content_exclusions=_use_default_c1_exclusions(args),
        included_content_categories=_included_c1_categories(args),
        exclude_cyrillic_only_names=_exclude_cyrillic_only_names(args),
    )


def _generation_options(args: argparse.Namespace) -> ops.GenerationOptions:
    return ops.GenerationOptions(
        map_names=args.map,
        default_map=args.default_map,
        include_full_name=args.include_full_name,
        min_token_length=args.min_token_length,
        stop_words=args.stop_word,
    )


def cmd_scan(args: argparse.Namespace) -> int:
    result = ops.scan_database(_settings(args))
    db_path = result.db_path
    deny_list = result.deny_list
    enabled_categories = result.enabled_content_categories
    report = result.report
    total, eligible = report.total, report.eligible
    print(heading("\nScan"))
    print(f"{info('Database:')} {db_path}")
    print(f"{info('Total indexed entries in tiles:')} {total}")
    print(f"{warning('Ignored by filtering rules:')} {report.ignored}")
    _print_filter_breakdown(report.reason_counts, deny_list, enabled_categories)
    print(f"{success('Eligible app candidates:')} {eligible}")
    return 0


def cmd_generate_all(args: argparse.Namespace) -> int:
    settings = _settings(args)
    candidates = ops.eligible_candidates(settings)
    return _generate_for_candidates(
        args, settings, candidates, "generate-all", SOURCE_GENERATED_AUTO, replace_source=True
    )


def cmd_auto(args: argparse.Namespace) -> int:
    args.yes = True
    settings = _settings(args)
    candidates = ops.eligible_candidates(settings)
    print(heading("\nAutomatic generation"))
    print(warning("This will create a backup, replace previous auto aliases, and restart SearchHost."))
    return _generate_for_candidates(args, settings, candidates, "auto", SOURCE_GENERATED_AUTO, replace_source=True)


def cmd_generate_selected(args: argparse.Namespace) -> int:
    settings = _settings(args)
    candidates = ops.eligible_candidates(settings)
    selected: list[AppCandidate] = []
    if args.app:
        for query in args.app:
            matches = search_candidates(candidates, query)
            if matches:
                selected.append(matches[0])
            else:
                print(warning(f"No eligible app matched: {query}"), file=sys.stderr)
    else:
        alias_sources = _managed_alias_sources(settings)
        selected = select_apps(
            candidates,
            aliased_names=set(alias_sources),
            label_for_candidate=_candidate_label_formatter(alias_sources),
        )
    return _generate_for_candidates(args, settings, selected, "generate-selected", SOURCE_GENERATED_MANUAL)


def cmd_add_custom(args: argparse.Namespace) -> int:
    settings = _settings(args)
    candidates = ops.eligible_candidates(settings)
    alias_sources = _managed_alias_sources(settings)
    selected = _select_one(
        candidates,
        args.app,
        aliased_names=set(alias_sources),
        label_for_candidate=_candidate_label_formatter(alias_sources),
    )
    if selected is None:
        return 0
    aliases = (
        _single_custom_alias(args.aliases) if args.aliases is not None else [prompt_custom_alias(selected.display_name)]
    )
    replace_existing = _confirm_custom_alias_replace(settings, selected.display_name, aliases, assume_yes=args.yes)
    if replace_existing is None:
        return 0
    plan = ops.build_custom_alias_plan(settings, selected, aliases, replace_existing=replace_existing)
    return _apply_plan(args, settings, plan)


def _single_custom_alias(aliases: list[str]) -> list[str]:
    return aliases[:1]


def _managed_alias_sources(settings: ops.AppSettings) -> dict[str, set[str]]:
    try:
        db_path = resolve_db_path(settings.db)
        return alias_sources_by_display_name(managed_rows(db_path))
    except DB_ERRORS:
        return {}


def _candidate_label_formatter(alias_sources: dict[str, set[str]]) -> Callable[[AppCandidate], str]:
    def label(candidate: AppCandidate) -> str:
        sources = alias_sources.get(candidate.display_name, set())
        if not sources:
            return candidate.display_name
        primary = primary_managed_source(sources)
        parts = [f"{_managed_source_color(candidate.display_name, primary)} {muted('[')}"]
        parts.extend(_managed_source_short_label(source) for source in ordered_managed_sources(sources))
        parts.append(muted("]"))
        return " ".join(parts)

    return label


def _managed_source_short_label(source: str) -> str:
    if source == SOURCE_CUSTOM:
        return warning("custom")
    if source == SOURCE_GENERATED_MANUAL:
        return color("manual", "blue")
    if source == SOURCE_GENERATED_AUTO:
        return success("auto")
    return muted(source)


def _custom_alias_rows(settings: ops.AppSettings, display_name: str) -> list[ManagedRow]:
    try:
        db_path = resolve_db_path(settings.db)
        return [row for row in managed_rows(db_path, {SOURCE_CUSTOM}) if row[0].casefold() == display_name.casefold()]
    except DB_ERRORS:
        return []


def _confirm_custom_alias_replace(
    settings: ops.AppSettings,
    display_name: str,
    aliases: list[str],
    *,
    assume_yes: bool,
) -> bool | None:
    rows = _custom_alias_rows(settings, display_name)
    if not rows:
        return False
    existing = ", ".join(sorted({row[1] for row in rows}))
    new_alias = aliases[0] if aliases else ""
    if not confirm(
        f"Replace existing custom alias for {display_name}? Existing: {existing}. New: {new_alias}",
        assume_yes=assume_yes,
        default=True,
    ):
        print(warning("Cancelled."))
        return None
    return True


def cmd_list_managed(args: argparse.Namespace) -> int:
    db_path = resolve_db_path(args.db)
    sources = ops.sources_from_kinds(args.kind)
    metadata = MetadataStore(args.state_dir)
    groups = [group for group in metadata.groups() if sources is None or _metadata_group_source(group) in sources]
    if groups:
        print(heading("\nManaged aliases"))
        for group in groups:
            source = _metadata_group_source(group)
            print(_managed_source_color(group.get("display_name", "Unknown app"), source))
            if group.get("alias_type") == "generated":
                print(f"  {info('source:')} {_managed_source_color(source, source)}")
                print(f"  {info('generated from layout:')} {group.get('keyboard_map')}")
                print(f"  {info('token aliases:')}")
                for alias in group.get("aliases", []):
                    token = alias.get("token") or "full-name"
                    print(f"    {muted(token)} -> {alias.get('synonym')}")
            else:
                print(f"  {info('custom aliases:')}")
                print(f"  {info('source:')} {_managed_source_color(source, source)}")
                for alias in group.get("aliases", []):
                    print(f"    {alias.get('synonym')}")
    else:
        rows = managed_rows(db_path, sources)
        if not rows:
            print(warning("No managed aliases found."))
            return 0
        print(heading("\nManaged aliases"))
        for display_name, synonym, _rank, source in rows:
            print(f"{_managed_source_color(display_name, source)}: {synonym} {_managed_source_label(source)}")
    return 0


def cmd_remove_managed(args: argparse.Namespace) -> int:
    settings = _settings(args)
    db_path = resolve_db_path(args.db)
    sources = ops.sources_from_kinds(getattr(args, "kind", None))
    names = set(args.app) if getattr(args, "app", None) else None
    rows = managed_rows(db_path, sources)
    if names:
        rows = [row for row in rows if row[0] in names]
    preview_text = _managed_rows_preview_text(rows)
    if getattr(args, "preview_in_viewer", False):
        tui.view_text(preview_text, title="managed aliases to remove")
    else:
        print(preview_text)
    if args.dry_run:
        return 0
    if not rows:
        return 0
    if not confirm("Create backup and remove these managed aliases?", assume_yes=args.yes):
        print(warning("Cancelled."))
        return 0
    result = ops.remove_managed_aliases(
        settings,
        display_names=names,
        kinds=getattr(args, "kind", None),
        create_backup_requested=True,
    )
    result_text = "\n".join(
        [
            success("Managed aliases removed."),
            f"{success('Backup created:')} {result.backup}",
            f"{success('Removed rows:')} {result.removed}",
        ]
    )
    if getattr(args, "result_in_viewer", False):
        tui.view_text(result_text, title="remove managed aliases")
    else:
        print(result_text)
    return 0


def cmd_restore_backup(args: argparse.Namespace) -> int:
    db_path = resolve_db_path(args.db)
    settings = _settings(args)
    if args.latest:
        backup = ops.latest_backup(settings)
        if backup is None:
            raise RuntimeError("No tracked backups found.")
    elif args.backup:
        backup = Path(args.backup)
    else:
        backup = _select_backup(settings)
        if backup is None:
            return 0
    print(f"\n{heading('Restore')} {db_path} {muted('from')} {backup}")
    if args.dry_run:
        return 0
    if not confirm("Create a safety backup and restore the database?", assume_yes=args.yes):
        print(warning("Cancelled."))
        return 0
    result = ops.restore_backup(settings, backup)
    print(f"{success('Safety backup created:')} {result.backup}")
    print(success("Database restored."))
    return 0


def cmd_interactive(args: argparse.Namespace) -> int:
    return _cmd_interactive_tui(args, tui.make_tui_screen())


def _interactive_commands(args: argparse.Namespace) -> list[InteractiveCommand]:
    args.toml_filters = getattr(args, "toml_filters", True)
    args.default_c1_exclusions = getattr(args, "default_c1_exclusions", True)
    args.include_c1_category = getattr(args, "include_c1_category", None)
    args.exclude_cyrillic_only = getattr(args, "exclude_cyrillic_only", True)
    return [
        InteractiveCommand("scan database", lambda: cmd_scan(_menu_args(args)), view_output=True),
        InteractiveCommand(
            "generate aliases for selected apps",
            lambda: cmd_generate_selected(_menu_args(args, generation=True)),
        ),
        InteractiveCommand(
            "add custom synonym",
            lambda: cmd_add_custom(_menu_args(args, aliases=None, app=None, dry_run=False, yes=False)),
        ),
        InteractiveCommand(
            "list managed aliases", lambda: cmd_list_managed(_menu_args(args, kind=None)), view_output=True
        ),
        InteractiveCommand(
            "remove managed aliases",
            lambda: _interactive_remove_managed(args),
        ),
        InteractiveCommand(
            "restore backup",
            lambda: cmd_restore_backup(_menu_args(args, latest=False, backup=None, dry_run=False, yes=False)),
        ),
        InteractiveCommand(
            "auto-generate for all eligible apps",
            lambda: _interactive_auto_generate(args),
        ),
        InteractiveCommand("filter settings", lambda: _configure_filter_categories(args)),
    ]


def _cmd_interactive_tui(args: argparse.Namespace, screen: tui.LiveScreen) -> int:
    commands = _interactive_commands(args)
    header = [
        heading("win-search-aliases"),
        muted("Internal Windows Search aliases manager"),
        "",
        *_registered_summary_lines(args),
    ]
    menu_items = [*[command.label for command in commands], "quit"]

    while True:
        choice = tui.select_index(
            menu_items,
            title="Commands",
            instructions="Up/down: move, Enter: run, Esc: quit",
            header_lines=header,
            screen=screen,
        )
        if choice is None or choice == len(commands):
            screen.close(clear=True)
            print(muted("Bye."))
            return 0

        screen.close(clear=True)
        try:
            result = _run_interactive_command(commands[choice])
        except APP_ERRORS as exc:
            print(danger(f"error: {exc}"), file=sys.stderr)
            continue
        if result == _INTERACTIVE_EXIT:
            return 0


def _run_interactive_command(command: InteractiveCommand) -> int:
    if command.view_output:
        return _run_tui_output_command(command.label, command.handler)
    return command.handler()


def _interactive_auto_generate(args: argparse.Namespace) -> int:
    if not confirm(
        "Auto-generate aliases for all eligible apps?",
        assume_yes=False,
        default=True,
    ):
        return 0
    if not confirm(
        "Create backup, replace previous auto aliases, and restart SearchHost?",
        assume_yes=False,
        default=True,
    ):
        return 0
    auto_args = _menu_args(args, generation=True, yes=True, map="auto")
    result = _run_tui_output_command(
        "auto-generate for all eligible apps",
        lambda: cmd_auto(auto_args),
        completion_message="All done OK. Press Enter to finish.",
    )
    return _INTERACTIVE_EXIT if result == 0 else result


def _use_default_c1_exclusions(args: argparse.Namespace) -> bool:
    return bool(getattr(args, "default_c1_exclusions", True))


def _use_toml_filters(args: argparse.Namespace) -> bool:
    return bool(getattr(args, "toml_filters", True))


def _exclude_cyrillic_only_names(args: argparse.Namespace) -> bool:
    return bool(getattr(args, "exclude_cyrillic_only", True))


def _included_c1_categories(args: argparse.Namespace) -> list[str] | None:
    cats = getattr(args, "include_c1_category", None)
    if cats is None:
        return None
    included = [category.casefold() for category in cats]
    if not included:
        return []
    deny_list = load_deny_list(getattr(args, "deny_list", None))
    merged: list[str] = []
    seen: set[str] = set()
    for category in [*deny_list.default_disabled_categories, *included]:
        if category not in seen:
            seen.add(category)
            merged.append(category)
    return merged


def _enabled_c1_categories(args: argparse.Namespace, deny_list) -> set[str]:
    return ops.enabled_content_categories(_settings(args), deny_list)


def _print_filter_breakdown(reason_counts: dict[str, int], deny_list, enabled_categories: set[str]) -> None:
    for line in _filter_breakdown_lines(reason_counts, deny_list, enabled_categories):
        print(line)


def _filter_breakdown_lines(reason_counts: dict[str, int], deny_list, enabled_categories: set[str]) -> list[str]:
    lines: list[str] = []
    missing = reason_counts.get(EMPTY_DISPLAY_NAME_REASON, 0)
    if missing > 0:
        lines.append(f"  {muted('-')} {info('missing display name:')} {missing}")

    deny = reason_counts.get(DENY_LIST_REASON, 0)
    if deny > 0:
        lines.append(f"  {muted('-')} {info('deny list:')} {deny}")

    cyrillic = reason_counts.get(CYRILLIC_ONLY_NAME_REASON, 0)
    if cyrillic > 0:
        lines.append(f"  {muted('-')} {info('Cyrillic names:')} {cyrillic}")

    for category in deny_list.content_category_names():
        if category in enabled_categories:
            key = f"c1:{category}"
            lines.append(f"  {muted('-')} {info(f'optional ({category}):')} {reason_counts.get(key, 0)}")
    return lines


def _configure_filter_categories(args: argparse.Namespace) -> int:
    return _configure_filter_categories_tui(args, tui.make_tui_screen())


def _filter_args(
    args: argparse.Namespace,
    *,
    toml_filters: bool,
    default_c1_exclusions: bool,
    include_c1_category: list[str],
    exclude_cyrillic_only: bool,
) -> argparse.Namespace:
    return argparse.Namespace(
        **{
            **vars(args),
            "toml_filters": toml_filters,
            "default_c1_exclusions": default_c1_exclusions,
            "include_c1_category": list(include_c1_category),
            "exclude_cyrillic_only": exclude_cyrillic_only,
        }
    )


def _configured_filtered_categories(
    names: list[str],
    *,
    default_c1_exclusions: bool,
    include_c1_category: list[str],
) -> set[str]:
    if not default_c1_exclusions:
        return set()
    included = {category.casefold() for category in include_c1_category}
    return {category for category in names if category.casefold() not in included}


def _configure_filter_categories_tui(args: argparse.Namespace, screen: tui.LiveScreen) -> int:
    deny_list = load_deny_list(getattr(args, "deny_list", None))
    names = list(deny_list.content_category_names())
    toml_filters = _use_toml_filters(args)
    default_c1_exclusions = _use_default_c1_exclusions(args)
    cats = _included_c1_categories(args)
    include_c1_category = list(cats) if cats is not None else list(deny_list.default_disabled_categories)
    exclude_cyrillic_only = _exclude_cyrillic_only_names(args)
    cursor = 0

    def current_args() -> argparse.Namespace:
        return _filter_args(
            args,
            toml_filters=toml_filters,
            default_c1_exclusions=default_c1_exclusions,
            include_c1_category=include_c1_category,
            exclude_cyrillic_only=exclude_cyrillic_only,
        )

    def toggle_category(category: str) -> None:
        nonlocal default_c1_exclusions, include_c1_category
        included = {value.casefold() for value in include_c1_category}

        category_name = category.casefold()
        if category_name in included:
            included.remove(category_name)
        else:
            included.add(category_name)
        default_c1_exclusions = True
        include_c1_category = sorted(included)

    def render() -> str:
        enabled_categories = _enabled_c1_categories(current_args(), deny_list)
        configured_filtered = _configured_filtered_categories(
            names,
            default_c1_exclusions=default_c1_exclusions,
            include_c1_category=include_c1_category,
        )
        counts = ops.filter_category_counts(_settings(current_args()))
        lines = [
            heading("Filter settings"),
            muted("Up/down: move, Space: toggle, a: include all, r: reset, Enter: apply, Esc: cancel"),
            "",
        ]
        rows = [
            ("TOML deny-list filters", toml_filters, toml_filters),
            ("Cyrillic names", exclude_cyrillic_only, exclude_cyrillic_only),
        ]
        for category in names:
            is_filtering = toml_filters and category in enabled_categories
            is_configured = category in configured_filtered
            rows.append((f"{category}: {counts.get(category, 0)}", is_configured, is_filtering))

        for index, (label, checked, is_filtering) in enumerate(rows):
            pointer = info(">") if index == cursor else " "
            check = success("[x]") if checked else "[ ]"
            if index > 1 and checked and not toml_filters:
                status = muted("paused")
            else:
                status = warning("filtering") if is_filtering else success("included")
            lines.append(f"  {pointer} {check} {label} {muted('[')}{status}{muted(']')}")
            if index == 1:
                lines.extend(["", heading("App Categories")])

        return "\n".join(lines)

    try:
        for key in tui.menu_loop(screen, render):
            cursor = max(0, min(cursor, len(names) + 1))
            if key in {"up", "k"}:
                cursor = (cursor - 1) % (len(names) + 2)
                continue
            if key in {"down", "j", "tab"}:
                cursor = (cursor + 1) % (len(names) + 2)
                continue
            if key == "space":
                if cursor == 0:
                    toml_filters = not toml_filters
                elif cursor == 1:
                    exclude_cyrillic_only = not exclude_cyrillic_only
                else:
                    toggle_category(names[cursor - 2])
                continue
            if key == "a":
                default_c1_exclusions = False
                include_c1_category = names
                continue
            if key == "r":
                default_c1_exclusions = True
                include_c1_category = list(deny_list.default_disabled_categories)
                continue
            if key == "enter":
                args.toml_filters = toml_filters
                args.default_c1_exclusions = default_c1_exclusions
                args.include_c1_category = list(include_c1_category)
                args.exclude_cyrillic_only = exclude_cyrillic_only
                return 0
            if key in {"esc", "q"}:
                return 0
    finally:
        screen.close(clear=True)
    return 0


class _TuiCaptureBuffer(io.StringIO):
    def isatty(self) -> bool:
        return True


def _run_tui_output_command(
    label: str,
    handler: Callable[[], int],
    *,
    completion_message: str | None = None,
) -> int:
    stdout_buffer = _TuiCaptureBuffer()
    stderr_buffer = _TuiCaptureBuffer()
    with contextlib.redirect_stdout(stdout_buffer), contextlib.redirect_stderr(stderr_buffer):
        result = handler()
    output = stdout_buffer.getvalue()
    error_output = stderr_buffer.getvalue()
    viewer_text = output
    if error_output:
        viewer_text = f"{viewer_text}\n{error_output}" if viewer_text else error_output
    viewer_text = viewer_text.strip("\n") or muted("No output.")
    if completion_message:
        viewer_text = f"{viewer_text}\n\n{success(completion_message)}"
    tui.view_text(viewer_text, title=label)
    return result


def _capture_tui_text(writer: Callable[[], None]) -> str:
    buffer = _TuiCaptureBuffer()
    with contextlib.redirect_stdout(buffer):
        writer()
    return buffer.getvalue().strip("\n")


def _generate_for_candidates(
    args: argparse.Namespace,
    settings: ops.AppSettings,
    candidates: list[AppCandidate],
    reason: str,
    source: str,
    replace_source: bool = False,
) -> int:
    plan = ops.build_generated_alias_plan(
        settings,
        _generation_options(args),
        candidates,
        reason,
        source,
        replace_source=replace_source,
    )
    print(f"{info('Keyboard maps:')} {', '.join(plan.selected_maps)}")
    return _apply_plan(args, settings, plan)


def _apply_plan(args: argparse.Namespace, settings: ops.AppSettings, plan: ops.AliasPlan) -> int:
    print(f"{info('Alias groups:')} {len(plan.groups)}")
    print(f"{info('Alias rows:')} {plan.total_aliases}")
    print(f"{info('Source:')} {plan.source}")
    _print_preview(plan.groups)
    if args.dry_run:
        return 0
    if not plan.groups and not plan.replace_source:
        return 0
    create_backup_requested = confirm(
        "Create a backup before applying these aliases?",
        assume_yes=args.yes,
        default=True,
    )
    if not confirm("Apply these aliases?", assume_yes=args.yes, default=True):
        print(warning("Cancelled."))
        return 0
    result = ops.apply_alias_plan(plan, settings, create_backup_requested=create_backup_requested)
    if result.backup is not None:
        print(f"{success('Backup created:')} {result.backup}")
    else:
        print(warning("Backup skipped."))
    if plan.replace_source or plan.replace_display_names:
        print(f"{warning('Removed old rows:')} {result.removed}")
    print(f"{success('Inserted new rows:')} {result.inserted}")
    return 0


def _print_preview(groups: list, limit: int = 20) -> None:
    shown = 0
    for group in groups:
        if shown >= limit:
            remaining = sum(len(item.aliases) for item in groups) - shown
            print(muted(f"... {remaining} more aliases"))
            return
        label = group.display_name
        if getattr(group, "keyboard_map", None):
            label = f"{label} [{group.keyboard_map}]"
        print(success(label))
        for alias in group.aliases:
            if shown >= limit:
                break
            label = f"{alias.token} -> " if alias.token else ""
            print(f"  {muted(label)}{alias.synonym}")
            shown += 1


def _managed_rows_preview_text(rows: list[ManagedRow]) -> str:
    def write_preview() -> None:
        print(f"\n{warning('Managed rows to remove:')} {len(rows)}")
        _print_managed_rows_preview(rows)

    return _capture_tui_text(write_preview)


def _print_managed_rows_preview(rows: list[ManagedRow], limit: int = 20) -> None:
    for index, (display_name, synonym, _rank, source) in enumerate(rows[:limit], start=1):
        print(
            f"  {muted(str(index) + '.')} "
            f"{_managed_source_color(display_name, source)}: {synonym} {_managed_source_label(source)}"
        )
    remaining = len(rows) - limit
    if remaining > 0:
        print(muted(f"... {remaining} more aliases"))


def _select_one(
    candidates: list[AppCandidate],
    query: str | None,
    *,
    aliased_names: set[str] | None = None,
    label_for_candidate: Callable[[AppCandidate], str] | None = None,
) -> AppCandidate | None:
    if query:
        matches = search_candidates(candidates, query)
        if not matches:
            raise RuntimeError(f"No eligible app matched: {query}")
        if len(matches) > 1:
            print(warning(f"Multiple matches for {query!r}; using {matches[0].display_name}."))
        return matches[0]
    return select_one_app(candidates, aliased_names=aliased_names, label_for_candidate=label_for_candidate)


def _interactive_remove_managed(args: argparse.Namespace) -> int:
    db_path = resolve_db_path(args.db)
    rows = managed_rows(db_path)
    counts = _managed_row_counts_by_kind(rows)
    kinds = _select_remove_kinds(counts)
    if kinds is None:
        print(warning("Cancelled."))
        return 0
    return cmd_remove_managed(
        _menu_args(args, app=None, kind=kinds, dry_run=False, yes=False, preview_in_viewer=True, result_in_viewer=True)
    )


def _select_remove_kinds(counts: dict[str, int] | None = None) -> list[str] | None:
    options = [
        ("auto-generated aliases", ["auto"]),
        ("manually added aliases", ["manual"]),
        ("custom synonyms", ["custom"]),
        ("all managed aliases", []),
    ]
    labels = []
    for label, kinds in options:
        count = _remove_option_count(kinds, counts)
        count_text = f" {warning(_format_row_count(count))}" if count is not None else ""
        labels.append(f"{label}{count_text}")

    choice = tui.select_index(
        labels,
        title="Remove managed aliases",
        instructions="Up/down: move, Enter: choose, Esc: cancel",
    )
    if choice is None:
        return None
    return options[choice][1]


def _managed_row_counts_by_kind(rows: list[ManagedRow]) -> dict[str, int]:
    return row_counts_by_kind(rows)


def _remove_option_count(kinds: list[str], counts: dict[str, int] | None) -> int | None:
    if counts is None:
        return None
    if not kinds:
        return sum(counts.values())
    return sum(counts[kind] for kind in kinds)


def _format_row_count(count: int | None) -> str:
    if count is None:
        return ""
    noun = "row" if count == 1 else "rows"
    return f"({count} {noun})"


def _metadata_group_source(group: dict) -> str:
    return group.get("source") or ""


def _managed_source_color(text: object, source: str) -> str:
    color_name = tui.SOURCE_STYLES.get(source, ("gray", source))[0]
    if color_name == "green":
        return success(text)
    if color_name == "blue":
        return color(text, "blue")
    if color_name == "yellow":
        return warning(text)
    return muted(text)


def _managed_source_label(source: str) -> str:
    label = tui.SOURCE_STYLES.get(source, ("gray", source))[1]
    return muted("[") + _managed_source_color(label, source) + muted("]")


def _select_backup(settings: ops.AppSettings) -> Path | None:
    backups = ops.backup_infos(settings)
    if not backups:
        raise RuntimeError("No tracked backups found.")

    labels: list[str] = []
    for backup in backups:
        color_status = danger if backup.has_error else warning if backup.status.startswith("modified:") else success
        details = f"{muted(f'[{backup.created_at}; {backup.reason};')} {color_status(backup.status)}{muted(']')}"
        labels.append(f"{color_status(backup.path.name)} {details}")

    def footer_lines(index: int) -> list[str]:
        backup = backups[index]
        return [muted(str(backup.path))]

    choice = tui.select_index(
        labels,
        title="Backups",
        instructions="Up/down: move, Enter: restore, Esc: cancel",
        footer_lines=footer_lines,
        page_size=8,
    )
    if choice is None:
        return None
    return backups[choice].path


def _print_registered_summary(args: argparse.Namespace) -> None:
    for line in _registered_summary_lines(args):
        print(line)


def _registered_summary_lines(args: argparse.Namespace) -> list[str]:
    args.toml_filters = getattr(args, "toml_filters", True)
    args.default_c1_exclusions = getattr(args, "default_c1_exclusions", True)
    args.include_c1_category = getattr(args, "include_c1_category", None)
    args.exclude_cyrillic_only = getattr(args, "exclude_cyrillic_only", True)
    summary = ops.managed_summary(_settings(args))
    rows = summary.rows
    groups = summary.metadata_groups
    lines = [
        heading("Registered aliases"),
        f"{info('Metadata groups:')} {len(groups)}",
        f"{info('Database rows:')} {len(rows)}",
    ]
    for source in sorted(SOURCE_BY_KIND.values()):
        count = sum(1 for row in rows if row[3] == source)
        lines.append(f"  {_managed_source_color(source + ':', source)} {count}")
    lines.append("")
    lines.append(heading("Current filtering"))
    if summary.scan is None:
        lines.append(muted("Database unavailable; run scan for details after fixing the database path."))
        return lines
    report = summary.scan.report
    lines.append(f"{info('Total indexed entries:')} {report.total}")
    lines.append(f"{warning('Ignored by filtering rules:')} {report.ignored}")
    lines.extend(
        _filter_breakdown_lines(
            report.reason_counts,
            summary.scan.deny_list,
            summary.scan.enabled_content_categories,
        )
    )
    lines.append(f"{success('Eligible app candidates:')} {report.eligible}")
    return lines


def _menu_args(args: argparse.Namespace, generation: bool = False, **overrides: object) -> argparse.Namespace:
    cats = getattr(args, "include_c1_category", None)
    values = {
        "db": args.db,
        "state_dir": args.state_dir,
        "toml_filters": getattr(args, "toml_filters", True),
        "default_c1_exclusions": getattr(args, "default_c1_exclusions", True),
        "include_c1_category": list(cats) if cats is not None else None,
        "exclude_cyrillic_only": getattr(args, "exclude_cyrillic_only", True),
        "deny_list": None,
    }
    if generation:
        values.update(
            {
                "default_map": "uk-jcuken",
                "map": "uk-jcuken",
                "include_full_name": False,
                "min_token_length": 4,
                "stop_word": [],
                "dry_run": False,
                "yes": False,
                "app": None,
            }
        )
    values.update(overrides)
    return argparse.Namespace(**values)
