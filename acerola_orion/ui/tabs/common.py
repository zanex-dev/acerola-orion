# -*- coding: utf-8 -*-
"""
Acerola-Orion 内置视图公共样式、顶栏与控件构造器 (acerola_orion/ui/tabs/common.py)
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Any, Sequence, Tuple

from ...constants import APP_CODENAME, APP_TRINITY, APP_VERSION
from ..theme import (
    ACCENT_CRIMSON,
    ACCENT_CRIMSON_DIM,
    ACCENT_CYAN,
    ACCENT_CYAN_DIM,
    ACCENT_GOLD,
    ACCENT_ROSE,
    BG_CARD,
    BG_CARD_ALT,
    BG_HEADER,
    BG_ROOT,
    BG_VOID,
    BORDER_CRIMSON,
    BORDER_SUBTLE,
    FG_MUTED,
    FG_TEXT,
    _rotated_ellipse_points,
)


def configure_styles(app: Any) -> None:
    """Configure ttk dark obsidian styles for Treeviews and Scrollbars."""
    style = ttk.Style(app)
    try:
        style.theme_use("clam")
    except Exception:
        pass

    style.configure(
        "Orion.Treeview",
        background=BG_CARD,
        fieldbackground=BG_CARD,
        foreground=FG_TEXT,
        rowheight=app._px(28),
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
    style.configure(
        "Orion.Horizontal.TScrollbar",
        background=BG_CARD_ALT,
        troughcolor=BG_ROOT,
        bordercolor=BORDER_SUBTLE,
        arrowcolor=FG_MUTED,
        relief="flat",
    )


def draw_mini_emblem(app: Any, canvas: tk.Canvas) -> None:
    """Draw the miniature Orion astrolabe emblem on the top header canvas."""
    canvas.delete("all")
    s = app._px(34)
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


def build_header(app: Any) -> None:
    """Build the top crimson-obsidian header bar including optional plugin status badge."""
    hdr = tk.Frame(
        app,
        bg=BG_HEADER,
        padx=app._px(16),
        pady=app._px(9),
        highlightthickness=1,
        highlightbackground=BORDER_CRIMSON,
    )
    hdr.pack(fill=tk.X)

    left = tk.Frame(hdr, bg=BG_HEADER)
    left.pack(side=tk.LEFT)

    em_sz = app._px(34)
    emblem = tk.Canvas(left, width=em_sz, height=em_sz, bg=BG_HEADER, highlightthickness=0)
    emblem.pack(side=tk.LEFT, padx=(0, app._px(10)))
    app._draw_mini_emblem(emblem)

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

    # Optional plugin header status badge (not packed when unused)
    app.lbl_header_badge = tk.Label(
        title_row,
        text="",
        bg=ACCENT_CYAN_DIM,
        fg=ACCENT_CYAN,
        padx=7,
        pady=1,
        font=("Microsoft YaHei UI", 8, "bold"),
    )
    app._header_badge_packed = False

    app.lbl_header_sub = tk.Label(
        titles,
        text=f"{APP_TRINITY}  ·  猎户座星盘配额观测台 & 事件驱动断头台",
        bg=BG_HEADER,
        fg=FG_MUTED,
        font=("Microsoft YaHei UI", 8),
    )
    app.lbl_header_sub.pack(anchor="w", pady=(1, 0))

    right = tk.Frame(hdr, bg=BG_HEADER)
    right.pack(side=tk.RIGHT)

    app.lbl_sync_time = tk.Label(
        right,
        text="正在连接 127.0.0.1 回环遥测...",
        bg=BG_HEADER,
        fg=FG_MUTED,
        font=("Consolas", 8),
    )
    app.lbl_sync_time.pack(anchor="e", pady=(0, 4))

    btn_row = tk.Frame(right, bg=BG_HEADER)
    btn_row.pack(anchor="e")

    app.btn_mode_toggle = tk.Button(
        btn_row,
        text="◐ 极简视图",
        command=app.toggle_view_mode,
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
    app.btn_mode_toggle.pack(side=tk.LEFT, padx=(0, 6))

    app.btn_refresh = tk.Button(
        btn_row,
        text="🔃 刷新",
        command=lambda: app.refresh_all_async(silent=False),
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
    app.btn_refresh.pack(side=tk.LEFT, padx=(0, 6))

    app.btn_exit_app = tk.Button(
        btn_row,
        text="✕ 退出",
        command=app.exit_app,
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
    app.btn_exit_app.pack(side=tk.LEFT)


def make_instrument_plate(
    app: Any,
    parent: tk.Widget,
    heading: str,
) -> Tuple[tk.Frame, tk.Label, tk.Label]:
    card = tk.Frame(
        parent,
        bg=BG_CARD,
        highlightthickness=1,
        highlightbackground=BORDER_SUBTLE,
        padx=app._px(12),
        pady=app._px(8),
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
        wraplength=app._px(300),
        anchor="w",
        justify="left",
    )
    sub_lbl.pack(anchor="w", fill=tk.X)
    card.bind(
        "<Configure>",
        lambda e, lbl=sub_lbl: lbl.configure(wraplength=max(160, int(e.width) - 24)),
    )
    return card, main_lbl, sub_lbl


def create_dark_text_box(
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


def create_scrolled_treeview(
    app: Any,
    parent: tk.Widget,
    columns: Sequence[str],
    headers: Sequence[Tuple[str, str, int, str]],
    height: int,
    expand: bool = False,
) -> ttk.Treeview:
    wrap = tk.Frame(parent, bg=BG_ROOT, highlightthickness=1, highlightbackground=BORDER_SUBTLE)
    wrap.pack(fill=tk.BOTH if expand else tk.X, expand=expand, pady=(2, 8))
    wrap.columnconfigure(0, weight=1)
    wrap.rowconfigure(0, weight=1)
    tree = ttk.Treeview(wrap, columns=columns, show="headings", height=height, style="Orion.Treeview")
    vsb = ttk.Scrollbar(wrap, orient="vertical", command=tree.yview, style="Orion.Vertical.TScrollbar")
    hsb = ttk.Scrollbar(wrap, orient="horizontal", command=tree.xview, style="Orion.Horizontal.TScrollbar")
    tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
    for cid, text, base_w, anchor in headers:
        tree.heading(cid, text=text)
        tree.column(cid, width=app._px(base_w), minwidth=app._px(55), anchor=anchor, stretch=True)
    tree.grid(row=0, column=0, sticky="nsew")
    vsb.grid(row=0, column=1, sticky="ns")
    hsb.grid(row=1, column=0, sticky="ew")
    return tree


__all__ = [
    "configure_styles",
    "draw_mini_emblem",
    "build_header",
    "make_instrument_plate",
    "create_dark_text_box",
    "create_scrolled_treeview",
]
