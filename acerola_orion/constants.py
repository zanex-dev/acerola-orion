# -*- coding: utf-8 -*-
"""
全局常量、本地路径约定与模型费率目录 (Model Rate-Card & Quota Pool Catalog)
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import contextlib
import itertools
import json
import os
from pathlib import Path
import re
import threading
import time
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

APP_NAME = "acerola-orion"
APP_CODENAME = "Acerola-Orion (雅赛劳拉莉昂)"
APP_TRINITY = "Kiss-Shot · Acerola-Orion · Heart-Under-Blade"
APP_VERSION = "1.4.0"

_ATOMIC_TMP_SEQ = itertools.count(1)


def atomic_write_text(path: Path | str, content: str, encoding: str = "utf-8") -> None:
    """
    Atomically write text to `path` via a unique sibling temporary file + `os.replace`,
    with bounded retry for transient Win32 sharing/rename violations.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tid = threading.get_ident()
    seq = next(_ATOMIC_TMP_SEQ)
    tmp = target.with_name(f".{target.name}.tmp.{os.getpid()}.{tid}.{seq}")
    try:
        with open(tmp, "w", encoding=encoding, newline="") as f:
            f.write(content)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass

        last_err: Optional[OSError] = None
        for attempt in range(40):
            try:
                os.replace(tmp, target)
                last_err = None
                break
            except PermissionError as e:
                last_err = e
                time.sleep(min(0.02, 0.0015 * (attempt + 1)))
            except OSError as e:
                if getattr(e, "winerror", 0) in (5, 32):
                    last_err = e
                    time.sleep(min(0.02, 0.0015 * (attempt + 1)))
                else:
                    raise
        if last_err is not None:
            raise last_err
    finally:
        if tmp.exists():
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass


_INPROC_FILE_LOCKS: Dict[str, threading.RLock] = {}
_INPROC_FILE_LOCKS_GUARD = threading.Lock()


class FileLockTimeout(TimeoutError):
    """Raised when a cross-process file lock cannot be acquired within the bounded deadline."""


@contextlib.contextmanager
def interprocess_file_lock(path: Path | str, timeout: float = 5.0) -> Iterator[bool]:
    """
    Cross-thread + cross-process exclusive lock guarding a read-modify-write cycle on `path`.
    Uses a persistent sibling `.<name>.lock` file locked via msvcrt (Windows) / fcntl (POSIX).
    """
    target = Path(path)
    key = os.path.normcase(os.path.abspath(str(target)))
    with _INPROC_FILE_LOCKS_GUARD:
        rlock = _INPROC_FILE_LOCKS.setdefault(key, threading.RLock())
    deadline = time.monotonic() + max(0.0, timeout)
    if not rlock.acquire(timeout=max(0.0, timeout)):
        raise FileLockTimeout(f"文件锁等待超时（进程内）：{target}")
    fh = None
    acquired = False
    try:
        lock_path = target.with_name(f".{target.name}.lock")
        target.parent.mkdir(parents=True, exist_ok=True)
        fh = open(lock_path, "a+b")
        while True:
            try:
                if os.name == "nt":
                    import msvcrt

                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except OSError:
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.02)
        if not acquired:
            raise FileLockTimeout(f"文件锁被其他进程占用超过 {timeout:.1f}s，已拒绝无锁写入：{target}")
        yield True
    finally:
        if fh is not None:
            try:
                if acquired:
                    if os.name == "nt":
                        import msvcrt

                        fh.seek(0)
                        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            fh.close()
        rlock.release()


GUILLOTINE_MUTEX_NAME = r"Local\AcerolaOrionGuillotineMutex_v1"
SCHEDULED_TASK_NAME = "AcerolaOrionGuard"
LEGACY_TASK_NAME = "AntigravityOrphanKiller"


def get_data_dir() -> Path:
    override = os.environ.get("ACEROLA_ORION_HOME")
    if override:
        p = Path(override)
    else:
        p = Path.home() / ".gemini" / "acerola_orion"
    p.mkdir(parents=True, exist_ok=True)
    return p


def get_config_path() -> Path:
    return get_data_dir() / "config.json"


def get_ledger_db_path() -> Path:
    return get_data_dir() / "ledger.db"


def get_guillotine_log_path() -> Path:
    return get_data_dir() / "guillotine.log"


def get_vault_dir() -> Path:
    p = get_data_dir() / "chat_vault"
    p.mkdir(parents=True, exist_ok=True)
    return p


def get_vault_index_db_path() -> Path:
    return get_vault_dir() / "vault_index.db"


def get_agy_home() -> Path:
    return Path.home() / ".gemini" / "antigravity"


def get_agy_daemon_dir() -> Path:
    return get_agy_home() / "daemon"


def get_agy_conversations_dir() -> Path:
    return get_agy_home() / "conversations"


def get_agy_summaries_db_path() -> Path:
    return get_agy_home() / "conversation_summaries.db"


def get_agy_brain_dir() -> Path:
    return get_agy_home() / "brain"


def get_agy_annotations_dir() -> Path:
    return get_agy_home() / "annotations"


