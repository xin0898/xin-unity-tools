"""Guards for Unity MCP tools (AnkleBreaker and similar servers that expose `unity_*` tools).

PreToolUse  (unity_execute_code): block EditorApplication.delayCall fire-and-forget code. It only
            postpones work, and domain reloads, Play Mode and scene switches drop it silently, so the
            result file being waited for never appears. Running it synchronously is equivalent.
PostToolUse (any unity_* tool): when the reply shows a busy, reloading or unreachable editor, an
            unknown outcome, a compile in progress, or multi-frame work was started (update callback /
            coroutine), tell Claude how to wait with a bound instead of re-calling or sleep-polling.

Hooks cannot shorten an MCP call that is already in flight; the server's own retry policy decides that.
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from unity_project import UJOB  # noqa: E402

mode = sys.argv[1] if len(sys.argv) > 1 else ""
try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(0)

tool = data.get("tool_name") or ""
if not re.search(r"__unity_", tool):
    sys.exit(0)
UJ = f'ujob (or python "{UJOB}")'

if mode == "pre":
    if not tool.endswith("unity_execute_code"):
        sys.exit(0)
    code = json.dumps(data.get("tool_input") or {}, ensure_ascii=False)
    if "poll-ok" in code:
        sys.exit(0)
    # delayCall only postpones work by a tick; running it synchronously is always equivalent.
    if re.search(r"EditorApplication\s*\.\s*delayCall\s*\+?=", code):
        sys.stderr.write(
            "Blocked by unity-job-guard: this execute_code queues work with EditorApplication.delayCall and "
            "returns before it runs. A domain reload, Play Mode change or scene switch drops it silently, so a "
            "result file you wait for may never appear (this caused most 10-minute dead waits).\n"
            "Run it synchronously instead: put the work in a C# snippet that returns the result, then "
            f"`{UJ} run job.cs` (detached CLI job, no 30 s limit; returns on done/fail/lost/deadline).\n"
            "If the deferral itself is the goal (not a long job), add the comment `// poll-ok` to the code.\n")
        sys.exit(2)
    sys.exit(0)

if mode == "post":
    reply = json.dumps(data.get("tool_response"), ensure_ascii=False)
    note = None
    if re.search(r"Outcome unknown|may have executed", reply, re.I):
        note = ("The Unity MCP could not confirm whether this request ran. Inspect the editor state before "
                "sending it again; re-sending may apply the change twice.")
    elif re.search(r"waiting for main thread|Queue polling timed out|Main thread operation timed out", reply, re.I):
        note = ("The Unity editor's main thread is busy (a long job, compile or another session). Don't re-send "
                f"this call in a loop. Run `{UJ} ready --deadline 120` once and retry only after READY; for "
                f"long work use `{UJ} run`.")
    elif re.search(r"Connection failed after|may be reloading or not running|Request timed out|"
                   r"may be in a long domain reload|Submission retry deadline", reply, re.I):
        note = ("The Unity editor was unreachable (reload, crash, modal dialog or Safe Mode). Don't keep calling "
                f"MCP tools to wait. Run `{UJ} ready --deadline 120`; exit 4 means check Unity itself "
                "(dialog? `unity pipeline list` for Safe Mode?).")
    elif tool.endswith("unity_get_compilation_errors") and re.search(r'\\?"isCompiling\\?":\s*true', reply):
        note = (f"Unity is still compiling. Don't poll this tool; run `{UJ} compile`, which waits through the "
                "domain reload and prints compiler errors.")
    elif tool.endswith("unity_execute_code") and re.search(
            r"EditorApplication\s*\.\s*(update|playModeStateChanged)\s*\+=|StartCoroutine\s*\(",
            json.dumps(data.get("tool_input") or {}, ensure_ascii=False)):
        note = ("This started multi-frame work that a domain reload, Play Mode change or exception drops silently. "
                "Wait for it only with "
                f"`{UJ} waitfile DONE_FILE --heartbeat HB_FILE` (have the callback touch HB_FILE every tick or "
                "step and write DONE_FILE, or a first line `ERROR ...`, when finished). It returns as soon as the "
                "work dies instead of waiting out the timeout. Don't sleep-poll the file.")
    if note:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse",
                                                 "additionalContext": "unity-job-guard: " + note}}))
    sys.exit(0)
sys.exit(0)
