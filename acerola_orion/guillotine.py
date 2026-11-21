# -*- coding: utf-8 -*-
"""
Acerola-Guillotine 内核句柄孤儿进程断头台守卫 (acerola_orion/guillotine.py)
- 替代依赖窗口句柄轮询（最小化易误杀）的粗糙脚本
- 采用 64-bit 安全 Win32 OpenProcess(SYNCHRONIZE) + WaitForMultipleObjects 等待真实 Electron UI 根主进程，
  并按周期复查经验证归属的 language_server 句柄：
  * 免疫 language_server -> cmd.exe -> Antigravity.exe (Node worker) 循环死锁；
  * 窗口最小化 / 托盘隐藏 / 页面重载期间句柄未激发，不会误杀活跃会话；
  * 仅追踪并终止经同用户 SID、同安装目录、同 UI 祖先链与自身 hub 文件验明归属的 language_server 句柄，
    在全部 UI 根主进程退出后及时清理残留孤儿进程，同时支持后台增量运行 Orion-Vault 对话备份巡检。
"""

from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from .constants import (
    GUILLOTINE_MUTEX_NAME,
    SCHEDULED_TASK_NAME,
    atomic_write_text,
    get_agy_conversations_dir,
    get_config_path,
    get_data_dir,
    get_guillotine_log_path,
    load_vault_config,
)
from .ledger import OrionLedger
from .rpc import (
    SYNCHRONIZE,
    WAIT_OBJECT_0,
    acquire_single_instance_mutex,
    close_handle_safe,
    discover_hub_endpoint,
    find_antigravity_process_tree,
    is_mutex_held,
    open_owned_language_server,
    open_process_handle,
    process_exists_win32,
    query_process_image_path_win32,
    release_and_close_mutex,
    snapshot_processes_fast,
    terminate_owned_handle,
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
    """Legacy artifacts lack an ownership marker; do not remove them automatically."""
    return {
        "ok": False,
        "killed_pids": [],
        "task_removed": False,
        "legacy_dir_removed": False,
        "message": "旧任务与目录未带本版本归属记录，已保留。请在 Windows 任务计划程序核对原操作。",
    }


def _resolve_pythonw_executable() -> str:
    exe = Path(sys.executable)
    pw = exe.with_name("pythonw.exe")
    if pw.is_file():
        return str(pw)
    return str(exe)


def start_guard_background() -> Dict[str, Any]:
    """Spawn the single-instance Acerola-Guillotine daemon detached in the background."""
    _guard_stop_file().unlink(missing_ok=True)

    if sys.platform != "win32":
        return {"ok": False, "message": "仅支持 Windows 内核句柄守护"}

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
                "message": f"Acerola-Guillotine 句柄等待与周期复查内核守卫已启动 (PID: {proc.pid})！",
            }

    return {
        "ok": proc.poll() is None,
        "already_running": False,
        "pid": proc.pid,
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

        return {"ok": False, "stopped_pid": 0, "message": "守卫尚未退出；停止标记已保留。"}

    return {"ok": True, "stopped_pid": pid, "message": "守护进程未在运行。"}


def install_guard_autostart() -> Dict[str, Any]:
    """Register Logon Scheduled Task for AcerolaOrionGuard and start it immediately."""
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
        "message": (
            f"已注册当前用户登录自启 [{SCHEDULED_TASK_NAME}] 并启动句柄等待与周期复查内核守卫（开机不弹窗且不唤起仪表盘；即使关闭面板仍持续值守）！"
            if task_ok
            else f"自启任务注册异常 ({err_detail})，但后台守卫状态: {bg_res.get('message')}"
        ),
    }


def uninstall_guard_autostart() -> Dict[str, Any]:
    stop_res = stop_guard_daemon()
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
        "ok": task_removed and bool(stop_res.get("ok")),
        "task_removed": task_removed,
        "stop_result": stop_res,
        "message": f"登录任务移除={task_removed}，守卫状态：{stop_res.get('message')}",
    }


def _release_tracked_servers(tracked: Dict[int, int], stop: bool = False) -> List[int]:
    killed: List[int] = []
    for pid, handle in tracked.items():
        try:
            if stop and terminate_owned_handle(handle):
                killed.append(pid)
        finally:
            close_handle_safe(handle)
    tracked.clear()
    return killed


def run_guillotine_daemon_loop(max_cycles: int = 0) -> int:
    """
    Main loop for `acerola-orion guard daemon`.
      1. Acquire single-instance Win32 Named Mutex.
      2. Snapshot process tree and verify/pin owned `language_server*.exe` handles
         (same user SID, same install directory, UI root ancestry, own hub file).
      3. When UI root `Antigravity.exe` processes are alive:
         - Record `session_online` & periodic `guard_heartbeat` quota watermarks via background worker;
         - Run periodic Orion-Vault incremental backup sweeps when `vault_auto_backup` is enabled;
         - Wait on UI root `SYNCHRONIZE` handles via `WaitForMultipleObjects(bWaitAll=True, timeout=1500ms)`.
      4. When all observed UI root processes exit, terminate ONLY previously verified tracked LS handles.
    """
    if sys.platform != "win32":
        return 1

    mutex_handle, already = acquire_single_instance_mutex(GUILLOTINE_MUTEX_NAME)
    if already:
        return 0
    if mutex_handle is None:
        return 1

    try:
        return _daemon_body(max_cycles=max_cycles)
    finally:
        _guard_pid_file().unlink(missing_ok=True)
        _guard_stop_file().unlink(missing_ok=True)
        release_and_close_mutex(mutex_handle)


