# -*- coding: utf-8 -*-
"""
Unit & Integration Tests for `acerola_orion.plugins.eoq_guard` & `EoqPlugin` (`tests/test_plugin_eoq_guard.py`)
- 纯临时文件 (`EOQ_GUARD_STATE`) 隔离测试，绝不触碰 `~/.gemini/config/` 线上状态
- 覆盖：原子读写与 Windows PermissionError 重试、mtime 缓存、`format_eoq_radar_hint` 全分档、
  `enrich_radar` 独立内核挂载、5 处 UI 入口联动、`conv_id` 优先级、纯 stdlib 导入隔离
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from typing import Any
import unittest
from unittest import mock

_TESTS_DIR = Path(__file__).resolve().parent
_PUB_ROOT = _TESTS_DIR.parent
if str(_PUB_ROOT) not in sys.path:
    sys.path.insert(0, str(_PUB_ROOT))

from acerola_orion.cache_radar import (  # noqa: E402
    PHASE_ACTIVE,
    PHASE_COLD,
    PHASE_EMPTY,
    PHASE_HOT,
    PHASE_WARNING,
    CacheRadarState,
)
from acerola_orion.core.kernel import (  # noqa: E402
    HookEvents,
    OrionKernel,
    get_kernel,
    reset_kernel_for_tests,
)
from acerola_orion.panel import AcerolaOrionApp  # noqa: E402
from acerola_orion.plugins import eoq_guard  # noqa: E402
from acerola_orion.plugins.eoq_plugin import (  # noqa: E402
    EOQ_EXT_KEY,
    MENU_LABEL_LOCK,
    MENU_LABEL_UNLOCK,
    RADAR_BTN_LOCKED,
    RADAR_BTN_UNLOCKED,
    TIER_BTN_LOCKED,
    TIER_BTN_UNLOCKED,
    EoqPlugin,
)


class EoqGuardPluginTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="orion_eoq_pub_test_")
        self.tmp_path = Path(self._tmp.name)
        self.state_file = self.tmp_path / ".eoq_guard_state.json"
        self._old_env = os.environ.get("EOQ_GUARD_STATE")
        os.environ["EOQ_GUARD_STATE"] = str(self.state_file)
        eoq_guard.invalidate_eoq_guard_cache()

    def tearDown(self) -> None:
        eoq_guard.invalidate_eoq_guard_cache()
        if self._old_env is None:
            os.environ.pop("EOQ_GUARD_STATE", None)
        else:
            os.environ["EOQ_GUARD_STATE"] = self._old_env
        self._tmp.cleanup()

    def test_01_atomic_read_write_preserves_fields_and_retries_permission_error(self) -> None:
        self.assertEqual(eoq_guard.get_eoq_guard_state_path(), self.state_file)
        cid = "11111111-2222-3333-4444-555555555555"
        self.state_file.write_text(
            json.dumps(
                {
                    "conversations": {
                        cid: {
                            "prompt_tokens": 185000,
                            "updated_at": 1700000000.0,
                            "pending_cliff_prompts": ["p1"],
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        self.assertFalse(eoq_guard.is_eoq_window_unrestricted(cid))

        real_replace = os.replace
        calls = {"n": 0}

        def _flaky_replace(src: Any, dst: Any) -> None:
            calls["n"] += 1
            if calls["n"] <= 2:
                raise PermissionError("simulated win32 lock")
            real_replace(src, dst)

        with mock.patch("acerola_orion.plugins.eoq_guard.os.replace", side_effect=_flaky_replace):
            ok, msg = eoq_guard.set_eoq_window_unrestricted(cid, True, source="panel_test")

        self.assertTrue(ok)
        self.assertEqual(calls["n"], 3)
        self.assertIn("11111111", msg)
        self.assertIn("~185K", msg)
        self.assertTrue(eoq_guard.is_eoq_window_unrestricted(cid))
        # No stray temp files left behind
        self.assertEqual([p.name for p in self.tmp_path.iterdir()], [".eoq_guard_state.json"])

        raw = json.loads(self.state_file.read_text(encoding="utf-8"))
        rec = raw["conversations"][cid]
        self.assertTrue(rec["unrestricted"])
        self.assertEqual(rec["unrestricted_source"], "panel_test")
        self.assertGreater(rec["unrestricted_at"], 0.0)
        self.assertFalse(rec["unrestricted_notified"])
        self.assertEqual(rec["pending_cliff_prompts"], [])
        self.assertEqual(rec["prompt_tokens"], 185000)

        # Lock back
        ok2, msg2 = eoq_guard.set_eoq_window_unrestricted(cid, False, source="panel_test")
        self.assertTrue(ok2)
        self.assertIn("已恢复窗口 11111111", msg2)
        self.assertFalse(eoq_guard.is_eoq_window_unrestricted(cid))
        rec2 = json.loads(self.state_file.read_text(encoding="utf-8"))["conversations"][cid]
        self.assertFalse(rec2["unrestricted"])
        self.assertEqual(rec2["unrestricted_source"], "")
        self.assertEqual(rec2["unrestricted_at"], 0.0)

    def test_02_mtime_cache_and_fallback_resolution(self) -> None:
        cid_old = "aaaaaaaa-0000-0000-0000-000000000001"
        cid_new = "bbbbbbbb-0000-0000-0000-000000000002"
        self.state_file.write_text(
            json.dumps(
                {
                    "conversations": {
                        cid_old: {"unrestricted": False, "updated_at": 100.0},
                        cid_new: {"unrestricted": True, "updated_at": 200.0, "unrestricted_at": 210.0},
                    }
                }
            ),
            encoding="utf-8",
        )

        with mock.patch(
            "acerola_orion.plugins.eoq_guard._read_json_dict_raw",
            wraps=eoq_guard._read_json_dict_raw,
        ) as spy:
            self.assertTrue(eoq_guard.is_eoq_window_unrestricted(cid_new))
            self.assertFalse(eoq_guard.is_eoq_window_unrestricted(cid_old))
            self.assertEqual(spy.call_count, 1)

        # External modification invalidates mtime/size cache
        self.state_file.write_text(
            json.dumps(
                {
                    "conversations": {
                        cid_old: {"unrestricted": True, "updated_at": 300.0},
                        cid_new: {"unrestricted": False, "updated_at": 200.0},
                    }
                }
            ),
            encoding="utf-8",
        )
        self.assertTrue(eoq_guard.is_eoq_window_unrestricted(cid_old))
        self.assertFalse(eoq_guard.is_eoq_window_unrestricted(cid_new))
        self.assertEqual(eoq_guard.get_latest_eoq_conv_id(), cid_old)

        # Empty state returns (False, ...) cleanly
        self.state_file.unlink()
        ok_empty, msg_empty = eoq_guard.set_eoq_window_unrestricted("", True)
        self.assertFalse(ok_empty)
        self.assertIn("未找到活跃会话窗口", msg_empty)

    def test_03_format_eoq_radar_hint_tiers(self) -> None:
        self.assertEqual(
            eoq_guard.format_eoq_radar_hint(CacheRadarState(phase=PHASE_HOT, context_tokens=50_000), True),
            "🔓 本窗免限中 (免200K锁/冷窗拦截)",
        )
        self.assertEqual(
            eoq_guard.format_eoq_radar_hint(CacheRadarState(phase=PHASE_EMPTY, context_tokens=0), False),
            "",
        )
        self.assertEqual(
            eoq_guard.format_eoq_radar_hint(CacheRadarState(phase=PHASE_COLD, context_tokens=29_999), False),
            "",
        )
        cold_hint = eoq_guard.format_eoq_radar_hint(CacheRadarState(phase=PHASE_COLD, context_tokens=30_000), False)
        self.assertIn("冷窗", cold_hint)
        self.assertIn("30K", cold_hint)

        self.assertEqual(
            eoq_guard.format_eoq_radar_hint(CacheRadarState(phase=PHASE_HOT, context_tokens=94_999), False),
            "",
        )
        soft_hint = eoq_guard.format_eoq_radar_hint(CacheRadarState(phase=PHASE_ACTIVE, context_tokens=95_000), False)
        self.assertIn("95K~120K", soft_hint)
        soft_top = eoq_guard.format_eoq_radar_hint(CacheRadarState(phase=PHASE_WARNING, context_tokens=120_000), False)
        self.assertIn("95K~120K", soft_top)

        hard_hint = eoq_guard.format_eoq_radar_hint(CacheRadarState(phase=PHASE_HOT, context_tokens=120_001), False)
        self.assertIn("120K", hard_hint)
        self.assertIn("收口", hard_hint)

    def test_04_independent_kernel_mount_and_enrich_radar_hook(self) -> None:
        kernel = OrionKernel()
        plugin = EoqPlugin()
        self.assertEqual(plugin.name, "eoq_guard")
        self.assertEqual(plugin.extra_launchers("py", "pyw", self.tmp_path), {})
        kernel.register(plugin)
        self.assertEqual(kernel.load_errors, ())

        cid = "cccccccc-1111-2222-3333-444444444444"
        st = CacheRadarState(
            phase=PHASE_HOT,
            conv_id=cid,
            context_tokens=110_000,
            detail_text="基础雷达明细",
        )
        kernel.bus.emit(HookEvents.ENRICH_RADAR, st)
        self.assertIn(EOQ_EXT_KEY, st.extensions)
        self.assertFalse(st.extensions[EOQ_EXT_KEY]["unrestricted"])
        self.assertIn("95K~120K", st.detail_text)
        self.assertTrue(st.detail_text.startswith("基础雷达明细 | "))

        # Enable unrestricted and re-enrich same state -> replaces old hint cleanly without duplication
        eoq_guard.set_eoq_window_unrestricted(cid, True, source="hook_test")
        kernel.bus.emit(HookEvents.ENRICH_RADAR, st)
        self.assertTrue(st.extensions[EOQ_EXT_KEY]["unrestricted"])
        self.assertIn(eoq_guard.EOQ_UNRESTRICTED_HINT, st.detail_text)
        self.assertNotIn("95K~120K", st.detail_text)

    def test_05_five_ui_entry_points_sync_and_conv_id_priority(self) -> None:
        cid_radar = "dddddddd-1111-2222-3333-444444444444"
        cid_pinned = "eeeeeeee-1111-2222-3333-444444444444"
        cid_latest = "ffffffff-1111-2222-3333-444444444444"
        self.state_file.write_text(
            json.dumps(
                {
                    "conversations": {
                        cid_latest: {"unrestricted": False, "updated_at": time.time(), "prompt_tokens": 98000},
                    }
                }
            ),
            encoding="utf-8",
        )

        # Mount ONLY EoqPlugin on a fresh kernel & real panel window (with HUD enabled)
        old_disable = os.environ.get("ACEROLA_ORION_DISABLE_PLUGINS")
        os.environ["ACEROLA_ORION_DISABLE_PLUGINS"] = "1"
        reset_kernel_for_tests()
        plugin = EoqPlugin()
        kernel = get_kernel()
        kernel.discover_plugins()
        kernel.register(plugin)
        app = AcerolaOrionApp(auto_refresh_ms=0, enable_tray=False, enable_hud=True)
        try:
            app.withdraw()
            app._cache_radar = None  # Pure in-memory state verification without touching local brain dir

            # Record console & HUD toast emissions
            toasts = []
            logs = []
            orig_flash = app._plugin_host.flash_hud_toast
            orig_log = app._plugin_host.append_console

            def _capture_toast(text: str, duration_sec: float = 1.8) -> None:
                toasts.append(text)
                orig_flash(text, duration_sec=duration_sec)

            def _capture_log(line: str) -> None:
                logs.append(line)
                orig_log(line)

            app._plugin_host.flash_hud_toast = _capture_toast  # type: ignore[method-assign]
            app._plugin_host.append_console = _capture_log  # type: ignore[method-assign]

            # Verify conv_id resolution priority: state.conv_id -> app._pinned_conv_id -> latest in state file
            app._radar_state = CacheRadarState(phase=PHASE_EMPTY, conv_id="")
            app._pinned_conv_id = ""
            self.assertEqual(plugin._resolve_active_conv_id(), cid_latest)

            app._pinned_conv_id = cid_pinned
            self.assertEqual(plugin._resolve_active_conv_id(), cid_pinned)

            st_2x = CacheRadarState(
                phase=PHASE_HOT,
                conv_id=cid_radar,
                context_tokens=205_000,
                context_label="205K",
                is_over_2x=True,
                tier_badge_text="💸 2x",
                tier_alert_text="已超 200K",
                detail_text="205K 明细",
            )
            app._apply_radar_state_to_ui(st_2x, force_tooltip=True)
            self.assertEqual(plugin._resolve_active_conv_id(), cid_radar)

            self.assertIsNotNone(plugin._btn_radar_unrestrict)
            self.assertIsNotNone(plugin._btn_tier_unrestrict)
            assert plugin._btn_radar_unrestrict is not None
            assert plugin._btn_tier_unrestrict is not None

            # Initial locked state
            self.assertEqual(plugin._btn_radar_unrestrict.cget("text"), RADAR_BTN_LOCKED)
            self.assertEqual(plugin._btn_tier_unrestrict.cget("text"), TIER_BTN_LOCKED)

            # Entry 1: Radar header button -> Unlock
            plugin._btn_radar_unrestrict.invoke()
            self.assertTrue(eoq_guard.is_eoq_window_unrestricted(cid_radar))
            self.assertEqual(plugin._btn_radar_unrestrict.cget("text"), RADAR_BTN_UNLOCKED)
            self.assertEqual(plugin._btn_tier_unrestrict.cget("text"), TIER_BTN_UNLOCKED)
            self.assertGreaterEqual(len(toasts), 1)
            self.assertGreaterEqual(len(logs), 1)
            self.assertIn(eoq_guard.EOQ_UNRESTRICTED_HINT, app._radar_state.detail_text)

            # Entry 2: 200K tier bar button -> Lock back
            plugin._btn_tier_unrestrict.invoke()
            self.assertFalse(eoq_guard.is_eoq_window_unrestricted(cid_radar))
            self.assertEqual(plugin._btn_radar_unrestrict.cget("text"), RADAR_BTN_LOCKED)
            self.assertEqual(plugin._btn_tier_unrestrict.cget("text"), TIER_BTN_LOCKED)

            # Entry 3: Target dropdown menu (`collect_radar_menu_items`) -> Unlock
            radar_items = app._plugin_host.collect_radar_menu_items()
            self.assertEqual(len(radar_items), 1)
            self.assertEqual(radar_items[0][0], MENU_LABEL_UNLOCK)
            radar_items[0][1]()
            self.assertTrue(eoq_guard.is_eoq_window_unrestricted(cid_radar))
            self.assertEqual(plugin._btn_radar_unrestrict.cget("text"), RADAR_BTN_UNLOCKED)

            # Entry 4: Floating Capsule HUD right-click menu provider -> Lock back
            self.assertIsNotNone(app._hud_win)
            hud_provider = getattr(app._hud_win, "_extra_menu_provider", None)
            self.assertTrue(callable(hud_provider))
            hud_items = hud_provider()
            self.assertEqual(len(hud_items), 1)
            self.assertEqual(hud_items[0][0], MENU_LABEL_LOCK)
            hud_items[0][1]()
            self.assertFalse(eoq_guard.is_eoq_window_unrestricted(cid_radar))
            self.assertEqual(plugin._btn_tier_unrestrict.cget("text"), TIER_BTN_LOCKED)

            # Entry 5: System tray menu (`collect_tray_items`) -> Unlock
            tray_items = app._plugin_host.collect_tray_items()
            self.assertEqual(len(tray_items), 1)
            self.assertEqual(tray_items[0][0], MENU_LABEL_UNLOCK)
            tray_items[0][1]()
            self.assertTrue(eoq_guard.is_eoq_window_unrestricted(cid_radar))
            self.assertEqual(plugin._btn_radar_unrestrict.cget("text"), RADAR_BTN_UNLOCKED)
            self.assertEqual(app._plugin_host.collect_tray_items()[0][0], MENU_LABEL_LOCK)
        finally:
            try:
                if getattr(app, "_hud_win", None) is not None:
                    app._hud_win.destroy()
            except Exception:
                pass
            try:
                app.destroy()
            except Exception:
                pass
            if old_disable is None:
                os.environ.pop("ACEROLA_ORION_DISABLE_PLUGINS", None)
            else:
                os.environ["ACEROLA_ORION_DISABLE_PLUGINS"] = old_disable
            reset_kernel_for_tests()

    def test_06_import_isolation_stdlib_guard_and_zero_sibling_load(self) -> None:
        code = (
            "import sys\n"
            f"sys.path.insert(0, {str(_PUB_ROOT)!r})\n"
            "import acerola_orion.plugins.eoq_guard as eg\n"
            "assert 'tkinter' not in sys.modules, 'eoq_guard must stay GUI-free'\n"
            "assert 'acerola_orion.plugins.tasks_plugin' not in sys.modules\n"
            "from acerola_orion.plugins.eoq_plugin import EoqPlugin\n"
            "p = EoqPlugin()\n"
            "assert p.name == 'eoq_guard'\n"
            "assert p.extra_launchers('py', 'pyw', '.') == {}\n"
            "forbidden = [\n"
            "    'acerola_orion.plugins.tasks_plugin',\n"
            "    'acerola_orion.plugins.task_board_tab',\n"
            "]\n"
            "loaded = [m for m in forbidden if m in sys.modules]\n"
            "assert not loaded, f'Unexpected eager imports: {loaded}'\n"
            "print('ISOLATION_OK')\n"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, msg=f"stderr={proc.stderr}\nstdout={proc.stdout}")
        self.assertIn("ISOLATION_OK", proc.stdout)


if __name__ == "__main__":
    unittest.main()
