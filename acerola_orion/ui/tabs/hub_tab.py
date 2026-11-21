# -*- coding: utf-8 -*-
"""
Acerola-Orion 星盘总览标签页 (Hub Observatory Tab) 模块 (acerola_orion/ui/tabs/hub_tab.py)
- 封装双星盘计时仪、KV 缓存雷达卡、三联状态铭牌、插件零占位扩展槽 `app.hub_plugin_slot` 及走势图/事件流
"""

from __future__ import annotations

from datetime import datetime
import math
import tkinter as tk
from typing import Any, List, Tuple

from ...cache_radar import COMPACT_COMMAND_TEXT
from ...guillotine import install_guard_autostart, uninstall_guard_autostart
from ...telemetry import OrionTelemetryReport
from ..astrolabe import AstrolabeDialWidget
from ..charts import OrionChartWidget
from ..theme import (
    ACCENT_AMBER,
    ACCENT_CRIMSON,
    ACCENT_CYAN,
    ACCENT_CYAN_DIM,
    ACCENT_GOLD,
    ACCENT_GREEN,
    ACCENT_ROSE,
    BG_CARD,
    BG_CARD_ALT,
    BG_ROOT,
    BG_VOID,
    BORDER_SUBTLE,
    FG_DIM,
    FG_MUTED,
    FG_TEXT,
)


