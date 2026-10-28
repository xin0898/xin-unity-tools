"""Reminds Claude to arm the staleness watchdog whenever Unity work goes to the background.

A background Bash task that drives Unity (a loop over test runs, a long ujob wait) can stall without
ever exiting: its notification never comes and the session waits on nothing. `ujob stalewatch`, run
through the Monitor tool, reports dead heartbeats, finished jobs, a lost editor and an idle driver.

  pre   PreToolUse on Bash/PowerShell: the call sets run_in_background and touches Unity work.
  post  PostToolUse on Bash/PowerShell: the user moved a running command to the background.

Active only inside Unity projects.
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from unity_project import UJOB, find_unity_project  # noqa: E402

mode = sys.argv[1] if len(sys.argv) > 1 else "pre"
try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(0)

tool_input = data.get("tool_input") or {}
cmd = tool_input.get("command") or ""
if not cmd or not find_unity_project(data.get("cwd"), os.environ.get("CLAUDE_PROJECT_DIR")):
    sys.exit(0)
if not re.search(r"\bujob\b|unity|AITestAgent|PlayModeAgent|DirectorAgent|waitfile", cmd, re.IGNORECASE):
    sys.exit(0)

if mode == "pre":
    triggered = bool(tool_input.get("run_in_background"))
else:
    response = json.dumps(data.get("tool_response") or "")
    triggered = bool(re.search(r"backgrounded|running in background|run_in_background", response, re.IGNORECASE))
if not triggered:
    sys.exit(0)

note = (
    "unity-job-guard: Unity work is now running in the background. Background tasks can stall without exiting "
    "(a loop that never starts the next job, a wait on a job that died), and then no notification ever arrives. "
    "Arm the staleness watchdog now, in your next message, with the Monitor tool:\n"
    f'  Monitor(command: python "{UJOB}" stalewatch, description: "Unity job staleness", timeout_ms: 1800000)\n'
    "It checks every 60 s and reports STALE (heartbeat older than 60 s, no DONE), FINISHED, EDITOR (unreachable or "
    "reloading) and IDLE (no job running for 180 s, then it exits). Re-arm it when it expires while the work is "
    "still going. On STALE or IDLE: read the task's output, stop the task (TaskStop) if it is stuck and continue "
    "by hand, one job per call. Drive multi-job runs as separate tool calls rather than one shell loop."
)
event = "PreToolUse" if mode == "pre" else "PostToolUse"
print(json.dumps({"hookSpecificOutput": {"hookEventName": event, "additionalContext": note}}))
sys.exit(0)
