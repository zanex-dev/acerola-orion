# -*- coding: utf-8 -*-
"""
Acerola-Guillotine 零轮询内核句柄孤儿进程断头台守卫 (acerola_orion/guillotine.py)
- 替代依赖窗口句柄轮询（最小化易误杀）的粗糙脚本
- 采用 64-bit 安全 Win32 OpenProcess(SYNCHRONIZE) + WaitForMultipleObjects 监听真实 Electron UI 根主进程：
  * 免疫 language_server -> cmd.exe -> Antigravity.exe (Node worker) 循环死锁；
  * 窗口最小化 / 托盘隐藏 / 页面重载期间句柄未激发，0.00% CPU 挂起且绝不误杀；
  * 主进程真正退出的 1 毫秒内立即内核态唤醒，当场清理全部残留 language_server*.exe 及其附属子进程，
    物理阻断关窗后的后台自动摘要与知识蒸馏偷跑配额。
"""

from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from .constants import (
    GUILLOTINE_MUTEX_NAME,
    LEGACY_TASK_NAME,
    SCHEDULED_TASK_NAME,
    atomic_write_text,
    get_data_dir,
    get_guillotine_log_path,
)
from .ledger import OrionLedger
from .rpc import (
    SYNCHRONIZE,
    WAIT_OBJECT_0,
    acquire_single_instance_mutex,
    clean_stale_hub_files,
    close_handle_safe,
    discover_hub_endpoint,
    find_antigravity_process_tree,
    is_mutex_held,
    open_process_handle,
    process_exists_win32,
    query_process_image_path_win32,
    release_and_close_mutex,
    snapshot_processes_fast,
    terminate_pid_native,
    terminate_pid_win32,
    wait_for_handles_all,
)
from .telemetry import collect_telemetry, load_json_safe

_ORIG_OPEN_PROCESS_HANDLE = open_process_handle


def _guard_pid_file() -> Path:
    return get_data_dir() / "guard.pid"


def _guard_stop_file() -> Path:
    return get_data_dir() / "guard.stop"


def _log_guard(msg: str) -> None:
    p = get_guillotine_log_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        if p.is_file() and p.stat().st_size > 512 * 1024:
            bak = p.with_suffix(".log.1")
            bak.unlink(missing_ok=True)
            p.replace(bak)
        ts = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")
        with open(p, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] {msg}\n")
    except Exception:
        pass


def retire_legacy_orphan_killer() -> Dict[str, Any]:
    """Unregister and stop legacy polling-based AntigravityOrphanKiller (ag_kill_orphan.ps1) if present."""
    killed_pids: List[int] = []
    task_removed = False
    legacy_dir_removed = False

    if sys.platform != "win32":
        return {"killed_pids": killed_pids, "task_removed": False, "legacy_dir_removed": False}

    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)

    try:
        q = subprocess.run(
            ["schtasks.exe", "/Query", "/TN", LEGACY_TASK_NAME],
            capture_output=True,
            timeout=4.0,
            creationflags=creationflags,
        )
        if q.returncode == 0:
            d = subprocess.run(
                ["schtasks.exe", "/Delete", "/TN", LEGACY_TASK_NAME, "/F"],
                capture_output=True,
                timeout=4.0,
                creationflags=creationflags,
            )
            task_removed = d.returncode == 0
    except Exception:
        pass

    my_pid = os.getpid()
    ps_candidates = [
        p.pid
        for p in snapshot_processes_fast()
        if p.name.lower() in ("powershell.exe", "pwsh.exe") and p.pid != my_pid
    ]
    if ps_candidates:
        try:
            r = subprocess.run(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    "Get-CimInstance Win32_Process | Where-Object { $_.ProcessId -ne $PID -and $_.CommandLine -match 'ag_kill_orphan\\.ps1' } | ForEach-Object { $_.ProcessId }",
                ],
                capture_output=True,
                text=True,
                timeout=5.0,
                creationflags=creationflags,
            )
            for line in (r.stdout or "").splitlines():
                s = line.strip()
                if s.isdigit():
                    pid = int(s)
                    if pid != my_pid and terminate_pid_native(
                        pid,
                        allowed_exe_prefixes=("powershell", "pwsh"),
                    ):
                        killed_pids.append(pid)
        except Exception:
            pass

    legacy_dir = Path.home() / ".gemini" / "ag_guard"
    if legacy_dir.is_dir():
        try:
            shutil.rmtree(legacy_dir, ignore_errors=True)
            legacy_dir_removed = not legacy_dir.exists()
        except Exception:
            pass

    if task_removed or killed_pids:
        _log_guard(
            f"Retired legacy {LEGACY_TASK_NAME}: task_removed={task_removed}, killed_pids={killed_pids}"
        )

    return {
        "killed_pids": killed_pids,
        "task_removed": task_removed,
        "legacy_dir_removed": legacy_dir_removed,
    }


