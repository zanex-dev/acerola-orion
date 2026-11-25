# -*- coding: utf-8 -*-
"""
TaskBoard 工作区白名单持久化与隐私路径校验 (`acerola_orion/plugins/workspace_prefs.py`)
- 基于公开地基 `acerola_orion.constants.load_app_config / update_app_config` 独立读写 `task_workspaces`
- 严守 agent-blindfold 隐私隔离：拦截根目录、用户主目录根、个人媒体/文档目录及隐私沙箱段
- 原子读写与未知配置键零踩踏，区分 `task_workspaces` 缺失/`None`（回退默认）与显式 `[]`（保持空列表）
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple, Union

from acerola_orion.constants import (
    get_agy_home,
    load_app_config,
    update_app_config,
)

_SENSITIVE_USER_DIR_NAMES: Tuple[str, ...] = (
    "Pictures",
    "Videos",
    "Music",
    "Desktop",
    "Documents",
)
_BLINDFOLD_SEGMENT_NAMES_LOWER: Set[str] = {
    ".user_uploaded",
    "tempmediastorage",
    "browser_sandbox",
}


def _collect_blocked_sensitive_roots() -> Tuple[Set[str], Set[str]]:
    """
    Return `(exact_blocked_normcases, subtree_blocked_normcases)` under agent-blindfold policy:
    - exact_blocked: user home root itself (`Path.home()`).
    - subtree_blocked: personal media/document roots (`~/Pictures`, `~/Videos`, `~/Music`,
      `~/Desktop`, `~/Documents`, and their `~/OneDrive/<dir>` equivalents) plus any subtrees beneath them.
    """
    home_variants: List[str] = []
    try:
        home_abs = os.path.abspath(str(Path.home()))
        home_variants.append(home_abs)
        home_real = os.path.realpath(home_abs)
        if os.path.normcase(home_real) != os.path.normcase(home_abs):
            home_variants.append(home_real)
    except Exception:
        return set(), set()

    exact_blocked: Set[str] = set()
    subtree_blocked: Set[str] = set()
    for hv in home_variants:
        hv_clean = hv.rstrip("\\/") or hv
        exact_blocked.add(os.path.normcase(hv_clean))
        for name in _SENSITIVE_USER_DIR_NAMES:
            direct = os.path.abspath(os.path.join(hv, name)).rstrip("\\/")
            onedrive = os.path.abspath(os.path.join(hv, "OneDrive", name)).rstrip("\\/")
            subtree_blocked.add(os.path.normcase(direct))
            subtree_blocked.add(os.path.normcase(onedrive))
    return exact_blocked, subtree_blocked


def _normalize_single_task_workspace(raw: Any) -> Tuple[Optional[str], str]:
    """Validate and normalize a single task workspace path; returns `(abs_path, reject_reason)`."""
    if isinstance(raw, os.PathLike):
        raw_str = os.fspath(raw)
    elif isinstance(raw, str):
        raw_str = raw
    else:
        return None, "工作区路径必须为字符串或 Path 对象"
    if not isinstance(raw_str, str):
        return None, "工作区路径必须为文本路径"
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in raw_str):
        return None, "工作区路径包含非法控制字符"
    s = raw_str.strip()
    if not s:
        return None, "工作区路径不能为空"

    try:
        expanded = os.path.expanduser(s)
        norm_abs = os.path.abspath(expanded)
    except (OSError, ValueError):
        return None, f"无效的工作区路径: {s}"

    if os.path.dirname(norm_abs) == norm_abs:
        return None, f"拒绝将文件系统根目录加入任务工作区白名单: {norm_abs}"

    exact_blocked, subtree_blocked = _collect_blocked_sensitive_roots()
    candidates = [norm_abs]
    try:
        real_abs = os.path.realpath(norm_abs)
        if os.path.normcase(real_abs) != os.path.normcase(norm_abs):
            candidates.append(real_abs)
    except OSError:
        pass

    sep = os.sep
    for cand in candidates:
        cand_clean = cand.rstrip("\\/") or cand
        cand_nc = os.path.normcase(cand_clean)
        if cand_nc in exact_blocked:
            return None, f"拒绝将用户主目录根加入任务工作区白名单（agent-blindfold 隐私隔离）: {norm_abs}"
        for blocked_nc in subtree_blocked:
            if cand_nc == blocked_nc or cand_nc.startswith(blocked_nc + sep):
                return None, f"拒绝将个人敏感目录加入任务工作区白名单（agent-blindfold 隐私隔离）: {norm_abs}"
        parts_lower = {p.lower() for p in Path(cand_clean).parts}
        if parts_lower & _BLINDFOLD_SEGMENT_NAMES_LOWER:
            return None, f"拒绝将隐私媒体/沙箱目录加入任务工作区白名单（agent-blindfold 隐私隔离）: {norm_abs}"

    return norm_abs, ""


def _normalize_task_workspaces(raw_items: Any) -> List[str]:
    """Normalize, validate, and case-insensitively deduplicate a sequence of task workspace paths."""
    if not isinstance(raw_items, (list, tuple)):
        return []
    out: List[str] = []
    seen: Set[str] = set()
    for item in raw_items:
        norm, _reason = _normalize_single_task_workspace(item)
        if not norm:
            continue
        key = os.path.normcase(norm)
        if key not in seen:
            seen.add(key)
            out.append(norm)
    return out


def default_task_workspaces() -> List[str]:
    """
    Return the default task-board workspace whitelist:
    prefers `[str((get_agy_home() / "scratch").resolve())]` when that directory exists,
    otherwise falls back to `[str(Path(__file__).resolve().parent.parent.parent)]`.
    """
    scratch = get_agy_home() / "scratch"
    try:
        if scratch.is_dir():
            candidate = str(scratch.resolve())
        else:
            candidate = str(Path(__file__).resolve().parent.parent.parent)
    except OSError:
        candidate = str(Path(__file__).resolve().parent.parent.parent)
    return _normalize_task_workspaces([candidate])


def _extract_task_workspaces(cfg: Dict[str, Any]) -> List[str]:
    """
    Extract normalized `task_workspaces` from raw config dict:
    - missing key or explicit `None` -> `default_task_workspaces()`
    - explicit list (including `[]`) -> `_normalize_task_workspaces(...)`
    """
    if ("task_workspaces" not in cfg) or (cfg.get("task_workspaces") is None):
        return default_task_workspaces()
    return _normalize_task_workspaces(cfg.get("task_workspaces"))


def get_task_workspaces() -> List[str]:
    """Return normalized task-board workspace whitelist from config.json."""
    return _extract_task_workspaces(load_app_config())


def save_task_workspaces(workspaces: Sequence[Union[str, Path]]) -> Dict[str, Any]:
    """
    Atomically replace the `task_workspaces` whitelist in `config.json` without clobbering
    concurrent keys. Passing an empty sequence explicitly stores `[]` (not reset to defaults).
    """
    if isinstance(workspaces, (str, bytes, os.PathLike)) or not isinstance(workspaces, Sequence):
        return {
            "ok": False,
            "workspaces": get_task_workspaces(),
            "rejected": [],
            "changed": False,
            "message": "workspaces 必须为路径序列（list / tuple）",
        }

    accepted: List[str] = []
    seen: Set[str] = set()
    rejected: List[Dict[str, str]] = []
    for item in workspaces:
        norm, reason = _normalize_single_task_workspace(item)
        if not norm:
            rejected.append({"input": str(item), "reason": reason})
            continue
        key = os.path.normcase(norm)
        if key not in seen:
            seen.add(key)
            accepted.append(norm)

    changed_holder: List[bool] = [False]

    def _mutate(c: Dict[str, Any]) -> None:
        prev = _extract_task_workspaces(c)
        c["task_workspaces"] = list(accepted)
        changed_holder[0] = prev != accepted

    updated = update_app_config(_mutate)
    saved = _extract_task_workspaces(updated)
    if rejected:
        msg = f"已保存 {len(saved)} 个任务工作区（已拦截 {len(rejected)} 项非法或敏感路径）"
    else:
        msg = f"已保存 {len(saved)} 个任务工作区"
    return {
        "ok": True,
        "workspaces": saved,
        "rejected": rejected,
        "changed": changed_holder[0],
        "message": msg,
    }


def add_task_workspace(workspace: Union[str, Path]) -> Dict[str, Any]:
    """Atomically append a validated workspace path to `task_workspaces` (deduped by normcase)."""
    norm, reason = _normalize_single_task_workspace(workspace)
    if not norm:
        return {
            "ok": False,
            "workspace": "",
            "workspaces": get_task_workspaces(),
            "added": False,
            "changed": False,
            "message": reason,
        }

    target_key = os.path.normcase(norm)
    added_holder: List[bool] = [False]
    matched_holder: List[str] = [norm]

    def _mutate(c: Dict[str, Any]) -> None:
        cur = _extract_task_workspaces(c)
        for existing in cur:
            if os.path.normcase(existing) == target_key:
                matched_holder[0] = existing
                added_holder[0] = False
                c["task_workspaces"] = cur
                return
        cur.append(norm)
        c["task_workspaces"] = cur
        matched_holder[0] = norm
        added_holder[0] = True

    updated = update_app_config(_mutate)
    saved = _extract_task_workspaces(updated)
    added = added_holder[0]
    msg = (
        f"已添加任务工作区: {matched_holder[0]}"
        if added
        else f"工作区已在白名单中: {matched_holder[0]}"
    )
    return {
        "ok": True,
        "workspace": matched_holder[0],
        "workspaces": saved,
        "added": added,
        "changed": added,
        "message": msg,
    }


def remove_task_workspace(workspace: Union[str, Path]) -> Dict[str, Any]:
    """Atomically remove a workspace path from `task_workspaces` (matched by normcase)."""
    if isinstance(workspace, os.PathLike):
        raw_str = os.fspath(workspace)
    elif isinstance(workspace, str):
        raw_str = workspace
    else:
        return {
            "ok": False,
            "workspace": "",
            "workspaces": get_task_workspaces(),
            "removed": False,
            "changed": False,
            "message": "工作区路径必须为字符串或 Path 对象",
        }
    if not isinstance(raw_str, str) or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in raw_str):
        return {
            "ok": False,
            "workspace": "",
            "workspaces": get_task_workspaces(),
            "removed": False,
            "changed": False,
            "message": "工作区路径包含非法控制字符",
        }
    s = raw_str.strip()
    if not s:
        return {
            "ok": False,
            "workspace": "",
            "workspaces": get_task_workspaces(),
            "removed": False,
            "changed": False,
            "message": "工作区路径不能为空",
        }

    target_abs = os.path.abspath(os.path.expanduser(s))
    target_key = os.path.normcase(target_abs)
    removed_holder: List[bool] = [False]
    removed_path_holder: List[str] = [target_abs]

    def _mutate(c: Dict[str, Any]) -> None:
        cur = _extract_task_workspaces(c)
        kept: List[str] = []
        for existing in cur:
            if os.path.normcase(existing) == target_key:
                removed_holder[0] = True
                removed_path_holder[0] = existing
                continue
            kept.append(existing)
        c["task_workspaces"] = kept

    updated = update_app_config(_mutate)
    saved = _extract_task_workspaces(updated)
    removed = removed_holder[0]
    msg = (
        f"已移除任务工作区: {removed_path_holder[0]}"
        if removed
        else f"未在白名单中找到工作区: {target_abs}"
    )
    return {
        "ok": True,
        "workspace": removed_path_holder[0],
        "workspaces": saved,
        "removed": removed,
        "changed": removed,
        "message": msg,
    }


__all__ = [
    "default_task_workspaces",
    "get_task_workspaces",
    "save_task_workspaces",
    "add_task_workspace",
    "remove_task_workspace",
]
