# xin-unity-tools

A personal Claude Code plugin marketplace for Unity work.

## unity-job-guard

Stops Claude Code from waiting forever on Unity.

- **`ujob`** (on the Bash PATH while the plugin is enabled): runs editor work as a detached Unity CLI job. It always returns: done (0), failed (1), lost to a domain reload (2), deadline (3), no editor (4) or compile errors (5). It also covers `compile` and `ready` waits.
- **`ujob waitfile DONE --heartbeat HB`**: waits for multi-frame work (Play Mode captures, update callbacks). It returns as soon as the heartbeat stops or a domain reload starts.
- **Shell hook**: in Unity projects, blocks sleep-poll loops on result files, `Editor.log` or background task output. Those loops waited the full 10 minutes whenever the Unity job had silently died.
- **Unity MCP hooks** (any server with `unity_*` tools, e.g. AnkleBreaker):
  - block `execute_code` that defers work with `EditorApplication.delayCall`;
  - after replies that show a busy or unreachable editor, an unknown outcome, a compile in progress or newly started multi-frame work, tell Claude to use a bounded `ujob` wait instead of re-calling.
- **Broker (0.5.0)**: lets several sessions share one editor. A per-project daemon, started on demand, schedules every `ujob run/compile/test/lease/import`:
  - **compiles** coalesce, with an offline gate so one session's broken file never breaks the others;
  - **AI tests** (`ujob test`) from several sessions share one Play session;
  - **hands-on work** gets an exclusive lease (`ujob lease play|edit|release`);
  - **read-only lookups** (`ujob run --read`) never wait.

  New exit codes: 6 (not started, reason printed) and 7 (ran on a stale build: not validated). `ujob status` shows who holds the editor.
- **Memory gate and restart (0.6.0)**: a request that would enter Play is refused (exit 6) when free commit memory is too low (full map scene: under 20 GB free; test map or data backend: under 10 GB) and runs with a warning under 30 GB, above 30 GB editor memory or after 24 h uptime. At the commit limit, leaving Play can fail to read the scene backup, close the editor and lose unsaved scenes. `ujob memory` shows the reading; `ujob restart [--save|--backup|--discard]` quits and starts the editor in the main lane, after the user chose what happens to unsaved scenes. Thresholds: `"broker": {"memory": {...}}` in `ProjectSettings/UnityBrokerProbe.json`.
- **Unity probe package** (`unity/com.xin.unity-broker-probe`, installed into a project with `ujob broker setup`): reports the editor's real state to the broker and keeps automatic asset refresh off. Project specifics go in `ProjectSettings/UnityBrokerProbe.json`.
- **Offline checks**: `ujob offline-compile` / `ujob offline-test` compile and run pure Edit Mode tests without the editor.
- **Broker hooks**: register and end sessions, remind lease holders, record which session wrote each script, keep Play/scene/compile-triggering Unity MCP calls to the lease holder (failing closed) and block raw `unity command eval/recompile` that bypasses the broker.
- **Session note + skill (`unity-long-jobs`)**: tells Claude where `ujob` is and how to use it.
- **Tests**: `python -m pytest plugins/unity-job-guard/tests` (scheduler logic against a fake editor).

Outside Unity projects the hooks do nothing. A Unity project is a folder with `ProjectSettings/ProjectVersion.txt`.

### Requirements on each machine

- Claude Code (CLI or desktop Code tab; plugins don't run in WSL sessions)
- Python 3.8+ on PATH as `python3`, `python` or `py`
- The Unity CLI (`unity`) with detached jobs, and `com.unity.pipeline` in each Unity project
- Git Bash on Windows (it comes with Git for Windows)

### Install

**This machine (local folder):** add to `~/.claude/settings.json`:

```json
"extraKnownMarketplaces": {
  "xin-unity-tools": { "source": { "source": "directory", "path": "C:/Users/xindi/ClaudePlugins/xin-unity-tools" } }
},
"enabledPlugins": { "unity-job-guard@xin-unity-tools": true }
```

Edits to the folder load at the next session start, or with `/reload-plugins`.

**Other machines:** the repo is private (`xin0898/xin-unity-tools`), so first give Git access to GitHub on that machine:

```bash
gh auth login
```

```bash
gh auth setup-git
```

(Install the GitHub CLI first, e.g. `winget install GitHub.cli` on Windows or `brew install gh` on macOS.) Then add to `~/.claude/settings.json`:

```json
"extraKnownMarketplaces": {
  "xin-unity-tools": { "source": { "source": "github", "repo": "xin0898/xin-unity-tools" } }
},
"enabledPlugins": { "unity-job-guard@xin-unity-tools": true }
```

Alternatively, use the desktop Code tab (**+ → Plugins → Add plugin**) or the CLI (`/plugin marketplace add xin0898/xin-unity-tools`, then `/plugin install unity-job-guard@xin-unity-tools`).

### Updating

Edit the files here, bump `version` in `plugins/unity-job-guard/.claude-plugin/plugin.json`, commit and `git push`. This machine loads the local folder directly (`/reload-plugins`); machines installed from GitHub pick up the new version on their next update.
