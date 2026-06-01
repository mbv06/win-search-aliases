import logging
import os

from win_search_aliases import cli, log_config
from win_search_aliases.ui import gui as ui  # type: ignore


def _managed_roles() -> list[str]:
    logger = logging.getLogger(log_config.LOGGER_NAME)
    return [role for handler in logger.handlers if (role := getattr(handler, "_win_search_aliases_role", None))]


def test_configure_logging_writes_file_without_console_by_default(tmp_path) -> None:
    log_path = log_config.configure_logging(tmp_path / "state")
    logger = logging.getLogger("win_search_aliases.test")

    logger.info("hello from test")
    for handler in logging.getLogger(log_config.LOGGER_NAME).handlers:
        handler.flush()

    assert log_path.parent == tmp_path / "state" / "logs"
    assert log_path.name.startswith(log_config.LOG_PREFIX)
    assert log_path.name.endswith(log_config.LOG_SUFFIX)
    assert str(os.getpid()) in log_path.name
    assert log_path.exists()
    assert "hello from test" in log_path.read_text(encoding="utf-8")
    assert _managed_roles() == ["file"]


def test_configure_logging_adds_verbose_console_without_duplicate_handlers(tmp_path) -> None:
    log_config.configure_logging(tmp_path / "state", verbose=True)
    log_config.configure_logging(tmp_path / "state", verbose=True)

    assert _managed_roles() == ["file", "console"]


def test_log_file_path_returns_none_before_configure(monkeypatch) -> None:
    monkeypatch.setattr(log_config, "_current_log_path", None)
    assert log_config.log_file_path() is None


def test_log_file_path_returns_session_file_after_configure(tmp_path) -> None:
    log_path = log_config.configure_logging(tmp_path / "state")
    assert log_config.log_file_path() == log_path
    assert log_path.is_file()


def test_log_dir_path_returns_logs_directory(tmp_path) -> None:
    assert log_config.log_dir_path(tmp_path / "state") == tmp_path / "state" / "logs"


def test_cli_verbose_is_accepted_for_subcommands(monkeypatch, tmp_path) -> None:
    seen = {}
    monkeypatch.setattr(cli, "_ensure_windows", lambda: None)

    def fake_scan(args):
        seen["verbose"] = args.verbose
        seen["state_dir"] = args.state_dir
        return 0

    monkeypatch.setattr(cli, "cmd_scan", fake_scan)

    assert cli.main(["scan", "--state-dir", str(tmp_path / "state"), "--verbose"]) == 0
    assert seen == {"verbose": True, "state_dir": str(tmp_path / "state")}


def test_cli_logs_handled_errors_to_state_dir(monkeypatch, tmp_path, capsys) -> None:
    state_dir = tmp_path / "state"
    monkeypatch.setattr(cli, "_ensure_windows", lambda: None)

    def fail(_args):
        raise ValueError("unit failure")

    monkeypatch.setattr(cli, "cmd_scan", fail)

    assert cli.main(["scan", "--state-dir", str(state_dir)]) == 1

    captured = capsys.readouterr()
    log_files = list((state_dir / "logs").glob(f"{log_config.LOG_PREFIX}*{log_config.LOG_SUFFIX}"))
    assert len(log_files) >= 1
    log_text = log_files[-1].read_text(encoding="utf-8")
    assert "error: unit failure" in captured.err
    assert "Command failed" in log_text
    assert "ValueError: unit failure" in log_text


def test_cleanup_removes_old_session_logs(tmp_path) -> None:
    log_dir = tmp_path / "state" / "logs"
    log_dir.mkdir(parents=True)
    for i in range(40):
        (log_dir / f"{log_config.LOG_PREFIX}2026-01-{i + 1:02d}_00-00-00-99999{log_config.LOG_SUFFIX}").touch()

    log_config.configure_logging(tmp_path / "state")
    remaining = list(log_dir.glob(f"{log_config.LOG_PREFIX}*{log_config.LOG_SUFFIX}"))
    assert len(remaining) <= log_config.MAX_SESSION_LOGS


def test_ui_open_log_uses_logging_config(monkeypatch) -> None:
    seen = {}  # type: ignore

    class FakeApp:
        def settings(self):
            return ui.ops.AppSettings(state_dir="state-dir")

    monkeypatch.setattr(ui.log_config, "open_current_log", lambda state_dir: seen.setdefault("state_dir", state_dir))

    ui.AliasApp.open_log(FakeApp())  # type: ignore

    assert seen == {"state_dir": "state-dir"}