def _resolve_pythonw_executable() -> str:
    exe = Path(sys.executable)
    pw = exe.with_name("pythonw.exe")
    if pw.is_file():
        return str(pw)
    return str(exe)


def start_guard_background() -> Dict[str, Any]:
    """Spawn the single-instance Acerola-Guillotine daemon detached in the background."""
    retire_info = retire_legacy_orphan_killer()
    _guard_stop_file().unlink(missing_ok=True)

    if sys.platform != "win32":
        return {"ok": False, "message": "仅支持 Windows 内核句柄守护", "retired_legacy": retire_info}

    if is_mutex_held(GUILLOTINE_MUTEX_NAME):
        pid = 0
        try:
            pid = int(json.loads(_guard_pid_file().read_text(encoding="utf-8")).get("pid") or 0)
        except Exception:
            pass
        return {
            "ok": True,
            "already_running": True,
            "pid": pid,
            "retired_legacy": retire_info,
            "message": f"Acerola-Guillotine 守护进程已在运行中 (PID: {pid or 'active'})。",
        }

    pyw = _resolve_pythonw_executable()
    pkg_root = str(Path(__file__).resolve().parent.parent)
    creationflags = (
        getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
        | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        | getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    )
    proc = subprocess.Popen(
        [pyw, "-m", "acerola_orion", "guard", "daemon"],
        cwd=pkg_root,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creationflags,
        close_fds=True,
    )

    for _ in range(15):
        time.sleep(0.15)
        if is_mutex_held(GUILLOTINE_MUTEX_NAME):
            return {
                "ok": True,
                "already_running": False,
                "pid": proc.pid,
                "retired_legacy": retire_info,
                "message": f"Acerola-Guillotine 零轮询内核守卫已启动 (PID: {proc.pid})！",
            }

    return {
        "ok": proc.poll() is None,
        "already_running": False,
        "pid": proc.pid,
        "retired_legacy": retire_info,
        "message": f"守护进程已拉起 (PID: {proc.pid})。",
    }


def _vbs_quote(s: str) -> str:
    """Escape an arbitrary string as a safe VBScript string literal (doubling `\"` -> `\"\"`)."""
    clean = str(s).replace("\r", "").replace("\n", "")
    return '"' + clean.replace('"', '""') + '"'


def _ps_single_quote(s: str) -> str:
    """Escape an arbitrary string as a safe PowerShell single-quoted literal (`'` -> `''`)."""
    clean = str(s).replace("\r", "").replace("\n", "")
    return "'" + clean.replace("'", "''") + "'"


def _build_guard_launcher_vbs(pyw: str, pkg_root: str) -> str:
    cmd_line = f'"{pyw}" -m acerola_orion guard daemon'
    return (
        'Set ws = CreateObject("WScript.Shell")\r\n'
        f"ws.CurrentDirectory = {_vbs_quote(pkg_root)}\r\n"
        f"ws.Run {_vbs_quote(cmd_line)}, 0, False\r\n"
    )


