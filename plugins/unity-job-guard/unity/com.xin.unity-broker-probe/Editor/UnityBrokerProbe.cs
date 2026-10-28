// Editor probe for the unity-job-guard broker (xin-unity-tools).
//
// Writes Library/UnityBrokerProbe/state.json about once a second and on every compile / Play / scene event, so the
// broker (a Python process outside Unity) works from what the editor actually did instead of its own bookkeeping:
// editor epoch, compiles and their errors, Play state, unsaved scenes and Prefab Mode, pending boot jobs, busy long
// runners and recent Unity MCP actions. It also keeps automatic asset refresh off (AssetDatabase.DisallowAutoRefresh)
// so a file written by any session compiles only when someone asks for it (ujob compile / Ctrl+R).
//
// It must never break the build: no compile-time references to project or package code. Project-specific things
// (busy flags, boot-job SessionState keys) come from ProjectSettings/UnityBrokerProbe.json and are read by
// reflection; the Unity MCP's action history is hooked by reflection too. Anything missing is reported, not thrown.
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Reflection;
using UnityEditor;
using UnityEditor.Compilation;
using UnityEditor.SceneManagement;
using UnityEngine;
using UnityEngine.SceneManagement;

namespace Xin.UnityBroker
{
    [InitializeOnLoad]
    public static class UnityBrokerProbe
    {
        const string Dir = "Library/UnityBrokerProbe";
        const string StatePath = Dir + "/state.json";
        const string ConfigPath = "ProjectSettings/UnityBrokerProbe.json";
        const string KeyEpoch = "XinBroker.Epoch";
        const string KeyCompile = "XinBroker.LastCompile";
        const string KeyDirty = "XinBroker.DirtySince";
        const string KeyPlay = "XinBroker.PlayChanged";
        const string KeyDisallow = "XinBroker.DisallowCount";
        const string PrefGuardOff = "XinBroker.AutoRefreshGuardOff";

        [Serializable] public class CompileError { public string file; public int line; public string message; }

        [Serializable]
        public class CompileInfo
        {
            public double startedUnix, finishedUnix;
            public bool running, succeeded;
            public int errorCount;
            public List<CompileError> errors = new List<CompileError>();
        }

        [Serializable] public class SceneInfo { public string path; public bool dirty; public double dirtySinceUnix; }
        [Serializable] public class KeyValue { public string key; public string value; }
        [Serializable] public class BusyInfo { public string name; public bool busy; public string error; }
        [Serializable] public class McpAction { public double unix; public string agent; public string action; public string category; public string status; }

        [Serializable]
        public class ProbeState
        {
            public int schema = 1;
            public int editorPid;
            public double editorStartUnix;
            public int epoch;
            public double loadedUnix, writtenUnix;
            public string projectPath;
            public bool playing, paused, compiling, updating, compileFailed;
            public string playState;
            public double playChangedUnix;
            public CompileInfo lastCompile = new CompileInfo();
            public string activeScene;
            public List<SceneInfo> scenes = new List<SceneInfo>();
            public string prefabStagePath;
            public bool prefabStageDirty;
            public List<KeyValue> bootJobs = new List<KeyValue>();
            public List<BusyInfo> busy = new List<BusyInfo>();
            public List<McpAction> mcp = new List<McpAction>();
            public bool mcpHooked;
            public bool autoRefreshGuard;
            public int autoRefreshDisallowCount;
            public List<string> problems = new List<string>();
        }

        [Serializable] public class BusyFlag { public string name; public string type; public string member; }
        [Serializable] class Config { public List<BusyFlag> busy = new List<BusyFlag>(); public List<string> bootKeys = new List<string>(); }
        [Serializable] class DirtyMap { public List<SceneInfo> items = new List<SceneInfo>(); }

        static readonly ProbeState State = new ProbeState();
        static readonly List<string> ConfigProblems = new List<string>();
        static Config config = new Config();
        static readonly List<(BusyFlag flag, Func<bool> read, string error)> busyReaders = new List<(BusyFlag, Func<bool>, string)>();
        static readonly List<McpAction> mcpRing = new List<McpAction>();
        static double nextWrite;

