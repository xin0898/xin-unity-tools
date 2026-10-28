"""Run Unity editor work through the Unity CLI and always come back with a definite answer.

Several sessions share one editor, so editor work goes through the broker (a per-project daemon that starts
on demand): it decides when Unity compiles, switches scenes or enters Play Mode, batches AI tests, and hands
each requester its own result. Never write `until [ -f result ]; do sleep; done` against Unity.

Editor work (through the broker):
  ujob run  FILE.cs | -e "CODE" [--read]   eval as a detached job and wait. --read promises the eval changes
                                           nothing: it then runs at once, even during another session's lease
                                           or batch (a whole eval `return X.Status();` counts as read on its own)
  ujob compile [--force]                   refresh assets, compile, report errors (coalesced across sessions;
                                           --force skips the offline gate)
  ujob test SPEC [--fixture] [--session S] [--visuals] [--map test|official]
                                           AI Unit Testing: SPEC is a tag, "*", a script .json or (--fixture) a C#
                                           fixture name. Each test runs on the map it declares ("any" = test map;
                                           --map forces one). Tests of several sessions share one Play session.
  ujob lease play [MODE] [--scene official|test|PATH] [--director | --aitest [--session S] [--visuals]]
  ujob lease edit [--why TEXT]             hold the editor for hands-on work (captures, perf windows, debugging,
  ujob lease release                       long edit jobs). Nobody else's mutating work runs until you release it.
  ujob import PATH...                      import non-script assets (automatic refresh is off)
  ujob wait T-TICKET                       keep waiting on a broker request that hit its deadline

Waits and checks (direct, no broker):
  ujob wait JOB_ID [--deadline S]          keep waiting on a Unity CLI job
  ujob ready [--deadline S]                wait until the editor is idle (after Play Mode / reload)
  ujob waitfile DONE [--heartbeat HB] [--stale S] [--deadline S]
        wait for a result file written by multi-frame work. Exits 2 as soon as a domain reload is seen or the
        heartbeat stops updating (default 20 s). A DONE file whose first line starts with ERROR exits 1.
  ujob stalewatch [--root DIR] [--interval 60] [--stale 60] [--idle 180]
        staleness watchdog for background Unity work; run it through the Monitor tool.
  ujob offline-compile [--all] [--assembly NAME] [--overlay DIR] [--keep]
  ujob offline-test [--filter TEXT] [--timeout S] [--all] [--overlay DIR]
        compile / run pure Edit Mode tests WITHOUT the editor (parallel-safe). Details: offline_compile.py.

Coordination:
  ujob status [--json]                     sessions, lease holder and idle time, queue, running work, editor
                                           state from the probe, broken files, pause state
  ujob register --role ROLE --brief TEXT   say what this session does (coordination|implementation|testing)
  ujob broker start|stop|pause|resume|log|restore-scene|setup [--restore]|release --force|session-end [SESSION]

Options: --project PATH (default: UNITY_PROJECT_PATH, else the Unity project containing the current directory,
else CLAUDE_PROJECT_DIR), --as NAME (identity for agents without a Claude session id, e.g. Codex), --deadline S,
--blocked-wait S (how long a blocked request waits before giving up with exit 6; default 120).
UJOB_DIRECT=1 bypasses the broker (emergencies; the daemon itself uses it).

Exit codes (every path ends in one of these; nothing waits silently):
  0 done        the job finished and returned success
  1 job failed  the snippet threw or did not compile, or tests failed; the error is printed
  2 job lost    a domain reload, Play Mode entry or editor restart dropped the job; it did NOT finish
  3 deadline    still running when the deadline hit; rerun `wait JOB_ID` / `wait T-TICKET` (the job keeps going)
  4 no editor   the editor stayed unreachable (crashed, modal dialog, stuck reload) or no Unity CLI
  5 compile     compile errors (in your files, when other sessions are active); the job was not started
  6 not started blocked, paused or cancelled: the reason is printed (lease holder, busy runner, unsaved scenes)
  7 stale build the work ran on the last good build, which lacks your listed changes: NOT validated
 64 usage       bad arguments or no Unity project found

The default deadline (540 s) stays under the Bash tool's 600 s limit.
Needs the Unity CLI (`unity`) with detached-job support and the com.unity.pipeline package in the project.
"""
import json
import os
import re
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import broker_common as bc  # noqa: E402
import unity_cli as uc  # noqa: E402

