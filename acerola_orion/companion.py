# -*- coding: utf-8 -*-
"""
Antigravity 配套 Hooks、全局白帽 Skills 与工作区插件非破坏性装配引擎 (acerola_orion/companion.py)
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .constants import atomic_write_text, interprocess_file_lock

ENV_GEMINI_CONFIG_DIR = "ORION_GEMINI_CONFIG_DIR"
MANAGED_HOOK_KEYS: Tuple[str, ...] = ("eoq-context-guard", "orion-chat-vault")
PYTHON_EXE_PLACEHOLDER = "<PYTHON_EXE>"


def get_gemini_config_dir() -> Path:
    """Resolve target ~/.gemini/config directory, honoring ORION_GEMINI_CONFIG_DIR override."""
    override = os.environ.get(ENV_GEMINI_CONFIG_DIR, "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".gemini" / "config"


def get_integrations_dir() -> Path:
    """Return bundled clean-room Antigravity integrations directory."""
    return Path(__file__).resolve().parent / "integrations" / "antigravity"


def format_python_exe(python_exe: Optional[str] = None) -> str:
    """Normalize interpreter path to forward slashes for cross-shell hooks.json compatibility."""
    raw = (python_exe or sys.executable).strip()
    posix_path = Path(raw).as_posix()
    return posix_path


def _command_python_token(posix_py: str) -> str:
    if " " in posix_py and not (posix_py.startswith('"') and posix_py.endswith('"')):
        return f'"{posix_py}"'
    return posix_py


def _substitute_placeholder(node: Any, placeholder: str, replacement: str) -> Any:
    if isinstance(node, str):
        return node.replace(placeholder, replacement)
    if isinstance(node, list):
        return [_substitute_placeholder(item, placeholder, replacement) for item in node]
    if isinstance(node, dict):
        return {k: _substitute_placeholder(v, placeholder, replacement) for k, v in node.items()}
    return node


def _load_json_dict(path: Path) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    if not path.exists():
        return {}, None
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        return None, f"读取 {path} 失败: {exc}"
    if not raw:
        return {}, None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        return None, f"{path} 不是合法 JSON，已中止修改以保护原文件: {exc}"
    if not isinstance(data, dict):
        return None, f"{path} 顶层结构不是 JSON 对象，已中止修改以保护原文件"
    return data, None


def _iter_bundle_files(src_dir: Path) -> List[Path]:
    if not src_dir.is_dir():
        return []
    return sorted(p for p in src_dir.rglob("*") if p.is_file())


def _copy_asset_file(
    src: Path,
    dst: Path,
    *,
    force: bool,
    installed: List[str],
    overwritten: List[str],
    skipped_existing: List[str],
) -> None:
    existed = dst.exists()
    if existed and not force:
        skipped_existing.append(str(dst))
        return
    content = src.read_text(encoding="utf-8")
    atomic_write_text(dst, content, encoding="utf-8")
    installed.append(str(dst))
    if existed:
        overwritten.append(str(dst))


def _global_asset_pairs(bundle_root: Path, config_dir: Path) -> List[Tuple[Path, Path]]:
    pairs: List[Tuple[Path, Path]] = []
    gp_root = bundle_root / "global-plugins"
    for src in _iter_bundle_files(gp_root):
        rel = src.relative_to(gp_root)
        pairs.append((src, config_dir / "plugins" / rel))

    gs_root = bundle_root / "global-skills"
    for src in _iter_bundle_files(gs_root):
        rel = src.relative_to(gs_root)
        pairs.append((src, config_dir / "skills" / rel))
    return pairs


def _workspace_asset_pairs(
    bundle_root: Path,
    ws_root: Path,
    *,
    include_rules: bool = False,
) -> List[Tuple[Path, Path]]:
    pairs: List[Tuple[Path, Path]] = []
    relay_root = bundle_root / "workspace-plugins" / "eoq-task-relay"
    for src in _iter_bundle_files(relay_root):
        rel = src.relative_to(relay_root)
        pairs.append((src, ws_root / ".agents" / "plugins" / "eoq-task-relay" / rel))

    eng_root = bundle_root / "workspace-plugins" / "engineering-core"
    plugin_manifest = eng_root / "plugin.json"
    if plugin_manifest.is_file():
        pairs.append((plugin_manifest, ws_root / ".agents" / "plugins" / "engineering-core" / "plugin.json"))

    vs_root = eng_root / "skills" / "version-seal"
    for src in _iter_bundle_files(vs_root):
        rel = src.relative_to(eng_root)
        pairs.append((src, ws_root / ".agents" / "plugins" / "engineering-core" / rel))

    if include_rules:
        rules_root = eng_root / "rules"
        for src in _iter_bundle_files(rules_root):
            rel = src.relative_to(eng_root)
            pairs.append((src, ws_root / ".agents" / "plugins" / "engineering-core" / rel))
    return pairs


def _prune_empty_dirs(paths: Iterable[Path], stop_at: Path) -> None:
    stop_resolved = stop_at.resolve()
    candidates = sorted({p.resolve() for p in paths}, key=lambda p: len(p.parts), reverse=True)
    for cur in candidates:
        while cur != stop_resolved and stop_resolved in cur.parents:
            if not cur.is_dir():
                cur = cur.parent
                continue
            try:
                next(cur.iterdir())
                break
            except StopIteration:
                try:
                    cur.rmdir()
                except OSError:
                    break
                cur = cur.parent
            except OSError:
                break


def companion_status(workspace: Optional[Path] = None) -> Dict[str, Any]:
    """Inspect current installation status of Antigravity hooks, global skills, and optional workspace plugins."""
    config_dir = get_gemini_config_dir()
    bundle_root = get_integrations_dir()
    hooks_path = config_dir / "hooks.json"

    hooks_data, hooks_err = _load_json_dict(hooks_path)
    hooks_valid = hooks_err is None
    hooks_obj = hooks_data if hooks_data is not None else {}

    managed_status: Dict[str, Dict[str, Any]] = {}
    for key in MANAGED_HOOK_KEYS:
        entry = hooks_obj.get(key)
        installed = isinstance(entry, dict)
        enabled = bool(entry.get("enabled", True)) if isinstance(entry, dict) else False
        managed_status[key] = {"installed": installed, "enabled": enabled}

    other_hook_keys = [k for k in hooks_obj.keys() if k not in MANAGED_HOOK_KEYS]

    ws_report: Optional[Dict[str, Any]] = None
    if workspace is not None:
        ws_root = Path(workspace).expanduser().resolve()
        eng_base = ws_root / ".agents" / "plugins" / "engineering-core"
        relay_base = ws_root / ".agents" / "plugins" / "eoq-task-relay"
        ws_report = {
            "path": str(ws_root),
            "eoq_task_relay": (relay_base / "plugin.json").is_file(),
            "task_decomposer": (relay_base / "skills" / "task-decomposer" / "SKILL.md").is_file(),
            "stage_handoff": (relay_base / "skills" / "stage-handoff" / "SKILL.md").is_file(),
            "version_seal": (eng_base / "skills" / "version-seal" / "SKILL.md").is_file(),
            "engineering_rules": (eng_base / "rules" / "AGENTS.md").is_file(),
        }

    return {
        "ok": hooks_valid and bundle_root.is_dir(),
        "config_dir": str(config_dir),
        "integrations_dir": str(bundle_root),
        "bundle_available": bundle_root.is_dir(),
        "hooks": {
            "hooks_file": str(hooks_path),
            "hooks_file_exists": hooks_path.is_file(),
            "hooks_valid_json": hooks_valid,
            "error": hooks_err,
            "managed_keys": managed_status,
            "other_hook_keys": other_hook_keys,
            "eoq_guard_config_exists": (config_dir / "eoq_guard_config.json").is_file(),
            "eoq_cheatsheet_exists": (config_dir / "docs" / "eoq_context_guard_cheatsheet.md").is_file(),
        },
        "global": {
            "plugin_acerola_orion": (config_dir / "plugins" / "acerola-orion" / "plugin.json").is_file(),
            "skills": {
                "acerola-orion": (
                    config_dir / "plugins" / "acerola-orion" / "skills" / "acerola-orion" / "SKILL.md"
                ).is_file(),
                "orion-vault": (
                    config_dir / "plugins" / "acerola-orion" / "skills" / "orion-vault" / "SKILL.md"
                ).is_file(),
                "session-recall": (config_dir / "skills" / "session-recall" / "SKILL.md").is_file(),
                "kv-keepalive": (config_dir / "skills" / "kv-keepalive" / "SKILL.md").is_file(),
            },
        },
        "workspace": ws_report,
    }


def install_companion(
    *,
    workspace: Optional[Path] = None,
    install_hooks: bool = True,
    install_global: bool = True,
    include_rules: bool = False,
    force: bool = False,
    python_exe: Optional[str] = None,
) -> Dict[str, Any]:
    """Non-destructively install Antigravity hooks, global skills, and optional workspace plugins."""
    config_dir = get_gemini_config_dir()
    bundle_root = get_integrations_dir()
    if not bundle_root.is_dir():
        return {
            "ok": False,
            "error": f"未找到内置配套资产目录: {bundle_root}",
            "config_dir": str(config_dir),
        }

    posix_py = format_python_exe(python_exe)
    py_token = _command_python_token(posix_py)
    ws_root = Path(workspace).expanduser().resolve() if workspace is not None else None

    installed: List[str] = []
    overwritten: List[str] = []
    skipped_existing: List[str] = []
    hooks_installed: List[str] = []
    preserved_hook_keys: List[str] = []

    if install_hooks:
        tpl_path = bundle_root / "hooks" / "hooks.template.json"
        tpl_data, tpl_err = _load_json_dict(tpl_path)
        if tpl_data is None:
            return {
                "ok": False,
                "error": tpl_err or f"无法加载模板: {tpl_path}",
                "config_dir": str(config_dir),
            }
        rendered_tpl = _substitute_placeholder(tpl_data, PYTHON_EXE_PLACEHOLDER, py_token)

        hooks_path = config_dir / "hooks.json"
        hooks_path.parent.mkdir(parents=True, exist_ok=True)
        with interprocess_file_lock(hooks_path):
            existing_hooks, hooks_err = _load_json_dict(hooks_path)
            if existing_hooks is None:
                return {
                    "ok": False,
                    "error": hooks_err,
                    "config_dir": str(config_dir),
                }
            preserved_hook_keys = [k for k in existing_hooks.keys() if k not in MANAGED_HOOK_KEYS]
            changed = False
            for key in MANAGED_HOOK_KEYS:
                if key not in rendered_tpl:
                    continue
                if key in existing_hooks and not force:
                    skipped_existing.append(f"hooks.json:{key}")
                    continue
                was_present = key in existing_hooks
                existing_hooks[key] = rendered_tpl[key]
                hooks_installed.append(key)
                if was_present:
                    overwritten.append(f"hooks.json:{key}")
                changed = True

            if changed or not hooks_path.exists():
                if changed:
                    payload = json.dumps(existing_hooks, ensure_ascii=False, indent=2) + "\n"
                    atomic_write_text(hooks_path, payload, encoding="utf-8")
                    installed.append(str(hooks_path))

        eoq_cfg_src = bundle_root / "hooks" / "eoq_guard_config.json"
        if eoq_cfg_src.is_file():
            _copy_asset_file(
                eoq_cfg_src,
                config_dir / "eoq_guard_config.json",
                force=force,
                installed=installed,
                overwritten=overwritten,
                skipped_existing=skipped_existing,
            )

        cheatsheet_src = bundle_root / "hooks" / "docs" / "eoq_context_guard_cheatsheet.md"
        if cheatsheet_src.is_file():
            _copy_asset_file(
                cheatsheet_src,
                config_dir / "docs" / "eoq_context_guard_cheatsheet.md",
                force=force,
                installed=installed,
                overwritten=overwritten,
                skipped_existing=skipped_existing,
            )

    if install_global:
        for src, dst in _global_asset_pairs(bundle_root, config_dir):
            _copy_asset_file(
                src,
                dst,
                force=force,
                installed=installed,
                overwritten=overwritten,
                skipped_existing=skipped_existing,
            )

    if ws_root is not None:
        for src, dst in _workspace_asset_pairs(bundle_root, ws_root, include_rules=include_rules):
            _copy_asset_file(
                src,
                dst,
                force=force,
                installed=installed,
                overwritten=overwritten,
                skipped_existing=skipped_existing,
            )

    return {
        "ok": True,
        "config_dir": str(config_dir),
        "workspace": str(ws_root) if ws_root is not None else None,
        "python_exe": posix_py,
        "hooks_installed": hooks_installed,
        "preserved_hook_keys": preserved_hook_keys,
        "installed": installed,
        "overwritten": overwritten,
        "skipped_existing": skipped_existing,
    }


def uninstall_companion(
    *,
    workspace: Optional[Path] = None,
    remove_hooks: bool = True,
    remove_global: bool = False,
    remove_workspace: bool = False,
) -> Dict[str, Any]:
    """Non-destructively remove managed Orion hook entries and optionally managed skill/plugin files."""
    config_dir = get_gemini_config_dir()
    bundle_root = get_integrations_dir()
    ws_root = Path(workspace).expanduser().resolve() if workspace is not None else None

    hooks_removed: List[str] = []
    preserved_hook_keys: List[str] = []
    removed: List[str] = []

    if remove_hooks:
        hooks_path = config_dir / "hooks.json"
        if hooks_path.is_file():
            with interprocess_file_lock(hooks_path):
                existing_hooks, hooks_err = _load_json_dict(hooks_path)
                if existing_hooks is None:
                    return {
                        "ok": False,
                        "error": hooks_err,
                        "config_dir": str(config_dir),
                    }
                for key in MANAGED_HOOK_KEYS:
                    if key in existing_hooks:
                        del existing_hooks[key]
                        hooks_removed.append(key)
                preserved_hook_keys = list(existing_hooks.keys())
                if hooks_removed:
                    payload = json.dumps(existing_hooks, ensure_ascii=False, indent=2) + "\n"
                    atomic_write_text(hooks_path, payload, encoding="utf-8")

    if remove_global and bundle_root.is_dir():
        touched_parents: List[Path] = []
        target_files = [dst for _, dst in _global_asset_pairs(bundle_root, config_dir)]
        cheatsheet_dst = config_dir / "docs" / "eoq_context_guard_cheatsheet.md"
        target_files.append(cheatsheet_dst)
        for dst in target_files:
            if dst.is_file():
                dst.unlink()
                removed.append(str(dst))
                touched_parents.append(dst.parent)
        if touched_parents:
            _prune_empty_dirs(touched_parents, stop_at=config_dir)

    if remove_workspace:
        if ws_root is None:
            return {
                "ok": False,
                "error": "启用 remove_workspace 时必须指定 workspace 目录",
                "config_dir": str(config_dir),
            }
        if bundle_root.is_dir():
            ws_parents: List[Path] = []
            ws_pairs = _workspace_asset_pairs(bundle_root, ws_root, include_rules=True)
            eng_manifest = ws_root / ".agents" / "plugins" / "engineering-core" / "plugin.json"
            for _, dst in ws_pairs:
                if dst == eng_manifest:
                    continue
                if dst.is_file():
                    dst.unlink()
                    removed.append(str(dst))
                    ws_parents.append(dst.parent)

            eng_dir = eng_manifest.parent
            if eng_manifest.is_file():
                remaining_others = [p for p in eng_dir.rglob("*") if p.is_file() and p != eng_manifest]
                if not remaining_others:
                    eng_manifest.unlink()
                    removed.append(str(eng_manifest))
                    ws_parents.append(eng_dir)

            plugins_root = ws_root / ".agents" / "plugins"
            if ws_parents and plugins_root.exists():
                _prune_empty_dirs(ws_parents, stop_at=ws_root)

    return {
        "ok": True,
        "config_dir": str(config_dir),
        "workspace": str(ws_root) if ws_root is not None else None,
        "hooks_removed": hooks_removed,
        "preserved_hook_keys": preserved_hook_keys,
        "removed": removed,
    }


def render_companion_report(action: str, payload: Dict[str, Any]) -> str:
    """Format human-readable CLI summary for companion status/install/uninstall."""
    if not payload.get("ok"):
        return f"❌ companion {action} 失败: {payload.get('error', '未知错误')}"

    lines: List[str] = []
    if action == "status":
        lines.append(f"=== Acerola-Orion Companion 状态 ({payload.get('config_dir')}) ===")
        hooks = payload.get("hooks", {})
        managed = hooks.get("managed_keys", {})
        for key in MANAGED_HOOK_KEYS:
            info = managed.get(key, {})
            badge = "✅ 已挂载" if info.get("installed") else "⚪ 未挂载"
            if info.get("installed") and not info.get("enabled", True):
                badge = "⏸️ 已挂载(禁用)"
            lines.append(f"  [Hook] {key}: {badge}")
        others = hooks.get("other_hook_keys") or []
        if others:
            lines.append(f"  [Hook] 其他保留项: {', '.join(others)}")
        lines.append(f"  [配置] eoq_guard_config.json: {'✅' if hooks.get('eoq_guard_config_exists') else '⚪'}")

        glob = payload.get("global", {})
        skills = glob.get("skills", {})
        skill_summary = ", ".join(f"{k}={'✅' if v else '⚪'}" for k, v in skills.items())
        lines.append(f"  [全局技能] {skill_summary}")

        ws = payload.get("workspace")
        if isinstance(ws, dict):
            lines.append(
                f"  [工作区] {ws.get('path')}: "
                f"eoq-task-relay={'✅' if ws.get('eoq_task_relay') else '⚪'}, "
                f"version-seal={'✅' if ws.get('version_seal') else '⚪'}, "
                f"rules={'✅' if ws.get('engineering_rules') else '⚪'}"
            )
        return "\n".join(lines)

    if action == "install":
        lines.append(f"✅ Companion 装配完成 (配置目录: {payload.get('config_dir')})")
        hooks_in = payload.get("hooks_installed") or []
        if hooks_in:
            lines.append(f"  已写入 Hook 键: {', '.join(hooks_in)} (Python: {payload.get('python_exe')})")
        lines.append(f"  写入文件数: {len(payload.get('installed') or [])}")
        skipped = payload.get("skipped_existing") or []
        if skipped:
            lines.append(f"  已跳过既有项 (保护本机定制，未传 --force): {len(skipped)} 项")
        return "\n".join(lines)

    lines.append(f"✅ Companion 卸载完成 (配置目录: {payload.get('config_dir')})")
    hooks_rm = payload.get("hooks_removed") or []
    if hooks_rm:
        lines.append(f"  已摘除受管 Hook 键: {', '.join(hooks_rm)}")
    preserved = payload.get("preserved_hook_keys") or []
    if preserved:
        lines.append(f"  完好保留其他 Hook 键: {', '.join(preserved)}")
    lines.append(f"  移除受管文件数: {len(payload.get('removed') or [])}")
    return "\n".join(lines)
