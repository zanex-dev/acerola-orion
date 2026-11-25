# -*- coding: utf-8 -*-
"""
Shared helpers for Acerola-Orion built-in optional plugins (`acerola_orion/plugins/_helpers.py`).
- 剪贴板写入与 UI 线程调度兜底
- PowerShell 单引号转义与启动器脚本模板
- 宿主异步动作分发封装
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Optional


def resolve_callable(obj: Any, *names: str) -> Optional[Callable[..., Any]]:
    """Return the first callable attribute among `names` on `obj`, or None."""
    for attr in names:
        fn = getattr(obj, attr, None)
        if callable(fn):
            return fn
    return None


def default_clipboard_writer(
    parent: Any,
    log_fn: Optional[Callable[[str], None]] = None,
    hud_getter: Optional[Callable[[], Any]] = None,
) -> Callable[..., str]:
    """Build a fallback clipboard writer bound to a Tkinter widget."""

    def _copy(text: str, toast: str = "", console_msg: str = "") -> str:
        clean = str(text or "").strip()
        if not clean:
            return ""
        try:
            parent.clipboard_clear()
            parent.clipboard_append(clean)
            parent.update_idletasks()
        except Exception:
            pass
        if hud_getter is not None and toast:
            try:
                hud = hud_getter()
                if hud is not None and callable(getattr(hud, "flash_toast", None)):
                    hud.flash_toast(str(toast), duration_sec=2.0)
            except Exception:
                pass
        if log_fn is not None and console_msg:
            try:
                log_fn(str(console_msg))
            except Exception:
                pass
        return clean

    return _copy


def default_ui_call(parent: Any) -> Callable[[Callable[[], None]], None]:
    """Build a fallback UI-thread dispatcher using `parent.after(0, cb)`."""

    def _post(cb: Callable[[], None]) -> None:
        try:
            parent.after(0, cb)
        except Exception:
            pass

    return _post


def ps_single_quote(raw: Path | str) -> str:
    """Safely wrap a string/path in PowerShell single quotes."""
    clean = str(raw).replace("\r", "").replace("\n", "")
    return "'" + clean.replace("'", "''") + "'"


def ps1_cli(py_exe: str, pkg_root: Path | str, args_str: str) -> str:
    """Build a UTF-8 PowerShell launcher snippet invoking `acerola_orion` CLI."""
    return (
        "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8\r\n"
        "$OutputEncoding = [System.Text.Encoding]::UTF8\r\n"
        f"Set-Location -LiteralPath {ps_single_quote(pkg_root)}\r\n"
        f"& {ps_single_quote(py_exe)} -m acerola_orion {args_str}\r\n"
    )


def invoke_host_action(host: Any, label: str, fn: Callable[[], Any]) -> Any:
    """Execute `fn` via `host.run_action(label, fn)` if available, else inline."""
    run_action = getattr(host, "run_action", None)
    if callable(run_action):
        return run_action(label, fn)
    return fn()


__all__ = [
    "resolve_callable",
    "default_clipboard_writer",
    "default_ui_call",
    "ps_single_quote",
    "ps1_cli",
    "invoke_host_action",
]
