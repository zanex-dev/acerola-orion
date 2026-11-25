# -*- coding: utf-8 -*-
"""
EOQ Window Guard & Radar Hint Built-in Plugin (`acerola_orion/plugins/eoq_plugin.py`)
- 独立微内核内置白帽插件 (`name = "eoq_guard"`)，纯本地状态读写与零第三方依赖
- 订阅 `enrich_radar` 钩子：写入 `state.extensions["eoq_guard"]` 并在雷达副标题拼入 EOQ 水位/免限提示
- 通过中立 UI 插槽在 5 处提供「🔓 本窗免限 / 🔒 恢复本窗 EOQ 锁定」一键联动：
  1. 雷达栏动作按钮 (`add_radar_action_widget`)
  2. 200K 告警条动作按钮 (`add_radar_tier_action_widget`)
  3. 窗口切换下拉菜单 (`add_radar_menu_items`)
  4. 悬浮胶囊 HUD 右键菜单 (`add_radar_menu_items` 共享)
  5. 系统托盘右键菜单 (`add_tray_items`)
- 监听 `on_radar_state_changed` 同步双按钮视觉状态，切换后推送 `flash_hud_toast` 与 `append_console`
"""

from __future__ import annotations

from pathlib import Path
import tkinter as tk
from typing import Any, Dict, List, Optional, Tuple

from acerola_orion.core.kernel import HookBus, HookEvents

from ._helpers import resolve_callable
from .eoq_guard import (
    format_eoq_radar_hint,
    get_latest_eoq_conv_id,
    is_eoq_window_unrestricted,
    set_eoq_window_unrestricted,
)

EOQ_EXT_KEY = "eoq_guard"

RADAR_BTN_LOCKED = "🔓 本窗免限"
RADAR_BTN_UNLOCKED = "🔓 免限中·点此恢复"

TIER_BTN_LOCKED = "🔓 本窗免限(一直做)"
TIER_BTN_UNLOCKED = "🔓 本窗免限中 (点此恢复)"

MENU_LABEL_UNLOCK = "🔓 本窗免限 (免200K锁/冷窗拦截)"
MENU_LABEL_LOCK = "🔒 恢复本窗 EOQ 锁定"


def _strip_hint_suffix(detail_text: str, old_hint: str) -> str:
    txt = str(detail_text or "")
    hint = str(old_hint or "").strip()
    if not txt or not hint:
        return txt
    sep_hint = f" | {hint}"
    if sep_hint in txt:
        return txt.replace(sep_hint, "")
    if txt == hint:
        return ""
    if txt.startswith(f"{hint} | "):
        return txt[len(hint) + 3 :]
    return txt


