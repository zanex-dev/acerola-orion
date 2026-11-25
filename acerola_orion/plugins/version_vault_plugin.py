# -*- coding: utf-8 -*-
"""
Version Vault Built-in Plugin (`acerola_orion/plugins/version_vault_plugin.py`).
- 独立微内核内置插件（`name = "version_vault"`）
- 独立负责 `archive` CLI 全部子命令（scan / seal / status / list / jobs / restore / push / worker / policy）
- 独立负责 `guard_start` / `guard_tick` / `guard_stop` 后台 ≥30s 节流异步巡检与队列消费
- 独立负责版本冷库 Panel Tab（`version_vault_tab` 可导入时挂载，否则静默降级）与托盘项（扫描/推云端）
- 零耦合：与其他可选插件相互独立
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from datetime import datetime, timezone
import inspect
import json
import os
from pathlib import Path
import threading
import time
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from acerola_orion.constants import (
    atomic_write_text,
    load_app_config,
    update_app_config,
)
from acerola_orion.core.kernel import HookBus

from .version_scanner import (
    CATEGORY_COMPLETED_TASK,
    CATEGORY_OLD_RELEASE,
    CATEGORY_STALE_PROJECT,
    CATEGORY_VERSIONED_SIBLING,
    CATEGORY_VERSIONS_DIR,
    TRIGGER_RELEASE_SEMVER_BUMP,
    TRIGGER_TASK_PACK_COMPLETED,
    TRIGGER_VERSIONED_SIBLING_BUMP,
    TRIGGER_VERSIONS_DIR_SNAPSHOT,
    VALID_CATEGORIES,
    VersionTriggerEvent,
    detect_new_version_triggers,
    scan_old_versions,
)
from .version_vault import VaultError, WorkspaceVersionVault
from ._helpers import (
    default_clipboard_writer,
    default_ui_call,
    invoke_host_action,
    ps1_cli,
    resolve_callable,
)

ARCHIVE_TAB_KEY = "version_vault"
ARCHIVE_TAB_TITLE = "🧊 版本冷库 (Archive)"

GUARD_POLL_INTERVAL_SEC = 30.0
DEFAULT_VAULT_MODE = "confirm"
VALID_VAULT_MODES: Tuple[str, ...] = ("confirm", "auto_after_settle")
DEFAULT_SETTLE_SEC = 300

_GUARD_STATE_KEY = "version_vault"

_TRIGGER_TO_CATEGORY: Dict[str, str] = {
    TRIGGER_VERSIONS_DIR_SNAPSHOT: CATEGORY_VERSIONS_DIR,
    TRIGGER_VERSIONED_SIBLING_BUMP: CATEGORY_VERSIONED_SIBLING,
    TRIGGER_RELEASE_SEMVER_BUMP: CATEGORY_OLD_RELEASE,
    TRIGGER_TASK_PACK_COMPLETED: CATEGORY_COMPLETED_TASK,
}


@dataclass
class VaultTrayActionResult:
    """Structured result returned by tray menu callbacks for panel host compatibility."""

    ok: bool
    message: str
    data: Dict[str, Any] = dataclasses.field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "message": self.message,
            "data": dict(self.data),
        }


def _utc_now_iso(ts: Optional[float] = None) -> str:
    dt = (
        datetime.now(timezone.utc)
        if ts is None
        else datetime.fromtimestamp(float(ts), tz=timezone.utc)
    )
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _format_bytes(num_bytes: int) -> str:
    val = float(max(0, int(num_bytes)))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if val < 1024.0 or unit == "TB":
            return f"{int(val)} {unit}" if unit == "B" else f"{val:.1f} {unit}"
        val /= 1024.0
    return f"{val:.1f} TB"


def _normalize_pin_entry(raw: str) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    p = Path(text)
    if p.is_absolute():
        return os.path.normpath(text)
    return text.replace("\\", "/").strip("/")


def _pin_match_key(raw: str) -> str:
    return _normalize_pin_entry(raw).replace("\\", "/").lower()


def _path_matches_pins(
    source_path: Path,
    workspace: str,
    pinned_paths: Sequence[str],
) -> bool:
    if not pinned_paths:
        return False
    try:
        src_nc = os.path.normcase(os.path.realpath(str(source_path)))
    except OSError:
        src_nc = os.path.normcase(os.path.abspath(str(source_path)))

    rel_low = ""
    if workspace:
        try:
            rel_low = source_path.relative_to(Path(workspace)).as_posix().lower()
        except ValueError:
            rel_low = ""

    for raw in pinned_paths:
        norm = _normalize_pin_entry(raw)
        if not norm:
            continue
        p = Path(norm)
        if p.is_absolute():
            try:
                pin_nc = os.path.normcase(os.path.realpath(str(p)))
            except OSError:
                pin_nc = os.path.normcase(os.path.abspath(str(p)))
            if src_nc == pin_nc or src_nc.startswith(pin_nc + os.sep):
                return True
            if pin_nc.startswith(src_nc + os.sep):
                return True
        else:
            pin_rel = norm.lower()
            if rel_low and (
                rel_low == pin_rel
                or rel_low.startswith(pin_rel + "/")
                or pin_rel.startswith(rel_low + "/")
            ):
                return True
            if workspace:
                cand = Path(workspace) / norm
                try:
                    pin_nc = os.path.normcase(os.path.realpath(str(cand)))
                except OSError:
                    pin_nc = os.path.normcase(os.path.abspath(str(cand)))
                if src_nc == pin_nc or src_nc.startswith(pin_nc + os.sep):
                    return True
                if pin_nc.startswith(src_nc + os.sep):
                    return True
    return False


def _compute_latest_mtime(target: Path) -> float:
    """Safely inspect latest mtime of a file or directory without following symlinks."""
    try:
        st = os.lstat(target)
    except OSError:
        return 0.0
    latest = float(st.st_mtime)
    if not target.is_dir() or target.is_symlink():
        return latest
    for dirpath, _dirnames, filenames in os.walk(target, topdown=True, followlinks=False):
        cur = Path(dirpath)
        try:
            cst = os.lstat(cur)
            if float(cst.st_mtime) > latest:
                latest = float(cst.st_mtime)
        except OSError:
            pass
        for fname in filenames:
            fpath = cur / fname
            try:
                fst = os.lstat(fpath)
                if float(fst.st_mtime) > latest:
                    latest = float(fst.st_mtime)
            except OSError:
                pass
    return latest


def _parse_categories(raw_categories: Optional[Sequence[str]]) -> List[str]:
    out: List[str] = []
    seen = set()
    for item in raw_categories or ():
        for part in str(item or "").split(","):
            cat = part.strip().lower()
            if cat and cat not in seen:
                seen.add(cat)
                out.append(cat)
    return out


def _as_json_flag(args: Any) -> bool:
    return bool(
        getattr(args, "json_sub", False)
        or getattr(args, "json_root", False)
        or getattr(args, "json", False)
    )


def _add_json_arg(parser: Any, *, dest: str = "json_sub") -> None:
    parser.add_argument(
        "--json",
        dest=dest,
        action="store_true",
        default=False,
        help="输出结构化 JSON",
    )


def _try_import_version_vault_tab() -> Any:
    """Import `VersionVaultTab` lazily; return None if UI module cannot be loaded."""
    try:
        from .version_vault_tab import VersionVaultTab

        return VersionVaultTab
    except ImportError:
        return None


def _build_toast_callback(ctx: Any, app: Any) -> Callable[..., None]:
    """Resolve `flash_hud_toast` (or HUD toast helper) from UI host / panel app."""
    direct = (
        resolve_callable(ctx, "flash_hud_toast", "toast_callback", "_flash_hud_toast")
        or resolve_callable(app, "flash_hud_toast", "toast_callback", "_flash_hud_toast")
    )

    def _toast(msg: str, color: str = "#38bdf8") -> None:
        text = str(msg or "").strip()
        if not text:
            return
        if direct is not None:
            try:
                direct(text, color)
                return
            except TypeError:
                try:
                    direct(text)
                    return
                except Exception:
                    pass
            except Exception:
                pass
        hud = getattr(app, "_hud_win", None) or getattr(ctx, "_hud_win", None)
        if hud is not None:
            for attr in ("flash_toast", "show_toast"):
                fn = getattr(hud, attr, None)
                if callable(fn):
                    try:
                        fn(text, color)
                        return
                    except TypeError:
                        try:
                            fn(text)
                            return
                        except Exception:
                            pass
                    except Exception:
                        pass

    return _toast


class VersionVaultPlugin:
    """Built-in plugin for Workspace Version Vault CLI, guard daemon, Tab, and Tray."""

    name: str = "version_vault"

    def __init__(
        self,
        *,
        vault_dir: Optional[Path | str] = None,
        config_path: Optional[Path | str] = None,
        poll_interval_sec: float = GUARD_POLL_INTERVAL_SEC,
        workspaces_override: Optional[Sequence[Path | str]] = None,
        editable_roots_override: Optional[Sequence[Path | str]] = None,
    ) -> None:
        self._vault_dir: Optional[Path] = Path(vault_dir) if vault_dir is not None else None
        self._config_path: Optional[Path] = Path(config_path) if config_path is not None else None
        self._poll_interval_sec: float = max(1.0, float(poll_interval_sec))
        self._workspaces_override: Optional[List[str]] = (
            [str(w) for w in workspaces_override] if workspaces_override is not None else None
        )
        self._editable_roots_override: Optional[List[str]] = (
            [str(e) for e in editable_roots_override]
            if editable_roots_override is not None
            else None
        )

        self._ui_host: Any = None
        self._archive_tab: Any = None

        self._guard_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._bg_thread: Optional[threading.Thread] = None
        self._last_poll_at: float = 0.0
        self._cycle_count: int = 0

    # ------------------------------------------------------------------
    # Vault, Policy & Pending Seals Helpers
    # ------------------------------------------------------------------
    def _make_vault(self) -> WorkspaceVersionVault:
        return WorkspaceVersionVault(
            vault_dir=self._vault_dir,
            config_path=self._config_path,
        )

    def _trigger_state_path(self, vault: WorkspaceVersionVault) -> Path:
        return vault.vault_dir / "trigger_state.json"

    def _pending_seals_path(self, vault: WorkspaceVersionVault) -> Path:
        return vault.vault_dir / "pending_seals.json"

    def load_full_policy(self, vault: Optional[WorkspaceVersionVault] = None) -> Dict[str, Any]:
        """Merge engine policy (`version_vault_*`) with daemon/plugin policy keys in `config.json`."""
        v = vault if vault is not None else self._make_vault()
        policy = v.load_policy()
        cfg = load_app_config(v.config_path)

        raw_mode = str(cfg.get("version_vault_mode") or DEFAULT_VAULT_MODE).strip().lower()
        mode = raw_mode if raw_mode in VALID_VAULT_MODES else DEFAULT_VAULT_MODE

        try:
            settle_sec = max(0, int(cfg.get("version_vault_settle_sec", DEFAULT_SETTLE_SEC)))
        except (TypeError, ValueError):
            settle_sec = DEFAULT_SETTLE_SEC

        raw_pins = cfg.get("version_vault_pinned_paths")
        pinned: List[str] = []
        seen_keys = set()
        if isinstance(raw_pins, list):
            for entry in raw_pins:
                norm = _normalize_pin_entry(str(entry))
                key = _pin_match_key(norm)
                if norm and key not in seen_keys:
                    seen_keys.add(key)
                    pinned.append(norm)

        policy["version_vault_mode"] = mode
        policy["version_vault_settle_sec"] = settle_sec
        policy["version_vault_pinned_paths"] = pinned
        return policy

    def update_full_policy(
        self,
        *,
        mode: Optional[str] = None,
        remote_url: Optional[str] = None,
        branch: Optional[str] = None,
        settle_sec: Optional[int] = None,
        pin_paths: Sequence[str] = (),
        unpin_paths: Sequence[str] = (),
        vault: Optional[WorkspaceVersionVault] = None,
    ) -> Dict[str, Any]:
        """Validate and persist both engine-level and plugin-level policy keys."""
        v = vault if vault is not None else self._make_vault()

        engine_updates: Dict[str, Any] = {}
        if remote_url is not None:
            engine_updates["remote_url"] = str(remote_url).strip()
        if branch is not None and str(branch).strip():
            engine_updates["branch"] = str(branch).strip()

        plugin_updates: Dict[str, Any] = {}
        if mode is not None:
            clean_mode = str(mode).strip().lower()
            if clean_mode not in VALID_VAULT_MODES:
                raise VaultError(
                    f"不支持的归档模式：{mode!r}（仅支持 {' | '.join(VALID_VAULT_MODES)}）"
                )
            plugin_updates["version_vault_mode"] = clean_mode

        if settle_sec is not None:
            val = int(settle_sec)
            if val < 0:
                raise VaultError(f"宽限静默秒数 --settle-sec 不能为负数：{val}")
            plugin_updates["version_vault_settle_sec"] = val

        if engine_updates:
            v.update_policy(**engine_updates)

        if plugin_updates or pin_paths or unpin_paths:
            def _mutate(cfg: Dict[str, Any]) -> Dict[str, Any]:
                cfg.update(plugin_updates)
                if pin_paths or unpin_paths:
                    raw_existing = cfg.get("version_vault_pinned_paths")
                    cur_list: List[str] = (
                        [str(x) for x in raw_existing if str(x).strip()]
                        if isinstance(raw_existing, list)
                        else []
                    )
                    items: List[str] = []
                    keys: set[str] = set()
                    for item in cur_list:
                        norm = _normalize_pin_entry(item)
                        k = _pin_match_key(norm)
                        if norm and k not in keys:
                            keys.add(k)
                            items.append(norm)
                    for raw_p in pin_paths:
                        norm = _normalize_pin_entry(raw_p)
                        k = _pin_match_key(norm)
                        if norm and k not in keys:
                            keys.add(k)
                            items.append(norm)
                    if unpin_paths:
                        remove_keys = {
                            _pin_match_key(raw_u)
                            for raw_u in unpin_paths
                            if _normalize_pin_entry(raw_u)
                        }
                        items = [it for it in items if _pin_match_key(it) not in remove_keys]
                    cfg["version_vault_pinned_paths"] = items
                return cfg

            update_app_config(_mutate, path=v.config_path)

        return self.load_full_policy(vault=v)

    def _load_pending_seals(self, vault: WorkspaceVersionVault) -> List[Dict[str, Any]]:
        path = self._pending_seals_path(vault)
        if not path.is_file():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        raw_items = data.get("items") if isinstance(data, dict) else None
        if not isinstance(raw_items, list):
            return []
        return [dict(it) for it in raw_items if isinstance(it, dict) and it.get("source_path")]

    def _save_pending_seals(
        self,
        vault: WorkspaceVersionVault,
        items: Sequence[Dict[str, Any]],
        *,
        now_ts: Optional[float] = None,
    ) -> None:
        path = self._pending_seals_path(vault)
        payload = {
            "schema_version": 1,
            "updated_at": _utc_now_iso(now_ts),
            "count": len(items),
            "items": list(items),
        }
        atomic_write_text(
            path,
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def _prune_pending_paths(
        self,
        vault: WorkspaceVersionVault,
        sealed_paths: Iterable[str],
    ) -> None:
        remove_nc = {
            os.path.normcase(os.path.abspath(str(p)))
            for p in sealed_paths
            if str(p).strip()
        }
        if not remove_nc:
            return
        existing = self._load_pending_seals(vault)
        if not existing:
            return
        kept = [
            it
            for it in existing
            if os.path.normcase(os.path.abspath(str(it.get("source_path") or "")))
            not in remove_nc
        ]
        if len(kept) != len(existing):
            self._save_pending_seals(vault, kept)

    def _resolve_workspaces(
        self,
        policy: Dict[str, Any],
        explicit_workspaces: Optional[Sequence[str]] = None,
    ) -> Optional[List[str]]:
        if explicit_workspaces:
            cleaned = [str(w).strip() for w in explicit_workspaces if str(w).strip()]
            if cleaned:
                return cleaned
        if self._workspaces_override is not None:
            return list(self._workspaces_override)
        scan_roots = policy.get("version_vault_scan_roots")
        if isinstance(scan_roots, list) and scan_roots:
            return [str(w) for w in scan_roots if str(w).strip()]
        return None

    # ------------------------------------------------------------------
    # 1. CLI Subcommand (`archive <action>`)
    # ------------------------------------------------------------------
    def register_cli(self, subparsers: Any) -> None:
        """Register CLI subcommand `archive` and its 9 sub-actions."""
        p_arch = subparsers.add_parser(
            "archive",
            help="工作区旧版本只增不删冷库（扫描 / 封存 / 状态 / 快照 / 队列 / 还原 / 推云端 / 策略）",
        )
        _add_json_arg(p_arch, dest="json_root")
        arch_sub = p_arch.add_subparsers(dest="archive_action", help="冷库操作子命令")

        # archive scan
        p_scan = arch_sub.add_parser("scan", help="扫描工作区 5+1 类旧版本候选（只读不改磁盘）")
        p_scan.add_argument("--workspace", action="append", default=[], help="指定扫描工作区（可重复）")
        p_scan.add_argument("--project", default="", help="按项目名过滤")
        p_scan.add_argument("--category", action="append", default=[], help="按类别过滤（可重复或逗号分隔）")
        p_scan.add_argument(
            "--include-stale-projects",
            action="store_true",
            default=False,
            help="包含静默失活项目目录（stale_project）",
        )
        p_scan.add_argument("--stale-days", type=float, default=1.0, help="失活项目天数阈值（默认 1.0）")
        _add_json_arg(p_scan)

        # archive seal
        p_seal = arch_sub.add_parser("seal", help="封存旧版本入冷库（默认 --dry-run 预览）")
        p_seal.add_argument("paths", nargs="*", help="可选：显式指定待封存文件/目录路径")
        p_seal.add_argument("--dry-run", action="store_true", default=False, help="仅预览待封存清单（默认）")
        p_seal.add_argument("--confirm", action="store_true", default=False, help="确认后异步入队并拉起后台 worker")
        p_seal.add_argument("--sync", action="store_true", default=False, help="同步执行封存与双向校验")
        p_seal.add_argument("--workspace", action="append", default=[], help="指定扫描工作区（可重复）")
        p_seal.add_argument("--project", default="", help="按项目名过滤候选")
        p_seal.add_argument("--category", action="append", default=[], help="按类别过滤候选")
        p_seal.add_argument(
            "--include-stale-projects",
            action="store_true",
            default=False,
            help="显式包含 stale_project 候选",
        )
        p_seal.add_argument("--stale-days", type=float, default=1.0, help="失活项目天数阈值（默认 1.0）")
        p_seal.add_argument(
            "--keep-source",
            action="store_true",
            default=False,
            help="封存入库后保留源路径不移出",
        )
        p_seal.add_argument("--reason", default="", help="封存原因备注")
        _add_json_arg(p_seal)

        # archive status
        p_status = arch_sub.add_parser("status", help="查看冷库快照、队列、Git 远端与待封版状态")
        _add_json_arg(p_status)

        # archive list
        p_list = arch_sub.add_parser("list", help="列出冷库历史快照")
        p_list.add_argument("--project", default="", help="按项目名过滤")
        p_list.add_argument("--category", default="", help="按类别过滤")
        p_list.add_argument("--query", default="", help="关键词模糊检索")
        p_list.add_argument("--limit", type=int, default=50, help="最多返回条数（默认 50）")
        _add_json_arg(p_list)

        # archive jobs
        p_jobs = arch_sub.add_parser("jobs", help="查看异步归档任务队列历史")
        p_jobs.add_argument("--status", default="", help="按状态过滤（queued/running/succeeded/failed/partial_failed）")
        p_jobs.add_argument("--limit", type=int, default=30, help="最多返回条数（默认 30）")
        _add_json_arg(p_jobs)

        # archive restore <id>
        p_restore = arch_sub.add_parser("restore", help="从冷库原样还原指定快照（校验 SHA-256）")
        p_restore.add_argument("snapshot_id", help="待还原的快照 ID")
        p_restore.add_argument("--target", default="", help="可选：还原到指定目标目录（不传则还原原路径）")
        p_restore.add_argument("--force", action="store_true", default=False, help="若目标已存在则强制覆盖")
        _add_json_arg(p_restore)

        # archive push
        p_push = arch_sub.add_parser("push", help="按需提交并推送冷库 Git 仓库到远端（永不 force push）")
        p_push.add_argument("--remote", default="", help="覆盖远端 Git 仓库 URL")
        p_push.add_argument("--branch", default="", help="覆盖推送分支（默认读取策略）")
        p_push.add_argument("--dry-run", action="store_true", default=False, help="仅检查变更，不执行提交和推送")
        _add_json_arg(p_push)

        # archive worker
        p_worker = arch_sub.add_parser("worker", help="手动消费一次待处理异步归档队列")
        p_worker.add_argument("--max-jobs", type=int, default=20, help="单次最多消费任务数（默认 20）")
        p_worker.add_argument("--spawn", action="store_true", default=False, help="以独立后台进程拉起 worker")
        _add_json_arg(p_worker)

        # archive policy
        p_policy = arch_sub.add_parser("policy", help="查看或修改冷库归档模式、远端、静默宽限期与豁免路径")
        p_policy.add_argument(
            "--mode",
            choices=list(VALID_VAULT_MODES),
            default=None,
            help="触发后策略：confirm（写待封版）| auto_after_settle（静默满宽限期自动外迁非 stale_project）",
        )
        p_policy.add_argument("--remote", default=None, help="设置冷库远端 Git URL")
        p_policy.add_argument("--branch", default=None, help="设置冷库 Git 分支")
        p_policy.add_argument("--settle-sec", type=int, default=None, help="自动外迁静默宽限秒数")
        p_policy.add_argument("--pin", action="append", default=[], help="新增豁免保护路径（可重复）")
        p_policy.add_argument("--unpin", action="append", default=[], help="移除豁免保护路径（可重复）")
        _add_json_arg(p_policy)

    def handle_cli(self, cmd: str, args: Any) -> Optional[int]:
        """Dispatch `archive <action>` CLI subcommand."""
        if cmd != "archive":
            return None

        action = str(getattr(args, "archive_action", None) or "status").strip().lower()
        as_json = _as_json_flag(args)

        dispatch = {
            "scan": self._cli_scan,
            "seal": self._cli_seal,
            "status": self._cli_status,
            "list": self._cli_list,
            "jobs": self._cli_jobs,
            "restore": self._cli_restore,
            "push": self._cli_push,
            "worker": self._cli_worker,
            "policy": self._cli_policy,
        }
        handler = dispatch.get(action)
        if handler is None:
            payload = {"ok": False, "error": f"未知 archive 子命令：{action}"}
            if as_json:
                print(json.dumps(payload, ensure_ascii=False, indent=2))
            else:
                print(f"❌ {payload['error']}")
            return 1

        try:
            return handler(args, as_json=as_json)
        except Exception as exc:
            payload = {
                "ok": False,
                "action": action,
                "error_type": type(exc).__name__,
                "error": str(exc),
            }
            if as_json:
                print(json.dumps(payload, ensure_ascii=False, indent=2))
            else:
                print(f"❌ archive {action} 失败: {exc}")
            return 1

    def _cli_scan(self, args: Any, *, as_json: bool) -> int:
        vault = self._make_vault()
        policy = self.load_full_policy(vault)
        workspaces = self._resolve_workspaces(policy, getattr(args, "workspace", None))
        cats = _parse_categories(getattr(args, "category", None))
        report = scan_old_versions(
            workspaces,
            include_stale_projects=bool(getattr(args, "include_stale_projects", False)),
            stale_days=float(getattr(args, "stale_days", 1.0)),
            project_filter=str(getattr(args, "project", "") or ""),
            pinned_paths=policy.get("version_vault_pinned_paths") or (),
            categories=cats or None,
            editable_roots=self._editable_roots_override,
        )
        data = report.to_dict()
        data["ok"] = True
        data["candidate_count"] = len(report.candidates)
        data["protected_count"] = len(report.protected_items)
        data["total_candidate_bytes"] = sum(c.size_bytes for c in report.candidates)
        if as_json:
            print(json.dumps(data, ensure_ascii=False, indent=2))
        else:
            print(
                f"🧊 扫描完成：{len(report.workspaces)} 个工作区 | "
                f"可封存候选 {len(report.candidates)} 项（{_format_bytes(data['total_candidate_bytes'])}）| "
                f"受保护豁免 {len(report.protected_items)} 项"
            )
            for c in report.candidates[:30]:
                tag = " [需二次确认]" if c.requires_explicit_confirm else ""
                print(
                    f"  - [{c.category}] {c.project}: {c.rel_path} "
                    f"({_format_bytes(c.size_bytes)}, {c.file_count} files){tag} — {c.trigger_reason}"
                )
            if len(report.candidates) > 30:
                print(f"  ... 其余 {len(report.candidates) - 30} 项请加 --json 查看完整清单")
            for w in report.warnings[:10]:
                print(f"  ⚠️ {w}")
        return 0

    def _cli_seal(self, args: Any, *, as_json: bool) -> int:
        vault = self._make_vault()
        policy = self.load_full_policy(vault)

        explicit_paths = [str(p).strip() for p in (getattr(args, "paths", None) or []) if str(p).strip()]
        project_filter = str(getattr(args, "project", "") or "").strip()
        cats = _parse_categories(getattr(args, "category", None))
        include_stale = bool(getattr(args, "include_stale_projects", False))
        keep_source = bool(getattr(args, "keep_source", False))
        reason = str(getattr(args, "reason", "") or "").strip()
        evict_source = False if keep_source else bool(policy.get("version_vault_evict_source", True))

        dry_run_flag = bool(getattr(args, "dry_run", False))
        confirm_flag = bool(getattr(args, "confirm", False))
        sync_flag = bool(getattr(args, "sync", False))
        effective_dry_run = dry_run_flag or (not confirm_flag and not sync_flag)

        items_to_seal: List[Dict[str, Any]] = []
        preview_items: List[Dict[str, Any]] = []
        total_bytes = 0

        if explicit_paths:
            default_cat = cats[0] if cats else ""
            for raw_p in explicit_paths:
                p = Path(raw_p)
                entry: Dict[str, Any] = {
                    "source_path": os.path.abspath(str(p)),
                    "project": project_filter,
                    "category": default_cat,
                    "trigger_reason": reason or "cli_explicit_seal",
                }
                items_to_seal.append(entry)
                preview_items.append(dict(entry))
        else:
            workspaces = self._resolve_workspaces(policy, getattr(args, "workspace", None))
            report = scan_old_versions(
                workspaces,
                include_stale_projects=include_stale,
                stale_days=float(getattr(args, "stale_days", 1.0)),
                project_filter=project_filter,
                pinned_paths=policy.get("version_vault_pinned_paths") or (),
                categories=cats or None,
                editable_roots=self._editable_roots_override,
            )
            for cand in report.candidates:
                if cand.protected:
                    continue
                if not include_stale and (
                    cand.category == CATEGORY_STALE_PROJECT or cand.requires_explicit_confirm
                ):
                    continue
                item_reason = reason or cand.trigger_reason or "cli_scan_seal"
                items_to_seal.append(
                    {
                        "source_path": cand.source_path,
                        "project": cand.project,
                        "category": cand.category,
                        "trigger_reason": item_reason,
                    }
                )
                preview_items.append(
                    {
                        "source_path": cand.source_path,
                        "rel_path": cand.rel_path,
                        "project": cand.project,
                        "category": cand.category,
                        "size_bytes": cand.size_bytes,
                        "file_count": cand.file_count,
                        "trigger_reason": item_reason,
                        "requires_explicit_confirm": cand.requires_explicit_confirm,
                    }
                )
                total_bytes += cand.size_bytes

        if not items_to_seal:
            payload = {
                "ok": True,
                "dry_run": effective_dry_run,
                "mode": "dry_run" if effective_dry_run else ("sync" if sync_flag else "async"),
                "evict_source": evict_source,
                "count": 0,
                "total_bytes": 0,
                "items": [],
                "message": "未找到符合条件的待封存旧版本条目",
            }
            if as_json:
                print(json.dumps(payload, ensure_ascii=False, indent=2))
            else:
                print(f"ℹ️ {payload['message']}")
            return 0

        if effective_dry_run:
            payload = {
                "ok": True,
                "dry_run": True,
                "mode": "dry_run",
                "evict_source": evict_source,
                "count": len(preview_items),
                "total_bytes": total_bytes,
                "items": preview_items,
                "message": (
                    f"dry-run 预览：共 {len(preview_items)} 项待封存"
                    f"（{_format_bytes(total_bytes)}，移出源={evict_source}）；"
                    "请附加 --confirm（异步）或 --sync（同步）执行"
                ),
            }
            if as_json:
                print(json.dumps(payload, ensure_ascii=False, indent=2))
            else:
                print(f"🧪 {payload['message']}")
                for it in preview_items[:30]:
                    print(
                        f"  - [{it.get('category') or 'auto'}] "
                        f"{it.get('project') or 'auto'}: {it['source_path']}"
                    )
            return 0

        if sync_flag:
            res = vault.seal_items_sync(
                items_to_seal,
                trigger_reason=reason or "cli_seal_sync",
                evict_source=evict_source,
            )
            self._prune_pending_paths(vault, [it["source_path"] for it in items_to_seal])
            payload = {
                **res,
                "dry_run": False,
                "mode": "sync",
                "evict_source": evict_source,
            }
            if as_json:
                print(json.dumps(payload, ensure_ascii=False, indent=2))
            else:
                prefix = "✅" if res["ok"] else "⚠️"
                print(
                    f"{prefix} 同步封存完成：job={res['job_id']} | "
                    f"成功 {res['completed_items']}/{res['total_items']} | "
                    f"失败 {res['failed_items']} | 移出源={evict_source}"
                )
            return 0 if res["ok"] else 1

        # --confirm: enqueue async job + spawn detached worker
        job = vault.enqueue_seal_job(
            items_to_seal,
            trigger_reason=reason or "cli_seal_confirm",
            evict_source=evict_source,
        )
        worker = vault.spawn_async_worker()
        self._prune_pending_paths(vault, [it["source_path"] for it in items_to_seal])
        payload = {
            "ok": True,
            "dry_run": False,
            "mode": "async",
            "evict_source": evict_source,
            "job_id": job.job_id,
            "job": job.to_dict(),
            "worker": worker,
            "message": f"已入队异步封存任务 {job.job_id}（{job.total_items} 项），后台 worker PID={worker.get('pid')}",
        }
        if as_json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print(f"✅ {payload['message']}")
        return 0

    def _cli_status(self, _args: Any, *, as_json: bool) -> int:
        vault = self._make_vault()
        status = vault.get_status()
        full_policy = self.load_full_policy(vault)
        pending = self._load_pending_seals(vault)

        data = status.to_dict()
        data["ok"] = True
        data["policy"] = full_policy
        data["mode"] = full_policy["version_vault_mode"]
        data["settle_sec"] = full_policy["version_vault_settle_sec"]
        data["pinned_paths"] = list(full_policy["version_vault_pinned_paths"])
        data["pending_seals_count"] = len(pending)
        data["pending_seals"] = pending

        if as_json:
            print(json.dumps(data, ensure_ascii=False, indent=2))
        else:
            print(f"🧊 Workspace Version Vault 状态 ({status.vault_dir})")
            print(
                f"  ● 历史快照 : {status.snapshot_count} 个 | "
                f"{status.total_files} 文件 | {_format_bytes(status.total_bytes)} | "
                f"本地隔离大文件 {status.large_asset_count} 项"
            )
            print(
                f"  ● 异步队列 : queued={status.queued_jobs} | running={status.running_jobs} | "
                f"succeeded={status.succeeded_jobs} | failed={status.failed_jobs}"
            )
            print(
                f"  ● 触发策略 : mode={data['mode']} | settle_sec={data['settle_sec']}s | "
                f"pinned={len(data['pinned_paths'])} | 待封版={len(pending)}"
            )
            print(
                f"  ● Git 远端 : initialized={status.git_initialized} | "
                f"branch={status.branch} | remote={status.remote_url or '(未配置)'} | "
                f"last_push={status.last_push_status or '-'} ({status.last_push_at or '-'})"
            )
        return 0

    def _cli_list(self, args: Any, *, as_json: bool) -> int:
        vault = self._make_vault()
        snaps = vault.list_snapshots(
            project=str(getattr(args, "project", "") or ""),
            category=str(getattr(args, "category", "") or ""),
            query=str(getattr(args, "query", "") or ""),
            limit=int(getattr(args, "limit", 50)),
        )
        payload = {
            "ok": True,
            "count": len(snaps),
            "snapshots": [s.to_dict() for s in snaps],
        }
        if as_json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print(f"🧊 冷库快照列表（共 {len(snaps)} 条）")
            for s in snaps:
                ev = "已外迁" if s.source_evicted else "保留源"
                print(
                    f"  - {s.snapshot_id} | {s.created_at} | {s.project}/{s.category} | "
                    f"{s.file_count} files ({_format_bytes(s.total_bytes)}) | {ev} | {s.source_path}"
                )
        return 0

    def _cli_jobs(self, args: Any, *, as_json: bool) -> int:
        vault = self._make_vault()
        jobs = vault.list_jobs(
            status=str(getattr(args, "status", "") or ""),
            limit=int(getattr(args, "limit", 30)),
        )
        payload = {
            "ok": True,
            "count": len(jobs),
            "jobs": [j.to_dict() for j in jobs],
        }
        if as_json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print(f"📋 冷库异步任务队列（共 {len(jobs)} 条）")
            for j in jobs:
                print(
                    f"  - {j.job_id} | [{j.status}] {j.completed_items}/{j.total_items} "
                    f"(fail={j.failed_items}) | {j.created_at} | {j.trigger_reason}"
                )
        return 0

    def _cli_restore(self, args: Any, *, as_json: bool) -> int:
        vault = self._make_vault()
        snap_id = str(getattr(args, "snapshot_id", "") or "").strip()
        target = str(getattr(args, "target", "") or "").strip()
        force = bool(getattr(args, "force", False))
        res = vault.restore_snapshot(
            snap_id,
            target_dir=target or None,
            force=force,
        )
        if as_json:
            print(json.dumps(res, ensure_ascii=False, indent=2))
        else:
            print(
                f"✅ 快照 {snap_id} 已还原并校验通过 -> {res['restored_path']} "
                f"({res['file_count']} files, {_format_bytes(res['total_bytes'])})"
            )
        return 0

    def _cli_push(self, args: Any, *, as_json: bool) -> int:
        vault = self._make_vault()
        res = vault.push_to_remote(
            remote_url=str(getattr(args, "remote", "") or "").strip(),
            branch=str(getattr(args, "branch", "") or "").strip(),
            dry_run=bool(getattr(args, "dry_run", False)),
        )
        data = res.to_dict()
        if as_json:
            print(json.dumps(data, ensure_ascii=False, indent=2))
        else:
            prefix = "✅" if res.ok else "❌"
            print(f"{prefix} {res.message} (branch={res.branch}, changed={res.changed_files})")
        return 0 if res.ok else 1

    def _cli_worker(self, args: Any, *, as_json: bool) -> int:
        vault = self._make_vault()
        max_jobs = int(getattr(args, "max_jobs", 20))
        if bool(getattr(args, "spawn", False)):
            res = vault.spawn_async_worker(max_jobs=max_jobs)
            payload = {"ok": True, **res}
            if as_json:
                print(json.dumps(payload, ensure_ascii=False, indent=2))
            else:
                print(f"✅ 已拉起后台 worker PID={res['pid']} (log={res['log_path']})")
            return 0

        summary = vault.run_worker_once(max_jobs=max_jobs)
        ok = int(summary.get("failed_jobs", 0)) == 0
        payload = {"ok": ok, **summary}
        if as_json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            prefix = "✅" if ok else "⚠️"
            print(
                f"{prefix} worker 消费完成：processed={summary['processed_jobs']} | "
                f"succeeded={summary['succeeded_jobs']} | partial={summary['partial_failed_jobs']} | "
                f"failed={summary['failed_jobs']} | snapshots={summary['snapshots_created']}"
            )
        return 0 if ok else 1

    def _cli_policy(self, args: Any, *, as_json: bool) -> int:
        vault = self._make_vault()
        mode = getattr(args, "mode", None)
        remote = getattr(args, "remote", None)
        branch = getattr(args, "branch", None)
        settle_sec = getattr(args, "settle_sec", None)
        pins = list(getattr(args, "pin", None) or [])
        unpins = list(getattr(args, "unpin", None) or [])

        changed = any(
            [
                mode is not None,
                remote is not None,
                branch is not None,
                settle_sec is not None,
                bool(pins),
                bool(unpins),
            ]
        )
        if changed:
            policy = self.update_full_policy(
                mode=mode,
                remote_url=remote,
                branch=branch,
                settle_sec=settle_sec,
                pin_paths=pins,
                unpin_paths=unpins,
                vault=vault,
            )
        else:
            policy = self.load_full_policy(vault)

        payload = {
            "ok": True,
            "updated": changed,
            "mode": policy["version_vault_mode"],
            "remote_url": policy["version_vault_remote_url"],
            "branch": policy["version_vault_branch"],
            "settle_sec": policy["version_vault_settle_sec"],
            "pinned_paths": list(policy["version_vault_pinned_paths"]),
            "policy": policy,
        }
        if as_json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            tag = "✅ 冷库策略已更新" if changed else "⚙️ 冷库当前策略"
            print(tag)
            print(f"  ● mode         : {payload['mode']}")
            print(f"  ● remote_url   : {payload['remote_url'] or '(未配置)'}")
            print(f"  ● branch       : {payload['branch']}")
            print(f"  ● settle_sec   : {payload['settle_sec']}s")
            print(f"  ● pinned_paths : {payload['pinned_paths']}")
        return 0

    # ------------------------------------------------------------------
    # 2. Microkernel HookBus (`guard_start` / `guard_tick` / `guard_stop`)
    # ------------------------------------------------------------------
    def register_hooks(self, bus: HookBus) -> None:
        """Subscribe to Guillotine daemon lifecycle hooks."""
        bus.on("guard_start", self._on_guard_start)
        bus.on("guard_tick", self._on_guard_tick)
        bus.on("guard_stop", self._on_guard_stop)

    def _ensure_guard_state(self, ctx: Dict[str, Any]) -> Dict[str, Any]:
        root_state = ctx.setdefault("state", {})
        st = root_state.get(_GUARD_STATE_KEY)
        if not isinstance(st, dict):
            st = {
                "last_poll_at": 0.0,
                "cycles": 0,
                "last_error": "",
                "stopped": False,
            }
            root_state[_GUARD_STATE_KEY] = st
        return st

    def _on_guard_start(self, ctx: Dict[str, Any]) -> None:
        log = ctx.get("log") or (lambda _m: None)
        with self._guard_lock:
            self._stop_event.clear()
            self._last_poll_at = 0.0
            self._cycle_count = 0
        st = self._ensure_guard_state(ctx)
        st["last_poll_at"] = 0.0
        st["cycles"] = 0
        st["last_error"] = ""
        st["stopped"] = False
        log(f"VersionVault guard hook armed (poll_interval={self._poll_interval_sec:.0f}s)")

    def _on_guard_tick(self, ctx: Dict[str, Any]) -> None:
        log = ctx.get("log") or (lambda _m: None)
        now = float(ctx.get("now") or time.time())
        st = self._ensure_guard_state(ctx)

        with self._guard_lock:
            if self._stop_event.is_set():
                return
            if self._bg_thread is not None and self._bg_thread.is_alive():
                return
            if self._last_poll_at > 0.0 and (now - self._last_poll_at) < self._poll_interval_sec:
                return
            self._last_poll_at = now
            self._cycle_count += 1
            st["last_poll_at"] = now
            st["cycles"] = self._cycle_count

            t = threading.Thread(
                target=self._run_guard_cycle_safe,
                kwargs={"now_ts": now, "log": log, "state_dict": st},
                name="orion-version-vault-guard",
                daemon=True,
            )
            self._bg_thread = t
            t.start()

    def _on_guard_stop(self, ctx: Dict[str, Any]) -> None:
        log = ctx.get("log") or (lambda _m: None)
        st = self._ensure_guard_state(ctx)
        st["stopped"] = True
        with self._guard_lock:
            self._stop_event.set()
            bg = self._bg_thread
        if bg is not None and bg.is_alive():
            bg.join(timeout=1.5)
        log("VersionVault guard hook stopped")

    def wait_for_idle(self, timeout: float = 5.0) -> bool:
        """Wait for any in-flight guard background cycle to complete (testing/shutdown helper)."""
        with self._guard_lock:
            bg = self._bg_thread
        if bg is None:
            return True
        bg.join(timeout=max(0.0, float(timeout)))
        return not bg.is_alive()

    def _run_guard_cycle_safe(
        self,
        *,
        now_ts: float,
        log: Callable[[str], None],
        state_dict: Dict[str, Any],
    ) -> None:
        try:
            self._run_guard_cycle(now_ts=now_ts, log=log)
            state_dict["last_error"] = ""
        except Exception as exc:
            state_dict["last_error"] = str(exc)
            try:
                log(f"VersionVault guard cycle error: {exc}")
            except Exception:
                pass

    def _run_guard_cycle(self, *, now_ts: float, log: Callable[[str], None]) -> Dict[str, Any]:
        """Single throttled background patrol: consume queue + detect triggers + apply mode."""
        vault = self._make_vault()
        policy = self.load_full_policy(vault)
        if not bool(policy.get("version_vault_enabled", True)):
            return {"enabled": False, "processed_jobs": 0, "triggers": 0, "auto_sealed": 0}

        # 1) Consume any pending queued archive jobs first
        processed_jobs = 0
        status_before = vault.get_status()
        has_queue_files = any(vault.queue_dir.glob("job_*.json"))
        if status_before.queued_jobs > 0 or has_queue_files:
            wres = vault.run_worker_once(max_jobs=5)
            processed_jobs = int(wres.get("processed_jobs", 0))
            if processed_jobs > 0:
                log(
                    f"VersionVault guard worker processed {processed_jobs} job(s), "
                    f"created {wres.get('snapshots_created', 0)} snapshot(s)"
                )

        if self._stop_event.is_set():
            return {"enabled": True, "processed_jobs": processed_jobs, "triggers": 0, "auto_sealed": 0}

        # 2) Detect newly created version triggers
        workspaces = self._resolve_workspaces(policy)
        pinned_paths: List[str] = list(policy.get("version_vault_pinned_paths") or [])
        events: List[VersionTriggerEvent] = detect_new_version_triggers(
            workspaces,
            self._trigger_state_path(vault),
            persist=True,
            pinned_paths=pinned_paths,
            editable_roots=self._editable_roots_override,
            now=now_ts,
        )

        # 3) Merge trigger events into persistent pending_seals.json
        existing_pending = self._load_pending_seals(vault)
        by_path_nc: Dict[str, Dict[str, Any]] = {}
        for item in existing_pending:
            sp = str(item.get("source_path") or "").strip()
            if not sp:
                continue
            by_path_nc[os.path.normcase(os.path.abspath(sp))] = item

        added_count = 0
        for ev in events:
            cat = _TRIGGER_TO_CATEGORY.get(ev.kind, "")
            if not cat or cat == CATEGORY_STALE_PROJECT:
                continue
            for raw_old in ev.old_candidate_paths:
                if not raw_old:
                    continue
                abs_old = os.path.abspath(str(raw_old))
                p_old = Path(abs_old)
                if not p_old.exists():
                    continue
                if _path_matches_pins(p_old, ev.workspace, pinned_paths):
                    continue
                k = os.path.normcase(abs_old)
                if k in by_path_nc:
                    by_path_nc[k]["last_seen_at"] = now_ts
                    by_path_nc[k]["newest_path"] = ev.newest_path
                    by_path_nc[k]["newest_version"] = ev.newest_version
                else:
                    by_path_nc[k] = {
                        "source_path": abs_old,
                        "category": cat,
                        "project": ev.project,
                        "workspace": ev.workspace,
                        "trigger_kind": ev.kind,
                        "trigger_reason": f"检测到新版 {ev.newest_version} ({ev.kind})",
                        "newest_path": ev.newest_path,
                        "newest_version": ev.newest_version,
                        "requires_explicit_confirm": False,
                        "first_seen_at": now_ts,
                        "last_seen_at": now_ts,
                        "detected_at": ev.detected_at,
                    }
                    added_count += 1

        # Clean up entries whose source disappeared or became pinned
        active_items: List[Dict[str, Any]] = []
        for item in by_path_nc.values():
            p_src = Path(str(item["source_path"]))
            if not p_src.exists():
                continue
            if _path_matches_pins(p_src, str(item.get("workspace") or ""), pinned_paths):
                continue
            active_items.append(item)

        mode = str(policy.get("version_vault_mode") or DEFAULT_VAULT_MODE)
        settle_sec = max(0, int(policy.get("version_vault_settle_sec", DEFAULT_SETTLE_SEC)))
        evict_source = bool(policy.get("version_vault_evict_source", True))

        auto_sealed_count = 0
        remaining_items: List[Dict[str, Any]] = []

        if mode == "auto_after_settle":
            ready_to_seal: List[Dict[str, Any]] = []
            for item in active_items:
                cat = str(item.get("category") or "")
                # Hard rule: auto_after_settle NEVER auto-evicts stale_project or explicit-confirm items
                if cat == CATEGORY_STALE_PROJECT or bool(item.get("requires_explicit_confirm", False)):
                    remaining_items.append(item)
                    continue
                first_seen = float(item.get("first_seen_at") or now_ts)
                seen_elapsed = now_ts - first_seen
                latest_mtime = _compute_latest_mtime(Path(str(item["source_path"])))
                mtime_elapsed = now_ts - latest_mtime
                if seen_elapsed >= settle_sec and mtime_elapsed >= settle_sec:
                    ready_to_seal.append(item)
                else:
                    remaining_items.append(item)

            if ready_to_seal and not self._stop_event.is_set():
                job = vault.enqueue_seal_job(
                    ready_to_seal,
                    trigger_reason="guard_auto_after_settle",
                    evict_source=evict_source,
                )
                wres = vault.run_worker_once(max_jobs=5)
                auto_sealed_count = int(wres.get("snapshots_created", 0))
                processed_jobs += int(wres.get("processed_jobs", 0))
                log(
                    f"VersionVault auto_after_settle sealed {auto_sealed_count} item(s) "
                    f"(job={job.job_id})"
                )
        else:
            remaining_items = active_items
            if added_count > 0:
                log(
                    f"VersionVault confirm mode recorded {added_count} new pending seal(s) "
                    f"(total pending={len(remaining_items)})"
                )

        if remaining_items or self._pending_seals_path(vault).exists():
            self._save_pending_seals(vault, remaining_items, now_ts=now_ts)

        return {
            "enabled": True,
            "mode": mode,
            "processed_jobs": processed_jobs,
            "triggers": len(events),
            "pending_count": len(remaining_items),
            "auto_sealed": auto_sealed_count,
        }

    # ------------------------------------------------------------------
    # 3. Desktop Launchers (`agyrun://` & `version-seal` compatibility)
    # ------------------------------------------------------------------
    def extra_launchers(
        self, py_exe: str, pyw_exe: str, pkg_root: Path | str
    ) -> Dict[str, str]:
        _ = pyw_exe
        return {
            "orion-archive-scan.ps1": ps1_cli(py_exe, pkg_root, "archive scan"),
            "orion-archive-seal.ps1": ps1_cli(py_exe, pkg_root, "archive seal"),
            "orion-archive-status.ps1": ps1_cli(py_exe, pkg_root, "archive status"),
            "orion-archive-push.ps1": ps1_cli(py_exe, pkg_root, "archive push"),
        }

    # ------------------------------------------------------------------
    # 4. Panel UI Slots (Optional 6th Tab + Tray Items)
    # ------------------------------------------------------------------
    def _tray_action_scan(self) -> VaultTrayActionResult:
        vault = self._make_vault()
        policy = self.load_full_policy(vault)
        workspaces = self._resolve_workspaces(policy)
        report = scan_old_versions(
            workspaces,
            include_stale_projects=False,
            pinned_paths=policy.get("version_vault_pinned_paths") or (),
            editable_roots=self._editable_roots_override,
        )
        total_bytes = sum(c.size_bytes for c in report.candidates)
        msg = (
            f"版本冷库扫描完成：可封存 {len(report.candidates)} 项"
            f"（{_format_bytes(total_bytes)}），受保护 {len(report.protected_items)} 项"
        )
        return VaultTrayActionResult(
            ok=True,
            message=msg,
            data={
                "candidate_count": len(report.candidates),
                "protected_count": len(report.protected_items),
                "total_bytes": total_bytes,
            },
        )

    def _tray_action_push(self) -> Any:
        vault = self._make_vault()
        return vault.push_to_remote()

    def _build_vault_tray_items(self) -> List[Any]:
        host = self._ui_host
        return [
            (
                "🧊 扫描旧版本候选 (Archive Scan)",
                lambda: invoke_host_action(
                    host,
                    "托盘 · 扫描工作区旧版本",
                    self._tray_action_scan,
                ),
            ),
            (
                "☁️ 推送版本冷库到云端 (Archive Push)",
                lambda: invoke_host_action(
                    host,
                    "托盘 · 推送版本冷库到云端",
                    self._tray_action_push,
                ),
            ),
        ]

    def register_ui(self, ui_host: Any) -> None:
        """Mount optional 6th Tab (if `version_vault_tab` exists) and Vault Tray Items."""
        self._ui_host = ui_host

        # 1) Mount Version Vault Tab when `version_vault_tab` is importable
        add_tab = getattr(ui_host, "add_tab", None)
        tab_cls = _try_import_version_vault_tab()
        if callable(add_tab) and tab_cls is not None:
            def _build_archive_tab(parent: Any, host: Any = None) -> Any:
                cls = _try_import_version_vault_tab() or tab_cls
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
                toast_fn = _build_toast_callback(ctx, app)
                raw_pal = getattr(ctx, "palette", None)
                if not isinstance(raw_pal, dict):
                    raw_pal = getattr(app, "palette", None)
                palette: Dict[str, str] = dict(raw_pal) if isinstance(raw_pal, dict) else {}
                if "BORDER_DIM" not in palette and "BORDER_SUBTLE" in palette:
                    palette["BORDER_DIM"] = palette["BORDER_SUBTLE"]

                vault_inst = self._make_vault()
                init_kwargs: Dict[str, Any] = {
                    "px": px_fn,
                    "log_callback": log_fn,
                    "ui_call": ui_call_fn,
                    "palette": palette,
                    "toast_callback": toast_fn,
                    "vault": vault_inst,
                    "copy_callback": copy_fn,
                    "workspaces_override": self._workspaces_override,
                    "editable_roots_override": self._editable_roots_override,
                }
                try:
                    sig = inspect.signature(cls)
                    has_var_kw = any(
                        param.kind == inspect.Parameter.VAR_KEYWORD
                        for param in sig.parameters.values()
                    )
                    if not has_var_kw:
                        init_kwargs = {
                            k: v for k, v in init_kwargs.items() if k in sig.parameters
                        }
                except (TypeError, ValueError):
                    pass

                tab = cls(parent, **init_kwargs)
                pack_fn = getattr(tab, "pack", None)
                if callable(pack_fn):
                    try:
                        import tkinter as tk

                        pack_fn(fill=tk.BOTH, expand=True)
                    except Exception:
                        pack_fn(fill="both", expand=True)
                self._archive_tab = tab
                return tab

            def _resolve_archive_tab(candidate: Any = None) -> Any:
                if candidate is not None and callable(getattr(candidate, "refresh_async", None)):
                    return candidate
                return self._archive_tab

            def _on_select(arg: Any = None) -> None:
                target = _resolve_archive_tab(arg)
                if target is not None and callable(getattr(target, "refresh_async", None)):
                    target.refresh_async(silent=True)

            def _on_refresh(
                report: Any = None, _context: Any = None, *_args: Any, **_kwargs: Any
            ) -> None:
                target = _resolve_archive_tab(report)
                if target is not None and callable(getattr(target, "refresh_async", None)):
                    target.refresh_async(silent=True)

            add_tab(
                key=ARCHIVE_TAB_KEY,
                title=ARCHIVE_TAB_TITLE,
                factory_fn=_build_archive_tab,
                on_select=_on_select,
                on_refresh=_on_refresh,
            )

        # 2) Register Vault Tray Items (Scan & Cloud Push)
        add_tray = getattr(ui_host, "add_tray_items", None)
        if callable(add_tray):
            add_tray(self._build_vault_tray_items)


__all__ = [
    "ARCHIVE_TAB_KEY",
    "ARCHIVE_TAB_TITLE",
    "DEFAULT_SETTLE_SEC",
    "DEFAULT_VAULT_MODE",
    "GUARD_POLL_INTERVAL_SEC",
    "VALID_VAULT_MODES",
    "VaultTrayActionResult",
    "VersionVaultPlugin",
]
