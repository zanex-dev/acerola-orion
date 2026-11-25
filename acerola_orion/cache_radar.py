# -*- coding: utf-8 -*-
"""
Orion-CacheRadar 本地活跃会话上下文与 Prompt KV 缓存倒计时观测雷达 (acerola_orion/cache_radar.py)
- 100% 零外联、零侵入、纯被动只读观测：仅读取本地会话日志末尾计算缓存余温，绝不发送任何网络包或挂钩端口。
- 严格媒体隐私隔离：仅通过目录 mtime 缓存定位 `~/.gemini/antigravity/brain/<uuid>/.system_generated/logs/transcript.jsonl`
  与 `annotations/<uuid>.pbtxt`，绝不枚举会话根目录、绝不触碰 `.user_uploaded` 或任何二进制媒体文件。
- 增量无锁尾读：仅当目标 `transcript.jsonl` 的 `(mtime_ns, size)` 发生变化时，复用 `rpc.py`
  的 Win32 `FILE_SHARE_READ | WRITE | DELETE` 无锁读取末尾 `16KB`，免疫半行截断与 UTF-8 断字。
- 支持三种监控窗口策略：`smart_guard`（默认智能守大窗：≥100K 未冷窗口优先防抢焦）、`latest`（纯跟随最新活跃）、
  `pinned`（手动锁定指定窗口），内置 250s (4分10秒) 缓存余温状态机与 1s 纯内存零 I/O 倒计时推演。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import stat
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from .constants import get_agy_brain_dir
from .core.kernel import get_kernel
from .ledger import _decode_pbtxt_title, cache_read_count
from .rpc import read_shared_tail_bytes

UUID_DIR_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")

TAIL_READ_BYTES = 16 * 1024  # Strict 16KB tail read budget
MAX_CANDIDATE_SESSIONS = 8
COMPACT_COMMAND_TEXT = "/compact"

TARGET_MODE_SMART_GUARD = "smart_guard"
TARGET_MODE_LATEST = "latest"
TARGET_MODE_PINNED = "pinned"
VALID_TARGET_MODES: Tuple[str, ...] = (
    TARGET_MODE_SMART_GUARD,
    TARGET_MODE_LATEST,
    TARGET_MODE_PINNED,
)

VALID_HUD_MODES: Tuple[str, ...] = ("smart", "always", "off")

CACHE_SAFE_TTL_SEC = 250.0  # 4m10s empirical prompt cache safety horizon
ACTIVE_GRACE_SEC = 5.0  # < 5s since response -> ACTIVE
WARN_IDLE_SEC = 190.0  # 190s ~ 250s -> last 60s warning window
SMART_HUD_MIN_TOKENS = 100_000  # Smart HUD only pops when context >= 100K
SMART_HUD_COLD_HIDE_SEC = 15.0  # Retract 15s after cooling (> 265s)
LARGE_CONTEXT_HINT_TOKENS = 100_000  # Suggest new window or /compact when cold >= 100K
TIER_2X_WARN_TOKENS = 180_000  # Approaching 200K long-context 2x pricing cliff (within 20K)
TIER_2X_THRESHOLD_TOKENS = 200_000  # >= 200K triggers 2x long-context pricing tier

PRICING_TIER_NORMAL = "1x"
PRICING_TIER_NEAR_2X = "near_2x"
PRICING_TIER_OVER_2X = "2x"

EXECUTING_STEP_MAX_AGE_SEC = 600.0  # 10-min cap for explicit RUNNING/STREAMING steps
INFLIGHT_TURN_MAX_AGE_SEC = 180.0  # 3-min cap for USER_INPUT / GENERIC / tool_calls awaiting response
PARTIAL_FLUSH_MAX_AGE_SEC = 60.0  # Cap for trailing mid-flush half-line detection

PHASE_EMPTY = "empty"
PHASE_ACTIVE = "active"
PHASE_HOT = "hot"
PHASE_WARNING = "warning"
PHASE_COLD = "cold"

_NON_TERMINAL_STATUSES = frozenset(
    {"RUNNING", "PENDING", "IN_PROGRESS", "STREAMING", "EXECUTING", "GENERATING", "THINKING"}
)
_ABORTED_OR_FAILED_STATUSES = frozenset(
    {"ERROR", "FAILED", "CANCELLED", "CANCELED", "ABORTED", "TIMED_OUT", "TIMEOUT"}
)


def _reject_nonfinite(tok: str) -> Any:
    raise ValueError(f"Non-finite JSON constant in transcript: {tok}")


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


def _try_parse_iso_epoch(iso_str: str) -> Optional[float]:
    if not iso_str or not isinstance(iso_str, str):
        return None
    s = iso_str.strip()
    if not s:
        return None
    try:
        if s.endswith("Z") or s.endswith("z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        ep = dt.timestamp()
        return ep if math.isfinite(ep) else None
    except Exception:
        return None


def _has_reparse_or_symlink(p: Path) -> bool:
    try:
        if os.path.islink(p):
            return True
        if hasattr(os.path, "isjunction") and os.path.isjunction(p):
            return True
        st = os.lstat(p)
        file_attrs = getattr(st, "st_file_attributes", 0)
        if file_attrs & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
            return True
        return bool(stat.S_ISLNK(st.st_mode))
    except OSError:
        return True


def _is_safe_contained_transcript(transcript_p: Path, brain_dir: Path) -> bool:
    """
    Verify every path segment from `brain_dir` down to `transcript.jsonl` is free of symlinks,
    NTFS junctions, and reparse points, and resolves strictly inside `brain_dir`.
    """
    try:
        if not os.path.lexists(brain_dir) or _has_reparse_or_symlink(brain_dir):
            return False
        root_res = brain_dir.resolve(strict=True)
        rel_parts = transcript_p.relative_to(brain_dir).parts
        cur = brain_dir
        for part in rel_parts:
            cur = cur / part
            if not os.path.lexists(cur) or _has_reparse_or_symlink(cur):
                return False
        target_res = transcript_p.resolve(strict=True)
        return target_res.is_relative_to(root_res)
    except Exception:
        return False


def format_tokens_compact(tokens: int) -> str:
    """Format token count into compact badge text (e.g. 228000 -> '228K', 1250000 -> '1.2M')."""
    n = max(0, int(tokens))
    if n <= 0:
        return "0K"
    if n < 1000:
        return str(n)
    if n >= 999_500:
        return f"{n / 1_000_000.0:.1f}M"
    return f"{int(round(n / 1000.0))}K"


def format_mmss(seconds: float, pad_min: bool = False) -> str:
    """Format remaining seconds as `M:SS` (`1:45`, `0:42`) or zero-padded `MM:SS` (`01:45`)."""
    rem = max(0, int(math.ceil(float(seconds) - 1e-9))) if seconds > 0 else 0
    mins, secs = divmod(rem, 60)
    if pad_min:
        return f"{mins:02d}:{secs:02d}"
    return f"{mins}:{secs:02d}"


@dataclass
class TranscriptTailSnapshot:
    conv_id: str = ""
    mtime_ns: int = 0
    file_size: int = 0
    has_planner_response: bool = False
    planner_step_index: int = -1
    planner_created_at: str = ""
    planner_epoch: float = 0.0
    input_tokens: int = 0
    cache_read_tokens: int = 0
    output_tokens: int = 0
    prompt_tokens: int = 0
    context_tokens: int = 0
    cache_hit_rate: float = 0.0
    last_step_index: int = -1
    last_step_type: str = ""
    last_step_status: str = ""
    last_step_created_at: str = ""
    last_step_epoch: float = 0.0
    last_step_has_tool_calls: bool = False
    trailing_partial: bool = False


@dataclass
class CacheRadarState:
    phase: str = PHASE_EMPTY
    conv_id: str = ""
    conv_title: str = ""
    target_mode: str = TARGET_MODE_SMART_GUARD
    pinned_conv_id: str = ""
    smart_guarded: bool = False
    context_tokens: int = 0
    context_label: str = "0K"
    input_tokens: int = 0
    cache_read_tokens: int = 0
    output_tokens: int = 0
    cache_hit_rate: float = 0.0
    cache_hit_pct: int = 0
    planner_created_at: str = ""
    last_step_type: str = ""
    last_step_status: str = ""
    is_executing: bool = False
    idle_sec: float = 0.0
    remaining_sec: float = 0.0
    ttl_fraction: float = 0.0
    badge_text: str = "⚪ 暂无活跃会话上下文"
    capsule_text: str = "⚪ 缓存雷达待命"
    detail_text: str = "等待 Antigravity 活跃对话采样（纯本地只读观测）..."
    tooltip_suffix: str = "KV: 暂无活跃会话"
    should_pop_smart_hud: bool = False
    pricing_tier: str = PRICING_TIER_NORMAL
    is_near_2x: bool = False
    is_over_2x: bool = False
    tokens_to_2x: int = TIER_2X_THRESHOLD_TOKENS
    tier_badge_text: str = ""
    hud_tier_tag: str = ""
    tier_alert_text: str = ""
    extensions: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class RadarSessionCandidate:
    conv_id: str
    title: str
    context_tokens: int
    context_label: str
    phase: str
    remaining_sec: float
    idle_sec: float
    mtime_ns: int
    is_pinned: bool = False
    is_current: bool = False
    menu_label: str = ""
    pricing_tier: str = PRICING_TIER_NORMAL

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _clean_single_line_title(raw_title: str, max_chars: int = 20) -> str:
    if not raw_title or not isinstance(raw_title, str):
        return ""
    collapsed = " ".join(raw_title.split()).strip()
    if not collapsed:
        return ""
    if max_chars > 2 and len(collapsed) > max_chars:
        return collapsed[: max_chars - 1] + "…"
    return collapsed


def parse_transcript_tail_bytes(
    raw: bytes,
    truncated_head: bool = False,
    conv_id: str = "",
    mtime_ns: int = 0,
    file_size: int = 0,
) -> TranscriptTailSnapshot:
    """
    Parse up to `16KB` of tail bytes from `transcript.jsonl` safely:
    - When `truncated_head=True`, discards everything through the first `\\n` so mid-UTF8 bytes or
      partial JSON fragments at the start of the tail window can never corrupt parsing.
    - Splits strictly on `0x0A` (`\\n`) rather than `str.splitlines()` so Unicode `\\u2028`/`\\u2029`
      inside JSON string values never break a valid JSONL record in half.
    - Gracefully tolerates an incomplete trailing half-line (concurrent mid-flush write without closing `}`)
      by setting `trailing_partial=True` while preserving the last complete `PLANNER_RESPONSE` token metrics.
    """
    snap = TranscriptTailSnapshot(conv_id=conv_id, mtime_ns=mtime_ns, file_size=file_size)
    if not raw:
        return snap

    work = raw
    if truncated_head:
        first_nl = work.find(b"\n")
        if first_nl == -1:
            snap.trailing_partial = True
            return snap
        work = work[first_nl + 1 :]
        if not work:
            snap.trailing_partial = True
            return snap

    raw_lines = work.split(b"\n")
    if work.endswith(b"\n") and raw_lines and raw_lines[-1] == b"":
        raw_lines.pop()

    nonempty_indices = [i for i, b_ln in enumerate(raw_lines) if b_ln.strip()]
    if not nonempty_indices:
        return snap
    last_nonempty_idx = nonempty_indices[-1]

    for seq_no, idx in enumerate(nonempty_indices):
        raw_ln = raw_lines[idx].strip()
        line_str = raw_ln.decode("utf-8", errors="replace")
        try:
            obj = json.loads(line_str, parse_constant=_reject_nonfinite)
        except Exception:
            if idx == last_nonempty_idx:
                snap.trailing_partial = True
            continue
        if not isinstance(obj, dict):
            if idx == last_nonempty_idx:
                snap.trailing_partial = True
            continue

        step_type = str(obj.get("type") or "").strip()
        has_step_idx = "step_index" in obj and not isinstance(obj.get("step_index"), bool)
        if not step_type and not has_step_idx:
            continue

        raw_sidx = obj.get("step_index")
        try:
            step_idx = int(raw_sidx) if has_step_idx and raw_sidx is not None else seq_no
        except (TypeError, ValueError):
            step_idx = seq_no

        step_status = str(obj.get("status") or "").strip()
        raw_created = obj.get("created_at")
        created_at = raw_created.strip() if isinstance(raw_created, str) else ""
        step_ep = _try_parse_iso_epoch(created_at) or 0.0
        raw_tc = obj.get("tool_calls")
        has_tc = isinstance(raw_tc, list) and len(raw_tc) > 0

        snap.last_step_index = step_idx
        snap.last_step_type = step_type
        snap.last_step_status = step_status
        snap.last_step_created_at = created_at
        snap.last_step_epoch = step_ep
        snap.last_step_has_tool_calls = has_tc
        snap.trailing_partial = False

        if step_type == "PLANNER_RESPONSE":
            in_tok = _safe_nonneg_int(obj.get("input_tokens"))
            cache_tok = cache_read_count(obj)
            out_tok = _safe_nonneg_int(obj.get("output_tokens"))
            ctx_tok = in_tok + cache_tok + out_tok
            if ctx_tok > 0:
                prompt_tok = in_tok + cache_tok
                snap.has_planner_response = True
                snap.planner_step_index = step_idx
                snap.planner_created_at = created_at
                snap.planner_epoch = step_ep
                snap.input_tokens = in_tok
                snap.cache_read_tokens = cache_tok
                snap.output_tokens = out_tok
                snap.prompt_tokens = prompt_tok
                snap.context_tokens = ctx_tok
                snap.cache_hit_rate = (cache_tok / float(prompt_tok)) if prompt_tok > 0 else 0.0

    return snap


def _merge_carry_forward_planner(
    prev: Optional[TranscriptTailSnapshot],
    curr: TranscriptTailSnapshot,
) -> TranscriptTailSnapshot:
    """
    If the same conversation grew append-only (`curr.file_size >= prev.file_size`) with large tool/user steps
    pushing the previous `PLANNER_RESPONSE` out of the 16KB tail window, preserve the cached `PLANNER_RESPONSE`
    token metrics without reading beyond 16KB.
    """
    if (
        prev is not None
        and prev.has_planner_response
        and not curr.has_planner_response
        and curr.conv_id
        and curr.conv_id == prev.conv_id
        and curr.file_size >= prev.file_size
    ):
        curr.has_planner_response = True
        curr.planner_step_index = prev.planner_step_index
        curr.planner_created_at = prev.planner_created_at
        curr.planner_epoch = prev.planner_epoch
        curr.input_tokens = prev.input_tokens
        curr.cache_read_tokens = prev.cache_read_tokens
        curr.output_tokens = prev.output_tokens
        curr.prompt_tokens = prev.prompt_tokens
        curr.context_tokens = prev.context_tokens
        curr.cache_hit_rate = prev.cache_hit_rate
    return curr


def normalize_target_mode(mode: Any, pinned_conv_id: Any = "") -> Tuple[str, str]:
    raw_pin = str(pinned_conv_id or "").strip().lower()
    pin_id = raw_pin if UUID_DIR_RE.match(raw_pin) else ""
    raw_mode = str(mode or "").strip().lower()
    if raw_mode not in VALID_TARGET_MODES:
        raw_mode = TARGET_MODE_SMART_GUARD
    if raw_mode == TARGET_MODE_PINNED and not pin_id:
        raw_mode = TARGET_MODE_SMART_GUARD
    return raw_mode, pin_id


def normalize_hud_mode(mode: Any) -> str:
    m = str(mode or "").strip().lower()
    return m if m in VALID_HUD_MODES else "smart"


def _evaluate_pricing_tier(context_tokens: int, ctx_lbl: str) -> Tuple[str, bool, bool, int, str, str, str]:
    """
    Evaluate 200K long-context 2x pricing cliff:
    - `>= 200,000`: active 2x pricing (`"2x"`)
    - `180,000 .. 199,999`: approaching 2x cliff (`"near_2x"`, within 20K)
    - `< 180,000`: standard 1x tier (`"1x"`)
    Returns `(pricing_tier, is_near_2x, is_over_2x, tokens_to_2x, tier_badge_text, hud_tier_tag, tier_alert_text)`.
    """
    n = max(0, int(context_tokens))
    if n <= 0:
        return (PRICING_TIER_NORMAL, False, False, TIER_2X_THRESHOLD_TOKENS, "", "", "")
    if n >= TIER_2X_THRESHOLD_TOKENS:
        return (
            PRICING_TIER_OVER_2X,
            False,
            True,
            0,
            "💸 已超200K · 双倍计价(2x)",
            "💸2x计价",
            f"💸 双倍计价生效中：当前 {ctx_lbl} 已超 200K 门槛，再聊下去每轮均按双倍 (2x) 费率扣减配额！建议立即 {COMPACT_COMMAND_TEXT} 或开新窗口",
        )
    if n >= TIER_2X_WARN_TOKENS:
        rem_tok = max(1, TIER_2X_THRESHOLD_TOKENS - n)
        gap_lbl = f"{max(1, int(math.ceil(rem_tok / 1000.0)))}K" if rem_tok >= 1000 else str(rem_tok)
        disp_ctx = f"{n / 1000.0:.1f}K" if n >= 199_500 else ctx_lbl
        return (
            PRICING_TIER_NEAR_2X,
            True,
            False,
            rem_tok,
            f"⚠️ 逼近200K (差{gap_lbl}翻倍)",
            f"⚠️差{gap_lbl}翻倍",
            f"⚠️ 逼近双倍计价线：当前 {disp_ctx} 距 200K 门槛仅差 {gap_lbl}，再聊下去即将触发双倍 (2x) 计价！建议准备 {COMPACT_COMMAND_TEXT} 或开新窗口",
        )
    return (PRICING_TIER_NORMAL, False, False, TIER_2X_THRESHOLD_TOKENS - n, "", "", "")


def _format_candidate_menu_label(cand_state: CacheRadarState, title: str, conv_id: str) -> str:
    short_id = conv_id[:8] if conv_id else "-"
    clean_t = _clean_single_line_title(title, max_chars=22) or "未命名窗口"
    tier_mark = "·2x" if cand_state.is_over_2x else ("·近200K" if cand_state.is_near_2x else "")
    if cand_state.phase == PHASE_ACTIVE:
        icon, tag = "⚡", f"{cand_state.context_label}{tier_mark}·推理中"
    elif cand_state.phase == PHASE_HOT:
        icon, tag = "🔥", f"{cand_state.context_label}{tier_mark}·剩{format_mmss(cand_state.remaining_sec)}"
    elif cand_state.phase == PHASE_WARNING:
        icon, tag = "⚠️", f"{cand_state.context_label}{tier_mark}·剩{format_mmss(cand_state.remaining_sec)}"
    elif cand_state.phase == PHASE_COLD:
        icon, tag = "❄️", f"{cand_state.context_label}{tier_mark}·已冷"
    else:
        icon, tag = "⚪", "新窗口"
    return f"{icon} [{tag}] {clean_t} ({short_id})"


def evaluate_cache_radar_state(
    snap: Optional[TranscriptTailSnapshot],
    now_epoch: Optional[float] = None,
    conv_title: str = "",
    target_mode: str = TARGET_MODE_SMART_GUARD,
    pinned_conv_id: str = "",
    smart_guarded: bool = False,
) -> CacheRadarState:
    """
    Pure, zero-I/O evaluator mapping `(TranscriptTailSnapshot, now_epoch)` to `CacheRadarState`
    according to the 250s (4分10秒) prompt KV cache TTL state machine.
    """
    now_ep = float(now_epoch) if now_epoch is not None else time.time()
    norm_mode, norm_pin = normalize_target_mode(target_mode, pinned_conv_id)
    clean_full_title = " ".join(str(conv_title or "").split()).strip()
    short_title = _clean_single_line_title(clean_full_title, max_chars=18)

    if snap is None or (not snap.has_planner_response and not snap.last_step_type):
        return CacheRadarState(
            phase=PHASE_EMPTY,
            target_mode=norm_mode,
            pinned_conv_id=norm_pin,
        )

    is_pinned_match = bool(
        norm_mode == TARGET_MODE_PINNED and norm_pin and snap.conv_id and snap.conv_id.lower() == norm_pin
    )
    pin_badge_prefix = "📌 " if is_pinned_match else ""

    mtime_ep = (snap.mtime_ns / 1_000_000_000.0) if snap.mtime_ns > 0 else 0.0
    step_ep = snap.last_step_epoch if snap.last_step_epoch > 0 else (snap.planner_epoch or mtime_ep)
    step_age = max(0.0, now_ep - step_ep) if step_ep > 0 else 0.0

    st_up = snap.last_step_status.upper()
    is_executing = False
    if st_up in _NON_TERMINAL_STATUSES and step_age <= EXECUTING_STEP_MAX_AGE_SEC:
        is_executing = True
    elif snap.trailing_partial:
        partial_ref = mtime_ep if mtime_ep > 0 else step_ep
        if partial_ref <= 0 or max(0.0, now_ep - partial_ref) <= PARTIAL_FLUSH_MAX_AGE_SEC:
            is_executing = True
    elif st_up not in _ABORTED_OR_FAILED_STATUSES:
        if snap.last_step_type in ("USER_INPUT", "GENERIC", "TOOL_RESPONSE") and step_age <= INFLIGHT_TURN_MAX_AGE_SEC:
            is_executing = True
        elif snap.last_step_type == "PLANNER_RESPONSE" and snap.last_step_has_tool_calls and step_age <= INFLIGHT_TURN_MAX_AGE_SEC:
            is_executing = True

    short_conv = snap.conv_id[:8] if snap.conv_id else "-"
    conv_header = f"会话 {short_conv}「{short_title}」" if short_title else f"会话 {short_conv}"
    if is_pinned_match:
        conv_header = f"📌锁定 · {conv_header}"
    elif smart_guarded:
        conv_header = f"🛡️守大窗 · {conv_header}"

    if not snap.has_planner_response:
        if is_executing:
            badge = f"{pin_badge_prefix}⚡ 0K · 推理活跃中"
            return CacheRadarState(
                phase=PHASE_ACTIVE,
                conv_id=snap.conv_id,
                conv_title=clean_full_title,
                target_mode=norm_mode,
                pinned_conv_id=norm_pin,
                smart_guarded=bool(smart_guarded and not is_pinned_match),
                context_tokens=0,
                context_label="0K",
                last_step_type=snap.last_step_type,
                last_step_status=snap.last_step_status,
                is_executing=True,
                remaining_sec=CACHE_SAFE_TTL_SEC,
                ttl_fraction=1.0,
                badge_text=badge,
                capsule_text=badge,
                detail_text=f"{conv_header} · 新对话首轮推理执行中（本地只读观测）",
                tooltip_suffix=f"KV: {badge}",
                should_pop_smart_hud=False,
            )
        return CacheRadarState(
            phase=PHASE_EMPTY,
            conv_id=snap.conv_id,
            conv_title=clean_full_title,
            target_mode=norm_mode,
            pinned_conv_id=norm_pin,
        )

    resp_ep = snap.planner_epoch if snap.planner_epoch > 0 else mtime_ep
    idle_sec = 0.0 if is_executing else (max(0.0, now_ep - resp_ep) if resp_ep > 0 else 0.0)

    ctx_lbl = format_tokens_compact(snap.context_tokens)
    (
        pricing_tier,
        is_near_2x,
        is_over_2x,
        tokens_to_2x,
        tier_badge_text,
        hud_tier_tag,
        tier_alert_text,
    ) = _evaluate_pricing_tier(snap.context_tokens, ctx_lbl)
    hit_pct = int(round(snap.cache_hit_rate * 100.0))
    tok_breakdown = (
        f"{conv_header} | 新鲜In={snap.input_tokens:,} · 缓存读={snap.cache_read_tokens:,} · Out={snap.output_tokens:,}"
    )

    if is_executing or idle_sec < ACTIVE_GRACE_SEC:
        phase = PHASE_ACTIVE
        remaining_sec = CACHE_SAFE_TTL_SEC
        ttl_fraction = 1.0
        badge_text = f"{pin_badge_prefix}⚡ {ctx_lbl} · 推理活跃中"
        capsule_text = f"{pin_badge_prefix}⚡ {ctx_lbl} · 推理活跃中"
        detail_text = f"{tok_breakdown} (上轮缓存读取占比{hit_pct}%，剩余时间按本地估算)"
    elif idle_sec < WARN_IDLE_SEC:
        phase = PHASE_HOT
        remaining_sec = max(0.0, CACHE_SAFE_TTL_SEC - idle_sec)
        ttl_fraction = remaining_sec / CACHE_SAFE_TTL_SEC
        badge_text = f"{pin_badge_prefix}🔥 {ctx_lbl} (上轮缓存读{hit_pct}%) · 预计 {format_mmss(remaining_sec, pad_min=False)}"
        capsule_text = f"{pin_badge_prefix}🔥 {ctx_lbl} · {format_mmss(remaining_sec, pad_min=True)}"
        detail_text = f"{tok_breakdown} | 250s 本地缓存余温估算倒计时中"
    elif idle_sec <= CACHE_SAFE_TTL_SEC:
        phase = PHASE_WARNING
        remaining_sec = max(0.0, CACHE_SAFE_TTL_SEC - idle_sec)
        ttl_fraction = remaining_sec / CACHE_SAFE_TTL_SEC
        badge_text = f"{pin_badge_prefix}⚠️ {ctx_lbl} 快凉 · 预计剩 {format_mmss(remaining_sec, pad_min=False)}"
        capsule_text = f"{pin_badge_prefix}⚠️ {ctx_lbl} 快凉 · 预计剩 {format_mmss(remaining_sec, pad_min=False)}"
        detail_text = f"{tok_breakdown} | ⏰ 本地预计剩余不足 60 秒；服务端缓存状态未知。"
    else:
        phase = PHASE_COLD
        remaining_sec = 0.0
        ttl_fraction = 0.0
        badge_text = f"{pin_badge_prefix}❄️ {ctx_lbl} 预计已冷 · 上轮上下文~{ctx_lbl}"
        capsule_text = f"{pin_badge_prefix}❄️ {ctx_lbl} 预计已冷 · 上轮上下文~{ctx_lbl}"
        if snap.context_tokens >= TIER_2X_THRESHOLD_TOKENS:
            cold_tip = "💸 已超 200K 双倍计价线且缓存已冷：续聊将触发 2x 全价重算！建议先执行 /compact 或直接开新窗口"
        elif snap.context_tokens >= TIER_2X_WARN_TOKENS:
            cold_tip = "⚠️ 逼近 200K 双倍计价线且缓存已冷：续聊易跨入 2x 费率区，建议先执行 /compact 或直接开新窗口"
        elif snap.context_tokens >= LARGE_CONTEXT_HINT_TOKENS:
            cold_tip = "💡 大上下文已过 250s 余温线：换新话题建议开新窗口，同任务建议先 /compact"
        else:
            cold_tip = "闲置已超本地 4 分 10 秒的估算窗口"
        detail_text = f"{tok_breakdown} | {cold_tip}"

    should_pop_smart = (
        not is_executing
        and (snap.context_tokens >= SMART_HUD_MIN_TOKENS or is_pinned_match)
        and WARN_IDLE_SEC <= idle_sec <= (CACHE_SAFE_TTL_SEC + SMART_HUD_COLD_HIDE_SEC)
    )
    tip_tier_suffix = " [2x]" if is_over_2x else (" [近200K]" if is_near_2x else "")

    return CacheRadarState(
        phase=phase,
        conv_id=snap.conv_id,
        conv_title=clean_full_title,
        target_mode=norm_mode,
        pinned_conv_id=norm_pin,
        smart_guarded=bool(smart_guarded and not is_pinned_match),
        context_tokens=snap.context_tokens,
        context_label=ctx_lbl,
        input_tokens=snap.input_tokens,
        cache_read_tokens=snap.cache_read_tokens,
        output_tokens=snap.output_tokens,
        cache_hit_rate=snap.cache_hit_rate,
        cache_hit_pct=hit_pct,
        planner_created_at=snap.planner_created_at,
        last_step_type=snap.last_step_type,
        last_step_status=snap.last_step_status,
        is_executing=is_executing,
        idle_sec=idle_sec,
        remaining_sec=remaining_sec,
        ttl_fraction=ttl_fraction,
        badge_text=badge_text,
        capsule_text=capsule_text,
        detail_text=detail_text,
        tooltip_suffix=f"KV: {badge_text}{tip_tier_suffix}",
        should_pop_smart_hud=should_pop_smart,
        pricing_tier=pricing_tier,
        is_near_2x=is_near_2x,
        is_over_2x=is_over_2x,
        tokens_to_2x=tokens_to_2x,
        tier_badge_text=tier_badge_text,
        hud_tier_tag=hud_tier_tag,
        tier_alert_text=tier_alert_text,
    )


class CacheRadarSampler:
    """
    Stateful, thread-safe, zero-egress active session KV cache radar sampler.
    - Uses `brain_dir` `mtime_ns` caching so UUID directory enumeration only runs when folders are added/removed.
    - Directly stats `<uuid>/.system_generated/logs/transcript.jsonl` (never enumerating `<uuid>/` contents or
      touching `.user_uploaded` / media files) and safely decodes `annotations/<uuid>.pbtxt` titles.
    - Supports three session targeting modes:
      1. `smart_guard` (default): tracks newest active window, EXCEPT when a `>=100K` heavy window is still warm
         (or in its 15s post-cold alert window) while the newest window is `<100K` — shields the heavy window!
      2. `latest`: strictly follows the most recently written `transcript.jsonl`.
      3. `pinned`: locks onto user-chosen `pinned_conv_id` (gracefully falls back to `smart_guard` if removed).
    - Calls `read_shared_tail_bytes(..., max_bytes=16384)` ONLY when a candidate's `(mtime_ns, size)` changed.
    - Supports zero-I/O 1-second UI countdown ticks via `evaluate_cached(now_epoch=...)`.
    """

    def __init__(
        self,
        brain_dir: Optional[Path] = None,
        annotations_dir: Optional[Path] = None,
        tail_bytes: int = TAIL_READ_BYTES,
        target_mode: str = TARGET_MODE_SMART_GUARD,
        pinned_conv_id: str = "",
    ) -> None:
        self.brain_dir = brain_dir or get_agy_brain_dir()
        self.annotations_dir = annotations_dir if annotations_dir is not None else (self.brain_dir.parent / "annotations")
        self.tail_bytes = max(512, int(tail_bytes))
        self._lock = threading.RLock()

        norm_mode, norm_pin = normalize_target_mode(target_mode, pinned_conv_id)
        self._target_mode: str = norm_mode
        self._pinned_conv_id: str = norm_pin

        self._brain_mtime_ns: Optional[int] = None
        self._uuid_dirs: Dict[str, Path] = {}

        self._snap_cache: Dict[str, Tuple[int, int, TranscriptTailSnapshot]] = {}
        self._title_cache: Dict[str, Tuple[int, int, str]] = {}

        self._active_sig: Optional[Tuple[str, int, int]] = None
        self._cached_snap: Optional[TranscriptTailSnapshot] = None
        self._cached_title: str = ""
        self._latest_snap: Optional[TranscriptTailSnapshot] = None
        self._latest_title: str = ""
        self._selected_smart_guarded: bool = False
        self._cached_extensions: Dict[str, Any] = {}
        self._cached_extensions_conv_id: str = ""
        self._cached_detail_suffix: str = ""
        self.tail_read_count: int = 0

    def get_target(self) -> Tuple[str, str]:
        with self._lock:
            return self._target_mode, self._pinned_conv_id

    def set_target(self, target_mode: str, pinned_conv_id: str = "") -> Tuple[str, str]:
        with self._lock:
            norm_mode, norm_pin = normalize_target_mode(target_mode, pinned_conv_id)
            self._target_mode = norm_mode
            self._pinned_conv_id = norm_pin
            return norm_mode, norm_pin

    def _resolve_title_locked(self, conv_id: str) -> str:
        if not conv_id or not UUID_DIR_RE.match(conv_id):
            return ""
        p = self.annotations_dir / f"{conv_id}.pbtxt"
        try:
            st = os.lstat(p)
            sig = (int(st.st_mtime_ns), int(st.st_size))
        except OSError:
            self._title_cache.pop(conv_id, None)
            return ""
        hit = self._title_cache.get(conv_id)
        if hit is not None and (hit[0], hit[1]) == sig:
            return hit[2]
        decoded = _decode_pbtxt_title(self.annotations_dir, conv_id)
        self._title_cache[conv_id] = (sig[0], sig[1], decoded)
        if len(self._title_cache) > 64:
            oldest_k = next(iter(self._title_cache))
            if oldest_k != conv_id:
                self._title_cache.pop(oldest_k, None)
        return decoded

    def _refresh_uuid_dirs_locked(self) -> None:
        if not os.path.lexists(self.brain_dir) or _has_reparse_or_symlink(self.brain_dir):
            self._brain_mtime_ns = None
            self._uuid_dirs.clear()
            return
        try:
            st_brain = self.brain_dir.stat()
        except OSError:
            return

        if self._brain_mtime_ns == st_brain.st_mtime_ns and self._uuid_dirs:
            return

        try:
            entries = list(os.scandir(self.brain_dir))
        except OSError:
            return

        valid_dirs: Dict[str, Path] = {}
        for entry in entries:
            try:
                if entry.is_symlink() or not UUID_DIR_RE.match(entry.name):
                    continue
                if not entry.is_dir(follow_symlinks=False):
                    continue
            except OSError:
                continue
            conv_dir = Path(entry.path)
            if _has_reparse_or_symlink(conv_dir):
                continue
            valid_dirs[entry.name] = conv_dir

        self._uuid_dirs = valid_dirs
        self._brain_mtime_ns = int(st_brain.st_mtime_ns)
        for dead_s in [k for k in self._snap_cache if k not in valid_dirs]:
            del self._snap_cache[dead_s]

    def _collect_ranked_transcripts_locked(self) -> List[Tuple[int, int, str, Path]]:
        self._refresh_uuid_dirs_locked()
        if not self._uuid_dirs:
            return []

        found: List[Tuple[int, int, str, Path]] = []
        for conv_id, conv_dir in list(self._uuid_dirs.items()):
            transcript_p = conv_dir / ".system_generated" / "logs" / "transcript.jsonl"
            try:
                st = os.lstat(transcript_p)
            except OSError:
                continue

            if stat.S_ISLNK(st.st_mode) or (
                getattr(st, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
            ):
                continue
            if not stat.S_ISREG(st.st_mode) or st.st_size <= 0:
                continue

            found.append((int(st.st_mtime_ns), int(st.st_size), conv_id, transcript_p))

        found.sort(key=lambda item: item[:3], reverse=True)
        return found

    def _read_or_reuse_snap_locked(
        self,
        conv_id: str,
        transcript_p: Path,
        mtime_ns: int,
        fsize: int,
    ) -> TranscriptTailSnapshot:
        cached = self._snap_cache.get(conv_id)
        if cached is not None and cached[0] == mtime_ns and cached[1] == fsize:
            return cached[2]

        raw_tail, trunc_head = read_shared_tail_bytes(transcript_p, max_bytes=self.tail_bytes)
        self.tail_read_count += 1
        parsed = parse_transcript_tail_bytes(
            raw_tail,
            truncated_head=trunc_head,
            conv_id=conv_id,
            mtime_ns=mtime_ns,
            file_size=fsize,
        )
        prev_snap = cached[2] if cached is not None else None
        merged = _merge_carry_forward_planner(prev_snap, parsed)
        self._snap_cache[conv_id] = (mtime_ns, fsize, merged)
        if len(self._snap_cache) > 24:
            for k in list(self._snap_cache.keys()):
                if k != conv_id and k != self._pinned_conv_id:
                    self._snap_cache.pop(k, None)
                    break
        return merged

    def _select_target_snap_locked(
        self,
        now_ep: float,
    ) -> Tuple[Optional[TranscriptTailSnapshot], Optional[TranscriptTailSnapshot], bool]:
        ranked = self._collect_ranked_transcripts_locked()
        if not ranked:
            return None, None, False

        if self._target_mode == TARGET_MODE_PINNED and self._pinned_conv_id:
            target_low = self._pinned_conv_id.lower()
            for mtime_ns, fsize, cid, tpath in ranked:
                if cid.lower() == target_low and _is_safe_contained_transcript(tpath, self.brain_dir):
                    pinned_snap = self._read_or_reuse_snap_locked(cid, tpath, mtime_ns, fsize)
                    return pinned_snap, pinned_snap, False

        top_entry: Optional[Tuple[int, int, str, Path]] = None
        top_idx = -1
        for idx, item in enumerate(ranked):
            if _is_safe_contained_transcript(item[3], self.brain_dir):
                top_entry = item
                top_idx = idx
                break
        if top_entry is None:
            return None, None, False

        top_mtime_ns, top_fsize, top_cid, top_path = top_entry
        top_snap = self._read_or_reuse_snap_locked(top_cid, top_path, top_mtime_ns, top_fsize)

        if self._target_mode == TARGET_MODE_LATEST:
            return top_snap, top_snap, False

        top_eval = evaluate_cache_radar_state(top_snap, now_epoch=now_ep)
        if top_eval.context_tokens >= SMART_HUD_MIN_TOKENS and (
            top_eval.phase in (PHASE_ACTIVE, PHASE_HOT, PHASE_WARNING) or top_eval.should_pop_smart_hud
        ):
            return top_snap, top_snap, False

        live_horizon_sec = CACHE_SAFE_TTL_SEC + SMART_HUD_COLD_HIDE_SEC + 5.0
        live_cutoff_ns = int(max(0.0, now_ep - live_horizon_sec) * 1_000_000_000)

        best_guard_snap: Optional[TranscriptTailSnapshot] = None
        best_guard_rank: Optional[Tuple[int, int, int]] = None

        inspected = 0
        for mtime_ns, fsize, cid, tpath in ranked[top_idx + 1 :]:
            if mtime_ns < live_cutoff_ns:
                break
            if inspected >= MAX_CANDIDATE_SESSIONS:
                break
            if not _is_safe_contained_transcript(tpath, self.brain_dir):
                continue
            inspected += 1
            cand_snap = self._read_or_reuse_snap_locked(cid, tpath, mtime_ns, fsize)
            cand_eval = evaluate_cache_radar_state(cand_snap, now_epoch=now_ep)
            is_guardable = cand_eval.context_tokens >= SMART_HUD_MIN_TOKENS and (
                cand_eval.phase in (PHASE_ACTIVE, PHASE_HOT, PHASE_WARNING) or cand_eval.should_pop_smart_hud
            )
            if is_guardable:
                cand_rank = (
                    1 if cand_eval.phase == PHASE_WARNING else 0,
                    mtime_ns,
                    cand_eval.context_tokens,
                )
                if best_guard_rank is None or cand_rank > best_guard_rank:
                    best_guard_rank = cand_rank
                    best_guard_snap = cand_snap

        if best_guard_snap is not None:
            return best_guard_snap, top_snap, True
        return top_snap, top_snap, False

    def list_recent_sessions(
        self,
        limit: int = MAX_CANDIDATE_SESSIONS,
        now_epoch: Optional[float] = None,
    ) -> List[RadarSessionCandidate]:
        now_ep = float(now_epoch) if now_epoch is not None else time.time()
        cap = max(1, min(16, int(limit)))
        out: List[RadarSessionCandidate] = []
        with self._lock:
            ranked = self._collect_ranked_transcripts_locked()
            cur_cid = (self._cached_snap.conv_id if self._cached_snap is not None else "").lower()
            pin_cid = self._pinned_conv_id.lower() if self._target_mode == TARGET_MODE_PINNED else ""

            selected_entries: List[Tuple[int, int, str, Path]] = []
            seen_ids: set[str] = set()
            for item in ranked:
                if len(selected_entries) >= cap:
                    break
                if not _is_safe_contained_transcript(item[3], self.brain_dir):
                    continue
                selected_entries.append(item)
                seen_ids.add(item[2].lower())

            if pin_cid and pin_cid not in seen_ids:
                for item in ranked:
                    if item[2].lower() == pin_cid and _is_safe_contained_transcript(item[3], self.brain_dir):
                        selected_entries.append(item)
                        break

            for mtime_ns, fsize, cid, tpath in selected_entries:
                snap = self._read_or_reuse_snap_locked(cid, tpath, mtime_ns, fsize)
                title = self._resolve_title_locked(cid)
                st = evaluate_cache_radar_state(snap, now_epoch=now_ep, conv_title=title)
                is_pin = bool(pin_cid and cid.lower() == pin_cid)
                is_cur = bool(cur_cid and cid.lower() == cur_cid)
                out.append(
                    RadarSessionCandidate(
                        conv_id=cid,
                        title=title,
                        context_tokens=st.context_tokens,
                        context_label=st.context_label,
                        phase=st.phase,
                        remaining_sec=st.remaining_sec,
                        idle_sec=st.idle_sec,
                        mtime_ns=mtime_ns,
                        is_pinned=is_pin,
                        is_current=is_cur,
                        menu_label=_format_candidate_menu_label(st, title, cid),
                        pricing_tier=st.pricing_tier,
                    )
                )
        return out

    def sample(self, now_epoch: Optional[float] = None) -> CacheRadarState:
        """
        Select target `transcript.jsonl` according to `target_mode` (`smart_guard` | `latest` | `pinned`)
        and read `16KB` tail ONLY if `(mtime_ns, size)` modified.
        """
        now_ep = float(now_epoch) if now_epoch is not None else time.time()
        with self._lock:
            chosen_snap, latest_snap, guarded = self._select_target_snap_locked(now_ep)
            if chosen_snap is None:
                self._active_sig = None
                self._cached_snap = None
                self._cached_title = ""
                self._latest_snap = None
                self._latest_title = ""
                self._selected_smart_guarded = False
            else:
                self._cached_snap = chosen_snap
                self._active_sig = (chosen_snap.conv_id, chosen_snap.mtime_ns, chosen_snap.file_size)
                self._cached_title = self._resolve_title_locked(chosen_snap.conv_id)
                self._latest_snap = latest_snap
                self._latest_title = (
                    self._cached_title
                    if (latest_snap is not None and latest_snap.conv_id == chosen_snap.conv_id)
                    else (self._resolve_title_locked(latest_snap.conv_id) if latest_snap is not None else "")
                )
                self._selected_smart_guarded = guarded

            snap_ref = self._cached_snap
            title_ref = self._cached_title
            tmode_ref = self._target_mode
            pin_ref = self._pinned_conv_id
            guard_ref = self._selected_smart_guarded

        state = evaluate_cache_radar_state(
            snap_ref,
            now_epoch=now_ep,
            conv_title=title_ref,
            target_mode=tmode_ref,
            pinned_conv_id=pin_ref,
            smart_guarded=guard_ref,
        )
        base_detail = state.detail_text
        get_kernel().bus.emit("enrich_radar", state)
        detail_suffix = ""
        if base_detail and state.detail_text.startswith(base_detail) and len(state.detail_text) > len(base_detail):
            detail_suffix = state.detail_text[len(base_detail):]
        elif not base_detail and state.detail_text:
            detail_suffix = state.detail_text
        with self._lock:
            self._cached_extensions = dict(state.extensions)
            self._cached_extensions_conv_id = state.conv_id
            self._cached_detail_suffix = detail_suffix
        return state

    def evaluate_cached(self, now_epoch: Optional[float] = None) -> CacheRadarState:
        """Pure in-memory 1s tick re-evaluation with ZERO disk or network I/O."""
        now_ep = float(now_epoch) if now_epoch is not None else time.time()
        with self._lock:
            snap_ref = self._cached_snap
            title_ref = self._cached_title
            latest_ref = self._latest_snap
            latest_title_ref = self._latest_title
            tmode_ref = self._target_mode
            pin_ref = self._pinned_conv_id
            guard_ref = self._selected_smart_guarded
            cached_ext = dict(self._cached_extensions)
            cached_ext_cid = self._cached_extensions_conv_id
            cached_suffix = self._cached_detail_suffix

        state = evaluate_cache_radar_state(
            snap_ref,
            now_epoch=now_ep,
            conv_title=title_ref,
            target_mode=tmode_ref,
            pinned_conv_id=pin_ref,
            smart_guarded=guard_ref,
        )
        if (
            guard_ref
            and tmode_ref == TARGET_MODE_SMART_GUARD
            and state.phase == PHASE_COLD
            and not state.should_pop_smart_hud
            and latest_ref is not None
            and (snap_ref is None or latest_ref.conv_id != snap_ref.conv_id)
        ):
            state = evaluate_cache_radar_state(
                latest_ref,
                now_epoch=now_ep,
                conv_title=latest_title_ref,
                target_mode=tmode_ref,
                pinned_conv_id=pin_ref,
                smart_guarded=False,
            )
        if state.conv_id == cached_ext_cid:
            if cached_ext:
                state.extensions.update(cached_ext)
            if cached_suffix and not state.detail_text.endswith(cached_suffix):
                state.detail_text = f"{state.detail_text}{cached_suffix}"
        return state
