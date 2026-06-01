import argparse
import sqlite3
from pathlib import Path
from types import SimpleNamespace

from win_search_aliases import cli
from win_search_aliases.aliases import custom_alias_group
from win_search_aliases.db import SOURCE_CUSTOM, SOURCE_GENERATED_AUTO, SOURCE_GENERATED_MANUAL, ManagedRow
from win_search_aliases.filters import AppCandidate, FilterReport
from win_search_aliases.metadata import MetadataStore
from win_search_aliases.ui import tui as interactive  # type: ignore
from win_search_aliases.ui.tui import confirm, prompt_custom_alias, select_apps, select_one_app


class FakeScreen:
    def __init__(self, keys: list[str], prompts: list[str] | None = None) -> None:
        self.available = True
        self._keys = iter(keys)
        self._prompts = iter(prompts or [])
        self.renders: list[str] = []
        self.prompt_messages: list[str] = []
        self.prompt_contexts: list[str] = []
        self.closed: list[bool] = []

    def render(self, text: str) -> None:
        self.renders.append(text)

    def read_key(self) -> str:
        try:
            return next(self._keys)
        except StopIteration as exc:
            raise AssertionError("FakeScreen ran out of keys") from exc

    def prompt(self, prompt: str) -> str:
        self.prompt_messages.append(prompt)
        try:
            return next(self._prompts)
        except StopIteration as exc:
            raise AssertionError("FakeScreen ran out of prompt answers") from exc

    def prompt_with_context(self, text: str, prompt: str) -> str:
        self.prompt_contexts.append(text)
        return self.prompt(prompt)

    def close(self, *, clear: bool = False) -> None:
        self.closed.append(clear)


def test_main_handles_keyboard_interrupt_without_traceback(monkeypatch, capsys) -> None:
    def interrupt(_args: argparse.Namespace) -> int:
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "_ensure_windows", lambda: None)
    monkeypatch.setattr(cli, "cmd_interactive", interrupt)

    assert cli.main([]) == 130
    output = capsys.readouterr()
    assert "Interrupted." in output.out
    assert "Traceback" not in output.err


def test_main_rejects_non_windows(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli.sys, "platform", "linux")

    assert cli.main(["scan"]) == 1
    output = capsys.readouterr()
    assert cli.WINDOWS_ONLY_ERROR in output.err


def test_auto_command_assumes_yes(monkeypatch, tmp_path) -> None:
    seen = {}

    monkeypatch.setattr(cli, "_ensure_windows", lambda: None)
    monkeypatch.setattr(
        cli.ops,
        "eligible_candidates",
        lambda _settings: [],
    )

    def fake_generate(args, settings, candidates, reason, source, replace_source=False):
        seen["yes"] = args.yes
        seen["reason"] = reason
        seen["replace_source"] = replace_source
        return 0

    monkeypatch.setattr(cli, "_generate_for_candidates", fake_generate)

    assert cli.main(["auto"]) == 0
    assert seen == {"yes": True, "reason": "auto", "replace_source": True}


def test_generation_builds_alias_groups_for_each_selected_keyboard_map(monkeypatch, tmp_path) -> None:
    seen = {}
    args = argparse.Namespace(
        map=["auto"],
        default_map="uk-jcuken",
        min_token_length=4,
        include_full_name=False,
        stop_word=[],
    )

    monkeypatch.setattr(
        cli.ops,
        "load_keyboard_maps",
        lambda: {
            "ru-jcuken": {"s": "ы", "p": "з", "o": "щ", "t": "е", "i": "ш", "f": "а", "y": "н"},
            "uk-jcuken": {"s": "і", "p": "з", "o": "щ", "t": "е", "i": "ш", "f": "а", "y": "н"},
        },
    )
    monkeypatch.setattr(cli.ops, "load_profiles", lambda: {"ru-jcuken": ["00000419"], "uk-jcuken": ["00000422"]})
    monkeypatch.setattr(
        cli.ops,
        "resolve_keyboard_map_names",
        lambda *_args, **_kwargs: ["ru-jcuken", "uk-jcuken"],
    )

    def fake_apply_plan(args, settings, plan):
        seen["maps"] = [group.keyboard_map for group in plan.groups]
        seen["aliases"] = [group.aliases[0].synonym for group in plan.groups]
        return 0

    monkeypatch.setattr(cli, "_apply_plan", fake_apply_plan)
    db_path = tmp_path / "AppsIndex.db"
    db_path.write_bytes(b"")

    assert (
        cli._generate_for_candidates(
            args,
            cli.ops.AppSettings(db=db_path),
            [AppCandidate("Spotify", "spotify-app", 1)],
            "unit-test",
            "UnitTestSource",
        )
        == 0
    )
    assert seen == {
        "maps": ["ru-jcuken", "uk-jcuken"],
        "aliases": ["ызщешан", "ізщешан"],
    }


def test_select_apps_accepts_enter_after_selection(monkeypatch) -> None:
    screen = FakeScreen(["space", "enter"])
    candidates = [AppCandidate("Google Chrome", "chrome", 1)]

    monkeypatch.setattr(interactive, "make_tui_screen", lambda: screen)

    selected = select_apps(candidates)

    assert [candidate.display_name for candidate in selected] == ["Google Chrome"]
    assert any("[x]" in render for render in screen.renders)


def test_confirm_uses_explicit_default_on_enter(monkeypatch) -> None:
    screen = FakeScreen(["enter"])

    monkeypatch.setattr(interactive, "make_tui_screen", lambda: screen)

    assert confirm("Create backup?", default=True) is True
    assert any("(default)" in render for render in screen.renders)


def test_select_apps_tui_supports_filtering_and_search(monkeypatch) -> None:
    screen = FakeScreen(["down", "u", "s", "space", "enter"], prompts=["chrome"])
    candidates = [
        AppCandidate("Google Chrome", "chrome", 1),
        AppCandidate("Spotify", "spotify", 2),
    ]

    monkeypatch.setattr(interactive, "make_tui_screen", lambda: screen)

    selected = select_apps(candidates, aliased_names={"Spotify"})

    assert [candidate.display_name for candidate in selected] == ["Google Chrome"]
    assert any("unaliased only" in render for render in screen.renders)
    assert "Search query>" in screen.prompt_messages[0]


