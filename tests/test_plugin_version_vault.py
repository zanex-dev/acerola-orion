# -*- coding: utf-8 -*-
"""Unit & integration tests for built-in `VersionVaultPlugin` (`acerola_orion.plugins.version_vault*`)."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest import mock

_PUB_ROOT = Path(__file__).resolve().parent.parent
if str(_PUB_ROOT) not in sys.path:
    sys.path.insert(0, str(_PUB_ROOT))

from acerola_orion.core.kernel import HookBus, OrionKernel  # noqa: E402
from acerola_orion.plugins.version_scanner import (  # noqa: E402
    CATEGORY_STALE_PROJECT,
    KEEP_MARKER_FILENAME,
    scan_old_versions,
)
from acerola_orion.plugins.version_vault import (  # noqa: E402
    VaultVerificationError,
    WorkspaceVersionVault,
)
from acerola_orion.plugins.version_vault_plugin import (  # noqa: E402
    ARCHIVE_TAB_KEY,
    ARCHIVE_TAB_TITLE,
    VersionVaultPlugin,
)


def _run_cli(plugin: VersionVaultPlugin, argv: list[str]) -> tuple[int, str]:
    parser = argparse.ArgumentParser(prog="orion")
    sub = parser.add_subparsers(dest="cmd")
    plugin.register_cli(sub)
    args = parser.parse_args(argv)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = plugin.handle_cli(args.cmd, args)
    assert rc is not None
    return rc, buf.getvalue()


def _init_bare_git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    res = subprocess.run(
        ["git", "init", "--bare", str(path)],
        capture_output=True,
        text=True,
        check=False,
    )
    if res.returncode != 0:
        raise RuntimeError(f"git init --bare failed: {res.stderr or res.stdout}")
    return path


class _FakeUiHost:
    def __init__(self) -> None:
        self.tabs: list[dict] = []
        self.tray_builders: list = []
        self.actions: list[str] = []
        self.logs: list[str] = []
        self.palette = {"BG_DARK": "#0f172a", "BORDER_SUBTLE": "#334155"}

    def add_tab(self, *, key, title, factory_fn, on_select=None, on_refresh=None) -> None:
        self.tabs.append(
            {
                "key": key,
                "title": title,
                "factory_fn": factory_fn,
                "on_select": on_select,
                "on_refresh": on_refresh,
            }
        )

    def add_tray_items(self, builder_fn) -> None:
        self.tray_builders.append(builder_fn)

    def run_action(self, label: str, fn):
        self.actions.append(label)
        return fn()

    def append_console(self, msg: str) -> None:
        self.logs.append(msg)

    def ui_call(self, cb) -> None:
        cb()

    def px(self, n: int) -> int:
        return int(n)


class TestPluginVersionVault(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="orion_pub_vault_test_")
        self.root = Path(self._tmp.name)
        self.vault_dir = self.root / "vault"
        self.config_path = self.root / "config.json"
        self.ws_dir = self.root / "workspace"
        self.ws_dir.mkdir(parents=True, exist_ok=True)

        self.plugin = VersionVaultPlugin(
            vault_dir=self.vault_dir,
            config_path=self.config_path,
            workspaces_override=[self.ws_dir],
            editable_roots_override=[],
        )

    def tearDown(self) -> None:
        self.plugin._on_guard_stop({"state": {}, "log": lambda _m: None})
        self._tmp.cleanup()

    def test_01_standalone_kernel_launchers_and_lazy_import(self) -> None:
        """Verify plugin metadata, 4 compatible desktop launchers, and zero coupling."""
        code = (
            "import json, sys\n"
            "from acerola_orion.plugins.version_vault_plugin import (\n"
            "  VersionVaultPlugin, ARCHIVE_TAB_KEY, ARCHIVE_TAB_TITLE,\n"
            ")\n"
            "forbidden = [\n"
            "  'acerola_orion.plugins.task_board_tab',\n"
            "  'acerola_orion.plugins.tasks_plugin',\n"
            "]\n"
            "loaded = [m for m in forbidden if m in sys.modules]\n"
            "print(json.dumps({\n"
            "  'name': VersionVaultPlugin().name,\n"
            "  'key': ARCHIVE_TAB_KEY,\n"
            "  'title': ARCHIVE_TAB_TITLE,\n"
            "  'loaded': loaded,\n"
            "}, ensure_ascii=False))\n"
        )
        env = os.environ.copy()
        env["PYTHONPATH"] = str(_PUB_ROOT)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        proc = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
            check=True,
        )
        out = json.loads(proc.stdout.strip())
        self.assertEqual(out["name"], "version_vault")
        self.assertEqual(out["key"], "version_vault")
        self.assertEqual(out["title"], "🧊 版本冷库 (Archive)")
        self.assertEqual(out["loaded"], [])

        # Standalone OrionKernel registration, launchers, and CLI delegation
        kernel = OrionKernel()
        kernel.register(self.plugin)
        self.assertTrue(kernel.has_plugin("version_vault"))

        launchers = kernel.collect_extra_launchers("python.exe", "pythonw.exe", self.root)
        self.assertEqual(
            sorted(launchers.keys()),
            [
                "orion-archive-push.ps1",
                "orion-archive-scan.ps1",
                "orion-archive-seal.ps1",
                "orion-archive-status.ps1",
            ],
        )
        self.assertIn("-m acerola_orion archive scan", launchers["orion-archive-scan.ps1"])
        self.assertIn("-m acerola_orion archive seal", launchers["orion-archive-seal.ps1"])
        self.assertIn("-m acerola_orion archive status", launchers["orion-archive-status.ps1"])
        self.assertIn("-m acerola_orion archive push", launchers["orion-archive-push.ps1"])

        parser = argparse.ArgumentParser(prog="orion")
        sub = parser.add_subparsers(dest="cmd")
        kernel.register_cli(sub)
        parsed = parser.parse_args(["archive", "status", "--json"])
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            k_rc = kernel.handle_cli(parsed.cmd, parsed)
        self.assertEqual(k_rc, 0)
        self.assertTrue(json.loads(buf.getvalue())["ok"])

    def test_02_cli_all_nine_subcommands_end_to_end(self) -> None:
        """Exercise scan/seal/status/list/jobs/restore/push/worker/policy end-to-end."""
        proj = self.ws_dir / "demo_proj"
        vdir = proj / "_versions"
        vdir.mkdir(parents=True, exist_ok=True)
        old_snap = vdir / "app_v1.py"
        old_snap.write_text("print('v1')\n", encoding="utf-8")
        (proj / "note_v1.txt").write_text("note v1\n", encoding="utf-8")
        (proj / "note_v2.txt").write_text("note v2\n", encoding="utf-8")

        # 1. scan
        rc, out = _run_cli(self.plugin, ["archive", "scan", "--json"])
        self.assertEqual(rc, 0)
        scan_data = json.loads(out)
        self.assertTrue(scan_data["ok"])
        self.assertEqual(scan_data["candidate_count"], 2)

        # 2. seal default dry-run (must NOT modify source or vault)
        rc, out = _run_cli(self.plugin, ["archive", "seal", "--json"])
        self.assertEqual(rc, 0)
        dry_data = json.loads(out)
        self.assertTrue(dry_data["dry_run"])
        self.assertEqual(dry_data["count"], 2)
        self.assertTrue(old_snap.exists())
        self.assertTrue((proj / "note_v1.txt").exists())

        # 3. seal --sync --keep-source
        rc, out = _run_cli(
            self.plugin,
            [
                "archive",
                "seal",
                "--sync",
                "--category",
                "versions_dir",
                "--keep-source",
                "--reason",
                "keep_copy_test",
                "--json",
            ],
        )
        self.assertEqual(rc, 0)
        sync_keep = json.loads(out)
        self.assertTrue(sync_keep["ok"])
        self.assertEqual(sync_keep["mode"], "sync")
        self.assertFalse(sync_keep["evict_source"])
        self.assertEqual(sync_keep["completed_items"], 1)
        self.assertTrue(old_snap.exists())
        snap_id_1 = sync_keep["snapshot_ids"][0]

        # 4. seal --confirm (async enqueue + mock detached worker) + jobs + worker
        with mock.patch.object(
            WorkspaceVersionVault,
            "spawn_async_worker",
            return_value={"spawned": True, "pid": 99999, "log_path": "mock.log"},
        ) as m_spawn:
            rc, out = _run_cli(
                self.plugin,
                [
                    "archive",
                    "seal",
                    "--confirm",
                    "--category",
                    "versioned_sibling",
                    "--reason",
                    "async_seal_test",
                    "--json",
                ],
            )
        self.assertEqual(rc, 0)
        m_spawn.assert_called_once()
        async_data = json.loads(out)
        self.assertEqual(async_data["mode"], "async")
        self.assertEqual(async_data["job"]["status"], "queued")
        self.assertTrue((proj / "note_v1.txt").exists())

        rc, out = _run_cli(self.plugin, ["archive", "jobs", "--status", "queued", "--json"])
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out)["count"], 1)

        rc, out = _run_cli(self.plugin, ["archive", "worker", "--json"])
        self.assertEqual(rc, 0)
        worker_data = json.loads(out)
        self.assertEqual(worker_data["processed_jobs"], 1)
        self.assertEqual(worker_data["snapshots_created"], 1)
        self.assertFalse((proj / "note_v1.txt").exists())

        # 5. list & restore (target + conflict guard + --force)
        rc, out = _run_cli(self.plugin, ["archive", "list", "--project", "demo_proj", "--json"])
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out)["count"], 2)

        restore_dir = self.root / "restored"
        rc, out = _run_cli(
            self.plugin,
            ["archive", "restore", snap_id_1, "--target", str(restore_dir), "--json"],
        )
        self.assertEqual(rc, 0)
        rest_data = json.loads(out)
        self.assertTrue(rest_data["verified"])
        self.assertEqual((restore_dir / "app_v1.py").read_text(encoding="utf-8"), "print('v1')\n")

        rc, out = _run_cli(
            self.plugin,
            ["archive", "restore", snap_id_1, "--target", str(restore_dir), "--json"],
        )
        self.assertEqual(rc, 1)
        self.assertFalse(json.loads(out)["ok"])

        rc, out = _run_cli(
            self.plugin,
            ["archive", "restore", snap_id_1, "--target", str(restore_dir), "--force", "--json"],
        )
        self.assertEqual(rc, 0)

        # 6. policy update (mode / remote / settle-sec / pin / unpin)
        bare_repo = _init_bare_git_repo(self.root / "remote.git")
        rc, out = _run_cli(
            self.plugin,
            [
                "archive",
                "policy",
                "--mode",
                "auto_after_settle",
                "--remote",
                str(bare_repo),
                "--settle-sec",
                "120",
                "--pin",
                "demo_proj/_versions/app_v1.py",
                "--json",
            ],
        )
        self.assertEqual(rc, 0)
        pol_data = json.loads(out)
        self.assertEqual(pol_data["mode"], "auto_after_settle")
        self.assertEqual(pol_data["settle_sec"], 120)
        self.assertEqual(pol_data["remote_url"], str(bare_repo))
        self.assertIn("demo_proj/_versions/app_v1.py", pol_data["pinned_paths"])

        rc, out = _run_cli(self.plugin, ["archive", "policy", "--settle-sec", "-5", "--json"])
        self.assertEqual(rc, 1)
        self.assertFalse(json.loads(out)["ok"])

        rc, out = _run_cli(
            self.plugin,
            ["archive", "policy", "--unpin", "demo_proj/_versions/app_v1.py", "--json"],
        )
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out)["pinned_paths"], [])

        # 7. push dry-run -> real push -> second push skipped
        rc, out = _run_cli(self.plugin, ["archive", "push", "--dry-run", "--json"])
        self.assertEqual(rc, 0)
        self.assertTrue(json.loads(out)["dry_run"])

        rc, out = _run_cli(self.plugin, ["archive", "push", "--json"])
        self.assertEqual(rc, 0)
        push_res = json.loads(out)
        self.assertTrue(push_res["ok"])
        self.assertFalse(push_res["skipped"])

        rc, out = _run_cli(self.plugin, ["archive", "push", "--json"])
        self.assertEqual(rc, 0)
        self.assertTrue(json.loads(out)["skipped"])

        # 8. status (with `--json` before subcommand)
        rc, out = _run_cli(self.plugin, ["archive", "--json", "status"])
        self.assertEqual(rc, 0)
        st_data = json.loads(out)
        self.assertTrue(st_data["ok"])
        self.assertEqual(st_data["snapshot_count"], 2)
        self.assertEqual(st_data["mode"], "auto_after_settle")

    def test_03_append_only_sha256_verification_and_detached_worker_module(self) -> None:
        """Verify append-only surface, bidirectional SHA-256 tamper abort, and real worker module."""
        vault = WorkspaceVersionVault(vault_dir=self.vault_dir, config_path=self.config_path)

        # Physical append-only: no delete/purge/prune API on WorkspaceVersionVault
        for forbidden_attr in ("delete_snapshot", "purge_snapshots", "prune_snapshots", "remove_snapshot"):
            self.assertFalse(hasattr(vault, forbidden_attr))

        # Bidirectional SHA-256 verification aborts and preserves source when payload mismatches
        src = self.ws_dir / "integrity_src.txt"
        src.write_text("original immutable content\n", encoding="utf-8")

        orig_verify = WorkspaceVersionVault._verify_bidirectional_integrity

        def _tamper_and_verify(self_v, source_path, snapshot_stage_dir, manifest):
            tampered = snapshot_stage_dir / "payload" / source_path.name
            tampered.write_text("corrupted bytes!\n", encoding="utf-8")
            return orig_verify(self_v, source_path, snapshot_stage_dir, manifest)

        with mock.patch.object(WorkspaceVersionVault, "_verify_bidirectional_integrity", _tamper_and_verify):
            res = vault.seal_items_sync([src], trigger_reason="tamper_test", evict_source=True)
        self.assertFalse(res["ok"])
        self.assertEqual(res["failed_items"], 1)
        self.assertEqual(res["errors"][0]["error_type"], VaultVerificationError.__name__)
        self.assertTrue(src.is_file())
        self.assertEqual(src.read_text(encoding="utf-8"), "original immutable content\n")
        self.assertEqual(vault.get_status().snapshot_count, 0)

        # Verify detached worker command uses `acerola_orion.plugins.version_vault` and executes cleanly
        vault.enqueue_seal_job([src], trigger_reason="worker_module_test", evict_source=True)
        env = os.environ.copy()
        env["PYTHONPATH"] = str(_PUB_ROOT)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "acerola_orion.plugins.version_vault",
                "--run-worker",
                "--vault-dir",
                str(self.vault_dir),
                "--config-path",
                str(self.config_path),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
            check=True,
        )
        summary = json.loads(proc.stdout.strip())
        self.assertEqual(summary["processed_jobs"], 1)
        self.assertEqual(summary["snapshots_created"], 1)
        self.assertFalse(src.exists())
        self.assertEqual(vault.get_status().snapshot_count, 1)

        # Also verify spawn_async_worker builds `-m acerola_orion.plugins.version_vault`
        with mock.patch("acerola_orion.plugins.version_vault.subprocess.Popen") as m_popen:
            fake_proc = mock.MagicMock()
            fake_proc.pid = 42424
            m_popen.return_value = fake_proc
            spawn_res = vault.spawn_async_worker(max_jobs=7)
        self.assertTrue(spawn_res["spawned"])
        spawned_cmd = m_popen.call_args[0][0]
        self.assertEqual(spawned_cmd[1:4], ["-m", "acerola_orion.plugins.version_vault", "--run-worker"])

    def test_04_scanner_protection_guardrails(self) -> None:
        """Verify `.orion-vault-keep` and editable-root hard protection in scanner."""
        keep_dir = self.ws_dir / "proj_keep" / "_versions"
        keep_dir.mkdir(parents=True, exist_ok=True)
        (keep_dir / KEEP_MARKER_FILENAME).write_text("keep\n", encoding="utf-8")
        (keep_dir / "old_v1.py").write_text("x = 1\n", encoding="utf-8")

        ed_proj = self.ws_dir / "editable_pkg"
        ed_proj.mkdir(parents=True, exist_ok=True)
        (ed_proj / "mod.py").write_text("a = 1\n", encoding="utf-8")
        old_ts = time.time() - 5 * 86400
        os.utime(ed_proj / "mod.py", (old_ts, old_ts))
        os.utime(ed_proj, (old_ts, old_ts))

        report = scan_old_versions(
            [self.ws_dir],
            include_stale_projects=True,
            stale_days=1.0,
            editable_roots=[ed_proj],
        )
        self.assertEqual(len(report.candidates), 0)
        prot_paths = {Path(p.source_path).resolve() for p in report.protected_items}
        self.assertIn((keep_dir / "old_v1.py").resolve(), prot_paths)
        self.assertIn(ed_proj.resolve(), prot_paths)

    def test_05_guard_hooks_throttle_confirm_mode_and_queue_consumption(self) -> None:
        """Verify >=30s throttle, confirm mode pending_seals without eviction, and queue drain."""
        proj = self.ws_dir / "alpha"
        proj.mkdir(parents=True, exist_ok=True)
        v1 = proj / "spec_v1.md"
        v1.write_text("v1 content\n", encoding="utf-8")

        bus = HookBus()
        self.plugin.register_hooks(bus)
        logs: list[str] = []
        t0 = 1_750_000_000.0
        ctx: dict = {"state": {}, "log": logs.append, "now": t0}

        bus.emit("guard_start", ctx)
        bus.emit("guard_tick", ctx)
        self.assertTrue(self.plugin.wait_for_idle(5.0))
        self.assertEqual(ctx["state"]["version_vault"]["last_error"], "")
        self.assertEqual(ctx["state"]["version_vault"]["cycles"], 1)

        # Create v2 and fire tick only 15s later -> throttled (<30s)
        (proj / "spec_v2.md").write_text("v2 content\n", encoding="utf-8")
        ctx["now"] = t0 + 15.0
        bus.emit("guard_tick", ctx)
        self.assertTrue(self.plugin.wait_for_idle(5.0))
        self.assertEqual(ctx["state"]["version_vault"]["cycles"], 1)

        # Enqueue an explicit async job and advance >=30s
        manual_file = proj / "manual_old.txt"
        manual_file.write_text("manual\n", encoding="utf-8")
        vault = WorkspaceVersionVault(vault_dir=self.vault_dir, config_path=self.config_path)
        vault.enqueue_seal_job([manual_file], trigger_reason="queued_for_guard", evict_source=True)

        ctx["now"] = t0 + 31.0
        bus.emit("guard_tick", ctx)
        self.assertTrue(self.plugin.wait_for_idle(5.0))
        self.assertEqual(ctx["state"]["version_vault"]["last_error"], "")
        self.assertEqual(ctx["state"]["version_vault"]["cycles"], 2)

        self.assertFalse(manual_file.exists())
        self.assertEqual(vault.get_status().snapshot_count, 1)

        # Confirm mode records spec_v1.md in pending_seals.json without evicting
        self.assertTrue(v1.exists())
        pending_file = self.vault_dir / "pending_seals.json"
        self.assertTrue(pending_file.is_file())
        pending_doc = json.loads(pending_file.read_text(encoding="utf-8"))
        pending_paths = [it["source_path"] for it in pending_doc["items"]]
        self.assertIn(os.path.abspath(str(v1)), pending_paths)

        bus.emit("guard_stop", ctx)
        self.assertTrue(ctx["state"]["version_vault"]["stopped"])

    def test_06_guard_auto_after_settle_evicts_non_stale_only_after_grace_period(self) -> None:
        """Verify auto_after_settle waits settle_sec and never auto-evicts stale_project."""
        proj = self.ws_dir / "beta"
        proj.mkdir(parents=True, exist_ok=True)
        v1 = proj / "draft_v1.py"
        v1.write_text("print(1)\n", encoding="utf-8")

        stale_proj = self.ws_dir / "dormant_proj"
        stale_proj.mkdir(parents=True, exist_ok=True)
        (stale_proj / "main.py").write_text("print('dormant')\n", encoding="utf-8")

        self.plugin.update_full_policy(mode="auto_after_settle", settle_sec=60)

        bus = HookBus()
        self.plugin.register_hooks(bus)
        t0 = time.time()
        ctx: dict = {"state": {}, "log": lambda _m: None, "now": t0}
        bus.emit("guard_start", ctx)
        bus.emit("guard_tick", ctx)
        self.assertTrue(self.plugin.wait_for_idle(5.0))

        (proj / "draft_v2.py").write_text("print(2)\n", encoding="utf-8")
        os.utime(v1, (t0 - 120, t0 - 120))

        vault = WorkspaceVersionVault(vault_dir=self.vault_dir, config_path=self.config_path)
        self.plugin._save_pending_seals(
            vault,
            [
                {
                    "source_path": os.path.abspath(str(stale_proj)),
                    "category": CATEGORY_STALE_PROJECT,
                    "project": "dormant_proj",
                    "workspace": str(self.ws_dir),
                    "trigger_kind": "manual",
                    "trigger_reason": "stale",
                    "requires_explicit_confirm": True,
                    "first_seen_at": t0 - 1000,
                    "last_seen_at": t0 - 1000,
                }
            ],
            now_ts=t0,
        )

        # Tick at t0 + 35: trigger detected -> recorded, not yet settled
        ctx["now"] = t0 + 35.0
        bus.emit("guard_tick", ctx)
        self.assertTrue(self.plugin.wait_for_idle(5.0))
        self.assertTrue(v1.exists())
        self.assertEqual(vault.get_status().snapshot_count, 0)

        # Tick at t0 + 70: 35s < 60s, still waiting
        ctx["now"] = t0 + 70.0
        bus.emit("guard_tick", ctx)
        self.assertTrue(self.plugin.wait_for_idle(5.0))
        self.assertTrue(v1.exists())
        self.assertEqual(vault.get_status().snapshot_count, 0)

        # Tick at t0 + 105: 70s >= 60s -> auto-sealed
        ctx["now"] = t0 + 105.0
        bus.emit("guard_tick", ctx)
        self.assertTrue(self.plugin.wait_for_idle(5.0))
        self.assertFalse(v1.exists())
        self.assertEqual(vault.get_status().snapshot_count, 1)

        # stale_project remains untouched
        self.assertTrue(stale_proj.is_dir())
        self.assertTrue((stale_proj / "main.py").is_file())

    def test_07_register_ui_degrades_gracefully_and_mounts_tray_and_tab(self) -> None:
        """Verify UI tab degrades when absent, mounts when available, and registers 2 tray items."""
        host1 = _FakeUiHost()
        with mock.patch(
            "acerola_orion.plugins.version_vault_plugin._try_import_version_vault_tab",
            return_value=None,
        ):
            self.plugin.register_ui(host1)

        self.assertEqual(len(host1.tabs), 0)
        self.assertEqual(len(host1.tray_builders), 1)
        tray_items = host1.tray_builders[0]()
        self.assertEqual(len(tray_items), 2)

        scan_label, scan_cb = tray_items[0]
        self.assertIn("扫描", scan_label)
        scan_res = scan_cb()
        self.assertTrue(scan_res.ok)
        self.assertIn("托盘 · 扫描工作区旧版本", host1.actions)

        push_label, push_cb = tray_items[1]
        self.assertIn("推送", push_label)
        push_res = push_cb()
        self.assertFalse(push_res.ok)
        self.assertIn("托盘 · 推送版本冷库到云端", host1.actions)

        class _FakeVaultTab:
            def __init__(self, parent, **kwargs) -> None:
                self.parent = parent
                self.kwargs = kwargs
                self.packed = False
                self.refreshes: list[bool] = []

            def pack(self, **kwargs) -> None:
                self.packed = True

            def refresh_async(self, silent: bool = False) -> None:
                self.refreshes.append(bool(silent))

        fake_mod = types.ModuleType("acerola_orion.plugins.version_vault_tab")
        fake_mod.VersionVaultTab = _FakeVaultTab  # type: ignore[attr-defined]

        host2 = _FakeUiHost()
        with mock.patch.dict(sys.modules, {"acerola_orion.plugins.version_vault_tab": fake_mod}):
            self.plugin.register_ui(host2)
            self.assertEqual(len(host2.tabs), 1)
            tab_spec = host2.tabs[0]
            self.assertEqual(tab_spec["key"], ARCHIVE_TAB_KEY)
            self.assertEqual(tab_spec["title"], ARCHIVE_TAB_TITLE)

            tab_instance = tab_spec["factory_fn"](object(), host2)
            self.assertIs(self.plugin._archive_tab, tab_instance)
            self.assertTrue(tab_instance.packed)
            self.assertEqual(tab_instance.kwargs["palette"]["BORDER_DIM"], "#334155")

            tab_spec["on_select"]()
            tab_spec["on_refresh"](None)
            self.assertEqual(tab_instance.refreshes, [True, True])


if __name__ == "__main__":
    unittest.main()
