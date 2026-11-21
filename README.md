<p align="center">
  <img src="assets/hero-banner.png" width="100%" alt="Acerola-Orion — Local Quota Observatory · Token Ledger · Conversation Time-Vault · Event-Driven Exit Guard" />
</p>

<h1 align="center">Acerola-Orion (雅赛劳拉莉昂)</h1>

<p align="center">
  <strong>7-Decimal Live Quota Telemetry, Passive Prompt KV Cache TTL &amp; 200K 2x Pricing Radar, Local Privacy-Isolated Token Ledger (Orion), Append-Only Conversation Time-Vault (Orion-Vault), and Event-Driven Win32 Kernel Orphan-Process Guard (Acerola) for Google Antigravity.</strong>
</p>

<p align="center">
  <a href="https://github.com/zanex-dev/acerola-orion/actions/workflows/ci.yml"><img src="https://img.shields.io/badge/tests-42%2F42%20passing-brightgreen" alt="Tests" /></a>
  <a href="pyproject.toml"><img src="https://img.shields.io/badge/version-v1.4.0-be123c" alt="Version v1.4.0" /></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-e11d48.svg" alt="License: MIT" /></a>
  <a href="pyproject.toml"><img src="https://img.shields.io/badge/python-3.10%2B%20(0%20deps)-3776AB.svg" alt="Python 3.10+" /></a>
  <img src="https://img.shields.io/badge/network-127.0.0.1%20loopback%20only-059669" alt="Loopback Only" />
</p>

<p align="center">
  <a href="README.md"><strong>English</strong></a> · <a href="README_ZH.md"><strong>简体中文</strong></a> · <a href="docs/PLUGIN_ARCHITECTURE.md">Plugin Architecture</a> · <a href="CHANGELOG.md">Changelog</a>
</p>

---

## 🔒 Zero-Trust Security Self-Verification (Audit in 3 Seconds)

Built from day one with strict local physical boundaries so anyone can inspect and verify the entire codebase in seconds:

1. **Zero Third-Party Dependencies (`dependencies = []`)**: 100% Python >= 3.10 standard library + native Win32 `ctypes` — zero external `pip` supply-chain risk.
2. **Zero External Network Egress (`127.0.0.1` Only)**: The sole HTTP client (`ConnectRpcClient`) enforces `host == "127.0.0.1"` in `__init__` and performs read-only status queries against the running local official language server. **Zero bytes are ever sent to Google or any external server.**
3. **Zero Credential Access & Strict Local Privacy Boundaries**:
   - Never requests OAuth authorization, and **never** reads `state.vscdb`, Windows DPAPI/Keychain, browser cookies, or tokens; proxy environment URLs are sanitized via `urllib.parse.urlsplit` to strip credentials (`userinfo`, query, fragment);
   - **Orion-Ledger** indexes only token integer counters and sanitized session titles (falling back to `conv_id[:8]` when no annotation title exists — never storing prompt/response bodies in the ledger DB);
   - **Orion-Vault** (local-only, toggleable) creates append-only local backups of text transcripts, SQLite conversation DBs, and text/SVG artifacts strictly under `~/.gemini/acerola_orion/chat_vault` while skipping all media/attachments (`.user_uploaded`, `browser_sandbox`, `tempmediaStorage`, `.allow_media`, bitmap/audio/video), blocking NTFS junctions/symlinks/reparse points, and redacting login emails from CLI/JSON output.
4. **Plain-Text Python Source Only (No Closed-Source `.exe` Installers)**: Official releases are distributed **strictly as plain-text auditable Python source code**. Never run untrusted third-party compiled `.exe` bundles claiming to be Acerola-Orion.
5. **3-Second Source Verification Command (PowerShell)**:

```powershell
# Inspect every network, subprocess, and sensitive keyword across the entire package:
Get-ChildItem .\acerola_orion -Recurse -Filter *.py | Select-String -Pattern "HTTPConnection|socket|subprocess|vscdb|Cookie|oauth"
```

---

## Architecture

