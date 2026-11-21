# -*- coding: utf-8 -*-
"""
Acerola-Orion 面板插件宿主 (acerola_orion/ui/plugin_host.py)
- 向微内核插件 (`OrionPlugin.register_ui(ui_host)`) 开放中立 UI 扩展插槽：
  1. `add_tab(key, title, factory_fn, on_select=None, on_refresh=None)`
  2. `add_hub_action_panel(factory_fn, *, framed=True)`（无插件时 0px 零占位）
  3. `add_tray_items(items)`（静态列表或动态构造器）
  4. `add_radar_markers(fn)` / `format_header_badge(fn)`
- 全部回调均做参数元数自适应与单插件异常隔离，杜绝插件异常破坏主控台。
"""

from __future__ import annotations

import inspect
import logging
import math
import tkinter as tk
from tkinter import ttk
from typing import TYPE_CHECKING, Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple, Union

from ..cache_radar import CACHE_SAFE_TTL_SEC, CacheRadarState
from ..telemetry import OrionTelemetryReport
from ..win_tray import MenuSpec
from .widgets import (
    ACCENT_CRIMSON,
    ACCENT_CYAN,
    ACCENT_CYAN_DIM,
    ACCENT_GOLD,
    BG_CARD,
    BG_ROOT,
    BORDER_SUBTLE,
    FG_MUTED,
    PALETTE,
)

if TYPE_CHECKING:
    from ..panel import AcerolaOrionApp

logger = logging.getLogger("acerola_orion.ui.plugin_host")

_BUILTIN_TAB_KEYS = frozenset(("hub", "ledger", "vault", "diag"))


def _count_positional_params(fn: Callable[..., Any]) -> Tuple[int, bool]:
    """Return `(named_positional_count, has_var_positional)` for flexible plugin callback adaptation."""
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return (1, True)
    named = 0
    has_varargs = False
    for p in sig.parameters.values():
        if p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD):
            named += 1
        elif p.kind == inspect.Parameter.VAR_POSITIONAL:
            has_varargs = True
    return (named, has_varargs)


def _invoke_with_arity(fn: Callable[..., Any], all_args: Tuple[Any, ...], default_vararg_arity: int = 1) -> Any:
    """Invoke `fn` passing only the positional arguments it declares (gracefully supporting 0/1/2-arg callbacks)."""
    named, has_varargs = _count_positional_params(fn)
    if named == 0 and not has_varargs:
        return fn()
    if named == 0 and has_varargs:
        arity = min(len(all_args), max(0, default_vararg_arity))
        return fn(*all_args[:arity])
    arity = min(len(all_args), named)
    return fn(*all_args[:arity])


def _normalize_radar_marker(raw: Any) -> Optional[Dict[str, Any]]:
    """Normalize a plugin radar marker specification into `{fraction, color, width, dash, tag}`."""
    frac_val: Optional[float] = None
    color = ACCENT_GOLD
    width: float = 1.5
    dash: Optional[Tuple[int, ...]] = None
    tag = ""

    if isinstance(raw, dict):
        if "fraction" in raw and raw["fraction"] is not None:
            try:
                frac_val = float(raw["fraction"])
            except (TypeError, ValueError):
                return None
        else:
            for sec_key in ("seconds", "sec", "remaining_sec"):
                if sec_key in raw and raw[sec_key] is not None:
                    try:
                        frac_val = float(raw[sec_key]) / float(CACHE_SAFE_TTL_SEC)
                    except (TypeError, ValueError):
                        return None
                    break
        raw_col = raw.get("color") or raw.get("fill")
        if isinstance(raw_col, str) and raw_col.strip():
            color = raw_col.strip()
        if raw.get("width") is not None:
            try:
                width = max(1.0, min(6.0, float(raw["width"])))
            except (TypeError, ValueError):
                width = 1.5
        raw_dash = raw.get("dash")
        if isinstance(raw_dash, (tuple, list)) and raw_dash:
            try:
                dash = tuple(max(1, int(x)) for x in raw_dash)
            except (TypeError, ValueError):
                dash = None
        raw_tag = raw.get("tag")
        if isinstance(raw_tag, str) and raw_tag.strip():
            tag = raw_tag.strip()
    elif isinstance(raw, (tuple, list)) and len(raw) >= 1:
        try:
            num = float(raw[0])
        except (TypeError, ValueError):
            return None
        frac_val = (num / float(CACHE_SAFE_TTL_SEC)) if num > 1.0 else num
        if len(raw) >= 2 and isinstance(raw[1], str) and raw[1].strip():
            color = raw[1].strip()
        if len(raw) >= 3 and raw[2] is not None:
            try:
                width = max(1.0, min(6.0, float(raw[2])))
            except (TypeError, ValueError):
                width = 1.5
    elif isinstance(raw, (int, float)) and not isinstance(raw, bool):
        num = float(raw)
        frac_val = (num / float(CACHE_SAFE_TTL_SEC)) if num > 1.0 else num
    else:
        return None

    if frac_val is None or not math.isfinite(frac_val):
        return None
    if frac_val <= 0.0 or frac_val >= 1.0:
        return None
    return {
        "fraction": frac_val,
        "color": color,
        "width": width,
        "dash": dash,
        "tag": tag,
    }


