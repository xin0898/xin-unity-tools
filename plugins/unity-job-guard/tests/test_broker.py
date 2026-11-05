"""Unit tests for the Unity broker scheduler, against a fake Unity executor (no editor needed).

    python -m pytest plugins/unity-job-guard/tests -q
"""
import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
import broker as B  # noqa: E402
import broker_common as bc  # noqa: E402


class Clock:
    def __init__(self):
        self.t = time.time()

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


class FakeExec:
    """Stands in for UnityExecutor. Records calls; `probe_state` is what the probe would report."""

    def __init__(self, project):
        self.project = project
        self.cfg = {"stopPlay": "stop", "maps": {"test": "Assets/Test.unity", "official": "Assets/Dev.unity"},
                    "defaultMap": "test", "aitest": True}
        self.calls = []
        self.compile_rc, self.compile_text = 0, "COMPILED OK"
        self.gate = (0, "", [])
        self.maps = {}
        self.probe_state = {"fresh": True, "playing": False, "bootJobs": [], "activeScene": "Assets/Test.unity",
                            "scenes": [{"path": "Assets/Test.unity", "dirty": False}], "busy": [], "writtenUnix": time.time()}

    def probe(self):
        return dict(self.probe_state)

    def eval(self, code=None, file=None, deadline=0):
        self.calls.append(("eval", code))
        if code and "Boot(" in code:
            self.probe_state["playing"] = True
        if code and ".Stop()" in code:
            self.probe_state["playing"] = False
        return 0, f"ran {code}"

    def compile(self, deadline=0):
        self.calls.append(("compile",))
        return self.compile_rc, self.compile_text

    def offline_gate(self):
        self.calls.append(("gate",))
        return self.gate

    def wait_ready(self, deadline=0):
        return None, ""

    def stop_play(self):
        self.calls.append(("stop_play",))
        self.probe_state["playing"] = False
        return 0, "stopped"

    def agent_job(self, code, deadline=0):
        self.calls.append(("agent", code))
        if "Boot(" in code:
            self.probe_state["playing"] = True
            return 0, "booted", {"ok": True}
        if "Run" in code:
            path = os.path.join(self.project, f"result_{len(self.calls)}.json")
            with open(path, "w") as f:
                json.dump({"total": 2, "notRun": 0, "resetLeaks": 0, "tests": []}, f)
            return 0, "PASS 2/2", {"ok": True, "result": {"result": path}}
        return 0, "ok", {"ok": True}

    def aitest_maps(self, spec):
        return set(self.maps.get(spec, {"test"}))

    def scene_for_map(self, m):
        return self.cfg["maps"].get(m)

    def import_assets(self, paths):
        return 0, "imported"

    def open_scene(self, path):
        self.calls.append(("open", path))
        return 0, path

    # memory and restart
    mem = None  # None = not measured (never gated)

    def memory(self):
        return dict(self.mem) if self.mem else None

    def save_open_scenes(self):
        self.calls.append(("save_scenes",))
        for s in self.probe_state["scenes"]:
            s["dirty"] = False
        return True, "saved"

    def backup_dirty_scenes(self, folder):
        self.calls.append(("backup_scenes", folder))
        return True, folder + "/Dev.unity"

    def editor_image(self, pid):
        return "C:/Unity/Editor/Unity.exe"

    def exit_editor(self):
        self.calls.append(("exit",))

    def wait_exit(self, pid, timeout):
        return True

    def launch_editor(self, exe):
        self.calls.append(("launch", exe))
        return 4242

    def wait_restarted(self, old_pid, deadline=0):
        self.calls.append(("wait_restarted", old_pid))
        self.probe_state["editorPid"] = 4242
        return 0, ""


@pytest.fixture
def env(tmp_path, monkeypatch):
    project = tmp_path / "proj"
    (project / "Assets").mkdir(parents=True)
    (project / "Assets" / "A.cs").write_text("class A {}")
    monkeypatch.setenv("UJOB_BROKER_HOME", str(tmp_path / "home"))
    sdir = bc.state_dir(str(project))
    clock = Clock()
    ex = FakeExec(str(project))
    br = B.Broker(str(project), sdir, ex, clock=clock, threaded=False)
    return br, ex, clock, sdir, project


