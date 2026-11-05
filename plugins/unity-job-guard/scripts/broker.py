"""Unity broker daemon: the one place that decides when the shared Unity editor compiles, switches scenes, enters
Play Mode or runs long work, for every session on this machine and project.

    python broker.py <project>          (started detached by `ujob`; exits after 15 idle minutes)

Two lanes:
  main lane   one request at a time: mutating evals, compiles, AI-test batches, leases, imports, scene restore
  read lane   read-only evals (`ujob run --read` or a whole eval of the form `return X.Status();`); they run at
              any time, also during another session's lease or batch and while paused

Rules (see the unity-long-jobs skill and the vault page Implementation/UnityBroker.md):
  - paused: every request that has not started gets exit 6; running work finishes; reads pass
  - lease (explicit `ujob lease`, or implicit when a session's eval starts Play / switches scenes): only the
    holder's main-lane requests run; others wait up to BLOCKED_WAIT s, then exit 6. Released by the holder, by
    its session ending or process dying, after 30 idle minutes (warning at 25), or by `broker release --force`.
  - busy long runner (probe) or unsaved user scenes that an operation would switch away from: blocked
  - compiles coalesce (5 s quiet, 30 s max); with two or more active sessions an offline compile gates them;
    a broken compile gives exit 5 to the sessions whose files are broken and exit 7 (stale build) to others
    that still have their own uncompiled files
  - AI-test requests with the same key (map, session, visuals, backend: data by default) share one Play session
  - memory gate: a request that would enter Play is refused (exit 6) when free commit memory is below the
    threshold (memory_guard.py), and runs with a warning when it is getting low; `ujob restart` restarts the editor
    in the main lane after the caller chose what happens to unsaved scenes
"""
import contextlib
import io
import json
import os
import re
import sys
import threading
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import broker_common as bc  # noqa: E402
import memory_guard as mg  # noqa: E402

IDLE_EXIT = 900
COMPILE_QUIET, COMPILE_MAX = 5.0, 30.0
BATCH_WINDOW = 60.0
IDLE_WARN, IDLE_RELEASE = 25 * 60, 30 * 60
IMPLICIT_GRACE = 5.0
PROBE_STALE = 15.0
RESPONSE_TTL = 3600
MAIN_OPS = {"eval", "compile", "test", "lease_play", "lease_edit", "lease_release", "import", "restore_scene", "restart"}
EXCLUSIVE_OPS = {"compile", "test", "lease_play", "lease_edit", "restore_scene", "restart"}
RESTART_SCENE_MODES = ("ask", "save", "backup", "discard")
ADMIN_OPS = {"pause", "resume", "release_force", "session_end", "stop", "ping"}
COMPILE_INPUT_EXT = (".cs", ".asmdef", ".asmref", ".rsp", ".dll")
SUITES_DIR = "Assets/XinWorkArea/Tests/AITests/Suites"  # named AI-test suites (<name>.json); config key "suitesDir"
STRUCTURAL_EXT = (".asmdef", ".asmref", ".rsp", ".dll")


# ── Project configuration (ProjectSettings/UnityBrokerProbe.json, shared with the probe) ─────────

def load_config(project):
    cfg = bc.read_json(os.path.join(project, "ProjectSettings", "UnityBrokerProbe.json"), {}) or {}
    b = cfg.get("broker") or {}
    return {
        "stopPlay": b.get("stopPlay") or 'UnityEditor.EditorApplication.ExitPlaymode(); return "{\\"ok\\":true}";',
        "maps": b.get("maps") or {},
        "defaultMap": b.get("defaultMap") or "test",
        "aitest": b.get("aitest", True),
        "memory": b.get("memory") or {},
    }


# ── Compile inputs (freshness) ───────────────────────────────────────────────────────────────────

def snapshot(project):
    """path -> [size, mtime_ns] for every compile input: Assets/ and embedded Packages/ sources, assembly
    definitions, response files, DLLs, plus the package manifest and the scripting-define settings."""
    snap = {}

    def add(full):
        try:
            st = os.stat(full)
        except OSError:
            return
        snap[os.path.relpath(full, project).replace("\\", "/")] = [st.st_size, st.st_mtime_ns]

    roots = [os.path.join(project, "Assets")]
    pk = os.path.join(project, "Packages")
    if os.path.isdir(pk):
        roots += [os.path.join(pk, d) for d in os.listdir(pk) if os.path.isdir(os.path.join(pk, d))]
    for root in roots:
        for d, dirs, files in os.walk(root):
            dirs[:] = [x for x in dirs if not x.startswith(".") and not x.endswith("~")]
            for fn in files:
                if fn.endswith(COMPILE_INPUT_EXT):
                    add(os.path.join(d, fn))
    for extra in ("Packages/manifest.json", "ProjectSettings/ProjectSettings.asset"):
        add(os.path.join(project, extra))
    return snap


def diff(a, b):
    """Paths that differ between two snapshots (added, removed or changed)."""
    if a is None or b is None:
        return None
    return sorted(p for p in set(a) | set(b) if a.get(p) != b.get(p))


def structural(paths):
    return any(p.endswith(STRUCTURAL_EXT) or p in ("Packages/manifest.json", "ProjectSettings/ProjectSettings.asset")
               for p in paths or ())


# ── Unity executor (everything that touches the editor; replaced by a fake in the unit tests) ─────