def build_hub_tab(app: Any) -> None:
    """Build Hub Observatory Tab (Dual Astrolabes + KV Cache Radar + 3 Plates + Plugin Slot + Charts/Console)."""
    # Row 1: Dual Orion Astrolabe Chronographs (Gemini & Claude/3P)
    dials_row = tk.Frame(app.tab_hub, bg=BG_ROOT)
    dials_row.pack(fill=tk.X, pady=(0, app._px(8)))
    dials_row.columnconfigure(0, weight=1, uniform="orion_dial")
    dials_row.columnconfigure(1, weight=1, uniform="orion_dial")

    app.dial_gemini = AstrolabeDialWidget(
        dials_row,
        title="✦ Gemini 星盘计时仪 (5h 外轨 · 周池内轨 · 金针时间基准)",
        subtitle_tag="GEMINI ASTROLABE",
        primary_hue="cyan",
        dial_px=app._px(208),
        simplified_digits=False,
        dpi_scale=app._dpi_scale,
    )
    app.dial_gemini.grid(row=0, column=0, sticky="nsew", padx=(0, app._px(5)))

    app.dial_claude = AstrolabeDialWidget(
        dials_row,
        title="◆ Claude / GPT 星盘计时仪 (3p-5h 外轨 · 3p-weekly 内轨)",
        subtitle_tag="CLAUDE / 3P ASTROLABE",
        primary_hue="rose",
        dial_px=app._px(208),
        simplified_digits=False,
        dpi_scale=app._dpi_scale,
    )
    app.dial_claude.grid(row=0, column=1, sticky="nsew", padx=(app._px(5), 0))

    # Row 1.5: Passive Prompt KV Cache 250s TTL Radar Bar (100% local read-only observability)
    app.radar_card = tk.Frame(
        app.tab_hub,
        bg=BG_CARD,
        highlightthickness=1,
        highlightbackground=BORDER_SUBTLE,
        padx=app._px(12),
        pady=app._px(6),
    )
    app.radar_card.pack(fill=tk.X, pady=(0, app._px(8)))

    r_top = tk.Frame(app.radar_card, bg=BG_CARD)
    r_top.pack(fill=tk.X)

    r_ctrl = tk.Frame(r_top, bg=BG_CARD)
    r_ctrl.pack(side=tk.RIGHT)

    app.btn_target_switch = tk.Button(
        r_ctrl,
        text="🧠 守大窗 ▾",
        command=app._popup_target_menu,
        bg=ACCENT_CYAN_DIM,
        fg=ACCENT_CYAN,
        activebackground=ACCENT_CRIMSON,
        activeforeground="#ffffff",
        relief="flat",
        padx=8,
        pady=1,
        font=("Microsoft YaHei UI", 8, "bold"),
        cursor="hand2",
    )
    app.btn_target_switch.pack(side=tk.LEFT, padx=(0, 6))

    tk.Label(r_ctrl, text="悬浮胶囊:", bg=BG_CARD, fg=FG_DIM, font=("Microsoft YaHei UI", 8)).pack(
        side=tk.LEFT, padx=(0, 3)
    )
    app._hud_mode_buttons = {}
    for hm_key, hm_lbl in (("smart", "智能预警"), ("always", "常驻"), ("off", "关闭")):
        hb = tk.Button(
            r_ctrl,
            text=hm_lbl,
            command=lambda k=hm_key: app.set_hud_mode(k),
            relief="flat",
            padx=7,
            pady=1,
            font=("Microsoft YaHei UI", 8, "bold"),
            cursor="hand2",
        )
        hb.pack(side=tk.LEFT, padx=1)
        app._hud_mode_buttons[hm_key] = hb

    r_left = tk.Frame(r_top, bg=BG_CARD)
    r_left.pack(side=tk.LEFT, fill=tk.X, expand=True)
    tk.Label(
        r_left,
        text="📡 KV 缓存雷达",
        bg=BG_CARD_ALT,
        fg=ACCENT_ROSE,
        padx=6,
        pady=1,
        font=("Microsoft YaHei UI", 8, "bold"),
    ).pack(side=tk.LEFT, padx=(0, 8))
    app.lbl_radar_main = tk.Label(
        r_left,
        text="⚪ 暂无活跃会话上下文",
        bg=BG_CARD,
        fg=FG_MUTED,
        font=("Microsoft YaHei UI", 9, "bold"),
        anchor="w",
    )
    app.lbl_radar_main.pack(side=tk.LEFT, fill=tk.X, expand=True)

    app.lbl_radar_tier_badge = tk.Label(
        r_left,
        text="",
        bg="#4c0519",
        fg="#fda4af",
        font=("Microsoft YaHei UI", 8, "bold"),
        padx=8,
        pady=1,
    )

    app.radar_canvas = tk.Canvas(
        app.radar_card,
        height=max(4, app._px(5)),
        bg=BG_VOID,
        highlightthickness=0,
    )
    app.radar_canvas.pack(fill=tk.X, pady=(4, 3))
    app.radar_canvas.bind("<Configure>", lambda _e: app._redraw_radar_bar(allow_defer=False))

    app.lbl_radar_sub = tk.Label(
        app.radar_card,
        text="等待 Antigravity 活跃对话采样（纯本地无锁尾读 · 零额外发包）...",
        bg=BG_CARD,
        fg=FG_MUTED,
        font=("Consolas", 8),
        anchor="w",
        justify="left",
    )
    app.lbl_radar_sub.pack(anchor="w", fill=tk.X)

    app.radar_tier_bar = tk.Frame(
        app.radar_card,
        bg="#2a0a18",
        highlightthickness=1,
        highlightbackground=ACCENT_CRIMSON,
        padx=8,
        pady=3,
    )
    app.btn_copy_compact = tk.Button(
        app.radar_tier_bar,
        text=f"✂️ 复制 {COMPACT_COMMAND_TEXT}",
        command=app.copy_compact_command,
        bg=ACCENT_CRIMSON,
        fg="#ffffff",
        activebackground=ACCENT_ROSE,
        activeforeground="#ffffff",
        relief="flat",
        padx=8,
        pady=1,
        font=("Microsoft YaHei UI", 8, "bold"),
        cursor="hand2",
    )
    app.btn_copy_compact.pack(side=tk.RIGHT, padx=(8, 0))
    app.lbl_radar_tier_alert = tk.Label(
        app.radar_tier_bar,
        text="",
        bg="#2a0a18",
        fg="#fda4af",
        font=("Microsoft YaHei UI", 8, "bold"),
        anchor="w",
        justify="left",
    )
    app.lbl_radar_tier_alert.pack(side=tk.LEFT, fill=tk.X, expand=True)

    app.radar_card.bind("<Configure>", app._on_radar_card_configure)
    app._refresh_hud_mode_buttons()
    app._refresh_target_switch_button()

    # Row 2: 3 Sleek Instrument Plates (IPC + Tier/Audit + Guillotine Guard Switchboard)
    plates_row = tk.Frame(app.tab_hub, bg=BG_ROOT)
    plates_row.pack(fill=tk.X, pady=(0, app._px(8)))
    for col in range(3):
        plates_row.columnconfigure(col, weight=1, uniform="hub_plate")

    p1, app.lbl_ipc_main, app.lbl_ipc_sub = app._make_instrument_plate(
        plates_row, "🔭 本地语言服务回环拓扑 (127.0.0.1)"
    )
    p1.grid(row=0, column=0, sticky="nsew", padx=(0, app._px(5)))

    p2, app.lbl_tier_main, app.lbl_tier_sub = app._make_instrument_plate(
        plates_row, "⚖️ 订阅层级 · 预设倍率估算与拥堵体征"
    )
    p2.grid(row=0, column=1, sticky="nsew", padx=app._px(5))

    # Plate 3: Guard Status + Integrated Tactile Controls
    p3 = tk.Frame(
        plates_row,
        bg=BG_CARD,
        highlightthickness=1,
        highlightbackground=BORDER_SUBTLE,
        padx=app._px(12),
        pady=app._px(8),
    )
    p3.grid(row=0, column=2, sticky="nsew", padx=(app._px(5), 0))
    tk.Label(
        p3,
        text="⚔️ Acerola 事件驱动进程断头台守卫",
        bg=BG_CARD,
        fg=FG_MUTED,
        font=("Microsoft YaHei UI", 8, "bold"),
    ).pack(anchor="w")
    app.lbl_guard_main = tk.Label(
        p3, text="--", bg=BG_CARD, fg=FG_TEXT, font=("Microsoft YaHei UI", 11, "bold"), pady=2
    )
    app.lbl_guard_main.pack(anchor="w")
    app.lbl_guard_sub = tk.Label(
        p3, text="--", bg=BG_CARD, fg=FG_MUTED, font=("Consolas", 8), anchor="w", justify="left"
    )
    app.lbl_guard_sub.pack(anchor="w", fill=tk.X, pady=(0, 4))
    p3.bind(
        "<Configure>",
        lambda e, lbl=app.lbl_guard_sub: lbl.configure(wraplength=max(160, int(e.width) - 24)),
    )

    act_bar = tk.Frame(p3, bg=BG_CARD)
    act_bar.pack(fill=tk.X)
    btn_specs = [
        ("🛡️ 自启守卫", "#065f46", lambda: app._run_action("部署断头台内核守卫", install_guard_autostart)),
        ("⏹️ 停用", "#272136", lambda: app._run_action("停用断头台内核守卫", uninstall_guard_autostart)),
        ("📸 采样快照", ACCENT_CRIMSON, lambda: app._run_action("采样配额水位快照", app._sample_watermark_now)),
    ]
    app._action_buttons.clear()
    for idx, (label, color, cb) in enumerate(btn_specs):
        act_bar.columnconfigure(idx, weight=1)
        b = tk.Button(
            act_bar,
            text=label,
            command=cb,
            bg=color,
            fg="#ffffff",
            disabledforeground=FG_MUTED,
            activebackground=ACCENT_ROSE,
            activeforeground="#ffffff",
            relief="flat",
            padx=6,
            pady=3,
            font=("Microsoft YaHei UI", 8, "bold"),
            cursor="hand2",
        )
        b.grid(row=0, column=idx, sticky="ew", padx=(0 if idx == 0 else 3, 0))
        app._action_buttons.append(b)

    # Optional Hub extension slot: intentionally NOT packed until a plugin registers an action panel
    app.hub_plugin_slot = tk.Frame(app.tab_hub, bg=BG_ROOT)

    # Row 3: Live Telemetry Trajectory Chart (left 62%) + Compact Event Console (right 38%)
    bottom_row = tk.Frame(app.tab_hub, bg=BG_ROOT)
    bottom_row.pack(fill=tk.BOTH, expand=True)
    app.hub_bottom_row = bottom_row
    bottom_row.columnconfigure(0, weight=62)
    bottom_row.columnconfigure(1, weight=38)
    bottom_row.rowconfigure(0, weight=1)

    chart_col = tk.Frame(bottom_row, bg=BG_CARD, highlightthickness=1, highlightbackground=BORDER_SUBTLE)
    chart_col.grid(row=0, column=0, sticky="nsew", padx=(0, app._px(5)))

    c_switch = tk.Frame(chart_col, bg=BG_CARD, padx=10, pady=5)
    c_switch.pack(fill=tk.X)
    tk.Label(
        c_switch, text="📈 观测曲线切换:", bg=BG_CARD, fg=FG_MUTED, font=("Microsoft YaHei UI", 8)
    ).pack(side=tk.LEFT, padx=(0, 6))

    app._hub_chart_btns = {}
    for key, lbl in (("gemini", "Gemini 水位轨迹"), ("claude", "Claude/3P 轨迹"), ("spend", "14日等效账单")):
        b = tk.Button(
            c_switch,
            text=lbl,
            command=lambda k=key: app.select_hub_chart(k),
            relief="flat",
            padx=8,
            pady=2,
            font=("Microsoft YaHei UI", 8, "bold"),
            cursor="hand2",
        )
        b.pack(side=tk.LEFT, padx=2)
        app._hub_chart_btns[key] = b

    app.hub_chart = OrionChartWidget(
        chart_col,
        title="近 24 小时 Gemini 配额水位轨迹",
        height_px=app._px(145),
        dpi_scale=app._dpi_scale,
    )
    app.hub_chart.configure(highlightthickness=0, padx=10, pady=4)
    app.hub_chart.pack(fill=tk.BOTH, expand=True)
    app.select_hub_chart("gemini")

    log_box = tk.Frame(bottom_row, bg=BG_CARD, highlightthickness=1, highlightbackground=BORDER_SUBTLE)
    log_box.grid(row=0, column=1, sticky="nsew", padx=(app._px(5), 0))
    tk.Label(
        log_box,
        text="✦ 实时遥测事件流 (Event Stream)",
        bg=BG_CARD,
        fg=FG_MUTED,
        padx=10,
        pady=5,
        font=("Microsoft YaHei UI", 8, "bold"),
    ).pack(anchor="w")
    app.txt_console = app._create_dark_text_box(log_box, fg_color="#e2e8f0", font_pt=8, height=6)
    app._append_console("✦ 猎户座星盘观测台已就绪。正在读取 127.0.0.1 状态与本地账本...")


