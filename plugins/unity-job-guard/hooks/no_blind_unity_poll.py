"""PreToolUse guard: block shell sleep-poll loops that wait on Unity side effects.

Loops like `until [ -f result.txt ]; do sleep 5; done` (or grepping Editor.log / a background
task's .output) only watch for success. When the Unity job never starts or dies (domain reload,
Play Mode entry, compile error, swallowed delayCall, main-thread timeout), they spin until the
600 s tool limit.

Active only inside Unity projects. To allow a loop the guard misreads, put `# poll-ok` in the command.
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from unity_project import UJOB, find_unity_project  # noqa: E402

try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(0)

cmd = (data.get("tool_input") or {}).get("command") or ""
if not cmd or "poll-ok" in cmd or re.search(r"\bujob\b", cmd):
    sys.exit(0)
if not find_unity_project(data.get("cwd"), os.environ.get("CLAUDE_PROJECT_DIR")):
    sys.exit(0)

SLEEP = r"\b(sleep\s+\d|Start-Sleep\b)"
loop = re.search(r"\b(until|while)\b[\s\S]*?" + SLEEP, cmd) or \
       re.search(r"\bfor\b[^\n]*\bseq\b[\s\S]*?" + SLEEP, cmd)
unity_side_effect = re.search(
    r"unity|Editor\.log|scratchpad|[/\\]tasks[/\\]|\.output\b|ScriptAssemblies|result\.txt|\.flag\b",
    cmd, re.IGNORECASE)

if loop and unity_side_effect:
    sys.stderr.write(
        "Blocked by unity-job-guard: sleep-poll loop waiting on a Unity side effect. These loops wait the full "
        "600 s when the job never started or died (domain reload, Play Mode, compile error, lost delayCall, "
        "main-thread timeout).\n"
        "Use instead:\n"
        f'  python "{UJOB}" run FILE.cs [--deadline 540]   # detached eval_file; returns on done/fail/lost/deadline\n'
        f'  python "{UJOB}" run -e "CODE"\n'
        f'  python "{UJOB}" wait JOB_ID                    # continue a job past its deadline\n'
        f'  python "{UJOB}" compile | ready\n'
        "Run the Unity work synchronously inside the job (no EditorApplication.delayCall / update callbacks + "
        "result files). If a loop is genuinely needed, give it a deadline AND a failure check, and add `# poll-ok`.\n")
    sys.exit(2)
sys.exit(0)
