# -*- coding: utf-8 -*-
"""
Antigravity Global Lifecycle Hook: EOQ Context Guard
(`python -m acerola_orion.plugins.eoq_hook --event PreInvocation|PreToolUse|PostToolUse`).

Core design:
- Pure Python stdlib only (~35-50ms startup, zero third-party deps, fail-open on unexpected errors).
- Shares `.eoq_guard_state.json` protocol (`EOQ_GUARD_STATE`) with `acerola_orion.plugins.eoq_guard`.
- Short-circuits immediately when `ACEROLA_ORION_DISABLE_PLUGINS=1` or `ACEROLA_ORION_DISABLED_PLUGINS`
  contains `eoq_guard`, preserving the bare-core zero-intrusion contract.
- Strictly reads only the `.jsonl` transcript file supplied by hook `transcriptPath` / `conversationId`
  (never scans or reads binary media directories).
- Three physical guardrails calibrated to empirical EOQ ledger math:
  1. Cold Window Gate (`PreInvocation`): blocks user turn on cold (>250s since last `PLANNER_RESPONSE`)
     windows above `cold_block_tokens` BEFORE any LLM API call fires, and writes a 0-Token local
     rescue draft to `.agents/checkpoints/cold_rescue_*.md`.
  2. Hot Window Odometer (`PreInvocation`): zero base-prompt bloat below threshold; one-shot per band
     injection at `hot_nudge_tokens` (95K sweet spot) and `cliff_warn_tokens` (175K pre-2x cliff),
     with automatic reset after `/compact`.
  3. C-Tier Anti-Runaway Cliff Lock (`PreToolUse` + `PostToolUse`): returns `force_ask` on
     context-bloating tools when `prompt_tokens >= cliff_tool_lock_tokens` (180K), always allows
     checkpoint writes, and auto-promotes window to unrestricted when the user clicks Allow.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import sys
import time
from typing import Any, Dict, List, Optional, Tuple
import urllib.parse

EOQ_GUARD_CONFIG_ENV = "EOQ_GUARD_CONFIG"
EOQ_GUARD_STATE_ENV = "EOQ_GUARD_STATE"
CONFIG_DEFAULT_PATH = Path.home() / ".gemini" / "config" / "eoq_guard_config.json"
STATE_DEFAULT_PATH = Path.home() / ".gemini" / "config" / ".eoq_guard_state.json"

TAIL_STAGES_BYTES: Tuple[int, ...] = (32 * 1024, 128 * 1024, 512 * 1024)
RESCUE_READ_BYTES = 512 * 1024
STATE_RETENTION_SEC = 7 * 86400
STATE_MAX_ENTRIES = 200
TIER_2X_THRESHOLD = 200_000

DEFAULT_CONFIG: Dict[str, Any] = {
    "enabled": True,
    "cold_ttl_sec": 250,
    "cold_block_tokens": 30000,
    "cold_auto_rescue_draft": True,
    "hot_nudge_tokens": 95000,
    "cliff_warn_tokens": 175000,
    "cliff_tool_lock_tokens": 180000,
    "compact_reset_drop_tokens": 40000,
    "exempt_workspace_names": ["chat", "tavern"],
    "chat_workspace_mode": "off",
    "show_user_ribbon": True,
    "bypass_tokens": ["!go", "!force", "/compact"],
    "cliff_allow_unlocks_window": True,
    "window_unlock_tokens": [
        "!unlock",
        "!nolimit",
        "!unlimit",
        "!force",
        "!一直做",
        "!免限",
        "解锁本窗",
        "本窗免限",
    ],
    "window_lock_tokens": [
        "!lock",
        "!relock",
        "锁定本窗",
        "恢复本窗锁",
    ],
    "exempt_input_prefixes": ["保活", "续杯", "取消保活", "停止保活", "[KV-KeepAlive"],
    "cliff_locked_tools": [
        "read_file",
        "run_command",
        "search_web",
        "call_mcp_tool",
        "invoke_subagent",
        "run_workflow",
        "read_resource",
    ],
    "cliff_allow_write_dir_markers": [
        ".agents/checkpoints",
        ".gemini/antigravity/brain",
    ],
    "handoff_dir_rel": ".agents/checkpoints",
    "handoff_max_chars": 2400,
}

# Win32 shared-read constants
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
_GENERIC_READ = 0x80000000
_FILE_SHARE_ALL = 0x00000001 | 0x00000002 | 0x00000004
_OPEN_EXISTING = 3
_FILE_ATTRIBUTE_NORMAL = 0x80
_FILE_BEGIN = 0
_KERNEL32: Any = None


def _is_eoq_plugin_disabled() -> bool:
    """Return True if global or per-plugin environment flags disable `eoq_guard`."""
    disable_all = os.environ.get("ACEROLA_ORION_DISABLE_PLUGINS", "").strip().lower()
    if disable_all in ("1", "true", "yes", "on"):
        return True
    raw_disabled = os.environ.get("ACEROLA_ORION_DISABLED_PLUGINS", "").strip()
    if raw_disabled:
        tokens = {
            tok.strip().lower()
            for tok in re.split(r"[,;\s]+", raw_disabled)
            if tok and tok.strip()
        }
        if tokens & {"eoq_guard", "eoq_hook", "*", "all"}:
            return True
    return False


def _get_kernel32() -> Any:
    global _KERNEL32
    if sys.platform != "win32":
        return None
    if _KERNEL32 is not None:
        return _KERNEL32
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateFileW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        k32.CreateFileW.restype = wintypes.HANDLE
        k32.GetFileSizeEx.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.LARGE_INTEGER)]
        k32.GetFileSizeEx.restype = wintypes.BOOL
        k32.SetFilePointerEx.argtypes = [
            wintypes.HANDLE,
            wintypes.LARGE_INTEGER,
            ctypes.POINTER(wintypes.LARGE_INTEGER),
            wintypes.DWORD,
        ]
        k32.SetFilePointerEx.restype = wintypes.BOOL
        k32.ReadFile.argtypes = [
            wintypes.HANDLE,
            wintypes.LPVOID,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            wintypes.LPVOID,
        ]
        k32.ReadFile.restype = wintypes.BOOL
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        k32.CloseHandle.restype = wintypes.BOOL
        _KERNEL32 = k32
    except Exception:
        _KERNEL32 = None
    return _KERNEL32


def read_shared_tail_bytes(path: Path, max_bytes: int) -> Tuple[bytes, bool, int]:
    """
    Read up to `max_bytes` from EOF without blocking concurrent writers on Windows.
    Returns `(raw_bytes, truncated_head, file_size)`.
    """
    k32 = _get_kernel32()
    if k32 is not None:
        h = k32.CreateFileW(
            str(path),
            _GENERIC_READ,
            _FILE_SHARE_ALL,
            None,
            _OPEN_EXISTING,
            _FILE_ATTRIBUTE_NORMAL,
            None,
        )
        if h and h != _INVALID_HANDLE_VALUE:
            try:
                sz = wintypes.LARGE_INTEGER(0)
                if not k32.GetFileSizeEx(h, ctypes.byref(sz)):
                    return b"", False, 0
                file_size = max(0, int(sz.value))
                if file_size == 0:
                    return b"", False, 0
                to_read = min(file_size, max(1, int(max_bytes)))
                offset = file_size - to_read
                if offset > 0:
                    new_pos = wintypes.LARGE_INTEGER(0)
                    if not k32.SetFilePointerEx(h, wintypes.LARGE_INTEGER(offset), ctypes.byref(new_pos), _FILE_BEGIN):
                        return b"", False, file_size
                buf = ctypes.create_string_buffer(to_read)
                bytes_read = wintypes.DWORD(0)
                if not k32.ReadFile(h, buf, to_read, ctypes.byref(bytes_read), None):
                    return b"", offset > 0, file_size
                return buf.raw[: int(bytes_read.value)], offset > 0, file_size
            finally:
                k32.CloseHandle(h)

    # Cross-platform or Win32 fallback
    try:
        file_size = path.stat().st_size
        if file_size <= 0:
            return b"", False, 0
        to_read = min(file_size, max(1, int(max_bytes)))
        offset = file_size - to_read
        with open(path, "rb") as f:
            if offset > 0:
                f.seek(offset)
            data = f.read(to_read)
        return data, offset > 0, file_size
    except OSError:
        return b"", False, 0


def _emit(payload: Dict[str, Any]) -> None:
    text = json.dumps(payload, ensure_ascii=False) + "\n"
    buf = getattr(sys.stdout, "buffer", None)
    if buf is not None:
        buf.write(text.encode("utf-8"))
        buf.flush()
    else:
        sys.stdout.write(text)
        sys.stdout.flush()


def _read_stdin_bytes() -> bytes:
    buf = getattr(sys.stdin, "buffer", None)
    if buf is not None:
        return buf.read()
    raw = sys.stdin.read()
    if isinstance(raw, bytes):
        return raw
    return str(raw or "").encode("utf-8")


def _safe_nonneg_int(val: Any) -> int:
    if val is None or isinstance(val, bool):
        return 0
    try:
        f = float(val)
    except (TypeError, ValueError):
        return 0
    if not math.isfinite(f) or f <= 0:
        return 0
    return int(f)


def _parse_iso_epoch(iso_str: Any) -> float:
    if not isinstance(iso_str, str):
        return 0.0
    s = iso_str.strip()
    if not s:
        return 0.0
    try:
        if s.endswith(("Z", "z")):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        ep = dt.timestamp()
        return ep if math.isfinite(ep) else 0.0
    except Exception:
        return 0.0


def format_tokens_k(tokens: int) -> str:
    n = max(0, int(tokens))
    if n < 1000:
        return f"{n}"
    if n >= 999_500:
        return f"{n / 1_000_000.0:.1f}M"
    return f"{int(round(n / 1000.0))}K"


def format_duration_cn(seconds: float) -> str:
    sec = max(0, int(round(seconds)))
    if sec < 60:
        return f"{sec}秒"
    mins, rem = divmod(sec, 60)
    if mins < 60:
        return f"{mins}分{rem:02d}秒" if rem else f"{mins}分钟"
    hrs, rem_m = divmod(mins, 60)
    return f"{hrs}小时{rem_m}分"


def _normalize_path_str(raw: str) -> str:
    if not raw or not isinstance(raw, str):
        return ""
    s = urllib.parse.unquote(raw.strip().strip('"').strip("'"))
    s = re.sub(r"^file:///+", "", s, flags=re.IGNORECASE)
    return s.replace("\\", "/").rstrip("/")


_DEFAULT_EXEMPT_WS_NAMES: Tuple[str, ...] = ("chat", "tavern")
_ROUTER_CHAT_ONLY_RE = re.compile(r"router-tier\s*:\s*chat-only", re.IGNORECASE)


def _get_exempt_workspace_names(cfg: Optional[Dict[str, Any]] = None) -> Tuple[str, ...]:
    if isinstance(cfg, dict):
        raw_list = cfg.get("exempt_workspace_names")
        if isinstance(raw_list, (list, tuple)):
            names = tuple(
                str(x).strip().strip("/\\").lower()
                for x in raw_list
                if isinstance(x, str) and str(x).strip().strip("/\\")
            )
            if names:
                return names
    return _DEFAULT_EXEMPT_WS_NAMES


def _has_chat_only_marker(norm_slash_path: str) -> bool:
    if not norm_slash_path:
        return False
    base = Path(norm_slash_path)
    for fname in ("AGENTS.md", "GEMINI.md"):
        rp = base / fname
        try:
            if rp.is_file():
                with open(rp, "rb") as fh:
                    head = fh.read(512).decode("utf-8", errors="ignore")
                if _ROUTER_CHAT_ONLY_RE.search(head):
                    return True
        except OSError:
            continue
    return False


def _is_exempt_workspace_path(raw: str, cfg: Optional[Dict[str, Any]] = None) -> bool:
    norm = _normalize_path_str(raw)
    if not norm:
        return False
    norm_low = norm.lower()
    for ws_name in _get_exempt_workspace_names(cfg):
        suffix = f"/workspaces/{ws_name}"
        if norm_low.endswith(suffix) or (suffix + "/") in (norm_low + "/"):
            return True
    return _has_chat_only_marker(norm)


def _is_chat_workspace_path(raw: str) -> bool:
    return _is_exempt_workspace_path(raw)


def is_exempt_workspace(payload: Dict[str, Any], cfg: Optional[Dict[str, Any]] = None) -> bool:
    cwd_raw = str(payload.get("cwd") or "").strip()
    ws_raw_list = [str(ws or "").strip() for ws in (payload.get("workspacePaths") or []) if str(ws or "").strip()]
    if cwd_raw and _is_exempt_workspace_path(cwd_raw, cfg):
        return True
    for ws in ws_raw_list:
        if _is_exempt_workspace_path(ws, cfg):
            return True
    if not cwd_raw and not ws_raw_list:
        if _is_exempt_workspace_path(os.getcwd(), cfg) or _is_exempt_workspace_path(os.environ.get("PWD", ""), cfg):
            return True
    return False


def is_chat_workspace(payload: Dict[str, Any], cfg: Optional[Dict[str, Any]] = None) -> bool:
    return is_exempt_workspace(payload, cfg)


def _config_path() -> Path:
    override = os.environ.get(EOQ_GUARD_CONFIG_ENV, "").strip()
    return Path(override) if override else CONFIG_DEFAULT_PATH


def _state_path() -> Path:
    override = os.environ.get(EOQ_GUARD_STATE_ENV, "").strip()
    return Path(override) if override else STATE_DEFAULT_PATH


def load_config() -> Dict[str, Any]:
    cfg = dict(DEFAULT_CONFIG)
    p = _config_path()
    if p.is_file():
        try:
            raw = json.loads(p.read_text(encoding="utf-8-sig", errors="replace"))
            if isinstance(raw, dict):
                cfg.update(raw)
        except Exception:
            pass
    return cfg


def load_state() -> Dict[str, Any]:
    p = _state_path()
    if not p.is_file():
        return {"conversations": {}}
    try:
        raw = json.loads(p.read_text(encoding="utf-8", errors="replace"))
        if isinstance(raw, dict) and isinstance(raw.get("conversations"), dict):
            return raw
    except Exception:
        pass
    return {"conversations": {}}


def save_state(state: Dict[str, Any], now_ep: float) -> None:
    convs = state.get("conversations")
    if not isinstance(convs, dict):
        return
    # Prune stale entries to keep state file tiny
    cutoff = now_ep - STATE_RETENTION_SEC
    pruned = {
        cid: rec
        for cid, rec in convs.items()
        if isinstance(rec, dict) and float(rec.get("updated_at", 0.0)) >= cutoff
    }
    if len(pruned) > STATE_MAX_ENTRIES:
        items = sorted(pruned.items(), key=lambda kv: float(kv[1].get("updated_at", 0.0)), reverse=True)
        pruned = dict(items[:STATE_MAX_ENTRIES])
    out = {"conversations": pruned}
    p = _state_path()
    tmp = p.with_suffix(f".tmp.{os.getpid()}.{int(time.time() * 1000) % 100000}")
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        for attempt in range(5):
            try:
                os.replace(tmp, p)
                return
            except PermissionError:
                if attempt < 4:
                    time.sleep(0.015 * (attempt + 1))
    except Exception:
        pass
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass


def _mutate_conv_state(conv_id: str, mutator: Any, now_ep: float) -> Dict[str, Any]:
    """
    Reload latest state from disk before mutating `conversations[conv_id]` so concurrent
    parallel PreToolUse hooks or external UI/launcher toggles never clobber each other.
    """
    if not conv_id:
        return {}
    fresh = load_state()
    convs = fresh.setdefault("conversations", {})
    rec = dict(convs.get(conv_id)) if isinstance(convs.get(conv_id), dict) else {}
    mutator(rec)
    rec["updated_at"] = max(float(rec.get("updated_at") or 0.0), float(now_ep))
    convs[conv_id] = rec
    save_state(fresh, now_ep)
    return rec


def set_window_unrestricted(
    conv_id: str,
    unrestricted: bool,
    source: str = "manual",
    now_ep: Optional[float] = None,
    notified: bool = False,
) -> Dict[str, Any]:
    ts = float(now_ep) if now_ep is not None else time.time()

    def _apply(rec: Dict[str, Any]) -> None:
        rec["unrestricted"] = bool(unrestricted)
        if unrestricted:
            rec["unrestricted_source"] = str(source or "manual")
            rec["unrestricted_at"] = ts
            rec["unrestricted_notified"] = bool(notified)
            rec["pending_cliff_prompts"] = []
        else:
            rec["unrestricted_source"] = ""
            rec["unrestricted_at"] = 0.0
            rec["unrestricted_notified"] = False
            rec["pending_cliff_prompts"] = []

    return _mutate_conv_state(conv_id, _apply, ts)


_DENIAL_TEXT_RE = re.compile(
    r"(?:user\s+(?:denied|rejected|declined|cancell?ed)"
    r"|rejected\s+by\s+user"
    r"|denied\s+by\s+(?:user|pre-tool\s+hook|hook)"
    r"|permission\s+denied"
    r"|tool\s+call\s+denied"
    r"|encountered\s+error\s+in\s+step\s+execution"
    r"|eoq\s*悬崖锁)",
    re.IGNORECASE,
)

_CLIFF_NL_UNLOCK_RE = re.compile(
    r"(?:(?:忽略|无视|解除|取消|关闭|别管|不要管|不管|放开|绕过|跳过|不要被|不想被).{0,12}"
    r"(?:eoq|200k|180k|20k|悬崖锁?|上下文限制|弹窗|收口|限制)"
    r"|(?:别|不要|不再|不用|无需).{0,6}(?:弹窗|问我|停下?|收口|切窗))",
    re.IGNORECASE,
)


@dataclass
class TranscriptSnap:
    file_size: int = 0
    has_planner: bool = False
    planner_step_index: int = -1
    planner_epoch: float = 0.0
    input_tokens: int = 0
    cache_read_tokens: int = 0
    output_tokens: int = 0
    prompt_tokens: int = 0
    total_tokens: int = 0
    user_input_count: int = 0
    last_user_step_index: int = -1
    last_user_text: str = ""
    last_step_type: str = ""
    last_step_status: str = ""
    last_step_epoch: float = 0.0
    last_step_has_tool_calls: bool = False
    max_step_index: int = -1
    completed_tool_steps: Dict[int, str] = field(default_factory=dict)
    denied_tool_steps: set = field(default_factory=set)
    planner_tool_batches: Dict[int, Tuple[List[str], int]] = field(default_factory=dict)


def _strip_user_request_tags(raw: str) -> str:
    if not raw:
        return ""
    m = re.search(r"<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>", raw, flags=re.DOTALL)
    text = m.group(1) if m else raw
    text = re.sub(r"<ADDITIONAL_METADATA>.*?</ADDITIONAL_METADATA>", "", text, flags=re.DOTALL)
    text = re.sub(r"<USER_SETTINGS_CHANGE>.*?</USER_SETTINGS_CHANGE>", "", text, flags=re.DOTALL)
    return " ".join(text.split()).strip()


def _extract_cache_read(obj: Dict[str, Any]) -> int:
    for key in ("cache_read_tokens", "cached_tokens", "cache_read_input_tokens"):
        v = _safe_nonneg_int(obj.get(key))
        if v > 0:
            return v
    return 0


def parse_jsonl_records(raw: bytes, truncated_head: bool) -> List[Dict[str, Any]]:
    if not raw:
        return []
    work = raw
    if truncated_head:
        nl = work.find(b"\n")
        if nl == -1:
            return []
        work = work[nl + 1 :]
    if not work:
        return []
    records: List[Dict[str, Any]] = []
    for line in work.split(b"\n"):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            obj = json.loads(stripped.decode("utf-8", errors="replace"))
        except Exception:
            continue
        if isinstance(obj, dict):
            records.append(obj)
    return records


def parse_transcript_snapshot(path: Optional[Path]) -> TranscriptSnap:
    snap = TranscriptSnap()
    if path is None or not path.is_file():
        return snap
    # Only ever read JSONL transcript files (never binary/media)
    if path.suffix.lower() != ".jsonl":
        return snap

    records: List[Dict[str, Any]] = []
    for stage_bytes in TAIL_STAGES_BYTES:
        raw, trunc, fsize = read_shared_tail_bytes(path, stage_bytes)
        snap.file_size = fsize
        records = parse_jsonl_records(raw, trunc)
        # Check if we already saw a valid PLANNER_RESPONSE
        has_pr = any(
            str(r.get("type") or "") == "PLANNER_RESPONSE"
            and (_safe_nonneg_int(r.get("input_tokens")) + _extract_cache_read(r)) > 0
            for r in records
        )
        if has_pr or not trunc:
            break

    planner_indices: List[Tuple[int, List[str]]] = []

    for obj in records:
        stype = str(obj.get("type") or "").strip()
        if not stype:
            continue
        raw_idx = obj.get("step_index")
        s_idx = _safe_nonneg_int(raw_idx) if raw_idx is not None else -1
        if raw_idx == 0:
            s_idx = 0
        if s_idx > snap.max_step_index:
            snap.max_step_index = s_idx
        ep = _parse_iso_epoch(obj.get("created_at"))
        tc = obj.get("tool_calls")
        status_str = str(obj.get("status") or "").strip().upper()
        snap.last_step_type = stype
        snap.last_step_status = status_str
        if ep > 0:
            snap.last_step_epoch = ep
        snap.last_step_has_tool_calls = isinstance(tc, list) and len(tc) > 0

        if stype == "USER_INPUT":
            snap.user_input_count += 1
            if s_idx >= 0:
                snap.last_user_step_index = s_idx
            utxt = _strip_user_request_tags(str(obj.get("content") or ""))
            if utxt:
                snap.last_user_text = utxt

        elif stype == "PLANNER_RESPONSE":
            if s_idx >= 0:
                t_names = [
                    str(c.get("name") or "").strip().lower()
                    for c in (tc if isinstance(tc, list) else [])
                    if isinstance(c, dict) and str(c.get("name") or "").strip()
                ]
                planner_indices.append((s_idx, t_names))
            in_tok = _safe_nonneg_int(obj.get("input_tokens"))
            cache_tok = _extract_cache_read(obj)
            out_tok = _safe_nonneg_int(obj.get("output_tokens"))
            prompt_tok = in_tok + cache_tok
            if prompt_tok > 0:
                snap.has_planner = True
                snap.planner_step_index = s_idx if s_idx >= 0 else _safe_nonneg_int(obj.get("step_index"))
                if ep > 0:
                    snap.planner_epoch = ep
                snap.input_tokens = in_tok
                snap.cache_read_tokens = cache_tok
                snap.output_tokens = out_tok
                snap.prompt_tokens = prompt_tok
                snap.total_tokens = prompt_tok + out_tok

        elif stype in ("GENERIC", "TOOL_RESPONSE") and s_idx >= 0:
            err_str = str(obj.get("error") or "").strip()
            head_txt = str(obj.get("content") or "")[:300]
            if status_str == "DONE" and not err_str and not _DENIAL_TEXT_RE.search(head_txt):
                snap.completed_tool_steps[s_idx] = head_txt.lower()
            elif (
                status_str in ("ERROR", "FAILED", "CANCELLED", "CANCELED", "REJECTED", "DENIED")
                or err_str
                or _DENIAL_TEXT_RE.search(head_txt)
            ):
                snap.denied_tool_steps.add(s_idx)

    for i, (p_idx, t_names) in enumerate(planner_indices):
        next_p = planner_indices[i + 1][0] if i + 1 < len(planner_indices) else 10**9
        snap.planner_tool_batches[p_idx] = (t_names, next_p)

    return snap


def resolve_transcript_path(payload: Dict[str, Any]) -> Optional[Path]:
    raw_tp = str(payload.get("transcriptPath") or "").strip()
    if raw_tp:
        p = Path(raw_tp)
        if p.is_file():
            return p
    conv_id = str(payload.get("conversationId") or "").strip()
    if conv_id and re.match(r"^[0-9a-fA-F-]{8,64}$", conv_id):
        cand = Path.home() / ".gemini" / "antigravity" / "brain" / conv_id / ".system_generated" / "logs" / "transcript.jsonl"
        if cand.is_file():
            return cand
    return None


def _apply_carry_forward(snap: TranscriptSnap, conv_state: Dict[str, Any]) -> TranscriptSnap:
    prev_size = _safe_nonneg_int(conv_state.get("file_size"))
    prev_prompt = _safe_nonneg_int(conv_state.get("prompt_tokens"))
    prev_epoch = float(conv_state.get("planner_epoch") or 0.0)
    if (
        not snap.has_planner
        and prev_prompt > 0
        and (snap.file_size == 0 or snap.file_size >= prev_size)
    ):
        snap.has_planner = True
        snap.prompt_tokens = prev_prompt
        snap.planner_epoch = prev_epoch
        snap.total_tokens = max(prev_prompt, _safe_nonneg_int(conv_state.get("total_tokens")))
    return snap


def _normalize_command_punct(text: str) -> str:
    return (text or "").replace("！", "!").replace("／", "/")


def _has_bypass_token(user_input: str, bypass_tokens: List[str]) -> bool:
    if not user_input:
        return False
    low = _normalize_command_punct(user_input).lower()
    for tok in bypass_tokens:
        t = _normalize_command_punct(str(tok or "").strip()).lower()
        if not t:
            continue
        if t.startswith("!"):
            pattern = r"(?<![a-z0-9_])" + re.escape(t) + r"(?![a-z0-9_])"
        elif t.startswith("/"):
            pattern = r"(?<![a-z0-9_:/\\.-])" + re.escape(t) + r"(?![a-z0-9_/-])"
        elif re.search(r"[\u4e00-\u9fff]", t):
            if t in low:
                return True
            continue
        else:
            pattern = r"(?:^|[\s,，。；;()\[\]<>!！])" + re.escape(t) + r"(?:$|[\s,，。；;()\[\]<>!！])"
        if re.search(pattern, low):
            return True
    return False


def _detect_approved_cliff_prompt(
    snap: TranscriptSnap,
    pending: Any,
    now_ep: float,
) -> Tuple[bool, List[Dict[str, Any]]]:
    """
    Detect whether any previously `force_ask`-gated cliff tool was approved & executed by the user.
    Returns `(approved, remaining_pending)`.
    """
    if not isinstance(pending, list) or not pending:
        return False, []
    remaining: List[Dict[str, Any]] = []
    for item in pending:
        if not isinstance(item, dict):
            continue
        ts = float(item.get("ts") or 0.0)
        if ts > 0 and (now_ep - ts) > 900.0:
            continue
        step_idx = _safe_nonneg_int(item.get("step_idx"))
        tname = str(item.get("name") or "").strip().lower()
        after_planner_idx = int(item.get("after_planner_idx", -1))

        if step_idx > 0:
            if step_idx in snap.denied_tool_steps:
                continue
            if step_idx in snap.completed_tool_steps:
                return True, []
        elif after_planner_idx >= 0 and after_planner_idx in snap.planner_tool_batches:
            batch_tools, next_p = snap.planner_tool_batches[after_planner_idx]
            if (
                tname in batch_tools
                and "write_file" not in batch_tools
                and "edit_file" not in batch_tools
            ):
                for done_idx in snap.completed_tool_steps:
                    if after_planner_idx < done_idx < next_p:
                        return True, []
                if any(after_planner_idx < den_idx < next_p for den_idx in snap.denied_tool_steps):
                    continue
        remaining.append(item)
    return False, remaining[-16:]


def _is_compact_command(user_input: str) -> bool:
    norm = _normalize_command_punct(user_input or "")
    return bool(re.match(r"^\s*/compact\b", norm, flags=re.IGNORECASE))


def _is_exempt_input(user_input: str, exempt_prefixes: List[str]) -> bool:
    s = (user_input or "").strip()
    if not s:
        return False
    s_low = s.lower()
    for pref in exempt_prefixes:
        p = str(pref or "").strip()
        if p and s_low.startswith(p.lower()):
            return True
    return False


def _is_fresh_user_turn(payload: Dict[str, Any], snap: TranscriptSnap) -> bool:
    raw_inv = payload.get("invocationNum")
    if raw_inv is not None:
        try:
            inv_num = int(raw_inv)
            if inv_num > 1:
                return False
        except (TypeError, ValueError):
            pass
    # Never block tool loops, system wakeups, or post-error SDK retries
    if snap.last_step_type in ("GENERIC", "TOOL_RESPONSE", "ERROR_MESSAGE", "SYSTEM_MESSAGE"):
        return False
    if snap.last_step_type == "PLANNER_RESPONSE":
        if snap.last_step_has_tool_calls:
            return False
        if snap.user_input_count > 0:
            return False
        return bool(_strip_user_request_tags(str(payload.get("lastUserInput") or "")))
    return True


def _extract_edited_path_from_tc(tc: Dict[str, Any]) -> str:
    name = str(tc.get("name") or "").lower()
    if name not in ("write_file", "edit_file"):
        return ""
    args = tc.get("args")
    if not isinstance(args, dict):
        return ""
    for k in ("Path", "path", "TargetFile", "file_path"):
        v = args.get(k)
        if isinstance(v, str) and v.strip():
            cleaned = v.strip().strip('"').strip("'").replace("\\\\", "\\")
            return cleaned
    return ""


def _resolve_rescue_dir(payload: Dict[str, Any], cfg: Dict[str, Any]) -> Optional[Path]:
    handoff_rel = str(cfg.get("handoff_dir_rel") or ".agents/checkpoints").strip()
    ws_list = payload.get("workspacePaths") or []
    for ws in ws_list:
        ws_str = str(ws or "").strip()
        if ws_str and not _is_exempt_workspace_path(ws_str, cfg):
            ws_p = Path(ws_str)
            if ws_p.is_dir():
                return ws_p / handoff_rel
    cwd_str = str(payload.get("cwd") or "").strip()
    if cwd_str and not _is_exempt_workspace_path(cwd_str, cfg):
        cwd_p = Path(cwd_str)
        if cwd_p.is_dir():
            return cwd_p / handoff_rel
    art_str = str(payload.get("artifactDirectoryPath") or "").strip()
    if art_str:
        return Path(art_str) / "checkpoints"
    return None


def generate_cold_rescue_draft(
    transcript_path: Optional[Path],
    payload: Dict[str, Any],
    snap: TranscriptSnap,
    idle_sec: float,
    cfg: Dict[str, Any],
    now_ep: float,
    reuse_path: Optional[Path] = None,
) -> Optional[Path]:
    """
    0-Token pure local transcript tail extractor written when Cold Gate blocks a stale session.
    Strictly text-only (no binary/media reads), bounded to <= handoff_max_chars.
    Reuses `reuse_path` within the same cold planner epoch to avoid duplicate file floods.
    """
    if transcript_path is None or not transcript_path.is_file():
        return None
    out_dir = _resolve_rescue_dir(payload, cfg)
    if out_dir is None:
        return None

    raw, trunc, _ = read_shared_tail_bytes(transcript_path, RESCUE_READ_BYTES)
    records = parse_jsonl_records(raw, trunc)

    user_msgs: List[str] = []
    edited_files: List[str] = []
    seen_files = set()
    last_assistant_text = ""

    for obj in records:
        stype = str(obj.get("type") or "")
        if stype == "USER_INPUT":
            txt = _strip_user_request_tags(str(obj.get("content") or ""))
            if txt and not _is_exempt_input(txt, cfg.get("exempt_input_prefixes") or []):
                user_msgs.append(txt[:160] + ("…" if len(txt) > 160 else ""))
        elif stype == "PLANNER_RESPONSE":
            tcs = obj.get("tool_calls")
            if isinstance(tcs, list):
                for tc in tcs:
                    if isinstance(tc, dict):
                        fp = _extract_edited_path_from_tc(tc)
                        if fp:
                            low = fp.lower()
                            if low not in seen_files:
                                seen_files.add(low)
                                edited_files.append(fp)
            content = str(obj.get("content") or "").strip()
            if content:
                last_assistant_text = content

    conv_id = str(payload.get("conversationId") or "unknown")
    conv8 = conv_id[:8]
    dt_local = datetime.fromtimestamp(now_ep).astimezone()
    ts_name = dt_local.strftime("%Y%m%d_%H%M%S")
    ts_human = dt_local.strftime("%Y-%m-%d %H:%M:%S")
    ctx_lbl = format_tokens_k(snap.prompt_tokens)
    idle_lbl = format_duration_cn(idle_sec)

    recent_users = user_msgs[-4:]
    recent_files = edited_files[-12:]
    assistant_excerpt = last_assistant_text
    if len(assistant_excerpt) > 750:
        assistant_excerpt = assistant_excerpt[:350].rstrip() + "\n...\n" + assistant_excerpt[-380:].lstrip()

    lines: List[str] = [
        f"# 冷窗零 Token 急救草稿（{conv8}）",
        f"- 来源会话：`{conv_id}` | 生成时间：`{ts_human}`",
        f"- 拦截状态：`❄️ 已冷（空闲 {idle_lbl}）` | 拦截前上下文：`~{ctx_lbl}`（免去全价重吞）",
        "",
        "## 1. 最近用户意图轨迹（尾部抽提）",
    ]
    if recent_users:
        for i, msg in enumerate(recent_users, 1):
            lines.append(f"{i}. {msg}")
    else:
        lines.append("- （尾部未检出历史用户消息，可在新窗用 `session-recall` 局部切片查阅）")

    lines.extend(["", "## 2. 最近改动/写入的文件锚点"])
    if recent_files:
        for fp in recent_files:
            lines.append(f"- `{fp}`")
    else:
        lines.append("- （尾部未检出 `write_file`/`edit_file` 记录）")

    lines.extend(["", "## 3. 末轮助手结论摘录"])
    if assistant_excerpt:
        lines.append(assistant_excerpt)
    else:
        lines.append("- （无末轮文本摘录）")

    if reuse_path is not None and reuse_path.parent == out_dir:
        out_path = reuse_path
    else:
        out_path = out_dir / f"cold_rescue_{ts_name}_{conv8}.md"
    lines.extend([
        "",
        "## 4. 新窗口首条启动指令（可直接复制）",
        f"> 请先读取冷窗急救草稿 `{out_path}`，承接其中记录的进度与文件锚点，继续处理当前任务（如需补查历史细节，仅用 `session-recall` 对 `{conv8}` 局部切片检索，严禁整窗通读）。",
        "",
    ])

    max_chars = max(800, _safe_nonneg_int(cfg.get("handoff_max_chars")) or 2400)
    body = "\n".join(lines)
    if len(body) > max_chars:
        body = body[: max_chars - 24].rstrip() + "\n\n...（已按 EOQ 上限截断）\n"

    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path.write_text(body, encoding="utf-8", newline="\n")
        return out_path
    except Exception:
        return None


def _make_system_step(model_text: str, user_markdown: str = "", show_ribbon: bool = True) -> Dict[str, Any]:
    inner: Dict[str, Any] = {"systemMessage": model_text}
    if show_ribbon and user_markdown:
        inner["metadata"] = {"user_markdown": user_markdown}
    return {"systemMessage": inner}


def _resolve_exempt_workspace_mode(cfg: Dict[str, Any]) -> str:
    raw = cfg.get("non_code_workspace_mode") or cfg.get("chat_workspace_mode") or "off"
    mode = str(raw).strip().lower()
    return mode if mode in ("off", "warn_only", "full") else "off"


def _make_unlock_step(
    source: str,
    had_prior_warn: bool,
    show_ribbon: bool = True,
) -> Dict[str, Any]:
    if source == "cliff_allow":
        model_msg = (
            "[EOQ 本窗免限已生效] 用户已在悬崖锁弹窗中选择「允许」并解锁本窗。请完全忽略此前所有 EOQ "
            "收口/停顿/落盘交接指令，直接在当前窗口连续完成用户的全部任务直至结束，后续工具不会再弹窗拦截。"
        )
        ribbon = (
            "🔓 **EOQ 本窗已自动解锁**：本窗后续工具将自动放行不再弹窗，AI 将连续做到完成"
            "（如需恢复保护随时发 `!lock` 或点 [🔒 恢复锁定](agyrun://eoq-lock-window?silent)）。"
        )
    elif source == "nl_override":
        model_msg = (
            "[EOQ 本窗免限已生效] 用户已明确要求忽略上下文/200K 限制并在当前窗口继续完成任务。请忽略此前所有 EOQ "
            "收口/停顿/落盘交接指令，连续执行直至完成，后续工具已解锁不再弹窗拦截。"
        )
        ribbon = (
            "🔓 **EOQ 本窗免限已自动开启**：已识别你的持续执行要求，本窗 180K/200K 弹窗锁与冷窗拦截均已解除"
            "（如需恢复保护随时发 `!lock` 或点 [🔒 恢复锁定](agyrun://eoq-lock-window?silent)）。"
        )
    else:
        model_msg = (
            "[EOQ 本窗免限已生效] 用户已主动开启本窗免限模式。请完全忽略此前所有 EOQ "
            "收口/停顿/落盘交接指令，直接在当前窗口连续完成用户的全部任务直至结束，后续工具不会再弹窗拦截。"
        )
        ribbon = (
            "🔓 **EOQ 本窗免限已开启**：本窗已解除上下文提醒、冷窗拦截与 180K/200K 弹窗锁，AI 将连续做到完成"
            "（如需恢复保护随时发 `!lock` 或点 [🔒 恢复锁定](agyrun://eoq-lock-window?silent)）。"
        )
    return _make_system_step(model_msg if had_prior_warn else "", ribbon, show_ribbon=show_ribbon)


def handle_pre_invocation(
    payload: Dict[str, Any],
    cfg: Dict[str, Any],
    state: Dict[str, Any],
    now_ep: float,
) -> Dict[str, Any]:
    if not cfg.get("enabled", True):
        return {}

    exempt_ws = is_exempt_workspace(payload, cfg)
    ws_mode = _resolve_exempt_workspace_mode(cfg)
    if exempt_ws and ws_mode == "off":
        return {}

    conv_id = str(payload.get("conversationId") or "").strip()
    payload_input = _strip_user_request_tags(str(payload.get("lastUserInput") or ""))
    convs = state.setdefault("conversations", {})
    conv_state: Dict[str, Any] = dict(convs.get(conv_id)) if isinstance(convs.get(conv_id), dict) else {}

    tpath = resolve_transcript_path(payload)
    snap = _apply_carry_forward(parse_transcript_snapshot(tpath), conv_state)

    if snap.last_step_type == "USER_INPUT" and snap.last_user_text:
        user_input = snap.last_user_text
    else:
        user_input = payload_input or snap.last_user_text

    show_ribbon = bool(cfg.get("show_user_ribbon", True))
    exempt = _is_exempt_input(user_input, cfg.get("exempt_input_prefixes") or [])
    is_compact = _is_compact_command(user_input)
    is_fresh_turn = _is_fresh_user_turn(payload, snap)
    is_internal_non_turn = (
        snap.last_step_type == "ERROR_MESSAGE"
        or (snap.last_step_type == "PLANNER_RESPONSE" and not snap.last_step_has_tool_calls and snap.user_input_count > 0)
    )

    prev_peak = _safe_nonneg_int(conv_state.get("peak_prompt_tokens"))
    warned_bands = set(conv_state.get("warned_bands") or [])
    compact_drop = _safe_nonneg_int(cfg.get("compact_reset_drop_tokens")) or 40000
    did_compact_reset = False
    if snap.has_planner and snap.prompt_tokens > 0 and prev_peak > 0 and (prev_peak - snap.prompt_tokens) >= compact_drop:
        warned_bands.clear()
        prev_peak = snap.prompt_tokens
        did_compact_reset = True
    new_peak = max(prev_peak, snap.prompt_tokens)

    rescue_epoch = float(conv_state.get("rescue_epoch") or 0.0)
    rescue_path_str = str(conv_state.get("rescue_path") or "").strip()
    unrestricted = bool(conv_state.get("unrestricted", False))
    unrestricted_source = str(conv_state.get("unrestricted_source") or "")
    unrestricted_at = float(conv_state.get("unrestricted_at") or 0.0)
    unrestricted_notified = bool(conv_state.get("unrestricted_notified", False))
    pending_prompts = list(conv_state.get("pending_cliff_prompts") or [])
    last_cmd_step_idx = int(conv_state.get("last_cmd_step_idx", -2))

    # Auto-promote to unrestricted if user previously clicked "Allow" on a cliff lock popup
    if not unrestricted and cfg.get("cliff_allow_unlocks_window", True) and pending_prompts:
        approved, remaining_pending = _detect_approved_cliff_prompt(snap, pending_prompts, now_ep)
        pending_prompts = remaining_pending
        if approved:
            unrestricted = True
            unrestricted_source = "cliff_allow"
            unrestricted_at = now_ep
            unrestricted_notified = False
            pending_prompts = []

    def _commit_state(*, force_unrestricted: Optional[bool] = None) -> None:
        if not conv_id:
            return

        def _apply(rec: Dict[str, Any]) -> None:
            rec["file_size"] = max(snap.file_size, _safe_nonneg_int(rec.get("file_size")))
            if snap.has_planner and snap.prompt_tokens > 0:
                rec["prompt_tokens"] = snap.prompt_tokens
                rec["total_tokens"] = snap.total_tokens
                rec["peak_prompt_tokens"] = new_peak if did_compact_reset else max(new_peak, _safe_nonneg_int(rec.get("peak_prompt_tokens")))
                rec["planner_epoch"] = snap.planner_epoch
            rec["warned_bands"] = sorted(warned_bands)
            rec["last_cmd_step_idx"] = last_cmd_step_idx
            if rescue_epoch > 0 and rescue_path_str:
                rec["rescue_epoch"] = rescue_epoch
                rec["rescue_path"] = rescue_path_str
            if force_unrestricted is not None:
                rec["unrestricted"] = bool(force_unrestricted)
                rec["unrestricted_source"] = unrestricted_source if force_unrestricted else ""
                rec["unrestricted_at"] = unrestricted_at if force_unrestricted else 0.0
                rec["unrestricted_notified"] = bool(unrestricted_notified) if force_unrestricted else False
                rec["pending_cliff_prompts"] = [] if force_unrestricted else pending_prompts
            else:
                disk_unr = bool(rec.get("unrestricted", False))
                eff_unr = unrestricted or disk_unr
                rec["unrestricted"] = eff_unr
                if eff_unr:
                    if not disk_unr:
                        rec["unrestricted_source"] = unrestricted_source
                        rec["unrestricted_at"] = unrestricted_at
                    rec["unrestricted_notified"] = bool(unrestricted_notified or rec.get("unrestricted_notified", False))
                    rec["pending_cliff_prompts"] = []
                else:
                    rec["pending_cliff_prompts"] = pending_prompts

        _mutate_conv_state(conv_id, _apply, now_ep)

    is_new_user_cmd = is_fresh_turn and (
        snap.last_user_step_index < 0 or snap.last_user_step_index != last_cmd_step_idx
    )
    unlock_tokens = cfg.get("window_unlock_tokens") or DEFAULT_CONFIG["window_unlock_tokens"]
    lock_tokens = cfg.get("window_lock_tokens") or DEFAULT_CONFIG["window_lock_tokens"]

    # 0a. Explicit per-window unlock/lock commands (work even on turn 1 before any PLANNER_RESPONSE)
    if is_new_user_cmd and not exempt and not is_compact:
        wants_unlock = _has_bypass_token(user_input, unlock_tokens)
        wants_lock = (not wants_unlock) and _has_bypass_token(user_input, lock_tokens)
        if wants_unlock:
            unrestricted = True
            unrestricted_source = "command"
            unrestricted_at = now_ep
            unrestricted_notified = True
            pending_prompts = []
            if snap.last_user_step_index >= 0:
                last_cmd_step_idx = snap.last_user_step_index
            _commit_state(force_unrestricted=True)
            return {
                "injectSteps": [
                    _make_unlock_step("command", had_prior_warn=bool(warned_bands), show_ribbon=show_ribbon)
                ]
            }
        if wants_lock:
            unrestricted = False
            unrestricted_source = ""
            unrestricted_at = 0.0
            unrestricted_notified = False
            pending_prompts = []
            if snap.last_user_step_index >= 0:
                last_cmd_step_idx = snap.last_user_step_index
            _commit_state(force_unrestricted=False)
            ribbon = "🔒 **EOQ 本窗保护已恢复**：已重新开启冷窗拦截、水位提醒与 180K/200K 悬崖锁。"
            return {"injectSteps": [_make_system_step("", ribbon, show_ribbon=show_ribbon)]}

    if not snap.has_planner or snap.prompt_tokens <= 0:
        return {}

    cliff_warn_tok = _safe_nonneg_int(cfg.get("cliff_warn_tokens")) or 175000
    cliff_lock_tok = _safe_nonneg_int(cfg.get("cliff_tool_lock_tokens")) or 180000
    hot_nudge_tok = _safe_nonneg_int(cfg.get("hot_nudge_tokens")) or 95000

    # 0b. Natural-language override ONLY when conversation is already in cliff/warn zone
    in_cliff_zone = (
        snap.prompt_tokens >= cliff_warn_tok
        or "cliff_175k" in warned_bands
        or bool(pending_prompts)
    )
    if (
        not unrestricted
        and in_cliff_zone
        and is_new_user_cmd
        and not exempt
        and not is_compact
        and _CLIFF_NL_UNLOCK_RE.search(user_input or "")
    ):
        unrestricted = True
        unrestricted_source = "nl_override"
        unrestricted_at = now_ep
        unrestricted_notified = True
        pending_prompts = []
        if snap.last_user_step_index >= 0:
            last_cmd_step_idx = snap.last_user_step_index
        _commit_state(force_unrestricted=True)
        return {
            "injectSteps": [
                _make_unlock_step("nl_override", had_prior_warn=bool(warned_bands), show_ribbon=show_ribbon)
            ]
        }

    # 0c. Active per-window unrestricted mode: bypass Cold Gate & Hot/Cliff nudges completely
    if unrestricted:
        if not unrestricted_notified and not exempt and not is_compact and not is_internal_non_turn:
            unrestricted_notified = True
            _commit_state()
            return {
                "injectSteps": [
                    _make_unlock_step(
                        unrestricted_source or "button",
                        had_prior_warn=bool(warned_bands),
                        show_ribbon=show_ribbon,
                    )
                ]
            }
        _commit_state()
        return {}

    bypass = _has_bypass_token(user_input, cfg.get("bypass_tokens") or [])
    ctx_lbl = format_tokens_k(snap.prompt_tokens)
    handoff_dir = str(cfg.get("handoff_dir_rel") or ".agents/checkpoints")

    # 1. Cold Window Gate (measured strictly from last PLANNER_RESPONSE timestamp)
    cold_ttl = float(cfg.get("cold_ttl_sec") or 250)
    cold_block_tok = _safe_nonneg_int(cfg.get("cold_block_tokens")) or 30000
    idle_sec = max(0.0, now_ep - snap.planner_epoch) if snap.planner_epoch > 0 else 0.0

    if (
        snap.planner_epoch > 0
        and idle_sec >= cold_ttl
        and snap.prompt_tokens >= cold_block_tok
        and is_fresh_turn
        and not bypass
        and not exempt
        and not is_compact
    ):
        idle_lbl = format_duration_cn(idle_sec)
        if exempt_ws and ws_mode == "warn_only":
            _commit_state()
            ribbon = (
                f"❄️ **EOQ 冷窗提示（非工程区仅提醒）**：本窗已空闲 `{idle_lbl}` 且上下文约 `{ctx_lbl}`，"
                f"本轮已按全价重算；后续若切话题建议 `/compact` 或开新窗。"
            )
            return {"injectSteps": [_make_system_step("", ribbon, show_ribbon=True)]}

        rescue_path: Optional[Path] = None
        if cfg.get("cold_auto_rescue_draft", True):
            cand_reuse: Optional[Path] = None
            if rescue_path_str and abs(rescue_epoch - snap.planner_epoch) < 1e-3:
                p_cand = Path(rescue_path_str)
                if p_cand.is_file():
                    cand_reuse = p_cand
            rescue_path = generate_cold_rescue_draft(
                tpath, payload, snap, idle_sec, cfg, now_ep, reuse_path=cand_reuse
            )
            if rescue_path is not None:
                rescue_epoch = snap.planner_epoch
                rescue_path_str = str(rescue_path)

        _commit_state()
        msg_lines = [
            f"🛑 [EOQ 冷窗熔断] 当前窗口已冷（空闲 {idle_lbl} > {int(cold_ttl)}s）且上下文约 {ctx_lbl}，"
            f"直接发话将全价重吞 ~{ctx_lbl}（开新窗仅 ~18.5K）。本次请求已在调用模型前拦截（0 Token 消耗）。",
        ]
        if rescue_path is not None:
            uri = "file:///" + str(rescue_path).replace("\\", "/")
            msg_lines.append(f"• 零 Token 急救草稿已落盘：[{rescue_path.name}]({uri})")
            msg_lines.append(f"• 开新窗直接粘贴：`请先读取冷窗急救草稿 {rescue_path}，承接继续当前任务。`")
        else:
            msg_lines.append("• 建议开新窗承接；如需翻查旧窗细节，在新窗用 `session-recall` 局部切片查阅。")
        msg_lines.append(
            "• 单次强行继续请加 `!go` 重发，或发 `/compact` 压缩；"
            "若需解除本窗所有限制并一路做完，请点 [🔓 一键解锁本窗](agyrun://eoq-unlock-window?silent) 后重发，或在消息末尾加 `!unlock`。"
        )

        return {
            "injectSteps": [{"errorMessage": {"userMessage": "\n".join(msg_lines)}}],
            "terminationBehavior": "terminate",
        }

    # Skip hot odometer injection on keepalive heartbeats, /compact, or internal non-turn calls
    if exempt or is_compact or is_internal_non_turn:
        _commit_state()
        return {}

    # 2. Hot Window One-Shot Odometer Injection
    lock_lbl = format_tokens_k(cliff_lock_tok)

    if snap.prompt_tokens >= cliff_warn_tok and "cliff_175k" not in warned_bands:
        warned_bands.add("cliff_175k")
        warned_bands.add("hot_95k")
        _commit_state()
        if snap.prompt_tokens >= TIER_2X_THRESHOLD:
            cliff_desc = "已超 200K 双倍计价墙（2x 费率生效中）"
            ribbon_desc = "已超 `200K` 双倍计价墙（2x 生效中）"
        else:
            gap_lbl = format_tokens_k(max(1000, TIER_2X_THRESHOLD - snap.prompt_tokens))
            cliff_desc = f"距 200K 双倍计价墙仅差 {gap_lbl}"
            ribbon_desc = f"距 `200K` 双倍计价墙仅差 `{gap_lbl}`"
        if exempt_ws and ws_mode == "warn_only":
            user_ribbon = f"🔴 **上下文悬崖提醒（非工程区仅提示）**：当前上下文 `{ctx_lbl}`（{ribbon_desc}），可按需 `/compact` 或归档切窗。"
            return {"injectSteps": [_make_system_step("", user_ribbon, show_ribbon=show_ribbon)]}
        model_msg = (
            f"[EOQ 悬崖预警] 当前会话实测上下文已达 {ctx_lbl}（{cliff_desc}；达到 {lock_lbl} 后"
            f"读文件/命令等增肥工具将触发 PreToolUse 弹窗锁定）。严禁新读大文件或开启新排查，"
            f"请立即收尾并按 stage-handoff 协议将 ≤1.5K 交接单落盘至 {handoff_dir}，附可复制的新窗启动指令后停止。"
        )
        user_ribbon = (
            f"🔴 **EOQ 悬崖预警**：当前上下文 `{ctx_lbl}`（{ribbon_desc}），已指令模型立即落盘 `≤1.5K` 交接单并收口。"
            f"｜ [🔓 本窗免限·一直做完](agyrun://eoq-unlock-window?silent)（或弹窗点「允许」/ 发 `!unlock`）"
        )
        return {"injectSteps": [_make_system_step(model_msg, user_ribbon, show_ribbon=show_ribbon)]}

    if snap.prompt_tokens >= hot_nudge_tok and "hot_95k" not in warned_bands:
        warned_bands.add("hot_95k")
        _commit_state()
        if exempt_ws and ws_mode == "warn_only":
            user_ribbon = f"🟡 **上下文甜点提醒（非工程区仅提示）**：当前上下文 `{ctx_lbl}`，长聊留意 `200K` 计价线。"
            return {"injectSteps": [_make_system_step("", user_ribbon, show_ribbon=show_ribbon)]}
        model_msg = (
            f"[EOQ 甜点收口提醒] 当前会话实测上下文已达 {ctx_lbl}（已进入 80K~120K 经济最优分段带）。"
            f"请完成手头当前子目标后在自然边界主动收口：按 stage-handoff 协议将 ≤1.5K 结构化交接单落盘至 "
            f"{handoff_dir}，并附可直接复制的新窗首条启动指令，勿继续展开下一独立阶段。"
        )
        user_ribbon = (
            f"🟡 **EOQ 甜点区提醒**：当前上下文 `{ctx_lbl}`（`80K~120K` 最优分段带），已提醒模型在本阶段自然边界落盘交接单切窗。"
            f"｜ [🔓 本窗免限·一直做完](agyrun://eoq-unlock-window?silent)（或发 `!unlock`）"
        )
        return {"injectSteps": [_make_system_step(model_msg, user_ribbon, show_ribbon=show_ribbon)]}

    _commit_state()
    return {}


def _extract_tool_target_path(tool_args: Dict[str, Any]) -> str:
    for k in ("Path", "path", "TargetFile", "file_path", "DirectoryPath"):
        v = tool_args.get(k)
        if isinstance(v, str) and v.strip():
            return _normalize_path_str(v).lower()
    return ""


def _is_checkpoint_write_allowed(tool_name: str, tool_args: Dict[str, Any], cfg: Dict[str, Any]) -> bool:
    if tool_name not in ("write_file", "edit_file"):
        return False
    target = _extract_tool_target_path(tool_args)
    if not target:
        return False
    for marker in cfg.get("cliff_allow_write_dir_markers") or []:
        m = _normalize_path_str(str(marker or "")).lower()
        if m and m in target:
            return True
    return False


def handle_pre_tool_use(
    payload: Dict[str, Any],
    cfg: Dict[str, Any],
    state: Dict[str, Any],
    now_ep: Optional[float] = None,
) -> Dict[str, Any]:
    if not cfg.get("enabled", True):
        return {"decision": "allow"}

    exempt_ws = is_exempt_workspace(payload, cfg)
    ws_mode = _resolve_exempt_workspace_mode(cfg)
    if exempt_ws and ws_mode != "full":
        return {"decision": "allow"}

    tool_call = payload.get("toolCall") if isinstance(payload.get("toolCall"), dict) else {}
    tool_name = str(tool_call.get("name") or "").strip().lower()
    tool_args = tool_call.get("args") if isinstance(tool_call.get("args"), dict) else {}

    # Always allow checkpoint / handoff writes so the agent can never deadlock
    if _is_checkpoint_write_allowed(tool_name, tool_args, cfg):
        return {"decision": "allow"}

    locked_tools = {str(t).strip().lower() for t in (cfg.get("cliff_locked_tools") or []) if str(t).strip()}
    if not tool_name or tool_name not in locked_tools:
        return {"decision": "allow"}

    conv_id = str(payload.get("conversationId") or "").strip()
    convs = state.get("conversations") if isinstance(state.get("conversations"), dict) else {}
    conv_state = convs.get(conv_id) if isinstance(convs.get(conv_id), dict) else {}

    # Fast path: per-window unrestricted mode allows all tools immediately with 0 transcript I/O
    if bool(conv_state.get("unrestricted", False)):
        return {"decision": "allow"}

    now_val = float(now_ep) if now_ep is not None else time.time()
    tpath = resolve_transcript_path(payload)
    snap = _apply_carry_forward(parse_transcript_snapshot(tpath), conv_state)
    lock_tok = _safe_nonneg_int(cfg.get("cliff_tool_lock_tokens")) or 180000

    if not snap.has_planner or snap.prompt_tokens < lock_tok:
        return {"decision": "allow"}

    allow_unlocks = bool(cfg.get("cliff_allow_unlocks_window", True))
    pending_prompts = list(conv_state.get("pending_cliff_prompts") or [])
    if allow_unlocks and pending_prompts:
        approved, remaining_pending = _detect_approved_cliff_prompt(snap, pending_prompts, now_val)
        pending_prompts = remaining_pending
        if approved:
            if conv_id:
                set_window_unrestricted(conv_id, True, source="cliff_allow", now_ep=now_val, notified=False)
            return {"decision": "allow"}

    tool_id = str(tool_call.get("id") or "").strip()
    step_idx = _safe_nonneg_int(payload.get("stepIdx"))
    if conv_id:
        entry = {
            "id": tool_id,
            "step_idx": step_idx,
            "name": tool_name,
            "after_planner_idx": snap.planner_step_index,
            "ts": now_val,
        }

        def _record_pending(rec: Dict[str, Any]) -> None:
            if bool(rec.get("unrestricted", False)):
                return
            rec["prompt_tokens"] = max(snap.prompt_tokens, _safe_nonneg_int(rec.get("prompt_tokens")))
            rec["peak_prompt_tokens"] = max(snap.prompt_tokens, _safe_nonneg_int(rec.get("peak_prompt_tokens")))
            if snap.planner_epoch > 0:
                rec["planner_epoch"] = snap.planner_epoch
            cur = [p for p in (rec.get("pending_cliff_prompts") or []) if isinstance(p, dict)]
            if tool_id:
                cur = [p for p in cur if str(p.get("id") or "") != tool_id]
            elif step_idx > 0:
                cur = [p for p in cur if _safe_nonneg_int(p.get("step_idx")) != step_idx]
            cur.append(entry)
            rec["pending_cliff_prompts"] = cur[-16:]

        updated = _mutate_conv_state(conv_id, _record_pending, now_val)
        if bool(updated.get("unrestricted", False)):
            return {"decision": "allow"}

    ctx_lbl = format_tokens_k(snap.prompt_tokens)
    if snap.prompt_tokens >= TIER_2X_THRESHOLD:
        status_desc = "已超 200K 双倍计价墙（2x 费率生效中）"
    else:
        gap_lbl = format_tokens_k(max(1000, TIER_2X_THRESHOLD - snap.prompt_tokens))
        status_desc = f"距 200K 双倍计价墙仅差 {gap_lbl}"

    if allow_unlocks:
        reason = (
            f"🛑 [EOQ 悬崖锁] 当前上下文已达 {ctx_lbl}（{status_desc}），调用 `{tool_name}` 可能跨入 2x 计价区。\n"
            f"• 点「允许」：立即解锁本窗免限模式，AI 将一直做到完成且不再重复弹窗（随时可发 !lock 恢复）；\n"
            f"• 点「拒绝」：拦截增肥，让模型直接落盘交接单切新窗。"
        )
    else:
        reason = (
            f"🛑 [EOQ 悬崖锁] 当前上下文已达 {ctx_lbl}（{status_desc}）。"
            f"继续调用 `{tool_name}` 可能直接冲破 2x 计价线！确认放行请点允许；"
            f"建议拒绝并让模型直接落盘交接单切新窗（本窗免限请发 !unlock）。"
        )
    return {"decision": "force_ask", "reason": reason}


def handle_post_tool_use(
    payload: Dict[str, Any],
    cfg: Dict[str, Any],
    state: Dict[str, Any],
    now_ep: float,
) -> Dict[str, Any]:
    if not cfg.get("enabled", True) or not cfg.get("cliff_allow_unlocks_window", True):
        return {}
    conv_id = str(payload.get("conversationId") or "").strip()
    if not conv_id:
        return {}
    convs = state.get("conversations") if isinstance(state.get("conversations"), dict) else {}
    conv_state = convs.get(conv_id) if isinstance(convs.get(conv_id), dict) else {}
    if bool(conv_state.get("unrestricted", False)):
        return {}
    pending = conv_state.get("pending_cliff_prompts")
    if not isinstance(pending, list) or not pending:
        return {}

    tool_call = payload.get("toolCall") if isinstance(payload.get("toolCall"), dict) else {}
    tool_id = str(tool_call.get("id") or "").strip()
    step_idx = _safe_nonneg_int(payload.get("stepIdx"))
    err_str = str(payload.get("error") or "").strip()

    matched = False
    for p in pending:
        if not isinstance(p, dict):
            continue
        if tool_id and str(p.get("id") or "") == tool_id:
            matched = True
            break
        if step_idx > 0 and _safe_nonneg_int(p.get("step_idx")) == step_idx:
            matched = True
            break
    if not matched:
        return {}

    if err_str and _DENIAL_TEXT_RE.search(err_str):
        def _drop(rec: Dict[str, Any]) -> None:
            cur = [p for p in (rec.get("pending_cliff_prompts") or []) if isinstance(p, dict)]
            rec["pending_cliff_prompts"] = [
                p for p in cur
                if not ((tool_id and str(p.get("id") or "") == tool_id) or (step_idx > 0 and _safe_nonneg_int(p.get("step_idx")) == step_idx))
            ]

        _mutate_conv_state(conv_id, _drop, now_ep)
        return {}

    set_window_unrestricted(conv_id, True, source="cliff_allow", now_ep=now_ep, notified=False)
    return {}


def dispatch(payload: Dict[str, Any], forced_event: str = "", now_ep: Optional[float] = None) -> Dict[str, Any]:
    event = (forced_event or str(payload.get("hookEventName") or "")).strip()
    is_pre_tool = event == "PreToolUse" or (not event and isinstance(payload.get("toolCall"), dict))
    if _is_eoq_plugin_disabled():
        return {"decision": "allow"} if is_pre_tool else {}

    cfg = load_config()
    state = load_state()
    now_val = float(now_ep) if now_ep is not None else time.time()

    if event == "PostToolUse":
        return handle_post_tool_use(payload, cfg, state, now_val)
    if is_pre_tool:
        return handle_pre_tool_use(payload, cfg, state, now_val)
    return handle_pre_invocation(payload, cfg, state, now_val)


def _parse_forced_event(args: List[str]) -> str:
    forced_event = ""
    for i, a in enumerate(args):
        if a == "--event" and i + 1 < len(args):
            forced_event = args[i + 1].strip()
        elif a.startswith("--event="):
            forced_event = a.split("=", 1)[1].strip()
    return forced_event


def main(argv: Optional[List[str]] = None) -> int:
    args = list(argv) if argv is not None else sys.argv[1:]
    forced_event = _parse_forced_event(args)
    is_forced_pre_tool = forced_event == "PreToolUse"
    try:
        # Bare-core zero-intrusion contract: short-circuit before touching config/state/transcript
        if _is_eoq_plugin_disabled():
            if forced_event:
                _emit({"decision": "allow"} if is_forced_pre_tool else {})
                return 0
            raw_disabled = _read_stdin_bytes()
            if raw_disabled and raw_disabled.strip():
                try:
                    peek = json.loads(raw_disabled.decode("utf-8", errors="replace"))
                    if isinstance(peek, dict):
                        ev = str(peek.get("hookEventName") or "").strip()
                        if ev == "PreToolUse" or (not ev and isinstance(peek.get("toolCall"), dict)):
                            _emit({"decision": "allow"})
                            return 0
                except Exception:
                    pass
            _emit({})
            return 0

        raw_bytes = _read_stdin_bytes()
        if not raw_bytes or not raw_bytes.strip():
            _emit({"decision": "allow"} if is_forced_pre_tool else {})
            return 0
        payload = json.loads(raw_bytes.decode("utf-8", errors="replace"))
        if not isinstance(payload, dict):
            _emit({"decision": "allow"} if is_forced_pre_tool else {})
            return 0
        result = dispatch(payload, forced_event=forced_event)
        _emit(result)
        return 0
    except Exception:
        # Strict fail-open so a hook bug can never break an active conversation
        _emit({"decision": "allow"} if is_forced_pre_tool else {})
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
