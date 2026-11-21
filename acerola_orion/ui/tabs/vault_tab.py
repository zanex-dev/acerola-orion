# -*- coding: utf-8 -*-
"""
Acerola-Orion 对话防删保险箱标签页模块 (acerola_orion/ui/tabs/vault_tab.py)
"""

from __future__ import annotations

import tkinter as tk
from typing import Any, List, Optional

from ...vault import VaultSessionItem, VaultStatusReport, _fmt_cn_time
from ..theme import (
    ACCENT_AMBER,
    ACCENT_CRIMSON,
    ACCENT_CYAN,
    ACCENT_GREEN,
    ACCENT_ROSE,
    BG_CARD,
    BG_CARD_ALT,
    BG_ROOT,
    BG_VOID,
    BORDER_SUBTLE,
    FG_MUTED,
    FG_TEXT,
)


def build_vault_tab(app: Any) -> None:
    """Build Orion-Vault Anti-Deletion Chat Archive Tab."""
    app.lbl_vault_banner = tk.Label(
        app.tab_vault,
        text="正在读取 Orion-Vault 对话防误删保险箱状态...",
        bg=BG_CARD_ALT,
        fg=ACCENT_CYAN,
        font=("Microsoft YaHei UI", 9, "bold"),
        anchor="w",
        justify="left",
        padx=12,
        pady=8,
    )
    app.lbl_vault_banner.pack(fill=tk.X, pady=(2, 6))
    app.tab_vault.bind(
        "<Configure>",
        lambda e: app.lbl_vault_banner.configure(wraplength=max(400, int(e.width) - 36)),
    )

    ctrl_bar = tk.Frame(
        app.tab_vault,
        bg=BG_CARD,
        highlightthickness=1,
        highlightbackground=BORDER_SUBTLE,
        padx=10,
        pady=8,
    )
    ctrl_bar.pack(fill=tk.X, pady=(0, 8))

    app._btn_vault_switch = tk.Button(
        ctrl_bar,
        text="🟢 自动备份: 已开启 (点此关闭)",
        command=app._toggle_vault_auto_backup,
        bg="#065f46",
        fg=FG_TEXT,
        activebackground="#047857",
        activeforeground=FG_TEXT,
        font=("Microsoft YaHei UI", 9, "bold"),
        relief="flat",
        padx=10,
        pady=4,
        cursor="hand2",
    )
    app._btn_vault_switch.pack(side=tk.LEFT, padx=(0, 8))
    app._action_buttons.append(app._btn_vault_switch)
    app._sync_vault_switch_button(app._vault_auto_enabled)

    app._btn_vault_filter = tk.Button(
        ctrl_bar,
        text="🔍 视图: 全部归档",
        command=app._toggle_vault_filter,
        bg="#334155",
        fg=FG_TEXT,
        activebackground="#475569",
        activeforeground=FG_TEXT,
        font=("Microsoft YaHei UI", 9),
        relief="flat",
        padx=10,
        pady=4,
        cursor="hand2",
    )
    app._btn_vault_filter.pack(side=tk.LEFT, padx=(0, 8))

    tk.Label(ctrl_bar, text="检索:", bg=BG_CARD, fg=FG_MUTED, font=("Microsoft YaHei UI", 9)).pack(side=tk.LEFT)
    app._vault_query_var = tk.StringVar(value="")
    ent_q = tk.Entry(
        ctrl_bar,
        textvariable=app._vault_query_var,
        bg=BG_VOID,
        fg=FG_TEXT,
        insertbackground=FG_TEXT,
        relief="flat",
        font=("Microsoft YaHei UI", 9),
        width=18,
    )
    ent_q.pack(side=tk.LEFT, padx=(4, 6), ipady=3)
    ent_q.bind("<Return>", lambda _e: app.refresh_all_async(silent=False))

    btn_search = tk.Button(
        ctrl_bar,
        text="筛选",
        command=lambda: app.refresh_all_async(silent=False),
        bg="#1e293b",
        fg=FG_TEXT,
        font=("Microsoft YaHei UI", 9),
        relief="flat",
        padx=8,
        pady=4,
        cursor="hand2",
    )
    btn_search.pack(side=tk.LEFT, padx=(0, 10))

    btn_export = tk.Button(
        ctrl_bar,
        text="📄 导出 Markdown",
        command=app._on_vault_export_selected,
        bg="#1e3a8a",
        fg=FG_TEXT,
        font=("Microsoft YaHei UI", 9, "bold"),
        relief="flat",
        padx=9,
        pady=4,
        cursor="hand2",
    )
    btn_export.pack(side=tk.RIGHT, padx=(6, 0))
    app._action_buttons.append(btn_export)

    btn_restore = tk.Button(
        ctrl_bar,
        text="♻️ 一键恢复已删",
        command=app._on_vault_restore_selected,
        bg=ACCENT_CRIMSON,
        fg=FG_TEXT,
        font=("Microsoft YaHei UI", 9, "bold"),
        relief="flat",
        padx=9,
        pady=4,
        cursor="hand2",
    )
    btn_restore.pack(side=tk.RIGHT, padx=(6, 0))
    app._action_buttons.append(btn_restore)

    btn_sync = tk.Button(
        ctrl_bar,
        text="⚡ 立即备份全量",
        command=lambda: app._run_action(
            "Orion-Vault · 手动全量增量备份",
            lambda: app._vault.sync_all(respect_switch=False),
        ),
        bg="#4c1d95",
        fg=FG_TEXT,
        font=("Microsoft YaHei UI", 9, "bold"),
        relief="flat",
        padx=9,
        pady=4,
        cursor="hand2",
    )
    btn_sync.pack(side=tk.RIGHT)
    app._action_buttons.append(btn_sync)

    tk.Label(
        app.tab_vault,
        text="🗄️ 已归档会话列表 (客户端误删对话将自动标记为 [🗑️已删可恢复] 并置顶保留，绝不随主库连坐清理)",
        bg=BG_ROOT,
        fg=ACCENT_ROSE,
        font=("Microsoft YaHei UI", 9, "bold"),
    ).pack(anchor="w", pady=(0, 2))

    cols_vault = ("status", "modified", "title", "model", "steps", "size", "uuid")
    headers_vault = [
        ("status", "主库状态", 115, "center"),
        ("modified", "最后活跃时间", 130, "center"),
        ("title", "会话标题", 360, "w"),
        ("model", "主模型", 165, "w"),
        ("steps", "步数", 65, "e"),
        ("size", "归档体积", 95, "e"),
        ("uuid", "Conversation UUID", 260, "w"),
    ]
    app.tree_vault = app._create_scrolled_treeview(
        app.tab_vault, cols_vault, headers_vault, height=14, expand=True
    )