def test_select_one_app_chooses_current_row_without_checkboxes(monkeypatch) -> None:
    screen = FakeScreen(["down", "enter"])
    candidates = [
        AppCandidate("Google Chrome", "chrome", 1),
        AppCandidate("Spotify", "spotify", 2),
    ]

    monkeypatch.setattr(interactive, "make_tui_screen", lambda: screen)

    selected = select_one_app(candidates)

    assert selected == candidates[1]
    assert any("Select one app" in render for render in screen.renders)
    assert all("[x]" not in render and "[ ]" not in render for render in screen.renders)


def test_select_one_app_cancel_returns_none(monkeypatch) -> None:
    screen = FakeScreen(["esc"])
    candidates = [AppCandidate("Google Chrome", "chrome", 1)]

    monkeypatch.setattr(interactive, "make_tui_screen", lambda: screen)

    assert select_one_app(candidates) is None


def test_confirm_tui_can_change_selection_with_tab(monkeypatch) -> None:
    screen = FakeScreen(["tab", "enter"])

    monkeypatch.setattr(interactive, "make_tui_screen", lambda: screen)

    assert confirm("Apply aliases?", default=True) is False
    assert any("Apply aliases?" in render for render in screen.renders)


def test_prompt_custom_alias_keeps_app_context(monkeypatch) -> None:
    screen = FakeScreen([], prompts=["browser"])

    monkeypatch.setattr(interactive, "make_tui_screen", lambda: screen)

    alias = prompt_custom_alias("Google Chrome")

    assert alias == "browser"
    assert all("Google Chrome" in context for context in screen.prompt_contexts)
    assert "not entered yet" in screen.prompt_contexts[0]
    assert "Custom alias>" in screen.prompt_messages[0]


def test_prompt_custom_alias_reprompts_on_blank(monkeypatch) -> None:
    screen = FakeScreen([], prompts=["", "browser"])

    monkeypatch.setattr(interactive, "make_tui_screen", lambda: screen)

    assert prompt_custom_alias("Google Chrome") == "browser"
    assert "Enter one alias" in screen.prompt_contexts[1]


def test_add_custom_collects_one_alias(monkeypatch, tmp_path) -> None:
    seen = {}
    args = argparse.Namespace(
        db=None,
        state_dir=str(tmp_path / "state"),
        deny_list=None,
        default_c1_exclusions=True,
        include_c1_category=[],
        app="Chrome",
        aliases=None,
        dry_run=False,
        yes=False,
    )

    monkeypatch.setattr(
        cli.ops,
        "eligible_candidates",
        lambda _settings: [AppCandidate("Google Chrome", "chrome", 1)],
    )

    def fake_build_custom_alias_plan(settings, candidate, aliases, *, replace_existing=False):
        seen["aliases"] = aliases
        seen["candidate"] = candidate.display_name
        seen["replace_existing"] = replace_existing
        return cli.ops.AliasPlan(tmp_path / "AppsIndex.db", [], "add-custom", SOURCE_CUSTOM)

    def fake_apply_plan(args, settings, plan):
        seen["reason"] = plan.reason
        return 0

    def fake_prompt_custom_alias(app_name: str) -> str:
        seen["prompt_app"] = app_name
        return "browser"

    monkeypatch.setattr(cli, "prompt_custom_alias", fake_prompt_custom_alias)
    monkeypatch.setattr(cli.ops, "build_custom_alias_plan", fake_build_custom_alias_plan)
    monkeypatch.setattr(cli, "_apply_plan", fake_apply_plan)

    assert cli.cmd_add_custom(args) == 0
    assert seen == {
        "prompt_app": "Google Chrome",
        "aliases": ["browser"],
        "candidate": "Google Chrome",
        "replace_existing": False,
        "reason": "add-custom",
    }


def test_add_custom_uses_only_one_cli_alias(monkeypatch, tmp_path) -> None:
    seen = {}
    args = argparse.Namespace(
        db=None,
        state_dir=str(tmp_path / "state"),
        deny_list=None,
        default_c1_exclusions=True,
        include_c1_category=[],
        app="Chrome",
        aliases=["browser", "work-browser"],
        dry_run=False,
        yes=False,
    )

    monkeypatch.setattr(cli.ops, "eligible_candidates", lambda _settings: [AppCandidate("Google Chrome", "chrome", 1)])

    def fake_build_custom_alias_plan(_settings, _candidate, aliases, **_kwargs):
        seen["aliases"] = aliases
        return cli.ops.AliasPlan(tmp_path / "AppsIndex.db", [], "add-custom", SOURCE_CUSTOM)

    monkeypatch.setattr(cli.ops, "build_custom_alias_plan", fake_build_custom_alias_plan)
    monkeypatch.setattr(cli, "_apply_plan", lambda *args, **kwargs: 0)

    assert cli.cmd_add_custom(args) == 0
    assert seen["aliases"] == ["browser"]


def test_add_custom_cancelled_app_selection_skips_execution(monkeypatch, tmp_path) -> None:
    seen = {}  # type: ignore
    args = argparse.Namespace(
        db=None,
        state_dir=str(tmp_path / "state"),
        deny_list=None,
        default_c1_exclusions=True,
        include_c1_category=[],
        app=None,
        aliases=["browser"],
        dry_run=False,
        yes=False,
    )

    monkeypatch.setattr(cli.ops, "eligible_candidates", lambda _settings: [AppCandidate("Google Chrome", "chrome", 1)])
    monkeypatch.setattr(cli, "select_one_app", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli.ops, "build_custom_alias_plan", lambda *args, **kwargs: seen.__setitem__("planned", True))

    assert cli.cmd_add_custom(args) == 0
    assert seen == {}


