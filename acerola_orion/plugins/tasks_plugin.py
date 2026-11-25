# -*- coding: utf-8 -*-
"""
Task Board Optional Plugin (`acerola_orion/plugins/tasks_plugin.py`).
- 独立实现 `OrionPlugin` 协议，专职注册与刷新桌面星盘 `Tasks`（任务拆解与阶段交接看板）页签
- 严格只读扫描本地 `.agents/tasks` 与 `.agents/checkpoints`，可独立注册至 `OrionKernel` 运行
"""

from __future__ import annotations

from pathlib import Path
import tkinter as tk
from typing import Any, Callable, Dict, Optional

from acerola_orion.core.kernel import HookBus

from ._helpers import default_clipboard_writer, default_ui_call, resolve_callable

TASKS_TAB_KEY = "tasks"
TASKS_TAB_TITLE = "📋 任务拆解与接力 (Tasks)"


class TasksPlugin:
    """Independent built-in optional plugin mounting the `Tasks` panel tab."""

    name: str = "tasks_board"

    def __init__(
        self,
        on_telemetry_refresh: Optional[Callable[[Any], None]] = None,
    ) -> None:
        self._tasks_tab: Any = None
        self._ui_host: Any = None
        self._on_telemetry_refresh = on_telemetry_refresh

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

    def register_hooks(self, bus: HookBus) -> None:
        _ = bus

    def register_ui(self, ui_host: Any) -> None:
        """Mount dynamic Tasks Tab (Task Decomposer & Stage Handoff Board)."""
        self._ui_host = ui_host
        add_tab = getattr(ui_host, "add_tab", None)
        if not callable(add_tab):
            return

        def _build_tasks_tab(parent: Any, host: Any = None) -> Any:
            from .task_board_tab import TaskBoardTab

            ctx = host if host is not None else ui_host
            app = getattr(ctx, "app", ctx)

            px_fn = (
                resolve_callable(ctx, "px", "_px")
                or resolve_callable(app, "px", "_px")
                or (lambda n: int(n))
            )
            log_fn = (
                resolve_callable(ctx, "append_console", "log_callback", "_append_console")
                or resolve_callable(app, "append_console", "log_callback", "_append_console")
                or (lambda _msg: None)
            )
            copy_fn = (
                resolve_callable(ctx, "copy_text_to_clipboard", "copy_callback")
                or resolve_callable(app, "copy_text_to_clipboard", "copy_callback")
                or default_clipboard_writer(
                    parent,
                    log_fn=log_fn,
                    hud_getter=lambda a=app: getattr(a, "_hud_win", None),
                )
            )
            ui_call_fn = (
                resolve_callable(ctx, "ui_call", "_ui_call")
                or resolve_callable(app, "ui_call", "_ui_call")
                or default_ui_call(parent)
            )
            raw_pal = getattr(ctx, "palette", None)
            if not isinstance(raw_pal, dict):
                raw_pal = getattr(app, "palette", None)
            palette: Dict[str, str] = dict(raw_pal) if isinstance(raw_pal, dict) else {}
            if "BORDER_DIM" not in palette and "BORDER_SUBTLE" in palette:
                palette["BORDER_DIM"] = palette["BORDER_SUBTLE"]

            tab = TaskBoardTab(
                parent,
                px=px_fn,
                copy_callback=copy_fn,
                log_callback=log_fn,
                ui_call=ui_call_fn,
                palette=palette,
            )
            tab.pack(fill=tk.BOTH, expand=True)
            self._tasks_tab = tab
            return tab

        def _resolve_tasks_tab(candidate: Any = None) -> Any:
            if candidate is not None and callable(getattr(candidate, "refresh_async", None)):
                return candidate
            return self._tasks_tab

        def _on_select(arg: Any = None) -> None:
            target = _resolve_tasks_tab(arg)
            if target is not None and callable(getattr(target, "refresh_async", None)):
                target.refresh_async(silent=True)

        def _on_refresh(report: Any = None, _context: Any = None, *_args: Any, **_kwargs: Any) -> None:
            if (
                self._on_telemetry_refresh is not None
                and report is not None
                and hasattr(report, "extensions")
            ):
                self._on_telemetry_refresh(report)
            target = _resolve_tasks_tab(report)
            if target is not None and callable(getattr(target, "refresh_async", None)):
                target.refresh_async(silent=True)

        add_tab(
            key=TASKS_TAB_KEY,
            title=TASKS_TAB_TITLE,
            factory_fn=_build_tasks_tab,
            on_select=_on_select,
            on_refresh=_on_refresh,
        )


__all__ = [
    "TASKS_TAB_KEY",
    "TASKS_TAB_TITLE",
    "TasksPlugin",
]
