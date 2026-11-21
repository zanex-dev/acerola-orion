# -*- coding: utf-8 -*-
"""
Acerola-Orion 猎户座星盘仪表盘与配额条控件 (acerola_orion/ui/astrolabe.py)
- QuotaBarWidget：向后兼容的线性配额进度条
- AstrolabeDialWidget：猎户座双星轨精密机械计时仪（外轨 5h + 内轨周池 + 金针时间基准）
"""

from __future__ import annotations

import math
import tkinter as tk
from typing import Optional, Tuple

from ..telemetry import describe_quota_timer
from .theme import (
    ACCENT_AMBER,
    ACCENT_AMBER_DIM,
    ACCENT_CRIMSON_DIM,
    ACCENT_CYAN,
    ACCENT_CYAN_DIM,
    ACCENT_GOLD,
    ACCENT_GREEN,
    ACCENT_GREEN_DIM,
    ACCENT_ROSE,
    ACCENT_VIOLET,
    BG_CARD,
    BG_CARD_ALT,
    BG_DIAL_WELL,
    BG_VOID,
    BORDER_CRIMSON,
    BORDER_SUBTLE,
    FG_DIM,
    FG_MUTED,
    FG_SUB,
    FG_TEXT,
    _compute_remaining_window_fraction,
    _quota_semantic_colors,
    _rotated_ellipse_points,
)


