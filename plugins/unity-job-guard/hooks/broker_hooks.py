"""Hooks that tie Claude Code sessions to the Unity broker.

  session-start  register the session (id, Claude pid, cwd) as "implementation" unless it already declared a role
  session-end    mark it ended: the broker cancels its queued requests and releases its lease
  stop           end of turn: if this session holds the editor lease, say so (with idle time)
  post-edit      Edit/Write/NotebookEdit: remember which session last wrote a compile input (a blame HINT only:
                 Bash edits, Codex, Rider and the user are not seen)
  mcp            PreToolUse on Unity MCP tools: Play/scene/compile-triggering tools need the editor lease; tools
                 that change the scene or assets wait while another session holds the lease or a test batch runs
  bash           PreToolUse on Bash/PowerShell: raw `unity command recompile|eval|eval_file|run_tests` bypasses
                 the broker; use ujob

Only active inside Unity projects. The mcp mode runs through pyrun_closed.sh for the Play/scene tools, so a
missing Python or a crash blocks those calls instead of letting them through.
"""
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "scripts"))
from unity_project import UJOB, find_unity_project  # noqa: E402
import broker_common as bc  # noqa: E402

mode = sys.argv[1] if len(sys.argv) > 1 else ""
try:
    data = json.load(sys.stdin)
except Exception:
    data = {}
project = find_unity_project(data.get("cwd"), os.environ.get("CLAUDE_PROJECT_DIR"))
if not project:
    sys.exit(0)
session = data.get("session_id") or os.environ.get("CLAUDE_CODE_SESSION_ID") or "user"
sdir = bc.state_dir(project)
UJ = "ujob"


def broker_state():
    st = bc.read_json(os.path.join(sdir, "state.json"), {}) or {}
    if time.time() - (st.get("heartbeat") or 0) > 15:
        st = {"lease": bc.read_json(os.path.join(sdir, "lease.json")), "stale": True}
    return st


def block(msg):
    sys.stderr.write("Blocked by unity-job-guard (broker): " + msg + "\n")
    sys.exit(2)


def ident():
    pid = os.environ.get("CLAUDE_PID")
    pid = int(pid) if pid and pid.isdigit() else None
    return {"session": session, "kind": "claude", "pid": pid, "pidStart": bc.proc_start(pid) if pid else None}


if mode == "session-start":
    rec_path = os.path.join(sdir, "registry", bc.safe_name(session) + ".json")
    bc.register(sdir, ident(), cwd=data.get("cwd"))
    sys.exit(0)

if mode == "session-end":
    rec_path = os.path.join(sdir, "registry", bc.safe_name(session) + ".json")
    rec = bc.read_json(rec_path)
    if rec:
        rec["endedAt"] = time.time()
        bc.write_json(rec_path, rec)
    if not bc.daemon_alive(sdir):
        lease = bc.read_json(os.path.join(sdir, "lease.json"))
        if lease and lease.get("session") == session:
            bc.remove(os.path.join(sdir, "lease.json"))
    sys.exit(0)

if mode == "stop":
    lease = (broker_state() or {}).get("lease")
    if lease and lease.get("session") == session:
        idle = int((lease.get("idleSeconds") or 0) // 60)
        print(json.dumps({"systemMessage":
              f"unity-job-guard: this session still holds the Unity editor lease ({lease.get('kind')}"
              f"{', implicit' if lease.get('implicit') else ''}, idle {idle} min). Other sessions' Unity work waits. "
              "Release it with `ujob lease release` (or stop Play) when done; it is released after 30 idle minutes."}))
    sys.exit(0)

if mode == "post-edit":
    ti = data.get("tool_input") or {}
    path = ti.get("file_path") or ti.get("notebook_path") or ""
    if path.lower().endswith((".cs", ".asmdef", ".asmref", ".rsp")):
        try:
            rel = os.path.relpath(os.path.abspath(path), project).replace("\\", "/")
        except ValueError:
            sys.exit(0)
        if not rel.startswith(".."):
            with open(os.path.join(sdir, "owners.jsonl"), "a", encoding="utf-8") as f:
                f.write(json.dumps({"path": rel, "session": session, "ts": time.time()}) + "\n")
    sys.exit(0)

if mode == "bash":
    cmd = (data.get("tool_input") or {}).get("command") or ""
    if re.search(r"(^|[\s;&|(])unity(\.exe)?\s+command\s+(recompile|eval|eval_file|run_tests)\b", cmd):
        block("raw `unity command recompile/eval/eval_file/run_tests` bypasses the broker, so it can compile or "
              "enter Play under another session's running test. Use `ujob compile`, `ujob run -e CODE` / "
              "`ujob run FILE.cs` (add --read for lookups) or `ujob test`. Emergency bypass: UJOB_DIRECT=1 ujob ...")
    sys.exit(0)

if mode == "mcp":
    tool = data.get("tool_name") or ""
    last = tool.rsplit("__", 1)[-1]  # mcp__<server>__unity_<name>; the server may itself be called "unity"
    if not last.startswith("unity_"):
        sys.exit(0)
    name = last[len("unity_"):]
    ti = data.get("tool_input") or {}
    blob = json.dumps(ti, ensure_ascii=False)
    st = broker_state()
    lease = st.get("lease")
    holder = bool(lease and lease.get("session") == session)

    # 1) Play / scene / compile-triggering tools: only the lease holder.
    gated = name in ("play_mode", "scene_open", "scene_new", "build", "script_create", "script_update")
    if name == "execute_menu_item":
        gated = bool(re.search(r"Edit/Play|Edit/Pause|Assets/Refresh|Assets/Reimport|File/(Open|New|Build)", blob))
    if name == "advanced_tool":
        gated = bool(re.search(r"test|compil|play|scene|build|refresh|reimport", blob, re.I))
    if name == "execute_code":
        code = ti.get("code") or blob
        gated = bc.enters_play_or_scene(code) or bool(re.search(
            r"AssetDatabase\s*\.\s*Refresh|RequestScriptCompilation|ImportAsset\s*\([^)]*\.cs", code))
    if gated and not holder:
        who = f" (currently held by {lease['session'][:8]})" if lease else ""
        block(f"`unity_{name}` can enter Play, switch scenes or compile, which only the editor lease holder may do{who}. "
              "Go through the broker instead: `ujob lease play MODE [--scene ...]` / `ujob lease edit` for hands-on "
              "work (then MCP calls are yours), `ujob test SPEC` for tests, `ujob compile` after writing scripts "
              "with the Write/Edit tools.")

    # 2) Read-only tools always pass.
    if re.search(r"(info|list|get|search|hierarchy|stats|state|ping|log|history|screenshot|capture|read|context|"
                 r"queue|agents|instances|errors|releases|install_path|editors|select_instance|selection_|"
                 r"find_by_type|focus_scene_view|referenceable|missing_references)", name):
        sys.exit(0)

    # 3) Everything else changes the scene or assets: not during another session's lease or batch.
    if lease and not holder:
        block(f"another session ({lease['session'][:8]}) holds the Unity editor lease; `unity_{name}` would change "
              "the editor under it. Wait (check `ujob status`) or ask the holder to release.")
    if st.get("batch") and not holder:
        block(f"a broker test batch is running in Play Mode; `unity_{name}` would change the scene under the tests "
              "(and Play-mode edits are lost). Wait for it to finish (`ujob status`).")
    sys.exit(0)

sys.exit(0)
