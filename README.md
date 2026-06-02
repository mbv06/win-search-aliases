<div align="center">

# win-search-aliases

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](https://opensource.org/licenses/MIT)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-3776AB.svg?logo=python&logoColor=white)](https://www.python.org/)
[![Windows 11](https://img.shields.io/badge/os-Windows%2011%20Only-0078D4.svg?logo=windows&logoColor=white)](https://www.microsoft.com/windows)
[![Tests](https://img.shields.io/github/actions/workflow/status/mbv06/win-search-aliases/tests.yml?logo=github)](https://github.com/mbv06/win-search-aliases/actions/workflows/tests.yml)
[![Latest Release](https://img.shields.io/github/v/release/mbv06/win-search-aliases?logo=github)](https://github.com/mbv06/win-search-aliases/releases/latest)

**Find any app from Windows Start search — even when you type in the wrong keyboard layout.**

<img width="766" height="426" alt="image" src="https://github.com/user-attachments/assets/64859070-8248-40c6-80c6-4fa3fcf859a5" />

Adds managed synonyms to the internal Windows Search `AppsIndex.db` so that apps are found regardless of whether you typed in Latin or Cyrillic (or any other supported layout).

[**View Changelog**](CHANGELOG.md)

</div>

> [!NOTE]
> **Windows 11 Only.** This tool relies on the Windows 11 internal Search database schema and will not work correctly on Windows 10 or older versions.

---

## Screenshots

| CLI | UI |
|:---:|:---:|
| <img width="410" height="481" alt="image" src="https://github.com/user-attachments/assets/4e68f2f9-2327-489b-bce0-a84cc8b7cba3" /> | <img width="604" height="448" alt="image" src="https://github.com/user-attachments/assets/fa75ca20-68be-41b0-a8de-f07da906e54b" />
|

---

## Quick Install

**One-liner** (PowerShell):

```powershell
powershell -ExecutionPolicy Bypass -c "iwr -useb https://raw.githubusercontent.com/mbv06/win-search-aliases/main/install.ps1 | iex"
```

<details>
<summary><b>Other install methods</b></summary>

### pip

```bash
python -m pip install --upgrade https://github.com/mbv06/win-search-aliases/archive/refs/heads/main.zip
```

### Standalone EXE

Download `win-search-aliases-ui-<version>.exe` from the [latest release](https://github.com/mbv06/win-search-aliases/releases/latest) — no Python needed.

</details>

---

## Usage

```powershell
# Auto-detect layouts & apply aliases
win-search-aliases auto

# Interactive generation for specific apps
win-search-aliases generate-selected --map uk-jcuken

# Open desktop UI
win-search-aliases-ui
```

<details>
<summary><b>More commands</b></summary>

```powershell
# Add a custom alias
win-search-aliases add-custom --app "Google Chrome" --alias browser

# List / remove managed aliases
win-search-aliases list-managed
win-search-aliases remove-managed

# Restore from backup
win-search-aliases restore-backup --latest

# Preview without changes
win-search-aliases generate-all --map auto --dry-run
```

</details>

---

## How It Works

1. Reads the Windows Search `AppsIndex.db` (SQLite) located in `%LOCALAPPDATA%`.
2. For each app, generates transliterated synonyms based on the selected keyboard layout map.
3. Writes synonym rows with managed `source` tags — only those rows are ever touched.
4. Restarts `SearchHost` so Start search picks up the changes immediately.

> [!NOTE]
> A timestamped backup is created before every write. Use `restore-backup` to roll back at any time.

---

## Supported Layouts

The tool comes with a set of default keyboard maps. You can see the full list of natively supported layouts in the [`keyboard_maps.toml`](src/win_search_aliases/config/keyboard_maps.toml) file.

If your preferred layout is not supported, you can easily add it yourself! Check out the [Adding Custom Keyboard Layouts](docs/custom_keyboard_layouts.md) guide for instructions on how to define your own layout.

---

## Safety

- Only rows created by this tool (sources: `WinSearchAliasesAuto`, `WinSearchAliasesManual`, `WinSearchAliasesCustom`) are modified or removed.
- Re-running commands is idempotent — no duplicate rows.
- This is an **unofficial** edit of the Windows Search database. It may need to be re-applied after major Windows updates or search index rebuilds.

---

## Uninstall

Before completely removing the tool, you should remove all managed aliases from the Windows Search database:

```powershell
win-search-aliases remove-managed
```

Then, depending on how you installed it:

**If installed via the PowerShell script (`install.ps1`):**
Simply delete the created virtual environment directory (this will keep your backups safe):
```powershell
Remove-Item -Recurse -Force "$env:LOCALAPPDATA\win-search-aliases\venv"
```

**If installed via pip:**
```bash
python -m pip uninstall win-search-aliases
```

---

## License

[MIT](https://opensource.org/licenses/MIT)