class EoqPlugin:
    """Built-in plugin adding EOQ per-window unrestricted toggle and radar hints."""

    name: str = "eoq_guard"

    def __init__(self) -> None:
        self._ui_host: Any = None
        self._btn_radar_unrestrict: Optional[tk.Button] = None
        self._btn_tier_unrestrict: Optional[tk.Button] = None
        self._tier_container: Optional[tk.Widget] = None
        self._last_radar_state: Any = None

    def register_cli(self, subparsers: Any) -> None:
        _ = subparsers

    def handle_cli(self, cmd: str, args: Any) -> Optional[int]:
        _ = (cmd, args)
        return None

    def extra_launchers(
        self, py_exe: str, pyw_exe: str, pkg_root: Path | str
    ) -> Dict[str, str]:
        _ = (py_exe, pyw_exe, pkg_root)
        return {}

    # ------------------------------------------------------------------
    # 1. Kernel Hooks (`enrich_radar`)
    # ------------------------------------------------------------------
    def register_hooks(self, bus: HookBus) -> None:
        bus.on(HookEvents.ENRICH_RADAR, self._on_enrich_radar)

    def _on_enrich_radar(self, state: Any) -> None:
        if state is None:
            return
        cid = str(getattr(state, "conv_id", "") or "").strip()
        unrestricted = is_eoq_window_unrestricted(cid) if cid else False
        hint = format_eoq_radar_hint(state, unrestricted)

        exts = getattr(state, "extensions", None)
        prev_hint = ""
        if isinstance(exts, dict):
            prev_payload = exts.get(EOQ_EXT_KEY)
            if isinstance(prev_payload, dict):
                prev_hint = str(prev_payload.get("hint") or "")
            exts[EOQ_EXT_KEY] = {
                "conv_id": cid,
                "unrestricted": unrestricted,
                "hint": hint,
            }

        if hasattr(state, "detail_text"):
            detail = str(getattr(state, "detail_text", "") or "")
            if prev_hint and prev_hint != hint:
                detail = _strip_hint_suffix(detail, prev_hint)
            if hint and hint not in detail:
                detail = f"{detail} | {hint}" if detail else hint
            state.detail_text = detail

    # ------------------------------------------------------------------
    # 2. Target Window Resolution & State Sync
    # ------------------------------------------------------------------
    def _get_app(self) -> Any:
        host = self._ui_host
        return getattr(host, "app", host) if host is not None else None

    def _resolve_active_conv_id(
        self,
        state: Any = None,
        *,
        prefer_unlocked: bool = False,
    ) -> str:
        # Strict priority: state.conv_id -> app._pinned_conv_id -> latest session in state file
        st = state
        app = self._get_app()
        if st is None:
            if app is not None:
                st = getattr(app, "__dict__", {}).get("_radar_state")
            if st is None:
                st = self._last_radar_state
        cid = str(getattr(st, "conv_id", "") or "").strip() if st is not None else ""
        if cid:
            return cid

        if app is not None:
            pinned = str(getattr(app, "_pinned_conv_id", "") or "").strip()
            if pinned:
                return pinned

        return get_latest_eoq_conv_id(prefer_unlocked=prefer_unlocked)

    def _is_current_unrestricted(self, state: Any = None) -> bool:
        cid = self._resolve_active_conv_id(state=state, prefer_unlocked=True)
        return is_eoq_window_unrestricted(cid) if cid else False

    def _sync_buttons(self, state: Any = None, *, unrestricted: Optional[bool] = None) -> None:
        is_free = self._is_current_unrestricted(state=state) if unrestricted is None else bool(unrestricted)

        btn_radar = self._btn_radar_unrestrict
        if btn_radar is not None:
            try:
                if is_free:
                    btn_radar.configure(
                        text=RADAR_BTN_UNLOCKED,
                        bg="#064e3b",
                        fg="#6ee7b7",
                        activebackground="#065f46",
                        activeforeground="#ecfdf5",
                    )
                else:
                    btn_radar.configure(
                        text=RADAR_BTN_LOCKED,
                        bg="#1f1630",
                        fg="#fbbf24",
                        activebackground="#2e2046",
                        activeforeground="#fef3c7",
                    )
            except Exception:
                pass

        tier_box = self._tier_container
        if tier_box is not None:
            try:
                app = self._get_app()
                slot = getattr(app, "__dict__", {}).get("radar_tier_plugin_actions") if app is not None else None
                if slot is not None:
                    tier_box.configure(bg=slot.cget("bg"))
            except Exception:
                pass

        btn_tier = self._btn_tier_unrestrict
        if btn_tier is not None:
            try:
                if is_free:
                    btn_tier.configure(
                        text=TIER_BTN_UNLOCKED,
                        bg="#065f46",
                        fg="#ecfdf5",
                        activebackground="#047857",
                        activeforeground="#ffffff",
                    )
                else:
                    btn_tier.configure(
                        text=TIER_BTN_LOCKED,
                        bg="#4c1d95",
                        fg="#f5f3ff",
                        activebackground="#5b21b6",
                        activeforeground="#ffffff",
                    )
            except Exception:
                pass

    def _on_radar_state_changed(self, state: Any, _host: Any = None) -> None:
        self._last_radar_state = state
        self._sync_buttons(state=state)

    def _build_menu_items(self, source: str) -> List[Tuple[str, Any]]:
        is_free = self._is_current_unrestricted()
        if is_free:
            return [(MENU_LABEL_LOCK, lambda s=source: self._toggle_current_window(False, source=s))]
        return [(MENU_LABEL_UNLOCK, lambda s=source: self._toggle_current_window(True, source=s))]

    def _refresh_radar_after_toggle(self) -> None:
        app = self._get_app()
        if app is None:
            return
        app_dict = getattr(app, "__dict__", {})
        radar = app_dict.get("_cache_radar")
        apply_fn = getattr(app, "_apply_radar_state_to_ui", None)
        if radar is not None and callable(getattr(radar, "sample", None)):
            try:
                st = radar.sample()
                if callable(apply_fn):
                    apply_fn(st, force_tooltip=True)
                else:
                    self._last_radar_state = st
                    self._sync_buttons(state=st)
                return
            except Exception:
                pass

        st = app_dict.get("_radar_state", self._last_radar_state)
        if st is not None:
            self._on_enrich_radar(st)
            if callable(apply_fn):
                try:
                    apply_fn(st, force_tooltip=True)
                    return
                except Exception:
                    pass
            self._sync_buttons(state=st)

    def _toggle_current_window(
        self,
        enabled: Optional[bool] = None,
        *,
        source: str = "panel",
    ) -> Tuple[bool, str]:
        if enabled is None:
            cur_cid = self._resolve_active_conv_id(prefer_unlocked=True)
            cur_flag = is_eoq_window_unrestricted(cur_cid) if cur_cid else False
            target_enabled = not cur_flag
        else:
            target_enabled = bool(enabled)

        target_cid = self._resolve_active_conv_id(prefer_unlocked=(not target_enabled))
        ok, msg = set_eoq_window_unrestricted(target_cid, target_enabled, source=source)

        host = self._ui_host
        app = self._get_app()

        if ok:
            self._sync_buttons(unrestricted=target_enabled)
            self._refresh_radar_after_toggle()
            toast = (
                f"🔓 本窗 {target_cid[:8]} 已开启免限 (免200K锁/冷窗拦截)"
                if target_enabled
                else f"🔒 本窗 {target_cid[:8]} 已恢复 EOQ 保护"
            )
        else:
            toast = msg

        flash_fn = (
            resolve_callable(host, "flash_hud_toast")
            if host is not None
            else None
        )
        if flash_fn is not None:
            try:
                flash_fn(toast, duration_sec=2.2)
            except TypeError:
                try:
                    flash_fn(toast)
                except Exception:
                    pass
            except Exception:
                pass

        log_fn = (
            (resolve_callable(host, "append_console", "_append_console") if host is not None else None)
            or (resolve_callable(app, "append_console", "_append_console") if app is not None else None)
        )
        if log_fn is not None:
            try:
                log_fn(msg)
            except Exception:
                pass

        return ok, msg

    # ------------------------------------------------------------------
    # 3. Panel UI Slot Registration (5-Way Synchronized Entry Points)
    # ------------------------------------------------------------------
    def register_ui(self, ui_host: Any) -> None:
        self._ui_host = ui_host

        # 1) Radar header action button
        add_radar_action = getattr(ui_host, "add_radar_action_widget", None)
        if callable(add_radar_action):
            def _build_radar_btn(parent: tk.Widget, host: Any = None) -> tk.Button:
                ctx = host if host is not None else ui_host
                px_fn = resolve_callable(ctx, "px", "_px") or (lambda n: int(n))
                btn = tk.Button(
                    parent,
                    text=RADAR_BTN_LOCKED,
                    command=lambda: self._toggle_current_window(None, source="radar_bar"),
                    bg="#1f1630",
                    fg="#fbbf24",
                    activebackground="#2e2046",
                    activeforeground="#fef3c7",
                    relief="flat",
                    bd=0,
                    padx=px_fn(8),
                    pady=px_fn(2),
                    font=("Microsoft YaHei UI", 8, "bold"),
                    cursor="hand2",
                )
                btn.pack(side=tk.LEFT)
                self._btn_radar_unrestrict = btn
                return btn

            add_radar_action(_build_radar_btn)

        # 2) 200K tier alert bar action button
        add_tier_action = getattr(ui_host, "add_radar_tier_action_widget", None)
        if callable(add_tier_action):
            def _build_tier_btn(parent: tk.Widget, host: Any = None) -> tk.Button:
                ctx = host if host is not None else ui_host
                px_fn = resolve_callable(ctx, "px", "_px") or (lambda n: int(n))
                self._tier_container = parent
                btn = tk.Button(
                    parent,
                    text=TIER_BTN_LOCKED,
                    command=lambda: self._toggle_current_window(None, source="tier_bar"),
                    bg="#4c1d95",
                    fg="#f5f3ff",
                    activebackground="#5b21b6",
                    activeforeground="#ffffff",
                    relief="flat",
                    bd=0,
                    padx=px_fn(8),
                    pady=px_fn(2),
                    font=("Microsoft YaHei UI", 8, "bold"),
                    cursor="hand2",
                )
                btn.pack(side=tk.LEFT)
                self._btn_tier_unrestrict = btn
                return btn

            add_tier_action(_build_tier_btn)

        # 3 & 4) Shared menu items for Target Dropdown + Floating Capsule HUD right-click menu
        add_radar_menu = getattr(ui_host, "add_radar_menu_items", None)
        if callable(add_radar_menu):
            add_radar_menu(lambda: self._build_menu_items(source="radar_menu"))

        # 5) System tray right-click menu item
        add_tray = getattr(ui_host, "add_tray_items", None)
        if callable(add_tray):
            add_tray(lambda: self._build_menu_items(source="tray"))

        # Radar state listener to keep both buttons synchronized
        on_radar_state = getattr(ui_host, "on_radar_state_changed", None)
        if callable(on_radar_state):
            on_radar_state(self._on_radar_state_changed)

        self._sync_buttons()


__all__ = [
    "EOQ_EXT_KEY",
    "RADAR_BTN_LOCKED",
    "RADAR_BTN_UNLOCKED",
    "TIER_BTN_LOCKED",
    "TIER_BTN_UNLOCKED",
    "MENU_LABEL_UNLOCK",
    "MENU_LABEL_LOCK",
    "EoqPlugin",
]