def stop_guard_daemon() -> Dict[str, Any]:
    atomic_write_text(_guard_stop_file(), "stop\n", encoding="utf-8")
    pid = 0
    try:
        pid_obj = load_json_safe(_guard_pid_file())
        pid = int(pid_obj.get("pid") or 0)
    except Exception:
        pass

    if sys.platform == "win32":
        for _ in range(20):
            if not is_mutex_held(GUILLOTINE_MUTEX_NAME):
                _guard_pid_file().unlink(missing_ok=True)
                _guard_stop_file().unlink(missing_ok=True)
                return {"ok": True, "stopped_pid": pid, "message": "Acerola-Guillotine 守护进程已优雅停止。"}
            time.sleep(0.15)

        if pid > 0:
            guard_prefixes = ("python", "acerola", "orion")
            img_name = os.path.basename(query_process_image_path_win32(pid)).lower()
            killed = False
            if img_name and img_name.startswith(guard_prefixes):
                killed = terminate_pid_win32(pid, exit_code=1, allowed_exe_prefixes=guard_prefixes)
            _guard_pid_file().unlink(missing_ok=True)
            _guard_stop_file().unlink(missing_ok=True)
            if killed:
                return {"ok": True, "stopped_pid": pid, "message": f"已强制终止守护进程 (PID: {pid})。"}
            return {
                "ok": False,
                "stopped_pid": 0,
                "message": f"守护句柄未释放，且 PID {pid} 非本守卫可执行映像 ({img_name or '不存在'})，已拒绝盲杀。",
            }

    return {"ok": True, "stopped_pid": pid, "message": "守护进程未在运行。"}


def install_guard_autostart() -> Dict[str, Any]:
    """Register Logon Scheduled Task for AcerolaOrionGuard and start it immediately."""
    retire_info = retire_legacy_orphan_killer()
    if sys.platform != "win32":
        return {"ok": False, "message": "仅支持 Windows 计划任务注册"}

    pyw = _resolve_pythonw_executable()
    pkg_root = str(Path(__file__).resolve().parent.parent)
    launcher_vbs = get_data_dir() / "start_guard_silent.vbs"
    vbs_body = _build_guard_launcher_vbs(pyw, pkg_root)
    atomic_write_text(launcher_vbs, vbs_body, encoding="utf-16")

    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    vbs_arg_quoted = _ps_single_quote(f'"{launcher_vbs}"')
    task_name_quoted = _ps_single_quote(SCHEDULED_TASK_NAME)
    ps_cmd = (
        f"[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
        f"$u = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name; "
        f"$act = New-ScheduledTaskAction -Execute 'wscript.exe' -Argument {vbs_arg_quoted}; "
        f"$trg = New-ScheduledTaskTrigger -AtLogOn -User $u; "
        f"$set = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit 0; "
        f"Register-ScheduledTask -TaskName {task_name_quoted} -Action $act -Trigger $trg -Settings $set -Force -ErrorAction Stop | Out-Null; "
        f"Start-ScheduledTask -TaskName {task_name_quoted}"
    )
    r = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps_cmd],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=8.0,
        creationflags=creationflags,
    )
    task_ok = r.returncode == 0
    if task_ok:
        mutex_up = False
        for _ in range(15):
            time.sleep(0.15)
            if is_mutex_held(GUILLOTINE_MUTEX_NAME):
                mutex_up = True
                break
        bg_res = start_guard_background() if not mutex_up else {"ok": True, "via": "scheduled_task"}
    else:
        bg_res = start_guard_background()
    err_detail = (r.stderr or r.stdout or "").strip()
    return {
        "ok": task_ok and bool(bg_res.get("ok")),
        "scheduled_task": SCHEDULED_TASK_NAME if task_ok else "failed",
        "launcher_vbs": str(launcher_vbs),
        "daemon": bg_res,
        "retired_legacy": retire_info,
        "message": (
            f"已开启断头台后台守卫并注册开机静默自启 [{SCHEDULED_TASK_NAME}]（独立 0% CPU 值守，开机不弹窗且不唤起仪表盘；即使关闭面板仍持续防关窗自动压缩与记忆污染）！"
            if task_ok
            else f"自启任务注册异常 ({err_detail})，但后台守卫状态: {bg_res.get('message')}"
        ),
    }