def get_agy_roaming_dir() -> Path:
    appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return Path(appdata) / "Antigravity"


def get_ls_log_path() -> Path:
    return get_agy_roaming_dir() / "logs" / "language_server.log"


def get_main_log_path() -> Path:
    return get_agy_roaming_dir() / "logs" / "main.log"


def get_user_settings_path() -> Path:
    return get_agy_roaming_dir() / "User" / "settings.json"


def get_launchers_dir() -> Path:
    return Path.home() / "workspaces" / "launchers"


@dataclass(frozen=True)
class ModelRateSpec:
    family_id: str
    family_label: str
    quota_group: str  # "gemini" or "3p"
    # Official 1.0x API list price ($ per 1,000,000 tokens)
    input_per_m: float
    cache_per_m: float
    output_per_m: float
    # Promised promotional multiplier vs Official 1.0x (e.g., 0.5x promotional rate on 4A/4C)
    promised_multiplier: float = 1.0
    # User-visible preset estimate only; this is not a provider billing record.
    suspected_agy_multiplier: float = 1.0

    def compute_usd(
        self,
        input_tokens: int,
        cache_read_tokens: int,
        output_tokens: int,
        multiplier: float = 1.0,
    ) -> float:
        base = (
            (input_tokens / 1_000_000.0) * self.input_per_m
            + (cache_read_tokens / 1_000_000.0) * self.cache_per_m
            + (output_tokens / 1_000_000.0) * self.output_per_m
        )
        return base * multiplier


MODEL_CATALOG: Dict[str, ModelRateSpec] = {
    "4c": ModelRateSpec(
        family_id="4c",
        family_label="Gemini 4 Carbon (4C)",
        quota_group="gemini",
        input_per_m=4.00,
        cache_per_m=0.20,
        output_per_m=20.00,
        promised_multiplier=0.5,
        suspected_agy_multiplier=1.5,
    ),
    "4n": ModelRateSpec(
        family_id="4n",
        family_label="Gemini 4 Neon (4N)",
        quota_group="gemini",
        input_per_m=3.00,
        cache_per_m=0.15,
        output_per_m=15.00,
        promised_multiplier=1.0,
        suspected_agy_multiplier=1.0,
    ),
    "4a": ModelRateSpec(
        family_id="4a",
        family_label="Gemini 4 Argon (4A)",
        quota_group="gemini",
        input_per_m=4.00,
        cache_per_m=0.20,
        output_per_m=20.00,
        promised_multiplier=0.5,
        suspected_agy_multiplier=1.5,
    ),
    "4h": ModelRateSpec(
        family_id="4h",
        family_label="Gemini 4 Hydrogen (4H)",
        quota_group="gemini",
        input_per_m=0.50,
        cache_per_m=0.025,
        output_per_m=3.00,
        promised_multiplier=1.0,
        suspected_agy_multiplier=1.0,
    ),
    "flash": ModelRateSpec(
        family_id="flash",
        family_label="Gemini 3.8 Flash",
        quota_group="gemini",
        input_per_m=0.30,
        cache_per_m=0.015,
        output_per_m=2.50,
        promised_multiplier=1.0,
        suspected_agy_multiplier=1.0,
    ),
    "claude-opus": ModelRateSpec(
        family_id="claude-opus",
        family_label="Claude Opus 5.5",
        quota_group="3p",
        input_per_m=5.00,
        cache_per_m=0.50,
        output_per_m=25.00,
        promised_multiplier=1.0,
        suspected_agy_multiplier=1.0,
    ),
    "claude-sonnet": ModelRateSpec(
        family_id="claude-sonnet",
        family_label="Claude Sonnet 5.5",
        quota_group="3p",
        input_per_m=3.00,
        cache_per_m=0.30,
        output_per_m=15.00,
        promised_multiplier=1.0,
        suspected_agy_multiplier=1.0,
    ),
    "gpt-oss": ModelRateSpec(
        family_id="gpt-oss",
        family_label="GPT-OSS 120B",
        quota_group="3p",
        input_per_m=0.60,
        cache_per_m=0.06,
        output_per_m=2.40,
        promised_multiplier=1.0,
        suspected_agy_multiplier=1.0,
    ),
    "gemini-other": ModelRateSpec(
        family_id="gemini-other",
        family_label="Gemini (Default/Other)",
        quota_group="gemini",
        input_per_m=2.50,
        cache_per_m=0.125,
        output_per_m=12.50,
        promised_multiplier=1.0,
        suspected_agy_multiplier=1.0,
    ),
}


def classify_model(raw_model: str | None) -> ModelRateSpec:
    if not raw_model:
        return MODEL_CATALOG["gemini-other"]
    s = raw_model.strip().lower()
    if "carbon" in s or s.startswith("4c"):
        return MODEL_CATALOG["4c"]
    if "neon" in s or s.startswith("4n"):
        return MODEL_CATALOG["4n"]
    if "argon" in s or s.startswith("4a"):
        return MODEL_CATALOG["4a"]
    if "hydrogen" in s or s.startswith("4h"):
        return MODEL_CATALOG["4h"]
    if "flash" in s:
        return MODEL_CATALOG["flash"]
    if "opus" in s:
        return MODEL_CATALOG["claude-opus"]
    if "sonnet" in s or "claude" in s:
        return MODEL_CATALOG["claude-sonnet"]
    if "gpt" in s:
        return MODEL_CATALOG["gpt-oss"]
    return MODEL_CATALOG["gemini-other"]


