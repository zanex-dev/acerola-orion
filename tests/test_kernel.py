# -*- coding: utf-8 -*-
"""
Unit tests for Acerola-Orion microkernel (`acerola_orion.core.kernel`)
and generic atomic configuration API (`load_app_config` / `update_app_config`).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

from acerola_orion import constants
from acerola_orion.core import (
    DISABLE_PLUGINS_ENV,
    DISABLED_PLUGINS_ENV,
    PLUGIN_ENTRYPOINT_GROUP,
    HookBus,
    HookEvents,
    OrionKernel,
    OrionPlugin,
    disabled_plugin_names,
    get_kernel,
    is_plugin_disabled,
    plugins_disabled,
    reset_kernel_for_tests,
)


class _FakeEntryPoint:
    def __init__(self, name: str, value: str, loader):
        self.name = name
        self.value = value
        self.group = PLUGIN_ENTRYPOINT_GROUP
        self._loader = loader

    def load(self):
        return self._loader()


class KernelAndConfigTestSuite(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="orion_kernel_test_")
        self.tmp_path = Path(self._tmp.name)
        self._old_home = os.environ.get("ACEROLA_ORION_HOME")
        self._old_disable = os.environ.get(DISABLE_PLUGINS_ENV)
        self._old_disabled_list = os.environ.get(DISABLED_PLUGINS_ENV)
        os.environ["ACEROLA_ORION_HOME"] = str(self.tmp_path / "orion_home")
        os.environ.pop(DISABLE_PLUGINS_ENV, None)
        os.environ.pop(DISABLED_PLUGINS_ENV, None)
        reset_kernel_for_tests()

    def tearDown(self) -> None:
        reset_kernel_for_tests()
        if self._old_home is None:
            os.environ.pop("ACEROLA_ORION_HOME", None)
        else:
            os.environ["ACEROLA_ORION_HOME"] = self._old_home
        if self._old_disable is None:
            os.environ.pop(DISABLE_PLUGINS_ENV, None)
        else:
            os.environ[DISABLE_PLUGINS_ENV] = self._old_disable
        if self._old_disabled_list is None:
            os.environ.pop(DISABLED_PLUGINS_ENV, None)
        else:
            os.environ[DISABLED_PLUGINS_ENV] = self._old_disabled_list
        self._tmp.cleanup()

    def test_01_app_config_preserves_unknown_extension_keys(self) -> None:
        self.assertEqual(constants.load_app_config(), {})

        def _seed(cfg: dict) -> None:
            cfg["custom_ext"] = {"enabled": True, "slots": [1, 2, 3]}
            cfg["theme_accent"] = "indigo"

        written = constants.update_app_config(_seed)
        self.assertEqual(written["custom_ext"], {"enabled": True, "slots": [1, 2, 3]})
        self.assertEqual(written["theme_accent"], "indigo")

        # Return-new-dict style mutator is also supported
        updated = constants.update_app_config(lambda c: {**c, "extra_flag": 42})
        self.assertEqual(updated["extra_flag"], 42)
        self.assertEqual(updated["custom_ext"], {"enabled": True, "slots": [1, 2, 3]})

        # Vault config mutation must preserve all unknown extension keys
        res = constants.configure_vault_auto_backup("off")
        self.assertTrue(res["ok"])
        self.assertFalse(res["enabled"])

        loaded = constants.load_app_config()
        self.assertFalse(loaded["vault_auto_backup"])
        self.assertEqual(loaded["custom_ext"], {"enabled": True, "slots": [1, 2, 3]})
        self.assertEqual(loaded["theme_accent"], "indigo")
        self.assertEqual(loaded["extra_flag"], 42)

        vault_cfg = constants.load_vault_config()
        self.assertFalse(vault_cfg["vault_auto_backup"])
        self.assertEqual(vault_cfg["custom_ext"]["slots"], [1, 2, 3])

    def test_02_app_config_corrupt_backup_and_trailing_comma_salvage(self) -> None:
        cfg_path = constants.get_config_path()
        cfg_path.parent.mkdir(parents=True, exist_ok=True)

        # Case 1: trailing comma -> salvageable + backup created on update
        raw_trailing = '{\n  "ext_saved": {"k": "v"},\n}\n'
        cfg_path.write_text(raw_trailing, encoding="utf-8")
        self.assertEqual(constants.load_app_config(), {"ext_saved": {"k": "v"}})

        res = constants.update_app_config(lambda c: c.__setitem__("added", True))
        self.assertEqual(res, {"ext_saved": {"k": "v"}, "added": True})
        baks = sorted(cfg_path.parent.glob("config.json.corrupt-*.bak"))
        self.assertEqual(len(baks), 1)
        self.assertEqual(baks[0].read_text(encoding="utf-8"), raw_trailing)

        # Case 2: irrecoverable corrupt payload -> backup preserved with unique name
        raw_broken = "{{{corrupt_json_payload"
        cfg_path.write_text(raw_broken, encoding="utf-8")
        self.assertEqual(constants.load_app_config(), {})

        res2 = constants.update_app_config(lambda c: c.__setitem__("recovered", 1))
        self.assertEqual(res2, {"recovered": 1})
        baks2 = sorted(cfg_path.parent.glob("config.json.corrupt-*.bak"))
        self.assertEqual(len(baks2), 2)
        self.assertIn(raw_broken, [b.read_text(encoding="utf-8") for b in baks2])
        self.assertEqual(json.loads(cfg_path.read_text(encoding="utf-8")), {"recovered": 1})

    def test_03_app_config_concurrent_updates_preserve_all_keys(self) -> None:
        errors = []

        def _worker(idx: int) -> None:
            try:
                constants.update_app_config(lambda c: c.__setitem__(f"worker_{idx}", idx))
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=_worker, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10.0)

        self.assertEqual(errors, [])
        final_cfg = constants.load_app_config()
        for i in range(8):
            self.assertEqual(final_cfg.get(f"worker_{i}"), i)

    def test_04_hook_bus_ordering_unsubscribe_and_fault_isolation(self) -> None:
        bus = HookBus()
        seen = []

        def _late(val: str) -> str:
            seen.append(f"late:{val}")
            return "late"

        def _bad(val: str) -> str:
            seen.append(f"bad:{val}")
            raise RuntimeError("boom from plugin")

        def _early(val: str) -> str:
            seen.append(f"early:{val}")
            return "early"

        unsub_late = bus.on("tick", _late, priority=10)
        bus.on("tick", _bad, priority=0)
        bus.on("tick", _early, priority=-5)

        self.assertTrue(bus.has_listeners("tick"))
        self.assertEqual(bus.listener_count("tick"), 3)
        self.assertFalse(bus.has_listeners("other"))

        with self.assertLogs("acerola_orion.core.kernel", level=logging.WARNING) as cm:
            results = bus.emit("tick", "x")

        self.assertEqual(seen, ["early:x", "bad:x", "late:x"])
        self.assertEqual(results, ["early", "late"])
        self.assertTrue(any("boom from plugin" in msg for msg in cm.output))

        # Unsubscribe idempotency + off()
        unsub_late()
        unsub_late()
        self.assertEqual(bus.listener_count("tick"), 2)
        self.assertTrue(bus.off("tick", _bad))
        self.assertFalse(bus.off("tick", _bad))
        self.assertEqual(bus.emit("tick", "y"), ["early"])

        bus.clear("tick")
        self.assertEqual(bus.listener_count("tick"), 0)

        with self.assertRaises(ValueError):
            bus.on("   ", _early)
        with self.assertRaises(TypeError):
            bus.on("tick", "not_callable")  # type: ignore[arg-type]

    def test_05_kernel_entrypoint_discovery_dedup_and_fault_isolation(self) -> None:
        self.assertEqual(PLUGIN_ENTRYPOINT_GROUP, "acerola_orion.plugins")

        class GoodClassPlugin:
            name = "good_class"

            def register_cli(self, subparsers) -> None:
                subparsers.add_parser("hello")

            def handle_cli(self, cmd: str, args) -> int | None:
                if cmd == "hello":
                    return 7
                return None

            def extra_launchers(self, py_exe: str, pyw_exe: str, pkg_root) -> dict[str, str]:
                return {"custom_tool.cmd": f'"{py_exe}" -m custom'}

            def register_hooks(self, bus: HookBus) -> None:
                bus.on("sample.event", lambda x: x + 1)

            def register_ui(self, ui_host) -> None:
                ui_host["tabs"].append("good_tab")

        class GoodFactoryPlugin:
            name = "good_factory"

            def register_hooks(self, bus: HookBus) -> None:
                bus.on("sample.event", lambda x: x + 10)

            def extra_launchers(self, py_exe: str, pyw_exe: str, pkg_root) -> dict[str, str]:
                return {"factory_tool.cmd": f'"{pyw_exe}" -m factory'}

        class FaultyHookPlugin:
            name = "faulty_plugin"

            def register_hooks(self, bus: HookBus) -> None:
                raise RuntimeError("register_hooks broken")

            def register_cli(self, subparsers) -> None:
                raise RuntimeError("register_cli broken")

            def handle_cli(self, cmd: str, args) -> int | None:
                raise RuntimeError("handle_cli broken")

            def extra_launchers(self, py_exe: str, pyw_exe: str, pkg_root) -> dict[str, str]:
                raise RuntimeError("extra_launchers broken")

            def register_ui(self, ui_host) -> None:
                raise RuntimeError("register_ui broken")

        self.assertIsInstance(GoodClassPlugin(), OrionPlugin)

        def _broken_loader():
            raise ImportError("missing optional extension dep")

        fake_eps = [
            _FakeEntryPoint("a_broken", "pkg:broken", _broken_loader),
            _FakeEntryPoint("b_faulty", "pkg:faulty", lambda: FaultyHookPlugin()),
            _FakeEntryPoint("c_class", "pkg:GoodClass", lambda: GoodClassPlugin),
            # Duplicate metadata entry must be deduplicated
            _FakeEntryPoint("c_class", "pkg:GoodClass", lambda: GoodClassPlugin),
            _FakeEntryPoint("d_factory", "pkg:make_plugin", lambda: (lambda: GoodFactoryPlugin())),
        ]

        with mock.patch("importlib.metadata.entry_points", return_value=fake_eps):
            with self.assertLogs("acerola_orion.core.kernel", level=logging.WARNING):
                k = get_kernel()

        self.assertIs(k, get_kernel())
        self.assertTrue(k.discovered)
        self.assertEqual(len(k.plugins), 3)
        self.assertTrue(k.has_plugin("good_class"))
        self.assertTrue(k.has_plugin("good_factory"))
        self.assertTrue(k.has_plugin("faulty_plugin"))
        # Both broken loader and faulty register_hooks recorded
        self.assertEqual(len(k.load_errors), 2)

        # Hooks work across surviving plugins
        self.assertEqual(k.emit("sample.event", 5), [6, 15])

        # CLI register & dispatch isolate faulty plugin and route to good plugin
        parser = argparse.ArgumentParser(prog="test")
        subparsers = parser.add_subparsers(dest="cmd")
        with self.assertLogs("acerola_orion.core.kernel", level=logging.WARNING):
            k.register_cli(subparsers)
        parsed = parser.parse_args(["hello"])
        with self.assertLogs("acerola_orion.core.kernel", level=logging.WARNING):
            self.assertEqual(k.handle_cli("hello", parsed), 7)
        with self.assertLogs("acerola_orion.core.kernel", level=logging.WARNING):
            self.assertIsNone(k.handle_cli("unknown", parsed))

        # Extra launchers collect and merge across plugins
        with self.assertLogs("acerola_orion.core.kernel", level=logging.WARNING):
            launchers = k.collect_extra_launchers("python.exe", "pythonw.exe", self.tmp_path)
        self.assertEqual(
            launchers,
            {
                "custom_tool.cmd": '"python.exe" -m custom',
                "factory_tool.cmd": '"pythonw.exe" -m factory',
            },
        )

        # UI slot dispatch isolates faulty plugin
        ui_host: dict = {"tabs": []}
        with self.assertLogs("acerola_orion.core.kernel", level=logging.WARNING):
            k.register_ui(ui_host)
        self.assertEqual(ui_host["tabs"], ["good_tab"])

    def test_06_disable_plugins_env_bare_core_isolation(self) -> None:
        called = {"count": 0}

        def _fake_entry_points(*args, **kwargs):
            called["count"] += 1
            return []

        for truthy in ("1", "true", "TRUE", "True"):
            reset_kernel_for_tests()
            os.environ[DISABLE_PLUGINS_ENV] = truthy
            self.assertTrue(plugins_disabled())
            with mock.patch("importlib.metadata.entry_points", side_effect=_fake_entry_points):
                k = get_kernel()
            self.assertTrue(k.discovered)
            self.assertEqual(k.plugins, ())
            self.assertEqual(called["count"], 0)

        for falsy in ("0", "false", ""):
            reset_kernel_for_tests()
            os.environ[DISABLE_PLUGINS_ENV] = falsy
            self.assertFalse(plugins_disabled())
            with mock.patch("importlib.metadata.entry_points", side_effect=_fake_entry_points):
                k = get_kernel()
            self.assertTrue(k.discovered)
            self.assertGreater(called["count"], 0)

    def test_07_reset_kernel_for_tests_clears_singleton_and_bus(self) -> None:
        os.environ[DISABLE_PLUGINS_ENV] = "1"
        k1 = get_kernel()
        k1.bus.on("ev", lambda: 123)
        self.assertEqual(k1.emit("ev"), [123])

        reset_kernel_for_tests()
        self.assertEqual(k1.bus.listener_count("ev"), 0)

        k2 = get_kernel()
        self.assertIsNot(k1, k2)
        self.assertEqual(k2.emit("ev"), [])

    def test_08_core_package_clean_room_and_packaging_include(self) -> None:
        repo_root = Path(__file__).resolve().parent.parent
        core_files = sorted((repo_root / "acerola_orion" / "core").glob("*.py"))
        self.assertGreaterEqual(len(core_files), 2)

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
        for f in core_files + [repo_root / "acerola_orion" / "constants.py"]:
            low = f.read_text(encoding="utf-8").lower()
            for tok in forbidden:
                self.assertNotIn(tok.lower(), low, f"Forbidden token {tok!r} found in {f.name}")

        pyproject_text = (repo_root / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn('include = ["acerola_orion*"]', pyproject_text)

    def test_09_hook_events_constants_and_per_plugin_disable_filter(self) -> None:
        expected_events = (
            "probe_guard",
            "enrich_telemetry",
            "render_status_lines",
            "enrich_radar",
            "guard_start",
            "guard_ls_seen",
            "guard_tick",
            "guard_stop",
        )
        self.assertEqual(HookEvents.PROBE_GUARD, "probe_guard")
        self.assertEqual(HookEvents.ENRICH_TELEMETRY, "enrich_telemetry")
        self.assertEqual(HookEvents.RENDER_STATUS_LINES, "render_status_lines")
        self.assertEqual(HookEvents.ENRICH_RADAR, "enrich_radar")
        self.assertEqual(HookEvents.GUARD_START, "guard_start")
        self.assertEqual(HookEvents.GUARD_LS_SEEN, "guard_ls_seen")
        self.assertEqual(HookEvents.GUARD_TICK, "guard_tick")
        self.assertEqual(HookEvents.GUARD_STOP, "guard_stop")
        self.assertEqual(HookEvents.ALL, expected_events)

        self.assertEqual(disabled_plugin_names(), frozenset())
        self.assertFalse(is_plugin_disabled("alpha"))

        os.environ[DISABLED_PLUGINS_ENV] = " Alpha_EP , , Beta_Plugin ,  "
        self.assertEqual(disabled_plugin_names(), frozenset({"alpha_ep", "beta_plugin"}))
        self.assertTrue(is_plugin_disabled("alpha_ep"))
        self.assertTrue(is_plugin_disabled("BETA_PLUGIN"))
        self.assertFalse(is_plugin_disabled("gamma"))
        self.assertFalse(is_plugin_disabled("   "))

        loader_calls: list[str] = []
        hook_calls: list[str] = []

        class _BetaPlugin:
            name = "beta_plugin"

            def register_hooks(self, bus: HookBus) -> None:
                hook_calls.append("beta")
                bus.on(HookEvents.ENRICH_RADAR, lambda x: x)

        class _GammaPlugin:
            name = "gamma_plugin"

            def register_hooks(self, bus: HookBus) -> None:
                hook_calls.append("gamma")
                bus.on(HookEvents.ENRICH_RADAR, lambda x: f"{x}:gamma")

        def _alpha_loader():
            loader_calls.append("alpha")
            return _GammaPlugin()

        def _beta_loader():
            loader_calls.append("beta")
            return _BetaPlugin()

        def _gamma_loader():
            loader_calls.append("gamma")
            return _GammaPlugin()

        fake_eps = [
            _FakeEntryPoint("alpha_ep", "pkg:alpha", _alpha_loader),
            _FakeEntryPoint("beta_ep", "pkg:beta", _beta_loader),
            _FakeEntryPoint("gamma_ep", "pkg:gamma", _gamma_loader),
        ]

        with mock.patch("importlib.metadata.entry_points", return_value=fake_eps):
            k = get_kernel()

        # Entry point matched by name must never even be loaded
        self.assertNotIn("alpha", loader_calls)
        # Plugin matched by .name must not register hooks or appear in plugins
        self.assertIn("beta", loader_calls)
        self.assertEqual(hook_calls, ["gamma"])
        self.assertEqual([getattr(p, "name", "") for p in k.plugins], ["gamma_plugin"])
        self.assertTrue(k.has_plugin("gamma_plugin"))
        self.assertFalse(k.has_plugin("beta_plugin"))
        self.assertEqual(k.emit(HookEvents.ENRICH_RADAR, "ok"), ["ok:gamma"])

        # Global disable still supersedes per-plugin list
        os.environ[DISABLE_PLUGINS_ENV] = "1"
        self.assertTrue(is_plugin_disabled("gamma_plugin"))


if __name__ == "__main__":
    unittest.main()
