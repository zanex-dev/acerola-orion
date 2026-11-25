# -*- coding: utf-8 -*-
"""
Unit tests for built-in optional plugin package `acerola_orion.plugins`
and the `tasks_board` plugin (`_helpers`, `workspace_prefs`, `task_board`, `tasks_plugin`).
"""

from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from acerola_orion.plugins import _helpers, task_board, workspace_prefs
from acerola_orion.plugins.tasks_plugin import (
    TASKS_TAB_KEY,
    TASKS_TAB_TITLE,
    TasksPlugin,
)


class TestPluginHelpers(unittest.TestCase):
    def test_exports_and_no_private_symbols(self) -> None:
        expected = {
            "resolve_callable",
            "default_clipboard_writer",
            "default_ui_call",
            "ps_single_quote",
            "ps1_cli",
            "invoke_host_action",
        }
        self.assertEqual(set(_helpers.__all__), expected)
        self.assertFalse(hasattr(_helpers, "PRIV_EXT_KEY"))
        self.assertFalse(hasattr(_helpers, "extract_latest_log_endpoint"))

    def test_resolve_callable_and_invoke_host_action(self) -> None:
        class DummyHost:
            def __init__(self) -> None:
                self.calls: list[str] = []

            def _px(self, n: int) -> int:
                return n * 2

            def run_action(self, label: str, fn):
                self.calls.append(label)
                return fn()

        host = DummyHost()
        fn = _helpers.resolve_callable(host, "px", "_px")
        self.assertIsNotNone(fn)
        self.assertEqual(fn(10), 20)
        self.assertIsNone(_helpers.resolve_callable(host, "missing_a", "missing_b"))

        res = _helpers.invoke_host_action(host, "demo", lambda: 42)
        self.assertEqual(res, 42)
        self.assertEqual(host.calls, ["demo"])
        self.assertEqual(_helpers.invoke_host_action(object(), "inline", lambda: "ok"), "ok")

    def test_ps_helpers_and_ui_fallbacks(self) -> None:
        quoted = _helpers.ps_single_quote("C:\\a'b\r\n")
        self.assertEqual(quoted, "'C:\\a''b'")
        snippet = _helpers.ps1_cli("python.exe", Path("C:/work/demo"), "status --json")
        self.assertIn("Set-Location -LiteralPath 'C:\\work\\demo'", snippet)
        self.assertIn("& 'python.exe' -m acerola_orion status --json", snippet)

        events: list[str] = []

        class FakeWidget:
            def clipboard_clear(self) -> None:
                events.append("clear")

            def clipboard_append(self, val: str) -> None:
                events.append(f"append:{val}")

            def update_idletasks(self) -> None:
                events.append("update")

            def after(self, delay: int, cb) -> None:
                events.append(f"after:{delay}")
                cb()

        class FakeHud:
            def flash_toast(self, msg: str, duration_sec: float = 2.0) -> None:
                events.append(f"toast:{msg}:{duration_sec}")

        w = FakeWidget()
        logs: list[str] = []
        writer = _helpers.default_clipboard_writer(w, log_fn=logs.append, hud_getter=lambda: FakeHud())
        self.assertEqual(writer("  hello  ", toast="ok", console_msg="logged"), "hello")
        self.assertEqual(writer("   "), "")
        self.assertIn("append:hello", events)
        self.assertIn("toast:ok:2.0", events)
        self.assertEqual(logs, ["logged"])

        ran: list[bool] = []
        _helpers.default_ui_call(w)(lambda: ran.append(True))
        self.assertEqual(ran, [True])


