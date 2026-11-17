# Changelog

**English** | [简体中文](./CHANGELOG_ZH.md)

All notable changes to **`acerola-orion`** will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.3.0] - 2026-11-17 — Passive Prompt KV Cache Radar, Timer Disambiguation & High-DPI UI Anti-Truncation

### Added & Hardened
- **Passive Local Prompt KV Cache 250s Warmth Radar & Floating Capsule HUD (`acerola_orion/cache_radar.py`, `rpc.py`, `panel.py`, `__main__.py`)**:
  - Added `read_shared_tail_bytes()` lock-free `16KB` tail reader and `CacheRadarSampler` (`0` network packets, `0` port hooks, strict media-folder isolation & junction containment) computing live context token volume, cache hit rate, and a **250s (4m10s) prompt cache warmth countdown** with zero-I/O 1-second memory ticks.
  - Added three session-targeting modes (`smart_guard` protecting warm `>=100K` heavy windows, `latest`, and manual `pinned`), integrated into the Astrolabe Hub radar bar, `acerola-orion radar` CLI subcommand, Zen Mode `Cache` focus, and a draggable frameless topmost capsule HUD (`smart` / `always` / `off`).
- **Standby Quota Timer Disambiguation & Log 429 De-Noising (`acerola_orion/telemetry.py` & `cli_render.py`)**:
  - Added `describe_quota_timer()` to distinguish `100.000%` untouched `5h` rolling buckets (sliding placeholder timestamp) from genuinely active reset countdowns.
  - Separated non-blocking auxiliary `429` events (auto-title, sidecar monitor, web summary) into `aux_429_count`, added stage-1/stage-2 sidecar failure counters, and stripped uninitialized Go `glog` noise prefixes.
- **High-DPI Layout & Tray-Wakeup Redraw Hardening (`acerola_orion/panel.py`)**:
  - Added `Orion.Horizontal.TScrollbar` across all ledger tables, packed right-aligned percentage/badge readouts before elastic titles to prevent narrow-window clipping, bound dynamic `wraplength` on guard/diagnostics banners, and added single-shot `_deferred_redraw_pending` latches plus tray-restore canvas recalibration to eliminate `1px` hidden-canvas truncation without idle spinlocks (`15/15` verification scenarios passing).

## [1.2.0] - 2026-11-16 — Orion Astrolabe Chronograph UI & Zen Focus Mode

### Added & Refined
- **Orion Astrolabe & Chronograph Desktop UI (`acerola_orion/panel.py`)**:
  - Redesigned the desktop observatory with custom vector **Dual Orion Astrolabe Chronographs** (`5h` outer arc, `Weekly` inner orbital ring, 60-tick precision watch bezel, and **Golden Time-Pace Marker** computing live quota surplus vs. overburn against window elapsed time).
  - Added interactive vector telemetry & spend charts (`OrionChartWidget` with hover crosshair inspection) covering 24h pool-isolated quota trajectories, 14-day daily USD spend bars, and live sidecar monitor latency waveforms, paired with native Win32 `DwmSetWindowAttribute` immersive dark titlebar & custom dark scrollbars.
- **Optional Zen / Simplified Focus Mode (`acerola-orion panel --zen`)**:
  - Added one-click toggleable **Zen Focus Mode** (persisted in `ui_prefs.json` and accessible via CLI `--zen` / `--full` / `--reload` or system tray menu) displaying strictly **1 Hero Metric + 1 Statistical Chart** at a time (`Gemini`, `Claude/3P`, `Today Spend`, or `Smoothness`).
- **Telemetry & Series Precision Refinements (`acerola_orion/telemetry.py` & `acerola_orion/ledger.py`)**:
  - Excluded background Google Clearcut analytics timeouts (`play.googleapis.com/log`, `firebaselogging`) from `sidecar.timeout_count` so only genuine sidecar/RPC timeouts are counted, and added weekly-reset pool fingerprinting in `get_recent_quota_series()` to eliminate cross-pool spikes (`14/14` verification scenarios passing).

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