def ident(session):
    return {"session": session, "kind": "claude", "pid": os.getpid(), "pidStart": bc.proc_start(os.getpid()),
            "client": os.getpid(), "clientStart": bc.proc_start(os.getpid())}


def submit(sdir, session, op, payload=None):
    rid, _ = bc.submit(sdir, ident(session), op, payload or {})
    return rid


def resp(sdir, rid):
    return bc.read_json(bc.response_path(sdir, rid))


def active(sdir, *sessions):
    for s in sessions:
        bc.touch(os.path.join(sdir, "activity", bc.safe_name(s) + ".last"))


# ── classifiers ──

def test_read_only_matcher():
    assert bc.is_read_only_eval("return AITestAgent.Status();")
    assert bc.is_read_only_eval('  return PlayModeAgent.Help() ; ')
    assert bc.is_read_only_eval('return AITestAgent.Get("city:1", "owner");')
    assert not bc.is_read_only_eval("AITestAgent.Status(); EditorApplication.EnterPlaymode();")
    assert not bc.is_read_only_eval("return AITestAgent.Status(); /* */ EditorApplication.EnterPlaymode();")
    assert not bc.is_read_only_eval("return AITestAgent.Boot();")
    assert not bc.is_read_only_eval("return X.Status(Foo());")
    assert not bc.is_read_only_eval("// return X.Status();\nEditorApplication.EnterPlaymode();")


def test_enters_play_matcher():
    assert bc.enters_play_or_scene('return PlayModeAgent.Boot("MapInspection", scene: "official");')
    assert bc.enters_play_or_scene("return AITestAgent.Reset(true);")
    assert not bc.enters_play_or_scene("return AITestAgent.Help(); // Boot( in a comment")
    assert not bc.enters_play_or_scene("StopCoroutine(x);")


# ── reads, leases, pause ──

def test_read_eval_passes_during_other_lease(env):
    br, ex, clock, sdir, _ = env
    br.take_lease("A", "play", False)
    rid = submit(sdir, "B", "eval", {"code": "return PlayModeAgent.Status();"})
    br.step()
    assert resp(sdir, rid)["rc"] == 0


def test_lease_blocks_others_then_exit6(env):
    br, ex, clock, sdir, _ = env
    br.take_lease("A", "play", False)
    rid = submit(sdir, "B", "eval", {"code": "Debug.Log(1);"})
    br.step()
    assert resp(sdir, rid) is None
    clock.advance(bc.BLOCKED_WAIT + 1)
    br.step()
    r = resp(sdir, rid)
    assert r["rc"] == bc.EXIT_NOT_STARTED and "leased by" in r["output"]


def test_holder_evals_pass(env):
    br, ex, clock, sdir, _ = env
    br.take_lease("A", "play", False)
    rid = submit(sdir, "A", "eval", {"code": "Debug.Log(1);"})
    br.step()
    assert resp(sdir, rid)["rc"] == 0


def test_pause_rejects_mutating_reads_pass(env):
    br, ex, clock, sdir, _ = env
    br.paused = True
    a = submit(sdir, "A", "eval", {"code": "Debug.Log(1);"})
    b = submit(sdir, "A", "eval", {"code": "return AITestAgent.Status();"})
    br.step()
    assert resp(sdir, a)["rc"] == bc.EXIT_NOT_STARTED
    assert resp(sdir, b)["rc"] == 0


def test_busy_runner_blocks_compile(env):
    br, ex, clock, sdir, _ = env
    ex.probe_state["busy"] = [{"name": "Hub build", "busy": True}]
    rid = submit(sdir, "A", "compile")
    clock.advance(10)
    br.step()
    assert resp(sdir, rid) is None
    assert "busy: Hub build" in br.pending[0].reason


def test_implicit_lease_and_release(env):
    br, ex, clock, sdir, _ = env
    rid = submit(sdir, "A", "eval", {"code": "return AITestAgent.Boot();"})
    br.step()
    assert resp(sdir, rid)["rc"] == 0
    assert br.lease and br.lease["session"] == "A" and br.lease["implicit"]
    other = submit(sdir, "B", "eval", {"code": "Debug.Log(2);"})
    br.step()
    assert resp(sdir, other) is None          # blocked by A's lease
    ex.probe_state["playing"] = False          # A stopped Play
    br.step()
    clock.advance(B.IMPLICIT_GRACE + 1)
    br.step()
    assert br.lease is None
    br.step()
    assert resp(sdir, other)["rc"] == 0