_CFG_TRAILING_COMMA_RE = re.compile(r",(\s*[}\]])")


def _salvage_json_dict(text: str) -> Optional[Dict[str, Any]]:
    try:
        obj = json.loads(_CFG_TRAILING_COMMA_RE.sub(r"\1", text))
    except Exception:
        return None
    return obj if isinstance(obj, dict) else None


def _read_config_file_state(path: Path) -> Tuple[Optional[Dict[str, Any]], Optional[bytes]]:
    raw: Optional[bytes] = None
    for attempt in range(20):
        if not path.exists():
            return {}, None
        try:
            raw = path.read_bytes()
            break
        except PermissionError:
            time.sleep(min(0.01, 0.001 * (attempt + 1)))
        except OSError as e:
            if getattr(e, "winerror", 0) in (5, 32):
                time.sleep(min(0.01, 0.001 * (attempt + 1)))
            else:
                raise
    if raw is None:
        raise OSError(f"config.json 持续不可读，拒绝以默认值覆盖：{path}")
    text = raw.decode("utf-8-sig", errors="replace")
    if not text.strip():
        return {}, None
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj, None
    except Exception:
        pass
    return _salvage_json_dict(text), raw


def load_vault_config() -> Dict[str, Any]:
    """Read `config.json` safely and return normalized vault configuration keys."""
    p = get_config_path()
    try:
        obj, _corrupt = _read_config_file_state(p)
    except OSError:
        obj = None
    cfg: Dict[str, Any] = dict(obj) if isinstance(obj, dict) else {}
    if not isinstance(cfg.get("vault_auto_backup"), bool):
        cfg["vault_auto_backup"] = bool(cfg.get("vault_auto_backup", True))
    return cfg


def _update_vault_config(mutator: Callable[[Dict[str, Any]], None]) -> Dict[str, Any]:
    p = get_config_path()
    with interprocess_file_lock(p):
        obj, corrupt_raw = _read_config_file_state(p)
        if corrupt_raw is not None:
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            backup = p.with_name(f"{p.name}.corrupt-{stamp}.bak")
            n = 1
            while backup.exists():
                backup = p.with_name(f"{p.name}.corrupt-{stamp}-{n}.bak")
                n += 1
            backup.write_bytes(corrupt_raw)
        cfg: Dict[str, Any] = dict(obj) if isinstance(obj, dict) else {}
        if not isinstance(cfg.get("vault_auto_backup"), bool):
            cfg["vault_auto_backup"] = bool(cfg.get("vault_auto_backup", True))
        mutator(cfg)
        atomic_write_text(p, json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        return cfg


def is_vault_auto_backup_enabled() -> bool:
    """Return whether automatic conversation backup (Orion-Vault) is enabled in config.json."""
    return bool(load_vault_config().get("vault_auto_backup", True))


def configure_vault_auto_backup(action: str = "status") -> Dict[str, Any]:
    """
    Controllable master switch for Orion-Vault automatic conversation backup.
    Supported actions: 'status' | 'on' | 'enable' | 'off' | 'disable' | 'toggle'.
    Takes effect immediately across Stop hooks and background Guillotine sweeps.
    """
    act = str(action or "status").strip().lower()
    if act == "status":
        cur = is_vault_auto_backup_enabled()
        state_lbl = "已开启 (ON)" if cur else "已关闭 (OFF)"
        return {
            "ok": True,
            "enabled": cur,
            "changed": False,
            "message": f"对话自动备份开关当前状态: {state_lbl}",
        }
    if act not in ("on", "enable", "off", "disable", "toggle"):
        return {
            "ok": False,
            "enabled": is_vault_auto_backup_enabled(),
            "changed": False,
            "message": f"未知开关动作: {action!r} (可选: status | on | off | toggle)",
        }

    prev_holder: List[bool] = [True]

    def _mutate(c: Dict[str, Any]) -> None:
        prev = bool(c.get("vault_auto_backup", True))
        prev_holder[0] = prev
        if act in ("on", "enable"):
            c["vault_auto_backup"] = True
        elif act in ("off", "disable"):
            c["vault_auto_backup"] = False
        else:
            c["vault_auto_backup"] = not prev

    updated = _update_vault_config(_mutate)
    now_enabled = bool(updated.get("vault_auto_backup", True))
    changed = now_enabled != prev_holder[0]
    if now_enabled:
        msg = "🟢 已开启对话自动备份 (Orion-Vault)：每轮对话结束与后台巡检将自动增量留档，误删可一键恢复。"
    else:
        msg = "⚪ 已关闭对话自动备份 (Orion-Vault)：实时 Hook 与后台巡检已暂停自动归档（已有历史备份完整保留）。"
    return {
        "ok": True,
        "enabled": now_enabled,
        "changed": changed,
        "message": msg,
    }
