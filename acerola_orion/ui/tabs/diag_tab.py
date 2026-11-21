# -*- coding: utf-8 -*-
"""
Acerola-Orion 旁路风控示波器与诊断日志标签页模块 (acerola_orion/ui/tabs/diag_tab.py)
"""

from __future__ import annotations

import tkinter as tk
from typing import Any, List, Tuple

from ...telemetry import OrionTelemetryReport, strip_glog_init_noise
from ..charts import OrionChartWidget
from ..theme import ACCENT_ROSE, BG_CARD_ALT, FG_TEXT


def build_diag_tab(app: Any) -> None:
    """Build Sidecar Risk Oscilloscope & Log Diagnostics Tab."""
    app.lbl_diag_summary = tk.Label(
        app.tab_diag,
        text="旁路安全审查 (Sidecar Risk Monitor) 与限流拥堵统计加载中...",
        bg=BG_CARD_ALT,
        fg=FG_TEXT,
        font=("Consolas", 9),
        anchor="w",
        justify="left",
        padx=12,
        pady=8,
    )
    app.lbl_diag_summary.pack(fill=tk.X, pady=(2, 6))
    app.tab_diag.bind(
        "<Configure>",
        lambda e: app.lbl_diag_summary.configure(wraplength=max(360, int(e.width) - 36)),
    )

    app.diag_wave_chart = OrionChartWidget(
        app.tab_diag,
        title="⚡ 旁路风控模型 (sir_flash_lite) 最近请求附加耗时示波器 (ms)",
        height_px=app._px(145),
        dpi_scale=app._dpi_scale,
    )
    app.diag_wave_chart.pack(fill=tk.X, pady=(0, 8))

    app.txt_diag = app._create_dark_text_box(
        app.tab_diag, fg_color="#cbd5e1", font_pt=9, height=12, pad_xy=(0, 0)
    )


def refresh_diag_waveform(app: Any, r: OrionTelemetryReport, lat_pts: List[Tuple[str, float]]) -> None:
    """Refresh Diagnostics tab sidecar latency waveform chart."""
    sc = r.log_stats.sidecar
    app.diag_wave_chart.set_series(
        "⚡ 旁路安全审查 (sir_flash_lite) 最近请求附加耗时示波器 (ms)",
        lat_pts,
        mode="area",
        line_color=ACCENT_ROSE,
        fill_color="#2d0f1e",
        unit_suffix=" ms",
        fixed_y_range=None,
        empty_hint="本次会话日志中暂未捕获 sir_flash_lite 旁路审查耗时样本",
        summary_badge=f"均值 {sc.avg_ms:.0f} ms · 峰值 {sc.max_ms:.0f} ms" if sc.eval_count else "0 次触发",
    )


def refresh_diag_tab_data(
    app: Any,
    r: OrionTelemetryReport,
    guard_lines: List[str],
    ls_tail: List[str],
) -> None:
    """Populate Diagnostics tab summary banner and live guard/LS log box."""
    ls = r.log_stats
    sc = ls.sidecar
    app.lbl_diag_summary.configure(
        text=(
            f"旁路安全审查 (sir_flash_lite / severe_internal_risks_agy): 累计 {sc.eval_count} 次 "
            f"(一审失败={getattr(sc, 'stage1_fail_count', 0)} / 二审失败={getattr(sc, 'stage2_fail_count', 0)}) | "
            f"均值 {sc.avg_ms:.0f} ms | 峰值 {sc.max_ms:.0f} ms | 10s超时 {sc.timeout_count} 次\n"
            f"容量耗尽排队: {ls.capacity_exhausted_count} 次 | 主对话 429/503: {ls.http_429_503_count} 次 | "
            f"旁路杂项 429: {getattr(ls, 'aux_429_count', 0)} 次 | 埋点超时: {getattr(ls, 'telemetry_timeout_count', 0)} 次 | "
            f"网络断流: {ls.network_changed_count} 次 | 残留 hub: {r.stale_hub_files} 个"
        )
    )
    cleaned_ls_tail = [strip_glog_init_noise(ln) for ln in ls_tail]
    app.txt_diag.delete("1.0", tk.END)
    app.txt_diag.insert(tk.END, "=== [Acerola-Guillotine 守护日志 (guillotine.log)] ===\n")
    app.txt_diag.insert(tk.END, ("\n".join(guard_lines) if guard_lines else "(暂无守护日志记录)") + "\n\n")
    app.txt_diag.insert(tk.END, "=== [language_server.log 最新尾部流 (无锁只读 · 已剥离 glog 前缀)] ===\n")
    app.txt_diag.insert(tk.END, "\n".join(cleaned_ls_tail) + "\n")


__all__ = ["build_diag_tab", "refresh_diag_waveform", "refresh_diag_tab_data"]
