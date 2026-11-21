# -*- coding: utf-8 -*-
"""
Unit tests for Acerola-Orion neutral microkernel extension hooks across
CLI (`__main__`), telemetry (`telemetry`), status renderer (`cli_render`),
cache radar (`cache_radar`), and daemon guard (`guillotine`).
"""

from __future__ import annotations

from contextlib import redirect_stdout
from datetime import datetime, timezone
import io
import json
import logging
import math
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import acerola_orion.__main__ as cli_main
import acerola_orion.cache_radar as cache_radar
import acerola_orion.cli_render as cli_render
import acerola_orion.guillotine as guillotine
import acerola_orion.rpc as rpc
import acerola_orion.telemetry as telemetry
from acerola_orion.core.kernel import (
    DISABLE_PLUGINS_ENV,
    HookBus,
    get_kernel,
    reset_kernel_for_tests,
)


class _DemoCliPlugin:
    name = "demo_cli_plugin"

    def __init__(self) -> None:
        self.handled: list[tuple[str, str]] = []

    def register_cli(self, subparsers) -> None:
        p = subparsers.add_parser("ext-ping", help="Demo extension ping command")
        p.add_argument("--label", default="ok")
        p.add_argument("--rc", type=int, default=0)

    def handle_cli(self, cmd: str, args) -> int | None:
        if cmd == "ext-ping":
            label = str(getattr(args, "label", "ok"))
            self.handled.append((cmd, label))
            print(f"EXT_PING:{label}")
            return int(getattr(args, "rc", 0))
        return None

    def extra_launchers(self, py_exe: str, pyw_exe: str, pkg_root) -> dict[str, str]:
        return {
            "ext-demo.ps1": f"# demo\r\n& '{py_exe}' -m acerola_orion ext-ping\r\n",
            "ext-demo.cmd": f'@echo off\r\nrem 插件扩展\r\n"{pyw_exe}" -m acerola_orion ext-ping\r\n',
            # Security & core-protection boundaries: must be ignored
            "orion-status.ps1": "SHOULD_NOT_OVERWRITE_CORE",
            "../escape.ps1": "SHOULD_NOT_ESCAPE",
            "nested/bad.ps1": "SHOULD_NOT_NEST",
        }


