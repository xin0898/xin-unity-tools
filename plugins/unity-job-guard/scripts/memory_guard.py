"""Editor memory checks and editor restarts for the Unity broker (`ujob memory`, `ujob restart`, the Play gate).

Why: a long-running editor grows until Windows nears its commit limit. Entering Play then works, but leaving Play
has to read the scene backup back, and at the commit limit that read fails ("__Backupscenes/0.backup is
corrupted"), the editor exits and every unsaved scene change is lost (seen 2026-11-02 at 126 of 127 GB).

Windows only: the readings use ctypes (no extra packages). Elsewhere `read()` returns None and nothing is gated.
"""
import ctypes
import os
import subprocess
import time

GB = 1024 ** 3

# Thresholds; override per project under "broker": {"memory": {...}} in ProjectSettings/UnityBrokerProbe.json.
DEFAULTS = {
    "refuseFreeGB": 20.0,        # free commit below this: no Play entry that loads a full map scene
    "refuseFreeGBLight": 10.0,   # ... and below this: not even a light one (test map, data backend)
    "warnFreeGB": 30.0,          # below this: run, with a warning
    "warnEditorGB": 30.0,        # editor private memory above this: warning
    "warnUptimeHours": 24.0,     # editor up longer than this: warning
}
GATE_OFF_ENV = "UJOB_MEMORY_GATE"  # "off" turns refusals into warnings (only when the user says so)


def thresholds(cfg=None):
    t = dict(DEFAULTS)
    for k, v in (cfg or {}).items():
        if k in t:
            try:
                t[k] = float(v)
            except (TypeError, ValueError):
                pass
    return t


# ── Readings ─────────────────────────────────────────────────────────────────────────────────────

class _MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]


class _PROCESS_MEMORY_COUNTERS_EX(ctypes.Structure):
    _fields_ = [("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong),
                ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
                ("PrivateUsage", ctypes.c_size_t)]


PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
SYNCHRONIZE = 0x00100000


def _open(pid, access=PROCESS_QUERY_LIMITED_INFORMATION):
    if os.name != "nt" or not pid:
        return None
    h = ctypes.windll.kernel32.OpenProcess(access, False, int(pid))
    return h or None


def editor_private_bytes(pid):
    h = _open(pid)
    if not h:
        return None
    try:
        c = _PROCESS_MEMORY_COUNTERS_EX()
        c.cb = ctypes.sizeof(c)
        ok = ctypes.windll.psapi.GetProcessMemoryInfo(h, ctypes.byref(c), c.cb)
        return int(c.PrivateUsage) if ok else None
    finally:
        ctypes.windll.kernel32.CloseHandle(h)


def process_image(pid):
    """Full path of the process's executable (the Unity.exe to start again), or None."""
    h = _open(pid)
    if not h:
        return None
    try:
        buf = ctypes.create_unicode_buffer(32768)
        size = ctypes.c_ulong(len(buf))
        ok = ctypes.windll.kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size))
        return buf.value if ok else None
    finally:
        ctypes.windll.kernel32.CloseHandle(h)


def process_alive(pid):
    h = _open(pid, SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION)
    if not h:
        return False
    try:
        return ctypes.windll.kernel32.WaitForSingleObject(h, 0) == 0x00000102  # WAIT_TIMEOUT: still running
    finally:
        ctypes.windll.kernel32.CloseHandle(h)


def read(editor_pid=None, editor_start_unix=None):
    """{"freeCommitGB", "commitLimitGB", "editorPrivateGB"|None, "editorUptimeHours"|None}, or None off Windows."""
    if os.name != "nt":
        return None
    st = _MEMORYSTATUSEX()
    st.dwLength = ctypes.sizeof(st)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
        return None
    priv = editor_private_bytes(editor_pid) if editor_pid else None
    return {
        "freeCommitGB": round(st.ullAvailPageFile / GB, 1),
        "commitLimitGB": round(st.ullTotalPageFile / GB, 1),
        "editorPrivateGB": round(priv / GB, 1) if priv is not None else None,
        "editorUptimeHours": round((time.time() - editor_start_unix) / 3600, 1) if editor_start_unix else None,
    }


# ── Verdict ──────────────────────────────────────────────────────────────────────────────────────

def describe(mem):
    if not mem:
        return "memory: not measured"
    s = f"{mem['freeCommitGB']} GB of {mem['commitLimitGB']} GB commit free"
    if mem.get("editorPrivateGB") is not None:
        s += f", editor {mem['editorPrivateGB']} GB private"
    if mem.get("editorUptimeHours") is not None:
        s += f", editor up {mem['editorUptimeHours']} h"
    return s


def assess(mem, cfg=None, light=False, gate_off=None):
    """("ok"|"warn"|"refuse", message). `light` = the Play entry loads no full map scene (test map, data backend).
    An unmeasured state is "ok": the gate never blocks on a reading it does not have."""
    if not mem:
        return "ok", None
    t = thresholds(cfg)
    gate_off = os.environ.get(GATE_OFF_ENV, "").lower() == "off" if gate_off is None else gate_off
    free = mem["freeCommitGB"]
    limit = t["refuseFreeGBLight"] if light else t["refuseFreeGB"]
    reasons = []
    if free < t["warnFreeGB"]:
        reasons.append(f"less than {t['warnFreeGB']:g} GB commit free")
    if (mem.get("editorPrivateGB") or 0) > t["warnEditorGB"]:
        reasons.append(f"editor above {t['warnEditorGB']:g} GB")
    if (mem.get("editorUptimeHours") or 0) > t["warnUptimeHours"]:
        reasons.append(f"editor up more than {t['warnUptimeHours']:g} h")
    if free < limit:
        text = (f"memory too low to enter Play: {describe(mem)} (needs {limit:g} GB). Leaving Play could fail to "
                "read the scene backup, close the editor and lose unsaved scenes. Ask the user to save and restart "
                "Unity (`ujob restart` with their choice for unsaved scenes).")
        if gate_off:
            return "warn", text + f" Not refused: {GATE_OFF_ENV}=off."
        return "refuse", text
    if reasons:
        return "warn", f"memory: {describe(mem)} ({'; '.join(reasons)}). Suggest a save and restart (`ujob restart`) soon."
    return "ok", None


# ── Restart helpers ──────────────────────────────────────────────────────────────────────────────

def wait_exit(pid, timeout):
    end = time.time() + timeout
    while time.time() < end:
        if not process_alive(pid):
            return True
        time.sleep(0.5)
    return not process_alive(pid)


def launch_editor(exe, project):
    """Start the editor detached, so it outlives the broker (which exits after idle minutes)."""
    kwargs = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                  close_fds=True, cwd=project)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("CLAUDE", "UJOB_"))}
    kwargs["env"] = env
    if os.name == "nt":
        kwargs["creationflags"] = 0x00000008 | 0x00000200 | 0x01000000  # DETACHED, NEW_PROCESS_GROUP, BREAKAWAY_FROM_JOB
        try:
            return subprocess.Popen([exe, "-projectPath", project], **kwargs).pid
        except OSError:
            kwargs["creationflags"] = 0x00000008 | 0x00000200  # not allowed to break away from the job
            return subprocess.Popen([exe, "-projectPath", project], **kwargs).pid
    kwargs["start_new_session"] = True
    return subprocess.Popen([exe, "-projectPath", project], **kwargs).pid