class TestWorkspacePrefs(unittest.TestCase):
    def test_crud_and_privacy_isolation(self) -> None:
        store: dict = {"keep_me": "untouched"}

        def fake_load() -> dict:
            return dict(store)

        def fake_update(mutator) -> dict:
            mutator(store)
            return dict(store)

        with tempfile.TemporaryDirectory() as td:
            ws_a = str(Path(td) / "proj_a")
            ws_b = str(Path(td) / "proj_b")
            os.makedirs(ws_a, exist_ok=True)
            os.makedirs(ws_b, exist_ok=True)

            with (
                patch.object(workspace_prefs, "load_app_config", side_effect=fake_load),
                patch.object(workspace_prefs, "update_app_config", side_effect=fake_update),
            ):
                # Explicit save with valid + sensitive paths + duplicate
                home_dir = Path.home()
                res_save = workspace_prefs.save_task_workspaces(
                    [ws_a, ws_a, str(home_dir), str(home_dir / "Pictures"), str(Path(td) / ".user_uploaded")]
                )
                self.assertTrue(res_save["ok"])
                self.assertEqual(res_save["workspaces"], [os.path.abspath(ws_a)])
                self.assertEqual(len(res_save["rejected"]), 3)
                self.assertEqual(store["keep_me"], "untouched")

                # Add second workspace + dedup check
                res_add = workspace_prefs.add_task_workspace(ws_b)
                self.assertTrue(res_add["ok"])
                self.assertTrue(res_add["added"])
                self.assertEqual(len(res_add["workspaces"]), 2)

                res_dup = workspace_prefs.add_task_workspace(ws_b)
                self.assertTrue(res_dup["ok"])
                self.assertFalse(res_dup["added"])

                # Remove first workspace
                res_rm = workspace_prefs.remove_task_workspace(ws_a)
                self.assertTrue(res_rm["ok"])
                self.assertTrue(res_rm["removed"])
                self.assertEqual(res_rm["workspaces"], [os.path.abspath(ws_b)])

                # Empty list stays explicit [] (does not fall back to defaults)
                res_empty = workspace_prefs.save_task_workspaces([])
                self.assertTrue(res_empty["ok"])
                self.assertEqual(res_empty["workspaces"], [])
                self.assertEqual(workspace_prefs.get_task_workspaces(), [])

                # Control char and root directory rejected
                bad_ctrl = workspace_prefs.add_task_workspace("C:\\bad\npath")
                self.assertFalse(bad_ctrl["ok"])
                root_dir = Path(os.path.abspath(ws_a)).anchor
                bad_root = workspace_prefs.add_task_workspace(root_dir)
                self.assertFalse(bad_root["ok"])


class TestTaskBoardEngine(unittest.TestCase):
    def test_scan_packs_checkpoints_and_limits(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td) / "demo_ws"
            pack_dir = ws / ".agents" / "tasks" / "20261125_1900_demo"
            ckpt_dir = ws / ".agents" / "checkpoints"
            pack_dir.mkdir(parents=True, exist_ok=True)
            ckpt_dir.mkdir(parents=True, exist_ok=True)

            (pack_dir / "T01_init.md").write_text("# T01\n", encoding="utf-8")
            (pack_dir / "T02_next.md").write_text("# T02\n", encoding="utf-8")
            (pack_dir / "PLAN.md").write_text(
                "\n".join(
                    [
                        "# 任务包：白帽插件迁移演练",
                        "- 任务包ID：`20261125_1900_demo`",
                        "- 创建时间：`2026-11-25 19:00`",
                        "- 总目标：验证只读任务看板解析",
                        "",
                        "## 任务进度表",
                        "| 状态 | 编号 | 任务卡 | 依赖 | 并行组 | 建议模型 | 一句话目标 |",
                        "|---|---|---|---|---|---|---|",
                        "| [x] | T01 | `T01_init.md` | - | P1 | high | 初始化目录 |",
                        "| [ ] | T02 | `T02_next.md` | T01 | P2 | standard | 迁入核心模块 |",
                        "| [ ] | T03 | `T03_final.md` | T02 | P3 | standard | 端到端收口 |",
                    ]
                ),
                encoding="utf-8",
            )

            (ckpt_dir / "handoff_20261125_1910_demo.md").write_text(
                "\n".join(
                    [
                        "# 阶段交接单：白帽底座迁入",
                        "- 落盘时间：2026-11-25 19:10 | 收口水位：112K",
                        "- 来源会话：12345678-abcd-ef00-1111-222233334444",
                        "",
                        "## 4. 下一窗口直接启动指令",
                        "> 请读取交接单继续推进 T02 迁移。",
                    ]
                ),
                encoding="utf-8",
            )
            (ckpt_dir / "cold_rescue_20261125_1912.md").write_text(
                "# 冷窗急救：状态保存\n\n## 4. 启动指令\n```\n继续恢复现场\n```\n",
                encoding="utf-8",
            )
            # Oversized markdown file should be skipped with warning
            (ckpt_dir / "oversized.md").write_bytes(b"a" * (task_board.MAX_MD_FILE_BYTES + 16))

            snap = task_board.scan_task_board([ws, Path.home() / "Pictures"])
            self.assertEqual(len(snap.workspaces), 1)
            self.assertEqual(len(snap.packs), 1)
            pack = snap.packs[0]
            self.assertEqual(pack.pack_id, "20261125_1900_demo")
            self.assertEqual(pack.title, "白帽插件迁移演练")
            self.assertEqual((pack.done_count, pack.total_count, pack.ready_count), (1, 3, 1))

            by_id = {c.task_id: c for c in pack.tasks}
            self.assertEqual(by_id["T01"].status, "done")
            self.assertFalse(by_id["T01"].is_ready)
            self.assertTrue(by_id["T02"].is_ready)
            self.assertEqual(by_id["T02"].blocked_by, [])
            self.assertFalse(by_id["T03"].is_ready)
            self.assertEqual(by_id["T03"].blocked_by, ["T02"])
            self.assertIn("T02", by_id["T02"].execute_prompt)

            by_name = {c.filename: c for c in snap.checkpoints}
            self.assertEqual(set(by_name.keys()), {"handoff_20261125_1910_demo.md", "cold_rescue_20261125_1912.md"})
            self.assertEqual(by_name["handoff_20261125_1910_demo.md"].kind, "handoff")
            self.assertEqual(by_name["handoff_20261125_1910_demo.md"].watermark, "112K")
            self.assertEqual(
                by_name["handoff_20261125_1910_demo.md"].resume_prompt,
                "请读取交接单继续推进 T02 迁移。",
            )
            self.assertEqual(by_name["cold_rescue_20261125_1912.md"].kind, "cold_rescue")
            self.assertEqual(by_name["cold_rescue_20261125_1912.md"].resume_prompt, "继续恢复现场")

            # Expect warnings for Pictures privacy skip and oversized markdown
            self.assertTrue(any("隐私隔离" in w for w in snap.warnings))
            self.assertTrue(any("超大 Markdown" in w for w in snap.warnings))

            # Prompt builders
            self.assertIn("task-decomposer", task_board.build_decompose_prompt(ws, "新增导出"))
            self.assertIn("stage-handoff", task_board.build_handoff_prompt(ws, "阶段收口"))


