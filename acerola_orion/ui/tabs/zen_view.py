# -*- coding: utf-8 -*-
"""
Acerola-Orion 极简专注模式 (Zen Mode) 视图模块 (acerola_orion/ui/tabs/zen_view.py)
"""

from __future__ import annotations

from datetime import datetime
import math
import tkinter as tk
from typing import Any, List, Tuple

from ...cache_radar import (
    PHASE_ACTIVE,
    PHASE_COLD,
    PHASE_HOT,
    PHASE_WARNING,
    CacheRadarState,
    format_mmss,
)
from ...telemetry import OrionTelemetryReport
from ..astrolabe import AstrolabeDialWidget
from ..charts import OrionChartWidget
from ..theme import (
    ACCENT_AMBER,
    ACCENT_AMBER_DIM,
    ACCENT_CRIMSON_DIM,
    ACCENT_CYAN,
    ACCENT_CYAN_DIM,
    ACCENT_GOLD,
    ACCENT_GREEN,
    ACCENT_GREEN_DIM,
    ACCENT_ROSE,
    BG_CARD_ALT,
    BG_ROOT,
    FG_MUTED,
    _radar_palette,
)


def build_zen_mode_ui(app: Any) -> None:
    """Build Mode 1: Zen / Simplified Focus Mode (1 Hero Metric + 1 Chart ONLY)."""
    pill_bar = tk.Frame(app.zen_container, bg=BG_ROOT)
    pill_bar.pack(fill=tk.X, pady=(0, app._px(8)))

    specs = [
        ("gemini", "✦ Gemini 配额"),
        ("claude", "◆ Claude / 3P 配额"),
        ("spend", "◈ 今日等效账单"),
        ("latency", "⚡ 响应顺畅度"),
        ("cache", "📡 缓存余温"),
    ]
    app._zen_pill_btns = {}
    for col, (key, label) in enumerate(specs):
        pill_bar.columnconfigure(col, weight=1, uniform="zen_pill")
        b = tk.Button(
            pill_bar,
            text=label,
            command=lambda k=key: app.select_zen_metric(k),
            relief="flat",
            padx=10,
            pady=6,
            font=("Microsoft YaHei UI", 9, "bold"),
            cursor="hand2",
        )
        b.grid(row=0, column=col, sticky="ew", padx=3)
        app._zen_pill_btns[key] = b

    app._update_zen_pill_styles()

    app.zen_dial = AstrolabeDialWidget(
        app.zen_container,
        title="Gemini 实时可用配额",
        subtitle_tag="5H 滚动池剩余",
        primary_hue="cyan",
        dial_px=app._px(225),
        simplified_digits=True,
        dpi_scale=app._dpi_scale,
    )
    app.zen_dial.pack(fill=tk.BOTH, expand=True, pady=(0, app._px(8)))

    app.zen_chart = OrionChartWidget(
        app.zen_container,
        title="近 24 小时 Gemini 配额水位变化曲线",
        height_px=app._px(195),
        dpi_scale=app._dpi_scale,
    )
    app.zen_chart.pack(fill=tk.BOTH, expand=True)


