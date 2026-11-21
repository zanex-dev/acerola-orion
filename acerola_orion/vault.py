# -*- coding: utf-8 -*-
"""
Orion-Vault 对话自动增量备份与防误删时光保险箱 (acerola_orion/vault.py)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
核心设计准则：
1. 只增不删 (Append-Only Vault)：
   - 在线热备 `conversations/<uuid>.db` (SQLite Online Backup + 快速 gzip 归档为 `conversation.db.gz`)；
   - 完整捕获 `conversation_summaries.db` 汇总行 (含 `raw_summary` Protobuf 二进制字段)、`conversations_fts` 索引块与 `annotations/<uuid>.pbtxt`；
   - 增量留档 `brain/<uuid>/` 下的 `transcript.jsonl`、`transcript_full.jsonl` 及纯文本/矢量 Artifacts。
   - 当用户在 Antigravity 界面点击“删除对话”抹除主库文件后，保险箱绝不跟随删除，而是自动将该会话标记为 `deleted`（已删可恢复）。
2. 严格媒体隐私隔离 (Heart-Under-Blade / agent-blindfold 100% 兼容)：
   - 物理跳过 `.user_uploaded`、`browser_sandbox`、`tempmediaStorage`、`.allow_media`、`agent-blindfold.json`
     以及全部位图/音视频二进制扩展名，零触碰敏感媒体。
3. 随时可控总开关 (`vault_auto_backup`)：
   - 关闭后，实时 `Stop` Hook 与后台守护巡检立即短路放行，零磁盘扫描、零写入，已有历史备份原封保留。
"""

from __future__ import annotations

import base64
from contextlib import closing, contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile
import threading
import time
from typing import Any, Dict, Iterator, List, Optional, Sequence, Set, Tuple

from .constants import (
    atomic_write_text,
    get_agy_annotations_dir,
    get_agy_brain_dir,
    get_agy_conversations_dir,
    get_agy_summaries_db_path,
    get_vault_dir,
    get_vault_index_db_path,
    interprocess_file_lock,
    is_vault_auto_backup_enabled,
)
from .rpc import read_file_bytes_shared

UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
PBTXT_TITLE_RE = re.compile(r'(?:^|[\s{;])title\s*:\s*"((?:[^"\\]|\\.)*)"')
PBTXT_SECONDS_RE = re.compile(r"last_user_view_time\s*:\s*\{\s*seconds\s*:\s*(\d+)")
USER_REQ_RE = re.compile(r"<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>", re.DOTALL)
MODEL_SWITCH_RE = re.compile(r"Model Selection`\s+from\s+.*?\s+to\s+([^\.\r\n]+)\.")
STRIP_BLOCKS_RE = re.compile(
    r"<(?:ADDITIONAL_METADATA|USER_SETTINGS_CHANGE|SYSTEM_MESSAGE)>.*?</(?:ADDITIONAL_METADATA|USER_SETTINGS_CHANGE|SYSTEM_MESSAGE)>",
    re.DOTALL,
)

# Strict Heart-Under-Blade media & ephemeral isolation sets
VAULT_SKIP_DIRS_LOWER: Set[str] = {
    ".user_uploaded",
    "browser_sandbox",
    "tempmediastorage",
    "__pycache__",
    ".git",
    "node_modules",
}

VAULT_SKIP_FILES_LOWER: Set[str] = {
    ".allow_media",
    "agent-blindfold.json",
    ".ds_store",
    "thumbs.db",
    "desktop.ini",
}

VAULT_SKIP_MEDIA_EXTS: Set[str] = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".ico", ".heic", ".heif", ".avif",
    ".mp4", ".mkv", ".avi", ".mov", ".webm", ".flv", ".wmv",
    ".mp3", ".wav", ".flac", ".m4a", ".ogg", ".aac",
}

MAX_ARTIFACT_FILE_BYTES = 64 * 1024 * 1024
MAX_TRANSCRIPT_FILE_BYTES = 256 * 1024 * 1024
MAX_SLICE_LINES = 40

_VAULT_DB_LOCK_GUARD = threading.Lock()
_VAULT_DB_LOCKS: Dict[str, threading.RLock] = {}


