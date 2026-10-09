---
name: unity-long-jobs
description: How to run and wait on Unity editor work without blind waits. Use for any Unity eval that may take longer than ~20 s, recompiles, waiting for the editor after Play Mode or a domain reload, long bakes or map/terrain builds, or whenever you are about to poll a result file, Editor.log or a background task's output for Unity.
---

# Waiting on Unity without dead waits

Blind waits are the failure to avoid: queuing work in Unity, then sleep-polling for a result that never comes. The job is lost when:
- `EditorApplication.delayCall`, `update` callbacks or coroutines are wiped by a domain reload, Play Mode entry or scene switch;
- the launch itself fails (main-thread timeout, network error, compile error) and the loop waits anyway;
- the loop only checks for success, so a dead job looks the same as a slow one until the 600 s tool limit.

## Use `ujob`

`ujob` is on the Bash PATH while this plugin is enabled. From PowerShell or another shell, run `python "${CLAUDE_PLUGIN_ROOT}/scripts/ujob.py" ...`.

```bash
ujob run path/to/job.cs            # eval_file as a detached CLI job; waits up to 540 s
ujob run -e 'return Foo.Bar();'    # inline code
ujob wait <JOB_ID>                 # continue after exit 3
ujob compile                       # recompile, wait through the domain reload, print compiler errors
ujob ready                         # wait until the editor is idle (after Play Mode / reload)
```

Options are `--deadline SECONDS` (default 540, under the Bash tool limit) and `--project PATH` (default: the Unity project containing the current directory).

Write the job to do its work **synchronously** and `return` the result. Detached jobs are not limited by the CLI's 30 s main-thread timeout.

| Exit | Meaning | Next step |
|---|---|---|
| 0 | done | use the printed result |
| 1 | the snippet threw or did not compile | fix the snippet |
| 2 | job lost (reload, Play Mode, restart): it did **not** finish | `ujob ready`, then rerun |
| 3 | deadline hit, still running | `ujob wait <JOB_ID>` |
| 4 | editor unreachable or no Unity CLI | check Unity: modal dialog, Safe Mode (`unity pipeline list`), crash |
| 5 | project has compile errors; job not started | fix them, then `ujob compile` |

Never treat 1, 2, 4 or 5 as "wait longer".

## Rules

- No `until [ -f result ]; do sleep …; done`, and no sleep loops on `Editor.log` or a background task's `.output`. The plugin's hook blocks these in Unity projects. Add `# poll-ok` only for a loop that has both a deadline and a failure check.
- After entering or leaving Play Mode, run `ujob ready` before the next job. Domain reloads drop in-flight jobs.
- To wait for a runtime state in Play Mode, query it once per `ujob run -e` call and decide in between. Don't loop inside Unity.
- One long job per editor at a time. The main thread is serial, so another session's long job makes your calls time out.
- Short calls (well under 20 s) can still use `unity command eval ... --timeout 30` directly.

## Requirements

- Unity CLI on PATH with detached jobs (`unity command ... --detach`, `unity job status`).
- The `com.unity.pipeline` package in the project.
- Python 3.8+.
