---
name: unity-long-jobs
description: How to run and wait on Unity editor work without blind waits, and how several sessions share one Unity editor through the broker (leases, batched tests, coalesced compiles, exit codes 6/7). Use for any Unity eval that may take longer than ~20 s, recompiles, Play Mode, scene switches, waiting for the editor after Play Mode or a domain reload, long bakes or map/terrain builds, hands-on Play work (captures, perf windows), or whenever you are about to poll a result file, Editor.log or a background task's output for Unity.
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
ujob run --read -e 'return X.Status();'  # a lookup that changes nothing: never waits for other sessions
ujob wait <JOB_ID>                 # continue after exit 3 (T-<ticket> for broker requests)
ujob compile                       # refresh + recompile, wait through the domain reload, print compiler errors
ujob ready                         # wait until the editor is idle (after Play Mode / reload)
ujob waitfile DONE --heartbeat HB  # wait for multi-frame work (see below)
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
| 5 | compile errors (in your files, when other sessions are active); job not started | fix them, then `ujob compile` |
| 6 | not started: another session holds the editor, a long job runs, the user has unsaved scenes, or the broker is paused (the reason is printed) | do other work, retry later; check `ujob status` |
| 7 | ran on the last good build, which lacks your listed changes (another session's files are broken) | **not validated**: wait for the fix, compile, rerun |

Never treat 1, 2, 4 or 5 as "wait longer", and never report 7 as a pass.

## Several sessions, one editor: the broker

Every `ujob run`, `compile`, `test`, `lease` and `import` goes through the **broker**, a small per-project daemon `ujob` starts on demand (state in `%LOCALAPPDATA%/xin-unity-tools/broker/`). It decides when the shared editor compiles, switches scenes or enters Play Mode; it adds about 0.2 s per call.

- **Lookups never wait:** `ujob run --read` (or an eval that is exactly `return Agent.Status();` / `Help` / `Get` / `Find` ...) runs at once, also during another session's lease or test batch.
- **Tests:** `ujob test TAG|*|script.json|NAME --fixture [--session S] [--map M] [--play]` (AI Unit Testing projects). The broker compiles if code changed, boots once, runs, stops Play. It boots the map's data backend (sim scene) by default; `--play` (or `--visuals`) boots the full scene, which projects may reserve for final verification. Tests that need the full scene report not run on data. Requests from several sessions share one Play session; with two or more active sessions a request waits up to 60 s for others.
- **Hands-on Play or long edit work:** `ujob lease play MODE [--scene official|test|PATH] [--director|--aitest]` returns once Play is ready; then your `ujob run` calls (and Unity MCP calls) are the only mutating ones allowed. `ujob lease edit` does the same without Play. **Always `ujob lease release` when done.** A lease is released after 30 idle minutes (warning at 25; a running `ujob waitfile` counts as activity), when your session ends, or by `ujob broker release --force`.
- **Implicit lease:** an eval that boots Play or opens a scene (`AITestAgent.Boot`, `PlayModeAgent.Boot`, `DirectorAgent.Boot`, `OpenScene` ...) gives your session the lease; it is released 5 s after you are back in Edit Mode. The old `ujob run -e 'return AITestAgent.Boot();'` → Run → Stop flow keeps working.
- **Compiles:** requests coalesce (one compile serves everyone queued); "up to date" when nothing changed. With two or more active sessions an offline compile gates the real one, so one session's broken file never puts the editor into an error state: that session gets 5, others get 7 if their own changes are missing. `--force` skips the gate.
- **Automatic refresh is off** (the probe package): files you write compile only on `ujob compile`; non-script assets you write (PNG, FBX, JSON) need `ujob import PATH...`.
- **Agents first, with limits:** the broker stops a Play session nobody leases (e.g. started by hand) before its work, but never saves or discards unsaved scenes ("blocked: user" instead) and waits while a long editor job (Hub build, mountain chain, road paint...) is busy. `ujob broker pause` / `resume` holds all agent Unity work.
- **Memory gate:** a request that would enter Play (`ujob test`, `ujob lease play`, an eval that boots Play) is refused with exit 6 "memory too low to enter Play" when free commit memory is too low (full map scene: under 20 GB; test map or data backend: under 10 GB), and runs with a `WARNING memory` line when it is getting low. Don't retry or switch tools: tell the user the numbers and ask them to save and restart Unity. `ujob memory` shows the reading at any time; `ujob status` shows it too. `UJOB_MEMORY_GATE=off` turns refusals into warnings, only when the user says so.
- **Restart:** `ujob restart` quits and starts the editor again in the main lane (after running work, refused in Play). Only run it when the user asked for a restart. With unsaved scenes it stops with exit 6 and their list: ask the user, then rerun with `--save` (save them), `--backup` (save copies to `TempArtifacts/Backups/Scenes/<stamp>/`, scene files unchanged) or `--discard`. A dirty flag alone does not prove an edit. Other sessions' next calls wait for the new editor.
- **See who does what:** `ujob status`. Say what your session does with `ujob register --role implementation|testing|coordination --brief "..."`. Agents without a Claude session (Codex) pass `--as NAME`.
- **Unity MCP:** Play/scene/compile-triggering tools (`unity_play_mode`, `scene_open`, `script_create`, Play/Refresh menu items, execute_code that boots Play) need the lease; scene-changing tools wait while another session holds it or a batch runs. Raw `unity command eval/recompile/run_tests` in Bash is blocked; use ujob. Emergency bypass: `UJOB_DIRECT=1 ujob ...`.
- **Setup per project:** `ujob broker setup` installs the probe package (`Packages/com.xin.unity-broker-probe`, check it in) and sets "Recompile After Finished Playing"; then `ujob compile --force`. Project specifics (busy flags, boot keys, map scenes) live in `ProjectSettings/UnityBrokerProbe.json`.

## Without the editor: `offline-compile` and `offline-test`

Only one Unity editor can hold a project, so every compile and Edit Mode test queues behind it (a Unity compile on a big project is ~15 s of build plus 30-45 s of domain reload). Two `ujob` verbs do the common checks with no editor at all, so any number of sessions can run them at once, also while another session is driving Unity:

```bash
ujob offline-compile                 # changed project assemblies + their dependents (~1-20 s)
ujob offline-compile --all           # every assembly with sources under Assets/
ujob offline-compile --overlay DIR   # DIR mirrors project paths (DIR/Assets/...): check code before it goes into Assets/
ujob offline-test [--filter Fixture] # offline-compile, then run Edit Mode NUnit tests in plain .NET
```

- **How it works:** it replays Unity's own last compiler command lines (`Library/Bee/artifacts/*E.dag/*.rsp`) with Unity's Roslyn, after updating the source lists from disk. The defines, references and analyzers are the same as Unity's. Output goes to `TempArtifacts/Scratch/OfflineCompile/` and is removed after a clean run.
- **Tests:** they run against the real UnityEngine DLLs. A test whose code reaches the native engine (`ScriptableObject.CreateInstance`, `GameObject`, `SceneManager`, `AssetDatabase`, `Shader.PropertyToID`, `LogAssert`, `[UnityTest]`...) is reported as **needs-editor**, not as a failure. This includes an engine call the code catches itself and turns into null. Pure logic runs: rules, economy, planners, parsers, maths.
- **Exit codes:** 0 clean, 5 compile errors (printed as `path(line,col): error ...`), 1 test failures, 4 no compile data or no .NET SDK.
- **Limits:** it sees only assemblies Unity has compiled once (a brand-new asmdef needs one Unity compile). It does not see asmdef, package or define changes made since the last Unity compile, and it runs no IL post-processing. A test passing on CoreCLR can still differ on Mono.
- **Use:** use it as a fast inner loop. Before calling a change done, run one real `ujob compile` (and the editor tests, or the Play Mode tests that need Unity).

## Multi-frame work (Play Mode captures, `EditorApplication.update`, coroutines)

Some work has to span frames, so it can't run synchronously. Make it observable:
1. The callback touches a heartbeat file every tick or step (`File.WriteAllText(hb, DateTime.Now.ToString("o"))`).
2. When finished it writes the DONE file. On failure, the DONE file's first line starts with `ERROR`.
3. Wait with `ujob waitfile DONE --heartbeat HB [--stale 20]`.

`waitfile` returns:
- 0 when the file appears;
- 1 when the file reports `ERROR`;
- 2 as soon as the heartbeat goes stale, never starts, or the editor begins a recompile or domain reload;
- 3 at the deadline while the heartbeat is still fresh (rerun to keep waiting);
- 4 when the editor is unreachable.

## Background work: arm the staleness watchdog

A background Bash task that drives Unity can stall without ever exiting. Typical cases:
- a shell loop over several jobs that never starts the next one;
- a wait on a job that died;
- a command the user moved to the background.

Its completion notification then never comes, and the session waits on nothing. This has happened repeatedly.

**Whenever Unity work runs in the background, arm the watchdog in the same turn** (the plugin's hook reminds you):

```
Monitor(command: ujob stalewatch, description: "Unity job staleness", timeout_ms: 1800000)
```

- **Interval:** it checks every 60 s (`--interval`), the recommended cadence. Jobs touch their heartbeat every step, so a minute is enough to notice a dead one without flooding the chat.
- **Events:**

  | Event | Meaning |
  |---|---|
  | `STALE` | a job folder's heartbeat is older than 60 s (`--stale`) and it has no DONE: the job died |
  | `FINISHED` | a job wrote DONE (first line included) |
  | `EDITOR` | the editor is unreachable for 90 s, or started a recompile or domain reload |
  | `IDLE` | no job has run for 180 s (`--idle`), so whatever should start the next job has stalled; the watch then exits |
- **Scope:** it watches job folders with a `heartbeat` file under `<project>/TempArtifacts/Logs` (`--root` for elsewhere) and ignores folders untouched for 15 min.
- **Expiry:** a Monitor ends after at most 30 min. If the work is still going, re-arm it.
- **On `STALE` or `IDLE`:**
  1. Read the background task's output.
  2. Stop the task (TaskStop) if it is stuck.
  3. Check `ujob ready`, then continue by hand.
- **False `IDLE`:** a long synchronous `ujob run` has no heartbeat folder, so it reads as idle after 180 s. Raise `--idle` for those.

**Prefer one job per tool call over one shell loop.** Start each run with `ujob run`, wait on it with `ujob waitfile` (with a `--deadline`), then start the next. Never `eval` a command string parsed out of another command's output: when the parsing misses, the loop hangs silently.

## Unity MCP tools (AnkleBreaker and similar)

- `unity_execute_code` with `EditorApplication.delayCall` is blocked. Run the work synchronously through `ujob run`.
- After a Unity MCP reply that shows a busy main thread, an unreachable editor, an unknown outcome or a compile in progress, the hook adds a note. Follow it: one bounded `ujob ready` or `ujob compile`, not repeated MCP calls. An "outcome unknown" reply means the request may already have run, so inspect before re-sending.
- Hooks can't shorten an MCP call that is already waiting. How long a call blocks is decided by the server's retry policy. AnkleBreaker server 2.30 can block a single call for over 10 minutes when the editor is busy; 2.36+ bounds its retries.

## Rules

- No `until [ -f result ]; do sleep …; done`, and no sleep loops on `Editor.log` or a background task's `.output`. The plugin's hook blocks these in Unity projects. Add `# poll-ok` only for a loop that has both a deadline and a failure check.
- Unity work in the background always gets a `ujob stalewatch` Monitor (see above).
- After entering or leaving Play Mode, run `ujob ready` before the next job. Domain reloads drop in-flight jobs.
- To wait for a runtime state in Play Mode, query it once per `ujob run -e` call and decide in between. Don't loop inside Unity.
- One long job per editor at a time. The main thread is serial, so another session's long job makes your calls time out.
- Short calls (well under 20 s) can still use `unity command eval ... --timeout 30` directly.

## Requirements

- Unity CLI on PATH with detached jobs (`unity command ... --detach`, `unity job status`).
- The `com.unity.pipeline` package in the project.
- Python 3.8+.