        static UnityBrokerProbe()
        {
            try
            {
                using (Process p = Process.GetCurrentProcess())
                {
                    State.editorPid = p.Id;
                    State.editorStartUnix = ToUnix(p.StartTime.ToUniversalTime());
                }
                State.epoch = SessionState.GetInt(KeyEpoch, 0) + 1;
                SessionState.SetInt(KeyEpoch, State.epoch);
                State.loadedUnix = Now();
                State.projectPath = Directory.GetCurrentDirectory().Replace('\\', '/');
                LoadCompile();
                State.playChangedUnix = SessionStateDouble(KeyPlay);

                LoadConfig();
                HookMcp();
                ApplyAutoRefreshGuard();

                CompilationPipeline.compilationStarted += OnCompileStarted;
                CompilationPipeline.assemblyCompilationFinished += OnAssemblyCompiled;
                CompilationPipeline.compilationFinished += OnCompileFinished;
                EditorApplication.playModeStateChanged += OnPlayModeChanged;
                EditorApplication.update += OnUpdate;
                EditorSceneManager.sceneSaved += _ => Write();
                EditorSceneManager.sceneOpened += (_, __) => Write();
                Write();
            }
            catch (Exception e)
            {
                UnityEngine.Debug.LogWarning("[UnityBrokerProbe] start-up problem (the probe keeps running): " + e.Message);
            }
        }

        /// <summary>The current probe state as JSON (also what state.json holds).</summary>
        public static string StatusJson() { Refresh(); return JsonUtility.ToJson(State, true); }

        // ── Auto refresh ───────────────────────────────────────────────────────────────────────

        /// <summary>
        /// Turn the agent auto-refresh guard on or off for this project on this machine (stored in EditorPrefs, so it
        /// survives restarts). DisallowAutoRefresh is a native counter that lives as long as the editor process; the
        /// probe remembers in SessionState how many times it called it and balances with AllowAutoRefresh exactly.
        /// </summary>
        public static string SetAutoRefreshGuard(bool on)
        {
            EditorPrefs.SetBool(PrefKey(PrefGuardOff), !on);
            ApplyAutoRefreshGuard();
            Write();
            return on ? "auto-refresh guard on: files compile/import only on an explicit refresh (Ctrl+R, ujob compile, ujob import)"
                      : "auto-refresh guard off: Unity refreshes automatically again";
        }

        [MenuItem("Tools/Unity Broker/Auto-refresh guard on")]
        static void MenuGuardOn() => UnityEngine.Debug.Log("[UnityBrokerProbe] " + SetAutoRefreshGuard(true));

        [MenuItem("Tools/Unity Broker/Auto-refresh guard off")]
        static void MenuGuardOff() => UnityEngine.Debug.Log("[UnityBrokerProbe] " + SetAutoRefreshGuard(false));

        static void ApplyAutoRefreshGuard()
        {
            bool want = !EditorPrefs.GetBool(PrefKey(PrefGuardOff), false);
            int count = SessionState.GetInt(KeyDisallow, 0);
            if (want && count == 0)
            {
                AssetDatabase.DisallowAutoRefresh();
                count = 1;
            }
            else if (!want && count > 0)
            {
                for (int i = 0; i < count; i++) AssetDatabase.AllowAutoRefresh();
                count = 0;
            }
            SessionState.SetInt(KeyDisallow, count);
            State.autoRefreshGuard = want;
            State.autoRefreshDisallowCount = count;
        }

        // EditorPrefs are per user, not per project: scope the key to this project.
        static string PrefKey(string key)
        {
            uint h = 2166136261; // FNV-1a: stable across runtimes, unlike string.GetHashCode
            foreach (char c in Directory.GetCurrentDirectory().Replace('\\', '/').ToLowerInvariant()) h = (h ^ c) * 16777619;
            return key + "." + h.ToString("x8");
        }

        // ── Events ─────────────────────────────────────────────────────────────────────────────

        static void OnCompileStarted(object _)
        {
            State.lastCompile = new CompileInfo { startedUnix = Now(), running = true };
            SaveCompile();
            Write();
        }

        static void OnAssemblyCompiled(string assembly, CompilerMessage[] messages)
        {
            foreach (CompilerMessage m in messages)
            {
                if (m.type != CompilerMessageType.Error) continue;
                State.lastCompile.errorCount++;
                if (State.lastCompile.errors.Count < 50)
                    State.lastCompile.errors.Add(new CompileError { file = (m.file ?? "").Replace('\\', '/'), line = m.line, message = m.message });
            }
            SaveCompile();
        }