def test_hand_started_play_is_unowned_and_stopped(env):
    br, ex, clock, sdir, _ = env
    ex.probe_state["playing"] = True           # the user pressed Play
    rid = submit(sdir, "A", "eval", {"code": "Debug.Log(1);"})
    br.step()
    assert br.lease is None                    # an unrelated eval does not take ownership
    t = submit(sdir, "A", "test", {"spec": "sample"})
    br.step()
    assert ("stop_play",) in ex.calls


def test_idle_lease_warn_and_release(env):
    br, ex, clock, sdir, _ = env
    br.take_lease("A", "play", False)
    br.lease["since"] = br.lease["lastEval"] = clock.t - B.IDLE_WARN - 5
    br.step()
    assert br.lease["warn"]
    br.lease["since"] = br.lease["lastEval"] = clock.t - B.IDLE_RELEASE - 5
    br.step()
    assert br.lease is None
    rid = submit(sdir, "A", "eval", {"code": "Debug.Log(1);"})
    br.step()
    assert resp(sdir, rid)["rc"] == bc.EXIT_NOT_STARTED and "released" in resp(sdir, rid)["output"]


def test_running_client_keeps_holder_active(env):
    br, ex, clock, sdir, _ = env
    br.take_lease("A", "play", False)
    br.lease["since"] = br.lease["lastEval"] = clock.t - B.IDLE_RELEASE - 5
    act = bc.Activity(sdir, ident("A"))      # a long `ujob waitfile` of the holder
    try:
        br.step()
        assert br.lease is not None
    finally:
        act.close()


def test_dead_holder_releases(env):
    br, ex, clock, sdir, _ = env
    bc.register(sdir, {"session": "A", "kind": "claude", "pid": 999999, "pidStart": 1})
    br.take_lease("A", "play", False)
    br.step()
    assert br.lease is None


def test_client_death_cancels(env):
    br, ex, clock, sdir, _ = env
    rid, _ = bc.submit(sdir, {**ident("A"), "client": 999999, "clientStart": 1}, "eval", {"code": "Debug.Log(1);"})
    br.take_lease("B", "play", False)        # keep it from starting
    br.step()
    assert not br.pending


def test_session_end_releases(env):
    br, ex, clock, sdir, _ = env
    br.take_lease("A", "play", False)
    rid = submit(sdir, "A", "session_end", {"session": "A"})
    br.step()
    assert br.lease is None


# ── compiles ──

def test_compile_coalescing(env):
    br, ex, clock, sdir, _ = env
    ids = [submit(sdir, s, "compile") for s in ("A", "B", "C")]
    br.step()
    assert ex.calls.count(("compile",)) == 0    # collecting
    clock.advance(B.COMPILE_QUIET + 0.5)
    br.step()
    assert ex.calls.count(("compile",)) == 1
    assert all(resp(sdir, i)["rc"] == 0 for i in ids)
    again = submit(sdir, "A", "compile")
    clock.advance(B.COMPILE_QUIET + 0.5)
    br.step()
    assert ex.calls.count(("compile",)) == 1    # nothing changed: answered without Unity
    assert "up to date" in resp(sdir, again)["output"]


def write_owner(sdir, path, session):
    with open(os.path.join(sdir, "owners.jsonl"), "a") as f:
        f.write(json.dumps({"path": path, "session": session}) + "\n")


