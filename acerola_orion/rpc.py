# -*- coding: utf-8 -*-
"""
Orion-RPC 底层通信与无锁系统探针 (acerola_orion/rpc.py)
- 原生 64-bit 安全 Win32 CreateToolhelp32Snapshot 进程快照与祖先链死锁免疫推导
- Win32 CreateFileW (FILE_SHARE_READ | WRITE | DELETE) 穿透读取被 Electron 锁定的 0 字节日志
- daemon/hub_*.json 存活 PID 校验端口发现与本地 Connect-RPC JSON 客户端
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass, field
import http.client
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import threading
import time
from typing import Any, Dict, List, Optional, Set, Tuple

from .constants import get_agy_daemon_dir

# Win32 Constants
TH32CS_SNAPPROCESS = 0x00000002
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
GENERIC_READ = 0x80000000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
FILE_SHARE_DELETE = 0x00000004
OPEN_EXISTING = 3
FILE_ATTRIBUTE_NORMAL = 0x80
FILE_BEGIN = 0
FILE_END = 2
SYNCHRONIZE = 0x00100000
PROCESS_TERMINATE = 0x0001
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
ERROR_ALREADY_EXISTS = 183
WAIT_OBJECT_0 = 0x00000000
WAIT_TIMEOUT = 0x00000102
WAIT_FAILED = 0xFFFFFFFF
_TAIL_LOOKBEHIND_BYTES = 4096


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * 260),
    ]


class FILETIME(ctypes.Structure):
    _fields_ = [
        ("dwLowDateTime", wintypes.DWORD),
        ("dwHighDateTime", wintypes.DWORD),
    ]


class BY_HANDLE_FILE_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("dwFileAttributes", wintypes.DWORD),
        ("ftCreationTime", FILETIME),
        ("ftLastAccessTime", FILETIME),
        ("ftLastWriteTime", FILETIME),
        ("dwVolumeSerialNumber", wintypes.DWORD),
        ("nFileSizeHigh", wintypes.DWORD),
        ("nFileSizeLow", wintypes.DWORD),
        ("nNumberOfLinks", wintypes.DWORD),
        ("nFileIndexHigh", wintypes.DWORD),
        ("nFileIndexLow", wintypes.DWORD),
    ]


def _init_win32_kernel() -> Any:
    """Bind 64-bit safe Win32 kernel32 prototypes with use_last_error=True."""
    if sys.platform != "win32":
        return None
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)

    k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE

    k32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    k32.Process32FirstW.restype = wintypes.BOOL

    k32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    k32.Process32NextW.restype = wintypes.BOOL

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

    k32.GetFileInformationByHandle.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(BY_HANDLE_FILE_INFORMATION),
    ]
    k32.GetFileInformationByHandle.restype = wintypes.BOOL

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

    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.OpenProcess.restype = wintypes.HANDLE

    k32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    k32.QueryFullProcessImageNameW.restype = wintypes.BOOL

    k32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    k32.TerminateProcess.restype = wintypes.BOOL

    k32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
    k32.CreateMutexW.restype = wintypes.HANDLE

    k32.OpenMutexW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    k32.OpenMutexW.restype = wintypes.HANDLE

    k32.ReleaseMutex.argtypes = [wintypes.HANDLE]
    k32.ReleaseMutex.restype = wintypes.BOOL

    k32.WaitForMultipleObjects.argtypes = [
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
        wintypes.BOOL,
        wintypes.DWORD,
    ]
    k32.WaitForMultipleObjects.restype = wintypes.DWORD

    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    k32.CloseHandle.restype = wintypes.BOOL
    return k32


_KERNEL32 = _init_win32_kernel()
_CMDLINE_CACHE: Dict[int, str] = {}
_CMDLINE_LOCK = threading.Lock()
_CMDLINE_CACHE_MAX = 64
_FLAG_PATTERN_CACHE: Dict[str, re.Pattern[str]] = {}


def _is_valid_handle(h: Any) -> bool:
    return bool(h) and h != INVALID_HANDLE_VALUE


def close_handle_safe(h: Any) -> None:
    if _KERNEL32 is not None and _is_valid_handle(h):
        try:
            _KERNEL32.CloseHandle(h)
        except Exception:
            pass


def open_process_handle(pid: int, access: int = SYNCHRONIZE) -> Optional[int]:
    if _KERNEL32 is None or pid <= 0:
        return None
    h = _KERNEL32.OpenProcess(int(access), False, int(pid))
    if not _is_valid_handle(h):
        return None
    return int(h)


def process_exists_win32(pid: int) -> Optional[bool]:
    """
    Kernel-level liveness probe that distinguishes *nonexistent* from *inaccessible* PIDs.
    Returns True (alive, even if access denied), False (kernel reports no such PID:
    ERROR_INVALID_PARAMETER), or None (undetermined / non-Windows).
    """
    if _KERNEL32 is None or pid <= 0:
        return None
    ctypes.set_last_error(0)
    h = _KERNEL32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if _is_valid_handle(h):
        close_handle_safe(h)
        return True
    err = ctypes.get_last_error()
    if err == 87:  # ERROR_INVALID_PARAMETER -> PID does not exist
        return False
    if err == 5:  # ERROR_ACCESS_DENIED -> exists but protected / higher integrity
        return True
    return None


def query_process_image_path_win32(pid: int) -> str:
    """Query full executable image path of `pid` via OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION)."""
    if _KERNEL32 is None or pid <= 0:
        return ""
    h = open_process_handle(pid, PROCESS_QUERY_LIMITED_INFORMATION)
    if h is None:
        return ""
    try:
        buf_len = wintypes.DWORD(1024)
        buf = ctypes.create_unicode_buffer(1024)
        if _KERNEL32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(buf_len)):
            return str(buf.value or "")
        return ""
    finally:
        close_handle_safe(h)


def _handle_image(handle: int) -> Optional[Path]:
    if _KERNEL32 is None or not handle:
        return None
    size = wintypes.DWORD(32768)
    buf = ctypes.create_unicode_buffer(size.value)
    if _KERNEL32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
        return Path(buf.value)
    return None


def _same_user(handle: int) -> bool:
    """Compare process-token user SIDs. Unavailable token information fails closed."""
    if sys.platform != "win32" or _KERNEL32 is None or not handle:
        return False
    adv = ctypes.WinDLL("advapi32", use_last_error=True)
    adv.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    adv.OpenProcessToken.restype = wintypes.BOOL
    adv.GetTokenInformation.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    adv.GetTokenInformation.restype = wintypes.BOOL
    adv.EqualSid.argtypes = [wintypes.LPVOID, wintypes.LPVOID]
    adv.EqualSid.restype = wintypes.BOOL
    buffers = []
    current_process = wintypes.HANDLE(-1)
    for process in (current_process, handle):
        token = wintypes.HANDLE()
        if not adv.OpenProcessToken(process, 0x0008, ctypes.byref(token)):  # TOKEN_QUERY
            return False
        try:
            length = wintypes.DWORD()
            adv.GetTokenInformation(token, 1, None, 0, ctypes.byref(length))
            if not length.value:
                return False
            buf = ctypes.create_string_buffer(length.value)
            if not adv.GetTokenInformation(token, 1, buf, length, ctypes.byref(length)):
                return False
            buffers.append(buf)
        finally:
            close_handle_safe(token.value)
    return bool(adv.EqualSid(*(ctypes.c_void_p.from_buffer(b).value for b in buffers)))


def open_owned_language_server(pid: int, entries: Optional[List[RawProcEntry]] = None) -> Optional[int]:
    """Pin a live LS object while its UI ancestry, install path, user and own hub can be verified."""
    handle = open_process_handle(pid, PROCESS_TERMINATE | PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE)
    if not handle:
        return None
    owned = False
    try:
        image = _handle_image(handle)
        if not image or not image.name.lower().startswith("language_server") or image.suffix.lower() != ".exe":
            return None
        if not _same_user(handle):
            return None
        hub = discover_hub_endpoint(pid)
        if hub is None or hub.pid != pid or not hub.csrf_token:
            return None
        procs = entries if entries is not None else snapshot_processes_fast()
        roots = set(find_antigravity_process_tree(procs)["main_pids"])
        parents = {p.pid: p.ppid for p in procs}
        seen = {pid}
        ancestor = parents.get(pid)
        while ancestor and ancestor not in seen:
            if ancestor in roots:
                root_path = query_process_image_path_win32(ancestor)
                if root_path:
                    root = Path(root_path).resolve()
                    if root.name.lower() == "antigravity.exe" and image.resolve().is_relative_to(root.parent):
                        owned = True
                        return handle
                break
            seen.add(ancestor)
            ancestor = parents.get(ancestor)
        return None
    except (OSError, ValueError):
        return None
    finally:
        if not owned:
            close_handle_safe(handle)


def terminate_owned_handle(handle: int) -> bool:
    return bool(_KERNEL32 and handle and _KERNEL32.TerminateProcess(handle, 1))


def terminate_pid_win32(
    pid: int,
    exit_code: int = 1,
    allowed_exe_prefixes: Optional[Tuple[str, ...]] = None,
) -> bool:
    """
    Terminate process `pid` safely on Windows.
    When `allowed_exe_prefixes` is provided, opens a single handle with
    `PROCESS_TERMINATE | PROCESS_QUERY_LIMITED_INFORMATION` so `QueryFullProcessImageNameW`
    and `TerminateProcess` execute on the exact same kernel process object (eliminating PID-reuse TOCTOU).
    """
    if _KERNEL32 is None or pid <= 0 or pid == os.getpid():
        return False
    access = PROCESS_TERMINATE
    if allowed_exe_prefixes:
        access |= PROCESS_QUERY_LIMITED_INFORMATION
    h = open_process_handle(pid, access)
    if h is None:
        return False
    try:
        if allowed_exe_prefixes:
            buf_len = wintypes.DWORD(1024)
            buf = ctypes.create_unicode_buffer(1024)
            if not _KERNEL32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(buf_len)):
                return False
            exe_name = os.path.basename(str(buf.value or "")).lower()
            prefixes = tuple(p.lower() for p in allowed_exe_prefixes if p)
            if not exe_name or not exe_name.startswith(prefixes):
                return False
        return bool(_KERNEL32.TerminateProcess(h, int(exit_code)))
    finally:
        close_handle_safe(h)


def is_mutex_held(name: str) -> bool:
    if _KERNEL32 is None or not name:
        return False
    h = _KERNEL32.OpenMutexW(SYNCHRONIZE, False, name)
    if _is_valid_handle(h):
        close_handle_safe(h)
        return True
    return False


def acquire_single_instance_mutex(name: str) -> Tuple[Optional[int], bool]:
    """
    Attempt to create and own a named Win32 mutex.
    Returns (handle, already_exists). If already_exists is True, handle is closed and None is returned.
    """
    if _KERNEL32 is None or not name:
        return None, False
    ctypes.set_last_error(0)
    h = _KERNEL32.CreateMutexW(None, True, name)
    err = ctypes.get_last_error()
    if not _is_valid_handle(h):
        return None, False
    if err == ERROR_ALREADY_EXISTS:
        close_handle_safe(h)
        return None, True
    return int(h), False


def release_and_close_mutex(h: Optional[int]) -> None:
    if _KERNEL32 is not None and _is_valid_handle(h):
        try:
            _KERNEL32.ReleaseMutex(h)
        except Exception:
            pass
        close_handle_safe(h)


def wait_for_handles_all(handles: List[int], timeout_ms: int) -> int:
    if _KERNEL32 is None or not handles:
        return WAIT_FAILED
    capped = handles[:64]
    arr_type = wintypes.HANDLE * len(capped)
    h_arr = arr_type(*capped)
    return int(_KERNEL32.WaitForMultipleObjects(len(capped), h_arr, True, int(timeout_ms)))


@dataclass
class RawProcEntry:
    pid: int
    ppid: int
    name: str


@dataclass
class HubEndpoint:
    file_path: str
    pid: int
    http_port: int
    https_port: int
    csrf_token: str
    total_hub_files: int = 0


@dataclass
class ActiveLsState:
    running: bool = False
    pid: int = 0
    parent_pid: int = 0
    name: str = ""
    command_line: str = ""
    ide_version: str = ""
    main_pids: List[int] = field(default_factory=list)
    all_ls_pids: List[int] = field(default_factory=list)
    total_hub_files: int = 0
    hub: Optional[HubEndpoint] = None


def snapshot_processes_fast() -> List[RawProcEntry]:
    """Native Win32 process table enumeration with zero subprocess spawns."""
    if _KERNEL32 is None:
        return []
    h_snap = _KERNEL32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not _is_valid_handle(h_snap):
        return []
    entries: List[RawProcEntry] = []
    try:
        pe = PROCESSENTRY32W()
        pe.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        if _KERNEL32.Process32FirstW(h_snap, ctypes.byref(pe)):
            while True:
                entries.append(
                    RawProcEntry(
                        pid=int(pe.th32ProcessID),
                        ppid=int(pe.th32ParentProcessID),
                        name=str(pe.szExeFile),
                    )
                )
                if not _KERNEL32.Process32NextW(h_snap, ctypes.byref(pe)):
                    break
    finally:
        close_handle_safe(h_snap)
    return entries


def find_antigravity_process_tree(entries: Optional[List[RawProcEntry]] = None) -> Dict[str, List[int]]:
    """
    Identify true Electron UI root Main PIDs, language_server PIDs, and subordinate LS worker PIDs.
    Critical anti-deadlock invariant:
      `language_server*.exe` often spawns `cmd.exe -> Antigravity.exe` (headless/node worker).
      Any process whose ancestor chain passes through `language_server*.exe` is a subordinate worker
      (`ls_worker_pids` / `ls_descendant_pids`), NEVER a root UI `main_pid`!
    """
    procs = entries if entries is not None else snapshot_processes_fast()
    proc_by_pid: Dict[int, RawProcEntry] = {p.pid: p for p in procs if p.pid > 0}
    agy_map: Dict[int, RawProcEntry] = {}
    ls_entries: List[RawProcEntry] = []

    for p in procs:
        if p.pid <= 0:
            continue
        low = p.name.lower()
        if low == "antigravity.exe":
            agy_map[p.pid] = p
        elif low.startswith("language_server") and low.endswith(".exe"):
            ls_entries.append(p)

    ls_set: Set[int] = {ls.pid for ls in ls_entries}

    def _is_self_ancestor(pid: int) -> bool:
        """True when `pid` appears in its own ppid chain (only possible via PID reuse)."""
        visited: Set[int] = {pid}
        cur = proc_by_pid.get(pid)
        depth = 0
        while cur is not None and depth < 48:
            ppid = cur.ppid
            if ppid <= 0:
                return False
            if ppid == pid:
                return True
            if ppid in visited:
                return False
            visited.add(ppid)
            cur = proc_by_pid.get(ppid)
            depth += 1
        return False

    # PIDs whose ppid links form a cycle are untrustworthy (stale parent PID reused by a descendant).
    cyclic_pids: Set[int] = {pid for pid in proc_by_pid if _is_self_ancestor(pid)}

    def _has_ancestor_in(pid: int, target_set: Set[int], stop_at_cycles: bool = False) -> bool:
        if not target_set:
            return False
        visited: Set[int] = {pid}
        cur = proc_by_pid.get(pid)
        depth = 0
        while cur is not None and depth < 48:
            if stop_at_cycles and cur.pid in cyclic_pids:
                # Never follow a parent link out of a PID-reuse cycle: e.g. UI Antigravity.exe(100)
                # whose dead launcher PID got reused by its own language_server(200) child would
                # otherwise be demoted to an "LS worker" and guillotined together with the LS.
                break
            ppid = cur.ppid
            if ppid <= 0 or ppid in visited:
                break
            if ppid in target_set:
                return True
            visited.add(ppid)
            cur = proc_by_pid.get(ppid)
            depth += 1
        return False

    ls_descendant_pids: Set[int] = {
        p.pid
        for p in procs
        if p.pid > 0 and p.pid not in ls_set and _has_ancestor_in(p.pid, ls_set, stop_at_cycles=True)
    }
    ls_worker_pids: Set[int] = {pid for pid in agy_map if pid in ls_descendant_pids}
    ui_agy_pids: Set[int] = {pid for pid in agy_map if pid not in ls_worker_pids}

    # A true Electron UI Root Main process is a UI Antigravity.exe with NO ancestor in ui_agy_pids
    root_main_pids: Set[int] = {
        pid for pid in ui_agy_pids if not _has_ancestor_in(pid, ui_agy_pids)
    }

    # Cycle-resilience invariant: if PID reuse creates a synthetic ppid cycle among
    # live UI Antigravity.exe processes (e.g. 100 <-> 200), recover each cycle's canonical representative
    # so guillotine NEVER misidentifies a live UI session as headless/closed.
    if ui_agy_pids:
        for start_pid in sorted(ui_agy_pids):
            order: List[int] = [start_pid]
            seen_idx: Dict[int, int] = {start_pid: 0}
            cur = proc_by_pid.get(start_pid)
            depth = 0
            while cur is not None and depth < 48:
                ppid = cur.ppid
                if ppid <= 0:
                    break
                if ppid in seen_idx:
                    cyc = order[seen_idx[ppid] :]
                    ui_cyc = [x for x in cyc if x in ui_agy_pids]
                    if ui_cyc:
                        root_main_pids.add(min(ui_cyc))
                    break
                seen_idx[ppid] = len(order)
                order.append(ppid)
                cur = proc_by_pid.get(ppid)
                depth += 1
        if not root_main_pids:
            root_main_pids = set(ui_agy_pids)

    orphan_ls_pids = [ls.pid for ls in ls_entries if not _has_ancestor_in(ls.pid, ui_agy_pids)]

    return {
        "main_pids": sorted(root_main_pids),
        "all_agy_pids": sorted(ui_agy_pids),
        "ls_worker_pids": sorted(ls_worker_pids),
        "ls_descendant_pids": sorted(ls_descendant_pids),
        "ls_pids": [ls.pid for ls in ls_entries],
        "orphan_ls_pids": orphan_ls_pids,
    }


def _prune_cmdline_cache(live_pids: Optional[Set[int]] = None) -> None:
    with _CMDLINE_LOCK:
        if live_pids is not None:
            for cached_pid in [k for k in _CMDLINE_CACHE if k not in live_pids]:
                del _CMDLINE_CACHE[cached_pid]
        while len(_CMDLINE_CACHE) > _CMDLINE_CACHE_MAX:
            oldest = next(iter(_CMDLINE_CACHE))
            del _CMDLINE_CACHE[oldest]


def _query_process_cmdline_wmi(
    pid: int,
    use_cache: bool = True,
    live_pids: Optional[Set[int]] = None,
) -> str:
    """Fetch full CommandLine of a single PID via hidden PowerShell CIM call with thread-safe PID memoization."""
    if sys.platform != "win32" or pid <= 0:
        return ""
    if live_pids is not None:
        _prune_cmdline_cache(live_pids)
    if use_cache:
        with _CMDLINE_LOCK:
            cached = _CMDLINE_CACHE.get(pid)
            if cached is not None:
                return cached

    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    cmd = [
        "powershell.exe",
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        f"(Get-CimInstance Win32_Process -Filter 'ProcessId = {int(pid)}').CommandLine",
    ]
    try:
        res = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=4.0,
            creationflags=creationflags,
        )
        out = (res.stdout or "").strip()
        if out:
            with _CMDLINE_LOCK:
                if len(_CMDLINE_CACHE) >= _CMDLINE_CACHE_MAX and pid not in _CMDLINE_CACHE:
                    oldest = next(iter(_CMDLINE_CACHE))
                    del _CMDLINE_CACHE[oldest]
                _CMDLINE_CACHE[pid] = out
        return out
    except Exception:
        return ""


def _grab_cmdline_flag(cmdline: str, flag: str) -> str:
    pat = _FLAG_PATTERN_CACHE.get(flag)
    if pat is None:
        pat = re.compile(
            rf'(?:^|\s){re.escape(flag)}(?:=|\s+)(?:"((?:[^"\\]|\\.)*)"|\'([^\']*)\'|([^\s"\']+))'
        )
        _FLAG_PATTERN_CACHE[flag] = pat
    m = pat.search(cmdline)
    if not m:
        return ""
    if m.group(1) is not None:
        return m.group(1).replace('\\"', '"').replace("\\\\", "\\").strip()
    if m.group(2) is not None:
        return m.group(2).strip()
    unquoted = (m.group(3) or "").strip()
    if unquoted.startswith("--"):
        return ""
    return unquoted


def parse_ls_cmdline(cmdline: str) -> Dict[str, str]:
    if not cmdline:
        return {
            "ide_version": "",
            "csrf_token": "",
        }

    return {
        "ide_version": _grab_cmdline_flag(cmdline, "--override_ide_version"),
        "csrf_token": _grab_cmdline_flag(cmdline, "--csrf_token"),
    }


def terminate_pid_native(
    pid: int,
    allowed_exe_prefixes: Optional[Tuple[str, ...]] = None,
) -> bool:
    return terminate_pid_win32(pid, exit_code=1, allowed_exe_prefixes=allowed_exe_prefixes)


def clean_stale_hub_files(
    keep_file: str = "",
    daemon_dir: Optional[Path] = None,
    preserve_pids: Optional[Set[int]] = None,
) -> int:
    d_dir = daemon_dir or get_agy_daemon_dir()
    if not d_dir.is_dir():
        return 0
    removed = 0
    keep_norm = os.path.normcase(os.path.abspath(keep_file)) if keep_file else ""
    for hf in d_dir.glob("hub_*.json"):
        try:
            if keep_norm and os.path.normcase(os.path.abspath(str(hf))) == keep_norm:
                continue
            if preserve_pids:
                try:
                    raw = json.loads(hf.read_text(encoding="utf-8", errors="replace"))
                    if int(raw.get("pid") or 0) in preserve_pids:
                        continue
                except Exception:
                    pass
            hf.unlink(missing_ok=True)
            removed += 1
        except Exception:
            continue
    return removed


def count_hub_files(daemon_dir: Optional[Path] = None) -> int:
    d_dir = daemon_dir or get_agy_daemon_dir()
    if not d_dir.is_dir():
        return 0
    try:
        return sum(1 for p in d_dir.glob("hub_*.json") if p.is_file())
    except Exception:
        return 0


def discover_hub_endpoint(
    target_pid: int = 0,
    daemon_dir: Optional[Path] = None,
    live_pids: Optional[Set[int]] = None,
) -> Optional[HubEndpoint]:
    """Locate matching daemon/hub_*.json for active language_server.exe."""
    d_dir = daemon_dir or get_agy_daemon_dir()
    if not d_dir.is_dir():
        return None
    try:
        hub_files = sorted(
            [p for p in d_dir.glob("hub_*.json") if p.is_file()],
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
    except Exception:
        return None

    total = len(hub_files)
    if total == 0:
        return None

    parsed_list: List[HubEndpoint] = []
    for hf in hub_files:
        try:
            raw = json.loads(hf.read_text(encoding="utf-8", errors="replace"))
            ep = HubEndpoint(
                file_path=str(hf),
                pid=int(raw.get("pid") or 0),
                http_port=int(raw.get("httpPort") or 0),
                https_port=int(raw.get("httpsPort") or 0),
                csrf_token=str(raw.get("csrfToken") or ""),
                total_hub_files=total,
            )
            if target_pid > 0 and ep.pid == target_pid and ep.http_port > 0 and ep.csrf_token:
                return ep
            if ep.http_port > 0 and ep.csrf_token:
                if live_pids is None or ep.pid in live_pids:
                    parsed_list.append(ep)
        except Exception:
            continue

    if target_pid == 0 and parsed_list:
        return parsed_list[0]
    return None


def inspect_active_ls(
    include_cmdline: bool = True,
    daemon_dir: Optional[Path] = None,
    entries: Optional[List[RawProcEntry]] = None,
    target_pid: int = 0,
) -> ActiveLsState:
    procs = entries if entries is not None else snapshot_processes_fast()
    live_all_pids = {p.pid for p in procs if p.pid > 0}
    _prune_cmdline_cache(live_all_pids)
    tree = find_antigravity_process_tree(procs)
    ls_entries = [
        p for p in procs if p.name.lower().startswith("language_server") and p.name.lower().endswith(".exe")
    ]
    total_hubs = count_hub_files(daemon_dir=daemon_dir)
    if not ls_entries:
        return ActiveLsState(
            running=False,
            main_pids=tree["main_pids"],
            all_ls_pids=[],
            total_hub_files=total_hubs,
            hub=None,
        )

    live_ls_pids = {p.pid for p in ls_entries}
    if target_pid > 0:
        exact = [p for p in ls_entries if p.pid == target_pid]
        if not exact:
            return ActiveLsState(
                running=False,
                main_pids=tree["main_pids"],
                all_ls_pids=[p.pid for p in ls_entries],
                total_hub_files=total_hubs,
                hub=None,
            )
        chosen = exact[0]
        hub = discover_hub_endpoint(chosen.pid, daemon_dir=daemon_dir, live_pids=live_ls_pids)
    else:
        # Prefer language_server whose parent is a real UI Antigravity.exe
        agy_set = set(tree["all_agy_pids"])
        live_children = [p for p in ls_entries if p.ppid in agy_set]
        chosen = live_children[0] if live_children else ls_entries[0]
        hub = discover_hub_endpoint(chosen.pid, daemon_dir=daemon_dir, live_pids=live_ls_pids)
        if hub is None:
            hub = discover_hub_endpoint(0, daemon_dir=daemon_dir, live_pids=live_ls_pids)
            if hub is not None:
                chosen = next((p for p in ls_entries if p.pid == hub.pid), chosen)

    cmdline = _query_process_cmdline_wmi(chosen.pid, live_pids=live_all_pids) if include_cmdline else ""
    flags = parse_ls_cmdline(cmdline)

    return ActiveLsState(
        running=True,
        pid=chosen.pid,
        parent_pid=chosen.ppid,
        name=chosen.name,
        command_line=cmdline,
        ide_version=flags["ide_version"],
        main_pids=tree["main_pids"],
        all_ls_pids=[p.pid for p in ls_entries],
        total_hub_files=hub.total_hub_files if hub else total_hubs,
        hub=hub,
    )


class ConnectRpcError(RuntimeError):
    def __init__(self, method: str, message: str, status_code: int = 0) -> None:
        super().__init__(f"[{method}] {message}")
        self.method = method
        self.status_code = status_code


class _TrackedHTTPConnection(http.client.HTTPConnection):
    """HTTPConnection retaining `raw_sock` even after getresponse() clears `sock` on Connection: close."""

    def __init__(self, host: str, port: int, timeout: float) -> None:
        super().__init__(host, port, timeout=timeout)
        self.raw_sock: Optional[socket.socket] = None

    def connect(self) -> None:
        super().connect()
        self.raw_sock = self.sock


class ConnectRpcClient:
    """Zero-dependency Connect-Protocol v1 JSON client locked strictly to loopback 127.0.0.1."""

    SERVICE_PREFIX = "exa.language_server_pb.LanguageServerService"

    def __init__(self, http_port: int, csrf_token: str, host: str = "127.0.0.1") -> None:
        if host != "127.0.0.1":
            raise ValueError(f"Security boundary violation: ConnectRpcClient only permits 127.0.0.1, got {host!r}")
        self.http_port = int(http_port)
        self.csrf_token = str(csrf_token)
        self.host = "127.0.0.1"

    @classmethod
    def from_hub(cls, hub: HubEndpoint) -> "ConnectRpcClient":
        return cls(http_port=hub.http_port, csrf_token=hub.csrf_token)

    MAX_RESPONSE_BYTES = 8 * 1024 * 1024  # hard cap on local RPC response bodies

    def call(
        self,
        method: str,
        payload: Optional[Dict[str, Any]] = None,
        timeout: float = 10.0,
    ) -> Dict[str, Any]:
        """
        `timeout` is a HARD TOTAL deadline for connect + headers + body. A watchdog timer forcibly shuts the
        socket down at the deadline (using `raw_sock` so `Connection: close` responses cannot unhook it), and each
        phase tightens the socket timeout to the remaining budget. Bodies are capped at MAX_RESPONSE_BYTES.
        Direct `http.client` to 127.0.0.1 never consults system HTTP proxies.
        """
        if self.http_port <= 0 or not self.csrf_token:
            raise ConnectRpcError(method, "Missing valid local RPC port or CSRF token")
        if not (1 <= self.http_port <= 65535):
            raise ConnectRpcError(method, f"Invalid local RPC port {self.http_port}")

        total = max(0.1, float(timeout))
        deadline = time.monotonic() + total
        path = f"/{self.SERVICE_PREFIX}/{method}"
        body_bytes = json.dumps(payload if payload is not None else {}).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Connect-Protocol-Version": "1",
            "x-codeium-csrf-token": self.csrf_token,
        }

        conn = _TrackedHTTPConnection(self.host, self.http_port, timeout=total)
        resp: Optional[http.client.HTTPResponse] = None
        aborted = threading.Event()

        def _abort() -> None:
            aborted.set()
            seen_socks: Set[int] = set()
            for sock in (conn.raw_sock, getattr(conn, "sock", None)):
                if sock is not None and id(sock) not in seen_socks:
                    seen_socks.add(id(sock))
                    try:
                        sock.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
            if resp is not None:
                try:
                    resp.close()
                except Exception:
                    pass
            try:
                conn.close()
            except Exception:
                pass

        def _tighten_sock_timeout() -> None:
            rem = deadline - time.monotonic()
            if rem <= 0:
                raise TimeoutError("total deadline exceeded")
            sock = conn.raw_sock or getattr(conn, "sock", None)
            if sock is not None:
                try:
                    sock.settimeout(max(0.05, rem))
                except OSError:
                    pass

        killer = threading.Timer(total, _abort)
        killer.daemon = True
        killer.start()

        def _reject_nonfinite(const_tok: str) -> Any:
            raise ValueError(f"Invalid non-finite JSON token in RPC response: {const_tok}")

        try:
            conn.request("POST", path, body=body_bytes, headers=headers)
            _tighten_sock_timeout()
            resp = conn.getresponse()
            chunks: List[bytes] = []
            got = 0
            while True:
                _tighten_sock_timeout()
                buf = resp.read1(65536)
                if not buf:
                    break
                got += len(buf)
                if got > self.MAX_RESPONSE_BYTES:
                    raise ConnectRpcError(method, f"Response exceeds {self.MAX_RESPONSE_BYTES} bytes")
                chunks.append(buf)
            raw_bytes = b"".join(chunks)
            if aborted.is_set():
                raise TimeoutError("total deadline exceeded")
            if not (200 <= resp.status < 300):
                detail = raw_bytes.decode("utf-8", errors="replace")[:300]
                raise ConnectRpcError(
                    method,
                    f"HTTP {resp.status} {resp.reason}: {detail}".strip(),
                    status_code=resp.status,
                )
            raw = raw_bytes.decode("utf-8", errors="replace").strip()
            if not raw:
                return {}
            decoded = json.loads(raw, parse_constant=_reject_nonfinite)
            if not isinstance(decoded, dict):
                raise ConnectRpcError(
                    method,
                    f"Expected JSON object root, got {type(decoded).__name__}",
                )
            return decoded
        except ConnectRpcError:
            raise
        except Exception as e:
            if aborted.is_set() or isinstance(e, TimeoutError):
                raise ConnectRpcError(method, f"Total RPC deadline of {total:.1f}s exceeded") from e
            raise ConnectRpcError(method, str(e)) from e
        finally:
            killer.cancel()
            if resp is not None:
                try:
                    resp.close()
                except Exception:
                    pass
            try:
                conn.close()
            except Exception:
                pass

    def get_user_status(self, timeout: float = 10.0) -> Dict[str, Any]:
        return self.call(
            "GetUserStatus",
            {
                "metadata": {
                    "ideName": "antigravity",
                    "extensionName": "antigravity",
                    "locale": "zh-CN",
                }
            },
            timeout=timeout,
        )

    def get_load_code_assist(self, timeout: float = 12.0) -> Dict[str, Any]:
        return self.call("GetLoadCodeAssist", {}, timeout=timeout)

    def retrieve_user_quota_summary(self, timeout: float = 10.0) -> Dict[str, Any]:
        return self.call("RetrieveUserQuotaSummary", {}, timeout=timeout)


def _align_tail_bytes(raw: bytes, actual_offset: int, target_offset: int) -> bytes:
    """
    Ensure partial tail reads never slice mid-UTF8 codepoint or emit incomplete partial first lines.
    - If `target_offset <= 0`, `raw` starts at byte 0 of file -> keep whole buffer.
    - Otherwise, scan backwards in `raw[:target_offset - actual_offset]` for the nearest `\\n`
      so reading aligns cleanly to the start of a complete line.
    - If a single line exceeds `_TAIL_LOOKBEHIND_BYTES` (`actual_offset > 0` and no `\\n` in lookbehind),
      strip any leading UTF-8 continuation bytes (`0x80..0xBF`) and discard up to the first `\\n`.
    """
    if not raw:
        return b""
    if target_offset <= 0:
        return raw

    rel = max(0, min(len(raw), target_offset - actual_offset))
    prev_nl = raw.rfind(b"\n", 0, rel)
    if prev_nl != -1:
        return raw[prev_nl + 1 :]
    if actual_offset == 0:
        return raw
    sliced = raw[rel:]
    idx = 0
    n = len(sliced)
    while idx < n and (0x80 <= sliced[idx] <= 0xBF):
        idx += 1
    sliced = sliced[idx:]
    first_nl = sliced.find(b"\n")
    if first_nl == -1:
        return b""
    return sliced[first_nl + 1 :]


def _slice_clean_tail_bytes(raw: bytes, actual_offset: int, target_offset: int) -> str:
    aligned = _align_tail_bytes(raw, actual_offset, target_offset)
    if not aligned:
        return ""
    return aligned.decode("utf-8", errors="replace")


def read_shared_file_delta(
    path: Path | str,
    prev_file_id: Optional[Tuple[int, int, int]] = None,
    prev_offset: int = 0,
    max_tail_bytes: int = 4 * 1024 * 1024,
) -> Tuple[Optional[Tuple[int, int, int]], int, bytes, bool]:
    """
    Incremental lock-free file reader.
    Returns `(file_id, new_offset, chunk_bytes, reset_required)`:
    - If file is unchanged since `(prev_file_id, prev_offset)`: `(file_id, total, b"", False)` (zero ReadFile).
    - If same file grew by `<= max_tail_bytes`: reads only `[prev_offset, total)` -> `(file_id, new_offset, chunk, False)`.
    - Otherwise (cold start, rotation/replacement, truncation, or jump > max_tail_bytes): aligns and reads the
      latest `max_tail_bytes` tail -> `(file_id, end_offset, clean_bytes, True)`.
    """
    if max_tail_bytes <= 0:
        return None, 0, b"", True
    p_str = str(path)
    if not os.path.exists(p_str):
        return None, 0, b"", True

    if _KERNEL32 is None:
        try:
            with open(p_str, "rb") as f:
                st = os.fstat(f.fileno())
                file_id: Optional[Tuple[int, int, int]] = (
                    int(st.st_dev),
                    int(st.st_ino),
                    int(getattr(st, "st_ctime_ns", 0)),
                )
                f.seek(0, os.SEEK_END)
                total = f.tell()
                if (
                    prev_file_id is not None
                    and file_id == prev_file_id
                    and prev_offset >= 0
                    and total == prev_offset
                ):
                    return file_id, total, b"", False
                if (
                    prev_file_id is not None
                    and file_id == prev_file_id
                    and 0 <= prev_offset < total
                    and (total - prev_offset) <= max_tail_bytes
                ):
                    f.seek(prev_offset, os.SEEK_SET)
                    raw = f.read(total - prev_offset)
                    return file_id, prev_offset + len(raw), raw, False
                if total <= 0:
                    return file_id, 0, b"", True
                target_offset = max(0, total - max_tail_bytes)
                actual_offset = max(0, target_offset - _TAIL_LOOKBEHIND_BYTES)
                f.seek(actual_offset, os.SEEK_SET)
                raw = f.read(total - actual_offset)
                end_off = actual_offset + len(raw)
                return file_id, end_off, _align_tail_bytes(raw, actual_offset, target_offset), True
        except Exception:
            return None, 0, b"", True

    h_file = _KERNEL32.CreateFileW(
        p_str,
        GENERIC_READ,
        FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
        None,
        OPEN_EXISTING,
        FILE_ATTRIBUTE_NORMAL,
        None,
    )
    if not _is_valid_handle(h_file):
        return None, 0, b"", True

    try:
        info = BY_HANDLE_FILE_INFORMATION()
        file_id_win: Optional[Tuple[int, int, int]] = None
        if _KERNEL32.GetFileInformationByHandle(h_file, ctypes.byref(info)):
            file_id_win = (
                int(info.dwVolumeSerialNumber),
                (int(info.nFileIndexHigh) << 32) | int(info.nFileIndexLow),
                (int(info.ftCreationTime.dwHighDateTime) << 32) | int(info.ftCreationTime.dwLowDateTime),
            )
        file_size = wintypes.LARGE_INTEGER(0)
        if not _KERNEL32.GetFileSizeEx(h_file, ctypes.byref(file_size)):
            return None, 0, b"", True
        total = int(file_size.value)

        if (
            file_id_win is not None
            and prev_file_id is not None
            and file_id_win == prev_file_id
            and prev_offset >= 0
            and total == prev_offset
        ):
            return file_id_win, total, b"", False

        def _seek_and_read(offset: int, length: int) -> Optional[bytes]:
            if length <= 0:
                return b""
            if offset > 0:
                new_pos = wintypes.LARGE_INTEGER(0)
                if not _KERNEL32.SetFilePointerEx(
                    h_file,
                    wintypes.LARGE_INTEGER(offset),
                    ctypes.byref(new_pos),
                    FILE_BEGIN,
                ):
                    return None
            buf = ctypes.create_string_buffer(length)
            bytes_read = wintypes.DWORD(0)
            if not _KERNEL32.ReadFile(h_file, buf, length, ctypes.byref(bytes_read), None):
                return None
            return buf.raw[: bytes_read.value]

        if (
            file_id_win is not None
            and prev_file_id is not None
            and file_id_win == prev_file_id
            and 0 <= prev_offset < total
            and (total - prev_offset) <= max_tail_bytes
        ):
            delta_raw = _seek_and_read(prev_offset, total - prev_offset)
            if delta_raw is None:
                return None, 0, b"", True
            return file_id_win, prev_offset + len(delta_raw), delta_raw, False

        if total <= 0:
            return file_id_win, 0, b"", True
        target_offset = max(0, total - max_tail_bytes)
        actual_offset = max(0, target_offset - _TAIL_LOOKBEHIND_BYTES)
        read_len = total - actual_offset
        raw = _seek_and_read(actual_offset, read_len)
        if raw is None:
            return None, 0, b"", True
        end_off = actual_offset + len(raw)
        return file_id_win, end_off, _align_tail_bytes(raw, actual_offset, target_offset), True
    finally:
        close_handle_safe(h_file)


def read_shared_file_text(path: Path | str, max_tail_bytes: int = 4 * 1024 * 1024) -> str:
    """
    Lock-free reader for files held open without flush by Electron/Go (e.g. language_server.log).
    Uses Win32 CreateFileW with FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE on Windows.
    """
    if max_tail_bytes <= 0:
        return ""
    p_str = str(path)
    if not os.path.exists(p_str):
        return ""

    if _KERNEL32 is None:
        try:
            with open(p_str, "rb") as f:
                f.seek(0, os.SEEK_END)
                total = f.tell()
                if total <= 0:
                    return ""
                target_offset = max(0, total - max_tail_bytes)
                actual_offset = max(0, target_offset - _TAIL_LOOKBEHIND_BYTES)
                f.seek(actual_offset, os.SEEK_SET)
                raw = f.read(total - actual_offset)
                return _slice_clean_tail_bytes(raw, actual_offset, target_offset)
        except Exception:
            return ""

    h_file = _KERNEL32.CreateFileW(
        p_str,
        GENERIC_READ,
        FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
        None,
        OPEN_EXISTING,
        FILE_ATTRIBUTE_NORMAL,
        None,
    )
    if not _is_valid_handle(h_file):
        return ""

    try:
        file_size = wintypes.LARGE_INTEGER(0)
        if not _KERNEL32.GetFileSizeEx(h_file, ctypes.byref(file_size)):
            return ""
        total = int(file_size.value)
        if total <= 0:
            return ""

        target_offset = max(0, total - max_tail_bytes)
        actual_offset = max(0, target_offset - _TAIL_LOOKBEHIND_BYTES)
        read_len = total - actual_offset
        if read_len <= 0:
            return ""

        if actual_offset > 0:
            new_pos = wintypes.LARGE_INTEGER(0)
            seek_ok = _KERNEL32.SetFilePointerEx(
                h_file,
                wintypes.LARGE_INTEGER(actual_offset),
                ctypes.byref(new_pos),
                FILE_BEGIN,
            )
            if not seek_ok:
                return ""

        buf = ctypes.create_string_buffer(read_len)
        bytes_read = wintypes.DWORD(0)
        ok = _KERNEL32.ReadFile(h_file, buf, read_len, ctypes.byref(bytes_read), None)
        if not ok or bytes_read.value == 0:
            return ""
        raw = buf.raw[: bytes_read.value]
        return _slice_clean_tail_bytes(raw, actual_offset, target_offset)
    finally:
        close_handle_safe(h_file)


def read_file_bytes_shared(path: Path | str, max_bytes: int = 16 * 1024 * 1024) -> Optional[bytes]:
    """
    Read a small file in full with FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE, so concurrent
    readers never block a writer's atomic `os.replace` (plain `open()` omits FILE_SHARE_DELETE on Windows, which
    makes MoveFileEx fail with a sharing violation and forces the writer into a retry storm).
    Returns bytes (possibly b"") on success, or None on failure (missing file / transient sharing error).
    """
    p_str = str(path)
    if max_bytes <= 0:
        return b""
    if _KERNEL32 is None:
        try:
            with open(p_str, "rb") as f:
                return f.read(max_bytes)
        except OSError:
            return None
    h_file = _KERNEL32.CreateFileW(
        p_str,
        GENERIC_READ,
        FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
        None,
        OPEN_EXISTING,
        FILE_ATTRIBUTE_NORMAL,
        None,
    )
    if not _is_valid_handle(h_file):
        return None
    try:
        file_size = wintypes.LARGE_INTEGER(0)
        if not _KERNEL32.GetFileSizeEx(h_file, ctypes.byref(file_size)):
            return None
        total = min(int(file_size.value), int(max_bytes))
        if total <= 0:
            return b""
        buf = ctypes.create_string_buffer(total)
        bytes_read = wintypes.DWORD(0)
        if not _KERNEL32.ReadFile(h_file, buf, total, ctypes.byref(bytes_read), None):
            return None
        return buf.raw[: bytes_read.value]
    finally:
        close_handle_safe(h_file)


def read_shared_file_lines(path: Path | str, max_tail_bytes: int = 4 * 1024 * 1024) -> List[str]:
    text = read_shared_file_text(path, max_tail_bytes=max_tail_bytes)
    if not text:
        return []
    return text.splitlines()


def read_shared_tail_bytes(path: Path | str, max_bytes: int = 16 * 1024) -> Tuple[bytes, bool]:
    """
    Lock-free tail byte reader using Win32 FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE.
    Reads at most `max_bytes` from the end of `path` (plus a 1-byte lookbehind when `size > max_bytes`
    to detect whether the tail starts on an exact `\\n` boundary).
    Returns `(tail_bytes, truncated_head)`:
      - `truncated_head=False` when `tail_bytes` starts cleanly at byte 0 of the file or immediately after `\\n`.
      - `truncated_head=True` when `tail_bytes` starts mid-line (caller should discard up to first `\\n`).
    """
    if max_bytes <= 0:
        return b"", False
    p_str = str(path)
    if not os.path.exists(p_str):
        return b"", False

    if _KERNEL32 is None:
        try:
            with open(p_str, "rb") as f:
                f.seek(0, os.SEEK_END)
                total = f.tell()
                if total <= 0:
                    return b"", False
                if total <= max_bytes:
                    f.seek(0, os.SEEK_SET)
                    return f.read(total), False
                seek_pos = total - max_bytes - 1
                f.seek(seek_pos, os.SEEK_SET)
                buf_raw = f.read(max_bytes + 1)
                if not buf_raw:
                    return b"", False
                return buf_raw[1:], (buf_raw[0] != 0x0A)
        except OSError:
            return b"", False

    h_file = _KERNEL32.CreateFileW(
        p_str,
        GENERIC_READ,
        FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
        None,
        OPEN_EXISTING,
        FILE_ATTRIBUTE_NORMAL,
        None,
    )
    if not _is_valid_handle(h_file):
        return b"", False

    try:
        file_size = wintypes.LARGE_INTEGER(0)
        if not _KERNEL32.GetFileSizeEx(h_file, ctypes.byref(file_size)):
            return b"", False
        total = int(file_size.value)
        if total <= 0:
            return b"", False

        if total <= max_bytes:
            buf = ctypes.create_string_buffer(total)
            bytes_read = wintypes.DWORD(0)
            if not _KERNEL32.ReadFile(h_file, buf, total, ctypes.byref(bytes_read), None) or bytes_read.value == 0:
                return b"", False
            return buf.raw[: bytes_read.value], False

        seek_pos = total - max_bytes - 1
        new_pos = wintypes.LARGE_INTEGER(0)
        if not _KERNEL32.SetFilePointerEx(
            h_file,
            wintypes.LARGE_INTEGER(seek_pos),
            ctypes.byref(new_pos),
            FILE_BEGIN,
        ):
            return b"", False

        read_len = max_bytes + 1
        buf = ctypes.create_string_buffer(read_len)
        bytes_read = wintypes.DWORD(0)
        if not _KERNEL32.ReadFile(h_file, buf, read_len, ctypes.byref(bytes_read), None) or bytes_read.value == 0:
            return b"", False
        chunk = buf.raw[: bytes_read.value]
        return chunk[1:], (chunk[0] != 0x0A)
    finally:
        close_handle_safe(h_file)