class QuotaBarWidget(tk.Frame):
    """Backward-compatible compact linear bar widget with anti-truncation & deferred single-shot redraw."""

    def __init__(self, parent: tk.Widget, title: str, bar_height: int = 10, default_window: str = "") -> None:
        super().__init__(
            parent, bg=BG_CARD, highlightthickness=1, highlightbackground=BORDER_SUBTLE, padx=10, pady=6
        )
        self._default_window = default_window
        top = tk.Frame(self, bg=BG_CARD)
        top.pack(fill=tk.X)
        # Anti-clipping rule: pack right-aligned percentage readout BEFORE left-aligned elastic title
        self.lbl_pct = tk.Label(
            top, text="--.---%", bg=BG_CARD, fg=ACCENT_CYAN, font=("Consolas", 10, "bold")
        )
        self.lbl_pct.pack(side=tk.RIGHT, padx=(6, 0))
        self.lbl_title = tk.Label(
            top, text=title, bg=BG_CARD, fg=FG_TEXT, font=("Microsoft YaHei UI", 9, "bold"), anchor="w"
        )
        self.lbl_title.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.canvas = tk.Canvas(self, height=bar_height, bg=BG_VOID, highlightthickness=0)
        self.canvas.pack(fill=tk.X, pady=(4, 3))
        self.lbl_sub = tk.Label(
            self, text="等待采样...", bg=BG_CARD, fg=FG_MUTED, font=("Microsoft YaHei UI", 8), anchor="w", justify="left"
        )
        self.lbl_sub.pack(anchor="w", fill=tk.X)
        self._fraction = 0.0
        self._timer_started = True
        self._deferred_redraw_pending = False
        self._deferred_redraw_id: Optional[str] = None
        self.canvas.bind("<Configure>", lambda _e: self._redraw(allow_defer=False))
        self.bind("<Configure>", self._on_card_configure)
        self.bind("<Destroy>", self._on_widget_destroy, add="+")

    def _on_card_configure(self, event: tk.Event) -> None:  # type: ignore[type-arg]
        self.lbl_sub.configure(wraplength=max(140, int(event.width) - 24))
        if int(self.canvas.winfo_width()) <= 10:
            self._redraw(allow_defer=False)

    def update_bucket(
        self,
        fraction: float,
        reset_countdown: str,
        status_text: Optional[str] = None,
        timer_started: Optional[bool] = None,
    ) -> None:
        try:
            val = float(fraction)
        except (TypeError, ValueError):
            val = math.nan
        default_win = str(getattr(self, "_default_window", "") or "")
        if not math.isfinite(val):
            self._fraction = 0.0
            self._timer_started = False
            sub_txt = status_text if status_text else describe_quota_timer(default_win, "", val, "", reset_countdown)[1]
            self.lbl_pct.configure(text="--.---%", fg=FG_MUTED)
            self.lbl_sub.configure(text=sub_txt, fg=FG_MUTED)
            self._redraw()
            return
        self._fraction = max(0.0, min(1.0, val))
        auto_started, auto_status = describe_quota_timer(default_win, "", self._fraction, "", reset_countdown)
        self._timer_started = auto_started if timer_started is None else bool(timer_started)
        sub_txt = status_text if status_text else auto_status
        pct = self._fraction * 100.0
        fg, _ = _quota_semantic_colors(pct)
        self.lbl_pct.configure(text=f"{pct:7.3f}%", fg=fg)
        self.lbl_sub.configure(text=sub_txt, fg=FG_SUB if self._timer_started else FG_MUTED)
        self._redraw()

    def _redraw(self, *, allow_defer: bool = True) -> None:
        self.canvas.delete("all")
        cw = int(self.canvas.winfo_width())
        ch = int(self.canvas.winfo_height())
        if cw <= 10:
            parent_w = int(self.winfo_width())
            if parent_w > 30:
                cw = max(10, parent_w - 22)
            elif allow_defer and not getattr(self, "_deferred_redraw_pending", False):
                self._deferred_redraw_pending = True
                self._deferred_redraw_id = self.after_idle(self._redraw_later)
        w = max(10, cw)
        h = max(6, ch)
        self.canvas.create_rectangle(0, 0, w, h, fill=BG_VOID, outline="")
        fill_w = int(round(w * self._fraction))
        if fill_w > 0:
            fg, _ = _quota_semantic_colors(self._fraction * 100.0)
            self.canvas.create_rectangle(0, 0, fill_w, h, fill=fg, outline="")

    def _cancel_deferred_redraw(self) -> None:
        aid = getattr(self, "_deferred_redraw_id", None)
        self._deferred_redraw_id = None
        self._deferred_redraw_pending = False
        if aid is not None:
            try:
                self.after_cancel(aid)
            except tk.TclError:
                pass

    def _on_widget_destroy(self, event: tk.Event) -> None:  # type: ignore[type-arg]
        if event.widget is self:
            self._cancel_deferred_redraw()

    def _redraw_later(self) -> None:
        self._deferred_redraw_id = None
        self._deferred_redraw_pending = False
        if self.winfo_exists():
            self._redraw(allow_defer=False)


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

        # Top header bar inside card (pack right badge first so narrow widths never clip status pill!)
        hdr = tk.Frame(self, bg=BG_CARD)
        hdr.pack(fill=tk.X, pady=(0, 2))
        self.lbl_badge = tk.Label(
            hdr,
            text=self._footer_right,
            bg=BG_CARD_ALT,
            fg=FG_MUTED,
            padx=8,
            pady=2,
            font=("Microsoft YaHei UI", 8, "bold"),
        )
        self.lbl_badge.pack(side=tk.RIGHT, padx=(6, 0))
        self.lbl_title = tk.Label(
            hdr,
            text=title,
            bg=BG_CARD,
            fg=FG_TEXT,
            font=("Microsoft YaHei UI", 10 if not simplified_digits else 11, "bold"),
            anchor="w",
        )
        self.lbl_title.pack(side=tk.LEFT, fill=tk.X, expand=True)

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
        self.lbl_weekly_meta = tk.Label(
            ftr,
            text="",
            bg=BG_CARD,
            fg=FG_DIM,
            font=("Consolas", 8),
        )
        self.lbl_weekly_meta.pack(side=tk.RIGHT, padx=(6, 0))
        self.lbl_reset = tk.Label(
            ftr,
            text=self._footer_left,
            bg=BG_CARD,
            fg=FG_MUTED,
            font=("Microsoft YaHei UI", 9 if simplified_digits else 8),
            anchor="w",
        )
        self.lbl_reset.pack(side=tk.LEFT, fill=tk.X, expand=True)

        self._deferred_redraw_pending = False
        self._deferred_redraw_id: Optional[str] = None
        self.canvas.bind("<Configure>", lambda _e: self._redraw(allow_defer=False))
        self.bind(
            "<Configure>",
            lambda _e: self._redraw(allow_defer=False) if int(self.canvas.winfo_width()) <= 10 else None,
        )
        self.bind("<Destroy>", self._on_widget_destroy, add="+")

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
        p_untouched_5h = bool(f5_ok and p_frac >= 0.999999)
        self._time_rem_frac = (
            _compute_remaining_window_fraction(reset_5h_iso, 18000.0)
            if (f5_ok and not p_untouched_5h)
            else None
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
            wk_untouched = s_frac >= 0.999999
            wk_lbl = (
                f"周满额未动 · {reset_weekly_countdown}"
                if wk_untouched and reset_weekly_countdown not in ("", "--", "未知")
                else f"周重置: {reset_weekly_countdown}"
            )
            self.lbl_weekly_meta.configure(
                text="" if self._simplified else wk_lbl,
                fg=FG_MUTED,
            )
        else:
            self._center_sub = "单通道直读"
            self.lbl_weekly_meta.configure(text="")

        cd_txt = reset_5h_countdown if f5_ok else reset_weekly_countdown
        if p_untouched_5h:
            self.lbl_reset.configure(
                text="满额待命 · 首条请求触发 5h 计时" if self._simplified else "5h 池: 满额待命 · 未开始计时",
                fg=FG_SUB if self._simplified else FG_MUTED,
            )
            badge_txt = "✦ 满额待命"
            badge_fg, badge_bg = ACCENT_CYAN, ACCENT_CYAN_DIM
        else:
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

    def _redraw(self, *, allow_defer: bool = True) -> None:
        c = self.canvas
        c.delete("all")
        cw = int(c.winfo_width())
        ch = int(c.winfo_height())
        if cw <= 10:
            pw = int(self.winfo_width())
            if pw > 90:
                cw = max(80, pw - 28)
            elif allow_defer and not getattr(self, "_deferred_redraw_pending", False):
                self._deferred_redraw_pending = True
                self._deferred_redraw_id = self.after_idle(self._redraw_later)
        w = max(80, cw)
        h = max(80, ch)
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
            c.create_line(
                cx + (r_outer - ring_w * 0.8) * ca,
                cy + (r_outer - ring_w * 0.8) * sa,
                cx + (radius * 0.96) * ca,
                cy + (radius * 0.96) * sa,
                fill=ACCENT_GOLD,
                width=1.8,
            )
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

    def _cancel_deferred_redraw(self) -> None:
        aid = getattr(self, "_deferred_redraw_id", None)
        self._deferred_redraw_id = None
        self._deferred_redraw_pending = False
        if aid is not None:
            try:
                self.after_cancel(aid)
            except tk.TclError:
                pass

    def _on_widget_destroy(self, event: tk.Event) -> None:  # type: ignore[type-arg]
        if event.widget is self:
            self._cancel_deferred_redraw()

    def _redraw_later(self) -> None:
        self._deferred_redraw_id = None
        self._deferred_redraw_pending = False
        if self.winfo_exists():
            self._redraw(allow_defer=False)


__all__ = ["QuotaBarWidget", "AstrolabeDialWidget"]
