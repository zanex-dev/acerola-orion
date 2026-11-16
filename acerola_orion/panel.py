# -*- coding: utf-8 -*-
"""
Acerola-Orion 桌面星盘主控台 (acerola_orion/panel.py)
- 视觉升级：猎户座星盘 · 精密机械双轨计时仪 (Orion Astrolabe & Chronograph)
- 双视图模式无缝切换：
  1) 全盘观测台 (Full Mode)：双座猎户座星盘计时仪 + 1.5x 电子秤验钞账本 + 零轮询断头台 + 风控示波台
  2) 极简专注模式 (Zen Mode)：专为直观展示设计，同屏严格遵循「1 个核心数据 + 1 张统计图」
- 原生 Win32 DWM 沉浸式暗黑标题栏 + 全分辨率 High-DPI 自适应 + 0% CPU 托盘休眠
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .constants import (
    APP_CODENAME,
    APP_TRINITY,
    APP_VERSION,
    atomic_write_text,
    get_data_dir,
    get_guillotine_log_path,
    get_ls_log_path,
)
from .guillotine import install_guard_autostart, uninstall_guard_autostart
from .ledger import OrionLedger
from .rpc import read_shared_file_lines, terminate_pid_win32
from .telemetry import (
    OrionTelemetryReport,
    collect_telemetry,
    invalidate_guard_probe_cache,
    load_json_safe,
)
from .win_tray import (
    MenuSpec,
    Win32TrayController,
    apply_tk_window_icon,
    bring_tk_window_to_front,
    set_app_user_model_id,
    signal_existing_instance,
)

APP_USER_MODEL_ID = "Trinity.AcerolaOrion.PublicAstrolabe"
TRAY_CLASS_NAME = "AcerolaOrionPublicAstrolabe_v2"

# Obsidian + Kiss-Shot Crimson + Orion Starlight Palette
BG_VOID = "#08060c"
BG_ROOT = "#0c0912"
BG_HEADER = "#120a18"
BG_CARD = "#14101d"
BG_CARD_ALT = "#1c1528"
BG_DIAL_WELL = "#0e0b15"
FG_TEXT = "#f8fafc"
FG_SUB = "#cbd5e1"
FG_MUTED = "#8b9bb4"
FG_DIM = "#526077"

ACCENT_CRIMSON = "#e11d48"
ACCENT_CRIMSON_DIM = "#4c0519"
ACCENT_ROSE = "#fb7185"
ACCENT_CYAN = "#38bdf8"
ACCENT_CYAN_DIM = "#0c2d48"
ACCENT_GREEN = "#10b981"
ACCENT_GREEN_DIM = "#063b2b"
ACCENT_AMBER = "#f59e0b"
ACCENT_AMBER_DIM = "#452706"
ACCENT_GOLD = "#fde047"
ACCENT_VIOLET = "#a78bfa"

BORDER_SUBTLE = "#261d36"
BORDER_CRIMSON = "#54162b"
BORDER_ACTIVE = "#be123c"


def _panel_window_title() -> str:
    return f"{APP_CODENAME} v{APP_VERSION} · Astrolabe — {APP_TRINITY}"


def _enable_high_dpi() -> None:
    if sys.platform == "win32":
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:
                pass


def _apply_win32_dark_chrome(win: tk.Tk) -> None:
    """Apply native Windows 10/11 DWM immersive dark titlebar and obsidian caption/border tint."""
    if sys.platform != "win32":
        return
    try:
        win.update_idletasks()
        hwnd = int(win.winfo_id())
        parent = ctypes.windll.user32.GetParent(wintypes.HWND(hwnd))
        target = int(parent) if parent else hwnd
        dwm = ctypes.windll.dwmapi
        dark_val = ctypes.c_int(1)
        for attr in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE
            if dwm.DwmSetWindowAttribute(
                wintypes.HWND(target),
                ctypes.c_uint(attr),
                ctypes.byref(dark_val),
                ctypes.sizeof(dark_val),
            ) == 0:
                break
        # Windows 11 DWMWA_CAPTION_COLOR (35) & DWMWA_BORDER_COLOR (34), COLORREF = 0x00BBGGRR
        cap_color = ctypes.c_uint(0x00180A12)  # #120a18
        bdr_color = ctypes.c_uint(0x00361D26)  # #261d36
        dwm.DwmSetWindowAttribute(
            wintypes.HWND(target), ctypes.c_uint(35), ctypes.byref(cap_color), ctypes.sizeof(cap_color)
        )
        dwm.DwmSetWindowAttribute(
            wintypes.HWND(target), ctypes.c_uint(34), ctypes.byref(bdr_color), ctypes.sizeof(bdr_color)
        )
    except Exception:
        pass


def _get_icon_path() -> Path:
    return Path(__file__).resolve().parent.parent / "assets" / "app.ico"


def _get_ui_prefs_path() -> Path:
    return get_data_dir() / "ui_prefs.json"


def _load_ui_prefs() -> Dict[str, Any]:
    raw = load_json_safe(_get_ui_prefs_path(), default={})
    mode = str(raw.get("view_mode") or "full").strip().lower()
    if mode not in ("full", "zen"):
        mode = "full"
    metric = str(raw.get("zen_metric") or "gemini").strip().lower()
    if metric not in ("gemini", "claude", "spend", "latency"):
        metric = "gemini"
    return {"view_mode": mode, "zen_metric": metric}


def _save_ui_prefs(view_mode: str, zen_metric: str) -> None:
    try:
        payload = json.dumps(
            {"view_mode": view_mode, "zen_metric": zen_metric},
            ensure_ascii=False,
            indent=2,
        )
        atomic_write_text(_get_ui_prefs_path(), payload)
    except Exception:
        pass


def _activate_existing_panel_window() -> bool:
    """If an identical-version panel (or tray-resident instance) is already open, restore and focus it."""
    return signal_existing_instance(TRAY_CLASS_NAME, _panel_window_title())


def _compute_remaining_window_fraction(reset_iso: str, window_sec: float = 18000.0) -> Optional[float]:
    """Compute remaining time fraction [0..1] within the quota rolling window from ISO reset timestamp."""
    if not reset_iso or window_sec <= 0:
        return None
    try:
        s = reset_iso.strip()
        if not s:
            return None
        if s.endswith("Z") or s.endswith("z"):
            s = s[:-1] + "+00:00"
        target = datetime.fromisoformat(s)
        if target.tzinfo is None:
            target = target.replace(tzinfo=timezone.utc)
        rem = (target - datetime.now(timezone.utc)).total_seconds()
        if not math.isfinite(rem) or rem < 0:
            return 0.0
        if rem > window_sec * 1.05:
            return None
        return max(0.0, min(1.0, rem / window_sec))
    except Exception:
        return None


def _quota_semantic_colors(pct: float, primary_hue: str = "cyan") -> Tuple[str, str]:
    """Return (bright_hex, dim_halo_hex) for a 0..100 percentage."""
    if not math.isfinite(pct):
        return FG_DIM, "#1c1629"
    if pct < 18.0:
        return ACCENT_CRIMSON, ACCENT_CRIMSON_DIM
    if pct < 38.0:
        return ACCENT_AMBER, ACCENT_AMBER_DIM
    if primary_hue == "rose":
        return ACCENT_ROSE, "#3f1024"
    if primary_hue == "emerald":
        return ACCENT_GREEN, ACCENT_GREEN_DIM
    return ACCENT_CYAN, ACCENT_CYAN_DIM


def _rotated_ellipse_points(
    cx: float, cy: float, rx: float, ry: float, deg: float, steps: int = 48
) -> List[float]:
    rad = math.radians(deg)
    ca, sa = math.cos(rad), math.sin(rad)
    pts: List[float] = []
    for i in range(steps + 1):
        t = (2.0 * math.pi * i) / steps
        ex = rx * math.cos(t)
        ey = ry * math.sin(t)
        pts.append(cx + ex * ca - ey * sa)
        pts.append(cy + ex * sa + ey * ca)
    return pts


class QuotaBarWidget(tk.Frame):
    """Backward-compatible compact linear bar widget."""

    def __init__(self, parent: tk.Widget, title: str, bar_height: int = 10) -> None:
        super().__init__(
            parent, bg=BG_CARD, highlightthickness=1, highlightbackground=BORDER_SUBTLE, padx=10, pady=6
        )
        top = tk.Frame(self, bg=BG_CARD)
        top.pack(fill=tk.X)
        self.lbl_title = tk.Label(
            top, text=title, bg=BG_CARD, fg=FG_TEXT, font=("Microsoft YaHei UI", 9, "bold")
        )
        self.lbl_title.pack(side=tk.LEFT)
        self.lbl_pct = tk.Label(
            top, text="--.---%", bg=BG_CARD, fg=ACCENT_CYAN, font=("Consolas", 10, "bold")
        )
        self.lbl_pct.pack(side=tk.RIGHT)
        self.canvas = tk.Canvas(self, height=bar_height, bg=BG_VOID, highlightthickness=0)
        self.canvas.pack(fill=tk.X, pady=(4, 3))
        self.lbl_sub = tk.Label(
            self, text="等待采样...", bg=BG_CARD, fg=FG_MUTED, font=("Microsoft YaHei UI", 8)
        )
        self.lbl_sub.pack(anchor="w")
        self._fraction = 0.0
        self.canvas.bind("<Configure>", lambda _e: self._redraw())

    def update_bucket(self, fraction: float, reset_countdown: str) -> None:
        try:
            val = float(fraction)
        except (TypeError, ValueError):
            val = math.nan
        if not math.isfinite(val):
            self._fraction = 0.0
            self.lbl_pct.configure(text="--.---%", fg=FG_MUTED)
            self.lbl_sub.configure(text=f"重置倒计时: {reset_countdown}")
            self._redraw()
            return
        self._fraction = max(0.0, min(1.0, val))
        pct = self._fraction * 100.0
        fg, _ = _quota_semantic_colors(pct)
        self.lbl_pct.configure(text=f"{pct:7.3f}%", fg=fg)
        self.lbl_sub.configure(text=f"重置倒计时: {reset_countdown}")
        self._redraw()

    def _redraw(self) -> None:
        self.canvas.delete("all")
        w = max(10, self.canvas.winfo_width())
        h = max(6, self.canvas.winfo_height())
        self.canvas.create_rectangle(0, 0, w, h, fill=BG_VOID, outline="")
        fill_w = int(round(w * self._fraction))
        if fill_w > 0:
            fg, _ = _quota_semantic_colors(self._fraction * 100.0)
            self.canvas.create_rectangle(0, 0, fill_w, h, fill=fg, outline="")


class AstrolabeDialWidget(tk.Frame):
    """
    Orion Astrolabe & Chronograph Dial (猎户座双星轨精密机械计时仪)
    - Outer Chronograph Bezel (60 precision radial ticks)
    - Outer Glowing Arc: Primary metric (e.g. 5h Rolling Quota Pool)
    - Golden Star Needle: Remaining Window Time reference (instant Surplus vs Overburn visual!)
    - Inner Starlight Ring: Secondary metric (e.g. Weekly Quota Ceiling Pool)
    - Center Precision Digital Readout + Bottom Pace Verdict Pill
    """

    def __init__(
        self,
        parent: tk.Widget,
        title: str,
        subtitle_tag: str = "ORION CHRONOGRAPH",
        primary_hue: str = "cyan",
        dial_px: int = 188,
        simplified_digits: bool = False,
        dpi_scale: float = 1.0,
    ) -> None:
        super().__init__(
            parent,
            bg=BG_CARD,
            highlightthickness=1,
            highlightbackground=BORDER_SUBTLE,
            padx=int(round(14 * dpi_scale)),
            pady=int(round(10 * dpi_scale)),
        )
        self._title = title
        self._subtitle_tag = subtitle_tag
        self._primary_hue = primary_hue
        self._simplified = simplified_digits
        self._dpi = dpi_scale

        # Dial state
        self._primary_frac: float = math.nan
        self._secondary_frac: float = math.nan
        self._time_rem_frac: Optional[float] = None
        self._center_top: str = subtitle_tag
        self._center_main: str = "--.-%" if simplified_digits else "--.---%"
        self._center_sub: str = "等待本地采样"
        self._footer_left: str = "重置倒计时: --"
        self._footer_right: str = "✦ 待机探活中"
        self._footer_right_fg: str = FG_MUTED
        self._custom_color: Optional[Tuple[str, str]] = None

        # Top header bar inside card
        hdr = tk.Frame(self, bg=BG_CARD)
        hdr.pack(fill=tk.X, pady=(0, 2))
        self.lbl_title = tk.Label(
            hdr,
            text=title,
            bg=BG_CARD,
            fg=FG_TEXT,
            font=("Microsoft YaHei UI", 10 if not simplified_digits else 11, "bold"),
        )
        self.lbl_title.pack(side=tk.LEFT)
        self.lbl_badge = tk.Label(
            hdr,
            text=self._footer_right,
            bg=BG_CARD_ALT,
            fg=FG_MUTED,
            padx=8,
            pady=2,
            font=("Microsoft YaHei UI", 8, "bold"),
        )
        self.lbl_badge.pack(side=tk.RIGHT)

        self.canvas = tk.Canvas(
            self,
            width=dial_px,
            height=dial_px,
            bg=BG_CARD,
            highlightthickness=0,
        )
        self.canvas.pack(fill=tk.BOTH, expand=True, pady=2)

        ftr = tk.Frame(self, bg=BG_CARD)
        ftr.pack(fill=tk.X, pady=(2, 0))
        self.lbl_reset = tk.Label(
            ftr,
            text=self._footer_left,
            bg=BG_CARD,
            fg=FG_MUTED,
            font=("Microsoft YaHei UI", 9 if simplified_digits else 8),
        )
        self.lbl_reset.pack(side=tk.LEFT)
        self.lbl_weekly_meta = tk.Label(
            ftr,
            text="",
            bg=BG_CARD,
            fg=FG_DIM,
            font=("Consolas", 8),
        )
        self.lbl_weekly_meta.pack(side=tk.RIGHT)

        self.canvas.bind("<Configure>", lambda _e: self._redraw())

    def set_quota_pair(
        self,
        frac_5h: float,
        reset_5h_countdown: str,
        reset_5h_iso: str,
        frac_weekly: float,
        reset_weekly_countdown: str,
        offline_reason: str = "等待采样",
    ) -> None:
        self._custom_color = None
        f5_ok = isinstance(frac_5h, (int, float)) and math.isfinite(frac_5h)
        fw_ok = isinstance(frac_weekly, (int, float)) and math.isfinite(frac_weekly)

        if not f5_ok and not fw_ok:
            self._primary_frac = math.nan
            self._secondary_frac = math.nan
            self._time_rem_frac = None
            self._center_top = self._subtitle_tag
            self._center_main = "--.-%" if self._simplified else "--.---%"
            self._center_sub = offline_reason
            self.lbl_reset.configure(text=f"状态: {offline_reason}", fg=FG_MUTED)
            self.lbl_weekly_meta.configure(text="")
            self.lbl_badge.configure(text="⚪ 未就绪", fg=FG_MUTED, bg=BG_CARD_ALT)
            self.configure(highlightbackground=BORDER_SUBTLE)
            self._redraw()
            return

        p_frac = max(0.0, min(1.0, float(frac_5h))) if f5_ok else max(0.0, min(1.0, float(frac_weekly)))
        s_frac = max(0.0, min(1.0, float(frac_weekly))) if (f5_ok and fw_ok) else math.nan
        self._primary_frac = p_frac
        self._secondary_frac = s_frac
        self._time_rem_frac = (
            _compute_remaining_window_fraction(reset_5h_iso, 18000.0) if f5_ok else None
        )

        pct5 = p_frac * 100.0
        fg_main, _ = _quota_semantic_colors(pct5, self._primary_hue)
        self._center_top = "5H 滚动池剩余" if f5_ok else "周额度池剩余"
        self._center_main = f"{pct5:.1f}%" if self._simplified else f"{pct5:7.3f}%".strip()

        if math.isfinite(s_frac):
            self._center_sub = (
                f"周池剩余 {s_frac * 100.0:.1f}%"
                if self._simplified
                else f"WEEKLY {s_frac * 100.0:6.2f}%"
            )
            self.lbl_weekly_meta.configure(
                text="" if self._simplified else f"周重置: {reset_weekly_countdown}",
                fg=FG_MUTED,
            )
        else:
            self._center_sub = "单通道直读"
            self.lbl_weekly_meta.configure(text="")

        cd_txt = reset_5h_countdown if f5_ok else reset_weekly_countdown
        self.lbl_reset.configure(
            text=f"距离满血重置: {cd_txt}" if self._simplified else f"5h 重置: {cd_txt}",
            fg=FG_SUB if self._simplified else FG_MUTED,
        )

        # Pace vs Time-Elapsed verdict
        if self._time_rem_frac is not None:
            delta_pct = (p_frac - self._time_rem_frac) * 100.0
            if delta_pct >= 4.0:
                badge_txt = "🟢 额度充裕" if self._simplified else f"✦ 盈余巡航 +{delta_pct:.1f}%"
                badge_fg, badge_bg = ACCENT_GREEN, ACCENT_GREEN_DIM
            elif delta_pct >= -5.0:
                badge_txt = "🔵 匀速消耗" if self._simplified else f"◎ 均衡配速 {delta_pct:+.1f}%"
                badge_fg, badge_bg = ACCENT_CYAN, ACCENT_CYAN_DIM
            else:
                badge_txt = "🟠 消耗偏快" if self._simplified else f"⚡ 超速透支 {delta_pct:+.1f}%"
                badge_fg, badge_bg = ACCENT_ROSE, ACCENT_CRIMSON_DIM
        else:
            if pct5 >= 50.0:
                badge_txt, badge_fg, badge_bg = ("🟢 水位健康", ACCENT_GREEN, ACCENT_GREEN_DIM)
            elif pct5 >= 20.0:
                badge_txt, badge_fg, badge_bg = ("🟡 注意余量", ACCENT_AMBER, ACCENT_AMBER_DIM)
            else:
                badge_txt, badge_fg, badge_bg = ("🔴 濒临见底", ACCENT_ROSE, ACCENT_CRIMSON_DIM)

        self.lbl_badge.configure(text=badge_txt, fg=badge_fg, bg=badge_bg)
        self.configure(highlightbackground=BORDER_CRIMSON if pct5 < 25.0 else BORDER_SUBTLE)
        _ = fg_main
        self._redraw()

    def set_custom_metric(
        self,
        ring_fraction: float,
        top_caption: str,
        big_value: str,
        sub_caption: str,
        badge_text: str,
        badge_fg: str,
        badge_bg: str,
        footer_text: str,
        ring_colors: Tuple[str, str],
    ) -> None:
        """Drive the Astrolabe dial for non-percentage hero metrics (Today's Spend or Risk Latency)."""
        self._primary_frac = max(0.0, min(1.0, ring_fraction)) if math.isfinite(ring_fraction) else math.nan
        self._secondary_frac = math.nan
        self._time_rem_frac = None
        self._custom_color = ring_colors
        self._center_top = top_caption
        self._center_main = big_value
        self._center_sub = sub_caption
        self.lbl_badge.configure(text=badge_text, fg=badge_fg, bg=badge_bg)
        self.lbl_reset.configure(text=footer_text, fg=FG_SUB)
        self.lbl_weekly_meta.configure(text="")
        self._redraw()

    def _redraw(self) -> None:
        c = self.canvas
        c.delete("all")
        w = max(80, c.winfo_width())
        h = max(80, c.winfo_height())
        cx, cy = w * 0.5, h * 0.5
        radius = min(w, h) * 0.43
        if radius < 36:
            return

        # 1. Background Orion Orbital Ellipses (echoing assets/logo.svg)
        orb1 = _rotated_ellipse_points(cx, cy, radius * 1.07, radius * 0.44, -26.0, steps=44)
        orb2 = _rotated_ellipse_points(cx, cy, radius * 1.07, radius * 0.44, 30.0, steps=44)
        c.create_line(*orb1, fill="#241326", width=1, dash=(4, 5))
        c.create_line(*orb2, fill="#122136", width=1, dash=(3, 6))

        # Inner dark astrolabe well
        r_well = radius * 0.96
        c.create_oval(
            cx - r_well,
            cy - r_well,
            cx + r_well,
            cy + r_well,
            fill=BG_DIAL_WELL,
            outline="#221933",
            width=1,
        )

        # 2. 60-Tick Chronograph Watch Bezel
        r_tick_outer = radius * 0.95
        p_ok = math.isfinite(self._primary_frac)
        active_color, halo_color = (
            self._custom_color
            if self._custom_color is not None
            else _quota_semantic_colors(self._primary_frac * 100.0 if p_ok else math.nan, self._primary_hue)
        )

        for i in range(60):
            frac_i = i / 60.0
            ang = -math.pi * 0.5 + 2.0 * math.pi * frac_i
            ca, sa = math.cos(ang), math.sin(ang)
            major = (i % 5) == 0
            r_in = radius * (0.875 if major else 0.91)
            lit = p_ok and frac_i <= self._primary_frac
            if lit:
                t_col = active_color if major else "#475569"
            else:
                t_col = "#362a4d" if major else "#1e172b"
            c.create_line(
                cx + r_in * ca,
                cy + r_in * sa,
                cx + r_tick_outer * ca,
                cy + r_tick_outer * sa,
                fill=t_col,
                width=1.6 if major else 1.0,
            )

        # 3. Outer Primary Ring (5h Quota Pool / Hero Metric)
        r_outer = radius * 0.80
        ring_w = max(6, int(round(radius * 0.10)))
        c.create_oval(
            cx - r_outer,
            cy - r_outer,
            cx + r_outer,
            cy + r_outer,
            outline="#1c152b",
            width=ring_w,
        )

        if p_ok and self._primary_frac > 0.0005:
            ext = -min(359.85, self._primary_frac * 360.0)
            # Soft outer glow halo
            c.create_arc(
                cx - r_outer,
                cy - r_outer,
                cx + r_outer,
                cy + r_outer,
                start=90.0,
                extent=ext,
                style=tk.ARC,
                outline=halo_color,
                width=ring_w + max(4, int(round(4 * self._dpi))),
            )
            # Crisp core arc
            c.create_arc(
                cx - r_outer,
                cy - r_outer,
                cx + r_outer,
                cy + r_outer,
                start=90.0,
                extent=ext,
                style=tk.ARC,
                outline=active_color,
                width=ring_w,
            )
            # Leading starlight bead
            tip_ang = -math.pi * 0.5 + 2.0 * math.pi * self._primary_frac
            tx = cx + r_outer * math.cos(tip_ang)
            ty = cy + r_outer * math.sin(tip_ang)
            br = max(3.5, ring_w * 0.58)
            c.create_oval(tx - br - 2, ty - br - 2, tx + br + 2, ty + br + 2, fill=active_color, outline="")
            c.create_oval(tx - br * 0.55, ty - br * 0.55, tx + br * 0.55, ty + br * 0.55, fill="#ffffff", outline="")

        # 4. Golden Time-Elapsed Reference Star Marker on Bezel
        if self._time_rem_frac is not None and math.isfinite(self._time_rem_frac):
            t_ang = -math.pi * 0.5 + 2.0 * math.pi * self._time_rem_frac
            ca, sa = math.cos(t_ang), math.sin(t_ang)
            # Radial hairline across outer ring
            c.create_line(
                cx + (r_outer - ring_w * 0.8) * ca,
                cy + (r_outer - ring_w * 0.8) * sa,
                cx + (radius * 0.96) * ca,
                cy + (radius * 0.96) * sa,
                fill=ACCENT_GOLD,
                width=1.8,
            )
            # Diamond star marker at bezel rim
            mx = cx + (radius * 0.97) * ca
            my = cy + (radius * 0.97) * sa
            ms = max(3.5, 4.0 * self._dpi)
            c.create_polygon(
                mx, my - ms, mx + ms, my, mx, my + ms, mx - ms, my,
                fill=ACCENT_GOLD,
                outline="#713f12",
            )

        # 5. Inner Concentric Ring (Weekly Ceiling Pool)
        r_inner = radius * 0.655
        inner_w = max(3, int(round(radius * 0.055)))
        if math.isfinite(self._secondary_frac):
            c.create_oval(
                cx - r_inner,
                cy - r_inner,
                cx + r_inner,
                cy + r_inner,
                outline="#181226",
                width=inner_w,
            )
            if self._secondary_frac > 0.0005:
                ext_in = -min(359.85, self._secondary_frac * 360.0)
                in_col = ACCENT_VIOLET if self._secondary_frac >= 0.25 else ACCENT_ROSE
                c.create_arc(
                    cx - r_inner,
                    cy - r_inner,
                    cx + r_inner,
                    cy + r_inner,
                    start=90.0,
                    extent=ext_in,
                    style=tk.ARC,
                    outline=in_col,
                    width=inner_w,
                )
        else:
            # Subtle decorative inner crosshair reticle
            c.create_oval(
                cx - r_inner,
                cy - r_inner,
                cx + r_inner,
                cy + r_inner,
                outline="#1d162c",
                width=1,
                dash=(2, 4),
            )

        # 6. Center Chronograph Typography (generous High-DPI inner clearance)
        top_fs = max(7, int(round(radius * 0.098 / self._dpi)))
        main_fs = max(12, int(round(radius * (0.235 if self._simplified else 0.19) / self._dpi)))
        sub_fs = max(7, int(round(radius * 0.095 / self._dpi)))

        c.create_text(
            cx,
            cy - radius * 0.23,
            text=self._center_top,
            fill=FG_MUTED,
            font=("Microsoft YaHei UI", top_fs),
        )
        c.create_text(
            cx,
            cy + radius * 0.005,
            text=self._center_main,
            fill=active_color if p_ok else FG_MUTED,
            font=("Consolas", main_fs, "bold"),
        )
        c.create_text(
            cx,
            cy + radius * 0.235,
            text=self._center_sub,
            fill=FG_SUB if self._simplified else ACCENT_VIOLET,
            font=("Consolas" if not self._simplified else "Microsoft YaHei UI", sub_fs),
        )


class OrionChartWidget(tk.Frame):
    """
    Adaptive Vector Statistical Chart (高颜值矢量统计图画布)
    - Supports `area` trajectory curves (24h Quota Watermark / Sidecar Latency Waveform)
      and `bar` column charts (14-Day Daily Spend / Token Trend)
    - Built-in mouse hover crosshair & exact value inspection pill
    """

    def __init__(
        self,
        parent: tk.Widget,
        title: str = "实时统计趋势",
        height_px: int = 180,
        dpi_scale: float = 1.0,
    ) -> None:
        super().__init__(
            parent,
            bg=BG_CARD,
            highlightthickness=1,
            highlightbackground=BORDER_SUBTLE,
            padx=int(round(14 * dpi_scale)),
            pady=int(round(10 * dpi_scale)),
        )
        self._dpi = dpi_scale
        self._mode: str = "area"
        self._points: List[Tuple[str, float]] = []
        self._secondary_points: List[Tuple[str, float]] = []
        self._unit_suffix: str = "%"
        self._unit_prefix: str = ""
        self._fixed_y_range: Optional[Tuple[float, float]] = (0.0, 100.0)
        self._line_color: str = ACCENT_CYAN
        self._fill_color: str = "#10263c"
        self._empty_hint: str = "等待采样生成趋势曲线..."
        self._hover_idx: Optional[int] = None

        hdr = tk.Frame(self, bg=BG_CARD)
        hdr.pack(fill=tk.X, pady=(0, 4))
        self.lbl_title = tk.Label(
            hdr, text=title, bg=BG_CARD, fg=FG_TEXT, font=("Microsoft YaHei UI", 10, "bold")
        )
        self.lbl_title.pack(side=tk.LEFT)
        self.lbl_hover = tk.Label(
            hdr,
            text="",
            bg=BG_CARD_ALT,
            fg=ACCENT_GOLD,
            padx=8,
            pady=1,
            font=("Consolas", 9, "bold"),
        )
        self.lbl_hover.pack(side=tk.RIGHT)

        self.canvas = tk.Canvas(self, height=height_px, bg=BG_DIAL_WELL, highlightthickness=0)
        self.canvas.pack(fill=tk.BOTH, expand=True)
        self.canvas.bind("<Configure>", lambda _e: self._redraw())
        self.canvas.bind("<Motion>", self._on_mouse_move)
        self.canvas.bind("<Leave>", self._on_mouse_leave)
        self._plot_xs: List[float] = []

    def set_series(
        self,
        title: str,
        points: Sequence[Tuple[str, float]],
        mode: str = "area",
        line_color: str = ACCENT_CYAN,
        fill_color: str = "#10263c",
        unit_prefix: str = "",
        unit_suffix: str = "%",
        fixed_y_range: Optional[Tuple[float, float]] = None,
        empty_hint: str = "暂无历史采样点",
        summary_badge: str = "",
    ) -> None:
        self.lbl_title.configure(text=title)
        clean: List[Tuple[str, float]] = []
        for lbl, val in points:
            try:
                fv = float(val)
            except (TypeError, ValueError):
                continue
            if math.isfinite(fv):
                clean.append((str(lbl), fv))
        self._points = clean
        self._mode = mode
        self._line_color = line_color
        self._fill_color = fill_color
        self._unit_prefix = unit_prefix
        self._unit_suffix = unit_suffix
        self._fixed_y_range = fixed_y_range
        self._empty_hint = empty_hint
        self._default_badge = summary_badge
        if self._hover_idx is None or self._hover_idx >= len(self._points):
            self._hover_idx = None
            self.lbl_hover.configure(text=summary_badge, fg=FG_SUB if summary_badge else FG_MUTED)
        self._redraw()

    def _format_val(self, v: float) -> str:
        if self._unit_prefix == "$":
            return f"${v:.2f}"
        if self._unit_suffix == " ms":
            return f"{v:.0f} ms"
        return f"{self._unit_prefix}{v:.2f}{self._unit_suffix}"

    def _on_mouse_move(self, event: tk.Event) -> None:  # type: ignore[type-arg]
        if not self._plot_xs or not self._points:
            return
        mx = float(event.x)
        best_i = min(range(len(self._plot_xs)), key=lambda i: abs(self._plot_xs[i] - mx))
        if best_i != self._hover_idx:
            self._hover_idx = best_i
            lbl, val = self._points[best_i]
            self.lbl_hover.configure(text=f"✦ {lbl}  ·  {self._format_val(val)}", fg=ACCENT_GOLD)
            self._redraw()

    def _on_mouse_leave(self, _event: tk.Event) -> None:  # type: ignore[type-arg]
        if self._hover_idx is not None:
            self._hover_idx = None
            badge = getattr(self, "_default_badge", "")
            self.lbl_hover.configure(text=badge, fg=FG_SUB if badge else FG_MUTED)
            self._redraw()

    def _redraw(self) -> None:
        c = self.canvas
        c.delete("all")
        self._plot_xs = []
        w = max(120, c.winfo_width())
        h = max(90, c.winfo_height())

        pad_l = int(round(48 * self._dpi))
        pad_r = int(round(18 * self._dpi))
        pad_t = int(round(16 * self._dpi))
        pad_b = int(round(26 * self._dpi))
        pw = max(40, w - pad_l - pad_r)
        ph = max(40, h - pad_t - pad_b)

        # Subtle border around plot frame
        c.create_rectangle(pad_l, pad_t, pad_l + pw, pad_t + ph, outline="#1e1730", width=1)

        if not self._points:
            c.create_text(
                w * 0.5,
                h * 0.5,
                text=self._empty_hint,
                fill=FG_MUTED,
                font=("Microsoft YaHei UI", 9),
            )
            return

        vals = [v for _, v in self._points]
        if self._fixed_y_range is not None:
            y_min, y_max = self._fixed_y_range
        else:
            lo_v, hi_v = min(vals), max(vals)
            y_min = 0.0 if lo_v >= 0 else lo_v * 1.1
            y_max = hi_v * 1.18 if hi_v > 0 else 10.0
            if y_max - y_min < 1e-6:
                y_max = y_min + 1.0

        def _y_of(v: float) -> float:
            clamped = max(y_min, min(y_max, v))
            ratio = (clamped - y_min) / (y_max - y_min)
            return pad_t + ph * (1.0 - ratio)

        # Horizontal reference gridlines (4 divisions)
        for step_i in range(5):
            frac = step_i / 4.0
            gy = pad_t + ph * (1.0 - frac)
            gv = y_min + (y_max - y_min) * frac
            if 0 < step_i < 4:
                c.create_line(pad_l, gy, pad_l + pw, gy, fill="#1b1429", dash=(3, 4))
            if step_i in (0, 2, 4):
                if self._unit_prefix == "$":
                    ylbl = f"${gv:.1f}" if y_max < 20 else f"${gv:.0f}"
                elif self._unit_suffix == " ms":
                    ylbl = f"{gv:.0f}"
                else:
                    ylbl = f"{gv:.0f}%"
                c.create_text(
                    pad_l - 6, gy, text=ylbl, anchor="e", fill=FG_DIM, font=("Consolas", 8)
                )

        n = len(self._points)
        base_y = pad_t + ph

        if self._mode == "bar":
            slot_w = pw / float(max(1, n))
            bar_w = max(6.0, min(slot_w * 0.62, 34.0 * self._dpi))
            for i, (lbl, val) in enumerate(self._points):
                cx_i = pad_l + slot_w * (i + 0.5)
                self._plot_xs.append(cx_i)
                top_y = _y_of(val)
                is_hov = i == self._hover_idx
                b_fill = ACCENT_ROSE if is_hov else self._fill_color
                b_cap = ACCENT_GOLD if is_hov else self._line_color
                c.create_rectangle(
                    cx_i - bar_w * 0.5,
                    top_y,
                    cx_i + bar_w * 0.5,
                    base_y,
                    fill=b_fill,
                    outline=b_cap,
                    width=1,
                )
                c.create_line(
                    cx_i - bar_w * 0.5,
                    top_y,
                    cx_i + bar_w * 0.5,
                    top_y,
                    fill=b_cap,
                    width=2.5,
                )
        else:
            if n == 1:
                # Single live anchor point -> draw clean live horizon line + glowing beacon
                only_lbl, only_val = self._points[0]
                py = _y_of(only_val)
                cx_mid = pad_l + pw * 0.5
                self._plot_xs.append(cx_mid)
                c.create_polygon(
                    pad_l, py, pad_l + pw, py, pad_l + pw, base_y, pad_l, base_y,
                    fill=self._fill_color, outline="",
                )
                c.create_line(pad_l, py, pad_l + pw, py, fill=self._line_color, width=2, dash=(6, 4))
                c.create_oval(cx_mid - 6, py - 6, cx_mid + 6, py + 6, fill=self._line_color, outline="")
                c.create_oval(cx_mid - 2.5, py - 2.5, cx_mid + 2.5, py + 2.5, fill="#ffffff", outline="")
                c.create_text(
                    cx_mid,
                    max(pad_t + 12, py - 14),
                    text=f"当前实时锚点 {self._format_val(only_val)} ({only_lbl}) · 持续运行将自动绘出轨迹",
                    fill=FG_SUB,
                    font=("Microsoft YaHei UI", 8),
                )
            else:
                poly_fill: List[float] = [float(pad_l), float(base_y)]
                line_pts: List[float] = []
                coords: List[Tuple[float, float]] = []
                for i, (_lbl, val) in enumerate(self._points):
                    px = pad_l + (pw * i) / float(n - 1)
                    py = _y_of(val)
                    self._plot_xs.append(px)
                    coords.append((px, py))
                    poly_fill.extend([px, py])
                    line_pts.extend([px, py])
                poly_fill.extend([float(pad_l + pw), float(base_y)])
                c.create_polygon(*poly_fill, fill=self._fill_color, outline="")
                c.create_line(*line_pts, fill=self._line_color, width=2.2, capstyle=tk.ROUND, joinstyle=tk.ROUND)

                # Draw subtle nodes (and highlight latest / hovered node)
                step_dot = max(1, n // 18)
                for i, (px, py) in enumerate(coords):
                    if i == self._hover_idx:
                        c.create_line(px, pad_t, px, base_y, fill=ACCENT_GOLD, dash=(2, 3))
                        c.create_oval(px - 5, py - 5, px + 5, py + 5, fill=ACCENT_GOLD, outline="#ffffff", width=1)
                    elif i == n - 1:
                        c.create_oval(px - 4.5, py - 4.5, px + 4.5, py + 4.5, fill=self._line_color, outline="#ffffff")
                    elif (i % step_dot) == 0:
                        c.create_oval(px - 2, py - 2, px + 2, py + 2, fill=self._line_color, outline="")

        # X-axis sparse labels (up to 6 evenly spaced labels)
        if self._plot_xs:
            max_lbls = min(6, len(self._points))
            idxs = (
                [0]
                if max_lbls == 1
                else sorted({int(round(k * (len(self._points) - 1) / (max_lbls - 1))) for k in range(max_lbls)})
            )
            for idx in idxs:
                c.create_text(
                    self._plot_xs[idx],
                    base_y + int(round(12 * self._dpi)),
                    text=self._points[idx][0],
                    fill=FG_DIM,
                    font=("Consolas", 8),
                )


class AcerolaOrionApp(tk.Tk):
    def __init__(
        self,
        auto_refresh_ms: int = 15000,
        start_minimized: bool = False,
        enable_tray: bool = True,
        initial_mode: Optional[str] = None,
    ) -> None:
        _enable_high_dpi()
        set_app_user_model_id(APP_USER_MODEL_ID)
        super().__init__()
        self.auto_refresh_ms = auto_refresh_ms
        self._refresh_busy = False
        self._action_busy = False
        self._refresh_gen = 0
        self._action_gen = 0
        self._pending_refresh = False
        self._offline_streak = 0
        self._closing = False
        self._hidden_to_tray = False
        self._tray_hint_shown = False
        self._tray: Optional[Win32TrayController] = None
        self._action_buttons: List[tk.Button] = []
        self._ledger = OrionLedger()
        self._last_report: Optional[OrionTelemetryReport] = None
        self._last_daily: List[Any] = []
        self._last_audit: Any = None
        self._last_quota_series: List[Dict[str, Any]] = []

        prefs = _load_ui_prefs()
        self._view_mode: str = (
            initial_mode if initial_mode in ("full", "zen") else str(prefs["view_mode"])
        )
        self._zen_metric: str = str(prefs["zen_metric"])
        self._active_full_tab: str = "hub"
        self._hub_chart_kind: str = "gemini"

        try:
            dpi = float(self.winfo_fpixels("1i"))
            self._dpi_scale = max(1.0, min(2.25, dpi / 96.0))
        except Exception:
            self._dpi_scale = 1.0

        self.title(_panel_window_title())
        apply_tk_window_icon(self, _get_icon_path())
        self.configure(bg=BG_ROOT)
        self._apply_window_geometry_for_mode(initial=True)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        _apply_win32_dark_chrome(self)

        self._configure_styles()
        self._build_header()
        self._build_body_containers()
        self._sync_view_mode_widgets()

        if enable_tray and sys.platform == "win32":
            self._tray = Win32TrayController(
                class_name=TRAY_CLASS_NAME,
                tooltip=f"{APP_CODENAME} v{APP_VERSION} · 单击唤醒星盘主控台",
                ico_path=_get_icon_path(),
                on_open=self.restore_from_tray,
                menu_builder=self._build_tray_menu,
                dispatch_ui=self._ui_call,
            )
            self._tray.start()

        self.after(50, self._pump_ui_queue)
        if start_minimized and self._tray is not None and self._tray.active:
            self.hide_to_tray(notify=False)
            self.after(300, lambda: self.refresh_all_async(silent=True))
        else:
            self.after(120, self.refresh_all_async)
            self._schedule_auto_refresh()

    def _px(self, base: int) -> int:
        return int(round(base * self._dpi_scale))

    def _apply_window_geometry_for_mode(self, initial: bool = False) -> None:
        sw = max(1024, self.winfo_screenwidth())
        sh = max(720, self.winfo_screenheight())
        if self._view_mode == "zen":
            win_w = min(self._px(720), sw - 80)
            win_h = min(self._px(650), sh - 90)
            min_w = min(self._px(580), sw - 120)
            min_h = min(self._px(540), sh - 120)
        else:
            win_w = min(self._px(1160), sw - 80)
            win_h = min(self._px(840), sh - 80)
            min_w = min(self._px(960), sw - 120)
            min_h = min(self._px(680), sh - 120)

        self.minsize(min_w, min_h)
        if initial:
            pos_x = max(20, (sw - win_w) // 2)
            pos_y = max(20, (sh - win_h) // 2 - 15)
            self.geometry(f"{win_w}x{win_h}+{pos_x}+{pos_y}")
        else:
            try:
                cur_x = max(20, min(sw - win_w - 20, self.winfo_x()))
                cur_y = max(20, min(sh - win_h - 40, self.winfo_y()))
                self.geometry(f"{win_w}x{win_h}+{cur_x}+{cur_y}")
            except Exception:
                self.geometry(f"{win_w}x{win_h}")

    def toggle_view_mode(self, target_mode: Optional[str] = None) -> None:
        new_mode = target_mode if target_mode in ("full", "zen") else (
            "zen" if self._view_mode == "full" else "full"
        )
        if new_mode == self._view_mode:
            return
        self._view_mode = new_mode
        _save_ui_prefs(self._view_mode, self._zen_metric)
        self._apply_window_geometry_for_mode(initial=False)
        self._sync_view_mode_widgets()
        self._refresh_active_charts_and_zen()

    def select_zen_metric(self, metric_key: str) -> None:
        if metric_key not in ("gemini", "claude", "spend", "latency"):
            return
        self._zen_metric = metric_key
        _save_ui_prefs(self._view_mode, self._zen_metric)
        self._update_zen_pill_styles()
        self._refresh_active_charts_and_zen()

    def select_full_tab(self, tab_key: str) -> None:
        if tab_key not in ("hub", "ledger", "diag"):
            return
        self._active_full_tab = tab_key
        for k, frame in self._full_tab_frames.items():
            if k == tab_key:
                frame.pack(fill=tk.BOTH, expand=True)
            else:
                frame.pack_forget()
        for k, btn in self._full_nav_buttons.items():
            active = k == tab_key
            btn.configure(
                bg=ACCENT_CRIMSON if active else BG_CARD,
                fg="#ffffff" if active else FG_MUTED,
            )

    def select_hub_chart(self, kind: str) -> None:
        if kind not in ("gemini", "claude", "spend"):
            return
        self._hub_chart_kind = kind
        for k, btn in self._hub_chart_btns.items():
            active = k == kind
            btn.configure(
                bg=ACCENT_CRIMSON_DIM if active else BG_CARD_ALT,
                fg=ACCENT_ROSE if active else FG_MUTED,
            )
        self._refresh_active_charts_and_zen()

    def _build_tray_menu(self) -> List[MenuSpec]:
        mode_lbl = (
            "✦ 切换至全盘星轨模式" if self._view_mode == "zen" else "◐ 切换至极简专注模式 (单指标+单图)"
        )
        return [
            (f"✦ 打开主控台 ({APP_CODENAME})", self.restore_from_tray),
            (mode_lbl, lambda: self._ui_call(self.toggle_view_mode)),
            None,
            ("🔍 立即采样配额水位快照", lambda: self._run_action("托盘 · 采样配额水位快照", self._sample_watermark_now)),
            ("🛡️ 启用并注册开机自启守卫", lambda: self._run_action("托盘 · 部署断头台内核守卫", install_guard_autostart)),
            ("⏹️ 停用并卸载断头台守卫", lambda: self._run_action("托盘 · 停用断头台内核守卫", uninstall_guard_autostart)),
            None,
            ("✕ 彻底退出主控台", self.exit_app),
        ]

    def hide_to_tray(self, notify: bool = True) -> None:
        if self.__dict__.get("_closing"):
            return
        tray = self.__dict__.get("_tray")
        if tray is None or not tray.active:
            self.exit_app()
            return
        self._hidden_to_tray = True
        try:
            self.withdraw()
        except Exception:
            pass
        if notify and not self.__dict__.get("_tray_hint_shown"):
            self._tray_hint_shown = True
            tray.notify_balloon(
                f"{APP_CODENAME} 已驻留托盘",
                "星盘主控台已转入右下角静默休眠（0% CPU）。单击托盘图标唤醒，右键可切换极简/全盘视图或彻底退出。",
            )

    def restore_from_tray(self) -> None:
        if self.__dict__.get("_closing"):
            return
        prefs = _load_ui_prefs()
        desired_mode = str(prefs.get("view_mode") or self._view_mode)
        if desired_mode in ("full", "zen") and desired_mode != self._view_mode:
            self.toggle_view_mode(desired_mode)
        was_hidden = bool(self.__dict__.get("_hidden_to_tray"))
        self._hidden_to_tray = False
        bring_tk_window_to_front(self)
        _apply_win32_dark_chrome(self)
        if was_hidden:
            self.refresh_all_async(silent=True)
            self._schedule_auto_refresh()

    def exit_app(self) -> None:
        self._closing = True
        self._hidden_to_tray = False
        tray = self.__dict__.get("_tray")
        if tray is not None:
            try:
                tray.stop()
            except Exception:
                pass
        try:
            self.destroy()
        except Exception:
            pass

    def _on_close(self) -> None:
        tray = self.__dict__.get("_tray")
        if tray is not None and tray.active:
            self.hide_to_tray(notify=True)
            return
        self.exit_app()

    def _ui_call(self, cb: Callable[[], None]) -> None:
        """Thread-safe UI marshalling: worker threads enqueue callbacks drained on the Tk main thread."""
        if self.__dict__.get("_closing"):
            return
        q = self.__dict__.get("_ui_queue")
        if q is None:
            q = queue.SimpleQueue()
            self.__dict__["_ui_queue"] = q
        q.put(cb)

    def _pump_ui_queue(self) -> None:
        """Main-thread pump: drain queued UI callbacks (each isolated) + generation watchdog for stuck busy flags."""
        if self.__dict__.get("_closing"):
            return
        q = self.__dict__.get("_ui_queue")
        if q is not None:
            for _ in range(256):
                try:
                    cb = q.get_nowait()
                except queue.Empty:
                    break
                try:
                    cb()
                except Exception as e:
                    try:
                        self._append_console(f"✗ 界面回调异常: {e}")
                    except Exception:
                        pass
        now_m = time.monotonic()
        if self.__dict__.get("_refresh_busy") and now_m - float(self.__dict__.get("_refresh_started", now_m)) > 120.0:
            self._refresh_gen = int(self.__dict__.get("_refresh_gen", 0)) + 1
            self._refresh_busy = False
        if self.__dict__.get("_action_busy") and now_m - float(self.__dict__.get("_action_started", now_m)) > 300.0:
            self._action_gen = int(self.__dict__.get("_action_gen", 0)) + 1
            self._action_busy = False
            try:
                self._set_action_buttons_enabled(True)
            except Exception:
                pass
        try:
            delay_ms = 400 if self.__dict__.get("_hidden_to_tray") else 50
            self.after(delay_ms, self._pump_ui_queue)
        except Exception:
            pass

    def _configure_styles(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except Exception:
            pass

        style.configure(
            "Orion.Treeview",
            background=BG_CARD,
            fieldbackground=BG_CARD,
            foreground=FG_TEXT,
            rowheight=self._px(28),
            borderwidth=0,
            font=("Consolas", 9),
        )
        style.configure(
            "Orion.Treeview.Heading",
            background=BG_CARD_ALT,
            foreground=ACCENT_ROSE,
            font=("Microsoft YaHei UI", 9, "bold"),
            relief="flat",
        )
        style.map("Orion.Treeview", background=[("selected", ACCENT_CRIMSON_DIM)], foreground=[("selected", "#ffffff")])

        style.configure(
            "Orion.Vertical.TScrollbar",
            background=BG_CARD_ALT,
            troughcolor=BG_ROOT,
            bordercolor=BORDER_SUBTLE,
            arrowcolor=FG_MUTED,
            relief="flat",
        )

    def _draw_mini_emblem(self, canvas: tk.Canvas) -> None:
        canvas.delete("all")
        s = self._px(34)
        cx, cy = s * 0.5, s * 0.5
        r = s * 0.44
        canvas.create_oval(cx - r, cy - r, cx + r, cy + r, fill="#1a0b16", outline=ACCENT_CRIMSON, width=1.5)
        e1 = _rotated_ellipse_points(cx, cy, r * 0.92, r * 0.38, -28.0, steps=24)
        e2 = _rotated_ellipse_points(cx, cy, r * 0.92, r * 0.38, 32.0, steps=24)
        canvas.create_line(*e1, fill=ACCENT_ROSE, width=1.2)
        canvas.create_line(*e2, fill=ACCENT_CYAN, width=1.0)
        canvas.create_polygon(
            cx, cy - r * 0.45, cx + r * 0.22, cy, cx, cy + r * 0.45, cx - r * 0.22, cy,
            fill=ACCENT_CRIMSON, outline="",
        )
        canvas.create_oval(cx - 2, cy - 2, cx + 2, cy + 2, fill=ACCENT_GOLD, outline="")

    def _build_header(self) -> None:
        hdr = tk.Frame(
            self,
            bg=BG_HEADER,
            padx=self._px(16),
            pady=self._px(9),
            highlightthickness=1,
            highlightbackground=BORDER_CRIMSON,
        )
        hdr.pack(fill=tk.X)

        left = tk.Frame(hdr, bg=BG_HEADER)
        left.pack(side=tk.LEFT)

        em_sz = self._px(34)
        emblem = tk.Canvas(left, width=em_sz, height=em_sz, bg=BG_HEADER, highlightthickness=0)
        emblem.pack(side=tk.LEFT, padx=(0, self._px(10)))
        self._draw_mini_emblem(emblem)

        titles = tk.Frame(left, bg=BG_HEADER)
        titles.pack(side=tk.LEFT)
        title_row = tk.Frame(titles, bg=BG_HEADER)
        title_row.pack(anchor="w")
        tk.Label(
            title_row,
            text=f"{APP_CODENAME}",
            bg=BG_HEADER,
            fg=FG_TEXT,
            font=("Microsoft YaHei UI", 12, "bold"),
        ).pack(side=tk.LEFT)
        tk.Label(
            title_row,
            text=f"v{APP_VERSION}",
            bg="#2b1021",
            fg=ACCENT_ROSE,
            padx=6,
            pady=1,
            font=("Consolas", 8, "bold"),
        ).pack(side=tk.LEFT, padx=(8, 0))

        self.lbl_header_sub = tk.Label(
            titles,
            text=f"{APP_TRINITY}  ·  猎户座星盘配额观测台 & 零轮询断头台",
            bg=BG_HEADER,
            fg=FG_MUTED,
            font=("Microsoft YaHei UI", 8),
        )
        self.lbl_header_sub.pack(anchor="w", pady=(1, 0))

        right = tk.Frame(hdr, bg=BG_HEADER)
        right.pack(side=tk.RIGHT)

        self.lbl_sync_time = tk.Label(
            right,
            text="正在连接 127.0.0.1 回环遥测...",
            bg=BG_HEADER,
            fg=FG_MUTED,
            font=("Consolas", 8),
        )
        self.lbl_sync_time.pack(anchor="e", pady=(0, 4))

        btn_row = tk.Frame(right, bg=BG_HEADER)
        btn_row.pack(anchor="e")

        self.btn_mode_toggle = tk.Button(
            btn_row,
            text="◐ 极简视图",
            command=self.toggle_view_mode,
            bg="#2b1224",
            fg=ACCENT_GOLD,
            activebackground=ACCENT_CRIMSON,
            activeforeground="#ffffff",
            relief="flat",
            padx=11,
            pady=3,
            font=("Microsoft YaHei UI", 9, "bold"),
            cursor="hand2",
        )
        self.btn_mode_toggle.pack(side=tk.LEFT, padx=(0, 6))

        self.btn_refresh = tk.Button(
            btn_row,
            text="🔃 刷新",
            command=lambda: self.refresh_all_async(silent=False),
            bg=BG_CARD_ALT,
            fg=FG_TEXT,
            activebackground=ACCENT_CRIMSON,
            activeforeground="#ffffff",
            relief="flat",
            padx=10,
            pady=3,
            font=("Microsoft YaHei UI", 9),
            cursor="hand2",
        )
        self.btn_refresh.pack(side=tk.LEFT, padx=(0, 6))

        self.btn_exit_app = tk.Button(
            btn_row,
            text="✕ 退出",
            command=self.exit_app,
            bg="#22121e",
            fg=FG_MUTED,
            activebackground=ACCENT_CRIMSON,
            activeforeground="#ffffff",
            relief="flat",
            padx=9,
            pady=3,
            font=("Microsoft YaHei UI", 9),
            cursor="hand2",
        )
        self.btn_exit_app.pack(side=tk.LEFT)

    def _build_body_containers(self) -> None:
        self.body_wrap = tk.Frame(self, bg=BG_ROOT)
        self.body_wrap.pack(fill=tk.BOTH, expand=True, padx=self._px(14), pady=self._px(10))

        self.zen_container = tk.Frame(self.body_wrap, bg=BG_ROOT)
        self.full_container = tk.Frame(self.body_wrap, bg=BG_ROOT)

        self._build_zen_mode_ui()
        self._build_full_mode_ui()

    def _sync_view_mode_widgets(self) -> None:
        if self._view_mode == "zen":
            self.full_container.pack_forget()
            self.zen_container.pack(fill=tk.BOTH, expand=True)
            self.btn_mode_toggle.configure(
                text="✦ 展开完整星盘",
                bg=ACCENT_CRIMSON_DIM,
                fg="#ffffff",
            )
            self.lbl_header_sub.configure(text="极简专注模式  ·  同屏仅聚焦单一核心指标与对应趋势图")
        else:
            self.zen_container.pack_forget()
            self.full_container.pack(fill=tk.BOTH, expand=True)
            self.btn_mode_toggle.configure(
                text="◐ 极简模式 (单指标+单图)",
                bg="#2b1224",
                fg=ACCENT_GOLD,
            )
            self.lbl_header_sub.configure(
                text=f"{APP_TRINITY}  ·  猎户座星盘配额观测台 & 零轮询断头台"
            )

    # ------------------------------------------------------------------
    # Mode 1: ZEN / SIMPLIFIED FOCUS MODE (1 Hero Metric + 1 Chart ONLY)
    # ------------------------------------------------------------------
    def _build_zen_mode_ui(self) -> None:
        pill_bar = tk.Frame(self.zen_container, bg=BG_ROOT)
        pill_bar.pack(fill=tk.X, pady=(0, self._px(8)))

        specs = [
            ("gemini", "✦ Gemini 配额"),
            ("claude", "◆ Claude / 3P 配额"),
            ("spend", "◈ 今日等效账单"),
            ("latency", "⚡ 响应顺畅度"),
        ]
        self._zen_pill_btns: Dict[str, tk.Button] = {}
        for col, (key, label) in enumerate(specs):
            pill_bar.columnconfigure(col, weight=1, uniform="zen_pill")
            b = tk.Button(
                pill_bar,
                text=label,
                command=lambda k=key: self.select_zen_metric(k),
                relief="flat",
                padx=10,
                pady=6,
                font=("Microsoft YaHei UI", 9, "bold"),
                cursor="hand2",
            )
            b.grid(row=0, column=col, sticky="ew", padx=3)
            self._zen_pill_btns[key] = b

        self._update_zen_pill_styles()

        # Single Hero Dial (Upper half)
        self.zen_dial = AstrolabeDialWidget(
            self.zen_container,
            title="Gemini 实时可用配额",
            subtitle_tag="5H 滚动池剩余",
            primary_hue="cyan",
            dial_px=self._px(225),
            simplified_digits=True,
            dpi_scale=self._dpi_scale,
        )
        self.zen_dial.pack(fill=tk.BOTH, expand=True, pady=(0, self._px(8)))

        # Single Statistical Chart (Lower half)
        self.zen_chart = OrionChartWidget(
            self.zen_container,
            title="近 24 小时 Gemini 配额水位变化曲线",
            height_px=self._px(195),
            dpi_scale=self._dpi_scale,
        )
        self.zen_chart.pack(fill=tk.BOTH, expand=True)

    def _update_zen_pill_styles(self) -> None:
        for k, b in self._zen_pill_btns.items():
            active = k == self._zen_metric
            b.configure(
                bg=ACCENT_CRIMSON if active else BG_CARD,
                fg="#ffffff" if active else FG_MUTED,
                activebackground=ACCENT_ROSE,
                activeforeground="#ffffff",
            )

    # ------------------------------------------------------------------
    # Mode 2: FULL ASTROLABE OBSERVATORY MODE
    # ------------------------------------------------------------------
    def _build_full_mode_ui(self) -> None:
        # Custom sleek segmented navigation bar (replaces clunky white-rimmed ttk.Notebook)
        nav_bar = tk.Frame(self.full_container, bg=BG_ROOT)
        nav_bar.pack(fill=tk.X, pady=(0, self._px(8)))

        nav_specs = [
            ("hub", "🔭 猎户座双星盘总览 (Astrolabe)"),
            ("ledger", "📒 Token 账单与 1.5x 验钞台 (Ledger)"),
            ("diag", "🩺 旁路风控示波与守护日志 (Diagnostics)"),
        ]
        self._full_nav_buttons: Dict[str, tk.Button] = {}
        for idx, (key, txt) in enumerate(nav_specs):
            b = tk.Button(
                nav_bar,
                text=txt,
                command=lambda k=key: self.select_full_tab(k),
                relief="flat",
                padx=self._px(14),
                pady=self._px(6),
                font=("Microsoft YaHei UI", 9, "bold"),
                cursor="hand2",
            )
            b.pack(side=tk.LEFT, padx=(0, 6))
            self._full_nav_buttons[key] = b

        self.tab_hub = tk.Frame(self.full_container, bg=BG_ROOT)
        self.tab_ledger = tk.Frame(self.full_container, bg=BG_ROOT)
        self.tab_diag = tk.Frame(self.full_container, bg=BG_ROOT)
        self._full_tab_frames = {
            "hub": self.tab_hub,
            "ledger": self.tab_ledger,
            "diag": self.tab_diag,
        }

        self._build_hub_tab()
        self._build_ledger_tab()
        self._build_diag_tab()
        self.select_full_tab("hub")

    def _make_instrument_plate(self, parent: tk.Widget, heading: str) -> Tuple[tk.Frame, tk.Label, tk.Label]:
        card = tk.Frame(
            parent,
            bg=BG_CARD,
            highlightthickness=1,
            highlightbackground=BORDER_SUBTLE,
            padx=self._px(12),
            pady=self._px(8),
        )
        tk.Label(card, text=heading, bg=BG_CARD, fg=FG_MUTED, font=("Microsoft YaHei UI", 8, "bold")).pack(anchor="w")
        main_lbl = tk.Label(
            card, text="--", bg=BG_CARD, fg=FG_TEXT, font=("Microsoft YaHei UI", 11, "bold"), pady=2
        )
        main_lbl.pack(anchor="w")
        sub_lbl = tk.Label(
            card,
            text="--",
            bg=BG_CARD,
            fg=FG_MUTED,
            font=("Consolas", 8),
            wraplength=self._px(300),
            anchor="w",
            justify="left",
        )
        sub_lbl.pack(anchor="w", fill=tk.X)
        card.bind(
            "<Configure>",
            lambda e, lbl=sub_lbl: lbl.configure(wraplength=max(160, int(e.width) - 24)),
        )
        return card, main_lbl, sub_lbl

    def _create_dark_text_box(
        self,
        parent: tk.Widget,
        fg_color: str = "#e2e8f0",
        font_pt: int = 8,
        height: int = 6,
        pad_xy: Tuple[int, int] = (6, 6),
    ) -> tk.Text:
        wrap = tk.Frame(parent, bg=BG_VOID)
        wrap.pack(fill=tk.BOTH, expand=True, padx=pad_xy[0], pady=(0, pad_xy[1]))
        txt = tk.Text(
            wrap,
            bg=BG_VOID,
            fg=fg_color,
            insertbackground="#ffffff",
            font=("Consolas", font_pt),
            relief="flat",
            height=height,
            wrap=tk.WORD,
            borderwidth=0,
            highlightthickness=0,
        )
        vsb = ttk.Scrollbar(wrap, orient="vertical", command=txt.yview, style="Orion.Vertical.TScrollbar")
        txt.configure(yscrollcommand=vsb.set)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        txt.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        return txt

    def _sample_watermark_now(self) -> Dict[str, Any]:
        rep = collect_telemetry(include_guard_check=False)
        recorded = self._ledger.record_quota_from_telemetry(rep, event_type="manual_probe", min_interval_sec=0.0)
        audit = self._ledger.compute_rate_audit()
        return {
            "ok": bool(rep.ls_running),
            "message": (
                f"已记录最新 7 位精度配额快照（总计 {audit.total_snapshots} 笔）"
                if recorded
                else "当前未获取到在线配额桶（客户端可能离线或未登录）"
            ),
        }

    def _build_hub_tab(self) -> None:
        # Row 1: Dual Orion Astrolabe Chronographs (Gemini & Claude/3P)
        dials_row = tk.Frame(self.tab_hub, bg=BG_ROOT)
        dials_row.pack(fill=tk.X, pady=(0, self._px(8)))
        dials_row.columnconfigure(0, weight=1, uniform="orion_dial")
        dials_row.columnconfigure(1, weight=1, uniform="orion_dial")

        self.dial_gemini = AstrolabeDialWidget(
            dials_row,
            title="✦ Gemini 星盘计时仪 (5h 外轨 · 周池内轨 · 金针时间基准)",
            subtitle_tag="GEMINI ASTROLABE",
            primary_hue="cyan",
            dial_px=self._px(208),
            simplified_digits=False,
            dpi_scale=self._dpi_scale,
        )
        self.dial_gemini.grid(row=0, column=0, sticky="nsew", padx=(0, self._px(5)))

        self.dial_claude = AstrolabeDialWidget(
            dials_row,
            title="◆ Claude / GPT 星盘计时仪 (3p-5h 外轨 · 3p-weekly 内轨)",
            subtitle_tag="CLAUDE / 3P ASTROLABE",
            primary_hue="rose",
            dial_px=self._px(208),
            simplified_digits=False,
            dpi_scale=self._dpi_scale,
        )
        self.dial_claude.grid(row=0, column=1, sticky="nsew", padx=(self._px(5), 0))

        # Row 2: 3 Sleek Instrument Plates (IPC + Tier/Audit + Guillotine Guard Switchboard)
        plates_row = tk.Frame(self.tab_hub, bg=BG_ROOT)
        plates_row.pack(fill=tk.X, pady=(0, self._px(8)))
        for col in range(3):
            plates_row.columnconfigure(col, weight=1, uniform="hub_plate")

        p1, self.lbl_ipc_main, self.lbl_ipc_sub = self._make_instrument_plate(
            plates_row, "🔭 本地语言服务回环拓扑 (127.0.0.1)"
        )
        p1.grid(row=0, column=0, sticky="nsew", padx=(0, self._px(5)))

        p2, self.lbl_tier_main, self.lbl_tier_sub = self._make_instrument_plate(
            plates_row, "⚖️ 订阅层级 · 1.5x 验钞与拥堵体征"
        )
        p2.grid(row=0, column=1, sticky="nsew", padx=self._px(5))

        # Plate 3: Guard Status + Integrated Tactile Controls
        p3 = tk.Frame(
            plates_row,
            bg=BG_CARD,
            highlightthickness=1,
            highlightbackground=BORDER_SUBTLE,
            padx=self._px(12),
            pady=self._px(8),
        )
        p3.grid(row=0, column=2, sticky="nsew", padx=(self._px(5), 0))
        tk.Label(
            p3,
            text="⚔️ Acerola 零轮询内核断头台守卫",
            bg=BG_CARD,
            fg=FG_MUTED,
            font=("Microsoft YaHei UI", 8, "bold"),
        ).pack(anchor="w")
        self.lbl_guard_main = tk.Label(
            p3, text="--", bg=BG_CARD, fg=FG_TEXT, font=("Microsoft YaHei UI", 11, "bold"), pady=2
        )
        self.lbl_guard_main.pack(anchor="w")
        self.lbl_guard_sub = tk.Label(
            p3, text="--", bg=BG_CARD, fg=FG_MUTED, font=("Consolas", 8), anchor="w", justify="left"
        )
        self.lbl_guard_sub.pack(anchor="w", fill=tk.X, pady=(0, 4))

        act_bar = tk.Frame(p3, bg=BG_CARD)
        act_bar.pack(fill=tk.X)
        btn_specs = [
            ("🛡️ 自启守卫", "#065f46", lambda: self._run_action("部署断头台内核守卫", install_guard_autostart)),
            ("⏹️ 停用", "#272136", lambda: self._run_action("停用断头台内核守卫", uninstall_guard_autostart)),
            ("📸 采样快照", ACCENT_CRIMSON, lambda: self._run_action("采样配额水位快照", self._sample_watermark_now)),
        ]
        self._action_buttons.clear()
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
            self._action_buttons.append(b)

        # Row 3: Live Telemetry Trajectory Chart (left 62%) + Compact Event Console (right 38%)
        bottom_row = tk.Frame(self.tab_hub, bg=BG_ROOT)
        bottom_row.pack(fill=tk.BOTH, expand=True)
        bottom_row.columnconfigure(0, weight=62)
        bottom_row.columnconfigure(1, weight=38)
        bottom_row.rowconfigure(0, weight=1)

        chart_col = tk.Frame(bottom_row, bg=BG_CARD, highlightthickness=1, highlightbackground=BORDER_SUBTLE)
        chart_col.grid(row=0, column=0, sticky="nsew", padx=(0, self._px(5)))

        c_switch = tk.Frame(chart_col, bg=BG_CARD, padx=10, pady=5)
        c_switch.pack(fill=tk.X)
        tk.Label(
            c_switch, text="📈 观测曲线切换:", bg=BG_CARD, fg=FG_MUTED, font=("Microsoft YaHei UI", 8)
        ).pack(side=tk.LEFT, padx=(0, 6))

        self._hub_chart_btns: Dict[str, tk.Button] = {}
        for key, lbl in (("gemini", "Gemini 水位轨迹"), ("claude", "Claude/3P 轨迹"), ("spend", "14日等效账单")):
            b = tk.Button(
                c_switch,
                text=lbl,
                command=lambda k=key: self.select_hub_chart(k),
                relief="flat",
                padx=8,
                pady=2,
                font=("Microsoft YaHei UI", 8, "bold"),
                cursor="hand2",
            )
            b.pack(side=tk.LEFT, padx=2)
            self._hub_chart_btns[key] = b

        self.hub_chart = OrionChartWidget(
            chart_col,
            title="近 24 小时 Gemini 配额水位轨迹",
            height_px=self._px(145),
            dpi_scale=self._dpi_scale,
        )
        self.hub_chart.configure(highlightthickness=0, padx=10, pady=4)
        self.hub_chart.pack(fill=tk.BOTH, expand=True)
        self.select_hub_chart("gemini")

        log_box = tk.Frame(bottom_row, bg=BG_CARD, highlightthickness=1, highlightbackground=BORDER_SUBTLE)
        log_box.grid(row=0, column=1, sticky="nsew", padx=(self._px(5), 0))
        tk.Label(
            log_box,
            text="✦ 实时遥测事件流 (Event Stream)",
            bg=BG_CARD,
            fg=FG_MUTED,
            padx=10,
            pady=5,
            font=("Microsoft YaHei UI", 8, "bold"),
        ).pack(anchor="w")
        self.txt_console = self._create_dark_text_box(log_box, fg_color="#e2e8f0", font_pt=8, height=6)
        self._append_console("✦ 猎户座星盘观测台已就绪。正在读取 127.0.0.1 状态与本地账本...")

    def _create_scrolled_treeview(
        self,
        parent: tk.Widget,
        columns: Sequence[str],
        headers: Sequence[Tuple[str, str, int, str]],
        height: int,
        expand: bool = False,
    ) -> ttk.Treeview:
        wrap = tk.Frame(parent, bg=BG_ROOT, highlightthickness=1, highlightbackground=BORDER_SUBTLE)
        wrap.pack(fill=tk.BOTH if expand else tk.X, expand=expand, pady=(2, 8))
        tree = ttk.Treeview(wrap, columns=columns, show="headings", height=height, style="Orion.Treeview")
        vsb = ttk.Scrollbar(wrap, orient="vertical", command=tree.yview, style="Orion.Vertical.TScrollbar")
        tree.configure(yscrollcommand=vsb.set)
        for cid, text, base_w, anchor in headers:
            tree.heading(cid, text=text)
            tree.column(cid, width=self._px(base_w), minwidth=self._px(50), anchor=anchor, stretch=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        return tree

    def _build_ledger_tab(self) -> None:
        self.lbl_auditor_banner = tk.Label(
            self.tab_ledger,
            text="正在汇总本地会话 Token 账本与扣费斜率...",
            bg=BG_CARD_ALT,
            fg=ACCENT_CYAN,
            font=("Microsoft YaHei UI", 9, "bold"),
            anchor="w",
            justify="left",
            padx=12,
            pady=8,
        )
        self.lbl_auditor_banner.pack(fill=tk.X, pady=(2, 6))
        self.tab_ledger.bind(
            "<Configure>",
            lambda e: self.lbl_auditor_banner.configure(wraplength=max(400, int(e.width) - 36)),
        )

        tk.Label(
            self.tab_ledger,
            text="📅 每日 Token 消耗与等效 API 美元明细 (近 14 天 · 严格本地只读统计)",
            bg=BG_ROOT,
            fg=ACCENT_ROSE,
            font=("Microsoft YaHei UI", 9, "bold"),
        ).pack(anchor="w")

        cols_daily = ("date", "sessions", "steps", "input", "cache", "output", "u1x", "uintro", "uagy")
        headers_daily = [
            ("date", "日期", 95, "center"),
            ("sessions", "会话数", 65, "e"),
            ("steps", "推理步数", 70, "e"),
            ("input", "新鲜 Input", 110, "e"),
            ("cache", "缓存读取 Cache", 125, "e"),
            ("output", "输出 Output", 105, "e"),
            ("u1x", "1.0x 官方原价", 105, "e"),
            ("uintro", "0.5x 承诺首发价", 110, "e"),
            ("uagy", "1.5x AGY暗扣估", 115, "e"),
        ]
        self.tree_daily = self._create_scrolled_treeview(
            self.tab_ledger, cols_daily, headers_daily, height=5, expand=False
        )

        tk.Label(
            self.tab_ledger,
            text="🧠 近 14 天模型家族拆分与费率倍率审计",
            bg=BG_ROOT,
            fg=ACCENT_ROSE,
            font=("Microsoft YaHei UI", 9, "bold"),
        ).pack(anchor="w")

        cols_mod = ("model", "pool", "steps", "incache", "output", "u1x", "uagy", "note")
        headers_mod = [
            ("model", "模型家族", 190, "w"),
            ("pool", "配额桶", 70, "e"),
            ("steps", "步数", 65, "e"),
            ("incache", "Input + Cache", 130, "e"),
            ("output", "Output", 100, "e"),
            ("u1x", "1.0x 原价", 95, "e"),
            ("uagy", "AGY 实际权重估", 110, "e"),
            ("note", "费率审计状态", 180, "w"),
        ]
        self.tree_models = self._create_scrolled_treeview(
            self.tab_ledger, cols_mod, headers_mod, height=4, expand=False
        )

        tk.Label(
            self.tab_ledger,
            text="💬 最近活跃会话账单明细 (Top 15)",
            bg=BG_ROOT,
            fg=ACCENT_ROSE,
            font=("Microsoft YaHei UI", 9, "bold"),
        ).pack(anchor="w")

        cols_sess = ("date", "title", "model", "steps", "output", "u1x", "uagy")
        headers_sess = [
            ("date", "日期", 90, "e"),
            ("title", "会话标题", 320, "w"),
            ("model", "主模型", 180, "w"),
            ("steps", "步数", 60, "e"),
            ("output", "Output Tokens", 110, "e"),
            ("u1x", "1.0x 原价", 90, "e"),
            ("uagy", "AGY 权重估", 95, "e"),
        ]
        self.tree_sessions = self._create_scrolled_treeview(
            self.tab_ledger, cols_sess, headers_sess, height=6, expand=True
        )

    def _build_diag_tab(self) -> None:
        self.lbl_diag_summary = tk.Label(
            self.tab_diag,
            text="旁路安全审查 (Sidecar Risk Monitor) 与限流拥堵统计加载中...",
            bg=BG_CARD_ALT,
            fg=FG_TEXT,
            font=("Consolas", 9),
            anchor="w",
            justify="left",
            padx=12,
            pady=8,
        )
        self.lbl_diag_summary.pack(fill=tk.X, pady=(2, 6))

        self.diag_wave_chart = OrionChartWidget(
            self.tab_diag,
            title="⚡ 旁路风控模型 (sir_flash_lite) 最近请求附加耗时示波器 (ms)",
            height_px=self._px(145),
            dpi_scale=self._dpi_scale,
        )
        self.diag_wave_chart.pack(fill=tk.X, pady=(0, 8))

        self.txt_diag = self._create_dark_text_box(self.tab_diag, fg_color="#cbd5e1", font_pt=9, height=12, pad_xy=(0, 0))

    def _append_console(self, line: str) -> None:
        if self._closing:
            return
        ts = datetime.now().strftime("%H:%M:%S")
        self.txt_console.insert(tk.END, f"[{ts}] {line}\n")
        self.txt_console.see(tk.END)

    def _set_action_buttons_enabled(self, enabled: bool) -> None:
        if self._closing:
            return
        st = tk.NORMAL if enabled else tk.DISABLED
        for b in self._action_buttons:
            b.configure(state=st)

    def _schedule_auto_refresh(self) -> None:
        if self.auto_refresh_ms > 0 and not self._closing and not self.__dict__.get("_hidden_to_tray"):
            self.after(self._next_auto_delay_ms(), self._on_auto_timer)

    def _next_auto_delay_ms(self) -> int:
        """15s while online; offline / logged-out backs off 30s -> 60s -> 120s to prevent idle polling."""
        base = int(self.auto_refresh_ms)
        streak = int(getattr(self, "_offline_streak", 0))
        if streak <= 0:
            return base
        return int(min(max(base, 120_000), base * (2 ** min(streak, 3))))

    def _on_auto_timer(self) -> None:
        if self._closing or self.__dict__.get("_hidden_to_tray"):
            return
        if not self._refresh_busy and not self._action_busy:
            self.refresh_all_async(silent=True)
        self._schedule_auto_refresh()

    def _run_action(self, label: str, fn: Callable[[], Any]) -> None:
        if self._action_busy or self._closing:
            return
        self._action_busy = True
        self._action_started = time.monotonic()
        my_action_gen = int(self.__dict__.get("_action_gen", 0)) + 1
        self._action_gen = my_action_gen
        self._set_action_buttons_enabled(False)
        self._append_console(f"▶ 正在执行 [{label}] ...")

        def worker() -> None:
            try:
                res = fn()
                msg = getattr(res, "message", "") if not isinstance(res, dict) else str(res.get("message") or res)
                ok = getattr(res, "ok", True) if not isinstance(res, dict) else bool(res.get("ok", True))
                prefix = "✓" if ok else "✗"
                self._ui_call(lambda m=f"{prefix} [{label}] {msg}": self._append_console(m))
            except Exception as e:
                self._ui_call(lambda err=str(e): self._append_console(f"✗ [{label}] 异常: {err}"))
            finally:
                def _finish_action() -> None:
                    if int(self.__dict__.get("_action_gen", 0)) != my_action_gen:
                        return
                    self._action_busy = False
                    self._set_action_buttons_enabled(True)
                    invalidate_guard_probe_cache()
                    self.refresh_all_async(silent=False)

                self._ui_call(_finish_action)

        threading.Thread(target=worker, daemon=True).start()

    def refresh_all_async(self, silent: bool = False) -> None:
        if self._closing:
            return
        if self._refresh_busy:
            if not silent:
                self._pending_refresh = True
            return
        self._refresh_busy = True
        self._refresh_started = time.monotonic()
        my_gen = int(self.__dict__.get("_refresh_gen", 0)) + 1
        self._refresh_gen = my_gen
        poll_state: Dict[str, bool] = {"offline": True}

        def worker() -> None:
            try:
                rep = collect_telemetry(include_guard_check=True)
                poll_state["offline"] = (not rep.ls_running) or (not rep.quota_buckets)
                self._ledger.record_quota_from_telemetry(rep, event_type="panel_poll", min_interval_sec=300.0)
                daily = self._ledger.get_daily_summary(days=14)
                models = self._ledger.get_model_breakdown(days=14)
                sessions = self._ledger.get_recent_sessions(limit=15)
                audit = self._ledger.compute_rate_audit()
                series_fn = getattr(self._ledger, "get_recent_quota_series", None)
                if callable(series_fn):
                    b_by_id = {b.bucket_id: b for b in rep.quota_buckets}
                    gw_rst = b_by_id["gemini-weekly"].reset_time if "gemini-weekly" in b_by_id else ""
                    pw_rst = b_by_id["3p-weekly"].reset_time if "3p-weekly" in b_by_id else ""
                    q_series = series_fn(
                        hours=24.0,
                        max_points=72,
                        active_gemini_wk_reset=gw_rst,
                        active_p3_wk_reset=pw_rst,
                    )
                else:
                    q_series = []
                guard_lines = read_shared_file_lines(get_guillotine_log_path(), max_tail_bytes=64 * 1024)[-30:]
                ls_tail = read_shared_file_lines(get_ls_log_path(), max_tail_bytes=128 * 1024)[-40:]

                def _apply_if_current() -> None:
                    if int(self.__dict__.get("_refresh_gen", 0)) != my_gen:
                        return
                    self.__dict__["_last_quota_series"] = q_series
                    self._apply_ui_data(rep, daily, models, sessions, audit, guard_lines, ls_tail, silent)

                self._ui_call(_apply_if_current)
            except Exception as e:
                if not silent:
                    def _log_if_current(err: str = str(e)) -> None:
                        if int(self.__dict__.get("_refresh_gen", 0)) != my_gen:
                            return
                        self._append_console(f"✗ 刷新异常: {err}")

                    self._ui_call(_log_if_current)
            finally:
                def _done() -> None:
                    if int(self.__dict__.get("_refresh_gen", 0)) != my_gen:
                        return
                    self._refresh_busy = False
                    if poll_state["offline"]:
                        self._offline_streak = min(int(getattr(self, "_offline_streak", 0)) + 1, 8)
                    else:
                        self._offline_streak = 0
                    if self._pending_refresh and not self._closing:
                        self._pending_refresh = False
                        self.refresh_all_async(silent=False)

                self._ui_call(_done)

        threading.Thread(target=worker, daemon=True).start()

    def _build_quota_points(self, col_key: str, live_bucket: Any) -> List[Tuple[str, float]]:
        pts: List[Tuple[str, float]] = []
        for row in self._last_quota_series:
            v = row.get(col_key)
            if isinstance(v, (int, float)) and math.isfinite(v):
                pts.append((str(row.get("label") or ""), float(v)))
        if live_bucket is not None and math.isfinite(getattr(live_bucket, "remaining_pct", math.nan)):
            now_lbl = datetime.now().strftime("%H:%M")
            live_v = float(live_bucket.remaining_pct)
            if not pts or abs(pts[-1][1] - live_v) > 0.001 or pts[-1][0] != now_lbl:
                pts.append((now_lbl, live_v))
        return pts

    def _build_daily_spend_points(self) -> List[Tuple[str, float]]:
        # self._last_daily is descending or chronological; sort ascending by date for left-to-right timeline
        rows = sorted(self._last_daily, key=lambda d: str(getattr(d, "local_date", "")))
        out: List[Tuple[str, float]] = []
        for d in rows[-14:]:
            dt_s = str(getattr(d, "local_date", ""))
            short_dt = dt_s[5:] if len(dt_s) >= 10 else dt_s
            val = float(getattr(d, "usd_agy_est", 0.0) or 0.0)
            out.append((short_dt, val))
        return out

    def _refresh_active_charts_and_zen(self) -> None:
        r = self._last_report
        if r is None:
            return
        bmap = {b.bucket_id: b for b in r.quota_buckets}
        missing_msg = "客户端未启动" if not r.ls_running else "暂无该通道数据"

        g5 = bmap.get("gemini-5h")
        gw = bmap.get("gemini-weekly")
        p5 = bmap.get("3p-5h")
        pw = bmap.get("3p-weekly")

        gem_pts = self._build_quota_points("gemini_5h", g5)
        cld_pts = self._build_quota_points("p3_5h", p5)
        spend_pts = self._build_daily_spend_points()
        sc = r.log_stats.sidecar
        lat_pts = [(f"#{i + 1}", float(v)) for i, v in enumerate(sc.recent_durations_ms[-36:])]

        # 1. Update Full Mode Hub Chart
        if self._hub_chart_kind == "gemini":
            cur_badge = f"最新 {g5.remaining_pct:.2f}%" if g5 and math.isfinite(g5.remaining_pct) else ""
            self.hub_chart.set_series(
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
        elif self._hub_chart_kind == "claude":
            cur_badge = f"最新 {p5.remaining_pct:.2f}%" if p5 and math.isfinite(p5.remaining_pct) else ""
            self.hub_chart.set_series(
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
            self.hub_chart.set_series(
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

        # 2. Update Diagnostics Waveform Chart
        self.diag_wave_chart.set_series(
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

        # 3. Update Zen / Simplified Mode (1 Hero Metric + 1 Chart ONLY)
        zm = self._zen_metric
        if zm == "gemini":
            self.zen_dial.lbl_title.configure(text="✦ Gemini 可用配额水位")
            self.zen_dial.set_quota_pair(
                g5.remaining_fraction if g5 else math.nan,
                g5.reset_countdown if g5 else "--",
                g5.reset_time if g5 else "",
                gw.remaining_fraction if gw else math.nan,
                gw.reset_countdown if gw else "--",
                offline_reason=missing_msg,
            )
            self.zen_chart.set_series(
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
            self.zen_dial.lbl_title.configure(text="◆ Claude / GPT 第三方池配额水位")
            self.zen_dial.set_quota_pair(
                p5.remaining_fraction if p5 else math.nan,
                p5.reset_countdown if p5 else "--",
                p5.reset_time if p5 else "",
                pw.remaining_fraction if pw else math.nan,
                pw.reset_countdown if pw else "--",
                offline_reason=missing_msg,
            )
            self.zen_chart.set_series(
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
            today_row = next((d for d in self._last_daily if getattr(d, "local_date", "") == today_str), None)
            today_usd = float(getattr(today_row, "usd_agy_est", 0.0) or 0.0) if today_row else 0.0
            today_steps = int(getattr(today_row, "steps", 0) or 0) if today_row else 0
            peak_usd = max([v for _, v in spend_pts] + [10.0])
            ring_f = min(1.0, today_usd / peak_usd) if peak_usd > 0 else 0.0
            self.zen_dial.lbl_title.configure(text="◈ 今日累计等效消耗")
            self.zen_dial.set_custom_metric(
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
            self.zen_chart.set_series(
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
        else:  # latency
            avg_ms = float(sc.avg_ms or 0.0)
            # Smoothness score: 0ms -> 100% ring, >=2500ms -> 15% ring
            health_f = max(0.12, 1.0 - min(0.88, avg_ms / 2500.0)) if sc.eval_count > 0 else 1.0
            if sc.timeout_count > 0 or avg_ms >= 1200:
                b_txt, b_fg, b_bg, r_cols = ("🟠 审查偏慢", ACCENT_AMBER, ACCENT_AMBER_DIM, (ACCENT_AMBER, ACCENT_AMBER_DIM))
            else:
                b_txt, b_fg, b_bg, r_cols = ("🟢 响应顺畅", ACCENT_GREEN, ACCENT_GREEN_DIM, (ACCENT_GREEN, ACCENT_GREEN_DIM))
            self.zen_dial.lbl_title.configure(text="⚡ 客户端响应与风控顺畅度")
            self.zen_dial.set_custom_metric(
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
            self.zen_chart.set_series(
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

    def _apply_ui_data(
        self,
        r: OrionTelemetryReport,
        daily: List[Any],
        models: List[Any],
        sessions: List[Any],
        audit: Any,
        guard_lines: List[str],
        ls_tail: List[str],
        silent: bool,
    ) -> None:
        if self._closing:
            return
        self._last_report = r
        self._last_daily = list(daily)
        self._last_audit = audit
        self.lbl_sync_time.configure(text=f"同步: {r.captured_at}  |  LS PID={r.ls_pid or '离线'}")

        # Plate 1: Local IPC Topology
        if not r.ls_running:
            self.lbl_ipc_main.configure(text="⚪ 离线 (客户端未启动)", fg=FG_MUTED)
            self.lbl_ipc_sub.configure(text="启动 Antigravity 后自动锁定回环端口")
        else:
            ver_txt = f"v{r.ide_version}" if r.ide_version else "在线"
            self.lbl_ipc_main.configure(text=f"🟢 在线 ({ver_txt})", fg=ACCENT_GREEN)
            self.lbl_ipc_sub.configure(
                text=f"127.0.0.1:{r.http_port} | 主进程 PID={r.main_pids}\n网络策略: {r.proxy_mode}"
            )

        # Plate 2: Tier, 1.5x Audit Quick Look & Congestion
        today_str = datetime.now().strftime("%Y-%m-%d")
        today_row = next((d for d in daily if getattr(d, "local_date", "") == today_str), None)
        phantom_cnt = len(getattr(audit, "phantom_drains", []) or [])
        phantom_tag = "CLEAN 0暗扣" if phantom_cnt == 0 else f"⚠️幽灵掉点x{phantom_cnt}"
        if r.ls_running:
            tier_txt = r.paid_tier_name or r.paid_tier_id or r.user_tier_id or "未知 Tier"
            self.lbl_tier_main.configure(text=f"👑 {tier_txt}", fg=ACCENT_CYAN)
            sc = r.log_stats.sidecar
            td_usd = f"${today_row.usd_agy_est:.2f}" if today_row else "$0.00"
            self.lbl_tier_sub.configure(
                text=f"今日1.5x估: {td_usd} ({phantom_tag}) | 429限流: {r.log_stats.http_429_503_count}次\n"
                f"旁路审查均值: {sc.avg_ms:.0f} ms (10s超时 {sc.timeout_count} 次)"
            )
        else:
            td_usd = f"${today_row.usd_agy_est:.2f}" if today_row else "$0.00"
            self.lbl_tier_main.configure(text=f"📒 今日账本 {td_usd}", fg=ACCENT_GOLD)
            self.lbl_tier_sub.configure(text=f"对账体征: {phantom_tag} | 累计快照 {getattr(audit, 'total_snapshots', 0)} 笔")

        # Plate 3: Guillotine Process Guard
        g = r.guard
        if g.mutex_active:
            self.lbl_guard_main.configure(text="🟢 武装就绪 · 0.00% CPU", fg=ACCENT_GREEN)
        else:
            self.lbl_guard_main.configure(text="⚪ 守卫未驻留", fg=ACCENT_AMBER)
        leg_warn = f" | ⚠️旧脚本:{g.legacy_pids}" if g.legacy_buggy_killer_active else ""
        auto_desc = "开机静默自启(不弹面板)" if g.scheduled_task_installed else "未开自启"
        self.lbl_guard_sub.configure(
            text=f"{auto_desc} · 防关窗压缩/污染记忆 | hub:{r.stale_hub_files}{leg_warn}"
        )

        # Dual Orion Astrolabe Chronographs (Full Mode)
        bmap = {b.bucket_id: b for b in r.quota_buckets}
        missing_msg = "客户端未运行" if not r.ls_running else "暂无配额数据"
        g5 = bmap.get("gemini-5h")
        gw = bmap.get("gemini-weekly")
        p5 = bmap.get("3p-5h")
        pw = bmap.get("3p-weekly")

        self.dial_gemini.set_quota_pair(
            g5.remaining_fraction if g5 else math.nan,
            g5.reset_countdown if g5 else "--",
            g5.reset_time if g5 else "",
            gw.remaining_fraction if gw else math.nan,
            gw.reset_countdown if gw else "--",
            offline_reason=missing_msg,
        )
        self.dial_claude.set_quota_pair(
            p5.remaining_fraction if p5 else math.nan,
            p5.reset_countdown if p5 else "--",
            p5.reset_time if p5 else "",
            pw.remaining_fraction if pw else math.nan,
            pw.reset_countdown if pw else "--",
            offline_reason=missing_msg,
        )

        # Update All Charts & Zen Mode View
        self._refresh_active_charts_and_zen()

        # Ledger Tab
        if today_row:
            t_summary = (
                f"📌 今日账单 ({today_str}): {today_row.sessions} 个会话 / {today_row.steps} 步  |  "
                f"Input={today_row.input_tokens:,}  Cache={today_row.cache_read_tokens:,}  Output={today_row.output_tokens:,}  |  "
                f"1.0x原价=${today_row.usd_1x:.2f}  (0.5x首发价=${today_row.usd_intro:.2f} / 1.5x暗扣估=${today_row.usd_agy_est:.2f})\n"
                f"🔍 验钞结论: {audit.verdict}"
            )
        else:
            t_summary = f"📌 今日暂无 Token 记录  |  🔍 验钞结论: {audit.verdict}"
        self.lbl_auditor_banner.configure(text=t_summary)

        self.tree_daily.delete(*self.tree_daily.get_children())
        for d in daily:
            self.tree_daily.insert(
                "",
                tk.END,
                values=(
                    d.local_date,
                    d.sessions,
                    d.steps,
                    f"{d.input_tokens:,}",
                    f"{d.cache_read_tokens:,}",
                    f"{d.output_tokens:,}",
                    f"${d.usd_1x:.2f}",
                    f"${d.usd_intro:.2f}",
                    f"${d.usd_agy_est:.2f}",
                ),
            )

        self.tree_models.delete(*self.tree_models.get_children())
        for m in models:
            self.tree_models.insert(
                "",
                tk.END,
                values=(
                    m.family_label,
                    m.quota_group,
                    m.steps,
                    f"{(m.input_tokens + m.cache_read_tokens):,}",
                    f"{m.output_tokens:,}",
                    f"${m.usd_1x:.2f}",
                    f"${m.usd_agy_est:.2f}",
                    m.multiplier_note,
                ),
            )

        self.tree_sessions.delete(*self.tree_sessions.get_children())
        for s in sessions:
            self.tree_sessions.insert(
                "",
                tk.END,
                values=(
                    s.local_date,
                    s.title,
                    s.primary_model,
                    s.steps,
                    f"{s.output_tokens:,}",
                    f"${s.usd_1x:.2f}",
                    f"${s.usd_agy_est:.2f}",
                ),
            )

        # Diagnostics Tab
        sc = r.log_stats.sidecar
        self.lbl_diag_summary.configure(
            text=(
                f"旁路安全审查 (sir_flash_lite / severe_internal_risks_agy): 累计 {sc.eval_count} 次 | "
                f"均值 {sc.avg_ms:.0f} ms | 峰值 {sc.max_ms:.0f} ms | 10s超时 {sc.timeout_count} 次\n"
                f"容量耗尽排队: {r.log_stats.capacity_exhausted_count} 次 | HTTP 429/503: {r.log_stats.http_429_503_count} 次 | "
                f"网络切换断流 (ERR_NETWORK_CHANGED): {r.log_stats.network_changed_count} 次 | 残留 hub 文件: {r.stale_hub_files} 个"
            )
        )
        self.txt_diag.delete("1.0", tk.END)
        self.txt_diag.insert(tk.END, "=== [Acerola-Guillotine 守护日志 (guillotine.log)] ===\n")
        self.txt_diag.insert(tk.END, ("\n".join(guard_lines) if guard_lines else "(暂无守护日志记录)") + "\n\n")
        self.txt_diag.insert(tk.END, "=== [language_server.log 最新尾部流 (无锁只读)] ===\n")
        self.txt_diag.insert(tk.END, "\n".join(ls_tail) + "\n")

        def _pct_txt(bid: str) -> str:
            b = bmap.get(bid)
            v = getattr(b, "remaining_pct", math.nan) if b is not None else math.nan
            return f"{v:.2f}%" if isinstance(v, (int, float)) and math.isfinite(v) else "--.--%"

        tray = self.__dict__.get("_tray")
        if tray is not None and tray.active:
            tray.update_tooltip(
                f"{APP_CODENAME} · 5h: {_pct_txt('gemini-5h')} | 周: {_pct_txt('gemini-weekly')}"
            )

        if not silent:
            self._append_console(
                f"✓ 星盘同步: Tier={r.paid_tier_id or r.user_tier_id or '-'} "
                f"Gemini-5h={_pct_txt('gemini-5h')} "
                f"Weekly={_pct_txt('gemini-weekly')}"
            )


def _terminate_existing_panel_instance() -> None:
    """Cleanly replace an already-running Public Astrolabe panel instance when --reload is requested."""
    if sys.platform != "win32":
        return
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
        user32.FindWindowW.restype = wintypes.HWND
        user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        hwnd = user32.FindWindowW(TRAY_CLASS_NAME, None) or user32.FindWindowW(None, _panel_window_title())
        if hwnd:
            pid = wintypes.DWORD(0)
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value:
                terminate_pid_win32(int(pid.value), allowed_exe_prefixes=("python",))
                time.sleep(0.15)
    except Exception:
        pass


def launch_panel(
    start_minimized: bool = False,
    initial_mode: Optional[str] = None,
    reload_existing: bool = False,
) -> int:
    _enable_high_dpi()
    set_app_user_model_id(APP_USER_MODEL_ID)
    if initial_mode in ("full", "zen"):
        cur_prefs = _load_ui_prefs()
        _save_ui_prefs(initial_mode, str(cur_prefs.get("zen_metric") or "gemini"))
    if reload_existing:
        _terminate_existing_panel_instance()
    elif _activate_existing_panel_window():
        return 0
    app = AcerolaOrionApp(start_minimized=start_minimized, initial_mode=initial_mode)
    app.mainloop()
    return 0