def test_gate_blames_and_stales(env):
    br, ex, clock, sdir, project = env
    br.records["good"] = B.snapshot(str(project))
    (project / "Assets" / "A.cs").write_text("class A { broken")
    (project / "Assets" / "B.cs").write_text("class B {}")
    write_owner(sdir, "Assets/A.cs", "A")
    write_owner(sdir, "Assets/B.cs", "B")
    active(sdir, "A", "B")
    ex.gate = (5, "Assets/A.cs(1,10): error CS1513: } expected", ["Assets/A.cs"])
    a = submit(sdir, "A", "compile")
    b = submit(sdir, "B", "compile")
    clock.advance(B.COMPILE_QUIET + 1)
    br.step()
    assert ("compile",) not in ex.calls          # the gate kept Unity out of the error state
    assert resp(sdir, a)["rc"] == 5
    assert resp(sdir, b)["rc"] == bc.EXIT_STALE
    # B's eval runs on the last good build, which lacks B.cs: exit 7, naming the file
    e = submit(sdir, "B", "eval", {"code": "Debug.Log(1);"})
    br.step()
    r = resp(sdir, e)
    assert r["rc"] == bc.EXIT_STALE and "Assets/B.cs" in r["output"]
    # A's own broken file blocks only A
    e2 = submit(sdir, "A", "eval", {"code": "Debug.Log(1);"})
    br.step()
    assert resp(sdir, e2)["rc"] == 5


def test_gate_skipped_for_single_session_and_structural(env):
    br, ex, clock, sdir, project = env
    br.records["good"] = B.snapshot(str(project))
    (project / "Assets" / "A.cs").write_text("class A { int x; }")
    active(sdir, "A")
    rid = submit(sdir, "A", "compile")
    clock.advance(B.COMPILE_QUIET + 1)
    br.step()
    assert ("gate",) not in ex.calls and ("compile",) in ex.calls
    # two sessions, but an asmdef changed: the replayed command lines would be wrong, so no gate
    active(sdir, "A", "B")
    (project / "Assets" / "X.asmdef").write_text("{}")
    rid = submit(sdir, "A", "compile")
    clock.advance(B.COMPILE_QUIET + 1)
    br.step()
    assert ex.calls.count(("gate",)) == 0 and "structural" in resp(sdir, rid)["output"]


def test_compile_force_bypasses_gate(env):
    br, ex, clock, sdir, project = env
    br.records["good"] = B.snapshot(str(project))
    (project / "Assets" / "A.cs").write_text("class A { int y; }")
    active(sdir, "A", "B")
    rid = submit(sdir, "A", "compile", {"force": True})
    clock.advance(B.COMPILE_QUIET + 1)
    br.step()
    assert ("gate",) not in ex.calls and resp(sdir, rid)["rc"] == 0


def test_unknown_owner_goes_to_requester(env):
    br, ex, clock, sdir, project = env
    ex.compile_rc, ex.compile_text = 5, "COMPILE FAILED:\nAssets/A.cs(1,1): error CS0001"
    rid = submit(sdir, "A", "compile")
    clock.advance(B.COMPILE_QUIET + 1)
    br.step()
    assert resp(sdir, rid)["rc"] == 5


# ── test batches ──

def test_single_session_boots_immediately(env):
    br, ex, clock, sdir, _ = env
    br.records["good"] = B.snapshot(env[4].__str__())
    active(sdir, "A")
    rid = submit(sdir, "A", "test", {"spec": "sample"})
    br.step()
    assert resp(sdir, rid)["rc"] == 0
    assert sum(1 for c in ex.calls if c[0] == "agent" and "Boot(" in c[1]) == 1


def test_two_sessions_share_one_boot(env):
    br, ex, clock, sdir, project = env
    br.records["good"] = B.snapshot(str(project))
    active(sdir, "A", "B")
    a = submit(sdir, "A", "test", {"spec": "sample"})
    br.step()
    assert resp(sdir, a) is None                   # window open: waiting for joiners
    b = submit(sdir, "B", "test", {"spec": "troops"})
    clock.advance(B.BATCH_WINDOW + 1)
    br.step()
    assert resp(sdir, a)["rc"] == 0 and resp(sdir, b)["rc"] == 0
    assert sum(1 for c in ex.calls if c[0] == "agent" and "Boot(" in c[1]) == 1


def test_spec_on_both_maps_runs_twice_answers_once(env):
    br, ex, clock, sdir, project = env
    br.records["good"] = B.snapshot(str(project))
    ex.maps["mixed"] = {"test", "official"}
    active(sdir, "A")
    rid = submit(sdir, "A", "test", {"spec": "mixed"})
    br.step()
    assert resp(sdir, rid) is None                 # first map done, continues on the other
    br.step()
    r = resp(sdir, rid)
    assert r["rc"] == 0 and "map official" in r["output"] and "map test" in r["output"]
    assert sum(1 for c in ex.calls if c[0] == "agent" and "Boot(" in c[1]) == 2


