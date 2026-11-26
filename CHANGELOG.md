# Changelog

**English** | [简体中文](./CHANGELOG_ZH.md)

All notable changes to **`acerola-orion`** will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [2.4.5] - 2026-11-26 — Journey's End (Companion Workflow Suite, Reversible Installer & Clean-Room Gate)

> *"Our final update, for real this time — we swear (in true Re-Logic fashion). Already on 2.x so we missed 1.4.5; 2.4.5 will have to do lol."*

### Added
- **Built-in 200K Context EOQ Watermark Hook (`acerola_orion/plugins/eoq_hook.py`)**:
  - Consolidated the zero-dependency stdlib session watermark hook (`python -m acerola_orion.plugins.eoq_hook`) into the core package, featuring `95K / 175K` hot-window handoff alerts, `180K` `PreToolUse` confirmation gates with `PostToolUse` auto-unlock, cold-window rescue draft saving, `/compact` watermark reset, fail-open safety, and bidirectional linkage with the `eoq_guard` per-window exemption toggle.
- **Bundled Official White-Hat Antigravity Companion Suite (`acerola_orion/integrations/antigravity/`, 18 plain-text auditable files)**:
  - **Global companion plugin & skills**: Ships the `acerola-orion` global plugin (`acerola-orion` & `orion-vault` skills) alongside `session-recall` (`recall.py` targeted cross-session slice lookup without full-transcript context bloat) and `kv-keepalive` (short-lived bounded Prompt KV cache warmth helper with automatic early cancel on user reply).
  - **Workspace workflow plugins**: Ships `eoq-task-relay` (`task-decomposer` read-only exploration into `PLAN.md` + `<=1.5K` atomic cards and `stage-handoff` checkpoints, powering the `tasks_board` desktop tab) and `engineering-core` (`version-seal` SHA-256 verified archival into `version_vault`, plus optional `rules/AGENTS.md` engineering rules).
  - **Hook templates & reference docs**: Includes `hooks.template.json`, `eoq_guard_config.json`, and `eoq_context_guard_cheatsheet.md`.
- **Opt-In Non-Destructive Companion Installer CLI (`acerola_orion/companion.py`, `acerola-orion companion`)**:
  - Never touches `~/.gemini/config/` unprompted; adds `companion status | install | uninstall` (`--workspace`, `--include-rules`, `--no-hooks`, `--no-global-skills`, `--force`, `--python-exe`, `--remove-global`, `--remove-workspace`, `--json`).
  - Performs non-destructive `hooks.json` merges (preserving custom user hooks and skipping existing files without `--force`) and surgical removal of Orion-managed entries on `uninstall`.
- **Repository Architecture & Agent Navigation Map (`AGENTS.md`)**:
  - Added module boundary overview, large-file symbol line anchors, and test-suite mapping for low-context code navigation.

### Security & Verification
- **Expanded Clean-Room Static Security Audit (`tests/test_public_suite.py`)**: Recursively scans all `.py` / `.md` / `.json` / `.svg` assets under `acerola_orion/integrations/` plus `AGENTS.md`, enforcing zero external egress, zero credential access, and zero hardcoded local user-directory path leakage.
- Added `tests/test_eoq_hook.py` and `tests/test_companion_installer.py`, expanding the zero-egress clean-room test suite to **`85/85` passing** across both default and `ACEROLA_ORION_DISABLE_PLUGINS=1` bare-kernel modes.

## [2.1.0] - 2026-11-25 — Built-in Optional White-Hat Plugins, Daily Log Archival & Selective Toggles

### Added
- **Three Built-in Official Optional White-Hat Plugins (`acerola_orion/plugins/`, still `0` third-party dependencies, enabled by default and individually toggleable)**:
  - `version_vault`: Append-only non-deletable workspace version vault (`~/.gemini/acerola_orion/version_vault/`) with full `acerola-orion archive` CLI (`status`, `scan`, `seal`, `list`, `restore`, `export`, `push`) and a desktop archive tab.
  - `tasks_board`: Read-only desktop board tab for inspecting `.agents/tasks/` task packs and stage handoff checkpoints.
  - `eoq_guard`: 200K context EOQ watermark alert with per-window exemption toggles.
