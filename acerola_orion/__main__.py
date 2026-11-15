# -*- coding: utf-8 -*-
"""
Acerola-Orion 统一命令行入口与启动器部署器 (acerola_orion/__main__.py)
支持子命令:
  status             实时 7 位精度配额水位、订阅层级、旁路审查延迟与内核守卫状态 (默认)
  ledger             本地 Token 明细账本 (每日/模型家族/活跃会话) 与电子秤验钞机报告
  audit              单独输出高精度配额斜率与无对话幽灵掉点验钞报告
  guard <action>     管理零轮询内核断头台守卫 (status | start | stop | install | uninstall | daemon | retire-legacy)
  panel              启动黑曜石绯红桌面可视化主控台
  install-launchers  在 ~/workspaces/launchers 生成快捷启动脚本
"""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys
from typing import Dict, List, Optional

from .cli_render import render_ledger_report, render_telemetry_report
from .constants import APP_CODENAME, APP_VERSION, atomic_write_text, get_launchers_dir
from .guillotine import (
    install_guard_autostart,
    retire_legacy_orphan_killer,
    run_guillotine_daemon_loop,
    start_guard_background,
    stop_guard_daemon,
    uninstall_guard_autostart,
)
from .ledger import MAX_LEDGER_DAYS, OrionLedger
from .telemetry import collect_telemetry, probe_guard_status


def _setup_utf8_stdout() -> None:
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


def deploy_agyrun_launchers(launchers_dir: Optional[Path] = None) -> List[str]:
    """Generate clean desktop/CLI launcher scripts in ~/workspaces/launchers/."""
    l_dir = launchers_dir or get_launchers_dir()
    l_dir.mkdir(parents=True, exist_ok=True)

    py_exe = Path(sys.executable)
    pyw_exe = py_exe.with_name("pythonw.exe")
    if not pyw_exe.is_file():
        pyw_exe = py_exe
    pkg_root = Path(__file__).resolve().parent.parent

    def _ps_single_quote(raw: Path | str) -> str:
        clean = str(raw).replace("\r", "").replace("\n", "")
        return "'" + clean.replace("'", "''") + "'"

    def _ps1_cli(args_str: str) -> str:
        return (
            "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8\r\n"
            "$OutputEncoding = [System.Text.Encoding]::UTF8\r\n"
            f"Set-Location -LiteralPath {_ps_single_quote(pkg_root)}\r\n"
            f"& {_ps_single_quote(py_exe)} -m acerola_orion {args_str}\r\n"
        )

    def _bat_path(raw: Path | str) -> str:
        return str(raw).replace("\r", "").replace("\n", "").replace("%", "%%")

    cmd_lines = [
        f'cd /d "{_bat_path(pkg_root)}"\r\n',
        f'start "" "{_bat_path(pyw_exe)}" -m acerola_orion panel\r\n',
        "exit\r\n",
    ]
    cmd_body = "@echo off\r\n" + "".join(cmd_lines)
    cmd_encoding = "ascii"
    if not cmd_body.isascii():
        oem_cp = ""
        if sys.platform == "win32":
            try:
                import ctypes

                oem_cp = f"cp{int(ctypes.windll.kernel32.GetOEMCP())}"
                "".encode(oem_cp)
            except Exception:
                oem_cp = ""
        try:
            if not oem_cp:
                raise LookupError("no OEM code page")
            cmd_body.encode(oem_cp)
            cmd_encoding = oem_cp
        except (LookupError, UnicodeEncodeError):
            cmd_body = "@echo off\r\nchcp 65001 >nul\r\n" + "".join(cmd_lines)
            cmd_encoding = "utf-8"

    files: Dict[str, str] = {
        "orion-panel.cmd": cmd_body,
        "orion-status.ps1": _ps1_cli("status"),
        "orion-ledger.ps1": _ps1_cli("ledger --days 14"),
        "orion-audit.ps1": _ps1_cli("audit"),
        "acerola-guard-on.ps1": _ps1_cli("guard install"),
        "acerola-guard-off.ps1": _ps1_cli("guard uninstall"),
    }

    written: List[str] = []
    for name, body in files.items():
        p = l_dir / name
        if name.endswith(".ps1"):
            enc = "utf-8-sig"
        elif name.endswith(".cmd"):
            enc = cmd_encoding
        else:
            enc = "utf-8"
        atomic_write_text(p, body, encoding=enc)
        written.append(str(p))
    return written