def test_generate_selected_passes_managed_source_labels_to_selector(monkeypatch, tmp_path) -> None:
    seen = {}
    candidate = AppCandidate("Google Chrome", "chrome", 1)
    args = argparse.Namespace(
        db=str(tmp_path / "AppsIndex.db"),
        state_dir=str(tmp_path / "state"),
        deny_list=None,
        default_c1_exclusions=True,
        include_c1_category=[],
        app=None,
        map=["uk-jcuken"],
        default_map="uk-jcuken",
        min_token_length=4,
        include_full_name=False,
        stop_word=[],
        dry_run=False,
        yes=False,
    )

    monkeypatch.setattr(cli.ops, "eligible_candidates", lambda _settings: [candidate])
    monkeypatch.setattr(cli, "resolve_db_path", lambda _path: tmp_path / "AppsIndex.db")
    monkeypatch.setattr(
        cli,
        "managed_rows",
        lambda _db_path, sources=None: [ManagedRow("Google Chrome", "browser", 1, SOURCE_CUSTOM)],
    )

    def fake_select_apps(candidates, *, label_for_candidate=None, **_kwargs):
        seen["label"] = label_for_candidate(candidates[0])
        return [candidates[0]]

    monkeypatch.setattr(cli, "select_apps", fake_select_apps)
    monkeypatch.setattr(cli, "_generate_for_candidates", lambda *args, **kwargs: 0)
    monkeypatch.setattr(cli, "warning", lambda text: f"<yellow>{text}</yellow>")

    assert cli.cmd_generate_selected(args) == 0
    assert "<yellow>Google Chrome</yellow>" in seen["label"]
    assert "<yellow>custom</yellow>" in seen["label"]


def test_add_custom_passes_managed_source_labels_to_single_selector(monkeypatch, tmp_path) -> None:
    seen = {}
    candidate = AppCandidate("Google Chrome", "chrome", 1)
    args = argparse.Namespace(
        db=str(tmp_path / "AppsIndex.db"),
        state_dir=str(tmp_path / "state"),
        deny_list=None,
        default_c1_exclusions=True,
        include_c1_category=[],
        app=None,
        aliases=["browser"],
        dry_run=False,
        yes=False,
    )

    monkeypatch.setattr(cli.ops, "eligible_candidates", lambda _settings: [candidate])
    monkeypatch.setattr(cli, "resolve_db_path", lambda _path: tmp_path / "AppsIndex.db")

    def fake_managed_rows(_db_path, sources=None):
        if sources == {SOURCE_CUSTOM}:
            return []
        return [ManagedRow("Google Chrome", "generated", 1, SOURCE_GENERATED_MANUAL)]

    monkeypatch.setattr(cli, "managed_rows", fake_managed_rows)

    def fake_select_one_app(candidates, *, label_for_candidate=None, **_kwargs):
        seen["label"] = label_for_candidate(candidates[0])
        return candidates[0]

    monkeypatch.setattr(cli, "select_one_app", fake_select_one_app)
    monkeypatch.setattr(
        cli.ops,
        "build_custom_alias_plan",
        lambda *args, **kwargs: cli.ops.AliasPlan(tmp_path / "AppsIndex.db", [], "add-custom", SOURCE_CUSTOM),
    )
    monkeypatch.setattr(cli, "_apply_plan", lambda *args, **kwargs: 0)
    monkeypatch.setattr(cli, "color", lambda text, name: f"<{name}>{text}</{name}>")

    assert cli.cmd_add_custom(args) == 0
    assert "<blue>Google Chrome</blue>" in seen["label"]
    assert "<blue>manual</blue>" in seen["label"]


def test_add_custom_offers_to_replace_existing_custom_alias(monkeypatch, tmp_path) -> None:
    seen = {}  # type: ignore
    args = argparse.Namespace(
        db=str(tmp_path / "AppsIndex.db"),
        state_dir=str(tmp_path / "state"),
        deny_list=None,
        default_c1_exclusions=True,
        include_c1_category=[],
        app="Chrome",
        aliases=["browser"],
        dry_run=False,
        yes=False,
    )

    monkeypatch.setattr(cli.ops, "eligible_candidates", lambda _settings: [AppCandidate("Google Chrome", "chrome", 1)])
    monkeypatch.setattr(cli, "resolve_db_path", lambda _path: tmp_path / "AppsIndex.db")
    monkeypatch.setattr(
        cli,
        "managed_rows",
        lambda _db_path, sources=None: [ManagedRow("Google Chrome", "old-browser", 1, SOURCE_CUSTOM)],
    )
    monkeypatch.setattr(cli, "confirm", lambda message, **_kwargs: seen.setdefault("confirm", message) or True)

    def fake_build_custom_alias_plan(settings, candidate, aliases, *, replace_existing=False):
        seen["replace_existing"] = replace_existing
        seen["aliases"] = aliases
        return cli.ops.AliasPlan(tmp_path / "AppsIndex.db", [], "add-custom", SOURCE_CUSTOM)

    monkeypatch.setattr(cli.ops, "build_custom_alias_plan", fake_build_custom_alias_plan)
    monkeypatch.setattr(cli, "_apply_plan", lambda *args, **kwargs: 0)

    assert cli.cmd_add_custom(args) == 0
    assert "Replace existing custom alias for Google Chrome" in seen["confirm"]
    assert seen["replace_existing"] is True
    assert seen["aliases"] == ["browser"]


