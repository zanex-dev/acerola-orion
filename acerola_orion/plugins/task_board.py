# -*- coding: utf-8 -*-
"""
Acerola-Orion 纯 stdlib 只读任务拆解与交接单接力看板引擎 (`acerola_orion/plugins/task_board.py`)
- 严格只读（零写盘、零第三方依赖）：仅白名单扫描 `<ws>/.agents/tasks/<pack>/` 与 `<ws>/.agents/checkpoints/` 下的 `.md`
- 严守 agent-blindfold 隐私隔离：拒绝敏感目录、跳过媒体/隐私文件、阻断逃逸工作区边界的 symlink、单文件硬上限 256 KiB
- 兼容解析 task-decomposer 7 列进度表与 stage-handoff 三类交接单（handoff / cold_rescue / note），并拼装极短剪贴板启动口令
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
import os
from pathlib import Path
import re
import stat
import time
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple, Union


MAX_MD_FILE_BYTES: int = 256 * 1024  # 256 KiB single-file hard cap

# Align with agent-blindfold isolation lists
_SKIP_DIR_NAMES_LOWER: Set[str] = {
    ".user_uploaded",
    "browser_sandbox",
    "tempmediastorage",
    "__pycache__",
    ".git",
    "node_modules",
    "_versions",
}
_SKIP_FILE_NAMES_LOWER: Set[str] = {
    ".allow_media",
    "agent-blindfold.json",
    ".ds_store",
    "thumbs.db",
    "desktop.ini",
}
_SENSITIVE_USER_DIR_NAMES: Tuple[str, ...] = (
    "Pictures",
    "Videos",
    "Music",
    "Desktop",
    "Documents",
)

_H1_RE = re.compile(r"^\s*#\s+(.+?)\s*$")
_H2_RE = re.compile(r"^\s*#{2,3}\s+(.+?)\s*$")
_TABLE_SEP_RE = re.compile(r"^\s*\|?(?:\s*:?-+:?\s*\|)+\s*:?-+:?\s*\|?\s*$")

_PLAN_PACK_ID_RE = re.compile(r"^\s*[-*]\s*任务包\s*ID\s*[:：]\s*(.+?)\s*$", re.IGNORECASE)
_PLAN_CREATED_RE = re.compile(r"^\s*[-*]\s*创建时间\s*[:：]\s*(.+?)\s*$")
_PLAN_GOAL_RE = re.compile(r"^\s*[-*]\s*总目标\s*[:：]\s*(.+?)\s*$")

_CKPT_TIME_RE = re.compile(
    r"(?:落盘时间|生成时间|创建时间)\s*[:：]\s*`?([^|\r\n`]+?)`?(?:\s*\||\s*$)",
    re.MULTILINE,
)
_CKPT_WATERMARK_RE = re.compile(
    r"(?:收口水位|拦截前上下文|当前水位|水位)\s*[:：]\s*`?([^|\r\n`]+?)`?(?:\s*\(|\s*（|\s*\||\s*$)",
    re.MULTILINE,
)
_CKPT_SESSION_RE = re.compile(r"来源会话\s*[:：]\s*`?([0-9a-fA-F-]{6,64})`?")
_DEP_SPLIT_RE = re.compile(r"[,，、;/\s]+")
_NO_DEP_TOKENS: Set[str] = {"", "-", "—", "--", "none", "null", "n/a", "无"}


@dataclass
class TaskCardItem:
    task_id: str
    status: str
    status_symbol: str
    filename: str
    file_path: str
    depends_on: List[str]
    parallel_group: str
    recommended_model: str
    goal: str
    is_ready: bool
    blocked_by: List[str]
    execute_prompt: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class TaskPackItem:
    pack_id: str
    title: str
    workspace: str
    pack_dir: str
    plan_path: str
    created_at: str
    goal: str
    tasks: List[TaskCardItem]
    done_count: int
    in_progress_count: int
    total_count: int
    ready_count: int
    mtime: float

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class CheckpointItem:
    filename: str
    file_path: str
    workspace: str
    kind: str
    title: str
    created_at: str
    watermark: str
    source_session: str
    resume_prompt: str
    mtime: float

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class TaskBoardSnapshot:
    workspaces: List[str]
    packs: List[TaskPackItem]
    checkpoints: List[CheckpointItem]
    warnings: List[str]
    scanned_at: float

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _strip_code_ticks(text: str) -> str:
    s = text.strip()
    if len(s) >= 2 and s.startswith("`") and s.endswith("`"):
        return s[1:-1].strip()
    return s


def _clean_inline_text(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _clean_prompt_path(path_val: Union[str, Path]) -> str:
    raw = os.fspath(path_val) if isinstance(path_val, os.PathLike) else str(path_val or "")
    s = raw.strip().replace("\r", "").replace("\n", "")
    if not s:
        return ""
    try:
        return os.path.abspath(os.path.expanduser(s))
    except (OSError, ValueError):
        return s


def _format_mtime(epoch: float) -> str:
    if epoch <= 0:
        return ""
    try:
        return datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M")
    except (OSError, OverflowError, ValueError):
        return ""


def _collect_blocked_roots() -> Tuple[Set[str], Set[str]]:
    home_variants: List[str] = []
    try:
        h_abs = os.path.abspath(str(Path.home()))
        home_variants.append(h_abs)
        h_real = os.path.realpath(h_abs)
        if os.path.normcase(h_real) != os.path.normcase(h_abs):
            home_variants.append(h_real)
    except Exception:
        return set(), set()

    exact: Set[str] = set()
    subtree: Set[str] = set()
    for hv in home_variants:
        hv_clean = hv.rstrip("\\/") or hv
        exact.add(os.path.normcase(hv_clean))
        for name in _SENSITIVE_USER_DIR_NAMES:
            subtree.add(os.path.normcase(os.path.abspath(os.path.join(hv, name)).rstrip("\\/")))
            subtree.add(os.path.normcase(os.path.abspath(os.path.join(hv, "OneDrive", name)).rstrip("\\/")))
    return exact, subtree


def _is_sensitive_workspace(abs_path: str) -> Optional[str]:
    if os.path.dirname(abs_path) == abs_path:
        return f"跳过文件系统根目录: {abs_path}"
    exact, subtree = _collect_blocked_roots()
    candidates = [abs_path]
    try:
        rp = os.path.realpath(abs_path)
        if os.path.normcase(rp) != os.path.normcase(abs_path):
            candidates.append(rp)
    except OSError:
        pass
    sep = os.sep
    for cand in candidates:
        c_clean = cand.rstrip("\\/") or cand
        c_nc = os.path.normcase(c_clean)
        if c_nc in exact:
            return f"隐私隔离：跳过用户主目录根: {abs_path}"
        for b_nc in subtree:
            if c_nc == b_nc or c_nc.startswith(b_nc + sep):
                return f"隐私隔离：跳过个人敏感目录: {abs_path}"
        parts_lower = {p.lower() for p in Path(c_clean).parts}
        if parts_lower & {".user_uploaded", "tempmediastorage", "browser_sandbox"}:
            return f"隐私隔离：跳过隐私媒体/沙箱目录: {abs_path}"
    return None


def _is_within_root(target: Path, root_real_nc: str) -> bool:
    """Return True iff `realpath(target)` stays strictly inside (or equal to) `root_real_nc`."""
    try:
        resolved = os.path.realpath(str(target))
    except OSError:
        return False
    res_clean = resolved.rstrip("\\/") or resolved
    res_nc = os.path.normcase(res_clean)
    return res_nc == root_real_nc or res_nc.startswith(root_real_nc + os.sep)


def _read_bounded_markdown(
    file_path: Path,
    root_real_nc: str,
    warnings: List[str],
) -> Optional[Tuple[str, float]]:
    """
    Safely read a `.md` file strictly within `root_real_nc` and `<= MAX_MD_FILE_BYTES`.
    Returns `(text, mtime)` or `None` when skipped/blocked.
    """
    name_lower = file_path.name.lower()
    if name_lower in _SKIP_FILE_NAMES_LOWER or not name_lower.endswith(".md"):
        return None
    if not _is_within_root(file_path, root_real_nc):
        warnings.append(f"安全拦截：跳过逃逸工作区边界的符号链接文件: {file_path}")
        return None
    try:
        st = os.stat(file_path)
    except OSError as exc:
        warnings.append(f"无法读取文件元信息 {file_path}: {exc}")
        return None
    if not stat.S_ISREG(st.st_mode):
        return None
    if st.st_size > MAX_MD_FILE_BYTES:
        warnings.append(
            f"跳过超大 Markdown 文件（{st.st_size} bytes > {MAX_MD_FILE_BYTES} bytes）: {file_path}"
        )
        return None
    try:
        with open(file_path, "rb") as fh:
            raw = fh.read(MAX_MD_FILE_BYTES + 1)
    except OSError as exc:
        warnings.append(f"读取文件失败 {file_path}: {exc}")
        return None
    if len(raw) > MAX_MD_FILE_BYTES:
        warnings.append(f"跳过超大 Markdown 文件（读取超 {MAX_MD_FILE_BYTES} bytes）: {file_path}")
        return None
    return raw.decode("utf-8-sig", errors="replace"), float(st.st_mtime)


# ---------------------------------------------------------------------------
# Prompt builders (coupled with task-decomposer & stage-handoff skills)
# ---------------------------------------------------------------------------

def build_decompose_prompt(workspace: Union[str, Path], requirement: str = "") -> str:
    """Build a concise one-shot prompt to trigger `task-decomposer` read-only planning."""
    ws = _clean_prompt_path(workspace)
    req = _clean_inline_text(requirement)
    if req:
        return f"请激活 task-decomposer 技能（只读不改源码），在工作区 {ws} 拆解需求落盘任务包：{req}"
    return f"请激活 task-decomposer 技能（只读不改源码），在工作区 {ws} 梳理需求落盘任务包（完成后停下来交给我）。"


def build_execute_task_prompt(pack_dir: Union[str, Path], task_id: str) -> str:
    """Build the standard single-step `task-decomposer` execution prompt for a specific card."""
    p_dir = _clean_prompt_path(pack_dir)
    tid = _clean_inline_text(task_id) or "T01"
    return f"请激活 task-decomposer 技能，执行任务包 {p_dir} 的 {tid}（完成后停下来问我）。"


def build_handoff_prompt(workspace: Union[str, Path], topic: str = "") -> str:
    """Build a concise prompt to trigger `stage-handoff` checkpoint creation."""
    ws = _clean_prompt_path(workspace)
    ckpt_dir = os.path.join(ws, ".agents", "checkpoints") if ws else ".agents/checkpoints"
    t = _clean_inline_text(topic)
    if t:
        return f"请激活 stage-handoff 技能，落盘交接单至 {ckpt_dir}（主题：{t}，落盘后立即收口）。"
    return f"请激活 stage-handoff 技能，落盘交接单至 {ckpt_dir}（落盘后立即收口）。"


def build_resume_checkpoint_prompt(checkpoint_path: Union[str, Path], extracted_prompt: str = "") -> str:
    """
    Return the extracted `## 4.` blockquote resume prompt when present;
    otherwise build a clean fallback prompt referencing `checkpoint_path`.
    """
    cleaned = _clean_inline_text(re.sub(r"^\s*>\s*", "", str(extracted_prompt or "")))
    if cleaned:
        return cleaned
    ckpt = _clean_prompt_path(checkpoint_path)
    return (
        f"请先读取交接单 `{ckpt}`，严格承接其中的结论、关键文件锚点与硬约束，"
        f"直接继续执行下一阶段目标。"
    )


# ---------------------------------------------------------------------------
# PLAN.md parser
# ---------------------------------------------------------------------------

def _parse_status_cell(cell: str) -> Tuple[str, str]:
    s = cell.strip()
    low = s.lower()
    if low in ("[x]", "x", "✅", "done", "已完成") or "[x]" in low:
        return "done", "[x]"
    if low in ("[~]", "~", "[-]", "🔄", "in_progress", "进行中") or "[~]" in low:
        return "in_progress", "[~]"
    return "todo", "[ ]"


def _parse_depends_cell(cell: str) -> List[str]:
    s = _strip_code_ticks(cell).strip()
    if s.lower() in _NO_DEP_TOKENS:
        return []
    deps: List[str] = []
    seen: Set[str] = set()
    for part in _DEP_SPLIT_RE.split(s):
        tok = _strip_code_ticks(part).strip()
        if tok.lower() in _NO_DEP_TOKENS:
            continue
        key = tok.upper()
        if key not in seen:
            seen.add(key)
            deps.append(tok)
    return deps


def _split_md_table_row(line: str) -> List[str]:
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in s.split("|")]


def _parse_plan_markdown(
    text: str,
    *,
    workspace: str,
    pack_dir: Path,
    plan_path: Path,
    plan_mtime: float,
    root_real_nc: str,
) -> TaskPackItem:
    lines = text.splitlines()
    title = ""
    pack_id = ""
    created_at = ""
    goal = ""

    for line in lines:
        if not title:
            m_h1 = _H1_RE.match(line)
            if m_h1:
                raw_t = m_h1.group(1).strip()
                title = re.sub(r"^任务包\s*[:：]\s*", "", raw_t).strip() or raw_t
                continue
        if not pack_id:
            m_id = _PLAN_PACK_ID_RE.match(line)
            if m_id:
                pack_id = _strip_code_ticks(m_id.group(1))
                continue
        if not created_at:
            m_ct = _PLAN_CREATED_RE.match(line)
            if m_ct:
                created_at = _strip_code_ticks(m_ct.group(1))
                continue
        if not goal:
            m_g = _PLAN_GOAL_RE.match(line)
            if m_g:
                goal = m_g.group(1).strip()
                continue

    if not pack_id:
        pack_id = pack_dir.name
    if not title:
        title = pack_id
    if not created_at:
        created_at = _format_mtime(plan_mtime)

    # Locate the 7-column progress table (状态 | 编号 | 任务卡 | 依赖 | 并行组 | 建议模型 | 一句话目标)
    raw_rows: List[Tuple[str, str, str, str, List[str], str, str, str]] = []
    in_table = False
    header_seen = False
    for line in lines:
        stripped = line.strip()
        if not in_table:
            if stripped.startswith("|") and ("状态" in stripped) and ("编号" in stripped):
                in_table = True
                header_seen = True
                continue
        else:
            if not stripped.startswith("|"):
                break
            if header_seen and _TABLE_SEP_RE.match(stripped):
                header_seen = False
                continue
            header_seen = False
            cells = _split_md_table_row(stripped)
            if len(cells) < 3:
                continue
            # Pad or merge overflow columns into the last goal column
            while len(cells) < 7:
                cells.append("")
            if len(cells) > 7:
                cells = cells[:6] + [" | ".join(cells[6:])]

            status, symbol = _parse_status_cell(cells[0])
            task_id = _strip_code_ticks(cells[1]).strip()
            if not task_id:
                continue
            filename = _strip_code_ticks(cells[2]).strip()
            deps = _parse_depends_cell(cells[3])
            pgroup = _strip_code_ticks(cells[4]).strip() or "-"
            model = _strip_code_ticks(cells[5]).strip() or "-"
            card_goal = cells[6].strip()
            raw_rows.append((task_id, status, symbol, filename, deps, pgroup, model, card_goal))

    done_ids_upper: Set[str] = {r[0].upper() for r in raw_rows if r[1] == "done"}
    max_mtime = plan_mtime
    pack_dir_str = str(pack_dir)
    tasks: List[TaskCardItem] = []

    for task_id, status, symbol, filename, deps, pgroup, model, card_goal in raw_rows:
        card_file_str = ""
        # Resolve card file path safely (disallow path traversal / escaping symlinks)
        if filename and ("/" not in filename) and ("\\" not in filename) and (".." not in filename):
            cand = pack_dir / filename
            if _is_within_root(cand, root_real_nc):
                card_file_str = str(cand)
                try:
                    st = os.stat(cand)
                    if stat.S_ISREG(st.st_mode) and st.st_mtime > max_mtime:
                        max_mtime = float(st.st_mtime)
                except OSError:
                    pass

        blocked_by = [d for d in deps if d.upper() not in done_ids_upper]
        is_ready = (status != "done") and (len(blocked_by) == 0)
        tasks.append(
            TaskCardItem(
                task_id=task_id,
                status=status,
                status_symbol=symbol,
                filename=filename,
                file_path=card_file_str,
                depends_on=deps,
                parallel_group=pgroup,
                recommended_model=model,
                goal=card_goal,
                is_ready=is_ready,
                blocked_by=blocked_by,
                execute_prompt=build_execute_task_prompt(pack_dir_str, task_id),
            )
        )

    done_count = sum(1 for t in tasks if t.status == "done")
    in_progress_count = sum(1 for t in tasks if t.status == "in_progress")
    ready_count = sum(1 for t in tasks if t.is_ready)

    return TaskPackItem(
        pack_id=pack_id,
        title=title,
        workspace=workspace,
        pack_dir=pack_dir_str,
        plan_path=str(plan_path),
        created_at=created_at,
        goal=goal,
        tasks=tasks,
        done_count=done_count,
        in_progress_count=in_progress_count,
        total_count=len(tasks),
        ready_count=ready_count,
        mtime=max_mtime,
    )


# ---------------------------------------------------------------------------
# Checkpoint (.agents/checkpoints/*.md) parser
# ---------------------------------------------------------------------------

def _extract_checkpoint_section4_prompt(lines: List[str]) -> str:
    """
    Extract the resume instruction under `## 4.` (or a heading mentioning 启动指令):
    prefers `> ` blockquote lines; falls back to a fenced code block inside that section.
    """
    in_sec = False
    sec_lines: List[str] = []
    for line in lines:
        m_h2 = _H2_RE.match(line)
        if m_h2:
            heading = m_h2.group(1).strip()
            if not in_sec:
                if heading.startswith("4.") or heading.startswith("4 ") or ("启动指令" in heading):
                    in_sec = True
                    continue
            else:
                break
        elif in_sec:
            sec_lines.append(line)

    if not sec_lines:
        return ""

    # 1) Prefer blockquote (`> ...`) lines
    quote_parts: List[str] = []
    for ln in sec_lines:
        stripped = ln.strip()
        if stripped.startswith(">"):
            body = stripped[1:].strip()
            if body:
                quote_parts.append(body)
        elif quote_parts:
            break
    if quote_parts:
        return " ".join(quote_parts).strip()

    # 2) Fallback: first fenced code block inside Section 4
    in_fence = False
    fence_parts: List[str] = []
    for ln in sec_lines:
        stripped = ln.strip()
        if stripped.startswith("```"):
            if in_fence:
                break
            in_fence = True
            continue
        if in_fence and stripped:
            fence_parts.append(stripped)
    if fence_parts:
        return " ".join(fence_parts).strip()

    return ""


def _classify_checkpoint_kind(filename: str, raw_h1: str) -> str:
    fn_low = filename.lower()
    if fn_low.startswith("cold_rescue") or ("冷窗" in raw_h1 and "急救" in raw_h1):
        return "cold_rescue"
    if fn_low.startswith("handoff") or ("交接单" in raw_h1):
        return "handoff"
    return "note"


def _parse_checkpoint_markdown(
    text: str,
    *,
    workspace: str,
    file_path: Path,
    mtime: float,
) -> CheckpointItem:
    lines = text.splitlines()
    raw_h1 = ""
    for line in lines:
        m_h1 = _H1_RE.match(line)
        if m_h1:
            raw_h1 = m_h1.group(1).strip()
            break

    kind = _classify_checkpoint_kind(file_path.name, raw_h1)
    if raw_h1:
        title = re.sub(r"^(?:阶段交接单|交接单)\s*[:：]\s*", "", raw_h1).strip() or raw_h1
    else:
        title = file_path.stem

    # Scan top metadata lines (first 20 lines) for time, watermark & source session
    head_block = "\n".join(lines[:20])
    m_time = _CKPT_TIME_RE.search(head_block)
    created_at = _strip_code_ticks(m_time.group(1)) if m_time else _format_mtime(mtime)

    m_wm = _CKPT_WATERMARK_RE.search(head_block)
    watermark = _strip_code_ticks(m_wm.group(1)) if m_wm else ""

    m_sess = _CKPT_SESSION_RE.search(head_block)
    source_session = _strip_code_ticks(m_sess.group(1)) if m_sess else ""

    extracted_prompt = _extract_checkpoint_section4_prompt(lines)
    resume_prompt = build_resume_checkpoint_prompt(file_path, extracted_prompt)

    return CheckpointItem(
        filename=file_path.name,
        file_path=str(file_path),
        workspace=workspace,
        kind=kind,
        title=title,
        created_at=created_at,
        watermark=watermark,
        source_session=source_session,
        resume_prompt=resume_prompt,
        mtime=mtime,
    )


# ---------------------------------------------------------------------------
# Main read-only whitelist scanner
# ---------------------------------------------------------------------------

def scan_task_board(
    workspaces: Sequence[Union[str, Path]],
    max_checkpoints: int = 30,
) -> TaskBoardSnapshot:
    """
    Strictly read-only whitelist scan across `workspaces` for:
      - `<ws>/.agents/tasks/<pack>/PLAN.md` (+ referenced `T0x_*.md` mtime check)
      - `<ws>/.agents/checkpoints/*.md`
    Enforces agent-blindfold privacy boundaries, non-escaping symlink verification,
    `.md`-only & `<= 256 KiB` cap, and descending `mtime` sorting.
    """
    try:
        ckpt_limit = max(0, int(max_checkpoints))
    except (TypeError, ValueError):
        ckpt_limit = 30

    warnings: List[str] = []
    valid_workspaces: List[str] = []
    packs: List[TaskPackItem] = []
    checkpoints: List[CheckpointItem] = []
    seen_ws: Set[str] = set()

    if isinstance(workspaces, (str, bytes, os.PathLike)):
        warnings.append("workspaces 参数应为路径序列而非单个路径")
        return TaskBoardSnapshot(
            workspaces=[],
            packs=[],
            checkpoints=[],
            warnings=warnings,
            scanned_at=time.time(),
        )

    for raw_ws in workspaces:
        if isinstance(raw_ws, os.PathLike):
            raw_str = os.fspath(raw_ws)
        elif isinstance(raw_ws, str):
            raw_str = raw_ws
        else:
            warnings.append(f"跳过无效工作区项: {raw_ws!r}")
            continue
        if not isinstance(raw_str, str) or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in raw_str):
            warnings.append(f"跳过包含非法控制字符的工作区路径: {raw_str!r}")
            continue
        s = raw_str.strip()
        if not s:
            continue
        try:
            ws_abs = os.path.abspath(os.path.expanduser(s))
        except (OSError, ValueError) as exc:
            warnings.append(f"无效工作区路径 {s}: {exc}")
            continue

        block_reason = _is_sensitive_workspace(ws_abs)
        if block_reason:
            warnings.append(block_reason)
            continue

        ws_key = os.path.normcase(ws_abs)
        if ws_key in seen_ws:
            continue
        seen_ws.add(ws_key)

        ws_path = Path(ws_abs)
        if not ws_path.is_dir():
            warnings.append(f"工作区目录不存在或不可访问: {ws_abs}")
            continue

        valid_workspaces.append(ws_abs)
        ws_real = os.path.realpath(ws_abs).rstrip("\\/") or os.path.realpath(ws_abs)
        ws_real_nc = os.path.normcase(ws_real)

        # 1) Scan <ws>/.agents/tasks/<pack>/PLAN.md
        tasks_root = ws_path / ".agents" / "tasks"
        if tasks_root.exists():
            if not _is_within_root(tasks_root, ws_real_nc):
                warnings.append(f"安全拦截：跳过逃逸工作区边界的 tasks 目录: {tasks_root}")
            elif tasks_root.is_dir():
                try:
                    entries = list(os.scandir(tasks_root))
                except OSError as exc:
                    warnings.append(f"扫描 tasks 目录失败 {tasks_root}: {exc}")
                    entries = []
                for entry in entries:
                    if entry.name.startswith(".") or entry.name.lower() in _SKIP_DIR_NAMES_LOWER:
                        continue
                    pack_dir = Path(entry.path)
                    if not _is_within_root(pack_dir, ws_real_nc):
                        warnings.append(f"安全拦截：跳过逃逸工作区边界的任务包目录: {pack_dir}")
                        continue
                    try:
                        if not pack_dir.is_dir():
                            continue
                    except OSError:
                        continue
                    plan_path = pack_dir / "PLAN.md"
                    if not plan_path.exists():
                        continue
                    read_res = _read_bounded_markdown(plan_path, ws_real_nc, warnings)
                    if read_res is None:
                        continue
                    plan_text, plan_mtime = read_res
                    packs.append(
                        _parse_plan_markdown(
                            plan_text,
                            workspace=ws_abs,
                            pack_dir=pack_dir,
                            plan_path=plan_path,
                            plan_mtime=plan_mtime,
                            root_real_nc=ws_real_nc,
                        )
                    )

        # 2) Scan <ws>/.agents/checkpoints/*.md
        ckpt_root = ws_path / ".agents" / "checkpoints"
        if ckpt_root.exists():
            if not _is_within_root(ckpt_root, ws_real_nc):
                warnings.append(f"安全拦截：跳过逃逸工作区边界的 checkpoints 目录: {ckpt_root}")
            elif ckpt_root.is_dir():
                try:
                    entries = list(os.scandir(ckpt_root))
                except OSError as exc:
                    warnings.append(f"扫描 checkpoints 目录失败 {ckpt_root}: {exc}")
                    entries = []
                for entry in entries:
                    name_low = entry.name.lower()
                    if name_low in _SKIP_FILE_NAMES_LOWER or not name_low.endswith(".md"):
                        continue
                    ckpt_path = Path(entry.path)
                    read_res = _read_bounded_markdown(ckpt_path, ws_real_nc, warnings)
                    if read_res is None:
                        continue
                    ckpt_text, ckpt_mtime = read_res
                    checkpoints.append(
                        _parse_checkpoint_markdown(
                            ckpt_text,
                            workspace=ws_abs,
                            file_path=ckpt_path,
                            mtime=ckpt_mtime,
                        )
                    )

    packs.sort(key=lambda p: (p.mtime, p.pack_id), reverse=True)
    checkpoints.sort(key=lambda c: (c.mtime, c.filename), reverse=True)
    if ckpt_limit == 0:
        checkpoints = []
    elif len(checkpoints) > ckpt_limit:
        checkpoints = checkpoints[:ckpt_limit]

    return TaskBoardSnapshot(
        workspaces=valid_workspaces,
        packs=packs,
        checkpoints=checkpoints,
        warnings=warnings,
        scanned_at=time.time(),
    )
