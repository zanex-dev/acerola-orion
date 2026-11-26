# -*- coding: utf-8 -*-
"""
Isolated unit tests for acerola_orion.companion installer & CLI subcommands.
All filesystem side effects are strictly sandboxed inside tempfile.TemporaryDirectory
via ORION_GEMINI_CONFIG_DIR; real ~/.gemini/config is never touched.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from acerola_orion.__main__ import main as cli_main
from acerola_orion.companion import (
    ENV_GEMINI_CONFIG_DIR,
    MANAGED_HOOK_KEYS,
    companion_status,
    get_gemini_config_dir,
    install_companion,
    uninstall_companion,
)


class TestCompanionInstaller(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="orion_companion_test_")
        self.root = Path(self._tmp.name)
        self.cfg_dir = self.root / "gemini_config"
        self.ws_dir = self.root / "sample_workspace"
        self.ws_dir.mkdir(parents=True, exist_ok=True)
        self._env_patch = mock.patch.dict(
            os.environ,
            {
                ENV_GEMINI_CONFIG_DIR: str(self.cfg_dir),
                "ACEROLA_ORION_DISABLE_PLUGINS": "1",
            },
        )
        self._env_patch.start()

    def tearDown(self) -> None:
        self._env_patch.stop()
        self._tmp.cleanup()

    def test_get_gemini_config_dir_respects_env_override(self) -> None:
        self.assertEqual(get_gemini_config_dir(), self.cfg_dir)

    def test_fresh_install_hooks_global_and_workspace_default_no_rules(self) -> None:
        fake_py = r"D:\venvs\orion\Scripts\python.exe"
        res = install_companion(
            workspace=self.ws_dir,
            install_hooks=True,
            install_global=True,
            include_rules=False,
            force=False,
            python_exe=fake_py,
        )
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["python_exe"], "D:/venvs/orion/Scripts/python.exe")
        self.assertEqual(set(res["hooks_installed"]), set(MANAGED_HOOK_KEYS))
        self.assertEqual(res["skipped_existing"], [])

        hooks_path = self.cfg_dir / "hooks.json"
        self.assertTrue(hooks_path.is_file())
        hooks_obj = json.loads(hooks_path.read_text(encoding="utf-8"))
        self.assertIn("eoq-context-guard", hooks_obj)
        self.assertIn("orion-chat-vault", hooks_obj)

        eoq_entry = hooks_obj["eoq-context-guard"]
        for event_name in ("PreInvocation", "PreToolUse", "PostToolUse"):
            self.assertIn(event_name, eoq_entry)
        raw_hooks_text = hooks_path.read_text(encoding="utf-8")
        self.assertNotIn("<PYTHON_EXE>", raw_hooks_text)
        self.assertIn("D:/venvs/orion/Scripts/python.exe -m acerola_orion.plugins.eoq_hook", raw_hooks_text)
        self.assertIn("D:/venvs/orion/Scripts/python.exe -m acerola_orion.vault_hook", raw_hooks_text)

        # Global assets installed
        self.assertTrue((self.cfg_dir / "eoq_guard_config.json").is_file())
        self.assertTrue((self.cfg_dir / "docs" / "eoq_context_guard_cheatsheet.md").is_file())
        self.assertTrue((self.cfg_dir / "plugins" / "acerola-orion" / "plugin.json").is_file())
        self.assertTrue(
            (self.cfg_dir / "plugins" / "acerola-orion" / "skills" / "acerola-orion" / "SKILL.md").is_file()
        )
        self.assertTrue(
            (self.cfg_dir / "plugins" / "acerola-orion" / "skills" / "orion-vault" / "SKILL.md").is_file()
        )
        self.assertTrue((self.cfg_dir / "skills" / "session-recall" / "SKILL.md").is_file())
        self.assertTrue((self.cfg_dir / "skills" / "session-recall" / "scripts" / "recall.py").is_file())
        self.assertTrue((self.cfg_dir / "skills" / "kv-keepalive" / "SKILL.md").is_file())

        # Workspace plugins installed without rules/AGENTS.md by default
        self.assertTrue((self.ws_dir / ".agents" / "plugins" / "eoq-task-relay" / "plugin.json").is_file())
        self.assertTrue(
            (self.ws_dir / ".agents" / "plugins" / "eoq-task-relay" / "skills" / "task-decomposer" / "SKILL.md").is_file()
        )
        self.assertTrue(
            (self.ws_dir / ".agents" / "plugins" / "eoq-task-relay" / "skills" / "stage-handoff" / "SKILL.md").is_file()
        )
        self.assertTrue(
            (self.ws_dir / ".agents" / "plugins" / "engineering-core" / "skills" / "version-seal" / "SKILL.md").is_file()
        )
        self.assertFalse(
            (self.ws_dir / ".agents" / "plugins" / "engineering-core" / "rules" / "AGENTS.md").exists()
        )

        # Status check reflects installed state
        st = companion_status(workspace=self.ws_dir)
        self.assertTrue(st["ok"])
        self.assertTrue(st["hooks"]["managed_keys"]["eoq-context-guard"]["installed"])
        self.assertTrue(st["hooks"]["managed_keys"]["orion-chat-vault"]["installed"])
        self.assertTrue(st["global"]["plugin_acerola_orion"])
        self.assertTrue(st["workspace"]["eoq_task_relay"])
        self.assertTrue(st["workspace"]["version_seal"])
        self.assertFalse(st["workspace"]["engineering_rules"])

    def test_include_rules_copies_engineering_rules(self) -> None:
        res = install_companion(
            workspace=self.ws_dir,
            install_hooks=False,
            install_global=False,
            include_rules=True,
        )
        self.assertTrue(res["ok"])
        self.assertFalse((self.cfg_dir / "hooks.json").exists())
        self.assertTrue(
            (self.ws_dir / ".agents" / "plugins" / "engineering-core" / "rules" / "AGENTS.md").is_file()
        )

    def test_nondestructive_merge_preserves_custom_hooks_and_skips_existing_without_force(self) -> None:
        self.cfg_dir.mkdir(parents=True, exist_ok=True)
        hooks_path = self.cfg_dir / "hooks.json"
        custom_hooks = {
            "user-custom-guard": {"enabled": True, "PreToolUse": [{"type": "command", "command": "echo ok"}]},
            "eoq-context-guard": {"enabled": False, "custom_marker": "keep_me"},
        }
        hooks_path.write_text(json.dumps(custom_hooks, indent=2), encoding="utf-8")

        eoq_cfg_path = self.cfg_dir / "eoq_guard_config.json"
        eoq_cfg_path.write_text('{"custom_threshold": 123456}\n', encoding="utf-8")

        custom_skill_path = self.cfg_dir / "skills" / "kv-keepalive" / "SKILL.md"
        custom_skill_path.parent.mkdir(parents=True, exist_ok=True)
        custom_skill_path.write_text("# Private customized keepalive\n", encoding="utf-8")

        res = install_companion(force=False, python_exe="/usr/bin/python3")
        self.assertTrue(res["ok"])
        self.assertIn("user-custom-guard", res["preserved_hook_keys"])
        self.assertEqual(res["hooks_installed"], ["orion-chat-vault"])
        self.assertIn("hooks.json:eoq-context-guard", res["skipped_existing"])
        self.assertIn(str(eoq_cfg_path), res["skipped_existing"])
        self.assertIn(str(custom_skill_path), res["skipped_existing"])

        merged = json.loads(hooks_path.read_text(encoding="utf-8"))
        self.assertEqual(merged["user-custom-guard"], custom_hooks["user-custom-guard"])
        self.assertEqual(merged["eoq-context-guard"]["custom_marker"], "keep_me")
        self.assertIn("orion-chat-vault", merged)
        self.assertEqual(eoq_cfg_path.read_text(encoding="utf-8"), '{"custom_threshold": 123456}\n')
        self.assertEqual(custom_skill_path.read_text(encoding="utf-8"), "# Private customized keepalive\n")

        # Now test force=True: overwrites managed entries/files while STILL preserving user-custom-guard
        res_force = install_companion(force=True, python_exe="/usr/bin/python3")
        self.assertTrue(res_force["ok"])
        self.assertEqual(res_force["skipped_existing"], [])
        merged_after_force = json.loads(hooks_path.read_text(encoding="utf-8"))
        self.assertEqual(merged_after_force["user-custom-guard"], custom_hooks["user-custom-guard"])
        self.assertNotIn("custom_marker", merged_after_force["eoq-context-guard"])
        self.assertNotEqual(custom_skill_path.read_text(encoding="utf-8"), "# Private customized keepalive\n")

    def test_uninstall_preserves_other_hooks_and_unmanaged_workspace_skills(self) -> None:
        self.cfg_dir.mkdir(parents=True, exist_ok=True)
        hooks_path = self.cfg_dir / "hooks.json"
        hooks_path.write_text(
            json.dumps({"third-party-hook": {"enabled": True}}, indent=2),
            encoding="utf-8",
        )
        install_companion(workspace=self.ws_dir, include_rules=True, python_exe="/usr/bin/python3")

        # Add an unmanaged user skill inside engineering-core to verify surgical workspace removal
        extra_skill = self.ws_dir / ".agents" / "plugins" / "engineering-core" / "skills" / "custom-skill" / "SKILL.md"
        extra_skill.parent.mkdir(parents=True, exist_ok=True)
        extra_skill.write_text("# User extra skill\n", encoding="utf-8")

        un_res = uninstall_companion(
            workspace=self.ws_dir,
            remove_hooks=True,
            remove_global=True,
            remove_workspace=True,
        )
        self.assertTrue(un_res["ok"])
        self.assertEqual(set(un_res["hooks_removed"]), set(MANAGED_HOOK_KEYS))
        self.assertEqual(un_res["preserved_hook_keys"], ["third-party-hook"])

        remaining_hooks = json.loads(hooks_path.read_text(encoding="utf-8"))
        self.assertEqual(remaining_hooks, {"third-party-hook": {"enabled": True}})
        # eoq_guard_config.json user config is preserved on uninstall
        self.assertTrue((self.cfg_dir / "eoq_guard_config.json").is_file())
        # Managed global skills removed
        self.assertFalse((self.cfg_dir / "skills" / "session-recall" / "SKILL.md").exists())
        # Managed workspace skills removed, but user extra skill & manifest preserved
        self.assertFalse((self.ws_dir / ".agents" / "plugins" / "eoq-task-relay").exists())
        self.assertFalse(
            (self.ws_dir / ".agents" / "plugins" / "engineering-core" / "skills" / "version-seal").exists()
        )
        self.assertTrue(extra_skill.is_file())
        self.assertTrue((self.ws_dir / ".agents" / "plugins" / "engineering-core" / "plugin.json").is_file())

    def test_cli_subcommand_roundtrip_json(self) -> None:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli_main(
                [
                    "companion",
                    "install",
                    "--workspace",
                    str(self.ws_dir),
                    "--no-global-skills",
                    "--json",
                ]
            )
        self.assertEqual(rc, 0)
        install_out = json.loads(buf.getvalue())
        self.assertTrue(install_out["ok"])
        self.assertFalse((self.cfg_dir / "skills" / "session-recall" / "SKILL.md").exists())
        self.assertTrue((self.ws_dir / ".agents" / "plugins" / "eoq-task-relay" / "plugin.json").is_file())

        buf_status = io.StringIO()
        with contextlib.redirect_stdout(buf_status):
            rc_st = cli_main(["companion", "status", "--workspace", str(self.ws_dir), "--json"])
        self.assertEqual(rc_st, 0)
        st_out = json.loads(buf_status.getvalue())
        self.assertTrue(st_out["hooks"]["managed_keys"]["eoq-context-guard"]["installed"])
        self.assertFalse(st_out["global"]["plugin_acerola_orion"])

        buf_un = io.StringIO()
        with contextlib.redirect_stdout(buf_un):
            rc_un = cli_main(["companion", "uninstall", "--workspace", str(self.ws_dir), "--json"])
        self.assertEqual(rc_un, 0)
        un_out = json.loads(buf_un.getvalue())
        self.assertTrue(un_out["ok"])
        self.assertEqual(set(un_out["hooks_removed"]), set(MANAGED_HOOK_KEYS))


if __name__ == "__main__":
    unittest.main()