def test_add_custom_replace_decline_skips_execution(monkeypatch, tmp_path) -> None:
    seen = {}  # type: ignore
    args = argparse.Namespace(
        db=str(tmp_path / "AppsIndex.db"),
        state_dir=str(tmp_path / "state"),
        deny_list=None,
        default_c1_exclusions=True,
        include_c1_category=[],
        app="Chrome",
        aliases=["browser"],
        dry_run=False,
        yes=False,
    )

    monkeypatch.setattr(cli.ops, "eligible_candidates", lambda _settings: [AppCandidate("Google Chrome", "chrome", 1)])
    monkeypatch.setattr(cli, "resolve_db_path", lambda _path: tmp_path / "AppsIndex.db")
    monkeypatch.setattr(
        cli,
        "managed_rows",
        lambda _db_path, sources=None: [ManagedRow("Google Chrome", "old-browser", 1, SOURCE_CUSTOM)],
    )
    monkeypatch.setattr(cli, "confirm", lambda *args, **kwargs: False)
    monkeypatch.setattr(cli.ops, "build_custom_alias_plan", lambda *args, **kwargs: seen.__setitem__("planned", True))

    assert cli.cmd_add_custom(args) == 0
    assert seen == {}


def test_candidate_label_formatter_colors_managed_sources(monkeypatch) -> None:
    monkeypatch.setattr(cli, "success", lambda text: f"<green>{text}</green>")
    monkeypatch.setattr(cli, "warning", lambda text: f"<yellow>{text}</yellow>")
    monkeypatch.setattr(cli, "color", lambda text, name: f"<{name}>{text}</{name}>")
    monkeypatch.setattr(cli, "muted", lambda text: str(text))

    label = cli._candidate_label_formatter(
        {
            "Auto App": {SOURCE_GENERATED_AUTO},
            "Manual App": {SOURCE_GENERATED_MANUAL},
            "Custom App": {SOURCE_CUSTOM},
            "Mixed App": {SOURCE_GENERATED_AUTO, SOURCE_CUSTOM},
        }
    )

    assert label(AppCandidate("Auto App", "auto")) == "<green>Auto App</green> [ <green>auto</green> ]"
    assert label(AppCandidate("Manual App", "manual")) == "<blue>Manual App</blue> [ <blue>manual</blue> ]"
    assert label(AppCandidate("Custom App", "custom")) == "<yellow>Custom App</yellow> [ <yellow>custom</yellow> ]"
    assert label(AppCandidate("Mixed App", "mixed")).startswith("<yellow>Mixed App</yellow>")


def test_apply_plan_can_skip_backup_when_confirmed(monkeypatch, tmp_path, capsys) -> None:
    seen = {}  # type: ignore
    answers = iter([False, True])
    args = argparse.Namespace(dry_run=False, yes=False, state_dir=str(tmp_path / "state"), db=None)
    group = custom_alias_group("Google Chrome", "chrome", ["browser"])
    plan = cli.ops.AliasPlan(
        db_path=tmp_path / "AppsIndex.db",
        groups=[group],
        reason="add-custom",
        source=SOURCE_CUSTOM,
    )
    settings = cli.ops.AppSettings(state_dir=str(tmp_path / "state"))

    def fake_confirm(message, *, assume_yes=False, default=False):
        seen.setdefault("messages", []).append((message, default))
        return next(answers)

    def fake_apply_alias_plan(plan, settings, *, create_backup_requested=True):
        seen["create_backup_requested"] = create_backup_requested
        return cli.ops.ApplyResult(inserted=1)

    monkeypatch.setattr(cli, "confirm", fake_confirm)
    monkeypatch.setattr(cli.ops, "apply_alias_plan", fake_apply_alias_plan)

    assert cli._apply_plan(args, settings, plan) == 0

    output = capsys.readouterr().out
    assert seen["messages"] == [
        ("Create a backup before applying these aliases?", True),
        ("Apply these aliases?", True),
    ]
    assert seen["create_backup_requested"] is False
    assert "Backup skipped." in output
    assert "Inserted new rows:" in output


def test_interactive_summary_shows_counts_without_alias_spam(monkeypatch, capsys, tmp_path) -> None:
    def fake_summary(_settings):
        scan = cli.ops.ScanResult(
            db_path=tmp_path / "AppsIndex.db",
            report=FilterReport(total=2, eligible=1, ignored=1, reason_counts={"c1:games": 1}),
            deny_list=cli.load_deny_list(None),
            enabled_content_categories={"games"},
        )
        return cli.ops.ManagedSummary(
            db_path=tmp_path / "AppsIndex.db",
            metadata_groups=[{"display_name": "Google Chrome"}, {"display_name": "MPC-HC"}],
            rows=[
                ManagedRow("Google Chrome", "сркщьу", 1, SOURCE_GENERATED_AUTO),
                ManagedRow("MPC-HC", "ьзс-рс", 1, SOURCE_CUSTOM),
            ],
            scan=scan,
        )

    monkeypatch.setattr(cli.ops, "managed_summary", fake_summary)

    args = argparse.Namespace(db=None, state_dir=None)
    cli._print_registered_summary(args)

    output = capsys.readouterr().out
    assert "Metadata groups:" in output
    assert "Database rows:" in output
    assert "Ignored by filtering rules:" in output
    assert "Google Chrome -> сркщьу" not in output


def test_interactive_summary_tolerates_temporary_sqlite_errors(monkeypatch, capsys, tmp_path) -> None:
    def fake_summary(_settings):
        return cli.ops.ManagedSummary(
            db_path=None,
            metadata_groups=[{"display_name": "Google Chrome"}],
            rows=[],
            scan=None,
        )

    monkeypatch.setattr(cli.ops, "managed_summary", fake_summary)

    args = argparse.Namespace(db=None, state_dir=None)
    cli._print_registered_summary(args)

    output = capsys.readouterr().out
    assert "Metadata groups:" in output
    assert "Database rows:" in output
    assert "Database unavailable" in output


