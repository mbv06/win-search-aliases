from __future__ import annotations

import ctypes
import shutil
import sys
from collections.abc import Callable, Iterator, Sequence
from typing import TextIO

from ..filters import AppCandidate, search_candidates
from .console import heading, info, muted, success, warning

STD_OUTPUT_HANDLE = -11
ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
INTERACTIVE_TUI_ERROR = "Interactive TUI requires a Windows terminal (TTY)."

KeyReader = Callable[[], str | None]
FooterLines = Sequence[str] | Callable[[int], Sequence[str]]

SOURCE_STYLES = {
    "WinSearchAliasesAuto": ("green", "auto"),
    "WinSearchAliasesManual": ("blue", "manual"),
    "WinSearchAliasesCustom": ("yellow", "custom"),
}


def menu_loop(screen: LiveScreen, render_fn: Callable[[], str]) -> Iterator[str]:
    """
    Run a continuous render-and-input loop for TUI menus.
    Yields keys pressed by the user.
    """
    while True:
        screen.render(render_fn())
        key = screen.read_key()
        if key is not None:
            yield key


def is_supported(stdin: TextIO | None = None, stdout: TextIO | None = None) -> bool:
    stdin = sys.stdin if stdin is None else stdin
    stdout = sys.stdout if stdout is None else stdout
    return stdin.isatty() and stdout.isatty()


def enable_virtual_terminal() -> bool:
    try:
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(STD_OUTPUT_HANDLE)
        if handle in (0, -1):
            return False

        mode = ctypes.c_uint()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)) == 0:
            return False

        new_mode = mode.value | ENABLE_VIRTUAL_TERMINAL_PROCESSING
        if new_mode != mode.value and kernel32.SetConsoleMode(handle, new_mode) == 0:
            return False
        return True
    except Exception:
        return False


def make_key_reader(stdin: TextIO | None = None) -> KeyReader | None:
    stdin = sys.stdin if stdin is None else stdin
    if not stdin.isatty():
        return None

    try:
        import msvcrt
    except ImportError:
        return None

    extended = {
        "G": "home",
        "H": "up",
        "I": "pageup",
        "K": "left",
        "M": "right",
        "O": "end",
        "P": "down",
        "Q": "pagedown",
    }

    def read_key() -> str | None:
        ch = msvcrt.getwch()
        if ch in ("\x00", "\xe0"):
            return extended.get(msvcrt.getwch())
        if ch in ("\r", "\n"):
            return "enter"
        if ch == "\x1b":
            return "esc"
        if ch == " ":
            return "space"
        if ch == "\t":
            return "tab"
        if ch == "\b":
            return "backspace"
        if ch == "\x03":
            raise KeyboardInterrupt
        if ch.isprintable():
            return ch.casefold()
        return None

    return read_key


class LiveScreen:
    def __init__(
        self,
        *,
        stdin: TextIO | None = None,
        stdout: TextIO | None = None,
        read_key: KeyReader | None = None,
        input_func: Callable[[str], str] = input,
        vt_enabled: bool | None = None,
    ) -> None:
        self.stdin = sys.stdin if stdin is None else stdin
        self.stdout = sys.stdout if stdout is None else stdout
        self._input_func = input_func
        self._read_key = read_key
        self._available = False
        self._active = False
        self._line_count = 0

        if not is_supported(self.stdin, self.stdout):
            return

        self._read_key = read_key or make_key_reader(self.stdin)
        if self._read_key is None:
            return

        self._available = enable_virtual_terminal() if vt_enabled is None else vt_enabled

    @property
    def available(self) -> bool:
        return self._available

    def require(self) -> LiveScreen:
        if not self.available:
            raise RuntimeError(INTERACTIVE_TUI_ERROR)
        return self

    def read_key(self) -> str | None:
        if self._read_key is None:
            return None
        return self._read_key()

    def render(self, text: str) -> None:
        if not self._available:
            return
        if self._active:
            self._clear_region()
        else:
            self.stdout.write("\033[?25l")
            self._active = True
        self.stdout.write(text + "\n")
        self.stdout.flush()
        self._line_count = text.count("\n") + 1

    def prompt(self, prompt: str) -> str:
        if not self._available:
            return self._input_func(prompt)

        if self._active:
            self._clear_region()
        self.stdout.write("\033[?25h")
        self.stdout.flush()
        try:
            return self._input_func(prompt)
        finally:
            self.stdout.write("\033[?25l")
            self.stdout.flush()
            self._active = True
            self._line_count = 0

    def prompt_with_context(self, text: str, prompt: str) -> str:
        if not self._available:
            print(text)
            return self._input_func(prompt)

        if self._active:
            self._clear_region()
        self.stdout.write("\033[?25h")
        self.stdout.write(text + "\n")
        self.stdout.flush()
        try:
            return self._input_func(prompt)
        finally:
            self.stdout.write("\033[?25l")
            self.stdout.flush()
            self._active = True
            self._line_count = text.count("\n") + 2

    def close(self, *, clear: bool = False) -> None:
        if not self._available:
            return
        if self._active and clear:
            self._clear_region()
        if self._active:
            self.stdout.write("\033[?25h")
            self.stdout.flush()
        self._active = False
        self._line_count = 0

    def _clear_region(self) -> None:
        if self._line_count <= 0:
            return
        self.stdout.write(f"\033[{self._line_count}A\033[J")
        self.stdout.flush()


