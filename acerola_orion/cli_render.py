# -*- coding: utf-8 -*-
"""
Acerola-Orion 终端高可读仪表盘渲染器 (acerola_orion/cli_render.py)
"""

from __future__ import annotations

import math
from typing import List, Optional

from .constants import APP_CODENAME, APP_TRINITY, APP_VERSION
from .ledger import DailyUsageRow, ModelUsageRow, RateAuditReport, SessionUsageRow
from .telemetry import OrionTelemetryReport
from .vault import VaultSearchHit, VaultSessionItem, VaultStatusReport, _fmt_cn_time


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


def render_telemetry_report(
    r: OrionTelemetryReport,
    vault_status: Optional[VaultStatusReport] = None,
) -> str:
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
            status_desc = getattr(b, "status_text", "") or f"重置倒计时: {b.reset_countdown}"
            lines.append(
                f"    {b.bucket_id:<15} [{_bar(b.remaining_fraction, 20)}] "
                f"{pct_str}  ({status_desc})"
            )

    ls = r.log_stats
    sc = ls.sidecar
    lines.append("\n【3】 🛡️ 旁路风控延迟与拥堵体征 (Sidecar Risk Monitor & Congestion)")
    lines.append(
        f"  ● 旁路安全审查   : 累计触发 {sc.eval_count} 次 "
        f"(一审失败={getattr(sc, 'stage1_fail_count', 0)} / 二审失败={getattr(sc, 'stage2_fail_count', 0)}) | "
        f"平均附加耗时 {sc.avg_ms:.0f} ms | 峰值 {sc.max_ms:.0f} ms | 10s卡死超时 {sc.timeout_count} 次"
    )
    lines.append(
        f"  ● 限流拥堵计数   : 容量耗尽/排队={ls.capacity_exhausted_count} 次 | "
        f"主对话429/503={ls.http_429_503_count} 次 | 旁路杂项429={getattr(ls, 'aux_429_count', 0)} 次 | "
        f"埋点超时={getattr(ls, 'telemetry_timeout_count', 0)} 次 | 网络切换断流={ls.network_changed_count} 次"
    )

    g = r.guard
    lines.append("\n【4】 ⚔️ Acerola · 内核句柄断头台守卫与保险箱状态 (Guillotine Guard & Vault)")
    g_state = "🟢 运行中 (内核句柄监听与周期复查)" if g.mutex_active else "⚪ 未运行"
    auto_state = "已开启开机自启" if g.scheduled_task_installed else "未注册自启"
    lines.append(f"  ● 孤儿进程守卫   : {g_state} [{auto_state}]")
    if vault_status is not None:
        sw_tag = "🟢 已开启 (实时防删留档)" if vault_status.auto_backup_enabled else "⚪ 已关闭 (手动模式)"
        lines.append(
            f"  ● 对话防删保险箱 : {sw_tag} | 已保护 {vault_status.total_conversations} 个会话 "
            f"(🗑️已删可恢复={vault_status.deleted_conversations} 个, 体积={vault_status.total_vault_mb:.2f} MB)"
        )
    if g.legacy_buggy_killer_active:
        lines.append(
            f"  ● ⚠️ 遗留脚本告警: 检测到旧版轮询脚本 ag_kill_orphan 仍在运行 (PID={g.legacy_pids})，建议手动停用并删除旧计划任务。"
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
    lines.append(f"  ✦ Orion-Ledger · 本地 Token 用量与配额快照分析 (近 {days} 天)")
    lines.append("═" * 82)

    lines.append("\n【A】 📅 每日 Token 用量与预设价格估算")
    lines.append(
        f"  {'日期':<12} {'会话':>4} {'步数':>5} {'新鲜Input':>12} {'缓存读取Cache':>14} {'输出Output':>11} {'1.0x估算':>9} {'0.5x估算':>10} {'1.5x预设倍率估':>10}"
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
        f"  {'模型家族':<24} {'池':<6} {'步数':>5} {'Input+Cache':>14} {'Output':>10} {'1.0x估算':>9} {'AGY估算权重':>10}  {'倍率状态'}"
    )
    lines.append("  " + "─" * 78)
    for m in models:
        lines.append(
            f"  {m.family_label:<24} {m.quota_group:<6} {m.steps:>5} "
            f"{(m.input_tokens + m.cache_read_tokens):>14,} {m.output_tokens:>10,} "
            f"${m.usd_1x:>8.2f} ${m.usd_agy_est:>9.2f}  {m.multiplier_note}"
        )

    lines.append("\n【C】 💬 最近活跃会话用量 Top 8 (Recent Sessions)")
    for s in sessions[:8]:
        short_title = s.title if len(s.title) <= 30 else s.title[:29] + "…"
        lines.append(
            f"  • [{s.local_date}] {short_title:<30} | {s.primary_model:<22} | "
            f"步={s.steps:<3} Out={s.output_tokens:>7,} | 1x=${s.usd_1x:>5.2f} (预设倍率估=${s.usd_agy_est:>5.2f})"
        )

    lines.append("\n【D】 🔍 配额快照与本机步骤比对")
    lines.append(f"  ● 历史水位快照数 : {audit.total_snapshots} 笔 (最新: {audit.latest_snapshot_ts or '刚刚'})")
    lines.append(f"  ● 快照分析       : {audit.verdict}")
    if audit.phantom_drains:
        lines.append(f"  ● ⚠️ 本机无匹配步骤的配额下降 ({len(audit.phantom_drains)} 次):")
        for p in audit.phantom_drains[-4:]:
            p3_seg = f" | Claude/3P周池掉 -{p.p3_weekly_drop_pct:.3f}%" if p.p3_weekly_drop_pct > 0 else ""
            if p.p3_5h_drop_pct > 0:
                p3_seg += f" | Claude/3P 5h池掉 -{p.p3_5h_drop_pct:.3f}%"
            lines.append(
                f"    - {p.from_ts} -> {p.to_ts} ({p.from_event}->{p.to_event}): "
                f"5h池掉 -{p.gemini_5h_drop_pct:.3f}% | 周池掉 -{p.gemini_weekly_drop_pct:.3f}%{p3_seg}"
            )
    else:
        lines.append("  ● 本机未匹配步骤检测 : ✓ 暂未发现本机无匹配步骤的配额变化")

    if audit.slope_samples:
        lines.append("  ● 配额差值与本机 Token 记录的斜率样本:")
        for sp in audit.slope_samples[-4:]:
            p5_txt = f"${sp.implied_5h_pool_usd_at_1x:.2f}" if sp.implied_5h_pool_usd_at_1x else "-"
            pw_txt = f"${sp.implied_weekly_pool_usd_at_1x:.2f}" if sp.implied_weekly_pool_usd_at_1x else "-"
            lines.append(
                f"    - [{sp.duration_min:>4.1f}m | {sp.dominant_model}] 1x消耗=${sp.usd_1x:.3f} -> "
                f"5h掉 {sp.gemini_5h_drop_pct:.3f}% (推算5h池={p5_txt}) | 周掉 {sp.gemini_weekly_drop_pct:.3f}% (推算周池={pw_txt})"
            )

    lines.append("═" * 82)
    return "\n".join(lines)


def render_vault_status_report(st: VaultStatusReport, sessions: Optional[List[VaultSessionItem]] = None) -> str:
    lines: List[str] = []
    lines.append("═" * 78)
    lines.append("  ✦ Orion-Vault · 对话自动增量备份与防误删时光保险箱")
    lines.append("═" * 78)
    sw = "🟢 已开启 (每轮对话结束实时增量归档 + 后台巡检)" if st.auto_backup_enabled else "⚪ 已关闭 (仅保留已有归档，暂停自动备份)"
    lines.append(f"  ● 自动备份总开关 : {sw}")
    lines.append(f"  ● 保险箱本地路径 : {st.vault_dir}")
    lines.append(
        f"  ● 已保护会话总数 : {st.total_conversations} 个 "
        f"(🟢 主库在线={st.active_conversations} 个 | 🗑️ 主库已删但可恢复={st.deleted_conversations} 个)"
    )
    lines.append(f"  ● 归档占用总体积 : {st.total_vault_mb:.2f} MB  |  最近同步: {_fmt_cn_time(st.last_sync_at)}")
    lines.append("  ● 快捷开关命令   : `python -m acerola_orion vault on` / `off` / `toggle`")

    if sessions is not None:
        lines.append("\n【最近归档对话列表 (已删优先置顶)】")
        if not sessions:
            lines.append("  (当前筛选条件下暂无归档记录)")
        for s in sessions:
            badge = "🗑️已删可恢复" if s.live_status == "deleted" else "🟢主库在线  "
            short_t = (s.title[:34] + "…") if len(s.title) > 35 else s.title
            lines.append(
                f"  • [{badge}] {s.conv_id[:8]} | {_fmt_cn_time(s.last_modified_at)} | "
                f"步={s.step_count:<4} | {short_t}"
            )
    lines.append("═" * 78)
    return "\n".join(lines)


def render_vault_search_report(hits: List[VaultSearchHit], keywords: List[str]) -> str:
    lines: List[str] = []
    lines.append("═" * 78)
    lines.append(f"  ✦ Orion-Vault · 保险箱全量检索 (含已删除对话) | 靶词: {' '.join(keywords)} | 命中: {len(hits)} 个会话")
    lines.append("═" * 78)
    if not hits:
        lines.append("  (未命中匹配会话)")
    for h in hits:
        badge = "🗑️已删可恢复" if h.live_status == "deleted" else "🟢在线"
        lines.append(f"\n  ▸ [{badge}] {h.conv_id}  ({_fmt_cn_time(h.last_modified_at)}, 步={h.step_count})")
        lines.append(f"    标题: {h.title}")
        for lno, role, snip in h.snippets:
            lines.append(f"    [L{lno:<4} {role:<5}] {snip}")
    lines.append("═" * 78)
    return "\n".join(lines)