def uninstall_guard_autostart() -> Dict[str, Any]:
    stop_res = stop_guard_daemon()
    retire_legacy_orphan_killer()
    launcher_vbs = get_data_dir() / "start_guard_silent.vbs"
    launcher_vbs.unlink(missing_ok=True)
    task_removed = False
    if sys.platform == "win32":
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        try:
            d = subprocess.run(
                ["schtasks.exe", "/Delete", "/TN", SCHEDULED_TASK_NAME, "/F"],
                capture_output=True,
                timeout=5.0,
                creationflags=creationflags,
            )
            task_removed = d.returncode == 0
        except Exception:
            pass
    return {
        "ok": True,
        "task_removed": task_removed,
        "stop_result": stop_res,
        "message": f"已移除 [{SCHEDULED_TASK_NAME}] 计划任务与启动脚本，并停止后台守卫。",
    }


def _guillotine_orphan_tree(tree: Dict[str, List[int]]) -> Tuple[List[int], int]:
    """Terminate orphan language_server PIDs first, then any subordinate worker descendants."""
    my_pid = os.getpid()
    ls_prefixes = ("language_server",)
    worker_prefixes = (
        "language_server",
        "antigravity",
        "cmd",
        "conhost",
        "node",
        "powershell",
        "pwsh",
        "rg",
        "git",
    )
    ordered_targets: List[Tuple[int, Tuple[str, ...]]] = []
    seen: set[int] = set()
    for pid in tree.get("ls_pids", []):
        if pid > 0 and pid != my_pid and pid not in seen:
            seen.add(pid)
            ordered_targets.append((pid, ls_prefixes))
    for pid in tree.get("ls_descendant_pids", []):
        if pid > 0 and pid != my_pid and pid not in seen:
            seen.add(pid)
            ordered_targets.append((pid, worker_prefixes))

    killed: List[int] = []
    for pid, prefixes in ordered_targets:
        try:
            if terminate_pid_native(pid, allowed_exe_prefixes=prefixes):
                killed.append(pid)
        except Exception:
            pass
    cleaned_hubs = clean_stale_hub_files()
    return killed, cleaned_hubs


def run_guillotine_daemon_loop(max_cycles: int = 0) -> int:
    """
    Zero-Polling Kernel Event Loop:
      1. Acquire single-instance Win32 Named Mutex.
      2. Snapshot process tree:
         - If NO true UI root `Antigravity.exe` is alive -> immediately guillotine orphan `language_server*.exe`
           and subordinate workers so post-exit background summarization cannot drain quota.
         - If UI root `Antigravity.exe` IS alive:
           * Record `session_online` & periodic `guard_heartbeat` quota watermarks via single-flight background worker;
           * Open `SYNCHRONIZE` kernel handles to ALL UI root `Antigravity.exe` processes and block on
             `WaitForMultipleObjects(bWaitAll=True, timeout=1500ms)` with 0.00% CPU usage.
           * The exact millisecond all UI root processes exit, `WaitForMultipleObjects` wakes immediately (`<1ms`)
             and triggers the orphan guillotine.
    """
    if sys.platform != "win32":
        return 1

    mutex_handle, already = acquire_single_instance_mutex(GUILLOTINE_MUTEX_NAME)
    if already or mutex_handle is None:
        return 0

    try:
        return _daemon_body(max_cycles=max_cycles)
    finally:
        _guard_pid_file().unlink(missing_ok=True)
        release_and_close_mutex(mutex_handle)


