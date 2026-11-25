# -*- coding: utf-8 -*-
"""
EOQ Window Guard State & Radar Hint Helpers (`acerola_orion/plugins/eoq_guard.py`)
- 纯 stdlib 实现，零第三方依赖与零 GUI 依赖
- 读写 `~/.gemini/config/.eoq_guard_state.json`（支持 `EOQ_GUARD_STATE` 环境变量覆写）
- 内置线程安全 `(path, mtime_ns, size)` 读缓存与 Windows `PermissionError` 重试原子替换
- 统一格式化 CacheRadar EOQ 水位与免限提示文案
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import threading
import time
from typing import Any, Dict, Optional, Tuple

EOQ_GUARD_STATE_ENV = "EOQ_GUARD_STATE"
DEFAULT_EOQ_GUARD_STATE_PATH = Path.home() / ".gemini" / "config" / ".eoq_guard_state.json"

EOQ_COLD_WARN_TOKENS = 30_000
EOQ_HOT_SOFT_TOKENS = 95_000
EOQ_HOT_HARD_TOKENS = 120_000
EOQ_UNRESTRICTED_HINT = "🔓 本窗免限中 (免200K锁/冷窗拦截)"

_CACHE_LOCK = threading.Lock()
_STATE_CACHE: Dict[str, Any] = {
    "key": None,
    "data": {},
}


def get_eoq_guard_state_path() -> Path:
    """Resolve `.eoq_guard_state.json` path (honoring `EOQ_GUARD_STATE` override)."""
    override = os.environ.get(EOQ_GUARD_STATE_ENV, "").strip()
    if override:
        return Path(override)
    return DEFAULT_EOQ_GUARD_STATE_PATH


def invalidate_eoq_guard_cache() -> None:
    """Clear the in-memory mtime cache (useful for tests or external resets)."""
    with _CACHE_LOCK:
        _STATE_CACHE["key"] = None
        _STATE_CACHE["data"] = {}


def _read_json_dict_raw(path: Path) -> Dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}


def _read_state_cached(path: Path) -> Dict[str, Any]:
    try:
        st = path.stat()
    except OSError:
        with _CACHE_LOCK:
            _STATE_CACHE["key"] = None
            _STATE_CACHE["data"] = {}
        return {}

    cache_key = (str(path), int(st.st_mtime_ns), int(st.st_size))
    with _CACHE_LOCK:
        if _STATE_CACHE["key"] == cache_key and isinstance(_STATE_CACHE["data"], dict):
            return _STATE_CACHE["data"]

    data = _read_json_dict_raw(path)
    with _CACHE_LOCK:
        _STATE_CACHE["key"] = cache_key
        _STATE_CACHE["data"] = data
    return data


def _write_state_atomic(path: Path, data: Dict[str, Any]) -> None:
    """Atomically write JSON with Windows `PermissionError` backoff retry."""
    path.parent.mkdir(parents=True, exist_ok=True)
    nonce = f"{os.getpid()}.{int(time.time() * 1000) % 100000}.{threading.get_ident() % 10000}"
    tmp = path.with_suffix(f".tmp.{nonce}")
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    try:
        tmp.write_text(payload, encoding="utf-8")
        for attempt in range(5):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                if attempt < 4:
                    time.sleep(0.015 * (attempt + 1))
                else:
                    raise
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass

    try:
        st = path.stat()
        cache_key = (str(path), int(st.st_mtime_ns), int(st.st_size))
    except OSError:
        cache_key = None
    with _CACHE_LOCK:
        _STATE_CACHE["key"] = cache_key
        _STATE_CACHE["data"] = data if cache_key is not None else {}


def _find_conv_record(convs: Dict[str, Any], cid: str) -> Tuple[str, Optional[Dict[str, Any]]]:
    """Match a conversation record by exact (case-insensitive) or prefix `conv_id`."""
    norm = str(cid or "").strip()
    if not norm or not isinstance(convs, dict):
        return "", None
    low = norm.lower()
    for k, v in convs.items():
        if isinstance(k, str) and k.lower() == low:
            return k, v if isinstance(v, dict) else {}
    for k, v in convs.items():
        if isinstance(k, str) and k.lower().startswith(low):
            return k, v if isinstance(v, dict) else {}
    return norm, None


def _pick_latest_conv_id(convs: Dict[str, Any], *, prefer_unlocked: bool = False) -> str:
    if not isinstance(convs, dict) or not convs:
        return ""
    now_ep = time.time()

    if prefer_unlocked:
        unlocked = [
            (cid, rec)
            for cid, rec in convs.items()
            if isinstance(cid, str) and isinstance(rec, dict) and bool(rec.get("unrestricted", False))
        ]
        if unlocked:
            unlocked.sort(
                key=lambda kv: float(kv[1].get("unrestricted_at") or kv[1].get("updated_at") or 0.0),
                reverse=True,
            )
            return unlocked[0][0]
    else:
        pending = [
            (cid, rec)
            for cid, rec in convs.items()
            if isinstance(cid, str)
            and isinstance(rec, dict)
            and not bool(rec.get("unrestricted", False))
            and isinstance(rec.get("pending_cliff_prompts"), list)
            and len(rec.get("pending_cliff_prompts") or []) > 0
            and (now_ep - float(rec.get("updated_at") or 0.0)) <= 600.0
        ]
        if pending:
            pending.sort(key=lambda kv: float(kv[1].get("updated_at") or 0.0), reverse=True)
            return pending[0][0]

    valid = [
        (cid, rec)
        for cid, rec in convs.items()
        if isinstance(cid, str) and cid.strip() and isinstance(rec, dict)
    ]
    if not valid:
        return ""
    valid.sort(key=lambda kv: float(kv[1].get("updated_at") or 0.0), reverse=True)
    return valid[0][0]


def get_latest_eoq_conv_id(*, prefer_unlocked: bool = False) -> str:
    """Return the most relevant conversation id from `.eoq_guard_state.json`, or `""`."""
    state = _read_state_cached(get_eoq_guard_state_path())
    convs = state.get("conversations")
    if not isinstance(convs, dict):
        return ""
    return _pick_latest_conv_id(convs, prefer_unlocked=prefer_unlocked)


def is_eoq_window_unrestricted(cid: str) -> bool:
    """Return True if the specified conversation window is currently EOQ-unrestricted."""
    norm = str(cid or "").strip()
    if not norm:
        return False
    state = _read_state_cached(get_eoq_guard_state_path())
    convs = state.get("conversations")
    if not isinstance(convs, dict):
        return False
    _matched_key, rec = _find_conv_record(convs, norm)
    return bool(isinstance(rec, dict) and rec.get("unrestricted", False))


def set_eoq_window_unrestricted(
    cid: str,
    enabled: bool,
    source: str = "panel",
) -> Tuple[bool, str]:
    """Toggle per-window EOQ unrestricted flag and atomically persist `.eoq_guard_state.json`."""
    spath = get_eoq_guard_state_path()
    state = _read_json_dict_raw(spath)
    convs = state.get("conversations")
    if not isinstance(convs, dict):
        convs = {}
        state["conversations"] = convs

    norm_cid = str(cid or "").strip()
    if norm_cid:
        target_cid, existing = _find_conv_record(convs, norm_cid)
    else:
        target_cid = _pick_latest_conv_id(convs, prefer_unlocked=(not bool(enabled)))
        existing = convs.get(target_cid) if target_cid and isinstance(convs.get(target_cid), dict) else None

    if not target_cid:
        return False, "[EOQ 本窗免限] 未找到活跃会话窗口，请先在目标窗口发送任意消息后再试。"

    now_ep = time.time()
    rec: Dict[str, Any] = dict(existing) if isinstance(existing, dict) else {}
    flag = bool(enabled)
    rec["unrestricted"] = flag
    if flag:
        rec["unrestricted_source"] = str(source or "panel").strip() or "panel"
        rec["unrestricted_at"] = now_ep
        rec["unrestricted_notified"] = False
        rec["pending_cliff_prompts"] = []
    else:
        rec["unrestricted_source"] = ""
        rec["unrestricted_at"] = 0.0
        rec["unrestricted_notified"] = False
        rec["pending_cliff_prompts"] = []
    rec["updated_at"] = max(float(rec.get("updated_at") or 0.0), now_ep)
    convs[target_cid] = rec

    _write_state_atomic(spath, state)

    tok = int(rec.get("prompt_tokens") or 0)
    tok_lbl = f" (~{int(round(tok / 1000.0))}K)" if tok >= 1000 else ""
    cid8 = target_cid[:8]
    if flag:
        return True, f"🔓 已解锁窗口 {cid8}{tok_lbl}：本窗免 200K 弹窗锁与冷窗拦截，将一直做到完成！"
    return True, f"🔒 已恢复窗口 {cid8}{tok_lbl} 的 EOQ 水位与 180K/200K 弹窗保护。"


def format_eoq_radar_hint(state: Any, unrestricted: bool) -> str:
    """Format EOQ tier / unrestricted radar suffix for `CacheRadarState.detail_text`."""
    if bool(unrestricted):
        return EOQ_UNRESTRICTED_HINT

    if state is None:
        return ""
    phase = str(getattr(state, "phase", "") or "").strip().lower()
    if phase == "empty":
        return ""

    try:
        tokens = int(getattr(state, "context_tokens", 0) or 0)
    except (TypeError, ValueError):
        tokens = 0
    if tokens <= 0:
        return ""

    if phase == "cold":
        if tokens >= EOQ_COLD_WARN_TOKENS:
            return "🧊 EOQ 冷窗≥30K · 建议切新窗/交接防冷启动损耗"
        return ""

    if phase in ("active", "hot", "warning"):
        if tokens > EOQ_HOT_HARD_TOKENS:
            return "🔴 EOQ >120K 强收口区 · 建议立即落盘交接/切窗"
        if tokens >= EOQ_HOT_SOFT_TOKENS:
            return "🟡 EOQ 95K~120K 收口窗 · 建议阶段收口"
    return ""


__all__ = [
    "EOQ_GUARD_STATE_ENV",
    "DEFAULT_EOQ_GUARD_STATE_PATH",
    "EOQ_COLD_WARN_TOKENS",
    "EOQ_HOT_SOFT_TOKENS",
    "EOQ_HOT_HARD_TOKENS",
    "EOQ_UNRESTRICTED_HINT",
    "get_eoq_guard_state_path",
    "invalidate_eoq_guard_cache",
    "get_latest_eoq_conv_id",
    "is_eoq_window_unrestricted",
    "set_eoq_window_unrestricted",
    "format_eoq_radar_hint",
]
