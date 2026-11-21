# -*- coding: utf-8 -*-
"""
Acerola-Orion 视觉控件兼容门面 (acerola_orion/ui/widgets.py)
- 底层实现已模块化拆分至 `theme.py` / `hud.py` / `astrolabe.py` / `charts.py`
- 本文件保持 100% 向后兼容重导出，避免破坏既有外部导入与测试引用
"""

from __future__ import annotations

from .astrolabe import AstrolabeDialWidget, QuotaBarWidget
from .charts import OrionChartWidget
from .hud import CacheRadarHudWindow
from .theme import (
    ACCENT_AMBER,
    ACCENT_AMBER_DIM,
    ACCENT_CRIMSON,
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
    BG_HEADER,
    BG_ROOT,
    BG_VOID,
    BORDER_ACTIVE,
    BORDER_CRIMSON,
    BORDER_SUBTLE,
    FG_DIM,
    FG_MUTED,
    FG_SUB,
    FG_TEXT,
    HUD_MODE_LABELS,
    PALETTE,
    TARGET_MODE_LABELS,
    _compose_tray_tooltip,
    _compute_remaining_window_fraction,
    _quota_semantic_colors,
    _radar_palette,
    _rotated_ellipse_points,
    _virtual_screen_rect,
    clamp_hud_coords,
)

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
    "CacheRadarHudWindow",
    "_compute_remaining_window_fraction",
    "_quota_semantic_colors",
    "_rotated_ellipse_points",
    "QuotaBarWidget",
    "AstrolabeDialWidget",
    "OrionChartWidget",
]
