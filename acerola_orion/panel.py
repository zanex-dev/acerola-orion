# -*- coding: utf-8 -*-
"""
Acerola-Orion 桌面可视化主控台 (acerola_orion/panel.py)
- 视觉延续 Kiss-Shot 暗黑黑曜石 + 绯红星芒风格 (Obsidian / Crimson / Starlight)
- 全分辨率 High-DPI 自适应布局 (100% / 125% / 150% / 200% 无裁切、暗色滚动条)
- 刷新锁与动作锁分离 + 代次防重看门狗 + 单实例前置唤醒 + 关窗线程安全保护
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from datetime import datetime
import math
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import scrolledtext, ttk
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .constants import (
    APP_CODENAME,
    APP_TRINITY,
    APP_VERSION,
    get_guillotine_log_path,
    get_ls_log_path,
)
from .guillotine import install_guard_autostart, uninstall_guard_autostart
from .ledger import OrionLedger
from .rpc import read_shared_file_lines
from .telemetry import OrionTelemetryReport, collect_telemetry, invalidate_guard_probe_cache

# Palette: Obsidian + Kiss-Shot Crimson + Orion Cyan
BG_ROOT = "#0c0a10"
BG_CARD = "#15111c"
BG_CARD_ALT = "#1d1726"
FG_TEXT = "#f8fafc"
FG_MUTED = "#94a3b8"
ACCENT_CRIMSON = "#e11d48"
ACCENT_ROSE = "#fb7185"
ACCENT_CYAN = "#38bdf8"
ACCENT_GREEN = "#10b981"
ACCENT_AMBER = "#f59e0b"
BORDER_DIM = "#2e243d"


def _panel_window_title() -> str:
    return f"{APP_CODENAME} v{APP_VERSION} — {APP_TRINITY}"


def _enable_high_dpi() -> None:
    if sys.platform == "win32":
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:
                pass


def _activate_existing_panel_window() -> bool:
    """If an identical-version panel window is already open on desktop, restore and focus it."""
    if sys.platform != "win32":
        return False
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
        user32.FindWindowW.restype = wintypes.HWND
        user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.ShowWindow.restype = wintypes.BOOL
        user32.SetForegroundWindow.argtypes = [wintypes.HWND]
        user32.SetForegroundWindow.restype = wintypes.BOOL

        hwnd = user32.FindWindowW(None, _panel_window_title())
        if hwnd:
            SW_RESTORE = 9
            user32.ShowWindow(hwnd, SW_RESTORE)
            user32.SetForegroundWindow(hwnd)
            return True
    except Exception:
        pass
    return False


class QuotaBarWidget(tk.Frame):
    def __init__(self, parent: tk.Widget, title: str, bar_height: int = 12) -> None:
        super().__init__(parent, bg=BG_CARD, highlightthickness=1, highlightbackground=BORDER_DIM, padx=12, pady=8)
        top = tk.Frame(self, bg=BG_CARD)
        top.pack(fill=tk.X)
        self.lbl_title = tk.Label(
            top, text=title, bg=BG_CARD, fg=FG_TEXT, font=("Microsoft YaHei UI", 10, "bold")
        )
        self.lbl_title.pack(side=tk.LEFT)
        self.lbl_pct = tk.Label(
            top, text="--.---%", bg=BG_CARD, fg=ACCENT_CYAN, font=("Consolas", 11, "bold")
        )
        self.lbl_pct.pack(side=tk.RIGHT)

        self.canvas = tk.Canvas(self, height=bar_height, bg="#09070c", highlightthickness=0)
        self.canvas.pack(fill=tk.X, pady=(6, 4))
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
            self.lbl_pct.configure(text="--.---%", fg=ACCENT_AMBER)
            self.lbl_sub.configure(text=f"重置倒计时: {reset_countdown}")
            self._redraw()
            return
        self._fraction = max(0.0, min(1.0, val))
        pct = self._fraction * 100.0
        color = ACCENT_GREEN if pct >= 50.0 else (ACCENT_AMBER if pct >= 20.0 else ACCENT_CRIMSON)
        self.lbl_pct.configure(text=f"{pct:7.3f}%", fg=color)
        self.lbl_sub.configure(text=f"重置倒计时: {reset_countdown}")
        self._redraw()

    def _redraw(self) -> None:
        self.canvas.delete("all")
        w = max(10, self.canvas.winfo_width())
        h = max(8, self.canvas.winfo_height())
        self.canvas.create_rectangle(0, 0, w, h, fill="#09070c", outline="")
        fill_w = int(round(w * self._fraction))
        if fill_w > 0:
            pct = self._fraction * 100.0
            color = ACCENT_CYAN if pct >= 50.0 else (ACCENT_AMBER if pct >= 20.0 else ACCENT_CRIMSON)
            self.canvas.create_rectangle(0, 0, fill_w, h, fill=color, outline="")


class AcerolaOrionApp(tk.Tk):
    def __init__(self, auto_refresh_ms: int = 15000) -> None:
        _enable_high_dpi()
        super().__init__()
        self.auto_refresh_ms = auto_refresh_ms
        self._refresh_busy = False
        self._action_busy = False
        self._refresh_gen = 0
        self._action_gen = 0
        self._pending_refresh = False
        self._offline_streak = 0
        self._closing = False
        self._action_buttons: List[tk.Button] = []
        self._ledger = OrionLedger()
        self._last_report: Optional[OrionTelemetryReport] = None

        try:
            dpi = float(self.winfo_fpixels("1i"))
            self._dpi_scale = max(1.0, min(2.25, dpi / 96.0))
        except Exception:
            self._dpi_scale = 1.0

        self.title(_panel_window_title())
        sw = max(1024, self.winfo_screenwidth())
        sh = max(720, self.winfo_screenheight())
        win_w = min(self._px(1140), sw - 80)
        win_h = min(self._px(810), sh - 90)
        min_w = min(self._px(960), sw - 120)
        min_h = min(self._px(660), sh - 120)
        pos_x = max(20, (sw - win_w) // 2)
        pos_y = max(20, (sh - win_h) // 2 - 15)
        self.geometry(f"{win_w}x{win_h}+{pos_x}+{pos_y}")
        self.minsize(min_w, min_h)
        self.configure(bg=BG_ROOT)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self._configure_styles()
        self._build_header()
        self._build_tabs()
        self.after(150, self.refresh_all_async)
        self.after(50, self._pump_ui_queue)
        self._schedule_auto_refresh()

    def _px(self, base: int) -> int:
        return int(round(base * self._dpi_scale))

    def _on_close(self) -> None:
        self._closing = True
        try:
            self.destroy()
        except Exception:
            pass

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
            self.after(50, self._pump_ui_queue)
        except Exception:
            pass

    def _configure_styles(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except Exception:
            pass

        style.configure("TNotebook", background=BG_ROOT, borderwidth=0)
        style.configure(
            "TNotebook.Tab",
            background=BG_CARD,
            foreground=FG_MUTED,
            padding=(self._px(14), self._px(7)),
            font=("Microsoft YaHei UI", 10, "bold"),
        )
        style.map(
            "TNotebook.Tab",
            background=[("selected", ACCENT_CRIMSON)],
            foreground=[("selected", "#ffffff")],
        )

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
        style.map("Orion.Treeview", background=[("selected", "#4c0519")], foreground=[("selected", "#ffffff")])

        style.configure(
            "Orion.Vertical.TScrollbar",
            background=BG_CARD_ALT,
            troughcolor=BG_ROOT,
            bordercolor=BORDER_DIM,
            arrowcolor=FG_MUTED,
            relief="flat",
        )

    def _build_header(self) -> None:
        hdr = tk.Frame(self, bg="#120914", padx=18, pady=10, highlightthickness=1, highlightbackground="#4c0519")
        hdr.pack(fill=tk.X)

        left = tk.Frame(hdr, bg="#120914")
        left.pack(side=tk.LEFT)
        tk.Label(
            left,
            text=f"✦ {APP_CODENAME} v{APP_VERSION}",
            bg="#120914",
            fg=ACCENT_ROSE,
            font=("Microsoft YaHei UI", 13, "bold"),
        ).pack(anchor="w")
        tk.Label(
            left,
            text=f"{APP_TRINITY}  ·  Orion 配额与验钞账本 & Acerola 零轮询内核进程守卫",
            bg="#120914",
            fg=FG_MUTED,
            font=("Microsoft YaHei UI", 9),
        ).pack(anchor="w")

        right = tk.Frame(hdr, bg="#120914")
        right.pack(side=tk.RIGHT)
        self.lbl_sync_time = tk.Label(
            right, text="正在探测本机语言服务进程...", bg="#120914", fg=FG_MUTED, font=("Consolas", 9)
        )
        self.lbl_sync_time.pack(anchor="e", pady=(0, 4))

        self.btn_refresh = tk.Button(
            right,
            text="🔃 立即全量刷新",
            command=lambda: self.refresh_all_async(silent=False),
            bg=BG_CARD_ALT,
            fg=FG_TEXT,
            activebackground=ACCENT_CRIMSON,
            activeforeground="#ffffff",
            relief="flat",
            padx=12,
            pady=4,
            font=("Microsoft YaHei UI", 9, "bold"),
            cursor="hand2",
        )
        self.btn_refresh.pack(anchor="e")

    def _build_tabs(self) -> None:
        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill=tk.BOTH, expand=True, padx=14, pady=8)

        self.tab_hub = tk.Frame(self.notebook, bg=BG_ROOT)
        self.tab_ledger = tk.Frame(self.notebook, bg=BG_ROOT)
        self.tab_diag = tk.Frame(self.notebook, bg=BG_ROOT)

        self.notebook.add(self.tab_hub, text="🔭 配额与内核守卫总览 (Overview)")
        self.notebook.add(self.tab_ledger, text="📒 Token 账单与黑盒验钞机 (Ledger)")
        self.notebook.add(self.tab_diag, text="🩺 旁路风控与守护日志 (Diagnostics)")

        self._build_hub_tab()
        self._build_ledger_tab()
        self._build_diag_tab()

    def _make_stat_card(self, parent: tk.Widget, heading: str) -> Tuple[tk.Frame, tk.Label, tk.Label]:
        card = tk.Frame(parent, bg=BG_CARD, highlightthickness=1, highlightbackground=BORDER_DIM, padx=14, pady=10)
        tk.Label(card, text=heading, bg=BG_CARD, fg=FG_MUTED, font=("Microsoft YaHei UI", 9)).pack(anchor="w")
        main_lbl = tk.Label(
            card, text="--", bg=BG_CARD, fg=FG_TEXT, font=("Microsoft YaHei UI", 12, "bold"), pady=3
        )
        main_lbl.pack(anchor="w")
        sub_lbl = tk.Label(
            card,
            text="--",
            bg=BG_CARD,
            fg=FG_MUTED,
            font=("Consolas", 9),
            wraplength=self._px(310),
            justify="left",
        )
        sub_lbl.pack(anchor="w", fill=tk.X)
        card.bind(
            "<Configure>",
            lambda e, lbl=sub_lbl: lbl.configure(wraplength=max(180, int(e.width) - 28)),
        )
        return card, main_lbl, sub_lbl

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
        cards_row = tk.Frame(self.tab_hub, bg=BG_ROOT)
        cards_row.pack(fill=tk.X, pady=(6, 8))
        for col in range(3):
            cards_row.columnconfigure(col, weight=1, uniform="hub_card")

        c1, self.lbl_ipc_main, self.lbl_ipc_sub = self._make_stat_card(cards_row, "本地语言服务拓扑 (127.0.0.1)")
        c1.grid(row=0, column=0, sticky="nsew", padx=(0, 6))

        c2, self.lbl_tier_main, self.lbl_tier_sub = self._make_stat_card(cards_row, "真实订阅层级与拥堵体征")
        c2.grid(row=0, column=1, sticky="nsew", padx=6)

        c3, self.lbl_guard_main, self.lbl_guard_sub = self._make_stat_card(cards_row, "Acerola 零轮询内核进程守卫")
        c3.grid(row=0, column=2, sticky="nsew", padx=(6, 0))

        # Quota Grid (2x2)
        q_frame = tk.LabelFrame(
            self.tab_hub,
            text="  📊 实时双通道配额水位 (7位浮点直读 · 本地回环只读查询)  ",
            bg=BG_ROOT,
            fg=ACCENT_ROSE,
            font=("Microsoft YaHei UI", 10, "bold"),
            bd=1,
            relief="solid",
            padx=10,
            pady=6,
        )
        q_frame.pack(fill=tk.X, pady=(0, 8))
        q_frame.columnconfigure(0, weight=1, uniform="q_col")
        q_frame.columnconfigure(1, weight=1, uniform="q_col")

        bh = self._px(12)
        self.bar_g5 = QuotaBarWidget(q_frame, "Gemini 5小时滚动池 (gemini-5h)", bar_height=bh)
        self.bar_g5.grid(row=0, column=0, sticky="ew", padx=(0, 6), pady=4)
        self.bar_gw = QuotaBarWidget(q_frame, "Gemini 周总额度上限池 (gemini-weekly)", bar_height=bh)
        self.bar_gw.grid(row=0, column=1, sticky="ew", padx=(6, 0), pady=4)
        self.bar_p5 = QuotaBarWidget(q_frame, "Claude / GPT 5小时滚动池 (3p-5h)", bar_height=bh)
        self.bar_p5.grid(row=1, column=0, sticky="ew", padx=(0, 6), pady=4)
        self.bar_pw = QuotaBarWidget(q_frame, "Claude / GPT 周总额度上限池 (3p-weekly)", bar_height=bh)
        self.bar_pw.grid(row=1, column=1, sticky="ew", padx=(6, 0), pady=4)

        # Action Bar: Clean 1x3 Responsive Grid
        act_frame = tk.LabelFrame(
            self.tab_hub,
            text="  ⚔️ Acerola 内核退出守卫与水位快照控制  ",
            bg=BG_ROOT,
            fg=ACCENT_CYAN,
            font=("Microsoft YaHei UI", 10, "bold"),
            bd=1,
            relief="solid",
            padx=10,
            pady=8,
        )
        act_frame.pack(fill=tk.X, pady=(0, 8))
        for col in range(3):
            act_frame.columnconfigure(col, weight=1, uniform="orion_act")

        btn_specs = [
            (
                "🛡️ 启用并注册开机自启守卫",
                "#047857",
                lambda: self._run_action("部署断头台内核守卫", install_guard_autostart),
            ),
            (
                "⏹️ 停用并卸载断头台守卫",
                "#334155",
                lambda: self._run_action("停用断头台内核守卫", uninstall_guard_autostart),
            ),
            (
                "🔍 立即采样配额水位快照",
                ACCENT_CRIMSON,
                lambda: self._run_action("采样配额水位快照", self._sample_watermark_now),
            ),
        ]
        self._action_buttons.clear()
        for idx, (label, color, cb) in enumerate(btn_specs):
            b = tk.Button(
                act_frame,
                text=label,
                command=cb,
                bg=color,
                fg="#ffffff",
                disabledforeground="#94a3b8",
                activebackground=ACCENT_ROSE,
                activeforeground="#ffffff",
                relief="flat",
                padx=10,
                pady=6,
                font=("Microsoft YaHei UI", 9, "bold"),
                cursor="hand2",
            )
            b.grid(row=0, column=idx, sticky="ew", padx=5, pady=4)
            self._action_buttons.append(b)

        log_box = tk.Frame(self.tab_hub, bg=BG_CARD, highlightthickness=1, highlightbackground=BORDER_DIM)
        log_box.pack(fill=tk.BOTH, expand=True)
        self.txt_console = scrolledtext.ScrolledText(
            log_box,
            bg="#09070c",
            fg="#e2e8f0",
            insertbackground="#ffffff",
            font=("Consolas", 9),
            relief="flat",
            height=7,
        )
        self.txt_console.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)
        self._append_console("✦ Acerola-Orion 控制台已就绪。正在读取本机 127.0.0.1 状态与本地 Token 账本...")

    def _create_scrolled_treeview(
        self,
        parent: tk.Widget,
        columns: Sequence[str],
        headers: Sequence[Tuple[str, str, int, str]],
        height: int,
        expand: bool = False,
    ) -> ttk.Treeview:
        wrap = tk.Frame(parent, bg=BG_ROOT)
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
        self.lbl_auditor_banner.pack(fill=tk.X, pady=(6, 6))
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
        self.lbl_diag_summary.pack(fill=tk.X, pady=(8, 8))

        self.txt_diag = scrolledtext.ScrolledText(
            self.tab_diag,
            bg="#09070c",
            fg="#cbd5e1",
            font=("Consolas", 9),
            relief="flat",
        )
        self.txt_diag.pack(fill=tk.BOTH, expand=True)

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
        if self.auto_refresh_ms > 0 and not self._closing:
            self.after(self._next_auto_delay_ms(), self._on_auto_timer)

    def _next_auto_delay_ms(self) -> int:
        """15s while online; offline / logged-out backs off 30s -> 60s -> 120s to prevent idle polling."""
        base = int(self.auto_refresh_ms)
        streak = int(getattr(self, "_offline_streak", 0))
        if streak <= 0:
            return base
        return int(min(max(base, 120_000), base * (2 ** min(streak, 3))))

    def _on_auto_timer(self) -> None:
        if self._closing:
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
                guard_lines = read_shared_file_lines(get_guillotine_log_path(), max_tail_bytes=64 * 1024)[-30:]
                ls_tail = read_shared_file_lines(get_ls_log_path(), max_tail_bytes=128 * 1024)[-40:]

                def _apply_if_current() -> None:
                    if int(self.__dict__.get("_refresh_gen", 0)) != my_gen:
                        return
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
        self.lbl_sync_time.configure(text=f"最后同步: {r.captured_at} | LS PID={r.ls_pid or '离线'}")

        # Card 1: Local IPC Topology
        if not r.ls_running:
            self.lbl_ipc_main.configure(text="⚪ 离线 (客户端未启动)", fg=FG_MUTED)
            self.lbl_ipc_sub.configure(text="打开 Antigravity 后自动探活")
        else:
            ver_txt = f"v{r.ide_version}" if r.ide_version else "在线"
            self.lbl_ipc_main.configure(text=f"🟢 在线 ({ver_txt})", fg=ACCENT_GREEN)
            self.lbl_ipc_sub.configure(
                text=f"回环: 127.0.0.1:{r.http_port} | 主PID={r.main_pids}\n代理: {r.proxy_mode}"
            )

        # Card 2: Tier & Congestion
        if r.ls_running:
            tier_txt = r.paid_tier_name or r.paid_tier_id or r.user_tier_id or "未知 Tier"
            self.lbl_tier_main.configure(text=f"👑 {tier_txt}", fg=ACCENT_CYAN)
            sc = r.log_stats.sidecar
            self.lbl_tier_sub.configure(
                text=f"429/503限流: {r.log_stats.http_429_503_count} 次 | 排队: {r.log_stats.capacity_exhausted_count} 次\n"
                f"旁路审查均值: {sc.avg_ms:.0f} ms (超时 {sc.timeout_count} 次)"
            )
        else:
            self.lbl_tier_main.configure(text="--", fg=FG_MUTED)
            self.lbl_tier_sub.configure(text="--")

        # Card 3: Guard
        g = r.guard
        if g.mutex_active:
            self.lbl_guard_main.configure(text="🟢 断头台守卫运行中 (0% CPU)", fg=ACCENT_GREEN)
        else:
            self.lbl_guard_main.configure(text="⚪ 断头台守卫未启动", fg=ACCENT_AMBER)
        leg_warn = f" | ⚠️旧脚本PID:{g.legacy_pids}" if g.legacy_buggy_killer_active else ""
        self.lbl_guard_sub.configure(
            text=f"开机自启: {'已注册' if g.scheduled_task_installed else '未注册'} | 残留hub: {r.stale_hub_files} 个{leg_warn}"
        )

        # Quota Bars
        bmap = {b.bucket_id: b for b in r.quota_buckets}
        missing_msg = "客户端未运行" if not r.ls_running else "暂无配额数据"
        for bid, bar_widget in (
            ("gemini-5h", self.bar_g5),
            ("gemini-weekly", self.bar_gw),
            ("3p-5h", self.bar_p5),
            ("3p-weekly", self.bar_pw),
        ):
            if bid in bmap:
                bar_widget.update_bucket(bmap[bid].remaining_fraction, bmap[bid].reset_countdown)
            else:
                bar_widget.update_bucket(math.nan, missing_msg)

        # Ledger Tab
        today_str = datetime.now().strftime("%Y-%m-%d")
        today_row = next((d for d in daily if d.local_date == today_str), None)
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

        if not silent:
            def _pct_txt(bid: str) -> str:
                b = bmap.get(bid)
                v = getattr(b, "remaining_pct", math.nan) if b is not None else math.nan
                return f"{v:.2f}%" if isinstance(v, (int, float)) and math.isfinite(v) else "--.--%"

            self._append_console(
                f"✓ 采样完成: Tier={r.paid_tier_id or r.user_tier_id or '-'} "
                f"Gemini-5h={_pct_txt('gemini-5h')} "
                f"Weekly={_pct_txt('gemini-weekly')}"
            )


def launch_panel() -> int:
    _enable_high_dpi()
    if _activate_existing_panel_window():
        return 0
    app = AcerolaOrionApp()
    app.mainloop()
    return 0