def test_dirty_user_scene_blocks_switch(env):
    br, ex, clock, sdir, project = env
    br.records["good"] = B.snapshot(str(project))
    ex.probe_state["activeScene"] = "Assets/Dev.unity"
    ex.probe_state["scenes"] = [{"path": "Assets/Dev.unity", "dirty": True, "dirtySinceUnix": 1}]
    rid = submit(sdir, "A", "test", {"spec": "sample"})
    br.step()
    assert resp(sdir, rid) is None and "blocked: user" in br.pending[0].reason


def test_test_compiles_first_when_code_changed(env):
    br, ex, clock, sdir, project = env
    br.records["good"] = B.snapshot(str(project))
    (project / "Assets" / "A.cs").write_text("class A { int z; }")
    rid = submit(sdir, "A", "test", {"spec": "sample"})
    br.step()
    assert ("compile",) in ex.calls and resp(sdir, rid)["rc"] == 0


# ── recovery ──

def test_recovery_requeues_once(env):
    br, ex, clock, sdir, project = env
    doc = {"id": "abc", "op": "eval", "payload": {"code": "Debug.Log(1);"}, "session": "A", "submitted": time.time()}
    bc.write_json(os.path.join(sdir, "running", "abc.json"), doc)
    br2 = B.Broker(str(project), sdir, ex, clock=clock, threaded=False)
    br2.step()
    assert resp(sdir, "abc")["rc"] == 0


# ── memory gate ──

import memory_guard as mg  # noqa: E402

LOW = {"freeCommitGB": 15.0, "commitLimitGB": 127.0, "editorPrivateGB": 32.0, "editorUptimeHours": 47.0}
FINE = {"freeCommitGB": 60.0, "commitLimitGB": 127.0, "editorPrivateGB": 10.0, "editorUptimeHours": 1.0}


def test_memory_assess_levels():
    assert mg.assess(None) == ("ok", None)
    assert mg.assess(FINE)[0] == "ok"
    assert mg.assess(LOW, gate_off=False)[0] == "refuse"
    assert mg.assess(LOW, light=True, gate_off=False)[0] == "warn"          # 15 GB is enough for the test map
    assert mg.assess(dict(LOW, freeCommitGB=8.0), light=True, gate_off=False)[0] == "refuse"
    assert mg.assess(LOW, gate_off=True)[0] == "warn"                       # UJOB_MEMORY_GATE=off
    assert mg.assess(dict(FINE, editorUptimeHours=30.0))[0] == "warn"
    assert mg.assess(LOW, {"refuseFreeGB": 12}, gate_off=False)[0] == "warn"  # project threshold


def test_memory_gate_refuses_full_scene_play(env, monkeypatch):
    br, ex, clock, sdir, _ = env
    monkeypatch.delenv(mg.GATE_OFF_ENV, raising=False)
    ex.mem = LOW
    t = submit(sdir, "A", "test", {"spec": "sample", "sim": False, "map": "official"})
    e = submit(sdir, "A", "eval", {"code": 'return PlayModeAgent.Boot("MapInspection", scene: "official");'})
    br.step()
    for rid in (t, e):
        r = resp(sdir, rid)
        assert r["rc"] == bc.EXIT_NOT_STARTED and "memory too low" in r["output"]
    assert not any(c[0] == "agent" for c in ex.calls)


def test_memory_gate_lets_light_play_run_with_warning(env, monkeypatch):
    br, ex, clock, sdir, project = env
    monkeypatch.delenv(mg.GATE_OFF_ENV, raising=False)
    br.records["good"] = B.snapshot(str(project))
    active(sdir, "A")
    ex.mem = LOW
    rid = submit(sdir, "A", "test", {"spec": "sample", "sim": True})
    br.step()
    r = resp(sdir, rid)
    assert r["rc"] == 0 and "WARNING memory" in r["output"]


