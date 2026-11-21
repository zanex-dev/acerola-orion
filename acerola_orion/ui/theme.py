# -*- coding: utf-8 -*-
"""
Acerola-Orion 视觉主题、调色板与几何/布局辅助 (acerola_orion/ui/theme.py)
- Obsidian + Kiss-Shot Crimson + Orion Starlight 调色板常量
- 悬浮胶囊虚拟桌面坐标钳制、KV 缓存雷达语义配色与星盘椭圆几何计算
"""

from __future__ import annotations

import ctypes
from datetime import datetime, timezone
import math
import sys
import tkinter as tk
from typing import Dict, List, Optional, Tuple

from ..cache_radar import (
    LARGE_CONTEXT_HINT_TOKENS,
    PHASE_ACTIVE,
    PHASE_COLD,
    PHASE_HOT,
    PHASE_WARNING,
    TARGET_MODE_LATEST,
    TARGET_MODE_PINNED,
    TARGET_MODE_SMART_GUARD,
    CacheRadarState,
)

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

PALETTE: Dict[str, str] = {
    "BG_VOID": BG_VOID,
    "BG_ROOT": BG_ROOT,
    "BG_HEADER": BG_HEADER,
    "BG_CARD": BG_CARD,
    "BG_CARD_ALT": BG_CARD_ALT,
    "BG_DIAL_WELL": BG_DIAL_WELL,
    "FG_TEXT": FG_TEXT,
    "FG_SUB": FG_SUB,
    "FG_MUTED": FG_MUTED,
    "FG_DIM": FG_DIM,
    "ACCENT_CRIMSON": ACCENT_CRIMSON,
    "ACCENT_CRIMSON_DIM": ACCENT_CRIMSON_DIM,
    "ACCENT_ROSE": ACCENT_ROSE,
    "ACCENT_CYAN": ACCENT_CYAN,
    "ACCENT_CYAN_DIM": ACCENT_CYAN_DIM,
    "ACCENT_GREEN": ACCENT_GREEN,
    "ACCENT_GREEN_DIM": ACCENT_GREEN_DIM,
    "ACCENT_AMBER": ACCENT_AMBER,
    "ACCENT_AMBER_DIM": ACCENT_AMBER_DIM,
    "ACCENT_GOLD": ACCENT_GOLD,
    "ACCENT_VIOLET": ACCENT_VIOLET,
    "BORDER_SUBTLE": BORDER_SUBTLE,
    "BORDER_CRIMSON": BORDER_CRIMSON,
    "BORDER_ACTIVE": BORDER_ACTIVE,
}

HUD_MODE_LABELS: Dict[str, str] = {
    "smart": "智能预警",
    "always": "常驻胶囊",
    "off": "关闭胶囊",
}

TARGET_MODE_LABELS: Dict[str, str] = {
    TARGET_MODE_SMART_GUARD: "智能守大窗 (≥100K未冷优先)",
    TARGET_MODE_LATEST: "跟随最新活跃窗口",
    TARGET_MODE_PINNED: "手动锁定指定窗口",
}


def _radar_palette(state: CacheRadarState) -> Tuple[str, str, str]:
    """Return `(fg_color, bar_color, border_color)` matching Obsidian + Crimson Astrolabe palette."""
    if state.phase == PHASE_ACTIVE:
        fg, bar, border = (ACCENT_CYAN, ACCENT_CYAN, "#1e3a5f")
    elif state.phase == PHASE_HOT:
        fg, bar, border = (ACCENT_GREEN, ACCENT_GREEN, BORDER_SUBTLE)
    elif state.phase == PHASE_WARNING:
        if state.remaining_sec <= 30.0:
            fg, bar, border = (ACCENT_ROSE, ACCENT_CRIMSON, ACCENT_CRIMSON)
        else:
            fg, bar, border = (ACCENT_AMBER, ACCENT_AMBER, ACCENT_CRIMSON)
    elif state.phase == PHASE_COLD:
        if state.context_tokens >= LARGE_CONTEXT_HINT_TOKENS:
            fg, bar, border = (ACCENT_AMBER, "#475569", "#78350f")
        else:
            fg, bar, border = (FG_MUTED, "#334155", BORDER_SUBTLE)
    else:
        return (FG_MUTED, "#1e1b29", BORDER_SUBTLE)

    if getattr(state, "is_over_2x", False):
        border = ACCENT_CRIMSON
    elif getattr(state, "is_near_2x", False) and border in (BORDER_SUBTLE, "#1e3a5f", "#78350f"):
        border = ACCENT_AMBER
    return (fg, bar, border)


