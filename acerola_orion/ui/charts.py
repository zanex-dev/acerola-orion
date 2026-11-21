# -*- coding: utf-8 -*-
"""
Acerola-Orion 自适应矢量统计图控件 (acerola_orion/ui/charts.py)
- 支持面积轨迹图（24h 配额水位 / 旁路风控延迟示波器）与柱状图（14 日等效消耗 / 缓存读取）
- 内置鼠标悬停十字准星与精确数值检视标签
"""

from __future__ import annotations

import math
import tkinter as tk
from typing import List, Optional, Sequence, Tuple

from .theme import (
    ACCENT_CYAN,
    ACCENT_GOLD,
    ACCENT_ROSE,
    BG_CARD,
    BG_CARD_ALT,
    BG_DIAL_WELL,
    BORDER_SUBTLE,
    FG_DIM,
    FG_MUTED,
    FG_SUB,
    FG_TEXT,
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
        # Pack right hover/summary pill first so long titles never squeeze it off-screen
        self.lbl_hover = tk.Label(
            hdr,
            text="",
            bg=BG_CARD_ALT,
            fg=ACCENT_GOLD,
            padx=8,
            pady=1,
            font=("Consolas", 9, "bold"),
        )
        self.lbl_hover.pack(side=tk.RIGHT, padx=(6, 0))
        self.lbl_title = tk.Label(
            hdr, text=title, bg=BG_CARD, fg=FG_TEXT, font=("Microsoft YaHei UI", 10, "bold"), anchor="w"
        )
        self.lbl_title.pack(side=tk.LEFT, fill=tk.X, expand=True)

        self.canvas = tk.Canvas(self, height=height_px, bg=BG_DIAL_WELL, highlightthickness=0)
        self.canvas.pack(fill=tk.BOTH, expand=True)
        self._deferred_redraw_pending = False
        self._deferred_redraw_id: Optional[str] = None
        self.canvas.bind("<Configure>", lambda _e: self._redraw(allow_defer=False))
        self.bind(
            "<Configure>",
            lambda _e: self._redraw(allow_defer=False) if int(self.canvas.winfo_width()) <= 10 else None,
        )
        self.bind("<Destroy>", self._on_widget_destroy, add="+")
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

    def _redraw(self, *, allow_defer: bool = True) -> None:
        c = self.canvas
        c.delete("all")
        self._plot_xs = []
        cw = int(c.winfo_width())
        ch = int(c.winfo_height())
        if cw <= 10:
            parent_w = int(self.winfo_width())
            if parent_w > 140:
                cw = max(120, parent_w - 28)
            elif allow_defer and not getattr(self, "_deferred_redraw_pending", False):
                self._deferred_redraw_pending = True
                self._deferred_redraw_id = self.after_idle(self._redraw_later)
        w = max(120, cw)
        h = max(90, ch)

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
            for i, (_lbl, val) in enumerate(self._points):
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


__all__ = ["OrionChartWidget"]
