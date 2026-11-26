# -*- coding: utf-8 -*-
"""
Acerola-Orion 统一命令行入口与启动器部署器 (acerola_orion/__main__.py)
支持子命令:
  status             实时 7 位精度配额水位、订阅层级、旁路审查延迟、内核守卫与保险箱状态 (默认)
  radar              实时观测本地活跃会话上下文体积、200K 2x 预警与 Prompt KV 缓存 250s 余温倒计时
  ledger             本地 Token 明细账本 (每日/模型家族/活跃会话) 与配额快照分析
  audit              单独输出高精度配额斜率与本机未匹配步骤配额变化分析
  vault <action>     Orion-Vault 对话自动增量备份、可控开关与误删对话恢复/导出
  guard <action>     管理 Windows 内核句柄断头台进程守卫 (status | start | stop | install | uninstall | daemon | retire-legacy)
  companion <action> 一键非破坏性装配/检查/卸载 Antigravity 配套 Hooks 与白帽 Skills (status | install | uninstall)
  panel              启动猎户座星盘桌面可视化主控台
  install-launchers  在 ~/workspaces/launchers 生成快捷启动脚本
"""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys
from typing import Dict, List, Optional

from .cli_render import (
    render_ledger_report,
    render_telemetry_report,
    render_vault_search_report,
    render_vault_status_report,
)
from .companion import (
    companion_status,
    install_companion,
    render_companion_report,
    uninstall_companion,
)
from .constants import (
    APP_CODENAME,
    APP_VERSION,
    atomic_write_text,
    configure_vault_auto_backup,
    get_launchers_dir,
)
from .core.kernel import get_kernel
from .guillotine import (
    install_guard_autostart,
    retire_legacy_orphan_killer,
    rollover_daily_logs,
    run_guillotine_daemon_loop,
    start_guard_background,
    stop_guard_daemon,
    uninstall_guard_autostart,
)
from .ledger import MAX_LEDGER_DAYS, OrionLedger
from .telemetry import collect_telemetry, probe_guard_status
from .vault import OrionVault, run_stop_hook_from_stdin


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
        "orion-vault-status.ps1": _ps1_cli("vault status"),
        "orion-vault-sync.ps1": _ps1_cli("vault sync"),
        "orion-vault-on.ps1": _ps1_cli("vault on"),
        "orion-vault-off.ps1": _ps1_cli("vault off"),
        "orion-vault-toggle.ps1": _ps1_cli("vault toggle"),
    }

    extra_files = get_kernel().collect_extra_launchers(str(py_exe), str(pyw_exe), pkg_root)
    for ext_name, ext_body in extra_files.items():
        if not ext_name or ext_name in files:
            continue
        if Path(ext_name).name != ext_name or ext_name in (".", "..") or "/" in ext_name or "\\" in ext_name:
            continue
        files[ext_name] = ext_body

    written: List[str] = []
    for name, body in files.items():
        p = l_dir / name
        if name.endswith(".ps1"):
            enc = "utf-8-sig"
        elif name.endswith(".cmd") or name.endswith(".bat"):
            if body.isascii():
                enc = "ascii"
            else:
                try:
                    body.encode(cmd_encoding)
                    enc = cmd_encoding
                except (LookupError, UnicodeEncodeError):
                    enc = "utf-8"
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
        description=f"{APP_CODENAME} v{APP_VERSION} — Google Antigravity 本地配额水位透视、Token 账本、对话防删保险箱与内核进程守卫",
    )
    sub = parser.add_subparsers(dest="subcommand")

    # status
    p_st = sub.add_parser("status", help="实时探测 7 位精度配额水位、真实订阅层级、旁路审查延迟与守卫/保险箱状态")
    p_st.add_argument("--json", action="store_true", help="输出结构化 JSON")
    p_st.add_argument("--no-record", action="store_true", help="不写入本地配额水位快照")

    # radar
    p_rd = sub.add_parser("radar", help="纯本地被动观测活跃会话上下文体积、200K 2x 预警与 Prompt KV 缓存 250s 余温状态")
    p_rd.add_argument(
        "--mode",
        choices=["smart_guard", "latest", "pinned"],
        default="smart_guard",
        help="会话监控策略: smart_guard (智能守≥100K大窗) | latest (跟最新) | pinned (手动锁定)",
    )
    p_rd.add_argument("--pin", default="", help="当 --mode pinned 时指定目标会话 UUID")
    p_rd.add_argument("--json", action="store_true", help="输出结构化 JSON")

    # ledger
    p_lg = sub.add_parser("ledger", help="汇总本机每日 Token 用量、预设价格估算与配额快照分析")
    p_lg.add_argument("--days", type=_ledger_days_arg, default=14, help="汇总最近 N 天 (默认 14，范围 1..3650)")
    p_lg.add_argument("--json", action="store_true", help="输出结构化 JSON")

    # audit
    p_au = sub.add_parser("audit", help="单独执行配额水位采样并比对配额变化与本机步骤")
    p_au.add_argument("--json", action="store_true", help="输出结构化 JSON")

    # guard
    p_gd = sub.add_parser("guard", help="管理可选的 Windows 内核句柄断头台进程守卫")
    p_gd.add_argument(
        "action",
        choices=["status", "start", "stop", "install", "uninstall", "daemon", "retire-legacy", "rollover"],
        nargs="?",
        default="status",
    )
    p_gd.add_argument("--json", action="store_true", help="输出结构化 JSON")

    # panel
    p_pn = sub.add_parser("panel", help="启动猎户座星盘桌面主控台 (支持全盘星轨模式、极简专注模式与缓存悬浮胶囊)")
    p_pn.add_argument("--minimized", action="store_true", help="启动后直接静默驻留右下角系统托盘")
    p_pn.add_argument("--reload", action="store_true", help="关闭已有同通道实例并重新加载最新界面")
    p_pn.add_argument(
        "--hud",
        choices=["smart", "always", "off"],
        default=None,
        help="设置桌面 KV 缓存悬浮胶囊模式: smart (≥100K临期预警) | always (常驻) | off (关闭)",
    )
    mode_grp = p_pn.add_mutually_exclusive_group()
    mode_grp.add_argument("--zen", action="store_true", help="以极简专注模式启动 (同屏仅展示 1 个核心数据 + 1 张统计图)")
    mode_grp.add_argument("--full", action="store_true", help="以全盘星轨观测台模式启动")

    # shortcut
    p_sc = sub.add_parser("shortcut", help="注册或移除 Windows 开始菜单与任务栏可固定快捷方式")
    p_sc.add_argument("--remove", action="store_true", help="移除已注册的开始菜单快捷方式")
    p_sc.add_argument("--json", action="store_true", help="输出结构化 JSON")

    # install-launchers
    p_il = sub.add_parser("install-launchers", help="在 ~/workspaces/launchers 部署快捷启动器脚本")
    p_il.add_argument("--json", action="store_true", help="输出结构化 JSON")

    # vault (Conversation Auto-Backup & Recovery Time-Vault)
    p_vt = sub.add_parser("vault", help="Orion-Vault 对话自动增量备份、可控开关与误删对话恢复/导出")
    p_vt.add_argument(
        "action",
        choices=[
            "status",
            "sync",
            "on",
            "enable",
            "off",
            "disable",
            "toggle",
            "list",
            "search",
            "show",
            "restore",
            "export",
            "hook-stop",
        ],
        nargs="?",
        default="status",
        help="status | sync | on/off/toggle (可控开关) | list | search | show | restore | export | hook-stop",
    )
    p_vt.add_argument("targets", nargs="*", help="目标会话 UUID/前缀 或 检索关键词")
    p_vt.add_argument("--deleted", action="store_true", help="仅列出或仅检索已在客户端删除的对话")
    p_vt.add_argument("--limit", type=int, default=25, help="返回条数上限 (默认 25)")
    p_vt.add_argument("--around-line", type=int, default=None, help="show 模式下围绕指定行号展开")
    p_vt.add_argument("--tail", type=int, default=12, help="show 模式下查看末尾 N 行")
    p_vt.add_argument("--force", action="store_true", help="restore 模式下若主库仍存在同名对话，留底后强制回滚覆盖")
    p_vt.add_argument("--auto-only", action="store_true", help="sync 模式下遵循自动备份开关状态")
    p_vt.add_argument("--json", action="store_true", help="输出结构化 JSON")

    # companion (Antigravity Hooks & Whitehat Skills Non-Destructive Installer)
    p_cp = sub.add_parser("companion", help="一键非破坏性装配、检查或卸载 Antigravity 配套 Hooks 与白帽 Skills")
    p_cp.add_argument(
        "action",
        choices=["status", "install", "uninstall"],
        nargs="?",
        default="status",
        help="status (状态检查) | install (非破坏性合并装配) | uninstall (摘除受管项)",
    )
    p_cp.add_argument("--workspace", type=str, default=None, help="目标工程工作区路径（安装/查看/卸载工作区配套插件）")
    p_cp.add_argument("--no-hooks", action="store_true", help="跳过全局 hooks.json 与 EOQ 配置操作")
    p_cp.add_argument("--no-global-skills", action="store_true", help="install 时跳过全局插件与全局技能部署")
    p_cp.add_argument("--include-rules", action="store_true", help="安装工作区插件时额外拷贝 engineering-core/rules/AGENTS.md")
    p_cp.add_argument("--force", action="store_true", help="强制覆盖已存在的受管钩子键、配置或技能文件")
    p_cp.add_argument("--python-exe", type=str, default=None, help="写入 hooks.json 的目标 Python 解释器路径")
    p_cp.add_argument("--remove-global", action="store_true", help="uninstall 时一并移除已部署的全局白帽插件与技能")
    p_cp.add_argument("--remove-workspace", action="store_true", help="uninstall 时一并移除指定工作区中的受管配套技能")
    p_cp.add_argument("--json", action="store_true", help="输出结构化 JSON")

    get_kernel().register_cli(sub)
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
            v_st = None
            try:
                v_st = OrionVault().get_status(refresh_deleted_flags=False)
            except Exception:
                v_st = None
            print(render_telemetry_report(rep, vault_status=v_st))
        return 0

    if cmd == "radar":
        from .cache_radar import CacheRadarSampler

        sampler = CacheRadarSampler(
            target_mode=str(getattr(args, "mode", "smart_guard") or "smart_guard"),
            pinned_conv_id=str(getattr(args, "pin", "") or ""),
        )
        state = sampler.sample()
        candidates = sampler.list_recent_sessions(limit=6)
        if getattr(args, "json", False):
            print(
                json.dumps(
                    {
                        "state": state.to_dict(),
                        "recent_sessions": [c.to_dict() for c in candidates],
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
        else:
            print(f"✦ Orion-CacheRadar 本地缓存余温观测: {state.badge_text}")
            if state.tier_alert_text:
                print(f"  ● 计价提醒: {state.tier_alert_text}")
            print(f"  ● 明细: {state.detail_text}")
            if candidates:
                print("  ● 近期活跃会话候选:")
                for c in candidates:
                    mark = "👉" if c.is_current else "  "
                    print(f"    {mark} {c.menu_label}")
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
            print(f"✦ 配额快照总数: {audit.total_snapshots} (最新: {audit.latest_snapshot_ts})")
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
        elif act == "rollover":
            out = rollover_daily_logs(include_host_logs=True, clean_hubs=True)
        else:
            out = {"ok": False, "message": f"Unknown action: {act}"}

        if args.json:
            print(json.dumps(out, ensure_ascii=False, indent=2))
        else:
            if "message" in out:
                print(f"✦ {out['message']}")
            else:
                print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0 if out.get("ok", True) else 1

    if cmd == "panel":
        from .panel import launch_panel

        init_mode: Optional[str] = "zen" if getattr(args, "zen", False) else ("full" if getattr(args, "full", False) else None)
        return launch_panel(
            start_minimized=bool(getattr(args, "minimized", False)),
            initial_mode=init_mode,
            initial_hud_mode=getattr(args, "hud", None),
            reload_existing=bool(getattr(args, "reload", False)),
        )

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

    if cmd == "vault":
        act_v = str(getattr(args, "action", "status") or "status").lower()
        targets: List[str] = list(getattr(args, "targets", None) or [])
        as_json = bool(getattr(args, "json", False))

        if act_v == "hook-stop":
            sys.stdout.write(run_stop_hook_from_stdin() + "\n")
            sys.stdout.flush()
            return 0

        if act_v in ("on", "enable", "off", "disable", "toggle"):
            res_sw = configure_vault_auto_backup(act_v)
            if as_json:
                print(json.dumps(res_sw, ensure_ascii=False, indent=2))
            else:
                print(res_sw["message"])
            return 0 if res_sw.get("ok") else 1

        vault = OrionVault()

        if act_v == "status":
            st = vault.get_status(refresh_deleted_flags=True)
            sess_preview = vault.list_sessions(
                deleted_only=bool(getattr(args, "deleted", False)),
                limit=max(1, int(getattr(args, "limit", 12))),
            )
            if as_json:
                payload_v = st.to_dict()
                payload_v["sessions"] = [s.to_dict() for s in sess_preview]
                print(json.dumps(payload_v, ensure_ascii=False, indent=2))
            else:
                print(render_vault_status_report(st, sessions=sess_preview))
            return 0

        if act_v == "list":
            st = vault.get_status(refresh_deleted_flags=True)
            q_str = " ".join(targets).strip()
            sess_list = vault.list_sessions(
                deleted_only=bool(getattr(args, "deleted", False)),
                query=q_str,
                limit=max(1, int(getattr(args, "limit", 30))),
            )
            if as_json:
                print(json.dumps([s.to_dict() for s in sess_list], ensure_ascii=False, indent=2))
            else:
                print(render_vault_status_report(st, sessions=sess_list))
            return 0

        if act_v == "sync":
            respect_sw = bool(getattr(args, "auto_only", False))
            if targets:
                res_s = vault.sync_conversation(targets[0], respect_switch=respect_sw)
            else:
                res_s = vault.sync_all(respect_switch=respect_sw)
            if as_json:
                print(json.dumps(res_s.to_dict(), ensure_ascii=False, indent=2))
            else:
                prefix = "✅" if res_s.ok else "❌"
                print(f"{prefix} {res_s.message}")
            return 0 if res_s.ok else 1

        if act_v == "search":
            if not targets:
                print("❌ 请提供至少 1 个检索关键词，例如: python -m acerola_orion vault search 关键词")
                return 1
            hits = vault.search_vault(
                targets,
                deleted_only=bool(getattr(args, "deleted", False)),
                limit=max(1, int(getattr(args, "limit", 20))),
            )
            if as_json:
                print(json.dumps([h.to_dict() for h in hits], ensure_ascii=False, indent=2))
            else:
                print(render_vault_search_report(hits, targets))
            return 0

        if act_v == "show":
            if not targets:
                print("❌ 请提供会话 UUID 或前 8 位前缀，例如: python -m acerola_orion vault show ce81582f")
                return 1
            try:
                sl = vault.show_slice(
                    targets[0],
                    around_line=getattr(args, "around_line", None),
                    tail=int(getattr(args, "tail", 12)),
                )
            except (KeyError, ValueError) as exc:
                if as_json:
                    print(json.dumps({"ok": False, "message": str(exc)}, ensure_ascii=False, indent=2))
                else:
                    print(f"❌ {exc}")
                return 1
            if as_json:
                print(json.dumps(sl, ensure_ascii=False, indent=2))
            elif not sl.get("ok"):
                print(f"⚠️ {sl.get('message')}")
                return 1
            else:
                rng = sl["range"]
                print(
                    f"✦ [{sl['live_status']}] {sl['title']} ({sl['conv_id']}) | "
                    f"行 {rng[0]}..{rng[1]} / 共 {sl['total_lines']} 行"
                )
                print("─" * 76)
                for step in sl["steps"]:
                    print(f"[L{step['line']:<4} {step['type']:<16} {step['created_at']}] {step['content']}")
            return 0

        if act_v == "restore":
            if not targets:
                print("❌ 请提供要恢复的会话 UUID 或前 8 位前缀，例如: python -m acerola_orion vault restore ce81582f")
                return 1
            try:
                res_r = vault.restore_conversation(targets[0], force=bool(getattr(args, "force", False)))
            except (KeyError, ValueError) as exc:
                res_r = {"ok": False, "message": str(exc)}
            if as_json:
                print(json.dumps(res_r, ensure_ascii=False, indent=2))
            else:
                prefix = "✅" if res_r.get("ok") else "⚠️"
                print(f"{prefix} {res_r.get('message')}")
            return 0 if res_r.get("ok") else 1

        if act_v == "export":
            if not targets:
                print("❌ 请提供要导出的会话 UUID 或前 8 位前缀，例如: python -m acerola_orion vault export ce81582f")
                return 1
            try:
                res_e = vault.export_markdown(targets[0])
            except (KeyError, ValueError) as exc:
                res_e = {"ok": False, "message": str(exc)}
            if as_json:
                print(json.dumps(res_e, ensure_ascii=False, indent=2))
            else:
                prefix = "✅" if res_e.get("ok") else "❌"
                print(f"{prefix} {res_e.get('message')}")
            return 0 if res_e.get("ok") else 1

    if cmd == "companion":
        cp_action = str(getattr(args, "action", "status") or "status")
        ws_arg = getattr(args, "workspace", None)
        ws_path = Path(ws_arg) if ws_arg else None
        as_json = bool(getattr(args, "json", False))

        if cp_action == "status":
            cp_res = companion_status(workspace=ws_path)
        elif cp_action == "install":
            cp_res = install_companion(
                workspace=ws_path,
                install_hooks=not bool(getattr(args, "no_hooks", False)),
                install_global=not bool(getattr(args, "no_global_skills", False)),
                include_rules=bool(getattr(args, "include_rules", False)),
                force=bool(getattr(args, "force", False)),
                python_exe=getattr(args, "python_exe", None),
            )
        else:
            cp_res = uninstall_companion(
                workspace=ws_path,
                remove_hooks=not bool(getattr(args, "no_hooks", False)),
                remove_global=bool(getattr(args, "remove_global", False)),
                remove_workspace=bool(getattr(args, "remove_workspace", False) or ws_path is not None),
            )

        if as_json:
            print(json.dumps(cp_res, ensure_ascii=False, indent=2))
        else:
            print(render_companion_report(cp_action, cp_res))
        return 0 if cp_res.get("ok") else 1

    plugin_rc = get_kernel().handle_cli(cmd, args)
    if plugin_rc is not None:
        return int(plugin_rc)

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
