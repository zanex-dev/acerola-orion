# -*- coding: utf-8 -*-
"""
Acerola-Orion v1.0.0 Public Edition Verification & Security Self-Audit Suite
Runs entirely inside isolated TemporaryDirectory() with zero external network or host side effects.
"""

from __future__ import annotations

from contextlib import redirect_stdout
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler
import io
import json
import math
import os
from pathlib import Path
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
from typing import Any, List
import unittest
from unittest.mock import patch

import acerola_orion.__main__ as cli_main
import acerola_orion.cache_radar as cache_radar
import acerola_orion.cli_render as cli_render
import acerola_orion.constants as constants
import acerola_orion.guillotine as guillotine
import acerola_orion.ledger as ledger_mod
import acerola_orion.panel as panel_mod
import acerola_orion.rpc as rpc
import acerola_orion.telemetry as telemetry


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
        os.environ["ACEROLA_ORION_HOME"] = str(self.tmp_path / "orion_home")
        telemetry.reset_log_cursor_cache()

    def tearDown(self) -> None:
        telemetry.reset_log_cursor_cache()
        if self._old_home is None:
            os.environ.pop("ACEROLA_ORION_HOME", None)
        else:
            os.environ["ACEROLA_ORION_HOME"] = self._old_home
        self._tmp.cleanup()

    def test_01_static_security_zero_egress_and_clean_room_audit(self) -> None:
        """Verify zero external HTTP deps, strict 127.0.0.1 loopback, zero credential access, and clean surface."""
        repo_root = Path(__file__).resolve().parent.parent
        py_files = sorted((repo_root / "acerola_orion").glob("*.py"))
        self.assertGreaterEqual(len(py_files), 7)

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
        ]

        scan_targets = list(py_files) + [
            repo_root / "pyproject.toml",
            repo_root / "assets" / "logo.svg",
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
        self.assertEqual(constants.APP_VERSION, "1.3.0")
        c4 = constants.classify_model("Gemini 4 Carbon (High)")
        self.assertEqual(c4.family_id, "4c")
        self.assertAlmostEqual(c4.compute_usd(1_000_000, 0, 100_000, 1.0), 6.0)
        self.assertAlmostEqual(c4.compute_usd(1_000_000, 0, 100_000, c4.promised_multiplier), 3.0)
        self.assertAlmostEqual(c4.compute_usd(1_000_000, 0, 100_000, c4.suspected_agy_multiplier), 9.0)
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

        # Launchers UTF-8 BOM & clean file list
        l_dir = self.tmp_path / "launchers"
        written = cli_main.deploy_agyrun_launchers(l_dir)
        self.assertEqual(len(written), 6)
        for p_str in written:
            p = Path(p_str)
            if p.suffix == ".ps1":
                self.assertTrue(p.read_bytes().startswith(b"\xef\xbb\xbf"))

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
            for tk_key in ("ledger", "diag", "hub"):
                app.select_full_tab(tk_key)
                app.update_idletasks()
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


if __name__ == "__main__":
    unittest.main()
