# -*- coding: utf-8 -*-
"""
Acerola-Orion 桌面星盘主控台薄壳与插件宿主 (acerola_orion/panel.py)
- 视觉控件位于 `acerola_orion.ui.widgets`，内置 4 标签页与禅定模式位于 `acerola_orion.ui.builtin_tabs`
- 通过 `PanelPluginHost` 向微内核插件 (`OrionPlugin.register_ui`) 开放动态 Tab、Hub 动作槽、
  托盘菜单、KV 缓存雷达自定义刻度线与顶栏状态徽章插槽
- 保持顶层全部公共类/函数/常量 100% 向后兼容导出
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from datetime import datetime
import json
import logging
import math
from pathlib import Path
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .cache_radar import (
    CACHE_SAFE_TTL_SEC,
    COMPACT_COMMAND_TEXT,
    LARGE_CONTEXT_HINT_TOKENS,
    PHASE_ACTIVE,
    PHASE_COLD,
    PHASE_HOT,
    PHASE_WARNING,
    TARGET_MODE_LATEST,
    TARGET_MODE_PINNED,
    TARGET_MODE_SMART_GUARD,
    VALID_HUD_MODES,
    CacheRadarSampler,
    CacheRadarState,
    format_tokens_compact,
    normalize_hud_mode,
    normalize_target_mode,
)
from .constants import (
    APP_CODENAME,
    APP_TRINITY,
    APP_VERSION,
    atomic_write_text,
    configure_vault_auto_backup,
    get_data_dir,
    get_guillotine_log_path,
    get_ls_log_path,
    interprocess_file_lock,
    is_vault_auto_backup_enabled,
)
from .core.kernel import get_kernel
from .guillotine import install_guard_autostart, uninstall_guard_autostart
from .ledger import OrionLedger
from .rpc import read_shared_file_lines, terminate_pid_win32
from .telemetry import (
    OrionTelemetryReport,
    collect_telemetry,
    invalidate_guard_probe_cache,
    load_json_safe,
)
from .ui.builtin_tabs import (
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
from .ui.plugin_host import PanelPluginHost, _BUILTIN_TAB_KEYS, _normalize_radar_marker
from .ui import radar_controller as _rc
from .ui.window_chrome import (
    APP_USER_MODEL_ID,
    TRAY_CLASS_NAME,
    _UNSET,
    _activate_existing_panel_window,
    _apply_win32_dark_chrome,
    _enable_high_dpi,
    _get_icon_path,
    _get_ui_prefs_path,
    _load_ui_prefs,
    _panel_window_title,
    _save_ui_prefs,
    _terminate_existing_panel_instance,
)
from .ui.widgets import (
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
    AstrolabeDialWidget,
    CacheRadarHudWindow,
    OrionChartWidget,
    QuotaBarWidget,
    _compose_tray_tooltip,
    _compute_remaining_window_fraction,
    _quota_semantic_colors,
    _radar_palette,
    _rotated_ellipse_points,
    _virtual_screen_rect,
    clamp_hud_coords,
)
from .vault import OrionVault, VaultSessionItem, VaultStatusReport
from .win_tray import (
    MenuSpec,
    Win32TrayController,
    apply_tk_window_icon,
    bring_tk_window_to_front,
    set_app_user_model_id,
    signal_existing_instance,
)

logger = logging.getLogger("acerola_orion.panel")


class AcerolaOrionApp(tk.Tk):
    def __init__(
        self,
        auto_refresh_ms: int = 15000,
        start_minimized: bool = False,
        enable_tray: bool = True,
        initial_mode: Optional[str] = None,
        enable_hud: Optional[bool] = None,
    ) -> None:
        _enable_high_dpi()
        set_app_user_model_id(APP_USER_MODEL_ID)
        super().__init__()
        self.auto_refresh_ms = auto_refresh_ms
        self._refresh_busy = False
        self._action_busy = False
        self._refresh_gen = 0
        self._action_gen = 0
        self._pending_refresh = False
        self._offline_streak = 0
        self._closing = False
        self._hidden_to_tray = False
        self._tray_hint_shown = False
        self._tray: Optional[Win32TrayController] = None
        self._hud_win: Optional[CacheRadarHudWindow] = None
        self._action_buttons: List[tk.Button] = []
        self._auto_refresh_id: Optional[str] = None
        self._ledger = OrionLedger()
        self._vault = OrionVault()
        self._vault_auto_enabled: bool = is_vault_auto_backup_enabled()
        self._vault_deleted_only: bool = False
        self._last_vault_status: Optional[VaultStatusReport] = None
        self._last_report: Optional[OrionTelemetryReport] = None
        self._last_daily: List[Any] = []
        self._last_audit: Any = None
        self._last_quota_series: List[Dict[str, Any]] = []
        self._notified_tier_levels: Dict[str, int] = {}
        self._tier_hud_until_mono: float = 0.0
        self._last_copied_text: str = ""
        self._header_badge_packed: bool = False

        prefs = _load_ui_prefs()
        self._view_mode: str = (
            initial_mode if initial_mode in ("full", "zen") else str(prefs["view_mode"])
        )
        self._zen_metric: str = str(prefs["zen_metric"])
        self._hud_mode: str = normalize_hud_mode(prefs.get("hud_mode"))
        self._target_mode, self._pinned_conv_id = normalize_target_mode(
            prefs.get("radar_target_mode"),
            prefs.get("radar_pinned_conv_id"),
        )
        hx, hy = prefs.get("hud_x"), prefs.get("hud_y")
        self._hud_pos: Optional[Tuple[int, int]] = (int(hx), int(hy)) if (hx is not None and hy is not None) else None
        self._cache_radar = CacheRadarSampler(
            target_mode=self._target_mode,
            pinned_conv_id=self._pinned_conv_id,
        )
        self._radar_state: CacheRadarState = CacheRadarState(
            target_mode=self._target_mode,
            pinned_conv_id=self._pinned_conv_id,
        )
        self._radar_bar_fraction: float = 0.0
        self._radar_bar_color: str = BORDER_SUBTLE
        self._radar_defer_pending: bool = False
        self._radar_tick_scheduled: bool = False
        self._radar_poll_scheduled: bool = False
        self._radar_poll_busy: bool = False
        self._last_base_tooltip: str = f"{APP_CODENAME} v{APP_VERSION}"
        self._last_sent_tooltip: str = ""
        self._last_tray_tip_mono: float = 0.0

        self._active_full_tab: str = "hub"
        self._hub_chart_kind: str = "gemini"

        try:
            dpi = float(self.winfo_fpixels("1i"))
            self._dpi_scale = max(1.0, min(2.25, dpi / 96.0))
        except Exception:
            self._dpi_scale = 1.0

        self.title(_panel_window_title())
        apply_tk_window_icon(self, _get_icon_path())
        self.configure(bg=BG_ROOT)
        self._apply_window_geometry_for_mode(initial=True)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        _apply_win32_dark_chrome(self)

        self._plugin_host = PanelPluginHost(self)
        self._configure_styles()
        self._build_header()
        self._build_body_containers()
        self._mount_ui_plugins()
        self._sync_view_mode_widgets()

        use_hud = enable_tray if enable_hud is None else bool(enable_hud)
        if use_hud:
            self._hud_win = CacheRadarHudWindow(
                master=self,
                dpi_scale=self._dpi_scale,
                initial_pos=self._hud_pos,
                on_drag_end=self._on_hud_drag_end,
                on_select_mode=self.set_hud_mode,
                on_open_panel=self.restore_from_tray,
                hud_mode_getter=lambda: self._hud_mode,
                target_getter=lambda: (self._target_mode, self._pinned_conv_id),
                sessions_provider=lambda: self._cache_radar.list_recent_sessions(limit=6),
                on_select_target=self.set_radar_target,
                on_copy_compact=self.copy_compact_command,
            )

        if enable_tray and sys.platform == "win32":
            self._tray = Win32TrayController(
                class_name=TRAY_CLASS_NAME,
                tooltip=f"{APP_CODENAME} v{APP_VERSION} · 单击唤醒星盘主控台",
                ico_path=_get_icon_path(),
                on_open=self.restore_from_tray,
                menu_builder=self._build_tray_menu,
                dispatch_ui=self._ui_call,
            )
            self._tray.start()

        self.after(50, self._pump_ui_queue)
        if start_minimized and self._tray is not None and self._tray.active:
            self.hide_to_tray(notify=False)
            self.after(300, lambda: self.refresh_all_async(silent=True))
        else:
            self.after(120, self.refresh_all_async)
            self._schedule_auto_refresh()
        if self.auto_refresh_ms > 0:
            self._ensure_radar_loops()

    @property
    def plugin_host(self) -> PanelPluginHost:
        return self._plugin_host

    def _mount_ui_plugins(self) -> None:
        try:
            get_kernel().register_ui(self._plugin_host)
        except Exception as exc:
            logger.warning("Failed to mount UI plugins: %s", exc, exc_info=True)
        self._refresh_header_badge(self._last_report)
        self._redraw_radar_bar()

    def _px(self, base: int) -> int:
        return int(round(base * self._dpi_scale))

    def _apply_window_geometry_for_mode(self, initial: bool = False) -> None:
        sw = max(1024, self.winfo_screenwidth())
        sh = max(720, self.winfo_screenheight())
        if self._view_mode == "zen":
            win_w = min(self._px(720), sw - 80)
            win_h = min(self._px(650), sh - 90)
            min_w = min(self._px(580), sw - 120)
            min_h = min(self._px(540), sh - 120)
        else:
            win_w = min(self._px(1160), sw - 80)
            win_h = min(self._px(840), sh - 80)
            min_w = min(self._px(960), sw - 120)
            min_h = min(self._px(680), sh - 120)

        self.minsize(min_w, min_h)
        if initial:
            pos_x = max(20, (sw - win_w) // 2)
            pos_y = max(20, (sh - win_h) // 2 - 15)
            self.geometry(f"{win_w}x{win_h}+{pos_x}+{pos_y}")
        else:
            try:
                cur_x = max(20, min(sw - win_w - 20, self.winfo_x()))
                cur_y = max(20, min(sh - win_h - 40, self.winfo_y()))
                self.geometry(f"{win_w}x{win_h}+{cur_x}+{cur_y}")
            except Exception:
                self.geometry(f"{win_w}x{win_h}")

    def toggle_view_mode(self, target_mode: Optional[str] = None) -> None:
        new_mode = target_mode if target_mode in ("full", "zen") else (
            "zen" if self._view_mode == "full" else "full"
        )
        if new_mode == self._view_mode:
            return
        self._view_mode = new_mode
        _save_ui_prefs(self._view_mode, self._zen_metric)
        self._apply_window_geometry_for_mode(initial=False)
        self._sync_view_mode_widgets()
        self._refresh_active_charts_and_zen()
        self._recalibrate_visible_canvases()

    def select_zen_metric(self, metric_key: str) -> None:
        if metric_key not in ("gemini", "claude", "spend", "latency", "cache"):
            return
        self._zen_metric = metric_key
        _save_ui_prefs(self._view_mode, self._zen_metric)
        self._update_zen_pill_styles()
        self._refresh_active_charts_and_zen()

    def select_full_tab(self, tab_key: str) -> None:
        if tab_key not in self._full_tab_frames:
            return
        self._active_full_tab = tab_key
        for k, frame in self._full_tab_frames.items():
            if k == tab_key:
                frame.pack(fill=tk.BOTH, expand=True)
            else:
                frame.pack_forget()
        for k, btn in self._full_nav_buttons.items():
            active = k == tab_key
            btn.configure(
                bg=ACCENT_CRIMSON if active else BG_CARD,
                fg="#ffffff" if active else FG_MUTED,
            )
        self._recalibrate_visible_canvases()
        host = self.__dict__.get("_plugin_host")
        if host is not None:
            host.notify_tab_selected(tab_key)

    def _recalibrate_visible_canvases(self) -> None:
        """Recalibrate visible canvases after tab switch or tray wakeup to heal 1px unmapped renders."""
        if self.__dict__.get("_closing") or self.__dict__.get("_hidden_to_tray"):
            return
        try:
            self.update_idletasks()
            if self._view_mode == "zen":
                self.zen_dial._redraw(allow_defer=False)
                self.zen_chart._redraw(allow_defer=False)
            else:
                if self._active_full_tab == "hub":
                    self.dial_gemini._redraw(allow_defer=False)
                    self.dial_claude._redraw(allow_defer=False)
                    self.hub_chart._redraw(allow_defer=False)
                    self._redraw_radar_bar(allow_defer=False)
                elif self._active_full_tab == "diag":
                    self.diag_wave_chart._redraw(allow_defer=False)
        except Exception:
            pass

    def select_hub_chart(self, kind: str) -> None:
        if kind not in ("gemini", "claude", "spend"):
            return
        self._hub_chart_kind = kind
        for k, btn in self._hub_chart_btns.items():
            active = k == kind
            btn.configure(
                bg=ACCENT_CRIMSON_DIM if active else BG_CARD_ALT,
                fg=ACCENT_ROSE if active else FG_MUTED,
            )
        self._refresh_active_charts_and_zen()

    def _build_tray_menu(self) -> List[MenuSpec]:
        mode_lbl = (
            "✦ 切换至全盘星轨模式" if self._view_mode == "zen" else "◐ 切换至极简专注模式 (单指标+单图)"
        )
        cur_hud = self.__dict__.get("_hud_mode", "smart")
        m_smart = "● " if cur_hud == "smart" else "○ "
        m_always = "● " if cur_hud == "always" else "○ "
        m_off = "● " if cur_hud == "off" else "○ "
        v_mark = "● " if bool(self.__dict__.get("_vault_auto_enabled", True)) else "○ "
        items: List[MenuSpec] = [
            (f"✦ 打开主控台 ({APP_CODENAME})", self.restore_from_tray),
            (mode_lbl, lambda: self._ui_call(self.toggle_view_mode)),
            (
                f"✂️ 复制上下文压缩口令 (「{COMPACT_COMMAND_TEXT}」· 免200K双倍计价)",
                lambda: self._ui_call(self.copy_compact_command),
            ),
            None,
            (f"{m_smart}🎯 缓存胶囊 · 智能预警 (≥100K临期)", lambda: self._ui_call(lambda: self.set_hud_mode("smart"))),
            (f"{m_always}📌 缓存胶囊 · 常驻桌面", lambda: self._ui_call(lambda: self.set_hud_mode("always"))),
            (f"{m_off}🚫 缓存胶囊 · 关闭悬浮", lambda: self._ui_call(lambda: self.set_hud_mode("off"))),
            None,
            (f"{v_mark}🗄️ 对话自动备份 (防误删保险箱)", lambda: self._ui_call(self._toggle_vault_auto_backup)),
            ("🔍 立即采样配额水位快照", lambda: self._run_action("托盘 · 采样配额水位快照", self._sample_watermark_now)),
            ("🛡️ 启用并注册开机自启守卫", lambda: self._run_action("托盘 · 部署断头台内核守卫", install_guard_autostart)),
            ("⏹️ 停用并卸载断头台守卫", lambda: self._run_action("托盘 · 停用断头台内核守卫", uninstall_guard_autostart)),
        ]
        host = self.__dict__.get("_plugin_host")
        if host is not None:
            plugin_items = host.collect_tray_items()
            if plugin_items:
                if items and items[-1] is not None:
                    items.append(None)
                items.extend(plugin_items)
        items.extend([
            None,
            ("✕ 彻底退出主控台", self.exit_app),
        ])
        return items

    def hide_to_tray(self, notify: bool = True) -> None:
        if self.__dict__.get("_closing"):
            return
        tray = self.__dict__.get("_tray")
        if tray is None or not tray.active:
            self.exit_app()
            return
        self._hidden_to_tray = True
        self._cancel_auto_refresh()
        try:
            self.withdraw()
        except Exception:
            pass
        if "_radar_state" in self.__dict__:
            self._apply_radar_state_to_ui(self._radar_state)
        if notify and not self.__dict__.get("_tray_hint_shown"):
            self._tray_hint_shown = True
            tray.notify_balloon(
                f"{APP_CODENAME} 已驻留托盘",
                "星盘主控台已转入右下角静默休眠。单击托盘图标唤醒，右键可切换极简/全盘视图、管理缓存胶囊、开关对话自动备份或彻底退出。",
            )

    def restore_from_tray(self) -> None:
        if self.__dict__.get("_closing"):
            return
        prefs = _load_ui_prefs()
        desired_mode = str(prefs.get("view_mode") or self._view_mode)
        if desired_mode in ("full", "zen") and desired_mode != self._view_mode:
            self.toggle_view_mode(desired_mode)
        was_hidden = bool(self.__dict__.get("_hidden_to_tray"))
        self._hidden_to_tray = False
        bring_tk_window_to_front(self)
        _apply_win32_dark_chrome(self)
        if "_radar_state" in self.__dict__:
            self._apply_radar_state_to_ui(self._radar_state, force_tooltip=True)
        self._recalibrate_visible_canvases()
        if was_hidden:
            self.refresh_all_async(silent=True)
            self._schedule_auto_refresh()
            if self.auto_refresh_ms > 0:
                self._ensure_radar_loops()

    def exit_app(self) -> None:
        self._closing = True
        self._cancel_auto_refresh()
        defer_id = self.__dict__.get("_radar_defer_id")
        if defer_id is not None:
            try:
                self.after_cancel(defer_id)
            except Exception:
                pass
            self._radar_defer_id = None
            self._radar_defer_pending = False
        self._hidden_to_tray = False
        hud = self.__dict__.get("_hud_win")
        if hud is not None:
            try:
                hud.destroy()
            except Exception:
                pass
            self._hud_win = None
        tray = self.__dict__.get("_tray")
        if tray is not None:
            try:
                tray.stop()
            except Exception:
                pass
        try:
            self.destroy()
        except Exception:
            pass

    def _on_close(self) -> None:
        tray = self.__dict__.get("_tray")
        if tray is not None and tray.active:
            self.hide_to_tray(notify=True)
            return
        self.exit_app()

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
            delay_ms = 400 if self.__dict__.get("_hidden_to_tray") else 50
            self.after(delay_ms, self._pump_ui_queue)
        except Exception:
            pass

    def _configure_styles(self) -> None:
        configure_styles(self)

    def _draw_mini_emblem(self, canvas: tk.Canvas) -> None:
        draw_mini_emblem(self, canvas)

    def _build_header(self) -> None:
        build_header(self)

    def _refresh_header_badge(self, report: Optional[OrionTelemetryReport]) -> None:
        lbl = self.__dict__.get("lbl_header_badge")
        host = self.__dict__.get("_plugin_host")
        if lbl is None or host is None:
            return
        badge = host.resolve_header_badge(report)
        if badge and badge.get("text"):
            lbl.configure(
                text=badge["text"],
                fg=badge.get("fg", ACCENT_CYAN),
                bg=badge.get("bg", ACCENT_CYAN_DIM),
            )
            if not self.__dict__.get("_header_badge_packed", False):
                lbl.pack(side=tk.LEFT, padx=(8, 0))
                self._header_badge_packed = True
        elif self.__dict__.get("_header_badge_packed", False):
            lbl.pack_forget()
            self._header_badge_packed = False

    def _build_body_containers(self) -> None:
        self.body_wrap = tk.Frame(self, bg=BG_ROOT)
        self.body_wrap.pack(fill=tk.BOTH, expand=True, padx=self._px(14), pady=self._px(10))

        self.zen_container = tk.Frame(self.body_wrap, bg=BG_ROOT)
        self.full_container = tk.Frame(self.body_wrap, bg=BG_ROOT)

        self._build_zen_mode_ui()
        self._build_full_mode_ui()

    def _sync_view_mode_widgets(self) -> None:
        if self._view_mode == "zen":
            self.full_container.pack_forget()
            self.zen_container.pack(fill=tk.BOTH, expand=True)
            self.btn_mode_toggle.configure(
                text="✦ 展开完整星盘",
                bg=ACCENT_CRIMSON_DIM,
                fg="#ffffff",
            )
            self.lbl_header_sub.configure(text="极简专注模式  ·  同屏仅聚焦单一核心指标与对应趋势图")
        else:
            self.zen_container.pack_forget()
            self.full_container.pack(fill=tk.BOTH, expand=True)
            self.btn_mode_toggle.configure(
                text="◐ 极简模式 (单指标+单图)",
                bg="#2b1224",
                fg=ACCENT_GOLD,
            )
            self.lbl_header_sub.configure(
                text=f"{APP_TRINITY}  ·  猎户座星盘配额观测台 & 事件驱动断头台"
            )

    def _build_zen_mode_ui(self) -> None:
        build_zen_mode_ui(self)

    def _update_zen_pill_styles(self) -> None:
        for k, b in self._zen_pill_btns.items():
            active = k == self._zen_metric
            b.configure(
                bg=ACCENT_CRIMSON if active else BG_CARD,
                fg="#ffffff" if active else FG_MUTED,
                activebackground=ACCENT_ROSE,
                activeforeground="#ffffff",
            )

    def _build_full_mode_ui(self) -> None:
        nav_bar = tk.Frame(self.full_container, bg=BG_ROOT)
        nav_bar.pack(fill=tk.X, pady=(0, self._px(8)))
        self._full_nav_bar = nav_bar

        nav_specs = [
            ("hub", "🔭 双星盘总览 (Astrolabe)"),
            ("ledger", "📒 Token 账单与估算 (Ledger)"),
            ("vault", "🗄️ 对话防删保险箱 (Vault)"),
            ("diag", "🩺 风控示波与日志 (Diagnostics)"),
        ]
        self._full_nav_buttons: Dict[str, tk.Button] = {}
        for key, txt in nav_specs:
            b = tk.Button(
                nav_bar,
                text=txt,
                command=lambda k=key: self.select_full_tab(k),
                relief="flat",
                padx=self._px(14),
                pady=self._px(6),
                font=("Microsoft YaHei UI", 9, "bold"),
                cursor="hand2",
            )
            b.pack(side=tk.LEFT, padx=(0, 6))
            self._full_nav_buttons[key] = b

        self.tab_hub = tk.Frame(self.full_container, bg=BG_ROOT)
        self.tab_ledger = tk.Frame(self.full_container, bg=BG_ROOT)
        self.tab_vault = tk.Frame(self.full_container, bg=BG_ROOT)
        self.tab_diag = tk.Frame(self.full_container, bg=BG_ROOT)
        self._full_tab_frames: Dict[str, tk.Frame] = {
            "hub": self.tab_hub,
            "ledger": self.tab_ledger,
            "vault": self.tab_vault,
            "diag": self.tab_diag,
        }

        self._build_hub_tab()
        self._build_ledger_tab()
        self._build_vault_tab()
        self._build_diag_tab()
        self.select_full_tab("hub")

    # Built-in component & tab builder delegates (preserved for subclass/test compatibility)
    def _make_instrument_plate(self, parent: tk.Widget, heading: str) -> Tuple[tk.Frame, tk.Label, tk.Label]:
        return make_instrument_plate(self, parent, heading)

    def _create_dark_text_box(
        self,
        parent: tk.Widget,
        fg_color: str = "#e2e8f0",
        font_pt: int = 8,
        height: int = 6,
        pad_xy: Tuple[int, int] = (6, 6),
    ) -> tk.Text:
        return create_dark_text_box(parent, fg_color=fg_color, font_pt=font_pt, height=height, pad_xy=pad_xy)

    def _create_scrolled_treeview(
        self,
        parent: tk.Widget,
        columns: Sequence[str],
        headers: Sequence[Tuple[str, str, int, str]],
        height: int,
        expand: bool = False,
    ) -> ttk.Treeview:
        return create_scrolled_treeview(self, parent, columns, headers, height, expand=expand)

    def _build_hub_tab(self) -> None:
        build_hub_tab(self)

    def _build_ledger_tab(self) -> None:
        build_ledger_tab(self)

    def _build_vault_tab(self) -> None:
        build_vault_tab(self)

    def _build_diag_tab(self) -> None:
        build_diag_tab(self)

    def _build_quota_points(self, col_key: str, live_bucket: Any) -> List[Tuple[str, float]]:
        return build_quota_points(self, col_key, live_bucket)

    def _build_daily_spend_points(self) -> List[Tuple[str, float]]:
        return build_daily_spend_points(self)

    def _refresh_active_charts_and_zen(self) -> None:
        refresh_active_charts_and_zen(self)

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

    def _cancel_auto_refresh(self) -> None:
        handle = self.__dict__.get("_auto_refresh_id")
        if handle is not None:
            try:
                self.after_cancel(handle)
            except Exception:
                pass
            self._auto_refresh_id = None

    def _schedule_auto_refresh(self) -> None:
        if self.auto_refresh_ms > 0 and not self._closing and not self.__dict__.get("_hidden_to_tray"):
            if self.__dict__.get("_auto_refresh_id") is None:
                self._auto_refresh_id = self.after(self._next_auto_delay_ms(), self._on_auto_timer)

    def _next_auto_delay_ms(self) -> int:
        """15s while online; offline / logged-out backs off 30s -> 60s -> 120s to prevent idle polling."""
        base = int(self.auto_refresh_ms)
        streak = int(getattr(self, "_offline_streak", 0))
        if streak <= 0:
            return base
        return int(min(max(base, 120_000), base * (2 ** min(streak, 3))))

    def _on_auto_timer(self) -> None:
        self._auto_refresh_id = None
        if self._closing or self.__dict__.get("_hidden_to_tray"):
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
        q_var = self.__dict__.get("_vault_query_var")
        vault_query = str(q_var.get()).strip() if q_var is not None else ""
        vault_del_only = bool(self.__dict__.get("_vault_deleted_only", False))

        def worker() -> None:
            try:
                rep = collect_telemetry(include_guard_check=True)
                poll_state["offline"] = (not rep.ls_running) or (not rep.quota_buckets)
                self._ledger.record_quota_from_telemetry(rep, event_type="panel_poll", min_interval_sec=300.0)
                daily = self._ledger.get_daily_summary(days=14)
                models = self._ledger.get_model_breakdown(days=14)
                sessions = self._ledger.get_recent_sessions(limit=15)
                audit = self._ledger.compute_rate_audit()
                series_fn = getattr(self._ledger, "get_recent_quota_series", None)
                if callable(series_fn):
                    b_by_id = {b.bucket_id: b for b in rep.quota_buckets}
                    gw_rst = b_by_id["gemini-weekly"].reset_time if "gemini-weekly" in b_by_id else ""
                    pw_rst = b_by_id["3p-weekly"].reset_time if "3p-weekly" in b_by_id else ""
                    q_series = series_fn(
                        hours=24.0,
                        max_points=72,
                        active_gemini_wk_reset=gw_rst,
                        active_p3_wk_reset=pw_rst,
                    )
                else:
                    q_series = []
                guard_lines = read_shared_file_lines(get_guillotine_log_path(), max_tail_bytes=64 * 1024)[-30:]
                ls_tail = read_shared_file_lines(get_ls_log_path(), max_tail_bytes=128 * 1024)[-40:]
                radar_obj = self.__dict__.get("_cache_radar")
                radar_snap = radar_obj.sample() if radar_obj is not None else None
                v_obj = self.__dict__.get("_vault")
                vault_st: Optional[VaultStatusReport] = None
                vault_rows: List[VaultSessionItem] = []
                if v_obj is not None:
                    try:
                        vault_st = v_obj.get_status(refresh_deleted_flags=True)
                        vault_rows = v_obj.list_sessions(deleted_only=vault_del_only, query=vault_query, limit=120)
                    except Exception:
                        vault_st = None

                def _apply_if_current() -> None:
                    if int(self.__dict__.get("_refresh_gen", 0)) != my_gen:
                        return
                    self.__dict__["_last_quota_series"] = q_series
                    if radar_snap is not None:
                        self._radar_state = radar_snap
                    self._apply_ui_data(
                        rep, daily, models, sessions, audit, guard_lines, ls_tail, silent,
                        vault_st=vault_st, vault_rows=vault_rows,
                    )

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

    def _sync_vault_switch_button(self, enabled: bool) -> None:
        self._vault_auto_enabled = bool(enabled)
        btn = self.__dict__.get("_btn_vault_switch")
        if btn is not None:
            try:
                btn.configure(
                    text="🟢 自动备份: 已开启 (点此关闭)" if self._vault_auto_enabled else "⚪ 自动备份: 已关闭 (点此开启)",
                    bg="#065f46" if self._vault_auto_enabled else "#475569",
                )
            except Exception:
                pass

    def _toggle_vault_auto_backup(self) -> None:
        cur = bool(self.__dict__.get("_vault_auto_enabled", True))
        next_act = "off" if cur else "on"
        lbl = "关闭对话自动备份开关" if cur else "开启对话自动备份开关"

        def _do_toggle() -> Dict[str, Any]:
            res = configure_vault_auto_backup(next_act)
            self._ui_call(lambda en=bool(res.get("enabled", not cur)): self._sync_vault_switch_button(en))
            return res

        self._run_action(lbl, _do_toggle)

    def _toggle_vault_filter(self) -> None:
        self._vault_deleted_only = not bool(self.__dict__.get("_vault_deleted_only", False))
        btn = self.__dict__.get("_btn_vault_filter")
        if btn is not None:
            btn.configure(
                text="🗑️ 视图: 仅看已删可恢复" if self._vault_deleted_only else "🔍 视图: 全部归档",
                bg="#9f1239" if self._vault_deleted_only else "#334155",
            )
        self.refresh_all_async(silent=False)

    def _get_selected_vault_conv_id(self) -> str:
        tree = self.__dict__.get("tree_vault")
        if tree is None:
            return ""
        sel = tree.selection()
        if not sel:
            return ""
        vals = tree.item(sel[0], "values")
        return str(vals[6]).strip() if vals and len(vals) >= 7 else ""

    def _on_vault_restore_selected(self) -> None:
        cid = self._get_selected_vault_conv_id()
        if not cid:
            self._append_console("⚠️ 请先在「对话防删保险箱」表格中单击选中要恢复的对话。")
            return
        self._run_action(f"恢复对话 {cid[:8]}", lambda: self._vault.restore_conversation(cid, force=False))

    def _on_vault_export_selected(self) -> None:
        cid = self._get_selected_vault_conv_id()
        if not cid:
            self._append_console("⚠️ 请先在「对话防删保险箱」表格中单击选中要导出 Markdown 的对话。")
            return
        self._run_action(f"导出对话 Markdown {cid[:8]}", lambda: self._vault.export_markdown(cid))

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
        vault_st: Optional[VaultStatusReport] = None,
        vault_rows: Optional[List[VaultSessionItem]] = None,
    ) -> None:
        if self._closing:
            return
        self._last_report = r
        self._last_daily = list(daily)
        self._last_audit = audit
        apply_builtin_tabs_data(
            self,
            r,
            daily,
            models,
            sessions,
            audit,
            guard_lines,
            ls_tail,
            silent,
            vault_st=vault_st,
            vault_rows=vault_rows,
        )
        self._refresh_header_badge(r)
        host = self.__dict__.get("_plugin_host")
        if host is not None:
            host.notify_refresh(
                r,
                {
                    "report": r,
                    "daily": daily,
                    "models": models,
                    "sessions": sessions,
                    "audit": audit,
                    "guard_lines": guard_lines,
                    "ls_tail": ls_tail,
                    "silent": silent,
                    "vault_st": vault_st,
                    "vault_rows": vault_rows,
                    "app": self,
                    "host": host,
                },
            )

    # ------------------------------------------------------------------
    # Passive KV Cache Radar & Floating Capsule Controls (delegated to ui.radar_controller)
    # ------------------------------------------------------------------
    def _get_effective_hud_mode(self) -> str:
        return _rc.get_effective_hud_mode(self)

    def _get_effective_target_spec(self) -> Tuple[str, str]:
        return _rc.get_effective_target_spec(self)

    def _on_radar_card_configure(self, event: tk.Event) -> None:  # type: ignore[type-arg]
        _rc.on_radar_card_configure(self, event)

    def _redraw_radar_bar(self, *, allow_defer: bool = True) -> None:
        _rc.redraw_radar_bar(self, allow_defer=allow_defer)

    def _redraw_radar_bar_later(self) -> None:
        _rc.redraw_radar_bar_later(self)

    def _refresh_hud_mode_buttons(self) -> None:
        _rc.refresh_hud_mode_buttons(self)

    def _refresh_target_switch_button(self) -> None:
        _rc.refresh_target_switch_button(self)

    def _popup_target_menu(self) -> None:
        _rc.popup_target_menu(self)

    def set_radar_target(self, target_mode: str, pinned_conv_id: str = "", persist: bool = True) -> Tuple[str, str]:
        return _rc.set_radar_target(self, target_mode, pinned_conv_id, persist, save_prefs=_save_ui_prefs)

    def copy_compact_command(self) -> str:
        return _rc.copy_compact_command(self)

    def set_hud_mode(self, mode: str, persist: bool = True) -> str:
        return _rc.set_hud_mode(self, mode, persist, save_prefs=_save_ui_prefs)

    def _on_hud_drag_end(self, x: int, y: int) -> None:
        _rc.on_hud_drag_end(self, x, y, save_prefs=_save_ui_prefs)

    def _should_show_hud(self, state: CacheRadarState) -> bool:
        return _rc.should_show_hud(self, state)

    def _track_2x_tier_crossing(self, state: CacheRadarState) -> None:
        _rc.track_2x_tier_crossing(self, state)

    def _apply_radar_state_to_ui(self, state: CacheRadarState, force_tooltip: bool = False) -> None:
        _rc.apply_radar_state_to_ui(self, state, force_tooltip=force_tooltip)

    def _radar_loops_needed(self) -> bool:
        return _rc.radar_loops_needed(self)

    def _schedule_after_safe(self, delay_ms: int, callback: Callable[[], None]) -> bool:
        return _rc.schedule_after_safe(self, delay_ms, callback)

    def _ensure_radar_loops(self) -> None:
        _rc.ensure_radar_loops(self)

    def _on_radar_memory_tick(self) -> None:
        _rc.on_radar_memory_tick(self)

    def _on_radar_bg_poll(self) -> None:
        _rc.on_radar_bg_poll(self)


def launch_panel(
    start_minimized: bool = False,
    initial_mode: Optional[str] = None,
    initial_hud_mode: Optional[str] = None,
    reload_existing: bool = False,
) -> int:
    _enable_high_dpi()
    set_app_user_model_id(APP_USER_MODEL_ID)
    if initial_mode in ("full", "zen") or initial_hud_mode in VALID_HUD_MODES:
        _save_ui_prefs(
            view_mode=initial_mode if initial_mode in ("full", "zen") else None,
            hud_mode=initial_hud_mode if initial_hud_mode in VALID_HUD_MODES else None,
        )
    if reload_existing:
        _terminate_existing_panel_instance()
    elif _activate_existing_panel_window():
        return 0
    app = AcerolaOrionApp(start_minimized=start_minimized, initial_mode=initial_mode)
    app.mainloop()
    return 0


__all__ = [
    "APP_USER_MODEL_ID",
    "TRAY_CLASS_NAME",
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
    "clamp_hud_coords",
    "CacheRadarHudWindow",
    "QuotaBarWidget",
    "AstrolabeDialWidget",
    "OrionChartWidget",
    "PanelPluginHost",
    "AcerolaOrionApp",
    "launch_panel",
]