SELF = uc.SELF
DEFAULT_DEADLINE = uc.DEFAULT_DEADLINE
PLUGIN = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROBE_PACKAGE = "com.xin.unity-broker-probe"
PROBE_CONFIG_TEMPLATE = {
    "busy": [],
    "bootKeys": [],
    "broker": {"maps": {}, "defaultMap": "test",
               "stopPlay": "UnityEditor.EditorApplication.ExitPlaymode(); return \"{\\\"ok\\\":true}\";"},
}


def say(msg):
    print(msg, flush=True)


def take_opt(args, name, default=None):
    if name in args:
        i = args.index(name)
        val = args[i + 1]
        del args[i:i + 2]
        return val
    return default


def take_flag(args, name):
    if name in args:
        args.remove(name)
        return True
    return False


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


# ── Talking to the broker ────────────────────────────────────────────────────────────────────────

class Client:
    def __init__(self, project, ident, act):
        self.project, self.ident, self.act = project, ident, act
        self.sdir = bc.state_dir(project)
        self.blocked_wait = None

    def request(self, op, payload, deadline_at):
        if self.blocked_wait is not None:
            payload["blocked_wait"] = self.blocked_wait
        if not bc.ensure_daemon(self.project, self.sdir):
            say("NO BROKER: the broker daemon did not start (see its log: `ujob broker log`). "
                "Emergency bypass: UJOB_DIRECT=1.")
            return 4
        rid, attached = bc.submit(self.sdir, self.ident, op, payload)
        if attached:
            say(f"[broker] attached to the identical request {rid} that is still in flight")
        return self.await_response(rid, deadline_at)

    def await_response(self, rid, deadline_at):
        path = bc.response_path(self.sdir, rid)
        last_note, last_alive, started = None, 0, time.time()
        while True:
            if os.path.isfile(path):
                doc = bc.read_json(path)
                if doc is not None:
                    bc.remove(path)
                    out = doc.get("output") or ""
                    if out:
                        say(out)
                    return int(doc.get("rc", 1))
            now = time.time()
            self.act.beat()
            if now - last_alive > 2:
                last_alive = now
                if not bc.daemon_alive(self.sdir):
                    say("[broker] daemon not running; starting it again (your request stays queued)")
                    bc.ensure_daemon(self.project, self.sdir)
            note = self.position(rid)
            if note and note != last_note:
                say(f"[broker] {note}")
                last_note = note
            if now > deadline_at:
                bc.mark_keep(self.sdir, rid)
                say(f"DEADLINE after {int(now - started)}s: request {rid} is {last_note or 'still queued'}.")
                say(f"It keeps its place. Continue with: ujob wait T-{rid}")
                return 3
            time.sleep(0.1 if now - started < 3 else 0.25)

    def position(self, rid):
        st = bc.read_json(os.path.join(self.sdir, "state.json"), {}) or {}
        run = st.get("running") or {}
        if rid in (run.get("ids") or []):
            return f"running: {run.get('op')}"
        if any(r.get("id") == rid for r in st.get("reads") or []):
            return "running (read lane)"
        for i, r in enumerate(st.get("pending") or []):
            if r.get("id") == rid:
                return f"queued #{i + 1}" + (f" — {r['waitingFor']}" if r.get("waitingFor") else "")
        return None


# ── Status ───────────────────────────────────────────────────────────────────────────────────────

def ago(t):
    if not t:
        return "never"
    s = int(time.time() - t)
    return f"{s}s ago" if s < 120 else f"{s // 60} min ago" if s < 7200 else f"{s // 3600} h ago"