def _get_vault_rlock(db_path: Path) -> threading.RLock:
    key = os.path.normcase(os.path.abspath(str(db_path)))
    with _VAULT_DB_LOCK_GUARD:
        lk = _VAULT_DB_LOCKS.get(key)
        if lk is None:
            lk = threading.RLock()
            _VAULT_DB_LOCKS[key] = lk
        return lk


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _fmt_cn_time(raw: Any) -> str:
    tz_cn = timezone(timedelta(hours=8))
    if isinstance(raw, (int, float)) and raw > 0:
        return datetime.fromtimestamp(float(raw), tz=timezone.utc).astimezone(tz_cn).strftime("%Y-%m-%d %H:%M")
    if isinstance(raw, str) and raw.strip():
        s = raw.strip()
        if s.endswith(("Z", "z")):
            s = s[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(tz_cn).strftime("%Y-%m-%d %H:%M")
        except ValueError:
            return s[:16]
    return "-"


def _unescape_pbtxt(raw_val: str) -> str:
    buf = bytearray()
    i, n = 0, len(raw_val)
    simple = {
        "n": ord("\n"),
        "r": ord("\r"),
        "t": ord("\t"),
        '"': ord('"'),
        "'": ord("'"),
        "\\": ord("\\"),
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
        elif esc in ("x", "X") and i + 3 <= n and all(c in "0123456789abcdefABCDEF" for c in raw_val[i + 1 : i + 3]):
            buf.append(int(raw_val[i + 1 : i + 3], 16) & 0xFF)
            i += 3
        elif esc == "u" and i + 5 <= n and all(c in "0123456789abcdefABCDEF" for c in raw_val[i + 1 : i + 5]):
            buf.extend(chr(int(raw_val[i + 1 : i + 5], 16)).encode("utf-8", errors="replace"))
            i += 5
        elif esc in simple:
            buf.append(simple[esc])
            i += 1
        else:
            buf.extend(esc.encode("utf-8"))
            i += 1
    return buf.decode("utf-8", errors="replace")


def _clean_user_prompt(raw: str) -> str:
    if not raw:
        return ""
    m = USER_REQ_RE.search(raw)
    if m:
        return m.group(1).strip()
    cleaned = STRIP_BLOCKS_RE.sub("", raw)
    cleaned = re.sub(r"The following is a <SYSTEM_MESSAGE>.*", "", cleaned, flags=re.DOTALL)
    return cleaned.strip()


def _one_line(text: str, max_len: int = 120) -> str:
    flat = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(flat) <= max_len:
        return flat
    return flat[: max_len - 1] + "…"


def _encode_sql_val(v: Any) -> Any:
    if isinstance(v, memoryview):
        v = bytes(v)
    if isinstance(v, (bytes, bytearray)):
        return {"__b64__": base64.b64encode(bytes(v)).decode("ascii")}
    return v


def _decode_sql_val(v: Any) -> Any:
    if isinstance(v, dict) and set(v.keys()) == {"__b64__"} and isinstance(v["__b64__"], str):
        return base64.b64decode(v["__b64__"].encode("ascii"))
    return v


def _is_reparse_or_link(p: Path) -> bool:
    """Return True if `p` is a symlink, Windows junction, or any NTFS reparse point."""
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
    try:
        lst = os.lstat(p)
        attrs = getattr(lst, "st_file_attributes", 0)
        if attrs & 0x0400:  # FILE_ATTRIBUTE_REPARSE_POINT
            return True
    except OSError:
        return True
    return False


def _should_skip_brain_file(src_file: Path) -> bool:
    if _is_reparse_or_link(src_file):
        return True
    name_low = src_file.name.lower()
    if name_low in VAULT_SKIP_FILES_LOWER:
        return True
    if name_low.endswith((".tmp", ".lock", ".pyc")):
        return True
    if src_file.suffix.lower() in VAULT_SKIP_MEDIA_EXTS:
        return True
    try:
        sz = os.lstat(src_file).st_size
    except OSError:
        return True
    limit = MAX_TRANSCRIPT_FILE_BYTES if name_low in ("transcript.jsonl", "transcript_full.jsonl") else MAX_ARTIFACT_FILE_BYTES
    return sz > limit


@dataclass
class VaultSessionItem:
    conv_id: str
    title: str
    preview: str
    model_name: str
    step_count: int
    workspace_uris: str
    last_modified_at: str
    last_backed_up_at: str
    live_status: str  # 'active' | 'deleted'
    deleted_detected_at: str
    restored_at: str
    conv_db_bytes: int
    brain_files_count: int
    brain_bytes: int

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class VaultStatusReport:
    auto_backup_enabled: bool
    vault_dir: str
    total_conversations: int
    active_conversations: int
    deleted_conversations: int
    total_vault_bytes: int
    total_vault_mb: float
    last_sync_at: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class VaultSyncResult:
    ok: bool
    skipped_by_switch: bool = False
    scanned: int = 0
    backed_up: int = 0
    unchanged: int = 0
    newly_marked_deleted: int = 0
    elapsed_ms: float = 0.0
    message: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class VaultSearchHit:
    conv_id: str
    title: str
    live_status: str
    last_modified_at: str
    step_count: int
    title_matched: bool
    snippets: List[Tuple[int, str, str]] = field(default_factory=list)  # (line_no, role, snippet)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class OrionVault:
    """Append-only incremental backup and recovery vault for Antigravity conversations."""

    def __init__(
        self,
        vault_dir: Optional[Path] = None,
        agy_conv_dir: Optional[Path] = None,
        agy_brain_dir: Optional[Path] = None,
        agy_anno_dir: Optional[Path] = None,
        agy_summaries_db: Optional[Path] = None,
    ) -> None:
        self.vault_dir = Path(vault_dir) if vault_dir is not None else get_vault_dir()
        self.sessions_dir = self.vault_dir / "sessions"
        self.exports_dir = self.vault_dir / "exports"
        self.pre_restore_dir = self.vault_dir / "pre_restore_backups"
        self.index_db_path = (
            (self.vault_dir / "vault_index.db") if vault_dir is not None else get_vault_index_db_path()
        )
        self.sync_lock_marker = self.vault_dir / "vault_sync.state"

        self.agy_conv_dir = Path(agy_conv_dir) if agy_conv_dir is not None else get_agy_conversations_dir()
        self.agy_brain_dir = Path(agy_brain_dir) if agy_brain_dir is not None else get_agy_brain_dir()
        self.agy_anno_dir = Path(agy_anno_dir) if agy_anno_dir is not None else get_agy_annotations_dir()
        self.agy_summaries_db = (
            Path(agy_summaries_db) if agy_summaries_db is not None else get_agy_summaries_db_path()
        )
        self._rlock = _get_vault_rlock(self.index_db_path)
        self._ensure_schema()

    @contextmanager
    def _connect_index(self) -> Iterator[sqlite3.Connection]:
        self.vault_dir.mkdir(parents=True, exist_ok=True)
        with self._rlock:
            conn = sqlite3.connect(str(self.index_db_path), timeout=15.0)
            conn.row_factory = sqlite3.Row
            try:
                conn.execute("PRAGMA journal_mode=WAL;")
                conn.execute("PRAGMA synchronous=NORMAL;")
                conn.execute("PRAGMA busy_timeout=10000;")
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    def _ensure_schema(self) -> None:
        with self._connect_index() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS vault_meta (
                    k TEXT PRIMARY KEY,
                    v TEXT NOT NULL DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS vault_conversations (
                    conv_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL DEFAULT '',
                    preview TEXT NOT NULL DEFAULT '',
                    workspace_uris TEXT NOT NULL DEFAULT '',
                    project_id TEXT NOT NULL DEFAULT '',
                    model_name TEXT NOT NULL DEFAULT '',
                    step_count INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL DEFAULT '',
                    last_modified_at TEXT NOT NULL DEFAULT '',
                    first_backed_up_at TEXT NOT NULL DEFAULT '',
                    last_backed_up_at TEXT NOT NULL DEFAULT '',
                    live_status TEXT NOT NULL DEFAULT 'active',
                    deleted_detected_at TEXT NOT NULL DEFAULT '',
                    restored_at TEXT NOT NULL DEFAULT '',
                    db_sig TEXT NOT NULL DEFAULT '',
                    anno_sig TEXT NOT NULL DEFAULT '',
                    brain_sig TEXT NOT NULL DEFAULT '',
                    conv_db_bytes INTEGER NOT NULL DEFAULT 0,
                    brain_files_count INTEGER NOT NULL DEFAULT 0,
                    brain_bytes INTEGER NOT NULL DEFAULT 0,
                    summary_row_json TEXT NOT NULL DEFAULT '',
                    fts_rows_json TEXT NOT NULL DEFAULT '',
                    parent_id TEXT NOT NULL DEFAULT ''
                );

                CREATE INDEX IF NOT EXISTS idx_vault_live_status
                    ON vault_conversations(live_status, last_modified_at DESC);
                CREATE INDEX IF NOT EXISTS idx_vault_modified
                    ON vault_conversations(last_modified_at DESC);
                """
            )

    # ------------------------------------------------------------------
    # Signatures & Metadata Extraction
    # ------------------------------------------------------------------

    def _compute_db_sig(self, conv_id: str) -> str:
        db_file = self.agy_conv_dir / f"{conv_id}.db"
        if not db_file.is_file():
            return ""
        parts: List[str] = []
        for suffix in ("", "-wal"):
            p = Path(str(db_file) + suffix)
            try:
                st = p.stat()
                parts.append(f"{st.st_size}:{st.st_mtime_ns}")
            except OSError:
                parts.append("0:0")
        return "|".join(parts)

    def _compute_anno_sig(self, conv_id: str) -> str:
        p = self.agy_anno_dir / f"{conv_id}.pbtxt"
        try:
            st = p.stat()
            return f"{st.st_size}:{st.st_mtime_ns}"
        except OSError:
            return ""

    def _compute_quick_brain_probe(self, conv_id: str) -> str:
        root_dir = self.agy_brain_dir / conv_id
        if not root_dir.is_dir():
            return ""
        try:
            r_mt = int(root_dir.stat().st_mtime_ns)
        except OSError:
            return ""
        t_p = root_dir / ".system_generated" / "logs" / "transcript.jsonl"
        try:
            tst = t_p.stat()
            t_tag = f"{tst.st_size}:{int(tst.st_mtime_ns)}"
        except OSError:
            t_tag = "0:0"
        return f"{r_mt}:{t_tag}"

    def _scan_brain_tree(self, conv_id: str) -> Tuple[str, List[Tuple[Path, Path, int, int]]]:
        """
        Return `(digest_sig, [(src_path, rel_path, size, mtime_ns), ...])` for safe text/vector files
        inside `brain/<conv_id>/`. Strictly skips `.user_uploaded`, `browser_sandbox`, media files,
        and symlinks / Windows NTFS reparse points.
        """
        root_dir = self.agy_brain_dir / conv_id
        if not root_dir.is_dir() or _is_reparse_or_link(root_dir):
            return "", []
        quick_p = self._compute_quick_brain_probe(conv_id)
        entries: List[Tuple[Path, Path, int, int]] = []
        h = hashlib.sha1()
        for cur_dir, dirs, files in os.walk(root_dir, followlinks=False):
            cur_p = Path(cur_dir)
            dirs[:] = sorted(
                d
                for d in dirs
                if d.lower() not in VAULT_SKIP_DIRS_LOWER and not _is_reparse_or_link(cur_p / d)
            )
            rel_dir = cur_p.relative_to(root_dir)
            for fname in sorted(files):
                src_f = cur_p / fname
                if _should_skip_brain_file(src_f):
                    continue
                try:
                    st = os.lstat(src_f)
                except OSError:
                    continue
                rel_f = rel_dir / fname
                entries.append((src_f, rel_f, int(st.st_size), int(st.st_mtime_ns)))
                h.update(f"{rel_f.as_posix()}:{st.st_size}:{st.st_mtime_ns}\n".encode("utf-8"))
        digest = h.hexdigest() if entries else "empty"
        return f"{quick_p}|{digest}", entries

    def _read_annotation_meta(self, conv_id: str) -> Tuple[str, Optional[float]]:
        p = self.agy_anno_dir / f"{conv_id}.pbtxt"
        if not p.is_file():
            return "", None
        try:
            raw = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return "", None
        m_title = PBTXT_TITLE_RE.search(raw)
        title = _unescape_pbtxt(m_title.group(1)).strip() if m_title else ""
        m_sec = PBTXT_SECONDS_RE.search(raw)
        sec = float(m_sec.group(1)) if m_sec else None
        return title, sec

    def _read_summary_bundle(self, conv_id: str) -> Tuple[Dict[str, Any], List[Dict[str, Any]], str]:
        """Read exact row(s) for `conv_id` from `conversation_summaries.db` (read-only)."""
        if not self.agy_summaries_db.is_file():
            return {}, [], ""
        uri = f"file:{self.agy_summaries_db.as_posix()}?mode=ro"
        summary_dict: Dict[str, Any] = {}
        fts_rows: List[Dict[str, Any]] = []
        parent_id = ""
        try:
            with closing(sqlite3.connect(uri, uri=True, timeout=5.0)) as conn:
                conn.row_factory = sqlite3.Row
                row = conn.execute(
                    "SELECT * FROM conversation_summaries WHERE conversation_id = ?",
                    (conv_id,),
                ).fetchone()
                if row is not None:
                    summary_dict = {k: _encode_sql_val(row[k]) for k in row.keys()}
                try:
                    for frow in conn.execute(
                        "SELECT * FROM conversations_fts WHERE conversation_id = ?",
                        (conv_id,),
                    ).fetchall():
                        fts_rows.append({k: _encode_sql_val(frow[k]) for k in frow.keys()})
                except sqlite3.Error:
                    pass
                try:
                    prow = conn.execute(
                        "SELECT parent_id FROM conversation_parents WHERE conversation_id = ?",
                        (conv_id,),
                    ).fetchone()
                    if prow is not None and prow["parent_id"]:
                        parent_id = str(prow["parent_id"])
                except sqlite3.Error:
                    pass
        except sqlite3.Error:
            pass
        return summary_dict, fts_rows, parent_id

    def _extract_transcript_hints(self, transcript_path: Path) -> Dict[str, Any]:
        """Lightweight single-pass extraction of fallback title, preview, model, steps, and timestamps."""
        info: Dict[str, Any] = {
            "first_user": "",
            "last_preview": "",
            "model_name": "",
            "steps": 0,
            "first_ts": "",
            "last_ts": "",
        }
        if not transcript_path.is_file():
            return info
        try:
            with open(transcript_path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    s = line.strip()
                    if not s:
                        continue
                    info["steps"] += 1
                    try:
                        obj = json.loads(s)
                    except json.JSONDecodeError:
                        continue
                    ts = str(obj.get("created_at") or "")
                    if ts:
                        if not info["first_ts"]:
                            info["first_ts"] = ts
                        info["last_ts"] = ts
                    raw_content = str(obj.get("content") or "")
                    m_mod = MODEL_SWITCH_RE.search(raw_content)
                    if m_mod:
                        info["model_name"] = m_mod.group(1).strip()
                    if obj.get("type") == "USER_INPUT":
                        cleaned = _clean_user_prompt(raw_content)
                        if cleaned:
                            if not info["first_user"]:
                                info["first_user"] = _one_line(cleaned, 80)
                            info["last_preview"] = _one_line(cleaned, 140)
                    elif obj.get("type") == "PLANNER_RESPONSE":
                        txt = str(obj.get("content") or "").strip()
                        if txt:
                            info["last_preview"] = _one_line(txt, 140)
        except OSError:
            pass
        return info

    # ------------------------------------------------------------------
    # Hot Backup Primitives
    # ------------------------------------------------------------------

    def _backup_conv_sqlite_gz(self, src_db: Path, dst_gz: Path) -> int:
        """Online SQLite backup of `src_db` into normalized DELETE-journal snapshot, compressed to `dst_gz`."""
        dst_gz.parent.mkdir(parents=True, exist_ok=True)
        tmp_fd, tmp_sqlite_str = tempfile.mkstemp(suffix=".sqlite.tmp", dir=str(dst_gz.parent))
        os.close(tmp_fd)
        tmp_sqlite = Path(tmp_sqlite_str)
        tmp_gz = dst_gz.with_name(f".{dst_gz.name}.tmp.{os.getpid()}.{threading.get_ident()}")
        try:
            backed_up_ok = False
            try:
                src_uri = f"file:{src_db.as_posix()}?mode=ro"
                with closing(sqlite3.connect(src_uri, uri=True, timeout=10.0)) as s_conn:
                    with closing(sqlite3.connect(str(tmp_sqlite), timeout=10.0)) as d_conn:
                        s_conn.backup(d_conn)
                with closing(sqlite3.connect(str(tmp_sqlite), timeout=10.0)) as c_conn:
                    c_conn.execute("PRAGMA journal_mode=DELETE;")
                backed_up_ok = tmp_sqlite.is_file() and tmp_sqlite.stat().st_size > 0
            except Exception:
                backed_up_ok = False

            if backed_up_ok:
                with open(tmp_sqlite, "rb") as fin, gzip.open(tmp_gz, "wb", compresslevel=1) as fout:
                    shutil.copyfileobj(fin, fout, length=256 * 1024)
            else:
                raw_bytes = read_file_bytes_shared(src_db)
                if not raw_bytes or not raw_bytes.startswith(b"SQLite format 3\x00"):
                    raise OSError(f"无法安全读取会话数据库: {src_db}")
                with gzip.open(tmp_gz, "wb", compresslevel=1) as fout:
                    fout.write(raw_bytes)

            for attempt in range(25):
                try:
                    os.replace(tmp_gz, dst_gz)
                    break
                except OSError:
                    if attempt == 24:
                        raise
                    time.sleep(0.01 * (attempt + 1))
            return int(dst_gz.stat().st_size)
        finally:
            tmp_sqlite.unlink(missing_ok=True)
            tmp_gz.unlink(missing_ok=True)

    def _sync_one_unlocked(
        self,
        conn: sqlite3.Connection,
        conv_id: str,
        existing_row: Optional[sqlite3.Row],
        allow_quick_probe: bool = False,
    ) -> bool:
        """
        Incrementally back up a single `conv_id`. Returns `True` if any artifact/db/metadata was updated,
        or `False` if signatures matched completely (fast skip).
        """
        cid = conv_id.strip().lower()
        if not UUID_RE.match(cid):
            return False
        src_db = self.agy_conv_dir / f"{cid}.db"
        if not src_db.is_file():
            return False

        db_sig = self._compute_db_sig(cid)
        anno_sig = self._compute_anno_sig(cid)
        sess_dir = self.sessions_dir / cid
        dst_gz = sess_dir / "conversation.db.gz"

        if (
            allow_quick_probe
            and existing_row is not None
            and existing_row["live_status"] == "active"
            and existing_row["db_sig"] == db_sig
            and existing_row["anno_sig"] == anno_sig
            and dst_gz.is_file()
        ):
            qp = self._compute_quick_brain_probe(cid)
            prev_bsig = str(existing_row["brain_sig"] or "")
            if (qp == "" and prev_bsig == "") or (qp and prev_bsig.startswith(qp + "|")):
                return False

        brain_sig, brain_entries = self._scan_brain_tree(cid)

        if (
            existing_row is not None
            and existing_row["live_status"] == "active"
            and existing_row["db_sig"] == db_sig
            and existing_row["anno_sig"] == anno_sig
            and existing_row["brain_sig"] == brain_sig
            and dst_gz.is_file()
        ):
            return False

        sess_dir.mkdir(parents=True, exist_ok=True)

        # 1. Conversation SQLite DB (only re-dump when db_sig changed or archive missing)
        if existing_row is None or existing_row["db_sig"] != db_sig or not dst_gz.is_file():
            conv_gz_bytes = self._backup_conv_sqlite_gz(src_db, dst_gz)
            # Re-sample db_sig after read-only connection closes (WAL recovery/checkpoint on open can touch mtime)
            db_sig = self._compute_db_sig(cid)
        else:
            conv_gz_bytes = int(dst_gz.stat().st_size)

        # 2. Annotation pbtxt
        src_anno = self.agy_anno_dir / f"{cid}.pbtxt"
        dst_anno = sess_dir / "annotation.pbtxt"
        if src_anno.is_file() and (existing_row is None or existing_row["anno_sig"] != anno_sig or not dst_anno.is_file()):
            raw_anno = read_file_bytes_shared(src_anno)
            if raw_anno is not None:
                atomic_write_text(dst_anno, raw_anno.decode("utf-8", errors="replace"), encoding="utf-8")

        # 3. Brain non-destructive incremental copy (never deletes older files in sess_dir/brain!)
        dst_brain_root = sess_dir / "brain"
        if brain_entries and (existing_row is None or existing_row["brain_sig"] != brain_sig or not dst_brain_root.is_dir()):
            for src_f, rel_f, sz, _mtime_ns in brain_entries:
                dst_f = dst_brain_root / rel_f
                need_copy = True
                if dst_f.is_file():
                    try:
                        dst_st = dst_f.stat()
                        src_st = src_f.stat()
                        if dst_st.st_size == sz and int(dst_st.st_mtime_ns) == int(src_st.st_mtime_ns):
                            need_copy = False
                    except OSError:
                        need_copy = True
                if need_copy:
                    dst_f.parent.mkdir(parents=True, exist_ok=True)
                    try:
                        shutil.copy2(src_f, dst_f)
                    except OSError:
                        raw_b = read_file_bytes_shared(src_f)
                        if raw_b is not None:
                            dst_f.write_bytes(raw_b)

        # Measure total backed-up brain files (including previously archived files)
        total_brain_files = 0
        total_brain_bytes = 0
        if dst_brain_root.is_dir():
            for cur_d, _, fnames in os.walk(dst_brain_root):
                cur_dp = Path(cur_d)
                for fn in fnames:
                    try:
                        total_brain_bytes += (cur_dp / fn).stat().st_size
                        total_brain_files += 1
                    except OSError:
                        pass

        # 4. Metadata & Summary Index capture
        anno_title, anno_view_sec = self._read_annotation_meta(cid)
        summary_dict, fts_rows, parent_id = self._read_summary_bundle(cid)
        vault_transcript = dst_brain_root / ".system_generated" / "logs" / "transcript.jsonl"
        live_transcript = self.agy_brain_dir / cid / ".system_generated" / "logs" / "transcript.jsonl"
        t_hints = self._extract_transcript_hints(vault_transcript if vault_transcript.is_file() else live_transcript)

        title = (
            anno_title
            or str(summary_dict.get("title") or "").strip()
            or str(t_hints.get("first_user") or "").strip()
            or (str(existing_row["title"]) if existing_row and existing_row["title"] else "")
            or f"未命名对话 ({cid[:8]})"
        )
        preview = (
            str(summary_dict.get("preview") or "").strip()
            or str(t_hints.get("last_preview") or "").strip()
            or (str(existing_row["preview"]) if existing_row else "")
        )
        workspace_uris = str(summary_dict.get("workspace_uris") or (existing_row["workspace_uris"] if existing_row else "") or "")
        project_id = str(summary_dict.get("project_id") or (existing_row["project_id"] if existing_row else "") or "")
        model_name = (
            str(t_hints.get("model_name") or "").strip()
            or (str(existing_row["model_name"]) if existing_row else "")
        )
        step_count = max(
            int(summary_dict.get("step_count") or 0),
            int(t_hints.get("steps") or 0),
            int(existing_row["step_count"]) if existing_row else 0,
        )
        created_at = (
            str(t_hints.get("first_ts") or "")
            or (str(existing_row["created_at"]) if existing_row else "")
            or _now_iso()
        )
        last_mod = (
            str(summary_dict.get("last_modified_time") or "").strip()
            or str(t_hints.get("last_ts") or "").strip()
        )
        if not last_mod:
            if anno_view_sec:
                last_mod = datetime.fromtimestamp(anno_view_sec, tz=timezone.utc).astimezone().isoformat(timespec="seconds")
            else:
                try:
                    last_mod = datetime.fromtimestamp(src_db.stat().st_mtime, tz=timezone.utc).astimezone().isoformat(timespec="seconds")
                except OSError:
                    last_mod = _now_iso()

        now_s = _now_iso()
        first_bak = str(existing_row["first_backed_up_at"]) if (existing_row and existing_row["first_backed_up_at"]) else now_s
        restored_at = str(existing_row["restored_at"]) if existing_row else ""

        # Preserve prior non-empty summary_row_json if live summary_dict hasn't flushed yet
        summary_json = (
            json.dumps(summary_dict, ensure_ascii=False)
            if summary_dict
            else (str(existing_row["summary_row_json"]) if existing_row else "")
        )
        fts_json = (
            json.dumps(fts_rows, ensure_ascii=False)
            if fts_rows
            else (str(existing_row["fts_rows_json"]) if existing_row else "")
        )
        eff_parent = parent_id or (str(existing_row["parent_id"]) if existing_row else "")

        conn.execute(
            """
            INSERT INTO vault_conversations (
                conv_id, title, preview, workspace_uris, project_id, model_name,
                step_count, created_at, last_modified_at, first_backed_up_at, last_backed_up_at,
                live_status, deleted_detected_at, restored_at,
                db_sig, anno_sig, brain_sig,
                conv_db_bytes, brain_files_count, brain_bytes,
                summary_row_json, fts_rows_json, parent_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', '', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(conv_id) DO UPDATE SET
                title = excluded.title,
                preview = excluded.preview,
                workspace_uris = excluded.workspace_uris,
                project_id = excluded.project_id,
                model_name = excluded.model_name,
                step_count = excluded.step_count,
                created_at = excluded.created_at,
                last_modified_at = excluded.last_modified_at,
                last_backed_up_at = excluded.last_backed_up_at,
                live_status = 'active',
                db_sig = excluded.db_sig,
                anno_sig = excluded.anno_sig,
                brain_sig = excluded.brain_sig,
                conv_db_bytes = excluded.conv_db_bytes,
                brain_files_count = excluded.brain_files_count,
                brain_bytes = excluded.brain_bytes,
                summary_row_json = excluded.summary_row_json,
                fts_rows_json = excluded.fts_rows_json,
                parent_id = excluded.parent_id
            """,
            (
                cid, title, preview, workspace_uris, project_id, model_name,
                step_count, created_at, last_mod, first_bak, now_s,
                restored_at,
                db_sig, anno_sig, brain_sig,
                conv_gz_bytes, total_brain_files, total_brain_bytes,
                summary_json, fts_json, eff_parent,
            ),
        )
        conn.execute(
            "INSERT OR REPLACE INTO vault_meta (k, v) VALUES ('last_sync_at', ?)",
            (now_s,),
        )
        return True

    # ------------------------------------------------------------------
    # Public Sync / Hook / Status / Query / Restore / Export APIs
    # ------------------------------------------------------------------

    def sync_conversation(self, conv_id: str, respect_switch: bool = False) -> VaultSyncResult:
        """Back up a single conversation by UUID (used by real-time `Stop` hook)."""
        t0 = time.perf_counter()
        if respect_switch and not is_vault_auto_backup_enabled():
            return VaultSyncResult(
                ok=True,
                skipped_by_switch=True,
                elapsed_ms=round((time.perf_counter() - t0) * 1000.0, 2),
                message="自动备份开关已关闭，跳过实时归档。",
            )
        cid = str(conv_id or "").strip().lower()
        if not UUID_RE.match(cid):
            return VaultSyncResult(ok=False, message=f"无效的会话 UUID: {conv_id!r}")

        with interprocess_file_lock(self.sync_lock_marker, timeout=8.0):
            with self._connect_index() as conn:
                existing = conn.execute(
                    "SELECT * FROM vault_conversations WHERE conv_id = ?",
                    (cid,),
                ).fetchone()
                updated = self._sync_one_unlocked(conn, cid, existing)

        dt_ms = round((time.perf_counter() - t0) * 1000.0, 2)
        return VaultSyncResult(
            ok=True,
            scanned=1,
            backed_up=1 if updated else 0,
            unchanged=0 if updated else 1,
            elapsed_ms=dt_ms,
            message=f"会话 {cid[:8]} 已增量备份 ({dt_ms:.1f} ms)" if updated else f"会话 {cid[:8]} 无变更 ({dt_ms:.1f} ms)",
        )

    def sync_all(
        self,
        respect_switch: bool = False,
        max_budget_sec: float = 0.0,
        abort_check: Optional[ Any ] = None,
    ) -> VaultSyncResult:
        """
        Incrementally back up all live Antigravity conversations and mark any vanished ones
        as `live_status = 'deleted'` without ever deleting their vault snapshots.
        """
        t0 = time.perf_counter()
        if respect_switch and not is_vault_auto_backup_enabled():
            return VaultSyncResult(
                ok=True,
                skipped_by_switch=True,
                elapsed_ms=round((time.perf_counter() - t0) * 1000.0, 2),
                message="对话自动备份开关当前处于关闭状态，已跳过自动同步。",
            )

        live_ids: Set[str] = set()
        if self.agy_conv_dir.is_dir():
            for entry in os.scandir(self.agy_conv_dir):
                if entry.is_file() and entry.name.endswith(".db"):
                    stem = entry.name[:-3].lower()
                    if UUID_RE.match(stem):
                        live_ids.add(stem)

        scanned = 0
        backed_up = 0
        unchanged = 0
        newly_deleted = 0
        budget_exhausted = False

        with interprocess_file_lock(self.sync_lock_marker, timeout=15.0):
            with self._connect_index() as conn:
                existing_map: Dict[str, sqlite3.Row] = {
                    str(r["conv_id"]).lower(): r
                    for r in conn.execute("SELECT * FROM vault_conversations").fetchall()
                }

                # Sort so unbacked or newest conversations get backed up first when max_budget_sec > 0
                def _sort_key(cid: str) -> Tuple[int, float]:
                    in_vault = 1 if cid in existing_map else 0
                    try:
                        mt = (self.agy_conv_dir / f"{cid}.db").stat().st_mtime
                    except OSError:
                        mt = 0.0
                    return (in_vault, -mt)

                for cid in sorted(live_ids, key=_sort_key):
                    if callable(abort_check) and abort_check():
                        budget_exhausted = True
                        break
                    if max_budget_sec > 0.0 and scanned > 0 and (time.perf_counter() - t0) >= max_budget_sec:
                        budget_exhausted = True
                        break
                    scanned += 1
                    try:
                        changed = self._sync_one_unlocked(conn, cid, existing_map.get(cid), allow_quick_probe=True)
                        if changed:
                            backed_up += 1
                        else:
                            unchanged += 1
                    except Exception:
                        continue

                # Detect conversations deleted in Antigravity UI -> preserve & mark 'deleted'
                if self.agy_conv_dir.is_dir():
                    now_s = _now_iso()
                    for cid, row in existing_map.items():
                        if cid not in live_ids and row["live_status"] != "deleted":
                            gz_p = self.sessions_dir / cid / "conversation.db.gz"
                            if gz_p.is_file():
                                conn.execute(
                                    """
                                    UPDATE vault_conversations
                                    SET live_status = 'deleted',
                                        deleted_detected_at = CASE
                                            WHEN deleted_detected_at = '' THEN ?
                                            ELSE deleted_detected_at
                                        END
                                    WHERE conv_id = ?
                                    """,
                                    (now_s, cid),
                                )
                                newly_deleted += 1

                conn.execute(
                    "INSERT OR REPLACE INTO vault_meta (k, v) VALUES ('last_sync_at', ?)",
                    (_now_iso(),),
                )

        dt_ms = round((time.perf_counter() - t0) * 1000.0, 1)
        partial_note = " [达到单次时间片上限，余下将在下次巡检续传]" if budget_exhausted else ""
        del_note = f"，新捕获 {newly_deleted} 个已删对话并保护留底" if newly_deleted > 0 else ""
        return VaultSyncResult(
            ok=True,
            scanned=scanned,
            backed_up=backed_up,
            unchanged=unchanged,
            newly_marked_deleted=newly_deleted,
            elapsed_ms=dt_ms,
            message=(
                f"保险箱同步完成：扫描 {scanned} 个会话，新增/更新 {backed_up} 个，"
                f"无变更跳过 {unchanged} 个{del_note} ({dt_ms:.0f} ms){partial_note}"
            ),
        )

    def get_status(self, refresh_deleted_flags: bool = True) -> VaultStatusReport:
        """Return Vault summary metrics via a lightweight SQLite query."""
        if refresh_deleted_flags and self.agy_conv_dir.is_dir():
            live_ids = {
                e.name[:-3].lower()
                for e in os.scandir(self.agy_conv_dir)
                if e.is_file() and e.name.endswith(".db") and UUID_RE.match(e.name[:-3])
            }
            with self._connect_index() as conn:
                now_s = _now_iso()
                for r in conn.execute("SELECT conv_id, live_status FROM vault_conversations").fetchall():
                    cid = str(r["conv_id"]).lower()
                    if cid not in live_ids and r["live_status"] != "deleted":
                        if (self.sessions_dir / cid / "conversation.db.gz").is_file():
                            conn.execute(
                                "UPDATE vault_conversations SET live_status = 'deleted', "
                                "deleted_detected_at = CASE WHEN deleted_detected_at = '' THEN ? ELSE deleted_detected_at END "
                                "WHERE conv_id = ?",
                                (now_s, cid),
                            )
                    elif cid in live_ids and r["live_status"] == "deleted":
                        conn.execute(
                            "UPDATE vault_conversations SET live_status = 'active' WHERE conv_id = ?",
                            (cid,),
                        )

        with self._connect_index() as conn:
            row = conn.execute(
                """
                SELECT
                    COUNT(*) AS total_cnt,
                    SUM(CASE WHEN live_status = 'active' THEN 1 ELSE 0 END) AS active_cnt,
                    SUM(CASE WHEN live_status = 'deleted' THEN 1 ELSE 0 END) AS deleted_cnt,
                    COALESCE(SUM(conv_db_bytes + brain_bytes), 0) AS total_bytes
                FROM vault_conversations
                """
            ).fetchone()
            meta_row = conn.execute("SELECT v FROM vault_meta WHERE k = 'last_sync_at'").fetchone()

        total_cnt = int(row["total_cnt"] or 0) if row else 0
        active_cnt = int(row["active_cnt"] or 0) if row else 0
        deleted_cnt = int(row["deleted_cnt"] or 0) if row else 0
        total_bytes = int(row["total_bytes"] or 0) if row else 0
        try:
            if self.index_db_path.is_file():
                total_bytes += int(self.index_db_path.stat().st_size)
        except OSError:
            pass
        last_sync = str(meta_row["v"]) if meta_row else ""
        return VaultStatusReport(
            auto_backup_enabled=is_vault_auto_backup_enabled(),
            vault_dir=str(self.vault_dir),
            total_conversations=total_cnt,
            active_conversations=active_cnt,
            deleted_conversations=deleted_cnt,
            total_vault_bytes=total_bytes,
            total_vault_mb=round(total_bytes / (1024.0 * 1024.0), 2),
            last_sync_at=last_sync,
        )

    def list_sessions(
        self,
        deleted_only: bool = False,
        query: str = "",
        limit: int = 100,
    ) -> List[VaultSessionItem]:
        q = str(query or "").strip().lower()
        sql = "SELECT * FROM vault_conversations"
        clauses: List[str] = []
        params: List[Any] = []
        if deleted_only:
            clauses.append("live_status = 'deleted'")
        if q:
            clauses.append("(LOWER(title) LIKE ? OR LOWER(preview) LIKE ? OR LOWER(conv_id) LIKE ?)")
            like_q = f"%{q}%"
            params.extend([like_q, like_q, like_q])
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY CASE WHEN live_status = 'deleted' THEN 0 ELSE 1 END, last_modified_at DESC LIMIT ?"
        params.append(max(1, min(2000, int(limit))))

        out: List[VaultSessionItem] = []
        with self._connect_index() as conn:
            for r in conn.execute(sql, params).fetchall():
                out.append(
                    VaultSessionItem(
                        conv_id=str(r["conv_id"]),
                        title=str(r["title"]),
                        preview=str(r["preview"]),
                        model_name=str(r["model_name"]),
                        step_count=int(r["step_count"] or 0),
                        workspace_uris=str(r["workspace_uris"]),
                        last_modified_at=str(r["last_modified_at"]),
                        last_backed_up_at=str(r["last_backed_up_at"]),
                        live_status=str(r["live_status"]),
                        deleted_detected_at=str(r["deleted_detected_at"]),
                        restored_at=str(r["restored_at"]),
                        conv_db_bytes=int(r["conv_db_bytes"] or 0),
                        brain_files_count=int(r["brain_files_count"] or 0),
                        brain_bytes=int(r["brain_bytes"] or 0),
                    )
                )
        return out

    def resolve_conv_id(self, id_or_prefix: str) -> str:
        raw = str(id_or_prefix or "").strip().lower()
        if not raw:
            raise ValueError("请提供会话 UUID 或前 8 位前缀。")
        with self._connect_index() as conn:
            exact = conn.execute(
                "SELECT conv_id FROM vault_conversations WHERE conv_id = ?",
                (raw,),
            ).fetchone()
            if exact is not None:
                return str(exact["conv_id"])
            rows = conn.execute(
                "SELECT conv_id, title FROM vault_conversations WHERE conv_id LIKE ? ORDER BY last_modified_at DESC LIMIT 10",
                (f"{raw}%",),
            ).fetchall()
        if not rows:
            raise KeyError(f"保险箱中未找到匹配 {id_or_prefix!r} 的对话备份。")
        if len(rows) > 1:
            cands = ", ".join(f"{r['conv_id'][:8]}({r['title'][:20]})" for r in rows[:5])
            raise ValueError(f"前缀 {id_or_prefix!r} 命中多个会话，请提供更长前缀: {cands}")
        return str(rows[0]["conv_id"])

    def search_vault(
        self,
        keywords: Sequence[str],
        deleted_only: bool = False,
        limit: int = 20,
        max_snippets: int = 4,
    ) -> List[VaultSearchHit]:
        terms = [k.strip() for k in keywords if k and k.strip()]
        if not terms:
            return []
        patterns = [re.compile(re.escape(t), re.IGNORECASE) for t in terms]

        sql = "SELECT conv_id, title, live_status, last_modified_at, step_count FROM vault_conversations"
        if deleted_only:
            sql += " WHERE live_status = 'deleted'"
        sql += " ORDER BY CASE WHEN live_status = 'deleted' THEN 0 ELSE 1 END, last_modified_at DESC"

        with self._connect_index() as conn:
            candidates = conn.execute(sql).fetchall()

        hits: List[VaultSearchHit] = []
        for r in candidates:
            cid = str(r["conv_id"])
            title = str(r["title"])
            title_matched = all(p.search(title) for p in patterns)
            snippets: List[Tuple[int, str, str]] = []
            t_path = self.sessions_dir / cid / "brain" / ".system_generated" / "logs" / "transcript.jsonl"
            matched_terms_in_body: Set[int] = {i for i, p in enumerate(patterns) if p.search(title)}

            if t_path.is_file():
                try:
                    with open(t_path, "r", encoding="utf-8", errors="replace") as f:
                        for line_no, raw_ln in enumerate(f, 1):
                            if not any(p.search(raw_ln) for p in patterns):
                                continue
                            try:
                                obj = json.loads(raw_ln)
                            except json.JSONDecodeError:
                                continue
                            stype = str(obj.get("type") or "")
                            if stype not in ("USER_INPUT", "PLANNER_RESPONSE"):
                                continue
                            content = str(obj.get("content") or "")
                            if stype == "USER_INPUT":
                                content = _clean_user_prompt(content)
                            if not content:
                                continue
                            line_hit = False
                            for idx_p, pat in enumerate(patterns):
                                if pat.search(content):
                                    matched_terms_in_body.add(idx_p)
                                    line_hit = True
                            if line_hit and len(snippets) < max_snippets:
                                flat = re.sub(r"\s+", " ", content).strip()
                                m0 = next((p.search(flat) for p in patterns if p.search(flat)), None)
                                if m0:
                                    s_idx = max(0, m0.start() - 55)
                                    e_idx = min(len(flat), m0.end() + 75)
                                    snip = ("…" if s_idx > 0 else "") + flat[s_idx:e_idx] + ("…" if e_idx < len(flat) else "")
                                else:
                                    snip = _one_line(flat, 130)
                                role = "USER" if stype == "USER_INPUT" else "AGENT"
                                snippets.append((line_no, role, snip))
                except OSError:
                    pass

            if title_matched or len(matched_terms_in_body) == len(patterns):
                hits.append(
                    VaultSearchHit(
                        conv_id=cid,
                        title=title,
                        live_status=str(r["live_status"]),
                        last_modified_at=str(r["last_modified_at"]),
                        step_count=int(r["step_count"] or 0),
                        title_matched=title_matched,
                        snippets=snippets,
                    )
                )
                if len(hits) >= limit:
                    break
        return hits

    def show_slice(
        self,
        id_or_prefix: str,
        around_line: Optional[int] = None,
        context: int = 4,
        tail: int = 12,
    ) -> Dict[str, Any]:
        """Read a bounded slice (<= 40 lines) of a backed-up conversation's transcript."""
        cid = self.resolve_conv_id(id_or_prefix)
        with self._connect_index() as conn:
            row = conn.execute("SELECT * FROM vault_conversations WHERE conv_id = ?", (cid,)).fetchone()
        t_path = self.sessions_dir / cid / "brain" / ".system_generated" / "logs" / "transcript.jsonl"
        if not t_path.is_file():
            return {"ok": False, "conv_id": cid, "message": "该备份中暂无 transcript.jsonl 轨迹文本。"}

        raw_lines = t_path.read_text(encoding="utf-8", errors="replace").splitlines()
        total = len(raw_lines)
        if around_line is not None and around_line > 0:
            ctx = max(1, min(18, int(context)))
            start_ln = max(1, int(around_line) - ctx)
            end_ln = min(total, int(around_line) + ctx)
        else:
            t_cnt = max(1, min(MAX_SLICE_LINES, int(tail)))
            start_ln = max(1, total - t_cnt + 1)
            end_ln = total

        if (end_ln - start_ln + 1) > MAX_SLICE_LINES:
            end_ln = start_ln + MAX_SLICE_LINES - 1

        rendered: List[Dict[str, Any]] = []
        for lno in range(start_ln, end_ln + 1):
            try:
                obj = json.loads(raw_lines[lno - 1])
            except Exception:
                continue
            stype = str(obj.get("type") or "")
            txt = str(obj.get("content") or "")
            if stype == "USER_INPUT":
                txt = _clean_user_prompt(txt)
            rendered.append(
                {
                    "line": lno,
                    "type": stype,
                    "created_at": _fmt_cn_time(obj.get("created_at")),
                    "content": _one_line(txt, 360),
                }
            )
        return {
            "ok": True,
            "conv_id": cid,
            "title": str(row["title"]) if row else cid,
            "live_status": str(row["live_status"]) if row else "unknown",
            "total_lines": total,
            "range": [start_ln, end_ln],
            "steps": rendered,
        }

    def restore_conversation(self, id_or_prefix: str, force: bool = False) -> Dict[str, Any]:
        """
        Losslessly restore a backed-up conversation back into Antigravity's live storage:
          1. `conversations/<uuid>.db`
          2. `annotations/<uuid>.pbtxt`
          3. `brain/<uuid>/` text & artifact files
          4. `conversation_summaries.db` (`conversation_summaries` + `conversations_fts` + `conversation_parents`)
        Never overwrites an existing live `<uuid>.db` unless `force=True` (in which case the existing
        live `.db` is first archived with an incrementing `_vN` suffix under `pre_restore_backups/`).
        """
        cid = self.resolve_conv_id(id_or_prefix)
        sess_dir = self.sessions_dir / cid
        src_gz = sess_dir / "conversation.db.gz"
        if not src_gz.is_file():
            return {"ok": False, "conv_id": cid, "message": f"未找到会话归档文件: {src_gz}"}

        with interprocess_file_lock(self.sync_lock_marker, timeout=15.0):
            with self._connect_index() as conn:
                row = conn.execute("SELECT * FROM vault_conversations WHERE conv_id = ?", (cid,)).fetchone()
                if row is None:
                    return {"ok": False, "conv_id": cid, "message": f"索引中不存在会话: {cid}"}

                dst_db = self.agy_conv_dir / f"{cid}.db"
                archived_prev = ""
                if dst_db.is_file():
                    if not force:
                        return {
                            "ok": False,
                            "conv_id": cid,
                            "title": str(row["title"]),
                            "message": (
                                f"主库中仍存在该对话 ({dst_db.name})，无需覆盖恢复。"
                                f"若确需用保险箱快照回滚，请追加 --force（将自动留底当前版本）。"
                            ),
                        }
                    self.pre_restore_dir.mkdir(parents=True, exist_ok=True)
                    ver = 1
                    bak_db = self.pre_restore_dir / f"{cid}_live_v{ver}.db"
                    while bak_db.exists():
                        ver += 1
                        bak_db = self.pre_restore_dir / f"{cid}_live_v{ver}.db"
                    shutil.copy2(dst_db, bak_db)
                    archived_prev = str(bak_db)

                # 1. Decompress conversation.db.gz -> conversations/<cid>.db atomically
                self.agy_conv_dir.mkdir(parents=True, exist_ok=True)
                tmp_db = dst_db.with_name(f".{dst_db.name}.restore.{os.getpid()}.tmp")
                try:
                    with gzip.open(src_gz, "rb") as fin, open(tmp_db, "wb") as fout:
                        shutil.copyfileobj(fin, fout, length=256 * 1024)
                        fout.flush()
                        try:
                            os.fsync(fout.fileno())
                        except OSError:
                            pass
                    if force:
                        for wal_ext in ("-wal", "-shm"):
                            Path(str(dst_db) + wal_ext).unlink(missing_ok=True)
                    os.replace(tmp_db, dst_db)
                finally:
                    tmp_db.unlink(missing_ok=True)

                # 2. Restore annotation.pbtxt
                src_anno = sess_dir / "annotation.pbtxt"
                dst_anno = self.agy_anno_dir / f"{cid}.pbtxt"
                if src_anno.is_file() and (force or not dst_anno.is_file()):
                    self.agy_anno_dir.mkdir(parents=True, exist_ok=True)
                    atomic_write_text(dst_anno, src_anno.read_text(encoding="utf-8", errors="replace"), encoding="utf-8")
                elif not dst_anno.is_file():
                    self.agy_anno_dir.mkdir(parents=True, exist_ok=True)
                    escaped_t = str(row["title"]).replace("\\", "\\\\").replace('"', '\\"')
                    sec_now = int(time.time())
                    atomic_write_text(
                        dst_anno,
                        f'title:"{escaped_t}"  last_user_view_time:{{seconds:{sec_now}  nanos:0}}',
                        encoding="utf-8",
                    )

                # 3. Restore brain/<cid>/ files
                restored_brain_files = 0
                src_brain = sess_dir / "brain"
                dst_brain = self.agy_brain_dir / cid
                if src_brain.is_dir():
                    for cur_d, _, fnames in os.walk(src_brain):
                        cur_p = Path(cur_d)
                        rel_d = cur_p.relative_to(src_brain)
                        for fn in fnames:
                            sf = cur_p / fn
                            df = dst_brain / rel_d / fn
                            if force or not df.exists():
                                df.parent.mkdir(parents=True, exist_ok=True)
                                shutil.copy2(sf, df)
                                restored_brain_files += 1

                # 4. Re-insert summary index into conversation_summaries.db
                summary_restored = False
                if self.agy_summaries_db.is_file():
                    try:
                        with closing(sqlite3.connect(str(self.agy_summaries_db), timeout=10.0)) as sconn:
                            sconn.execute("PRAGMA busy_timeout=10000;")
                            tbl_cols = [
                                r[1]
                                for r in sconn.execute("PRAGMA table_info(conversation_summaries)").fetchall()
                            ]
                            if tbl_cols:
                                saved_sum: Dict[str, Any] = {}
                                if row["summary_row_json"]:
                                    try:
                                        raw_parsed = json.loads(str(row["summary_row_json"]))
                                        if isinstance(raw_parsed, dict):
                                            saved_sum = {k: _decode_sql_val(v) for k, v in raw_parsed.items()}
                                    except Exception:
                                        saved_sum = {}
                                if not saved_sum:
                                    saved_sum = {
                                        "conversation_id": cid,
                                        "title": str(row["title"]),
                                        "preview": str(row["preview"]),
                                        "step_count": int(row["step_count"] or 0),
                                        "last_modified_time": str(row["last_modified_at"] or _now_iso()),
                                        "workspace_uris": str(row["workspace_uris"] or "[]"),
                                        "status": "done",
                                        "project_id": str(row["project_id"] or ""),
                                        "last_user_input_time": str(row["last_modified_at"] or _now_iso()),
                                    }
                                insert_cols = [c for c in tbl_cols if c in saved_sum]
                                if "conversation_id" in tbl_cols and "conversation_id" not in insert_cols:
                                    saved_sum["conversation_id"] = cid
                                    insert_cols.insert(0, "conversation_id")
                                placeholders = ", ".join("?" for _ in insert_cols)
                                col_sql = ", ".join(f"[{c}]" for c in insert_cols)
                                vals = [saved_sum[c] for c in insert_cols]
                                sconn.execute(
                                    f"INSERT OR REPLACE INTO conversation_summaries ({col_sql}) VALUES ({placeholders})",
                                    vals,
                                )
                                summary_restored = True

                            if row["fts_rows_json"]:
                                try:
                                    fts_list = json.loads(str(row["fts_rows_json"]))
                                    if isinstance(fts_list, list) and fts_list:
                                        sconn.execute(
                                            "DELETE FROM conversations_fts WHERE conversation_id = ?",
                                            (cid,),
                                        )
                                        for fitem in fts_list:
                                            if isinstance(fitem, dict) and fitem:
                                                fcols = list(fitem.keys())
                                                fph = ", ".join("?" for _ in fcols)
                                                fcsql = ", ".join(f"[{c}]" for c in fcols)
                                                fvals = [_decode_sql_val(fitem[c]) for c in fcols]
                                                sconn.execute(
                                                    f"INSERT INTO conversations_fts ({fcsql}) VALUES ({fph})",
                                                    fvals,
                                                )
                                except sqlite3.Error:
                                    pass

                            if row["parent_id"]:
                                try:
                                    sconn.execute(
                                        "INSERT OR REPLACE INTO conversation_parents (conversation_id, parent_id) VALUES (?, ?)",
                                        (cid, str(row["parent_id"])),
                                    )
                                except sqlite3.Error:
                                    pass
                            sconn.commit()
                    except sqlite3.Error:
                        summary_restored = False

                now_s = _now_iso()
                new_db_sig = self._compute_db_sig(cid)
                new_anno_sig = self._compute_anno_sig(cid)
                new_brain_sig, _ = self._scan_brain_tree(cid)
                conn.execute(
                    """
                    UPDATE vault_conversations
                    SET live_status = 'active',
                        restored_at = ?,
                        db_sig = ?,
                        anno_sig = ?,
                        brain_sig = ?
                    WHERE conv_id = ?
                    """,
                    (now_s, new_db_sig, new_anno_sig, new_brain_sig, cid),
                )

        return {
            "ok": True,
            "conv_id": cid,
            "title": str(row["title"]),
            "restored_db": str(dst_db),
            "restored_brain_files": restored_brain_files,
            "summary_index_restored": summary_restored,
            "archived_previous_live_db": archived_prev,
            "message": (
                f"♻️ 已成功恢复对话「{row['title']}」({cid[:8]})！"
                f"（数据库 + {restored_brain_files} 个上下文文件 + 历史列表索引已回写；"
                f"若侧边栏未立即刷新，重新打开历史面板或重启客户端即可看到）"
            ),
        }

    def export_markdown(self, id_or_prefix: str, out_dir: Optional[Path] = None) -> Dict[str, Any]:
        """
        Export a backed-up conversation (active or deleted) to a standalone readable Markdown file.
        Uses incrementing `_v1.md`, `_v2.md` filenames so existing exports are never overwritten.
        """
        cid = self.resolve_conv_id(id_or_prefix)
        with self._connect_index() as conn:
            row = conn.execute("SELECT * FROM vault_conversations WHERE conv_id = ?", (cid,)).fetchone()
        if row is None:
            return {"ok": False, "conv_id": cid, "message": f"未找到会话: {cid}"}

        sess_dir = self.sessions_dir / cid
        logs_dir = sess_dir / "brain" / ".system_generated" / "logs"
        t_compact = logs_dir / "transcript.jsonl"
        t_full = logs_dir / "transcript_full.jsonl"

        full_lines: Optional[List[str]] = None
        lines_out: List[str] = [
            f"# {row['title']}",
            "",
            f"- **Conversation ID**: `{cid}`",
            f"- **主库状态**: `{'已删除 (由 Orion-Vault 保护留档)' if row['live_status'] == 'deleted' else '在线活跃'}`",
            f"- **主模型**: `{row['model_name'] or '默认'}`",
            f"- **总步数**: `{row['step_count']}`",
            f"- **最后活跃**: `{_fmt_cn_time(row['last_modified_at'])}`",
            f"- **备份时间**: `{_fmt_cn_time(row['last_backed_up_at'])}`",
            "",
            "---",
            "",
        ]

        turn_count = 0
        if t_compact.is_file():
            raw_compact = t_compact.read_text(encoding="utf-8", errors="replace").splitlines()
            for idx, raw_ln in enumerate(raw_compact):
                if not raw_ln.strip():
                    continue
                try:
                    obj = json.loads(raw_ln)
                except json.JSONDecodeError:
                    continue
                if obj.get("truncated_fields") and "content" in (obj.get("truncated_fields") or []) and t_full.is_file():
                    if full_lines is None:
                        try:
                            full_lines = t_full.read_text(encoding="utf-8", errors="replace").splitlines()
                        except OSError:
                            full_lines = []
                    if full_lines and idx < len(full_lines):
                        try:
                            obj = json.loads(full_lines[idx])
                        except json.JSONDecodeError:
                            pass

                stype = str(obj.get("type") or "")
                ts_str = _fmt_cn_time(obj.get("created_at"))
                if stype == "USER_INPUT":
                    prompt_txt = _clean_user_prompt(str(obj.get("content") or ""))
                    if prompt_txt:
                        turn_count += 1
                        lines_out.append(f"## 🙋 用户 ({ts_str})")
                        lines_out.append("")
                        lines_out.append(prompt_txt)
                        lines_out.append("")
                elif stype == "PLANNER_RESPONSE":
                    reply_txt = str(obj.get("content") or "").strip()
                    tcalls = obj.get("tool_calls") or []
                    if reply_txt or tcalls:
                        lines_out.append(f"## 🤖 Antigravity ({ts_str})")
                        lines_out.append("")
                        if reply_txt:
                            lines_out.append(reply_txt)
                            lines_out.append("")
                        if tcalls and isinstance(tcalls, list):
                            tool_names = [
                                str(tc.get("name") or tc.get("tool_name") or "tool")
                                for tc in tcalls
                                if isinstance(tc, dict)
                            ]
                            if tool_names:
                                lines_out.append(f"> 🛠️ 调用工具: `{', '.join(tool_names)}`")
                                lines_out.append("")
        else:
            lines_out.append("> *(该会话尚未生成 transcript.jsonl 文本日志，但底层 SQLite `conversation.db.gz` 已完整备份，可通过 `vault restore` 恢复至客户端查看。)*")
            lines_out.append("")

        target_dir = Path(out_dir) if out_dir is not None else self.exports_dir
        target_dir.mkdir(parents=True, exist_ok=True)
        safe_title = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", str(row["title"])).strip(" ._")[:40] or "chat"
        date_prefix = _fmt_cn_time(row["last_modified_at"])[:10].replace("-", "")
        if not date_prefix.isdigit():
            date_prefix = datetime.now().strftime("%Y%m%d")

        ver = 1
        out_file = target_dir / f"{date_prefix}_{safe_title}_{cid[:8]}_v{ver}.md"
        while out_file.exists():
            ver += 1
            out_file = target_dir / f"{date_prefix}_{safe_title}_{cid[:8]}_v{ver}.md"

        atomic_write_text(out_file, "\n".join(lines_out) + "\n", encoding="utf-8")
        return {
            "ok": True,
            "conv_id": cid,
            "title": str(row["title"]),
            "export_path": str(out_file),
            "turns_exported": turn_count,
            "message": f"📄 已导出对话 Markdown 归档: {out_file}",
        }


def run_stop_hook_from_stdin(raw_stdin: Optional[str] = None) -> str:
    """
    Entrypoint for Antigravity `Stop` lifecycle hook.
    - Immediately short-circuits when `vault_auto_backup` switch is OFF.
    - Never blocks or raises: always returns valid `{"decision": "stop"}` JSON.
    """
    try:
        if not is_vault_auto_backup_enabled():
            return '{"decision": "stop"}'
        if raw_stdin is None:
            import sys

            raw_bytes = sys.stdin.buffer.read()
            raw_stdin = raw_bytes.decode("utf-8-sig", errors="replace") if raw_bytes else ""
        payload = json.loads(raw_stdin) if raw_stdin and raw_stdin.strip() else {}
        if isinstance(payload, dict):
            conv_id = str(payload.get("conversationId") or "").strip()
            if conv_id and UUID_RE.match(conv_id):
                OrionVault().sync_conversation(conv_id, respect_switch=True)
    except Exception:
        pass
    return '{"decision": "stop"}'