def test_memory_gate_ignores_edit_mode_work_and_unmeasured(env):
    br, ex, clock, sdir, _ = env
    ex.mem = dict(LOW, freeCommitGB=2.0)
    a = submit(sdir, "A", "eval", {"code": "Debug.Log(1);"})               # no Play entry: never gated
    br.step()
    assert resp(sdir, a)["rc"] == 0
    ex.mem = None
    b = submit(sdir, "A", "eval", {"code": 'return PlayModeAgent.Boot("Director", scene: "official");'})
    br.step()
    assert resp(sdir, b)["rc"] == 0


def test_memory_gate_off_turns_refusal_into_warning(env, monkeypatch):
    br, ex, clock, sdir, _ = env
    monkeypatch.setenv(mg.GATE_OFF_ENV, "off")
    ex.mem = LOW
    rid = submit(sdir, "A", "eval", {"code": 'return PlayModeAgent.Boot("MapInspection", scene: "official");'})
    br.step()
    r = resp(sdir, rid)
    assert r["rc"] == 0 and "WARNING" in r["output"]


# ── restart ──

def dirty_dev(ex):
    ex.probe_state.update({"editorPid": 1111, "activeScene": "Assets/Dev.unity",
                           "scenes": [{"path": "Assets/Dev.unity", "dirty": True}]})


def test_restart_asks_about_unsaved_scenes(env):
    br, ex, clock, sdir, _ = env
    dirty_dev(ex)
    rid = submit(sdir, "A", "restart", {"scenes": "ask"})
    br.step()
    r = resp(sdir, rid)
    assert r["rc"] == bc.EXIT_NOT_STARTED and "Assets/Dev.unity" in r["output"] and "--backup" in r["output"]
    assert ("exit",) not in ex.calls


def test_restart_refused_in_play_and_with_dirty_prefab(env):
    br, ex, clock, sdir, _ = env
    ex.probe_state.update({"editorPid": 1111, "playing": True})
    a = submit(sdir, "A", "restart", {"scenes": "discard"})
    br.step()
    assert resp(sdir, a)["rc"] == bc.EXIT_NOT_STARTED and "Play Mode" in resp(sdir, a)["output"]
    ex.probe_state.update({"playing": False, "prefabStageDirty": True, "prefabStagePath": "Assets/P.prefab"})
    b = submit(sdir, "A", "restart", {"scenes": "discard"})
    br.step()
    assert resp(sdir, b)["rc"] == bc.EXIT_NOT_STARTED and "Prefab Mode" in resp(sdir, b)["output"]
    assert ("exit",) not in ex.calls


def test_restart_backup_then_relaunch(env):
    br, ex, clock, sdir, _ = env
    dirty_dev(ex)
    rid = submit(sdir, "A", "restart", {"scenes": "backup"})
    br.step()
    r = resp(sdir, rid)
    assert r["rc"] == 0 and "1111 -> 4242" in r["output"] and "backed up" in r["output"]
    names = [c[0] for c in ex.calls]
    assert names.index("backup_scenes") < names.index("exit") < names.index("launch") < names.index("wait_restarted")
    assert ("save_scenes",) not in ex.calls


def test_restart_save_and_clean(env):
    br, ex, clock, sdir, _ = env
    dirty_dev(ex)
    a = submit(sdir, "A", "restart", {"scenes": "save"})
    br.step()
    assert resp(sdir, a)["rc"] == 0 and ("save_scenes",) in ex.calls
    ex.calls.clear()
    ex.probe_state["editorPid"] = 1111                                     # clean editor: no choice needed
    b = submit(sdir, "A", "restart", {})
    br.step()
    assert resp(sdir, b)["rc"] == 0 and ("exit",) in ex.calls and ("save_scenes",) not in ex.calls


def test_restart_releases_the_callers_lease_and_waits_for_others(env):
    br, ex, clock, sdir, _ = env
    ex.probe_state["editorPid"] = 1111
    br.take_lease("A", "edit", False)
    a = submit(sdir, "A", "restart", {})
    br.step()
    assert resp(sdir, a)["rc"] == 0 and br.lease is None
    br.take_lease("B", "edit", False)
    c = submit(sdir, "A", "restart", {})
    br.step()
    assert resp(sdir, c) is None                                           # blocked by B's lease
    clock.advance(bc.BLOCKED_WAIT + 1)
    br.step()
    assert resp(sdir, c)["rc"] == bc.EXIT_NOT_STARTED and "leased by" in resp(sdir, c)["output"]