class UnityExecutor:
    def __init__(self, project):
        import unity_cli as uc
        self.uc = uc
        self.project = project
        uc.configure(project)
        uc.POLL = 0.5
        self._cfg, self._cfg_stamp = None, None

    @property
    def cfg(self):
        """ProjectSettings/UnityBrokerProbe.json, reloaded whenever the file changes."""
        path = os.path.join(self.project, "ProjectSettings", "UnityBrokerProbe.json")
        stamp = os.path.getmtime(path) if os.path.isfile(path) else None
        if self._cfg is None or stamp != self._cfg_stamp:
            self._cfg, self._cfg_stamp = load_config(self.project), stamp
        return self._cfg

    def _run(self, fn, *a, **kw):
        buf = self.uc.capture()
        try:
            rc = fn(*a, **kw)
        except Exception:
            buf.append("broker: " + traceback.format_exc(limit=3))
            rc = 4
        finally:
            self.uc.release()
        return rc, "\n".join(buf)

    def probe(self):
        st = bc.read_json(os.path.join(self.project, "Library", "UnityBrokerProbe", "state.json"))
        if not st:
            return None
        st["fresh"] = time.time() - st.get("writtenUnix", 0) < PROBE_STALE
        return st

    def eval(self, code=None, file=None, deadline=3600):
        now = time.time()
        return self._run(self.uc.run_eval, code, file, now + deadline, now)

    def compile(self, deadline=1800):
        now = time.time()
        return self._run(self.uc.compile_project, now + deadline, now)

    def quick(self, code, timeout=120):
        ok, res, err = self.uc.quick_eval(code, timeout)
        return ok, res if ok else err

    def wait_ready(self, deadline=900):
        return self._run(self.uc.wait_ready, time.time() + deadline, True)

    def wait_job_file(self, job_json, deadline=3600):
        """Wait on a job described by an agent's {"ok":true,"result":{"job":...,"wait":"ujob waitfile ..."}}."""
        def find_wait(node):
            if isinstance(node, dict):
                if isinstance(node.get("wait"), str):
                    return node["wait"]
                for v in node.values():
                    w = find_wait(v)
                    if w:
                        return w
            return None
        m = re.search(r'waitfile\s+"([^"]+)"(?:\s+--heartbeat\s+"([^"]+)")?', find_wait(job_json) or "")
        if not m:
            return 1, f"no job to wait on in {json.dumps(job_json)[:400]}"
        now = time.time()
        return self._run(self.uc.wait_file, m.group(1), m.group(2), 60, now + deadline, now)

    def agent_job(self, code, deadline=3600):
        """Run an agent call that returns a job (Boot, Run, Reset...) and wait for its DONE file."""
        rc, res, err = self.uc.eval_result(code, time.time() + 900)
        if rc != 0:
            return rc, f"{code}: {err}", None
        try:
            doc = json.loads(res) if isinstance(res, str) else res
        except ValueError:
            return 1, f"{code}: not JSON: {str(res)[:400]}", None
        if not isinstance(doc, dict):
            return 1, f"{code}: unexpected result {str(res)[:400]}", None
        if not doc.get("ok", False):
            return 1, f"{code}: {json.dumps(doc, ensure_ascii=False)[:1500]}", doc
        rc, text = self.wait_job_file(doc, deadline)
        return rc, text, doc

    def stop_play(self):
        ok, res = self.quick(self.cfg["stopPlay"])
        end = time.time() + 120
        while time.time() < end:
            p = self.probe()
            if p and p.get("fresh") and not p.get("playing"):
                break
            time.sleep(0.5)
        rc, text = self.wait_ready(300)
        return rc, (str(res) + "\n" + text).strip()

    def aitest_maps(self, spec):
        """The maps the tests named by `spec` declare ({"test"}, {"official"}, or both); "any" counts as the default."""
        default = self.cfg["defaultMap"]
        if spec.lower().endswith(".json") and os.path.isfile(os.path.join(self.project, spec)):
            data = bc.read_json(os.path.join(self.project, spec), {})
            items = data if isinstance(data, list) else (data.get("tests") if isinstance(data, dict) and "tests" in data else [data])
            maps = {(t or {}).get("map") or "any" for t in items}
        else:
            doc, err = self.uc.quick_json("return AITestAgent.Fixtures();", timeout=60)
            res = (doc or {}).get("result") or {}
            maps = set()
            for entry in (res.get("scripts") or []) + (res.get("fixtures") or []):
                tags = [str(t).lower() for t in (entry.get("tags") or [])]
                suites = [str(t).lower() for t in (entry.get("suites") or [])]
                if spec in ("*", "all") or entry.get("name") == spec or spec.lower() in tags or spec.lower() in suites:
                    maps.add(entry.get("map") or "any")
        maps = {default if m in (None, "", "any") else m for m in maps}
        return maps or {default}

    def scene_for_map(self, m):
        return self.cfg["maps"].get(m)

    def import_assets(self, paths):
        code = ("var done = new System.Collections.Generic.List<string>(); foreach (var p in new[]{"
                + ",".join(json.dumps(p) for p in paths)
                + "}) { UnityEditor.AssetDatabase.ImportAsset(p, UnityEditor.ImportAssetOptions.ForceUpdate); done.Add(p); }"
                  " return string.Join(\", \", done);")
        ok, res = self.quick(code, timeout=300)
        return (0 if ok else 1), (f"imported: {res}" if ok else f"import failed: {res}")

    def open_scene(self, path):
        code = ('var s = UnityEditor.SceneManagement.EditorSceneManager.OpenScene(' + json.dumps(path)
                + ', UnityEditor.SceneManagement.OpenSceneMode.Single); return s.path;')
        ok, res = self.quick(code, timeout=600)
        return (0 if ok else 1), str(res)

    def offline_gate(self):
        """Run offline-compile on the changed assemblies. Returns (rc, text, broken_files)."""
        import offline_compile as oc
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            try:
                opts, _ = oc.parse_args([])
                rc, _ = oc.build(self.project, opts)
            except Exception:
                print(traceback.format_exc(limit=3))
                rc = 4
        text = out.getvalue()
        files = sorted({m.group(1).split(" [overlay]")[0].replace("\\", "/")
                        for m in re.finditer(r"^(.*?)\(\d+,\d+\): error", text, re.M)})
        return rc, text, files

    # ── memory and restart (memory_guard.py) ──
    def memory(self):
        p = self.probe() or {}
        try:
            return mg.read(p.get("editorPid"), p.get("editorStartUnix"))
        except Exception:
            return None

    def save_open_scenes(self):
        ok, res = self.quick('return UnityEditor.SceneManagement.EditorSceneManager.SaveOpenScenes() ? "saved" : "NOT SAVED";', timeout=600)
        return ok and res == "saved", str(res)

    def backup_dirty_scenes(self, folder):
        """Saves a copy of every dirty loaded scene into `folder` (outside Assets); the scenes on disk are untouched."""
        code = ('var dir = ' + json.dumps(folder.replace("\\", "/")) + '; System.IO.Directory.CreateDirectory(dir); '
                'var done = new System.Collections.Generic.List<string>(); '
                'for (int i = 0; i < UnityEngine.SceneManagement.SceneManager.sceneCount; i++) { '
                'var s = UnityEngine.SceneManagement.SceneManager.GetSceneAt(i); if (!s.isDirty) continue; '
                'var name = string.IsNullOrEmpty(s.path) ? "Untitled_" + i : System.IO.Path.GetFileNameWithoutExtension(s.path); '
                'var dst = dir + "/" + name + ".unity"; '
                'if (!UnityEditor.SceneManagement.EditorSceneManager.SaveScene(s, dst, true)) return "FAILED " + dst; done.Add(dst); } '
                'return string.Join("\\n", done);')
        ok, res = self.quick(code, timeout=900)
        return ok and not str(res).startswith("FAILED"), str(res)

    def editor_image(self, pid):
        return mg.process_image(pid)

    def exit_editor(self):
        """Asks the editor to quit without saving. The call usually returns no answer: the editor is gone."""
        self.quick("UnityEditor.EditorApplication.Exit(0); return \"exiting\";", timeout=30)

    def wait_exit(self, pid, timeout):
        return mg.wait_exit(pid, timeout)

    def launch_editor(self, exe):
        return mg.launch_editor(exe, self.project)

    def wait_restarted(self, old_pid, deadline=900):
        """Waits until a new editor writes the probe and is ready. Returns (rc, text); rc None/0 = ready."""
        end = time.time() + deadline
        while time.time() < end:
            p = self.probe()
            if p and p.get("fresh") and p.get("editorPid") and p.get("editorPid") != old_pid:
                rc, text = self.wait_ready(max(60, end - time.time()))
                return (rc or 0), text
            time.sleep(2)
        return 4, f"no new editor reported in {deadline} s (is a dialog open, or did Unity Hub ask for something?)"


