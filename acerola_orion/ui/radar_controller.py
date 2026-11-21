# -*- coding: utf-8 -*-
"""
Pure-function KV Cache Radar & Floating Capsule HUD controller helpers
for Acerola-Orion (`acerola_orion.ui.radar_controller`).

All functions accept `app` (typically an `AcerolaOrionApp` instance or a bare
`object.__new__(AcerolaOrionApp)` test stub) and access optional attributes via
`app.__dict__` so callers never depend on `tk.Tk.__init__` state.
"""

from __future__ import annotations

import threading
import time
import tkinter as tk
from typing import Any, Callable, Dict, Tuple

from ..cache_radar import (
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
    CacheRadarState,
    format_tokens_compact,
    normalize_hud_mode,
    normalize_target_mode,
)
from ..constants import APP_CODENAME, APP_VERSION
from .widgets import (
    ACCENT_AMBER,
    ACCENT_CRIMSON,
    ACCENT_CRIMSON_DIM,
    ACCENT_CYAN,
    ACCENT_CYAN_DIM,
    ACCENT_ROSE,
    BG_CARD_ALT,
    BG_VOID,
    BORDER_SUBTLE,
    FG_MUTED,
    FG_TEXT,
    HUD_MODE_LABELS,
    TARGET_MODE_LABELS,
    _compose_tray_tooltip,
    _radar_palette,
)
from .window_chrome import _save_ui_prefs


def get_effective_hud_mode(app: Any) -> str:
    return normalize_hud_mode(app.__dict__.get("_hud_mode", "smart"))


def get_effective_target_spec(app: Any) -> Tuple[str, str]:
    return normalize_target_mode(
        app.__dict__.get("_target_mode", TARGET_MODE_SMART_GUARD),
        app.__dict__.get("_pinned_conv_id", ""),
    )


def on_radar_card_configure(app: Any, event: tk.Event) -> None:  # type: ignore[type-arg]
    lbl = app.__dict__.get("lbl_radar_sub")
    if lbl is not None:
        lbl.configure(wraplength=max(360, int(event.width) - 28))
    lbl_tier = app.__dict__.get("lbl_radar_tier_alert")
    if lbl_tier is not None:
        lbl_tier.configure(wraplength=max(260, int(event.width) - app._px(155)))
    canvas = app.__dict__.get("radar_canvas")
    if canvas is not None and int(canvas.winfo_width()) <= 10:
        app._redraw_radar_bar(allow_defer=False)


def redraw_radar_bar(app: Any, *, allow_defer: bool = True) -> None:
    canvas = app.__dict__.get("radar_canvas")
    if canvas is None:
        return
    canvas.delete("all")
    cw = int(canvas.winfo_width())
    ch = int(canvas.winfo_height())
    if cw <= 10:
        card = app.__dict__.get("radar_card")
        if card and int(card.winfo_width()) > 30:
            cw = max(10, int(card.winfo_width()) - 28)
        elif allow_defer and not app.__dict__.get("_radar_defer_pending", False):
            app._radar_defer_pending = True
            app._radar_defer_id = app.after_idle(app._redraw_radar_bar_later)
    w = max(10, cw)
    h = max(4, ch)
    canvas.create_rectangle(0, 0, w, h, fill=BG_VOID, outline="")
    frac = max(0.0, min(1.0, float(app.__dict__.get("_radar_bar_fraction", 0.0))))
    fw = int(round(w * frac))
    if fw > 0:
        canvas.create_rectangle(0, 0, fw, h, fill=app.__dict__.get("_radar_bar_color", BORDER_SUBTLE), outline="")
    # 60s last-minute warning tick (24% of 250s horizon)
    warn_x = int(round(w * (60.0 / CACHE_SAFE_TTL_SEC)))
    if 2 < warn_x < w - 2:
        canvas.create_line(warn_x, 0, warn_x, h, fill=ACCENT_CRIMSON_DIM, width=1)
    # Optional plugin-supplied radar tick markers
    host = app.__dict__.get("_plugin_host")
    if host is not None:
        state = app.__dict__.get("_radar_state", CacheRadarState())
        for marker in host.collect_radar_markers(state):
            mx = int(round(w * marker["fraction"]))
            if 1 < mx < w - 1:
                tags = ("plugin_radar_marker", marker["tag"]) if marker.get("tag") else ("plugin_radar_marker",)
                line_kw: Dict[str, Any] = {
                    "fill": marker["color"],
                    "width": marker["width"],
                    "tags": tags,
                }
                if marker.get("dash"):
                    line_kw["dash"] = marker["dash"]
                canvas.create_line(mx, 0, mx, h, **line_kw)


