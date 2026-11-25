# -*- coding: utf-8 -*-
"""
Acerola-Orion 任务拆解与交接单接力看板页签 (`acerola_orion/plugins/task_board_tab.py`)
- 独立 `TaskBoardTab(tk.Frame)` 组件，零循环导入 `panel.py`，纯 stdlib + Tkinter/ttk
- 三段黑曜石交互：工作区白名单与口令栏 / 任务包与原子卡片联动表 / 交接单接力表
- 后台 daemon 线程扫描 + `ui_call` 投递回主线程，销毁后全路径防 `TclError`
"""

from __future__ import annotations

from datetime import datetime
import os
import threading
import tkinter as tk
from tkinter import filedialog, ttk
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .workspace_prefs import add_task_workspace, get_task_workspaces, remove_task_workspace
from .task_board import (
    CheckpointItem,
    TaskBoardSnapshot,
    TaskCardItem,
    TaskPackItem,
    build_decompose_prompt,
    build_execute_task_prompt,
    build_handoff_prompt,
    build_resume_checkpoint_prompt,
    scan_task_board,
)

_DEFAULT_PALETTE: Dict[str, str] = {
    "BG_ROOT": "#0c0a10",
    "BG_CARD": "#15111c",
    "BG_CARD_ALT": "#1d1726",
    "FG_TEXT": "#f8fafc",
    "FG_MUTED": "#94a3b8",
    "ACCENT_CRIMSON": "#e11d48",
    "ACCENT_ROSE": "#fb7185",
    "ACCENT_CYAN": "#38bdf8",
    "ACCENT_GREEN": "#10b981",
    "ACCENT_AMBER": "#f59e0b",
    "BORDER_DIM": "#2e243d",
}

_CHECKPOINT_KIND_LABELS: Dict[str, str] = {
    "handoff": "📦 阶段交接",
    "cold_rescue": "🧊 冷启救援",
    "note": "📝 笔记",
}


def _short_ws_name(ws: str) -> str:
    cleaned = str(ws or "").rstrip("\\/")
    return os.path.basename(cleaned) or cleaned


def _format_epoch_short(epoch: float) -> str:
    if not epoch or epoch <= 0:
        return ""
    try:
        return datetime.fromtimestamp(epoch).strftime("%m-%d %H:%M")
    except (OSError, OverflowError, ValueError):
        return ""


