# -*- coding: utf-8 -*-
"""
Acerola-Orion 内置视图兼容门面 (acerola_orion/ui/builtin_tabs.py)
- 底层实现已按业务域拆分至 `acerola_orion/ui/tabs/{common,zen_view,hub_tab,ledger_tab,vault_tab,diag_tab}.py`
- 本文件保持 100% 向后兼容导出
"""

from __future__ import annotations

from .tabs import (
    apply_builtin_tabs_data,
    build_daily_spend_points,
    build_diag_tab,
    build_header,
    build_hub_tab,
    build_ledger_tab,
    build_quota_points,
    build_vault_tab,
    build_zen_mode_ui,
    configure_styles,
    create_dark_text_box,
    create_scrolled_treeview,
    draw_mini_emblem,
    make_instrument_plate,
    refresh_active_charts_and_zen,
)

__all__ = [
    "configure_styles",
    "draw_mini_emblem",
    "build_header",
    "make_instrument_plate",
    "create_dark_text_box",
    "create_scrolled_treeview",
    "build_zen_mode_ui",
    "build_hub_tab",
    "build_ledger_tab",
    "build_vault_tab",
    "build_diag_tab",
    "build_quota_points",
    "build_daily_spend_points",
    "refresh_active_charts_and_zen",
    "apply_builtin_tabs_data",
]
