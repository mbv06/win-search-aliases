# Changelog

## v0.1.2 - 2026-06-04

### Changed

- Switched the Python install flow to stable GitHub Release assets instead of installing from the `main` branch.
- Release builds now publish a fixed-name `win-search-aliases-python.tar.gz` asset for `pip install` and include `install.ps1` next to the Windows `.exe`.
- Updated the README and PowerShell installer so one-liner and manual installs both use the latest stable release.

## v0.1.1 - 2026-06-03

### Fixed

- Improved reliability when Windows Search is using the app index, so both the CLI and desktop UI can recover from temporary database locks while loading apps, showing managed aliases, or generating aliases.
- Reduced the wait before recovery starts when the app index is locked.

## v0.1.0 - 2026-06-01

Initial version of `win-search-aliases`.

### Added

- CLI for scanning the Windows Search `AppsIndex.db` and managing app search aliases.
- Automatic alias generation for Windows Start search across supported keyboard layouts.
- Interactive selection flow for generating aliases only for chosen apps.
- Custom aliases for individual apps.
- Managed alias sources for auto-generated, manually generated, and custom entries.
- Safe update flow with timestamped backups, restore support, and idempotent writes.
- Filtering rules and bundled TOML configuration for layouts, profiles, and deny lists.
- Desktop UI entry point via `win-search-aliases-ui`.
- PowerShell installer and packaging metadata for Python 3.11+ on Windows.
- Test coverage for aliases, CLI behavior, database operations, filters, layouts, logging, metadata, subprocess flags, and UI helpers.
