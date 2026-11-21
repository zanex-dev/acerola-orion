# -*- coding: utf-8 -*-
"""
Acerola-Orion KV 缓存雷达悬浮胶囊窗口 (acerola_orion/ui/hud.py)
- 无边框置顶 High-DPI 悬浮胶囊（100% 本地只读观测）
- 支持拖拽记忆、双击唤醒主控台、窗口目标锁定菜单、200K 2x 计价标识与一键复制压缩口令
"""

from __future__ import annotations

import time
import tkinter as tk
from typing import Any, Callable, Optional, Sequence, Tuple

from ..cache_radar import (
    COMPACT_COMMAND_TEXT,
    TARGET_MODE_LATEST,
    TARGET_MODE_PINNED,
    TARGET_MODE_SMART_GUARD,
    CacheRadarState,
    RadarSessionCandidate,
)
from .theme import (
    ACCENT_CRIMSON,
    ACCENT_CYAN,
    BG_ROOT,
    BG_VOID,
    BORDER_SUBTLE,
    FG_TEXT,
    _radar_palette,
    _virtual_screen_rect,
    clamp_hud_coords,
)


class CacheRadarHudWindow(tk.Toplevel):
    """
    Frameless, always-on-top High-DPI Obsidian+Crimson mini floating capsule HUD (100% passive read-only).
    Supports mouse dragging with coordinate persistence, double-click panel wakeup, window-target switching,
    200K 2x pricing tier indicator, and quick `/compact` copy.
    """

    def __init__(
        self,
        master: tk.Misc,
        dpi_scale: float,
        initial_pos: Optional[Tuple[int, int]],
        on_drag_end: Callable[[int, int], None],
        on_select_mode: Callable[[str], Any],
        on_open_panel: Callable[[], None],
        hud_mode_getter: Optional[Callable[[], str]] = None,
        target_getter: Optional[Callable[[], Tuple[str, str]]] = None,
        sessions_provider: Optional[Callable[[], Sequence[RadarSessionCandidate]]] = None,
        on_select_target: Optional[Callable[[str, str], Any]] = None,
        on_copy_compact: Optional[Callable[[], Any]] = None,
    ) -> None:
        super().__init__(master, bg=BG_ROOT)
        self.withdraw()
        self._dpi_scale = max(1.0, float(dpi_scale))
        self._saved_pos: Optional[Tuple[int, int]] = initial_pos
        self._on_drag_end = on_drag_end
        self._on_select_mode = on_select_mode
        self._on_open_panel = on_open_panel
        self._hud_mode_getter = hud_mode_getter
        self._target_getter = target_getter
        self._sessions_provider = sessions_provider
        self._on_select_target = on_select_target
        self._on_copy_compact = on_copy_compact
        self._is_shown = False
        self._positioned_once = False
        self._drag_origin: Optional[Tuple[int, int, int, int]] = None
        self._drag_moved = False
        self._fraction = 0.0
        self._bar_color = BORDER_SUBTLE
        self._toast_text = ""
        self._toast_until_mono = 0.0
        self._hud_tier_packed = False

        try:
            self.overrideredirect(True)
        except Exception:
            pass
        try:
            self.attributes("-topmost", True)
        except Exception:
            pass
        try:
            self.attributes("-alpha", 0.94)
        except Exception:
            pass

        self.shell = tk.Frame(
            self,
            bg="#120d18",
            highlightthickness=1,
            highlightbackground=BORDER_SUBTLE,
        )
        self.shell.pack(fill=tk.BOTH, expand=True)

        row = tk.Frame(self.shell, bg="#120d18")
        row.pack(fill=tk.X)

        self.accent_strip = tk.Frame(row, bg=ACCENT_CRIMSON, width=self._px(4))
        self.accent_strip.pack(side=tk.LEFT, fill=tk.Y)

        self.lbl_capsule = tk.Label(
            row,
            text="⚪ 缓存雷达待命",
            bg="#120d18",
            fg=FG_TEXT,
            font=("Microsoft YaHei UI", 9, "bold"),
            padx=self._px(10),
            pady=self._px(5),
            cursor="fleur",
        )
        self.lbl_capsule.pack(side=tk.LEFT, fill=tk.X, expand=True)

        self.lbl_hud_tier = tk.Label(
            row,
            text="",
            bg="#4c0519",
            fg="#fda4af",
            font=("Microsoft YaHei UI", 8, "bold"),
            padx=self._px(6),
            pady=self._px(2),
            cursor="fleur",
        )

        self.bar_canvas = tk.Canvas(
            self.shell,
            height=max(2, self._px(3)),
            bg=BG_VOID,
            highlightthickness=0,
        )
        self.bar_canvas.pack(fill=tk.X)
        self.bar_canvas.bind("<Configure>", lambda _e: self._redraw_bar())

        self._ctx_menu = tk.Menu(
            self,
            tearoff=0,
            bg="#16111f",
            fg=FG_TEXT,
            activebackground=ACCENT_CRIMSON,
            activeforeground="#ffffff",
            font=("Microsoft YaHei UI", 9),
        )
        self._rebuild_ctx_menu()

        for w in (self.shell, row, self.accent_strip, self.lbl_capsule, self.lbl_hud_tier, self.bar_canvas):
            w.bind("<ButtonPress-1>", self._on_press)
            w.bind("<B1-Motion>", self._on_motion)
            w.bind("<ButtonRelease-1>", self._on_release)
            w.bind("<Double-Button-1>", lambda _e: self._on_open_panel())
            w.bind("<Button-3>", self._on_right_click)

    def _px(self, base: int) -> int:
        return int(round(base * self._dpi_scale))

    def flash_toast(self, text: str, duration_sec: float = 1.6) -> None:
        self._toast_text = str(text or "").strip()
        self._toast_until_mono = time.monotonic() + max(0.3, float(duration_sec))
        if self._toast_text:
            try:
                self.lbl_capsule.configure(text=self._toast_text, fg=ACCENT_CYAN)
            except Exception:
                pass

    def _rebuild_ctx_menu(self) -> None:
        self._ctx_menu.delete(0, tk.END)
        cur_hud = self._hud_mode_getter() if self._hud_mode_getter is not None else ""
        t_mode, pin_cid = self._target_getter() if self._target_getter is not None else (TARGET_MODE_SMART_GUARD, "")

        if self._on_copy_compact is not None:
            self._ctx_menu.add_command(
                label=f"✂️ 复制上下文压缩口令 (「{COMPACT_COMMAND_TEXT}」· 压回 200K 内免双倍计价)",
                command=self._on_copy_compact,
            )
            self._ctx_menu.add_separator()

        if self._on_select_target is not None:
            sel_fn = self._on_select_target
            b_smart = "● " if t_mode == TARGET_MODE_SMART_GUARD else "○ "
            b_latest = "● " if t_mode == TARGET_MODE_LATEST else "○ "
            self._ctx_menu.add_command(
                label=f"{b_smart}🧠 监控窗口 · 智能守大窗 (≥100K未冷优先)",
                command=lambda: sel_fn(TARGET_MODE_SMART_GUARD, ""),
            )
            self._ctx_menu.add_command(
                label=f"{b_latest}🔄 监控窗口 · 纯跟随最新活跃",
                command=lambda: sel_fn(TARGET_MODE_LATEST, ""),
            )
            sessions: Sequence[RadarSessionCandidate] = ()
            if self._sessions_provider is not None:
                try:
                    sessions = self._sessions_provider()
                except Exception:
                    sessions = ()
            for cand in list(sessions)[:6]:
                is_pin = t_mode == TARGET_MODE_PINNED and pin_cid and cand.conv_id.lower() == pin_cid.lower()
                mark = "● " if is_pin else "○ "
                self._ctx_menu.add_command(
                    label=f"{mark}📌 锁定: {cand.menu_label}",
                    command=lambda cid=cand.conv_id: sel_fn(TARGET_MODE_PINNED, cid),
                )
            self._ctx_menu.add_separator()

        m_smart = "● " if cur_hud == "smart" else ("○ " if cur_hud else "")
        m_always = "● " if cur_hud == "always" else ("○ " if cur_hud else "")
        m_off = "● " if cur_hud == "off" else ("○ " if cur_hud else "")
        self._ctx_menu.add_command(
            label=f"{m_smart}🎯 智能预警 (≥100K 临期自动弹出)",
            command=lambda: self._on_select_mode("smart"),
        )
        self._ctx_menu.add_command(
            label=f"{m_always}📌 常驻悬浮胶囊",
            command=lambda: self._on_select_mode("always"),
        )
        self._ctx_menu.add_command(
            label=f"{m_off}🚫 关闭悬浮胶囊",
            command=lambda: self._on_select_mode("off"),
        )
        self._ctx_menu.add_separator()
        self._ctx_menu.add_command(label="✦ 打开猎户座星盘主控台", command=self._on_open_panel)

    def _redraw_bar(self) -> None:
        self.bar_canvas.delete("all")
        w = max(10, int(self.bar_canvas.winfo_width()))
        h = max(2, int(self.bar_canvas.winfo_height()))
        self.bar_canvas.create_rectangle(0, 0, w, h, fill=BG_VOID, outline="")
        fw = int(round(w * max(0.0, min(1.0, self._fraction))))
        if fw > 0:
            self.bar_canvas.create_rectangle(0, 0, fw, h, fill=self._bar_color, outline="")

    def _ensure_geometry(self) -> None:
        try:
            self.update_idletasks()
            req_w = max(self._px(160), int(self.winfo_reqwidth()))
            req_h = max(self._px(28), int(self.winfo_reqheight()))
            rect = _virtual_screen_rect(self)
            px_x = self._saved_pos[0] if self._saved_pos is not None else None
            px_y = self._saved_pos[1] if self._saved_pos is not None else None
            cx, cy = clamp_hud_coords(px_x, px_y, req_w, req_h, rect, margin=self._px(16))
            self._saved_pos = (cx, cy)
            self.geometry(f"+{cx}+{cy}")
            self._positioned_once = True
        except Exception:
            pass

    def render_state(self, state: CacheRadarState, visible: bool) -> None:
        fg_color, bar_color, border_color = _radar_palette(state)
        self._fraction = state.ttl_fraction
        self._bar_color = bar_color
        toast_active = bool(self._toast_text and time.monotonic() < self._toast_until_mono)
        if toast_active:
            self.lbl_capsule.configure(text=self._toast_text, fg=ACCENT_CYAN)
        else:
            self._toast_text = ""
            self.lbl_capsule.configure(text=state.capsule_text, fg=fg_color)

        hud_tag = "" if toast_active else str(getattr(state, "hud_tier_tag", "") or "")
        if hud_tag:
            is_2x = bool(getattr(state, "is_over_2x", False))
            self.lbl_hud_tier.configure(
                text=hud_tag,
                bg="#4c0519" if is_2x else "#451a03",
                fg="#fda4af" if is_2x else "#fcd34d",
            )
            if not self._hud_tier_packed:
                self.lbl_hud_tier.pack(side=tk.LEFT, padx=(0, self._px(6)), pady=self._px(3))
                self._hud_tier_packed = True
                if self._positioned_once:
                    self._ensure_geometry()
        elif self._hud_tier_packed:
            self.lbl_hud_tier.pack_forget()
            self._hud_tier_packed = False

        self.accent_strip.configure(bg=ACCENT_CRIMSON if getattr(state, "is_over_2x", False) else bar_color)
        self.shell.configure(highlightbackground=border_color)
        self._redraw_bar()

        if visible:
            if not self._positioned_once:
                self._ensure_geometry()
            if not self._is_shown:
                self._is_shown = True
                try:
                    self.deiconify()
                    self.lift()
                    self.attributes("-topmost", True)
                except Exception:
                    pass
        else:
            if self._is_shown:
                self._is_shown = False
                try:
                    self.withdraw()
                except Exception:
                    pass

    def _on_press(self, event: tk.Event) -> None:  # type: ignore[type-arg]
        try:
            self._drag_origin = (int(event.x_root), int(event.y_root), int(self.winfo_x()), int(self.winfo_y()))
            self._drag_moved = False
        except Exception:
            self._drag_origin = None

    def _on_motion(self, event: tk.Event) -> None:  # type: ignore[type-arg]
        if self._drag_origin is None:
            return
        ox, oy, wx, wy = self._drag_origin
        dx = int(event.x_root) - ox
        dy = int(event.y_root) - oy
        if abs(dx) + abs(dy) >= 3:
            self._drag_moved = True
        try:
            req_w = max(80, int(self.winfo_width()))
            req_h = max(24, int(self.winfo_height()))
            nx, ny = clamp_hud_coords(
                wx + dx,
                wy + dy,
                req_w,
                req_h,
                _virtual_screen_rect(self),
                margin=self._px(6),
            )
            self._saved_pos = (nx, ny)
            self.geometry(f"+{nx}+{ny}")
        except Exception:
            pass

    def _on_release(self, _event: tk.Event) -> None:  # type: ignore[type-arg]
        moved = self._drag_moved
        pos = self._saved_pos
        self._drag_origin = None
        self._drag_moved = False
        if moved and pos is not None:
            try:
                self._on_drag_end(pos[0], pos[1])
            except Exception:
                pass

    def _on_right_click(self, event: tk.Event) -> None:  # type: ignore[type-arg]
        try:
            self._rebuild_ctx_menu()
            self._ctx_menu.tk_popup(int(event.x_root), int(event.y_root))
        finally:
            try:
                self._ctx_menu.grab_release()
            except Exception:
                pass

    def destroy(self) -> None:
        try:
            if getattr(self, "_ctx_menu", None) is not None:
                self._ctx_menu.delete(0, tk.END)
        except Exception:
            pass
        super().destroy()


__all__ = ["CacheRadarHudWindow"]