class CoreHooksTestSuite(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="orion_hooks_test_")
        self.tmp_path = Path(self._tmp.name)
        self._old_home = os.environ.get("ACEROLA_ORION_HOME")
        self._old_disable = os.environ.get(DISABLE_PLUGINS_ENV)
        os.environ["ACEROLA_ORION_HOME"] = str(self.tmp_path / "orion_home")
        os.environ[DISABLE_PLUGINS_ENV] = "1"
        reset_kernel_for_tests()
        telemetry.reset_log_cursor_cache()
        telemetry.invalidate_guard_probe_cache()

    def tearDown(self) -> None:
        reset_kernel_for_tests()
        telemetry.reset_log_cursor_cache()
        telemetry.invalidate_guard_probe_cache()
        if self._old_home is None:
            os.environ.pop("ACEROLA_ORION_HOME", None)
        else:
            os.environ["ACEROLA_ORION_HOME"] = self._old_home
        if self._old_disable is None:
            os.environ.pop(DISABLE_PLUGINS_ENV, None)
        else:
            os.environ[DISABLE_PLUGINS_ENV] = self._old_disable
        self._tmp.cleanup()

    def test_01_cli_plugin_dispatch_and_bare_core_rejects_shift(self) -> None:
        # Bare core must reject unknown / removed subcommands such as `shift`
        with self.assertRaises(SystemExit):
            cli_main.build_parser().parse_args(["shift", "prod"])
        with self.assertRaises(SystemExit):
            cli_main.build_parser().parse_args(["ext-ping"])

        # Register a faulty plugin before a healthy plugin to confirm fault isolation
        class _FaultyPlugin:
            name = "faulty_cli"

            def register_cli(self, subparsers) -> None:
                raise RuntimeError("register_cli boom")

            def handle_cli(self, cmd: str, args) -> int | None:
                raise RuntimeError("handle_cli boom")

        demo = _DemoCliPlugin()
        kernel = get_kernel()
        kernel.register(_FaultyPlugin())
        kernel.register(demo)

        buf = io.StringIO()
        with self.assertLogs("acerola_orion.core.kernel", level=logging.WARNING):
            with redirect_stdout(buf):
                rc = cli_main.main(["ext-ping", "--label", "alpha", "--rc", "7"])
        self.assertEqual(rc, 7)
        self.assertEqual(demo.handled, [("ext-ping", "alpha")])
        self.assertIn("EXT_PING:alpha", buf.getvalue())

        # `shift` is still rejected because neither core nor demo plugin registered it
        with self.assertLogs("acerola_orion.core.kernel", level=logging.WARNING):
            with self.assertRaises(SystemExit):
                cli_main.build_parser().parse_args(["shift", "prod"])

    def test_02_deploy_launchers_merges_plugin_entries_and_blocks_traversal(self) -> None:
        # Bare core writes exactly 11 launchers
        bare_dir = self.tmp_path / "launchers_bare"
        bare_written = cli_main.deploy_agyrun_launchers(bare_dir)
        self.assertEqual(len(bare_written), 11)

        # With plugin registered, safe extra launchers are merged without overwriting core or escaping dir
        kernel = get_kernel()
        kernel.register(_DemoCliPlugin())
        ext_dir = self.tmp_path / "launchers_ext"
        ext_written = cli_main.deploy_agyrun_launchers(ext_dir)
        self.assertEqual(len(ext_written), 13)
        names = {Path(p).name for p in ext_written}
        self.assertIn("ext-demo.ps1", names)
        self.assertIn("ext-demo.cmd", names)
        self.assertNotIn("escape.ps1", names)
        self.assertFalse((self.tmp_path / "escape.ps1").exists())
        self.assertFalse((ext_dir / "nested").exists())

        # Core launcher content must remain intact
        core_status_txt = (ext_dir / "orion-status.ps1").read_text(encoding="utf-8-sig")
        self.assertNotIn("SHOULD_NOT_OVERWRITE_CORE", core_status_txt)
        self.assertIn("-m acerola_orion status", core_status_txt)
        # Plugin .ps1 launcher gets UTF-8 BOM
        self.assertTrue((ext_dir / "ext-demo.ps1").read_bytes().startswith(b"\xef\xbb\xbf"))
        self.assertTrue((ext_dir / "ext-demo.cmd").is_file())

    def test_03_telemetry_probe_guard_and_enrich_hooks_online_and_offline(self) -> None:
        bus = get_kernel().bus

        def _bad_hook(_arg) -> None:
            raise RuntimeError("plugin telemetry crash")

        def _on_probe_guard(view: telemetry.GuardStatusView) -> None:
            view.extensions["ext_guard"] = {"active": True, "mode": "watch"}

        def _on_enrich_telemetry(rep: telemetry.OrionTelemetryReport) -> None:
            rep.extensions["ext_probe"] = {
                "ls_running": rep.ls_running,
                "ls_pid": rep.ls_pid,
                "guard_seen": bool(rep.guard.extensions.get("ext_guard")),
                "nan_metric": math.nan,
            }

        bus.on("probe_guard", _bad_hook)
        bus.on("probe_guard", _on_probe_guard)
        bus.on("enrich_telemetry", _bad_hook)
        bus.on("enrich_telemetry", _on_enrich_telemetry)

        ls_log = self.tmp_path / "ls.log"
        main_log = self.tmp_path / "main.log"
        settings = self.tmp_path / "settings.json"
        ls_log.write_text("", encoding="utf-8")
        main_log.write_text("", encoding="utf-8")
        settings.write_text("{}", encoding="utf-8")

        offline_ls = rpc.ActiveLsState(running=False)
        with (
            patch.object(telemetry, "snapshot_processes_fast", return_value=[]),
            patch.object(telemetry, "inspect_active_ls", return_value=offline_ls),
            patch.object(telemetry, "get_ls_log_path", return_value=ls_log),
            patch.object(telemetry, "get_main_log_path", return_value=main_log),
            patch.object(telemetry, "get_user_settings_path", return_value=settings),
            patch.object(telemetry, "is_mutex_held", return_value=False),
            patch.object(telemetry.subprocess, "run", side_effect=OSError("skip schtasks")),
            self.assertLogs("acerola_orion.core.kernel", level=logging.WARNING),
        ):
            rep_off = telemetry.collect_telemetry(include_guard_check=True)

        self.assertEqual(rep_off.guard.extensions.get("ext_guard"), {"active": True, "mode": "watch"})
        self.assertFalse(rep_off.extensions["ext_probe"]["ls_running"])
        self.assertTrue(rep_off.extensions["ext_probe"]["guard_seen"])

        # Strict JSON serialization converts NaN inside extensions to null
        as_json = json.dumps(rep_off.to_dict(), allow_nan=False)
        parsed = json.loads(as_json)
        self.assertIsNone(parsed["extensions"]["ext_probe"]["nan_metric"])
        self.assertEqual(parsed["guard"]["extensions"]["ext_guard"]["mode"], "watch")

        # Online branch also emits enrich_telemetry after RPC calls
        online_ls = rpc.ActiveLsState(
            running=True,
            pid=7788,
            main_pids=[7700],
            all_ls_pids=[7788],
            hub=rpc.HubEndpoint(file_path="hub.json", pid=7788, http_port=55123, https_port=0, csrf_token="tok"),
        )

        class _FakeRpc:
            @classmethod
            def from_hub(cls, _hub):
                return cls()

            def get_user_status(self, timeout: float = 8.0):
                return {"userStatus": {"name": "Alice"}}

            def get_load_code_assist(self, timeout: float = 10.0):
                return {"response": {"paidTier": {"id": "pro", "name": "Pro"}}}

            def retrieve_user_quota_summary(self, timeout: float = 8.0):
                return {"response": {"groups": []}}

        with (
            patch.object(telemetry, "snapshot_processes_fast", return_value=[]),
            patch.object(telemetry, "inspect_active_ls", return_value=online_ls),
            patch.object(telemetry, "ConnectRpcClient", _FakeRpc),
            patch.object(telemetry, "get_ls_log_path", return_value=ls_log),
            patch.object(telemetry, "get_main_log_path", return_value=main_log),
            patch.object(telemetry, "get_user_settings_path", return_value=settings),
            self.assertLogs("acerola_orion.core.kernel", level=logging.WARNING),
        ):
            rep_on = telemetry.collect_telemetry(include_guard_check=False)

        self.assertTrue(rep_on.extensions["ext_probe"]["ls_running"])
        self.assertEqual(rep_on.extensions["ext_probe"]["ls_pid"], 7788)

    def test_04_cli_render_status_lines_hook(self) -> None:
        rep = telemetry.OrionTelemetryReport(
            captured_at="2026-11-21 16:00:00 +0800",
            ls_running=False,
            rpc_errors=["demo_rpc_warn"],
            extensions={"ext_panel": {"label": "READY"}},
        )
        # Bare core output has no extension section
        bare_txt = cli_render.render_telemetry_report(rep)
        self.assertNotIn("扩展探针", bare_txt)

        bus = get_kernel().bus
        bus.on("render_status_lines", lambda _r: (_ for _ in ()).throw(RuntimeError("render crash")))
        bus.on("render_status_lines", lambda _r: None)
        bus.on(
            "render_status_lines",
            lambda r: [
                "\n【5】 🔌 扩展探针状态 (Extensions)",
                f"  ● 状态标签       : {r.extensions.get('ext_panel', {}).get('label')}",
            ],
        )
        bus.on("render_status_lines", lambda _r: "  ● 附加单行       : OK")

        with self.assertLogs("acerola_orion.core.kernel", level=logging.WARNING):
            ext_txt = cli_render.render_telemetry_report(rep)

        self.assertIn("【5】 🔌 扩展探针状态 (Extensions)", ext_txt)
        self.assertIn("状态标签       : READY", ext_txt)
        self.assertIn("附加单行       : OK", ext_txt)
        # Extension lines appear before RPC warnings and bottom border
        self.assertLess(ext_txt.index("附加单行"), ext_txt.index("[RPC 告警]"))

    def test_05_cache_radar_enrich_hook_and_cached_tick_preservation(self) -> None:
        brain = self.tmp_path / "brain"
        ann = self.tmp_path / "annotations"
        ann.mkdir(parents=True)
        cid = "12345678-1234-4123-8123-123456789abc"
        now_ep = 1_800_000_000.0
        iso = datetime.fromtimestamp(now_ep - 30.0, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        logs_dir = brain / cid / ".system_generated" / "logs"
        logs_dir.mkdir(parents=True)
        (ann / f"{cid}.pbtxt").write_text('title: "Radar Hook Session"\n', encoding="utf-8")
        tp = logs_dir / "transcript.jsonl"
        tp.write_text(
            json.dumps(
                {
                    "step_index": 1,
                    "type": "PLANNER_RESPONSE",
                    "status": "DONE",
                    "created_at": iso,
                    "input_tokens": 5000,
                    "cache_read_tokens": 115000,
                    "output_tokens": 1000,
                }
            )
            + "\n",
            encoding="utf-8",
        )
        os.utime(tp, ns=(int((now_ep - 30.0) * 1_000_000_000), int((now_ep - 30.0) * 1_000_000_000)))

        calls = {"count": 0}

        def _on_enrich_radar(st: cache_radar.CacheRadarState) -> None:
            calls["count"] += 1
            st.extensions["demo_ext"] = {
                "seen_conv": st.conv_id,
                "ctx_label": st.context_label,
                "call_no": calls["count"],
            }

        get_kernel().bus.on("enrich_radar", _on_enrich_radar)

        sampler = cache_radar.CacheRadarSampler(brain_dir=brain, annotations_dir=ann)
        st = sampler.sample(now_epoch=now_ep)
        self.assertEqual(calls["count"], 1)
        self.assertEqual(st.extensions["demo_ext"]["seen_conv"], cid)
        self.assertEqual(st.to_dict()["extensions"]["demo_ext"]["call_no"], 1)

        # Pure in-memory tick preserves extensions without re-invoking hook
        st_tick = sampler.evaluate_cached(now_epoch=now_ep + 10.0)
        self.assertEqual(calls["count"], 1)
        self.assertEqual(st_tick.extensions["demo_ext"]["seen_conv"], cid)

        # CLI `radar --json` exposes `state.extensions`
        buf = io.StringIO()
        with patch.object(cache_radar, "get_agy_brain_dir", return_value=brain), redirect_stdout(buf):
            rc = cli_main.main(["radar", "--json"])
        self.assertEqual(rc, 0)
        radar_payload = json.loads(buf.getvalue())
        self.assertEqual(radar_payload["state"]["extensions"]["demo_ext"]["seen_conv"], cid)

    @unittest.skipUnless(sys.platform == "win32", "Windows daemon lifecycle hook test")
    def test_06_guillotine_lifecycle_hooks_start_ls_seen_tick_stop(self) -> None:
        events: list[tuple[str, dict]] = []
        bus: HookBus = get_kernel().bus

        def _on_start(ctx: dict) -> None:
            ctx["state"]["started_marker"] = True
            events.append(("guard_start", dict(ctx)))

        def _on_ls_seen(ctx: dict) -> None:
            events.append(("guard_ls_seen", {"ls_pid": ctx["ls_pid"], "hub_port": ctx["hub"].http_port if ctx["hub"] else 0}))

        def _on_tick(ctx: dict) -> None:
            events.append(("guard_tick", {"cycle": ctx["cycle"], "main_pids": list(ctx["main_pids"]), "ls_pids": list(ctx["ls_pids"])}))

        def _on_stop(ctx: dict) -> None:
            events.append(("guard_stop", {"cycles": ctx["cycles"], "had_marker": bool(ctx["state"].get("started_marker"))}))

        bus.on("guard_start", _on_start)
        bus.on("guard_ls_seen", _on_ls_seen)
        bus.on("guard_tick", lambda _ctx: (_ for _ in ()).throw(RuntimeError("tick hook boom")))
        bus.on("guard_tick", _on_tick)
        bus.on("guard_stop", _on_stop)

        procs_c1 = [
            rpc.RawProcEntry(pid=1000, ppid=500, name="Antigravity.exe"),
            rpc.RawProcEntry(pid=2000, ppid=1000, name="language_server_windows_x64.exe"),
        ]
        procs_c2 = procs_c1 + [
            rpc.RawProcEntry(pid=2001, ppid=1000, name="language_server_windows_x64.exe"),
        ]
        procs_c3: list[rpc.RawProcEntry] = []

        snap_calls = {"n": 0}

        def _fake_snaps():
            snap_calls["n"] += 1
            if snap_calls["n"] == 1:
                return procs_c1
            if snap_calls["n"] == 2:
                return procs_c2
            return procs_c3

        hub_map = {
            2000: rpc.HubEndpoint("hub_2000.json", 2000, 52000, 0, "tok", 1),
            2001: rpc.HubEndpoint("hub_2001.json", 2001, 52001, 0, "tok", 1),
        }

        def _fake_hub(lpid: int, live_pids=None):
            return hub_map.get(lpid)

        real_time = time

        class _FastTime:
            @staticmethod
            def time() -> float:
                return real_time.time()

            @staticmethod
            def monotonic() -> float:
                return real_time.monotonic()

            @staticmethod
            def sleep(_s: float) -> None:
                real_time.sleep(0)

        with (
            patch.object(guillotine, "time", _FastTime),
            patch.object(guillotine, "acquire_single_instance_mutex", return_value=(1234, False)),
            patch.object(guillotine, "release_and_close_mutex"),
            patch.object(guillotine, "snapshot_processes_fast", side_effect=_fake_snaps),
            patch.object(guillotine, "open_owned_language_server", side_effect=lambda pid, _e: 8000 + pid),
            patch.object(guillotine, "terminate_owned_handle", return_value=True),
            patch.object(guillotine, "discover_hub_endpoint", side_effect=_fake_hub),
            patch.object(guillotine, "collect_telemetry", return_value=telemetry.OrionTelemetryReport(captured_at="now", ls_running=False)),
            patch.object(guillotine, "open_process_handle", return_value=9999),
            patch.object(guillotine, "wait_for_handles_all", return_value=rpc.WAIT_TIMEOUT),
            patch.object(guillotine, "close_handle_safe"),
            self.assertLogs("acerola_orion.core.kernel", level=logging.WARNING),
        ):
            rc = guillotine.run_guillotine_daemon_loop(max_cycles=3)

        self.assertEqual(rc, 0)
        names = [e[0] for e in events]
        self.assertEqual(names[0], "guard_start")
        self.assertEqual(names[-1], "guard_stop")

        ls_seen = [e[1] for e in events if e[0] == "guard_ls_seen"]
        self.assertEqual(
            ls_seen,
            [
                {"ls_pid": 2000, "hub_port": 52000},
                {"ls_pid": 2001, "hub_port": 52001},
            ],
        )

        ticks = [e[1] for e in events if e[0] == "guard_tick"]
        self.assertEqual(len(ticks), 3)
        self.assertEqual(ticks[0]["main_pids"], [1000])
        self.assertEqual(ticks[1]["ls_pids"], [2000, 2001])
        self.assertEqual(ticks[2]["main_pids"], [])

        stop_evt = [e[1] for e in events if e[0] == "guard_stop"][0]
        self.assertEqual(stop_evt["cycles"], 3)
        self.assertTrue(stop_evt["had_marker"])


if __name__ == "__main__":
    unittest.main()
