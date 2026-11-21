# -*- coding: utf-8 -*-
"""
Orion-Ledger 本地 Token 明细账本与黑盒电子秤验钞机 (acerola_orion/ledger.py)
- 严格媒体隐私隔离 (Heart-Under-Blade 兼容)：仅读取 brain/<uuid>/.system_generated/logs/transcript.jsonl
  与 annotations/<uuid>.pbtxt 纯文本，绝不枚举或打开 .user_uploaded、tempmediaStorage 或任何媒体文件。
- 增量 SQLite 索引：毫秒级汇总每日 / 每模型 / 每会话 Input、Cache-Read、Output Token 与等效美元成本。
- 高精度配额快照库：比对本机步骤与配额变化，按预设价格推算趋势。
"""

from __future__ import annotations

import bisect
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
from statistics import median
import threading
import time
from typing import Any, Dict, Iterator, List, Optional, Set, Tuple

from .constants import (
    MODEL_CATALOG,
    classify_model,
    get_agy_annotations_dir,
    get_agy_brain_dir,
    get_ledger_db_path,
    interprocess_file_lock,
)
from .telemetry import OrionTelemetryReport

UUID_DIR_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
MODEL_SWITCH_RE = re.compile(r"Model Selection`\s+from\s+.*?\s+to\s+([^\.\r\n]+)\.")
PBTXT_TITLE_RE = re.compile(r'(?:^|[\s{;])title\s*:\s*"((?:[^"\\]|\\.)*)"')

_DB_REGISTRY_LOCK = threading.Lock()
_DB_PATH_LOCKS: Dict[str, threading.RLock] = {}
_DB_WAL_ENABLED: Set[Tuple[str, int]] = set()


def _get_db_lock(db_path: Path) -> Tuple[str, threading.RLock]:
    key = os.path.normcase(os.path.abspath(str(db_path)))
    with _DB_REGISTRY_LOCK:
        lk = _DB_PATH_LOCKS.get(key)
        if lk is None:
            lk = threading.RLock()
            _DB_PATH_LOCKS[key] = lk
        return key, lk


@dataclass
class DailyUsageRow:
    local_date: str
    sessions: int
    steps: int
    input_tokens: int
    cache_read_tokens: int
    output_tokens: int
    usd_1x: float
    usd_intro: float
    usd_agy_est: float


@dataclass
class ModelUsageRow:
    family_id: str
    family_label: str
    quota_group: str
    steps: int
    input_tokens: int
    cache_read_tokens: int
    output_tokens: int
    usd_1x: float
    usd_intro: float
    usd_agy_est: float
    multiplier_note: str


@dataclass
class SessionUsageRow:
    conv_id: str
    title: str
    local_date: str
    last_ts: str
    primary_model: str
    steps: int
    input_tokens: int
    cache_read_tokens: int
    output_tokens: int
    usd_1x: float
    usd_agy_est: float


@dataclass
class PhantomDrainEvent:
    from_ts: str
    to_ts: str
    from_event: str
    to_event: str
    gemini_5h_drop_pct: float
    gemini_weekly_drop_pct: float
    p3_weekly_drop_pct: float
    p3_5h_drop_pct: float = 0.0


@dataclass
class RateSlopeSample:
    from_ts: str
    to_ts: str
    duration_min: float
    steps: int
    dominant_model: str
    usd_1x: float
    usd_agy_15x_est: float
    gemini_5h_drop_pct: float
    gemini_weekly_drop_pct: float
    implied_5h_pool_usd_at_1x: Optional[float]
    implied_weekly_pool_usd_at_1x: Optional[float]


