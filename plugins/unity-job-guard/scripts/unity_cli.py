"""Unity CLI layer shared by the `ujob` client and the broker daemon.

Everything that talks to the Unity editor lives here: the CLI wrapper, editor/compile state, and the bounded
waits (ready, detached job, result file). `say` writes to a per-thread sink when one is set, so the daemon can
run several of these at once and hand each requester its own output.
"""
import json
import os
import re
import shutil
import subprocess
import threading
import time

PROJECT = None  # resolved in main()
UNITY = shutil.which("unity") or next(
    (p for p in (os.path.expandvars(r"%LOCALAPPDATA%\Unity\bin\unity.exe"),
                 os.path.expanduser("~/.unity/bin/unity"), "/usr/local/bin/unity")
     if os.path.isfile(p)), None)
SELF = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ujob.py").replace("\\", "/")
POLL = 3
UNREACHABLE_GRACE = 90  # a domain reload of the dev project can take this long
DEFAULT_DEADLINE = 540
# The broker daemon has no console: without this every CLI call would open (and flash) a console window of
# its own, which is slow and can fail outright when several start at once (0xC0000142).
NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def cli(*args, timeout=60):
    """Run the Unity CLI with --json; return (ok, data, error_text)."""
    cmd = [UNITY, *args, "--project-path", PROJECT, "--json", "--no-banner"]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout, stdin=subprocess.DEVNULL,
                           creationflags=NO_WINDOW)
    except subprocess.TimeoutExpired:
        return False, None, f"CLI call timed out after {timeout}s: {' '.join(args[:2])}"
    out = p.stdout.strip()
    try:
        doc = json.loads(out[out.index("{"):]) if "{" in out else None
    except ValueError:
        doc = None
    if doc is None:
        return False, None, (out or p.stderr.strip() or f"exit {p.returncode}")[:2000]
    if not doc.get("success", False):
        errs = doc.get("errors") or []
        text = "; ".join((e.get("message") or e.get("code") or str(e)) if isinstance(e, dict) else str(e)
                         for e in errs) or out
        return False, doc.get("data"), text[:2000]
    return True, doc.get("data"), None


_sink = threading.local()


def say(msg):
    buf = getattr(_sink, "buf", None)
    if buf is not None:
        buf.append(str(msg))
    else:
        print(msg, flush=True)


def capture():
    """Start collecting this thread's `say` output; returns the list that fills up."""
    _sink.buf = []
    return _sink.buf


def release():
    _sink.buf = None


def configure(project):
    global PROJECT
    PROJECT = project


ON_POLL = None  # called on every wait-loop tick (the client uses it to keep its session's activity fresh)


def _tick(seconds):
    if ON_POLL:
        try:
            ON_POLL()
        except Exception:
            pass
    time.sleep(seconds)


def editor_status():
    ok, data, err = cli("command", "editor_status", timeout=20)
    if ok and isinstance(data, dict):
        return data.get("result", data), None
    return None, err


def compile_state():
    ok, data, _ = cli("command", "console_status", timeout=20)
    if ok and isinstance(data, dict):
        gt = (data.get("result") or data).get("groundTruth") or {}
        return gt.get("compiling"), gt.get("compilationFailed")
    return None, None


def wait_ready(deadline_at, quiet=False):
    """Wait until the editor is reachable, idle and not compiling. Returns an exit code or None."""
    first_fail = None
    last_note = 0
    while True:
        st, err = editor_status()
        if st is not None:
            first_fail = None
            # Play Mode reports "playing"/"paused"; the editor still runs jobs then, so treat it as idle.
            busy = (st.get("compiling") or st.get("domainReloadInProgress")
                    or st.get("status") not in (None, "ready", "playing", "paused"))
            if not busy:
                return None
            note = f"editor busy: status={st.get('status')} compiling={st.get('compiling')} reload={st.get('domainReloadInProgress')}"
        else:
            first_fail = first_fail or time.time()
            if time.time() - first_fail > UNREACHABLE_GRACE:
                say(f"NO EDITOR: unreachable for {UNREACHABLE_GRACE}s. Last error: {err}")
                say("Check: is Unity running, is a modal dialog open (save scene? safe mode?), run `unity pipeline list`.")
                return 4
            note = f"editor unreachable ({err[:120] if err else '?'}), retrying"
        if time.time() > deadline_at:
            say(f"DEADLINE: editor still not ready ({note}).")
            return 4
        if not quiet and time.time() - last_note > 30:
            say(note)
            last_note = time.time()
        _tick(POLL)