def clamp_hud_coords(
    x: Optional[int],
    y: Optional[int],
    req_w: int,
    req_h: int,
    screen_rect: Tuple[int, int, int, int],
    margin: int = 16,
) -> Tuple[int, int]:
    """Clamp HUD `(x, y)` inside virtual desktop `screen_rect=(vx, vy, vw, vh)`, or pick top-right default."""
    vx, vy, vw, vh = screen_rect
    w = max(80, int(req_w))
    h = max(24, int(req_h))
    m = max(4, int(margin))
    if x is None or y is None:
        tx = vx + vw - w - m * 2
        ty = vy + m * 3
    else:
        tx = int(x)
        ty = int(y)
    min_x = vx + m
    max_x = max(min_x, vx + vw - w - m)
    min_y = vy + m
    max_y = max(min_y, vy + vh - h - m)
    return (max(min_x, min(max_x, tx)), max(min_y, min(max_y, ty)))


def _virtual_screen_rect(widget: tk.Misc) -> Tuple[int, int, int, int]:
    if sys.platform == "win32":
        try:
            u32 = ctypes.windll.user32
            vx = int(u32.GetSystemMetrics(76))
            vy = int(u32.GetSystemMetrics(77))
            vw = int(u32.GetSystemMetrics(78))
            vh = int(u32.GetSystemMetrics(79))
            if vw >= 640 and vh >= 480:
                return (vx, vy, vw, vh)
        except Exception:
            pass
    try:
        sw = max(800, int(widget.winfo_screenwidth()))
        sh = max(600, int(widget.winfo_screenheight()))
    except Exception:
        sw, sh = 1920, 1080
    return (0, 0, sw, sh)


def _compose_tray_tooltip(base_line: str, radar_state: Optional[CacheRadarState]) -> str:
    """Compose Win32 127-WCHAR safe tray tooltip appending live KV Cache Radar status."""
    base = (base_line or "").strip()
    if radar_state is None or not radar_state.tooltip_suffix:
        return base[:127]
    combined = f"{base}\n{radar_state.tooltip_suffix}"
    if len(combined) <= 127:
        return combined
    budget = max(24, 126 - len(radar_state.tooltip_suffix))
    return f"{base[:budget]}\n{radar_state.tooltip_suffix}"[:127]


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


__all__ = [
    "BG_VOID",
    "BG_ROOT",
    "BG_HEADER",
    "BG_CARD",
    "BG_CARD_ALT",
    "BG_DIAL_WELL",
    "FG_TEXT",
    "FG_SUB",
    "FG_MUTED",
    "FG_DIM",
    "ACCENT_CRIMSON",
    "ACCENT_CRIMSON_DIM",
    "ACCENT_ROSE",
    "ACCENT_CYAN",
    "ACCENT_CYAN_DIM",
    "ACCENT_GREEN",
    "ACCENT_GREEN_DIM",
    "ACCENT_AMBER",
    "ACCENT_AMBER_DIM",
    "ACCENT_GOLD",
    "ACCENT_VIOLET",
    "BORDER_SUBTLE",
    "BORDER_CRIMSON",
    "BORDER_ACTIVE",
    "PALETTE",
    "HUD_MODE_LABELS",
    "TARGET_MODE_LABELS",
    "_radar_palette",
    "clamp_hud_coords",
    "_virtual_screen_rect",
    "_compose_tray_tooltip",
    "_compute_remaining_window_fraction",
    "_quota_semantic_colors",
    "_rotated_ellipse_points",
]