def refresh_vault_tab_data(
    app: Any,
    vault_st: Optional[VaultStatusReport],
    vault_rows: Optional[List[VaultSessionItem]],
) -> None:
    """Update Vault banner and archived conversation Treeview."""
    if vault_st is not None and hasattr(app, "lbl_vault_banner"):
        app._last_vault_status = vault_st
        app._sync_vault_switch_button(vault_st.auto_backup_enabled)
        sw_txt = (
            "🟢 已开启 (每轮对话结束实时增量归档)"
            if vault_st.auto_backup_enabled
            else "⚪ 已关闭 (暂停自动备份 · 历史归档保留)"
        )
        app.lbl_vault_banner.configure(
            text=(
                f"🗄️ 自动备份总开关: {sw_txt}  |  已保护总会话: {vault_st.total_conversations} 个 "
                f"(🟢在线={vault_st.active_conversations} / 🗑️已删可恢复={vault_st.deleted_conversations})  |  "
                f"总占用: {vault_st.total_vault_mb:.2f} MB  |  最近同步: {_fmt_cn_time(vault_st.last_sync_at)}\n"
                f"📂 归档目录: {vault_st.vault_dir}"
            ),
            fg=ACCENT_GREEN if vault_st.auto_backup_enabled else ACCENT_AMBER,
        )
    if vault_rows is not None and hasattr(app, "tree_vault"):
        prev_sel_cid = app._get_selected_vault_conv_id()
        app.tree_vault.delete(*app.tree_vault.get_children())
        reselect_iid = None
        for vr in vault_rows:
            badge = "🗑️ 已删可恢复" if vr.live_status == "deleted" else "🟢 主库在线"
            kb = (vr.conv_db_bytes + vr.brain_bytes) / 1024.0
            size_str = f"{kb / 1024.0:.2f} MB" if kb >= 1024.0 else f"{kb:.0f} KB"
            iid = app.tree_vault.insert(
                "",
                tk.END,
                values=(
                    badge,
                    _fmt_cn_time(vr.last_modified_at),
                    vr.title,
                    vr.model_name or "-",
                    vr.step_count,
                    size_str,
                    vr.conv_id,
                ),
            )
            if prev_sel_cid and vr.conv_id == prev_sel_cid:
                reselect_iid = iid
        if reselect_iid is not None:
            app.tree_vault.selection_set(reselect_iid)


__all__ = ["build_vault_tab", "refresh_vault_tab_data"]