class PanelPluginHost:
    """
    Neutral UI extension host passed to `OrionPlugin.register_ui(ui_host)`.

    Exposed extension slots:
    1. `add_tab(key, title, factory_fn, on_select=None, on_refresh=None)`
    2. `add_hub_action_panel(factory_fn, *, framed=True)`
    3. `add_tray_items(items)` (accepts static `Sequence[MenuSpec]` or dynamic zero-arg builder)
    4. `add_radar_markers(fn)` / `format_header_badge(fn)`
    """

    def __init__(self, app: "AcerolaOrionApp") -> None:
        self._app = app
        self._tabs: Dict[str, Dict[str, Any]] = {}
        self._hub_panels: List[tk.Frame] = []
        self._tray_sources: List[Union[List[MenuSpec], Callable[[], Sequence[MenuSpec]]]] = []
        self._radar_marker_providers: List[Callable[..., Any]] = []
        self._header_badge_formatters: List[Callable[..., Any]] = []

    @property
    def app(self) -> "AcerolaOrionApp":
        return self._app

    @property
    def dpi_scale(self) -> float:
        return float(self._app._dpi_scale)

    @property
    def palette(self) -> Dict[str, str]:
        return dict(PALETTE)

    @property
    def plugin_tab_keys(self) -> Tuple[str, ...]:
        return tuple(self._tabs.keys())

    @property
    def hub_action_panels(self) -> Tuple[tk.Frame, ...]:
        return tuple(self._hub_panels)

    def px(self, base: int) -> int:
        return self._app._px(base)

    def ui_call(self, cb: Callable[[], None]) -> None:
        self._app._ui_call(cb)

    def append_console(self, line: str) -> None:
        self._app._append_console(line)

    def run_action(self, label: str, fn: Callable[[], Any]) -> None:
        self._app._run_action(label, fn)

    def refresh_all(self, silent: bool = False) -> None:
        self._app.refresh_all_async(silent=silent)

    def register_action_button(self, btn: tk.Button) -> tk.Button:
        if btn not in self._app._action_buttons:
            self._app._action_buttons.append(btn)
        return btn

    def select_tab(self, key: str) -> None:
        self._app.select_full_tab(key)

    def create_scrolled_treeview(
        self,
        parent: tk.Widget,
        columns: Sequence[str],
        headers: Sequence[Tuple[str, str, int, str]],
        height: int,
        expand: bool = False,
    ) -> ttk.Treeview:
        return self._app._create_scrolled_treeview(parent, columns, headers, height, expand=expand)

    def create_dark_text_box(
        self,
        parent: tk.Widget,
        fg_color: str = "#e2e8f0",
        font_pt: int = 8,
        height: int = 6,
        pad_xy: Tuple[int, int] = (6, 6),
    ) -> tk.Text:
        return self._app._create_dark_text_box(parent, fg_color=fg_color, font_pt=font_pt, height=height, pad_xy=pad_xy)

    def refresh_header_badge(self) -> None:
        self._app._refresh_header_badge(self._app.__dict__.get("_last_report"))

    def redraw_radar_bar(self) -> None:
        self._app._redraw_radar_bar()

    # ------------------------------------------------------------------
    # Slot 1: Dynamic Full-Mode Tabs
    # ------------------------------------------------------------------
    def add_tab(
        self,
        key: str,
        title: str,
        factory_fn: Callable[..., Any],
        on_select: Optional[Callable[..., Any]] = None,
        on_refresh: Optional[Callable[..., Any]] = None,
    ) -> tk.Frame:
        norm_key = str(key or "").strip()
        if not norm_key:
            raise ValueError("Tab key must be a non-empty string")
        if norm_key in _BUILTIN_TAB_KEYS or norm_key in self._app._full_tab_frames:
            raise ValueError(f"Tab key {norm_key!r} is already registered")
        if not callable(factory_fn):
            raise TypeError("Tab factory_fn must be callable")
        if on_select is not None and not callable(on_select):
            raise TypeError("Tab on_select must be callable or None")
        if on_refresh is not None and not callable(on_refresh):
            raise TypeError("Tab on_refresh must be callable or None")

        tab_frame = tk.Frame(self._app.full_container, bg=BG_ROOT)
        try:
            built_result = _invoke_with_arity(factory_fn, (tab_frame, self), default_vararg_arity=1)
        except Exception:
            try:
                tab_frame.destroy()
            except Exception:
                pass
            raise

        btn = tk.Button(
            self._app._full_nav_bar,
            text=str(title or norm_key),
            command=lambda k=norm_key: self._app.select_full_tab(k),
            bg=BG_CARD,
            fg=FG_MUTED,
            activebackground=ACCENT_CRIMSON,
            activeforeground="#ffffff",
            relief="flat",
            padx=self._app._px(14),
            pady=self._app._px(6),
            font=("Microsoft YaHei UI", 9, "bold"),
            cursor="hand2",
        )
        btn.pack(side=tk.LEFT, padx=(0, 6))

        self._app._full_tab_frames[norm_key] = tab_frame
        self._app._full_nav_buttons[norm_key] = btn
        self._tabs[norm_key] = {
            "key": norm_key,
            "title": str(title or norm_key),
            "frame": tab_frame,
            "button": btn,
            "on_select": on_select,
            "on_refresh": on_refresh,
            "result": built_result,
        }
        return tab_frame

    # ------------------------------------------------------------------
    # Slot 2: Hub Optional Extension Control Strip (0px when unused)
    # ------------------------------------------------------------------
    def add_hub_action_panel(
        self,
        factory_fn: Callable[..., Any],
        *,
        framed: bool = True,
    ) -> tk.Frame:
        if not callable(factory_fn):
            raise TypeError("Hub action panel factory_fn must be callable")
        slot = self._app.__dict__.get("hub_plugin_slot")
        if slot is None:
            raise RuntimeError("Hub plugin slot is not initialized")

        had_panels = bool(self._hub_panels)
        if not slot.winfo_manager():
            bottom_row = self._app.__dict__.get("hub_bottom_row")
            if bottom_row is not None and bottom_row.winfo_manager():
                slot.pack(before=bottom_row, fill=tk.X, pady=(0, self._app._px(8)))
            else:
                slot.pack(fill=tk.X, pady=(0, self._app._px(8)))

        if framed:
            container = tk.Frame(
                slot,
                bg=BG_CARD,
                highlightthickness=1,
                highlightbackground=BORDER_SUBTLE,
                padx=self._app._px(12),
                pady=self._app._px(6),
            )
        else:
            container = tk.Frame(slot, bg=BG_ROOT)
        container.pack(fill=tk.X, pady=(0 if not had_panels else self._app._px(6), 0))

        try:
            _invoke_with_arity(factory_fn, (container, self), default_vararg_arity=1)
        except Exception:
            try:
                container.destroy()
            except Exception:
                pass
            if not self._hub_panels:
                try:
                    slot.pack_forget()
                except Exception:
                    pass
            raise

        self._hub_panels.append(container)
        return container

    # ------------------------------------------------------------------
    # Slot 3: Tray Context Menu Extension Items
    # ------------------------------------------------------------------
    def add_tray_items(
        self,
        items: Union[Sequence[MenuSpec], Callable[[], Sequence[MenuSpec]]],
    ) -> None:
        if callable(items):
            self._tray_sources.append(items)
            return
        if isinstance(items, Iterable) and not isinstance(items, (str, bytes)):
            self._tray_sources.append(list(items))
            return
        raise TypeError("Tray items must be a sequence of MenuSpec or a zero-arg callable returning them")

    def collect_tray_items(self) -> List[MenuSpec]:
        collected: List[MenuSpec] = []
        for src in self._tray_sources:
            try:
                raw_items = src() if callable(src) else src
            except Exception as exc:
                logger.warning("Plugin tray item builder %r failed: %s", src, exc, exc_info=True)
                continue
            if not isinstance(raw_items, Iterable) or isinstance(raw_items, (str, bytes)):
                continue
            for spec in raw_items:
                if spec is None:
                    collected.append(None)
                elif (
                    isinstance(spec, tuple)
                    and len(spec) == 2
                    and isinstance(spec[0], str)
                    and callable(spec[1])
                ):
                    collected.append((spec[0], spec[1]))
        return collected

    # ------------------------------------------------------------------
    # Slot 4: Radar Custom Tick Markers & Header Badge Formatters
    # ------------------------------------------------------------------
    def add_radar_markers(self, fn: Callable[..., Any]) -> None:
        if not callable(fn):
            raise TypeError("Radar marker provider must be callable")
        self._radar_marker_providers.append(fn)
        if "radar_canvas" in self._app.__dict__:
            try:
                self._app._redraw_radar_bar()
            except Exception:
                pass

    def collect_radar_markers(self, state: CacheRadarState) -> List[Dict[str, Any]]:
        markers: List[Dict[str, Any]] = []
        for fn in self._radar_marker_providers:
            try:
                raw_list = _invoke_with_arity(fn, (state, CACHE_SAFE_TTL_SEC), default_vararg_arity=1)
            except Exception as exc:
                logger.warning("Plugin radar marker provider %r failed: %s", fn, exc, exc_info=True)
                continue
            if not raw_list:
                continue
            if isinstance(raw_list, (dict, int, float)) or (
                isinstance(raw_list, tuple) and len(raw_list) in (2, 3) and isinstance(raw_list[0], (int, float))
            ):
                raw_iter: Iterable[Any] = (raw_list,)
            elif isinstance(raw_list, Iterable) and not isinstance(raw_list, (str, bytes)):
                raw_iter = raw_list
            else:
                continue
            for item in raw_iter:
                norm = _normalize_radar_marker(item)
                if norm is not None:
                    markers.append(norm)
        return markers

    def format_header_badge(self, fn: Callable[..., Any]) -> None:
        if not callable(fn):
            raise TypeError("Header badge formatter must be callable")
        self._header_badge_formatters.append(fn)
        if "lbl_header_badge" in self._app.__dict__:
            try:
                self._app._refresh_header_badge(self._app.__dict__.get("_last_report"))
            except Exception:
                pass

    def resolve_header_badge(self, report: Optional[OrionTelemetryReport]) -> Optional[Dict[str, str]]:
        text_cur = ""
        fg_cur = ACCENT_CYAN
        bg_cur = ACCENT_CYAN_DIM
        for fn in self._header_badge_formatters:
            named_pos, _has_var = _count_positional_params(fn)
            try:
                res = _invoke_with_arity(fn, (report, text_cur), default_vararg_arity=1)
            except Exception as exc:
                logger.warning("Plugin header badge formatter %r failed: %s", fn, exc, exc_info=True)
                continue
            if res is None:
                continue
            piece_txt = ""
            piece_fg: Optional[str] = None
            piece_bg: Optional[str] = None
            if isinstance(res, str):
                piece_txt = res.strip()
            elif isinstance(res, dict):
                piece_txt = str(res.get("text") or "").strip()
                if isinstance(res.get("fg"), str) and res["fg"].strip():
                    piece_fg = res["fg"].strip()
                if isinstance(res.get("bg"), str) and res["bg"].strip():
                    piece_bg = res["bg"].strip()
            elif isinstance(res, (tuple, list)) and len(res) >= 1:
                piece_txt = str(res[0] or "").strip()
                if len(res) >= 2 and isinstance(res[1], str) and res[1].strip():
                    piece_fg = res[1].strip()
                if len(res) >= 3 and isinstance(res[2], str) and res[2].strip():
                    piece_bg = res[2].strip()
            else:
                continue

            if not piece_txt:
                continue
            if named_pos >= 2:
                text_cur = piece_txt
            elif text_cur:
                text_cur = f"{text_cur}  ·  {piece_txt}"
            else:
                text_cur = piece_txt
            if piece_fg:
                fg_cur = piece_fg
            if piece_bg:
                bg_cur = piece_bg

        if not text_cur:
            return None
        return {"text": text_cur, "fg": fg_cur, "bg": bg_cur}

    # ------------------------------------------------------------------
    # Lifecycle Notifications Called by AcerolaOrionApp
    # ------------------------------------------------------------------
    def notify_tab_selected(self, key: str) -> None:
        meta = self._tabs.get(key)
        if not meta:
            return
        cb = meta.get("on_select")
        if callable(cb):
            try:
                _invoke_with_arity(cb, (self,), default_vararg_arity=0)
            except Exception as exc:
                logger.warning("Plugin tab %r on_select failed: %s", key, exc, exc_info=True)

    def notify_refresh(self, report: OrionTelemetryReport, context: Dict[str, Any]) -> None:
        for key, meta in list(self._tabs.items()):
            cb = meta.get("on_refresh")
            if callable(cb):
                try:
                    _invoke_with_arity(cb, (report, context), default_vararg_arity=1)
                except Exception as exc:
                    logger.warning("Plugin tab %r on_refresh failed: %s", key, exc, exc_info=True)


__all__ = [
    "PanelPluginHost",
    "_BUILTIN_TAB_KEYS",
    "_normalize_radar_marker",
]