def test_cmd_interactive_tui_runs_selected_command_and_quits(monkeypatch) -> None:
    screen = FakeScreen(["enter", "esc"])
    seen = {}  # type: ignore

    def fake_scan(_args) -> int:
        print("Scan summary")
        seen["scan"] = seen.get("scan", 0) + 1
        return 0

    def fake_view_text(text: str, *, title: str, **_kwargs) -> None:
        seen["title"] = title
        seen["text"] = text

    monkeypatch.setattr(cli.tui, "make_tui_screen", lambda: screen)
    monkeypatch.setattr(cli, "_registered_summary_lines", lambda _args: ["Summary line"])
    monkeypatch.setattr(cli, "cmd_scan", fake_scan)
    monkeypatch.setattr(cli.tui, "view_text", fake_view_text)

    args = argparse.Namespace(db=None, state_dir=None, verbose=False)

    assert cli.cmd_interactive(args) == 0
    assert seen["scan"] == 1
    assert seen["title"] == "scan database"
    assert "Scan summary" in seen["text"]
    assert any("Commands" in render for render in screen.renders)
    assert all("Scan summary" not in render for render in screen.renders)


def test_cmd_interactive_tui_routes_list_managed_aliases_through_viewer(monkeypatch) -> None:
    screen = FakeScreen(["down", "down", "down", "enter", "esc"])
    seen = {}  # type: ignore

    def fake_list_managed(_args) -> int:
        print("Managed aliases")
        print("Google Chrome: browser")
        seen["runs"] = seen.get("runs", 0) + 1
        return 0

    def fake_view_text(text: str, *, title: str, **_kwargs) -> None:
        seen["title"] = title
        seen["text"] = text

    monkeypatch.setattr(cli.tui, "make_tui_screen", lambda: screen)
    monkeypatch.setattr(cli, "_registered_summary_lines", lambda _args: ["Summary line"])
    monkeypatch.setattr(cli, "cmd_list_managed", fake_list_managed)
    monkeypatch.setattr(cli.tui, "view_text", fake_view_text)

    args = argparse.Namespace(db=None, state_dir=None, verbose=False)

    assert cli.cmd_interactive(args) == 0
    assert seen["runs"] == 1
    assert seen["title"] == "list managed aliases"
    assert "Google Chrome: browser" in seen["text"]
    assert all("Google Chrome: browser" not in render for render in screen.renders)


def test_interactive_auto_generate_confirms_then_routes_output_through_viewer(monkeypatch) -> None:
    seen = {}  # type: ignore
    confirm_answers = iter([True, True])

    def fake_confirm(message, *, assume_yes=False, default=False):
        seen.setdefault("confirms", []).append((message, default))
        return next(confirm_answers)

    def fake_cmd_auto(auto_args) -> int:
        seen["yes"] = auto_args.yes
        seen["map"] = auto_args.map
        seen["has_preview_limit"] = hasattr(auto_args, "preview_limit")
        print("Automatic generation")
        print("Inserted new rows: 3")
        return 0

    def fake_view_text(text: str, *, title: str, **_kwargs) -> None:
        seen["title"] = title
        seen["text"] = text

    monkeypatch.setattr(cli, "confirm", fake_confirm)
    monkeypatch.setattr(cli, "cmd_auto", fake_cmd_auto)
    monkeypatch.setattr(cli.tui, "view_text", fake_view_text)

    args = argparse.Namespace(db=None, state_dir=None)

    assert cli._interactive_auto_generate(args) == cli._INTERACTIVE_EXIT
    assert seen["confirms"] == [
        ("Auto-generate aliases for all eligible apps?", True),
        ("Create backup, replace previous auto aliases, and restart SearchHost?", True),
    ]
    assert seen["yes"] is True
    assert seen["map"] == "auto"
    assert seen["has_preview_limit"] is False
    assert seen["title"] == "auto-generate for all eligible apps"
    assert "Inserted new rows: 3" in seen["text"]
    assert "All done OK. Press Enter to finish." in seen["text"]


def test_interactive_auto_generate_cancel_skips_execution(monkeypatch) -> None:
    seen = {}  # type: ignore

    monkeypatch.setattr(cli, "confirm", lambda *args, **kwargs: False)
    monkeypatch.setattr(cli, "cmd_auto", lambda _args: seen.__setitem__("ran", True) or 0)  # type: ignore
    monkeypatch.setattr(cli.tui, "view_text", lambda *args, **kwargs: seen.__setitem__("viewed", True))

    assert cli._interactive_auto_generate(argparse.Namespace(db=None, state_dir=None)) == 0
    assert seen == {}


def test_run_tui_output_command_preserves_ansi_colors(monkeypatch) -> None:
    seen = {}

    def fake_view_text(text: str, *, title: str, **_kwargs) -> None:
        seen["title"] = title
        seen["text"] = text

    def handler() -> int:
        print(cli.success("Managed aliases"))
        return 0

    monkeypatch.setattr(cli.tui, "view_text", fake_view_text)
    monkeypatch.delenv("NO_COLOR", raising=False)

    assert cli._run_tui_output_command("list managed aliases", handler) == 0
    assert seen["title"] == "list managed aliases"
    assert "\033[" in seen["text"]


def test_run_tui_output_command_respects_no_color(monkeypatch) -> None:
    seen = {}

    def fake_view_text(text: str, *, title: str, **_kwargs) -> None:
        seen["title"] = title
        seen["text"] = text

    def handler() -> int:
        print(cli.success("Managed aliases"))
        return 0

    monkeypatch.setattr(cli.tui, "view_text", fake_view_text)
    monkeypatch.setenv("NO_COLOR", "1")

    assert cli._run_tui_output_command("list managed aliases", handler) == 0
    assert seen["title"] == "list managed aliases"
    assert "\033[" not in seen["text"]
    assert seen["text"] == "Managed aliases"


def test_print_preview_keeps_short_default(capsys) -> None:
    aliases = [SimpleNamespace(token=f"token-{index}", synonym=f"alias-{index}") for index in range(21)]
    groups = [
        SimpleNamespace(
            display_name="Example App",
            keyboard_map="uk-jcuken",
            aliases=aliases,
        ),
        SimpleNamespace(
            display_name="Another App",
            keyboard_map="uk-jcuken",
            aliases=[SimpleNamespace(token="third", synonym="ершкв")],
        ),
    ]

    cli._print_preview(groups)

    output = capsys.readouterr().out
    assert "Example App [uk-jcuken]" in output
    assert "token-0 -> alias-0" in output
    assert "token-19 -> alias-19" in output
    assert "token-20 -> alias-20" not in output
    assert "... 2 more aliases" in output