# ── The broker ───────────────────────────────────────────────────────────────────────────────────

class Request:
    def __init__(self, doc):
        self.doc = doc
        self.id = doc["id"]
        self.op = doc["op"]
        self.payload = doc.get("payload") or {}
        self.session = doc.get("session") or "user"
        self.submitted = doc.get("submitted") or time.time()
        self.blocked_since = None
        self.reason = None
        self.warn = None            # a memory warning to print with the answer
        self.attempts = doc.get("attempts", 0)

    @property
    def is_read(self):
        return self.op == "eval" and (self.payload.get("read") or bc.is_read_only_eval(self.payload.get("code")))

    @property
    def enters_play(self):
        return self.op == "eval" and bc.enters_play_or_scene(self.payload.get("code") or self.payload.get("file_text"))

    @property
    def light_play(self):
        """The Play entry loads no full map scene: the test map, or the data backend's sim scene of the test map.
        Anything not clearly light counts as heavy (an eval's Boot may load the open official scene)."""
        if self.op == "test":
            return (self.payload.get("map") or "test") == "test" and bool(self.payload.get("sim"))
        if self.op == "lease_play":
            return self.payload.get("scene") in ("test", "sim-test")
        code = self.payload.get("code") or self.payload.get("file_text") or ""
        if "official" in code:
            return False
        return "SimAgent.Boot(" in code or '"sim-test"' in code or 'scene: "test"' in code


