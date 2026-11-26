# -*- coding: utf-8 -*-
"""
Acerola-Orion v2.0.0 Public Edition Verification & Security Self-Audit Suite
Runs entirely inside isolated TemporaryDirectory() with zero external network or host side effects.
"""

from __future__ import annotations

from contextlib import closing, redirect_stdout
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler
import io
import json
import math
import os
from pathlib import Path
import shutil
import socket
import socketserver
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
from typing import Any, List
import unittest
from unittest.mock import Mock, patch

import acerola_orion.__main__ as cli_main
import acerola_orion.cache_radar as cache_radar
import acerola_orion.cli_render as cli_render
import acerola_orion.constants as constants
from acerola_orion.core.kernel import DISABLE_PLUGINS_ENV, reset_kernel_for_tests
import acerola_orion.guillotine as guillotine
import acerola_orion.ledger as ledger_mod
import acerola_orion.panel as panel_mod
import acerola_orion.rpc as rpc
import acerola_orion.telemetry as telemetry
import acerola_orion.vault as vault_mod
import acerola_orion.vault_hook as vault_hook_mod


class _MockLocalLsHandler(BaseHTTPRequestHandler):
    csrf_expected = "pub_test_csrf_tok"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return

    def do_POST(self) -> None:
        clen = int(self.headers.get("Content-Length") or "0")
        if clen > 0:
            self.rfile.read(clen)
        tok = self.headers.get("x-codeium-csrf-token", "")
        if tok != self.csrf_expected:
            self.send_response(403)
            self.end_headers()
            self.wfile.write(b'{"error":"bad csrf"}')
            return
        if self.path.endswith("/GetUserStatus"):
            resp: Any = {
                "userStatus": {
                    "email": "should_not_leak@example.com",
                    "name": "DevUser",
                    "userTier": {"id": "g1-pro-tier", "name": "Google AI Pro"},
                    "planStatus": {"planInfo": {"teamsTier": "TEAMS_TIER_PRO"}},
                }
            }
        elif self.path.endswith("/GetLoadCodeAssist"):
            resp = {
                "response": {
                    "paidTier": {"id": "g1-ultra-tier", "name": "Google AI Ultra"},
                    "currentTier": {"id": "free-tier"},
                    "gcpManaged": False,
                }
            }
        elif self.path.endswith("/RetrieveUserQuotaSummary"):
            resp = {
                "response": {
                    "groups": [
                        {
                            "displayName": "Gemini Pool",
                            "buckets": [
                                {
                                    "bucketId": "gemini-5h",
                                    "displayName": "5h",
                                    "window": "18000s",
                                    "remainingFraction": 0.8765432,
                                    "resetTime": "2030-01-01T00:00:00Z",
                                },
                                {
                                    "bucketId": "gemini-weekly",
                                    "displayName": "Weekly",
                                    "window": "604800s",
                                    "remainingFraction": 0.654321,
                                    "resetTime": "2030-01-05T00:00:00Z",
                                },
                            ],
                        }
                    ]
                }
            }
        else:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b'{"error":"unknown method"}')
            return

        payload = json.dumps(resp).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class AcerolaOrionPublicSuite(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="orion_pub_")
        self.tmp_path = Path(self._tmp.name)
        self._old_home = os.environ.get("ACEROLA_ORION_HOME")
        self._old_disable_plugins = os.environ.get(DISABLE_PLUGINS_ENV)
        os.environ["ACEROLA_ORION_HOME"] = str(self.tmp_path / "orion_home")
        os.environ[DISABLE_PLUGINS_ENV] = "1"
        reset_kernel_for_tests()
        telemetry.reset_log_cursor_cache()

    def tearDown(self) -> None:
        reset_kernel_for_tests()
        telemetry.reset_log_cursor_cache()
        if self._old_disable_plugins is None:
            os.environ.pop(DISABLE_PLUGINS_ENV, None)
        else:
            os.environ[DISABLE_PLUGINS_ENV] = self._old_disable_plugins
        if self._old_home is None:
            os.environ.pop("ACEROLA_ORION_HOME", None)
        else:
            os.environ["ACEROLA_ORION_HOME"] = self._old_home
        self._tmp.cleanup()

    def test_01_static_security_zero_egress_and_clean_room_audit(self) -> None:
        """Verify zero external HTTP deps, strict 127.0.0.1 loopback, zero credential access, and clean surface."""
        repo_root = Path(__file__).resolve().parent.parent
        py_files = sorted(
            p
            for p in (repo_root / "acerola_orion").rglob("*.py")
            if "_versions" not in p.parts and "__pycache__" not in p.parts
        )
        self.assertGreaterEqual(len(py_files), 15)
        py_names = {p.name for p in py_files}
        self.assertIn("vault.py", py_names)
        self.assertIn("vault_hook.py", py_names)
        self.assertIn("kernel.py", py_names)
        self.assertIn("plugin_host.py", py_names)
        self.assertNotIn("mux.py", py_names)
        self.assertNotIn("scal" + "pel.py", py_names)

        forbidden_code_tokens = [
            "urllib." + "request",
            "import " + "requests",
            "import " + "httpx",
            "import " + "aiohttp",
            "state." + "vscdb",
            "CryptUnprotect" + "Data",
            "refresh_" + "token",
        ]
        clean_room_tokens = [
            "scal" + "pel",
            "SetCloud" + "CodeURL",
            "set_cloud_" + "code_url",
            "cloudcode" + "-pa",
            "shift_" + "endpoint",
            "smart_" + "reroute",
            "lock_" + "endpoint",
            "UseAICode" + "Mendel",
            "Response" + "ID:",
            "ROUND1_" + "STATE",
            "ROUND2_" + "STATE",
            "ROUND3_" + "STATE",
            "orion-" + "mux",
            "probe_active_" + "mux",
            "edge_" + "pin",
            "KEEPALIVE_" + "PROMPT",
            "account_" + "email",
            "companion_" + "project",
            "model_pool_" + "quotas",
            "orion_" + "private",
        ]

        scan_targets = list(py_files) + [
            repo_root / "pyproject.toml",
            repo_root / "assets" / "logo.svg",
            repo_root / "AGENTS.md",
            repo_root / "README.md",
            repo_root / "README_ZH.md",
            repo_root / "CHANGELOG.md",
            repo_root / "CHANGELOG_ZH.md",
        ]
        for p in scan_targets:
            if not p.is_file():
                continue
            txt = p.read_text(encoding="utf-8")
            low = txt.lower()
            if p.suffix == ".py":
                for bad in forbidden_code_tokens:
                    self.assertNotIn(bad.lower(), low, f"Forbidden token {bad!r} in {p.name}")
            for clue in clean_room_tokens:
                self.assertNotIn(clue.lower(), low, f"Unexpected token {clue!r} in {p.name}")

        # Runtime enforcement: ConnectRpcClient must reject any non-127.0.0.1 host
        with self.assertRaises(ValueError):
            rpc.ConnectRpcClient(http_port=8080, csrf_token="tok", host="localhost")
        with self.assertRaises(ValueError):
            rpc.ConnectRpcClient(http_port=8080, csrf_token="tok", host="example.com")

    def test_02_version_and_model_pricing_math(self) -> None:
        self.assertEqual(constants.APP_VERSION, "2.1.0")
        c4 = constants.classify_model("Gemini 4 Carbon (High)")
        self.assertEqual(c4.family_id, "4c")
        self.assertAlmostEqual(c4.compute_usd(1_000_000, 0, 100_000, 1.0), 6.0)
        self.assertAlmostEqual(c4.compute_usd(1_000_000, 0, 100_000, c4.promised_multiplier), 3.0)
        self.assertAlmostEqual(c4.compute_usd(1_000_000, 0, 100_000, c4.suspected_agy_multiplier), 9.0)
        n4 = constants.classify_model("Gemini 4 Neon (High)")
        self.assertEqual(n4.family_id, "4n")
        self.assertEqual(n4.quota_group, "gemini")
        self.assertAlmostEqual(n4.compute_usd(1_000_000, 0, 100_000, 1.0), 4.5)
        self.assertEqual(constants.classify_model("Claude Opus 5.5").quota_group, "3p")

    def test_03_process_tree_deadlock_exclusion_and_cycle_recovery(self) -> None:
        # UI Main (9744) -> LS (20468) -> cmd.exe (20148) -> headless worker Antigravity.exe (9832)
        live_procs = [
            rpc.RawProcEntry(pid=9744, ppid=500, name="Antigravity.exe"),
            rpc.RawProcEntry(pid=10100, ppid=9744, name="Antigravity.exe"),
            rpc.RawProcEntry(pid=20468, ppid=9744, name="language_server_windows_x64.exe"),
            rpc.RawProcEntry(pid=20148, ppid=20468, name="cmd.exe"),
            rpc.RawProcEntry(pid=9832, ppid=20148, name="Antigravity.exe"),
        ]
        t_live = rpc.find_antigravity_process_tree(live_procs)
        self.assertEqual(t_live["main_pids"], [9744])
        self.assertEqual(t_live["ls_worker_pids"], [9832])
        self.assertEqual(t_live["orphan_ls_pids"], [])

        # UI exits -> only LS + worker remain -> main_pids=[] so guillotine fires
        after_exit = live_procs[2:]
        t_exit = rpc.find_antigravity_process_tree(after_exit)
        self.assertEqual(t_exit["main_pids"], [])
        self.assertEqual(t_exit["orphan_ls_pids"], [20468])

        # Synthetic 2-node PID-reuse cycle on live UI processes must NEVER yield main_pids=[]
        cyc_procs = [
            rpc.RawProcEntry(pid=100, ppid=200, name="Antigravity.exe"),
            rpc.RawProcEntry(pid=200, ppid=100, name="Antigravity.exe"),
            rpc.RawProcEntry(pid=300, ppid=100, name="language_server_windows_x64.exe"),
        ]
        t_cyc = rpc.find_antigravity_process_tree(cyc_procs)
        self.assertEqual(t_cyc["main_pids"], [100])

    @unittest.skipUnless(sys.platform == "win32", "Windows handle & mutex tests")
    def test_04_win32_handle_verified_kill_and_mutex(self) -> None:
        # Must refuse to terminate current process or mismatched executable prefix
        self.assertFalse(rpc.terminate_pid_win32(os.getpid()))
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(10)"], creationflags=creationflags)
        try:
            self.assertFalse(rpc.terminate_pid_win32(p.pid, allowed_exe_prefixes=("language_server",)))
            self.assertIsNone(p.poll())
            self.assertTrue(rpc.terminate_pid_win32(p.pid, allowed_exe_prefixes=("python",)))
            p.wait(timeout=3.0)
        finally:
            if p.poll() is None:
                p.kill()
                p.wait(timeout=3.0)

        m_name = rf"Local\OrionPubTestMutex_{os.getpid()}_{time.time_ns()}"
        h1, ex1 = rpc.acquire_single_instance_mutex(m_name)
        self.assertIsNotNone(h1)
        self.assertFalse(ex1)
        try:
            h2, ex2 = rpc.acquire_single_instance_mutex(m_name)
            self.assertIsNone(h2)
            self.assertTrue(ex2)
        finally:
            rpc.release_and_close_mutex(h1)

    def test_05_cmdline_quoted_flags_and_hub_discovery(self) -> None:
        parsed = rpc.parse_ls_cmdline(
            '"C:\\Program Files\\Antigravity\\language_server.exe" --override_ide_version="2.32.1" --csrf_token=\'tok_abc\''
        )
        self.assertEqual(parsed["ide_version"], "2.32.1")
        self.assertEqual(parsed["csrf_token"], "tok_abc")

        d_dir = self.tmp_path / "daemon"
        d_dir.mkdir()
        (d_dir / "hub_111.json").write_text(
            json.dumps({"pid": 111, "httpPort": 50001, "httpsPort": 0, "csrfToken": "dead_tok"}),
            encoding="utf-8",
        )
        live_hub = d_dir / "hub_222.json"
        live_hub.write_text(
            json.dumps({"pid": 222, "httpPort": 50002, "httpsPort": 50003, "csrfToken": "live_tok"}),
            encoding="utf-8",
        )
        ep = rpc.discover_hub_endpoint(0, daemon_dir=d_dir, live_pids={222})
        self.assertIsNotNone(ep)
        assert ep is not None
        self.assertEqual(ep.pid, 222)
        removed = rpc.clean_stale_hub_files(daemon_dir=d_dir, preserve_pids={222})
        self.assertEqual(removed, 1)
        self.assertTrue(live_hub.exists())

    def test_06_shared_file_tail_utf8_and_incremental_log_cursor(self) -> None:
        ls_log = self.tmp_path / "language_server.log"
        main_log = self.tmp_path / "main.log"
        ls_log.write_text(
            "I1115 10:00:01 file.go:429] normal info line with 429 line number\n"
            "I1115 10:01:05 runner.go:8] Monitor sir_flash_lite FINISHED: triggered=false, duration=1m500ms\n"
            "E1115 10:01:09 client.go:1] HTTP/2 429 Too Many Requests\n"
            "I1115 10:01:10 partial_line_without_nl",
            encoding="utf-8",
        )
        main_log.write_text("I1115 net: ERR_NETWORK_CHANGED\n", encoding="utf-8")

        self.assertEqual(rpc.read_shared_file_lines(ls_log, max_tail_bytes=0), [])
        self.assertEqual(rpc.read_shared_file_lines(ls_log, max_tail_bytes=-5), [])

        def assert_inc_eq_full() -> telemetry.LogCongestionStats:
            inc = telemetry.analyze_logs_incremental(ls_log, main_log)
            full = telemetry.analyze_logs(
                rpc.read_shared_file_lines(ls_log),
                rpc.read_shared_file_lines(main_log),
            )
            self.assertEqual(inc, full)
            return inc

        st1 = assert_inc_eq_full()
        self.assertEqual(st1.http_429_503_count, 1)  # file.go:429 must NOT false-trigger!
        self.assertAlmostEqual(st1.sidecar.avg_ms, 60500.0, places=1)
        self.assertEqual(st1.network_changed_count, 1)

        # Unchanged file delta -> zero-byte read
        fid, off, _chunk, reset = rpc.read_shared_file_delta(ls_log)
        self.assertTrue(reset)
        fid2, off2, chunk2, reset2 = rpc.read_shared_file_delta(ls_log, prev_file_id=fid, prev_offset=off)
        self.assertEqual((fid2, off2, chunk2, reset2), (fid, off, b"", False))

        # Append & rotate
        with open(ls_log, "a", encoding="utf-8") as f:
            f.write(" completed\nE1115 10:02:00 x.go:1] MODEL_CAPACITY_EXHAUSTED\n")
        st2 = assert_inc_eq_full()
        self.assertEqual(st2.capacity_exhausted_count, 1)

        ls_log.write_text("E1115 11:00:00 x.go:1] status=503\n", encoding="utf-8")
        main_log.write_text("", encoding="utf-8")
        st3 = assert_inc_eq_full()
        self.assertEqual(st3.http_429_503_count, 1)
        self.assertEqual(st3.capacity_exhausted_count, 0)

    def test_06b_daily_log_scope_midnight_reset_and_timeout_dedup(self) -> None:
        ls_log = self.tmp_path / "daily_language_server.log"
        main_log = self.tmp_path / "daily_main.log"
        telemetry.reset_log_cursor_cache()

        # 1) Cross-day filtering (including glog prefix + continuation line inheritance)
        ls_log.write_text(
            "I1124 23:58:00 runner.go:8] Monitor sir_flash_lite FINISHED: triggered=false, duration=250ms\n"
            "ERROR: logging before google.Init: E1124 23:59:00 client.go:1] HTTP/2 429 Too Many Requests\n"
            "  continuation line without timestamp HTTP/2 503 Service Unavailable\n"
            "E1124 23:59:30 mon.go:42] LLM_MONITOR_LLM_CALL_TIMEOUT: old timeout\n"
            "I1125 00:01:00 runner.go:8] Monitor sir_flash_lite FINISHED: triggered=false, duration=400ms\n"
            "ERROR: logging before google.Init: E1125 00:02:00 client.go:1] HTTP/2 429 Too Many Requests\n"
            "  continuation line on active date status=503\n",
            encoding="utf-8",
        )
        main_log.write_text(
            "[2026-11-24 23:59:50.100] [main] ERR_NETWORK_CHANGED\n"
            "  continuation yesterday ERR_NETWORK_CHANGED\n"
            "[2026-11-25 00:00:10.200] [main] ERR_NETWORK_CHANGED\n"
            "  continuation today ERR_NETWORK_CHANGED\n",
            encoding="utf-8",
        )

        st_today_inc = telemetry.analyze_logs_incremental(ls_log, main_log, active_date="2026-11-25")
        st_today_full = telemetry.analyze_logs(
            rpc.read_shared_file_lines(ls_log),
            rpc.read_shared_file_lines(main_log),
            active_date="2026-11-25",
        )
        self.assertEqual(st_today_inc, st_today_full)
        self.assertEqual(st_today_inc.sidecar.eval_count, 1)
        self.assertAlmostEqual(st_today_inc.sidecar.avg_ms, 400.0, places=1)
        self.assertEqual(st_today_inc.sidecar.timeout_count, 0)
        self.assertEqual(st_today_inc.http_429_503_count, 2)
        self.assertEqual(st_today_inc.network_changed_count, 2)

        # active_date=None preserves all historical lines
        st_all = telemetry.analyze_logs(
            rpc.read_shared_file_lines(ls_log),
            rpc.read_shared_file_lines(main_log),
        )
        self.assertEqual(st_all.sidecar.eval_count, 2)
        self.assertEqual(st_all.sidecar.timeout_count, 1)
        self.assertEqual(st_all.http_429_503_count, 4)
        self.assertEqual(st_all.network_changed_count, 4)

        # 2) Midnight cursor auto-reset when active_date rolls to next day (even without file growth)
        st_next_zero = telemetry.analyze_logs_incremental(ls_log, main_log, active_date="2026-11-26")
        self.assertEqual(st_next_zero.sidecar.eval_count, 0)
        self.assertEqual(st_next_zero.http_429_503_count, 0)
        self.assertEqual(st_next_zero.network_changed_count, 0)

        with open(ls_log, "a", encoding="utf-8") as f:
            f.write("I1126 00:00:05 runner.go:8] Monitor severe_internal_risks_agy FINISHED: duration=120ms\n")
        with open(main_log, "a", encoding="utf-8") as f:
            f.write("[2026-11-26 00:00:06.000] [main] ERR_NETWORK_CHANGED\n")
        st_next_app = telemetry.analyze_logs_incremental(ls_log, main_log, active_date="2026-11-26")
        self.assertEqual(st_next_app.sidecar.eval_count, 1)
        self.assertAlmostEqual(st_next_app.sidecar.avg_ms, 120.0, places=1)
        self.assertEqual(st_next_app.network_changed_count, 1)

        # Rotation/truncation mid-day preserves active_date filtering
        ls_log.write_text(
            "I1125 23:59:59 client.go:1] HTTP/2 429 Too Many Requests\n"
            "I1126 00:05:00 client.go:1] status=503\n",
            encoding="utf-8",
        )
        st_rot = telemetry.analyze_logs_incremental(ls_log, main_log, active_date="2026-11-26")
        self.assertEqual(st_rot.http_429_503_count, 1)

        # 3) 10s sidecar timeout deduplication (forward/reversed <=4 lines, distance >4, standalone, cross-chunk)
        # Forward pair (distance=1) + reversed pair (distance=4) + non-paired (distance=5) + standalone
        dedup_lines = [
            "E1126 01:00:00 mon.go:10] Monitor sir_flash_lite FINISHED: Failed: context deadline exceeded, duration=10.01s",
            "E1126 01:00:00 mon.go:11] LLM_MONITOR_LLM_CALL_TIMEOUT: sir_flash_lite exceeded 10s",
            "I1126 01:00:10 info.go:1] noise",
            "E1126 01:01:00 mon.go:11] LLM_MONITOR_LLM_CALL_TIMEOUT: stage2 exceeded 10s",
            "I1126 01:01:00 info.go:2] noise 1",
            "I1126 01:01:00 info.go:3] noise 2",
            "I1126 01:01:00 info.go:4] noise 3",
            "E1126 01:01:00 mon.go:10] Monitor severe_internal_risks_agy FINISHED: Failed: context deadline exceeded, duration=10.02s",
            "I1126 01:02:00 info.go:5] gap",
            "E1126 01:03:00 mon.go:10] Monitor sir_flash_lite FINISHED: Failed: context deadline exceeded, duration=10.03s",
            "I1126 01:03:00 info.go:6] noise 1",
            "I1126 01:03:00 info.go:7] noise 2",
            "I1126 01:03:00 info.go:8] noise 3",
            "I1126 01:03:00 info.go:9] noise 4",
            "E1126 01:03:01 mon.go:11] LLM_MONITOR_LLM_CALL_TIMEOUT: separate timeout (distance=5)",
            'E1126 01:03:02 clearcut.go:1] Post "https://play.googleapis.com/log": context deadline exceeded',
        ]
        st_dedup = telemetry.analyze_logs(dedup_lines, [], active_date="2026-11-26")
        self.assertEqual(st_dedup.sidecar.timeout_count, 4)  # 1 (fwd) + 1 (rev d=4) + 2 (d=5 not paired)
        self.assertEqual(st_dedup.telemetry_timeout_count, 1)

        # Standalone single lines remain 1 each
        st_single_a = telemetry.analyze_logs(
            ["E1126 02:00:00 mon.go:10] Monitor sir_flash_lite FINISHED: Failed: context deadline exceeded, duration=10s"],
            [],
            active_date="2026-11-26",
        )
        st_single_b = telemetry.analyze_logs(
            ["E1126 02:00:00 mon.go:11] LLM_MONITOR_LLM_CALL_TIMEOUT: single"],
            [],
            active_date="2026-11-26",
        )
        self.assertEqual(st_single_a.sidecar.timeout_count, 1)
        self.assertEqual(st_single_b.sidecar.timeout_count, 1)

        # Cross-chunk incremental pairing
        pair_log = self.tmp_path / "pair_ls.log"
        empty_main = self.tmp_path / "pair_main.log"
        empty_main.write_text("", encoding="utf-8")
        pair_log.write_text(
            "E1126 03:00:00 mon.go:11] LLM_MONITOR_LLM_CALL_TIMEOUT: chunk1\n",
            encoding="utf-8",
        )
        st_c1 = telemetry.analyze_logs_incremental(pair_log, empty_main, active_date="2026-11-26")
        self.assertEqual(st_c1.sidecar.timeout_count, 1)
        with open(pair_log, "a", encoding="utf-8") as f:
            f.write(
                "I1126 03:00:00 info.go:1] noise\n"
                "E1126 03:00:00 mon.go:10] Monitor sir_flash_lite FINISHED: Failed: context deadline exceeded, duration=10s\n"
            )
        st_c2 = telemetry.analyze_logs_incremental(pair_log, empty_main, active_date="2026-11-26")
        self.assertEqual(st_c2.sidecar.timeout_count, 1)
        self.assertEqual(
            st_c2,
            telemetry.analyze_logs(rpc.read_shared_file_lines(pair_log), [], active_date="2026-11-26"),
        )

    def test_07_rpc_deadline_slow_drip_connection_close_and_nonfinite(self) -> None:
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(4)
        port = srv.getsockname()[1]
        stop_ev = threading.Event()
        mode = {"kind": "close_stall"}

        def accept_loop() -> None:
            while not stop_ev.is_set():
                srv.settimeout(0.2)
                try:
                    c, _ = srv.accept()
                except socket.timeout:
                    continue
                except OSError:
                    break

                def handle(sock: socket.socket) -> None:
                    try:
                        sock.recv(4096)
                        if mode["kind"] == "close_stall":
                            sock.sendall(
                                b"HTTP/1.1 200 OK\r\nConnection: close\r\nContent-Length: 50\r\n\r\n"
                            )
                            stop_ev.wait(0.3)
                            try:
                                sock.sendall(b"{")
                            except OSError:
                                pass
                            stop_ev.wait(3.0)
                        elif mode["kind"] == "nan":
                            body = b'{"x": NaN}'
                            sock.sendall(
                                f"HTTP/1.1 200 OK\r\nContent-Length: {len(body)}\r\n\r\n".encode("ascii") + body
                            )
                            try:
                                sock.shutdown(socket.SHUT_WR)
                            except OSError:
                                pass
                            stop_ev.wait(0.2)
                    except OSError:
                        pass
                    finally:
                        try:
                            sock.close()
                        except OSError:
                            pass

                threading.Thread(target=handle, args=(c,), daemon=True).start()

        threading.Thread(target=accept_loop, daemon=True).start()
        try:
            cli = rpc.ConnectRpcClient(http_port=port, csrf_token="tok")
            t0 = time.monotonic()
            with self.assertRaises(rpc.ConnectRpcError):
                cli.call("GetUserStatus", {}, timeout=0.6)
            self.assertLess(time.monotonic() - t0, 2.0)

            mode["kind"] = "nan"
            with self.assertRaises(rpc.ConnectRpcError):
                cli.call("GetUserStatus", {}, timeout=1.0)
        finally:
            stop_ev.set()
            srv.close()

    def test_08_mock_rpc_telemetry_and_cli_json(self) -> None:
        ls_log = self.tmp_path / "language_server.log"
        main_log = self.tmp_path / "main.log"
        settings = self.tmp_path / "settings.json"
        ls_log.write_text("", encoding="utf-8")
        main_log.write_text("", encoding="utf-8")
        settings.write_text('{"http.proxy": "http://user:secret@127.0.0.1:7890"}', encoding="utf-8")

        with socketserver.TCPServer(("127.0.0.1", 0), _MockLocalLsHandler) as httpd:
            port = httpd.server_address[1]
            threading.Thread(target=httpd.serve_forever, daemon=True).start()
            try:
                mock_ls = rpc.ActiveLsState(
                    running=True,
                    pid=42424,
                    main_pids=[40000],
                    all_ls_pids=[42424],
                    ide_version="2.32.1",
                    hub=rpc.HubEndpoint(
                        file_path=str(self.tmp_path / "hub_42424.json"),
                        pid=42424,
                        http_port=port,
                        https_port=0,
                        csrf_token=_MockLocalLsHandler.csrf_expected,
                        total_hub_files=1,
                    ),
                )
                with (
                    patch.object(telemetry, "inspect_active_ls", return_value=mock_ls),
                    patch.object(telemetry, "get_ls_log_path", return_value=ls_log),
                    patch.object(telemetry, "get_main_log_path", return_value=main_log),
                    patch.object(telemetry, "get_user_settings_path", return_value=settings),
                    patch.object(telemetry, "probe_guard_status", return_value=telemetry.GuardStatusView()),
                ):
                    buf = io.StringIO()
                    with redirect_stdout(buf):
                        rc = cli_main.main(["status", "--json", "--no-record"])
                    self.assertEqual(rc, 0)
                    raw_out = buf.getvalue()
                    # Email from GetUserStatus must NOT be persisted or printed in public edition!
                    self.assertNotIn("should_not_leak@example.com", raw_out)
                    # Proxy credentials must be masked!
                    self.assertNotIn("secret", raw_out)
                    parsed = json.loads(raw_out)
                    self.assertEqual(parsed["paid_tier_id"], "g1-ultra-tier")
                    self.assertEqual(len(parsed["quota_buckets"]), 2)
            finally:
                httpd.shutdown()

    def test_09_ledger_privacy_containment_pbtxt_cache_and_pruning(self) -> None:
        brain = self.tmp_path / "brain"
        ann = self.tmp_path / "ann"
        cid = "11111111-1111-1111-1111-111111111111"
        conv_dir = brain / cid
        (conv_dir / ".system_generated" / "logs").mkdir(parents=True)
        (conv_dir / ".user_uploaded").mkdir(parents=True)
        (conv_dir / ".user_uploaded" / "secret.png").write_bytes(b"NO_READ")
        ann.mkdir(parents=True)
        (ann / f"{cid}.pbtxt").write_text('subtitle: "Ignore"\ntitle: "\\351\\233\\205\\350\\265\\233"\n', encoding="utf-8")

        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        (conv_dir / ".system_generated" / "logs" / "transcript.jsonl").write_text(
            json.dumps({"step_index": 0, "type": "USER_INPUT", "content": "Model Selection` from X to Gemini 4 Carbon (High)."}) + "\n"
            + json.dumps({"step_index": 1, "type": "PLANNER_RESPONSE", "created_at": now_iso, "input_tokens": 1000, "cache_read_input_tokens": 500, "output_tokens": 200}) + "\n",
            encoding="utf-8",
        )

        led = ledger_mod.OrionLedger(db_path=self.tmp_path / "led.db", brain_dir=brain, annotations_dir=ann)
        sessions = led.get_recent_sessions(limit=10)
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0].title, "雅赛")
        self.assertEqual(sessions[0].output_tokens, 200)

        # Delete conversation directory -> pruning removes stale rows
        import shutil

        shutil.rmtree(conv_dir)
        led.sync_transcripts()
        self.assertEqual(led.get_recent_sessions(limit=10), [])

    def test_10_clock_skew_retention_and_null_gap_phantom_drain(self) -> None:
        led = ledger_mod.OrionLedger(
            db_path=self.tmp_path / "skew.db",
            brain_dir=self.tmp_path / "no_brain",
            annotations_dir=self.tmp_path / "no_ann",
        )
        now = time.time()
        with led._connect(immediate=True) as c:
            for i in range(40):
                c.execute(
                    "INSERT INTO quota_snapshots (ts_utc, ts_epoch, event_type, gemini_5h, gemini_5h_reset) VALUES (?,?,?,?,?)",
                    ("hist", now - 5 * 86400 + i * 7200, "guard_heartbeat", 0.95 - i * 0.001, "R0"),
                )
            # Future clock-skew row (+400d) must NOT wipe real history
            c.execute(
                "INSERT INTO quota_snapshots (ts_utc, ts_epoch, event_type, gemini_5h, gemini_5h_reset) VALUES (?,?,?,?,?)",
                ("future", now + 400 * 86400, "panel_poll", 0.5, "RF"),
            )
            # Real 6% drop across a NULL gap
            c.execute(
                "INSERT INTO quota_snapshots (ts_utc, ts_epoch, event_type, gemini_5h, gemini_5h_reset) VALUES (?,?,?,?,?)",
                ("s1", now - 120.0, "panel_poll", 0.90, "R1"),
            )
            c.execute(
                "INSERT INTO quota_snapshots (ts_utc, ts_epoch, event_type, gemini_5h, gemini_5h_reset) VALUES (?,?,?,?,?)",
                ("s2", now - 60.0, "panel_poll", None, ""),
            )
            c.execute(
                "INSERT INTO quota_snapshots (ts_utc, ts_epoch, event_type, gemini_5h, gemini_5h_reset) VALUES (?,?,?,?,?)",
                ("s3", now - 10.0, "panel_poll", 0.84, "R1"),
            )

        led._last_retention_mono = None
        rep = telemetry.OrionTelemetryReport(
            captured_at="now",
            ls_running=True,
            ls_pid=1,
            main_pids=[1],
            quota_buckets=[telemetry.QuotaBucketView("G", "gemini-5h", "5h", "5h", 0.84, 84.0, "R1", "1h")],
        )
        led.record_quota_from_telemetry(rep, event_type="probe")
        with led._connect() as c:
            hist_left = c.execute("SELECT COUNT(*) FROM quota_snapshots WHERE ts_utc='hist'").fetchone()[0]
        self.assertEqual(hist_left, 40)

        audit = led.compute_rate_audit()
        self.assertNotEqual(audit.latest_snapshot_ts, "future")
        self.assertGreaterEqual(len(audit.phantom_drains), 1)
        self.assertAlmostEqual(audit.phantom_drains[-1].gemini_5h_drop_pct, 6.0, places=2)

    @unittest.skipUnless(sys.platform == "win32", "Windows VBScript & daemon watchdog test")
    def test_11_guillotine_vbs_syntax_and_hung_telemetry_watchdog(self) -> None:
        vbs_path = self.tmp_path / "路径测试_guard.vbs"
        vbs_body = guillotine._build_guard_launcher_vbs(r"C:\测试 路径\pythonw.exe", str(self.tmp_path))
        # Compile-only check by replacing ws.Run with WScript.Quit 0
        check_vbs = vbs_body.splitlines()[0] + "\r\n" + vbs_body.splitlines()[1] + "\r\nWScript.Quit 0\r\n"
        constants.atomic_write_text(vbs_path, check_vbs, encoding="utf-16")
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        r = subprocess.run(
            ["cscript.exe", "//NoLogo", "//B", str(vbs_path)],
            capture_output=True,
            timeout=5.0,
            creationflags=creationflags,
        )
        self.assertEqual(r.returncode, 0)

        # Hung background telemetry watchdog recovery (event-synchronized, zero CI disk timing race)
        hang = threading.Event()
        job1_entered = threading.Event()
        job2_entered = threading.Event()
        job1_threads: List[threading.Thread] = []
        calls = 0

        def hung_collect(include_guard_check: bool = False) -> Any:
            nonlocal calls
            calls += 1
            if calls == 1:
                job1_threads.append(threading.current_thread())
                job1_entered.set()
                hang.wait(5.0)
            else:
                job2_entered.set()
            return telemetry.OrionTelemetryReport(
                captured_at="x",
                ls_running=True,
                ls_pid=2000,
                main_pids=[1000],
                quota_buckets=[telemetry.QuotaBucketView("G", "gemini-5h", "5h", "5h", 0.9, 90.0, "R", "1h")],
            )

        procs_a = [
            rpc.RawProcEntry(pid=1000, ppid=500, name="Antigravity.exe"),
            rpc.RawProcEntry(pid=2000, ppid=1000, name="language_server_windows_x64.exe"),
        ]
        procs_b = procs_a + [rpc.RawProcEntry(pid=2001, ppid=1000, name="language_server_windows_x64.exe")]
        cyc = 0
        clock_off = 0.0
        real_time = time

        class FakeTime:
            @staticmethod
            def time() -> float:
                return real_time.time() + clock_off

            @staticmethod
            def monotonic() -> float:
                return real_time.monotonic() + clock_off

            @staticmethod
            def sleep(_s: float) -> None:
                real_time.sleep(0)

        def fake_wait(_handles: List[int], _timeout_ms: int) -> int:
            nonlocal cyc, clock_off
            cyc += 1
            if cyc == 1:
                self.assertTrue(job1_entered.wait(5.0))
                clock_off += 100.0
            elif cyc == 2:
                clock_off += 20.0
            elif cyc == 3:
                self.assertTrue(job2_entered.wait(5.0))
                for t in list(threading.enumerate()):
                    if t.name.startswith("orion-guard-telemetry-") and t not in job1_threads:
                        t.join(timeout=5.0)
                clock_off += 1.0
            else:
                clock_off += 1.0
            return rpc.WAIT_TIMEOUT

        hub = rpc.HubEndpoint("hub.json", 2000, 51234, 0, "tok", 1)
        try:
            with (
                patch.object(guillotine, "time", FakeTime),
                patch.object(guillotine, "acquire_single_instance_mutex", return_value=(1234, False)),
                patch.object(guillotine, "release_and_close_mutex"),
                patch.object(guillotine, "retire_legacy_orphan_killer", return_value={}),
                patch.object(guillotine, "snapshot_processes_fast", side_effect=lambda: procs_a if cyc < 2 else procs_b),
                patch.object(guillotine, "discover_hub_endpoint", return_value=hub),
                patch.object(guillotine, "collect_telemetry", side_effect=hung_collect),
                patch.object(guillotine, "open_process_handle", return_value=9999),
                patch.object(guillotine, "wait_for_handles_all", side_effect=fake_wait),
                patch.object(guillotine, "close_handle_safe"),
            ):
                rc = guillotine.run_guillotine_daemon_loop(max_cycles=6)
        finally:
            hang.set()
            real_time.sleep(0.05)

        self.assertEqual(rc, 0)
        self.assertGreater(calls, 1)
        self.assertIn("LS PID 2001", constants.get_guillotine_log_path().read_text(encoding="utf-8"))

    def test_12_panel_generation_guard_cli_bounds_and_launchers(self) -> None:
        # CLI --days bounds
        with self.assertRaises(SystemExit):
            cli_main.build_parser().parse_args(["ledger", "--days", "999999999"])
        with self.assertRaises(SystemExit):
            cli_main.build_parser().parse_args(["shift", "prod"])

        # CLI bar NaN safety
        self.assertEqual(cli_render._bar(math.nan, 10), "░" * 10)

        # Launchers UTF-8 BOM & clean file list (6 core + 5 Orion-Vault launchers = 11)
        l_dir = self.tmp_path / "launchers"
        written = cli_main.deploy_agyrun_launchers(l_dir)
        self.assertEqual(len(written), 11)
        names = {Path(p).name for p in written}
        for expected in (
            "orion-vault-status.ps1",
            "orion-vault-sync.ps1",
            "orion-vault-on.ps1",
            "orion-vault-off.ps1",
            "orion-vault-toggle.ps1",
        ):
            self.assertIn(expected, names)
        for p_str in written:
            p = Path(p_str)
            if p.suffix == ".ps1":
                self.assertTrue(p.read_bytes().startswith(b"\xef\xbb\xbf"))
                self.assertNotIn("Stop-Process", p.read_text(encoding="utf-8-sig"))

        # Panel refresh generation guard
        app = panel_mod.AcerolaOrionApp.__new__(panel_mod.AcerolaOrionApp)
        app._closing = False
        app._refresh_busy = False
        app._action_busy = False
        app._pending_refresh = False
        app._offline_streak = 0
        app._action_buttons = []
        app._ledger = SimpleNamespace(
            record_quota_from_telemetry=lambda *a, **k: False,
            get_daily_summary=lambda **k: [],
            get_model_breakdown=lambda **k: [],
            get_recent_sessions=lambda **k: [],
            compute_rate_audit=lambda: SimpleNamespace(verdict=""),
        )
        app.after = lambda *a, **k: None  # type: ignore[assignment]
        app._append_console = lambda *_a, **_k: None  # type: ignore[assignment]
        applied: List[int] = []
        app._apply_ui_data = lambda rep, *a, **k: applied.append(rep.ls_pid)  # type: ignore[assignment]

        e1_entered = threading.Event()
        e2_entered = threading.Event()
        e1_done = threading.Event()
        e2_done = threading.Event()
        g1 = threading.Event()
        g2 = threading.Event()
        seq = 0

        def fake_collect(include_guard_check: bool = True) -> telemetry.OrionTelemetryReport:
            nonlocal seq
            seq += 1
            my = seq
            if my == 1:
                e1_entered.set()
                g1.wait(5.0)
            else:
                e2_entered.set()
                g2.wait(5.0)
            try:
                return telemetry.OrionTelemetryReport(captured_at=str(my), ls_running=True, ls_pid=my, main_pids=[1])
            finally:
                (e1_done if my == 1 else e2_done).set()

        with (
            patch.object(panel_mod, "collect_telemetry", side_effect=fake_collect),
            patch.object(panel_mod, "read_shared_file_lines", return_value=[]),
        ):
            app.refresh_all_async(silent=True)
            self.assertTrue(e1_entered.wait(2.0))
            app._refresh_started = time.monotonic() - 125.0
            panel_mod.AcerolaOrionApp._pump_ui_queue(app)
            app.refresh_all_async(silent=True)
            self.assertTrue(e2_entered.wait(2.0))
            g1.set()
            self.assertTrue(e1_done.wait(2.0))
            time.sleep(0.05)
            panel_mod.AcerolaOrionApp._pump_ui_queue(app)
            self.assertTrue(app._refresh_busy)
            self.assertEqual(applied, [])
            g2.set()
            self.assertTrue(e2_done.wait(2.0))
            for _ in range(40):
                panel_mod.AcerolaOrionApp._pump_ui_queue(app)
                if not app._refresh_busy:
                    break
                time.sleep(0.02)
            self.assertFalse(app._refresh_busy)
            self.assertEqual(applied, [2])

    def test_13_win32_tray_and_shortcut_clean_room(self) -> None:
        from acerola_orion import win_tray

        ns = cli_main.build_parser().parse_args(["panel", "--minimized"])
        self.assertTrue(ns.minimized)
        ns_sc = cli_main.build_parser().parse_args(["shortcut", "--remove"])
        self.assertTrue(ns_sc.remove)

        ico = panel_mod._get_icon_path()
        self.assertTrue(ico.is_file())

        if sys.platform == "win32":
            woke: List[int] = []
            cls_name = f"OrionPubTrayTest_{os.getpid()}_{time.time_ns()}"
            tray = win_tray.Win32TrayController(
                class_name=cls_name,
                tooltip="Orion Public Test",
                ico_path=ico,
                on_open=lambda: woke.append(1),
                menu_builder=lambda: [("Open", lambda: None)],
                dispatch_ui=lambda cb: cb(),
            )
            try:
                tray.start()
                self.assertNotEqual(tray._hwnd, 0)
                self.assertTrue(win_tray.signal_existing_instance(cls_name))
                for _ in range(30):
                    if woke:
                        break
                    time.sleep(0.02)
                self.assertEqual(woke, [1])
            finally:
                tray.stop()

            sc_dir = self.tmp_path / "start_menu"
            res = win_tray.install_start_menu_shortcut(
                shortcut_name="AcerolaOrionTest",
                arguments="-m acerola_orion panel",
                working_dir=self.tmp_path,
                icon_path=ico,
                app_id=panel_mod.APP_USER_MODEL_ID,
                description="Unit test shortcut",
                target_dir=sc_dir,
            )
            self.assertTrue(res["ok"])
            self.assertTrue(Path(str(res["path"])).is_file())
            rem = win_tray.remove_start_menu_shortcut("AcerolaOrionTest", target_dir=sc_dir)
            self.assertTrue(rem["ok"])
            self.assertTrue(rem["removed"])

    def test_14_astrolabe_ui_zen_mode_and_quota_series(self) -> None:
        ns_zen = cli_main.build_parser().parse_args(["panel", "--zen"])
        self.assertTrue(ns_zen.zen)
        ns_full = cli_main.build_parser().parse_args(["panel", "--full"])
        self.assertTrue(ns_full.full)

        # Verify ledger.get_recent_quota_series
        led = ledger_mod.OrionLedger(
            db_path=self.tmp_path / "ui_series.db",
            brain_dir=self.tmp_path / "empty_brain",
            annotations_dir=self.tmp_path / "empty_ann",
        )
        r_sample = telemetry.OrionTelemetryReport(
            captured_at="2026-11-16 11:00:00",
            ls_running=True,
            ls_pid=4321,
            main_pids=[1234],
            paid_tier_id="g1-ultra-tier",
            paid_tier_name="Google AI Ultra",
            quota_buckets=[
                telemetry.QuotaBucketView("G", "gemini-5h", "5h", "18000s", 0.8341928, 83.41928, "2030-01-01T00:00:00Z", "2小时 14分"),
                telemetry.QuotaBucketView("G", "gemini-weekly", "wk", "604800s", 0.9108, 91.08, "2030-01-05T00:00:00Z", "4天 11小时"),
                telemetry.QuotaBucketView("P", "3p-5h", "3p5h", "18000s", 0.4125, 41.25, "2030-01-01T00:00:00Z", "48分钟"),
                telemetry.QuotaBucketView("P", "3p-weekly", "3pwk", "604800s", 0.642, 64.2, "2030-01-05T00:00:00Z", "3天 2小时"),
            ],
            log_stats=telemetry.LogCongestionStats(
                http_429_503_count=1,
                sidecar=telemetry.SidecarMonitorStats(eval_count=4, avg_ms=320.0, max_ms=610.0, recent_durations_ms=[210.0, 340.0, 610.0, 120.0]),
            ),
        )
        self.assertTrue(led.record_quota_from_telemetry(r_sample, event_type="t1", min_interval_sec=0.0))
        # Interleave a secondary pool row with a different weekly reset timestamp (e.g. multi-account spike)
        r_other_pool = telemetry.OrionTelemetryReport(
            captured_at="2026-11-16 11:01:00",
            ls_running=True,
            ls_pid=4322,
            main_pids=[1234],
            quota_buckets=[
                telemetry.QuotaBucketView("G", "gemini-5h", "5h", "18000s", 1.0, 100.0, "2030-01-01T02:00:00Z", "5h"),
                telemetry.QuotaBucketView("G", "gemini-weekly", "wk", "604800s", 0.5635, 56.35, "2030-01-02T00:00:00Z", "1d"),
            ],
        )
        self.assertTrue(led.record_quota_from_telemetry(r_other_pool, event_type="t2", min_interval_sec=0.0))
        q_series = led.get_recent_quota_series(hours=24.0, active_gemini_wk_reset="2030-01-05T00:00:00Z")
        self.assertEqual(len(q_series), 1)
        self.assertAlmostEqual(q_series[0]["gemini_5h"], 83.4193, places=3)

        # Verify play.googleapis.com/log Clearcut timeouts do NOT false-trigger sidecar.timeout_count
        log_stats = telemetry.analyze_logs(
            [
                'E1116 11:00:00 client.go:98] Post "https://play.googleapis.com/log": context deadline exceeded',
                "E1116 11:00:05 mon.go:42] LLM_MONITOR_LLM_CALL_TIMEOUT: sir_flash_lite exceeded 10s",
            ],
            [],
        )
        self.assertEqual(log_stats.sidecar.timeout_count, 1)

        # Instantiate real Tk Astrolabe UI inside isolated test session (skip gracefully on headless CI without Tcl/Tk display)
        import tkinter as tk

        try:
            with patch.object(panel_mod.AcerolaOrionApp, "refresh_all_async", lambda self, silent=False: None):
                app = panel_mod.AcerolaOrionApp(auto_refresh_ms=0, enable_tray=False, initial_mode="full")
        except tk.TclError as exc:
            self.skipTest(f"Tk display unavailable in headless environment: {exc}")
            return
        try:
            app._last_quota_series = q_series
            today_str = datetime.now().strftime("%Y-%m-%d")
            daily_rows = [
                ledger_mod.DailyUsageRow(today_str, 3, 28, 120_000, 900_000, 45_000, 1.58, 0.79, 2.37),
            ]
            audit_rep = led.compute_rate_audit()
            app._apply_ui_data(r_sample, daily_rows, [], [], audit_rep, ["guard ok"], ["ls ok"], silent=False)
            app.update_idletasks()
            app.dial_gemini._redraw()
            app.dial_claude._redraw()
            app.hub_chart._redraw()
            self.assertGreater(len(app.dial_gemini.canvas.find_all()), 20)
            self.assertGreater(len(app.hub_chart.canvas.find_all()), 5)

            # Switch to Zen / Simplified Mode and cycle through all 4 single-metric + single-chart focuses
            app.toggle_view_mode("zen")
            self.assertEqual(app._view_mode, "zen")
            for mk in ("gemini", "claude", "spend", "latency"):
                app.select_zen_metric(mk)
                app.update_idletasks()
                app.zen_dial._redraw()
                app.zen_chart._redraw()
                self.assertGreater(len(app.zen_dial.canvas.find_all()), 20)
                self.assertGreater(len(app.zen_chart.canvas.find_all()), 3)

            # Verify persisted UI prefs
            saved_prefs = panel_mod._load_ui_prefs()
            self.assertEqual(saved_prefs["view_mode"], "zen")
            self.assertEqual(saved_prefs["zen_metric"], "latency")

            # Switch back to Full Mode and cycle tabs & chart modes
            app.toggle_view_mode("full")
            self.assertEqual(app._view_mode, "full")
            for hk in ("gemini", "claude", "spend"):
                app.select_hub_chart(hk)
                app.hub_chart._redraw()
            for tk_key in ("ledger", "vault", "diag", "hub"):
                app.select_full_tab(tk_key)
                app.update_idletasks()
            self.assertTrue(bool(app.tree_vault.cget("xscrollcommand")))
        finally:
            app.exit_app()

    def test_15_passive_cache_radar_timer_disambiguation_and_ui_anti_truncation(self) -> None:
        # 1) Timer disambiguation: 100.000% 5h bucket is standby, <100% is active countdown
        b_full = telemetry.QuotaBucketView("G", "gemini-5h", "5h", "18000s", 1.0, 100.0, "2030-01-01T00:00:00Z", "4小时 59分")
        self.assertFalse(b_full.timer_started)
        self.assertIn("满额待命", b_full.status_text)

        b_used = telemetry.QuotaBucketView("G", "gemini-5h", "5h", "18000s", 0.75, 75.0, "2030-01-01T00:00:00Z", "3小时 10分")
        self.assertTrue(b_used.timer_started)
        self.assertIn("重置倒计时", b_used.status_text)

        # 2) Auxiliary 429 separation & stage1/stage2 sidecar failure count
        log_stats = telemetry.analyze_logs(
            [
                "E1117 10:00:01 mon.go:11] Monitor sir_flash_lite FINISHED: Failed: HTTP 429 Too Many Requests",
                "E1117 10:00:02 title.go:22] conversation_title failed: status=429",
                "E1117 10:00:03 chat.go:33] HTTP/2 429 Too Many Requests",
            ],
            [],
        )
        self.assertEqual(log_stats.sidecar.stage1_fail_count, 1)
        self.assertEqual(log_stats.aux_429_count, 2)
        self.assertEqual(log_stats.http_429_503_count, 1)

        # 3) Passive CacheRadarSampler: smart_guard protects warm >=100K window & zero redundant reads
        brain = self.tmp_path / "radar_brain"
        ann = self.tmp_path / "radar_ann"
        ann.mkdir(parents=True)
        cid_big = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
        cid_small = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
        now_ep = 1_800_000_000.0

        def _write_conv(cid: str, title: str, in_tok: int, cache_tok: int, out_tok: int, ep: float) -> Path:
            cdir = brain / cid
            (cdir / ".system_generated" / "logs").mkdir(parents=True, exist_ok=True)
            (cdir / ".user_uploaded").mkdir(parents=True, exist_ok=True)
            (cdir / ".user_uploaded" / "private.png").write_bytes(b"SKIP_ME")
            (ann / f"{cid}.pbtxt").write_text(f'title: "{title}"\n', encoding="utf-8")
            iso = datetime.fromtimestamp(ep, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            tp = cdir / ".system_generated" / "logs" / "transcript.jsonl"
            tp.write_text(
                json.dumps(
                    {
                        "step_index": 2,
                        "type": "PLANNER_RESPONSE",
                        "status": "DONE",
                        "created_at": iso,
                        "input_tokens": in_tok,
                        "cache_read_tokens": cache_tok,
                        "output_tokens": out_tok,
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            os.utime(tp, ns=(int(ep * 1_000_000_000), int(ep * 1_000_000_000)))
            return tp

        _write_conv(cid_big, "Heavy Refactor", 20_000, 180_000, 4_000, now_ep - 200.0)
        _write_conv(cid_small, "Quick Chat", 2_000, 0, 300, now_ep - 10.0)

        sampler = cache_radar.CacheRadarSampler(brain_dir=brain, annotations_dir=ann, target_mode="smart_guard")
        st_guard = sampler.sample(now_epoch=now_ep)
        self.assertEqual(st_guard.conv_id, cid_big)
        self.assertTrue(st_guard.smart_guarded)
        self.assertEqual(st_guard.phase, cache_radar.PHASE_WARNING)
        self.assertTrue(st_guard.should_pop_smart_hud)
        reads_after_first = sampler.tail_read_count
        # Second sample without file changes must perform ZERO tail reads
        st_guard_2 = sampler.sample(now_epoch=now_ep + 5.0)
        self.assertEqual(sampler.tail_read_count, reads_after_first)
        self.assertAlmostEqual(st_guard_2.remaining_sec, 45.0, places=1)

        # CLI `radar --json` smoke check
        buf = io.StringIO()
        with (
            patch.object(cache_radar, "get_agy_brain_dir", return_value=brain),
            redirect_stdout(buf),
        ):
            rc = cli_main.main(["radar", "--json"])
        self.assertEqual(rc, 0)
        radar_json = json.loads(buf.getvalue())
        self.assertEqual(radar_json["state"]["conv_id"], cid_big)

        # 4) UI anti-truncation & withdrawn-window update_idletasks() zero-spinlock check
        import tkinter as tk

        try:
            with patch.object(panel_mod.AcerolaOrionApp, "refresh_all_async", lambda self, silent=False: None):
                app = panel_mod.AcerolaOrionApp(auto_refresh_ms=0, enable_tray=False, enable_hud=True, initial_mode="full")
        except tk.TclError as exc:
            self.skipTest(f"Tk display unavailable in headless environment: {exc}")
            return
        try:
            # Verify horizontal scrollbar contract on Treeviews
            self.assertTrue(bool(app.tree_daily.cget("xscrollcommand")))
            self.assertTrue(bool(app.tree_models.cget("xscrollcommand")))
            self.assertTrue(bool(app.tree_sessions.cget("xscrollcommand")))

            # Withdraw window (simulating tray sleep) and trigger redraws: must NOT spinlock on after_idle!
            app.withdraw()
            app._apply_radar_state_to_ui(st_guard, force_tooltip=True)
            app.dial_gemini._redraw()
            app.hub_chart._redraw()
            app._redraw_radar_bar()
            t0 = time.monotonic()
            app.update_idletasks()
            self.assertLess(time.monotonic() - t0, 2.0)

            # Verify Zen mode "cache" pill & HUD mode switching
            app.deiconify()
            app.set_hud_mode("always")
            self.assertEqual(app._hud_mode, "always")
            self.assertIsNotNone(app._hud_win)
            assert app._hud_win is not None
            self.assertTrue(app._hud_win._is_shown)
            app.set_hud_mode("off")
            self.assertFalse(app._hud_win._is_shown)
            app.toggle_view_mode("zen")
            app.select_zen_metric("cache")
            self.assertEqual(app._zen_metric, "cache")
        finally:
            app.exit_app()

    def test_16_security_audit_ledger_incremental_sha256_median_and_prompt_scrub(self) -> None:
        brain = self.tmp_path / "rev_brain"
        ann = self.tmp_path / "rev_ann"
        cid = "00000000-0000-4000-8000-000000000001"
        logs = brain / cid / ".system_generated" / "logs"
        logs.mkdir(parents=True)
        tfile = logs / "transcript.jsonl"
        db = self.tmp_path / "rev_ledger.db"

        def _line(index: int, cache: int = 5000, model: str = "Gemini 4 Carbon") -> str:
            return (
                json.dumps(
                    {
                        "type": "PLANNER_RESPONSE",
                        "step_index": index,
                        "model": model,
                        "created_at": "2026-11-18T10:00:00Z",
                        "cache_read_input_tokens": cache,
                    }
                )
                + "\n"
            )

        # A01/A04: incremental append only parses new lines, rewrite/truncate triggers SHA-256 rebuild
        tfile.write_text(_line(1) + _line(2), encoding="utf-8")
        book = ledger_mod.OrionLedger(db, brain, ann)
        self.assertEqual(book.sync_transcripts(), 1)
        reads: List[str] = []
        orig_loads = json.loads

        def _measure(value: Any, *args: Any, **kwargs: Any) -> Any:
            if isinstance(value, str) and '"PLANNER_RESPONSE"' in value:
                reads.append(value)
            return orig_loads(value, *args, **kwargs)

        with tfile.open("a", encoding="utf-8") as stream:
            stream.write(_line(2, 7000))
        with patch.object(ledger_mod.json, "loads", side_effect=_measure):
            book.sync_transcripts()
        self.assertEqual(len(reads), 1)
        s0 = book.get_recent_sessions()[0]
        self.assertEqual((s0.steps, s0.cache_read_tokens), (2, 12000))

        # Rewrite earlier byte with larger total length -> SHA-256 prefix mismatch forces full re-parse
        tfile.write_text(_line(1, 9999) + _line(2, 7000) + _line(3) + _line(4), encoding="utf-8")
        book.sync_transcripts()
        self.assertEqual(book.get_recent_sessions()[0].cache_read_tokens, 26999)

        # Model vote replacement & broken cursor recovery
        with tfile.open("a", encoding="utf-8") as stream:
            stream.write(
                _line(1, model="Claude Opus 4.6")
                + _line(2, model="Claude Opus 4.6")
                + _line(3, model="Claude Opus 4.6")
            )
        book.sync_transcripts()
        self.assertEqual(
            book.get_recent_sessions()[0].primary_model,
            constants.classify_model("Claude Opus 4.6").family_label,
        )
        with book._connect(immediate=True) as conn:
            conn.execute("UPDATE session_index SET parse_cursor='truncated'")
        with tfile.open("a", encoding="utf-8") as stream:
            stream.write(_line(5))
        book.sync_transcripts()
        self.assertEqual(book.get_recent_sessions()[0].steps, 5)

        # A15: Prompt title scrub & split record safety
        cid2 = "00000000-0000-4000-8000-000000000002"
        logs2 = brain / cid2 / ".system_generated" / "logs"
        logs2.mkdir(parents=True)
        tfile2 = logs2 / "transcript.jsonl"
        note = json.dumps({"type": "USER_INPUT", "content": "fixture-secret-prompt"}) + "\n"
        raw = _line(1)
        tfile2.write_text(note + raw[:30], encoding="utf-8")
        book.sync_transcripts()
        self.assertNotIn(cid2, [r.conv_id for r in book.get_recent_sessions()])
        with tfile2.open("a", encoding="utf-8") as stream:
            stream.write(raw[30:])
        book.sync_transcripts()
        row2 = next(r for r in book.get_recent_sessions() if r.conv_id == cid2)
        self.assertEqual((row2.steps, row2.title), (1, cid2[:8]))

        # A07/A08/A15: Old DB schema migration scrubs prompt title & populates last_epoch
        db_old = self.tmp_path / "old_schema.db"
        st2 = tfile2.stat()
        with closing(sqlite3.connect(db_old)) as conn, conn:
            conn.execute(
                "CREATE TABLE session_index(conv_id TEXT PRIMARY KEY,mtime_ns INTEGER,file_size INTEGER,"
                "title TEXT,primary_model TEXT,first_ts TEXT,last_ts TEXT,local_date TEXT)"
            )
            conn.execute(
                "INSERT INTO session_index VALUES (?,?,?,?,?,?,?,?)",
                (cid2, st2.st_mtime_ns, st2.st_size, "fixture-secret-prompt", "M", "", "2026-11-18T10:00:00Z", ""),
            )
        book_old = ledger_mod.OrionLedger(db_old, brain, ann)
        with closing(sqlite3.connect(db_old)) as conn:
            title_after, epoch_after = conn.execute(
                "SELECT title, last_epoch FROM session_index WHERE conv_id=?", (cid2,)
            ).fetchone()
        self.assertEqual(title_after, cid2[:8])
        self.assertIsInstance(epoch_after, float)
        self.assertGreater(len(book_old.get_recent_sessions(limit=1)), 0)

        # A05/A06: Even median uses statistics.median & p3_5h_drop_pct is independent of p3_weekly_drop_pct
        book_m = ledger_mod.OrionLedger(self.tmp_path / "med.db", self.tmp_path / "nb", self.tmp_path / "na")
        now = int(datetime.now(timezone.utc).timestamp()) - 800
        with book_m._connect(immediate=True) as conn:
            for i, f in enumerate((1.0, 0.99, 0.98)):
                conn.execute(
                    "INSERT INTO quota_snapshots(ts_utc,ts_epoch,event_type,gemini_5h,p3_5h,p3_weekly) VALUES(?,?,?,?,?,?)",
                    (str(i), now + 300 * i, "fixture", f, 1.0, 1.0),
                )
            for i, u in enumerate((1.0, 3.0)):
                conn.execute(
                    "INSERT INTO step_ledger VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    ("fixture", i, "", now + 150 + 300 * i, "", "M", "4c", "gemini", 1, 0, 1, u, u, u),
                )
        self.assertAlmostEqual(book_m.compute_rate_audit().median_5h_pool_usd_at_1x or 0.0, 200.0, places=2)
        with book_m._connect(immediate=True) as conn:
            conn.execute(
                "INSERT INTO quota_snapshots(ts_utc,ts_epoch,event_type,gemini_5h,p3_5h,p3_weekly) VALUES(?,?,?,?,?,?)",
                ("3", now + 900, "fixture", 0.98, 0.90, 1.0),
            )
        ev = book_m.compute_rate_audit().phantom_drains[-1]
        self.assertAlmostEqual(ev.p3_5h_drop_pct, 10.0, places=2)
        self.assertAlmostEqual(ev.p3_weekly_drop_pct, 0.0, places=2)

    def test_17_security_audit_owned_handle_guard_proxy_redaction_and_timer(self) -> None:
        # A02: Late-appearing transcript discovered without restarting sampler
        brain = self.tmp_path / "a02_brain"
        cid = "00000000-0000-4000-8000-000000000017"
        logs = brain / cid / ".system_generated" / "logs"
        logs.mkdir(parents=True)
        sampler = cache_radar.CacheRadarSampler(brain_dir=brain, annotations_dir=self.tmp_path / "a02_ann")
        self.assertEqual(sampler.list_recent_sessions(), [])
        (logs / "transcript.jsonl").write_text(
            json.dumps(
                {
                    "type": "PLANNER_RESPONSE",
                    "step_index": 1,
                    "created_at": "2026-11-18T10:00:00Z",
                    "cache_read_input_tokens": 6000,
                }
            )
            + "\n",
            encoding="utf-8",
        )
        self.assertEqual(sampler.list_recent_sessions()[0].conv_id, cid)

        # A03: Auto-refresh timer schedules once and cancels cleanly
        app = object.__new__(panel_mod.AcerolaOrionApp)
        app._closing = False
        app.auto_refresh_ms = 15
        app._offline_streak = 0
        app._auto_refresh_id = None
        app._hidden_to_tray = False
        app.after = Mock(return_value="timer_id")  # type: ignore[assignment]
        app.after_cancel = Mock()  # type: ignore[assignment]
        for _ in range(3):
            app._schedule_auto_refresh()
            app._schedule_auto_refresh()
            app._cancel_auto_refresh()
        self.assertEqual(app.after.call_count, 3)
        self.assertEqual(app.after_cancel.call_count, 3)

        # A10: Guard only terminates pinned handles & open_owned_language_server rejects foreign/unrelated servers
        closed = Mock()
        with (
            patch.object(guillotine, "terminate_owned_handle", return_value=True) as kill,
            patch.object(guillotine, "close_handle_safe", closed),
        ):
            self.assertEqual(guillotine._release_tracked_servers({}, stop=True), [])
            self.assertEqual(guillotine._release_tracked_servers({5: 101}, stop=True), [5])
            kill.assert_called_once_with(101)
            closed.assert_called_once_with(101)

        app_exe = self.tmp_path / "editor" / "Antigravity.exe"
        bad_srv = self.tmp_path / "unrelated" / "language_server.exe"
        entries = [rpc.RawProcEntry(1, 0, "Antigravity.exe"), rpc.RawProcEntry(2, 1, bad_srv.name)]
        hub = rpc.HubEndpoint("fixture", 2, 123, 0, "fixture")
        with (
            patch.object(rpc, "open_process_handle", return_value=99),
            patch.object(rpc, "close_handle_safe") as close_h,
            patch.object(rpc, "_same_user", return_value=False) as same_u,
            patch.object(rpc, "_handle_image", return_value=bad_srv) as img_h,
            patch.object(rpc, "query_process_image_path_win32", return_value=str(app_exe)),
            patch.object(rpc, "discover_hub_endpoint", return_value=hub),
        ):
            self.assertIsNone(rpc.open_owned_language_server(2, entries))
            same_u.return_value = True
            self.assertIsNone(rpc.open_owned_language_server(2, entries))
            img_h.return_value = app_exe.parent / "bin" / bad_srv.name
            self.assertEqual(rpc.open_owned_language_server(2, entries), 99)
            self.assertEqual(close_h.call_count, 2)

        # A13: Proxy diagnostic strips userinfo, query, and fragment via urlsplit
        settings = self.tmp_path / "proxy_settings.json"
        settings.write_text(
            json.dumps({"http.proxy": "http://secret_user:secret_pass@localhost:7890/path?token=secret_q#secret_f"}),
            encoding="utf-8",
        )
        with patch.object(telemetry, "get_user_settings_path", return_value=settings):
            proxy_report = telemetry._inspect_proxy_mode()
        self.assertNotIn("secret_user", proxy_report)
        self.assertNotIn("secret_pass", proxy_report)
        self.assertNotIn("secret_q", proxy_report)
        self.assertNotIn("secret_f", proxy_report)
        self.assertIn("localhost:7890", proxy_report)

        # A18: Legacy retirement helper is strictly non-destructive
        with patch.object(guillotine.subprocess, "run") as proc_run:
            self.assertFalse(guillotine.retire_legacy_orphan_killer()["legacy_dir_removed"])
            proc_run.assert_not_called()

    def test_18_pricing_tier_200k_double_alert_and_compact_copy(self) -> None:
        cid = "22222222-3333-4444-5555-666666666666"
        t0 = 1_800_000_000.0

        def _make_state(ctx_tokens: int, offset_sec: float = 40.0) -> cache_radar.CacheRadarState:
            in_t = ctx_tokens // 5
            cache_t = ctx_tokens - in_t
            snap = cache_radar.TranscriptTailSnapshot(
                conv_id=cid,
                mtime_ns=int(t0 * 1_000_000_000),
                file_size=4096,
                has_planner_response=True,
                planner_step_index=10,
                planner_epoch=t0,
                input_tokens=in_t,
                cache_read_tokens=cache_t,
                output_tokens=1200,
                prompt_tokens=ctx_tokens,
                context_tokens=ctx_tokens,
                cache_hit_rate=(cache_t / ctx_tokens) if ctx_tokens > 0 else 0.0,
                last_step_index=10,
                last_step_type="PLANNER_RESPONSE",
                last_step_status="DONE",
                last_step_epoch=t0,
            )
            return cache_radar.evaluate_cache_radar_state(snap, now_epoch=t0 + offset_sec, conv_title="Long Context Audit")

        s_106k = _make_state(106_000)
        self.assertEqual(s_106k.pricing_tier, cache_radar.PRICING_TIER_NORMAL)
        self.assertFalse(s_106k.is_near_2x)
        self.assertFalse(s_106k.is_over_2x)
        self.assertEqual(s_106k.tier_badge_text, "")
        self.assertEqual(panel_mod._radar_palette(s_106k)[2], panel_mod.BORDER_SUBTLE)

        s_185k = _make_state(185_000)
        self.assertEqual(s_185k.pricing_tier, cache_radar.PRICING_TIER_NEAR_2X)
        self.assertTrue(s_185k.is_near_2x)
        self.assertEqual(s_185k.tokens_to_2x, 15_000)
        self.assertIn(cache_radar.COMPACT_COMMAND_TEXT, s_185k.tier_alert_text)
        self.assertEqual(panel_mod._radar_palette(s_185k)[2], panel_mod.ACCENT_AMBER)

        s_200k = _make_state(200_000)
        self.assertEqual(s_200k.pricing_tier, cache_radar.PRICING_TIER_OVER_2X)
        self.assertTrue(s_200k.is_over_2x)
        self.assertEqual(s_200k.tokens_to_2x, 0)
        self.assertIn("2x", s_200k.tier_badge_text)
        self.assertEqual(panel_mod._radar_palette(s_200k)[2], panel_mod.ACCENT_CRIMSON)

        toasts: List[str] = []
        app = panel_mod.AcerolaOrionApp.__new__(panel_mod.AcerolaOrionApp)
        app._closing = False
        app._hidden_to_tray = False
        app._view_mode = "full"
        app._hud_mode = "smart"
        app._vault_auto_enabled = True
        app._hud_win = SimpleNamespace(
            flash_toast=lambda msg, duration_sec=1.6: toasts.append(msg),
            render_state=lambda _st, visible=False: None,
        )  # type: ignore[assignment]
        app._tray = SimpleNamespace(active=True, update_tooltip=lambda _t: None)  # type: ignore[assignment]
        app._ui_call = lambda cb: cb()  # type: ignore[assignment]
        app.restore_from_tray = lambda: None  # type: ignore[assignment]
        app.exit_app = lambda: None  # type: ignore[assignment]
        app._run_action = lambda _lbl, fn: fn()  # type: ignore[assignment]

        app._apply_radar_state_to_ui(s_106k)
        self.assertFalse(app._should_show_hud(s_106k))
        self.assertEqual(len(toasts), 0)

        app._apply_radar_state_to_ui(s_185k)
        self.assertTrue(app._should_show_hud(s_185k))
        self.assertEqual(len(toasts), 1)
        self.assertIn("逼近200K", toasts[-1])

        app._apply_radar_state_to_ui(s_200k)
        self.assertTrue(app._should_show_hud(s_200k))
        self.assertEqual(len(toasts), 2)
        self.assertIn("双倍计价", toasts[-1])

        s_65k = _make_state(65_000)
        app._apply_radar_state_to_ui(s_65k)
        self.assertFalse(app._should_show_hud(s_65k))
        self.assertEqual(app._notified_tier_levels[cid], 0)

        tray_items = panel_mod.AcerolaOrionApp._build_tray_menu(app)
        compact_cb = next(it[1] for it in tray_items if it is not None and cache_radar.COMPACT_COMMAND_TEXT in it[0])
        compact_cb()
        self.assertEqual(app._last_copied_text, cache_radar.COMPACT_COMMAND_TEXT)

    def _make_fake_vault_env(self) -> tuple[dict[str, Path], vault_mod.OrionVault]:
        agy = self.tmp_path / "vault_agy"
        conv_dir = agy / "conversations"
        brain_dir = agy / "brain"
        anno_dir = agy / "annotations"
        conv_dir.mkdir(parents=True, exist_ok=True)
        brain_dir.mkdir(parents=True, exist_ok=True)
        anno_dir.mkdir(parents=True, exist_ok=True)
        sum_db = agy / "conversation_summaries.db"
        with closing(sqlite3.connect(str(sum_db))) as conn, conn:
            conn.executescript(
                """
                CREATE TABLE conversation_summaries (
                    conversation_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL DEFAULT '',
                    preview TEXT NOT NULL DEFAULT '',
                    step_count INTEGER NOT NULL DEFAULT 0,
                    last_modified_time TEXT NOT NULL DEFAULT '',
                    workspace_uris TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT '',
                    project_id TEXT NOT NULL DEFAULT '',
                    last_user_input_time TEXT NOT NULL DEFAULT '',
                    raw_summary BLOB
                );
                CREATE VIRTUAL TABLE conversations_fts USING fts5(
                    conversation_id UNINDEXED,
                    chunk_index UNINDEXED,
                    title,
                    user_body,
                    agent_body
                );
                """
            )
        env = {"agy": agy, "conv_dir": conv_dir, "brain_dir": brain_dir, "anno_dir": anno_dir, "sum_db": sum_db}
        vault = vault_mod.OrionVault(
            vault_dir=Path(os.environ["ACEROLA_ORION_HOME"]) / "chat_vault",
            agy_conv_dir=conv_dir,
            agy_brain_dir=brain_dir,
            agy_anno_dir=anno_dir,
            agy_summaries_db=sum_db,
        )
        return env, vault

    @staticmethod
    def _populate_vault_conv(
        env: dict[str, Path],
        conv_id: str,
        title: str,
        user_msg: str,
        agent_msg: str,
        raw_blob: bytes = b"\x08\x01\x12\x08protobin",
    ) -> None:
        with closing(sqlite3.connect(str(env["conv_dir"] / f"{conv_id}.db"))) as c, c:
            c.execute("CREATE TABLE steps (idx INTEGER PRIMARY KEY, step_payload BLOB)")
            c.execute("INSERT INTO steps (idx, step_payload) VALUES (1, ?)", (user_msg.encode("utf-8"),))
            c.execute("INSERT INTO steps (idx, step_payload) VALUES (2, ?)", (agent_msg.encode("utf-8"),))

        (env["anno_dir"] / f"{conv_id}.pbtxt").write_text(
            f'title:"{title}"  last_user_view_time:{{seconds:1795008000  nanos:0}}',
            encoding="utf-8",
        )
        b_root = env["brain_dir"] / conv_id
        logs_dir = b_root / ".system_generated" / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        steps_jsonl = [
            json.dumps(
                {
                    "step_index": 0,
                    "type": "USER_INPUT",
                    "created_at": "2026-11-18T13:20:00Z",
                    "content": f"<USER_REQUEST>\n{user_msg}\n</USER_REQUEST>\n"
                    "<USER_SETTINGS_CHANGE>\nThe user changed setting `Model Selection` from None to Gemini 4 Neon (High).\n</USER_SETTINGS_CHANGE>",
                },
                ensure_ascii=False,
            ),
            json.dumps(
                {
                    "step_index": 1,
                    "type": "PLANNER_RESPONSE",
                    "created_at": "2026-11-18T13:20:05Z",
                    "content": agent_msg,
                    "tool_calls": [{"name": "read_file"}],
                },
                ensure_ascii=False,
            ),
        ]
        (logs_dir / "transcript.jsonl").write_text("\n".join(steps_jsonl) + "\n", encoding="utf-8")
        (b_root / "plan.md").write_text("# Design Notes\nPlain text only", encoding="utf-8")
        (b_root / "diagram.svg").write_text("<svg></svg>", encoding="utf-8")
        up_dir = b_root / ".user_uploaded"
        up_dir.mkdir(parents=True, exist_ok=True)
        (up_dir / "secret_photo.png").write_bytes(b"\x89PNG\r\n\x1a\nsecret")
        (b_root / "screenshot.png").write_bytes(b"\x89PNG\r\n\x1a\nraster")
        (b_root / "demo.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42")
        (b_root / ".allow_media").write_text("pass", encoding="utf-8")

        with closing(sqlite3.connect(str(env["sum_db"]))) as sc, sc:
            sc.execute(
                """
                INSERT OR REPLACE INTO conversation_summaries
                (conversation_id, title, preview, step_count, last_modified_time, workspace_uris, status, project_id, last_user_input_time, raw_summary)
                VALUES (?, ?, ?, 2, '2026-11-18T21:20:05+08:00', '["file:///workspace"]', 'done', 'proj-1', '2026-11-18T21:20:00+08:00', ?)
                """,
                (conv_id, title, agent_msg[:60], raw_blob),
            )
            sc.execute(
                "INSERT INTO conversations_fts (conversation_id, chunk_index, title, user_body, agent_body) VALUES (?, 0, ?, ?, ?)",
                (conv_id, title, user_msg, agent_msg),
            )

    def test_19_orion_vault_switch_privacy_delete_interception_export_and_restore(self) -> None:
        env, vault = self._make_fake_vault_env()
        cid = "deadbeef-1234-5678-9abc-def012345678"
        secret_blob = b"\x0a\x0f\xfe\xffraw_proto_summary_payload"
        self._populate_vault_conv(
            env,
            cid,
            "误删恢复全链路测试",
            "帮我检查本地星盘遥测架构",
            "核心结论：使用 Orion-Astrolabe 本地回环采样并开启对话保险箱。",
            raw_blob=secret_blob,
        )

        # 1) Controllable switch & Stop hook short-circuit
        self.assertTrue(constants.is_vault_auto_backup_enabled())
        self.assertTrue(vault_hook_mod._quick_switch_enabled())
        res_off = constants.configure_vault_auto_backup("off")
        self.assertTrue(res_off["ok"] and not res_off["enabled"])
        self.assertFalse(vault_hook_mod._quick_switch_enabled())
        self.assertTrue(vault.sync_all(respect_switch=True).skipped_by_switch)
        with patch.object(vault_mod, "OrionVault", return_value=vault):
            self.assertEqual(json.loads(vault_mod.run_stop_hook_from_stdin(json.dumps({"conversationId": cid}))), {"decision": "stop"})
            self.assertEqual(vault.get_status().total_conversations, 0)

        self.assertTrue(constants.configure_vault_auto_backup("toggle")["enabled"])
        with patch.object(vault_mod, "OrionVault", return_value=vault):
            self.assertEqual(json.loads(vault_mod.run_stop_hook_from_stdin(json.dumps({"conversationId": cid}))), {"decision": "stop"})

        # 2) Media privacy isolation & incremental idempotence
        r2 = vault.sync_all()
        self.assertEqual((r2.backed_up, r2.unchanged), (0, 1))
        sess_brain = vault.sessions_dir / cid / "brain"
        self.assertTrue((sess_brain / "plan.md").is_file())
        self.assertTrue((sess_brain / "diagram.svg").is_file())
        self.assertFalse((sess_brain / ".user_uploaded").exists())
        self.assertFalse((sess_brain / "screenshot.png").exists())
        self.assertFalse((sess_brain / "demo.mp4").exists())
        self.assertFalse((sess_brain / ".allow_media").exists())

        # 3) Simulate UI permanent deletion -> Vault preserves archive & flags deleted
        for f in env["conv_dir"].glob(f"{cid}.db*"):
            f.unlink()
        shutil.rmtree(env["brain_dir"] / cid)
        (env["anno_dir"] / f"{cid}.pbtxt").unlink()
        with closing(sqlite3.connect(str(env["sum_db"]))) as sc, sc:
            sc.execute("DELETE FROM conversation_summaries WHERE conversation_id = ?", (cid,))
            sc.execute("DELETE FROM conversations_fts WHERE conversation_id = ?", (cid,))

        r_del = vault.sync_all()
        self.assertEqual(r_del.newly_marked_deleted, 1)
        st = vault.get_status()
        self.assertEqual((st.total_conversations, st.active_conversations, st.deleted_conversations), (1, 0, 1))

        # 4) Search & non-overwriting Markdown export (_v1.md -> _v2.md)
        hits = vault.search_vault(["Orion-Astrolabe", "保险箱"], deleted_only=True)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].live_status, "deleted")
        exp1 = vault.export_markdown("deadbeef")
        exp2 = vault.export_markdown("deadbeef")
        self.assertTrue(exp1["ok"] and exp2["ok"])
        self.assertTrue(exp1["export_path"].endswith("_v1.md"))
        self.assertTrue(exp2["export_path"].endswith("_v2.md"))
        self.assertIn("Orion-Astrolabe", Path(exp1["export_path"]).read_text(encoding="utf-8"))

        # 5) Lossless restore + --force safety backup
        res_rst = vault.restore_conversation("deadbeef")
        self.assertTrue(res_rst["ok"])
        with closing(sqlite3.connect(str(env["conv_dir"] / f"{cid}.db"))) as rc:
            self.assertEqual(rc.execute("SELECT COUNT(*) FROM steps").fetchone()[0], 2)
        with closing(sqlite3.connect(str(env["sum_db"]))) as sc2:
            restored_blob = sc2.execute(
                "SELECT raw_summary FROM conversation_summaries WHERE conversation_id = ?", (cid,)
            ).fetchone()[0]
            fts_cnt = sc2.execute(
                "SELECT COUNT(*) FROM conversations_fts WHERE conversation_id = ?", (cid,)
            ).fetchone()[0]
        self.assertEqual(bytes(restored_blob), secret_blob)
        self.assertEqual(fts_cnt, 1)

        self.assertFalse(vault.restore_conversation("deadbeef", force=False)["ok"])
        res_forced = vault.restore_conversation("deadbeef", force=True)
        self.assertTrue(res_forced["ok"])
        self.assertTrue(Path(res_forced["archived_previous_live_db"]).is_file())

    def test_20_orion_vault_cli_and_panel_tab_integration(self) -> None:
        env, vault = self._make_fake_vault_env()
        cid = "99999999-8888-7777-6666-555555555555"
        self._populate_vault_conv(env, cid, "CLI保险箱集成测试", "测试CLI开关", "Vault CLI 响应正常")

        with patch.object(cli_main, "OrionVault", return_value=vault):
            for args, key, expected in [
                (["vault", "off", "--json"], "enabled", False),
                (["vault", "on", "--json"], "enabled", True),
                (["vault", "sync", "--json"], "backed_up", 1),
                (["vault", "status", "--json"], "total_conversations", 1),
            ]:
                buf = io.StringIO()
                with redirect_stdout(buf):
                    rc = cli_main.main(args)
                self.assertEqual(rc, 0)
                self.assertEqual(json.loads(buf.getvalue())[key], expected)

        import tkinter as tk

        try:
            with patch.object(panel_mod.AcerolaOrionApp, "refresh_all_async", lambda self, silent=False: None):
                app = panel_mod.AcerolaOrionApp(auto_refresh_ms=0, enable_tray=False, initial_mode="full")
        except tk.TclError as exc:
            self.skipTest(f"Tk display unavailable in headless environment: {exc}")
            return
        try:
            self.assertTrue(hasattr(app, "tab_vault"))
            self.assertTrue(hasattr(app, "tree_vault"))
            self.assertIsNotNone(app._btn_vault_switch)
            menu_labels = [m[0] for m in app._build_tray_menu() if m is not None]
            self.assertTrue(any("对话自动备份" in lbl for lbl in menu_labels))
            self.assertTrue(any(cache_radar.COMPACT_COMMAND_TEXT in lbl for lbl in menu_labels))
            app._sync_vault_switch_button(False)
            self.assertIn("已关闭", app._btn_vault_switch.cget("text"))
            app._sync_vault_switch_button(True)
            self.assertIn("已开启", app._btn_vault_switch.cget("text"))
        finally:
            app.exit_app()


if __name__ == "__main__":
    unittest.main()