def build_quota_points(app: Any, col_key: str, live_bucket: Any) -> List[Tuple[str, float]]:
    pts: List[Tuple[str, float]] = []
    for row in app._last_quota_series:
        v = row.get(col_key)
        if isinstance(v, (int, float)) and math.isfinite(v):
            pts.append((str(row.get("label") or ""), float(v)))
    if live_bucket is not None and math.isfinite(getattr(live_bucket, "remaining_pct", math.nan)):
        now_lbl = datetime.now().strftime("%H:%M")
        live_v = float(live_bucket.remaining_pct)
        if not pts or abs(pts[-1][1] - live_v) > 0.001 or pts[-1][0] != now_lbl:
            pts.append((now_lbl, live_v))
    return pts


def build_daily_spend_points(app: Any) -> List[Tuple[str, float]]:
    rows = sorted(app._last_daily, key=lambda d: str(getattr(d, "local_date", "")))
    out: List[Tuple[str, float]] = []
    for d in rows[-14:]:
        dt_s = str(getattr(d, "local_date", ""))
        short_dt = dt_s[5:] if len(dt_s) >= 10 else dt_s
        val = float(getattr(d, "usd_agy_est", 0.0) or 0.0)
        out.append((short_dt, val))
    return out


def refresh_hub_chart(
    app: Any,
    g5: Any,
    p5: Any,
    gem_pts: List[Tuple[str, float]],
    cld_pts: List[Tuple[str, float]],
    spend_pts: List[Tuple[str, float]],
) -> None:
    """Refresh the Hub tab primary statistical trajectory chart."""
    if app._hub_chart_kind == "gemini":
        cur_badge = f"最新 {g5.remaining_pct:.2f}%" if g5 and math.isfinite(g5.remaining_pct) else ""
        app.hub_chart.set_series(
            "📈 近 24 小时 Gemini 5h 配额水位轨迹 (%)",
            gem_pts,
            mode="area",
            line_color=ACCENT_CYAN,
            fill_color="#0f2438",
            unit_suffix="%",
            fixed_y_range=(0.0, 100.0),
            empty_hint="打开 Antigravity 后自动绘制 Gemini 配额水位轨迹",
            summary_badge=cur_badge,
        )
    elif app._hub_chart_kind == "claude":
        cur_badge = f"最新 {p5.remaining_pct:.2f}%" if p5 and math.isfinite(p5.remaining_pct) else ""
        app.hub_chart.set_series(
            "📈 近 24 小时 Claude / 3P 5h 配额水位轨迹 (%)",
            cld_pts,
            mode="area",
            line_color=ACCENT_ROSE,
            fill_color="#2e1020",
            unit_suffix="%",
            fixed_y_range=(0.0, 100.0),
            empty_hint="当前暂无 Claude / 3P 配额采样记录",
            summary_badge=cur_badge,
        )
    else:
        tot_14d = sum(v for _, v in spend_pts)
        app.hub_chart.set_series(
            "📊 近 14 天每日等效消耗趋势 (1.5x 权重美元折算)",
            spend_pts,
            mode="bar",
            line_color=ACCENT_GOLD,
            fill_color="#38220f",
            unit_prefix="$",
            unit_suffix="",
            fixed_y_range=None,
            empty_hint="近期暂无本地对话 Token 账单记录",
            summary_badge=f"14日累计 ${tot_14d:.2f}",
        )


