"""Shared pieces of the Unity broker: state paths, atomic files, process liveness, session identity, activity,
the registry, the daemon lock and launch, request submission, and the eval classifiers.

The broker keeps its state outside the project, per machine and per project:
  %LOCALAPPDATA%/xin-unity-tools/broker/<project-hash>/
    queue/<seq>-<id>.json      requests (written by clients, consumed by the daemon)
    responses/<id>.json        final answers (written once by the daemon, deleted by the client)
    keep/<id>                  the client hit its deadline and will come back with `ujob wait T-<id>`
    inflight/<key>             idempotency: id of the in-flight request with this key
    registry/<session>.json    sessions: role, brief, pid
    activity/<session>.last    mtime = last ujob call of that session
    activity/<session>__<pid>_<start>.act   one per running ujob process (removed on exit)
    owners.jsonl               file -> session hints written by the PostToolUse hook
    state.json                 the daemon's live state (written every second)
    lease.json, paused, records.json, journal.jsonl, broker.log, broker.lock
"""
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))

# Exit codes on top of ujob's 0-5 / 64.
EXIT_NOT_STARTED = 6   # blocked, paused or cancelled: the work did not start
EXIT_STALE = 7         # the work ran, but on the last good build without the listed files: not validated

ACTIVE_WINDOW = 600    # a session with a ujob call in the last 10 min counts as active
BLOCKED_WAIT = 120     # how long a client waits while its request is blocked before giving up with exit 6


# ── Paths and files ──────────────────────────────────────────────────────────────────────────────

def project_key(project):
    norm = os.path.normcase(os.path.abspath(project)).replace("\\", "/").rstrip("/")
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()[:12]


def state_dir(project):
    base = os.environ.get("UJOB_BROKER_HOME") or os.path.join(
        os.environ.get("LOCALAPPDATA") or os.path.expanduser("~/.cache"), "xin-unity-tools", "broker")
    d = os.path.join(base, project_key(project))
    for sub in ("queue", "responses", "keep", "inflight", "registry", "activity"):
        os.makedirs(os.path.join(d, sub), exist_ok=True)
    return d


def retry(fn, attempts=20, delay=0.05):
    """Windows: a reader, the indexer or Defender can hold a file for a moment; retry with backoff."""
    for i in range(attempts):
        try:
            return fn()
        except (PermissionError, OSError) as e:
            if isinstance(e, FileNotFoundError) or i == attempts - 1:
                raise
            time.sleep(delay * (1 + i))


def write_json(path, obj):
    tmp = f"{path}.{os.getpid()}.{uuid.uuid4().hex[:6]}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    retry(lambda: os.replace(tmp, path))


def read_json(path, default=None):
    try:
        def load():
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        return retry(load, attempts=5)
    except (OSError, ValueError):
        return default


def remove(path):
    try:
        retry(lambda: os.remove(path), attempts=10)
    except OSError:
        pass


def touch(path):
    try:
        with open(path, "a", encoding="utf-8"):
            pass
        os.utime(path, None)
    except OSError:
        pass


def safe_name(session):
    return re.sub(r"[^A-Za-z0-9_.-]", "_", session)[:80]


# ── Processes ────────────────────────────────────────────────────────────────────────────────────

def proc_start(pid):
    """Creation time of a process (an opaque number), or None if it does not exist. With the pid it identifies
    the process even after Windows reuses the pid."""
    if not pid:
        return None
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        k = ctypes.windll.kernel32
        h = k.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return None
        try:
            c, e, kt, ut = (wintypes.FILETIME() for _ in range(4))
            if not k.GetProcessTimes(h, ctypes.byref(c), ctypes.byref(e), ctypes.byref(kt), ctypes.byref(ut)):
                return None
            code = wintypes.DWORD()
            if k.GetExitCodeProcess(h, ctypes.byref(code)) and code.value != 259:  # STILL_ACTIVE
                return None
            return (c.dwHighDateTime << 32) | c.dwLowDateTime
        finally:
            k.CloseHandle(h)
    try:
        os.kill(int(pid), 0)
    except OSError:
        return None
    try:
        with open(f"/proc/{int(pid)}/stat") as f:
            return int(f.read().rsplit(")", 1)[1].split()[19])
    except (OSError, ValueError, IndexError):
        return 1


def alive(pid, start):
    now = proc_start(pid)
    return now is not None and (start is None or now == start)


