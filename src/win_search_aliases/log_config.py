from __future__ import annotations

import contextlib
import logging
import os
import platform
from collections.abc import Callable
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as package_version
from pathlib import Path

from .metadata import default_state_dir

_current_log_path: Path | None = None

LOGGER_NAME = "win_search_aliases"
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
LOG_PREFIX = "app-"
LOG_SUFFIX = ".log"
MAX_SESSION_LOGS = 30


def log_dir_path(state_dir: str | Path | None = None) -> Path:
    root = Path(state_dir) if state_dir else default_state_dir()
    return root / "logs"


def log_file_path() -> Path | None:
    """Return the current session log file, or ``None`` if not yet configured."""
    return _current_log_path


def _session_filename() -> str:
    stamp = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d_%H-%M-%S")
    return f"{LOG_PREFIX}{stamp}-{os.getpid()}{LOG_SUFFIX}"


def _cleanup_old_logs(directory: Path) -> None:
    with contextlib.suppress(OSError):
        logs = sorted(directory.glob(f"{LOG_PREFIX}*{LOG_SUFFIX}"), key=lambda p: p.name, reverse=True)
        for old in logs[MAX_SESSION_LOGS:]:
            old.unlink(missing_ok=True)


def configure_logging(
    state_dir: str | Path | None = None,
    *,
    verbose: bool = False,
    app_mode: str = "unknown",
) -> Path:
    global _current_log_path  # noqa: PLW0603
    directory = log_dir_path(state_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / _session_filename()
    _current_log_path = path

    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    _remove_managed_handlers(logger)

    file_handler = logging.FileHandler(path, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG if verbose else logging.INFO)
    file_handler.setFormatter(logging.Formatter(LOG_FORMAT))
    _mark_handler(file_handler, "file")
    logger.addHandler(file_handler)

    if verbose:
        console_handler = logging.StreamHandler()
        console_handler.setLevel(logging.DEBUG)
        console_handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
        _mark_handler(console_handler, "console")
        logger.addHandler(console_handler)

    _emit_session_header(logger, app_mode)

    logger.debug("Logging configured: path=%s verbose=%s mode=%s", path, verbose, app_mode)
    _cleanup_old_logs(directory)
    return path


def open_current_log(state_dir: str | Path | None = None) -> Path:
    path = log_file_path()
    if path and path.exists():
        os.startfile(str(path))  # type: ignore[attr-defined]
        return path

    directory = log_dir_path(state_dir)
    directory.mkdir(parents=True, exist_ok=True)
    os.startfile(str(directory))  # type: ignore[attr-defined]
    return directory


def _safe_get(func: Callable[[], str | int], default: str = "unknown") -> str:
    try:
        return str(func())
    except Exception:
        return default


def _emit_session_header(logger: logging.Logger, app_mode: str) -> None:
    def _version() -> str:
        try:
            return package_version("win-search-aliases")
        except PackageNotFoundError:
            return "unknown"

    logger.info(
        "Session started: version=%s mode=%s pid=%s python=%s os=%s arch=%s",
        _safe_get(_version),
        app_mode,
        _safe_get(os.getpid),
        _safe_get(platform.python_version),
        _safe_get(lambda: f'"{platform.system()} {platform.release()}"'),
        _safe_get(platform.machine),
    )


def _remove_managed_handlers(logger: logging.Logger) -> None:
    for handler in list(logger.handlers):
        if getattr(handler, "_win_search_aliases_role", None):
            logger.removeHandler(handler)
            handler.close()


def _mark_handler(handler: logging.Handler, role: str) -> None:
    handler._win_search_aliases_role = role  # type: ignore[attr-defined]
