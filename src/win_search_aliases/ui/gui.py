from __future__ import annotations

import contextlib
import logging
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
import typing
from dataclasses import dataclass
from datetime import datetime
from importlib import resources
from pathlib import Path
from tkinter import filedialog, ttk

from .. import config, log_config
from .. import operations as ops
from ..db import SOURCE_CUSTOM, SOURCE_GENERATED_AUTO, SOURCE_GENERATED_MANUAL, ManagedRow
from ..filters import AppCandidate, search_candidates
from ..managed_sources import alias_sources_by_display_name, row_counts_by_kind
from ..metadata import default_state_dir
from ..utils import WINDOWS_ONLY_ERROR

SMOKE_TEST_ARG = "--smoke-test"
CHECKED_MARK = "[x]"
UNCHECKED_MARK = "[ ]"
CANDIDATE_ALIAS_AUTO_TAG = "alias-auto"
CANDIDATE_ALIAS_MANUAL_TAG = "alias-manual"
CANDIDATE_ALIAS_CUSTOM_TAG = "alias-custom"
APP_ICON_ICO_RESOURCE = "assets/app.ico"
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TaskError:
    label: str
    error: Exception


@dataclass
class CheckableTreeState:
    anchor_index: int | None = None
    sort_column: str | None = None
    sort_descending: bool = False


def _find_ctrl_backspace_start(text: str) -> int:
    end = len(text)
    while end > 0 and text[end - 1].isspace():
        end -= 1
    if end == 0:
        return 0
    is_word_char = text[end - 1].isalnum() or text[end - 1] == "_"
    start = end
    while start > 0:
        c = text[start - 1]
        if c.isspace() or (c.isalnum() or c == "_") != is_word_char:
            break
        start -= 1
    return start


def bind_entry_shortcuts(widget: ttk.Entry) -> None:
    def on_control(event: tk.Event) -> str | None:
        if getattr(event, "keysym", "") == "BackSpace" or getattr(event, "keycode", 0) == 8:
            if widget.select_present():
                widget.delete(tk.SEL_FIRST, tk.SEL_LAST)
            else:
                pos = widget.index(tk.INSERT)
                if pos > 0:
                    text = widget.get()[:pos]
                    start = _find_ctrl_backspace_start(text)
                    widget.delete(start, pos)
            return "break"
        if (
            getattr(event, "keysym", "") in ("a", "A", "Cyrillic_ef", "Cyrillic_EF", "ф", "Ф")
            or getattr(event, "keycode", 0) == 65
        ):
            widget.select_range(0, tk.END)
            widget.icursor(tk.END)
            return "break"
        if (
            getattr(event, "keysym", "") in ("x", "X", "Cyrillic_ch", "Cyrillic_CH", "ч", "Ч")
            or getattr(event, "keycode", 0) == 88
        ):
            widget.event_generate("<<Cut>>")
            return "break"
        if (
            getattr(event, "keysym", "") in ("c", "C", "Cyrillic_es", "Cyrillic_ES", "с", "С")
            or getattr(event, "keycode", 0) == 67
        ):
            widget.event_generate("<<Copy>>")
            return "break"
        if (
            getattr(event, "keysym", "") in ("v", "V", "Cyrillic_em", "Cyrillic_EM", "м", "М")
            or getattr(event, "keycode", 0) == 86
        ):
            widget.event_generate("<<Paste>>")
            return "break"
        return None

    widget.bind("<Control-KeyPress>", on_control)
    widget.bind("<Control-BackSpace>", on_control)


def ask_yes_no(master: tk.Misc, title: str, message: str) -> bool:
    result = tk.BooleanVar(master=master, value=False)
    dialog = tk.Toplevel(master)
    dialog.title(title)
    dialog.resizable(False, False)
    dialog.transient(master.winfo_toplevel())
    dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)

    body = ttk.Frame(dialog, padding=14)
    body.grid(sticky="nsew")
    ttk.Label(body, text=message, wraplength=440, justify="left").grid(row=0, column=0, columnspan=2, sticky="w")
    buttons = ttk.Frame(body)
    buttons.grid(row=1, column=0, columnspan=2, sticky="e", pady=(14, 0))

    def choose(value: bool) -> None:
        result.set(value)
        dialog.destroy()

    ttk.Button(buttons, text="Yes", command=lambda: choose(True)).grid(row=0, column=0, padx=(0, 8))
    ttk.Button(buttons, text="No", command=lambda: choose(False)).grid(row=0, column=1)
    center_dialog(dialog, master)
    dialog.grab_set()
    dialog.wait_window()
    return result.get()


def ask_synonym(master: tk.Misc) -> str | None:
    result = tk.StringVar(master=master, value="")
    dialog = tk.Toplevel(master)
    dialog.title("Add synonym")
    dialog.resizable(False, False)
    dialog.transient(master.winfo_toplevel())
    dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)

    body = ttk.Frame(dialog, padding=14)
    body.grid(sticky="nsew")
    ttk.Label(body, text="Synonym").grid(row=0, column=0, sticky="w")
    entry = ttk.Entry(body, textvariable=result, width=44)
    bind_entry_shortcuts(entry)
    entry.grid(row=1, column=0, sticky="ew", pady=(4, 0))
    buttons = ttk.Frame(body)
    buttons.grid(row=2, column=0, sticky="e", pady=(14, 0))

    accepted = tk.BooleanVar(master=master, value=False)

    def submit() -> None:
        accepted.set(True)
        dialog.destroy()

    ttk.Button(buttons, text="Add", command=submit).grid(row=0, column=0, padx=(0, 8))
    ttk.Button(buttons, text="Cancel", command=dialog.destroy).grid(row=0, column=1)
    entry.bind("<Return>", lambda _event: submit())
    entry.focus_set()
    center_dialog(dialog, master)
    dialog.grab_set()
    dialog.wait_window()
    value = result.get().strip()
    if not accepted.get() or not value:
        return None
    return value


def show_message(master: tk.Misc, title: str, message: str, kind: str = "info") -> None:
    dialog = tk.Toplevel(master)
    dialog.title(title)
    dialog.resizable(False, False)
    if master.winfo_viewable():
        dialog.transient(master.winfo_toplevel())
    dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)

    if kind == "error":
        dialog.bell()

    body = ttk.Frame(dialog, padding=14)
    body.grid(sticky="nsew")

    icon_name = "::tk::icons::information"
    if kind == "error":
        icon_name = "::tk::icons::error"
    elif kind == "warning":
        icon_name = "::tk::icons::warning"

    ttk.Label(body, image=icon_name).grid(row=0, column=0, padx=(0, 14), sticky="n")
    ttk.Label(body, text=message, wraplength=440, justify="left").grid(row=0, column=1, sticky="w")

    buttons = ttk.Frame(body)
    buttons.grid(row=1, column=0, columnspan=2, sticky="e", pady=(14, 0))

    btn = ttk.Button(buttons, text="OK", command=dialog.destroy)
    btn.grid(row=0, column=0)

    dialog.bind("<Return>", lambda _e: dialog.destroy())
    dialog.bind("<Escape>", lambda _e: dialog.destroy())
    btn.focus_set()

    center_dialog(dialog, master)
    if not master.winfo_viewable():
        dialog.lift()
        dialog.focus_force()
        dialog.attributes("-topmost", True)
        dialog.after_idle(dialog.attributes, "-topmost", False)
    dialog.grab_set()
    dialog.wait_window()