def parent_pid(pid=None):
    if os.name != "nt":
        return os.getppid()
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command",
                              f"(Get-CimInstance Win32_Process -Filter 'ProcessId={pid or os.getpid()}').ParentProcessId"],
                             capture_output=True, text=True, timeout=10).stdout.strip()
        return int(out)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


# ── Identity ─────────────────────────────────────────────────────────────────────────────────────

def identity(as_name=None):
    """Who is calling. Claude Code sessions are identified by CLAUDE_CODE_SESSION_ID (subagents share their
    parent's), other agents pass --as NAME (or UJOB_AS), a person at a terminal is "user"."""
    as_name = as_name or os.environ.get("UJOB_AS")
    if as_name:
        session, kind = "as:" + as_name, "named"
    elif os.environ.get("CLAUDE_CODE_SESSION_ID"):
        session, kind = os.environ["CLAUDE_CODE_SESSION_ID"], "claude"
    else:
        session, kind = "user", "user"
    pid = os.environ.get("CLAUDE_PID") if kind == "claude" else None
    pid = int(pid) if pid and pid.isdigit() else None
    return {"session": session, "kind": kind, "pid": pid, "pidStart": proc_start(pid) if pid else None,
            "client": os.getpid(), "clientStart": proc_start(os.getpid())}


# ── Activity and registry ────────────────────────────────────────────────────────────────────────

class Activity:
    """Marks this ujob process as running for its session (the lease idle clock reads it)."""

    def __init__(self, sdir, ident):
        self.dir = os.path.join(sdir, "activity")
        self.last = os.path.join(self.dir, safe_name(ident["session"]) + ".last")
        self.mine = os.path.join(self.dir, f"{safe_name(ident['session'])}__{ident['client']}_{ident['clientStart']}.act")
        self.next = 0
        try:
            with open(self.mine, "w", encoding="utf-8") as f:
                json.dump({"session": ident["session"], "pid": ident["client"], "start": ident["clientStart"]}, f)
        except OSError:
            pass
        self.beat(force=True)

    def beat(self, force=False):
        if force or time.time() >= self.next:
            touch(self.last)
            touch(self.mine)
            self.next = time.time() + 5

    def close(self):
        touch(self.last)
        remove(self.mine)


def session_activity(sdir):
    """{session: {"last": unix, "running": n}} from the activity folder; dead processes' markers are swept."""
    out = {}
    d = os.path.join(sdir, "activity")
    for fn in os.listdir(d):
        p = os.path.join(d, fn)
        if fn.endswith(".last"):
            out.setdefault(fn[:-5], {"last": 0, "running": 0})["last"] = os.path.getmtime(p)
        elif fn.endswith(".act"):
            info = read_json(p, {}) or {}
            if alive(info.get("pid"), info.get("start")):
                out.setdefault(fn.split("__")[0], {"last": 0, "running": 0})["running"] += 1
            else:
                remove(p)
    return out


def register(sdir, ident, role=None, brief=None, cwd=None):
    path = os.path.join(sdir, "registry", safe_name(ident["session"]) + ".json")
    rec = read_json(path, {}) or {}
    rec.update({"session": ident["session"], "kind": ident["kind"]})
    if ident.get("pid"):
        rec.update({"pid": ident["pid"], "pidStart": ident["pidStart"]})
    rec.setdefault("role", "implementation")
    rec.setdefault("registeredAt", time.time())
    if role:
        rec["role"] = role
    if brief is not None:
        rec["brief"] = brief
    if cwd:
        rec["cwd"] = cwd
    rec.pop("endedAt", None)
    write_json(path, rec)
    return rec


def registry(sdir):
    d = os.path.join(sdir, "registry")
    return {r["session"]: r for r in (read_json(os.path.join(d, fn)) for fn in os.listdir(d) if fn.endswith(".json")) if r}


# ── Daemon lock and launch ───────────────────────────────────────────────────────────────────────

def lock_path(sdir):
    return os.path.join(sdir, "broker.lock")


def try_lock(sdir):
    """Take the daemon lock (an OS lock on an open handle; released by the OS when the process dies).
    Returns the open file on success, None if another process holds it."""
    f = open(lock_path(sdir), "a+")
    try:
        if os.name == "nt":
            import msvcrt
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return f
    except OSError:
        f.close()
        return None