def _ledger_days_arg(raw: str) -> int:
    try:
        v = int(raw)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError(f"--days 需要整数，收到 {raw!r}")
    if not (1 <= v <= MAX_LEDGER_DAYS):
        raise argparse.ArgumentTypeError(f"--days 取值范围为 1..{MAX_LEDGER_DAYS}，收到 {v}")
    return v


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="acerola-orion",
        description=f"{APP_CODENAME} v{APP_VERSION} — Google Antigravity 本地配额水位透视、Token 验钞账本与零轮询内核进程守卫",
    )
    sub = parser.add_subparsers(dest="subcommand")

    # status
    p_st = sub.add_parser("status", help="实时探测 7 位精度配额水位、真实订阅层级、旁路审查延迟与守卫状态")
    p_st.add_argument("--json", action="store_true", help="输出结构化 JSON")
    p_st.add_argument("--no-record", action="store_true", help="不写入本地配额水位快照")

    # ledger
    p_lg = sub.add_parser("ledger", help="输出 GPT 同款每日 Token 账本、模型倍率审计与黑盒验钞报告")
    p_lg.add_argument("--days", type=_ledger_days_arg, default=14, help="汇总最近 N 天 (默认 14，范围 1..3650)")
    p_lg.add_argument("--json", action="store_true", help="输出结构化 JSON")

    # audit
    p_au = sub.add_parser("audit", help="单独执行配额水位采样并输出扣费斜率与幽灵掉点验钞报告")
    p_au.add_argument("--json", action="store_true", help="输出结构化 JSON")

    # guard
    p_gd = sub.add_parser("guard", help="管理零轮询 Win32 内核句柄断头台进程守卫")
    p_gd.add_argument(
        "action",
        choices=["status", "start", "stop", "install", "uninstall", "daemon", "retire-legacy"],
        nargs="?",
        default="status",
    )
    p_gd.add_argument("--json", action="store_true", help="输出结构化 JSON")

    # panel
    p_pn = sub.add_parser("panel", help="启动黑曜石绯红桌面可视化主控台")
    p_pn.add_argument("--minimized", action="store_true", help="启动后直接静默驻留右下角系统托盘")

    # shortcut
    p_sc = sub.add_parser("shortcut", help="注册或移除 Windows 开始菜单与任务栏可固定快捷方式")
    p_sc.add_argument("--remove", action="store_true", help="移除已注册的开始菜单快捷方式")
    p_sc.add_argument("--json", action="store_true", help="输出结构化 JSON")

    # install-launchers
    p_il = sub.add_parser("install-launchers", help="在 ~/workspaces/launchers 部署快捷启动器脚本")
    p_il.add_argument("--json", action="store_true", help="输出结构化 JSON")

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    _setup_utf8_stdout()
    parser = build_parser()
    args = parser.parse_args(argv)
    cmd = args.subcommand or "status"

    if cmd == "status":
        rep = collect_telemetry(include_guard_check=True)
        if not getattr(args, "no_record", False):
            try:
                OrionLedger().record_quota_from_telemetry(rep, event_type="cli_status", min_interval_sec=60.0)
            except Exception:
                pass
        if getattr(args, "json", False):
            print(json.dumps(rep.to_dict(), ensure_ascii=False, indent=2))
        else:
            print(render_telemetry_report(rep))
        return 0

    if cmd == "ledger":
        days = int(getattr(args, "days", 14))
        ledger = OrionLedger()
        try:
            rep = collect_telemetry(include_guard_check=False)
            ledger.record_quota_from_telemetry(rep, event_type="cli_ledger", min_interval_sec=60.0)
        except Exception:
            pass
        daily = ledger.get_daily_summary(days=days)
        models = ledger.get_model_breakdown(days=days)
        sessions = ledger.get_recent_sessions(limit=15)
        audit = ledger.compute_rate_audit()
        if getattr(args, "json", False):
            payload = {
                "days": days,
                "daily": [asdict(x) for x in daily],
                "models": [asdict(x) for x in models],
                "sessions": [asdict(x) for x in sessions],
                "audit": audit.to_dict(),
            }
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print(render_ledger_report(daily, models, sessions, audit, days=days))
        return 0

    if cmd == "audit":
        ledger = OrionLedger()
        try:
            rep = collect_telemetry(include_guard_check=False)
            ledger.record_quota_from_telemetry(rep, event_type="cli_audit", min_interval_sec=0.0)
        except Exception:
            pass
        audit = ledger.compute_rate_audit()
        if getattr(args, "json", False):
            print(json.dumps(audit.to_dict(), ensure_ascii=False, indent=2))
        else:
            print(f"✦ Orion 验钞快照总数: {audit.total_snapshots} (最新: {audit.latest_snapshot_ts})")
            print(f"✦ 结论: {audit.verdict}")
        return 0

    if cmd == "guard":
        act = args.action
        if act == "daemon":
            return run_guillotine_daemon_loop()
        if act == "status":
            st = probe_guard_status()
            out = asdict(st)
        elif act == "start":
            out = start_guard_background()
        elif act == "stop":
            out = stop_guard_daemon()
        elif act == "install":
            out = install_guard_autostart()
        elif act == "uninstall":
            out = uninstall_guard_autostart()
        elif act == "retire-legacy":
            out = retire_legacy_orphan_killer()
        else:
            out = {"ok": False, "message": f"Unknown action: {act}"}

        if args.json:
            print(json.dumps(out, ensure_ascii=False, indent=2))
        else:
            if "message" in out:
                print(f"✦ {out['message']}")
            else:
                print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    if cmd == "panel":
        from .panel import launch_panel

        return launch_panel(start_minimized=bool(getattr(args, "minimized", False)))

    if cmd == "shortcut":
        from .panel import APP_USER_MODEL_ID, _get_icon_path
        from .win_tray import install_start_menu_shortcut, remove_start_menu_shortcut

        pkg_root = Path(__file__).resolve().parent.parent
        if getattr(args, "remove", False):
            out_sc = remove_start_menu_shortcut(APP_CODENAME)
        else:
            out_sc = install_start_menu_shortcut(
                shortcut_name=APP_CODENAME,
                arguments="-m acerola_orion panel",
                working_dir=pkg_root,
                icon_path=_get_icon_path(),
                app_id=APP_USER_MODEL_ID,
                description=f"{APP_CODENAME} · 本地配额水位透视与内核退出守卫",
            )
        if getattr(args, "json", False):
            print(json.dumps(out_sc, ensure_ascii=False, indent=2))
        else:
            print(f"✦ {out_sc.get('message')}")
        return 0 if out_sc.get("ok") else 1

    if cmd == "install-launchers":
        written = deploy_agyrun_launchers()
        if args.json:
            print(json.dumps({"ok": True, "files": written}, ensure_ascii=False, indent=2))
        else:
            print(f"✅ 已在启动器目录部署 {len(written)} 个快捷脚本:")
            for w in written:
                print(f"  - {w}")
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