class TestTasksPluginContract(unittest.TestCase):
    def test_plugin_metadata_and_ui_registration(self) -> None:
        telemetry_seen: list[object] = []
        plugin = TasksPlugin(on_telemetry_refresh=telemetry_seen.append)
        self.assertEqual(plugin.name, "tasks_board")
        self.assertEqual(TASKS_TAB_KEY, "tasks")
        self.assertEqual(TASKS_TAB_TITLE, "📋 任务拆解与接力 (Tasks)")
        self.assertIsNone(plugin.handle_cli("unknown", None))
        self.assertEqual(plugin.extra_launchers("py", "pyw", _REPO_ROOT), {})

        registered: dict = {}

        class FakeHost:
            def add_tab(self, *, key, title, factory_fn, on_select, on_refresh) -> None:
                registered.update(
                    {
                        "key": key,
                        "title": title,
                        "factory_fn": factory_fn,
                        "on_select": on_select,
                        "on_refresh": on_refresh,
                    }
                )

        plugin.register_ui(FakeHost())
        self.assertEqual(registered["key"], TASKS_TAB_KEY)
        self.assertEqual(registered["title"], TASKS_TAB_TITLE)

        refresh_calls: list[bool] = []

        class FakeTab:
            def __init__(self, parent, **kwargs) -> None:
                self.parent = parent
                self.kwargs = kwargs

            def pack(self, **_kw) -> None:
                pass

            def refresh_async(self, silent: bool = False) -> None:
                refresh_calls.append(silent)

        with patch("acerola_orion.plugins.task_board_tab.TaskBoardTab", FakeTab):
            tab = registered["factory_fn"](object(), FakeHost())
            self.assertIsInstance(tab, FakeTab)

        registered["on_select"]()
        self.assertEqual(refresh_calls, [True])

        class FakeReport:
            extensions = {}

        rep = FakeReport()
        registered["on_refresh"](rep)
        self.assertEqual(telemetry_seen, [rep])
        self.assertEqual(refresh_calls, [True, True])

    def test_plugins_subpackage_cleanroom_keywords(self) -> None:
        plugins_dir = _REPO_ROOT / "acerola_orion" / "plugins"
        forbidden = [
            "orion_" + "private",
            "v1" + "internal",
            "scal" + "pel",
            "orion-" + "mux",
        ]
        py_files = sorted(plugins_dir.glob("*.py"))
        self.assertGreaterEqual(len(py_files), 6)
        for path in py_files:
            content = path.read_text(encoding="utf-8").lower()
            for word in forbidden:
                self.assertNotIn(word.lower(), content, f"Forbidden token {word!r} leaked into {path.name}")


if __name__ == "__main__":
    unittest.main()