def make_tui_screen() -> LiveScreen:
    return LiveScreen().require()


def select_index(
    items: Sequence[str],
    *,
    title: str,
    instructions: str,
    default_index: int = 0,
    page_size: int = 10,
    header_lines: Sequence[str] | None = None,
    footer_lines: FooterLines | None = None,
    screen: LiveScreen | None = None,
) -> int | None:
    owned_screen = screen is None
    screen = LiveScreen() if screen is None else screen
    if not screen.available:
        return None

    cursor = max(0, min(default_index, len(items) - 1)) if items else 0
    page = cursor // page_size if page_size > 0 else 0
    page_size = max(1, page_size)

    def render() -> str:
        lines: list[str] = []
        if header_lines:
            lines.extend(header_lines)
            lines.append("")
        lines.extend([heading(title), muted(instructions), ""])
        if not items:
            lines.append(warning("No items available."))
        else:
            page_count = max(1, (len(items) + page_size - 1) // page_size)
            current_page = max(0, min(page, page_count - 1))
            start = current_page * page_size
            end = min(start + page_size, len(items))
            for index in range(start, end):
                pointer = info(">") if index == cursor else " "
                lines.append(f"  {pointer} {items[index]}")
            if page_count > 1:
                lines.extend(["", muted(f"Page {current_page + 1}/{page_count}")])

        if callable(footer_lines):
            footer = footer_lines(cursor) if items else None
        else:
            footer = footer_lines
        if footer:
            lines.extend(["", *footer])
        return "\n".join(lines)

    try:
        for key in menu_loop(screen, render):
            if key in {"up", "k"} and items:
                cursor = (cursor - 1) % len(items)
                page = cursor // page_size
                continue
            if key in {"down", "j", "tab"} and items:
                cursor = (cursor + 1) % len(items)
                page = cursor // page_size
                continue
            if key in {"left", "pageup"} and items:
                page = max(0, page - 1)
                cursor = page * page_size
                continue
            if key in {"right", "pagedown"} and items:
                page_count = max(1, (len(items) + page_size - 1) // page_size)
                page = min(page_count - 1, page + 1)
                cursor = page * page_size
                continue
            if key == "home" and items:
                cursor = 0
                page = 0
                continue
            if key == "end" and items:
                cursor = len(items) - 1
                page = cursor // page_size
                continue
            if key == "enter":
                return cursor if items else None
            if key == "esc":
                return None
        return None
    finally:
        if owned_screen:
            screen.close(clear=True)


def view_text(
    text: str,
    *,
    title: str,
    instructions: str = "Up/down: scroll, PgUp/PgDn: page, Home/End: jump, Enter/Esc: close",
    page_size: int | None = None,
    screen: LiveScreen | None = None,
) -> None:
    owned_screen = screen is None
    screen = LiveScreen() if screen is None else screen
    if not screen.available:
        print(text)
        return

    lines = text.splitlines() or [""]
    height = page_size
    if height is None:
        height = max(8, shutil.get_terminal_size((120, 30)).lines - 6)
    height = max(1, height)
    offset = 0

    def render() -> str:
        end = min(offset + height, len(lines))
        page_lines = lines[offset:end]
        view = [heading(title), muted(instructions), ""]
        view.extend(page_lines)
        view.extend(["", muted(f"Lines {offset + 1}-{end} of {len(lines)}")])
        return "\n".join(view)

    try:
        for key in menu_loop(screen, render):
            if key in {"up", "k"}:
                offset = max(0, offset - 1)
                continue
            if key in {"down", "j"}:
                offset = min(max(0, len(lines) - height), offset + 1)
                continue
            if key in {"pageup", "left"}:
                offset = max(0, offset - height)
                continue
            if key in {"pagedown", "right", "tab"}:
                offset = min(max(0, len(lines) - height), offset + height)
                continue
            if key == "home":
                offset = 0
                continue
            if key == "end":
                offset = max(0, len(lines) - height)
                continue
            if key in {"enter", "esc", "q"}:
                return
    finally:
        if owned_screen:
            screen.close(clear=True)


CandidateLabelFormatter = Callable[[AppCandidate], str]


def select_apps(
    candidates: list[AppCandidate],
    *,
    page_size: int = 15,
    aliased_names: set[str] | None = None,
    label_for_candidate: CandidateLabelFormatter | None = None,
) -> list[AppCandidate]:
    return _select_candidates_tui(
        make_tui_screen(),
        candidates,
        page_size=page_size,
        aliased_names=aliased_names,
        label_for_candidate=label_for_candidate,
        multiple=True,
    )


def select_one_app(
    candidates: list[AppCandidate],
    *,
    page_size: int = 15,
    aliased_names: set[str] | None = None,
    label_for_candidate: CandidateLabelFormatter | None = None,
) -> AppCandidate | None:
    selected = _select_candidates_tui(
        make_tui_screen(),
        candidates,
        page_size=page_size,
        aliased_names=aliased_names,
        label_for_candidate=label_for_candidate,
        multiple=False,
    )
    return selected[0] if selected else None


def prompt_custom_alias(app_name: str) -> str:
    return _prompt_custom_alias_tui(make_tui_screen(), app_name)


def _select_candidates_tui(
    screen: LiveScreen,
    candidates: list[AppCandidate],
    *,
    page_size: int,
    aliased_names: set[str] | None,
    label_for_candidate: CandidateLabelFormatter | None,
    multiple: bool,
) -> list[AppCandidate]:
    selected: dict[int, AppCandidate] = {}
    visible = list(candidates)
    current_query = ""
    show_unaliased_only = False
    cursor = 0
    page_size = max(1, page_size)
    label_for_candidate = label_for_candidate or _default_candidate_label
    title = "Select apps" if multiple else "Select one app"
    instructions = (
        "Up/down: move, Space: toggle, Enter: confirm, Esc: cancel, PgUp/PgDn: page, s: search"
        if multiple
        else "Up/down: move, Enter: choose, Esc: cancel, PgUp/PgDn: page, s: search"
    )

    def apply_filters(query: str) -> None:
        nonlocal visible, cursor
        result = search_candidates(candidates, query)
        if show_unaliased_only and aliased_names is not None:
            result = [candidate for candidate in result if candidate.display_name not in aliased_names]
        visible = result
        cursor = 0

    def render() -> str:
        lines = [
            heading(title),
            muted(instructions),
        ]
        status_parts = []
        if current_query:
            status_parts.append(f"query: {current_query}")
        if show_unaliased_only:
            status_parts.append("unaliased only")
        if status_parts:
            lines.append(muted(" | ".join(status_parts)))
        lines.append("")

        if not visible:
            lines.append(warning("No matches. Press s to search again or Esc to cancel."))
        else:
            page_count = max(1, (len(visible) + page_size - 1) // page_size)
            current_page = min(cursor // page_size, page_count - 1)
            start = current_page * page_size
            end = min(start + page_size, len(visible))
            count_text = f"; selected {len(selected)}" if multiple else ""
            lines.append(heading(f"Apps {start + 1}-{end} of {len(visible)}{count_text}"))
            for index in range(start, end):
                candidate = visible[index]
                pointer = info(">") if index == cursor else " "
                check = f"{success('[x]') if id(candidate) in selected else '[ ]'} " if multiple else ""
                app_id = f" [{candidate.app_id}]" if candidate.app_id else ""
                lines.append(f"  {pointer} {check}{label_for_candidate(candidate)}{muted(app_id)}")
            if page_count > 1:
                lines.extend(["", muted(f"Page {current_page + 1}/{page_count}")])

        if aliased_names is not None:
            toggle_label = warning("ON") if show_unaliased_only else muted("OFF")
            lines.extend(["", f"{info('u')} Unaliased filter: {toggle_label}"])

        return "\n".join(lines)

    try:
        for key in menu_loop(screen, render):
            cursor = max(0, min(cursor, len(visible) - 1)) if visible else 0

            if key in {"up", "k"} and visible:
                cursor = (cursor - 1) % len(visible)
                continue
            if key in {"down", "j", "tab"} and visible:
                cursor = (cursor + 1) % len(visible)
                continue
            if key in {"pageup", "left"} and visible:
                cursor = max(0, cursor - page_size)
                continue
            if key in {"pagedown", "right"} and visible:
                cursor = min(len(visible) - 1, cursor + page_size)
                continue
            if key == "home" and visible:
                cursor = 0
                continue
            if key == "end" and visible:
                cursor = len(visible) - 1
                continue
            if key == "space" and visible and multiple:
                candidate = visible[cursor]
                selected_key = id(candidate)
                if selected_key in selected:
                    del selected[selected_key]
                else:
                    selected[selected_key] = candidate
                continue
            if key == "s":
                current_query = screen.prompt(info("Search query> ")).strip()
                apply_filters(current_query)
                continue
            if key == "u" and aliased_names is not None:
                show_unaliased_only = not show_unaliased_only
                apply_filters(current_query)
                continue
            if key == "enter" and multiple:
                return list(selected.values())
            if key == "enter" and visible:
                return [visible[cursor]]
            if key == "enter":
                return []
            if key in {"esc", "q"}:
                return []
    finally:
        screen.close(clear=True)
    return []


def _prompt_custom_alias_tui(screen: LiveScreen, app_name: str) -> str:
    message = ""

    def render() -> str:
        lines = [
            heading("Custom alias"),
            muted("Type one alias and press Enter."),
            "",
            f"{info('App:')} {success(app_name)}",
            "",
            heading("Alias"),
        ]
        lines.append(f"  {muted('not entered yet')}")
        if message:
            lines.extend(["", warning(message)])
        return "\n".join(lines)

    try:
        while True:
            value = screen.prompt_with_context(render(), info("Custom alias> ")).strip()
            if value:
                return value
            message = "Enter one alias, or close the terminal with Ctrl+C to cancel."
    finally:
        screen.close(clear=True)


def _default_candidate_label(candidate: AppCandidate) -> str:
    return success(candidate.display_name)


def confirm(message: str, *, assume_yes: bool = False, default: bool | None = False) -> bool:
    if assume_yes:
        return True
    return _confirm_tui(make_tui_screen(), message, default=default)


def _confirm_tui(screen: LiveScreen, message: str, *, default: bool | None) -> bool:
    options = [("Yes", True), ("No", False)]
    cursor = 0 if default is not False else 1

    def render() -> str:
        lines = [
            warning(message),
            muted("Up/down or Tab: switch, Enter: confirm, Esc: cancel, y/n: quick choice"),
            "",
        ]
        for index, (label, value) in enumerate(options):
            pointer = info(">") if index == cursor else " "
            default_suffix = muted(" (default)") if default is value else ""
            lines.append(f"  {pointer} {label}{default_suffix}")
        return "\n".join(lines)

    try:
        for key in menu_loop(screen, render):
            if key in {"up", "down", "left", "right", "tab"}:
                cursor = 1 - cursor
                continue
            if key == "y":
                return True
            if key == "n":
                return False
            if key == "enter":
                return options[cursor][1]
            if key == "esc":
                return False
    finally:
        screen.close(clear=True)
    return False