class Broker:
    def __init__(self, project, sdir, ex, clock=time.time, threaded=True):
        self.project, self.sdir, self.ex, self.now = project, sdir, ex, clock
        self.threaded = threaded
        self.lock = threading.RLock()
        self.pending = []
        self.main = None            # (Request or [Requests], detail) running in the main lane
        self.reads = {}             # id -> Request running in the read lane
        self.lease = bc.read_json(os.path.join(sdir, "lease.json"))
        self.paused = os.path.isfile(os.path.join(sdir, "paused"))
        self.records = bc.read_json(os.path.join(sdir, "records.json"), {}) or {}
        self.notices = {}           # session -> one-time notice (lease released while it was away)
        self.batch = None
        self.started = clock()
        self.last_busy = clock()
        self.stop_requested = False
        self.implicit_clear_since = None
        self.log_path = os.path.join(sdir, "broker.log")
        self._recover()

    # ── logging / journal ──
    def log(self, msg):
        line = time.strftime("%Y-%m-%d %H:%M:%S ") + msg
        try:
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass

    def journal(self, **entry):
        entry["t"] = self.now()
        try:
            with open(os.path.join(self.sdir, "journal.jsonl"), "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def _recover(self):
        """Requests that were running when a previous daemon died are requeued once."""
        run_dir = os.path.join(self.sdir, "running")
        os.makedirs(run_dir, exist_ok=True)
        for fn in os.listdir(run_dir):
            doc = bc.read_json(os.path.join(run_dir, fn))
            bc.remove(os.path.join(run_dir, fn))
            if not doc:
                continue
            if doc.get("attempts", 0) >= 1:
                self._write_response(doc["id"], doc.get("key"), 4, "broker restarted twice while this request ran; "
                                     "it may or may not have finished. Check the editor, then run it again.")
            else:
                doc["attempts"] = doc.get("attempts", 0) + 1
                self.pending.append(Request(doc))
                self.log(f"recovered {doc['id']} ({doc['op']}) after a daemon restart")

    # ── persistence ──
    def save_lease(self):
        p = os.path.join(self.sdir, "lease.json")
        if self.lease:
            bc.write_json(p, self.lease)
        else:
            bc.remove(p)

    def save_records(self):
        bc.write_json(os.path.join(self.sdir, "records.json"), self.records)

    # ── responses ──
    def _write_response(self, rid, key, rc, text, extra=None):
        bc.write_json(bc.response_path(self.sdir, rid), {"id": rid, "rc": rc, "output": text, "extra": extra or {},
                                                         "finished": self.now()})
        if key:
            bc.remove(os.path.join(self.sdir, "inflight", key))
        bc.remove(os.path.join(self.sdir, "keep", rid))
        bc.remove(os.path.join(self.sdir, "running", rid + ".json"))

    def respond(self, req, rc, text, extra=None):
        if isinstance(req, _Internal):
            return
        if getattr(req, "warn", None) and rc != bc.EXIT_NOT_STARTED:
            text = f"[broker] WARNING {req.warn}\n{text}"
        self._write_response(req.id, req.doc.get("key"), rc, text, extra)
        self.journal(ev="done", id=req.id, op=req.op, session=req.session, rc=rc)
        self.log(f"done {req.id} {req.op} {req.session[:8]} rc={rc}")

    # ── intake ──
    def ingest(self):
        qdir = os.path.join(self.sdir, "queue")
        for fn in sorted(os.listdir(qdir)):
            if not fn.endswith(".json"):
                continue
            path = os.path.join(qdir, fn)
            doc = bc.read_json(path)
            bc.remove(path)
            if not doc or "id" not in doc:
                continue
            req = Request(doc)
            if req.op == "eval" and req.payload.get("file") and not req.payload.get("file_text"):
                try:
                    with open(req.payload["file"], encoding="utf-8", errors="replace") as f:
                        req.payload["file_text"] = f.read()
                except OSError:
                    pass
            if doc.get("key"):
                try:
                    with open(os.path.join(self.sdir, "inflight", doc["key"]), "w", encoding="utf-8") as f:
                        f.write(req.id)
                except OSError:
                    pass
            self.journal(ev="queued", id=req.id, op=req.op, session=req.session)
            if req.op in ADMIN_OPS:
                self.admin(req)
            elif req.op in MAIN_OPS:
                if req.op == "test":
                    self.apply_suite(req.payload)
                if req.op == "test" and req.payload.get("map") and not req.payload.get("maps"):
                    req.payload["maps"] = [req.payload["map"]]  # --map forces one map
                if req.op == "test" and not req.payload.get("maps"):
                    try:
                        req.payload["maps"] = sorted(self.ex.aitest_maps(req.payload.get("spec") or "*"))
                    except Exception as e:
                        req.payload["maps"] = [self.ex.cfg["defaultMap"]]
                        self.log(f"map lookup for {req.id} failed: {e}")
                    req.payload["map"] = req.payload["maps"][0]
                self.pending.append(req)
            else:
                self.respond(req, 64, f"unknown broker op {req.op}")

    def apply_suite(self, payload):
        """A named suite (SUITES_DIR/<spec>.json in the project) says where it runs: its map, session and backend
        fill in what the request leaves open. The backend is data unless the request or the suite says play."""
        self.read_suite(payload)
        if "backend" in payload:
            payload["sim"] = payload.get("backend") != "play"

    def read_suite(self, payload):
        spec = payload.get("spec") or ""
        if payload.get("fixture") or not spec or spec.lower().endswith(".json"):
            return
        path = os.path.join(self.project, self.ex.cfg.get("suitesDir", SUITES_DIR), spec + ".json")
        suite = bc.read_json(path, None) if os.path.isfile(path) else None
        if not isinstance(suite, dict):
            return
        if suite.get("map") and suite["map"] != "any" and not payload.get("map"):
            payload["map"] = suite["map"]
        if suite.get("session") and not payload.get("session"):
            payload["session"] = suite["session"]
        if suite.get("backend") in ("data", "play") and not payload.get("backend"):
            payload["backend"] = suite["backend"]

    def admin(self, req):
        if req.op == "pause":
            self.paused = True
            bc.touch(os.path.join(self.sdir, "paused"))
            self.respond(req, 0, "broker paused: no new Unity work starts until `ujob broker resume` "
                                 "(running work finishes, read evals still pass)")
        elif req.op == "resume":
            self.paused = False
            bc.remove(os.path.join(self.sdir, "paused"))
            self.respond(req, 0, "broker resumed")
        elif req.op == "release_force":
            held = self.lease
            self.release_lease("force-released by " + req.session)
            self.respond(req, 0, f"released lease of {held['session']}" if held else "no lease was held")
        elif req.op == "session_end":
            target = req.payload.get("session") or req.session
            self.end_session(target)
            self.respond(req, 0, f"session {target} ended: queued requests cancelled, lease released")
        elif req.op == "stop":
            self.stop_requested = True
            self.respond(req, 0, "broker stops after the running work")
        elif req.op == "ping":
            self.respond(req, 0, "pong")

    # ── sessions, leases ──
    def active_sessions(self):
        act = bc.session_activity(self.sdir)
        now = self.now()
        return {s for s, a in act.items() if a["running"] or now - a["last"] < bc.ACTIVE_WINDOW}

    def holder_idle(self):
        if not self.lease:
            return 0
        a = bc.session_activity(self.sdir).get(bc.safe_name(self.lease["session"]))
        if a and a["running"]:
            return 0
        last = max(a["last"] if a else 0, self.lease.get("since", 0), self.lease.get("lastEval", 0))
        return max(0, self.now() - last)

    def release_lease(self, why):
        if not self.lease:
            return
        self.log(f"lease of {self.lease['session'][:8]} released: {why}")
        self.journal(ev="lease_released", session=self.lease["session"], why=why)
        if why.startswith(("idle", "force")):
            self.notices[self.lease["session"]] = f"your editor lease was released ({why})"
        self.lease = None
        self.implicit_clear_since = None
        self.save_lease()

    def take_lease(self, session, kind, implicit, detail=""):
        self.lease = {"session": session, "kind": kind, "implicit": implicit, "since": self.now(),
                      "lastEval": self.now(), "detail": detail}
        self.save_lease()
        self.log(f"lease taken by {session[:8]} ({kind}{', implicit' if implicit else ''}) {detail}")
        self.journal(ev="lease_taken", session=session, kind=kind, implicit=implicit)

    def end_session(self, session):
        for r in [r for r in self.pending if r.session == session]:
            self.pending.remove(r)
            self.respond(r, bc.EXIT_NOT_STARTED, "cancelled: the session ended")
        if self.lease and self.lease["session"] == session:
            self.release_lease("session ended")
        path = os.path.join(self.sdir, "registry", bc.safe_name(session) + ".json")
        rec = bc.read_json(path)
        if rec:
            rec["endedAt"] = self.now()
            bc.write_json(path, rec)

    def housekeeping(self):
        now = self.now()
        reg = bc.registry(self.sdir)
        # Clients that died before their request started (and did not ask to come back) take it with them.
        for r in list(self.pending):
            keep = os.path.isfile(os.path.join(self.sdir, "keep", r.id))
            if not keep and r.doc.get("client") and not bc.alive(r.doc.get("client"), r.doc.get("clientStart")):
                self.pending.remove(r)
                self._write_response(r.id, r.doc.get("key"), bc.EXIT_NOT_STARTED, "cancelled: the client exited")
                self.log(f"cancelled {r.id}: client gone")
        # Ended sessions (SessionEnd hook) lose queued work and their lease.
        for s, rec in reg.items():
            if rec.get("endedAt") and (any(r.session == s for r in self.pending) or (self.lease and self.lease["session"] == s)):
                self.end_session(s)
        if self.lease:
            rec = reg.get(self.lease["session"])
            if rec and rec.get("pid") and not bc.alive(rec.get("pid"), rec.get("pidStart")):
                self.release_lease("holder process exited")
        if self.lease:
            idle = self.holder_idle()
            self.lease["idleSeconds"] = int(idle)
            self.lease["warn"] = idle >= IDLE_WARN
            if idle >= IDLE_RELEASE:
                self.release_lease(f"idle {int(idle // 60)} min")
        if self.lease and self.lease.get("implicit") and not self.main_busy_for(self.lease["session"]):
            p = self.ex.probe()
            clear = p is not None and p.get("fresh") and not p.get("playing") and not p.get("bootJobs")
            if clear:
                self.implicit_clear_since = self.implicit_clear_since or now
                if now - self.implicit_clear_since >= IMPLICIT_GRACE:
                    self.release_lease("implicit: back in Edit Mode")
            else:
                self.implicit_clear_since = None
        # Blocked too long: give up with exit 6 so the agent is not stuck for the whole deadline.
        for r in list(self.pending):
            if r.blocked_since and now - r.blocked_since > r.payload.get("blocked_wait", bc.BLOCKED_WAIT):
                self.pending.remove(r)
                self.respond(r, bc.EXIT_NOT_STARTED, f"not started: {r.reason}. Try again later or check `ujob status`.")
        # Old responses nobody collected.
        rdir = os.path.join(self.sdir, "responses")
        for fn in os.listdir(rdir):
            p = os.path.join(rdir, fn)
            try:
                if now - os.path.getmtime(p) > RESPONSE_TTL:
                    bc.remove(p)
            except OSError:
                pass

    def main_busy_for(self, session):
        if not self.main:
            return False
        reqs = self.main[0] if isinstance(self.main[0], list) else [self.main[0]]
        return any(r.session == session for r in reqs)

    # ── blocking rules ──
    def busy_runners(self, p):
        return [b["name"] for b in (p or {}).get("busy") or [] if b.get("busy")] if p and p.get("fresh") else []

    def user_dirty(self, p, target_scene):
        """Unsaved scenes the operation would have to switch away from (blocked: user)."""
        if not p or not p.get("fresh"):
            return []
        if target_scene and p.get("activeScene") == target_scene and not p.get("prefabStageDirty"):
            return []
        dirty = []
        for s in p.get("scenes") or []:
            if not s.get("dirty"):
                continue
            # The test scene dirtied by a Play exit moments ago is the harness's, not the user's.
            if s["path"] == self.ex.scene_for_map("test") and abs(s.get("dirtySinceUnix", 0) - p.get("playChangedUnix", 0)) < 10:
                continue
            dirty.append(s["path"] or "(untitled scene)")
        if p.get("prefabStageDirty"):
            dirty.append("Prefab Mode: " + (p.get("prefabStagePath") or "?"))
        return dirty

    def block_reason(self, r, p):
        """Why `r` cannot start now (None if it can). Paused/holder-misuse answers are immediate (returns 'NOW:')."""
        if self.paused:
            return "NOW:paused (ujob broker resume)"
        if r.session in self.notices:
            return "NOW:" + self.notices.pop(r.session)
        if self.lease and self.lease["session"] != r.session:
            reg = bc.registry(self.sdir).get(self.lease["session"], {})
            who = f"{self.lease['session'][:8]} ({reg.get('role', '?')}: {reg.get('brief') or 'no brief'})"
            return f"editor leased by {who}, idle {int(self.holder_idle() // 60)} min"
        if self.lease and self.lease["session"] == r.session:
            if r.op == "test":
                return "NOW:you hold an editor lease; release it (`ujob lease release`) or run the tests inside it"
            if r.op == "compile" and p and p.get("playing"):
                return "NOW:you are in Play Mode under your lease; stop Play first (compiles never run during Play)"
            if r.op in ("lease_play", "lease_edit"):
                return "NOW:you already hold the editor lease"
        if r.op in ("test", "lease_play") or r.enters_play:
            level, text = mg.assess(self.ex.memory(), self.ex.cfg.get("memory"), light=r.light_play)
            if level == "refuse":
                return "NOW:" + text
            r.warn = text if level == "warn" else None
        if r.op in EXCLUSIVE_OPS or r.enters_play:
            busy = self.busy_runners(p)
            if busy:
                return "busy: " + ", ".join(busy)
        target = None
        if r.op == "test":
            target = self.ex.scene_for_map(r.payload.get("map") or self.ex.cfg["defaultMap"])
        elif r.op == "lease_play" and r.payload.get("scene"):
            target = self.ex.scene_for_map(r.payload["scene"]) or r.payload["scene"]
        elif r.op == "restore_scene":
            target = self.records.get("priorScene")
        if target:
            dirty = self.user_dirty(p, target)
            if dirty:
                return "blocked: user has unsaved changes in " + ", ".join(dirty)
        return None

    # ── picking ──
    def pick(self):
        p = self.ex.probe()
        now = self.now()
        compiles = [r for r in self.pending if r.op == "compile"]
        for r in list(self.pending):
            if r.is_read:
                continue
            reason = self.block_reason(r, p)
            if reason and reason.startswith("NOW:"):
                self.pending.remove(r)
                self.respond(r, bc.EXIT_NOT_STARTED, "not started: " + reason[4:])
                continue
            if reason:
                if r.reason != reason:
                    r.reason = reason
                r.blocked_since = r.blocked_since or now
                continue
            r.blocked_since, r.reason = None, None
            if r.op == "compile":
                first = min(c.submitted for c in compiles)
                last = max(c.submitted for c in compiles)
                if now - last < COMPILE_QUIET and now - first < COMPILE_MAX:
                    r.reason = "collecting compile requests"
                    continue
                return ("compile", [c for c in compiles if not self.block_reason(c, p)])
            if r.op == "test":
                if compiles:
                    r.reason = "waiting for the pending compile"
                    continue
                key = self.test_key(r)
                group = [t for t in self.pending if t.op == "test" and self.test_key(t) == key and not self.block_reason(t, p)]
                if len(self.active_sessions()) >= 2 and now - min(t.submitted for t in group) < BATCH_WINDOW:
                    r.reason = f"batch window: waiting up to {int(BATCH_WINDOW)} s for other sessions' tests"
                    continue
                return ("test", group)
            return (r.op, [r])
        return None

    def test_key(self, r):
        return (r.payload.get("map") or self.ex.cfg["defaultMap"], r.payload.get("session") or "", bool(r.payload.get("visuals")), bool(r.payload.get("sim")))

    # ── main loop ──
    def step(self):
        with self.lock:
            self.ingest()
            self.housekeeping()
            for r in [r for r in self.pending if r.is_read]:
                self.pending.remove(r)
                self.reads[r.id] = r
                self.spawn(self.run_read, r)
            if self.main is None:
                picked = self.pick()
                if picked:
                    op, reqs = picked
                    for r in reqs:
                        self.pending.remove(r)
                        bc.write_json(os.path.join(self.sdir, "running", r.id + ".json"), r.doc)
                    self.main = (reqs, op)
                    self.journal(ev="start", op=op, ids=[r.id for r in reqs])
                    self.spawn(self.run_main, op, reqs)
            if self.main or self.reads or self.pending or self.lease:
                self.last_busy = self.now()
            self.write_state()

    def spawn(self, fn, *args):
        if self.threaded:
            threading.Thread(target=fn, args=args, daemon=True).start()
        else:
            fn(*args)

    def run_read(self, r):
        try:
            rc, text = self.ex.eval(r.payload.get("code"), r.payload.get("file"))
            self.respond(r, rc, text)
        except Exception:
            self.respond(r, 4, "broker: " + traceback.format_exc(limit=3))
        finally:
            with self.lock:
                self.reads.pop(r.id, None)

    def run_main(self, op, reqs):
        try:
            getattr(self, "do_" + op)(reqs)
        except Exception:
            text = "broker error: " + traceback.format_exc(limit=4)
            self.log(text)
            for r in reqs:
                if not os.path.isfile(bc.response_path(self.sdir, r.id)):
                    self.respond(r, 4, text)
        finally:
            with self.lock:
                self.main = None

    def set_detail(self, detail):
        with self.lock:
            if self.main:
                self.main = (self.main[0], detail)

    # ── stale-build bookkeeping ──
    def owners(self):
        out = {}
        try:
            with open(os.path.join(self.sdir, "owners.jsonl"), encoding="utf-8") as f:
                for line in f:
                    try:
                        o = json.loads(line)
                        out[o["path"]] = o["session"]
                    except (ValueError, KeyError):
                        pass
        except OSError:
            pass
        return out

    def stale_info(self, session):
        """(broken_files_owned_by_session, own_uncompiled_files, broken_all) while the editor is behind a failed or
        gated compile; ([], [], []) when it is not."""
        failed = self.records.get("failed")
        if not failed:
            return [], [], []
        owners = self.owners()
        broken = failed.get("files") or []
        mine = [f for f in broken if owners.get(f) == session or (owners.get(f) is None and session in failed.get("requesters", []))]
        good = self.records.get("good")
        changed = diff(good, snapshot(self.project)) if good else []
        own = [f for f in (changed or []) if owners.get(f) == session]
        return mine, own, broken

    def stale_result(self, r, rc, text):
        mine, own, broken = self.stale_info(r.session)
        if not broken:
            return rc, text
        owners = self.owners()
        note = ("\nSTALE BUILD: the editor runs the last good build; the newest compile failed on "
                + ", ".join(f"{f} ({(owners.get(f) or 'unknown')[:8]})" for f in broken[:10]) + ".")
        if own:
            note += "\nYour own changes NOT in this build: " + ", ".join(own[:20])
            return (bc.EXIT_STALE if rc == 0 else rc), text + note
        return rc, text + note + "\n(None of your own changed files are missing from this build.)"

    # ── operations ──
    def do_eval(self, reqs):
        r = reqs[0]
        mine, _, broken = self.stale_info(r.session)
        if mine:
            self.respond(r, 5, "COMPILE ERRORS in your files: " + ", ".join(mine)
                         + "\nFix them, then `ujob compile`. (Other sessions keep running on the last good build.)")
            return
        p0 = self.ex.probe() or {}
        if r.enters_play:
            self.ensure_no_unowned_play(r)
        rc, text = self.ex.eval(r.payload.get("code"), r.payload.get("file"))
        with self.lock:
            if self.lease and self.lease["session"] == r.session:
                self.lease["lastEval"] = self.now()
                self.save_lease()
        if r.enters_play and not self.lease and rc == 0:
            time.sleep(1.5 if self.threaded else 0)
            p1 = self.ex.probe() or {}
            if p1.get("playing") or p1.get("bootJobs") or p1.get("activeScene") != p0.get("activeScene"):
                with self.lock:
                    self.take_lease(r.session, "play" if (p1.get("playing") or p1.get("bootJobs")) else "edit",
                                    True, (r.payload.get("code") or r.payload.get("file") or "")[:120])
                text += ("\n[broker] you now hold the editor lease (implicit). It is released when you are back in "
                         "Edit Mode, or with `ujob lease release`.")
        rc, text = self.stale_result(r, rc, text)
        self.respond(r, rc, text)

    def ensure_no_unowned_play(self, r):
        """Agents have priority: a Play session nobody leases (started by hand) is stopped before exclusive work."""
        p = self.ex.probe()
        if p and p.get("playing") and not self.lease:
            self.set_detail("stopping a Play session nobody leases")
            self.log(f"stopping unowned Play before {r.op} for {r.session[:8]}")
            self.ex.stop_play()

    def do_compile(self, reqs):
        """Compile for every request in `reqs`; returns rc (0 ok, 5 failed)."""
        self.set_detail("compile")
        sessions = sorted({r.session for r in reqs})
        force = any(r.payload.get("force") for r in reqs)
        now_snap = snapshot(self.project)
        good, failed = self.records.get("good"), self.records.get("failed")
        p = self.ex.probe() or {}
        editor_failed = p.get("compileFailed") if p.get("fresh") else None
        if not force and good is not None and now_snap == good and not editor_failed:
            if self.records.pop("failed", None) is not None:  # the broken changes were reverted
                self.save_records()
            for r in reqs:
                self.respond(r, 0, "COMPILED OK: up to date (nothing changed since the last successful compile)")
            return 0
        if not force and failed and failed.get("snapshot") == now_snap:
            self._compile_failed_responses(reqs, failed, "unchanged since the last failed compile")
            return 5
        if p.get("playing"):
            self.ensure_no_unowned_play(reqs[0])
        changed = diff(good, now_snap)
        gate = len(self.active_sessions()) >= 2 and not force and not structural(changed) and good is not None
        gate_note = ""
        if gate:
            self.set_detail("compile: offline gate")
            grc, gtext, files = self.ex.offline_gate()
            if grc == 5:
                self.records["failed"] = {"snapshot": now_snap, "files": files, "requesters": sessions,
                                          "at": self.now(), "errors": gtext[-4000:], "gated": True}
                self.save_records()
                self._compile_failed_responses(reqs, self.records["failed"], "offline gate (Unity was not asked to compile)")
                return 5
            if grc in (4, 64):
                gate_note = "\n[broker] offline gate skipped: its tooling failed"
                self.journal(ev="gate_skipped", why="tooling", rc=grc)
        elif len(self.active_sessions()) >= 2:
            why = "--force" if force else ("structural change" if structural(changed) else "no last good build on record")
            self.journal(ev="gate_skipped", why=why)
            gate_note = f"\n[broker] offline gate skipped ({why})"
        self.set_detail("compile: Unity")
        rc, text = self.ex.compile()
        if rc == 0:
            self.records["good"] = snapshot(self.project)
            self.records.pop("failed", None)
            self.records["goodAt"] = self.now()
            self.save_records()
            for r in reqs:
                self.respond(r, 0, text + gate_note)
            return 0
        if rc == 5:
            files = sorted({m.group(1).replace("\\", "/") for m in re.finditer(r"((?:Assets|Packages)[/\\][^\s(:]+\.cs)", text)})
            p = self.ex.probe() or {}
            files = files or sorted({e.get("file") for e in (p.get("lastCompile") or {}).get("errors") or [] if e.get("file")})
            self.records["failed"] = {"snapshot": now_snap, "files": files, "requesters": sessions,
                                      "at": self.now(), "errors": text[-4000:], "gated": False}
            self.save_records()
            self._compile_failed_responses(reqs, self.records["failed"], "Unity compile")
            return 5
        for r in reqs:
            self.respond(r, rc, text + gate_note)
        return rc

    def _compile_failed_responses(self, reqs, failed, where):
        owners = self.owners()
        files = failed.get("files") or []
        for r in reqs:
            mine = [f for f in files if owners.get(f) == r.session or owners.get(f) is None]
            listing = ", ".join(f"{f} ({(owners.get(f) or 'unknown')[:8]})" for f in files[:15])
            if mine or not files:
                self.respond(r, 5, f"COMPILE FAILED ({where}). Broken files (probable owner): {listing or '?'}\n"
                                   + (failed.get("errors") or "")[-3000:])
            else:
                self.respond(r, bc.EXIT_STALE, f"NOT COMPILED ({where}): other sessions' files are broken: {listing}. "
                                               "The editor keeps the last good build; your work runs on it (stale) until they are fixed.")

    def do_test(self, reqs):
        key = self.test_key(reqs[0])
        m, session, visuals, sim = key
        self.set_detail(f"test batch {m}")
        # Fresh code first (one coalesced compile), unless it already failed on exactly this code.
        good, failed, now_snap = self.records.get("good"), self.records.get("failed"), snapshot(self.project)
        if good != now_snap and not (failed and failed.get("snapshot") == now_snap):
            self.set_detail("test batch: compiling first")
            with self.lock:
                extra = [r for r in self.pending if r.op == "compile"]
                for r in extra:
                    self.pending.remove(r)
            self.do_compile(extra + [_Internal(reqs[0].session)])
        runnable = []
        for r in reqs:
            mine, _, _ = self.stale_info(r.session)
            if mine:
                self.respond(r, 5, "COMPILE ERRORS in your files: " + ", ".join(mine) + "\nFix them, then `ujob test` again.")
            else:
                runnable.append(r)
        if not runnable:
            return
        p = self.ex.probe() or {}
        target = self.ex.scene_for_map(m)
        if p.get("playing"):
            self.ensure_no_unowned_play(runnable[0])
        if target and p.get("activeScene") and p.get("activeScene") != target:
            with self.lock:
                self.records["priorScene"] = p.get("activeScene")
                self.save_records()
        self.set_detail(f"test batch {m}: booting")
        boot_code = f"return AITestAgent.Boot({str(visuals).lower()}, 900f, {json.dumps(session or None) if session else 'null'}, {json.dumps(m)}, sim: {str(sim).lower()});"
        rc, text, _ = self.ex.agent_job(boot_code)
        if rc != 0:
            for r in runnable:
                self.respond(r, 4 if rc in (2, 3, 4) else 1, "AI test boot failed:\n" + text)
            return
        queue = list(runnable)
        done = 0
        while queue:
            r = queue.pop(0)
            done += 1
            self.set_detail(f"test batch {m}: {done} running ({r.payload.get('spec')})")
            rc, text, doc = self.ex.agent_job(self.run_code(r.payload))
            result = self.read_result(doc)
            if result:
                if result.get("resetLeaks"):
                    self.ex.agent_job("return AITestAgent.Reset(true);")
                    self.ex.wait_ready(300)
                elif any(((t.get("reset") or {}).get("kept")) for t in result.get("tests") or []):
                    self.ex.agent_job("return AITestAgent.Reset();")
            path = ((doc or {}).get("result") or {}).get("result")
            text = text + (f"\nresult: {path}" if path else "")
            rc, text = self.stale_result(r, rc, text)
            maps = r.payload.get("maps") or [m]
            parts = r.payload.setdefault("parts", [])
            parts.append({"map": m, "rc": rc, "text": text, "result": path, "notRun": (result or {}).get("notRun", 0),
                          "total": (result or {}).get("total", 0)})
            later = [x for x in maps if x not in [q["map"] for q in parts]]
            if later:
                # The same request continues on the other map in its own batch; one response at the end.
                r.payload["map"] = later[0]
                r.submitted = self.now() - BATCH_WINDOW  # it already waited its window
                with self.lock:
                    self.pending.insert(0, r)
                bc.remove(os.path.join(self.sdir, "running", r.id + ".json"))
            else:
                self.respond_test(r, parts)
            with self.lock:
                joiners = self.take_joiners(key)
            queue += joiners
        self.set_detail(f"test batch {m}: stopping Play")
        self.ex.stop_play()

    def respond_test(self, r, parts):
        """One answer for a test request that may have run on two maps. A test reported "not run" on one map counts
        only if it did not run on the other one either."""
        hint = ""
        if r.payload.get("sim") and any(q["notRun"] for q in parts):
            hint = ("\nTests reported not run may need the full scene: `ujob test ... --play` runs them "
                    "(final verification: ask the user first).")
        if len(parts) == 1:
            q = parts[0]
            self.respond(r, q["rc"], q["text"] + hint, {"result": q["result"], "notRun": q["notRun"]})
            return
        order = {0: 0, bc.EXIT_STALE: 1, 1: 2, 3: 3, 2: 4, 4: 5, 5: 6}
        rcs = [q["rc"] for q in parts]
        # "ERROR NOT RUN" (all tests on the other map) is rc 1 for that pass; it is not a failure if the other ran.
        effective = [q["rc"] for q in parts if not (q["rc"] == 1 and q["notRun"] and q["notRun"] == q["total"])]
        rc = max(effective or rcs, key=lambda x: order.get(x, 9))
        text = "\n\n".join(f"-- map {q['map']} --\n{q['text']}" for q in parts) + hint
        self.respond(r, rc, text, {"results": [q["result"] for q in parts]})

    def take_joiners(self, key):
        """Same-key test requests join the running batch, unless a compile or another-key test is waiting."""
        p = self.ex.probe()
        if any(r.op == "compile" for r in self.pending):
            return []
        if any(r.op == "test" and self.test_key(r) != key and self.now() - r.submitted > BATCH_WINDOW for r in self.pending):
            return []
        joiners = [r for r in self.pending if r.op == "test" and self.test_key(r) == key and not self.block_reason(r, p)]
        for r in joiners:
            self.pending.remove(r)
            bc.write_json(os.path.join(self.sdir, "running", r.id + ".json"), r.doc)
        return joiners

    @staticmethod
    def run_code(payload):
        spec = payload.get("spec") or "*"
        if spec.lower().endswith(".json") or spec.lstrip().startswith(("{", "[")):
            return f"return AITestAgent.Run({json.dumps(spec)});"
        if payload.get("all") or spec in ("*", "all"):
            return f"return AITestAgent.RunAll({json.dumps('*' if spec in ('all',) else spec)});"
        return f"return AITestAgent.RunFixture({json.dumps(spec)});" if payload.get("fixture") else f"return AITestAgent.RunAll({json.dumps(spec)});"

    def read_result(self, doc):
        path = ((doc or {}).get("result") or {}).get("result")
        if not path:
            return None
        full = path if os.path.isabs(path) else os.path.join(self.project, path)
        return bc.read_json(full)

    def do_lease_play(self, reqs):
        r = reqs[0]
        self.ensure_no_unowned_play(r)
        mode = r.payload.get("mode") or "MapInspection"
        scene = r.payload.get("scene")
        variant = r.payload.get("variant")
        p0 = self.ex.probe() or {}
        target = self.ex.scene_for_map(scene) if scene else None
        if target and p0.get("activeScene") and p0.get("activeScene") != target:
            self.records["priorScene"] = p0.get("activeScene")
            self.save_records()
        if variant == "director":
            code = f"return DirectorAgent.Boot(900f, {json.dumps(scene or 'official')});"
        elif variant == "aitest":
            code = f"return AITestAgent.Boot({str(bool(r.payload.get('visuals'))).lower()}, 900f, {json.dumps(r.payload.get('session')) if r.payload.get('session') else 'null'}, {json.dumps(scene or self.ex.cfg['defaultMap'])});"
        else:
            code = f"return PlayModeAgent.Boot({json.dumps(mode)}, 900f, {json.dumps(scene) if scene else 'null'});"
        with self.lock:
            self.take_lease(r.session, "play", False, code)
        self.set_detail(f"lease: booting {variant or mode}")
        rc, text, _ = self.ex.agent_job(code)
        if rc != 0:
            with self.lock:
                self.release_lease("boot failed")
            self.respond(r, rc, "boot failed; lease not held:\n" + text)
            return
        self.respond(r, 0, text + "\n[broker] you hold the editor lease (Play). Release it with `ujob lease release` when done.")

    def do_lease_edit(self, reqs):
        r = reqs[0]
        self.ensure_no_unowned_play(r)
        with self.lock:
            self.take_lease(r.session, "edit", False, r.payload.get("why", ""))
        self.respond(r, 0, "you hold the editor lease (Edit Mode). Release it with `ujob lease release` when done.")

    def do_lease_release(self, reqs):
        r = reqs[0]
        if not self.lease or self.lease["session"] != r.session:
            self.respond(r, bc.EXIT_NOT_STARTED, "you hold no lease")
            return
        p = self.ex.probe() or {}
        text = ""
        if p.get("playing") or self.lease.get("kind") == "play":
            _, text = self.ex.stop_play()
        with self.lock:
            self.release_lease("released by holder")
        self.respond(r, 0, (text + "\nlease released").strip())

    def do_import(self, reqs):
        r = reqs[0]
        paths = r.payload.get("paths") or []
        bad = [x for x in paths if x.lower().endswith((".cs", ".asmdef", ".asmref", ".dll", ".rsp"))]
        if bad:
            self.respond(r, 64, "not imported (these compile; use `ujob compile`): " + ", ".join(bad))
            return
        rc, text = self.ex.import_assets(paths)
        self.respond(r, rc, text)

    def do_restore_scene(self, reqs):
        r = reqs[0]
        prior = self.records.get("priorScene")
        if not prior:
            self.respond(r, 0, "no scene to restore")
            return
        self.ensure_no_unowned_play(r)
        rc, text = self.ex.open_scene(prior)
        if rc == 0:
            self.records.pop("priorScene", None)
            self.save_records()
        self.respond(r, rc, f"reopened {prior}" if rc == 0 else text)

    def do_restart(self, reqs):
        """Quit and start the editor again. Unsaved scenes need the caller's choice (the user's, not the agent's):
        save them, back them up as copies (TempArtifacts/Backups/Scenes/<stamp>/) or discard them."""
        r = reqs[0]
        mode = r.payload.get("scenes") or "ask"
        if mode not in RESTART_SCENE_MODES:
            self.respond(r, 64, f"restart: unknown scene choice '{mode}' ({', '.join(RESTART_SCENE_MODES)})")
            return
        p = self.ex.probe()
        if not p or not p.get("fresh") or not p.get("editorPid"):
            self.respond(r, bc.EXIT_NOT_STARTED, "not started: the editor is not answering (no fresh probe), so its unsaved "
                                                 "scenes cannot be checked. Restart it by hand.")
            return
        if p.get("playing"):
            self.respond(r, bc.EXIT_NOT_STARTED, "not started: the editor is in Play Mode. Stop Play first "
                                                 "(`ujob lease release`, or PlayModeAgent.Stop()), then restart.")
            return
        dirty = [s.get("path") or "(untitled scene)" for s in p.get("scenes") or [] if s.get("dirty")]
        if p.get("prefabStageDirty"):
            self.respond(r, bc.EXIT_NOT_STARTED, "not started: Prefab Mode has unsaved changes in "
                                                 f"{p.get('prefabStagePath') or '?'}. Ask the user to save or close it first.")
            return
        if dirty and mode == "ask":
            self.respond(r, bc.EXIT_NOT_STARTED,
                         "not started: unsaved scenes: " + ", ".join(dirty) + ". Ask the user what to do with them, then run "
                         "`ujob restart --save` (save them over the scene files), `--backup` (save copies to "
                         "TempArtifacts/Backups/Scenes/, leave the scene files as they are) or `--discard`. A dirty flag alone "
                         "does not prove an edit: test batches can mark a scene dirty.")
            return
        old_pid = p["editorPid"]
        exe = self.ex.editor_image(old_pid)
        if not exe:
            self.respond(r, 4, f"restart: could not read the editor's executable (pid {old_pid}); nothing was changed")
            return
        before = self.ex.memory()
        notes = []
        if dirty and mode == "save":
            self.set_detail("restart: saving scenes")
            ok, res = self.ex.save_open_scenes()
            if not ok:
                self.respond(r, 1, f"restart stopped: saving the scenes failed ({res}); the editor keeps running")
                return
            notes.append("saved: " + ", ".join(dirty))
        elif dirty and mode == "backup":
            self.set_detail("restart: backing up scenes")
            folder = os.path.join(self.project, "TempArtifacts", "Backups", "Scenes", time.strftime("%Y%m%d-%H%M%S"))
            ok, res = self.ex.backup_dirty_scenes(folder)
            if not ok:
                self.respond(r, 1, f"restart stopped: backing up the scenes failed ({res}); the editor keeps running")
                return
            notes.append("backed up (scene files unchanged): " + res.replace("\n", ", "))
        elif dirty:
            notes.append("discarded unsaved changes in: " + ", ".join(dirty))
        others = sorted(s[:8] for s in self.active_sessions() if s != r.session)
        self.set_detail("restart: closing the editor")
        self.log(f"restart by {r.session[:8]}: editor pid {old_pid} ({mode})")
        t0 = self.now()
        self.ex.exit_editor()
        if not self.ex.wait_exit(old_pid, 180):
            self.respond(r, 4, f"restart: the editor (pid {old_pid}) did not close within 180 s. A dialog may be open; "
                               "look at the editor. Nothing was relaunched.")
            return
        self.set_detail("restart: starting the editor")
        new_pid = self.ex.launch_editor(exe)
        rc, text = self.ex.wait_restarted(old_pid, 900)
        with self.lock:
            if self.lease and self.lease["session"] == r.session:
                self.release_lease("editor restarted")
        after = self.ex.memory()
        lines = [f"editor restarted in {int(self.now() - t0)} s: pid {old_pid} -> {new_pid}"] + notes
        lines.append(f"memory before: {mg.describe(before)}")
        lines.append(f"memory after: {mg.describe(after)}")
        if others:
            lines.append(f"other active sessions (their next calls wait for the editor): {', '.join(others)}")
        if rc:
            lines.append("the new editor is not ready yet: " + (text or "").strip()[-400:])
        self.respond(r, rc or 0, "\n".join(lines))

    # ── state for `ujob status` and the hooks ──
    def write_state(self):
        reqs, detail = (self.main if self.main else (None, None))
        running = None
        if reqs:
            running = {"op": detail, "ids": [r.id for r in reqs], "sessions": sorted({r.session for r in reqs})}
        failed = self.records.get("failed")
        state = {
            "heartbeat": self.now(), "daemonPid": os.getpid(), "started": self.started,
            "paused": self.paused, "lease": self.lease, "running": running,
            "reads": [{"id": r.id, "session": r.session} for r in self.reads.values()],
            "pending": [{"id": r.id, "op": r.op, "session": r.session, "since": r.submitted, "waitingFor": r.reason}
                        for r in self.pending],
            "batch": bool(reqs and reqs[0].op == "test"),
            "lastGoodCompile": self.records.get("goodAt"),
            "brokenFiles": (failed or {}).get("files"), "brokenSince": (failed or {}).get("at"),
            "priorScene": self.records.get("priorScene"),
        }
        try:
            bc.write_json(os.path.join(self.sdir, "state.json"), state)
        except OSError:
            pass

    def idle_exit_due(self):
        return self.stop_requested and not self.main or (
            not self.main and not self.reads and not self.pending and not self.lease
            and self.now() - self.last_busy > IDLE_EXIT)


class _Internal:
    """A stand-in requester for a compile the broker starts itself (before a test batch)."""

    def __init__(self, session):
        self.session, self.id, self.op, self.payload, self.doc = session, None, "compile", {}, {}


def main(project):
    sdir = bc.state_dir(project)
    lock = bc.try_lock(sdir)
    if lock is None:
        return 0  # another daemon owns this project
    broker = Broker(project, sdir, UnityExecutor(project))
    broker.log(f"daemon started pid={os.getpid()} project={project}")
    try:
        while True:
            broker.step()
            if broker.idle_exit_due():
                unlock_and_check = True
                bc.unlock(lock)
                lock = None
                # A request that arrived while unlocking must not be stranded.
                if os.listdir(os.path.join(sdir, "queue")) and unlock_and_check:
                    lock = bc.try_lock(sdir)
                    if lock is not None:
                        continue
                break
            time.sleep(0.1)
    finally:
        broker.log("daemon exiting")
        if lock is not None:
            bc.unlock(lock)
    return 0


if __name__ == "__main__":
    sys.exit(main(os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else os.getcwd()))