def unlock(f):
    if not f:
        return
    try:
        if os.name == "nt":
            import msvcrt
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass
    f.close()


def daemon_alive(sdir):
    f = try_lock(sdir)
    if f is None:
        return True
    unlock(f)
    return False


def spawn_daemon(project):
    """Start the broker daemon detached from this process tree (it must outlive the Bash call and the session)."""
    exe = sys.executable
    if os.name == "nt":
        w = exe.replace("python.exe", "pythonw.exe")
        exe = w if os.path.isfile(w) else exe
    env = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE")}
    env.pop("UJOB_AS", None)
    kwargs = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                  close_fds=True, env=env, cwd=project)
    if os.name == "nt":
        kwargs["creationflags"] = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen([exe, "-I", os.path.join(HERE, "broker.py"), project], **kwargs)


def ensure_daemon(project, sdir, wait=10.0):
    if daemon_alive(sdir):
        return True
    spawn_daemon(project)
    end = time.time() + wait
    while time.time() < end:
        if daemon_alive(sdir):
            return True
        time.sleep(0.1)
    return False


# ── Requests ─────────────────────────────────────────────────────────────────────────────────────

def request_key(session, op, payload):
    blob = json.dumps({"s": session, "op": op, "p": payload}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]


def submit(sdir, ident, op, payload):
    """Queue a request (or attach to an identical one still in flight). Returns its id."""
    key = request_key(ident["session"], op, payload)
    existing = os.path.join(sdir, "inflight", key)
    if os.path.isfile(existing):
        try:
            with open(existing, encoding="utf-8") as f:
                rid = f.read().strip()
            if rid and not os.path.isfile(os.path.join(sdir, "responses", rid + ".json")):
                return rid, True
        except OSError:
            pass
    rid = uuid.uuid4().hex[:12]
    req = {"id": rid, "op": op, "payload": payload, "key": key, "submitted": time.time(), **ident}
    seq = f"{time.time_ns():020d}"
    tmp = os.path.join(sdir, "queue", f".{rid}.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(req, f, ensure_ascii=False)
    retry(lambda: os.replace(tmp, os.path.join(sdir, "queue", f"{seq}-{rid}.json")))
    return rid, False


def mark_keep(sdir, rid):
    touch(os.path.join(sdir, "keep", rid))


def response_path(sdir, rid):
    return os.path.join(sdir, "responses", rid + ".json")


# ── Eval classifiers ─────────────────────────────────────────────────────────────────────────────

READ_METHODS = ("Status", "Help", "Report", "Fixtures", "Ops", "Get", "Inspect", "Find", "Events", "Diff", "Modes")
_LITERAL = r'(?:"(?:[^"\\]|\\.)*"|-?\d+(?:\.\d+)?f?|true|false|null)'
_READ_RE = re.compile(r'^return\s+[A-Za-z_][A-Za-z0-9_.]*\.(?:' + "|".join(READ_METHODS) + r')\(\s*(?:'
                      + _LITERAL + r'(?:\s*,\s*' + _LITERAL + r')*)?\s*\)\s*;?$')


def strip_comments(code):
    code = re.sub(r"/\*.*?\*/", " ", code, flags=re.S)
    return re.sub(r"//[^\n]*", " ", code)


def is_read_only_eval(code):
    """Conservative: the whole eval is one `return Agent.Method(literal, ...);` with a read-only method name."""
    if code is None:
        return False
    return bool(_READ_RE.match(strip_comments(code).strip()))


PLAY_SCENE_RE = re.compile(
    r"\b\w*Agent\s*\.\s*Boot\s*\(|StrategicPerfAgent\s*\.\s*Run\s*\(|\bEnterPlaymode\s*\(|\bEnsureSceneOpen\s*\("
    r"|\bOpenScene\s*\(|\.Reset\s*\(\s*true\s*\)|\bisPlaying\s*=\s*true|ExecuteMenuItem\s*\(\s*\"Edit/Play\"|\bBeginPlayHop\s*\("
    r"|\bRecordBatch\s*\(|MapVisualAgent\s*\.\s*FullBuild\s*\(")


def enters_play_or_scene(code):
    """Text hint that an eval may start Play, switch scenes or start a long job (ownership is then confirmed
    by what the probe sees)."""
    return bool(code) and bool(PLAY_SCENE_RE.search(strip_comments(code)))
