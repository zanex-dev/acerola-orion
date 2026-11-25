# -*- coding: utf-8 -*-
"""
Version Vault Tab UI Component (`acerola_orion/plugins/version_vault_tab.py`).
- 星盘动态标签页：`🧊 版本冷库 (Archive)`
- 严格采用黑曜石暗色风格，零循环导入 `panel.py` 及其他独立子插件
- 全量文件扫描、异步封版入队、原样还原与 Git 推送均在 daemon 线程中执行，经 `ui_call` 回主线程更新
- 绑定 `<Destroy>` 拦截销毁后更新，彻底规避 `TclError`
- 物理不提供任何删除/清空冷库快照的 UI 按钮（Append-Only 只增不删安全红线）
"""

from __future__ import annotations

from datetime import datetime
import os
from pathlib import Path
import threading
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

from acerola_orion.constants import load_app_config, update_app_config

from .version_scanner import (
    ArchiveCandidate,
    CATEGORY_COMPLETED_TASK,
    CATEGORY_LEGACY_ARCHIVE,
    CATEGORY_OLD_RELEASE,
    CATEGORY_STALE_PROJECT,
    CATEGORY_VERSIONED_SIBLING,
    CATEGORY_VERSIONS_DIR,
    scan_old_versions,
)
from .version_vault import (
    VaultSnapshotRecord,
    VersionVaultStatus,
    WorkspaceVersionVault,
)

__all__ = ["VersionVaultTab"]

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

_CATEGORY_LABELS: Dict[str, str] = {
    CATEGORY_VERSIONS_DIR: "📁 版本目录",
    CATEGORY_VERSIONED_SIBLING: "📄 同级旧版",
    CATEGORY_OLD_RELEASE: "📦 旧发行包",
    CATEGORY_COMPLETED_TASK: "✅ 完结任务包",
    CATEGORY_LEGACY_ARCHIVE: "🗂 历史归档",
    CATEGORY_STALE_PROJECT: "💤 失活项目",
}

_MODE_CONFIRM = "confirm"
_MODE_AUTO = "auto_after_settle"
_VALID_MODES = (_MODE_CONFIRM, _MODE_AUTO)


def _format_bytes(num_bytes: int) -> str:
    val = float(max(0, int(num_bytes)))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if val < 1024.0 or unit == "TB":
            return f"{int(val)} {unit}" if unit == "B" else f"{val:.1f} {unit}"
        val /= 1024.0
    return f"{val:.1f} TB"


def _short_iso_time(iso_str: str) -> str:
    raw = str(iso_str or "").strip()
    if not raw:
        return "-"
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return dt.astimezone().strftime("%m-%d %H:%M")
    except ValueError:
        return raw[:16].replace("T", " ")


def _short_path(path_str: str, max_len: int = 56) -> str:
    text = str(path_str or "").strip()
    if len(text) <= max_len:
        return text
    head = text[:20]
    tail = text[-(max_len - 23):]
    return f"{head}...{tail}"


def _compute_push_state(snap: VaultSnapshotRecord, status: Optional[VersionVaultStatus]) -> str:
    if snap.large_file_count > 0:
        base = f"隔离大文件({snap.large_file_count})"
    else:
        base = ""
    if status is None or not status.remote_url:
        return f"本地冷库{' · ' + base if base else ''}"
    last_push_at = str(status.last_push_at or "").strip()
    last_push_status = str(status.last_push_status or "").strip().lower()
    if last_push_status in ("ok", "pushed", "succeeded", "skipped") and last_push_at:
        if snap.created_at and snap.created_at <= last_push_at:
            return f"☁️ 已推送{' · ' + base if base else ''}"
    if last_push_status in ("error", "failed"):
        return f"⚠️ 推送失败{' · ' + base if base else ''}"
    return f"⏳ 待推送{' · ' + base if base else ''}"


