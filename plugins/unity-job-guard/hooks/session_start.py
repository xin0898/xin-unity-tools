"""SessionStart: in a Unity project, tell Claude where ujob.py is and the one rule that matters."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from unity_project import UJOB, find_unity_project  # noqa: E402

try:
    data = json.load(sys.stdin)
except Exception:
    data = {}

if find_unity_project(data.get("cwd") or os.getcwd(), os.environ.get("CLAUDE_PROJECT_DIR")):
    print(
        "unity-job-guard: for Unity editor work longer than ~20 s, or any wait on Unity (compile, Play Mode, "
        "long eval), use `ujob run FILE.cs|-e CODE`, `ujob wait JOB_ID`, `ujob compile`, `ujob ready`, and for multi-frame work `ujob waitfile DONE --heartbeat HB` "
        f'(`ujob` is on the Bash PATH; elsewhere run `python "{UJOB}" ...`). '
        "It returns on success, failure, a job lost to a domain reload, or a deadline (exit 0/1/2/3, 4 no editor, "
        "5 compile errors). Do not queue work with EditorApplication.delayCall/update and poll a result file, "
        "and do not write sleep loops on Editor.log or task output; a hook blocks those. "
        "To check that code compiles, or to run pure-logic Edit Mode tests, without waiting for the editor "
        "(parallel-safe, also while another session drives Unity): `ujob offline-compile [--overlay DIR]` and "
        "`ujob offline-test [--filter Fixture]`; finish with one real `ujob compile` before calling a change done. "
        "Sessions share the editor through the broker: `ujob test TAG` for AI tests, `ujob lease play MODE` / "
        "`ujob lease release` for hands-on Play, `ujob run --read` for lookups; exit 6 = not started (reason "
        "printed), exit 7 = ran on a stale build (not validated); `ujob status` shows who holds the editor; "
        "`ujob register --role R --brief TEXT` says what this session does. "
        "Details: the unity-long-jobs skill.")
sys.exit(0)