def refresh_hub_tab_data(
    app: Any,
    r: OrionTelemetryReport,
    today_row: Any,
    audit: Any,
    bmap: dict,
) -> None:
    """Update the Hub tab 3 instrument plates and dual Astrolabe chronograph dials."""
    # Plate 1: Local IPC Topology
    if not r.ls_running:
        app.lbl_ipc_main.configure(text="⚪ 离线 (客户端未启动)", fg=FG_MUTED)
        app.lbl_ipc_sub.configure(text="启动 Antigravity 后自动锁定回环端口")
    else:
        ver_txt = f"v{r.ide_version}" if r.ide_version else "在线"
        app.lbl_ipc_main.configure(text=f"🟢 在线 ({ver_txt})", fg=ACCENT_GREEN)
        app.lbl_ipc_sub.configure(
            text=f"127.0.0.1:{r.http_port} | 主进程 PID={r.main_pids}\n网络策略: {r.proxy_mode}"
        )

    # Plate 2: Tier, 1.5x Preset Estimate Quick Look & Congestion
    phantom_cnt = len(getattr(audit, "phantom_drains", []) or [])
    phantom_tag = "0疑似空耗" if phantom_cnt == 0 else f"⚠️异常掉点x{phantom_cnt}"
    if r.ls_running:
        tier_txt = r.paid_tier_name or r.paid_tier_id or r.user_tier_id or "未知 Tier"
        app.lbl_tier_main.configure(text=f"👑 {tier_txt}", fg=ACCENT_CYAN)
        sc = r.log_stats.sidecar
        td_usd = f"${today_row.usd_agy_est:.2f}" if today_row else "$0.00"
        app.lbl_tier_sub.configure(
            text=f"今日预设倍率估: {td_usd} ({phantom_tag}) | 主429: {r.log_stats.http_429_503_count}次 (旁路{getattr(r.log_stats, 'aux_429_count', 0)}次)\n"
            f"旁路审查均值: {sc.avg_ms:.0f} ms (10s超时 {sc.timeout_count} 次)"
        )
    else:
        td_usd = f"${today_row.usd_agy_est:.2f}" if today_row else "$0.00"
        app.lbl_tier_main.configure(text=f"📒 今日账本 {td_usd}", fg=ACCENT_GOLD)
        app.lbl_tier_sub.configure(text=f"对账体征: {phantom_tag} | 累计快照 {getattr(audit, 'total_snapshots', 0)} 笔")

    # Plate 3: Guillotine Process Guard
    g = r.guard
    if g.mutex_active:
        app.lbl_guard_main.configure(text="🟢 进程守卫运行中", fg=ACCENT_GREEN)
    else:
        app.lbl_guard_main.configure(text="⚪ 守卫未驻留", fg=ACCENT_AMBER)
    leg_warn = f" | ⚠️旧脚本:{g.legacy_pids}" if g.legacy_buggy_killer_active else ""
    auto_desc = "开机静默自启(不弹面板)" if g.scheduled_task_installed else "未开自启"
    app.lbl_guard_sub.configure(
        text=f"{auto_desc} · 防关窗压缩/污染记忆 | hub:{r.stale_hub_files}{leg_warn}"
    )

    # Dual Orion Astrolabe Chronographs (Full Mode)
    missing_msg = "客户端未运行" if not r.ls_running else "暂无配额数据"
    g5 = bmap.get("gemini-5h")
    gw = bmap.get("gemini-weekly")
    p5 = bmap.get("3p-5h")
    pw = bmap.get("3p-weekly")

    app.dial_gemini.set_quota_pair(
        g5.remaining_fraction if g5 else math.nan,
        g5.reset_countdown if g5 else "--",
        g5.reset_time if g5 else "",
        gw.remaining_fraction if gw else math.nan,
        gw.reset_countdown if gw else "--",
        offline_reason=missing_msg,
    )
    app.dial_claude.set_quota_pair(
        p5.remaining_fraction if p5 else math.nan,
        p5.reset_countdown if p5 else "--",
        p5.reset_time if p5 else "",
        pw.remaining_fraction if pw else math.nan,
        pw.reset_countdown if pw else "--",
        offline_reason=missing_msg,
    )


__all__ = [
    "build_hub_tab",
    "build_quota_points",
    "build_daily_spend_points",
    "refresh_hub_chart",
    "refresh_hub_tab_data",
]