class TaskBoardTab(tk.Frame):
    """Independent Tab component for Task Decomposer packs and Stage Handoff checkpoints."""

    def __init__(
        self,
        master: tk.Misc,
        *,
        px: Callable[[int], int],
        copy_callback: Callable[..., str],
        log_callback: Callable[[str], None],
        ui_call: Callable[[Callable[[], None]], None],
        palette: Dict[str, str],
    ) -> None:
        self._palette: Dict[str, str] = dict(_DEFAULT_PALETTE)
        if palette:
            self._palette.update({k: str(v) for k, v in palette.items() if v})
        super().__init__(master, bg=self._palette["BG_ROOT"])

        self._px = px
        self._copy_callback = copy_callback
        self._log_callback = log_callback
        self._ui_call = ui_call

        self._destroyed: bool = False
        self._refresh_busy: bool = False
        self._refresh_gen: int = 0
        self._pending_refresh: bool = False

        self._workspaces: List[str] = []
        self._snapshot: Optional[TaskBoardSnapshot] = None
        self._packs_by_iid: Dict[str, TaskPackItem] = {}
        self._cards_by_iid: Dict[str, TaskCardItem] = {}
        self._checkpoints_by_iid: Dict[str, CheckpointItem] = {}
        self._selected_pack_dir: str = ""

        self._ws_var = tk.StringVar(value="")
        self._req_var = tk.StringVar(value="")

        self.bind("<Destroy>", self._on_destroy, add="+")
        self._ensure_local_styles()
        self._build_ui()
        self._load_initial_workspaces()

    # ------------------------------------------------------------------
    # Lifecycle & safe helpers
    # ------------------------------------------------------------------

    def _on_destroy(self, event: Optional[tk.Event] = None) -> None:
        if event is None or event.widget is self:
            self._destroyed = True

    def _is_alive(self) -> bool:
        if self._destroyed:
            return False
        try:
            return bool(self.winfo_exists())
        except tk.TclError:
            self._destroyed = True
            return False

    def _safe_log(self, msg: str) -> None:
        if not msg:
            return
        try:
            self._log_callback(msg)
        except Exception:
            pass

    def _safe_copy(self, prompt: str, toast: str, console_msg: str) -> str:
        try:
            return str(self._copy_callback(prompt, toast, console_msg) or prompt)
        except TypeError:
            try:
                return str(self._copy_callback(prompt) or prompt)
            except Exception:
                return prompt
        except Exception:
            return prompt

    def _set_feedback(self, text: str, color: Optional[str] = None) -> None:
        if not self._is_alive():
            return
        try:
            fg = color or self._palette["ACCENT_CYAN"]
            self.lbl_feedback.configure(text=text, fg=fg)
        except tk.TclError:
            pass

    def _ensure_local_styles(self) -> None:
        p = self._palette
        try:
            style = ttk.Style(self)
            style.configure(
                "Orion.TCombobox",
                fieldbackground="#09070c",
                background=p["BG_CARD_ALT"],
                foreground=p["FG_TEXT"],
                arrowcolor=p["ACCENT_ROSE"],
                bordercolor=p["BORDER_DIM"],
                lightcolor=p["BORDER_DIM"],
                darkcolor=p["BORDER_DIM"],
                selectbackground="#4c0519",
                selectforeground="#ffffff",
            )
            style.map(
                "Orion.TCombobox",
                fieldbackground=[("readonly", "#09070c"), ("disabled", p["BG_CARD"])],
                foreground=[("readonly", p["FG_TEXT"]), ("disabled", p["FG_MUTED"])],
                selectbackground=[("readonly", "#09070c")],
                selectforeground=[("readonly", p["FG_TEXT"])],
            )
            # Standalone fallback when TaskBoardTab is mounted outside AcerolaOrionApp
            if not style.configure("Orion.Treeview"):
                style.configure(
                    "Orion.Treeview",
                    background=p["BG_CARD"],
                    fieldbackground=p["BG_CARD"],
                    foreground=p["FG_TEXT"],
                    rowheight=self._px(26),
                    borderwidth=0,
                    font=("Consolas", 9),
                )
                style.configure(
                    "Orion.Treeview.Heading",
                    background=p["BG_CARD_ALT"],
                    foreground=p["ACCENT_ROSE"],
                    font=("Microsoft YaHei UI", 9, "bold"),
                    relief="flat",
                )
                style.map("Orion.Treeview", background=[("selected", "#4c0519")], foreground=[("selected", "#ffffff")])
        except Exception:
            pass

    def _create_scrolled_treeview(
        self,
        parent: tk.Widget,
        columns: Sequence[str],
        headers: Sequence[Tuple[str, str, int, str]],
        height: int,
    ) -> Tuple[tk.Frame, ttk.Treeview]:
        p = self._palette
        wrap = tk.Frame(parent, bg=p["BG_ROOT"])
        wrap.columnconfigure(0, weight=1)
        wrap.rowconfigure(0, weight=1)

        tree = ttk.Treeview(wrap, columns=columns, show="headings", height=height, style="Orion.Treeview")
        vsb = ttk.Scrollbar(wrap, orient="vertical", command=tree.yview, style="Orion.Vertical.TScrollbar")
        hsb = ttk.Scrollbar(wrap, orient="horizontal", command=tree.xview, style="Orion.Horizontal.TScrollbar")
        tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)

        for cid, text, base_w, anchor in headers:
            tree.heading(cid, text=text)
            tree.column(cid, width=self._px(base_w), minwidth=self._px(50), anchor=anchor, stretch=True)

        tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        return wrap, tree

    # ------------------------------------------------------------------
    # UI Construction (3 sections)
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        p = self._palette

        # Status / feedback banner
        self.lbl_feedback = tk.Label(
            self,
            text="📋 任务拆解与交接单接力看板已就绪 · 点击刷新扫描 .agents/tasks 与 .agents/checkpoints",
            bg=p["BG_CARD_ALT"],
            fg=p["ACCENT_CYAN"],
            font=("Microsoft YaHei UI", 9, "bold"),
            anchor="w",
            justify="left",
            padx=12,
            pady=7,
        )
        self.lbl_feedback.pack(fill=tk.X, pady=(6, 6))
        self.bind("<Configure>", self._on_resize, add="+")

        # Section 1: Workspace whitelist + Requirement input + Prompt buttons
        ctrl = tk.Frame(
            self,
            bg=p["BG_CARD"],
            highlightthickness=1,
            highlightbackground=p["BORDER_DIM"],
            padx=10,
            pady=8,
        )
        ctrl.pack(fill=tk.X, pady=(0, 8))

        row1 = tk.Frame(ctrl, bg=p["BG_CARD"])
        row1.pack(fill=tk.X, pady=(0, 6))

        tk.Label(
            row1,
            text="工作区:",
            bg=p["BG_CARD"],
            fg=p["FG_MUTED"],
            font=("Microsoft YaHei UI", 9, "bold"),
        ).pack(side=tk.LEFT, padx=(0, 6))

        self.ws_combo = ttk.Combobox(
            row1,
            textvariable=self._ws_var,
            state="readonly",
            style="Orion.TCombobox",
            font=("Consolas", 9),
        )
        self.ws_combo.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 8))
        self.ws_combo.bind("<<ComboboxSelected>>", self._on_workspace_selected)

        btn_add_ws = tk.Button(
            row1,
            text="➕ 添加",
            command=self._on_add_workspace_click,
            bg="#1e293b",
            fg=p["FG_TEXT"],
            activebackground="#334155",
            activeforeground="#ffffff",
            relief="flat",
            padx=10,
            pady=3,
            font=("Microsoft YaHei UI", 9),
            cursor="hand2",
        )
        btn_add_ws.pack(side=tk.LEFT, padx=(0, 6))

        btn_del_ws = tk.Button(
            row1,
            text="➖ 移除",
            command=self.remove_selected_workspace,
            bg="#26131f",
            fg=p["FG_MUTED"],
            activebackground=p["ACCENT_CRIMSON"],
            activeforeground="#ffffff",
            relief="flat",
            padx=10,
            pady=3,
            font=("Microsoft YaHei UI", 9),
            cursor="hand2",
        )
        btn_del_ws.pack(side=tk.LEFT, padx=(0, 8))

        btn_refresh = tk.Button(
            row1,
            text="🔄 刷新看板",
            command=lambda: self.refresh_async(silent=False),
            bg=p["BG_CARD_ALT"],
            fg=p["FG_TEXT"],
            activebackground=p["ACCENT_CRIMSON"],
            activeforeground="#ffffff",
            relief="flat",
            padx=12,
            pady=3,
            font=("Microsoft YaHei UI", 9, "bold"),
            cursor="hand2",
        )
        btn_refresh.pack(side=tk.RIGHT)

        row2 = tk.Frame(ctrl, bg=p["BG_CARD"])
        row2.pack(fill=tk.X)

        tk.Label(
            row2,
            text="需求/主题:",
            bg=p["BG_CARD"],
            fg=p["FG_MUTED"],
            font=("Microsoft YaHei UI", 9, "bold"),
        ).pack(side=tk.LEFT, padx=(0, 6))

        self.req_entry = tk.Entry(
            row2,
            textvariable=self._req_var,
            bg="#09070c",
            fg=p["FG_TEXT"],
            insertbackground=p["FG_TEXT"],
            relief="flat",
            font=("Microsoft YaHei UI", 9),
        )
        self.req_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 8), ipady=4)
        self.req_entry.bind("<Return>", lambda _e: self.copy_decompose_command())

        btn_decompose = tk.Button(
            row2,
            text="🧭 复制探索拆解口令",
            command=self.copy_decompose_command,
            bg=p["ACCENT_CRIMSON"],
            fg="#ffffff",
            activebackground="#be123c",
            activeforeground="#ffffff",
            relief="flat",
            padx=12,
            pady=4,
            font=("Microsoft YaHei UI", 9, "bold"),
            cursor="hand2",
        )
        btn_decompose.pack(side=tk.LEFT, padx=(0, 6))

        btn_handoff = tk.Button(
            row2,
            text="📦 复制写交接单口令",
            command=self.copy_handoff_command,
            bg="#4c1d95",
            fg="#ffffff",
            activebackground="#5b21b6",
            activeforeground="#ffffff",
            relief="flat",
            padx=12,
            pady=4,
            font=("Microsoft YaHei UI", 9, "bold"),
            cursor="hand2",
        )
        btn_handoff.pack(side=tk.LEFT)

        # Main split container: Middle (packs + cards) & Bottom (checkpoints)
        body = tk.Frame(self, bg=p["BG_ROOT"])
        body.pack(fill=tk.BOTH, expand=True)
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=3)
        body.rowconfigure(1, weight=2)

        # Section 2: Task Packs & Atomic Cards
        mid = tk.Frame(body, bg=p["BG_ROOT"])
        mid.grid(row=0, column=0, sticky="nsew", pady=(0, 6))
        mid.columnconfigure(0, weight=2)
        mid.columnconfigure(1, weight=3)
        mid.rowconfigure(1, weight=1)

        mid_hdr = tk.Frame(mid, bg=p["BG_ROOT"])
        mid_hdr.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 3))
        tk.Label(
            mid_hdr,
            text="📦 拆解任务包与原子卡片（左选任务包联动右侧卡片 · 双击未完成卡直接复制执行指令）",
            bg=p["BG_ROOT"],
            fg=p["ACCENT_ROSE"],
            font=("Microsoft YaHei UI", 9, "bold"),
        ).pack(side=tk.LEFT)
        tk.Button(
            mid_hdr,
            text="📋 复制选中卡片执行指令",
            command=self.copy_selected_task_command,
            bg="#1e3a8a",
            fg="#ffffff",
            activebackground="#1d4ed8",
            activeforeground="#ffffff",
            relief="flat",
            padx=10,
            pady=2,
            font=("Microsoft YaHei UI", 9, "bold"),
            cursor="hand2",
        ).pack(side=tk.RIGHT)

        pack_cols = ("progress", "ready", "title", "workspace")
        pack_headers = [
            ("progress", "进度", 70, "center"),
            ("ready", "可启动", 72, "center"),
            ("title", "任务包标题", 220, "w"),
            ("workspace", "工作区", 95, "w"),
        ]
        packs_wrap, self.tree_packs = self._create_scrolled_treeview(mid, pack_cols, pack_headers, height=7)
        packs_wrap.grid(row=1, column=0, sticky="nsew", padx=(0, 6))
        self.tree_packs.bind("<<TreeviewSelect>>", self._on_pack_selected)

        card_cols = ("status", "task_id", "deps", "group", "model", "goal")
        card_headers = [
            ("status", "状态", 105, "center"),
            ("task_id", "编号", 55, "center"),
            ("deps", "依赖", 80, "center"),
            ("group", "并行组", 60, "center"),
            ("model", "建议模型", 80, "center"),
            ("goal", "一句话目标", 260, "w"),
        ]
        cards_wrap, self.tree_cards = self._create_scrolled_treeview(mid, card_cols, card_headers, height=7)
        cards_wrap.grid(row=1, column=1, sticky="nsew")
        self.tree_cards.tag_configure("done", foreground=p["ACCENT_GREEN"])
        self.tree_cards.tag_configure("ready", foreground=p["ACCENT_CYAN"])
        self.tree_cards.tag_configure("in_progress", foreground=p["ACCENT_AMBER"])
        self.tree_cards.tag_configure("blocked", foreground=p["FG_MUTED"])
        self.tree_cards.bind("<Double-1>", self._on_card_double_click)

        # Section 3: Checkpoints / Handoff Relay List
        btm = tk.Frame(body, bg=p["BG_ROOT"])
        btm.grid(row=1, column=0, sticky="nsew")
        btm.columnconfigure(0, weight=1)
        btm.rowconfigure(1, weight=1)

        btm_hdr = tk.Frame(btm, bg=p["BG_ROOT"])
        btm_hdr.grid(row=0, column=0, sticky="ew", pady=(0, 3))
        tk.Label(
            btm_hdr,
            text="🔖 阶段交接单与冷启救援列表（.agents/checkpoints/*.md · 双击或点击按钮复制新窗接力指令）",
            bg=p["BG_ROOT"],
            fg=p["ACCENT_CYAN"],
            font=("Microsoft YaHei UI", 9, "bold"),
        ).pack(side=tk.LEFT)
        tk.Button(
            btm_hdr,
            text="🚀 复制选中交接单接力指令",
            command=self.copy_selected_checkpoint_command,
            bg="#065f46",
            fg="#ffffff",
            activebackground="#047857",
            activeforeground="#ffffff",
            relief="flat",
            padx=10,
            pady=2,
            font=("Microsoft YaHei UI", 9, "bold"),
            cursor="hand2",
        ).pack(side=tk.RIGHT)

        ckpt_cols = ("kind", "created_at", "watermark", "title", "filename", "workspace")
        ckpt_headers = [
            ("kind", "类型", 98, "center"),
            ("created_at", "落盘时间", 128, "center"),
            ("watermark", "水位", 85, "center"),
            ("title", "主题 / 标题", 280, "w"),
            ("filename", "文件名", 215, "w"),
            ("workspace", "工作区", 100, "w"),
        ]
        ckpt_wrap, self.tree_checkpoints = self._create_scrolled_treeview(btm, ckpt_cols, ckpt_headers, height=5)
        ckpt_wrap.grid(row=1, column=0, sticky="nsew")
        self.tree_checkpoints.tag_configure("handoff", foreground=p["ACCENT_CYAN"])
        self.tree_checkpoints.tag_configure("cold_rescue", foreground=p["ACCENT_AMBER"])
        self.tree_checkpoints.tag_configure("note", foreground=p["FG_MUTED"])
        self.tree_checkpoints.bind("<Double-1>", self._on_checkpoint_double_click)

    def _on_resize(self, event: tk.Event) -> None:
        if not self._is_alive():
            return
        try:
            self.lbl_feedback.configure(wraplength=max(380, int(event.width) - 36))
        except tk.TclError:
            pass

    # ------------------------------------------------------------------
    # Workspace management
    # ------------------------------------------------------------------

    def _load_initial_workspaces(self) -> None:
        try:
            ws_list = get_task_workspaces()
        except Exception:
            ws_list = []
        self._sync_workspace_choices(ws_list)

    def _sync_workspace_choices(self, workspaces: Sequence[str], prefer: str = "") -> None:
        if not self._is_alive():
            return
        clean_list = [str(w).strip() for w in workspaces if str(w).strip()]
        self._workspaces = clean_list
        cur = self._ws_var.get().strip()
        target = ""
        if prefer:
            pref_nc = os.path.normcase(prefer)
            for w in clean_list:
                if os.path.normcase(w) == pref_nc:
                    target = w
                    break
        if not target and cur:
            cur_nc = os.path.normcase(cur)
            for w in clean_list:
                if os.path.normcase(w) == cur_nc:
                    target = w
                    break
        if not target and clean_list:
            target = clean_list[0]
        try:
            self.ws_combo.configure(values=tuple(clean_list))
            self._ws_var.set(target)
        except tk.TclError:
            pass

    def _get_active_workspace(self) -> str:
        cur = self._ws_var.get().strip()
        if cur:
            return cur
        if self._workspaces:
            return self._workspaces[0]
        try:
            ws_list = get_task_workspaces()
            if ws_list:
                self._sync_workspace_choices(ws_list)
                return ws_list[0]
        except Exception:
            pass
        return ""

    def _on_workspace_selected(self, _event: Optional[tk.Event] = None) -> None:
        ws = self._ws_var.get().strip()
        if ws:
            self._set_feedback(
                f"📂 当前目标工作区：{ws}（探索拆解与写交接单口令将绑定此路径）",
                color=self._palette["ACCENT_CYAN"],
            )

    def _on_add_workspace_click(self) -> None:
        if not self._is_alive():
            return
        initial = self._get_active_workspace() or os.getcwd()
        try:
            chosen = filedialog.askdirectory(
                parent=self,
                title="选择要监控的工程工作区",
                initialdir=initial if os.path.isdir(initial) else None,
            )
        except tk.TclError:
            return
        if not chosen:
            return
        self.add_workspace(chosen)

    def add_workspace(self, path: Optional[str] = None) -> Dict[str, Any]:
        raw = str(path or "").strip()
        if not raw:
            msg = "未提供工作区路径。"
            self._set_feedback(f"⚠️ {msg}", color=self._palette["ACCENT_AMBER"])
            return {
                "ok": False,
                "workspace": "",
                "workspaces": list(self._workspaces),
                "added": False,
                "changed": False,
                "message": msg,
            }
        res = add_task_workspace(raw)
        ws_list = res.get("workspaces")
        if isinstance(ws_list, list):
            self._sync_workspace_choices(ws_list, prefer=str(res.get("workspace") or raw))
        msg = str(res.get("message") or ("工作区已添加" if res.get("ok") else "添加工作区失败"))
        ok = bool(res.get("ok"))
        self._set_feedback(
            f"{'✓' if ok else '⚠️'} {msg}",
            color=self._palette["ACCENT_GREEN"] if ok else self._palette["ACCENT_AMBER"],
        )
        self._safe_log(f"{'✓' if ok else '⚠️'} [任务看板] {msg}")
        if ok:
            self.refresh_async(silent=True)
        return res

    def remove_selected_workspace(self) -> Dict[str, Any]:
        ws = self._ws_var.get().strip()
        if not ws:
            msg = "当前未选中可移除的工作区。"
            self._set_feedback(f"⚠️ {msg}", color=self._palette["ACCENT_AMBER"])
            return {
                "ok": False,
                "workspace": "",
                "workspaces": list(self._workspaces),
                "removed": False,
                "changed": False,
                "message": msg,
            }
        res = remove_task_workspace(ws)
        ws_list = res.get("workspaces")
        if isinstance(ws_list, list):
            self._sync_workspace_choices(ws_list)
        msg = str(res.get("message") or ("已移除工作区" if res.get("ok") else "移除工作区失败"))
        ok = bool(res.get("ok"))
        self._set_feedback(
            f"{'✓' if ok else '⚠️'} {msg}",
            color=self._palette["ACCENT_GREEN"] if ok else self._palette["ACCENT_AMBER"],
        )
        self._safe_log(f"{'✓' if ok else '⚠️'} [任务看板] {msg}")
        if ok:
            self.refresh_async(silent=True)
        return res

    # ------------------------------------------------------------------
    # Async scan & snapshot rendering
    # ------------------------------------------------------------------

    def refresh_async(self, silent: bool = False) -> None:
        if not self._is_alive():
            return
        if self._refresh_busy:
            if not silent:
                self._pending_refresh = True
            return
        self._refresh_busy = True
        self._refresh_gen += 1
        my_gen = self._refresh_gen
        if not silent:
            self._set_feedback("🔄 正在只读扫描白名单工作区的任务包与交接单...", color=self._palette["ACCENT_CYAN"])

        def worker() -> None:
            try:
                workspaces = get_task_workspaces()
                snap = scan_task_board(workspaces, max_checkpoints=30)

                def _deliver() -> None:
                    if not self._is_alive() or self._refresh_gen != my_gen:
                        return
                    self.apply_snapshot(snap)
                    if not silent:
                        self._safe_log(
                            f"✦ 任务看板已刷新: {len(snap.workspaces)} 个工作区 / "
                            f"{len(snap.packs)} 个任务包 / {len(snap.checkpoints)} 份交接单"
                        )

                self._ui_call(_deliver)
            except Exception as exc:
                err_text = str(exc)

                def _on_err() -> None:
                    if not self._is_alive() or self._refresh_gen != my_gen:
                        return
                    self._set_feedback(f"✗ 任务看板扫描异常: {err_text}", color=self._palette["ACCENT_ROSE"])
                    if not silent:
                        self._safe_log(f"✗ 任务看板扫描异常: {err_text}")

                self._ui_call(_on_err)
            finally:
                def _finish() -> None:
                    if not self._is_alive() or self._refresh_gen != my_gen:
                        return
                    self._refresh_busy = False
                    if self._pending_refresh:
                        self._pending_refresh = False
                        self.refresh_async(silent=False)

                self._ui_call(_finish)

        threading.Thread(target=worker, daemon=True, name="orion-task-board-scan").start()

    def apply_snapshot(self, snapshot: TaskBoardSnapshot) -> None:
        if not self._is_alive():
            return
        self._snapshot = snapshot
        prev_pack_nc = os.path.normcase(self._selected_pack_dir) if self._selected_pack_dir else ""

        try:
            self._sync_workspace_choices(snapshot.workspaces)

            # 1) Populate Task Packs
            self.tree_packs.delete(*self.tree_packs.get_children())
            self._packs_by_iid.clear()
            target_pack_iid = ""
            first_pack_iid = ""

            for idx, pack in enumerate(snapshot.packs):
                iid = f"pack_{idx}"
                prog = f"{pack.done_count}/{pack.total_count}"
                if pack.ready_count > 0:
                    ready_lbl = f"🟢 {pack.ready_count} 可干"
                elif pack.total_count > 0 and pack.done_count >= pack.total_count:
                    ready_lbl = "✅ 完成"
                else:
                    ready_lbl = "0"
                self.tree_packs.insert(
                    "",
                    tk.END,
                    iid=iid,
                    values=(prog, ready_lbl, pack.title or pack.pack_id, _short_ws_name(pack.workspace)),
                )
                self._packs_by_iid[iid] = pack
                if not first_pack_iid:
                    first_pack_iid = iid
                if prev_pack_nc and os.path.normcase(pack.pack_dir) == prev_pack_nc:
                    target_pack_iid = iid

            chosen_iid = target_pack_iid or first_pack_iid
            if chosen_iid and chosen_iid in self._packs_by_iid:
                self.tree_packs.selection_set(chosen_iid)
                self.tree_packs.focus(chosen_iid)
                chosen_pack = self._packs_by_iid[chosen_iid]
                self._selected_pack_dir = chosen_pack.pack_dir
                self._render_cards_for_pack(chosen_pack)
            else:
                self._selected_pack_dir = ""
                self._clear_cards()

            # 2) Populate Checkpoints
            self.tree_checkpoints.delete(*self.tree_checkpoints.get_children())
            self._checkpoints_by_iid.clear()
            for idx, ckpt in enumerate(snapshot.checkpoints):
                iid = f"ckpt_{idx}"
                kind_lbl = _CHECKPOINT_KIND_LABELS.get(ckpt.kind, ckpt.kind or "📝 笔记")
                created = ckpt.created_at or _format_epoch_short(ckpt.mtime)
                wm = ckpt.watermark or "-"
                tag = ckpt.kind if ckpt.kind in ("handoff", "cold_rescue", "note") else "note"
                self.tree_checkpoints.insert(
                    "",
                    tk.END,
                    iid=iid,
                    values=(
                        kind_lbl,
                        created,
                        wm,
                        ckpt.title or ckpt.filename,
                        ckpt.filename,
                        _short_ws_name(ckpt.workspace),
                    ),
                    tags=(tag,),
                )
                self._checkpoints_by_iid[iid] = ckpt

            # 3) Update summary banner
            ready_total = sum(pk.ready_count for pk in snapshot.packs)
            if not snapshot.workspaces:
                summary = "📋 尚未配置监控工作区 · 点击「➕ 添加」纳入工程目录后即可扫描任务包与交接单"
                color = self._palette["ACCENT_AMBER"]
            elif snapshot.warnings:
                summary = (
                    f"📋 已监控 {len(snapshot.workspaces)} 个工作区 · {len(snapshot.packs)} 个任务包"
                    f"（{ready_total} 张就绪卡）· {len(snapshot.checkpoints)} 份交接单  |  "
                    f"⚠️ {len(snapshot.warnings)} 条提示：{snapshot.warnings[0]}"
                )
                color = self._palette["ACCENT_AMBER"]
            else:
                summary = (
                    f"📋 已监控 {len(snapshot.workspaces)} 个工作区 · {len(snapshot.packs)} 个任务包"
                    f"（{ready_total} 张就绪卡）· {len(snapshot.checkpoints)} 份交接单 · 双击未完成卡片或交接单即可复制启动指令"
                )
                color = self._palette["ACCENT_CYAN"]
            self._set_feedback(summary, color=color)
        except tk.TclError:
            self._destroyed = True

    def _clear_cards(self) -> None:
        if not self._is_alive():
            return
        try:
            self.tree_cards.delete(*self.tree_cards.get_children())
        except tk.TclError:
            pass
        self._cards_by_iid.clear()

    def _render_cards_for_pack(self, pack: TaskPackItem) -> None:
        if not self._is_alive():
            return
        try:
            self.tree_cards.delete(*self.tree_cards.get_children())
            self._cards_by_iid.clear()
            for idx, card in enumerate(pack.tasks):
                iid = f"card_{idx}"
                if card.status == "done":
                    st_lbl = "✅ [x] 已验收"
                    tag = "done"
                elif card.status == "in_progress":
                    st_lbl = "🟠 [~] 依赖未满" if card.blocked_by else "🔄 [~] 进行中"
                    tag = "in_progress"
                elif card.is_ready:
                    st_lbl = "🟢 [ ] 就绪"
                    tag = "ready"
                else:
                    st_lbl = "🔒 [ ] 待依赖"
                    tag = "blocked"
                deps_lbl = ", ".join(card.depends_on) if card.depends_on else "-"
                self.tree_cards.insert(
                    "",
                    tk.END,
                    iid=iid,
                    values=(
                        st_lbl,
                        card.task_id,
                        deps_lbl,
                        card.parallel_group or "-",
                        card.recommended_model or "-",
                        card.goal,
                    ),
                    tags=(tag,),
                )
                self._cards_by_iid[iid] = card
        except tk.TclError:
            self._destroyed = True

    # ------------------------------------------------------------------
    # Selection & Copy Prompt Actions
    # ------------------------------------------------------------------

    def _on_pack_selected(self, _event: Optional[tk.Event] = None) -> None:
        if not self._is_alive():
            return
        try:
            sel = self.tree_packs.selection()
        except tk.TclError:
            return
        if not sel:
            return
        pack = self._packs_by_iid.get(sel[0])
        if pack is None:
            return
        self._selected_pack_dir = pack.pack_dir
        self._render_cards_for_pack(pack)

    def _on_card_double_click(self, event: Optional[tk.Event] = None) -> None:
        if not self._is_alive():
            return
        if event is not None:
            try:
                row_id = self.tree_cards.identify_row(event.y)
                if not row_id:
                    return
                self.tree_cards.selection_set(row_id)
                self.tree_cards.focus(row_id)
            except tk.TclError:
                return
        self.copy_selected_task_command()

    def _on_checkpoint_double_click(self, event: Optional[tk.Event] = None) -> None:
        if not self._is_alive():
            return
        if event is not None:
            try:
                row_id = self.tree_checkpoints.identify_row(event.y)
                if not row_id:
                    return
                self.tree_checkpoints.selection_set(row_id)
                self.tree_checkpoints.focus(row_id)
            except tk.TclError:
                return
        self.copy_selected_checkpoint_command()

    def copy_decompose_command(self) -> str:
        ws = self._get_active_workspace()
        if not ws:
            msg = "请先添加一个工程工作区后再生成探索拆解口令。"
            self._set_feedback(f"⚠️ {msg}", color=self._palette["ACCENT_AMBER"])
            self._safe_log(f"⚠️ [任务看板] {msg}")
            return ""
        req = self.req_entry.get().strip() if self._is_alive() else ""
        if not req:
            msg = "请先在上方「需求/主题」输入框填写要拆解的需求内容，再复制探索拆解口令。"
            self._set_feedback(f"⚠️ {msg}", color=self._palette["ACCENT_AMBER"])
            self._safe_log(f"⚠️ [任务看板] {msg}")
            try:
                self.req_entry.focus_set()
            except tk.TclError:
                pass
            return ""
        prompt = build_decompose_prompt(ws, req)
        ws_short = _short_ws_name(ws)
        toast = f"✓ 已复制探索拆解口令 ({ws_short})"
        console_msg = f"✓ 已复制工作区「{ws_short}」探索拆解口令 — 粘贴到新窗口只读生成 PLAN.md 与原子卡片。"
        copied = self._safe_copy(prompt, toast, console_msg)
        self._set_feedback(f"✓ 已复制「{ws_short}」探索拆解口令，粘贴到新窗口发送即可只读拆解。", color=self._palette["ACCENT_GREEN"])
        return copied

    def copy_handoff_command(self) -> str:
        ws = self._get_active_workspace()
        if not ws:
            msg = "请先添加一个工程工作区后再生成写交接单口令。"
            self._set_feedback(f"⚠️ {msg}", color=self._palette["ACCENT_AMBER"])
            self._safe_log(f"⚠️ [任务看板] {msg}")
            return ""
        topic = self.req_entry.get().strip() if self._is_alive() else ""
        prompt = build_handoff_prompt(ws, topic)
        ws_short = _short_ws_name(ws)
        toast = f"✓ 已复制写交接单口令 ({ws_short})"
        console_msg = f"✓ 已复制工作区「{ws_short}」写交接单口令 — 粘贴到当前窗口落盘 ≤1.5K 交接单后收口。"
        copied = self._safe_copy(prompt, toast, console_msg)
        self._set_feedback(f"✓ 已复制「{ws_short}」写交接单口令，粘贴到当前窗口发送即可落盘收口。", color=self._palette["ACCENT_GREEN"])
        return copied

    def copy_selected_task_command(self) -> str:
        if not self._is_alive():
            return ""
        try:
            sel = self.tree_cards.selection()
        except tk.TclError:
            return ""
        if not sel:
            msg = "请先在原子卡片列表中选中一张任务卡。"
            self._set_feedback(f"⚠️ {msg}", color=self._palette["ACCENT_AMBER"])
            return ""
        card = self._cards_by_iid.get(sel[0])
        if card is None:
            return ""
        if card.status == "done":
            msg = f"卡片 {card.task_id} 已验收 ([x])，无需重复复制执行指令。"
            self._set_feedback(f"✅ {msg}", color=self._palette["ACCENT_GREEN"])
            self._safe_log(f"✦ [任务看板] {msg}")
            return ""

        prompt = card.execute_prompt or build_execute_task_prompt(self._selected_pack_dir, card.task_id)
        if card.blocked_by:
            blocked_str = ", ".join(card.blocked_by)
            toast = f"⚠ 已复制 {card.task_id} 执行指令（前置依赖未完成: {blocked_str}）"
            console_msg = (
                f"⚠️ 已复制卡片 {card.task_id} 执行指令（注意前置依赖尚未验收：{blocked_str}，请确认后再启动）。"
            )
            copied = self._safe_copy(prompt, toast, console_msg)
            self._set_feedback(console_msg, color=self._palette["ACCENT_AMBER"])
            return copied

        meta = "/".join(x for x in (card.parallel_group, card.recommended_model) if x)
        meta_hint = f"（{meta}）" if meta else ""
        toast = f"✓ 已复制 {card.task_id} 执行指令 · 粘贴到新窗口启动"
        console_msg = f"✓ 已复制卡片 {card.task_id}{meta_hint} 执行指令 — 粘贴到新窗口即可精准闭环施工。"
        copied = self._safe_copy(prompt, toast, console_msg)
        self._set_feedback(console_msg, color=self._palette["ACCENT_GREEN"])
        return copied

    def copy_selected_checkpoint_command(self) -> str:
        if not self._is_alive():
            return ""
        try:
            sel = self.tree_checkpoints.selection()
        except tk.TclError:
            return ""
        if not sel:
            msg = "请先在交接单列表中选中一条交接单。"
            self._set_feedback(f"⚠️ {msg}", color=self._palette["ACCENT_AMBER"])
            return ""
        ckpt = self._checkpoints_by_iid.get(sel[0])
        if ckpt is None:
            return ""
        prompt = ckpt.resume_prompt or build_resume_checkpoint_prompt(ckpt.file_path)
        toast = f"✓ 已复制交接单接力指令 · {ckpt.filename}"
        console_msg = f"✓ 已复制交接单「{ckpt.filename}」的新窗口接力指令 — 粘贴到新窗口即可无缝续接。"
        copied = self._safe_copy(prompt, toast, console_msg)
        self._set_feedback(console_msg, color=self._palette["ACCENT_GREEN"])
        return copied