def wait_job(job_id, deadline_at, started):
    first_fail = None
    last_note = time.time()
    while True:
        ok, data, err = cli("job", "status", job_id, timeout=30)
        if ok and isinstance(data, dict):
            first_fail = None
            state = data.get("state")
            if state in ("completed", "failed", "cancelled", "canceled"):
                return report(job_id, data, started)
            if time.time() - last_note > 60:
                prog = data.get("progress")
                say(f"[{int(time.time() - started)}s] job {state}" + (f" progress={prog}" if prog else ""))
                last_note = time.time()
        elif err and ("Not Found" in err or "not found" in err or "No job" in err):
            say(f"JOB LOST after {int(time.time() - started)}s: {err}")
            say("The job did not finish. Usual cause: a recompile, Play Mode entry/exit or scene reload during the run. "
                f'Run `python "{SELF}" ready`, then start it again.')
            return 2
        else:
            first_fail = first_fail or time.time()
            if time.time() - first_fail > UNREACHABLE_GRACE:
                say(f"NO EDITOR: job status unreachable for {UNREACHABLE_GRACE}s. Last error: {err}")
                return 4
        if time.time() > deadline_at:
            say(f"DEADLINE after {int(time.time() - started)}s: job {job_id} is still running.")
            say(f'Continue with: python "{SELF}" wait {job_id}')
            return 3
        _tick(POLL)


def wait_file(done, hb, stale, deadline_at, started):
    """Wait for `done` while checking that the deferred work is still alive."""
    say(f"waiting for {done}" + (f" (heartbeat {hb}, stale after {stale}s)" if hb else ""))
    first_fail = None
    last_status = 0
    while True:
        now = time.time()
        if os.path.isfile(done):
            time.sleep(0.5)  # let the writer finish
            with open(done, encoding="utf-8", errors="replace") as f:
                text = f.read()
            say(text[:8000])
            failed = text.lstrip().upper().startswith("ERROR")
            say(f"{'FILE REPORTS ERROR' if failed else 'DONE'} after {int(now - started)}s")
            return 1 if failed else 0
        if hb:
            if os.path.isfile(hb):
                age = now - os.path.getmtime(hb)
                if age > stale:
                    say(f"JOB LOST: heartbeat {hb} last updated {int(age)}s ago; the callback stopped "
                        "(domain reload, Play Mode change, exception or scene switch). It will not finish.")
                    return 2
            elif now - started > stale:
                say(f"JOB LOST: no heartbeat at {hb} after {stale}s; the deferred work never started.")
                return 2
        if now - last_status > 5:
            last_status = now
            st, err = editor_status()
            if st is not None:
                first_fail = None
                if st.get("domainReloadInProgress") or st.get("compiling"):
                    say("JOB LOST: the editor started a recompile/domain reload; deferred callbacks are dropped. "
                        "Run `ready`, then start the work again.")
                    return 2
            else:
                first_fail = first_fail or now
                if now - first_fail > UNREACHABLE_GRACE:
                    say(f"NO EDITOR: unreachable for {UNREACHABLE_GRACE}s while waiting. Last error: {err}")
                    return 4
        if now > deadline_at:
            say(f"DEADLINE after {int(now - started)}s: {done} not written yet"
                + (" (heartbeat still fresh, so it is probably still running)" if hb else "")
                + ". Rerun the same waitfile to keep waiting.")
            return 3
        _tick(2)


def job_dirs(root, recent):
    """Folders under `root` (3 levels) holding a heartbeat touched in the last `recent` seconds."""
    found, now = [], time.time()
    base = root.rstrip("/\\").count(os.sep)
    for dirpath, dirnames, filenames in os.walk(root):
        if dirpath.count(os.sep) - base >= 3:
            dirnames[:] = []
        if "heartbeat" in filenames:
            hb = os.path.join(dirpath, "heartbeat")
            done = os.path.join(dirpath, "DONE")
            stamp = max(os.path.getmtime(hb), os.path.getmtime(done) if os.path.isfile(done) else 0)
            if now - stamp < recent:
                found.append(dirpath)
    return found