def status(project, as_json):
    sdir = bc.state_dir(project)
    st = bc.read_json(os.path.join(sdir, "state.json"), {}) or {}
    alive = bc.daemon_alive(sdir)
    reg = bc.registry(sdir)
    act = bc.session_activity(sdir)
    probe = bc.read_json(os.path.join(project, "Library", "UnityBrokerProbe", "state.json"))
    if as_json:
        say(json.dumps({"daemon": alive, "state": st, "sessions": reg, "activity": act, "probe": probe},
                       ensure_ascii=False, indent=1))
        return 0
    say(f"broker: {'running' if alive else 'not running (starts on the next request)'}"
        + (f", pid {st.get('daemonPid')}, up {ago(st.get('started')).replace(' ago', '')}" if alive else "")
        + (", PAUSED" if st.get("paused") or os.path.isfile(os.path.join(sdir, "paused")) else ""))
    lease = st.get("lease") if alive else bc.read_json(os.path.join(sdir, "lease.json"))
    if lease:
        r = reg.get(lease["session"], {})
        idle = lease.get("idleSeconds")
        say(f"LEASE: {lease['session'][:8]} ({r.get('role', '?')}: {r.get('brief') or 'no brief'}) holds the editor "
            f"[{lease.get('kind')}{', implicit' if lease.get('implicit') else ''}] since {ago(lease.get('since'))}"
            + (f", idle {idle // 60} min" if idle is not None else "")
            + ("  ** idle warning: released at 30 min **" if lease.get("warn") else ""))
    else:
        say("lease: none")
    run = st.get("running")
    if run and alive:
        say(f"running: {run.get('op')} for {', '.join(s[:8] for s in run.get('sessions') or [])}")
    for r in (st.get("pending") or []) if alive else []:
        say(f"  queued {r['id']} {r['op']} {r['session'][:8]} {ago(r.get('since'))}"
            + (f" — {r['waitingFor']}" if r.get("waitingFor") else ""))
    if st.get("brokenFiles"):
        say(f"BROKEN BUILD since {ago(st.get('brokenSince'))}: {', '.join(st['brokenFiles'][:10])} "
            "(sessions without broken files run on the last good build)")
    if st.get("priorScene"):
        say(f"scene before the last batch: {st['priorScene']} (`ujob broker restore-scene`)")
    say("sessions:")
    for s, r in sorted(reg.items(), key=lambda kv: -(act.get(bc.safe_name(kv[0]), {}).get("last") or 0)):
        if r.get("endedAt"):
            continue
        a = act.get(bc.safe_name(s), {})
        say(f"  {s[:8]} {r.get('role', '?'):<14} {(r.get('brief') or '-')[:60]:<60} last call {ago(a.get('last'))}"
            + (f", {a['running']} running" if a.get("running") else ""))
    if probe:
        fresh = time.time() - probe.get("writtenUnix", 0) < 15
        busy = [b["name"] for b in probe.get("busy") or [] if b.get("busy")]
        dirty = [x["path"] or "(untitled)" for x in probe.get("scenes") or [] if x.get("dirty")]
        lc = probe.get("lastCompile") or {}
        say(f"editor: {'Play' if probe.get('playing') else 'Edit'} Mode, scene {probe.get('activeScene')}"
            + (f", UNSAVED: {', '.join(dirty)}" if dirty else "")
            + (f", busy: {', '.join(busy)}" if busy else "")
            + (f", last compile {'OK' if lc.get('succeeded') else 'FAILED'} {ago(lc.get('finishedUnix'))}" if lc.get("finishedUnix") else "")
            + (f", auto refresh {'OFF' if probe.get('autoRefreshGuard') else 'on'}")
            + ("" if fresh else f"  (probe last wrote {ago(probe.get('writtenUnix'))}: editor busy, closed or a modal dialog)"))
        for prob in probe.get("problems") or []:
            say(f"  probe: {prob}")
    else:
        say("editor: no probe state (run `ujob broker setup`, then `ujob compile`)")
    return 0


# ── Setup ────────────────────────────────────────────────────────────────────────────────────────

def setup(project, client, restore, deadline_at):
    sdir = client.sdir
    backup = os.path.join(sdir, "prefs_backup.json")
    if restore:
        old = (bc.read_json(backup, {}) or {}).get("ScriptCompilationDuringPlay", 0)
        code = (f'UnityEditor.EditorPrefs.SetInt("ScriptCompilationDuringPlay", {int(old)}); '
                'var t = System.Type.GetType("Xin.UnityBroker.UnityBrokerProbe, Xin.UnityBrokerProbe.Editor"); '
                'var m = t == null ? null : t.GetMethod("SetAutoRefreshGuard"); '
                'return "pref restored" + (m == null ? "" : "; " + m.Invoke(null, new object[] { false }));')
        return client.request("eval", {"code": code}, deadline_at)
    src = os.path.join(PLUGIN, "unity", PROBE_PACKAGE)
    dst = os.path.join(project, "Packages", PROBE_PACKAGE)
    copied = []
    for d, _, files in os.walk(src):
        for fn in files:
            rel = os.path.relpath(os.path.join(d, fn), src)
            target = os.path.join(dst, rel)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(os.path.join(d, fn), "rb") as a:
                data = a.read()
            old = open(target, "rb").read() if os.path.isfile(target) else None
            if old != data:
                with open(target, "wb") as b:
                    b.write(data)
                copied.append(rel.replace("\\", "/"))
    say(f"probe package {'updated: ' + ', '.join(copied) if copied else 'already up to date'} ({dst})")
    cfg = os.path.join(project, "ProjectSettings", "UnityBrokerProbe.json")
    if not os.path.isfile(cfg):
        bc.write_json(cfg, PROBE_CONFIG_TEMPLATE)
        say(f"wrote a config template: {cfg} (add busy flags, boot keys and map scenes)")
    code = ('var old = UnityEditor.EditorPrefs.GetInt("ScriptCompilationDuringPlay", 0); '
            'UnityEditor.EditorPrefs.SetInt("ScriptCompilationDuringPlay", 1); return old.ToString();')
    ok, res, err = uc.quick_eval(code)
    if ok:
        if not os.path.isfile(backup):
            bc.write_json(backup, {"ScriptCompilationDuringPlay": int(res or 0), "at": time.time()})
        say(f"Script Changes While Playing: Recompile After Finished Playing (was {res}; per user, so all your projects)")
    else:
        say(f"could not set the Play-compile preference: {err}")
    if copied:
        # A new embedded package is only compiled after Package Manager resolves it.
        uc.quick_eval("UnityEditor.PackageManager.Client.Resolve(); return \"resolve requested\";")
        say("Now run `ujob compile --force` so Unity loads the probe.")
    return 0