@dataclass
class RateAuditReport:
    total_snapshots: int = 0
    latest_snapshot_ts: str = ""
    phantom_drains: List[PhantomDrainEvent] = field(default_factory=list)
    slope_samples: List[RateSlopeSample] = field(default_factory=list)
    median_5h_pool_usd_at_1x: Optional[float] = None
    median_weekly_pool_usd_at_1x: Optional[float] = None
    verdict: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _try_parse_iso_epoch(iso_str: str) -> Optional[Tuple[float, str]]:
    if not iso_str or not isinstance(iso_str, str):
        return None
    try:
        s = iso_str.strip()
        if not s:
            return None
        if s.endswith("Z") or s.endswith("z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        dt_local = dt.astimezone()
        ep = dt_local.timestamp()
        if not math.isfinite(ep):
            return None
        return ep, dt_local.strftime("%Y-%m-%d")
    except Exception:
        return None


def _iso_to_epoch_and_local_date(
    iso_str: str,
    fallback_epoch: Optional[float] = None,
) -> Tuple[float, str]:
    parsed = _try_parse_iso_epoch(iso_str)
    if parsed is not None:
        return parsed
    if fallback_epoch is not None and math.isfinite(fallback_epoch) and fallback_epoch > 0:
        dt_fb = datetime.fromtimestamp(fallback_epoch, tz=timezone.utc).astimezone()
        return dt_fb.timestamp(), dt_fb.strftime("%Y-%m-%d")
    now = datetime.now().astimezone()
    return now.timestamp(), now.strftime("%Y-%m-%d")


MAX_LEDGER_DAYS = 3650  # timedelta / CLI upper bound (10 years)
MAX_SESSION_LIMIT = 500  # bounded SQLite LIMIT


def _clamp_int(val: Any, lo: int, hi: int, default: int) -> int:
    """Coerce untrusted numeric args into [lo, hi] (bool / NaN / garbage -> default)."""
    if isinstance(val, bool):
        return default
    try:
        f = float(val)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(f):
        return default
    return max(lo, min(hi, int(f)))


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


def _is_safe_contained_path(target: Path, root_dir: Path) -> bool:
    """
    Reject symlinks, NTFS directory junctions, and reparse points at every segment under `root_dir`,
    and verify `target.resolve()` stays strictly inside `root_dir.resolve()`.
    """
    try:
        if not os.path.lexists(root_dir) or _has_reparse_or_symlink(root_dir):
            return False
        root_res = root_dir.resolve(strict=True)
        rel_parts = target.relative_to(root_dir).parts
        cur = root_dir
        for part in rel_parts:
            cur = cur / part
            if not os.path.lexists(cur):
                return False
            if _has_reparse_or_symlink(cur):
                return False
        target_res = target.resolve(strict=True)
        return target_res.is_relative_to(root_res)
    except Exception:
        return False


def _unescape_pbtxt_string(raw_val: str) -> str:
    """
    Single-pass C / Protobuf TextFormat string unescaper supporting octal UTF-8 byte escapes
    (`\\351\\233\\205`), hex escapes (`\\xHH`), Unicode escapes (`\\uHHHH`, `\\UHHHHHHHH`), and standard escapes.
    """
    buf = bytearray()
    i = 0
    n = len(raw_val)
    simple_escapes = {
        "n": ord("\n"),
        "r": ord("\r"),
        "t": ord("\t"),
        '"': ord('"'),
        "'": ord("'"),
        "\\": ord("\\"),
        "a": ord("\a"),
        "b": ord("\b"),
        "f": ord("\f"),
        "v": ord("\v"),
        "?": ord("?"),
    }
    while i < n:
        ch = raw_val[i]
        if ch != "\\":
            buf.extend(ch.encode("utf-8"))
            i += 1
            continue
        i += 1
        if i >= n:
            buf.append(ord("\\"))
            break
        esc = raw_val[i]
        if esc in "01234567":
            j = i
            while j < min(n, i + 3) and raw_val[j] in "01234567":
                j += 1
            buf.append(int(raw_val[i:j], 8) & 0xFF)
            i = j
        elif esc in ("x", "X"):
            j = i + 1
            while j < min(n, i + 3) and raw_val[j] in "0123456789abcdefABCDEF":
                j += 1
            if j > i + 1:
                buf.append(int(raw_val[i + 1 : j], 16) & 0xFF)
                i = j
            else:
                buf.extend(esc.encode("utf-8"))
                i += 1
        elif esc == "u" and i + 5 <= n and all(c in "0123456789abcdefABCDEF" for c in raw_val[i + 1 : i + 5]):
            cp = int(raw_val[i + 1 : i + 5], 16)
            buf.extend(chr(cp).encode("utf-8", errors="replace"))
            i += 5
        elif esc == "U" and i + 9 <= n and all(c in "0123456789abcdefABCDEF" for c in raw_val[i + 1 : i + 9]):
            cp = int(raw_val[i + 1 : i + 9], 16)
            if 0 <= cp <= 0x10FFFF:
                buf.extend(chr(cp).encode("utf-8", errors="replace"))
            i += 9
        elif esc in simple_escapes:
            buf.append(simple_escapes[esc])
            i += 1
        else:
            buf.extend(esc.encode("utf-8"))
            i += 1
    return bytes(buf).decode("utf-8", errors="replace").strip()


def _decode_pbtxt_title(annotations_dir: Path, conv_id: str) -> str:
    if not UUID_DIR_RE.match(conv_id):
        return ""
    p = annotations_dir / f"{conv_id}.pbtxt"
    if not _is_safe_contained_path(p, annotations_dir) or not p.is_file():
        return ""
    try:
        raw = p.read_text(encoding="utf-8", errors="replace")
        m = PBTXT_TITLE_RE.search(raw)
        if not m:
            return ""
        return _unescape_pbtxt_string(m.group(1))
    except Exception:
        return ""


def _safe_nonneg_int(val: Any) -> int:
    if val is None or isinstance(val, bool):
        return 0
    if isinstance(val, int):
        return max(0, val)
    if isinstance(val, float):
        if not math.isfinite(val):
            return 0
        return max(0, int(val))
    if isinstance(val, str):
        s = val.strip()
        if not s:
            return 0
        try:
            f = float(s)
            if not math.isfinite(f):
                return 0
            return max(0, int(f))
        except ValueError:
            return 0
    return 0


def _parse_step_index(raw_idx: Any, fallback_neg_idx: int) -> int:
    if raw_idx is None or isinstance(raw_idx, bool):
        return fallback_neg_idx
    if isinstance(raw_idx, int):
        return raw_idx
    if isinstance(raw_idx, float):
        return int(raw_idx) if math.isfinite(raw_idx) else fallback_neg_idx
    if isinstance(raw_idx, str):
        s = raw_idx.strip()
        if s and (s.isdigit() or (s.startswith("-") and s[1:].isdigit())):
            return int(s)
    return fallback_neg_idx


def cache_read_count(record: Dict[str, Any]) -> int:
    return _safe_nonneg_int(record.get("cache_read_tokens")) or _safe_nonneg_int(record.get("cache_read_input_tokens"))


def _load_parse_cursor(value: Optional[str]) -> Optional[Dict[str, Any]]:
    if not value:
        return None
    try:
        c = json.loads(value)
        if not (
            isinstance(c, dict)
            and isinstance(c["file_id"], list)
            and isinstance(c["offset"], int)
            and c["offset"] > 0
            and isinstance(c["lines"], int)
            and c["lines"] >= 0
            and all(isinstance(c[k], str) for k in ("model", "first_ts", "last_ts", "local_date", "sha256"))
            and (c["epoch"] is None or (isinstance(c["epoch"], (int, float)) and math.isfinite(c["epoch"])))
            and isinstance(c["counts"], dict)
            and all(k in MODEL_CATALOG and isinstance(v, int) and v > 0 for k, v in c["counts"].items())
        ):
            return None
        return c
    except (KeyError, TypeError, ValueError):
        return None


class OrionLedger:
    def __init__(
        self,
        db_path: Optional[Path] = None,
        brain_dir: Optional[Path] = None,
        annotations_dir: Optional[Path] = None,
    ) -> None:
        self.db_path = db_path or get_ledger_db_path()
        self.brain_dir = brain_dir or get_agy_brain_dir()
        self.annotations_dir = annotations_dir or get_agy_annotations_dir()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db_key, self._db_lock = _get_db_lock(self.db_path)
        # Implicit syncs from consecutive read queries are coalesced; .pbtxt titles cached by (mtime, size)
        self._sync_lock = threading.Lock()
        self._last_sync_mono: Optional[float] = None
        self._title_cache: Dict[str, Tuple[int, int, str]] = {}
        self._last_retention_mono: Optional[float] = None
        self._init_schema()

    IMPLICIT_SYNC_MIN_INTERVAL_SEC = 3.0
    AUDIT_WINDOW_SEC = 14 * 86400.0  # slope / phantom audit window, anchored at the newest snapshot
    CARRY_MAX_GAP_SEC = 6 * 3600.0  # max NULL gap bridged by carry-forward in compute_rate_audit
    SNAPSHOT_RETENTION_SEC = 45 * 86400.0  # quota_snapshots retention horizon

    def _ensure_synced(self) -> None:
        """
        Implicit sync used by read queries. A panel refresh issues 4 queries back-to-back; only the first one
        rescans brain/. Explicit `sync_transcripts()` calls are never throttled.
        """
        with self._sync_lock:
            last = self._last_sync_mono
            if last is not None and (time.monotonic() - last) < self.IMPLICIT_SYNC_MIN_INTERVAL_SEC:
                return
        self.sync_transcripts()

    def _cached_title(self, conv_id: str) -> str:
        """Re-decode `<conv_id>.pbtxt` only when its (mtime_ns, size) signature changes."""
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
        title = _decode_pbtxt_title(self.annotations_dir, conv_id)
        self._title_cache[conv_id] = (sig[0], sig[1], title)
        return title

    def _begin_immediate_with_retry(self, conn: sqlite3.Connection) -> None:
        for attempt in range(30):
            try:
                conn.execute("BEGIN IMMEDIATE;")
                return
            except sqlite3.OperationalError as e:
                if "locked" in str(e).lower() or "busy" in str(e).lower():
                    time.sleep(min(0.05, 0.005 * (attempt + 1)))
                else:
                    raise
        conn.execute("BEGIN IMMEDIATE;")

    @contextmanager
    def _connect(self, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        with self._db_lock:
            conn = sqlite3.connect(str(self.db_path), timeout=15.0, isolation_level=None)
            conn.row_factory = sqlite3.Row
            try:
                conn.execute("PRAGMA busy_timeout=15000;")
                try:
                    st_ctime = self.db_path.stat().st_ctime_ns if self.db_path.exists() else 0
                except OSError:
                    st_ctime = 0
                wal_token = (self._db_key, st_ctime)
                if wal_token not in _DB_WAL_ENABLED:
                    try:
                        conn.execute("PRAGMA journal_mode=WAL;")
                        with _DB_REGISTRY_LOCK:
                            _DB_WAL_ENABLED.add(wal_token)
                    except sqlite3.OperationalError:
                        pass
                conn.execute("PRAGMA synchronous=NORMAL;")
                if immediate:
                    self._begin_immediate_with_retry(conn)
                yield conn
                if conn.in_transaction:
                    conn.execute("COMMIT;")
            except Exception:
                if conn.in_transaction:
                    try:
                        conn.execute("ROLLBACK;")
                    except Exception:
                        pass
                raise
            finally:
                conn.close()

    def _init_schema(self) -> None:
        with self._db_lock:
            with self._connect(immediate=True) as conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS session_index (
                        conv_id TEXT PRIMARY KEY,
                        mtime_ns INTEGER NOT NULL,
                        file_size INTEGER NOT NULL,
                        title TEXT NOT NULL DEFAULT '',
                        primary_model TEXT NOT NULL DEFAULT '',
                        first_ts TEXT NOT NULL DEFAULT '',
                        last_ts TEXT NOT NULL DEFAULT '',
                        local_date TEXT NOT NULL DEFAULT ''
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS step_ledger (
                        conv_id TEXT NOT NULL,
                        step_index INTEGER NOT NULL,
                        created_at TEXT NOT NULL,
                        ts_epoch REAL NOT NULL,
                        local_date TEXT NOT NULL,
                        model_raw TEXT NOT NULL,
                        family_id TEXT NOT NULL,
                        quota_group TEXT NOT NULL,
                        input_tokens INTEGER NOT NULL,
                        cache_read_tokens INTEGER NOT NULL,
                        output_tokens INTEGER NOT NULL,
                        usd_1x REAL NOT NULL,
                        usd_intro REAL NOT NULL,
                        usd_agy_est REAL NOT NULL,
                        PRIMARY KEY (conv_id, step_index)
                    )
                    """
                )
                conn.execute("CREATE INDEX IF NOT EXISTS idx_step_local_date ON step_ledger(local_date)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_step_epoch ON step_ledger(ts_epoch)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_step_family ON step_ledger(family_id)")
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS quota_snapshots (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        ts_utc TEXT NOT NULL,
                        ts_epoch REAL NOT NULL,
                        event_type TEXT NOT NULL,
                        ls_pid INTEGER NOT NULL DEFAULT 0,
                        gemini_5h REAL,
                        gemini_5h_reset TEXT NOT NULL DEFAULT '',
                        gemini_weekly REAL,
                        gemini_weekly_reset TEXT NOT NULL DEFAULT '',
                        p3_5h REAL,
                        p3_5h_reset TEXT NOT NULL DEFAULT '',
                        p3_weekly REAL,
                        p3_weekly_reset TEXT NOT NULL DEFAULT ''
                    )
                    """
                )
                conn.execute("CREATE INDEX IF NOT EXISTS idx_qsnap_epoch ON quota_snapshots(ts_epoch)")
                columns = {r[1] for r in conn.execute("PRAGMA table_info(session_index)")}
                if "last_epoch" not in columns:
                    conn.execute("ALTER TABLE session_index ADD COLUMN last_epoch REAL")
                    conn.execute("ALTER TABLE session_index ADD COLUMN parse_cursor TEXT")
                    # The old title could contain prompt text. Rebuild titles and counts from source on next sync.
                    old_sessions = conn.execute("SELECT conv_id, last_ts FROM session_index").fetchall()
                    for row in old_sessions:
                        stamp = _try_parse_iso_epoch(row["last_ts"])
                        conn.execute(
                            "UPDATE session_index SET last_epoch=?, mtime_ns=-1, title=substr(conv_id,1,8) WHERE conv_id=?",
                            (stamp[0] if stamp else None, row["conv_id"]),
                        )
                if "file_identity" not in columns:
                    conn.execute("ALTER TABLE session_index ADD COLUMN file_identity TEXT")
                # Auto-invalidate cached sessions whose fallback 'gemini-other' steps now match a known model catalog entry (e.g., 4N).
                reclass_ids = {
                    str(r["conv_id"])
                    for r in conn.execute(
                        "SELECT DISTINCT conv_id, model_raw FROM step_ledger WHERE family_id = 'gemini-other'"
                    )
                    if classify_model(str(r["model_raw"] or "")).family_id != "gemini-other"
                }
                if reclass_ids:
                    conn.executemany(
                        "UPDATE session_index SET mtime_ns = -1, parse_cursor = NULL WHERE conv_id = ?",
                        [(cid,) for cid in reclass_ids],
                    )
                conn.execute("CREATE INDEX IF NOT EXISTS idx_recent_sessions ON session_index(last_epoch DESC, conv_id)")
                self._compact_poll_spam_in_conn(conn)

    @staticmethod
    def _compact_poll_spam_in_conn(conn: sqlite3.Connection) -> int:
        """Remove redundant interior poll rows where all 4 quota buckets match both neighbors."""
        rows = conn.execute(
            "SELECT id, event_type, gemini_5h, gemini_weekly, p3_5h, p3_weekly "
            "FROM quota_snapshots ORDER BY ts_epoch ASC, id ASC"
        ).fetchall()
        if len(rows) < 3:
            return 0

        def _norm_cell(val: Any) -> Any:
            if val is None:
                return None
            try:
                f = float(val)
            except (TypeError, ValueError):
                return ("nonfloat", str(val))
            if not math.isfinite(f):
                return ("nonfinite", str(f))
            return round(f, 7)

        def _qkey(r: sqlite3.Row) -> Tuple[Any, ...]:
            return (
                _norm_cell(r["gemini_5h"]),
                _norm_cell(r["gemini_weekly"]),
                _norm_cell(r["p3_5h"]),
                _norm_cell(r["p3_weekly"]),
            )

        poll_types = {"panel_poll", "cli_status", "cli_ledger", "guard_heartbeat"}
        to_delete: List[Tuple[int]] = []
        for i in range(1, len(rows) - 1):
            curr = rows[i]
            if str(curr["event_type"]) in poll_types:
                if _qkey(rows[i - 1]) == _qkey(curr) == _qkey(rows[i + 1]):
                    to_delete.append((int(curr["id"]),))
        if to_delete:
            conn.executemany("DELETE FROM quota_snapshots WHERE id = ?", to_delete)
        return len(to_delete)

    def sync_transcripts(self) -> int:
        """
        Incrementally index only transcript.jsonl files under brain/<uuid>/.system_generated/logs/.
        Parses files outside the SQLite write lock, enforces symlink/junction containment,
        and prunes deleted conversations inside an atomic BEGIN IMMEDIATE write transaction.
        Returns number of conversations newly parsed or updated.
        """
        with self._sync_lock:
            self._last_sync_mono = time.monotonic()
        with interprocess_file_lock(self.db_path.with_name(self.db_path.name + ".sync"), timeout=30):
            return self._sync_transcripts_locked()

    def _sync_transcripts_locked(self) -> int:
        if not self.brain_dir.is_dir() or _has_reparse_or_symlink(self.brain_dir):
            return 0

        with self._connect(immediate=False) as conn:
            existing = {
                str(row["conv_id"]): (
                    int(row["mtime_ns"]),
                    int(row["file_size"]),
                    str(row["title"]),
                    row["parse_cursor"],
                    row["file_identity"],
                )
                for row in conn.execute(
                    "SELECT conv_id, mtime_ns, file_size, title, parse_cursor, file_identity FROM session_index"
                )
            }

        try:
            entries = list(os.scandir(self.brain_dir))
        except Exception:
            return 0

        seen_conv_ids: Set[str] = set()
        title_updates: List[Tuple[str, str]] = []
        parsed_convs: List[Tuple[Tuple[Any, ...], List[Tuple[Any, ...]], bool]] = []

        for entry in entries:
            try:
                if entry.is_symlink() or not UUID_DIR_RE.match(entry.name):
                    continue
                if not entry.is_dir(follow_symlinks=False):
                    continue
            except OSError:
                continue

            conv_id = entry.name
            conv_dir = Path(entry.path)
            # Junction / reparse-point conversation dirs are never trusted (and get pruned if indexed).
            if _has_reparse_or_symlink(conv_dir):
                continue
            transcript_p = conv_dir / ".system_generated" / "logs" / "transcript.jsonl"
            if not os.path.lexists(transcript_p):
                # Conversation dir still exists but transcript is momentarily absent (being rewritten /
                # replaced by the IDE): keep existing ledger rows instead of pruning them as "deleted".
                if conv_id in existing:
                    seen_conv_ids.add(conv_id)
                continue
            if not _is_safe_contained_path(transcript_p, self.brain_dir):
                continue
            if not transcript_p.is_file():
                continue

            try:
                st = transcript_p.stat()
            except OSError:
                # Transient sharing violation / AV scan lock: retain prior rows, retry next sync.
                if conv_id in existing:
                    seen_conv_ids.add(conv_id)
                continue
            except Exception:
                continue

            seen_conv_ids.add(conv_id)
            title = self._cached_title(conv_id) or conv_id[:8]
            prev = existing.get(conv_id)
            file_id = f"{st.st_dev}:{st.st_ino}"
            if prev and prev[0] == st.st_mtime_ns and prev[1] == st.st_size and prev[4] == file_id:
                if title and title != prev[2]:
                    title_updates.append((title, conv_id))
                continue

            parsed_item = self._parse_conversation_file(
                conv_id=conv_id,
                transcript_path=transcript_p,
                mtime_ns=st.st_mtime_ns,
                file_size=st.st_size,
                title=title,
                previous_cursor=prev[3] if prev and st.st_size > prev[1] else None,
            )
            if parsed_item is not None:
                parsed_convs.append(parsed_item)

        stale_ids = set(existing.keys()) - seen_conv_ids
        if parsed_convs or title_updates or stale_ids:
            with self._connect(immediate=True) as conn:
                if stale_ids:
                    stale_params = [(sid,) for sid in stale_ids]
                    conn.executemany("DELETE FROM step_ledger WHERE conv_id = ?", stale_params)
                    conn.executemany("DELETE FROM session_index WHERE conv_id = ?", stale_params)
                if title_updates:
                    conn.executemany(
                        "UPDATE session_index SET title = ? WHERE conv_id = ?",
                        title_updates,
                    )
                for parsed in parsed_convs:
                    self._upsert_parsed_conversation(conn, *parsed)

        return len(parsed_convs)

    def _parse_conversation_file(
        self,
        conv_id: str,
        transcript_path: Path,
        mtime_ns: int,
        file_size: int,
        title: str,
        previous_cursor: Optional[str] = None,
    ) -> Optional[Tuple[Tuple[Any, ...], List[Tuple[Any, ...]], bool]]:
        current_model = ""
        first_ts = ""
        last_ts = ""
        last_local_date = ""
        file_mtime_epoch = (mtime_ns / 1_000_000_000.0) if mtime_ns > 0 else time.time()
        last_valid_epoch: Optional[float] = None
        model_counts: Dict[str, int] = {}
        steps_by_idx: Dict[int, Tuple[Any, ...]] = {}
        appended = False
        used_file_time = False
        line_no = 0
        complete_line = False
        digest = hashlib.sha256()

        def _reject_nonfinite(tok: str) -> Any:
            raise ValueError(f"Non-finite token in transcript: {tok}")

        try:
            with open(transcript_path, "rb") as f:
                source = os.fstat(f.fileno())
                identity = [source.st_dev, source.st_ino]
                cursor = _load_parse_cursor(previous_cursor)
                if cursor:
                    offset = int(cursor["offset"])
                    if identity == cursor["file_id"] and 0 < offset < file_size:
                        # A producer can rewrite any earlier line and grow the file in one update.
                        # Verify the prefix in bulk before reusing parsed state.
                        remaining = offset
                        while remaining:
                            chunk = f.read(min(1024 * 1024, remaining))
                            if not chunk:
                                break
                            digest.update(chunk)
                            remaining -= len(chunk)
                        appended = remaining == 0 and digest.hexdigest() == cursor["sha256"]
                    if appended:
                        current_model = cursor["model"]
                        first_ts, last_ts = cursor["first_ts"], cursor["last_ts"]
                        last_local_date = cursor["local_date"]
                        last_valid_epoch = cursor["epoch"]
                        model_counts = cursor["counts"]
                        line_no = cursor["lines"]
                    else:
                        f.seek(0)
                        digest = hashlib.sha256()
                for raw_bytes in f:
                    line_no += 1
                    digest.update(raw_bytes)
                    complete_line = raw_bytes.endswith(b"\n")
                    raw_line = raw_bytes.decode("utf-8", errors="replace").strip()
                    if not raw_line:
                        continue
                    try:
                        obj = json.loads(raw_line, parse_constant=_reject_nonfinite)
                    except Exception:
                        continue
                    if not isinstance(obj, dict):
                        continue

                    try:
                        step_type = str(obj.get("type") or "")
                        raw_created = obj.get("created_at")
                        created_at = str(raw_created).strip() if isinstance(raw_created, str) else ""
                        parsed_ts = _try_parse_iso_epoch(created_at)
                        if parsed_ts is not None:
                            last_valid_epoch = parsed_ts[0]
                            if not first_ts:
                                first_ts = created_at
                            last_ts = created_at

                        if step_type == "USER_INPUT":
                            content = str(obj.get("content") or "")
                            m_mod = MODEL_SWITCH_RE.search(content)
                            if m_mod:
                                current_model = m_mod.group(1).strip()

                        elif step_type == "PLANNER_RESPONSE":
                            in_tok = _safe_nonneg_int(obj.get("input_tokens"))
                            cache_tok = cache_read_count(obj)
                            out_tok = _safe_nonneg_int(obj.get("output_tokens"))
                            if in_tok == 0 and cache_tok == 0 and out_tok == 0:
                                continue

                            step_idx = _parse_step_index(obj.get("step_index"), fallback_neg_idx=-line_no)
                            if last_valid_epoch is None:
                                used_file_time = True
                            fb_epoch = last_valid_epoch if last_valid_epoch is not None else file_mtime_epoch
                            if parsed_ts is not None:
                                ts_epoch, local_date = parsed_ts
                                stored_created_at = created_at
                            else:
                                ts_epoch, local_date = _iso_to_epoch_and_local_date("", fallback_epoch=fb_epoch)
                                stored_created_at = datetime.fromtimestamp(ts_epoch, tz=timezone.utc).strftime(
                                    "%Y-%m-%dT%H:%M:%SZ"
                                )
                            last_local_date = local_date

                            step_model = str(obj.get("model") or obj.get("model_name") or "").strip()
                            effective_model_raw = step_model or current_model
                            spec = classify_model(effective_model_raw)
                            model_label = effective_model_raw or spec.family_label

                            usd_1x = spec.compute_usd(in_tok, cache_tok, out_tok, multiplier=1.0)
                            usd_intro = spec.compute_usd(
                                in_tok, cache_tok, out_tok, multiplier=spec.promised_multiplier
                            )
                            usd_agy = spec.compute_usd(
                                in_tok, cache_tok, out_tok, multiplier=spec.suspected_agy_multiplier
                            )

                            steps_by_idx[step_idx] = (
                                conv_id,
                                step_idx,
                                stored_created_at,
                                ts_epoch,
                                local_date,
                                model_label,
                                spec.family_id,
                                spec.quota_group,
                                in_tok,
                                cache_tok,
                                out_tok,
                                usd_1x,
                                usd_intro,
                                usd_agy,
                            )
                    except Exception:
                        continue
                end = transcript_path.stat()
                if [end.st_dev, end.st_ino] != identity or end.st_mtime_ns != mtime_ns or f.tell() != file_size:
                    return None
        except (OSError, ValueError, KeyError, TypeError):
            return None

        rows_to_insert = list(steps_by_idx.values())
        primary_model = current_model or "Unknown"
        effective_title = title or conv_id[:8]
        if not last_local_date:
            fb_epoch = last_valid_epoch if last_valid_epoch is not None else file_mtime_epoch
            _, last_local_date = _iso_to_epoch_and_local_date(
                last_ts or first_ts, fallback_epoch=fb_epoch
            )
        if not last_ts and rows_to_insert:
            last_ts = str(rows_to_insert[-1][2])
        if not first_ts and rows_to_insert:
            first_ts = str(rows_to_insert[0][2])

        parsed_end = _try_parse_iso_epoch(last_ts)
        cursor_data = json.dumps(
            {
                "offset": file_size if complete_line and not used_file_time else 0,
                "sha256": digest.hexdigest(),
                "file_id": identity,
                "lines": line_no,
                "model": current_model,
                "counts": model_counts,
                "epoch": last_valid_epoch,
                "first_ts": first_ts,
                "last_ts": last_ts,
                "local_date": last_local_date,
            }
        )
        session_tuple = (
            conv_id,
            mtime_ns,
            file_size,
            effective_title,
            primary_model,
            first_ts,
            last_ts,
            last_local_date,
            parsed_end[0] if parsed_end else None,
            cursor_data,
            f"{source.st_dev}:{source.st_ino}",
        )
        return session_tuple, rows_to_insert, appended

    @staticmethod
    def _upsert_parsed_conversation(
        conn: sqlite3.Connection,
        session_tuple: Tuple[Any, ...],
        rows_to_insert: List[Tuple[Any, ...]],
        appended: bool,
    ) -> None:
        conv_id = str(session_tuple[0])
        cursor_state = json.loads(session_tuple[9])
        counts = cursor_state["counts"]
        if not appended:
            conn.execute("DELETE FROM step_ledger WHERE conv_id = ?", (conv_id,))
        for row in rows_to_insert:
            if appended:
                previous = conn.execute(
                    "SELECT family_id FROM step_ledger WHERE conv_id = ? AND step_index = ?",
                    (conv_id, row[1]),
                ).fetchone()
                if previous:
                    family = previous[0]
                    prev_cnt = counts.get(family, 0)
                    if prev_cnt > 1:
                        counts[family] = prev_cnt - 1
                    else:
                        counts.pop(family, None)
            family = row[6]
            counts[family] = counts.get(family, 0) + 1
        if counts:
            winner = min(counts, key=lambda family: (-counts[family], family))
            session_tuple = (*session_tuple[:4], MODEL_CATALOG[winner].family_label, *session_tuple[5:])
        session_tuple = (*session_tuple[:9], json.dumps(cursor_state), *session_tuple[10:])
        if rows_to_insert:
            conn.executemany(
                """
                INSERT OR REPLACE INTO step_ledger (
                    conv_id, step_index, created_at, ts_epoch, local_date,
                    model_raw, family_id, quota_group,
                    input_tokens, cache_read_tokens, output_tokens,
                    usd_1x, usd_intro, usd_agy_est
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows_to_insert,
            )

        conn.execute(
            """
            INSERT INTO session_index (
                conv_id, mtime_ns, file_size, title, primary_model, first_ts, last_ts, local_date,
                last_epoch, parse_cursor, file_identity
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(conv_id) DO UPDATE SET
                mtime_ns = excluded.mtime_ns,
                file_size = excluded.file_size,
                title = excluded.title,
                primary_model = excluded.primary_model,
                first_ts = excluded.first_ts,
                last_ts = excluded.last_ts,
                local_date = excluded.local_date,
                last_epoch = excluded.last_epoch,
                parse_cursor = excluded.parse_cursor,
                file_identity = excluded.file_identity
            """,
            session_tuple,
        )

    def _parse_and_upsert_conversation(
        self,
        conn: sqlite3.Connection,
        conv_id: str,
        transcript_path: Path,
        mtime_ns: int,
        file_size: int,
        title: str,
    ) -> None:
        parsed = self._parse_conversation_file(
            conv_id=conv_id,
            transcript_path=transcript_path,
            mtime_ns=mtime_ns,
            file_size=file_size,
            title=title,
        )
        if parsed is not None:
            self._upsert_parsed_conversation(conn, *parsed)

    def get_daily_summary(self, days: int = 14) -> List[DailyUsageRow]:
        self._ensure_synced()
        cutoff = (datetime.now().astimezone() - timedelta(days=_clamp_int(days, 1, MAX_LEDGER_DAYS, 14) - 1)).strftime("%Y-%m-%d")
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    local_date,
                    COUNT(DISTINCT conv_id) AS sessions,
                    COUNT(*) AS steps,
                    SUM(input_tokens) AS in_tok,
                    SUM(cache_read_tokens) AS cache_tok,
                    SUM(output_tokens) AS out_tok,
                    SUM(usd_1x) AS u1,
                    SUM(usd_intro) AS uintro,
                    SUM(usd_agy_est) AS uagy
                FROM step_ledger
                WHERE local_date >= ?
                GROUP BY local_date
                ORDER BY local_date DESC
                """,
                (cutoff,),
            ).fetchall()

        return [
            DailyUsageRow(
                local_date=str(r["local_date"]),
                sessions=int(r["sessions"] or 0),
                steps=int(r["steps"] or 0),
                input_tokens=int(r["in_tok"] or 0),
                cache_read_tokens=int(r["cache_tok"] or 0),
                output_tokens=int(r["out_tok"] or 0),
                usd_1x=round(float(r["u1"] or 0.0), 4),
                usd_intro=round(float(r["uintro"] or 0.0), 4),
                usd_agy_est=round(float(r["uagy"] or 0.0), 4),
            )
            for r in rows
        ]

    def get_model_breakdown(self, days: int = 7) -> List[ModelUsageRow]:
        self._ensure_synced()
        cutoff = (datetime.now().astimezone() - timedelta(days=_clamp_int(days, 1, MAX_LEDGER_DAYS, 14) - 1)).strftime("%Y-%m-%d")
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    family_id,
                    quota_group,
                    COUNT(*) AS steps,
                    SUM(input_tokens) AS in_tok,
                    SUM(cache_read_tokens) AS cache_tok,
                    SUM(output_tokens) AS out_tok,
                    SUM(usd_1x) AS u1,
                    SUM(usd_intro) AS uintro,
                    SUM(usd_agy_est) AS uagy
                FROM step_ledger
                WHERE local_date >= ?
                GROUP BY family_id, quota_group
                ORDER BY uagy DESC
                """,
                (cutoff,),
            ).fetchall()

        out: List[ModelUsageRow] = []
        for r in rows:
            fid = str(r["family_id"])
            spec = MODEL_CATALOG.get(fid, MODEL_CATALOG["gemini-other"])
            if spec.suspected_agy_multiplier > 1.0 and spec.promised_multiplier < 1.0:
                ratio = spec.suspected_agy_multiplier / spec.promised_multiplier
                note = f"{spec.suspected_agy_multiplier:.1f}x (优惠价的 {ratio:.0f} 倍!)"
            else:
                note = f"{spec.suspected_agy_multiplier:.1f}x 正常"
            out.append(
                ModelUsageRow(
                    family_id=fid,
                    family_label=spec.family_label,
                    quota_group=str(r["quota_group"]),
                    steps=int(r["steps"] or 0),
                    input_tokens=int(r["in_tok"] or 0),
                    cache_read_tokens=int(r["cache_tok"] or 0),
                    output_tokens=int(r["out_tok"] or 0),
                    usd_1x=round(float(r["u1"] or 0.0), 4),
                    usd_intro=round(float(r["uintro"] or 0.0), 4),
                    usd_agy_est=round(float(r["uagy"] or 0.0), 4),
                    multiplier_note=note,
                )
            )
        return out

    def get_recent_sessions(self, limit: int = 15) -> List[SessionUsageRow]:
        self._ensure_synced()
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    s.conv_id,
                    s.title,
                    s.local_date,
                    s.last_ts,
                    s.primary_model,
                    COUNT(l.step_index) AS steps,
                    COALESCE(SUM(l.input_tokens), 0) AS in_tok,
                    COALESCE(SUM(l.cache_read_tokens), 0) AS cache_tok,
                    COALESCE(SUM(l.output_tokens), 0) AS out_tok,
                    COALESCE(SUM(l.usd_1x), 0.0) AS u1,
                    COALESCE(SUM(l.usd_agy_est), 0.0) AS uagy
                FROM (
                    SELECT si.* FROM session_index si
                    WHERE EXISTS (SELECT 1 FROM step_ledger x WHERE x.conv_id = si.conv_id)
                    ORDER BY si.last_epoch DESC, si.conv_id
                    LIMIT ?
                ) s
                JOIN step_ledger l ON s.conv_id = l.conv_id
                GROUP BY s.conv_id
                ORDER BY s.last_epoch DESC, s.conv_id
                """,
                (_clamp_int(limit, 1, MAX_SESSION_LIMIT, 15),),
            ).fetchall()

        return [
            SessionUsageRow(
                conv_id=str(r["conv_id"]),
                title=str(r["title"]),
                local_date=str(r["local_date"]),
                last_ts=str(r["last_ts"]),
                primary_model=str(r["primary_model"]),
                steps=int(r["steps"] or 0),
                input_tokens=int(r["in_tok"] or 0),
                cache_read_tokens=int(r["cache_tok"] or 0),
                output_tokens=int(r["out_tok"] or 0),
                usd_1x=round(float(r["u1"] or 0.0), 4),
                usd_agy_est=round(float(r["uagy"] or 0.0), 4),
            )
            for r in rows
        ]

    def record_quota_from_telemetry(
        self,
        report: OrionTelemetryReport,
        event_type: str = "probe",
        min_interval_sec: float = 0.0,
    ) -> bool:
        if not report.quota_buckets:
            return False

        bmap = {b.bucket_id: b for b in report.quota_buckets}
        g5 = bmap.get("gemini-5h")
        gw = bmap.get("gemini-weekly")
        p5 = bmap.get("3p-5h")
        pw = bmap.get("3p-weekly")

        def _finite_frac(bucket: Any) -> Optional[float]:
            if bucket is None:
                return None
            try:
                v = float(bucket.remaining_fraction)
            except (TypeError, ValueError):
                return None
            if not math.isfinite(v):
                return None
            return max(0.0, min(1.0, v))

        g5_val = _finite_frac(g5)
        gw_val = _finite_frac(gw)
        p5_val = _finite_frac(p5)
        pw_val = _finite_frac(pw)
        if g5_val is None and gw_val is None and p5_val is None and pw_val is None:
            return False

        now_epoch = time.time()
        now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        def _same_frac(new_val: Optional[float], db_val: Any) -> bool:
            if new_val is None and db_val is None:
                return True
            if new_val is None or db_val is None:
                return False
            try:
                dbf = float(db_val)
            except (TypeError, ValueError):
                return False
            if not math.isfinite(dbf):
                return False
            return abs(new_val - dbf) < 1e-7

        with self._connect(immediate=True) as conn:
            if min_interval_sec > 0:
                last = conn.execute(
                    "SELECT ts_epoch, gemini_5h, gemini_weekly, p3_5h, p3_weekly "
                    "FROM quota_snapshots WHERE ts_epoch <= ? ORDER BY ts_epoch DESC, id DESC LIMIT 1",
                    (now_epoch + self.FUTURE_SKEW_TOLERANCE_SEC,),  # future-stamped clock-skew rows are ignored
                ).fetchone()
                if last and (now_epoch - float(last["ts_epoch"])) < min_interval_sec:
                    # Still record immediately if any quota fraction actually changed!
                    if (
                        _same_frac(g5_val, last["gemini_5h"])
                        and _same_frac(gw_val, last["gemini_weekly"])
                        and _same_frac(p5_val, last["p3_5h"])
                        and _same_frac(pw_val, last["p3_weekly"])
                    ):
                        return False

            conn.execute(
                """
                INSERT INTO quota_snapshots (
                    ts_utc, ts_epoch, event_type, ls_pid,
                    gemini_5h, gemini_5h_reset,
                    gemini_weekly, gemini_weekly_reset,
                    p3_5h, p3_5h_reset,
                    p3_weekly, p3_weekly_reset
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    now_utc,
                    now_epoch,
                    event_type,
                    report.ls_pid,
                    g5_val,
                    g5.reset_time if (g5 and g5_val is not None) else "",
                    gw_val,
                    gw.reset_time if (gw and gw_val is not None) else "",
                    p5_val,
                    p5.reset_time if (p5 and p5_val is not None) else "",
                    pw_val,
                    pw.reset_time if (pw and pw_val is not None) else "",
                ),
            )
            # Bounded growth: at most hourly per instance, drop snapshots older than the retention
            # horizon (anchored at the newest snapshot) and compact redundant poll rows.
            now_m = time.monotonic()
            if self._last_retention_mono is None or (now_m - self._last_retention_mono) >= 3600.0:
                self._last_retention_mono = now_m
                self._apply_retention_in_conn(conn)
        return True

    FUTURE_SKEW_TOLERANCE_SEC = 86400.0  # snapshots stamped > now + 1 day are clock-skew artifacts
    RETENTION_MIN_CORROBORATING_ROWS = 24
    RETENTION_MIN_CORROBORATING_SPAN_SEC = 6 * 3600.0

    def _robust_anchor_in_conn(self, conn: sqlite3.Connection) -> Optional[float]:
        """Newest snapshot that is NOT in the future relative to the wall clock (skewed rows excluded)."""
        row = conn.execute(
            "SELECT MAX(ts_epoch) AS mx FROM quota_snapshots WHERE ts_epoch <= ?",
            (time.time() + self.FUTURE_SKEW_TOLERANCE_SEC,),
        ).fetchone()
        mx = row["mx"] if row is not None else None
        return float(mx) if mx is not None else None

    def _apply_retention_in_conn(self, conn: sqlite3.Connection) -> int:
        """
        Clock-skew-safe retention: the anchor ignores future-stamped rows, and pruning only happens when the
        anchor is corroborated by >= 24 snapshots spanning >= 6h in the week before it — a single skewed sample
        (or a brief skewed session) can never redefine "now" and wipe the real history.
        """
        anchor = self._robust_anchor_in_conn(conn)
        if anchor is None:
            return 0
        corr = conn.execute(
            "SELECT COUNT(*) AS c, MIN(ts_epoch) AS mn FROM quota_snapshots WHERE ts_epoch >= ? AND ts_epoch <= ?",
            (anchor - 7 * 86400.0, anchor),
        ).fetchone()
        corr_rows = int(corr["c"] or 0)
        corr_span = anchor - float(corr["mn"]) if corr["mn"] is not None else 0.0
        if corr_rows < self.RETENTION_MIN_CORROBORATING_ROWS or corr_span < self.RETENTION_MIN_CORROBORATING_SPAN_SEC:
            return 0
        cur = conn.execute(
            "DELETE FROM quota_snapshots WHERE ts_epoch < ?",
            (anchor - self.SNAPSHOT_RETENTION_SEC,),
        )
        self._compact_poll_spam_in_conn(conn)
        return int(cur.rowcount or 0)

    def get_recent_quota_series(
        self,
        hours: float = 24.0,
        max_points: int = 80,
        active_gemini_wk_reset: str = "",
        active_p3_wk_reset: str = "",
    ) -> List[Dict[str, Any]]:
        """Read recent non-future quota snapshots for UI watermark trajectory plotting (pool-isolated)."""
        span_h = max(1.0, min(168.0, float(hours) if math.isfinite(float(hours)) else 24.0))
        cap_pts = _clamp_int(max_points, 8, 240, 80)
        future_cap = time.time() + self.FUTURE_SKEW_TOLERANCE_SEC
        with self._connect() as conn:
            anchor = self._robust_anchor_in_conn(conn)
            if anchor is None:
                return []
            lo = float(anchor) - span_h * 3600.0
            rows = conn.execute(
                "SELECT ts_epoch, gemini_5h, gemini_weekly, gemini_weekly_reset, "
                "p3_5h, p3_weekly, p3_weekly_reset "
                "FROM quota_snapshots WHERE ts_epoch >= ? AND ts_epoch <= ? "
                "ORDER BY ts_epoch ASC, id ASC",
                (lo, future_cap),
            ).fetchall()
        if not rows:
            return []

        gem_rst_target = active_gemini_wk_reset.strip()
        if not gem_rst_target:
            for r in reversed(rows):
                cand = str(r["gemini_weekly_reset"] or "").strip()
                if cand:
                    gem_rst_target = cand
                    break

        p3_rst_target = active_p3_wk_reset.strip()
        if not p3_rst_target:
            for r in reversed(rows):
                cand = str(r["p3_weekly_reset"] or "").strip()
                if cand:
                    p3_rst_target = cand
                    break

        def _pct_or_none(v: Any) -> Optional[float]:
            if v is None:
                return None
            try:
                fv = float(v) * 100.0
            except (TypeError, ValueError):
                return None
            return round(max(0.0, min(100.0, fv)), 4) if math.isfinite(fv) else None

        parsed: List[Dict[str, Any]] = []
        for r in rows:
            try:
                ep = float(r["ts_epoch"])
            except (TypeError, ValueError):
                continue
            if not math.isfinite(ep):
                continue
            r_gem_rst = str(r["gemini_weekly_reset"] or "").strip()
            r_p3_rst = str(r["p3_weekly_reset"] or "").strip()
            gem_match = (not gem_rst_target) or (not r_gem_rst) or (r_gem_rst == gem_rst_target)
            p3_match = (not p3_rst_target) or (not r_p3_rst) or (r_p3_rst == p3_rst_target)

            g5_v = _pct_or_none(r["gemini_5h"]) if gem_match else None
            gw_v = _pct_or_none(r["gemini_weekly"]) if gem_match else None
            p5_v = _pct_or_none(r["p3_5h"]) if p3_match else None
            pw_v = _pct_or_none(r["p3_weekly"]) if p3_match else None
            if g5_v is None and gw_v is None and p5_v is None and pw_v is None:
                continue

            label = datetime.fromtimestamp(ep).strftime("%H:%M")
            parsed.append(
                {
                    "ts_epoch": ep,
                    "label": label,
                    "gemini_5h": g5_v,
                    "gemini_weekly": gw_v,
                    "p3_5h": p5_v,
                    "p3_weekly": pw_v,
                }
            )
        if len(parsed) <= cap_pts:
            return parsed
        # Preserve endpoints and evenly sample interior points
        step = (len(parsed) - 1) / float(cap_pts - 1)
        sampled = [parsed[int(round(i * step))] for i in range(cap_pts - 1)]
        sampled.append(parsed[-1])
        return sampled

    def compute_rate_audit(self) -> RateAuditReport:
        self._ensure_synced()
        audit = RateAuditReport()

        with self._connect() as conn:
            total_row = conn.execute(
                "SELECT COUNT(*) AS c FROM quota_snapshots"
            ).fetchone()
            audit.total_snapshots = int(total_row["c"] or 0)
            if audit.total_snapshots == 0:
                audit.verdict = "已建立高精度配额快照表（当前为首笔基线，后续对话将自动比对实测扣费斜率与本机无匹配步骤的配额变化）。"
                return audit

            # Bounded audit window, single pass, zero per-pair SQL; anchored at newest non-future snapshot.
            future_cap = time.time() + self.FUTURE_SKEW_TOLERANCE_SEC
            anchor = self._robust_anchor_in_conn(conn)
            max_ep = float(anchor) if anchor is not None else 0.0
            window_start = max_ep - self.AUDIT_WINDOW_SEC
            snaps = conn.execute(
                "SELECT ts_utc, ts_epoch, event_type, gemini_5h, gemini_5h_reset, gemini_weekly, gemini_weekly_reset, "
                "p3_5h, p3_5h_reset, p3_weekly, p3_weekly_reset FROM quota_snapshots "
                "WHERE ts_epoch >= ? AND ts_epoch <= ? ORDER BY ts_epoch ASC, id ASC",
                (window_start - self.CARRY_MAX_GAP_SEC, future_cap),
            ).fetchall()
            step_rows = conn.execute(
                "SELECT ts_epoch, quota_group, family_id, usd_1x, usd_agy_est FROM step_ledger "
                "WHERE ts_epoch >= ? ORDER BY ts_epoch ASC",
                (window_start - self.CARRY_MAX_GAP_SEC - 300.0,),
            ).fetchall()

        if not snaps:
            audit.verdict = f"已累计 {audit.total_snapshots} 笔水位快照，暂未捕获跨度大于 0.05% 的有效斜率样本。"
            return audit
        audit.latest_snapshot_ts = str(snaps[-1]["ts_utc"])
        pool_5h_estimates: List[float] = []
        pool_wk_estimates: List[float] = []

        # In-memory step index per quota group: sorted epochs + prefix sums (bisect range queries)
        grp_ep: Dict[str, List[float]] = {"gemini": [], "3p": []}
        grp_u1: Dict[str, List[float]] = {"gemini": [0.0], "3p": [0.0]}
        grp_uagy: Dict[str, List[float]] = {"gemini": [0.0], "3p": [0.0]}
        grp_fam: Dict[str, List[str]] = {"gemini": [], "3p": []}
        for sr in step_rows:
            g = str(sr["quota_group"] or "")
            if g not in grp_ep:
                continue
            try:
                sep = float(sr["ts_epoch"])
                su1 = float(sr["usd_1x"] or 0.0)
                suagy = float(sr["usd_agy_est"] or 0.0)
            except (TypeError, ValueError):
                continue
            if not (math.isfinite(sep) and math.isfinite(su1) and math.isfinite(suagy)):
                continue
            grp_ep[g].append(sep)
            grp_u1[g].append(grp_u1[g][-1] + su1)
            grp_uagy[g].append(grp_uagy[g][-1] + suagy)
            grp_fam[g].append(str(sr["family_id"] or "gemini-other"))

        def _span(group: str, lo: float, hi: float) -> Tuple[int, int]:
            eps = grp_ep[group]
            return bisect.bisect_left(eps, lo), bisect.bisect_right(eps, hi)

        def _finite_or_none(v: Any) -> Optional[float]:
            if v is None:
                return None
            try:
                fv = float(v)
            except (TypeError, ValueError):
                return None
            return fv if math.isfinite(fv) else None

        # Carry the last REAL observation of each bucket across NULL (offline / NaN) gaps of up to
        # CARRY_MAX_GAP_SEC, remembering its source epoch so interval attribution still starts at that sample.
        cols = (
            ("gemini_5h", "gemini_5h_reset"),
            ("gemini_weekly", "gemini_weekly_reset"),
            ("p3_5h", "p3_5h_reset"),
            ("p3_weekly", "p3_weekly_reset"),
        )
        last_seen: Dict[str, Tuple[float, str, float]] = {}
        filled: List[Dict[str, Any]] = []
        for r in snaps:
            ep = float(r["ts_epoch"])
            d: Dict[str, Any] = {
                "ts_utc": str(r["ts_utc"]),
                "event_type": str(r["event_type"]),
                "ts_epoch": ep,
                "src": {},
            }
            for vc, rc in cols:
                v = _finite_or_none(r[vc])
                if v is not None:
                    d[vc] = v
                    d[rc] = str(r[rc] or "")
                    d["src"][vc] = ep
                    last_seen[vc] = (v, d[rc], ep)
                else:
                    seen = last_seen.get(vc)
                    if seen is not None and (ep - seen[2]) <= self.CARRY_MAX_GAP_SEC:
                        d[vc] = seen[0]
                        d[rc] = seen[1]
                        d["src"][vc] = seen[2]
                    else:
                        d[vc] = None
                        d[rc] = ""
            filled.append(d)

        def _calc_drop_pct(prev_row: Dict[str, Any], curr_row: Dict[str, Any], val_col: str, reset_col: str) -> float:
            fv1 = prev_row[val_col]
            fv2 = curr_row[val_col]
            if fv1 is None or fv2 is None:
                return 0.0
            r1 = str(prev_row[reset_col] or "")
            r2 = str(curr_row[reset_col] or "")
            if r1 and r2 and r1 != r2:
                # Quota reset boundary crossed between snapshots; ignore interval for drop/slope math
                return 0.0
            diff = (fv1 - fv2) * 100.0
            if not math.isfinite(diff):
                return 0.0
            return round(diff, 4) if diff > 0.0005 else 0.0

        for i in range(1, len(filled)):
            prev = filled[i - 1]
            curr = filled[i]
            t2 = float(curr["ts_epoch"])
            if t2 < window_start:
                continue  # lookback rows only seed carry-forward state
            if t2 - float(prev["ts_epoch"]) < 5.0:
                continue

            g5_drop = _calc_drop_pct(prev, curr, "gemini_5h", "gemini_5h_reset")
            gw_drop = _calc_drop_pct(prev, curr, "gemini_weekly", "gemini_weekly_reset")
            p5_drop = _calc_drop_pct(prev, curr, "p3_5h", "p3_5h_reset")
            pw_drop = _calc_drop_pct(prev, curr, "p3_weekly", "p3_weekly_reset")

            if g5_drop <= 0 and gw_drop <= 0 and p5_drop <= 0 and pw_drop <= 0:
                continue

            # Interval starts at the earliest REAL observation behind any dropping bucket (carry-forward aware)
            dropping = [
                vc
                for vc, dv in (("gemini_5h", g5_drop), ("gemini_weekly", gw_drop), ("p3_5h", p5_drop), ("p3_weekly", pw_drop))
                if dv > 0
            ]
            t1 = min(float(prev["src"].get(vc, prev["ts_epoch"])) for vc in dropping)

            # Exact Gemini transcript steps in [t1 - 5s, t2 + 5s] for slope attribution
            gi, gj = _span("gemini", t1 - 5.0, t2 + 5.0)
            steps = gj - gi
            u1 = grp_u1["gemini"][gj] - grp_u1["gemini"][gi]
            uagy = grp_uagy["gemini"][gj] - grp_uagy["gemini"][gi]

            # Wider grace window [t1 - 120s, t2 + 5s] per quota_group so in-flight streaming steps
            # (whose created_at precedes t1 while quota deducts before t2) and Claude/3p turns
            # never trigger false PhantomDrain alerts.
            gem_sig_drop = g5_drop >= 0.02 or gw_drop >= 0.01
            p3_sig_drop = p5_drop >= 0.02 or pw_drop >= 0.02

            gem_phantom = False
            p3_phantom = False
            if (t2 - t1) >= 30.0 and (gem_sig_drop or p3_sig_drop):
                if gem_sig_drop:
                    a, b = _span("gemini", t1 - 120.0, t2 + 5.0)
                    gem_phantom = (b - a) == 0
                if p3_sig_drop:
                    a, b = _span("3p", t1 - 120.0, t2 + 5.0)
                    p3_phantom = (b - a) == 0

            if gem_phantom or p3_phantom:
                audit.phantom_drains.append(
                    PhantomDrainEvent(
                        from_ts=str(prev["ts_utc"]),
                        to_ts=str(curr["ts_utc"]),
                        from_event=str(prev["event_type"]),
                        to_event=str(curr["event_type"]),
                        gemini_5h_drop_pct=g5_drop if gem_phantom else 0.0,
                        gemini_weekly_drop_pct=gw_drop if gem_phantom else 0.0,
                        p3_weekly_drop_pct=pw_drop if p3_phantom else 0.0,
                        p3_5h_drop_pct=p5_drop if p3_phantom else 0.0,
                    )
                )
            elif steps > 0 and u1 > 0.01 and (g5_drop >= 0.05 or gw_drop >= 0.02):
                fam_counts: Dict[str, int] = {}
                for fid in grp_fam["gemini"][gi:gj]:
                    fam_counts[fid] = fam_counts.get(fid, 0) + 1
                dom_fid = max(fam_counts.items(), key=lambda kv: kv[1])[0] if fam_counts else "gemini-other"
                dom_label = MODEL_CATALOG.get(dom_fid, MODEL_CATALOG["gemini-other"]).family_label

                imp_5h = round(u1 / (g5_drop / 100.0), 2) if g5_drop >= 0.05 else None
                imp_wk = round(u1 / (gw_drop / 100.0), 2) if gw_drop >= 0.02 else None
                if imp_5h is not None:
                    pool_5h_estimates.append(imp_5h)
                if imp_wk is not None:
                    pool_wk_estimates.append(imp_wk)

                audit.slope_samples.append(
                    RateSlopeSample(
                        from_ts=str(prev["ts_utc"]),
                        to_ts=str(curr["ts_utc"]),
                        duration_min=round((t2 - t1) / 60.0, 1),
                        steps=steps,
                        dominant_model=dom_label,
                        usd_1x=round(u1, 4),
                        usd_agy_15x_est=round(uagy, 4),
                        gemini_5h_drop_pct=g5_drop,
                        gemini_weekly_drop_pct=gw_drop,
                        implied_5h_pool_usd_at_1x=imp_5h,
                        implied_weekly_pool_usd_at_1x=imp_wk,
                    )
                )

        audit.slope_samples = audit.slope_samples[-10:]
        audit.phantom_drains = audit.phantom_drains[-10:]

        if pool_5h_estimates:
            audit.median_5h_pool_usd_at_1x = median(pool_5h_estimates)
        if pool_wk_estimates:
            audit.median_weekly_pool_usd_at_1x = median(pool_wk_estimates)

        if audit.median_5h_pool_usd_at_1x is not None:
            wk_extra = (
                f"；周总额度池实测等效总容量约为 ${audit.median_weekly_pool_usd_at_1x:.2f}"
                if audit.median_weekly_pool_usd_at_1x is not None
                else ""
            )
            audit.verdict = (
                f"按 1.0x 官方标准价折算，当前 5h 池实测等效总容量约为 ${audit.median_5h_pool_usd_at_1x:.2f}{wk_extra} "
                f"（4A/4C 的 1.5x 为预设估算倍率；配额变化可能包含其他设备的用量）。"
            )
        elif audit.median_weekly_pool_usd_at_1x is not None:
            audit.verdict = (
                f"按 1.0x 官方标准价折算，当前周总额度池实测等效总容量约为 ${audit.median_weekly_pool_usd_at_1x:.2f} "
                f"（5h 池单次跨度小于 0.05%，已根据周池高精度斜率完成解算）。"
            )
        elif audit.total_snapshots == 1:
            audit.verdict = "已记录首笔精确配额基线快照；再进行几轮对话后再次查看，即可自动解算实测扣费斜率与等效美元池容量。"
        else:
            audit.verdict = f"已累计 {audit.total_snapshots} 笔水位快照，暂未捕获跨度大于 0.05% 的有效斜率样本。"

        return audit