        static void OnCompileFinished(object _)
        {
            State.lastCompile.running = false;
            State.lastCompile.finishedUnix = Now();
            State.lastCompile.succeeded = State.lastCompile.errorCount == 0;
            SaveCompile();
            Write();
        }

        static void OnPlayModeChanged(PlayModeStateChange change)
        {
            State.playState = change.ToString();
            State.playChangedUnix = Now();
            SessionState.SetString(KeyPlay, State.playChangedUnix.ToString("R", System.Globalization.CultureInfo.InvariantCulture));
            Write();
        }

        static void OnUpdate()
        {
            if (Now() < nextWrite) return;
            Write();
        }

        // ── State ──────────────────────────────────────────────────────────────────────────────

        static void Refresh()
        {
            State.playing = EditorApplication.isPlaying;
            State.paused = EditorApplication.isPaused;
            State.compiling = EditorApplication.isCompiling;
            State.updating = EditorApplication.isUpdating;
            State.compileFailed = EditorUtility.scriptCompilationFailed;
            if (string.IsNullOrEmpty(State.playState)) State.playState = State.playing ? "Playing" : "Edit";
            State.activeScene = SceneManager.GetActiveScene().path;

            DirtyMap since = LoadDirty();
            var scenes = new List<SceneInfo>();
            for (int i = 0; i < SceneManager.sceneCount; i++)
            {
                Scene s = SceneManager.GetSceneAt(i);
                var info = new SceneInfo { path = s.path, dirty = s.isDirty };
                if (s.isDirty)
                {
                    SceneInfo known = since.items.FirstOrDefault(x => x.path == s.path);
                    info.dirtySinceUnix = known != null && known.dirtySinceUnix > 0 ? known.dirtySinceUnix : Now();
                }
                scenes.Add(info);
            }
            State.scenes = scenes;
            SaveDirty(new DirtyMap { items = scenes.Where(x => x.dirty).ToList() });

            PrefabStage stage = PrefabStageUtility.GetCurrentPrefabStage();
            State.prefabStagePath = stage != null ? stage.assetPath : null;
            State.prefabStageDirty = stage != null && stage.scene.isDirty;

            State.bootJobs = config.bootKeys
                .Select(k => new KeyValue { key = k, value = SessionState.GetString(k, "") })
                .Where(kv => !string.IsNullOrEmpty(kv.value)).ToList();

            State.busy = busyReaders.Select(r =>
            {
                if (r.read == null) return new BusyInfo { name = r.flag.name, busy = false, error = r.error };
                try { return new BusyInfo { name = r.flag.name, busy = r.read() }; }
                catch (Exception e) { return new BusyInfo { name = r.flag.name, busy = false, error = e.GetType().Name + ": " + e.Message }; }
            }).ToList();

            State.mcp = mcpRing.ToList();
            State.problems = ConfigProblems.ToList();
        }

        static void Write()
        {
            nextWrite = Now() + 1.0;
            try
            {
                Refresh();
                State.writtenUnix = Now();
                Directory.CreateDirectory(Dir);
                string tmp = StatePath + ".tmp";
                File.WriteAllText(tmp, JsonUtility.ToJson(State, true));
                if (File.Exists(StatePath)) File.Replace(tmp, StatePath, null);
                else File.Move(tmp, StatePath);
            }
            catch (IOException) { /* a reader holds the file; the next tick writes again */ }
            catch (UnauthorizedAccessException) { }
            catch (Exception e) { UnityEngine.Debug.LogWarning("[UnityBrokerProbe] write failed: " + e.Message); }
        }

        // ── Config and reflection ──────────────────────────────────────────────────────────────

