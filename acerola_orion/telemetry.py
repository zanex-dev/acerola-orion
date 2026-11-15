# -*- coding: utf-8 -*-
"""
Orion-Telemetry 本地配额与运行体征遥测引擎 (acerola_orion/telemetry.py)
- 聚合进程树、本地 127.0.0.1 Connect-RPC 状态查询、无锁增量日志体征、旁路安全审查延迟与本地守护状态
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .constants import (
    GUILLOTINE_MUTEX_NAME,
    SCHEDULED_TASK_NAME,
    get_ls_log_path,
    get_main_log_path,
    get_user_settings_path,
)
from .rpc import (
    ConnectRpcClient,
    ConnectRpcError,
    RawProcEntry,
    inspect_active_ls,
    is_mutex_held,
    read_file_bytes_shared,
    read_shared_file_delta,
    read_shared_file_lines,
    snapshot_processes_fast,
)

_ORIG_READ_SHARED_FILE_LINES = read_shared_file_lines


@dataclass
class QuotaBucketView:
    group_name: str
    bucket_id: str
    display_name: str
    window: str
    remaining_fraction: float
    remaining_pct: float
    reset_time: str
    reset_countdown: str


@dataclass
class SidecarMonitorStats:
    eval_count: int = 0
    avg_ms: float = 0.0
    max_ms: float = 0.0
    timeout_count: int = 0
    recent_durations_ms: List[float] = field(default_factory=list)
    recent_timeouts: List[str] = field(default_factory=list)


@dataclass
class LogCongestionStats:
    ls_log_lines: int = 0
    capacity_exhausted_count: int = 0
    http_429_503_count: int = 0
    network_changed_count: int = 0
    sidecar: SidecarMonitorStats = field(default_factory=SidecarMonitorStats)


@dataclass
class GuardStatusView:
    mutex_active: bool = False
    scheduled_task_installed: bool = False
    legacy_buggy_killer_active: bool = False
    legacy_pids: List[int] = field(default_factory=list)


@dataclass
class OrionTelemetryReport:
    captured_at: str
    ls_running: bool
    ls_pid: int = 0
    main_pids: List[int] = field(default_factory=list)
    ide_version: str = ""
    http_port: int = 0
    https_port: int = 0
    stale_hub_files: int = 0
    account_name: str = ""
    user_tier_id: str = ""
    user_tier_name: str = ""
    teams_tier: str = ""
    paid_tier_id: str = ""
    paid_tier_name: str = ""
    current_tier_id: str = ""
    gcp_managed: bool = False
    proxy_mode: str = "直连 / 系统默认"
    quota_buckets: List[QuotaBucketView] = field(default_factory=list)
    log_stats: LogCongestionStats = field(default_factory=LogCongestionStats)
    guard: GuardStatusView = field(default_factory=GuardStatusView)
    rpc_errors: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        # Strict-JSON safe: NaN / Inf (unknown quota) are emitted as null, never as bare `NaN`.
        return _json_finite(asdict(self))


def _json_finite(obj: Any) -> Any:
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: _json_finite(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_finite(v) for v in obj]
    return obj


_DURATION_SEG_RE = re.compile(r"(\d+(?:\.\d+)?)(ns|us|µs|μs|ms|s|m|h)")
_TRAILING_COMMA_RE = re.compile(r",(\s*[}\]])")
_DURATION_UNIT_MS: Dict[str, float] = {
    "ns": 1e-6,
    "us": 1e-3,
    "µs": 1e-3,
    "μs": 1e-3,
    "ms": 1.0,
    "s": 1000.0,
    "m": 60_000.0,
    "h": 3_600_000.0,
}


def load_json_safe(path: Path, default: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    fallback: Dict[str, Any] = dict(default) if isinstance(default, dict) else {}
    if not path.exists():
        return dict(fallback)
    for attempt in range(20):
        try:
            if not path.is_file():
                return dict(fallback)
            data = read_file_bytes_shared(path)
            if data is None:
                if not path.exists():
                    return dict(fallback)
                time.sleep(min(0.01, 0.001 * (attempt + 1)))
                continue
            raw = data.decode("utf-8-sig", errors="replace")
            try:
                val = json.loads(raw)
            except json.JSONDecodeError:
                val = json.loads(_TRAILING_COMMA_RE.sub(r"\1", raw))
            if isinstance(val, dict):
                return val
            return dict(fallback)
        except PermissionError:
            time.sleep(min(0.01, 0.001 * (attempt + 1)))
        except OSError as e:
            if getattr(e, "winerror", 0) in (5, 32):
                time.sleep(min(0.01, 0.001 * (attempt + 1)))
            else:
                return dict(fallback)
        except Exception:
            return dict(fallback)
    return dict(fallback)


def _format_reset_countdown(iso_str: str) -> str:
    if not iso_str:
        return "未知"
    try:
        s = iso_str.strip()
        if s.endswith("Z") or s.endswith("z"):
            s = s[:-1] + "+00:00"
        target = datetime.fromisoformat(s)
        if target.tzinfo is None:
            target = target.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        sec = int((target - now).total_seconds())
        if sec <= 0:
            return "即将刷新"
        if sec < 60:
            return "<1分钟"
        days, rem = divmod(sec, 86400)
        hours, rem = divmod(rem, 3600)
        mins = rem // 60
        if days > 0:
            return f"{days}天 {hours}小时 {mins}分"
        if hours > 0:
            return f"{hours}小时 {mins}分"
        return f"{mins}分钟"
    except Exception:
        return iso_str


def _parse_duration_to_ms(raw: str) -> float:
    """Convert Go time.Duration string (e.g. '1h2m3s', '1m500ms', '3.1295s', '420.5ms', '33.7µs') to ms."""
    s = raw.strip()
    if not s:
        raise ValueError("Empty duration string")
    if s == "0":
        return 0.0
    sign = 1.0
    if s.startswith("-"):
        sign = -1.0
        s = s[1:]
    elif s.startswith("+"):
        s = s[1:]
    pos = 0
    total_ms = 0.0
    matched = False
    for m in _DURATION_SEG_RE.finditer(s):
        if m.start() != pos:
            raise ValueError(f"Invalid duration segment in {raw!r}")
        pos = m.end()
        matched = True
        total_ms += float(m.group(1)) * _DURATION_UNIT_MS[m.group(2)]
    if not matched or pos != len(s):
        raise ValueError(f"Invalid Go duration string: {raw!r}")
    return sign * total_ms


_LOG_MON_RE = re.compile(
    r"Monitor\s+(sir_flash_lite|severe_internal_risks_agy)\s+FINISHED:.*duration=([0-9.a-zA-Zµμ]+)"
)
_LOG_CAP_RE = re.compile(
    r"MODEL_CAPACITY_EXHAUSTED|OVERLOADED_TOO_MANY_RETRIES_PER_REQUEST|ResourceExhausted"
)
_LOG_HTTP_ERR_RE = re.compile(
    r"(?:HTTP/\d(?:\.\d)?\s+|HTTP\s+|status(?:\s+code)?\s*[=:\s]\s*|code\s*[=:]\s*)(?:429|503)\b"
    r"|\b(?:429\s+Too\s+Many\s+Requests|503\s+Service\s+Unavailable)\b"
    r"|rateLimitExceeded|RESOURCE_EXHAUSTED|\bcode\s*=\s*Unavailable\b|\brpc\s+error:.*\bUnavailable\b",
    re.IGNORECASE,
)


@dataclass
class _LsLogCursorState:
    file_id: Optional[Tuple[int, int, int]] = None
    offset: int = 0
    carry_bytes: bytes = b""
    ls_log_lines: int = 0
    capacity_exhausted_count: int = 0
    http_429_503_count: int = 0
    eval_count: int = 0
    duration_sum_ms: float = 0.0
    max_ms: float = 0.0
    recent_durations_ms: List[float] = field(default_factory=list)
    timeout_count: int = 0
    recent_timeouts: List[str] = field(default_factory=list)

    def copy(self) -> "_LsLogCursorState":
        return _LsLogCursorState(
            file_id=self.file_id,
            offset=self.offset,
            carry_bytes=self.carry_bytes,
            ls_log_lines=self.ls_log_lines,
            capacity_exhausted_count=self.capacity_exhausted_count,
            http_429_503_count=self.http_429_503_count,
            eval_count=self.eval_count,
            duration_sum_ms=self.duration_sum_ms,
            max_ms=self.max_ms,
            recent_durations_ms=list(self.recent_durations_ms),
            timeout_count=self.timeout_count,
            recent_timeouts=list(self.recent_timeouts),
        )


@dataclass
class _MainLogCursorState:
    file_id: Optional[Tuple[int, int, int]] = None
    offset: int = 0
    carry_bytes: bytes = b""
    network_changed_count: int = 0

    def copy(self) -> "_MainLogCursorState":
        return _MainLogCursorState(
            file_id=self.file_id,
            offset=self.offset,
            carry_bytes=self.carry_bytes,
            network_changed_count=self.network_changed_count,
        )


def _fold_ls_lines(st: _LsLogCursorState, lines: Sequence[str]) -> None:
    """Fold log lines into `_LsLogCursorState` using fast C substring pre-filters before regex."""
    st.ls_log_lines += len(lines)
    for line in lines:
        if "FINISHED:" in line and "duration=" in line and "Monitor" in line:
            m_mon = _LOG_MON_RE.search(line)
            if m_mon:
                try:
                    d_ms = round(_parse_duration_to_ms(m_mon.group(2)), 1)
                except Exception:
                    d_ms = None
                if d_ms is not None:
                    st.eval_count += 1
                    st.duration_sum_ms += d_ms
                    if st.eval_count == 1 or d_ms > st.max_ms:
                        st.max_ms = d_ms
                    st.recent_durations_ms.append(d_ms)
                    if len(st.recent_durations_ms) > 12:
                        del st.recent_durations_ms[:-6]

        if (
            "EXHAUSTED" in line
            or "OVERLOADED" in line
            or "ResourceExhausted" in line
        ) and _LOG_CAP_RE.search(line):
            st.capacity_exhausted_count += 1

        has_http_hint = "429" in line or "503" in line
        if not has_http_hint:
            low = line.lower()
            has_http_hint = (
                "unavailable" in low
                or "ratelimitexceeded" in low
                or "resource_exhausted" in low
            )
        if has_http_hint and _LOG_HTTP_ERR_RE.search(line):
            st.http_429_503_count += 1

        if "LLM_MONITOR_LLM_CALL_TIMEOUT" in line or "context deadline exceeded" in line:
            short = line.strip()
            if len(short) > 140:
                short = short[-140:]
            st.timeout_count += 1
            st.recent_timeouts.append(short)
            if len(st.recent_timeouts) > 8:
                del st.recent_timeouts[:-4]


def _fold_main_lines(st: _MainLogCursorState, lines: Sequence[str]) -> None:
    for ml in lines:
        if "ERR_NETWORK_CHANGED" in ml:
            st.network_changed_count += 1


def _build_congestion_stats(ls_st: _LsLogCursorState, main_st: _MainLogCursorState) -> LogCongestionStats:
    avg_ms = round(ls_st.duration_sum_ms / ls_st.eval_count, 1) if ls_st.eval_count else 0.0
    max_ms = round(ls_st.max_ms, 1) if ls_st.eval_count else 0.0
    return LogCongestionStats(
        ls_log_lines=ls_st.ls_log_lines,
        capacity_exhausted_count=ls_st.capacity_exhausted_count,
        http_429_503_count=ls_st.http_429_503_count,
        network_changed_count=main_st.network_changed_count,
        sidecar=SidecarMonitorStats(
            eval_count=ls_st.eval_count,
            avg_ms=avg_ms,
            max_ms=max_ms,
            timeout_count=ls_st.timeout_count,
            recent_durations_ms=ls_st.recent_durations_ms[-6:],
            recent_timeouts=ls_st.recent_timeouts[-4:],
        ),
    )


def analyze_logs(
    ls_lines: Sequence[str],
    main_lines: Sequence[str],
) -> LogCongestionStats:
    ls_st = _LsLogCursorState()
    main_st = _MainLogCursorState()
    _fold_ls_lines(ls_st, ls_lines)
    _fold_main_lines(main_st, main_lines)
    return _build_congestion_stats(ls_st, main_st)


_LOG_CURSOR_LOCK = threading.Lock()
_LOG_CURSOR: Dict[Tuple[str, str], Tuple[_LsLogCursorState, _MainLogCursorState]] = {}
_LOG_CURSOR_MAX = 8


def reset_log_cursor_cache() -> None:
    """Clear cached incremental log cursors."""
    with _LOG_CURSOR_LOCK:
        _LOG_CURSOR.clear()


def _advance_text_cursor(
    st: Any,
    path: Path,
    max_tail_bytes: int,
    new_state_fn: Callable[..., Any],
    fold_fn: Callable[[Any, Sequence[str]], None],
) -> Tuple[Any, Any]:
    """Read only appended bytes since the previous `(file_id, offset)` and fold complete lines."""
    fid, new_off, chunk, reset = read_shared_file_delta(
        path,
        prev_file_id=st.file_id,
        prev_offset=st.offset,
        max_tail_bytes=max_tail_bytes,
    )
    if reset:
        st = new_state_fn(file_id=fid, offset=new_off)
    else:
        st.file_id = fid
        st.offset = new_off
    if chunk:
        buf = st.carry_bytes + chunk
        last_nl = buf.rfind(b"\n")
        if last_nl != -1:
            fold_fn(st, buf[: last_nl + 1].decode("utf-8", errors="replace").splitlines())
            st.carry_bytes = buf[last_nl + 1 :]
        else:
            st.carry_bytes = buf[-max_tail_bytes:] if len(buf) > max_tail_bytes else buf
    if st.carry_bytes:
        snap = st.copy()
        fold_fn(snap, st.carry_bytes.decode("utf-8", errors="replace").splitlines())
        return st, snap
    return st, st


def analyze_logs_incremental(
    ls_log_path: Path,
    main_log_path: Path,
    ls_max_tail_bytes: int = 4 * 1024 * 1024,
    main_max_tail_bytes: int = 2 * 1024 * 1024,
) -> LogCongestionStats:
    """
    Incrementally scan `language_server.log` and `main.log` using `(file_id, offset)` cursors.
    When neither file grew, zero bytes are read or regex-scanned. Rotations/truncations auto-reset.
    """
    key = (
        os.path.normcase(os.path.abspath(str(ls_log_path))),
        os.path.normcase(os.path.abspath(str(main_log_path))),
    )
    with _LOG_CURSOR_LOCK:
        entry = _LOG_CURSOR.get(key)
        if entry is None:
            ls_st, main_st = _LsLogCursorState(), _MainLogCursorState()
        else:
            ls_st, main_st = entry
        ls_st, ls_snap = _advance_text_cursor(
            ls_st, ls_log_path, ls_max_tail_bytes, _LsLogCursorState, _fold_ls_lines
        )
        main_st, main_snap = _advance_text_cursor(
            main_st, main_log_path, main_max_tail_bytes, _MainLogCursorState, _fold_main_lines
        )
        if key not in _LOG_CURSOR and len(_LOG_CURSOR) >= _LOG_CURSOR_MAX:
            oldest = next(iter(_LOG_CURSOR))
            del _LOG_CURSOR[oldest]
        _LOG_CURSOR[key] = (ls_st, main_st)
        return _build_congestion_stats(ls_snap, main_snap)


_GUARD_PROBE_LOCK = threading.Lock()
_GUARD_PROBE_CACHE: Dict[str, Tuple[float, Any]] = {}
GUARD_TASK_PROBE_TTL_SEC = 120.0
GUARD_LEGACY_PROBE_TTL_SEC = 300.0


def invalidate_guard_probe_cache() -> None:
    """Drop cached guard probe results (call after install / uninstall / retire actions)."""
    with _GUARD_PROBE_LOCK:
        _GUARD_PROBE_CACHE.clear()


def _guard_probe_cached(key: str, ttl: float) -> Tuple[bool, Any]:
    with _GUARD_PROBE_LOCK:
        hit = _GUARD_PROBE_CACHE.get(key)
    if hit is not None and (time.monotonic() - hit[0]) < ttl:
        return True, hit[1]
    return False, None


def _guard_probe_store(key: str, value: Any) -> None:
    with _GUARD_PROBE_LOCK:
        _GUARD_PROBE_CACHE[key] = (time.monotonic(), value)


def probe_guard_status(entries: Optional[List[RawProcEntry]] = None) -> GuardStatusView:
    """Check kernel mutex liveness (real-time) and TTL-cached scheduled task / legacy script status."""
    view = GuardStatusView()

    if sys.platform == "win32":
        view.mutex_active = is_mutex_held(GUILLOTINE_MUTEX_NAME)

        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        hit, cached_task = _guard_probe_cached("task", GUARD_TASK_PROBE_TTL_SEC)
        if hit:
            view.scheduled_task_installed = bool(cached_task)
        else:
            try:
                r = subprocess.run(
                    ["schtasks.exe", "/Query", "/TN", SCHEDULED_TASK_NAME],
                    capture_output=True,
                    timeout=3.0,
                    creationflags=creationflags,
                )
                view.scheduled_task_installed = r.returncode == 0
                _guard_probe_store("task", view.scheduled_task_installed)
            except Exception:
                pass

        procs = entries if entries is not None else snapshot_processes_fast()
        my_pid = os.getpid()
        ps_pids = {
            p.pid
            for p in procs
            if p.name.lower() in ("powershell.exe", "pwsh.exe") and p.pid != my_pid
        }
        if ps_pids:
            hit, cached_legacy = _guard_probe_cached("legacy", GUARD_LEGACY_PROBE_TTL_SEC)
            if hit:
                pids = [pid for pid in (cached_legacy or []) if pid in ps_pids]
                view.legacy_pids = pids
                view.legacy_buggy_killer_active = len(pids) > 0
            else:
                try:
                    r_leg = subprocess.run(
                        [
                            "powershell.exe",
                            "-NoProfile",
                            "-NonInteractive",
                            "-Command",
                            "Get-CimInstance Win32_Process | Where-Object { $_.ProcessId -ne $PID -and $_.CommandLine -match 'ag_kill_orphan\\.ps1' } | ForEach-Object { $_.ProcessId }",
                        ],
                        capture_output=True,
                        text=True,
                        timeout=4.0,
                        creationflags=creationflags,
                    )
                    pids = [
                        int(x.strip())
                        for x in (r_leg.stdout or "").splitlines()
                        if x.strip().isdigit() and int(x.strip()) != my_pid
                    ]
                    view.legacy_pids = pids
                    view.legacy_buggy_killer_active = len(pids) > 0
                    _guard_probe_store("legacy", list(pids))
                except Exception:
                    pass

    return view


def _inspect_proxy_mode() -> str:
    """Inspect whether a custom HTTP proxy is configured in user settings (read-only, masks credentials)."""
    settings_p = get_user_settings_path()
    if settings_p.is_file():
        st = load_json_safe(settings_p)
        hp = st.get("http.proxy")
        if isinstance(hp, str) and hp.strip():
            ps = st.get("http.proxySupport", "override")
            # Strip any userinfo before displaying
            clean_hp = hp.strip().split("@")[-1]
            return f"自定义代理 ({clean_hp}, mode={ps})"
    return "直连 / 系统默认"


def collect_telemetry(include_guard_check: bool = True) -> OrionTelemetryReport:
    now_str = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %z")
    entries = snapshot_processes_fast()
    ls_state = inspect_active_ls(include_cmdline=True, entries=entries)

    if read_shared_file_lines is _ORIG_READ_SHARED_FILE_LINES:
        log_stats = analyze_logs_incremental(get_ls_log_path(), get_main_log_path())
    else:
        ls_lines = read_shared_file_lines(get_ls_log_path())
        main_lines = read_shared_file_lines(get_main_log_path(), max_tail_bytes=2 * 1024 * 1024)
        log_stats = analyze_logs(ls_lines, main_lines)

    proxy_mode = _inspect_proxy_mode()
    guard = probe_guard_status(entries=entries) if include_guard_check else GuardStatusView()

    http_port = ls_state.hub.http_port if ls_state.hub else 0
    https_port = ls_state.hub.https_port if ls_state.hub else 0
    stale_hubs = max(0, ls_state.total_hub_files - (1 if (ls_state.running and ls_state.hub) else 0))

    report = OrionTelemetryReport(
        captured_at=now_str,
        ls_running=ls_state.running,
        ls_pid=ls_state.pid,
        main_pids=ls_state.main_pids,
        ide_version=ls_state.ide_version,
        http_port=http_port,
        https_port=https_port,
        stale_hub_files=stale_hubs,
        proxy_mode=proxy_mode,
        log_stats=log_stats,
        guard=guard,
    )

    if not ls_state.running or not ls_state.hub:
        return report

    client = ConnectRpcClient.from_hub(ls_state.hub)

    def _as_dict(v: Any) -> Dict[str, Any]:
        return v if isinstance(v, dict) else {}

    def _as_list(v: Any) -> List[Any]:
        return v if isinstance(v, list) else []

    # 1. GetUserStatus (note: email intentionally omitted in public edition for screenshot/screen-share privacy!)
    try:
        us_resp = _as_dict(client.get_user_status(timeout=8.0))
        u = _as_dict(us_resp.get("userStatus"))
        report.account_name = str(u.get("name") or "")
        ut = _as_dict(u.get("userTier"))
        report.user_tier_id = str(ut.get("id") or "")
        report.user_tier_name = str(ut.get("name") or "")
        ps = _as_dict(_as_dict(u.get("planStatus")).get("planInfo"))
        report.teams_tier = str(ps.get("teamsTier") or "")
    except ConnectRpcError as e:
        report.rpc_errors.append(str(e))

    # 2. GetLoadCodeAssist (tier inspection only)
    try:
        lca_resp = _as_dict(client.get_load_code_assist(timeout=10.0))
        r = _as_dict(lca_resp.get("response"))
        pt = _as_dict(r.get("paidTier"))
        ct = _as_dict(r.get("currentTier"))
        report.paid_tier_id = str(pt.get("id") or "")
        report.paid_tier_name = str(pt.get("name") or "")
        report.current_tier_id = str(ct.get("id") or "")
        report.gcp_managed = bool(r.get("gcpManaged"))
    except ConnectRpcError as e:
        report.rpc_errors.append(str(e))

    # 3. RetrieveUserQuotaSummary
    try:
        qt_resp = _as_dict(client.retrieve_user_quota_summary(timeout=8.0))
        groups = _as_list(_as_dict(qt_resp.get("response")).get("groups"))
        for g in groups:
            if not isinstance(g, dict):
                continue
            g_name = str(g.get("displayName") or "")
            for b in _as_list(g.get("buckets")):
                if not isinstance(b, dict):
                    continue
                raw_frac = b.get("remainingFraction")
                try:
                    frac_val = (
                        float(raw_frac)
                        if raw_frac is not None and not isinstance(raw_frac, bool)
                        else math.nan
                    )
                except (TypeError, ValueError):
                    frac_val = math.nan
                frac = max(0.0, min(1.0, frac_val)) if math.isfinite(frac_val) else math.nan
                r_time = str(b.get("resetTime") or "")
                report.quota_buckets.append(
                    QuotaBucketView(
                        group_name=g_name,
                        bucket_id=str(b.get("bucketId") or ""),
                        display_name=str(b.get("displayName") or ""),
                        window=str(b.get("window") or ""),
                        remaining_fraction=frac,
                        remaining_pct=round(frac * 100.0, 4) if math.isfinite(frac) else math.nan,
                        reset_time=r_time,
                        reset_countdown=_format_reset_countdown(r_time),
                    )
                )
    except ConnectRpcError as e:
        report.rpc_errors.append(str(e))

    return report