def test_configure_filter_categories_tui_preserves_category_configuration_when_toml_paused(monkeypatch) -> None:
    screen = FakeScreen(["space", "enter"])

    class FakeDenyList:
        default_disabled_categories = ["games", "social"]

        @staticmethod
        def content_category_names() -> list[str]:
            return ["games", "social"]

    def fake_enabled_categories(args, deny_list) -> set[str]:
        if not cli._use_toml_filters(args):
            return set()
        if not cli._use_default_c1_exclusions(args):
            return set()
        return set(deny_list.content_category_names()) - set(cli._included_c1_categories(args) or [])

    monkeypatch.setattr(cli.tui, "make_tui_screen", lambda: screen)
    monkeypatch.setattr(cli, "load_deny_list", lambda _path: FakeDenyList())
    monkeypatch.setattr(cli, "_enabled_c1_categories", fake_enabled_categories)
    monkeypatch.setattr(cli.ops, "filter_category_counts", lambda _settings: {"games": 3, "social": 1})

    args = argparse.Namespace(
        db=None,
        state_dir=None,
        deny_list=None,
        toml_filters=True,
        default_c1_exclusions=True,
        include_c1_category=["social"],
        exclude_cyrillic_only=True,
    )

    assert cli._configure_filter_categories(args) == 0
    assert args.toml_filters is False
    assert args.include_c1_category == ["games", "social"]
    assert args.default_c1_exclusions is True


def test_configure_filter_categories_tui_shows_paused_status_for_configured_categories(monkeypatch) -> None:
    screen = FakeScreen(["space", "enter"])

    class FakeDenyList:
        default_disabled_categories = ["games"]

        @staticmethod
        def content_category_names() -> list[str]:
            return ["games", "social"]

    def fake_enabled_categories(args, deny_list) -> set[str]:
        if not cli._use_toml_filters(args):
            return set()
        if not cli._use_default_c1_exclusions(args):
            return set()
        return set(deny_list.content_category_names()) - set(cli._included_c1_categories(args) or [])

    monkeypatch.setattr(cli.tui, "make_tui_screen", lambda: screen)
    monkeypatch.setattr(cli, "load_deny_list", lambda _path: FakeDenyList())
    monkeypatch.setattr(cli, "_enabled_c1_categories", fake_enabled_categories)
    monkeypatch.setattr(cli.ops, "filter_category_counts", lambda _settings: {"games": 3, "social": 1})

    args = argparse.Namespace(
        db=None,
        state_dir=None,
        deny_list=None,
        toml_filters=True,
        default_c1_exclusions=True,
        include_c1_category=None,
        exclude_cyrillic_only=True,
    )

    assert cli._configure_filter_categories(args) == 0
    assert any("[ ] games: 3 [included]" in render for render in screen.renders)
    assert any("[x] social: 1 [paused]" in render for render in screen.renders)


def test_configure_filter_categories_tui_shows_all_filtered_categories_checked_initially(monkeypatch) -> None:
    screen = FakeScreen(["enter"])

    class FakeDenyList:
        default_disabled_categories = ["microsoft"]

        @staticmethod
        def content_category_names() -> list[str]:
            return ["games", "microsoft-autogenerated", "microsoft"]

    monkeypatch.setattr(cli.tui, "make_tui_screen", lambda: screen)
    monkeypatch.setattr(cli, "load_deny_list", lambda _path: FakeDenyList())
    monkeypatch.setattr(
        cli.ops,
        "filter_category_counts",
        lambda _settings: {"games": 67, "microsoft-autogenerated": 49, "microsoft": 118},
    )

    args = argparse.Namespace(
        db=None,
        state_dir=None,
        deny_list=None,
        toml_filters=True,
        default_c1_exclusions=True,
        include_c1_category=[],
        exclude_cyrillic_only=True,
    )

    assert cli._configure_filter_categories(args) == 0
    first_render = screen.renders[0]
    assert "[x] games: 67 [filtering]" in first_render
    assert "[x] microsoft-autogenerated: 49 [filtering]" in first_render
    assert "[x] microsoft: 118 [filtering]" in first_render


def test_configure_filter_categories_tui_toggles_selected_category_not_another_one(monkeypatch) -> None:
    screen = FakeScreen(["down", "down", "down", "space", "enter"])

    class FakeDenyList:
        default_disabled_categories = ["microsoft"]

        @staticmethod
        def content_category_names() -> list[str]:
            return ["games", "microsoft-autogenerated", "microsoft"]

    monkeypatch.setattr(cli.tui, "make_tui_screen", lambda: screen)
    monkeypatch.setattr(cli, "load_deny_list", lambda _path: FakeDenyList())
    monkeypatch.setattr(
        cli.ops,
        "filter_category_counts",
        lambda _settings: {"games": 67, "microsoft-autogenerated": 49, "microsoft": 118},
    )

    args = argparse.Namespace(
        db=None,
        state_dir=None,
        deny_list=None,
        toml_filters=True,
        default_c1_exclusions=True,
        include_c1_category=[],
        exclude_cyrillic_only=True,
    )

    assert cli._configure_filter_categories(args) == 0
    assert args.include_c1_category == ["microsoft-autogenerated"]


def test_select_remove_kinds_uses_tui_selector(monkeypatch) -> None:
    monkeypatch.setattr(cli.tui, "make_tui_screen", lambda: FakeScreen([]))
    monkeypatch.setattr(cli.tui, "select_index", lambda *args, **kwargs: 2)

    assert cli._select_remove_kinds({"auto": 1, "manual": 2, "custom": 3}) == ["custom"]


