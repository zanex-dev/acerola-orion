# -*- coding: utf-8 -*-
"""
Acerola-Orion 终端高可读仪表盘渲染器 (acerola_orion/cli_render.py)
"""

from __future__ import annotations

import math
from typing import List

from .constants import APP_CODENAME, APP_TRINITY, APP_VERSION
from .ledger import DailyUsageRow, ModelUsageRow, RateAuditReport, SessionUsageRow
from .telemetry import OrionTelemetryReport


def _bar(fraction: float, width: int = 22) -> str:
    try:
        val = float(fraction)
    except (TypeError, ValueError):
        val = 0.0
    if not math.isfinite(val):
        val = 0.0
    f = max(0.0, min(1.0, val))
    filled = int(round(f * width))
    return "█" * filled + "░" * (width - filled)


def render_telemetry_report(r: OrionTelemetryReport) -> str:
    lines: List[str] = []
    lines.append("═" * 74)
    lines.append(f"  ✦ {APP_CODENAME} v{APP_VERSION}  |  {APP_TRINITY}")
    lines.append(f"  采样时间 : {r.captured_at}")
    lines.append("═" * 74)

    lines.append("\n【1】 🔭 Orion · 本地语言服务拓扑与订阅层级 (Local IPC & Tier)")
    if not r.ls_running:
        lines.append("  ● 运行状态       : [离线] 未检测到运行中的 language_server.exe")
    else:
        lines.append(
            f"  ● 进程链路       : Antigravity 主进程 PID={r.main_pids} -> language_server.exe (PID={r.ls_pid}, v{r.ide_version})"
        )
        lines.append(
            f"  ● 本地回环端口   : 127.0.0.1:{r.http_port} (HTTPS:{r.https_port} | 历史残留 hub 文件: {r.stale_hub_files} 个)"
        )
        lines.append(
            f"  ● 生效订阅层级   : {r.paid_tier_id or r.user_tier_id or '未知'} "
            f"[{r.paid_tier_name or r.user_tier_name}] | Plan={r.teams_tier}"
        )
        lines.append(f"  ● 代理配置模式   : {r.proxy_mode}")

    if r.quota_buckets:
        lines.append("\n【2】 📊 实时高精度配额桶水位 (Live Quota Buckets · 7位浮点精度)")
        cur_group = ""
        for b in r.quota_buckets:
            if b.group_name != cur_group:
                cur_group = b.group_name
                lines.append(f"  ▸ {cur_group}")
            pct_str = f"{b.remaining_pct:7.3f}%" if math.isfinite(b.remaining_pct) else " --.---%"
            lines.append(
                f"    {b.bucket_id:<15} [{_bar(b.remaining_fraction, 20)}] "
                f"{pct_str}  (重置倒计时: {b.reset_countdown})"
            )

    ls = r.log_stats
    sc = ls.sidecar
    lines.append("\n【3】 🛡️ 旁路风控延迟与拥堵体征 (Sidecar Risk Monitor & Congestion)")
    lines.append(
        f"  ● 旁路安全审查   : 累计触发 {sc.eval_count} 次 | 平均附加耗时 {sc.avg_ms:.0f} ms | "
        f"峰值 {sc.max_ms:.0f} ms | 10s卡死超时 {sc.timeout_count} 次"
    )
    lines.append(
        f"  ● 限流拥堵计数   : 容量耗尽/排队={ls.capacity_exhausted_count} 次 | "
        f"HTTP 429/503={ls.http_429_503_count} 次 | 网络切换断流={ls.network_changed_count} 次"
    )

    g = r.guard
    lines.append("\n【4】 ⚔️ Acerola · 零轮询内核断头台守卫状态 (Guillotine Process Guard)")
    g_state = "🟢 运行中 (0% CPU 内核句柄监听 · 关窗毫秒级即斩)" if g.mutex_active else "⚪ 未运行"
    auto_state = "已开启开机自启" if g.scheduled_task_installed else "未注册自启"
    lines.append(f"  ● 孤儿进程守卫   : {g_state} [{auto_state}]")
    if g.legacy_buggy_killer_active:
        lines.append(
            f"  ● ⚠️ 遗留脚本告警: 检测到旧版轮询脚本 ag_kill_orphan 仍在运行 (PID={g.legacy_pids})，建议执行 `acerola-orion guard install` 清理替换！"
        )

    if r.rpc_errors:
        lines.append("\n  [RPC 告警] " + " | ".join(r.rpc_errors))

    lines.append("═" * 74)
    return "\n".join(lines)