def redraw_radar_bar_later(app: Any) -> None:
    app._radar_defer_pending = False
    app._radar_defer_id = None
    if not app.__dict__.get("_closing") and app.winfo_exists():
        app._redraw_radar_bar(allow_defer=False)


def refresh_hud_mode_buttons(app: Any) -> None:
    btns = app.__dict__.get("_hud_mode_buttons") or {}
    cur = app._get_effective_hud_mode()
    for k, btn in btns.items():
        if k == cur:
            btn.configure(bg=ACCENT_CRIMSON, fg="#ffffff")
        else:
            btn.configure(bg=BG_CARD_ALT, fg=FG_MUTED)


def refresh_target_switch_button(app: Any) -> None:
    btn = app.__dict__.get("btn_target_switch")
    if btn is None:
        return
    t_mode, pin_cid = app._get_effective_target_spec()
    if t_mode == TARGET_MODE_PINNED and pin_cid:
        btn.configure(text=f"📌 锁 {pin_cid[:6]} ▾", bg=ACCENT_CRIMSON_DIM, fg=ACCENT_ROSE)
    elif t_mode == TARGET_MODE_LATEST:
        btn.configure(text="🔄 跟最新 ▾", bg=BG_CARD_ALT, fg=FG_TEXT)
    else:
        btn.configure(text="🧠 守大窗 ▾", bg=ACCENT_CYAN_DIM, fg=ACCENT_CYAN)


def popup_target_menu(app: Any) -> None:
    btn = app.__dict__.get("btn_target_switch")
    if btn is None:
        return
    menu = app.__dict__.get("_target_menu")
    if menu is None:
        menu = tk.Menu(
            app,
            tearoff=0,
            bg="#16111f",
            fg=FG_TEXT,
            activebackground=ACCENT_CRIMSON,
            activeforeground="#ffffff",
            font=("Microsoft YaHei UI", 9),
        )
        app._target_menu = menu
    menu.delete(0, tk.END)

    t_mode, pin_cid = app._get_effective_target_spec()
    menu.add_command(
        label=f"{'● ' if t_mode == TARGET_MODE_SMART_GUARD else '○ '}🧠 智能守大窗 (≥100K未冷优先 · 推荐)",
        command=lambda: app.set_radar_target(TARGET_MODE_SMART_GUARD, ""),
    )
    menu.add_command(
        label=f"{'● ' if t_mode == TARGET_MODE_LATEST else '○ '}🔄 纯跟随最新活跃窗口",
        command=lambda: app.set_radar_target(TARGET_MODE_LATEST, ""),
    )
    menu.add_separator()

    radar = app.__dict__.get("_cache_radar")
    sessions = radar.list_recent_sessions(limit=8) if radar is not None else []
    if not sessions:
        menu.add_command(label="  (暂无可锁定的历史会话记录)", state=tk.DISABLED)
    else:
        for cand in sessions:
            is_pin = t_mode == TARGET_MODE_PINNED and pin_cid and cand.conv_id.lower() == pin_cid.lower()
            mark = "● " if is_pin else "○ "
            menu.add_command(
                label=f"{mark}📌 锁定: {cand.menu_label}",
                command=lambda cid=cand.conv_id: app.set_radar_target(TARGET_MODE_PINNED, cid),
            )

    try:
        bx = int(btn.winfo_rootx())
        by = int(btn.winfo_rooty() + btn.winfo_height() + 2)
        menu.tk_popup(bx, by)
    finally:
        try:
            menu.grab_release()
        except Exception:
            pass


