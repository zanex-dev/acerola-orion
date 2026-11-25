# -*- coding: utf-8 -*-
"""
Workspace Version Vault (acerola_orion.plugins.version_vault)

只增不删（Append-Only）外部冷库存储引擎、持久化异步队列与按需 Git 云端推送模块：
- 物理无任何 delete / purge / prune 快照删除接口，冷库快照一旦封存入库终身保留；
- 外迁强制 5 步安全流水线：
  1) 源路径与符号链接/联接点（NTFS reparse point）安全检查；
  2) 复制至 staging 暂存区并生成 MANIFEST.json（逐文件 SHA-256 + 根哈希）；
  3) 双向 SHA-256 核验（源端 <-> 冷库端全量复算比对），任意失败立即熔断保留源；
  4) 原子迁入 repo/snapshots/、写入 SQLite 索引、更新 LARGE_ASSETS_LOCAL_ONLY.json / .gitignore / repo/INDEX.md；
  5) 仅在前 4 步全绿提交后，才按 evict_source 决定是否移出源路径。
- 持久化异步队列（queue/*.json + SQLite archive_jobs）与分离后台 worker；
- 按需 Git 推送（自动忽略 node_modules / __pycache__ / .venv 及 >50MB 单文件、无变更跳过、禁用 force push）。
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

from acerola_orion.constants import (
    atomic_write_text,
    get_data_dir,
    interprocess_file_lock,
    load_app_config,
    update_app_config,
)

__all__ = [
    "DEFAULT_LARGE_FILE_BYTES",
    "DEFAULT_VERSION_VAULT_POLICY",
    "VaultError",
    "VaultSecurityError",
    "VaultVerificationError",
    "VaultSnapshotNotFoundError",
    "VaultSnapshotRecord",
    "ArchiveJobRecord",
    "VaultPushResult",
    "VersionVaultStatus",
    "get_version_vault_dir",
    "WorkspaceVersionVault",
]

DEFAULT_LARGE_FILE_MB = 50
DEFAULT_LARGE_FILE_BYTES = DEFAULT_LARGE_FILE_MB * 1024 * 1024
_HASH_CHUNK_BYTES = 1024 * 1024

_BASE_GITIGNORE_LINES = (
    "# Managed by Acerola-Orion Workspace Version Vault",
    "node_modules/",
    "**/node_modules/",
    "__pycache__/",
    "**/__pycache__/",
    ".venv/",
    "**/.venv/",
    ".DS_Store",
    "Thumbs.db",
)

_LARGE_ASSETS_MARKER_BEGIN = "# BEGIN LARGE_ASSETS_LOCAL_ONLY (auto-managed)"
_LARGE_ASSETS_MARKER_END = "# END LARGE_ASSETS_LOCAL_ONLY"

DEFAULT_VERSION_VAULT_POLICY: Dict[str, Any] = {
    "version_vault_enabled": True,
    "version_vault_remote_url": "",
    "version_vault_branch": "main",
    "version_vault_auto_push": False,
    "version_vault_evict_source": True,
    "version_vault_large_file_mb": DEFAULT_LARGE_FILE_MB,
    "version_vault_keep_recent_versions": 2,
    "version_vault_min_age_hours": 24,
    "version_vault_scan_roots": [],
    "version_vault_exclude_globs": ["node_modules", "__pycache__", ".venv", ".git"],
}

_VERSION_CONTAINER_NAMES = frozenset(
    {
        "_versions",
        "versions",
        ".versions",
        "archive",
        "archives",
        "backup",
        "backups",
        "old",
        "drafts",
    }
)

_VISUAL_EXTS = frozenset(
    {
        ".svg",
        ".png",
        ".jpg",
        ".jpeg",
        ".webp",
        ".gif",
        ".bmp",
        ".ico",
        ".mp4",
        ".webm",
        ".mov",
    }
)


class VaultError(RuntimeError):
    """Base error for Workspace Version Vault operations."""


class VaultSecurityError(VaultError):
    """Raised when a source/target path violates symlink, junction, or vault boundary safety."""


class VaultVerificationError(VaultError):
    """Raised when bidirectional SHA-256 verification fails."""


class VaultSnapshotNotFoundError(VaultError, FileNotFoundError):
    """Raised when a requested snapshot ID does not exist."""


@dataclass
class VaultSnapshotRecord:
    snapshot_id: str
    project: str
    category: str
    source_path: str
    source_kind: str
    source_name: str
    vault_relpath: str
    file_count: int
    total_bytes: int
    root_sha256: str
    trigger_reason: str
    source_evicted: bool
    large_file_count: int = 0
    created_at: str = ""
    status: str = "sealed"
    manifest_path: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)


@dataclass
class ArchiveJobRecord:
    job_id: str
    status: str
    trigger_reason: str
    evict_source: bool
    items: List[Dict[str, Any]] = field(default_factory=list)
    total_items: int = 0
    completed_items: int = 0
    failed_items: int = 0
    snapshot_ids: List[str] = field(default_factory=list)
    errors: List[Dict[str, Any]] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""
    started_at: str = ""
    finished_at: str = ""
    worker_pid: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)


@dataclass
class VaultPushResult:
    ok: bool
    skipped: bool
    dry_run: bool
    remote_url: str
    branch: str
    commit_sha: str = ""
    changed_files: int = 0
    large_files_isolated: int = 0
    message: str = ""
    pushed_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)


@dataclass
class VersionVaultStatus:
    vault_dir: str
    repo_dir: str
    db_path: str
    snapshot_count: int
    total_files: int
    total_bytes: int
    large_asset_count: int
    queued_jobs: int
    running_jobs: int
    failed_jobs: int
    succeeded_jobs: int
    git_initialized: bool
    remote_url: str
    branch: str
    last_snapshot_at: str
    last_push_at: str
    last_push_status: str
    policy: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)


def get_version_vault_dir() -> Path:
    """Return `~/.gemini/acerola_orion/version_vault` (respecting ACEROLA_ORION_HOME)."""
    root = get_data_dir() / "version_vault"
    for sub in (root, root / "queue", root / "repo", root / "logs"):
        sub.mkdir(parents=True, exist_ok=True)
    return root


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _slugify(value: str, default: str = "default", max_len: int = 64) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", (value or "").strip()).strip("._-")
    if not cleaned or cleaned in (".", ".."):
        return default
    return cleaned[:max_len]


def _is_reparse_or_link(p: Path) -> bool:
    """Return True if `p` is a symlink, Windows junction, or any NTFS reparse point."""
    try:
        lst = os.lstat(p)
    except FileNotFoundError:
        return False
    except OSError:
        return True
    try:
        if p.is_symlink():
            return True
    except OSError:
        return True
    is_junc = getattr(os.path, "isjunction", None)
    if callable(is_junc):
        try:
            if is_junc(str(p)):
                return True
        except OSError:
            return True
    attrs = getattr(lst, "st_file_attributes", 0)
    if attrs & 0x0400:  # FILE_ATTRIBUTE_REPARSE_POINT
        return True
    return False


def _sha256_file(path: Path) -> Tuple[str, int]:
    """Compute SHA-256 hex digest and exact byte size of `path`."""
    h = hashlib.sha256()
    total = 0
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(_HASH_CHUNK_BYTES)
            if not chunk:
                break
            h.update(chunk)
            total += len(chunk)
    return h.hexdigest(), total


def _compute_root_sha256(
    files: Sequence[Dict[str, Any]],
    empty_dirs: Sequence[str],
) -> str:
    h = hashlib.sha256()
    for entry in sorted(files, key=lambda x: str(x["relpath"])):
        line = f"F\t{entry['relpath']}\t{int(entry['size'])}\t{entry['sha256']}\n"
        h.update(line.encode("utf-8"))
    for d in sorted(set(empty_dirs)):
        line = f"D\t{d}\n"
        h.update(line.encode("utf-8"))
    return h.hexdigest()


def _is_subpath(child: Path, parent: Path) -> bool:
    try:
        child.resolve(strict=False).relative_to(parent.resolve(strict=False))
        return True
    except ValueError:
        return False


def _infer_project_and_category(source_path: Path) -> Tuple[str, str]:
    parent = source_path.parent
    project = "default"
    parts = list(parent.parts)
    for part in reversed(parts):
        low = part.lower()
        if not part or part in ("\\", "/") or part.endswith(":"):
            continue
        if low in _VERSION_CONTAINER_NAMES:
            continue
        project = _slugify(part, default="default")
        break

    category = "workspace_archive"
    low_parts = [p.lower() for p in source_path.parts]
    if any(p in _VERSION_CONTAINER_NAMES for p in low_parts):
        category = "versions"
    elif source_path.suffix.lower() in _VISUAL_EXTS:
        category = "visual_versions"
    return project, category


def _clear_readonly_and_remove(func: Any, path: str, _exc_info: Any) -> None:
    try:
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
    except OSError:
        pass
    func(path)


def _remove_source_path(source_path: Path) -> None:
    """Remove an already-verified and committed source file/dir (never following symlinks)."""
    if _is_reparse_or_link(source_path):
        raise VaultSecurityError(f"拒绝移除符号链接或联接点源路径：{source_path}")
    if source_path.is_dir():
        if sys.version_info >= (3, 12):
            shutil.rmtree(source_path, onexc=_clear_readonly_and_remove)
        else:
            shutil.rmtree(source_path, onerror=_clear_readonly_and_remove)
    elif source_path.exists():
        try:
            os.chmod(source_path, stat.S_IWRITE | stat.S_IREAD)
        except OSError:
            pass
        source_path.unlink()


def _subprocess_creationflags() -> int:
    if os.name == "nt":
        return int(getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))
    return 0


def _sanitize_remote_url(url: str) -> str:
    """Mask embedded credentials if any appear in a remote URL before logging/returning."""
    return re.sub(r"(https?://)([^/@:\s]+):([^/@\s]+)@", r"\1\2:***@", url)


def _validate_branch_name(branch: str) -> str:
    b = (branch or "main").strip()
    if not b or b.startswith("-") or not re.fullmatch(r"[A-Za-z0-9._/-]{1,120}", b) or ".." in b:
        raise VaultError(f"非法的 Git 分支名称：{branch!r}")
    return b


def _validate_remote_url(remote_url: str) -> str:
    url = (remote_url or "").strip()
    if url.startswith("-"):
        raise VaultSecurityError(f"非法的远端仓库地址（禁止以 '-' 开头）：{remote_url!r}")
    return url


def _format_gitignore_path(rel_posix: str) -> str:
    clean = rel_posix.lstrip("/")
    if not clean:
        return ""
    return f"/{clean}"


class WorkspaceVersionVault:
    """
    Append-only workspace version vault engine.
    By design, exposes NO delete/purge/prune API for committed snapshots.
    """

    def __init__(
        self,
        vault_dir: Optional[Path | str] = None,
        *,
        config_path: Optional[Path | str] = None,
        large_file_threshold_bytes: Optional[int] = None,
    ) -> None:
        self.vault_dir = Path(vault_dir) if vault_dir is not None else get_version_vault_dir()
        self.config_path: Optional[Path] = Path(config_path) if config_path is not None else None
        self._explicit_large_threshold = large_file_threshold_bytes

        self.db_path = self.vault_dir / "version_vault.db"
        self.queue_dir = self.vault_dir / "queue"
        self.repo_dir = self.vault_dir / "repo"
        self.snapshots_dir = self.repo_dir / "snapshots"
        self.logs_dir = self.vault_dir / "logs"
        self.staging_dir = self.vault_dir / ".staging"
        self.index_md_path = self.repo_dir / "INDEX.md"
        self.large_assets_path = self.repo_dir / "LARGE_ASSETS_LOCAL_ONLY.json"
        self.gitignore_path = self.repo_dir / ".gitignore"

        self._ensure_layout()
        self._init_db()

    def _ensure_layout(self) -> None:
        for d in (
            self.vault_dir,
            self.queue_dir,
            self.repo_dir,
            self.snapshots_dir,
            self.logs_dir,
            self.staging_dir,
        ):
            d.mkdir(parents=True, exist_ok=True)
        if not self.gitignore_path.exists():
            self._sync_gitignore_and_large_assets_locked(new_entries=[])

    @property
    def large_file_threshold_bytes(self) -> int:
        if self._explicit_large_threshold is not None:
            return max(1, int(self._explicit_large_threshold))
        policy = self.load_policy()
        mb = int(policy.get("version_vault_large_file_mb", DEFAULT_LARGE_FILE_MB))
        return max(1, mb) * 1024 * 1024

    @contextlib.contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.db_path), timeout=15.0)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=5000")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_db(self) -> None:
        with interprocess_file_lock(self.db_path):
            with self._connect() as conn:
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS snapshots (
                        snapshot_id TEXT PRIMARY KEY,
                        project TEXT NOT NULL,
                        category TEXT NOT NULL,
                        source_path TEXT NOT NULL,
                        source_kind TEXT NOT NULL,
                        source_name TEXT NOT NULL,
                        vault_relpath TEXT NOT NULL,
                        file_count INTEGER NOT NULL,
                        total_bytes INTEGER NOT NULL,
                        root_sha256 TEXT NOT NULL,
                        trigger_reason TEXT NOT NULL DEFAULT '',
                        source_evicted INTEGER NOT NULL DEFAULT 0,
                        large_file_count INTEGER NOT NULL DEFAULT 0,
                        created_at TEXT NOT NULL,
                        status TEXT NOT NULL DEFAULT 'sealed'
                    );
                    CREATE INDEX IF NOT EXISTS idx_snapshots_created
                        ON snapshots(created_at DESC);
                    CREATE INDEX IF NOT EXISTS idx_snapshots_proj_cat
                        ON snapshots(project, category);

                    CREATE TABLE IF NOT EXISTS archive_jobs (
                        job_id TEXT PRIMARY KEY,
                        status TEXT NOT NULL,
                        trigger_reason TEXT NOT NULL DEFAULT '',
                        evict_source INTEGER NOT NULL DEFAULT 1,
                        items_json TEXT NOT NULL,
                        total_items INTEGER NOT NULL DEFAULT 0,
                        completed_items INTEGER NOT NULL DEFAULT 0,
                        failed_items INTEGER NOT NULL DEFAULT 0,
                        snapshot_ids_json TEXT NOT NULL DEFAULT '[]',
                        errors_json TEXT NOT NULL DEFAULT '[]',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        started_at TEXT NOT NULL DEFAULT '',
                        finished_at TEXT NOT NULL DEFAULT '',
                        worker_pid INTEGER NOT NULL DEFAULT 0
                    );
                    CREATE INDEX IF NOT EXISTS idx_jobs_status_created
                        ON archive_jobs(status, created_at ASC);

                    CREATE TABLE IF NOT EXISTS vault_meta (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    """
                )

    def _append_log(self, level: str, message: str) -> None:
        log_file = self.logs_dir / "vault.log"
        line = f"[{_utc_now_iso()}] [{level}] {message}\n"
        try:
            with open(log_file, "a", encoding="utf-8", newline="\n") as fh:
                fh.write(line)
        except OSError:
            pass

    def _set_meta(self, conn: sqlite3.Connection, key: str, value: str) -> None:
        conn.execute(
            """
            INSERT INTO vault_meta(key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
            """,
            (key, value, _utc_now_iso()),
        )

    def _get_meta(self, conn: sqlite3.Connection, key: str, default: str = "") -> str:
        row = conn.execute("SELECT value FROM vault_meta WHERE key = ?", (key,)).fetchone()
        return str(row["value"]) if row is not None else default

    # ------------------------------------------------------------------
    # Policy (config.json `version_vault_*` keys)
    # ------------------------------------------------------------------

    def load_policy(self) -> Dict[str, Any]:
        cfg = load_app_config(self.config_path)
        policy: Dict[str, Any] = {}
        for k, default in DEFAULT_VERSION_VAULT_POLICY.items():
            raw = cfg.get(k, default)
            if isinstance(default, bool):
                policy[k] = bool(raw)
            elif isinstance(default, int):
                try:
                    val = int(raw)
                except (TypeError, ValueError):
                    val = default
                if k == "version_vault_large_file_mb":
                    val = max(1, val)
                elif k in ("version_vault_keep_recent_versions", "version_vault_min_age_hours"):
                    val = max(0, val)
                policy[k] = val
            elif isinstance(default, list):
                if isinstance(raw, list):
                    policy[k] = [str(x) for x in raw if str(x).strip()]
                else:
                    policy[k] = list(default)
            else:
                policy[k] = str(raw) if raw is not None else default
        return policy

    def update_policy(self, **kwargs: Any) -> Dict[str, Any]:
        normalized_updates: Dict[str, Any] = {}
        for raw_key, raw_val in kwargs.items():
            key = raw_key if raw_key.startswith("version_vault_") else f"version_vault_{raw_key}"
            if key not in DEFAULT_VERSION_VAULT_POLICY:
                raise VaultError(f"不支持的冷库策略配置项：{raw_key!r}")
            default = DEFAULT_VERSION_VAULT_POLICY[key]
            if isinstance(default, bool):
                normalized_updates[key] = bool(raw_val)
            elif isinstance(default, int):
                val = int(raw_val)
                if key == "version_vault_large_file_mb":
                    val = max(1, val)
                elif key in ("version_vault_keep_recent_versions", "version_vault_min_age_hours"):
                    val = max(0, val)
                normalized_updates[key] = val
            elif isinstance(default, list):
                if not isinstance(raw_val, (list, tuple)):
                    raise VaultError(f"策略项 {key} 必须为列表")
                normalized_updates[key] = [str(x) for x in raw_val if str(x).strip()]
            else:
                text = str(raw_val or "").strip()
                if key == "version_vault_remote_url" and text:
                    _validate_remote_url(text)
                if key == "version_vault_branch" and text:
                    text = _validate_branch_name(text)
                normalized_updates[key] = text

        def _mutate(cfg: Dict[str, Any]) -> Dict[str, Any]:
            cfg.update(normalized_updates)
            return cfg

        update_app_config(_mutate, path=self.config_path)
        return self.load_policy()

    # ------------------------------------------------------------------
    # Normalization & Security Inspection
    # ------------------------------------------------------------------

    def _normalize_items(
        self,
        items: Sequence[Any],
        *,
        default_reason: str = "",
    ) -> List[Dict[str, Any]]:
        if not items:
            raise VaultError("待封存条目列表不能为空")
        normalized: List[Dict[str, Any]] = []
        for entry in items:
            if isinstance(entry, (str, os.PathLike)):
                raw_path = str(entry)
                project = ""
                category = ""
                reason = default_reason
            elif isinstance(entry, dict):
                raw_path = str(entry.get("source_path") or entry.get("path") or "").strip()
                project = str(entry.get("project") or "").strip()
                category = str(entry.get("category") or "").strip()
                reason = str(entry.get("trigger_reason") or entry.get("reason") or default_reason).strip()
            else:
                raise VaultError(f"不支持的待封存条目类型：{type(entry).__name__}")
            if not raw_path:
                raise VaultError("待封存条目缺少有效路径")
            src = Path(raw_path)
            inferred_proj, inferred_cat = _infer_project_and_category(src)
            normalized.append(
                {
                    "source_path": os.path.abspath(str(src)),
                    "project": _slugify(project or inferred_proj, default="default"),
                    "category": _slugify(category or inferred_cat, default="workspace_archive"),
                    "trigger_reason": reason or default_reason,
                }
            )
        return normalized

    def _inspect_source_safety(self, source_path: Path) -> Tuple[str, List[Tuple[Path, str]], List[str]]:
        """
        Validate source path existence, vault boundary isolation, and absence of unsafe
        symlinks / NTFS junctions / reparse points.
        Returns `(source_kind, [(abs_file_path, rel_posix_path), ...], [empty_dir_rel_posix, ...])`.
        """
        if not source_path.exists() and not _is_reparse_or_link(source_path):
            raise FileNotFoundError(f"源路径不存在：{source_path}")

        if _is_reparse_or_link(source_path):
            raise VaultSecurityError(f"源路径本身为符号链接/联接点/重解析点，已熔断并保留源：{source_path}")

        vault_resolved = self.vault_dir.resolve(strict=False)
        src_resolved = source_path.resolve(strict=False)
        if _is_subpath(src_resolved, vault_resolved) or _is_subpath(vault_resolved, src_resolved):
            raise VaultSecurityError(f"源路径与冷库目录重叠，已拒绝归档：{source_path}")

        if source_path.is_file():
            return "file", [(source_path, source_path.name)], []

        if not source_path.is_dir():
            raise VaultSecurityError(f"不支持的源路径类型（非常规文件或目录）：{source_path}")

        files: List[Tuple[Path, str]] = []
        empty_dirs: List[str] = []

        for dirpath, dirnames, filenames in os.walk(source_path, topdown=True, followlinks=False):
            cur_dir = Path(dirpath)
            if _is_reparse_or_link(cur_dir):
                raise VaultSecurityError(f"源目录包含符号链接/联接点，已熔断并保留源：{cur_dir}")

            # Check child directories before descending
            for dname in list(dirnames):
                dpath = cur_dir / dname
                if _is_reparse_or_link(dpath):
                    raise VaultSecurityError(f"源目录包含逃逸/不安全目录链接，已熔断并保留源：{dpath}")

            for fname in filenames:
                fpath = cur_dir / fname
                if _is_reparse_or_link(fpath):
                    raise VaultSecurityError(f"源目录包含符号链接/联接点文件，已熔断并保留源：{fpath}")
                if not fpath.is_file():
                    raise VaultSecurityError(f"源目录包含非常规文件对象，已熔断并保留源：{fpath}")
                rel_posix = fpath.relative_to(source_path).as_posix()
                files.append((fpath, rel_posix))

            if not dirnames and not filenames and cur_dir != source_path:
                empty_dirs.append(cur_dir.relative_to(source_path).as_posix())

        files.sort(key=lambda item: item[1])
        empty_dirs.sort()
        return "dir", files, empty_dirs

    # ------------------------------------------------------------------
    # Bidirectional SHA-256 Verification & Seal Core
    # ------------------------------------------------------------------

    def _verify_bidirectional_integrity(
        self,
        source_path: Path,
        snapshot_stage_dir: Path,
        manifest: Dict[str, Any],
    ) -> None:
        """
        Strict bidirectional SHA-256 verification:
        1) Forward (vault payload <-> MANIFEST.json): re-hash every file in vault payload,
           check exact file set, sizes, per-file SHA-256, and root_sha256.
        2) Reverse (source <-> MANIFEST.json): re-hash every file in source, confirm
           source did not mutate mid-copy and matches vault payload bit-for-bit.
        """
        manifest_file = snapshot_stage_dir / "MANIFEST.json"
        if not manifest_file.is_file():
            raise VaultVerificationError(f"缺少 MANIFEST.json：{manifest_file}")
        disk_manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
        if disk_manifest.get("root_sha256") != manifest.get("root_sha256"):
            raise VaultVerificationError("落盘 MANIFEST.json 根哈希不一致")

        payload_dir = snapshot_stage_dir / "payload"
        if not payload_dir.is_dir():
            raise VaultVerificationError(f"缺少冷库 payload 目录：{payload_dir}")

        expected_entries = {str(e["relpath"]): e for e in disk_manifest.get("files", [])}
        expected_empty_dirs = sorted(str(d) for d in disk_manifest.get("empty_dirs", []))

        # 1) Enumerate and hash vault payload
        actual_payload_files: Dict[str, Path] = {}
        actual_empty_dirs: List[str] = []
        for dirpath, dirnames, filenames in os.walk(payload_dir, topdown=True, followlinks=False):
            cur = Path(dirpath)
            for dname in dirnames:
                if _is_reparse_or_link(cur / dname):
                    raise VaultVerificationError(f"冷库暂存区出现非法链接目录：{cur / dname}")
            for fname in filenames:
                fpath = cur / fname
                if _is_reparse_or_link(fpath):
                    raise VaultVerificationError(f"冷库暂存区出现非法链接文件：{fpath}")
                rel = fpath.relative_to(payload_dir).as_posix()
                actual_payload_files[rel] = fpath
            if not dirnames and not filenames and cur != payload_dir:
                actual_empty_dirs.append(cur.relative_to(payload_dir).as_posix())

        if set(actual_payload_files.keys()) != set(expected_entries.keys()):
            raise VaultVerificationError(
                f"冷库文件清单不匹配：expected={len(expected_entries)}, actual={len(actual_payload_files)}"
            )
        if sorted(actual_empty_dirs) != expected_empty_dirs:
            raise VaultVerificationError("冷库空目录结构与 MANIFEST 不匹配")

        verified_entries: List[Dict[str, Any]] = []
        for rel, expected in sorted(expected_entries.items()):
            vault_file = actual_payload_files[rel]
            v_sha, v_size = _sha256_file(vault_file)
            if v_size != int(expected["size"]) or v_sha != str(expected["sha256"]):
                raise VaultVerificationError(
                    f"冷库端 SHA-256 核验失败：{rel} (expected={expected['sha256']}, actual={v_sha})"
                )
            verified_entries.append({"relpath": rel, "size": v_size, "sha256": v_sha})

        recomputed_root = _compute_root_sha256(verified_entries, actual_empty_dirs)
        if recomputed_root != str(disk_manifest.get("root_sha256")):
            raise VaultVerificationError("冷库端 root_sha256 复算不一致")

        # 2) Reverse verification against source
        source_kind, src_files, src_empty_dirs = self._inspect_source_safety(source_path)
        if source_kind != str(disk_manifest.get("source_kind")):
            raise VaultVerificationError("源路径类型在封存期间发生变化")
        src_map = {rel: fpath for fpath, rel in src_files}
        if set(src_map.keys()) != set(expected_entries.keys()):
            raise VaultVerificationError("源端文件集合在封存期间发生变化，双向校验失败")
        if sorted(src_empty_dirs) != expected_empty_dirs:
            raise VaultVerificationError("源端空目录结构在封存期间发生变化，双向校验失败")

        for rel, expected in sorted(expected_entries.items()):
            s_sha, s_size = _sha256_file(src_map[rel])
            if s_size != int(expected["size"]) or s_sha != str(expected["sha256"]):
                raise VaultVerificationError(
                    f"源端反向 SHA-256 核验失败：{rel} (expected={expected['sha256']}, source={s_sha})"
                )

    def _load_large_assets_registry(self) -> List[Dict[str, Any]]:
        if not self.large_assets_path.is_file():
            return []
        try:
            data = json.loads(self.large_assets_path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("files"), list):
                return [dict(x) for x in data["files"] if isinstance(x, dict)]
        except (OSError, json.JSONDecodeError):
            pass
        return []

    def _sync_gitignore_and_large_assets_locked(
        self,
        new_entries: Sequence[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        existing = self._load_large_assets_registry()
        by_rel: Dict[str, Dict[str, Any]] = {}
        for item in existing:
            rel = str(item.get("repo_relpath") or "").strip()
            if rel:
                by_rel[rel] = item
        for item in new_entries:
            rel = str(item.get("repo_relpath") or "").strip()
            if not rel:
                continue
            prev = by_rel.get(rel)
            if prev is not None:
                merged_item = dict(prev)
                for k, v in item.items():
                    if k == "recorded_at" and prev.get("recorded_at"):
                        continue
                    if v not in ("", None) or k not in merged_item:
                        merged_item[k] = v
                by_rel[rel] = merged_item
            else:
                by_rel[rel] = dict(item)

        merged = [by_rel[k] for k in sorted(by_rel.keys())]
        existing_sorted = sorted(
            (x for x in existing if str(x.get("repo_relpath") or "").strip()),
            key=lambda x: str(x["repo_relpath"]),
        )
        need_write_registry = (not self.large_assets_path.is_file()) or (merged != existing_sorted)
        if need_write_registry:
            registry_doc = {
                "schema_version": 1,
                "threshold_bytes": self.large_file_threshold_bytes,
                "updated_at": _utc_now_iso(),
                "count": len(merged),
                "files": merged,
            }
            atomic_write_text(
                self.large_assets_path,
                json.dumps(registry_doc, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

        # Preserve any user custom lines outside the managed large-assets section
        preserved_custom: List[str] = []
        existing_gitignore_text = ""
        if self.gitignore_path.is_file():
            existing_gitignore_text = self.gitignore_path.read_text(encoding="utf-8")
            raw_lines = existing_gitignore_text.splitlines()
            in_large_block = False
            base_set = set(_BASE_GITIGNORE_LINES)
            for line in raw_lines:
                stripped = line.strip()
                if stripped == _LARGE_ASSETS_MARKER_BEGIN:
                    in_large_block = True
                    continue
                if stripped == _LARGE_ASSETS_MARKER_END:
                    in_large_block = False
                    continue
                if in_large_block:
                    continue
                if stripped and stripped not in base_set:
                    preserved_custom.append(line)

        out_lines: List[str] = list(_BASE_GITIGNORE_LINES)
        if preserved_custom:
            out_lines.append("")
            out_lines.extend(preserved_custom)
        out_lines.append("")
        out_lines.append(_LARGE_ASSETS_MARKER_BEGIN)
        for item in merged:
            rule = _format_gitignore_path(str(item["repo_relpath"]))
            if rule:
                out_lines.append(rule)
        out_lines.append(_LARGE_ASSETS_MARKER_END)
        new_gitignore_text = "\n".join(out_lines) + "\n"
        if (not self.gitignore_path.is_file()) or new_gitignore_text != existing_gitignore_text:
            atomic_write_text(self.gitignore_path, new_gitignore_text, encoding="utf-8")
        return merged

    def _rebuild_index_md_locked(self, conn: sqlite3.Connection) -> None:
        rows = conn.execute(
            """
            SELECT snapshot_id, created_at, project, category, source_kind, source_path,
                   file_count, total_bytes, root_sha256, source_evicted, large_file_count,
                   trigger_reason, vault_relpath
            FROM snapshots
            ORDER BY created_at DESC, snapshot_id DESC
            """
        ).fetchall()
        lines = [
            "# Workspace Version Vault Index (Append-Only)",
            "",
            f"- Updated At: `{_utc_now_iso()}`",
            f"- Total Snapshots: `{len(rows)}`",
            "",
            "| Snapshot ID | Created At | Project | Category | Kind | Files | Bytes | Large | Evicted | Root SHA-256 | Source | Reason |",
            "|---|---|---|---|---|---:|---:|---:|---|---|---|---|",
        ]
        for r in rows:
            src_esc = str(r["source_path"]).replace("|", "\\|")
            reason_esc = str(r["trigger_reason"]).replace("|", "\\|")
            evicted = "yes" if int(r["source_evicted"]) else "no"
            lines.append(
                f"| `{r['snapshot_id']}` | `{r['created_at']}` | `{r['project']}` | "
                f"`{r['category']}` | `{r['source_kind']}` | {int(r['file_count'])} | "
                f"{int(r['total_bytes'])} | {int(r['large_file_count'])} | {evicted} | "
                f"`{str(r['root_sha256'])[:12]}` | `{src_esc}` | {reason_esc} |"
            )
        atomic_write_text(self.index_md_path, "\n".join(lines) + "\n", encoding="utf-8")

    def _seal_single_item(
        self,
        item: Dict[str, Any],
        *,
        evict_source: bool,
    ) -> VaultSnapshotRecord:
        source_path = Path(item["source_path"])
        project = _slugify(str(item.get("project") or "default"))
        category = _slugify(str(item.get("category") or "workspace_archive"))
        trigger_reason = str(item.get("trigger_reason") or "").strip()

        # Step 1: Security & boundary inspection (before touching anything)
        source_kind, src_files, empty_dirs = self._inspect_source_safety(source_path)

        created_at = _utc_now_iso()
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        snapshot_id = f"snap_{stamp}_{uuid.uuid4().hex[:8]}"
        vault_relpath = f"snapshots/{project}/{category}/{snapshot_id}"
        final_snapshot_dir = self.repo_dir / "snapshots" / project / category / snapshot_id
        stage_dir = self.staging_dir / f"{snapshot_id}_{uuid.uuid4().hex[:6]}"
        payload_dir = stage_dir / "payload"

        threshold = self.large_file_threshold_bytes
        manifest_files: List[Dict[str, Any]] = []
        large_entries: List[Dict[str, Any]] = []
        total_bytes = 0

        try:
            payload_dir.mkdir(parents=True, exist_ok=True)
            for rel_dir in empty_dirs:
                (payload_dir / Path(rel_dir)).mkdir(parents=True, exist_ok=True)

            # Step 2: Copy source into staging payload & build MANIFEST.json
            for abs_src, rel_posix in src_files:
                dst_file = payload_dir / Path(rel_posix)
                dst_file.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(abs_src, dst_file)
                sha, size = _sha256_file(dst_file)
                st = os.lstat(abs_src)
                is_large = size > threshold
                manifest_files.append(
                    {
                        "relpath": rel_posix,
                        "size": size,
                        "sha256": sha,
                        "mtime": float(st.st_mtime),
                        "large_local_only": is_large,
                    }
                )
                total_bytes += size
                if is_large:
                    large_entries.append(
                        {
                            "snapshot_id": snapshot_id,
                            "project": project,
                            "category": category,
                            "repo_relpath": f"{vault_relpath}/payload/{rel_posix}",
                            "source_path": str(abs_src),
                            "size": size,
                            "sha256": sha,
                            "recorded_at": created_at,
                        }
                    )

            root_sha256 = _compute_root_sha256(manifest_files, empty_dirs)
            manifest = {
                "schema_version": 1,
                "snapshot_id": snapshot_id,
                "created_at": created_at,
                "project": project,
                "category": category,
                "source_path": str(source_path),
                "source_kind": source_kind,
                "source_name": source_path.name,
                "vault_relpath": vault_relpath,
                "trigger_reason": trigger_reason,
                "evict_source_requested": bool(evict_source),
                "file_count": len(manifest_files),
                "total_bytes": total_bytes,
                "large_file_count": len(large_entries),
                "root_sha256": root_sha256,
                "empty_dirs": empty_dirs,
                "files": manifest_files,
            }
            atomic_write_text(
                stage_dir / "MANIFEST.json",
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

            # Step 3: Bidirectional SHA-256 verification (before committing to repo/snapshots/)
            self._verify_bidirectional_integrity(source_path, stage_dir, manifest)

            # Step 4: Commit atomically into append-only repo/snapshots/ + SQLite + INDEX.md
            with interprocess_file_lock(self.db_path):
                final_snapshot_dir.parent.mkdir(parents=True, exist_ok=True)
                if final_snapshot_dir.exists():
                    raise VaultError(f"快照目录冲突（只增不删，拒绝覆盖）：{final_snapshot_dir}")
                shutil.move(str(stage_dir), str(final_snapshot_dir))

                if large_entries or not self.gitignore_path.exists():
                    self._sync_gitignore_and_large_assets_locked(large_entries)

                with self._connect() as conn:
                    conn.execute(
                        """
                        INSERT INTO snapshots(
                            snapshot_id, project, category, source_path, source_kind,
                            source_name, vault_relpath, file_count, total_bytes,
                            root_sha256, trigger_reason, source_evicted, large_file_count,
                            created_at, status
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            snapshot_id,
                            project,
                            category,
                            str(source_path),
                            source_kind,
                            source_path.name,
                            vault_relpath,
                            len(manifest_files),
                            total_bytes,
                            root_sha256,
                            trigger_reason,
                            0,
                            len(large_entries),
                            created_at,
                            "sealed",
                        ),
                    )
                    self._set_meta(conn, "last_snapshot_at", created_at)
                    self._rebuild_index_md_locked(conn)
        except BaseException:
            # Clean up uncommitted staging temp dir ONLY (never touches committed snapshots or source)
            if stage_dir.exists():
                shutil.rmtree(stage_dir, ignore_errors=True)
            raise

        # Step 5: Only AFTER commit & index persistence, evict source if requested
        actually_evicted = False
        if evict_source:
            _remove_source_path(source_path)
            actually_evicted = True
            with interprocess_file_lock(self.db_path):
                with self._connect() as conn:
                    conn.execute(
                        "UPDATE snapshots SET source_evicted = 1 WHERE snapshot_id = ?",
                        (snapshot_id,),
                    )
                    self._rebuild_index_md_locked(conn)

        self._append_log(
            "INFO",
            f"sealed snapshot={snapshot_id} project={project} category={category} "
            f"files={len(manifest_files)} bytes={total_bytes} evicted={actually_evicted} "
            f"source={source_path}",
        )
        return VaultSnapshotRecord(
            snapshot_id=snapshot_id,
            project=project,
            category=category,
            source_path=str(source_path),
            source_kind=source_kind,
            source_name=source_path.name,
            vault_relpath=vault_relpath,
            file_count=len(manifest_files),
            total_bytes=total_bytes,
            root_sha256=root_sha256,
            trigger_reason=trigger_reason,
            source_evicted=actually_evicted,
            large_file_count=len(large_entries),
            created_at=created_at,
            status="sealed",
            manifest_path=str(final_snapshot_dir / "MANIFEST.json"),
        )

    # ------------------------------------------------------------------
    # Synchronous & Asynchronous Queue APIs
    # ------------------------------------------------------------------

    def _job_file_path(self, job_id: str) -> Path:
        return self.queue_dir / f"{job_id}.json"

    def _persist_job_locked(self, conn: sqlite3.Connection, job: ArchiveJobRecord) -> None:
        conn.execute(
            """
            INSERT INTO archive_jobs(
                job_id, status, trigger_reason, evict_source, items_json,
                total_items, completed_items, failed_items, snapshot_ids_json,
                errors_json, created_at, updated_at, started_at, finished_at, worker_pid
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(job_id) DO UPDATE SET
                status=excluded.status,
                completed_items=excluded.completed_items,
                failed_items=excluded.failed_items,
                snapshot_ids_json=excluded.snapshot_ids_json,
                errors_json=excluded.errors_json,
                updated_at=excluded.updated_at,
                started_at=excluded.started_at,
                finished_at=excluded.finished_at,
                worker_pid=excluded.worker_pid
            """,
            (
                job.job_id,
                job.status,
                job.trigger_reason,
                1 if job.evict_source else 0,
                json.dumps(job.items, ensure_ascii=False),
                job.total_items,
                job.completed_items,
                job.failed_items,
                json.dumps(job.snapshot_ids, ensure_ascii=False),
                json.dumps(job.errors, ensure_ascii=False),
                job.created_at,
                job.updated_at,
                job.started_at,
                job.finished_at,
                job.worker_pid,
            ),
        )
        atomic_write_text(
            self._job_file_path(job.job_id),
            json.dumps(job.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def _row_to_job(self, row: sqlite3.Row) -> ArchiveJobRecord:
        return ArchiveJobRecord(
            job_id=str(row["job_id"]),
            status=str(row["status"]),
            trigger_reason=str(row["trigger_reason"]),
            evict_source=bool(int(row["evict_source"])),
            items=json.loads(str(row["items_json"])),
            total_items=int(row["total_items"]),
            completed_items=int(row["completed_items"]),
            failed_items=int(row["failed_items"]),
            snapshot_ids=json.loads(str(row["snapshot_ids_json"])),
            errors=json.loads(str(row["errors_json"])),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            started_at=str(row["started_at"]),
            finished_at=str(row["finished_at"]),
            worker_pid=int(row["worker_pid"]),
        )

    def _row_to_snapshot(self, row: sqlite3.Row) -> VaultSnapshotRecord:
        vault_relpath = str(row["vault_relpath"])
        return VaultSnapshotRecord(
            snapshot_id=str(row["snapshot_id"]),
            project=str(row["project"]),
            category=str(row["category"]),
            source_path=str(row["source_path"]),
            source_kind=str(row["source_kind"]),
            source_name=str(row["source_name"]),
            vault_relpath=vault_relpath,
            file_count=int(row["file_count"]),
            total_bytes=int(row["total_bytes"]),
            root_sha256=str(row["root_sha256"]),
            trigger_reason=str(row["trigger_reason"]),
            source_evicted=bool(int(row["source_evicted"])),
            large_file_count=int(row["large_file_count"]),
            created_at=str(row["created_at"]),
            status=str(row["status"]),
            manifest_path=str(self.repo_dir / Path(vault_relpath) / "MANIFEST.json"),
        )

    def _sync_orphan_queue_files_locked(self, conn: sqlite3.Connection) -> None:
        """Recover any queue/*.json files written before a crash that are not yet in SQLite."""
        for qfile in sorted(self.queue_dir.glob("job_*.json")):
            try:
                data = json.loads(qfile.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            job_id = str(data.get("job_id") or "").strip()
            if not job_id:
                continue
            exists = conn.execute(
                "SELECT 1 FROM archive_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            if exists is not None:
                continue
            now = _utc_now_iso()
            job = ArchiveJobRecord(
                job_id=job_id,
                status=str(data.get("status") or "queued"),
                trigger_reason=str(data.get("trigger_reason") or ""),
                evict_source=bool(data.get("evict_source", True)),
                items=list(data.get("items") or []),
                total_items=int(data.get("total_items") or len(data.get("items") or [])),
                completed_items=int(data.get("completed_items") or 0),
                failed_items=int(data.get("failed_items") or 0),
                snapshot_ids=list(data.get("snapshot_ids") or []),
                errors=list(data.get("errors") or []),
                created_at=str(data.get("created_at") or now),
                updated_at=str(data.get("updated_at") or now),
                started_at=str(data.get("started_at") or ""),
                finished_at=str(data.get("finished_at") or ""),
                worker_pid=int(data.get("worker_pid") or 0),
            )
            self._persist_job_locked(conn, job)

    def enqueue_seal_job(
        self,
        items: Sequence[Any],
        *,
        trigger_reason: str = "",
        evict_source: bool = True,
    ) -> ArchiveJobRecord:
        normalized = self._normalize_items(items, default_reason=trigger_reason)
        now = _utc_now_iso()
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        job_id = f"job_{stamp}_{uuid.uuid4().hex[:8]}"
        job = ArchiveJobRecord(
            job_id=job_id,
            status="queued",
            trigger_reason=trigger_reason,
            evict_source=bool(evict_source),
            items=normalized,
            total_items=len(normalized),
            completed_items=0,
            failed_items=0,
            snapshot_ids=[],
            errors=[],
            created_at=now,
            updated_at=now,
        )
        with interprocess_file_lock(self.db_path):
            with self._connect() as conn:
                self._persist_job_locked(conn, job)
        self._append_log("INFO", f"enqueued job={job_id} items={len(normalized)} evict={evict_source}")
        return job

    def _execute_job(self, job: ArchiveJobRecord) -> Tuple[ArchiveJobRecord, List[VaultSnapshotRecord]]:
        job.status = "running"
        job.started_at = _utc_now_iso()
        job.updated_at = job.started_at
        job.worker_pid = os.getpid()
        with interprocess_file_lock(self.db_path):
            with self._connect() as conn:
                self._persist_job_locked(conn, job)

        snapshots: List[VaultSnapshotRecord] = []
        snapshot_ids: List[str] = []
        errors: List[Dict[str, Any]] = []

        for item in job.items:
            try:
                rec = self._seal_single_item(item, evict_source=job.evict_source)
                snapshots.append(rec)
                snapshot_ids.append(rec.snapshot_id)
            except Exception as exc:
                err_entry = {
                    "source_path": str(item.get("source_path") or ""),
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                }
                errors.append(err_entry)
                self._append_log(
                    "ERROR",
                    f"job={job.job_id} seal failed source={err_entry['source_path']}: "
                    f"{err_entry['error_type']}: {err_entry['message']}",
                )

        job.completed_items = len(snapshot_ids)
        job.failed_items = len(errors)
        job.snapshot_ids = snapshot_ids
        job.errors = errors
        job.finished_at = _utc_now_iso()
        job.updated_at = job.finished_at
        if not errors:
            job.status = "succeeded"
        elif snapshot_ids:
            job.status = "partial_failed"
        else:
            job.status = "failed"

        with interprocess_file_lock(self.db_path):
            with self._connect() as conn:
                self._persist_job_locked(conn, job)

        return job, snapshots

    def seal_items_sync(
        self,
        items: Sequence[Any],
        *,
        trigger_reason: str = "",
        evict_source: bool = True,
    ) -> Dict[str, Any]:
        job = self.enqueue_seal_job(
            items,
            trigger_reason=trigger_reason,
            evict_source=evict_source,
        )
        done_job, snapshots = self._execute_job(job)
        return {
            "ok": done_job.failed_items == 0,
            "job_id": done_job.job_id,
            "status": done_job.status,
            "total_items": done_job.total_items,
            "completed_items": done_job.completed_items,
            "failed_items": done_job.failed_items,
            "snapshot_ids": list(done_job.snapshot_ids),
            "snapshots": [s.to_dict() for s in snapshots],
            "errors": list(done_job.errors),
        }

    def run_worker_once(self, max_jobs: int = 10) -> Dict[str, Any]:
        max_n = max(1, int(max_jobs))
        worker_lock = self.queue_dir / "worker.lock"
        processed: List[ArchiveJobRecord] = []
        snapshots_created = 0

        with interprocess_file_lock(worker_lock, timeout=5.0):
            with interprocess_file_lock(self.db_path):
                with self._connect() as conn:
                    self._sync_orphan_queue_files_locked(conn)
                    rows = conn.execute(
                        """
                        SELECT * FROM archive_jobs
                        WHERE status = 'queued'
                        ORDER BY created_at ASC, job_id ASC
                        LIMIT ?
                        """,
                        (max_n,),
                    ).fetchall()
                    pending_jobs = [self._row_to_job(r) for r in rows]

            for job in pending_jobs:
                done_job, snaps = self._execute_job(job)
                processed.append(done_job)
                snapshots_created += len(snaps)

        succeeded = sum(1 for j in processed if j.status == "succeeded")
        partial_failed = sum(1 for j in processed if j.status == "partial_failed")
        failed = sum(1 for j in processed if j.status == "failed")
        return {
            "processed_jobs": len(processed),
            "succeeded_jobs": succeeded,
            "partial_failed_jobs": partial_failed,
            "failed_jobs": failed,
            "snapshots_created": snapshots_created,
            "job_ids": [j.job_id for j in processed],
            "jobs": [j.to_dict() for j in processed],
        }

    def spawn_async_worker(self, *, max_jobs: int = 20) -> Dict[str, Any]:
        log_path = self.logs_dir / "worker.log"
        cmd = [
            sys.executable,
            "-m",
            "acerola_orion.plugins.version_vault",
            "--run-worker",
            "--vault-dir",
            str(self.vault_dir),
            "--max-jobs",
            str(max(1, int(max_jobs))),
        ]
        if self.config_path is not None:
            cmd.extend(["--config-path", str(self.config_path)])
        if self._explicit_large_threshold is not None:
            cmd.extend(["--large-threshold-bytes", str(self._explicit_large_threshold)])

        env = os.environ.copy()
        extra_paths: List[str] = []
        # Ensure detached child can import acerola_orion
        pkg_root = str(Path(__file__).resolve().parent.parent.parent)
        extra_paths.append(pkg_root)
        existing_pp = env.get("PYTHONPATH", "")
        if existing_pp:
            extra_paths.append(existing_pp)
        env["PYTHONPATH"] = os.pathsep.join(extra_paths)

        popen_kwargs: Dict[str, Any] = {
            "stdin": subprocess.DEVNULL,
            "env": env,
            "close_fds": True,
        }
        if os.name == "nt":
            flags = (
                int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200))
                | int(getattr(subprocess, "DETACHED_PROCESS", 0x00000008))
                | _subprocess_creationflags()
            )
            popen_kwargs["creationflags"] = flags
        else:
            popen_kwargs["start_new_session"] = True

        with open(log_path, "a", encoding="utf-8") as log_fh:
            popen_kwargs["stdout"] = log_fh
            popen_kwargs["stderr"] = log_fh
            proc = subprocess.Popen(cmd, **popen_kwargs)
        # Detached background worker is intentionally fire-and-forget; suppress CPython
        # GC ResourceWarning while leaving the OS child process running independently.
        proc._child_created = False  # type: ignore[attr-defined]

        self._append_log("INFO", f"spawned async worker pid={proc.pid} max_jobs={max_jobs}")
        return {
            "spawned": True,
            "pid": proc.pid,
            "vault_dir": str(self.vault_dir),
            "log_path": str(log_path),
        }

    # ------------------------------------------------------------------
    # Query & Status APIs
    # ------------------------------------------------------------------

    def list_snapshots(
        self,
        project: str = "",
        category: str = "",
        query: str = "",
        limit: int = 100,
    ) -> List[VaultSnapshotRecord]:
        clauses: List[str] = []
        params: List[Any] = []
        if project.strip():
            clauses.append("project = ?")
            params.append(project.strip())
        if category.strip():
            clauses.append("category = ?")
            params.append(category.strip())
        if query.strip():
            like = f"%{query.strip()}%"
            clauses.append(
                "(snapshot_id LIKE ? OR project LIKE ? OR category LIKE ? "
                "OR source_path LIKE ? OR source_name LIKE ? OR trigger_reason LIKE ?)"
            )
            params.extend([like] * 6)

        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        lim = max(1, min(int(limit), 5000))
        params.append(lim)

        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM snapshots
                {where_sql}
                ORDER BY created_at DESC, snapshot_id DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
            return [self._row_to_snapshot(r) for r in rows]

    def list_jobs(self, status: str = "", limit: int = 30) -> List[ArchiveJobRecord]:
        clauses: List[str] = []
        params: List[Any] = []
        if status.strip():
            clauses.append("status = ?")
            params.append(status.strip())
        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        lim = max(1, min(int(limit), 1000))
        params.append(lim)

        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM archive_jobs
                {where_sql}
                ORDER BY created_at DESC, job_id DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
            return [self._row_to_job(r) for r in rows]

    def get_status(self) -> VersionVaultStatus:
        policy = self.load_policy()
        large_assets = self._load_large_assets_registry()
        with self._connect() as conn:
            snap_row = conn.execute(
                """
                SELECT COUNT(*) AS cnt,
                       COALESCE(SUM(file_count), 0) AS files,
                       COALESCE(SUM(total_bytes), 0) AS bytes,
                       COALESCE(MAX(created_at), '') AS last_at
                FROM snapshots
                """
            ).fetchone()
            job_rows = conn.execute(
                "SELECT status, COUNT(*) AS cnt FROM archive_jobs GROUP BY status"
            ).fetchall()
            job_counts = {str(r["status"]): int(r["cnt"]) for r in job_rows}
            last_push_at = self._get_meta(conn, "last_push_at", "")
            last_push_status = self._get_meta(conn, "last_push_status", "")

        return VersionVaultStatus(
            vault_dir=str(self.vault_dir),
            repo_dir=str(self.repo_dir),
            db_path=str(self.db_path),
            snapshot_count=int(snap_row["cnt"]) if snap_row else 0,
            total_files=int(snap_row["files"]) if snap_row else 0,
            total_bytes=int(snap_row["bytes"]) if snap_row else 0,
            large_asset_count=len(large_assets),
            queued_jobs=job_counts.get("queued", 0),
            running_jobs=job_counts.get("running", 0),
            failed_jobs=job_counts.get("failed", 0) + job_counts.get("partial_failed", 0),
            succeeded_jobs=job_counts.get("succeeded", 0),
            git_initialized=(self.repo_dir / ".git").exists(),
            remote_url=str(policy.get("version_vault_remote_url") or ""),
            branch=str(policy.get("version_vault_branch") or "main"),
            last_snapshot_at=str(snap_row["last_at"]) if snap_row else "",
            last_push_at=last_push_at,
            last_push_status=last_push_status,
            policy=policy,
        )

    # ------------------------------------------------------------------
    # Restore API (Non-destructive to vault; verifies integrity before & after)
    # ------------------------------------------------------------------

    def restore_snapshot(
        self,
        snapshot_id: str,
        target_dir: Optional[Path | str] = None,
        force: bool = False,
    ) -> Dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM snapshots WHERE snapshot_id = ?", (snapshot_id,)
            ).fetchone()
        if row is None:
            raise VaultSnapshotNotFoundError(f"未找到冷库快照：{snapshot_id}")
        record = self._row_to_snapshot(row)

        snapshot_dir = self.repo_dir / Path(record.vault_relpath)
        manifest_path = snapshot_dir / "MANIFEST.json"
        payload_dir = snapshot_dir / "payload"
        if not manifest_path.is_file() or not payload_dir.is_dir():
            raise VaultVerificationError(f"冷库快照目录不完整：{snapshot_dir}")

        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest_files = list(manifest.get("files") or [])
        empty_dirs = list(manifest.get("empty_dirs") or [])

        # Verify vault copy integrity before restoring
        verified_meta: List[Dict[str, Any]] = []
        for entry in manifest_files:
            rel = str(entry["relpath"])
            vfile = payload_dir / Path(rel)
            if not vfile.is_file() or _is_reparse_or_link(vfile):
                raise VaultVerificationError(f"冷库文件缺失或异常：{vfile}")
            sha, size = _sha256_file(vfile)
            if sha != str(entry["sha256"]) or size != int(entry["size"]):
                raise VaultVerificationError(f"冷库文件 SHA-256 校验失败：{rel}")
            verified_meta.append({"relpath": rel, "size": size, "sha256": sha})
        if _compute_root_sha256(verified_meta, empty_dirs) != str(manifest.get("root_sha256")):
            raise VaultVerificationError(f"冷库快照根哈希损坏：{snapshot_id}")

        if target_dir is None:
            dest = Path(record.source_path)
        else:
            dest = Path(target_dir) / record.source_name

        if _is_reparse_or_link(dest):
            raise VaultSecurityError(f"拒绝还原至符号链接/联接点路径：{dest}")
        if _is_subpath(dest, self.vault_dir):
            raise VaultSecurityError(f"禁止将快照还原到冷库目录内部：{dest}")

        if dest.exists():
            if not force:
                raise FileExistsError(f"还原目标已存在（如需覆盖请显式传入 force=True）：{dest}")
            _remove_source_path(dest)

        if record.source_kind == "file":
            if len(manifest_files) != 1:
                raise VaultVerificationError(f"单文件快照清单条目异常：{snapshot_id}")
            entry = manifest_files[0]
            src_payload = payload_dir / Path(str(entry["relpath"]))
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_payload, dest)
            r_sha, r_size = _sha256_file(dest)
            if r_sha != str(entry["sha256"]) or r_size != int(entry["size"]):
                raise VaultVerificationError(f"还原后目标文件校验失败：{dest}")
        else:
            dest.mkdir(parents=True, exist_ok=True)
            for rel_dir in empty_dirs:
                (dest / Path(rel_dir)).mkdir(parents=True, exist_ok=True)
            for entry in manifest_files:
                rel = str(entry["relpath"])
                src_payload = payload_dir / Path(rel)
                dst_file = dest / Path(rel)
                dst_file.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src_payload, dst_file)
                r_sha, r_size = _sha256_file(dst_file)
                if r_sha != str(entry["sha256"]) or r_size != int(entry["size"]):
                    raise VaultVerificationError(f"还原后目标文件校验失败：{dst_file}")

        self._append_log("INFO", f"restored snapshot={snapshot_id} -> {dest} force={force}")
        return {
            "ok": True,
            "snapshot_id": snapshot_id,
            "source_kind": record.source_kind,
            "restored_path": str(dest),
            "file_count": len(manifest_files),
            "total_bytes": int(manifest.get("total_bytes") or 0),
            "root_sha256": str(manifest.get("root_sha256") or ""),
            "verified": True,
        }

    # ------------------------------------------------------------------
    # Git Repository & On-Demand Push (Append-Only, Large-Asset Isolated)
    # ------------------------------------------------------------------

    def _run_git(
        self,
        args: Sequence[str],
        *,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(self.repo_dir),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=_subprocess_creationflags(),
        )
        if check and proc.returncode != 0:
            err = _sanitize_remote_url((proc.stderr or proc.stdout or "").strip())
            raise VaultError(f"git {' '.join(args)} 执行失败 (rc={proc.returncode}): {err}")
        return proc

    def _ensure_git_repo_locked(self, branch: str) -> None:
        git_dir = self.repo_dir / ".git"
        if not git_dir.exists():
            init_res = self._run_git(["init", "-b", branch], check=False)
            if init_res.returncode != 0:
                self._run_git(["init"])
                self._run_git(["checkout", "-B", branch], check=False)

        self._run_git(["config", "core.quotepath", "false"])
        name_check = self._run_git(["config", "user.name"], check=False)
        if name_check.returncode != 0 or not name_check.stdout.strip():
            self._run_git(["config", "user.name", "Acerola-Orion Vault"])
        email_check = self._run_git(["config", "user.email"], check=False)
        if email_check.returncode != 0 or not email_check.stdout.strip():
            self._run_git(["config", "user.email", "vault@acerola-orion.local"])

    def _scan_and_isolate_large_files_locked(self) -> List[Dict[str, Any]]:
        threshold = self.large_file_threshold_bytes
        discovered: List[Dict[str, Any]] = []
        if self.snapshots_dir.is_dir():
            for dirpath, _dirnames, filenames in os.walk(self.snapshots_dir, followlinks=False):
                cur = Path(dirpath)
                rel_dir = cur.relative_to(self.repo_dir).as_posix()
                # Never treat snapshot metadata (MANIFEST.json) as large payload assets
                if "/payload" not in f"/{rel_dir}":
                    continue
                for fname in filenames:
                    fpath = cur / fname
                    if _is_reparse_or_link(fpath):
                        continue
                    try:
                        sz = os.lstat(fpath).st_size
                    except OSError:
                        continue
                    if sz > threshold:
                        rel_posix = fpath.relative_to(self.repo_dir).as_posix()
                        sha, exact_sz = _sha256_file(fpath)
                        discovered.append(
                            {
                                "repo_relpath": rel_posix,
                                "size": exact_sz,
                                "sha256": sha,
                                "recorded_at": _utc_now_iso(),
                            }
                        )
        merged = self._sync_gitignore_and_large_assets_locked(discovered)
        return merged

    def _get_head_sha(self) -> str:
        res = self._run_git(["rev-parse", "HEAD"], check=False)
        return res.stdout.strip() if res.returncode == 0 else ""

    def _get_remote_branch_sha(self, remote_url: str, branch: str) -> str:
        res = self._run_git(["ls-remote", "--heads", remote_url, f"refs/heads/{branch}"], check=False)
        if res.returncode != 0:
            return ""
        for line in res.stdout.splitlines():
            parts = line.strip().split()
            if len(parts) >= 2 and parts[1] == f"refs/heads/{branch}":
                return parts[0]
        return ""

    def push_to_remote(
        self,
        remote_url: str = "",
        branch: str = "main",
        dry_run: bool = False,
    ) -> VaultPushResult:
        policy = self.load_policy()
        resolved_url = _validate_remote_url(
            remote_url or str(policy.get("version_vault_remote_url") or "")
        )
        resolved_branch = _validate_branch_name(
            branch or str(policy.get("version_vault_branch") or "main")
        )
        safe_url = _sanitize_remote_url(resolved_url)
        now = _utc_now_iso()

        git_lock = self.vault_dir / "vault_git.lock"
        with interprocess_file_lock(git_lock, timeout=15.0):
            self._ensure_git_repo_locked(resolved_branch)
            large_merged = self._scan_and_isolate_large_files_locked()
            large_count = len(large_merged)

            status_res = self._run_git(["status", "--porcelain"])
            changed_lines = [ln for ln in status_res.stdout.splitlines() if ln.strip()]
            changed_files = len(changed_lines)
            head_before = self._get_head_sha()

            if dry_run:
                remote_sha = (
                    self._get_remote_branch_sha(resolved_url, resolved_branch)
                    if resolved_url
                    else ""
                )
                needs_push = changed_files > 0 or (bool(head_before) and head_before != remote_sha)
                return VaultPushResult(
                    ok=True,
                    skipped=not needs_push,
                    dry_run=True,
                    remote_url=safe_url,
                    branch=resolved_branch,
                    commit_sha=head_before,
                    changed_files=changed_files,
                    large_files_isolated=large_count,
                    message=(
                        f"dry-run：检测到 {changed_files} 项变更待提交/推送"
                        if needs_push
                        else "dry-run：冷库无新增变更，无需推送"
                    ),
                    pushed_at=now,
                )

            if not resolved_url:
                return VaultPushResult(
                    ok=False,
                    skipped=False,
                    dry_run=False,
                    remote_url="",
                    branch=resolved_branch,
                    commit_sha=head_before,
                    changed_files=changed_files,
                    large_files_isolated=large_count,
                    message="未配置远端 Git 仓库地址 (remote_url)",
                    pushed_at=now,
                )

            # Configure origin remote safely
            rem_check = self._run_git(["remote", "get-url", "origin"], check=False)
            if rem_check.returncode == 0:
                if rem_check.stdout.strip() != resolved_url:
                    self._run_git(["remote", "set-url", "origin", resolved_url])
            else:
                self._run_git(["remote", "add", "origin", resolved_url])

            committed_now = False
            if changed_files > 0:
                self._run_git(["add", "-A"])
                # Double-check large files are untracked if previously added
                for item in large_merged:
                    rel = str(item.get("repo_relpath") or "").strip()
                    if rel:
                        self._run_git(["rm", "--cached", "--ignore-unmatch", "--", rel], check=False)

                staged = self._run_git(["diff", "--cached", "--name-only"])
                staged_files = [ln for ln in staged.stdout.splitlines() if ln.strip()]
                if staged_files:
                    changed_files = len(staged_files)
                    msg = f"vault: archive {changed_files} file(s) at {now}"
                    self._run_git(["commit", "-m", msg])
                    committed_now = True
                else:
                    changed_files = 0

            head_after = self._get_head_sha()
            if not head_after:
                return VaultPushResult(
                    ok=True,
                    skipped=True,
                    dry_run=False,
                    remote_url=safe_url,
                    branch=resolved_branch,
                    commit_sha="",
                    changed_files=0,
                    large_files_isolated=large_count,
                    message="冷库暂无可推送快照，已跳过推送",
                    pushed_at=now,
                )

            remote_sha = self._get_remote_branch_sha(resolved_url, resolved_branch)
            if not committed_now and head_after == remote_sha:
                with interprocess_file_lock(self.db_path):
                    with self._connect() as conn:
                        self._set_meta(conn, "last_push_status", "skipped_no_changes")
                return VaultPushResult(
                    ok=True,
                    skipped=True,
                    dry_run=False,
                    remote_url=safe_url,
                    branch=resolved_branch,
                    commit_sha=head_after,
                    changed_files=0,
                    large_files_isolated=large_count,
                    message="冷库无新增变更，已跳过推送",
                    pushed_at=now,
                )

            # Never use --force (strict append-only)
            push_proc = self._run_git(
                ["push", "-u", "origin", f"HEAD:refs/heads/{resolved_branch}"],
                check=False,
            )
            if push_proc.returncode != 0:
                err_msg = _sanitize_remote_url((push_proc.stderr or push_proc.stdout or "").strip())
                with interprocess_file_lock(self.db_path):
                    with self._connect() as conn:
                        self._set_meta(conn, "last_push_status", f"failed: {err_msg[:200]}")
                self._append_log("ERROR", f"push failed remote={safe_url} branch={resolved_branch}: {err_msg}")
                return VaultPushResult(
                    ok=False,
                    skipped=False,
                    dry_run=False,
                    remote_url=safe_url,
                    branch=resolved_branch,
                    commit_sha=head_after,
                    changed_files=changed_files,
                    large_files_isolated=large_count,
                    message=f"推送远端失败：{err_msg}",
                    pushed_at=now,
                )

            with interprocess_file_lock(self.db_path):
                with self._connect() as conn:
                    self._set_meta(conn, "last_push_at", now)
                    self._set_meta(conn, "last_push_status", "ok")
            self._append_log(
                "INFO",
                f"pushed remote={safe_url} branch={resolved_branch} sha={head_after[:12]} "
                f"changed={changed_files} large_isolated={large_count}",
            )
            return VaultPushResult(
                ok=True,
                skipped=False,
                dry_run=False,
                remote_url=safe_url,
                branch=resolved_branch,
                commit_sha=head_after,
                changed_files=changed_files,
                large_files_isolated=large_count,
                message=f"推送成功（{head_after[:12]}）",
                pushed_at=now,
            )


def _cli_main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Acerola-Orion Workspace Version Vault Worker")
    parser.add_argument("--run-worker", "--worker", dest="run_worker", action="store_true", help="Process queued archive jobs once")
    parser.add_argument("--vault-dir", default="", help="Override version vault directory")
    parser.add_argument("--config-path", default="", help="Override config.json path")
    parser.add_argument("--max-jobs", type=int, default=20, help="Max queued jobs per run")
    parser.add_argument(
        "--large-threshold-bytes",
        type=int,
        default=0,
        help="Override large-file isolation threshold in bytes",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    vault = WorkspaceVersionVault(
        vault_dir=args.vault_dir or None,
        config_path=args.config_path or None,
        large_file_threshold_bytes=args.large_threshold_bytes if args.large_threshold_bytes > 0 else None,
    )
    if args.run_worker:
        summary = vault.run_worker_once(max_jobs=args.max_jobs)
        print(json.dumps(summary, ensure_ascii=False))
        return 0
    print(json.dumps(vault.get_status().to_dict(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli_main())
