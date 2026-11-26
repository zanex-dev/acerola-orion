# -*- coding: utf-8 -*-
"""
Unit tests for Acerola-Orion modular UI split (`acerola_orion.ui.*`),
`PanelPluginHost` extension slots, bare-core zero-footprint behavior,
and per-plugin fault isolation.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
import tempfile
import tkinter as tk
import unittest
from unittest.mock import patch

import acerola_orion.cache_radar as cache_radar
from acerola_orion.core import (
    DISABLE_PLUGINS_ENV,
    get_kernel,
    reset_kernel_for_tests,
)
import acerola_orion.ledger as ledger_mod
import acerola_orion.panel as panel_mod
import acerola_orion.telemetry as telemetry
import acerola_orion.ui as ui_mod


def _make_dummy_report(*, ls_running: bool = True, tier: str = "g1-pro-tier") -> telemetry.OrionTelemetryReport:
    return telemetry.OrionTelemetryReport(
        captured_at="2030-01-01 12:00:00",
        ls_running=ls_running,
        ls_pid=4242 if ls_running else 0,
        http_port=18080 if ls_running else 0,
        main_pids=[1001] if ls_running else [],
        ide_version="1.20.0" if ls_running else "",
        proxy_mode="direct",
        user_tier_id=tier,
        paid_tier_id=tier,
        paid_tier_name="Google AI Pro" if ls_running else "",
        quota_buckets=[],
        log_stats=telemetry.LogCongestionStats(),
        guard=telemetry.GuardStatusView(),
        stale_hub_files=0,
    )


class PanelUiSlotsTestSuite(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="orion_ui_slots_")
        self.tmp_path = Path(self._tmp.name)
        self._old_home = os.environ.get("ACEROLA_ORION_HOME")
        self._old_disable = os.environ.get(DISABLE_PLUGINS_ENV)
        os.environ["ACEROLA_ORION_HOME"] = str(self.tmp_path / "orion_home")
        os.environ[DISABLE_PLUGINS_ENV] = "1"
        reset_kernel_for_tests()
        self._apps: list[panel_mod.AcerolaOrionApp] = []

    def tearDown(self) -> None:
        for app in reversed(self._apps):
            try:
                app.exit_app()
            except Exception:
                pass
        self._apps.clear()
        reset_kernel_for_tests()
        if self._old_home is None:
            os.environ.pop("ACEROLA_ORION_HOME", None)
        else:
            os.environ["ACEROLA_ORION_HOME"] = self._old_home
        if self._old_disable is None:
            os.environ.pop(DISABLE_PLUGINS_ENV, None)
        else:
            os.environ[DISABLE_PLUGINS_ENV] = self._old_disable
        self._tmp.cleanup()

    def _create_app(self) -> panel_mod.AcerolaOrionApp:
        try:
            with patch.object(panel_mod.AcerolaOrionApp, "refresh_all_async", lambda self, silent=False: None):
                app = panel_mod.AcerolaOrionApp(
                    auto_refresh_ms=0,
                    enable_tray=False,
                    enable_hud=False,
                    initial_mode="full",
                )
        except tk.TclError as exc:
            self.skipTest(f"Tk display unavailable in headless environment: {exc}")
            raise
        self._apps.append(app)
        return app

    def test_01_compat_exports_line_budget_and_ui_clean_room(self) -> None:
        # Top-level compatibility exports from acerola_orion.panel
        required_exports = (
            "AcerolaOrionApp",
            "CacheRadarHudWindow",
            "AstrolabeDialWidget",
            "OrionChartWidget",
            "QuotaBarWidget",
            "clamp_hud_coords",
            "launch_panel",
            "PanelPluginHost",
        )
        for name in required_exports:
            self.assertTrue(hasattr(panel_mod, name), f"Missing export {name} on acerola_orion.panel")

        self.assertIs(panel_mod.CacheRadarHudWindow, ui_mod.CacheRadarHudWindow)
        self.assertIs(panel_mod.AstrolabeDialWidget, ui_mod.AstrolabeDialWidget)
        self.assertIs(panel_mod.OrionChartWidget, ui_mod.OrionChartWidget)
        self.assertIs(panel_mod.QuotaBarWidget, ui_mod.QuotaBarWidget)
        self.assertIs(panel_mod.clamp_hud_coords, ui_mod.clamp_hud_coords)
        self.assertIs(panel_mod.PanelPluginHost, ui_mod.PanelPluginHost)

        repo_root = Path(__file__).resolve().parent.parent
        backup_v1 = repo_root / "_versions" / "panel_v1.py"
        if backup_v1.is_file():
            self.assertGreater(len(backup_v1.read_text(encoding="utf-8").splitlines()), 3500)

        panel_path = repo_root / "acerola_orion" / "panel.py"
        panel_lines = len(panel_path.read_text(encoding="utf-8").splitlines())
        self.assertLessEqual(
            panel_lines,
            1800,
            f"panel.py must stay slim (<=1800 lines), currently {panel_lines} lines",
        )

        # Clean-room audit across panel.py and the entire acerola_orion/ui/ subpackage
        ui_files = sorted((repo_root / "acerola_orion" / "ui").glob("*.py"))
        self.assertGreaterEqual(len(ui_files), 4)
        forbidden = [
            "urllib." + "request",
            "import " + "requests",
            "import " + "httpx",
            "import " + "aiohttp",
            "state." + "vscdb",
            "CryptUnprotect" + "Data",
            "refresh_" + "token",
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
        ]
        for f in [panel_path] + ui_files:
            low = f.read_text(encoding="utf-8").lower()
            for tok in forbidden:
                self.assertNotIn(tok.lower(), low, f"Forbidden token {tok!r} found in {f.name}")

    def test_02_bare_core_zero_plugin_footprint(self) -> None:
        app = self._create_app()
        app.update_idletasks()

        self.assertEqual(set(app._full_tab_frames.keys()), {"hub", "ledger", "vault", "diag"})
        self.assertEqual(app.plugin_host.plugin_tab_keys, ())
        self.assertEqual(app.plugin_host.hub_action_panels, ())

        # Hub extension slot & header badge must remain unpacked (0px) when no plugin uses them
        self.assertEqual(app.hub_plugin_slot.winfo_manager(), "")
        self.assertEqual(app.lbl_header_badge.winfo_manager(), "")
        self.assertFalse(app._header_badge_packed)
        self.assertEqual(app.radar_canvas.find_withtag("plugin_radar_marker"), ())

        tray_items = app._build_tray_menu()
        self.assertIsNone(tray_items[-2])
        self.assertEqual(tray_items[-1][0], "✕ 彻底退出主控台")

    def test_03_dynamic_tabs_arity_lifecycle_and_validation(self) -> None:
        events: list[tuple[str, object]] = []

        class _TabsPlugin:
            name = "tabs_demo"

            def register_ui(self, host: panel_mod.PanelPluginHost) -> None:
                def build_tab_a(parent: tk.Frame, ui_host: panel_mod.PanelPluginHost) -> str:
                    tk.Label(parent, text="Ext Tab A").pack()
                    events.append(("build_a", ui_host is host))
                    return "built_a"

                def on_select_a(ui_host: panel_mod.PanelPluginHost) -> None:
                    events.append(("select_a", ui_host is host))

                def on_refresh_a(rep: telemetry.OrionTelemetryReport, ctx: dict) -> None:
                    events.append(("refresh_a", (rep.ls_pid, ctx["host"] is host)))

                def build_tab_b(parent: tk.Frame) -> None:
                    tk.Label(parent, text="Ext Tab B").pack()
                    events.append(("build_b", True))

                def on_select_b() -> None:
                    events.append(("select_b", True))

                def on_refresh_b(rep: telemetry.OrionTelemetryReport) -> None:
                    events.append(("refresh_b", rep.ls_pid))

                host.add_tab("ext_a", "🧩 扩展A", build_tab_a, on_select=on_select_a, on_refresh=on_refresh_a)
                host.add_tab("ext_b", "🔧 扩展B", build_tab_b, on_select=on_select_b, on_refresh=on_refresh_b)

        get_kernel().register(_TabsPlugin())
        app = self._create_app()
        app.update_idletasks()

        self.assertEqual(app.plugin_host.plugin_tab_keys, ("ext_a", "ext_b"))
        self.assertIn("ext_a", app._full_tab_frames)
        self.assertIn("ext_b", app._full_tab_frames)
        self.assertIn(("build_a", True), events)
        self.assertIn(("build_b", True), events)

        # Selecting dynamic tabs switches visibility and fires on_select
        app.select_full_tab("ext_a")
        self.assertEqual(app._active_full_tab, "ext_a")
        self.assertEqual(app._full_tab_frames["ext_a"].winfo_manager(), "pack")
        self.assertEqual(app._full_tab_frames["hub"].winfo_manager(), "")
        self.assertIn(("select_a", True), events)

        app.plugin_host.select_tab("ext_b")
        self.assertEqual(app._active_full_tab, "ext_b")
        self.assertIn(("select_b", True), events)

        # Telemetry refresh propagates to plugin tabs
        rep = _make_dummy_report()
        audit = ledger_mod.OrionLedger().compute_rate_audit()
        app._apply_ui_data(rep, [], [], [], audit, [], [], silent=True)
        self.assertIn(("refresh_a", (4242, True)), events)
        self.assertIn(("refresh_b", 4242), events)

        # Validation guards
        with self.assertRaises(ValueError):
            app.plugin_host.add_tab("hub", "冲突内置", lambda p: None)
        with self.assertRaises(ValueError):
            app.plugin_host.add_tab("ext_a", "冲突插件", lambda p: None)
        with self.assertRaises(ValueError):
            app.plugin_host.add_tab("   ", "空键", lambda p: None)
        with self.assertRaises(TypeError):
            app.plugin_host.add_tab("ext_c", "坏工厂", "not_callable")  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            app.plugin_host.add_tab("ext_c", "坏选择", lambda p: None, on_select="bad")  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            app.plugin_host.add_tab("ext_c", "坏刷新", lambda p: None, on_refresh="bad")  # type: ignore[arg-type]

    def test_04_hub_action_panel_slot_order_and_failure_rollback(self) -> None:
        # Case 1: Only failing hub action panel -> slot must roll back to unpacked (0px)
        class _FailHubPlugin:
            name = "fail_hub"

            def register_ui(self, host: panel_mod.PanelPluginHost) -> None:
                def _boom(_frame: tk.Frame) -> None:
                    raise RuntimeError("hub panel exploded")

                host.add_hub_action_panel(_boom)

        get_kernel().register(_FailHubPlugin())
        with self.assertLogs("acerola_orion.core.kernel", level=logging.WARNING):
            app_empty = self._create_app()
        app_empty.update_idletasks()
        self.assertEqual(app_empty.hub_plugin_slot.winfo_manager(), "")
        self.assertEqual(app_empty.plugin_host.hub_action_panels, ())
        app_empty.exit_app()
        self._apps.remove(app_empty)

        # Case 2: Multiple healthy hub action panels pack before hub_bottom_row
        reset_kernel_for_tests()

        class _OkHubPlugin:
            name = "ok_hub"

            def register_ui(self, host: panel_mod.PanelPluginHost) -> None:
                host.add_hub_action_panel(
                    lambda frame, h: tk.Label(frame, text=f"Strip1 scale={h.dpi_scale:.1f}").pack(side=tk.LEFT),
                    framed=True,
                )
                host.add_hub_action_panel(
                    lambda frame: tk.Label(frame, text="Strip2 raw").pack(side=tk.LEFT),
                    framed=False,
                )

        get_kernel().register(_OkHubPlugin())
        app_ok = self._create_app()
        app_ok.update_idletasks()

        self.assertEqual(app_ok.hub_plugin_slot.winfo_manager(), "pack")
        self.assertEqual(len(app_ok.plugin_host.hub_action_panels), 2)
        slaves = list(app_ok.tab_hub.pack_slaves())
        self.assertLess(slaves.index(app_ok.hub_plugin_slot), slaves.index(app_ok.hub_bottom_row))

        with self.assertRaises(TypeError):
            app_ok.plugin_host.add_hub_action_panel("bad")  # type: ignore[arg-type]

    def test_05_tray_items_static_dynamic_and_fault_isolation(self) -> None:
        invoked: list[str] = []

        class _TrayPlugin:
            name = "tray_demo"

            def register_ui(self, host: panel_mod.PanelPluginHost) -> None:
                host.add_tray_items(
                    [
                        ("⚡ 静态扩展动作", lambda: invoked.append("static")),
                        None,
                        ("malformed_no_callable",),  # type: ignore[list-item]
                    ]
                )

                def _bad_builder():
                    raise RuntimeError("tray builder exploded")

                host.add_tray_items(_bad_builder)
                host.add_tray_items(lambda: [("🔄 动态扩展动作", lambda: invoked.append("dynamic"))])

        get_kernel().register(_TrayPlugin())
        app = self._create_app()

        with self.assertLogs("acerola_orion.ui.plugin_host", level=logging.WARNING):
            menu_items = app._build_tray_menu()

        labels = [it[0] for it in menu_items if it is not None]
        self.assertIn("⚡ 静态扩展动作", labels)
        self.assertIn("🔄 动态扩展动作", labels)
        self.assertEqual(labels[-1], "✕ 彻底退出主控台")

        for item in menu_items:
            if item is not None and item[0] in ("⚡ 静态扩展动作", "🔄 动态扩展动作"):
                item[1]()
        self.assertEqual(invoked, ["static", "dynamic"])

        with self.assertRaises(TypeError):
            app.plugin_host.add_tray_items("invalid_string")  # type: ignore[arg-type]

    def test_06_radar_markers_and_header_badge_slots(self) -> None:
        badge_state = {"visible": True}

        class _DecorPlugin:
            name = "decor_demo"

            def register_ui(self, host: panel_mod.PanelPluginHost) -> None:
                host.add_radar_markers(
                    lambda _state: [
                        {"seconds": 125.0, "color": "#22d3ee", "width": 2, "dash": (3, 2), "tag": "half_mark"},
                        (0.8, "#f59e0b", 2.0),
                        0.3,
                        {"fraction": 0.0},
                        {"fraction": 1.0},
                        {"fraction": float("nan")},
                        True,
                    ]
                )
                host.add_radar_markers(lambda _state: (_ for _ in ()).throw(RuntimeError("marker crash")))

                host.format_header_badge(
                    lambda _rep: (
                        {"text": "EXT-A", "fg": "#10b981", "bg": "#064e3b"} if badge_state["visible"] else None
                    )
                )
                host.format_header_badge(lambda _rep: (_ for _ in ()).throw(RuntimeError("badge crash")))
                host.format_header_badge(lambda _rep: "EXT-B" if badge_state["visible"] else "")

        get_kernel().register(_DecorPlugin())
        with self.assertLogs("acerola_orion.ui.plugin_host", level=logging.WARNING):
            app = self._create_app()
            app.radar_canvas.configure(width=300, height=8)
            app.update_idletasks()

        # Verify radar markers collection & rendering with fault isolation
        with self.assertLogs("acerola_orion.ui.plugin_host", level=logging.WARNING):
            markers = app.plugin_host.collect_radar_markers(cache_radar.CacheRadarState())
        self.assertEqual(len(markers), 3)
        self.assertAlmostEqual(markers[0]["fraction"], 0.5, places=4)
        self.assertEqual(markers[0]["tag"], "half_mark")
        self.assertEqual(markers[0]["dash"], (3, 2))
        self.assertAlmostEqual(markers[1]["fraction"], 0.8, places=4)
        self.assertAlmostEqual(markers[2]["fraction"], 0.3, places=4)

        with self.assertLogs("acerola_orion.ui.plugin_host", level=logging.WARNING):
            app._redraw_radar_bar(allow_defer=False)
        self.assertEqual(len(app.radar_canvas.find_withtag("plugin_radar_marker")), 3)
        self.assertEqual(len(app.radar_canvas.find_withtag("half_mark")), 1)

        # Verify header badge composition & dynamic hide/show
        with self.assertLogs("acerola_orion.ui.plugin_host", level=logging.WARNING):
            app.plugin_host.refresh_header_badge()
        self.assertTrue(app._header_badge_packed)
        self.assertEqual(app.lbl_header_badge.cget("text"), "EXT-A  ·  EXT-B")
        self.assertEqual(app.lbl_header_badge.cget("fg"), "#10b981")

        # 2-arg transforming formatter overrides accumulated text
        with self.assertLogs("acerola_orion.ui.plugin_host", level=logging.WARNING):
            app.plugin_host.format_header_badge(lambda _rep, cur: f"[{cur}]" if cur else None)
            app.plugin_host.refresh_header_badge()
        self.assertEqual(app.lbl_header_badge.cget("text"), "[EXT-A  ·  EXT-B]")

        # When all formatters return empty/None, badge unpacks cleanly
        badge_state["visible"] = False
        with self.assertLogs("acerola_orion.ui.plugin_host", level=logging.WARNING):
            app.plugin_host.refresh_header_badge()
        self.assertFalse(app._header_badge_packed)
        self.assertEqual(app.lbl_header_badge.winfo_manager(), "")

        with self.assertRaises(TypeError):
            app.plugin_host.add_radar_markers("bad")  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            app.plugin_host.format_header_badge("bad")  # type: ignore[arg-type]

    def test_07_multi_plugin_fault_isolation_end_to_end(self) -> None:
        healthy_calls: list[str] = []

        class _BrokenMountPlugin:
            name = "broken_mount"

            def register_ui(self, host: panel_mod.PanelPluginHost) -> None:
                def _exploding_factory(_parent: tk.Frame) -> None:
                    raise RuntimeError("tab build crashed")

                host.add_tab("broken_tab", "💥 崩溃页", _exploding_factory)

        class _CrashingLifecyclePlugin:
            name = "crashing_lifecycle"

            def register_ui(self, host: panel_mod.PanelPluginHost) -> None:
                host.add_tab(
                    "crash_life",
                    "⚠️ 生命周期异常页",
                    lambda parent: tk.Label(parent, text="Loaded").pack(),
                    on_select=lambda: (_ for _ in ()).throw(RuntimeError("on_select crash")),
                    on_refresh=lambda _rep: (_ for _ in ()).throw(RuntimeError("on_refresh crash")),
                )

        class _HealthyPlugin:
            name = "healthy_ext"

            def register_ui(self, host: panel_mod.PanelPluginHost) -> None:
                host.add_tab(
                    "healthy_tab",
                    "✅ 正常扩展页",
                    lambda parent: tk.Label(parent, text="Healthy").pack(),
                    on_select=lambda: healthy_calls.append("select"),
                    on_refresh=lambda _rep: healthy_calls.append("refresh"),
                )
                host.add_hub_action_panel(lambda parent: tk.Label(parent, text="Healthy Hub").pack())
                host.add_tray_items([("✅ 正常托盘", lambda: healthy_calls.append("tray"))])
                host.add_radar_markers(lambda _st: [{"fraction": 0.45, "tag": "healthy_mark"}])
                host.format_header_badge(lambda _rep: "HEALTHY")

        k = get_kernel()
        k.register(_BrokenMountPlugin())
        k.register(_CrashingLifecyclePlugin())
        k.register(_HealthyPlugin())

        with self.assertLogs("acerola_orion.core.kernel", level=logging.WARNING):
            app = self._create_app()
        app.update_idletasks()

        # BrokenMountPlugin's tab must not pollute _full_tab_frames or _full_nav_buttons
        self.assertNotIn("broken_tab", app._full_tab_frames)
        self.assertNotIn("broken_tab", app._full_nav_buttons)

        # HealthyPlugin mounted completely despite earlier plugin crashing
        self.assertIn("healthy_tab", app._full_tab_frames)
        self.assertIn("crash_life", app._full_tab_frames)
        self.assertEqual(len(app.plugin_host.hub_action_panels), 1)
        self.assertEqual(app.lbl_header_badge.cget("text"), "HEALTHY")

        # Crashing on_select must not break tab selection
        with self.assertLogs("acerola_orion.ui.plugin_host", level=logging.WARNING):
            app.select_full_tab("crash_life")
        self.assertEqual(app._active_full_tab, "crash_life")

        app.select_full_tab("healthy_tab")
        self.assertEqual(app._active_full_tab, "healthy_tab")
        self.assertIn("select", healthy_calls)

        # Crashing on_refresh must not prevent HealthyPlugin.on_refresh from running
        rep = _make_dummy_report()
        audit = ledger_mod.OrionLedger().compute_rate_audit()
        with self.assertLogs("acerola_orion.ui.plugin_host", level=logging.WARNING):
            app._apply_ui_data(rep, [], [], [], audit, [], [], silent=True)
        self.assertIn("refresh", healthy_calls)

    def test_radar_slots_zero_footprint_without_plugins(self) -> None:
        app = self._create_app()
        app.update_idletasks()

        self.assertEqual(app.radar_plugin_actions.winfo_manager(), "")
        self.assertEqual(app.radar_tier_plugin_actions.winfo_manager(), "")
        self.assertEqual(app.plugin_host.radar_action_widgets, [])
        self.assertEqual(app.plugin_host.radar_tier_action_widgets, [])
        self.assertEqual(app.plugin_host.collect_radar_menu_items(), [])

        # Calling flash_hud_toast without an active HUD must be a silent no-op
        app.plugin_host.flash_hud_toast("No HUD attached", duration_sec=1.2)

    def test_radar_action_and_tier_widgets_lazy_pack_and_rollback(self) -> None:
        app = self._create_app()
        host = app.plugin_host

        with self.assertRaises(RuntimeError):
            host.add_radar_action_widget(lambda _c: (_ for _ in ()).throw(RuntimeError("boom")))
        self.assertEqual(app.radar_plugin_actions.winfo_manager(), "")
        self.assertEqual(host.radar_action_widgets, [])

        with self.assertRaises(RuntimeError):
            host.add_radar_tier_action_widget(lambda _c, _h: (_ for _ in ()).throw(RuntimeError("tier boom")))
        self.assertEqual(app.radar_tier_plugin_actions.winfo_manager(), "")
        self.assertEqual(host.radar_tier_action_widgets, [])

        received_hosts = []

        def _build_two_arg(parent: tk.Frame, h: panel_mod.PanelPluginHost) -> None:
            received_hosts.append(h)
            tk.Label(parent, text="Act1").pack()

        host.add_radar_action_widget(_build_two_arg)
        host.add_radar_action_widget(lambda parent: tk.Label(parent, text="Act2").pack())
        host.add_radar_tier_action_widget(lambda parent, _h: tk.Label(parent, text="TierAct").pack())
        app.update_idletasks()

        self.assertEqual(received_hosts, [host])
        self.assertEqual(app.radar_plugin_actions.winfo_manager(), "pack")
        self.assertEqual(app.radar_tier_plugin_actions.winfo_manager(), "pack")
        self.assertEqual(len(host.radar_action_widgets), 2)
        self.assertEqual(len(host.radar_tier_action_widgets), 1)

        r_ctrl = app.btn_target_switch.master
        r_slaves = r_ctrl.pack_slaves()
        self.assertLess(r_slaves.index(app.radar_plugin_actions), r_slaves.index(app.btn_target_switch))

        tier_slaves = app.radar_tier_bar.pack_slaves()
        self.assertEqual(tier_slaves.index(app.radar_tier_plugin_actions), tier_slaves.index(app.btn_copy_compact) + 1)
        self.assertEqual(app.radar_tier_plugin_actions.pack_info().get("side"), "right")

    def test_radar_menu_items_shared_with_target_menu_and_hud_and_toast(self) -> None:
        from unittest import mock

        from acerola_orion.ui.hud import CacheRadarHudWindow
        from acerola_orion.ui.radar_controller import popup_target_menu

        app = self._create_app()
        host = app.plugin_host
        invoked = []

        host.add_radar_menu_items([("⚡ 静态雷达项", lambda: invoked.append("static"))])
        host.add_radar_menu_items(lambda: [("🛰️ 动态雷达项", lambda: invoked.append("dynamic"))])
        host.add_radar_menu_items(lambda: (_ for _ in ()).throw(RuntimeError("menu builder crash")))

        with self.assertLogs("acerola_orion.ui.plugin_host", level=logging.WARNING):
            items = host.collect_radar_menu_items()
        self.assertEqual([it[0] for it in items if it is not None], ["⚡ 静态雷达项", "🛰️ 动态雷达项"])

        with mock.patch.object(tk.Menu, "tk_popup", lambda *_a, **_kw: None):
            with self.assertLogs("acerola_orion.ui.plugin_host", level=logging.WARNING):
                popup_target_menu(app)

        target_labels = [
            app._target_menu.entrycget(i, "label")
            for i in range((app._target_menu.index("end") or -1) + 1)
            if app._target_menu.type(i) == "command"
        ]
        self.assertIn("⚡ 静态雷达项", target_labels)
        self.assertIn("🛰️ 动态雷达项", target_labels)

        hud = CacheRadarHudWindow(
            master=app,
            dpi_scale=1.0,
            initial_pos=None,
            on_drag_end=lambda _x, _y: None,
            on_select_mode=lambda _m: None,
            on_open_panel=lambda: None,
            on_copy_compact=lambda: None,
            on_select_target=lambda _m, _c: None,
            extra_menu_provider=host.collect_radar_menu_items,
        )
        try:
            app._hud_win = hud
            with self.assertLogs("acerola_orion.ui.plugin_host", level=logging.WARNING):
                hud._rebuild_ctx_menu()
            hud_labels = [
                hud._ctx_menu.entrycget(i, "label")
                for i in range((hud._ctx_menu.index("end") or -1) + 1)
                if hud._ctx_menu.type(i) == "command"
            ]
            self.assertIn("⚡ 静态雷达项", hud_labels)
            self.assertIn("🛰️ 动态雷达项", hud_labels)
            # Injected right below copy_compact at index 0
            self.assertEqual(hud_labels[1], "⚡ 静态雷达项")
            self.assertEqual(hud_labels[2], "🛰️ 动态雷达项")

            host.flash_hud_toast("Radar Toast Ready", duration_sec=2.0)
            self.assertEqual(hud._toast_text, "Radar Toast Ready")
            self.assertGreater(hud._toast_until_mono, 0.0)
        finally:
            app._hud_win = None
            hud.destroy()

    def test_radar_state_changed_arity_isolation_and_detail_suffix_cache(self) -> None:
        import tempfile
        from pathlib import Path

        from acerola_orion.cache_radar import CacheRadarSampler, evaluate_cache_radar_state
        from acerola_orion.ui.radar_controller import apply_radar_state_to_ui

        app = self._create_app()
        host = app.plugin_host
        seen = []

        host.on_radar_state_changed(lambda: seen.append(("zero",)))
        host.on_radar_state_changed(lambda st: seen.append(("one", st.phase)))
        host.on_radar_state_changed(lambda st, h: seen.append(("two", st.phase, h is host)))
        host.on_radar_state_changed(lambda _st: (_ for _ in ()).throw(RuntimeError("listener crash")))

        sample_state = evaluate_cache_radar_state(None, now_epoch=1000.0)
        with self.assertLogs("acerola_orion.ui.plugin_host", level=logging.WARNING):
            apply_radar_state_to_ui(app, sample_state)

        self.assertIn(("zero",), seen)
        self.assertIn(("one", sample_state.phase), seen)
        self.assertIn(("two", sample_state.phase, True), seen)

        suffix = " · [plugin-suffix]"

        def _enrich(st) -> None:
            st.detail_text = f"{st.detail_text}{suffix}"
            st.extensions["sample_flag"] = True

        bus = get_kernel().bus
        bus.on("enrich_radar", _enrich)
        with tempfile.TemporaryDirectory() as tmpdir:
            sampler = CacheRadarSampler(brain_dir=Path(tmpdir))
            st1 = sampler.sample(now_epoch=2000.0)
            self.assertTrue(st1.detail_text.endswith(suffix))
            self.assertEqual(sampler._cached_detail_suffix, suffix)

            st2 = sampler.evaluate_cached(now_epoch=2001.0)
            self.assertTrue(st2.detail_text.endswith(suffix))
            self.assertTrue(st2.extensions.get("sample_flag"))

            sampler._cached_extensions_conv_id = "different-conv-id"
            st3 = sampler.evaluate_cached(now_epoch=2002.0)
            self.assertFalse(st3.detail_text.endswith(suffix))
            self.assertNotIn("sample_flag", st3.extensions)

    def test_10_compact_tab_title_helper(self) -> None:
        self.assertEqual(panel_mod._compact_tab_title("🔭 双星盘总览 (Astrolabe)"), "🔭 双星盘总览")
        self.assertEqual(panel_mod._compact_tab_title("🧊 版本冷库 (Archive)"), "🧊 版本冷库")
        self.assertEqual(panel_mod._compact_tab_title("🧩 插件中心 (Foo/Bar-2.0 & Co.)"), "🧩 插件中心")
        self.assertEqual(panel_mod._compact_tab_title("(Archive)"), "(Archive)")
        self.assertEqual(panel_mod._compact_tab_title("版本冷库 (中文括号)"), "版本冷库 (中文括号)")
        self.assertEqual(panel_mod._compact_tab_title("纯中文标题"), "纯中文标题")

    def test_11_full_nav_bar_responsive_tiers_and_no_clipping(self) -> None:
        app = self._create_app()
        host = app.plugin_host
        host.add_tab("tasks", "🧭 任务接力板 (Task Board)", lambda parent: tk.Label(parent, text="tasks"))
        host.add_tab("archive", "🧊 版本冷库 (Archive)", lambda parent: tk.Label(parent, text="archive"))

        self.assertEqual(len(app._full_nav_order), 6)
        self.assertEqual(host._tabs["archive"]["title"], "🧊 版本冷库 (Archive)")
        archive_btn = app._full_nav_buttons["archive"]

        # 1) Wide window: full bilingual single row
        mode_wide = app._relayout_full_nav_bar(avail_w=1800)
        self.assertEqual(mode_wide, "full")
        self.assertEqual(archive_btn.cget("text"), "🧊 版本冷库 (Archive)")
        app.update_idletasks()
        self.assertEqual(app._full_nav_row_frames, [])
        self.assertEqual(
            {int(app._full_nav_buttons[k].grid_info()["row"]) for k in app._full_nav_order},
            {0},
        )

        # 2) Default/narrow window (e.g. 960~1160px): compact Chinese single row, no clipping
        mode_narrow = app._relayout_full_nav_bar(avail_w=1040)
        self.assertIn(mode_narrow, ("compact", "compact_sm"))
        self.assertEqual(archive_btn.cget("text"), "🧊 版本冷库")
        app.update_idletasks()
        self.assertEqual(app._full_nav_row_frames, [])
        self.assertLessEqual(app._full_nav_bar.winfo_reqwidth(), 1040)

        # 3) Very narrow window: multi-row wrap with every row fitting inside avail_w
        mode_wrap = app._relayout_full_nav_bar(avail_w=520)
        self.assertEqual(mode_wrap, "wrap")
        self.assertEqual(archive_btn.cget("text"), "🧊 版本冷库")
        app.update_idletasks()
        self.assertGreaterEqual(len(app._full_nav_row_frames), 2)
        for rf in app._full_nav_row_frames:
            self.assertLessEqual(rf.winfo_reqwidth(), 520)
        for k in app._full_nav_order:
            self.assertEqual(app._full_nav_buttons[k].winfo_manager(), "grid")

        # 4) Expanding back to wide restores full bilingual single row cleanly
        mode_back = app._relayout_full_nav_bar(avail_w=1800)
        self.assertEqual(mode_back, "full")
        self.assertEqual(archive_btn.cget("text"), "🧊 版本冷库 (Archive)")
        self.assertEqual(app._full_nav_row_frames, [])


if __name__ == "__main__":
    unittest.main()