- **Bare-Kernel & Per-Plugin Selective Toggles**:
  - Individually disable any built-in or external plugin via `ACEROLA_ORION_DISABLED_PLUGINS=eoq_guard` (comma-separated), or run in pure zero-plugin bare-kernel mode via `ACEROLA_ORION_DISABLE_PLUGINS=1`.
- **Daily Log Archival, Midnight Counter Reset & Expanded Radar/HUD Slots (`guillotine.py`, `telemetry.py`, `panel.py`, `ui/plugin_host.py`, `ui/hud.py`)**:
  - Added zero-overwrite daily & 512KB log rollover archival, read-only past-day host log snapshots, stale hub cleanup, automatic midnight telemetry counter reset, timeout deduplication, and expanded radar/HUD plugin slots.

### License, Documentation & Verification
- Updated the repository license to [PolyForm Noncommercial License 1.0.0](./LICENSE) (free for personal and noncommercial use; commercial use and resale prohibited).
- Updated bilingual `README` files and [docs/PLUGIN_ARCHITECTURE.md](./docs/PLUGIN_ARCHITECTURE.md) with the official built-in entry point matrix and opt-out examples.
- Expanded the zero-egress clean-room test suite to **`74/74` passing** across both bare-kernel and plugin-enabled states.

## [2.0.0] - 2026-11-21 — Stdlib Microkernel & Slot-Driven Modular UI Architecture

> *"Ask me why I refactored the whole thing? Because I'm the fucking open-source GOAT."*

### Architecture & Refactoring
- **Zero-Dependency Stdlib Microkernel (`acerola_orion/core/kernel.py`, `core/__init__.py`)**:
  - Extracted a pure Python standard-library microkernel (`dependencies = []`) providing plugin discovery, dependency ordering, lifecycle hooks, and clean isolation between the public base and external/private plugin packages.
  - Future features can now be authored as standalone plugins with explicit plugin dependency declarations rather than modifying monolith internals.
- **Slot-Driven Modular UI (`acerola_orion/ui/**`, `panel.py`)**:
  - Decomposed the 3,400+ line monolithic `panel.py` into focused, maintainable UI modules: `astrolabe.py`, `charts.py`, `hud.py`, `plugin_host.py`, `radar_controller.py`, `window_chrome.py`, `theme.py`, `widgets.py`, `builtin_tabs.py`, and pluggable tab slots under `ui/tabs/` (`common.py`, `hub_tab.py`, `ledger_tab.py`, `vault_tab.py`, `diag_tab.py`, `zen_view.py`).
- **Documentation & Verification (`docs/PLUGIN_ARCHITECTURE.md`, `tests/test_kernel.py`, `tests/test_core_hooks.py`, `tests/test_panel_ui_slots.py`)**:
  - Added plugin architecture specification (`docs/PLUGIN_ARCHITECTURE.md`) detailing hook points, slot registration, and plugin contracts.
  - Expanded the zero-egress clean-room test suite with 1,400+ lines of kernel, hook, and UI-slot tests (**`42/42` passing**).

## [1.4.0] - 2026-11-21 — Orion-Vault Time-Vault, Gemini 4 Neon, 200K 2x Pricing Alert & Security Audit Hardening

