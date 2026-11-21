# -*- coding: utf-8 -*-
"""
Acerola-Orion 内置标签页模块化包 (acerola_orion/ui/tabs/)
- 协调 `common` / `zen_view` / `hub_tab` / `ledger_tab` / `vault_tab` / `diag_tab` 六个独立视图子模块
"""

from __future__ import annotations

from datetime import datetime
import math
from typing import Any, List, Optional

from ...constants import APP_CODENAME
from ...telemetry import OrionTelemetryReport
from ...vault import VaultSessionItem, VaultStatusReport
from .common import (
    build_header,
    configure_styles,
    create_dark_text_box,
    create_scrolled_treeview,
    draw_mini_emblem,
    make_instrument_plate,
)
from .diag_tab import build_diag_tab, refresh_diag_tab_data, refresh_diag_waveform
from .hub_tab import (
    build_daily_spend_points,
    build_hub_tab,
    build_quota_points,
    refresh_hub_chart,
    refresh_hub_tab_data,
)
from .ledger_tab import build_ledger_tab, refresh_ledger_tab_data
from .vault_tab import build_vault_tab, refresh_vault_tab_data
from .zen_view import build_zen_mode_ui, refresh_zen_mode_ui


def refresh_active_charts_and_zen(app: Any) -> None:
    """Refresh Hub chart, Diagnostics waveform chart, and Zen mode hero dial + chart."""
    r = app._last_report
    if r is None:
        return
    bmap = {b.bucket_id: b for b in r.quota_buckets}
    missing_msg = "客户端未启动" if not r.ls_running else "暂无该通道数据"

    g5 = bmap.get("gemini-5h")
    gw = bmap.get("gemini-weekly")
    p5 = bmap.get("3p-5h")
    pw = bmap.get("3p-weekly")

    gem_pts = app._build_quota_points("gemini_5h", g5)
    cld_pts = app._build_quota_points("p3_5h", p5)
    spend_pts = app._build_daily_spend_points()
    sc = r.log_stats.sidecar
    lat_pts = [(f"#{i + 1}", float(v)) for i, v in enumerate(sc.recent_durations_ms[-36:])]

    refresh_hub_chart(app, g5, p5, gem_pts, cld_pts, spend_pts)
    refresh_diag_waveform(app, r, lat_pts)
    refresh_zen_mode_ui(app, r, g5, gw, p5, pw, gem_pts, cld_pts, spend_pts, lat_pts, missing_msg)


def apply_builtin_tabs_data(
    app: Any,
    r: OrionTelemetryReport,
    daily: List[Any],
    models: List[Any],
    sessions: List[Any],
    audit: Any,
    guard_lines: List[str],
    ls_tail: List[str],
    silent: bool,
    vault_st: Optional[VaultStatusReport] = None,
    vault_rows: Optional[List[VaultSessionItem]] = None,
) -> None:
    """Apply telemetry, ledger, vault, and diagnostics data onto all built-in tabs."""
    app.lbl_sync_time.configure(text=f"同步: {r.captured_at}  |  LS PID={r.ls_pid or '离线'}")

    today_str = datetime.now().strftime("%Y-%m-%d")
    today_row = next((d for d in daily if getattr(d, "local_date", "") == today_str), None)
    bmap = {b.bucket_id: b for b in r.quota_buckets}

    refresh_hub_tab_data(app, r, today_row, audit, bmap)
    app._refresh_active_charts_and_zen()
    refresh_ledger_tab_data(app, today_str, today_row, daily, models, sessions, audit)
    refresh_diag_tab_data(app, r, guard_lines, ls_tail)
    refresh_vault_tab_data(app, vault_st, vault_rows)

    def _pct_txt(bid: str) -> str:
        b = bmap.get(bid)
        v = getattr(b, "remaining_pct", math.nan) if b is not None else math.nan
        return f"{v:.2f}%" if isinstance(v, (int, float)) and math.isfinite(v) else "--.--%"

    app._last_base_tooltip = (
        f"{APP_CODENAME} · 5h: {_pct_txt('gemini-5h')} | 周: {_pct_txt('gemini-weekly')}"
    )
    if "_radar_state" in app.__dict__:
        app._apply_radar_state_to_ui(app._radar_state, force_tooltip=True)
    else:
        tray = app.__dict__.get("_tray")
        if tray is not None and tray.active:
            tray.update_tooltip(app._last_base_tooltip)

    if not silent:
        app._append_console(
            f"✓ 星盘同步: Tier={r.paid_tier_id or r.user_tier_id or '-'} "
            f"Gemini-5h={_pct_txt('gemini-5h')} "
            f"Weekly={_pct_txt('gemini-weekly')}"
        )


__all__ = [
    "configure_styles",
    "draw_mini_emblem",
    "build_header",
    "make_instrument_plate",
    "create_dark_text_box",
    "create_scrolled_treeview",
    "build_zen_mode_ui",
    "refresh_zen_mode_ui",
    "build_hub_tab",
    "refresh_hub_chart",
    "refresh_hub_tab_data",
    "build_ledger_tab",
    "refresh_ledger_tab_data",
    "build_vault_tab",
    "refresh_vault_tab_data",
    "build_diag_tab",
    "refresh_diag_waveform",
    "refresh_diag_tab_data",
    "build_quota_points",
    "build_daily_spend_points",
    "refresh_active_charts_and_zen",
    "apply_builtin_tabs_data",
]