def refresh_zen_mode_ui(
    app: Any,
    r: OrionTelemetryReport,
    g5: Any,
    gw: Any,
    p5: Any,
    pw: Any,
    gem_pts: List[Tuple[str, float]],
    cld_pts: List[Tuple[str, float]],
    spend_pts: List[Tuple[str, float]],
    lat_pts: List[Tuple[str, float]],
    missing_msg: str,
) -> None:
    """Refresh the active Zen focus metric dial and single trend chart."""
    sc = r.log_stats.sidecar
    zm = app._zen_metric
    if zm == "gemini":
        app.zen_dial.lbl_title.configure(text="✦ Gemini 可用配额水位")
        app.zen_dial.set_quota_pair(
            g5.remaining_fraction if g5 else math.nan,
            g5.reset_countdown if g5 else "--",
            g5.reset_time if g5 else "",
            gw.remaining_fraction if gw else math.nan,
            gw.reset_countdown if gw else "--",
            offline_reason=missing_msg,
        )
        app.zen_chart.set_series(
            "近 24 小时配额水位变化趋势",
            gem_pts,
            mode="area",
            line_color=ACCENT_CYAN,
            fill_color="#0f2438",
            unit_suffix="%",
            fixed_y_range=(0.0, 100.0),
            empty_hint="打开 Antigravity 后将自动记录并绘制水位曲线",
            summary_badge=f"当前 {g5.remaining_pct:.1f}%" if g5 and math.isfinite(g5.remaining_pct) else "",
        )
    elif zm == "claude":
        app.zen_dial.lbl_title.configure(text="◆ Claude / GPT 第三方池配额水位")
        app.zen_dial.set_quota_pair(
            p5.remaining_fraction if p5 else math.nan,
            p5.reset_countdown if p5 else "--",
            p5.reset_time if p5 else "",
            pw.remaining_fraction if pw else math.nan,
            pw.reset_countdown if pw else "--",
            offline_reason=missing_msg,
        )
        app.zen_chart.set_series(
            "近 24 小时 Claude / 3P 配额水位变化趋势",
            cld_pts,
            mode="area",
            line_color=ACCENT_ROSE,
            fill_color="#2e1020",
            unit_suffix="%",
            fixed_y_range=(0.0, 100.0),
            empty_hint="暂无 Claude / 3P 历史水位采样点",
            summary_badge=f"当前 {p5.remaining_pct:.1f}%" if p5 and math.isfinite(p5.remaining_pct) else "",
        )
    elif zm == "spend":
        today_str = datetime.now().strftime("%Y-%m-%d")
        today_row = next((d for d in app._last_daily if getattr(d, "local_date", "") == today_str), None)
        today_usd = float(getattr(today_row, "usd_agy_est", 0.0) or 0.0) if today_row else 0.0
        today_steps = int(getattr(today_row, "steps", 0) or 0) if today_row else 0
        peak_usd = max([v for _, v in spend_pts] + [10.0])
        ring_f = min(1.0, today_usd / peak_usd) if peak_usd > 0 else 0.0
        app.zen_dial.lbl_title.configure(text="◈ 今日累计等效消耗")
        app.zen_dial.set_custom_metric(
            ring_fraction=max(0.04, ring_f) if today_usd > 0 else 0.0,
            top_caption="今日等效消耗估值",
            big_value=f"${today_usd:.2f}",
            sub_caption=f"今日共完成 {today_steps} 轮有效推理",
            badge_text="📒 本地只读记账",
            badge_fg=ACCENT_GOLD,
            badge_bg="#3a2807",
            footer_text="基于本地会话 Token 自动折算（零联网、零隐私上传）",
            ring_colors=(ACCENT_GOLD, "#422d0b"),
        )
        tot_14d = sum(v for _, v in spend_pts)
        app.zen_chart.set_series(
            "近 14 天每日等效消耗统计图 (USD)",
            spend_pts,
            mode="bar",
            line_color=ACCENT_GOLD,
            fill_color="#38220f",
            unit_prefix="$",
            unit_suffix="",
            fixed_y_range=None,
            empty_hint="最近 14 天暂无本地会话记录",
            summary_badge=f"14日合计 ${tot_14d:.2f}",
        )
    elif zm == "cache":
        rst = app.__dict__.get("_radar_state", CacheRadarState())
        fg_col, bar_col, _ = _radar_palette(rst)
        if rst.phase == PHASE_ACTIVE:
            big_v = "推理中"
            b_txt, b_fg, b_bg, r_cols = ("⚡ 活跃推理", ACCENT_CYAN, ACCENT_CYAN_DIM, (ACCENT_CYAN, ACCENT_CYAN_DIM))
        elif rst.phase == PHASE_HOT:
            big_v = format_mmss(rst.remaining_sec, pad_min=True)
            b_txt, b_fg, b_bg, r_cols = ("🔥 缓存热态", ACCENT_GREEN, ACCENT_GREEN_DIM, (ACCENT_GREEN, ACCENT_GREEN_DIM))
        elif rst.phase == PHASE_WARNING:
            big_v = format_mmss(rst.remaining_sec, pad_min=True)
            b_txt, b_fg, b_bg, r_cols = ("⚠️ 临期预警", ACCENT_ROSE, ACCENT_CRIMSON_DIM, (ACCENT_ROSE, ACCENT_CRIMSON_DIM))
        elif rst.phase == PHASE_COLD:
            big_v = "00:00"
            b_txt, b_fg, b_bg, r_cols = ("❄️ 缓存已冷", FG_MUTED, BG_CARD_ALT, ("#475569", BG_CARD_ALT))
        else:
            big_v = "--:--"
            b_txt, b_fg, b_bg, r_cols = ("⚪ 待命观测", FG_MUTED, BG_CARD_ALT, ("#334155", BG_CARD_ALT))
        _ = (fg_col, bar_col)
        app.zen_dial.lbl_title.configure(text="📡 活跃会话 KV 缓存 250s 余温")
        app.zen_dial.set_custom_metric(
            ring_fraction=rst.ttl_fraction,
            top_caption=f"当前监控上下文 {rst.context_label}",
            big_value=big_v,
            sub_caption=f"上轮缓存命中率 {rst.cache_hit_pct}% (读取 {rst.cache_read_tokens:,})",
            badge_text=b_txt,
            badge_fg=b_fg,
            badge_bg=b_bg,
            footer_text=rst.badge_text,
            ring_colors=r_cols,
        )
        cache_pts = [
            (
                str(getattr(d, "local_date", ""))[5:]
                if len(str(getattr(d, "local_date", ""))) >= 10
                else str(getattr(d, "local_date", "")),
                round(float(getattr(d, "cache_read_tokens", 0) or 0) / 1_000_000.0, 2),
            )
            for d in sorted(app._last_daily, key=lambda item: str(getattr(item, "local_date", "")))[-14:]
        ]
        tot_m = sum(v for _, v in cache_pts)
        app.zen_chart.set_series(
            "近 14 天每日缓存命中读取总量 (百万 Tokens / M)",
            cache_pts,
            mode="bar",
            line_color=ACCENT_CYAN,
            fill_color="#0f2438",
            unit_suffix="M",
            fixed_y_range=None,
            empty_hint="近期暂无本地缓存读取记录",
            summary_badge=f"14日累计 {tot_m:.1f}M",
        )
    else:  # latency
        avg_ms = float(sc.avg_ms or 0.0)
        health_f = max(0.12, 1.0 - min(0.88, avg_ms / 2500.0)) if sc.eval_count > 0 else 1.0
        if sc.timeout_count > 0 or avg_ms >= 1200:
            b_txt, b_fg, b_bg, r_cols = ("🟠 审查偏慢", ACCENT_AMBER, ACCENT_AMBER_DIM, (ACCENT_AMBER, ACCENT_AMBER_DIM))
        else:
            b_txt, b_fg, b_bg, r_cols = ("🟢 响应顺畅", ACCENT_GREEN, ACCENT_GREEN_DIM, (ACCENT_GREEN, ACCENT_GREEN_DIM))
        app.zen_dial.lbl_title.configure(text="⚡ 客户端响应与风控顺畅度")
        app.zen_dial.set_custom_metric(
            ring_fraction=health_f,
            top_caption="旁路安全审查平均耗时",
            big_value=f"{avg_ms:.0f} ms" if sc.eval_count > 0 else "0 ms",
            sub_caption=f"429 限流拥堵 {r.log_stats.http_429_503_count} 次",
            badge_text=b_txt,
            badge_fg=b_fg,
            badge_bg=b_bg,
            footer_text=f"本轮累计审查 {sc.eval_count} 次 · 峰值 {sc.max_ms:.0f} ms",
            ring_colors=r_cols,
        )
        app.zen_chart.set_series(
            "最近请求旁路审查延迟波动图 (ms)",
            lat_pts,
            mode="area",
            line_color=ACCENT_GREEN if avg_ms < 1200 else ACCENT_AMBER,
            fill_color="#0a2e23" if avg_ms < 1200 else "#38220f",
            unit_suffix=" ms",
            fixed_y_range=None,
            empty_hint="暂未检测到风控附加延迟（当前请求通道完全畅通）",
            summary_badge=f"峰值 {sc.max_ms:.0f} ms" if sc.eval_count else "畅通无阻",
        )


__all__ = ["build_zen_mode_ui", "refresh_zen_mode_ui"]