        static void LoadConfig()
        {
            ConfigProblems.Clear();
            busyReaders.Clear();
            if (File.Exists(ConfigPath))
            {
                try { config = JsonUtility.FromJson<Config>(File.ReadAllText(ConfigPath)) ?? new Config(); }
                catch (Exception e) { ConfigProblems.Add($"{ConfigPath}: {e.Message}"); config = new Config(); }
            }
            else ConfigProblems.Add($"{ConfigPath} missing: no busy flags or boot keys are watched");

            foreach (BusyFlag flag in config.busy ?? new List<BusyFlag>())
            {
                if (string.IsNullOrEmpty(flag.name)) flag.name = flag.type + "." + flag.member;
                Type t = FindType(flag.type);
                if (t == null) { Add(flag, null, $"type {flag.type} not found"); continue; }
                const BindingFlags F = BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Static;
                PropertyInfo p = t.GetProperty(flag.member, F);
                FieldInfo f = p == null ? t.GetField(flag.member, F) : null;
                Type valueType = p?.PropertyType ?? f?.FieldType;
                if (valueType != typeof(bool)) { Add(flag, null, $"{flag.type}.{flag.member} is not a static bool"); continue; }
                Add(flag, p != null ? (Func<bool>)(() => (bool)p.GetValue(null)) : () => (bool)f.GetValue(null), null);
            }

            void Add(BusyFlag flag, Func<bool> read, string error)
            {
                busyReaders.Add((flag, read, error));
                if (error != null) ConfigProblems.Add("busy flag " + flag.name + ": " + error);
            }
        }

        static Type FindType(string name)
        {
            if (string.IsNullOrEmpty(name)) return null;
            foreach (System.Reflection.Assembly a in AppDomain.CurrentDomain.GetAssemblies())
            {
                Type t = a.GetType(name, false);
                if (t != null) return t;
            }
            foreach (System.Reflection.Assembly a in AppDomain.CurrentDomain.GetAssemblies())
            {
                Type[] types;
                try { types = a.GetTypes(); } catch (ReflectionTypeLoadException e) { types = e.Types.Where(x => x != null).ToArray(); } catch { continue; }
                Type t = types.FirstOrDefault(x => x.Name == name);
                if (t != null) return t;
            }
            return null;
        }

        static void HookMcp()
        {
            try
            {
                Type history = FindType("UnityMCP.Editor.MCPActionHistory");
                EventInfo ev = history?.GetEvent("OnActionRecorded", BindingFlags.Public | BindingFlags.Static);
                if (ev == null) { State.mcpHooked = false; return; }
                MethodInfo handler = typeof(UnityBrokerProbe).GetMethod(nameof(OnMcpAction), BindingFlags.NonPublic | BindingFlags.Static);
                ev.AddEventHandler(null, Delegate.CreateDelegate(ev.EventHandlerType, handler));
                State.mcpHooked = true;
            }
            catch (Exception e)
            {
                State.mcpHooked = false;
                ConfigProblems.Add("MCP action hook: " + e.Message);
            }
        }

        static void OnMcpAction(object record)
        {
            try
            {
                Type t = record.GetType();
                string S(string prop) => t.GetProperty(prop)?.GetValue(record)?.ToString();
                mcpRing.Add(new McpAction { unix = Now(), agent = S("AgentId"), action = S("ActionName"), category = S("Category"), status = S("Status") });
                if (mcpRing.Count > 50) mcpRing.RemoveAt(0);
            }
            catch { }
        }

        // ── Persistence across domain reloads (SessionState lives as long as the editor process) ──

        static void SaveCompile() => SessionState.SetString(KeyCompile, JsonUtility.ToJson(State.lastCompile));

        static void LoadCompile()
        {
            string json = SessionState.GetString(KeyCompile, "");
            if (string.IsNullOrEmpty(json)) return;
            try { State.lastCompile = JsonUtility.FromJson<CompileInfo>(json) ?? new CompileInfo(); } catch { }
            // A compile that was "running" when the domain reloaded has finished (successfully, or the reload would not happen).
            if (State.lastCompile.running)
            {
                State.lastCompile.running = false;
                State.lastCompile.finishedUnix = Now();
                State.lastCompile.succeeded = State.lastCompile.errorCount == 0;
                SaveCompile();
            }
        }

        static DirtyMap LoadDirty()
        {
            try { return JsonUtility.FromJson<DirtyMap>(SessionState.GetString(KeyDirty, "{}")) ?? new DirtyMap(); }
            catch { return new DirtyMap(); }
        }

        static void SaveDirty(DirtyMap map) => SessionState.SetString(KeyDirty, JsonUtility.ToJson(map));

        static double SessionStateDouble(string key) =>
            double.TryParse(SessionState.GetString(key, ""), System.Globalization.NumberStyles.Float, System.Globalization.CultureInfo.InvariantCulture, out double v) ? v : 0;

        static double Now() => ToUnix(DateTime.UtcNow);
        static double ToUnix(DateTime utc) => (utc - new DateTime(1970, 1, 1, 0, 0, 0, DateTimeKind.Utc)).TotalSeconds;
    }
}