1. **Orion-Telemetry (`acerola_orion/telemetry.py`)**
   - Win32 `CreateToolhelp32Snapshot` process-tree inspection + `127.0.0.1` local Connect-RPC read-only status queries (`GetUserStatus`, `GetLoadCodeAssist`, `RetrieveUserQuotaSummary`) for 7-decimal `gemini-5h`, `gemini-weekly`, `3p-5h`, and `3p-weekly` remaining quota, disambiguating `100.000%` untouched standby windows from active reset countdowns;
   - Lock-free `FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE` incremental `(file_id, offset)` log cursor separating main-turn `HTTP 429/503` throttling from auxiliary sidecar/title `429` noise while tracking sidecar monitor (`sir_flash_lite` / `severe_internal_risks_agy`) latency and sanitizing proxy URLs via `urlsplit`.
2. **Orion-CacheRadar & 200K 2x Pricing Alert (`acerola_orion/cache_radar.py`)**
   - **100% Passive Zero-Egress Prompt KV Cache Thermometer**: Reads only the trailing `16KB` of `transcript.jsonl` via lock-free shared handles when `(mtime_ns, size)` changes (with no stale negative missing-file cache) to compute live context volume, cache hit rate, and a calibrated **250s (4m10s) cache warmth countdown**;
   - **200K 2x Pricing Tier Early Warning**: Tracks approaching (`180K–200K`) and active (`>=200K`) 2x double-billing context thresholds with one-click `/compact` clipboard copy across the desktop console, draggable frameless floating capsule HUD (`smart` / `always` / `off`), and system tray menu; supports `smart_guard`, `latest`, and manual `pinned` targeting modes.
3. **Orion-Ledger & Local Rate Estimator (`acerola_orion/ledger.py`)**
   - Strictly privacy-isolated incremental SQLite indexer over `brain/<uuid>/.system_generated/logs/transcript.jsonl` and `annotations/<uuid>.pbtxt` with cross-process file locking, SHA-256 incremental prefix verification, and sanitized session titles;
   - Daily, per-model-family (`4C`, `4N` / **Gemini 4 Neon**, `4A`, `4H`, `Flash`, `Claude`, `GPT-OSS`), and per-session token tables comparing `1.0x` standard API USD, `0.5x` promotional USD, and `1.5x` local preset weight estimates (user-visible estimation only);
   - Pairs 7-decimal quota watermarks against matched local step deltas (including dedicated `p3_5h_drop_pct` tracking and even-count median aggregation) to highlight unmatched-step quota drops and estimate implied pool capacity.
4. **Orion-Vault — Append-Only Conversation Time-Vault (`acerola_orion/vault.py` & `acerola_orion/vault_hook.py`)**
   - **Local-Only Deletion Protection**: Incrementally backs up `conversations/<uuid>.db` (via SQLite Online Backup + gzip), `conversation_summaries.db` rows (including `raw_summary`), `annotations/<uuid>.pbtxt`, and text/SVG artifacts under `brain/<uuid>/` without ever mirroring client deletions;
   - When a conversation is deleted inside Antigravity, Orion-Vault preserves the backup and flags it as `deleted` for one-click restoration (`vault restore`) or Markdown export (`vault export`);
   - Controlled by a toggleable `vault_auto_backup` switch (`vault on/off/toggle`, tray menu, or panel tab) and enforces strict media-privacy isolation plus symlink/reparse-point blocking.
5. **Acerola-Guillotine (`acerola_orion/guillotine.py`)**
   - **Prevents Post-Exit Context Compaction, Memory Pollution & Silent Quota Drain**: When you close the Antigravity window, lingering orphan `language_server*.exe` processes can stay alive in the background inheriting your active frontier model (`Gemini 4 Carbon/Neon/Argon`, `Claude Opus`) to run unprompted context compaction, background summarization, and memory distillation;
   - **Event-Driven Win32 Kernel Handle Cleanup**: Anchors Win32 kernel `SYNCHRONIZE` handles (`WaitForMultipleObjects`) directly to the root Electron UI process without polling loops — never false-triggers on minimize/tray, and terminates only tracked `language_server*.exe` handles verified by same-user SID (`advapi32`, fail-closed), install directory, UI ancestry, and active hub upon real app exit; also hosts the toggleable Orion-Vault background sweep thread.
