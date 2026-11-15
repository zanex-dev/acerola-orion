<p align="center">
  <img src="assets/hero-banner.png" width="100%" alt="Acerola-Orion — Local Quota Observatory · Token Ledger · Zero-Polling Exit Guard" />
</p>

<h1 align="center">Acerola-Orion (雅赛劳拉莉昂)</h1>

<p align="center">
  <strong>7-Decimal Live Quota Telemetry, Local Privacy-Isolated Token Ledger / Rate-Card Auditor (Orion), and Zero-Polling Win32 Kernel Orphan-Process Guard (Acerola) for Google Antigravity.</strong>
</p>

<p align="center">
  <a href="https://github.com/zanex-dev/acerola-orion/actions/workflows/ci.yml"><img src="https://img.shields.io/badge/tests-12%2F12%20passing-brightgreen" alt="Tests" /></a>
  <a href="pyproject.toml"><img src="https://img.shields.io/badge/version-v0.2.2-be123c" alt="Version v0.2.2" /></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-e11d48.svg" alt="License: MIT" /></a>
  <a href="pyproject.toml"><img src="https://img.shields.io/badge/python-3.10%2B%20(0%20deps)-3776AB.svg" alt="Python 3.10+" /></a>
  <img src="https://img.shields.io/badge/network-127.0.0.1%20loopback%20only-059669" alt="Loopback Only" />
</p>

<p align="center">
  <a href="README.md"><strong>English</strong></a> · <a href="README_ZH.md"><strong>简体中文</strong></a>
</p>

---

## 🔒 Zero-Trust Security Self-Verification (Audit in 3 Seconds)

Built from day one with strict local physical boundaries so anyone can inspect and verify the entire codebase in seconds:

1. **Zero Third-Party Dependencies (`dependencies = []`)**: 100% Python >= 3.10 standard library + native Win32 `ctypes` — zero external `pip` supply-chain risk.
2. **Zero External Network Egress (`127.0.0.1` Only)**: The sole HTTP client (`ConnectRpcClient`) enforces `host == "127.0.0.1"` in `__init__` and performs read-only status queries against the running local official language server. **Zero bytes are ever sent to Google or any external server.**
3. **Zero Credential or Prompt Access**:
   - Never requests OAuth authorization, and **never** reads `state.vscdb`, Windows DPAPI/Keychain, browser cookies, or tokens;
   - Indexes token integer counters and session titles from local `transcript.jsonl` / `.pbtxt` only — never stores prompt/response bodies, skips all media/attachments, blocks NTFS junctions/symlinks, and redacts account emails from CLI/JSON output.
4. **3-Second Source Verification Command (PowerShell)**:

```powershell
# Inspect every network, subprocess, and sensitive keyword across the entire package:
Select-String -Path .\acerola_orion\*.py -Pattern "HTTPConnection|socket|subprocess|vscdb|Cookie|oauth"
```

---

## Architecture

1. **Orion-Telemetry (`acerola_orion/telemetry.py`)**
   - Sub-millisecond Win32 `CreateToolhelp32Snapshot` process-tree inspection + `127.0.0.1` local Connect-RPC read-only status queries (`GetUserStatus`, `GetLoadCodeAssist`, `RetrieveUserQuotaSummary`) for 7-decimal `gemini-5h`, `gemini-weekly`, `3p-5h`, and `3p-weekly` remaining quota & reset countdowns;
   - Lock-free `FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE` incremental `(file_id, offset)` log cursor tracking `HTTP 429/503` throttling counts and sidecar monitor (`sir_flash_lite` / `severe_internal_risks_agy`) latency.
2. **Orion-Ledger & Rate Auditor (`acerola_orion/ledger.py`)**
   - Strictly privacy-isolated incremental SQLite indexer over `brain/<uuid>/.system_generated/logs/transcript.jsonl` and `annotations/<uuid>.pbtxt`;
   - Daily, per-model-family (`4C`, `4A`, `4H`, `Flash`, `Claude`, `GPT-OSS`), and per-session token tables comparing `1.0x` standard API USD, `0.5x` promotional USD, and `1.5x` internal weight estimates;
   - Pairs 7-decimal quota watermarks against local token deltas to detect zero-chat offline **Phantom Drains** and estimate implied pool capacity.
3. **Acerola-Guillotine (`acerola_orion/guillotine.py`)**
   - Zero-polling (`0.00%` CPU) Win32 kernel `SYNCHRONIZE` handle guard (`WaitForMultipleObjects`) anchored to the root Electron UI process — never false-triggers on window minimize/tray, and terminates lingering orphan `language_server*.exe` processes in `<1ms` upon real app exit (with same-handle executable image verification) to prevent unwanted post-close background summarization token drain.
4. **Desktop Console (`acerola_orion/panel.py`)**
   - Native Obsidian/Crimson High-DPI `tkinter` dashboard with live quota gauges, token ledger tables, sidecar diagnostics, and one-click guard controls.

---

## Quick Start

```powershell
git clone https://github.com/zanex-dev/acerola-orion.git
cd acerola-orion
pip install -e . --no-deps

# On-demand CLI & GUI (no background service required)
acerola-orion status
acerola-orion ledger --days 14
acerola-orion audit
acerola-orion panel

# Optional: enable or cleanly remove logon autostart orphan-process guard
acerola-orion guard install
acerola-orion guard uninstall
```

> **Autostart Transparency**: `acerola-orion guard install` writes a 3-line plain-text VBScript at `~/.gemini/acerola_orion/start_guard_silent.vbs` (launching `pythonw.exe -m acerola_orion guard daemon` without a console flash) and registers a user-level Windows Logon Scheduled Task named `AcerolaOrionGuard` (no admin UAC required, zero registry or `app.asar` changes). `acerola-orion guard uninstall` stops the daemon and deletes both cleanly.

Run the verification & security self-audit suite:

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