def stale_watch(root, interval, stale, idle):
    """Emit one line per staleness event (Monitor notifications); exit once nothing has run for `idle` s."""
    def emit(msg):
        print(msg, flush=True)

    started = time.time()
    reported, finished = set(), set()
    last_activity = started
    unreachable_since, reload_reported = None, False
    emit(f"WATCHING {root} every {interval}s (stale {stale}s, idle {idle}s)")
    while True:
        now = time.time()
        running = 0
        for d in job_dirs(root, 900):  # older folders are past runs, not this watch's business
            hb, done = os.path.join(d, "heartbeat"), os.path.join(d, "DONE")
            if os.path.isfile(done):
                last_activity = max(last_activity, os.path.getmtime(done))
                if d not in finished and os.path.getmtime(done) >= started - interval:
                    finished.add(d)
                    with open(done, encoding="utf-8", errors="replace") as f:
                        first = (f.readline() or "").strip()
                    emit(f"FINISHED {d}: {first[:240]}")
                continue
            age = now - os.path.getmtime(hb)
            if age > stale:
                if d not in reported:
                    reported.add(d)
                    emit(f"STALE {d}: heartbeat {int(age)}s old and no DONE; the job died. "
                         "Check the editor (`ujob ready`) and rerun it.")
            else:
                running += 1
                last_activity = now
        st, err = editor_status()
        if st is None:
            unreachable_since = unreachable_since or now
            if now - unreachable_since > UNREACHABLE_GRACE:
                emit(f"EDITOR unreachable for {int(now - unreachable_since)}s: {err}")
                unreachable_since = now  # repeat at most every grace period
        else:
            unreachable_since = None
            busy = st.get("domainReloadInProgress") or st.get("compiling")
            if busy and not reload_reported:
                emit("EDITOR started a recompile/domain reload: deferred jobs are dropped.")
            reload_reported = bool(busy)
            if busy:
                last_activity = now
        if running == 0 and now - last_activity > idle:
            emit(f"IDLE {int(now - last_activity)}s: no Unity job is running. If a background task should "
                 "have started the next job, it has stalled: check it, stop it and continue by hand.")
            return 0
        time.sleep(interval)


def report(job_id, data, started):
    res = data.get("result") or {}
    secs = int(time.time() - started)
    if data.get("state") == "completed" and isinstance(res, dict) and res.get("success", True) and not data.get("error"):
        if res.get("output"):
            say(f"output: {res['output']}")
        say(json.dumps(res.get("result", res), ensure_ascii=False, indent=2) if not isinstance(res.get("result"), str)
            else res["result"])
        say(f"DONE in {secs}s (job {job_id})")
        return 0
    say(f"JOB FAILED after {secs}s (state={data.get('state')}):")
    detail = {k: v for k, v in (res if isinstance(res, dict) else {}).items()
              if k in ("error", "errorDetails", "message", "diagnostics", "output") and v}
    if data.get("error"):
        detail["jobError"] = data.get("error")
        detail["jobErrorDetails"] = data.get("errorDetails")
    say(json.dumps(detail or res, ensure_ascii=False, indent=2)[:6000])
    return 1


# ── Higher-level operations shared by the client (direct mode) and the broker daemon ─────────────

def quick_eval(code, timeout=60):
    """A short synchronous eval (main-thread limit ~30 s). Returns (ok, result, error_text)."""
    ok, data, err = cli("command", "eval", "--code", code, timeout=timeout)
    if not ok:
        return False, None, err
    res = (data or {}).get("result") or {}
    if isinstance(res, dict) and res.get("success") is False:
        return False, None, json.dumps(res.get("diagnostics") or res, ensure_ascii=False)[:2000]
    return True, res.get("result") if isinstance(res, dict) else res, None


def quick_json(code, timeout=60):
    """quick_eval for snippets that return a JSON string; returns (dict|None, error_text)."""
    ok, res, err = quick_eval(code, timeout)
    if not ok:
        return None, err
    try:
        return (json.loads(res) if isinstance(res, str) else res), None
    except ValueError:
        return None, f"not JSON: {str(res)[:300]}"


def start_eval(code=None, file=None):
    """Launch a detached eval job; returns (job_id, error_text)."""
    launch = (("command", "eval", "--code", code, "--detach") if code is not None
              else ("command", "eval_file", "--file", file, "--detach"))
    ok, data, err = cli(*launch, timeout=60)
    job_id = isinstance(data, dict) and (data.get("jobId") or (data.get("result") or {}).get("jobId"))
    if not ok or not job_id:
        return None, err or str(data)
    return job_id, None