def set_radar_target(
    app: Any,
    target_mode: str,
    pinned_conv_id: str = "",
    persist: bool = True,
    *,
    save_prefs: Callable[..., None] = _save_ui_prefs,
) -> Tuple[str, str]:
    radar = app.__dict__.get("_cache_radar")
    if radar is not None:
        norm_mode, norm_pin = radar.set_target(target_mode, pinned_conv_id)
        st = radar.sample()
    else:
        norm_mode, norm_pin = normalize_target_mode(target_mode, pinned_conv_id)
        st = app.__dict__.get("_radar_state", CacheRadarState())
    app._target_mode = norm_mode
    app._pinned_conv_id = norm_pin
    app._radar_state = st
    app._refresh_target_switch_button()
    app._apply_radar_state_to_ui(st, force_tooltip=True)
    if app.auto_refresh_ms > 0:
        app._ensure_radar_loops()
    if persist:
        save_prefs(radar_target_mode=norm_mode, radar_pinned_conv_id=norm_pin)
    if "txt_console" in app.__dict__:
        if norm_mode == TARGET_MODE_PINNED and norm_pin:
            title_hint = f"「{st.conv_title}」" if getattr(st, "conv_title", "") else ""
            app._append_console(f"✦ 缓存雷达已手动锁定监控窗口: {norm_pin[:8]} {title_hint}".strip())
        else:
            app._append_console(
                f"✦ 缓存雷达监控策略已切换为: [{TARGET_MODE_LABELS.get(norm_mode, norm_mode)}]"
            )
    return norm_mode, norm_pin


def copy_compact_command(app: Any) -> str:
    txt = COMPACT_COMMAND_TEXT
    app._last_copied_text = txt
    try:
        app.clipboard_clear()
        app.clipboard_append(txt)
        app.update_idletasks()
    except Exception:
        pass
    hud = app.__dict__.get("_hud_win")
    if hud is not None:
        try:
            hud.flash_toast(f"✓ 已复制「{txt}」· 粘贴压缩防 2x 翻倍", duration_sec=1.8)
        except Exception:
            pass
    if "txt_console" in app.__dict__:
        app._append_console(
            f"✓ 已复制上下文压缩口令「{txt}」到剪贴板 — 粘贴到当前超长窗口发送即可压缩上下文、避开 200K 双倍 (2x) 计价。"
        )
    return txt


def set_hud_mode(
    app: Any,
    mode: str,
    persist: bool = True,
    *,
    save_prefs: Callable[..., None] = _save_ui_prefs,
) -> str:
    norm = normalize_hud_mode(mode)
    app._hud_mode = norm
    app._refresh_hud_mode_buttons()
    radar = app.__dict__.get("_cache_radar")
    st = radar.evaluate_cached() if radar is not None else app.__dict__.get("_radar_state", CacheRadarState())
    app._radar_state = st
    app._apply_radar_state_to_ui(st, force_tooltip=True)
    if app.auto_refresh_ms > 0:
        app._ensure_radar_loops()
    if persist:
        save_prefs(hud_mode=norm)
    if "txt_console" in app.__dict__:
        app._append_console(f"✦ 缓存悬浮胶囊模式已切换为: [{HUD_MODE_LABELS.get(norm, norm)}]")
    return norm


def on_hud_drag_end(
    app: Any,
    x: int,
    y: int,
    *,
    save_prefs: Callable[..., None] = _save_ui_prefs,
) -> None:
    app._hud_pos = (int(x), int(y))
    save_prefs(hud_x=int(x), hud_y=int(y))


def should_show_hud(app: Any, state: CacheRadarState) -> bool:
    mode = app._get_effective_hud_mode()
    if mode == "always":
        return True
    if mode == "smart":
        if bool(state.should_pop_smart_hud):
            return True
        until_m = float(app.__dict__.get("_tier_hud_until_mono", 0.0))
        if until_m > 0.0 and time.monotonic() < until_m and (
            bool(getattr(state, "is_near_2x", False)) or bool(getattr(state, "is_over_2x", False))
        ):
            return True
    return False


