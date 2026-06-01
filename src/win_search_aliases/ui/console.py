from __future__ import annotations

import os
import sys

RESET = "\033[0m"
COLORS = {
    "blue": "\033[94m",
    "cyan": "\033[36m",
    "green": "\033[32m",
    "red": "\033[31m",
    "yellow": "\033[33m",
    "muted": "\033[90m",
    "bold": "\033[1m",
}


def color(text: object, name: str) -> str:
    if os.environ.get("NO_COLOR") or not sys.stdout.isatty():
        return str(text)
    return f"{COLORS.get(name, '')}{text}{RESET}"


def info(text: object) -> str:
    return color(text, "cyan")


def success(text: object) -> str:
    return color(text, "green")


def warning(text: object) -> str:
    return color(text, "yellow")


def danger(text: object) -> str:
    return color(text, "red")


def heading(text: object) -> str:
    return color(text, "bold")


def muted(text: object) -> str:
    return color(text, "muted")