def center_dialog(dialog: tk.Toplevel, master: tk.Misc) -> None:
    dialog.update_idletasks()
    master.update_idletasks()
    dialog_width = dialog.winfo_reqwidth()
    dialog_height = dialog.winfo_reqheight()
    if not master.winfo_viewable():
        screen_width = dialog.winfo_screenwidth()
        screen_height = dialog.winfo_screenheight()
        x = max((screen_width - dialog_width) // 2, 0)
        y = max((screen_height - dialog_height) // 2, 0)
        dialog.geometry(f"+{x}+{y}")
        return

    master_width = master.winfo_width()
    master_height = master.winfo_height()
    master_x = master.winfo_rootx()
    master_y = master.winfo_rooty()
    x = master_x + max((master_width - dialog_width) // 2, 0)
    y = master_y + max((master_height - dialog_height) // 2, 0)
    dialog.geometry(f"+{x}+{y}")


def apply_app_icon(window: tk.Tk | tk.Toplevel) -> None:
    try:
        ico = resources.files("win_search_aliases").joinpath(APP_ICON_ICO_RESOURCE)
        with resources.as_file(ico) as ico_path:
            window.iconbitmap(default=str(ico_path))
    except (FileNotFoundError, ModuleNotFoundError, ValueError, tk.TclError):
        logger.debug("Could not apply window icon", exc_info=True)


class AliasApp(ttk.Frame):
    def __init__(self, master: tk.Tk) -> None:
        super().__init__(master, padding=10)
        self.master = master
        self.queue: queue.Queue[tuple[str, typing.Any]] = queue.Queue()
        self.candidates: list[AppCandidate] = []
        self.visible_candidates: list[AppCandidate] = []
        self.checked_candidate_keys: set[str] = set()
        self.candidate_alias_sources_by_name: dict[str, set[str]] = {}
        self.candidate_tree_state = CheckableTreeState()
        self.managed_rows: list[ManagedRow] = []
        self.checked_managed_records: set[tuple[str, str, str]] = set()
        self.managed_tree_state = CheckableTreeState()
        self.backups: list[ops.BackupInfo] = []
        self.checked_backup_paths: set[Path] = set()
        self.backup_tree_state = CheckableTreeState()
        deny_list = config.load_deny_list(None)
        self.included_categories: set[str] = set(deny_list.default_disabled_categories)
        self.category_vars: dict[str, tk.BooleanVar] = {}
        self.use_toml_filters = tk.BooleanVar(value=True)
        self.use_default_categories = tk.BooleanVar(value=True)
        self.exclude_cyrillic_only = tk.BooleanVar(value=True)
        self.db_var = tk.StringVar()
        self.use_auto_db_var = tk.BooleanVar(value=True)
        self.db_status_var = tk.StringVar(value="AppsIndex: auto")
        self.total_entries_var = tk.StringVar(value="Total indexed entries: -")
        self.ignored_entries_var = tk.StringVar(value="Ignored by filtering rules: -")
        self.map_var = tk.StringVar(value="auto")
        self.include_full_name_var = tk.BooleanVar(value=False)
        self.min_token_var = tk.IntVar(value=4)
        self.search_var = tk.StringVar()
        self.show_without_aliases_var = tk.BooleanVar(value=False)
        self.remove_kind_var = tk.StringVar(value="all")
        self.status_var = tk.StringVar(value="Ready")
        self.backups_status_var = tk.StringVar(value="")
        self.generate_status_var = tk.StringVar(value="")
        self.managed_status_var = tk.StringVar(value="")
        self._build()
        self.update_db_status()
        self.search_var.trace_add("write", lambda *_args: self.filter_candidates())
        self.show_without_aliases_var.trace_add("write", lambda *_args: self.filter_candidates())
        self._poll_queue()
        self.refresh_managed()
        self.refresh_backups()
        self.refresh_filters()
        self.autoload_candidates_if_database_exists()

    def settings(self) -> ops.AppSettings:
        return ops.AppSettings(
            db=self.db_var.get().strip() or None,
            state_dir=None,
            use_deny_list_filters=self.use_toml_filters.get(),
            use_default_content_exclusions=self.use_default_categories.get(),
            included_content_categories=sorted(self.included_categories),
            exclude_cyrillic_only_names=self.exclude_cyrillic_only.get(),
        )

    def generation_options(self) -> ops.GenerationOptions:
        maps = [item.strip() for item in self.map_var.get().split(",") if item.strip()]
        return ops.GenerationOptions(
            map_names=maps or None,
            default_map="auto",
            include_full_name=self.include_full_name_var.get(),
            min_token_length=self.min_token_var.get(),
        )

    def _build(self) -> None:
        self.winfo_toplevel().title("win-search-aliases")
        self.winfo_toplevel().minsize(900, 620)
        self._build_menu_bar()
        self.grid(sticky="nsew")
        self.winfo_toplevel().columnconfigure(0, weight=1)
        self.winfo_toplevel().rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)

        tabs = ttk.Notebook(self)
        tabs.grid(row=0, column=0, sticky="nsew")
        self._build_generate(tabs)
        self._build_managed(tabs)
        self._build_backups(tabs)

        style = ttk.Style()
        style.configure("Primary.TButton", font=("Segoe UI", 9, "bold"))
        btn = ttk.Button(self, text="Auto Generate All", command=self.auto_generate, style="Primary.TButton")
        btn.place(relx=1.0, y=0, anchor="ne")

        status_frame = ttk.Frame(self)
        status_frame.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        status_frame.columnconfigure(1, weight=1)
        ttk.Label(status_frame, textvariable=self.db_status_var, anchor="w").grid(row=0, column=0, sticky="w")
        ttk.Label(status_frame, textvariable=self.status_var, anchor="e").grid(row=0, column=2, sticky="e")
        style.configure("Refresh.TButton", font=("Segoe UI", 12, "bold"))
        ttk.Button(status_frame, text="↻", width=3, command=self.refresh_all, style="Refresh.TButton").grid(
            row=0, column=3, sticky="e", padx=(8, 0)
        )

    def _build_menu_bar(self) -> None:
        self.menu_bar = tk.Menu(self.winfo_toplevel())

        self.open_menu = tk.Menu(self.menu_bar, tearoff=False)
        self.open_menu.add_command(label="AppsIndex location", command=self.open_apps_index_location)
        self.open_menu.add_command(label="Backups location", command=self.open_backups_location)
        self.open_menu.add_command(label="Logs location", command=self.open_logs_location)
        self.menu_bar.add_cascade(label="Open", menu=self.open_menu)

        self.apps_index_menu = tk.Menu(self.menu_bar, tearoff=False)
        self.apps_index_menu.add_command(label="Choose AppsIndex.db...", command=self.browse_db)
        self.apps_index_menu.add_checkbutton(
            label="Use auto-detected AppsIndex.db", variable=self.use_auto_db_var, command=self._on_auto_db_toggled
        )
        self.menu_bar.add_cascade(label="AppsIndex", menu=self.apps_index_menu)

        self.filter_menu = tk.Menu(self.menu_bar, tearoff=False)
        self.menu_bar.add_cascade(label="Filters", menu=self.filter_menu)

        self.diagnostics_menu = tk.Menu(self.menu_bar, tearoff=False)
        self.diagnostics_menu.add_command(label="Open Log", command=self.open_log)
        self.menu_bar.add_cascade(label="Diagnostics", menu=self.diagnostics_menu)

        self.winfo_toplevel().config(menu=self.menu_bar)

    def open_apps_index_location(self) -> None:
        db = self.db_var.get().strip() or self._auto_db_path()
        if not db:
            show_message(self.master, "win-search-aliases", "AppsIndex database not found.", "info")
            return

        db_path = Path(db)
        if db_path.exists():
            subprocess.run(["explorer", "/select,", str(db_path)], creationflags=subprocess.CREATE_NO_WINDOW)
        else:
            show_message(self.master, "win-search-aliases", "AppsIndex database not found on disk.", "info")

    def open_backups_location(self) -> None:
        state_dir = self.settings().state_dir
        if not state_dir:
            state_dir = default_state_dir()
        backups_dir = Path(state_dir) / "backups"
        if backups_dir.exists():
            os.startfile(str(backups_dir))
        else:
            show_message(self.master, "win-search-aliases", "Backups directory does not exist yet.", "info")

    def open_logs_location(self) -> None:
        try:
            log_dir = log_config.log_dir_path(self.settings().state_dir)
            log_dir.mkdir(parents=True, exist_ok=True)
            subprocess.run(["explorer", str(log_dir)], creationflags=subprocess.CREATE_NO_WINDOW)
        except OSError as exc:
            logger.exception("Failed to open logs location")
            show_message(self.master, "win-search-aliases", f"Could not open logs location:\n\n{exc}", "error")

    def open_log(self) -> None:
        try:
            log_config.open_current_log(self.settings().state_dir)
        except OSError as exc:
            logger.exception("Failed to open log file")
            show_message(self.master, "win-search-aliases", f"Could not open log file:\n\n{exc}", "error")

    def browse_db(self) -> None:
        auto_db = self._auto_db_path()
        if not self.db_var.get().strip() and auto_db is not None:
            confirmed = ask_yes_no(
                self.master,
                "Confirm",
                f"Auto-detected AppsIndex.db:\n{auto_db}\n\nChoose a custom AppsIndex.db instead?",
            )
            if not confirmed:
                return
        path = filedialog.askopenfilename(
            title="Select AppsIndex.db",
            filetypes=[("SQLite database", "*.db"), ("All files", "*.*")],
            parent=self.master,
        )
        if path:
            self.db_var.set(path)
            self.use_auto_db_var.set(False)
            self._after_db_changed()
        else:
            if not self.db_var.get().strip():
                self.use_auto_db_var.set(True)

    def _on_auto_db_toggled(self) -> None:
        if self.use_auto_db_var.get():
            self.use_auto_db()
        else:
            self.use_auto_db_var.set(True)
            self.browse_db()

    def use_auto_db(self) -> None:
        if self.db_var.get().strip() and not ask_yes_no(self.master, "Confirm", "Use the auto-detected AppsIndex.db?"):
            self.use_auto_db_var.set(False)
            return
        self.db_var.set("")
        self.use_auto_db_var.set(True)
        self._after_db_changed()

    def _after_db_changed(self) -> None:
        self.update_db_status()
        self.refresh_managed()
        self.refresh_backups()
        self.refresh_filters()
        self.autoload_candidates_if_database_exists()

    def update_db_status(self) -> None:
        selected = self.db_var.get().strip()
        if selected:
            self.db_status_var.set(f"AppsIndex: {selected}")
            return
        auto_db = self._auto_db_path()
        if auto_db is None:
            self.db_status_var.set("AppsIndex: auto (not found)")
        else:
            self.db_status_var.set("AppsIndex: auto")

    @staticmethod
    def _auto_db_path() -> Path | None:
        try:
            return ops.resolve_db_path(None)
        except (FileNotFoundError, RuntimeError):
            return None

    @staticmethod
    def _create_table_frame(parent: ttk.Frame, *, row: int, pady: tuple[int, int] = (0, 0)) -> ttk.Frame:
        frame = ttk.Frame(parent)
        frame.grid(row=row, column=0, sticky="nsew", pady=pady)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        return frame

    @staticmethod
    def _create_scrolled_tree(
        parent: ttk.Frame,
        *,
        columns: tuple[str, ...],
        row: int = 0,
        column: int = 0,
    ) -> ttk.Treeview:
        tree = ttk.Treeview(
            parent,
            columns=columns,
            show="headings",
            selectmode="extended",
        )
        AliasApp._configure_check_column(tree)
        scrollbar = ttk.Scrollbar(parent, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scrollbar.set)
        tree.grid(row=row, column=column, sticky="nsew")
        scrollbar.grid(row=row, column=column + 1, sticky="ns")
        return tree

    @staticmethod
    def _configure_check_column(tree: ttk.Treeview) -> None:
        tree.heading("checked", text="")
        tree.column("checked", width=34, minwidth=34, stretch=False, anchor="center")

    def _configure_sortable_headings(
        self,
        tree: ttk.Treeview,
        state: CheckableTreeState,
        labels: dict[str, str],
        refresh_func: typing.Callable[[], None],
    ) -> None:
        for column, label in labels.items():

            def make_cmd(col: str) -> typing.Callable[[], None]:
                return lambda: self.sort_tree_by(state, col, refresh_func)

            tree.heading(column, text=label, command=make_cmd(column))
        self._update_tree_headings(tree, state, labels)

    def _build_generate(self, tabs: ttk.Notebook) -> None:
        frame = ttk.Frame(tabs, padding=10)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(2, weight=1)
        tabs.add(frame, text="Generate")

        controls = ttk.Frame(frame)
        controls.grid(row=0, column=0, sticky="ew")
        ttk.Label(controls, text="Maps").grid(row=0, column=0, sticky="w")
        map_values = ["auto", *config.load_keyboard_maps().keys()]
        ttk.Combobox(
            controls,
            textvariable=self.map_var,
            values=map_values,
            state="readonly",
        ).grid(row=0, column=1, sticky="w", padx=6)
        ttk.Checkbutton(controls, text="Full name", variable=self.include_full_name_var).grid(row=0, column=2)
        ttk.Label(controls, text="Min token").grid(row=0, column=3, padx=(12, 4))
        ttk.Spinbox(controls, from_=1, to=40, textvariable=self.min_token_var, width=5).grid(row=0, column=4)
        ttk.Label(controls, textvariable=self.total_entries_var).grid(row=0, column=5, padx=(12, 4))
        ttk.Label(controls, textvariable=self.ignored_entries_var).grid(row=0, column=6, padx=(12, 4))
        controls.columnconfigure(7, weight=1)
        ttk.Label(controls, textvariable=self.generate_status_var, anchor="e").grid(row=0, column=8, sticky="e")

        search = ttk.Frame(frame)
        search.grid(row=1, column=0, sticky="ew", pady=(8, 8))
        search.columnconfigure(1, weight=1)
        ttk.Label(search, text="Filter:").grid(row=0, column=0, sticky="w")
        search_entry = ttk.Entry(search, textvariable=self.search_var)
        bind_entry_shortcuts(search_entry)
        search_entry.grid(row=0, column=1, sticky="ew", padx=6)
        ttk.Checkbutton(search, text="Without aliases", variable=self.show_without_aliases_var).grid(row=0, column=2)
        ttk.Button(search, text="Select/Deselect", command=self.toggle_highlighted_candidates).grid(row=0, column=3)
        ttk.Button(search, text="Generate Checked", command=self.generate_selected).grid(row=0, column=4, padx=(6, 0))
        ttk.Button(search, text="Generate Visible", command=self.generate_visible).grid(row=0, column=5, padx=(6, 0))
        self.add_synonym_button = ttk.Button(
            search, text="Add synonym", command=self.add_custom_alias, state=tk.DISABLED
        )
        self.add_synonym_button.grid(row=0, column=6, padx=(6, 0))

        self.candidate_tree = self._create_scrolled_tree(
            self._create_table_frame(frame, row=2, pady=(8, 0)),
            columns=("checked", "number", "app", "path"),
        )
        self._configure_sortable_headings(
            self.candidate_tree,
            self.candidate_tree_state,
            {"number": "#", "app": "App", "path": "Path"},
            self.filter_candidates,
        )
        self.candidate_tree.column("number", width=42, minwidth=36, stretch=False, anchor="e")
        self.candidate_tree.column("app", width=260, minwidth=160)
        self.candidate_tree.column("path", width=520, minwidth=220)
        self._configure_alias_tags(self.candidate_tree)
        self._bind_checkable_tree(
            self.candidate_tree,
            self.candidate_tree_state,
            self._candidate_count,
            self._candidate_key_for_item,
            self.checked_candidate_keys,
            self.filter_candidates,
        )
        self.candidate_tree.bind("<<TreeviewSelect>>", lambda _event: self.update_add_synonym_button_visibility())
        self.candidate_tree.bind("<Button-3>", self._on_candidate_tree_right_click)

    def _build_managed(self, tabs: ttk.Notebook) -> None:
        frame = ttk.Frame(tabs, padding=10)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(1, weight=1)
        tabs.add(frame, text="Managed")
        controls = ttk.Frame(frame)
        controls.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        self.managed_kind_box = ttk.Combobox(
            controls,
            textvariable=self.remove_kind_var,
            values=["all", "auto", "manual", "custom"],
            width=10,
            state="readonly",
        )
        self.managed_kind_box.grid(row=0, column=0)
        self.managed_kind_box.bind("<<ComboboxSelected>>", lambda _event: self.refresh_managed())
        ttk.Button(controls, text="Select/Deselect", command=self.toggle_highlighted_managed).grid(
            row=0, column=1, padx=(6, 0)
        )
        ttk.Button(controls, text="Remove Selected/Kind", command=self.remove_managed).grid(
            row=0, column=2, padx=(6, 0)
        )
        ttk.Button(controls, text="Remove Visible", command=self.remove_visible_managed).grid(
            row=0, column=3, padx=(6, 0)
        )
        controls.columnconfigure(4, weight=1)
        ttk.Label(controls, textvariable=self.managed_status_var, anchor="e").grid(row=0, column=5, sticky="e")
        self.managed_tree = self._create_scrolled_tree(
            frame,
            columns=("checked", "app", "synonym", "source"),
            row=1,
        )
        self._configure_sortable_headings(
            self.managed_tree,
            self.managed_tree_state,
            {"app": "App", "synonym": "Alias", "source": "Source"},
            self._populate_managed_tree,
        )
        self._configure_alias_tags(self.managed_tree)
        self._bind_checkable_tree(
            self.managed_tree,
            self.managed_tree_state,
            self._managed_count,
            self._managed_record_for_item,
            self.checked_managed_records,
            self._refresh_managed_tree_marks,
        )
        self.managed_tree.bind("<Delete>", lambda _event: self.remove_managed())
        self.managed_tree.bind("<Button-3>", self._on_managed_tree_right_click)

    def _build_backups(self, tabs: ttk.Notebook) -> None:
        frame = ttk.Frame(tabs, padding=10)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(1, weight=1)
        tabs.add(frame, text="Backups")
        controls = ttk.Frame(frame)
        controls.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        ttk.Button(controls, text="Select/Deselect", command=self.toggle_highlighted_backups).grid(row=0, column=0)
        ttk.Button(controls, text="Restore Selected", command=self.restore_selected_backup).grid(
            row=0,
            column=1,
            padx=(6, 0),
        )
        ttk.Button(controls, text="Delete Selected", command=self.delete_selected_backup).grid(
            row=0,
            column=2,
            padx=(6, 0),
        )
        controls.columnconfigure(3, weight=1)
        ttk.Label(controls, textvariable=self.backups_status_var, anchor="e").grid(row=0, column=4, sticky="e")
        self.backup_tree = self._create_scrolled_tree(
            frame,
            columns=("checked", "created", "reason", "status"),
            row=1,
        )
        self._configure_sortable_headings(
            self.backup_tree,
            self.backup_tree_state,
            {"created": "Created", "reason": "Reason", "status": "Status"},
            self._populate_backup_tree,
        )
        self.backup_tree.column("created", width=180, minwidth=140, stretch=False)
        self.backup_tree.column("reason", width=180, minwidth=120)
        self.backup_tree.column("status", width=320, minwidth=180)
        self.backup_tree.tag_configure("clean", background="#dff3e3")
        self.backup_tree.tag_configure("managed", background="#fff2c2")
        self.backup_tree.tag_configure("error", background="#f8d7da")
        self._bind_checkable_tree(
            self.backup_tree,
            self.backup_tree_state,
            self._backup_count,
            self._backup_path_for_item,
            self.checked_backup_paths,
            self._refresh_backup_tree_marks,
        )
        self.backup_tree.bind("<Delete>", lambda _event: self.delete_selected_backup())

    def run_task(self, label: str, worker, done) -> None:
        self.status_var.set(label)

        def target() -> None:
            try:
                result = worker()
                self.queue.put(("ok", (done, result)))
            except Exception as exc:  # noqa: BLE001 - surfaced through the UI.
                logger.exception("UI task failed: %s", label)
                self.queue.put(("error", TaskError(label, exc)))

        threading.Thread(target=target, daemon=True).start()

    def _poll_queue(self) -> None:
        with contextlib.suppress(queue.Empty):
            while True:
                kind, payload = self.queue.get_nowait()
                if kind == "ok":
                    done, result = payload
                    done(result)
                else:
                    error = payload
                    self.status_var.set("Error")
                    show_message(self.master, "win-search-aliases", f"{error.label}\n\n{error.error}", "error")
        self.after(100, self._poll_queue)

    def refresh_all(self) -> None:
        self.refresh_managed()
        self.refresh_backups()
        self.refresh_filters()
        self.load_candidates()

    def scan(self) -> None:
        settings = self.settings()
        self.run_task("Scanning database...", lambda: ops.scan_database(settings), self._show_scan)

    def _show_scan(self, result: ops.ScanResult) -> None:
        report = result.report
        self.total_entries_var.set(f"Total indexed entries: {report.total}")
        self.ignored_entries_var.set(f"Ignored by filtering rules: {report.ignored}")
        self.status_var.set("Scan complete")

    def load_candidates(self) -> None:
        settings = self.settings()

        def worker() -> tuple[list[AppCandidate], ops.ScanResult | None, list[ManagedRow]]:
            candidates = ops.eligible_candidates(settings)
            managed_rows = ops.managed_alias_rows(settings)
            try:
                scan = ops.scan_database(settings)
            except Exception:  # noqa: BLE001 - scanning is supplemental for the summary panel.
                scan = None
            return candidates, scan, managed_rows

        self.run_task("Loading apps...", worker, self._set_candidates_with_scan)

    def autoload_candidates_if_database_exists(self) -> None:
        try:
            ops.resolve_db_path(self.settings().db)
        except (FileNotFoundError, RuntimeError):
            return
        self.load_candidates()

    def _set_candidates(self, candidates: list[AppCandidate]) -> None:
        self.candidates = candidates
        self.candidate_tree_state.anchor_index = None
        valid_keys = {self._candidate_key(candidate) for candidate in candidates}
        self.checked_candidate_keys.intersection_update(valid_keys)
        self.filter_candidates()
        self.generate_status_var.set(f"Loaded {len(candidates)} eligible apps")
        self.status_var.set("Ready")

    def _set_candidates_with_scan(
        self,
        result: tuple[list[AppCandidate], ops.ScanResult | None, list[ManagedRow]],
    ) -> None:
        candidates, scan, managed_rows = result
        self._set_candidate_alias_sources(managed_rows)
        if scan is not None:
            self._show_scan(scan)
        self._set_candidates(candidates)

    def _set_candidate_alias_sources(self, rows: list[ManagedRow]) -> None:
        self.candidate_alias_sources_by_name = alias_sources_by_display_name(rows)

    def filter_candidates(self) -> None:
        selected_keys = {
            self._candidate_key(self.visible_candidates[int(item)])
            for item in self.candidate_tree.selection()
            if int(item) < len(self.visible_candidates)
        }
        filtered = list(search_candidates(self.candidates, self.search_var.get()))
        if self.show_without_aliases_var.get():
            filtered = [c for c in filtered if c.display_name not in self.candidate_alias_sources_by_name]
        self.visible_candidates = filtered
        self._sort_visible_candidates()
        self.candidate_tree.delete(*self.candidate_tree.get_children())
        for index, candidate in enumerate(self.visible_candidates, start=1):
            item = self.candidate_tree.insert(
                "",
                tk.END,
                iid=str(index - 1),
                values=(
                    self._candidate_check_mark(candidate),
                    index,
                    candidate.display_name,
                    self._candidate_path(candidate),
                ),
                tags=self._candidate_alias_tags(candidate),
            )
            if self._candidate_key(candidate) in selected_keys:
                self.candidate_tree.selection_add(item)
        self._update_tree_headings(
            self.candidate_tree,
            self.candidate_tree_state,
            {"number": "#", "app": "App", "path": "Path"},
        )
        self.update_add_synonym_button_visibility()

    @staticmethod
    def _configure_alias_tags(tree: ttk.Treeview) -> None:
        tree.tag_configure(CANDIDATE_ALIAS_AUTO_TAG, background="#dff3e3")
        tree.tag_configure(CANDIDATE_ALIAS_MANUAL_TAG, background="#dfeeff")
        tree.tag_configure(CANDIDATE_ALIAS_CUSTOM_TAG, background="#fff2c2")

    def sort_tree_by(self, state: CheckableTreeState, column: str, refresh_func) -> None:
        if state.sort_column == column:
            state.sort_descending = not state.sort_descending
        else:
            state.sort_column = column
            state.sort_descending = False
        refresh_func()

    def _sort_visible_candidates(self) -> None:
        if self.candidate_tree_state.sort_column is None:
            return
        original_order = {self._candidate_key(candidate): index for index, candidate in enumerate(self.candidates)}
        self.visible_candidates.sort(
            key=lambda candidate: self._candidate_sort_key(candidate, original_order),
            reverse=self.candidate_tree_state.sort_descending,
        )

    def _candidate_sort_key(self, candidate: AppCandidate, original_order: dict[str, int]) -> tuple[object, ...]:
        column = self.candidate_tree_state.sort_column
        if column == "app":
            return (
                candidate.display_name.casefold(),
                self._candidate_path(candidate).casefold(),
                original_order.get(self._candidate_key(candidate), 0),
            )
        if column == "path":
            return (
                self._candidate_path(candidate).casefold(),
                candidate.display_name.casefold(),
                original_order.get(self._candidate_key(candidate), 0),
            )
        return (original_order.get(self._candidate_key(candidate), 0),)

    def _update_tree_headings(self, tree: ttk.Treeview, state: CheckableTreeState, labels: dict[str, str]) -> None:
        for column, label in labels.items():
            suffix = ""
            if state.sort_column == column:
                suffix = " v" if state.sort_descending else " ^"
            tree.heading(column, text=f"{label}{suffix}")

    def selected_candidates(self) -> list[AppCandidate]:
        return [
            candidate for candidate in self.candidates if self._candidate_key(candidate) in self.checked_candidate_keys
        ]

    def highlighted_candidates(self) -> list[AppCandidate]:
        return [self.visible_candidates[int(item)] for item in self.candidate_tree.selection()]

    def _bind_checkable_tree(
        self,
        tree: ttk.Treeview,
        state: CheckableTreeState,
        row_count,
        key_for_item,
        checked_keys: set,
        refresh,
    ) -> None:
        tree.bind(
            "<Button-1>",
            lambda event: self._on_checkable_tree_click(
                event, tree, state, row_count, key_for_item, checked_keys, refresh
            ),
        )
        tree.bind(
            "<space>",
            lambda _event: self._toggle_highlighted_tree_items_event(
                tree, state, row_count, key_for_item, checked_keys, refresh
            ),
        )
        tree.bind("<Up>", lambda _event: self._move_tree_selection_event(tree, state, row_count, -1))
        tree.bind("<Down>", lambda _event: self._move_tree_selection_event(tree, state, row_count, 1))
        tree.bind("<Shift-Up>", lambda _event: self._extend_tree_selection_event(tree, state, row_count, -1))
        tree.bind("<Shift-Down>", lambda _event: self._extend_tree_selection_event(tree, state, row_count, 1))
        tree.bind("<Control-KeyPress>", lambda event: self._on_control_keypress(event, tree))

    def _on_control_keypress(self, event, tree: ttk.Treeview) -> str | None:
        if event.keysym in ("a", "A", "Cyrillic_ef", "Cyrillic_EF", "ф", "Ф") or event.keycode == 65:
            return self._select_all_tree_items_event(tree)
        return None

    def _select_all_tree_items_event(self, tree: ttk.Treeview) -> str:
        children = tree.get_children()
        if children:
            tree.selection_set(children)
        return "break"

    def _move_tree_selection_event(
        self, tree: ttk.Treeview, state: CheckableTreeState, row_count, direction: int
    ) -> str:
        self._move_tree_selection(tree, state, row_count, direction)
        return "break"

    def _move_tree_selection(self, tree: ttk.Treeview, state: CheckableTreeState, row_count, direction: int) -> None:
        count = row_count()
        if count == 0:
            return
        current = self._current_tree_focus_index(tree)
        if current is None:
            current = 0
        target = max(0, min(count - 1, current + direction))
        self._set_tree_focus(tree, state, target, select=True, update_anchor=True)

    def _extend_tree_selection_event(
        self,
        tree: ttk.Treeview,
        state: CheckableTreeState,
        row_count,
        direction: int,
    ) -> str:
        self._extend_tree_selection(tree, state, row_count, direction)
        return "break"

    def _extend_tree_selection(self, tree: ttk.Treeview, state: CheckableTreeState, row_count, direction: int) -> None:
        count = row_count()
        if count == 0:
            return
        current = self._current_tree_focus_index(tree)
        if current is None:
            current = 0
        if state.anchor_index is None or state.anchor_index >= count:
            state.anchor_index = current
        target = max(0, min(count - 1, current + direction))
        start, end = sorted((state.anchor_index, target))
        tree.selection_set(*[str(index) for index in range(start, end + 1)])
        self._set_tree_focus(tree, state, target, select=False, update_anchor=False)

    @staticmethod
    def _current_tree_focus_index(tree: ttk.Treeview) -> int | None:
        focused = tree.focus()
        if focused:
            return int(focused)
        selection = tree.selection()
        if selection:
            return int(selection[-1])
        return None

    @staticmethod
    def _set_tree_focus(
        tree: ttk.Treeview,
        state: CheckableTreeState,
        index: int,
        *,
        select: bool,
        update_anchor: bool,
    ) -> None:
        item = str(index)
        if update_anchor:
            state.anchor_index = index
        if select:
            tree.selection_set(item)
        tree.focus(item)
        tree.see(item)
        tree.focus_set()

    def _toggle_highlighted_tree_items_event(
        self,
        tree: ttk.Treeview,
        state: CheckableTreeState,
        row_count,
        key_for_item,
        checked_keys: set,
        refresh,
    ) -> str:
        focus = self._current_tree_focus_index(tree)
        self._toggle_highlighted_tree_items(tree, key_for_item, checked_keys, refresh)
        self._restore_tree_keyboard_position(tree, state, row_count, focus)
        return "break"

    def _toggle_highlighted_tree_items(self, tree: ttk.Treeview, key_for_item, checked_keys: set, refresh) -> None:
        highlighted = [key_for_item(item) for item in tree.selection()]
        should_check = any(key not in checked_keys for key in highlighted)
        for key in highlighted:
            if should_check:
                checked_keys.add(key)
            else:
                checked_keys.discard(key)
        refresh()

    def _on_checkable_tree_click(
        self,
        event,
        tree: ttk.Treeview,
        state: CheckableTreeState,
        row_count,
        key_for_item,
        checked_keys: set,
        refresh,
    ) -> str | None:
        if tree.identify_column(event.x) != "#1":
            return None
        item = tree.identify_row(event.y)
        if not item:
            return None
        key = key_for_item(item)
        if key in checked_keys:
            checked_keys.remove(key)
        else:
            checked_keys.add(key)
        index = int(item)
        refresh()
        tree.selection_set(item)
        self._restore_tree_keyboard_position(tree, state, row_count, index)
        return "break"

    def _restore_tree_keyboard_position(
        self,
        tree: ttk.Treeview,
        state: CheckableTreeState,
        row_count,
        index: int | None,
    ) -> None:
        if index is None or index >= row_count():
            return
        self._set_tree_focus(tree, state, index, select=False, update_anchor=True)

    def move_candidate_selection_event(self, direction: int) -> str:
        return self._move_tree_selection_event(
            self.candidate_tree, self.candidate_tree_state, self._candidate_count, direction
        )

    def extend_candidate_selection_event(self, direction: int) -> str:
        return self._extend_tree_selection_event(
            self.candidate_tree, self.candidate_tree_state, self._candidate_count, direction
        )

    def extend_candidate_selection(self, direction: int) -> None:
        self._extend_tree_selection(self.candidate_tree, self.candidate_tree_state, self._candidate_count, direction)

    def check_highlighted_candidates(self) -> None:
        self._check_highlighted_tree_items(
            self.candidate_tree, self._candidate_key_for_item, self.checked_candidate_keys, self.filter_candidates
        )

    def toggle_highlighted_candidates_event(self, _event) -> str:
        return self._toggle_highlighted_tree_items_event(
            self.candidate_tree,
            self.candidate_tree_state,
            self._candidate_count,
            self._candidate_key_for_item,
            self.checked_candidate_keys,
            self.filter_candidates,
        )

    def toggle_highlighted_candidates(self) -> None:
        self._toggle_highlighted_tree_items(
            self.candidate_tree, self._candidate_key_for_item, self.checked_candidate_keys, self.filter_candidates
        )

    def _check_highlighted_tree_items(self, tree: ttk.Treeview, key_for_item, checked_keys: set, refresh) -> None:
        for item in tree.selection():
            checked_keys.add(key_for_item(item))
        refresh()

    def _candidate_count(self) -> int:
        return len(self.visible_candidates)

    def _candidate_key_for_item(self, item: str) -> str:
        return self._candidate_key(self.visible_candidates[int(item)])

    def _managed_count(self) -> int:
        return len(self.managed_rows)

    def _managed_record_for_item(self, item: str) -> tuple[str, str, str]:
        display_name, synonym, _rank, source = self.managed_rows[int(item)]
        return display_name, synonym, source

    def _managed_records_for_items(self, items) -> set[tuple[str, str, str]]:
        return {self._managed_record_for_item(item) for item in items if int(item) < len(self.managed_rows)}

    def _backup_count(self) -> int:
        return len(self.backups)

    def _backup_path_for_item(self, item: str) -> Path:
        return self.backups[int(item)].path

    def _selected_backups(self) -> list[ops.BackupInfo]:
        if self.checked_backup_paths:
            return [backup for backup in self.backups if backup.path in self.checked_backup_paths]
        return [self.backups[int(item)] for item in self.backup_tree.selection()]

    def toggle_highlighted_managed(self) -> None:
        self._toggle_highlighted_tree_items(
            self.managed_tree,
            self._managed_record_for_item,
            self.checked_managed_records,
            self._refresh_managed_tree_marks,
        )

    def _refresh_managed_tree_marks(self) -> None:
        self._refresh_tree_marks(
            self.managed_tree,
            self._managed_record_for_item,
            self._managed_check_mark,
        )

    def toggle_highlighted_backups(self) -> None:
        self._toggle_highlighted_tree_items(
            self.backup_tree,
            self._backup_path_for_item,
            self.checked_backup_paths,
            self._refresh_backup_tree_marks,
        )

    def _refresh_backup_tree_marks(self) -> None:
        self._refresh_tree_marks(
            self.backup_tree,
            self._backup_path_for_item,
            self._backup_check_mark,
        )

    def _refresh_tree_marks(self, tree: ttk.Treeview, key_for_item, check_mark_func) -> None:
        for item in tree.get_children():
            values = list(tree.item(item, "values"))
            if not values:
                continue
            values[0] = check_mark_func(key_for_item(item))
            tree.item(item, values=values)

    @staticmethod
    def _check_mark(key, checked_keys: set) -> str:
        return CHECKED_MARK if key in checked_keys else UNCHECKED_MARK

    def update_add_synonym_button_visibility(self) -> None:
        if len(self.candidate_tree.selection()) == 1:
            self.add_synonym_button.state(["!disabled"])
        else:
            self.add_synonym_button.state(["disabled"])

    def _on_candidate_tree_right_click(self, event: tk.Event) -> None:
        region = self.candidate_tree.identify_region(event.x, event.y)
        if region != "cell":
            return
        row_id = self.candidate_tree.identify_row(event.y)
        if not row_id:
            return

        values = self.candidate_tree.item(row_id, "values")
        if not values or len(values) < 4:
            return

        app_name = str(values[2])
        path = str(values[3])

        self.candidate_tree.selection_set(row_id)

        menu = tk.Menu(self.master, tearoff=False)
        menu.add_command(label="Copy App Name", command=lambda: self._copy_to_clipboard(app_name))
        menu.add_command(label="Copy Path", command=lambda: self._copy_to_clipboard(path))
        menu.tk_popup(event.x_root, event.y_root)

    def _on_managed_tree_right_click(self, event: tk.Event) -> None:
        region = self.managed_tree.identify_region(event.x, event.y)
        if region != "cell":
            return
        row_id = self.managed_tree.identify_row(event.y)
        if not row_id:
            return

        values = self.managed_tree.item(row_id, "values")
        if not values or len(values) < 3:
            return

        app_name = str(values[1])
        alias = str(values[2])

        self.managed_tree.selection_set(row_id)

        menu = tk.Menu(self.master, tearoff=False)
        menu.add_command(label="Copy App Name", command=lambda: self._copy_to_clipboard(app_name))
        menu.add_command(label="Copy Alias", command=lambda: self._copy_to_clipboard(alias))
        menu.tk_popup(event.x_root, event.y_root)

    def _copy_to_clipboard(self, text: str) -> None:
        self.clipboard_clear()
        self.clipboard_append(text)

    def _candidate_check_mark(self, candidate: AppCandidate) -> str:
        return self._check_mark(self._candidate_key(candidate), self.checked_candidate_keys)

    def _managed_check_mark(self, record: tuple[str, str, str]) -> str:
        return self._check_mark(record, self.checked_managed_records)

    def _backup_check_mark(self, path: Path) -> str:
        return self._check_mark(path, self.checked_backup_paths)

    def _candidate_alias_tags(self, candidate: AppCandidate) -> tuple[str, ...]:
        sources = self.candidate_alias_sources_by_name.get(candidate.display_name, set())
        return self._alias_source_tags(sources)

    @staticmethod
    def _alias_source_tags(sources: set[str]) -> tuple[str, ...]:
        if SOURCE_CUSTOM in sources:
            return (CANDIDATE_ALIAS_CUSTOM_TAG,)
        if SOURCE_GENERATED_MANUAL in sources:
            return (CANDIDATE_ALIAS_MANUAL_TAG,)
        if SOURCE_GENERATED_AUTO in sources:
            return (CANDIDATE_ALIAS_AUTO_TAG,)
        return ()

    @staticmethod
    def _alias_row_tags(source: str) -> tuple[str, ...]:
        return AliasApp._alias_source_tags({source})

    @staticmethod
    def _candidate_path(candidate: AppCandidate) -> str:
        return candidate.content_c1 or candidate.app_id

    @staticmethod
    def _candidate_key(candidate: AppCandidate) -> str:
        return "\0".join([candidate.display_name, candidate.app_id, candidate.content_c1])

    def generate_selected(self) -> None:
        self._generate_for_candidates(
            self.selected_candidates(),
            "generate-selected",
            "Select at least one app.",
            "Generate selected aliases?",
        )

    def generate_visible(self) -> None:
        visible = self.visible_candidates
        self._generate_for_candidates(
            visible,
            "generate-visible",
            "No apps visible to generate.",
            f"Generate aliases for {len(visible)} visible apps?",
        )

    def _generate_for_candidates(
        self,
        candidates: list,
        reason: str,
        empty_message: str,
        confirm_question: str,
    ) -> None:
        if not candidates:
            show_message(self.master, "win-search-aliases", empty_message, "warning")
            return
        settings = self.settings()
        generation_options = self.generation_options()
        self._preview_then_apply(
            lambda: ops.build_generated_alias_plan(
                settings,
                generation_options,
                candidates,
                reason,
                SOURCE_GENERATED_MANUAL,
            ),
            confirm_question,
        )

    def auto_generate(self) -> None:
        settings = self.settings()

        def worker() -> ops.AliasPlan:
            return ops.build_generated_alias_plan(
                settings,
                ops.GenerationOptions(map_names="auto", default_map="auto"),
                ops.eligible_candidates(settings),
                "auto",
                SOURCE_GENERATED_AUTO,
                replace_source=True,
            )

        self._preview_then_apply(worker, "Auto-generate aliases for all eligible apps?")

    def add_custom_alias(self) -> None:
        selected = self.highlighted_candidates()
        if len(selected) != 1:
            show_message(self.master, "win-search-aliases", "Select exactly one app in the Generate tab.", "warning")
            return
        value = ask_synonym(self.master)
        aliases = [item.strip() for item in (value or "").split(",") if item.strip()]
        if not aliases:
            return
        settings = self.settings()
        self._preview_then_apply(
            lambda: ops.build_custom_alias_plan(settings, selected[0], aliases),
            "Add custom aliases?",
        )

    def _preview_then_apply(self, worker, question: str) -> None:
        def done(plan: ops.AliasPlan) -> None:
            if not plan.groups and not plan.replace_source:
                show_message(self.master, "win-search-aliases", "No aliases to apply.", "info")
                self.status_var.set("Nothing to apply")
                return
            summary = f"Alias groups: {len(plan.groups)}\nAlias rows: {plan.total_aliases}\nSource: {plan.source}"
            create_backup = ask_yes_no(self.master, "Backup", f"{summary}\n\nCreate a backup before applying?")
            if not ask_yes_no(self.master, "Confirm", question):
                self.status_var.set("Cancelled")
                return
            settings = self.settings()
            self.run_task(
                "Applying aliases...",
                lambda: ops.apply_alias_plan(plan, settings, create_backup_requested=create_backup),
                self._show_apply_result,
            )

        self.run_task("Preparing aliases...", worker, done)

    def _show_apply_result(self, result: ops.ApplyResult) -> None:
        backup = f"\nBackup: {result.backup}" if result.backup else ""
        show_message(
            self.master,
            "win-search-aliases",
            f"Inserted: {result.inserted}\nRemoved: {result.removed}{backup}",
            "info",
        )
        self._refresh_after_modification("Applied")

    def _refresh_after_modification(self, status: str) -> None:
        self.status_var.set(status)
        self.refresh_candidate_alias_sources()
        self.refresh_managed()
        self.refresh_backups()

    def refresh_candidate_alias_sources(self) -> None:
        if not self.candidates:
            return
        settings = self.settings()
        self.run_task("Loading alias markers...", lambda: ops.managed_alias_rows(settings), self._show_alias_sources)

    def _show_alias_sources(self, rows: list[ManagedRow]) -> None:
        self._set_candidate_alias_sources(rows)
        self.filter_candidates()

    def refresh_managed(self) -> None:
        kind = self.remove_kind_var.get()
        kinds = None if kind == "all" else [kind]
        settings = self.settings()
        self.run_task(
            "Loading managed aliases...",
            lambda: ops.managed_alias_rows(settings, kinds),
            self._show_managed,
        )

    def _show_managed(self, rows: list[ManagedRow]) -> None:
        self.managed_rows = rows
        valid_records = {self._managed_record_for_item(str(index)) for index in range(len(rows))}
        self.checked_managed_records.intersection_update(valid_records)
        self._populate_managed_tree()
        if self.remove_kind_var.get() == "all":
            self._set_candidate_alias_sources(rows)
            self.filter_candidates()
            status = self._managed_counts_status(row_counts_by_kind(rows))
            self.managed_status_var.set(status)
        else:
            self.managed_status_var.set(f"Loaded {len(rows)} aliases")
        self.status_var.set("Ready")

    @staticmethod
    def _managed_counts_status(counts: dict[str, int]) -> str:
        labels = [("auto", "Auto"), ("manual", "Manual"), ("custom", "Custom")]
        parts = [f"{label}: {counts[kind]}" for kind, label in labels if counts[kind]]
        return " | ".join(parts) if parts else "No aliases"

    def _populate_managed_tree(self) -> None:
        if self.managed_tree_state.sort_column is not None:
            self._sort_managed_rows()
        self.managed_tree_state.anchor_index = None
        self.managed_tree.delete(*self.managed_tree.get_children())
        for index, row in enumerate(self.managed_rows):
            display_name, synonym, _rank, source = row
            record = display_name, synonym, source
            self.managed_tree.insert(
                "",
                tk.END,
                iid=str(index),
                text=display_name,
                values=(self._managed_check_mark(record), display_name, synonym, source),
                tags=self._alias_row_tags(source),
            )
        self._update_tree_headings(
            self.managed_tree,
            self.managed_tree_state,
            {"app": "App", "synonym": "Alias", "source": "Source"},
        )

    def _sort_managed_rows(self) -> None:
        column = self.managed_tree_state.sort_column
        reverse = self.managed_tree_state.sort_descending

        def sort_key(row):
            display_name, synonym, _rank, source = row
            if column == "app":
                return display_name.casefold(), synonym.casefold()
            if column == "synonym":
                return synonym.casefold(), display_name.casefold()
            if column == "source":
                return source.casefold(), display_name.casefold()
            return ()

        self.managed_rows.sort(key=sort_key, reverse=reverse)

    def remove_managed(self) -> None:
        records = set(self.checked_managed_records)
        if not records:
            records = self._managed_records_for_items(self.managed_tree.selection())
        kind = self.remove_kind_var.get()
        kinds = None if kind == "all" else [kind]
        if records:
            message = f"Create a backup and remove {len(records)} selected alias row(s)?"
        else:
            message = "Create a backup and remove all matching managed aliases for the selected kind?"

        if not ask_yes_no(self.master, "Confirm", message):
            return
        settings = self.settings()
        if records:

            def worker() -> ops.ApplyResult:
                return ops.remove_managed_alias_records_exact(settings, records)

        else:

            def worker() -> ops.ApplyResult:
                return ops.remove_managed_aliases(settings, kinds=kinds)

        self.run_task(
            "Removing managed aliases...",
            worker,
            self._show_remove_result,
        )

    def remove_visible_managed(self) -> None:
        if not self.managed_rows:
            show_message(self.master, "win-search-aliases", "No managed aliases visible to remove.", "warning")
            return

        records = {(row[0], row[1], row[3]) for row in self.managed_rows}

        if not ask_yes_no(
            self.master, "Confirm", f"Create a backup and remove {len(records)} visible managed alias(es)?"
        ):
            return

        settings = self.settings()

        def worker() -> ops.ApplyResult:
            return ops.remove_managed_alias_records_exact(settings, records)

        self.run_task(
            "Removing managed aliases...",
            worker,
            self._show_remove_result,
        )

    def _show_remove_result(self, result: ops.ApplyResult) -> None:
        show_message(self.master, "win-search-aliases", f"Removed: {result.removed}\nBackup: {result.backup}", "info")
        self._refresh_after_modification("Removed managed aliases")

    def refresh_backups(self) -> None:
        settings = self.settings()
        self.run_task("Loading backups...", lambda: ops.backup_infos(settings), self._show_backups)

    def _show_backups(self, backups: list[ops.BackupInfo]) -> None:
        self.backups = backups
        self.checked_backup_paths.intersection_update({backup.path for backup in backups})
        self._populate_backup_tree()
        self.backups_status_var.set(f"Loaded {len(backups)} backups")

    def _populate_backup_tree(self) -> None:
        if self.backup_tree_state.sort_column is not None:
            self._sort_backups()
        self.backup_tree_state.anchor_index = None
        self.backup_tree.delete(*self.backup_tree.get_children())
        for index, backup in enumerate(self.backups):
            try:
                dt = datetime.fromisoformat(backup.created_at.replace("Z", "+00:00"))
                created_at_human = dt.astimezone().strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                created_at_human = backup.created_at

            self.backup_tree.insert(
                "",
                tk.END,
                iid=str(index),
                values=(self._backup_check_mark(backup.path), created_at_human, backup.reason, backup.status),
                tags=(self._backup_tag(backup),),
            )
        self._update_tree_headings(
            self.backup_tree,
            self.backup_tree_state,
            {"created": "Created", "reason": "Reason", "status": "Status"},
        )

    def _sort_backups(self) -> None:
        column = self.backup_tree_state.sort_column
        reverse = self.backup_tree_state.sort_descending

        def sort_key(backup: ops.BackupInfo):
            if column == "created":
                return backup.created_at, backup.path
            if column == "reason":
                return backup.reason.casefold(), backup.created_at
            if column == "status":
                return backup.status.casefold(), backup.created_at
            return ()

        self.backups.sort(key=sort_key, reverse=reverse)

    @staticmethod
    def _backup_tag(backup: ops.BackupInfo) -> str:
        if backup.has_error:
            return "error"
        if backup.status.startswith("modified:"):
            return "managed"
        return "clean"

    def restore_selected_backup(self) -> None:
        backups = self._selected_backups()
        if not backups:
            show_message(self.master, "win-search-aliases", "Select a backup.", "warning")
            return
        if len(backups) != 1:
            show_message(self.master, "win-search-aliases", "Select exactly one backup to restore.", "warning")
            return
        backup = backups[0]
        if not ask_yes_no(self.master, "Confirm", f"Restore database from {backup.path.name}?"):
            return
        settings = self.settings()
        self.run_task(
            "Restoring backup...",
            lambda: ops.restore_backup(settings, backup.path),
            self._show_restore_result,
        )

    def _show_restore_result(self, result: ops.ApplyResult) -> None:
        show_message(self.master, "win-search-aliases", f"Database restored.\nSafety backup: {result.backup}", "info")
        self.status_var.set("Database restored")
        self.refresh_backups()

    def delete_selected_backup(self) -> None:
        backups = self._selected_backups()
        if not backups:
            show_message(self.master, "win-search-aliases", "Select a backup.", "warning")
            return
        label = backups[0].path.name if len(backups) == 1 else f"{len(backups)} backups"
        if not ask_yes_no(self.master, "Confirm", f"Delete {label}?"):
            return
        settings = self.settings()
        self.run_task(
            "Deleting backup...",
            lambda: [ops.delete_backup(settings, backup.path) for backup in backups],
            self._show_delete_backup_result,
        )

    def _show_delete_backup_result(self, results: list[ops.DeleteBackupResult]) -> None:
        removed_files = sum(1 for result in results if result.removed_file)
        missing_files = len(results) - removed_files
        show_message(
            self.master,
            "win-search-aliases",
            f"Backups deleted: {len(results)}\nFiles removed: {removed_files}\nAlready missing: {missing_files}",
            "info",
        )
        self.status_var.set("Backups deleted")
        self.refresh_backups()

    def refresh_filters(self) -> None:
        counts = ops.filter_category_counts(self.settings())
        self._sync_filter_checkbuttons(counts)

    def _sync_filter_checkbuttons(self, counts: dict[str, int]) -> None:
        self.filter_menu.delete(0, tk.END)
        self.category_vars = {}
        self.filter_menu.add_checkbutton(
            label="TOML deny-list filters",
            variable=self.use_toml_filters,
            onvalue=True,
            offvalue=False,
            command=self.update_filter_settings,
        )
        self.filter_menu.add_checkbutton(
            label="Cyrillic names",
            variable=self.exclude_cyrillic_only,
            onvalue=True,
            offvalue=False,
            command=self.update_filter_settings,
        )
        self.filter_menu.add_separator()
        for category, count in counts.items():
            var = tk.BooleanVar(value=category not in self.included_categories)
            self.category_vars[category] = var
            self.filter_menu.add_checkbutton(
                label=f"{category.replace('-', ' ').title()} ({count})",
                variable=var,
                onvalue=True,
                offvalue=False,
                command=self.update_filter_categories,
            )
        if not counts:
            self.filter_menu.add_command(label="No filter categories", state=tk.DISABLED)

    def update_filter_settings(self) -> None:
        self.refresh_filters()
        self.autoload_candidates_if_database_exists()

    def update_filter_categories(self) -> None:
        self.included_categories = {category for category, var in self.category_vars.items() if not var.get()}
        self.use_default_categories.set(True)
        self.refresh_filters()
        self.autoload_candidates_if_database_exists()


def smoke_test() -> int:
    root = tk.Tk()
    apply_app_icon(root)
    root.withdraw()
    root.update_idletasks()
    root.destroy()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    log_path = log_config.configure_logging(app_mode="gui")
    logger.debug("UI logging to %s", log_path)
    if SMOKE_TEST_ARG in args:
        return smoke_test()

    root = tk.Tk()
    apply_app_icon(root)
    if sys.platform != "win32":
        logger.error(WINDOWS_ONLY_ERROR)
        root.withdraw()
        show_message(root, "win-search-aliases", WINDOWS_ONLY_ERROR, "error")
        root.destroy()
        return 1

    AliasApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