def run_eval(code=None, file=None, deadline_at=None, started=None):
    """`ujob run`: wait for the editor, refuse on compile errors, run detached, wait. Returns an exit code."""
    started = started or time.time()
    deadline_at = deadline_at or started + DEFAULT_DEADLINE
    rc = wait_ready(deadline_at, quiet=True)
    if rc is not None:
        return rc
    _, failed = compile_state()
    if failed:
        say("COMPILE ERRORS in the project: not starting the job (it would run stale code or fail). "
            f'Fix them, then `python "{SELF}" compile`.')
        return 5
    job_id, err = start_eval(code, file)
    if not job_id:
        say(f"LAUNCH FAILED: {err}")
        if err and re.search(r"unknown option|--detach", err, re.I):
            say("This Unity CLI has no detached jobs. Update the Unity CLI and the com.unity.pipeline package.")
        return 1
    say(f"job {job_id} started")
    return wait_job(job_id, deadline_at, started)


REFRESH_AND_COMPILE = ("UnityEditor.AssetDatabase.Refresh(); "
                       "UnityEditor.Compilation.CompilationPipeline.RequestScriptCompilation(); "
                       "return \"refreshed\";")


def compile_project(deadline_at=None, started=None):
    """Refresh assets explicitly (auto refresh may be off), request a compile, wait it out, report errors."""
    started = started or time.time()
    deadline_at = deadline_at or started + DEFAULT_DEADLINE
    rc = wait_ready(deadline_at, quiet=True)
    if rc is not None:
        return rc
    ok, _, err = quick_eval(REFRESH_AND_COMPILE, timeout=120)
    if not ok:
        say(f"refresh request: {err} (falling back to the CLI recompile)")
        ok, _, err = cli("command", "recompile", "--timeout", "30", timeout=60)
        if not ok:
            say(f"recompile request: {err} (continuing to watch compile state)")
    time.sleep(2)
    rc = wait_ready(deadline_at)
    if rc is not None:
        return rc
    _, failed = compile_state()
    if failed:
        _, data, _ = cli("command", "recompile_status", timeout=30)
        errors = ((data or {}).get("result") or data or {}).get("errors") or []
        say("COMPILE FAILED:")
        say("\n".join(errors[:30]) if errors else "(no compiler messages returned; check `unity command console --level error`)")
        return 5
    say(f"COMPILED OK in {int(time.time() - started)}s")
    return 0


def eval_result(code, deadline_at, started=None):
    """Run `code` as a detached job (no main-thread time limit) and return (rc, result, text): the snippet's
    return value on success. Used for agent calls that may open scenes or enter Play (AITestAgent.Boot...)."""
    started = started or time.time()
    rc = wait_ready(deadline_at, quiet=True)
    if rc is not None:
        return rc, None, ""
    job_id, err = start_eval(code)
    if not job_id:
        return 1, None, f"LAUNCH FAILED: {err}"
    first_fail = None
    while True:
        ok, data, err = cli("job", "status", job_id, timeout=30)
        if ok and isinstance(data, dict):
            first_fail = None
            state = data.get("state")
            if state in ("completed", "failed", "cancelled", "canceled"):
                res = data.get("result") or {}
                if state == "completed" and isinstance(res, dict) and res.get("success", True) and not data.get("error"):
                    return 0, res.get("result"), ""
                detail = {k: v for k, v in (res if isinstance(res, dict) else {}).items()
                          if k in ("error", "errorDetails", "message", "diagnostics", "output") and v}
                if data.get("error"):
                    detail["jobError"] = data.get("error")
                return 1, None, json.dumps(detail or res, ensure_ascii=False)[:4000]
        elif err and ("Not Found" in err or "not found" in err or "No job" in err):
            return 2, None, f"JOB LOST: {err}"
        else:
            first_fail = first_fail or time.time()
            if time.time() - first_fail > UNREACHABLE_GRACE:
                return 4, None, f"NO EDITOR: job status unreachable for {UNREACHABLE_GRACE}s. Last error: {err}"
        if time.time() > deadline_at:
            return 3, None, f"DEADLINE: job {job_id} still running"
        _tick(POLL)