### Added
- **Orion-Vault Append-Only Conversation Backup & Recovery (`acerola_orion/vault.py`, `vault_hook.py`, `guillotine.py`, `panel.py`, `cli_render.py`, `__main__.py`)**:
  - Added local-only append-only backup of `conversations/<uuid>.db` (SQLite Online Backup + gzip `conversation.db.gz`), `conversation_summaries.db` rows (`raw_summary` Protobuf preserved), `annotations/<uuid>.pbtxt`, and text/SVG artifacts under `brain/<uuid>/` with deletion interception (`deleted` status), one-click restoration (`vault restore`), and Markdown export (`vault export`).
  - Added controllable `vault_auto_backup` toggle (`config.json` with cross-process file lock & atomic write) accessible from CLI (`vault status|sync|on|off|toggle|list|search|show|restore|export|hook-stop`), the new 4th Full Mode desktop tab (`🗄️ 对话防删保险箱`), system tray menu, and the guard daemon background incremental sweep thread; added 5 Vault launcher scripts (11 total).
  - Enforced strict media-privacy isolation (skipping `.user_uploaded`, `browser_sandbox`, `tempmediaStorage`, `.allow_media`, and bitmap/audio/video binaries) plus symlink/reparse-point traversal blocking (`_is_reparse_or_link`).
- **Gemini 4 Neon (`4N`) Support & 200K 2x Pricing Tier Early Warning (`constants.py`, `ledger.py`, `cache_radar.py`, `panel.py`)**:
  - Added `Gemini 4 Neon (4N)` rate-card classification with automatic ledger cache invalidation when reclassified models appear.
  - Added `180K` approaching and `200K` active 2x double-billing context tier alerts (`COMPACT_COMMAND_TEXT = "/compact"`) with one-click clipboard copy across CLI `radar`, main panel, floating capsule HUD, and system tray menu.

### Security & Reliability Hardening (A01–A18 Audit Baseline)
- **Owned-Handle Process Guard & Non-Destructive Lifecycle (`rpc.py`, `guillotine.py`, `__main__.py`)**:
  - Upgraded orphan `language_server*.exe` termination to `open_owned_language_server` / `terminate_owned_handle` with `advapi32` same-user SID fail-closed verification, install-directory/UI-ancestor/hub ownership checks, and `finally` handle cleanup (A10).
  - Converted `retire-legacy` into a non-destructive read-only inspection helper, eliminated blind PID kills in `stop_guard_daemon`, and aligned `uninstall_guard_autostart` / `guard` CLI exit codes with real status (A18).
- **Proxy Credential Stripping (`telemetry.py`)**:
  - Upgraded `_inspect_proxy_mode()` to parse proxy env vars via `urllib.parse.urlsplit`, stripping userinfo, query, and fragment to preserve only `scheme://host[:port]` (A13).
- **Ledger & Cache Radar Integrity (`ledger.py`, `cache_radar.py`)**:
  - Added `cache_read_input_tokens` compatibility (A01), removed stale missing-file negative caching in `CacheRadarSampler` (A02), hardened incremental transcript parsing with cross-process file locks, SHA-256 prefix verification, corrupt-cursor fallback, and same-step model-switch replacement (A04).
  - Added dedicated `p3_5h_drop_pct` field (A05), even-count `statistics.median` aggregation (A06), numeric `last_epoch` sorting with legacy DB migration (A07), Top-N `EXISTS` query pre-filtering (A08), and prompt-leakage prevention falling back to `conv_id[:8]` with legacy title scrubbing (A15).
- **UI Timer Lifecycle & Accurate Copy Alignment (`panel.py`, `win_tray.py`, `cli_render.py`, `README*`)**:
  - Canceled `_auto_refresh_id` on tray hide/close so repeated hides/restores never stack refresh timers (A03).
  - Aligned ledger/audit wording to state `1.5x` as local preset estimates and removed unmeasured `0.00% CPU` / `<1ms` latency claims (A16 / A17).
- **Verification & Static Clean-Room Audit (`tests/test_public_suite.py`, `.github/workflows/ci.yml`)**:
  - Expanded automated verification suite to **`20/20` passing tests**, covering A01–A18 regressions, Gemini 4 Neon, 200K 2x pricing alerts, and Orion-Vault end-to-end flows while extending `test_01` static zero-egress/clean-room scanning to `vault.py`, `vault_hook.py`, and release documentation.

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
