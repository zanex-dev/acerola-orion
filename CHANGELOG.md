# Changelog

**English** | [简体中文](./CHANGELOG_ZH.md)

All notable changes to **`acerola-orion`** will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.1.0] - 2026-11-15 — Native System Tray Residency, Multi-Scale Icon & Start Menu Integration

### Added
- **Zero-Dependency Win32 System Tray Residency (`acerola_orion/win_tray.py` & `acerola_orion/panel.py`)**:
  - Added native `Shell_NotifyIconW` system tray controller running on a dedicated Win32 message-pump thread (`0%` idle CPU, automatic `TaskbarCreated` re-registration on Explorer restarts, live `5h` / `Weekly` quota hover tooltip, and right-click quick actions).
  - Closing the desktop console (`×`) now smoothly minimizes into the system tray while pausing background UI refresh timers (`0%` CPU sleep) until restored; added `--minimized` startup flag and cross-instance `AllowSetForegroundWindow` + `PostMessageW` tray wakeup.
- **Multi-Size High-DPI Application Icon & Taskbar Identity (`assets/app.ico` & `acerola-orion shortcut`)**:
  - Bundled 9-scale (`16px`–`256px`) RGBA `.ico` artwork and bound `SetCurrentProcessExplicitAppUserModelID` (`Trinity.AcerolaOrion.Panel`) for clean, ungrouped Windows Taskbar identity.
  - Added pure-`ctypes` COM (`IShellLinkW` + `IPropertyStore` `PKEY_AppUserModel_ID`) `acerola-orion shortcut [--remove]` CLI command to register `pythonw.exe` console-free Start Menu & Taskbar-pinnable shortcuts with zero third-party dependencies (`13/13` verification scenarios passing).

## [1.0.0] - 2026-11-15 — Codename: `Acerola-Orion (雅赛劳拉莉昂)` · Stable Observatory Release

### Changed & Promoted
- **Production/Stable Promotion (`pyproject.toml` & `acerola_orion`)**:
  - Promoted package maturity classifier from `4 - Beta` to `5 - Production/Stable` following full static zero-egress audit, Win32 kernel handle lifecycle verification, and Windows CI event-synchronization hardening (`12/12` automated test scenarios passing).
- **Zero-Trust Security Boundary Freeze**:
  - Frozen the `1.x` zero-trust local architecture contract: `0` third-party dependencies (`dependencies = []`), strict `127.0.0.1`-only loopback read-only status queries (`ConnectRpcClient`), zero OAuth/cookie/credential access, zero prompt-body storage, NTFS junction/symlink traversal blocking, and automatic account email redaction.
- **Stable CLI & Desktop Observatory Baseline (Google Antigravity `v2.32+`)**:
  - Stabilized CLI subcommands (`status`, `ledger`, `guard`, `panel`) and High-DPI Obsidian/Crimson `tkinter` console across **Orion-Telemetry** (7-decimal quota watermarks & lock-free `(file_id, offset)` log cursor), **Orion-Ledger** (incremental SQLite token rate-card & offline phantom-drain auditor), and **Acerola-Guillotine** (`0.00%` CPU Win32 `WaitForMultipleObjects` orphan `language_server` exit guard).
- **Documentation & Suite Alignment**:
  - Added bilingual `CHANGELOG.md` / `CHANGELOG_ZH.md` and extended static security self-audit coverage (`tests/test_public_suite.py`) over release documentation, completing `1.x` alignment across the **Kiss-Shot · Acerola-Orion · Heart-Under-Blade** local security suite.

## [0.2.2] - 2026-11-15 — Initial Public Release & Windows CI Synchronization

### Added & Hardened
- **Orion-Telemetry (`acerola_orion/telemetry.py` & `acerola_orion/rpc.py`)**:
  - Sub-millisecond Win32 `CreateToolhelp32Snapshot` process-tree discovery + `127.0.0.1` Connect-RPC read-only status telemetry (`GetUserStatus`, `GetLoadCodeAssist`, `RetrieveUserQuotaSummary`) for 7-decimal `gemini-5h`, `gemini-weekly`, `3p-5h`, and `3p-weekly` remaining quota & reset countdowns.
  - Lock-free `FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE` incremental log cursor tracking `HTTP 429/503` throttling counts and sidecar monitor latency.
- **Orion-Ledger & Phantom-Drain Auditor (`acerola_orion/ledger.py`)**:
  - Privacy-isolated incremental SQLite token indexer comparing `1.0x` standard API USD, `0.5x` promotional USD, and `1.5x` internal weight estimates while pairing 7-decimal quota watermarks against local token deltas.
- **Acerola-Guillotine (`acerola_orion/guillotine.py`)**:
  - Zero-polling Win32 kernel `SYNCHRONIZE` handle guard anchored to the root Electron UI process with same-handle executable image verification, UTF-16LE BOM Unicode WSH launcher support, and event-synchronized Windows CI unit tests.
- **Native Desktop Console (`acerola_orion/panel.py`)**:
  - High-DPI `tkinter` dashboard with live quota gauges, ledger breakdowns, sidecar diagnostics, and one-click guard controls.
