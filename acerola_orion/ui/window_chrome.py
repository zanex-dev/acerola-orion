# -*- coding: utf-8 -*-
"""
Win32 DPI / DWM dark chrome, UI preferences JSON persistence, and single-instance
window activation/termination helpers for Acerola-Orion (`acerola_orion.ui.window_chrome`).
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
import math
from pathlib import Path
import sys
import time
import tkinter as tk
from typing import Any, Dict, Optional

from ..cache_radar import normalize_hud_mode, normalize_target_mode
from ..constants import (
    APP_CODENAME,
    APP_TRINITY,
    APP_VERSION,
    atomic_write_text,
    get_data_dir,
    interprocess_file_lock,
)
from ..rpc import terminate_pid_win32
from ..telemetry import load_json_safe
from ..win_tray import signal_existing_instance

APP_USER_MODEL_ID = "Trinity.AcerolaOrion.PublicAstrolabe"
TRAY_CLASS_NAME = "AcerolaOrionPublicAstrolabe_v2"

_UNSET = object()


def _panel_window_title() -> str:
    return f"{APP_CODENAME} v{APP_VERSION} · Astrolabe — {APP_TRINITY}"


def _enable_high_dpi() -> None:
    if sys.platform == "win32":
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:
                pass


def _apply_win32_dark_chrome(win: tk.Tk) -> None:
    """Apply native Windows 10/11 DWM immersive dark titlebar and obsidian caption/border tint."""
    if sys.platform != "win32":
        return
    try:
        win.update_idletasks()
        hwnd = int(win.winfo_id())
        parent = ctypes.windll.user32.GetParent(wintypes.HWND(hwnd))
        target = int(parent) if parent else hwnd
        dwm = ctypes.windll.dwmapi
        dark_val = ctypes.c_int(1)
        for attr in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE
            if dwm.DwmSetWindowAttribute(
                wintypes.HWND(target),
                ctypes.c_uint(attr),
                ctypes.byref(dark_val),
                ctypes.sizeof(dark_val),
            ) == 0:
                break
        # Windows 11 DWMWA_CAPTION_COLOR (35) & DWMWA_BORDER_COLOR (34), COLORREF = 0x00BBGGRR
        cap_color = ctypes.c_uint(0x00180A12)  # #120a18
        bdr_color = ctypes.c_uint(0x00361D26)  # #261d36
        dwm.DwmSetWindowAttribute(
            wintypes.HWND(target), ctypes.c_uint(35), ctypes.byref(cap_color), ctypes.sizeof(cap_color)
        )
        dwm.DwmSetWindowAttribute(
            wintypes.HWND(target), ctypes.c_uint(34), ctypes.byref(bdr_color), ctypes.sizeof(bdr_color)
        )
    except Exception:
        pass


def _get_icon_path() -> Path:
    return Path(__file__).resolve().parent.parent.parent / "assets" / "app.ico"


def _get_ui_prefs_path() -> Path:
    return get_data_dir() / "ui_prefs.json"


def _load_ui_prefs() -> Dict[str, Any]:
    raw = load_json_safe(_get_ui_prefs_path(), default={})
    mode = str(raw.get("view_mode") or "full").strip().lower()
    if mode not in ("full", "zen"):
        mode = "full"
    metric = str(raw.get("zen_metric") or "gemini").strip().lower()
    if metric not in ("gemini", "claude", "spend", "latency", "cache"):
        metric = "gemini"
    hud_mode = normalize_hud_mode(raw.get("hud_mode"))
    t_mode, pin_cid = normalize_target_mode(raw.get("radar_target_mode"), raw.get("radar_pinned_conv_id"))
    raw_x = raw.get("hud_x")
    raw_y = raw.get("hud_y")
    hud_x: Optional[int] = None
    hud_y: Optional[int] = None
    try:
        if raw_x is not None and not isinstance(raw_x, bool) and raw_y is not None and not isinstance(raw_y, bool):
            fx, fy = float(raw_x), float(raw_y)
            if math.isfinite(fx) and math.isfinite(fy):
                hud_x, hud_y = int(fx), int(fy)
    except (TypeError, ValueError):
        hud_x, hud_y = None, None
    return {
        "view_mode": mode,
        "zen_metric": metric,
        "hud_mode": hud_mode,
        "radar_target_mode": t_mode,
        "radar_pinned_conv_id": pin_cid,
        "hud_x": hud_x,
        "hud_y": hud_y,
    }


def _save_ui_prefs(
    view_mode: Optional[str] = None,
    zen_metric: Optional[str] = None,
    *,
    hud_mode: Optional[str] = None,
    radar_target_mode: Optional[str] = None,
    radar_pinned_conv_id: Optional[str] = None,
    hud_x: Any = _UNSET,
    hud_y: Any = _UNSET,
) -> None:
    try:
        prefs_path = _get_ui_prefs_path()
        with interprocess_file_lock(prefs_path):
            cur = _load_ui_prefs()
            if view_mode in ("full", "zen"):
                cur["view_mode"] = view_mode
            if zen_metric in ("gemini", "claude", "spend", "latency", "cache"):
                cur["zen_metric"] = zen_metric
            if hud_mode is not None:
                cur["hud_mode"] = normalize_hud_mode(hud_mode)
            if radar_target_mode is not None or radar_pinned_conv_id is not None:
                tm_in = radar_target_mode if radar_target_mode is not None else cur["radar_target_mode"]
                pc_in = radar_pinned_conv_id if radar_pinned_conv_id is not None else cur["radar_pinned_conv_id"]
                tm_norm, pc_norm = normalize_target_mode(tm_in, pc_in)
                cur["radar_target_mode"] = tm_norm
                cur["radar_pinned_conv_id"] = pc_norm
            if hud_x is not _UNSET:
                cur["hud_x"] = int(hud_x) if hud_x is not None else None
            if hud_y is not _UNSET:
                cur["hud_y"] = int(hud_y) if hud_y is not None else None
            payload = json.dumps(cur, ensure_ascii=False, indent=2)
            atomic_write_text(prefs_path, payload)
    except Exception:
        pass


def _activate_existing_panel_window() -> bool:
    """If an identical-version panel (or tray-resident instance) is already open, restore and focus it."""
    return signal_existing_instance(TRAY_CLASS_NAME, _panel_window_title())


def _terminate_existing_panel_instance() -> None:
    """Cleanly replace an already-running Public Astrolabe panel instance when --reload is requested."""
    if sys.platform != "win32":
        return
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
        user32.FindWindowW.restype = wintypes.HWND
        user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        hwnd = user32.FindWindowW(TRAY_CLASS_NAME, None) or user32.FindWindowW(None, _panel_window_title())
        if hwnd:
            pid = wintypes.DWORD(0)
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value:
                terminate_pid_win32(int(pid.value), allowed_exe_prefixes=("python",))
                time.sleep(0.15)
    except Exception:
        pass


__all__ = [
    "APP_USER_MODEL_ID",
    "TRAY_CLASS_NAME",
    "_UNSET",
    "_panel_window_title",
    "_enable_high_dpi",
    "_apply_win32_dark_chrome",
    "_get_icon_path",
    "_get_ui_prefs_path",
    "_load_ui_prefs",
    "_save_ui_prefs",
    "_activate_existing_panel_window",
    "_terminate_existing_panel_instance",
]