def test_select_backup_uses_tui_selector(monkeypatch, tmp_path) -> None:
    backups = [
        SimpleNamespace(
            path=tmp_path / "first.db",
            created_at="2026-05-31",
            reason="first",
            status="clean: no managed rows",
            has_error=False,
        ),
        SimpleNamespace(
            path=tmp_path / "second.db",
            created_at="2026-06-01",
            reason="second",
            status="modified: managed rows: 1",
            has_error=False,
        ),
    ]
    seen = {}

    def fake_select_index(labels, **kwargs):
        seen["labels"] = labels
        seen["footer"] = kwargs["footer_lines"](1)
        return 1

    monkeypatch.setattr(cli.tui, "make_tui_screen", lambda: FakeScreen([]))
    monkeypatch.setattr(cli.ops, "backup_infos", lambda _settings: backups)
    monkeypatch.setattr(cli.tui, "select_index", fake_select_index)

    selected = cli._select_backup(cli.ops.AppSettings(state_dir=str(tmp_path / "state")))

    assert selected == backups[1].path
    assert Path("second.db").name in seen["labels"][1]
    assert str(backups[1].path) in seen["footer"][0]


def test_menu_args_preserve_interactive_c1_category_settings() -> None:
    args = argparse.Namespace(
        db=None,
        state_dir=None,
        toml_filters=False,
        default_c1_exclusions=True,
        include_c1_category=["games"],
        exclude_cyrillic_only=False,
    )

    menu_args = cli._menu_args(args, generation=True)

    assert menu_args.toml_filters is False
    assert menu_args.default_c1_exclusions is True
    assert menu_args.include_c1_category == ["games"]
    assert menu_args.exclude_cyrillic_only is False


def test_filter_cli_flags_flow_into_settings(tmp_path) -> None:
    args = argparse.Namespace(
        db=str(tmp_path / "AppsIndex.db"),
        state_dir=None,
        deny_list=None,
        toml_filters=False,
        default_c1_exclusions=True,
        include_c1_category=[],
        exclude_cyrillic_only=False,
    )

    settings = cli._settings(args)

    assert settings.use_deny_list_filters is False
    assert settings.exclude_cyrillic_only_names is False


def test_settings_merge_cli_categories_with_default_disabled_categories(monkeypatch) -> None:
    class FakeDenyList:
        default_disabled_categories = ("microsoft",)

    monkeypatch.setattr(cli, "load_deny_list", lambda _path: FakeDenyList())

    args = argparse.Namespace(
        db=None,
        state_dir=None,
        deny_list=None,
        toml_filters=True,
        default_c1_exclusions=True,
        include_c1_category=["games"],
        exclude_cyrillic_only=True,
    )

    settings = cli._settings(args)

    assert settings.included_content_categories == ["microsoft", "games"]


def test_interactive_remove_managed_prompts_for_kind(monkeypatch) -> None:
    seen = {}
    args = argparse.Namespace(
        db=None,
        state_dir=None,
        default_c1_exclusions=True,
        include_c1_category=[],
    )
    monkeypatch.setattr(cli, "resolve_db_path", lambda _path: "AppsIndex.db")
    monkeypatch.setattr(
        cli,
        "managed_rows",
        lambda _db_path: [
            ManagedRow("Google Chrome", "сркщьу", 1, SOURCE_CUSTOM),
            ManagedRow("MPC-HC", "ьзс-рс", 1, SOURCE_GENERATED_AUTO),
        ],
    )
    monkeypatch.setattr(cli.tui, "select_index", lambda *args, **kwargs: 2)

    def fake_remove(remove_args):
        seen["kind"] = remove_args.kind
        seen["preview_in_viewer"] = remove_args.preview_in_viewer
        seen["result_in_viewer"] = remove_args.result_in_viewer
        return 0

    monkeypatch.setattr(cli, "cmd_remove_managed", fake_remove)

    assert cli._interactive_remove_managed(args) == 0
    assert seen == {"kind": ["custom"], "preview_in_viewer": True, "result_in_viewer": True}


def test_select_remove_kinds_all_is_not_cancel(monkeypatch) -> None:
    monkeypatch.setattr(cli.tui, "select_index", lambda *args, **kwargs: 3)
    assert cli._select_remove_kinds({"auto": 1, "manual": 2, "custom": 3}) == []


def test_select_remove_kinds_shows_counts_without_duplicate_remove(monkeypatch) -> None:
    seen = {}

    def fake_select_index(labels, **_kwargs):
        seen["labels"] = labels
        return 0

    monkeypatch.setattr(cli, "warning", lambda text: f"<yellow>{text}</yellow>")
    monkeypatch.setattr(cli.tui, "select_index", fake_select_index)

    assert cli._select_remove_kinds({"auto": 1, "manual": 2, "custom": 3}) == ["auto"]
    assert seen["labels"] == [
        "auto-generated aliases <yellow>(1 row)</yellow>",
        "manually added aliases <yellow>(2 rows)</yellow>",
        "custom synonyms <yellow>(3 rows)</yellow>",
        "all managed aliases <yellow>(6 rows)</yellow>",
    ]


def test_remove_managed_previews_selected_rows(monkeypatch, capsys, tmp_path) -> None:
    seen = {}
    args = argparse.Namespace(
        db=str(tmp_path / "AppsIndex.db"),
        state_dir=None,
        app=None,
        kind=["manual"],
        dry_run=True,
        yes=False,
    )

    def fake_managed_rows(_db_path, sources=None):
        seen["sources"] = sources
        return [
            ManagedRow("Google Chrome", "сркщьу", 1, SOURCE_GENERATED_MANUAL),
            ManagedRow("MPC-HC", "ьзс-рс", 1, SOURCE_GENERATED_MANUAL),
        ]

    monkeypatch.setattr(cli, "resolve_db_path", lambda _path: tmp_path / "AppsIndex.db")
    monkeypatch.setattr(cli, "managed_rows", fake_managed_rows)

    assert cli.cmd_remove_managed(args) == 0

    output = capsys.readouterr().out
    assert seen == {"sources": {SOURCE_GENERATED_MANUAL}}
    assert "Managed rows to remove:" in output
    assert "Google Chrome" in output
    assert "MPC-HC" in output
    assert "manual" in output


