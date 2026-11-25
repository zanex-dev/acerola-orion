# -*- coding: utf-8 -*-
"""Isolated unit tests for daily lossless log archiving and stale hub cleanup."""

from __future__ import annotations

import contextlib
from datetime import datetime
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from acerola_orion import __main__ as cli_main
from acerola_orion import guillotine, panel


class TestDailyLogArchive(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.orion_home = self.root / "orion_home"
        self.orion_home.mkdir(parents=True, exist_ok=True)
        self.host_logs_dir = self.root / "host_logs"
        self.host_logs_dir.mkdir(parents=True, exist_ok=True)
        self.daemon_dir = self.root / "agy_daemon"
        self.daemon_dir.mkdir(parents=True, exist_ok=True)

        self._env_patch = mock.patch.dict(
            os.environ,
            {"ACEROLA_ORION_HOME": str(self.orion_home)},
            clear=False,
        )
        self._env_patch.start()

    def tearDown(self) -> None:
        self._env_patch.stop()
        self._tmp.cleanup()

    def test_cross_day_guillotine_split_zero_overwrite_and_idempotent(self) -> None:
        log_path = guillotine.get_guillotine_log_path()
        log_path.write_text(
            "\n".join(
                [
                    "[2026-11-23 23:58:00] oldest day event",
                    "[2026-11-24 08:15:10] yesterday event A",
                    "  continuation detail for yesterday",
                    "[2026-11-24 22:40:00] yesterday event B",
                    "[2026-11-25 00:05:00] today event 1",
                    "[2026-11-25 09:30:00] today event 2",
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        ref_dt = datetime(2026, 11, 25, 10, 0, 0).astimezone()
        res1 = guillotine.rollover_daily_logs(
            now_dt=ref_dt,
            include_host_logs=False,
            clean_hubs=False,
        )
        self.assertTrue(res1["ok"])
        self.assertEqual(res1["guillotine"]["archived_dates"], ["2026-11-23", "2026-11-24"])
        self.assertEqual(res1["guillotine"]["kept_today_lines"], 2)

        arc_root = guillotine.get_log_archive_dir()
        self.assertEqual(arc_root, self.orion_home / "log_archives")
        f23 = arc_root / "2026-11-23" / "guillotine.log"
        f24 = arc_root / "2026-11-24" / "guillotine.log"
        self.assertTrue(f23.is_file())
        self.assertTrue(f24.is_file())
        self.assertEqual(
            f23.read_text(encoding="utf-8"),
            "[2026-11-23 23:58:00] oldest day event\n",
        )
        self.assertEqual(
            f24.read_text(encoding="utf-8"),
            "[2026-11-24 08:15:10] yesterday event A\n"
            "  continuation detail for yesterday\n"
            "[2026-11-24 22:40:00] yesterday event B\n",
        )
        self.assertEqual(
            log_path.read_text(encoding="utf-8"),
            "[2026-11-25 00:05:00] today event 1\n[2026-11-25 09:30:00] today event 2\n",
        )

        # Second call on the same day is strictly idempotent.
        res2 = guillotine.rollover_daily_logs(
            now_dt=ref_dt,
            include_host_logs=False,
            clean_hubs=False,
        )
        self.assertEqual(res2["guillotine"]["archived_files"], [])
        self.assertEqual(res2["guillotine"]["archived_lines"], 0)
        self.assertFalse((arc_root / "2026-11-24" / "guillotine_part1.log").exists())

        # Late past-day lines never overwrite existing 2026-11-24/guillotine.log.
        orig_24_text = f24.read_text(encoding="utf-8")
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write("[2026-11-24 23:59:59] late flushed line\n")
        res3 = guillotine.rollover_daily_logs(
            now_dt=ref_dt,
            include_host_logs=False,
            clean_hubs=False,
        )
        self.assertEqual(len(res3["guillotine"]["archived_files"]), 1)
        part1 = arc_root / "2026-11-24" / "guillotine_part1.log"
        self.assertTrue(part1.is_file())
        self.assertEqual(f24.read_text(encoding="utf-8"), orig_24_text)
        self.assertEqual(part1.read_text(encoding="utf-8"), "[2026-11-24 23:59:59] late flushed line\n")

    def test_same_day_512kb_split_preserves_all_parts_zero_overwrite(self) -> None:
        log_path = guillotine.get_guillotine_log_path()
        today_str = datetime.now().astimezone().strftime("%Y-%m-%d")
        arc_today = guillotine.get_log_archive_dir() / today_str

        chunk_line = f"[{today_str} 12:00:00] " + ("A" * 1000) + "\n"
        batch1 = chunk_line * 540  # > 512KB
        log_path.write_text(batch1, encoding="utf-8")

        guillotine._log_guard("marker after part1")
        part1 = arc_today / "guillotine_part1.log"
        self.assertTrue(part1.is_file())
        self.assertEqual(part1.read_text(encoding="utf-8"), batch1)
        self.assertFalse(log_path.with_suffix(".log.1").exists())
        self.assertIn("marker after part1", log_path.read_text(encoding="utf-8"))

        chunk_line_b = f"[{today_str} 13:00:00] " + ("B" * 1000) + "\n"
        batch2 = chunk_line_b * 540  # > 512KB
        log_path.write_text(batch2, encoding="utf-8")

        guillotine._log_guard("marker after part2")
        part2 = arc_today / "guillotine_part2.log"
        self.assertTrue(part1.is_file())
        self.assertTrue(part2.is_file())
        self.assertEqual(part1.read_text(encoding="utf-8"), batch1)
        self.assertEqual(part2.read_text(encoding="utf-8"), batch2)
        self.assertIn("marker after part2", log_path.read_text(encoding="utf-8"))

    def test_host_logs_readonly_past_snapshot_and_state_dedup(self) -> None:
        main_log = self.host_logs_dir / "main.log"
        ls_log = self.host_logs_dir / "language_server.log"

        main_content = (
            "2026-11-23 19:00:00.100 [info] main old day\n"
            "2026-11-24 09:00:00.200 [info] main yesterday\n"
            "  main stack trace line\n"
            "2026-11-25 08:00:00.300 [info] main today active\n"
        )
        ls_content = (
            "I1124 09:10:11.123456 1001 server.go:42] ls glog yesterday\n"
            "2026-11-24 18:20:00 [info] ls iso yesterday\n"
            "I1125 08:30:00.000000 1001 server.go:99] ls glog today\n"
        )
        main_log.write_text(main_content, encoding="utf-8")
        ls_log.write_text(ls_content, encoding="utf-8")

        ref_dt = datetime(2026, 11, 25, 11, 0, 0).astimezone()
        with (
            mock.patch.object(guillotine, "get_main_log_path", return_value=main_log),
            mock.patch.object(guillotine, "get_ls_log_path", return_value=ls_log),
        ):
            res1 = guillotine.rollover_daily_logs(
                now_dt=ref_dt,
                include_host_logs=True,
                clean_hubs=False,
            )
            res2 = guillotine.rollover_daily_logs(
                now_dt=ref_dt,
                include_host_logs=True,
                clean_hubs=False,
            )

        # Host files must never be modified or locked in-place.
        self.assertEqual(main_log.read_text(encoding="utf-8"), main_content)
        self.assertEqual(ls_log.read_text(encoding="utf-8"), ls_content)
        self.assertEqual(sorted(p.name for p in self.host_logs_dir.iterdir()), ["language_server.log", "main.log"])

        arc_root = guillotine.get_log_archive_dir()
        self.assertEqual(res1["host_logs"]["archived"]["main.log"], ["2026-11-23", "2026-11-24"])
        self.assertEqual(res1["host_logs"]["archived"]["language_server.log"], ["2026-11-24"])
        self.assertEqual(
            (arc_root / "2026-11-23" / "main.log").read_text(encoding="utf-8"),
            "2026-11-23 19:00:00.100 [info] main old day\n",
        )
        self.assertEqual(
            (arc_root / "2026-11-24" / "main.log").read_text(encoding="utf-8"),
            "2026-11-24 09:00:00.200 [info] main yesterday\n  main stack trace line\n",
        )
        self.assertEqual(
            (arc_root / "2026-11-24" / "language_server.log").read_text(encoding="utf-8"),
            "I1124 09:10:11.123456 1001 server.go:42] ls glog yesterday\n"
            "2026-11-24 18:20:00 [info] ls iso yesterday\n",
        )
        self.assertFalse((arc_root / "2026-11-25" / "main.log").exists())
        self.assertFalse((arc_root / "2026-11-25" / "language_server.log").exists())

        state_file = arc_root / "daily_archive_state.json"
        self.assertTrue(state_file.is_file())
        state = json.loads(state_file.read_text(encoding="utf-8"))
        self.assertEqual(state["host_logs"]["main.log"]["archived_dates"], ["2026-11-23", "2026-11-24"])
        self.assertEqual(state["host_logs"]["language_server.log"]["archived_dates"], ["2026-11-24"])

        # Second rollover skips already-archived dates without copying again.
        self.assertEqual(res2["host_logs"]["archived_files"], [])
        self.assertIn("main.log:2026-11-24", res2["host_logs"]["skipped_dates"])
        self.assertIn("language_server.log:2026-11-24", res2["host_logs"]["skipped_dates"])

    def test_stale_hub_cleanup_preserves_live_pids(self) -> None:
        (self.daemon_dir / "hub_live.json").write_text(json.dumps({"pid": 4242}), encoding="utf-8")
        (self.daemon_dir / "hub_dead1.json").write_text(json.dumps({"pid": 9991}), encoding="utf-8")
        (self.daemon_dir / "hub_dead2.json").write_text("not-json", encoding="utf-8")

        with mock.patch("acerola_orion.rpc.get_agy_daemon_dir", return_value=self.daemon_dir):
            res = guillotine.rollover_daily_logs(
                include_host_logs=False,
                clean_hubs=True,
                live_ls_pids={4242},
            )

        self.assertTrue(res["ok"])
        self.assertEqual(res["hubs_cleaned"], 2)
        remaining = sorted(p.name for p in self.daemon_dir.glob("hub_*.json"))
        self.assertEqual(remaining, ["hub_live.json"])

    def test_cli_guard_rollover_json_and_panel_daily_once(self) -> None:
        log_path = guillotine.get_guillotine_log_path()
        log_path.write_text("[2020-01-02 03:04:05] ancient line\n", encoding="utf-8")
        (self.daemon_dir / "hub_stale.json").write_text(json.dumps({"pid": 88888}), encoding="utf-8")
        fake_main = self.host_logs_dir / "missing_main.log"
        fake_ls = self.host_logs_dir / "missing_ls.log"

        buf = io.StringIO()
        with (
            mock.patch.object(guillotine, "get_main_log_path", return_value=fake_main),
            mock.patch.object(guillotine, "get_ls_log_path", return_value=fake_ls),
            mock.patch.object(guillotine, "snapshot_processes_fast", return_value=[object()]),
            mock.patch.object(guillotine, "find_antigravity_process_tree", return_value={"ls_pids": []}),
            mock.patch("acerola_orion.rpc.get_agy_daemon_dir", return_value=self.daemon_dir),
            contextlib.redirect_stdout(buf),
        ):
            rc = cli_main.main(["guard", "rollover", "--json"])

        self.assertEqual(rc, 0)
        payload = json.loads(buf.getvalue())
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["guillotine"]["archived_dates"], ["2020-01-02"])
        self.assertEqual(payload["hubs_cleaned"], 1)

        # Panel helper triggers rollover once per calendar day in-process.
        panel._PANEL_LAST_ROLLOVER_DATE = ""
        with mock.patch.object(panel, "rollover_daily_logs", return_value={"ok": True}) as m_roll:
            panel._maybe_rollover_daily_logs_inproc()
            panel._maybe_rollover_daily_logs_inproc()
        m_roll.assert_called_once_with(include_host_logs=True, clean_hubs=False)


if __name__ == "__main__":
    unittest.main()