def track_2x_tier_crossing(app: Any, state: CacheRadarState) -> None:
    cid = str(getattr(state, "conv_id", "") or "").strip().lower()
    if not cid:
        return
    levels = app.__dict__.get("_notified_tier_levels")
    if not isinstance(levels, dict):
        levels = {}
        app.__dict__["_notified_tier_levels"] = levels

    is_2x = bool(getattr(state, "is_over_2x", False))
    is_near = bool(getattr(state, "is_near_2x", False))
    cur_lvl = 2 if is_2x else (1 if is_near else 0)
    prev_lvl = int(levels.get(cid, 0))

    if cur_lvl < prev_lvl:
        levels[cid] = cur_lvl
        if cur_lvl == 0:
            app._tier_hud_until_mono = 0.0
        return

    if cur_lvl > prev_lvl and state.phase in (PHASE_ACTIVE, PHASE_HOT, PHASE_WARNING):
        levels[cid] = cur_lvl
        if len(levels) > 64:
            oldest_cid = next(iter(levels))
            if oldest_cid != cid:
                levels.pop(oldest_cid, None)
        app._tier_hud_until_mono = time.monotonic() + 12.0
        hud = app.__dict__.get("_hud_win")
        if cur_lvl == 2:
            toast_msg = f"💸 {state.context_label} 已超200K！再聊下去双倍计价(2x)"
            log_msg = (
                f"💸 [200K双倍计价告警] 会话 {cid[:8]} 上下文已达 {state.context_label} (≥200K)，"
                f"再聊下去每轮均按 2x 双倍扣减配额！建议执行 {COMPACT_COMMAND_TEXT} 或开新窗口。"
            )
        else:
            gap_lbl = format_tokens_compact(max(1, int(getattr(state, "tokens_to_2x", 0))))
            toast_msg = f"⚠️ {state.context_label} 逼近200K！仅差{gap_lbl}触发2x计价"
            log_msg = (
                f"⚠️ [逼近200K翻倍线] 会话 {cid[:8]} 上下文已达 {state.context_label}（距 200K 仅差 {gap_lbl}），"
                f"继续长聊即将触发 2x 双倍计价，建议适时 {COMPACT_COMMAND_TEXT}。"
            )
        if hud is not None:
            try:
                hud.flash_toast(toast_msg, duration_sec=8.0)
            except Exception:
                pass
        if "txt_console" in app.__dict__:
            app._append_console(log_msg)


def apply_radar_state_to_ui(app: Any, state: CacheRadarState, force_tooltip: bool = False) -> None:
    if app.__dict__.get("_closing"):
        return
    app._radar_state = state
    app._track_2x_tier_crossing(state)
    fg_color, bar_color, border_color = _radar_palette(state)

    if not app.__dict__.get("_hidden_to_tray"):
        if "lbl_radar_main" in app.__dict__:
            app.lbl_radar_main.configure(text=state.badge_text, fg=fg_color)
            app.lbl_radar_sub.configure(
                text=state.detail_text,
                fg=ACCENT_ROSE
                if (state.phase == PHASE_COLD and getattr(state, "is_over_2x", False))
                else (
                    ACCENT_AMBER
                    if (state.phase == PHASE_COLD and state.context_tokens >= LARGE_CONTEXT_HINT_TOKENS)
                    else FG_MUTED
                ),
            )

            badge_lbl = app.__dict__.get("lbl_radar_tier_badge")
            tier_badge_txt = str(getattr(state, "tier_badge_text", "") or "")
            if badge_lbl is not None:
                if tier_badge_txt:
                    is_2x = bool(getattr(state, "is_over_2x", False))
                    badge_lbl.configure(
                        text=tier_badge_txt,
                        bg="#4c0519" if is_2x else "#451a03",
                        fg="#fda4af" if is_2x else "#fcd34d",
                    )
                    if not app.__dict__.get("_radar_tier_badge_packed", False):
                        badge_lbl.pack(side=tk.LEFT, padx=(6, 4))
                        app._radar_tier_badge_packed = True
                elif app.__dict__.get("_radar_tier_badge_packed", False):
                    badge_lbl.pack_forget()
                    app._radar_tier_badge_packed = False

            tier_bar = app.__dict__.get("radar_tier_bar")
            tier_alert_lbl = app.__dict__.get("lbl_radar_tier_alert")
            btn_compact = app.__dict__.get("btn_copy_compact")
            tier_alert_txt = str(getattr(state, "tier_alert_text", "") or "")
            if tier_bar is not None and tier_alert_lbl is not None:
                if tier_alert_txt:
                    is_2x = bool(getattr(state, "is_over_2x", False))
                    bar_bg = "#2a0a18" if is_2x else "#291805"
                    bar_bd = ACCENT_CRIMSON if is_2x else ACCENT_AMBER
                    bar_fg = "#fda4af" if is_2x else "#fcd34d"
                    tier_bar.configure(bg=bar_bg, highlightbackground=bar_bd)
                    tier_alert_lbl.configure(text=tier_alert_txt, bg=bar_bg, fg=bar_fg)
                    if btn_compact is not None:
                        btn_compact.configure(
                            bg=ACCENT_CRIMSON if is_2x else "#b45309",
                            fg="#ffffff",
                        )
                    if not app.__dict__.get("_radar_tier_bar_packed", False):
                        tier_bar.pack(fill=tk.X, pady=(5, 0))
                        app._radar_tier_bar_packed = True
                elif app.__dict__.get("_radar_tier_bar_packed", False):
                    tier_bar.pack_forget()
                    app._radar_tier_bar_packed = False

            app.radar_card.configure(highlightbackground=border_color)
            app._radar_bar_fraction = state.ttl_fraction
            app._radar_bar_color = bar_color
            app._redraw_radar_bar()
        if app.__dict__.get("_view_mode") == "zen" and app.__dict__.get("_zen_metric") == "cache":
            app._refresh_active_charts_and_zen()

    hud = app.__dict__.get("_hud_win")
    if hud is not None:
        hud.render_state(state, visible=app._should_show_hud(state))

    tray = app.__dict__.get("_tray")
    if tray is not None and tray.active:
        now_m = time.monotonic()
        hidden = bool(app.__dict__.get("_hidden_to_tray"))
        if force_tooltip or (not hidden) or (now_m - float(app.__dict__.get("_last_tray_tip_mono", 0.0)) >= 5.0):
            base_tip = str(app.__dict__.get("_last_base_tooltip") or f"{APP_CODENAME} v{APP_VERSION}")
            full_tip = _compose_tray_tooltip(base_tip, state)
            if force_tooltip or full_tip != app.__dict__.get("_last_sent_tooltip"):
                app._last_sent_tooltip = full_tip
                app._last_tray_tip_mono = now_m
                tray.update_tooltip(full_tip)