def render_ledger_report(
    daily: List[DailyUsageRow],
    models: List[ModelUsageRow],
    sessions: List[SessionUsageRow],
    audit: RateAuditReport,
    days: int = 14,
) -> str:
    lines: List[str] = []
    lines.append("═" * 82)
    lines.append(f"  ✦ Orion-Ledger · 本地 Token 明细账本与黑盒电子秤验钞报告 (近 {days} 天)")
    lines.append("═" * 82)

    lines.append("\n【A】 📅 每日 Token 消耗与等效 API 账单明细 (GPT-Style Daily Token Table)")
    lines.append(
        f"  {'日期':<12} {'会话':>4} {'步数':>5} {'新鲜Input':>12} {'缓存读取Cache':>14} {'输出Output':>11} {'1.0x原价':>9} {'0.5x首发价':>10} {'1.5x暗扣估':>10}"
    )
    lines.append("  " + "─" * 78)
    tot_in = tot_ca = tot_out = 0
    tot_1x = tot_in05 = tot_15x = 0.0
    for d in daily:
        tot_in += d.input_tokens
        tot_ca += d.cache_read_tokens
        tot_out += d.output_tokens
        tot_1x += d.usd_1x
        tot_in05 += d.usd_intro
        tot_15x += d.usd_agy_est
        lines.append(
            f"  {d.local_date:<12} {d.sessions:>4} {d.steps:>5} "
            f"{d.input_tokens:>12,} {d.cache_read_tokens:>14,} {d.output_tokens:>11,} "
            f"${d.usd_1x:>8.2f} ${d.usd_intro:>9.2f} ${d.usd_agy_est:>9.2f}"
        )
    if daily:
        lines.append("  " + "─" * 78)
        lines.append(
            f"  {'合计 Total':<12} {'-':>4} {'-':>5} "
            f"{tot_in:>12,} {tot_ca:>14,} {tot_out:>11,} "
            f"${tot_1x:>8.2f} ${tot_in05:>9.2f} ${tot_15x:>9.2f}"
        )
    else:
        lines.append("  (暂无本地会话 Token 记录)")

    lines.append(f"\n【B】 🧠 近 {days} 天按模型家族拆分消耗与倍率审计 (Model Family Breakdown)")
    lines.append(
        f"  {'模型家族':<24} {'池':<6} {'步数':>5} {'Input+Cache':>14} {'Output':>10} {'1.0x原价':>9} {'AGY估算权重':>10}  {'倍率状态'}"
    )
    lines.append("  " + "─" * 78)
    for m in models:
        lines.append(
            f"  {m.family_label:<24} {m.quota_group:<6} {m.steps:>5} "
            f"{(m.input_tokens + m.cache_read_tokens):>14,} {m.output_tokens:>10,} "
            f"${m.usd_1x:>8.2f} ${m.usd_agy_est:>9.2f}  {m.multiplier_note}"
        )

    lines.append("\n【C】 💬 最近活跃会话账单 Top 8 (Recent Sessions)")
    for s in sessions[:8]:
        short_title = s.title if len(s.title) <= 30 else s.title[:29] + "…"
        lines.append(
            f"  • [{s.local_date}] {short_title:<30} | {s.primary_model:<22} | "
            f"步={s.steps:<3} Out={s.output_tokens:>7,} | 1x=${s.usd_1x:>5.2f} (暗扣估=${s.usd_agy_est:>5.2f})"
        )

    lines.append("\n【D】 🔍 黑盒进度条电子秤验钞机 (Quota Watermark & Phantom Drain Auditor)")
    lines.append(f"  ● 历史水位快照数 : {audit.total_snapshots} 笔 (最新: {audit.latest_snapshot_ts or '刚刚'})")
    lines.append(f"  ● 验钞结论       : {audit.verdict}")
    if audit.phantom_drains:
        lines.append(f"  ● ⚠️ 捕获无对话幽灵掉点事件 ({len(audit.phantom_drains)} 次):")
        for p in audit.phantom_drains[-4:]:
            p3_seg = f" | Claude/3P周池掉 -{p.p3_weekly_drop_pct:.3f}%" if p.p3_weekly_drop_pct > 0 else ""
            lines.append(
                f"    - {p.from_ts} -> {p.to_ts} ({p.from_event}->{p.to_event}): "
                f"5h池掉 -{p.gemini_5h_drop_pct:.3f}% | 周池掉 -{p.gemini_weekly_drop_pct:.3f}%{p3_seg}"
            )
    else:
        lines.append("  ● 幽灵暗扣检测   : ✓ 暂未发现零对话区间的异常掉点")

    if audit.slope_samples:
        lines.append("  ● 实测扣费斜率样本 (ΔQuota% vs 真实 Token 消耗):")
        for sp in audit.slope_samples[-4:]:
            p5_txt = f"${sp.implied_5h_pool_usd_at_1x:.2f}" if sp.implied_5h_pool_usd_at_1x else "-"
            pw_txt = f"${sp.implied_weekly_pool_usd_at_1x:.2f}" if sp.implied_weekly_pool_usd_at_1x else "-"
            lines.append(
                f"    - [{sp.duration_min:>4.1f}m | {sp.dominant_model}] 1x消耗=${sp.usd_1x:.3f} -> "
                f"5h掉 {sp.gemini_5h_drop_pct:.3f}% (推算5h池={p5_txt}) | 周掉 {sp.gemini_weekly_drop_pct:.3f}% (推算周池={pw_txt})"
            )

    lines.append("═" * 82)
    return "\n".join(lines)
