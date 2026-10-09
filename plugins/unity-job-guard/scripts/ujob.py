"""Run Unity editor work through the Unity CLI and always come back with a definite answer.

Never write `until [ -f result ]; do sleep; done` against Unity. Use this instead:

  python ujob.py run  FILE.cs   [--deadline S]   eval_file as a detached job, wait for it
  python ujob.py run  -e "CODE" [--deadline S]   same for inline code
  python ujob.py wait JOB_ID    [--deadline S]   keep waiting on a job that hit its deadline
  python ujob.py compile        [--deadline S]   recompile, wait, report compile errors
  python ujob.py ready          [--deadline S]   wait until the editor is idle (after Play Mode / reload)

Options: --project PATH (default: UNITY_PROJECT_PATH, else the Unity project containing the
current directory, else CLAUDE_PROJECT_DIR).

Exit codes (every path ends in one of these; nothing waits silently):
  0 done        the job finished and returned success
  1 job failed  the snippet threw or did not compile; the error is printed
  2 job lost    a domain reload, Play Mode entry or editor restart dropped the job; it did NOT finish
  3 deadline    still running when the deadline hit; rerun `wait JOB_ID` (the job keeps going)
  4 no editor   the editor stayed unreachable (crashed, modal dialog, stuck reload) or no Unity CLI
  5 compile     the project has compile errors, so the job was not started
 64 usage       bad arguments or no Unity project found

The default deadline (540 s) stays under the Bash tool's 600 s limit.
Needs the Unity CLI (`unity`) with detached-job support and the com.unity.pipeline package in the project.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import time

PROJECT = None  # resolved in main()
UNITY = shutil.which("unity") or next(
    (p for p in (os.path.expandvars(r"%LOCALAPPDATA%\Unity\bin\unity.exe"),
                 os.path.expanduser("~/.unity/bin/unity"), "/usr/local/bin/unity")
     if os.path.isfile(p)), None)
SELF = os.path.abspath(__file__).replace("\\", "/")
POLL = 3
UNREACHABLE_GRACE = 90  # a domain reload of the dev project can take this long
DEFAULT_DEADLINE = 540


def cli(*args, timeout=60):
    """Run the Unity CLI with --json; return (ok, data, error_text)."""
    cmd = [UNITY, *args, "--project-path", PROJECT, "--json", "--no-banner"]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout)
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


def say(msg):
    print(msg, flush=True)


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
            busy = st.get("compiling") or st.get("domainReloadInProgress") or st.get("status") not in (None, "ready")
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
        time.sleep(POLL)


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
        time.sleep(POLL)


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


def take_opt(args, name, default=None):
    if name in args:
        i = args.index(name)
        val = args[i + 1]
        del args[i:i + 2]
        return val
    return default


def native_path(p):
    """Turn a Git Bash path (/c/Users/...) into C:/Users/... when running under Windows Python."""
    m = re.match(r"^/([a-zA-Z])(/.*)?$", p or "")
    if os.name == "nt" and m:
        return f"{m.group(1).upper()}:{m.group(2) or '/'}"
    return p


def find_unity_project(start):
    d = os.path.abspath(native_path(start))
    while True:
        if os.path.isfile(os.path.join(d, "ProjectSettings", "ProjectVersion.txt")):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


def resolve_project(explicit):
    for cand in (explicit, os.environ.get("UNITY_PROJECT_PATH")):
        if cand:
            return os.path.abspath(native_path(cand))
    for start in (os.getcwd(), os.environ.get("CLAUDE_PROJECT_DIR")):
        if start:
            found = find_unity_project(start)
            if found:
                return found
    return None


def main(argv):
    global PROJECT
    args = list(argv)
    if not args or args[0] in ("-h", "--help", "help"):
        say(__doc__)
        return 64
    verb = args.pop(0)
    deadline = int(take_opt(args, "--deadline", DEFAULT_DEADLINE))
    PROJECT = resolve_project(take_opt(args, "--project"))
    if not PROJECT:
        say("NO UNITY PROJECT: run from inside a Unity project, set UNITY_PROJECT_PATH, or pass --project PATH.")
        return 64
    if not UNITY:
        say("NO UNITY CLI: `unity` is not on PATH. Install it from https://unity.com (Unity CLI) and reopen the shell.")
        return 4
    started = time.time()
    deadline_at = started + deadline

    if verb == "ready":
        rc = wait_ready(deadline_at)
        if rc is None:
            say(f"READY after {int(time.time() - started)}s")
            return 0
        return rc

    if verb == "wait":
        return wait_job(args[0], deadline_at, started)

    if verb == "compile":
        rc = wait_ready(deadline_at, quiet=True)
        if rc is not None:
            return rc
        ok, data, err = cli("command", "recompile", "--timeout", "30", timeout=60)
        if not ok:
            say(f"recompile request: {err} (continuing to watch compile state)")
        time.sleep(2)
        rc = wait_ready(deadline_at)
        if rc is not None:
            return rc
        compiling, failed = compile_state()
        if failed:
            _, data, _ = cli("command", "recompile_status", timeout=30)
            errors = ((data or {}).get("result") or data or {}).get("errors") or []
            say("COMPILE FAILED:")
            say("\n".join(errors[:30]) if errors else "(no compiler messages returned; check `unity command console --level error`)")
            return 5
        say(f"COMPILED OK in {int(time.time() - started)}s")
        return 0

    if verb == "run":
        if not args:
            say("run needs FILE.cs or -e CODE")
            return 64
        rc = wait_ready(deadline_at, quiet=True)
        if rc is not None:
            return rc
        _, failed = compile_state()
        if failed:
            say("COMPILE ERRORS in the project: not starting the job (it would run stale code or fail). "
                f'Fix them, then `python "{SELF}" compile`.')
            return 5
        if args[0] == "-e":
            launch = ("command", "eval", "--code", args[1], "--detach")
        else:
            path = os.path.abspath(native_path(args[0]))
            if not os.path.isfile(path):
                say(f"no such file: {path}")
                return 64
            launch = ("command", "eval_file", "--file", path, "--detach")
        ok, data, err = cli(*launch, timeout=60)
        job_id = isinstance(data, dict) and (data.get("jobId") or (data.get("result") or {}).get("jobId"))
        if not ok or not job_id:
            say(f"LAUNCH FAILED: {err or data}")
            if err and re.search(r"unknown option|--detach", err, re.I):
                say("This Unity CLI has no detached jobs. Update the Unity CLI and the com.unity.pipeline package.")
            return 1
        say(f"job {job_id} started")
        return wait_job(job_id, deadline_at, started)

    say(f"unknown verb {verb}\n{__doc__}")
    return 64


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