def _daemon_body(max_cycles: int = 0) -> int:
    _guard_stop_file().unlink(missing_ok=True)
    atomic_write_text(
        _guard_pid_file(),
        json.dumps({"pid": os.getpid(), "started_at": datetime.now().astimezone().isoformat()}, indent=2),
        encoding="utf-8",
    )
    retire_legacy_orphan_killer()
    _log_guard(f"Guillotine daemon started (PID={os.getpid()})")

    sealed_login_ls_pids: set[int] = set()
    next_seal_try: Dict[int, float] = {}
    seal_backoff: Dict[int, float] = {}
    last_periodic_snap = time.time()
    cycle_count = 0
    phantom_main_cycles = 0
    ledger = OrionLedger()

    tele_lock = threading.Lock()
    tele_state: Dict[str, Any] = {
        "inflight": False,
        "result": None,
        "thread": None,
        "gen": 0,
        "started": 0.0,
        "kind": "",
        "lpids": [],
    }
    TELE_JOB_DEADLINE_SEC = 90.0

    def _tele_busy() -> bool:
        with tele_lock:
            return bool(tele_state["inflight"])

    def _launch_tele_job(kind: str, lpids: List[int]) -> None:
        job_lpids = list(lpids)
        with tele_lock:
            tele_state["gen"] += 1
            job_gen = tele_state["gen"]

        def _run() -> None:
            ok = False
            try:
                rep = collect_telemetry(include_guard_check=False)
                with tele_lock:
                    if tele_state["gen"] != job_gen:
                        return
                if kind == "seal":
                    ok = bool(
                        rep is not None
                        and ledger.record_quota_from_telemetry(rep, event_type="session_online")
                    )
                else:
                    ledger.record_quota_from_telemetry(rep, event_type="guard_heartbeat", min_interval_sec=300.0)
                    ok = True
            except Exception as e:
                _log_guard(f"Background telemetry job '{kind}' failed: {e}")
                ok = False
            with tele_lock:
                if tele_state["gen"] == job_gen:
                    tele_state["result"] = (kind, job_lpids, ok)

        with tele_lock:
            tele_state["inflight"] = True
            tele_state["result"] = None
            tele_state["started"] = time.monotonic()
            tele_state["kind"] = kind
            tele_state["lpids"] = job_lpids
        t = threading.Thread(target=_run, name=f"orion-guard-telemetry-{kind}", daemon=True)
        tele_state["thread"] = t
        t.start()

    def _reset_all_tracking() -> None:
        sealed_login_ls_pids.clear()
        next_seal_try.clear()
        seal_backoff.clear()

    try:
        while not _guard_stop_file().exists():
            if max_cycles > 0 and cycle_count >= max_cycles:
                break
            cycle_count += 1

            entries = snapshot_processes_fast()
            tree = find_antigravity_process_tree(entries)
            main_pids = tree["main_pids"]
            ls_pids = tree["ls_pids"]

            if not main_pids:
                phantom_main_cycles = 0
                if ls_pids or tree.get("ls_worker_pids"):
                    killed, cleaned = _guillotine_orphan_tree(tree)
                    _log_guard(
                        f"GUILLOTINE TRIGGERED (no UI main process): terminated PIDs={killed}, cleaned_hubs={cleaned}"
                    )
                    _reset_all_tracking()
                for _ in range(10):
                    if _guard_stop_file().exists():
                        break
                    time.sleep(0.2)
                continue

            live_ls_set = set(ls_pids)
            sealed_login_ls_pids.intersection_update(live_ls_set)
            for tracking_map in (next_seal_try, seal_backoff):
                for dead_pid in [k for k in tracking_map if k not in live_ls_set]:
                    del tracking_map[dead_pid]

            now = time.time()
            with tele_lock:
                tele_res = tele_state["result"]
                tele_state["result"] = None
                if tele_res is not None:
                    tele_state["inflight"] = False
                elif tele_state["inflight"] and (time.monotonic() - float(tele_state["started"])) > TELE_JOB_DEADLINE_SEC:
                    tele_state["gen"] += 1
                    tele_state["inflight"] = False
                    tele_res = (str(tele_state["kind"]), list(tele_state["lpids"]), False)
                    _log_guard(
                        f"Background telemetry job '{tele_state['kind']}' exceeded {TELE_JOB_DEADLINE_SEC:.0f}s; abandoned"
                    )
            if tele_res is not None:
                res_kind, res_lpids, res_ok = tele_res
                if res_kind == "seal":
                    for lpid in res_lpids:
                        if lpid not in live_ls_set:
                            continue
                        if res_ok:
                            sealed_login_ls_pids.add(lpid)
                            next_seal_try.pop(lpid, None)
                            seal_backoff.pop(lpid, None)
                            _log_guard(f"Recorded session_online quota watermark for LS PID {lpid}")
                        else:
                            delay = min(60.0, seal_backoff.get(lpid, 7.5) * 2.0)
                            seal_backoff[lpid] = delay
                            next_seal_try[lpid] = now + delay
                    if res_ok:
                        last_periodic_snap = now

            seal_candidates: List[int] = []
            for lpid in ls_pids:
                if (
                    lpid not in sealed_login_ls_pids
                    and now >= next_seal_try.get(lpid, 0.0)
                    and not _tele_busy()
                ):
                    hub = discover_hub_endpoint(lpid, live_pids=live_ls_set)
                    if hub and hub.http_port > 0:
                        seal_candidates.append(lpid)
                    else:
                        delay = min(60.0, seal_backoff.get(lpid, 7.5) * 2.0)
                        seal_backoff[lpid] = delay
                        next_seal_try[lpid] = now + delay

            if seal_candidates and not _tele_busy():
                _launch_tele_job("seal", seal_candidates)

            now = time.time()
            if ls_pids and (now - last_periodic_snap) >= 600.0 and not _tele_busy():
                last_periodic_snap = now
                _launch_tele_job("heartbeat", [])

            handles: List[int] = []
            alive_unwaitable = False
            use_real_kernel_handle = open_process_handle is _ORIG_OPEN_PROCESS_HANDLE
            for mpid in main_pids[:64]:
                kernel_alive: Optional[bool] = None
                if use_real_kernel_handle:
                    img = query_process_image_path_win32(mpid)
                    if img:
                        if os.path.basename(img).lower() != "antigravity.exe":
                            continue
                        kernel_alive = True
                    else:
                        kernel_alive = process_exists_win32(mpid)
                        if kernel_alive is False:
                            continue
                h = open_process_handle(mpid, SYNCHRONIZE)
                if h is not None:
                    handles.append(h)
                elif use_real_kernel_handle and kernel_alive is not False:
                    alive_unwaitable = True

            if not handles:
                if use_real_kernel_handle and not alive_unwaitable:
                    phantom_main_cycles += 1
                    if phantom_main_cycles >= 2:
                        _log_guard(
                            f"Stopping daemon loop: snapshot main_pids={main_pids} absent in OS kernel across 2 cycles"
                        )
                        break
                    time.sleep(0.05)
                else:
                    phantom_main_cycles = 0
                    time.sleep(0.5)
                continue

            phantom_main_cycles = 0

            try:
                res = wait_for_handles_all(handles, 1500)
                if WAIT_OBJECT_0 <= res < WAIT_OBJECT_0 + len(handles):
                    recheck = find_antigravity_process_tree(snapshot_processes_fast())
                    if not recheck["main_pids"] and (recheck["ls_pids"] or recheck.get("ls_worker_pids")):
                        killed, cleaned = _guillotine_orphan_tree(recheck)
                        _log_guard(
                            f"INSTANT EXIT GUILLOTINE (<1ms): killed trailing LS/worker PIDs={killed}, cleaned_hubs={cleaned}"
                        )
                        _reset_all_tracking()
            finally:
                for h in handles:
                    close_handle_safe(h)

        return 0
    finally:
        tele_thread = tele_state.get("thread")
        if isinstance(tele_thread, threading.Thread) and tele_thread.is_alive():
            tele_thread.join(timeout=2.0)
