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
- **Session note + skill (`unity-long-jobs`)**: tells Claude where `ujob` is and how to use it.

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

**Other machines (after pushing this repo to GitHub):**

```json
"extraKnownMarketplaces": {
  "xin-unity-tools": { "source": { "source": "github", "repo": "<owner>/xin-unity-tools" } }
},
"enabledPlugins": { "unity-job-guard@xin-unity-tools": true }
```

Alternatively, use the desktop Code tab (**+ → Plugins → Add plugin**) or the CLI (`/plugin marketplace add <owner>/xin-unity-tools`, then `/plugin install unity-job-guard@xin-unity-tools`). Bump `version` in `plugin.json` when you push changes, so copies installed from GitHub update.