class VersionVaultTab(tk.Frame):
    """Independent 6th Tab component (`🧊 版本冷库 (Archive)`) for Workspace Version Vault."""

    def __init__(
        self,
        master: tk.Misc,
        *,
        px: Callable[[int], int],
        log_callback: Callable[[str], None],
        ui_call: Callable[[Callable[[], None]], None],
        palette: Dict[str, str],
        toast_callback: Optional[Callable[..., None]] = None,
        vault: Optional[WorkspaceVersionVault] = None,
        copy_callback: Optional[Callable[..., Any]] = None,
        workspaces_override: Optional[Sequence[Path | str]] = None,
        editable_roots_override: Optional[Sequence[Path | str]] = None,
    ) -> None:
        self._palette: Dict[str, str] = dict(_DEFAULT_PALETTE)
        if palette:
            self._palette.update({k: str(v) for k, v in palette.items() if v})
        if "BORDER_DIM" not in self._palette and "BORDER_SUBTLE" in self._palette:
            self._palette["BORDER_DIM"] = self._palette["BORDER_SUBTLE"]

        super().__init__(master, bg=self._palette["BG_ROOT"])

        self._px = px
        self._log_callback = log_callback
        self._ui_call = ui_call
        self._toast_callback = toast_callback
        self._copy_callback = copy_callback
        self._vault_override = vault
        self._workspaces_override: Optional[List[str]] = (
            [str(w) for w in workspaces_override] if workspaces_override is not None else None
        )
        self._editable_roots_override: Optional[List[str]] = (
            [str(e) for e in editable_roots_override]
            if editable_roots_override is not None
            else None
        )

        # Lifecycle & concurrency state
        self._destroyed: bool = False
        self._refresh_busy: bool = False
        self._refresh_gen: int = 0
        self._pending_refresh: bool = False
        self._action_busy: bool = False
        self._thread_lock = threading.Lock()
        self._active_threads: Set[threading.Thread] = set()

        # Data state
        self._status: Optional[VersionVaultStatus] = None
        self._mode: str = _MODE_CONFIRM
        self._settle_sec: int = 300
        self._pinned_paths: List[str] = []
        self._candidates: List[ArchiveCandidate] = []
        self._protected_items: List[ArchiveCandidate] = []
        self._snapshots: List[VaultSnapshotRecord] = []
        self._candidates_by_iid: Dict[str, ArchiveCandidate] = {}
        self._snapshots_by_iid: Dict[str, VaultSnapshotRecord] = {}

        # Tk variables
        self._vault_dir_var = tk.StringVar(value="冷库目录：加载中...")
        self._metrics_var = tk.StringVar(value="快照：- | 体积：- | 队列：-")
        self._push_info_var = tk.StringVar(value="云端推送：-")
        self._mode_btn_var = tk.StringVar(value="🛡 确认封版")
        self._status_pill_var = tk.StringVar(value="就绪")
        self._cand_count_var = tk.StringVar(value="候选 0 · 豁免 0")
        self._snap_count_var = tk.StringVar(value="快照 0")
        self._detail_var = tk.StringVar(
            value="🔒 只增不删安全声明：所有历史版本封存入冷库后终身保留，UI 不提供删除入口。"
        )
        self._include_stale_var = tk.BooleanVar(value=False)
        self._search_var = tk.StringVar(value="")

        self.bind("<Destroy>", self._on_destroy, add="+")
        self._ensure_local_styles()
        self._build_ui()

    # ------------------------------------------------------------------
    # Lifecycle & Thread-Safe Helpers
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

    def _get_vault(self) -> WorkspaceVersionVault:
        if self._vault_override is not None:
            return self._vault_override
        return WorkspaceVersionVault()

    def _safe_log(self, msg: str) -> None:
        if not msg:
            return
        try:
            self._log_callback(msg)
        except Exception:
            pass

    def _safe_toast(self, msg: str, color: Optional[str] = None) -> None:
        if not msg or self._toast_callback is None:
            return
        tone = color or self._palette["ACCENT_CYAN"]
        try:
            self._toast_callback(msg, tone)
        except TypeError:
            try:
                self._toast_callback(msg)
            except Exception:
                pass
        except Exception:
            pass

    def _safe_ui_call(self, fn: Callable[[], None]) -> None:
        """Schedule `fn` onto the Tk main thread without touching Tcl from worker threads."""
        if self._destroyed:
            return

        def _guarded() -> None:
            if not self._is_alive():
                return
            try:
                fn()
            except tk.TclError:
                self._destroyed = True
            except Exception:
                pass

        try:
            self._ui_call(_guarded)
        except tk.TclError:
            self._destroyed = True
        except RuntimeError:
            # Main loop may have exited or not started yet; ignore late UI callback
            pass
        except Exception:
            pass

    def _spawn_daemon(self, name: str, target: Callable[[], None]) -> threading.Thread:
        def _runner() -> None:
            try:
                target()
            finally:
                with self._thread_lock:
                    self._active_threads.discard(threading.current_thread())

        t = threading.Thread(target=_runner, name=name, daemon=True)
        with self._thread_lock:
            self._active_threads.add(t)
        t.start()
        return t

    def wait_for_idle(self, timeout: float = 5.0) -> bool:
        """Wait for all active background worker threads of this tab to finish."""
        deadline = threading.Event()
        import time

        end_at = time.monotonic() + max(0.0, float(timeout))
        while True:
            with self._thread_lock:
                alive = [t for t in self._active_threads if t.is_alive()]
            if not alive:
                return True
            remaining = end_at - time.monotonic()
            if remaining <= 0:
                return False
            alive[0].join(timeout=min(0.1, remaining))
            _ = deadline

    def _p(self, n: int) -> int:
        try:
            return max(1, int(self._px(n)))
        except Exception:
            return max(1, int(n))

    def _font(self, size: int, weight: str = "normal", family: str = "Microsoft YaHei UI") -> Tuple[str, int, str]:
        return (family, max(8, int(size)), weight)

    def _mono_font(self, size: int, weight: str = "normal") -> Tuple[str, int, str]:
        return ("Consolas", max(8, int(size)), weight)

    # ------------------------------------------------------------------
    # Styling & Layout
    # ------------------------------------------------------------------
    def _ensure_local_styles(self) -> None:
        p = self._palette
        style = ttk.Style(self)
        try:
            style.configure(
                "OrionVault.Treeview",
                background=p["BG_CARD"],
                foreground=p["FG_TEXT"],
                fieldbackground=p["BG_CARD"],
                borderwidth=0,
                rowheight=self._p(24),
                font=("Microsoft YaHei UI", 9),
            )
            style.map(
                "OrionVault.Treeview",
                background=[("selected", p["BG_CARD_ALT"])],
                foreground=[("selected", p["ACCENT_CYAN"])],
            )
            style.configure(
                "OrionVault.Treeview.Heading",
                background=p["BG_CARD_ALT"],
                foreground=p["FG_MUTED"],
                relief="flat",
                borderwidth=0,
                font=("Microsoft YaHei UI", 9, "bold"),
            )
            style.map(
                "OrionVault.Treeview.Heading",
                background=[("active", p["BORDER_DIM"])],
                foreground=[("active", p["FG_TEXT"])],
            )
            style.configure(
                "OrionVault.Vertical.TScrollbar",
                background=p["BG_CARD_ALT"],
                troughcolor=p["BG_ROOT"],
                bordercolor=p["BG_ROOT"],
                arrowcolor=p["FG_MUTED"],
                relief="flat",
            )
        except tk.TclError:
            pass

    def _make_button(
        self,
        parent: tk.Misc,
        text: str,
        command: Callable[[], None],
        *,
        bg: Optional[str] = None,
        fg: Optional[str] = None,
        active_bg: Optional[str] = None,
        textvariable: Optional[tk.StringVar] = None,
    ) -> tk.Button:
        p = self._palette
        btn_bg = bg or p["BG_CARD_ALT"]
        btn_fg = fg or p["FG_TEXT"]
        act_bg = active_bg or p["BORDER_DIM"]
        kwargs: Dict[str, Any] = {
            "command": command,
            "bg": btn_bg,
            "fg": btn_fg,
            "activebackground": act_bg,
            "activeforeground": p["FG_TEXT"],
            "relief": "flat",
            "bd": 0,
            "highlightthickness": 1,
            "highlightbackground": p["BORDER_DIM"],
            "cursor": "hand2",
            "font": self._font(9, "bold"),
            "padx": self._p(10),
            "pady": self._p(5),
        }
        if textvariable is not None:
            kwargs["textvariable"] = textvariable
        else:
            kwargs["text"] = text
        return tk.Button(parent, **kwargs)

    def _build_ui(self) -> None:
        p = self._palette
        pad_x = self._p(10)
        pad_y = self._p(6)

        self.grid_rowconfigure(2, weight=1)
        self.grid_columnconfigure(0, weight=1)

        # 1) 顶部横幅：冷库目录、累计快照数/体积、队列状态、最近云端推送与触发模式切换按钮
        banner = tk.Frame(
            self,
            bg=p["BG_CARD"],
            highlightthickness=1,
            highlightbackground=p["BORDER_DIM"],
        )
        banner.grid(row=0, column=0, sticky="ew", padx=pad_x, pady=(pad_y, self._p(4)))
        banner.grid_columnconfigure(0, weight=1)

        top_row = tk.Frame(banner, bg=p["BG_CARD"])
        top_row.grid(row=0, column=0, sticky="ew", padx=self._p(10), pady=(self._p(8), self._p(2)))
        top_row.grid_columnconfigure(1, weight=1)

        tk.Label(
            top_row,
            text="🧊 版本冷库 (Append-Only · 只增不删)",
            bg=p["BG_CARD"],
            fg=p["ACCENT_CYAN"],
            font=self._font(11, "bold"),
        ).grid(row=0, column=0, sticky="w")

        tk.Label(
            top_row,
            textvariable=self._vault_dir_var,
            bg=p["BG_CARD"],
            fg=p["FG_MUTED"],
            font=self._mono_font(8),
            anchor="w",
        ).grid(row=0, column=1, sticky="ew", padx=(self._p(10), self._p(10)))

        self._mode_btn = self._make_button(
            top_row,
            "",
            self._on_toggle_mode_click,
            bg=p["BG_CARD_ALT"],
            fg=p["ACCENT_AMBER"],
            textvariable=self._mode_btn_var,
        )
        self._mode_btn.grid(row=0, column=2, sticky="e")

        meta_row = tk.Frame(banner, bg=p["BG_CARD"])
        meta_row.grid(row=1, column=0, sticky="ew", padx=self._p(10), pady=(self._p(2), self._p(8)))
        meta_row.grid_columnconfigure(0, weight=1)

        tk.Label(
            meta_row,
            textvariable=self._metrics_var,
            bg=p["BG_CARD"],
            fg=p["FG_TEXT"],
            font=self._font(9),
            anchor="w",
        ).grid(row=0, column=0, sticky="w")

        tk.Label(
            meta_row,
            textvariable=self._push_info_var,
            bg=p["BG_CARD"],
            fg=p["FG_MUTED"],
            font=self._font(8),
            anchor="e",
        ).grid(row=0, column=1, sticky="e", padx=(self._p(10), 0))

        # 2) 动作栏：🔍 扫描旧版 / ⚡ 异步归档选中 / 📦 一键归档全部版本层 / ♻️ 原样还原快照 / ☁️ 推送云端
        action_bar = tk.Frame(
            self,
            bg=p["BG_CARD_ALT"],
            highlightthickness=1,
            highlightbackground=p["BORDER_DIM"],
        )
        action_bar.grid(row=1, column=0, sticky="ew", padx=pad_x, pady=self._p(4))

        btn_specs = [
            ("scan", "🔍 扫描旧版", self._on_scan_click, p["BG_CARD"], p["ACCENT_CYAN"]),
            ("seal_sel", "⚡ 异步归档选中", self._on_seal_selected_click, p["BG_CARD"], p["ACCENT_GREEN"]),
            ("seal_all", "📦 一键归档全部版本层", self._on_seal_all_versions_click, p["BG_CARD"], p["ACCENT_AMBER"]),
            ("restore", "♻️ 原样还原快照", self._on_restore_selected_click, p["BG_CARD"], p["ACCENT_ROSE"]),
            ("push", "☁️ 推送云端", self._on_push_remote_click, p["BG_CARD"], p["ACCENT_CYAN"]),
        ]
        self._action_buttons: Dict[str, tk.Button] = {}
        for key, label, cmd, bg, fg in btn_specs:
            btn = self._make_button(action_bar, label, cmd, bg=bg, fg=fg)
            btn.pack(side=tk.LEFT, padx=(self._p(6), 0), pady=self._p(6))
            self._action_buttons[key] = btn

        self._stale_chk = tk.Checkbutton(
            action_bar,
            text="含失活项目",
            variable=self._include_stale_var,
            command=lambda: self.refresh_async(silent=False),
            bg=p["BG_CARD_ALT"],
            fg=p["FG_MUTED"],
            selectcolor=p["BG_ROOT"],
            activebackground=p["BG_CARD_ALT"],
            activeforeground=p["FG_TEXT"],
            font=self._font(8),
            cursor="hand2",
            bd=0,
            highlightthickness=0,
        )
        self._stale_chk.pack(side=tk.LEFT, padx=(self._p(10), 0), pady=self._p(6))

        tk.Label(
            action_bar,
            textvariable=self._status_pill_var,
            bg=p["BG_CARD_ALT"],
            fg=p["ACCENT_GREEN"],
            font=self._font(8, "bold"),
            padx=self._p(8),
        ).pack(side=tk.RIGHT, padx=self._p(6))

        # 3) 双联动 Treeview（PanedWindow 上下分栏）
        paned = tk.PanedWindow(
            self,
            orient=tk.VERTICAL,
            bg=p["BG_ROOT"],
            sashwidth=self._p(5),
            sashrelief="flat",
            bd=0,
        )
        paned.grid(row=2, column=0, sticky="nsew", padx=pad_x, pady=(self._p(4), self._p(4)))

        # --- 上表：工作区待外迁旧版本候选（含保护标记与触发原因） ---
        cand_card = tk.Frame(
            paned,
            bg=p["BG_CARD"],
            highlightthickness=1,
            highlightbackground=p["BORDER_DIM"],
        )
        cand_card.grid_rowconfigure(1, weight=1)
        cand_card.grid_columnconfigure(0, weight=1)

        cand_hdr = tk.Frame(cand_card, bg=p["BG_CARD_ALT"])
        cand_hdr.grid(row=0, column=0, columnspan=2, sticky="ew")
        tk.Label(
            cand_hdr,
            text="📋 工作区待外迁旧版本候选（含保护标记与触发原因）",
            bg=p["BG_CARD_ALT"],
            fg=p["FG_TEXT"],
            font=self._font(9, "bold"),
            padx=self._p(8),
            pady=self._p(5),
        ).pack(side=tk.LEFT)
        tk.Label(
            cand_hdr,
            textvariable=self._cand_count_var,
            bg=p["BG_CARD_ALT"],
            fg=p["FG_MUTED"],
            font=self._font(8),
            padx=self._p(8),
        ).pack(side=tk.RIGHT)

        cand_cols = ("guard", "category", "project", "rel_path", "size", "reason")
        self._cand_tree = ttk.Treeview(
            cand_card,
            columns=cand_cols,
            show="headings",
            selectmode="extended",
            style="OrionVault.Treeview",
            height=7,
        )
        cand_specs = [
            ("guard", "保护状态", self._p(95), "center", False),
            ("category", "类别", self._p(105), "w", False),
            ("project", "项目", self._p(130), "w", False),
            ("rel_path", "工作区相对路径", self._p(230), "w", True),
            ("size", "体积 / 文件", self._p(105), "e", False),
            ("reason", "触发原因 / 保护说明", self._p(230), "w", True),
        ]
        for col, title, width, anchor, stretch in cand_specs:
            self._cand_tree.heading(col, text=title, anchor=anchor)
            self._cand_tree.column(col, width=width, minwidth=self._p(56), anchor=anchor, stretch=stretch)

        self._cand_tree.tag_configure("cand_normal", foreground=p["FG_TEXT"])
        self._cand_tree.tag_configure("cand_confirm", foreground=p["ACCENT_AMBER"])
        self._cand_tree.tag_configure("cand_protected", foreground=p["FG_MUTED"])

        cand_scroll = ttk.Scrollbar(
            cand_card,
            orient=tk.VERTICAL,
            command=self._cand_tree.yview,
            style="OrionVault.Vertical.TScrollbar",
        )
        self._cand_tree.configure(yscrollcommand=cand_scroll.set)
        self._cand_tree.grid(row=1, column=0, sticky="nsew")
        cand_scroll.grid(row=1, column=1, sticky="ns")
        self._cand_tree.bind("<<TreeviewSelect>>", self._on_candidate_select)

        # --- 下表：不可删外部冷库快照历史 ---
        snap_card = tk.Frame(
            paned,
            bg=p["BG_CARD"],
            highlightthickness=1,
            highlightbackground=p["BORDER_DIM"],
        )
        snap_card.grid_rowconfigure(1, weight=1)
        snap_card.grid_columnconfigure(0, weight=1)

        snap_hdr = tk.Frame(snap_card, bg=p["BG_CARD_ALT"])
        snap_hdr.grid(row=0, column=0, columnspan=2, sticky="ew")
        tk.Label(
            snap_hdr,
            text="🧊 不可删外部冷库快照历史（终身只增不删 · 校验还原）",
            bg=p["BG_CARD_ALT"],
            fg=p["FG_TEXT"],
            font=self._font(9, "bold"),
            padx=self._p(8),
            pady=self._p(5),
        ).pack(side=tk.LEFT)

        tk.Label(
            snap_hdr,
            textvariable=self._snap_count_var,
            bg=p["BG_CARD_ALT"],
            fg=p["FG_MUTED"],
            font=self._font(8),
            padx=self._p(6),
        ).pack(side=tk.RIGHT)

        search_entry = tk.Entry(
            snap_hdr,
            textvariable=self._search_var,
            bg=p["BG_ROOT"],
            fg=p["FG_TEXT"],
            insertbackground=p["ACCENT_CYAN"],
            relief="flat",
            highlightthickness=1,
            highlightbackground=p["BORDER_DIM"],
            font=self._font(8),
            width=22,
        )
        search_entry.pack(side=tk.RIGHT, padx=(self._p(4), self._p(4)), pady=self._p(3))
        search_entry.bind("<Return>", lambda _e: self._apply_snapshot_filter())
        search_entry.bind("<KeyRelease>", lambda _e: self._apply_snapshot_filter())

        tk.Label(
            snap_hdr,
            text="检索:",
            bg=p["BG_CARD_ALT"],
            fg=p["FG_MUTED"],
            font=self._font(8),
        ).pack(side=tk.RIGHT)

        snap_cols = ("snapshot_id", "created_at", "category", "project", "files_size", "sha256", "push_state")
        self._snap_tree = ttk.Treeview(
            snap_card,
            columns=snap_cols,
            show="headings",
            selectmode="browse",
            style="OrionVault.Treeview",
            height=7,
        )
        snap_specs = [
            ("snapshot_id", "快照 ID", self._p(165), "w", True),
            ("created_at", "封存时间", self._p(95), "center", False),
            ("category", "类别", self._p(100), "w", False),
            ("project", "项目", self._p(120), "w", False),
            ("files_size", "文件数 / 体积", self._p(115), "e", False),
            ("sha256", "SHA-256", self._p(120), "w", False),
            ("push_state", "推送状态", self._p(115), "center", False),
        ]
        for col, title, width, anchor, stretch in snap_specs:
            self._snap_tree.heading(col, text=title, anchor=anchor)
            self._snap_tree.column(col, width=width, minwidth=self._p(56), anchor=anchor, stretch=stretch)

        self._snap_tree.tag_configure("snap_normal", foreground=p["FG_TEXT"])
        self._snap_tree.tag_configure("snap_related", foreground=p["ACCENT_CYAN"])

        snap_scroll = ttk.Scrollbar(
            snap_card,
            orient=tk.VERTICAL,
            command=self._snap_tree.yview,
            style="OrionVault.Vertical.TScrollbar",
        )
        self._snap_tree.configure(yscrollcommand=snap_scroll.set)
        self._snap_tree.grid(row=1, column=0, sticky="nsew")
        snap_scroll.grid(row=1, column=1, sticky="ns")
        self._snap_tree.bind("<<TreeviewSelect>>", self._on_snapshot_select)

        paned.add(cand_card, minsize=self._p(120))
        paned.add(snap_card, minsize=self._p(120))

        # 4) 底部详情与安全声明条
        footer = tk.Frame(
            self,
            bg=p["BG_CARD"],
            highlightthickness=1,
            highlightbackground=p["BORDER_DIM"],
        )
        footer.grid(row=3, column=0, sticky="ew", padx=pad_x, pady=(self._p(2), pad_y))
        tk.Label(
            footer,
            textvariable=self._detail_var,
            bg=p["BG_CARD"],
            fg=p["FG_MUTED"],
            font=self._mono_font(8),
            anchor="w",
            padx=self._p(8),
            pady=self._p(4),
        ).pack(fill=tk.X, expand=True)

    # ------------------------------------------------------------------
    # Public Contract API
    # ------------------------------------------------------------------
    def refresh_async(self, silent: bool = False) -> None:
        """Asynchronously refresh vault status, workspace old-version candidates, and snapshots."""
        if not self._is_alive():
            return
        if self._refresh_busy:
            self._pending_refresh = True
            return

        self._refresh_busy = True
        self._refresh_gen += 1
        gen = self._refresh_gen
        include_stale = bool(self._include_stale_var.get())
        if not silent:
            self._status_pill_var.set("扫描刷新中...")

        def _worker() -> None:
            error_msg = ""
            status: Optional[VersionVaultStatus] = None
            snapshots: List[VaultSnapshotRecord] = []
            candidates: List[ArchiveCandidate] = []
            protected_items: List[ArchiveCandidate] = []
            mode = _MODE_CONFIRM
            settle_sec = 300
            pinned_paths: List[str] = []

            try:
                vault = self._get_vault()
                status = vault.get_status()
                cfg = load_app_config(vault.config_path)
                raw_mode = str(cfg.get("version_vault_mode") or _MODE_CONFIRM).strip().lower()
                mode = raw_mode if raw_mode in _VALID_MODES else _MODE_CONFIRM
                try:
                    settle_sec = max(0, int(cfg.get("version_vault_settle_sec", 300)))
                except (TypeError, ValueError):
                    settle_sec = 300
                raw_pins = cfg.get("version_vault_pinned_paths")
                if isinstance(raw_pins, list):
                    pinned_paths = [str(x).strip() for x in raw_pins if str(x).strip()]

                snapshots = vault.list_snapshots(limit=200)

                workspaces: Optional[List[str]] = self._workspaces_override
                if workspaces is None:
                    scan_roots = status.policy.get("version_vault_scan_roots")
                    if isinstance(scan_roots, list) and scan_roots:
                        workspaces = [str(w) for w in scan_roots if str(w).strip()]

                report = scan_old_versions(
                    workspaces,
                    include_stale_projects=include_stale,
                    pinned_paths=pinned_paths,
                    editable_roots=self._editable_roots_override,
                )
                candidates = list(report.candidates)
                protected_items = list(report.protected_items)
            except Exception as exc:
                error_msg = str(exc)

            def _apply() -> None:
                self._refresh_busy = False
                if gen != self._refresh_gen:
                    return
                if error_msg:
                    self._status_pill_var.set("刷新异常")
                    self._safe_log(f"⚠️ [VersionVault] 刷新失败: {error_msg}")
                    if not silent:
                        self._safe_toast(f"版本冷库刷新失败: {error_msg}", self._palette["ACCENT_CRIMSON"])
                else:
                    self._status = status
                    self._mode = mode
                    self._settle_sec = settle_sec
                    self._pinned_paths = pinned_paths
                    self._candidates = candidates
                    self._protected_items = protected_items
                    self._snapshots = snapshots
                    self._render_banner()
                    self._render_candidates()
                    self._render_snapshots()
                    self._status_pill_var.set("就绪")
                    if not silent:
                        msg = (
                            f"🧊 冷库扫描完成：可封存 {len(candidates)} 项 / 受保护 {len(protected_items)} 项 "
                            f"| 冷库快照 {len(snapshots)} 条"
                        )
                        self._safe_log(msg)
                        self._safe_toast(msg, self._palette["ACCENT_CYAN"])

                if self._pending_refresh:
                    self._pending_refresh = False
                    self.refresh_async(silent=True)

            self._safe_ui_call(_apply)

        self._spawn_daemon("orion-vault-refresh", _worker)

    def get_selected_candidate_ids(self) -> List[str]:
        """Return list of selected `candidate_id` values from the upper candidates Treeview."""
        if not self._is_alive():
            return []
        try:
            selection = self._cand_tree.selection()
        except tk.TclError:
            return []
        result: List[str] = []
        for iid in selection:
            cand = self._candidates_by_iid.get(iid)
            if cand is not None and cand.candidate_id:
                result.append(cand.candidate_id)
        return result

    def get_selected_snapshot_id(self) -> str:
        """Return selected `snapshot_id` from the lower vault snapshots Treeview, or empty string."""
        if not self._is_alive():
            return ""
        try:
            selection = self._snap_tree.selection()
        except tk.TclError:
            return ""
        if not selection:
            return ""
        snap = self._snapshots_by_iid.get(selection[0])
        return snap.snapshot_id if snap is not None else ""

    # ------------------------------------------------------------------
    # Rendering Helpers (Main Thread Only)
    # ------------------------------------------------------------------
    def _render_banner(self) -> None:
        st = self._status
        if st is None:
            return
        self._vault_dir_var.set(f"冷库: {_short_path(st.vault_dir)}")
        self._metrics_var.set(
            f"快照: {st.snapshot_count} 个 ({st.total_files} 文件 · {_format_bytes(st.total_bytes)})   |   "
            f"队列: 排队 {st.queued_jobs} · 进行中 {st.running_jobs} · 完成 {st.succeeded_jobs}"
            + (f" · 失败 {st.failed_jobs}" if st.failed_jobs else "")
        )
        remote_label = st.branch or "main"
        if not st.remote_url:
            push_text = f"云端: 未配置远端 ({remote_label})"
        else:
            last_st = st.last_push_status or "未推送"
            last_at = _short_iso_time(st.last_push_at) if st.last_push_at else "-"
            push_text = f"云端({remote_label}): {last_st} · {last_at}"
        self._push_info_var.set(push_text)

        if self._mode == _MODE_AUTO:
            self._mode_btn_var.set(f"⚡ 新版静默自动 ({self._settle_sec}s)")
            self._mode_btn.configure(fg=self._palette["ACCENT_GREEN"])
        else:
            self._mode_btn_var.set("🛡 确认封版")
            self._mode_btn.configure(fg=self._palette["ACCENT_AMBER"])

    def _render_candidates(self) -> None:
        prev_ids = set(self.get_selected_candidate_ids())
        for iid in self._cand_tree.get_children():
            self._cand_tree.delete(iid)
        self._candidates_by_iid.clear()

        to_reselect: List[str] = []
        all_rows: List[ArchiveCandidate] = list(self._candidates) + list(self._protected_items)
        for idx, cand in enumerate(all_rows):
            iid = f"cand_{idx}"
            self._candidates_by_iid[iid] = cand
            if cand.protected:
                guard_text = "🔒 受保护"
                tag = "cand_protected"
                reason_text = cand.protect_reason or cand.trigger_reason or "已豁免保护"
            elif cand.requires_explicit_confirm:
                guard_text = "⚠️ 需确认"
                tag = "cand_confirm"
                reason_text = cand.trigger_reason or "失活项目需二次确认"
            else:
                guard_text = "✅ 可外迁"
                tag = "cand_normal"
                reason_text = cand.trigger_reason or "-"

            cat_label = _CATEGORY_LABELS.get(cand.category, cand.category)
            size_text = f"{_format_bytes(cand.size_bytes)} / {cand.file_count}f"
            self._cand_tree.insert(
                "",
                tk.END,
                iid=iid,
                values=(
                    guard_text,
                    cat_label,
                    cand.project or "-",
                    cand.rel_path or os.path.basename(cand.source_path),
                    size_text,
                    reason_text,
                ),
                tags=(tag,),
            )
            if cand.candidate_id in prev_ids:
                to_reselect.append(iid)

        if to_reselect:
            try:
                self._cand_tree.selection_set(to_reselect)
            except tk.TclError:
                pass

        total_bytes = sum(c.size_bytes for c in self._candidates)
        self._cand_count_var.set(
            f"可封存 {len(self._candidates)} 项 ({_format_bytes(total_bytes)}) · 受保护 {len(self._protected_items)} 项"
        )

    def _render_snapshots(self, highlight_project: str = "") -> None:
        prev_snap_id = self.get_selected_snapshot_id()
        query = str(self._search_var.get() or "").strip().lower()

        for iid in self._snap_tree.get_children():
            self._snap_tree.delete(iid)
        self._snapshots_by_iid.clear()

        matched = 0
        reselect_iid = ""
        hl_proj = highlight_project.strip().lower()

        for idx, snap in enumerate(self._snapshots):
            if query:
                haystack = " ".join(
                    [
                        snap.snapshot_id,
                        snap.project,
                        snap.category,
                        snap.source_path,
                        snap.root_sha256,
                        snap.trigger_reason,
                    ]
                ).lower()
                if query not in haystack:
                    continue

            iid = f"snap_{idx}"
            self._snapshots_by_iid[iid] = snap
            matched += 1
            cat_label = _CATEGORY_LABELS.get(snap.category, snap.category)
            files_size = f"{snap.file_count}f / {_format_bytes(snap.total_bytes)}"
            short_sha = (snap.root_sha256 or "")[:12] or "-"
            push_state = _compute_push_state(snap, self._status)
            tag = "snap_related" if (hl_proj and snap.project.lower() == hl_proj) else "snap_normal"
            self._snap_tree.insert(
                "",
                tk.END,
                iid=iid,
                values=(
                    snap.snapshot_id,
                    _short_iso_time(snap.created_at),
                    cat_label,
                    snap.project or "-",
                    files_size,
                    short_sha,
                    push_state,
                ),
                tags=(tag,),
            )
            if prev_snap_id and snap.snapshot_id == prev_snap_id:
                reselect_iid = iid

        if reselect_iid:
            try:
                self._snap_tree.selection_set(reselect_iid)
            except tk.TclError:
                pass

        if query:
            self._snap_count_var.set(f"匹配 {matched} / {len(self._snapshots)} 条快照")
        else:
            self._snap_count_var.set(f"共 {len(self._snapshots)} 条不可删快照")

    def _apply_snapshot_filter(self) -> None:
        if not self._is_alive():
            return
        self._render_snapshots()

    # ------------------------------------------------------------------
    # Selection Linkage Events
    # ------------------------------------------------------------------
    def _on_candidate_select(self, _event: Optional[tk.Event] = None) -> None:
        if not self._is_alive():
            return
        sel = self._cand_tree.selection()
        if not sel:
            return
        cand = self._candidates_by_iid.get(sel[0])
        if cand is None:
            return
        status_note = f"受保护({cand.protect_reason})" if cand.protected else cand.trigger_reason
        self._detail_var.set(
            f"📌 候选 [{cand.category}] {cand.project} | {cand.source_path} | {status_note}"
        )
        # 联动高亮下表同项目历史快照
        if cand.project:
            self._render_snapshots(highlight_project=cand.project)

    def _on_snapshot_select(self, _event: Optional[tk.Event] = None) -> None:
        if not self._is_alive():
            return
        sel = self._snap_tree.selection()
        if not sel:
            return
        snap = self._snapshots_by_iid.get(sel[0])
        if snap is None:
            return
        self._detail_var.set(
            f"🧊 快照 {snap.snapshot_id} | 原路径: {snap.source_path} | "
            f"SHA-256: {snap.root_sha256[:16]}... | 原因: {snap.trigger_reason or '-'}"
        )

    # ------------------------------------------------------------------
    # User Action Handlers (All I/O Runs on Daemon Threads)
    # ------------------------------------------------------------------
    def _on_scan_click(self) -> None:
        self.refresh_async(silent=False)

    def _on_toggle_mode_click(self) -> None:
        if not self._is_alive():
            return
        next_mode = _MODE_AUTO if self._mode == _MODE_CONFIRM else _MODE_CONFIRM
        self.set_trigger_mode_async(next_mode)

    def set_trigger_mode_async(self, mode: str) -> None:
        """Persist trigger mode (`confirm` or `auto_after_settle`) asynchronously."""
        clean_mode = str(mode or "").strip().lower()
        if clean_mode not in _VALID_MODES:
            return
        if not self._is_alive():
            return
        self._status_pill_var.set("切换策略中...")

        def _worker() -> None:
            err = ""
            try:
                vault = self._get_vault()
                update_app_config(
                    lambda cfg: {**cfg, "version_vault_mode": clean_mode},
                    path=vault.config_path,
                )
            except Exception as exc:
                err = str(exc)

            def _done() -> None:
                if err:
                    self._status_pill_var.set("策略切换失败")
                    self._safe_log(f"❌ [VersionVault] 切换触发策略失败: {err}")
                    self._safe_toast(f"策略切换失败: {err}", self._palette["ACCENT_CRIMSON"])
                    return
                self._mode = clean_mode
                self._render_banner()
                self._status_pill_var.set("就绪")
                label = "新版静默自动 (auto_after_settle)" if clean_mode == _MODE_AUTO else "确认封版 (confirm)"
                msg = f"⚙️ 版本冷库触发模式已切换为：{label}"
                self._safe_log(msg)
                self._safe_toast(msg, self._palette["ACCENT_AMBER"])

            self._safe_ui_call(_done)

        self._spawn_daemon("orion-vault-policy", _worker)

    def _on_seal_selected_click(self) -> None:
        if not self._is_alive():
            return
        sel = self._cand_tree.selection()
        if not sel:
            msg = "请先在上表选择至少一项待外迁旧版本候选"
            self._safe_toast(msg, self._palette["ACCENT_AMBER"])
            self._safe_log(f"ℹ️ [VersionVault] {msg}")
            return

        selected_cands: List[ArchiveCandidate] = []
        skipped_protected = 0
        has_explicit_confirm = False
        for iid in sel:
            cand = self._candidates_by_iid.get(iid)
            if cand is None:
                continue
            if cand.protected:
                skipped_protected += 1
                continue
            if cand.requires_explicit_confirm or cand.category == CATEGORY_STALE_PROJECT:
                has_explicit_confirm = True
            selected_cands.append(cand)

        if not selected_cands:
            msg = f"所选 {skipped_protected} 项均为受保护豁免条目，已自动拦截跳过"
            self._safe_toast(msg, self._palette["ACCENT_AMBER"])
            self._safe_log(f"🔒 [VersionVault] {msg}")
            return

        if has_explicit_confirm:
            confirmed = messagebox.askyesno(
                "确认外迁失活项目",
                f"选中条目中包含需二次确认的失活项目（共 {len(selected_cands)} 项）。\n"
                "确认将其封存入不可删外部版本冷库并按策略外迁吗？",
                parent=self,
            )
            if not confirmed:
                return

        self.enqueue_candidates_async(
            selected_cands,
            trigger_reason="ui_seal_selected",
            spawn_worker=True,
        )

    def _on_seal_all_versions_click(self) -> None:
        if not self._is_alive():
            return
        eligible = [
            c
            for c in self._candidates
            if not c.protected
            and not c.requires_explicit_confirm
            and c.category != CATEGORY_STALE_PROJECT
        ]
        if not eligible:
            msg = "当前工作区没有可一键归档的版本层旧版本（已自动跳过受保护项与失活项目）"
            self._safe_toast(msg, self._palette["ACCENT_AMBER"])
            self._safe_log(f"ℹ️ [VersionVault] {msg}")
            return

        self.enqueue_candidates_async(
            eligible,
            trigger_reason="ui_seal_all_versions",
            spawn_worker=True,
        )

    def enqueue_candidates_async(
        self,
        candidates: Sequence[ArchiveCandidate],
        *,
        trigger_reason: str = "ui_seal",
        spawn_worker: bool = True,
        run_inline: bool = False,
    ) -> None:
        """Enqueue selected archive candidates in a daemon thread and notify UI upon completion."""
        items_payload: List[Dict[str, Any]] = []
        for cand in candidates:
            if cand.protected:
                continue
            items_payload.append(
                {
                    "source_path": cand.source_path,
                    "project": cand.project,
                    "category": cand.category,
                    "trigger_reason": cand.trigger_reason or trigger_reason,
                }
            )
        if not items_payload or not self._is_alive():
            return

        self._status_pill_var.set(f"入队封版 {len(items_payload)} 项...")

        def _worker() -> None:
            err = ""
            job_id = ""
            worker_pid = 0
            completed = 0
            try:
                vault = self._get_vault()
                policy = vault.load_policy()
                evict_source = bool(policy.get("version_vault_evict_source", True))
                job = vault.enqueue_seal_job(
                    items_payload,
                    trigger_reason=trigger_reason,
                    evict_source=evict_source,
                )
                job_id = job.job_id
                if run_inline:
                    summary = vault.run_worker_once(max_jobs=5)
                    completed = int(summary.get("snapshots_created", 0))
                elif spawn_worker:
                    wres = vault.spawn_async_worker()
                    worker_pid = int(wres.get("pid", 0))
            except Exception as exc:
                err = str(exc)

            def _done() -> None:
                if err:
                    self._status_pill_var.set("归档入队失败")
                    self._safe_log(f"❌ [VersionVault] 异步封版失败: {err}")
                    self._safe_toast(f"异步封版失败: {err}", self._palette["ACCENT_CRIMSON"])
                    return
                if run_inline:
                    msg = f"✅ 归档任务 {job_id} 完成，新增 {completed} 条冷库快照"
                else:
                    msg = f"⚡ 已入队异步封版 {job_id}（{len(items_payload)} 项，worker PID={worker_pid}）"
                self._status_pill_var.set("已入队")
                self._safe_log(msg)
                self._safe_toast(msg, self._palette["ACCENT_GREEN"])
                self.refresh_async(silent=True)

            self._safe_ui_call(_done)

        self._spawn_daemon("orion-vault-enqueue", _worker)

    def _on_restore_selected_click(self) -> None:
        if not self._is_alive():
            return
        snap_id = self.get_selected_snapshot_id()
        if not snap_id:
            msg = "请先在下表选择一条冷库快照再执行还原"
            self._safe_toast(msg, self._palette["ACCENT_AMBER"])
            self._safe_log(f"ℹ️ [VersionVault] {msg}")
            return
        self.restore_snapshot_async(snap_id)

    def restore_snapshot_async(
        self,
        snapshot_id: str,
        *,
        target_dir: Optional[str] = None,
        force: bool = False,
    ) -> None:
        """Restore a vault snapshot with SHA-256 verification on a daemon thread."""
        clean_id = str(snapshot_id or "").strip()
        if not clean_id or not self._is_alive():
            return
        self._status_pill_var.set(f"还原 {clean_id[:16]}...")

        def _worker() -> None:
            err = ""
            res: Dict[str, Any] = {}
            try:
                vault = self._get_vault()
                res = vault.restore_snapshot(clean_id, target_dir=target_dir, force=force)
            except Exception as exc:
                err = str(exc)

            def _done() -> None:
                if err:
                    self._status_pill_var.set("还原失败")
                    self._safe_log(f"❌ [VersionVault] 快照 {clean_id} 还原失败: {err}")
                    self._safe_toast(f"还原失败: {err}", self._palette["ACCENT_CRIMSON"])
                    return
                restored_path = str(res.get("restored_path") or "")
                fcount = int(res.get("file_count", 0))
                tbytes = int(res.get("total_bytes", 0))
                msg = f"♻️ 快照 {clean_id} 校验还原成功 -> {restored_path} ({fcount} 文件, {_format_bytes(tbytes)})"
                self._status_pill_var.set("还原完成")
                self._safe_log(msg)
                self._safe_toast(f"♻️ 快照 {clean_id} 原样还原完成", self._palette["ACCENT_GREEN"])
                self.refresh_async(silent=True)

            self._safe_ui_call(_done)

        self._spawn_daemon("orion-vault-restore", _worker)

    def _on_push_remote_click(self) -> None:
        if not self._is_alive():
            return
        self.push_remote_async()

    def push_remote_async(self, *, remote_url: str = "", branch: str = "", dry_run: bool = False) -> None:
        """Push the append-only vault Git repo to remote on a daemon thread (never force push)."""
        if not self._is_alive():
            return
        self._status_pill_var.set("推送云端中...")

        def _worker() -> None:
            err = ""
            res: Any = None
            try:
                vault = self._get_vault()
                res = vault.push_to_remote(remote_url=remote_url, branch=branch, dry_run=dry_run)
            except Exception as exc:
                err = str(exc)

            def _done() -> None:
                if err:
                    self._status_pill_var.set("推送异常")
                    self._safe_log(f"❌ [VersionVault] 云端推送异常: {err}")
                    self._safe_toast(f"云端推送异常: {err}", self._palette["ACCENT_CRIMSON"])
                    return
                ok = bool(getattr(res, "ok", False))
                message = str(getattr(res, "message", "") or ("推送完成" if ok else "推送失败"))
                tone = self._palette["ACCENT_GREEN"] if ok else self._palette["ACCENT_AMBER"]
                self._status_pill_var.set("推送完成" if ok else "推送受阻")
                prefix = "☁️" if ok else "⚠️"
                self._safe_log(f"{prefix} [VersionVault] {message}")
                self._safe_toast(f"{prefix} {message}", tone)
                self.refresh_async(silent=True)

            self._safe_ui_call(_done)

        self._spawn_daemon("orion-vault-push", _worker)
