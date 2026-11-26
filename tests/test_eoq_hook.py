# -*- coding: utf-8 -*-
"""Isolated unit tests for `acerola_orion.plugins.eoq_hook` and `eoq_guard` linkage."""

from __future__ import annotations

from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from acerola_orion.plugins import eoq_hook
from acerola_orion.plugins.eoq_guard import (
    invalidate_eoq_guard_cache,
    is_eoq_window_unrestricted,
    set_eoq_window_unrestricted,
)


def _iso_utc(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _write_transcript(path: Path, records: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(r, ensure_ascii=False) for r in records]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


class TestEoqHook(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.ws_dir = self.root / "project_ws"
        self.ws_dir.mkdir(parents=True, exist_ok=True)
        self.cfg_path = self.root / "eoq_guard_config.json"
        self.state_path = self.root / ".eoq_guard_state.json"

        self._env_patcher = mock.patch.dict(
            os.environ,
            {
                "EOQ_GUARD_CONFIG": str(self.cfg_path),
                "EOQ_GUARD_STATE": str(self.state_path),
                "ACEROLA_ORION_DISABLE_PLUGINS": "",
                "ACEROLA_ORION_DISABLED_PLUGINS": "",
            },
            clear=False,
        )
        self._env_patcher.start()
        invalidate_eoq_guard_cache()

    def tearDown(self) -> None:
        invalidate_eoq_guard_cache()
        self._env_patcher.stop()
        self._tmp.cleanup()

    def _base_payload(
        self,
        conv_id: str,
        transcript_path: Path,
        *,
        user_input: str = "继续实现模块",
        extra: dict | None = None,
    ) -> dict:
        payload = {
            "conversationId": conv_id,
            "transcriptPath": str(transcript_path),
            "workspacePaths": [str(self.ws_dir)],
            "cwd": str(self.ws_dir),
            "lastUserInput": f"<USER_REQUEST>\n{user_input}\n</USER_REQUEST>",
        }
        if extra:
            payload.update(extra)
        return payload

    def test_cold_window_gate_blocks_and_writes_rescue_draft(self) -> None:
        conv_id = "11112222-3333-4444-5555-666677778888"
        t0 = 1_760_000_000.0
        tpath = _write_transcript(
            self.root / "brain" / conv_id / "transcript.jsonl",
            [
                {
                    "type": "USER_INPUT",
                    "step_index": 1,
                    "created_at": _iso_utc(t0 - 10),
                    "content": "<USER_REQUEST>修复登录超时并补充单测</USER_REQUEST>",
                },
                {
                    "type": "PLANNER_RESPONSE",
                    "step_index": 2,
                    "created_at": _iso_utc(t0),
                    "input_tokens": 12000,
                    "cache_read_tokens": 33000,
                    "output_tokens": 600,
                    "content": "已定位超时重试配置，下一步准备补单测。",
                    "tool_calls": [
                        {"name": "edit_file", "args": {"Path": str(self.ws_dir / "src" / "auth.py")}}
                    ],
                },
                {
                    "type": "USER_INPUT",
                    "step_index": 3,
                    "created_at": _iso_utc(t0 + 320),
                    "content": "<USER_REQUEST>接着把测试写完</USER_REQUEST>",
                },
            ],
        )

        payload = self._base_payload(conv_id, tpath, user_input="接着把测试写完")
        out = eoq_hook.dispatch(payload, forced_event="PreInvocation", now_ep=t0 + 320)
        self.assertEqual(out.get("terminationBehavior"), "terminate")
        err_msg = out["injectSteps"][0]["errorMessage"]["userMessage"]
        self.assertIn("EOQ 冷窗熔断", err_msg)

        checkpoints = list((self.ws_dir / ".agents" / "checkpoints").glob("cold_rescue_*_11112222.md"))
        self.assertEqual(len(checkpoints), 1)
        draft_text = checkpoints[0].read_text(encoding="utf-8")
        self.assertIn("修复登录超时并补充单测", draft_text)
        self.assertIn("auth.py", draft_text)
        self.assertIn("已定位超时重试配置", draft_text)

        # Re-blocking in the same planner epoch reuses the existing rescue draft
        out_repeat = eoq_hook.dispatch(payload, forced_event="PreInvocation", now_ep=t0 + 400)
        self.assertEqual(out_repeat.get("terminationBehavior"), "terminate")
        self.assertEqual(len(list((self.ws_dir / ".agents" / "checkpoints").glob("cold_rescue_*.md"))), 1)

        # Explicit !go bypasses the Cold Window Gate
        tpath_go = _write_transcript(
            tpath,
            [
                {
                    "type": "PLANNER_RESPONSE",
                    "step_index": 2,
                    "created_at": _iso_utc(t0),
                    "input_tokens": 45000,
                    "output_tokens": 500,
                    "content": "done",
                },
                {
                    "type": "USER_INPUT",
                    "step_index": 4,
                    "created_at": _iso_utc(t0 + 410),
                    "content": "<USER_REQUEST>接着写 !go</USER_REQUEST>",
                },
            ],
        )
        out_go = eoq_hook.dispatch(
            self._base_payload(conv_id, tpath_go, user_input="接着写 !go"),
            forced_event="PreInvocation",
            now_ep=t0 + 410,
        )
        self.assertNotEqual(out_go.get("terminationBehavior"), "terminate")

    def test_hot_95k_and_cliff_175k_watermarks_and_compact_reset(self) -> None:
        conv_id = "22223333-4444-5555-6666-777788889999"
        t0 = 1_760_010_000.0
        tpath = self.root / "brain" / conv_id / "transcript.jsonl"

        def _turn(prompt_tok: int, step: int, now_offset: float, text: str = "继续") -> dict:
            _write_transcript(
                tpath,
                [
                    {
                        "type": "PLANNER_RESPONSE",
                        "step_index": step,
                        "created_at": _iso_utc(t0 + now_offset - 20),
                        "input_tokens": prompt_tok,
                        "output_tokens": 400,
                        "content": "ok",
                    },
                    {
                        "type": "USER_INPUT",
                        "step_index": step + 1,
                        "created_at": _iso_utc(t0 + now_offset),
                        "content": f"<USER_REQUEST>{text}</USER_REQUEST>",
                    },
                ],
            )
            return eoq_hook.dispatch(
                self._base_payload(conv_id, tpath, user_input=text),
                forced_event="PreInvocation",
                now_ep=t0 + now_offset,
            )

        # 95K sweet-spot nudge fires once per band
        res_95k = _turn(98_000, 1, 30)
        self.assertIn("EOQ 甜点收口提醒", res_95k["injectSteps"][0]["systemMessage"]["systemMessage"])
        self.assertEqual(_turn(105_000, 3, 60), {})

        # 175K cliff warning fires once when crossing 175K
        res_175k = _turn(176_000, 5, 90)
        self.assertIn("EOQ 悬崖预警", res_175k["injectSteps"][0]["systemMessage"]["systemMessage"])
        self.assertEqual(_turn(178_000, 7, 120), {})

        # Large drop (>= 40K after /compact) resets warned bands so 95K triggers again
        self.assertEqual(_turn(70_000, 9, 150), {})
        res_re95k = _turn(96_000, 11, 180)
        self.assertIn("EOQ 甜点收口提醒", res_re95k["injectSteps"][0]["systemMessage"]["systemMessage"])

    def test_180k_pre_tool_use_force_ask_and_post_tool_use_auto_unlock(self) -> None:
        conv_id = "33334444-5555-6666-7777-888899990000"
        t0 = 1_760_020_000.0
        tpath = _write_transcript(
            self.root / "brain" / conv_id / "transcript.jsonl",
            [
                {
                    "type": "PLANNER_RESPONSE",
                    "step_index": 10,
                    "created_at": _iso_utc(t0),
                    "input_tokens": 185_000,
                    "output_tokens": 500,
                    "content": "checking",
                }
            ],
        )

        # Checkpoint handoff write is always allowed even >= 180K
        allow_ckpt = eoq_hook.dispatch(
            self._base_payload(
                conv_id,
                tpath,
                extra={
                    "stepIdx": 11,
                    "toolCall": {
                        "id": "tc_ckpt",
                        "name": "write_file",
                        "args": {"Path": str(self.ws_dir / ".agents" / "checkpoints" / "handoff.md")},
                    },
                },
            ),
            forced_event="PreToolUse",
            now_ep=t0 + 5,
        )
        self.assertEqual(allow_ckpt, {"decision": "allow"})

        # Context-bloating read_file at 185K triggers force_ask
        ask_res = eoq_hook.dispatch(
            self._base_payload(
                conv_id,
                tpath,
                extra={
                    "stepIdx": 12,
                    "toolCall": {
                        "id": "tc_deny",
                        "name": "read_file",
                        "args": {"Path": str(self.ws_dir / "big.py")},
                    },
                },
            ),
            forced_event="PreToolUse",
            now_ep=t0 + 6,
        )
        self.assertEqual(ask_res.get("decision"), "force_ask")
        self.assertIn("EOQ 悬崖锁", ask_res.get("reason", ""))

        # Denied PostToolUse drops pending prompt and does NOT unlock window
        eoq_hook.dispatch(
            self._base_payload(
                conv_id,
                tpath,
                extra={
                    "stepIdx": 12,
                    "toolCall": {"id": "tc_deny", "name": "read_file"},
                    "error": "Tool call denied by user",
                },
            ),
            forced_event="PostToolUse",
            now_ep=t0 + 7,
        )
        invalidate_eoq_guard_cache()
        self.assertFalse(is_eoq_window_unrestricted(conv_id))

        # Second PreToolUse followed by clean PostToolUse (user clicked Allow) unlocks window
        ask_res_2 = eoq_hook.dispatch(
            self._base_payload(
                conv_id,
                tpath,
                extra={
                    "stepIdx": 13,
                    "toolCall": {
                        "id": "tc_allow",
                        "name": "run_command",
                        "args": {"CommandLine": "pytest"},
                    },
                },
            ),
            forced_event="PreToolUse",
            now_ep=t0 + 8,
        )
        self.assertEqual(ask_res_2.get("decision"), "force_ask")

        eoq_hook.dispatch(
            self._base_payload(
                conv_id,
                tpath,
                extra={
                    "stepIdx": 13,
                    "toolCall": {"id": "tc_allow", "name": "run_command"},
                },
            ),
            forced_event="PostToolUse",
            now_ep=t0 + 9,
        )
        invalidate_eoq_guard_cache()
        self.assertTrue(is_eoq_window_unrestricted(conv_id))

        # Subsequent PreToolUse at 185K now fast-paths allow
        after_unlock = eoq_hook.dispatch(
            self._base_payload(
                conv_id,
                tpath,
                extra={
                    "stepIdx": 14,
                    "toolCall": {"id": "tc_next", "name": "read_file", "args": {"Path": "x.py"}},
                },
            ),
            forced_event="PreToolUse",
            now_ep=t0 + 10,
        )
        self.assertEqual(after_unlock, {"decision": "allow"})

    def test_set_eoq_window_unrestricted_bidirectional_linkage(self) -> None:
        conv_id = "44445555-6666-7777-8888-999900001111"
        t0 = 1_760_030_000.0
        tpath = _write_transcript(
            self.root / "brain" / conv_id / "transcript.jsonl",
            [
                {
                    "type": "PLANNER_RESPONSE",
                    "step_index": 1,
                    "created_at": _iso_utc(t0),
                    "input_tokens": 190_000,
                    "output_tokens": 500,
                    "content": "work",
                },
                {
                    "type": "USER_INPUT",
                    "step_index": 2,
                    "created_at": _iso_utc(t0 + 400),
                    "content": "<USER_REQUEST>继续收尾</USER_REQUEST>",
                },
            ],
        )

        # Panel toggles unrestricted ON before hook runs
        ok, msg = set_eoq_window_unrestricted(conv_id, True, source="panel")
        self.assertTrue(ok)
        self.assertIn("已解锁", msg)

        # PreInvocation does not cold-block even after 400s idle at 190K
        pre_inv = eoq_hook.dispatch(
            self._base_payload(conv_id, tpath, user_input="继续收尾"),
            forced_event="PreInvocation",
            now_ep=t0 + 400,
        )
        self.assertNotEqual(pre_inv.get("terminationBehavior"), "terminate")
        self.assertIn("injectSteps", pre_inv)

        # PreToolUse allows locked tools at 190K
        pre_tool = eoq_hook.dispatch(
            self._base_payload(
                conv_id,
                tpath,
                extra={"stepIdx": 3, "toolCall": {"id": "t1", "name": "read_file", "args": {"Path": "a.py"}}},
            ),
            forced_event="PreToolUse",
            now_ep=t0 + 401,
        )
        self.assertEqual(pre_tool, {"decision": "allow"})

        # Panel re-locks window -> 190K PreToolUse returns force_ask again
        ok_lock, _ = set_eoq_window_unrestricted(conv_id, False, source="panel")
        self.assertTrue(ok_lock)
        relocked_tool = eoq_hook.dispatch(
            self._base_payload(
                conv_id,
                tpath,
                extra={"stepIdx": 4, "toolCall": {"id": "t2", "name": "read_file", "args": {"Path": "a.py"}}},
            ),
            forced_event="PreToolUse",
            now_ep=t0 + 402,
        )
        self.assertEqual(relocked_tool.get("decision"), "force_ask")

    def test_cli_main_fail_open_and_plugin_disable_short_circuit(self) -> None:
        conv_id = "55556666-7777-8888-9999-000011112222"
        t0 = 1_760_040_000.0
        tpath = _write_transcript(
            self.root / "brain" / conv_id / "transcript.jsonl",
            [
                {
                    "type": "PLANNER_RESPONSE",
                    "step_index": 1,
                    "created_at": _iso_utc(t0),
                    "input_tokens": 192_000,
                    "output_tokens": 300,
                    "content": "cliff",
                }
            ],
        )
        cliff_payload = self._base_payload(
            conv_id,
            tpath,
            extra={"stepIdx": 2, "toolCall": {"id": "c1", "name": "run_command", "args": {"CommandLine": "dir"}}},
        )

        # 1. Fail-open on corrupt stdin JSON
        for ev, expected in (("PreToolUse", {"decision": "allow"}), ("PreInvocation", {}), ("PostToolUse", {})):
            stdout_buf = io.StringIO()
            with mock.patch.object(sys, "stdin", io.StringIO("{not-valid-json")), mock.patch.object(
                sys, "stdout", stdout_buf
            ):
                rc = eoq_hook.main(["--event", ev])
            self.assertEqual(rc, 0)
            self.assertEqual(json.loads(stdout_buf.getvalue()), expected)

        # 2. ACEROLA_ORION_DISABLE_PLUGINS=1 short-circuits main()
        with mock.patch.dict(os.environ, {"ACEROLA_ORION_DISABLE_PLUGINS": "1"}, clear=False):
            stdout_buf = io.StringIO()
            with mock.patch.object(sys, "stdin", io.StringIO(json.dumps(cliff_payload))), mock.patch.object(
                sys, "stdout", stdout_buf
            ):
                rc = eoq_hook.main(["--event", "PreToolUse"])
            self.assertEqual(rc, 0)
            self.assertEqual(json.loads(stdout_buf.getvalue()), {"decision": "allow"})
            self.assertFalse(self.state_path.exists())

        # 3. ACEROLA_ORION_DISABLED_PLUGINS containing eoq_guard short-circuits main()
        with mock.patch.dict(os.environ, {"ACEROLA_ORION_DISABLED_PLUGINS": "other_mod, eoq_guard"}, clear=False):
            stdout_buf = io.StringIO()
            with mock.patch.object(sys, "stdin", io.StringIO(json.dumps(cliff_payload))), mock.patch.object(
                sys, "stdout", stdout_buf
            ):
                rc = eoq_hook.main(["--event", "PreToolUse"])
            self.assertEqual(rc, 0)
            self.assertEqual(json.loads(stdout_buf.getvalue()), {"decision": "allow"})
            self.assertFalse(self.state_path.exists())

        # 4. Real subprocess `python -m acerola_orion.plugins.eoq_hook --event PreToolUse`
        repo_root = Path(__file__).resolve().parents[1]
        env = dict(os.environ)
        env["EOQ_GUARD_CONFIG"] = str(self.cfg_path)
        env["EOQ_GUARD_STATE"] = str(self.state_path)
        env.pop("ACEROLA_ORION_DISABLE_PLUGINS", None)
        env.pop("ACEROLA_ORION_DISABLED_PLUGINS", None)
        proc = subprocess.run(
            [sys.executable, "-m", "acerola_orion.plugins.eoq_hook", "--event", "PreToolUse"],
            input=json.dumps(cliff_payload).encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(repo_root),
            env=env,
            timeout=15,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, msg=proc.stderr.decode("utf-8", errors="replace"))
        sub_out = json.loads(proc.stdout.decode("utf-8"))
        self.assertEqual(sub_out.get("decision"), "force_ask")


if __name__ == "__main__":
    unittest.main()