def _daemon_body(max_cycles: int = 0) -> int:
    _guard_stop_file().unlink(missing_ok=True)
    atomic_write_text(
        _guard_pid_file(),
        json.dumps({"pid": os.getpid(), "started_at": datetime.now().astimezone().isoformat()}, indent=2),
        encoding="utf-8",
    )
    _log_guard(f"Guillotine daemon started (PID={os.getpid()})")

    tracked_servers: Dict[int, int] = {}
    sealed_login_ls_pids: set[int] = set()
    next_seal_try: Dict[int, float] = {}
    seal_backoff: Dict[int, float] = {}
    last_periodic_snap = time.time()
    cycle_count = 0
    phantom_main_cycles = 0
    cfg_cache: Dict[str, Any] = {}
    cfg_cache_sig: Optional[Tuple[int, int]] = None
    cfg_cache_loaded_at = 0.0
    cfg_cache_valid = False
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

    vault_lock = threading.Lock()
    vault_stop_evt = threading.Event()
    vault_state: Dict[str, Any] = {
        "inflight": False,
        "thread": None,
        "last_run": 0.0,
        "last_conv_dir_mtime": -1,
    }

    def _launch_vault_sweep() -> None:
        with vault_lock:
            if vault_state["inflight"] or vault_stop_evt.is_set():
                return
            vault_state["inflight"] = True
            vault_state["last_run"] = time.time()

        def _vrun() -> None:
            try:
                from .vault import OrionVault

                res_v = OrionVault().sync_all(
                    respect_switch=True,
                    max_budget_sec=6.0,
                    abort_check=vault_stop_evt.is_set,
                )
                if res_v.newly_marked_deleted > 0:
                    _log_guard(
                        f"Orion-Vault preserved {res_v.newly_marked_deleted} deleted conversation(s) in vault"
                    )
            except Exception as ve:
                _log_guard(f"Orion-Vault background sweep warning: {ve}")
            finally:
                with vault_lock:
                    vault_state["inflight"] = False

        vt = threading.Thread(target=_vrun, name="orion-guard-vault-sweep", daemon=True)
        with vault_lock:
            vault_state["thread"] = vt
        vt.start()

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
                if tracked_servers:
                    killed = _release_tracked_servers(tracked_servers, stop=True)
                    _log_guard(f"UI closed: terminated tracked servers {killed}")
                    _reset_all_tracking()
                for _ in range(10):
                    if _guard_stop_file().exists():
                        break
                    time.sleep(0.2)
                continue

            live_ls_set = set(ls_pids)
            for pid in list(tracked_servers):
                if pid not in live_ls_set:
                    close_handle_safe(tracked_servers.pop(pid))
            for pid in ls_pids:
                if pid not in tracked_servers:
                    verified = open_owned_language_server(pid, entries)
                    if verified is not None:
                        tracked_servers[pid] = verified

            sealed_login_ls_pids.intersection_update(live_ls_set)
            for tracking_map in (next_seal_try, seal_backoff):
                for dead_pid in [k for k in tracking_map if k not in live_ls_set]:
                    del tracking_map[dead_pid]

            now = time.time()
            try:
                _st = get_config_path().stat()
                cur_cfg_sig: Optional[Tuple[int, int]] = (int(_st.st_mtime_ns), int(_st.st_size))
            except OSError:
                cur_cfg_sig = None
            if (
                not cfg_cache_valid
                or cur_cfg_sig != cfg_cache_sig
                or (now - cfg_cache_loaded_at) >= 30.0
            ):
                cfg_cache = load_vault_config()
                cfg_cache_sig = cur_cfg_sig
                cfg_cache_loaded_at = now
                cfg_cache_valid = True
            cfg = cfg_cache

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

            if (
                max_cycles == 0
                and open_process_handle is _ORIG_OPEN_PROCESS_HANDLE
                and bool(cfg.get("vault_auto_backup", True))
            ):
                try:
                    cdir_mt = int(get_agy_conversations_dir().stat().st_mtime_ns)
                except OSError:
                    cdir_mt = -1
                if cdir_mt != vault_state["last_conv_dir_mtime"] or (now - float(vault_state["last_run"])) >= 25.0:
                    vault_state["last_conv_dir_mtime"] = cdir_mt
                    _launch_vault_sweep()

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
                    if not recheck["main_pids"] and tracked_servers:
                        killed = _release_tracked_servers(tracked_servers, stop=True)
                        _log_guard(f"UI exited: terminated tracked servers {killed}")
                        _reset_all_tracking()
            finally:
                for h in handles:
                    close_handle_safe(h)

        return 0
    finally:
        vault_stop_evt.set()
        _release_tracked_servers(tracked_servers)
        tele_thread = tele_state.get("thread")
        if isinstance(tele_thread, threading.Thread) and tele_thread.is_alive():
            tele_thread.join(timeout=2.0)
        vault_thread = vault_state.get("thread")
        if isinstance(vault_thread, threading.Thread) and vault_thread.is_alive():
            vault_thread.join(timeout=2.0)
        _log_guard(f"Guillotine daemon exiting cleanly (PID={os.getpid()})")