# ── Main ─────────────────────────────────────────────────────────────────────────────────────────

def main(argv):
    args = list(argv)
    if not args or args[0] in ("-h", "--help", "help"):
        say(__doc__)
        return 64
    deadline = int(take_opt(args, "--deadline", DEFAULT_DEADLINE))
    as_name = take_opt(args, "--as")
    project = resolve_project(take_opt(args, "--project"))
    if not args:
        say(__doc__)
        return 64
    verb = args.pop(0)
    if not project:
        say("NO UNITY PROJECT: run from inside a Unity project, set UNITY_PROJECT_PATH, or pass --project PATH.")
        return 64
    uc.configure(project)
    started = time.time()
    deadline_at = started + deadline

    if verb in ("offline-compile", "offline-test"):
        import offline_compile
        import offline_test
        return (offline_compile if verb == "offline-compile" else offline_test).main(project, args)

    ident = bc.identity(as_name)
    sdir = bc.state_dir(project)
    act = bc.Activity(sdir, ident)
    uc.ON_POLL = act.beat
    try:
        return dispatch(verb, args, project, ident, act, deadline_at, started)
    finally:
        act.close()


def dispatch(verb, args, project, ident, act, deadline_at, started):
    if verb == "status":
        return status(project, take_flag(args, "--json"))

    if verb == "register":
        role, brief = take_opt(args, "--role"), take_opt(args, "--brief")
        if role and role not in ("coordination", "implementation", "testing"):
            say("--role must be coordination, implementation or testing")
            return 64
        rec = bc.register(bc.state_dir(project), ident, role, brief, os.getcwd())
        say(f"registered {rec['session'][:8]} as {rec['role']}: {rec.get('brief') or '(no brief)'}")
        return 0

    if not uc.UNITY:
        say("NO UNITY CLI: `unity` is not on PATH. Install it from https://unity.com (Unity CLI) and reopen the shell.")
        return 4

    if verb == "ready":
        rc = uc.wait_ready(deadline_at)
        if rc is None:
            say(f"READY after {int(time.time() - started)}s")
            return 0
        return rc

    client = Client(project, ident, act)
    bw = take_opt(args, "--blocked-wait")
    client.blocked_wait = int(bw) if bw else None
    if verb == "wait":
        if not args:
            say("wait needs a JOB_ID or T-TICKET")
            return 64
        if args[0].startswith("T-"):
            return client.await_response(args[0][2:], deadline_at)
        return uc.wait_job(args[0], deadline_at, started)

    if verb == "waitfile":
        if not args:
            say("waitfile needs the result file path")
            return 64
        hb = take_opt(args, "--heartbeat")
        stale = int(take_opt(args, "--stale", 20))
        return uc.wait_file(native_path(args[0]), native_path(hb) if hb else None, stale, deadline_at, started)

    if verb == "stalewatch":
        root = native_path(take_opt(args, "--root", os.path.join(project, "TempArtifacts", "Logs")))
        return uc.stale_watch(os.path.abspath(root), int(take_opt(args, "--interval", 60)),
                              int(take_opt(args, "--stale", 60)), int(take_opt(args, "--idle", 180)))

    direct = os.environ.get("UJOB_DIRECT") == "1" or os.environ.get("UJOB_BROKER") == "0"
    bc.register(client.sdir, ident) if not os.path.isfile(
        os.path.join(client.sdir, "registry", bc.safe_name(ident["session"]) + ".json")) else None

    if verb == "run":
        read = take_flag(args, "--read")
        if not args:
            say("run needs FILE.cs or -e CODE")
            return 64
        if args[0] == "-e":
            if len(args) < 2:
                say("run -e needs CODE")
                return 64
            payload = {"code": args[1]}
        else:
            path = os.path.abspath(native_path(args[0]))
            if not os.path.isfile(path):
                say(f"no such file: {path}")
                return 64
            payload = {"file": path}
        if read:
            payload["read"] = True
        if direct:
            return uc.run_eval(payload.get("code"), payload.get("file"), deadline_at, started)
        return client.request("eval", payload, deadline_at)

    if verb == "compile":
        if direct:
            return uc.compile_project(deadline_at, started)
        return client.request("compile", {"force": take_flag(args, "--force")}, deadline_at)

    if verb == "test":
        fixture = take_flag(args, "--fixture")
        visuals = take_flag(args, "--visuals")
        session = take_opt(args, "--session")
        force_map = take_opt(args, "--map")
        if not args:
            say("test needs a SPEC: a tag, \"*\", a script .json, or --fixture NAME")
            return 64
        spec = args[0]
        if spec.lower().endswith(".json") and os.path.isfile(native_path(spec)):
            spec = os.path.relpath(os.path.abspath(native_path(spec)), project).replace("\\", "/")
        payload = {"spec": spec, "fixture": fixture, "visuals": visuals, "session": session}
        if force_map:
            payload["map"] = force_map
        return client.request("test", payload, deadline_at)

    if verb == "lease":
        sub = args.pop(0) if args else ""
        if sub == "play":
            payload = {"mode": args.pop(0) if args and not args[0].startswith("--") else "MapInspection",
                       "scene": take_opt(args, "--scene"), "session": take_opt(args, "--session"),
                       "visuals": take_flag(args, "--visuals"),
                       "variant": "director" if take_flag(args, "--director") else "aitest" if take_flag(args, "--aitest") else None}
            return client.request("lease_play", payload, deadline_at)
        if sub == "edit":
            return client.request("lease_edit", {"why": take_opt(args, "--why", "")}, deadline_at)
        if sub == "release":
            return client.request("lease_release", {}, deadline_at)
        say("lease play|edit|release")
        return 64

    if verb == "import":
        if not args:
            say("import needs asset paths (project-relative, e.g. Assets/Art/foo.png)")
            return 64
        paths = [os.path.relpath(os.path.abspath(native_path(p)), project).replace("\\", "/") if os.path.isabs(native_path(p)) else p.replace("\\", "/")
                 for p in args]
        return client.request("import", {"paths": paths}, deadline_at)

    if verb == "broker":
        sub = args.pop(0) if args else ""
        if sub == "start":
            ok = bc.ensure_daemon(project, client.sdir)
            say("broker running" if ok else "broker did not start (see `ujob broker log`)")
            return 0 if ok else 4
        if sub == "log":
            p = os.path.join(client.sdir, "broker.log")
            if os.path.isfile(p):
                with open(p, encoding="utf-8", errors="replace") as f:
                    say("".join(f.readlines()[-int(take_opt(args, "--lines", 60)):]))
            return 0
        if sub == "setup":
            return setup(project, client, take_flag(args, "--restore"), deadline_at)
        if sub == "restore-scene":
            return client.request("restore_scene", {}, deadline_at)
        if sub == "release":
            if not take_flag(args, "--force"):
                say("broker release needs --force (it takes the editor lease from its holder)")
                return 64
            return client.request("release_force", {}, deadline_at)
        if sub in ("pause", "resume", "stop"):
            if sub == "stop" and not bc.daemon_alive(client.sdir):
                say("broker not running")
                return 0
            return client.request(sub, {}, deadline_at)
        if sub == "session-end":
            target = args[0] if args else ident["session"]
            if not bc.daemon_alive(client.sdir):
                rec_path = os.path.join(client.sdir, "registry", bc.safe_name(target) + ".json")
                rec = bc.read_json(rec_path)
                if rec:
                    rec["endedAt"] = time.time()
                    bc.write_json(rec_path, rec)
                lease = bc.read_json(os.path.join(client.sdir, "lease.json"))
                if lease and lease.get("session") == target:
                    bc.remove(os.path.join(client.sdir, "lease.json"))
                return 0
            return client.request("session_end", {"session": target}, deadline_at)
        say("broker start|stop|pause|resume|log|restore-scene|setup [--restore]|release --force|session-end [SESSION]")
        return 64

    say(f"unknown verb {verb}\n{__doc__}")
    return 64


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