6. **Orion Astrolabe Desktop Console & System Tray (`acerola_orion/panel.py` & `acerola_orion/win_tray.py`)**
   - Native Win32 DWM immersive dark Obsidian/Crimson High-DPI dashboard featuring **Dual Orion Astrolabe Chronographs** (`5h` outer arc + `Weekly` inner orbital ring + **Golden Time-Pace Marker**), interactive 24h trajectory / 14-day spend vector charts, and a dedicated **Orion-Vault** tab (4th Full Mode tab) for searching, syncing, restoring, and exporting conversations;
   - One-click toggle between **Full Observatory Mode** and **Zen Focus Mode (`--zen`)** alongside close-to-tray residency that cleanly cancels background UI refresh timers while hidden.

---

## Quick Start

```powershell
git clone https://github.com/zanex-dev/acerola-orion.git
cd acerola-orion
pip install -e . --no-deps

# On-demand CLI & GUI (no background service required)
acerola-orion status
acerola-orion radar               # Passive Prompt KV cache 250s warmth & 200K 2x pricing radar
acerola-orion ledger --days 14
acerola-orion audit
acerola-orion panel               # Launch Orion Astrolabe desktop observatory
acerola-orion panel --zen         # Launch in Zen Focus Mode (1 Hero Metric + 1 Chart)
acerola-orion panel --minimized   # Start directly in the Windows system tray
acerola-orion shortcut            # Register Windows Start Menu & Taskbar-pinnable app shortcut

# Orion-Vault: local append-only conversation backup, switch & recovery
acerola-orion vault status
acerola-orion vault sync
acerola-orion vault on            # Enable auto-backup (or `off` / `toggle`)
acerola-orion vault list --deleted
acerola-orion vault search "keyword"
acerola-orion vault restore <uuid-prefix>
acerola-orion vault export <uuid-prefix>

# Optional: enable or cleanly remove logon autostart orphan-process guard
acerola-orion guard install
acerola-orion guard status
acerola-orion guard uninstall
```

### ⚔️ How the Guillotine Guard Works ("Enable Once, Forget Forever")

- **Independent Headless Daemon**: Enabling the guard once — either via `acerola-orion guard install` or by clicking **`🛡️ 自启守卫`** inside `acerola-orion panel` — starts a standalone event-driven kernel-wait background guard process that **stays active even if you completely exit the GUI dashboard** (and runs periodic Orion-Vault incremental sweeps when `vault_auto_backup` is enabled).
- **Silent Logon Autostart (Never Pops Up the Dashboard)**: Across PC reboots, Windows automatically starts **only** the headless `guard daemon` in the background — no console flash, no tray clutter, and **it never pops up the GUI panel unless you open `panel` yourself**.
- **Autostart Transparency & 1-Click Removal**: `guard install` writes a 3-line plain-text VBScript at `~/.gemini/acerola_orion/start_guard_silent.vbs` (launching `pythonw.exe -m acerola_orion guard daemon`) and registers a user-level Windows Logon Scheduled Task named `AcerolaOrionGuard` (no admin UAC required, zero registry or `app.asar` changes). Running `acerola-orion guard uninstall` (or clicking **`⏹️ 停用`**) stops the daemon and deletes both cleanly.

Run the verification & security self-audit suite (`42/42` tests):

```powershell
python -m unittest discover -s tests -v
```

---

## ⚠️ Disclaimer

1. Independent local observability, token accounting, and post-exit orphan-process cleanup utility — **not affiliated with, endorsed by, or sponsored by Google**.
2. Operates strictly on local text logs and `127.0.0.1` loopback read-only status IPC; never modifies `app.asar`, never breaks code signatures, never touches credentials, and never proxies or alters cloud traffic.
3. Provided "AS IS" under the MIT License at your own risk. The author assumes no responsibility for account restrictions arising from third-party OAuth token extractors, `2API` reverse proxies, modified client binaries, or shared/resold accounts.

## License

Released under the [MIT License](LICENSE).
