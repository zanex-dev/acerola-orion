"""Workspace old-version scanner, new-version trigger detector, and safety guardrails.

Pure-stdlib module scanning workspace old versions across 5+1 categories while
enforcing editable-root protection, agent-blindfold privacy boundaries,
`.orion-vault-keep` / `pinned_paths` exemptions, and symlink-escape prevention.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import site
import stat
import sys
import time
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple, Union
from urllib.parse import unquote, urlparse

from .task_board import (
    _is_within_root,
    _parse_plan_markdown,
    _read_bounded_markdown,
)
from .workspace_prefs import (
    _normalize_single_task_workspace,
    get_task_workspaces,
)

# ---------------------------------------------------------------------------
# Constants & Patterns
# ---------------------------------------------------------------------------

CATEGORY_VERSIONS_DIR = "versions_dir"
CATEGORY_VERSIONED_SIBLING = "versioned_sibling"
CATEGORY_OLD_RELEASE = "old_release"
CATEGORY_COMPLETED_TASK = "completed_task"
CATEGORY_LEGACY_ARCHIVE = "legacy_archive"
CATEGORY_STALE_PROJECT = "stale_project"

VALID_CATEGORIES: Tuple[str, ...] = (
    CATEGORY_VERSIONS_DIR,
    CATEGORY_VERSIONED_SIBLING,
    CATEGORY_OLD_RELEASE,
    CATEGORY_COMPLETED_TASK,
    CATEGORY_LEGACY_ARCHIVE,
    CATEGORY_STALE_PROJECT,
)

TRIGGER_VERSIONS_DIR_SNAPSHOT = "versions_dir_snapshot"
TRIGGER_VERSIONED_SIBLING_BUMP = "versioned_sibling_bump"
TRIGGER_RELEASE_SEMVER_BUMP = "release_semver_bump"
TRIGGER_TASK_PACK_COMPLETED = "task_pack_completed"

KEEP_MARKER_FILENAME = ".orion-vault-keep"
DEFAULT_PINNED_REL_PATHS: Tuple[str, ...] = (
    "acerola-orion-public/_versions/panel_v1.py",
)

_PRIVACY_CONTROL_FILES_LOWER: Set[str] = {
    ".allow_media",
    "agent-blindfold.json",
}

_BLINDFOLD_SEGMENTS_LOWER: Set[str] = {
    ".user_uploaded",
    "tempmediastorage",
    "browser_sandbox",
}

_SKIP_TRAVERSE_DIRS_LOWER: Set[str] = {
    ".git",
    ".hg",
    ".svn",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".tox",
    ".nox",
    "node_modules",
    ".venv",
    "venv",
    ".idea",
    ".vscode",
    "build",
    "dist",
    ".user_uploaded",
    "tempmediastorage",
    "browser_sandbox",
}

_COMPOUND_EXTS: Tuple[str, ...] = (
    ".tar.gz",
    ".tar.bz2",
    ".tar.xz",
    ".tar.zst",
)

_RELEASE_STRIP_EXTS: Tuple[str, ...] = (
    ".tar.gz",
    ".tar.bz2",
    ".tar.xz",
    ".tar.zst",
    ".tgz",
    ".zip",
    ".7z",
    ".whl",
    ".exe",
    ".msi",
    ".dmg",
    ".vsix",
    ".crx",
    ".deb",
    ".rpm",
    ".apk",
    ".json",
    ".md",
    ".txt",
)

_VERSIONED_STEM_RE = re.compile(r"^(?P<base>.+?)[_-]v(?P<num>\d+)$", re.IGNORECASE)
_SEMVER_NAME_RE = re.compile(
    r"^(?:(?P<prefix>.+?)[_\-\s])?v?(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)"
    r"(?:-(?P<pre>[0-9A-Za-z.\-]+))?(?:\+[0-9A-Za-z.\-]+)?$",
    re.IGNORECASE,
)

STATE_SCHEMA_VERSION = 1


# ---------------------------------------------------------------------------
# Data Models
# ---------------------------------------------------------------------------

@dataclass
class ArchiveCandidate:
    candidate_id: str
    category: str
    project: str
    workspace: str
    source_path: str
    rel_path: str
    size_bytes: int
    file_count: int
    mtime: float
    trigger_reason: str
    requires_explicit_confirm: bool
    protected: bool
    protect_reason: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "category": self.category,
            "project": self.project,
            "workspace": self.workspace,
            "source_path": self.source_path,
            "rel_path": self.rel_path,
            "size_bytes": self.size_bytes,
            "file_count": self.file_count,
            "mtime": self.mtime,
            "trigger_reason": self.trigger_reason,
            "requires_explicit_confirm": self.requires_explicit_confirm,
            "protected": self.protected,
            "protect_reason": self.protect_reason,
        }


@dataclass
class VersionTriggerEvent:
    kind: str
    project: str
    workspace: str
    newest_path: str
    newest_version: str
    old_candidate_paths: List[str]
    detected_at: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "project": self.project,
            "workspace": self.workspace,
            "newest_path": self.newest_path,
            "newest_version": self.newest_version,
            "old_candidate_paths": list(self.old_candidate_paths),
            "detected_at": self.detected_at,
        }


@dataclass
class WorkspaceScanReport:
    workspaces: List[str]
    candidates: List[ArchiveCandidate]
    protected_items: List[ArchiveCandidate]
    triggers: List[VersionTriggerEvent]
    warnings: List[str]
    scanned_at: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "workspaces": list(self.workspaces),
            "candidates": [c.to_dict() for c in self.candidates],
            "protected_items": [p.to_dict() for p in self.protected_items],
            "triggers": [t.to_dict() for t in self.triggers],
            "warnings": list(self.warnings),
            "scanned_at": self.scanned_at,
        }


# ---------------------------------------------------------------------------
# Path & Editable Root Helpers
# ---------------------------------------------------------------------------

def _now_iso(ts: Optional[float] = None) -> str:
    dt = datetime.fromtimestamp(time.time() if ts is None else ts).astimezone()
    return dt.isoformat(timespec="seconds")


def _clean_path_str(p: Union[str, os.PathLike]) -> str:
    raw = os.fspath(p)
    cleaned = raw.rstrip("\\/")
    return cleaned or raw


def _norm_real(p: Union[str, os.PathLike]) -> str:
    try:
        resolved = os.path.realpath(os.fspath(p))
    except OSError:
        resolved = os.path.abspath(os.fspath(p))
    return _clean_path_str(resolved)


def _norm_nc(p: Union[str, os.PathLike]) -> str:
    return os.path.normcase(_norm_real(p))


def _path_is_same_or_descendant(path_nc: str, ancestor_nc: str) -> bool:
    return path_nc == ancestor_nc or path_nc.startswith(ancestor_nc + os.sep)


def _make_candidate_id(category: str, source_path: str) -> str:
    digest = hashlib.sha1(
        f"{category}|{os.path.normcase(_clean_path_str(source_path))}".encode("utf-8")
    ).hexdigest()
    return digest[:16]


def _file_url_to_path(url: str) -> Optional[str]:
    if not isinstance(url, str) or not url.lower().startswith("file://"):
        return None
    parsed = urlparse(url)
    if parsed.scheme.lower() != "file":
        return None
    # Handle file://localhost/C:/... or file:///C:/... without importing network modules
    netloc = unquote(parsed.netloc or "")
    path_part = unquote(parsed.path or "")
    if netloc and netloc.lower() != "localhost":
        if os.name == "nt":
            local = f"\\\\{netloc}" + path_part.replace("/", "\\")
        else:
            local = f"//{netloc}{path_part}"
        return local or None
    if os.name == "nt":
        work = path_part
        if len(work) >= 3 and work[0] == "/" and work[1].isalpha() and work[2] in (":", "|"):
            work = work[1] + ":" + work[3:]
        elif len(work) >= 2 and work[0].isalpha() and work[1] == "|":
            work = work[0] + ":" + work[2:]
        local = work.replace("/", "\\")
    else:
        local = path_part
    return local or None


def _collect_editable_roots_from_site_dirs(site_dirs: Iterable[Path]) -> Set[str]:
    roots: Set[str] = set()
    visited_dirs: Set[str] = set()

    for sdir in site_dirs:
        try:
            sdir_real = _norm_real(sdir)
        except OSError:
            continue
        sdir_nc = os.path.normcase(sdir_real)
        if sdir_nc in visited_dirs:
            continue
        visited_dirs.add(sdir_nc)
        sdir_path = Path(sdir_real)
        if not sdir_path.is_dir():
            continue

        try:
            entries = list(os.scandir(sdir_path))
        except OSError:
            continue

        for entry in entries:
            name_lower = entry.name.lower()
            # 1) *.pth files (editable paths / easy-install.pth)
            if name_lower.endswith(".pth") and entry.is_file(follow_symlinks=True):
                try:
                    with open(entry.path, "r", encoding="utf-8-sig", errors="replace") as fh:
                        for raw_line in fh:
                            line = raw_line.strip()
                            if not line or line.startswith("#"):
                                continue
                            if line.startswith("import ") or line.startswith("import\t"):
                                continue
                            cand = Path(line)
                            if not cand.is_absolute():
                                cand = sdir_path / cand
                            try:
                                if cand.is_dir():
                                    roots.add(_norm_real(cand))
                            except OSError:
                                pass
                except OSError:
                    pass
                continue

            # 2) *.dist-info/direct_url.json (PEP 610 / PEP 660 editable installs)
            if name_lower.endswith(".dist-info") and entry.is_dir(follow_symlinks=True):
                direct_url_file = Path(entry.path) / "direct_url.json"
                if not direct_url_file.is_file():
                    continue
                try:
                    with open(direct_url_file, "r", encoding="utf-8-sig") as fh:
                        data = json.load(fh)
                except (OSError, ValueError):
                    continue
                if not isinstance(data, dict):
                    continue
                dir_info = data.get("dir_info")
                if not isinstance(dir_info, dict) or not dir_info.get("editable"):
                    continue
                local_path = _file_url_to_path(data.get("url", ""))
                if not local_path:
                    continue
                try:
                    if os.path.isdir(local_path):
                        roots.add(_norm_real(local_path))
                except OSError:
                    pass

    return roots


def detect_editable_roots(
    *,
    extra_site_dirs: Optional[Iterable[Union[str, os.PathLike]]] = None,
    include_current_package: bool = True,
) -> Set[str]:
    """Detect editable-installed package root directories from site-packages and current package."""
    site_dirs: List[Path] = []
    if extra_site_dirs is None:
        try:
            for sp in site.getsitepackages():
                site_dirs.append(Path(sp))
        except Exception:
            pass
        try:
            usp = site.getusersitepackages()
            if isinstance(usp, str) and usp:
                site_dirs.append(Path(usp))
        except Exception:
            pass
        for p in sys.path:
            if not p:
                continue
            low = p.lower()
            if "site-packages" in low or "dist-packages" in low:
                site_dirs.append(Path(p))
    else:
        for d in extra_site_dirs:
            site_dirs.append(Path(d))

    roots = _collect_editable_roots_from_site_dirs(site_dirs)
    if include_current_package:
        pkg_root = Path(__file__).resolve().parent.parent.parent
        if pkg_root.is_dir():
            roots.add(_norm_real(pkg_root))
    return roots


# ---------------------------------------------------------------------------
# Version Parsing & Filesystem Stat Helpers
# ---------------------------------------------------------------------------

def _split_name_and_ext(name: str, is_dir: bool) -> Tuple[str, str]:
    if is_dir:
        return name, ""
    lower = name.lower()
    for ext in _COMPOUND_EXTS:
        if lower.endswith(ext) and len(name) > len(ext):
            return name[:-len(ext)], name[-len(ext):]
    stem, ext = os.path.splitext(name)
    return stem, ext


def _parse_versioned_sibling_name(name: str, is_dir: bool) -> Optional[Tuple[str, int, str]]:
    """Parse `*_vN` sibling name -> `(base, version_int, ext)` or `None`."""
    stem, ext = _split_name_and_ext(name, is_dir)
    m = _VERSIONED_STEM_RE.match(stem)
    if not m:
        return None
    base = m.group("base")
    if not base or not base.strip("_-."):
        return None
    try:
        ver_num = int(m.group("num"))
    except ValueError:
        return None
    return base, ver_num, ext


def _prerelease_sort_key(pre: Optional[str]) -> Tuple[int, Tuple[Tuple[int, Any], ...]]:
    if not pre:
        # Final release ranks higher than any prerelease
        return (1, ())
    parts: List[Tuple[int, Any]] = []
    for token in pre.split("."):
        if token.isdigit():
            parts.append((0, int(token)))
        else:
            parts.append((1, token.lower()))
    return (0, tuple(parts))


def _parse_release_semver(
    name: str, is_dir: bool
) -> Optional[Tuple[str, Tuple[int, int, int, int, Tuple[Tuple[int, Any], ...]], str]]:
    """Parse a release item name -> `(prefix_norm, semver_sort_key, semver_str)` or `None`."""
    work = name
    if not is_dir:
        low = work.lower()
        for ext in _RELEASE_STRIP_EXTS:
            if low.endswith(ext) and len(work) > len(ext):
                work = work[:-len(ext)]
                break
    m = _SEMVER_NAME_RE.match(work)
    if not m:
        return None
    prefix = (m.group("prefix") or "").strip("_- ").lower()
    major = int(m.group("major"))
    minor = int(m.group("minor"))
    patch = int(m.group("patch"))
    pre = m.group("pre")
    pre_flag, pre_parts = _prerelease_sort_key(pre)
    sort_key = (major, minor, patch, pre_flag, pre_parts)
    ver_str = f"{major}.{minor}.{patch}" + (f"-{pre}" if pre else "")
    return prefix, sort_key, ver_str


def _is_escaping_symlink(path: Path, root_real_nc: str) -> bool:
    """Return True iff `path` is a symlink/junction resolving outside `root_real_nc`."""
    try:
        is_link = path.is_symlink()
    except OSError:
        is_link = False
    if is_link:
        return not _is_within_root(path, root_real_nc)
    # Also guard Windows directory junctions (os.path.islink is False on some Python/Win combos)
    return not _is_within_root(path, root_real_nc)


def _safe_measure_path(
    target: Path,
    root_real_nc: str,
    warnings: List[str],
    *,
    skip_cache_dirs: bool = False,
) -> Tuple[int, int, float, bool, bool]:
    """
    Safely compute `(size_bytes, file_count, latest_mtime, has_keep_marker, has_privacy_ctrl)`
    for a file or directory without following symlinks or crossing `root_real_nc`.
    """
    if not _is_within_root(target, root_real_nc):
        warnings.append(f"安全拦截：跳过逃逸工作区边界的符号链接: {target}")
        return 0, 0, 0.0, False, False

    try:
        st = os.lstat(target)
    except OSError as exc:
        warnings.append(f"无法读取路径元信息 {target}: {exc}")
        return 0, 0, 0.0, False, False

    if stat.S_ISLNK(st.st_mode):
        # Even internal symlinks are not dereferenced for sizing
        return 0, 0, float(st.st_mtime), False, False

    name_lower = target.name.lower()
    if stat.S_ISREG(st.st_mode):
        return (
            int(st.st_size),
            1,
            float(st.st_mtime),
            name_lower == KEEP_MARKER_FILENAME,
            name_lower in _PRIVACY_CONTROL_FILES_LOWER,
        )

    if not stat.S_ISDIR(st.st_mode):
        return 0, 0, float(st.st_mtime), False, False

    total_size = 0
    total_files = 0
    latest_mtime = float(st.st_mtime)
    has_keep = False
    has_privacy = False

    stack: List[Path] = [target]
    while stack:
        cur = stack.pop()
        try:
            with os.scandir(cur) as it:
                for entry in it:
                    ep = Path(entry.path)
                    ename_lower = entry.name.lower()
                    if entry.is_symlink():
                        if not _is_within_root(ep, root_real_nc):
                            warnings.append(f"安全拦截：跳过逃逸工作区边界的符号链接: {ep}")
                        continue
                    if not _is_within_root(ep, root_real_nc):
                        warnings.append(f"安全拦截：跳过逃逸工作区边界的目录联接: {ep}")
                        continue
                    try:
                        est = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    if stat.S_ISDIR(est.st_mode):
                        if skip_cache_dirs and ename_lower in _SKIP_TRAVERSE_DIRS_LOWER:
                            continue
                        if float(est.st_mtime) > latest_mtime:
                            latest_mtime = float(est.st_mtime)
                        stack.append(ep)
                    elif stat.S_ISREG(est.st_mode):
                        if ename_lower == KEEP_MARKER_FILENAME:
                            has_keep = True
                        if ename_lower in _PRIVACY_CONTROL_FILES_LOWER:
                            has_privacy = True
                        total_size += int(est.st_size)
                        total_files += 1
                        if float(est.st_mtime) > latest_mtime:
                            latest_mtime = float(est.st_mtime)
        except OSError as exc:
            warnings.append(f"无法遍历目录 {cur}: {exc}")

    return total_size, total_files, latest_mtime, has_keep, has_privacy


def _rel_posix(target: Path, ws_path: Path) -> str:
    try:
        return target.relative_to(ws_path).as_posix()
    except ValueError:
        try:
            return Path(os.path.relpath(str(target), str(ws_path))).as_posix()
        except ValueError:
            return target.name


def _derive_project_name(rel_posix: str, ws_path: Path) -> str:
    parts = [p for p in rel_posix.split("/") if p and p != "."]
    if not parts:
        return ws_path.name
    first = parts[0]
    first_low = first.lower()
    if len(parts) == 1 or first_low in {"_versions", "_releases", "_archive"}:
        return ws_path.name
    return first


# ---------------------------------------------------------------------------
# Protection Guardrail Evaluation
# ---------------------------------------------------------------------------

def _is_under_agents_plugins(rel_posix: str) -> bool:
    parts = [p.lower() for p in rel_posix.split("/") if p]
    for idx in range(len(parts) - 1):
        if parts[idx] == ".agents" and parts[idx + 1] == "plugins":
            return True
    return False


def _has_ancestor_keep_marker(target: Path, ws_path: Path) -> bool:
    ws_nc = _norm_nc(ws_path)
    cur = target.parent
    while True:
        cur_nc = _norm_nc(cur)
        if not _path_is_same_or_descendant(cur_nc, ws_nc):
            break
        marker = cur / KEEP_MARKER_FILENAME
        try:
            if marker.is_file() and not marker.is_symlink():
                return True
        except OSError:
            pass
        if cur_nc == ws_nc:
            break
        parent = cur.parent
        if parent == cur:
            break
        cur = parent
    return False


def _resolve_pinned_sets(
    ws_path: Path,
    pinned_paths: Iterable[Union[str, os.PathLike]],
) -> Tuple[Set[str], Set[str]]:
    """Return `(pinned_abs_nc_set, pinned_rel_posix_lower_set)` for a workspace."""
    abs_nc: Set[str] = set()
    rel_lower: Set[str] = set()

    all_raw: List[Union[str, os.PathLike]] = list(DEFAULT_PINNED_REL_PATHS)
    all_raw.extend(pinned_paths)

    for raw in all_raw:
        if not raw:
            continue
        raw_str = os.fspath(raw).strip()
        if not raw_str:
            continue
        p = Path(raw_str)
        if p.is_absolute():
            abs_nc.add(_norm_nc(p))
            try:
                rel_lower.add(p.relative_to(ws_path).as_posix().lower())
            except ValueError:
                pass
        else:
            norm_rel = raw_str.replace("\\", "/").strip("/")
            if norm_rel:
                rel_lower.add(norm_rel.lower())
                abs_nc.add(_norm_nc(ws_path / norm_rel))

    return abs_nc, rel_lower


def _check_protection(
    target: Path,
    ws_path: Path,
    rel_posix: str,
    *,
    category: str,
    editable_roots_nc: Set[str],
    pinned_abs_nc: Set[str],
    pinned_rel_lower: Set[str],
    has_internal_keep: bool,
    has_internal_privacy_ctrl: bool,
) -> Tuple[bool, str]:
    """Evaluate all hard & exemption protection rules for a candidate path."""
    name_lower = target.name.lower()
    target_nc = _norm_nc(target)
    rel_low = rel_posix.lower().strip("/")

    # 1. Hard protect: .agents/plugins/ & blindfold sandbox segments
    if _is_under_agents_plugins(rel_posix):
        return True, "插件目录 .agents/plugins/ 受硬保护"
    if any(seg.lower() in _BLINDFOLD_SEGMENTS_LOWER for seg in rel_posix.split("/") if seg):
        return True, "隐私媒体/沙箱目录受硬保护（agent-blindfold）"

    # 2. Hard protect: .allow_media / agent-blindfold.json
    if name_lower in _PRIVACY_CONTROL_FILES_LOWER or has_internal_privacy_ctrl:
        return True, "隐私控制文件（.allow_media / agent-blindfold.json）受硬保护"

    # 3. Hard protect: .orion-vault-keep marker (self, inside dir, or ancestor dir)
    if (
        name_lower == KEEP_MARKER_FILENAME
        or has_internal_keep
        or _has_ancestor_keep_marker(target, ws_path)
    ):
        return True, f"命中 {KEEP_MARKER_FILENAME} 保留标记"

    # 4. Exemption: pinned_paths (exact match, inside pinned dir, or ancestor of pinned file)
    if rel_low in pinned_rel_lower or target_nc in pinned_abs_nc:
        return True, "命中 pinned_paths 豁免清单"
    for pin_nc in pinned_abs_nc:
        if _path_is_same_or_descendant(target_nc, pin_nc):
            return True, "位于 pinned_paths 豁免目录内"
        if _path_is_same_or_descendant(pin_nc, target_nc):
            return True, "包含 pinned_paths 豁免路径，禁止整移"

    # 5. Hard protect: editable package root (protect root/ancestor from whole move,
    #    while still allowing cleanup of old versions strictly inside the editable root)
    for ed_nc in editable_roots_nc:
        if target_nc == ed_nc or _path_is_same_or_descendant(ed_nc, target_nc):
            return True, "editable 安装包根目录受硬保护（仅允许清理内部旧版本，禁止整移）"

    return False, ""


# ---------------------------------------------------------------------------
# Workspace Validation Helper
# ---------------------------------------------------------------------------

def _validate_workspaces(
    workspaces: Optional[Sequence[Union[str, os.PathLike]]],
    warnings: List[str],
) -> List[Tuple[str, Path, str]]:
    """Validate workspace list against agent-blindfold rules; returns `[(ws_str, ws_path, root_real_nc)]`."""
    if workspaces is None:
        raw_list: Sequence[Union[str, os.PathLike]] = get_task_workspaces()
    elif isinstance(workspaces, (str, os.PathLike)):
        raw_list = [workspaces]
    else:
        raw_list = list(workspaces)

    valid: List[Tuple[str, Path, str]] = []
    seen_nc: Set[str] = set()
    for raw in raw_list:
        norm, reject_reason = _normalize_single_task_workspace(raw)
        if not norm:
            warnings.append(reject_reason or f"拒绝非法工作区路径: {raw}")
            continue
        ws_path = Path(norm)
        try:
            if not ws_path.is_dir():
                warnings.append(f"跳过不存在或非目录的工作区: {norm}")
                continue
            real_ws = _norm_real(ws_path)
            # Re-validate resolved realpath in case the workspace itself was a symlink to a blocked vault
            norm_real_check, real_reject = _normalize_single_task_workspace(real_ws)
            if not norm_real_check:
                warnings.append(real_reject or f"安全拦截：工作区解析目标受隐私隔离限制: {norm}")
                continue
        except OSError as exc:
            warnings.append(f"无法访问工作区 {norm}: {exc}")
            continue

        root_real_nc = os.path.normcase(real_ws)
        if root_real_nc in seen_nc:
            continue
        seen_nc.add(root_real_nc)
        valid.append((norm, ws_path, root_real_nc))
    return valid


# ---------------------------------------------------------------------------
# Core Workspace Scanner
# ---------------------------------------------------------------------------

def _emit_candidate(
    *,
    category: str,
    target: Path,
    ws_str: str,
    ws_path: Path,
    root_real_nc: str,
    trigger_reason: str,
    requires_explicit_confirm: bool,
    editable_roots_nc: Set[str],
    pinned_abs_nc: Set[str],
    pinned_rel_lower: Set[str],
    warnings: List[str],
    candidates: List[ArchiveCandidate],
    protected_items: List[ArchiveCandidate],
    force_protected: bool = False,
    force_protect_reason: str = "",
    skip_cache_dirs: bool = False,
    premeasured: Optional[Tuple[int, int, float, bool, bool]] = None,
) -> Optional[ArchiveCandidate]:
    if _is_escaping_symlink(target, root_real_nc):
        warnings.append(f"安全拦截：跳过逃逸工作区边界的符号链接: {target}")
        return None

    rel_posix = _rel_posix(target, ws_path)
    project = _derive_project_name(rel_posix, ws_path)
    if premeasured is None:
        size_b, fcount, mtime, has_keep, has_priv = _safe_measure_path(
            target, root_real_nc, warnings, skip_cache_dirs=skip_cache_dirs
        )
    else:
        size_b, fcount, mtime, has_keep, has_priv = premeasured

    if force_protected:
        protected = True
        protect_reason = force_protect_reason
    else:
        protected, protect_reason = _check_protection(
            target,
            ws_path,
            rel_posix,
            category=category,
            editable_roots_nc=editable_roots_nc,
            pinned_abs_nc=pinned_abs_nc,
            pinned_rel_lower=pinned_rel_lower,
            has_internal_keep=has_keep,
            has_internal_privacy_ctrl=has_priv,
        )

    item = ArchiveCandidate(
        candidate_id=_make_candidate_id(category, str(target)),
        category=category,
        project=project,
        workspace=ws_str,
        source_path=str(target),
        rel_path=rel_posix,
        size_bytes=size_b,
        file_count=fcount,
        mtime=mtime,
        trigger_reason=trigger_reason,
        requires_explicit_confirm=requires_explicit_confirm,
        protected=protected,
        protect_reason=protect_reason,
    )
    if protected:
        protected_items.append(item)
    else:
        candidates.append(item)
    return item


def _scan_agents_dir(
    agents_dir: Path,
    ws_str: str,
    ws_path: Path,
    root_real_nc: str,
    today_date: Any,
    editable_roots_nc: Set[str],
    pinned_abs_nc: Set[str],
    pinned_rel_lower: Set[str],
    warnings: List[str],
    candidates: List[ArchiveCandidate],
    protected_items: List[ArchiveCandidate],
) -> None:
    """Scan `.agents/` for completed task packs, stale checkpoints, and `_versions/` (skipping `plugins/`)."""
    if _is_escaping_symlink(agents_dir, root_real_nc):
        warnings.append(f"安全拦截：跳过逃逸工作区边界的符号链接目录: {agents_dir}")
        return

    # 1) .agents/tasks/<pack>
    tasks_dir = agents_dir / "tasks"
    if tasks_dir.is_dir() and not _is_escaping_symlink(tasks_dir, root_real_nc):
        try:
            task_entries = sorted(os.scandir(tasks_dir), key=lambda e: e.name.lower())
        except OSError as exc:
            warnings.append(f"无法扫描任务目录 {tasks_dir}: {exc}")
            task_entries = []
        for entry in task_entries:
            pack_dir = Path(entry.path)
            if entry.is_symlink() or not _is_within_root(pack_dir, root_real_nc):
                if not _is_within_root(pack_dir, root_real_nc):
                    warnings.append(f"安全拦截：跳过逃逸工作区边界的符号链接: {pack_dir}")
                continue
            if not entry.is_dir(follow_symlinks=False):
                continue

            plan_path = pack_dir / "PLAN.md"
            is_completed = False
            reason = "未完结任务包受硬保护"
            if plan_path.is_file():
                read_res = _read_bounded_markdown(plan_path, root_real_nc, warnings)
                if read_res is not None:
                    text, plan_mtime = read_res
                    pack_item = _parse_plan_markdown(
                        text,
                        workspace=ws_str,
                        pack_dir=pack_dir,
                        plan_path=plan_path,
                        plan_mtime=plan_mtime,
                        root_real_nc=root_real_nc,
                    )
                    if pack_item.total_count > 0 and pack_item.done_count == pack_item.total_count:
                        is_completed = True
                        reason = f"任务包已全部验收（{pack_item.done_count}/{pack_item.total_count}）"
                    else:
                        reason = (
                            f"未完结任务包受硬保护（已完成 {pack_item.done_count}/{pack_item.total_count}）"
                        )
            _emit_candidate(
                category=CATEGORY_COMPLETED_TASK,
                target=pack_dir,
                ws_str=ws_str,
                ws_path=ws_path,
                root_real_nc=root_real_nc,
                trigger_reason=reason if is_completed else "任务包尚未全部验收",
                requires_explicit_confirm=False,
                editable_roots_nc=editable_roots_nc,
                pinned_abs_nc=pinned_abs_nc,
                pinned_rel_lower=pinned_rel_lower,
                warnings=warnings,
                candidates=candidates,
                protected_items=protected_items,
                force_protected=not is_completed,
                force_protect_reason="" if is_completed else reason,
            )

    # 2) .agents/checkpoints/<item> (non-today checkpoints)
    checkpoints_dir = agents_dir / "checkpoints"
    if checkpoints_dir.is_dir() and not _is_escaping_symlink(checkpoints_dir, root_real_nc):
        try:
            cp_entries = sorted(os.scandir(checkpoints_dir), key=lambda e: e.name.lower())
        except OSError as exc:
            warnings.append(f"无法扫描 checkpoints 目录 {checkpoints_dir}: {exc}")
            cp_entries = []
        for entry in cp_entries:
            cp_path = Path(entry.path)
            if entry.is_symlink() or not _is_within_root(cp_path, root_real_nc):
                if not _is_within_root(cp_path, root_real_nc):
                    warnings.append(f"安全拦截：跳过逃逸工作区边界的符号链接: {cp_path}")
                continue
            if entry.name.lower() == KEEP_MARKER_FILENAME:
                continue
            measured = _safe_measure_path(cp_path, root_real_nc, warnings)
            _, _, item_mtime, _, _ = measured
            if item_mtime <= 0:
                continue
            item_date = datetime.fromtimestamp(item_mtime).date()
            if item_date < today_date:
                _emit_candidate(
                    category=CATEGORY_COMPLETED_TASK,
                    target=cp_path,
                    ws_str=ws_str,
                    ws_path=ws_path,
                    root_real_nc=root_real_nc,
                    trigger_reason=f"非当日历史 checkpoint（{item_date.isoformat()}）",
                    requires_explicit_confirm=False,
                    editable_roots_nc=editable_roots_nc,
                    pinned_abs_nc=pinned_abs_nc,
                    pinned_rel_lower=pinned_rel_lower,
                    warnings=warnings,
                    candidates=candidates,
                    protected_items=protected_items,
                    premeasured=measured,
                )

    # 3) Recursively scan other subdirs under .agents (excluding plugins/, tasks/, checkpoints/)
    #    for `_versions/` and `_archive/`
    stack: List[Path] = []
    try:
        for entry in sorted(os.scandir(agents_dir), key=lambda e: e.name.lower()):
            ep = Path(entry.path)
            if entry.is_symlink() or not _is_within_root(ep, root_real_nc):
                if not _is_within_root(ep, root_real_nc):
                    warnings.append(f"安全拦截：跳过逃逸工作区边界的符号链接: {ep}")
                continue
            if not entry.is_dir(follow_symlinks=False):
                continue
            low = entry.name.lower()
            if low in {"plugins", "tasks", "checkpoints"}:
                continue
            stack.append(ep)
    except OSError:
        return

    while stack:
        cur = stack.pop()
        low_cur = cur.name.lower()
        if low_cur == "_versions":
            _scan_flat_bucket_dir(
                cur,
                category=CATEGORY_VERSIONS_DIR,
                reason="位于 _versions/ 历史版本目录",
                ws_str=ws_str,
                ws_path=ws_path,
                root_real_nc=root_real_nc,
                editable_roots_nc=editable_roots_nc,
                pinned_abs_nc=pinned_abs_nc,
                pinned_rel_lower=pinned_rel_lower,
                warnings=warnings,
                candidates=candidates,
                protected_items=protected_items,
            )
            continue
        if low_cur == "_archive":
            _scan_flat_bucket_dir(
                cur,
                category=CATEGORY_LEGACY_ARCHIVE,
                reason="位于 _archive/ 历史归档目录",
                ws_str=ws_str,
                ws_path=ws_path,
                root_real_nc=root_real_nc,
                editable_roots_nc=editable_roots_nc,
                pinned_abs_nc=pinned_abs_nc,
                pinned_rel_lower=pinned_rel_lower,
                warnings=warnings,
                candidates=candidates,
                protected_items=protected_items,
            )
            continue
        try:
            for entry in sorted(os.scandir(cur), key=lambda e: e.name.lower()):
                ep = Path(entry.path)
                if entry.is_symlink() or not _is_within_root(ep, root_real_nc):
                    if not _is_within_root(ep, root_real_nc):
                        warnings.append(f"安全拦截：跳过逃逸工作区边界的符号链接: {ep}")
                    continue
                if entry.is_dir(follow_symlinks=False):
                    if entry.name.lower() not in _SKIP_TRAVERSE_DIRS_LOWER:
                        stack.append(ep)
        except OSError:
            continue


def _scan_flat_bucket_dir(
    bucket_dir: Path,
    *,
    category: str,
    reason: str,
    ws_str: str,
    ws_path: Path,
    root_real_nc: str,
    editable_roots_nc: Set[str],
    pinned_abs_nc: Set[str],
    pinned_rel_lower: Set[str],
    warnings: List[str],
    candidates: List[ArchiveCandidate],
    protected_items: List[ArchiveCandidate],
) -> None:
    """Scan direct children of `_versions/` or `_archive/` into candidates/protected_items."""
    if _is_escaping_symlink(bucket_dir, root_real_nc):
        warnings.append(f"安全拦截：跳过逃逸工作区边界的符号链接目录: {bucket_dir}")
        return
    try:
        entries = sorted(os.scandir(bucket_dir), key=lambda e: e.name.lower())
    except OSError as exc:
        warnings.append(f"无法扫描目录 {bucket_dir}: {exc}")
        return

    for entry in entries:
        ep = Path(entry.path)
        if entry.is_symlink() or not _is_within_root(ep, root_real_nc):
            if not _is_within_root(ep, root_real_nc):
                warnings.append(f"安全拦截：跳过逃逸工作区边界的符号链接: {ep}")
            continue
        if entry.name.lower() == KEEP_MARKER_FILENAME:
            continue
        _emit_candidate(
            category=category,
            target=ep,
            ws_str=ws_str,
            ws_path=ws_path,
            root_real_nc=root_real_nc,
            trigger_reason=reason,
            requires_explicit_confirm=False,
            editable_roots_nc=editable_roots_nc,
            pinned_abs_nc=pinned_abs_nc,
            pinned_rel_lower=pinned_rel_lower,
            warnings=warnings,
            candidates=candidates,
            protected_items=protected_items,
        )


def _scan_releases_dir(
    releases_dir: Path,
    *,
    ws_str: str,
    ws_path: Path,
    root_real_nc: str,
    editable_roots_nc: Set[str],
    pinned_abs_nc: Set[str],
    pinned_rel_lower: Set[str],
    warnings: List[str],
    candidates: List[ArchiveCandidate],
    protected_items: List[ArchiveCandidate],
) -> None:
    """Scan `_releases/` for older semver releases (keeping the highest semver per prefix)."""
    if _is_escaping_symlink(releases_dir, root_real_nc):
        warnings.append(f"安全拦截：跳过逃逸工作区边界的符号链接目录: {releases_dir}")
        return
    try:
        entries = sorted(os.scandir(releases_dir), key=lambda e: e.name.lower())
    except OSError as exc:
        warnings.append(f"无法扫描发布目录 {releases_dir}: {exc}")
        return

    groups: Dict[str, List[Tuple[Tuple[int, int, int, int, Tuple], str, Path]]] = {}
    for entry in entries:
        ep = Path(entry.path)
        if entry.is_symlink() or not _is_within_root(ep, root_real_nc):
            if not _is_within_root(ep, root_real_nc):
                warnings.append(f"安全拦截：跳过逃逸工作区边界的符号链接: {ep}")
            continue
        is_dir = entry.is_dir(follow_symlinks=False)
        parsed = _parse_release_semver(entry.name, is_dir)
        if parsed is None:
            continue
        prefix, sort_key, ver_str = parsed
        groups.setdefault(prefix, []).append((sort_key, ver_str, ep))

    for _prefix, items in groups.items():
        distinct_keys = {it[0] for it in items}
        if len(distinct_keys) < 2:
            continue
        max_key = max(distinct_keys)
        newest_ver_str = next(it[1] for it in items if it[0] == max_key)
        for sort_key, ver_str, ep in sorted(items, key=lambda x: (x[0], x[2].name.lower())):
            if sort_key < max_key:
                _emit_candidate(
                    category=CATEGORY_OLD_RELEASE,
                    target=ep,
                    ws_str=ws_str,
                    ws_path=ws_path,
                    root_real_nc=root_real_nc,
                    trigger_reason=f"旧发布版本 {ver_str}（已有更高版本 {newest_ver_str}）",
                    requires_explicit_confirm=False,
                    editable_roots_nc=editable_roots_nc,
                    pinned_abs_nc=pinned_abs_nc,
                    pinned_rel_lower=pinned_rel_lower,
                    warnings=warnings,
                    candidates=candidates,
                    protected_items=protected_items,
                )


def _scan_workspace_tree(
    ws_str: str,
    ws_path: Path,
    root_real_nc: str,
    *,
    now_ts: float,
    include_stale_projects: bool,
    stale_days: float,
    editable_roots_nc: Set[str],
    pinned_abs_nc: Set[str],
    pinned_rel_lower: Set[str],
    warnings: List[str],
    candidates: List[ArchiveCandidate],
    protected_items: List[ArchiveCandidate],
) -> None:
    today_date = datetime.fromtimestamp(now_ts).date()
    stale_threshold_sec = max(0.0, float(stale_days)) * 86400.0

    # Optional: stale top-level project scan (requires explicit confirmation; editable roots protected)
    if include_stale_projects:
        try:
            top_entries = sorted(os.scandir(ws_path), key=lambda e: e.name.lower())
        except OSError as exc:
            warnings.append(f"无法扫描工作区根目录 {ws_path}: {exc}")
            top_entries = []
        for entry in top_entries:
            ep = Path(entry.path)
            if entry.is_symlink() or not _is_within_root(ep, root_real_nc):
                if not _is_within_root(ep, root_real_nc):
                    warnings.append(f"安全拦截：跳过逃逸工作区边界的符号链接: {ep}")
                continue
            if not entry.is_dir(follow_symlinks=False):
                continue
            low = entry.name.lower()
            if (
                low.startswith(".")
                or low in _SKIP_TRAVERSE_DIRS_LOWER
                or low in {"_versions", "_releases", "_archive"}
            ):
                continue
            measured = _safe_measure_path(ep, root_real_nc, warnings, skip_cache_dirs=True)
            _, _, latest_mtime, _, _ = measured
            if latest_mtime > 0 and (now_ts - latest_mtime) >= stale_threshold_sec:
                days_idle = (now_ts - latest_mtime) / 86400.0
                _emit_candidate(
                    category=CATEGORY_STALE_PROJECT,
                    target=ep,
                    ws_str=ws_str,
                    ws_path=ws_path,
                    root_real_nc=root_real_nc,
                    trigger_reason=f"顶层项目已静默 {days_idle:.1f} 天（阈值 {stale_days:g} 天，需显式确认）",
                    requires_explicit_confirm=True,
                    editable_roots_nc=editable_roots_nc,
                    pinned_abs_nc=pinned_abs_nc,
                    pinned_rel_lower=pinned_rel_lower,
                    warnings=warnings,
                    candidates=candidates,
                    protected_items=protected_items,
                    premeasured=measured,
                )

    # Walk workspace tree (DFS, no symlink following)
    stack: List[Path] = [ws_path]
    while stack:
        cur_dir = stack.pop()
        try:
            entries = sorted(os.scandir(cur_dir), key=lambda e: e.name.lower())
        except OSError as exc:
            warnings.append(f"无法读取目录 {cur_dir}: {exc}")
            continue

        sibling_groups: Dict[Tuple[bool, str, str], List[Tuple[int, Path]]] = {}
        child_dirs: List[Path] = []
        old_sibling_dirs_nc: Set[str] = set()

        for entry in entries:
            ep = Path(entry.path)
            if entry.is_symlink() or not _is_within_root(ep, root_real_nc):
                if not _is_within_root(ep, root_real_nc):
                    warnings.append(f"安全拦截：跳过逃逸工作区边界的符号链接: {ep}")
                continue

            ename = entry.name
            elow = ename.lower()
            is_dir = entry.is_dir(follow_symlinks=False)

            if is_dir:
                if elow == ".agents":
                    _scan_agents_dir(
                        ep,
                        ws_str,
                        ws_path,
                        root_real_nc,
                        today_date,
                        editable_roots_nc,
                        pinned_abs_nc,
                        pinned_rel_lower,
                        warnings,
                        candidates,
                        protected_items,
                    )
                    continue
                if elow == "_versions":
                    _scan_flat_bucket_dir(
                        ep,
                        category=CATEGORY_VERSIONS_DIR,
                        reason="位于 _versions/ 历史版本目录",
                        ws_str=ws_str,
                        ws_path=ws_path,
                        root_real_nc=root_real_nc,
                        editable_roots_nc=editable_roots_nc,
                        pinned_abs_nc=pinned_abs_nc,
                        pinned_rel_lower=pinned_rel_lower,
                        warnings=warnings,
                        candidates=candidates,
                        protected_items=protected_items,
                    )
                    continue
                if elow == "_archive":
                    _scan_flat_bucket_dir(
                        ep,
                        category=CATEGORY_LEGACY_ARCHIVE,
                        reason="位于 _archive/ 历史归档目录",
                        ws_str=ws_str,
                        ws_path=ws_path,
                        root_real_nc=root_real_nc,
                        editable_roots_nc=editable_roots_nc,
                        pinned_abs_nc=pinned_abs_nc,
                        pinned_rel_lower=pinned_rel_lower,
                        warnings=warnings,
                        candidates=candidates,
                        protected_items=protected_items,
                    )
                    continue
                if elow == "_releases":
                    _scan_releases_dir(
                        ep,
                        ws_str=ws_str,
                        ws_path=ws_path,
                        root_real_nc=root_real_nc,
                        editable_roots_nc=editable_roots_nc,
                        pinned_abs_nc=pinned_abs_nc,
                        pinned_rel_lower=pinned_rel_lower,
                        warnings=warnings,
                        candidates=candidates,
                        protected_items=protected_items,
                    )
                    continue
                if elow in _SKIP_TRAVERSE_DIRS_LOWER or elow.endswith(".egg-info"):
                    continue

                parsed_sib = _parse_versioned_sibling_name(ename, True)
                if parsed_sib is not None:
                    base, ver_num, ext = parsed_sib
                    sibling_groups.setdefault((True, base.lower(), ext.lower()), []).append(
                        (ver_num, ep)
                    )
                child_dirs.append(ep)
            else:
                parsed_sib = _parse_versioned_sibling_name(ename, False)
                if parsed_sib is not None:
                    base, ver_num, ext = parsed_sib
                    sibling_groups.setdefault((False, base.lower(), ext.lower()), []).append(
                        (ver_num, ep)
                    )

        # Emit old versioned siblings in cur_dir
        for (is_dir, _base_low, _ext_low), items in sibling_groups.items():
            distinct_vers = {v for v, _ in items}
            if len(distinct_vers) < 2:
                continue
            max_ver = max(distinct_vers)
            for ver_num, ep in sorted(items, key=lambda x: (x[0], x[1].name.lower())):
                if ver_num < max_ver:
                    emitted = _emit_candidate(
                        category=CATEGORY_VERSIONED_SIBLING,
                        target=ep,
                        ws_str=ws_str,
                        ws_path=ws_path,
                        root_real_nc=root_real_nc,
                        trigger_reason=f"同级旧版本 v{ver_num}（已有更新版本 v{max_ver}）",
                        requires_explicit_confirm=False,
                        editable_roots_nc=editable_roots_nc,
                        pinned_abs_nc=pinned_abs_nc,
                        pinned_rel_lower=pinned_rel_lower,
                        warnings=warnings,
                        candidates=candidates,
                        protected_items=protected_items,
                    )
                    if is_dir and emitted is not None and not emitted.protected:
                        old_sibling_dirs_nc.add(_norm_nc(ep))

        # Push remaining child directories (skip already-emitted unprotected old sibling dirs)
        for cdir in reversed(child_dirs):
            if _norm_nc(cdir) in old_sibling_dirs_nc:
                continue
            stack.append(cdir)


def scan_old_versions(
    workspaces: Optional[Sequence[Union[str, os.PathLike]]],
    *,
    include_stale_projects: bool = False,
    stale_days: float = 1.0,
    project_filter: str = "",
    pinned_paths: Iterable[Union[str, os.PathLike]] = (),
    categories: Optional[Iterable[str]] = None,
    editable_roots: Optional[Iterable[Union[str, os.PathLike]]] = None,
    now: Optional[float] = None,
) -> WorkspaceScanReport:
    """Scan workspaces for archivable old versions and protected items."""
    now_ts = time.time() if now is None else float(now)
    warnings: List[str] = []
    valid_ws = _validate_workspaces(workspaces, warnings)

    if editable_roots is None:
        raw_editable = detect_editable_roots()
    else:
        raw_editable = {_norm_real(p) for p in editable_roots if p}
    editable_roots_nc = {os.path.normcase(p) for p in raw_editable}

    candidates: List[ArchiveCandidate] = []
    protected_items: List[ArchiveCandidate] = []
    pin_tuple = tuple(pinned_paths)

    for ws_str, ws_path, root_real_nc in valid_ws:
        pinned_abs_nc, pinned_rel_lower = _resolve_pinned_sets(ws_path, pin_tuple)
        _scan_workspace_tree(
            ws_str,
            ws_path,
            root_real_nc,
            now_ts=now_ts,
            include_stale_projects=include_stale_projects,
            stale_days=stale_days,
            editable_roots_nc=editable_roots_nc,
            pinned_abs_nc=pinned_abs_nc,
            pinned_rel_lower=pinned_rel_lower,
            warnings=warnings,
            candidates=candidates,
            protected_items=protected_items,
        )

    # Apply project_filter and categories filters
    pf = project_filter.strip().lower() if isinstance(project_filter, str) else ""
    allowed_cats: Optional[Set[str]] = None
    if categories is not None:
        allowed_cats = {str(c).strip().lower() for c in categories if str(c).strip()}

    def _keep(item: ArchiveCandidate) -> bool:
        if pf and item.project.lower() != pf:
            return False
        if allowed_cats is not None and item.category.lower() not in allowed_cats:
            return False
        return True

    candidates = [c for c in candidates if _keep(c)]
    protected_items = [p for p in protected_items if _keep(p)]

    return WorkspaceScanReport(
        workspaces=[w[0] for w in valid_ws],
        candidates=candidates,
        protected_items=protected_items,
        triggers=[],
        warnings=warnings,
        scanned_at=_now_iso(now_ts),
    )


# ---------------------------------------------------------------------------
# New-Version Trigger Detector
# ---------------------------------------------------------------------------

def _capture_workspace_snapshot(
    ws_str: str,
    ws_path: Path,
    root_real_nc: str,
    editable_roots_nc: Set[str],
    pinned_abs_nc: Set[str],
    pinned_rel_lower: Set[str],
    warnings: List[str],
) -> Dict[str, Any]:
    """Capture current version state snapshot for all 4 trigger classes in one workspace."""
    versions_dirs: Dict[str, Dict[str, Any]] = {}
    versioned_siblings: Dict[str, Dict[str, Any]] = {}
    releases: Dict[str, Dict[str, Any]] = {}
    task_packs: Dict[str, Dict[str, Any]] = {}

    def _is_item_protected(target: Path, category: str) -> bool:
        rel_posix = _rel_posix(target, ws_path)
        _, _, _, has_keep, has_priv = _safe_measure_path(target, root_real_nc, warnings)
        prot, _ = _check_protection(
            target,
            ws_path,
            rel_posix,
            category=category,
            editable_roots_nc=editable_roots_nc,
            pinned_abs_nc=pinned_abs_nc,
            pinned_rel_lower=pinned_rel_lower,
            has_internal_keep=has_keep,
            has_internal_privacy_ctrl=has_priv,
        )
        return prot

    def _record_versions_dir(vdir: Path) -> None:
        if _is_escaping_symlink(vdir, root_real_nc):
            return
        entries_map: Dict[str, Dict[str, Any]] = {}
        try:
            entries = sorted(os.scandir(vdir), key=lambda e: e.name.lower())
        except OSError:
            return
        for entry in entries:
            ep = Path(entry.path)
            if entry.is_symlink() or not _is_within_root(ep, root_real_nc):
                continue
            if entry.name.lower() == KEEP_MARKER_FILENAME:
                continue
            try:
                st = entry.stat(follow_symlinks=False)
                mtime = float(st.st_mtime)
            except OSError:
                mtime = 0.0
            entries_map[_norm_nc(ep)] = {
                "path": str(ep),
                "name": entry.name,
                "mtime": mtime,
                "protected": _is_item_protected(ep, CATEGORY_VERSIONS_DIR),
            }
        rel_dir = _rel_posix(vdir, ws_path)
        versions_dirs[_norm_nc(vdir)] = {
            "dir_path": str(vdir),
            "project": _derive_project_name(rel_dir, ws_path),
            "entries": entries_map,
        }

    def _record_releases_dir(rdir: Path) -> None:
        if _is_escaping_symlink(rdir, root_real_nc):
            return
        try:
            entries = sorted(os.scandir(rdir), key=lambda e: e.name.lower())
        except OSError:
            return
        grouped: Dict[str, List[Tuple[Tuple[int, int, int, int, Tuple], str, Path]]] = {}
        for entry in entries:
            ep = Path(entry.path)
            if entry.is_symlink() or not _is_within_root(ep, root_real_nc):
                continue
            parsed = _parse_release_semver(entry.name, entry.is_dir(follow_symlinks=False))
            if parsed is None:
                continue
            prefix, sort_key, ver_str = parsed
            grouped.setdefault(prefix, []).append((sort_key, ver_str, ep))

        rel_dir = _rel_posix(rdir, ws_path)
        project = _derive_project_name(rel_dir, ws_path)
        for prefix, items in grouped.items():
            items_sorted = sorted(items, key=lambda x: (x[0], x[2].name.lower()))
            max_key, max_ver_str, newest_path = items_sorted[-1]
            # Convert prerelease tuple to JSON-serializable nested lists
            key_json = [
                max_key[0],
                max_key[1],
                max_key[2],
                max_key[3],
                [[t[0], t[1]] for t in max_key[4]],
            ]
            old_paths = [
                str(ep)
                for sk, _vs, ep in items_sorted
                if sk < max_key and not _is_item_protected(ep, CATEGORY_OLD_RELEASE)
            ]
            gkey = f"{_norm_nc(rdir)}|{prefix}"
            releases[gkey] = {
                "project": project,
                "max_semver": key_json,
                "max_semver_str": max_ver_str,
                "newest_path": str(newest_path),
                "version_count": len({it[0] for it in items_sorted}),
                "old_paths": old_paths,
            }

    def _record_agents_dir(adir: Path) -> None:
        if _is_escaping_symlink(adir, root_real_nc):
            return
        tasks_dir = adir / "tasks"
        if tasks_dir.is_dir() and not _is_escaping_symlink(tasks_dir, root_real_nc):
            try:
                entries = sorted(os.scandir(tasks_dir), key=lambda e: e.name.lower())
            except OSError:
                entries = []
            for entry in entries:
                pack_dir = Path(entry.path)
                if entry.is_symlink() or not _is_within_root(pack_dir, root_real_nc):
                    continue
                if not entry.is_dir(follow_symlinks=False):
                    continue
                plan_path = pack_dir / "PLAN.md"
                completed = False
                done_count = 0
                total_count = 0
                if plan_path.is_file():
                    read_res = _read_bounded_markdown(plan_path, root_real_nc, warnings)
                    if read_res is not None:
                        text, plan_mtime = read_res
                        pack_item = _parse_plan_markdown(
                            text,
                            workspace=ws_str,
                            pack_dir=pack_dir,
                            plan_path=plan_path,
                            plan_mtime=plan_mtime,
                            root_real_nc=root_real_nc,
                        )
                        done_count = pack_item.done_count
                        total_count = pack_item.total_count
                        completed = total_count > 0 and done_count == total_count
                rel_pack = _rel_posix(pack_dir, ws_path)
                task_packs[_norm_nc(pack_dir)] = {
                    "project": _derive_project_name(rel_pack, ws_path),
                    "pack_dir": str(pack_dir),
                    "plan_path": str(plan_path),
                    "completed": completed,
                    "done_count": done_count,
                    "total_count": total_count,
                    "protected": _is_item_protected(pack_dir, CATEGORY_COMPLETED_TASK),
                }

        # Scan nested _versions under .agents (skipping plugins/tasks/checkpoints)
        sub_stack: List[Path] = []
        try:
            for entry in sorted(os.scandir(adir), key=lambda e: e.name.lower()):
                ep = Path(entry.path)
                if entry.is_symlink() or not _is_within_root(ep, root_real_nc):
                    continue
                if entry.is_dir(follow_symlinks=False):
                    if entry.name.lower() not in {"plugins", "tasks", "checkpoints"}:
                        sub_stack.append(ep)
        except OSError:
            return
        while sub_stack:
            cur = sub_stack.pop()
            if cur.name.lower() == "_versions":
                _record_versions_dir(cur)
                continue
            try:
                for entry in sorted(os.scandir(cur), key=lambda e: e.name.lower()):
                    ep = Path(entry.path)
                    if entry.is_symlink() or not _is_within_root(ep, root_real_nc):
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        if entry.name.lower() not in _SKIP_TRAVERSE_DIRS_LOWER:
                            sub_stack.append(ep)
            except OSError:
                continue

    stack: List[Path] = [ws_path]
    while stack:
        cur = stack.pop()
        try:
            entries = sorted(os.scandir(cur), key=lambda e: e.name.lower())
        except OSError:
            continue

        sib_groups: Dict[Tuple[bool, str, str], List[Tuple[int, Path]]] = {}
        child_dirs: List[Path] = []
        for entry in entries:
            ep = Path(entry.path)
            if entry.is_symlink() or not _is_within_root(ep, root_real_nc):
                continue
            elow = entry.name.lower()
            is_dir = entry.is_dir(follow_symlinks=False)
            if is_dir:
                if elow == ".agents":
                    _record_agents_dir(ep)
                    continue
                if elow == "_versions":
                    _record_versions_dir(ep)
                    continue
                if elow == "_releases":
                    _record_releases_dir(ep)
                    continue
                if elow == "_archive" or elow in _SKIP_TRAVERSE_DIRS_LOWER or elow.endswith(".egg-info"):
                    continue
                parsed = _parse_versioned_sibling_name(entry.name, True)
                if parsed is not None:
                    base, vnum, ext = parsed
                    sib_groups.setdefault((True, base.lower(), ext.lower()), []).append((vnum, ep))
                child_dirs.append(ep)
            else:
                parsed = _parse_versioned_sibling_name(entry.name, False)
                if parsed is not None:
                    base, vnum, ext = parsed
                    sib_groups.setdefault((False, base.lower(), ext.lower()), []).append((vnum, ep))

        for (is_dir, base_low, ext_low), items in sib_groups.items():
            items_sorted = sorted(items, key=lambda x: (x[0], x[1].name.lower()))
            max_ver, newest_path = items_sorted[-1]
            distinct_cnt = len({v for v, _ in items_sorted})
            old_paths = [
                str(ep)
                for v, ep in items_sorted
                if v < max_ver and not _is_item_protected(ep, CATEGORY_VERSIONED_SIBLING)
            ]
            rel_newest = _rel_posix(newest_path, ws_path)
            gkey = f"{_norm_nc(cur)}|{'D' if is_dir else 'F'}|{base_low}|{ext_low}"
            versioned_siblings[gkey] = {
                "project": _derive_project_name(rel_newest, ws_path),
                "max_version": max_ver,
                "version_count": distinct_cnt,
                "newest_path": str(newest_path),
                "old_paths": old_paths,
            }

        for cdir in reversed(child_dirs):
            stack.append(cdir)

    return {
        "versions_dirs": versions_dirs,
        "versioned_siblings": versioned_siblings,
        "releases": releases,
        "task_packs": task_packs,
    }


def _semver_json_to_tuple(raw: Any) -> Tuple[int, int, int, int, Tuple[Tuple[int, Any], ...]]:
    if not isinstance(raw, list) or len(raw) < 5:
        return (0, 0, 0, 0, ())
    pre_raw = raw[4] if isinstance(raw[4], list) else []
    pre_tuple = tuple(
        (int(pair[0]), pair[1])
        for pair in pre_raw
        if isinstance(pair, list) and len(pair) == 2
    )
    return (int(raw[0]), int(raw[1]), int(raw[2]), int(raw[3]), pre_tuple)


def _diff_workspace_snapshots(
    ws_str: str,
    prev_ws: Dict[str, Any],
    curr_ws: Dict[str, Any],
    detected_at: str,
) -> List[VersionTriggerEvent]:
    events: List[VersionTriggerEvent] = []

    # 1) _versions/ new snapshot entries
    prev_vdirs = prev_ws.get("versions_dirs") or {}
    curr_vdirs = curr_ws.get("versions_dirs") or {}
    for vdir_key, vdata in curr_vdirs.items():
        prev_entries = (prev_vdirs.get(vdir_key) or {}).get("entries") or {}
        curr_entries = vdata.get("entries") or {}
        added_keys = [k for k in curr_entries.keys() if k not in prev_entries]
        if not added_keys:
            continue
        added_items = sorted(
            (curr_entries[k] for k in added_keys),
            key=lambda x: (float(x.get("mtime", 0.0)), str(x.get("name", "")).lower()),
        )
        newest = added_items[-1]
        newest_path = str(newest.get("path", ""))
        newest_name = str(newest.get("name", ""))
        parsed_v = _parse_versioned_sibling_name(newest_name, False)
        newest_ver = f"v{parsed_v[1]}" if parsed_v else newest_name
        # Candidate paths: all unprotected snapshots in this _versions/ dir
        old_paths = sorted(
            str(e.get("path", ""))
            for e in curr_entries.values()
            if not e.get("protected", False) and e.get("path")
        )
        events.append(
            VersionTriggerEvent(
                kind=TRIGGER_VERSIONS_DIR_SNAPSHOT,
                project=str(vdata.get("project", "")),
                workspace=ws_str,
                newest_path=newest_path,
                newest_version=newest_ver,
                old_candidate_paths=old_paths,
                detected_at=detected_at,
            )
        )

    # 2) versioned_sibling bump (e.g., v1 -> v2, v2 -> v3)
    prev_sibs = prev_ws.get("versioned_siblings") or {}
    curr_sibs = curr_ws.get("versioned_siblings") or {}
    for gkey, gdata in curr_sibs.items():
        if int(gdata.get("version_count", 0)) < 2:
            continue
        curr_max = int(gdata.get("max_version", 0))
        prev_entry = prev_sibs.get(gkey) or {}
        prev_max = int(prev_entry.get("max_version", 0))
        if curr_max > prev_max:
            events.append(
                VersionTriggerEvent(
                    kind=TRIGGER_VERSIONED_SIBLING_BUMP,
                    project=str(gdata.get("project", "")),
                    workspace=ws_str,
                    newest_path=str(gdata.get("newest_path", "")),
                    newest_version=f"v{curr_max}",
                    old_candidate_paths=list(gdata.get("old_paths") or []),
                    detected_at=detected_at,
                )
            )

    # 3) _releases/ semver bump
    prev_rels = prev_ws.get("releases") or {}
    curr_rels = curr_ws.get("releases") or {}
    for gkey, gdata in curr_rels.items():
        if int(gdata.get("version_count", 0)) < 2:
            continue
        curr_key = _semver_json_to_tuple(gdata.get("max_semver"))
        prev_entry = prev_rels.get(gkey)
        prev_key = (
            _semver_json_to_tuple(prev_entry.get("max_semver"))
            if isinstance(prev_entry, dict) and prev_entry.get("max_semver")
            else None
        )
        if prev_key is None or curr_key > prev_key:
            events.append(
                VersionTriggerEvent(
                    kind=TRIGGER_RELEASE_SEMVER_BUMP,
                    project=str(gdata.get("project", "")),
                    workspace=ws_str,
                    newest_path=str(gdata.get("newest_path", "")),
                    newest_version=str(gdata.get("max_semver_str", "")),
                    old_candidate_paths=list(gdata.get("old_paths") or []),
                    detected_at=detected_at,
                )
            )

    # 4) .agents/tasks/<pack> transitioned to completed (done == total > 0)
    prev_packs = prev_ws.get("task_packs") or {}
    curr_packs = curr_ws.get("task_packs") or {}
    for pkey, pdata in curr_packs.items():
        if not pdata.get("completed", False) or pdata.get("protected", False):
            continue
        prev_entry = prev_packs.get(pkey) or {}
        if not prev_entry.get("completed", False):
            done_c = int(pdata.get("done_count", 0))
            tot_c = int(pdata.get("total_count", 0))
            pack_dir_str = str(pdata.get("pack_dir", ""))
            events.append(
                VersionTriggerEvent(
                    kind=TRIGGER_TASK_PACK_COMPLETED,
                    project=str(pdata.get("project", "")),
                    workspace=ws_str,
                    newest_path=pack_dir_str,
                    newest_version=f"{done_c}/{tot_c}",
                    old_candidate_paths=[pack_dir_str] if pack_dir_str else [],
                    detected_at=detected_at,
                )
            )

    return events


def _load_trigger_state(state_path: Path) -> Optional[Dict[str, Any]]:
    if not state_path.is_file():
        return None
    try:
        with open(state_path, "r", encoding="utf-8-sig") as fh:
            data = json.load(fh)
        if isinstance(data, dict) and isinstance(data.get("workspaces"), dict):
            return data
    except (OSError, ValueError):
        return None
    return None


def _save_trigger_state(state_path: Path, payload: Dict[str, Any]) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = state_path.with_suffix(state_path.suffix + f".tmp.{os.getpid()}")
    with open(tmp_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    os.replace(tmp_path, state_path)


def detect_new_version_triggers(
    workspaces: Optional[Sequence[Union[str, os.PathLike]]],
    state_path: Union[str, Path],
    *,
    persist: bool = True,
    pinned_paths: Iterable[Union[str, os.PathLike]] = (),
    editable_roots: Optional[Iterable[Union[str, os.PathLike]]] = None,
    now: Optional[float] = None,
) -> List[VersionTriggerEvent]:
    """Detect newly created version events by diffing workspace snapshots against `state_path`."""
    now_ts = time.time() if now is None else float(now)
    detected_at = _now_iso(now_ts)
    sp = Path(state_path)

    warnings: List[str] = []
    valid_ws = _validate_workspaces(workspaces, warnings)

    if editable_roots is None:
        raw_editable = detect_editable_roots()
    else:
        raw_editable = {_norm_real(p) for p in editable_roots if p}
    editable_roots_nc = {os.path.normcase(p) for p in raw_editable}

    prev_state = _load_trigger_state(sp)
    prev_ws_map: Dict[str, Any] = (
        prev_state.get("workspaces", {}) if isinstance(prev_state, dict) else {}
    )

    new_ws_map: Dict[str, Any] = dict(prev_ws_map)
    events: List[VersionTriggerEvent] = []
    pin_tuple = tuple(pinned_paths)

    for ws_str, ws_path, root_real_nc in valid_ws:
        pinned_abs_nc, pinned_rel_lower = _resolve_pinned_sets(ws_path, pin_tuple)
        curr_snap = _capture_workspace_snapshot(
            ws_str,
            ws_path,
            root_real_nc,
            editable_roots_nc,
            pinned_abs_nc,
            pinned_rel_lower,
            warnings,
        )
        if root_real_nc in prev_ws_map and isinstance(prev_ws_map[root_real_nc], dict):
            events.extend(
                _diff_workspace_snapshots(
                    ws_str,
                    prev_ws_map[root_real_nc],
                    curr_snap,
                    detected_at,
                )
            )
        new_ws_map[root_real_nc] = curr_snap

    if persist:
        _save_trigger_state(
            sp,
            {
                "schema_version": STATE_SCHEMA_VERSION,
                "updated_at": detected_at,
                "workspaces": new_ws_map,
            },
        )

    return events