def radar_loops_needed(app: Any) -> bool:
    if app.__dict__.get("_closing") or app.auto_refresh_ms <= 0:
        return False
    return (not bool(app.__dict__.get("_hidden_to_tray"))) or (app._get_effective_hud_mode() != "off")


def schedule_after_safe(app: Any, delay_ms: int, callback: Callable[[], None]) -> bool:
    if "tk" not in app.__dict__ and "after" not in app.__dict__:
        return False
    try:
        app.after(delay_ms, callback)
        return True
    except Exception:
        return False


def ensure_radar_loops(app: Any) -> None:
    if not app._radar_loops_needed():
        return
    if not app.__dict__.get("_radar_tick_scheduled"):
        if app._schedule_after_safe(1000, app._on_radar_memory_tick):
            app._radar_tick_scheduled = True
    if not app.__dict__.get("_radar_poll_scheduled"):
        if app._schedule_after_safe(400, app._on_radar_bg_poll):
            app._radar_poll_scheduled = True


def on_radar_memory_tick(app: Any) -> None:
    app._radar_tick_scheduled = False
    if not app._radar_loops_needed():
        return
    radar = app.__dict__.get("_cache_radar")
    if radar is not None:
        st = radar.evaluate_cached()
        app._apply_radar_state_to_ui(st)
    if app._radar_loops_needed():
        if app._schedule_after_safe(1000, app._on_radar_memory_tick):
            app._radar_tick_scheduled = True


def on_radar_bg_poll(app: Any) -> None:
    app._radar_poll_scheduled = False
    if not app._radar_loops_needed():
        return
    radar = app.__dict__.get("_cache_radar")
    if radar is not None and not app.__dict__.get("_radar_poll_busy"):
        app._radar_poll_busy = True

        def _poll_worker() -> None:
            try:
                st = radar.sample()
                app._ui_call(lambda s=st: app._apply_radar_state_to_ui(s))
            except Exception:
                pass
            finally:
                app._ui_call(lambda: setattr(app, "_radar_poll_busy", False))

        threading.Thread(target=_poll_worker, daemon=True).start()

    if app._radar_loops_needed():
        if app._schedule_after_safe(3000, app._on_radar_bg_poll):
            app._radar_poll_scheduled = True


__all__ = [
    "get_effective_hud_mode",
    "get_effective_target_spec",
    "on_radar_card_configure",
    "redraw_radar_bar",
    "redraw_radar_bar_later",
    "refresh_hud_mode_buttons",
    "refresh_target_switch_button",
    "popup_target_menu",
    "set_radar_target",
    "copy_compact_command",
    "set_hud_mode",
    "on_hud_drag_end",
    "should_show_hud",
    "track_2x_tier_crossing",
    "apply_radar_state_to_ui",
    "radar_loops_needed",
    "schedule_after_safe",
    "ensure_radar_loops",
    "on_radar_memory_tick",
    "on_radar_bg_poll",
]
