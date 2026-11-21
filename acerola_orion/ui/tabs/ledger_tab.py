# -*- coding: utf-8 -*-
"""
Acerola-Orion Token 账本与费率审计标签页模块 (acerola_orion/ui/tabs/ledger_tab.py)
"""

from __future__ import annotations

import tkinter as tk
from typing import Any, List

from ..theme import ACCENT_CYAN, ACCENT_ROSE, BG_CARD_ALT, BG_ROOT


def build_ledger_tab(app: Any) -> None:
    """Build Token Ledger & Rate Audit Tab."""
    app.lbl_auditor_banner = tk.Label(
        app.tab_ledger,
        text="正在汇总本地会话 Token 账本与扣费斜率...",
        bg=BG_CARD_ALT,
        fg=ACCENT_CYAN,
        font=("Microsoft YaHei UI", 9, "bold"),
        anchor="w",
        justify="left",
        padx=12,
        pady=8,
    )
    app.lbl_auditor_banner.pack(fill=tk.X, pady=(2, 6))
    app.tab_ledger.bind(
        "<Configure>",
        lambda e: app.lbl_auditor_banner.configure(wraplength=max(400, int(e.width) - 36)),
    )

    tk.Label(
        app.tab_ledger,
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
        ("uagy", "1.5x AGY预设倍率估", 115, "e"),
    ]
    app.tree_daily = app._create_scrolled_treeview(
        app.tab_ledger, cols_daily, headers_daily, height=5, expand=False
    )

    tk.Label(
        app.tab_ledger,
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
    app.tree_models = app._create_scrolled_treeview(
        app.tab_ledger, cols_mod, headers_mod, height=4, expand=False
    )

    tk.Label(
        app.tab_ledger,
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
    app.tree_sessions = app._create_scrolled_treeview(
        app.tab_ledger, cols_sess, headers_sess, height=6, expand=True
    )


def refresh_ledger_tab_data(
    app: Any,
    today_str: str,
    today_row: Any,
    daily: List[Any],
    models: List[Any],
    sessions: List[Any],
    audit: Any,
) -> None:
    """Populate Token Ledger banner and daily / model / session Treeviews."""
    if today_row:
        t_summary = (
            f"📌 今日用量 ({today_str}): {today_row.sessions} 个会话 / {today_row.steps} 步  |  "
            f"Input={today_row.input_tokens:,}  Cache={today_row.cache_read_tokens:,}  Output={today_row.output_tokens:,}  |  "
            f"1.0x原价=${today_row.usd_1x:.2f}  (0.5x估算=${today_row.usd_intro:.2f} / 1.5x预设倍率估=${today_row.usd_agy_est:.2f})\n"
            f"🔍 快照分析: {audit.verdict}"
        )
    else:
        t_summary = f"📌 今日暂无 Token 记录  |  🔍 快照分析: {audit.verdict}"
    app.lbl_auditor_banner.configure(text=t_summary)

    app.tree_daily.delete(*app.tree_daily.get_children())
    for d in daily:
        app.tree_daily.insert(
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

    app.tree_models.delete(*app.tree_models.get_children())
    for m in models:
        app.tree_models.insert(
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

    app.tree_sessions.delete(*app.tree_sessions.get_children())
    for s in sessions:
        app.tree_sessions.insert(
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


__all__ = ["build_ledger_tab", "refresh_ledger_tab_data"]