def test_remove_managed_routes_tui_preview_through_viewer(monkeypatch, capsys, tmp_path) -> None:
    seen = {}
    args = argparse.Namespace(
        db=str(tmp_path / "AppsIndex.db"),
        state_dir=None,
        app=None,
        kind=["manual"],
        dry_run=True,
        yes=False,
        preview_in_viewer=True,
    )

    monkeypatch.setattr(cli, "resolve_db_path", lambda _path: tmp_path / "AppsIndex.db")
    monkeypatch.setattr(
        cli,
        "managed_rows",
        lambda _db_path, sources=None: [
            ManagedRow("Google Chrome", "сркщьу", 1, SOURCE_GENERATED_MANUAL),
            ManagedRow("MPC-HC", "ьзс-рс", 1, SOURCE_GENERATED_MANUAL),
        ],
    )

    def fake_view_text(text: str, *, title: str, **_kwargs) -> None:
        seen["title"] = title
        seen["text"] = text

    monkeypatch.setattr(cli.tui, "view_text", fake_view_text)

    assert cli.cmd_remove_managed(args) == 0

    output = capsys.readouterr().out
    assert output == ""
    assert seen["title"] == "managed aliases to remove"
    assert "Managed rows to remove:" in seen["text"]
    assert "Google Chrome" in seen["text"]
    assert "MPC-HC" in seen["text"]


def test_managed_rows_preview_colors_sources(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli, "success", lambda text: f"<green>{text}</green>")
    monkeypatch.setattr(cli, "warning", lambda text: f"<yellow>{text}</yellow>")
    monkeypatch.setattr(cli, "color", lambda text, name: f"<{name}>{text}</{name}>")
    monkeypatch.setattr(cli, "muted", lambda text: str(text))

    cli._print_managed_rows_preview(
        [
            ManagedRow("Auto App", "auto", 1, SOURCE_GENERATED_AUTO),
            ManagedRow("Manual App", "manual", 1, SOURCE_GENERATED_MANUAL),
            ManagedRow("Custom App", "custom", 1, SOURCE_CUSTOM),
        ]
    )

    output = capsys.readouterr().out
    assert "<green>Auto App</green>" in output
    assert "<blue>Manual App</blue>" in output
    assert "<yellow>Custom App</yellow>" in output
    assert "[<blue>manual</blue>]" in output
    assert "[<yellow>custom</yellow>]" in output


def test_select_backup_lists_managed_row_status(monkeypatch, tmp_path) -> None:
    store = MetadataStore(tmp_path / "state")
    empty_backup = tmp_path / "empty.db"
    managed_backup = tmp_path / "managed.db"
    _create_synonyms_db(empty_backup)
    _create_synonyms_db(managed_backup)
    _insert_synonym(managed_backup, "Google Chrome", "сркщьу", SOURCE_GENERATED_AUTO)
    store.add_backup(empty_backup, tmp_path / "AppsIndex.db", "first")
    store.add_backup(managed_backup, tmp_path / "AppsIndex.db", "second")
    monkeypatch.setattr(cli, "success", lambda text: f"<green>{text}</green>")
    monkeypatch.setattr(cli, "warning", lambda text: f"<yellow>{text}</yellow>")
    seen = {}

    def fake_select_index(labels, **kwargs):
        seen["labels"] = labels
        seen["footer"] = kwargs["footer_lines"](1)
        return 1

    monkeypatch.setattr(cli.tui, "select_index", fake_select_index)

    selected = cli._select_backup(store)  # type: ignore

    assert selected == managed_backup
    assert "<green>empty.db</green>" in seen["labels"][0]
    assert "<green>clean: no managed rows</green>" in seen["labels"][0]
    assert "<yellow>managed.db</yellow>" in seen["labels"][1]
    assert "<yellow>modified: managed rows: 1</yellow>" in seen["labels"][1]
    assert str(managed_backup) in seen["footer"][0]


def test_restore_backup_without_flags_prompts_for_backup(monkeypatch, tmp_path) -> None:
    db_path = tmp_path / "AppsIndex.db"
    db_path.write_text("current", encoding="utf-8")
    backup_path = tmp_path / "backup.db"
    backup_path.write_text("backup", encoding="utf-8")
    store = MetadataStore(tmp_path / "state")
    store.add_backup(backup_path, db_path, "unit-test")
    args = argparse.Namespace(db=str(db_path), state_dir=str(store.state_dir), latest=False, backup=None, dry_run=True)
    monkeypatch.setattr(cli.tui, "select_index", lambda *args, **kwargs: 0)

    assert cli.cmd_restore_backup(args) == 0


def test_restore_backup_cancel_is_not_an_error(monkeypatch, capsys, tmp_path) -> None:
    db_path = tmp_path / "AppsIndex.db"
    db_path.write_text("current", encoding="utf-8")
    backup_path = tmp_path / "backup.db"
    backup_path.write_text("backup", encoding="utf-8")
    store = MetadataStore(tmp_path / "state")
    store.add_backup(backup_path, db_path, "unit-test")
    args = argparse.Namespace(db=str(db_path), state_dir=str(store.state_dir), latest=False, backup=None, dry_run=False)
    monkeypatch.setattr(cli.tui, "select_index", lambda *args, **kwargs: None)

    assert cli.cmd_restore_backup(args) == 0

    output = capsys.readouterr()
    assert "Backup restore cancelled" not in output.out
    assert "error:" not in output.err


def _create_synonyms_db(path) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        "create virtual table synonyms using fts5(displayName UNINDEXED, rankPenalty UNINDEXED, synonym, source UNINDEXED)"
    )
    conn.commit()
    conn.close()


def _insert_synonym(path, display_name: str, synonym: str, source: str) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        "insert into synonyms(displayName, rankPenalty, synonym, source) values (?, ?, ?, ?)",
        (display_name, 1, synonym, source),
    )
    conn.commit()
    conn.close()
