"""Compile a Unity project's own C# assemblies without the Unity editor.

Unity keeps the exact compiler command line of its last compile in
Library/Bee/artifacts/<dag>/<Assembly>.rsp. This replays those response files with Unity's own
Roslyn (Editor/Data/DotNetSdkRoslyn/csc.dll), after bringing the source lists up to date with the
files on disk (new .cs files join the assembly Unity would put them in, deleted ones drop out).
Nothing in Assets/ or Library/ is written: output goes to TempArtifacts/Scratch/OfflineCompile/<run>/
and is deleted afterwards. Any number of these can run in parallel with each other and with a
Unity editor that has the project open.

  ujob offline-compile [--all] [--assembly NAME]... [--overlay DIR] [--keep] [--max-errors N]

    (default)        compile the project assemblies (sources under Assets/) whose files changed since
                     Unity's last compile, plus every project assembly that references them
    --all            compile every project assembly
    --assembly NAME  compile this assembly (repeatable) plus its dependents
    --overlay DIR    DIR mirrors project paths (DIR/Assets/...); its .cs files replace or add to the
                     real ones for this compile only. Use it to check code before it goes into Assets/,
                     e.g. while another session is driving Unity.
    --keep           keep the output folder (DLLs, rewritten .rsp files, compiler log)

Exit codes: 0 compiled clean, 5 compile errors (printed), 4 no Unity compile data or compiler,
64 usage. Limits: it knows only assemblies Unity has compiled at least once (a brand-new asmdef needs
one Unity compile first), it does not run IL post-processors, and asmdef/package/define changes made
since the last Unity compile are not seen.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

ERR_RE = re.compile(r"^(?P<file>.*?)\((?P<line>\d+),(?P<col>\d+)\): (?P<kind>error|warning) (?P<code>\w+): (?P<msg>.*)$")
SKIP_DIRS = {".git", "node_modules"}


def say(msg):
    print(msg, flush=True)


def unity_install(project):
    ver = None
    with open(os.path.join(project, "ProjectSettings", "ProjectVersion.txt"), encoding="utf-8") as f:
        for line in f:
            if line.startswith("m_EditorVersion:"):
                ver = line.split(":", 1)[1].strip()
    cands = [os.environ.get("UNITY_EDITOR_DATA")] if os.environ.get("UNITY_EDITOR_DATA") else []
    cands += [rf"C:\Program Files\Unity\Hub\Editor\{ver}\Editor\Data",
              f"/Applications/Unity/Hub/Editor/{ver}/Unity.app/Contents",
              os.path.expanduser(f"~/Unity/Hub/Editor/{ver}/Editor/Data")]
    for d in cands:
        if d and os.path.isfile(os.path.join(d, "DotNetSdkRoslyn", "csc.dll")):
            return d
    return None


def find_dag(project):
    """The editor dag (…E.dag) holding Assembly-CSharp.rsp, newest first."""
    root = os.path.join(project, "Library", "Bee", "artifacts")
    if not os.path.isdir(root):
        return None
    dags = [os.path.join(root, d) for d in os.listdir(root)
            if d.endswith("E.dag") and os.path.isfile(os.path.join(root, d, "Assembly-CSharp.rsp"))]
    return max(dags, key=os.path.getmtime) if dags else None


class Asm:
    def __init__(self, name, rsp_path):
        self.name = name
        self.rsp_path = rsp_path
        self.args, self.sources, self.refs = [], [], []
        with open(rsp_path, encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                if line.startswith('"') and line.endswith('.cs"'):
                    self.sources.append(line[1:-1])
                elif line.startswith("-r:"):
                    self.refs.append(line[3:].strip('"'))
                elif line.startswith(("-out:", "-refout:")):
                    continue
                else:
                    self.args.append(line)
        self.dll = os.path.join(os.path.dirname(rsp_path), name + ".dll")

    @property
    def owned(self):
        return any(s.startswith("Assets/") for s in self.sources)


def load_assemblies(dag):
    out = {}
    for fn in os.listdir(dag):
        if fn.endswith(".rsp") and not fn.endswith(".mvfrm.rsp"):
            name = fn[:-4]
            out[name] = Asm(name, os.path.join(dag, fn))
    return out


def scan_cs(project):
    """Every .cs under Assets/ as a project-relative forward-slash path, plus asmdef folders."""
    files, asmdirs = [], {}
    base = os.path.join(project, "Assets")
    for d, dirs, fns in os.walk(base):
        dirs[:] = [x for x in dirs if x not in SKIP_DIRS and not x.endswith("~") and not x.startswith(".")]
        rel = os.path.relpath(d, project).replace("\\", "/")
        for fn in fns:
            if fn.endswith(".cs"):
                files.append(f"{rel}/{fn}")
            elif fn.endswith((".asmdef", ".asmref")):
                asmdirs[rel] = os.path.join(d, fn)
    return files, asmdirs


def asmdef_name(path, cache={}):
    if path not in cache:
        try:
            with open(path, encoding="utf-8-sig") as f:
                data = json.load(f)
            cache[path] = data.get("name") or data.get("reference", "").replace("GUID:", "") or None
        except (OSError, ValueError):
            cache[path] = None
    return cache[path]


def owner_of(rel, asmdirs, asms):
    """Which assembly Unity would compile a new file into."""
    d = rel.rsplit("/", 1)[0]
    while d:
        if d in asmdirs:
            return asmdef_name(asmdirs[d])
        d = d.rsplit("/", 1)[0] if "/" in d else ""
    parts = rel.split("/")
    editor = "Editor" in parts[:-1]
    first = len(parts) > 1 and parts[1] in ("Plugins", "Standard Assets", "Pro Standard Assets")
    name = "Assembly-CSharp" + ("-Editor" if editor else "") + ("-firstpass" if first else "")
    return name if name in asms else ("Assembly-CSharp-Editor" if editor else "Assembly-CSharp")


def parse_args(args, extra=()):
    """--all, --keep, --assembly NAME, --overlay DIR, --max-errors N, plus `extra` options taking a value."""
    opts = {"all": False, "keep": False, "named": [], "overlay": None, "max_errors": 40}
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("--all", "--keep"):
            opts[a[2:]] = True; i += 1
        elif a == "--assembly" and i + 1 < len(args):
            opts["named"].append(args[i + 1]); i += 2
        elif a == "--overlay" and i + 1 < len(args):
            opts["overlay"] = os.path.abspath(args[i + 1]); i += 2
        elif a == "--max-errors" and i + 1 < len(args):
            opts["max_errors"] = int(args[i + 1]); i += 2
        elif a in extra and i + 1 < len(args):
            opts[a[2:]] = args[i + 1]; i += 2
        else:
            return None, a
    return opts, None


def main(project, args):
    opts, bad = parse_args(args)
    if bad:
        say(f"offline-compile: unknown argument {bad}\n{__doc__}")
        return 64
    return build(project, opts)[0]


def build(project, opts):
    """Compile; returns (exit code, context). Context has out_dir, owned assemblies, built {name: dll}."""
    want_all, keep, named, overlay, max_errors = (opts["all"], opts["keep"], opts["named"],
                                                  opts["overlay"], opts["max_errors"])
    ctx = {"out_dir": None, "owned": {}, "built": {}, "dag": None}
    started = time.time()
    data = unity_install(project)
    if not data:
        say("NO COMPILER: Unity's DotNetSdkRoslyn/csc.dll for this project's editor version was not found "
            "(set UNITY_EDITOR_DATA to <editor>/Editor/Data).")
        return (4, ctx)
    dotnet = os.path.join(data, "NetCoreRuntime", "dotnet.exe" if os.name == "nt" else "dotnet")
    if not os.path.isfile(dotnet):
        dotnet = shutil.which("dotnet")
    csc = os.path.join(data, "DotNetSdkRoslyn", "csc.dll")
    dag = find_dag(project)
    if not dag:
        say("NO COMPILE DATA: Library/Bee has no editor compile yet. Open the project in Unity once.")
        return (4, ctx)
    asms = load_assemblies(dag)
    owned = {n: a for n, a in asms.items() if a.owned}
    ctx["owned"], ctx["dag"] = owned, dag

    # Bring source lists up to date with the disk (+ overlay).
    disk, asmdirs = scan_cs(project)
    overlay_files = {}
    if overlay:
        if not os.path.isdir(overlay):
            say(f"offline-compile: no overlay folder {overlay}")
            return (64, ctx)
        for d, _, fns in os.walk(overlay):
            for fn in fns:
                if fn.endswith(".cs"):
                    full = os.path.join(d, fn)
                    overlay_files[os.path.relpath(full, overlay).replace("\\", "/")] = full
    present = set(disk) | set(overlay_files)
    known = {s for a in owned.values() for s in a.sources}
    added, unknown_owner = {}, []
    for rel in sorted(present - known):
        if not rel.startswith("Assets/"):
            continue
        o = owner_of(rel, asmdirs, asms)
        if o in owned or o in asms:
            added.setdefault(o, []).append(rel)
        else:
            unknown_owner.append((rel, o))
    changed = set()
    for name, a in owned.items():
        before = len(a.sources)
        a.sources = [s for s in a.sources if s in present or not s.startswith("Assets/")]
        if len(a.sources) != before:
            changed.add(name)
        if name in added:
            a.sources += added[name]
            changed.add(name)
        if name not in changed:
            stamp = os.path.getmtime(a.dll) if os.path.isfile(a.dll) else 0
            def newer(s):
                if s in overlay_files:
                    return True
                full = os.path.join(project, s)
                return s.startswith("Assets/") and os.path.exists(full) and os.path.getmtime(full) > stamp
            if any(newer(s) for s in a.sources):
                changed.add(name)

    # Pick targets: requested/changed assemblies and everything in the project that references them.
    by_ref = {n: {os.path.basename(r)[:-len(".ref.dll")] for r in a.refs if r.endswith(".ref.dll")}
              for n, a in owned.items()}
    seeds = set(owned) if want_all else (set(named) if named else changed)
    missing = [n for n in seeds if n not in owned]
    if missing:
        say(f"offline-compile: not a project assembly Unity has compiled: {', '.join(missing)}")
        return (64, ctx)
    targets, frontier = set(seeds), list(seeds)
    while frontier:
        cur = frontier.pop()
        for n, deps in by_ref.items():
            if cur in deps and n not in targets:
                targets.add(n); frontier.append(n)
    for rel, o in unknown_owner:
        say(f"note: {rel} belongs to {o}, which Unity has not compiled yet; skipped")
    if not targets:
        say(f"NOTHING TO COMPILE: no project assembly changed since Unity's last compile "
            f"({time.time() - started:.1f}s). Use --all to compile anyway.")
        return (0, ctx)

    # Topological order over the targets.
    order, done = [], set()
    def visit(n, stack=()):
        if n in done:
            return
        for d in by_ref[n]:
            if d in targets and d not in stack:
                visit(d, stack + (n,))
        done.add(n); order.append(n)
    for n in sorted(targets):
        visit(n)

    runs = os.path.join(project, "TempArtifacts", "Scratch", "OfflineCompile")
    if os.path.isdir(runs):  # failed runs keep their log for a while; drop the old ones
        for old in os.listdir(runs):
            if time.time() - os.path.getmtime(os.path.join(runs, old)) > 6 * 3600:
                shutil.rmtree(os.path.join(runs, old), ignore_errors=True)
    out_dir = os.path.join(runs, time.strftime("%Y%m%d-%H%M%S") + f"-{os.getpid()}")
    ctx["out_dir"] = out_dir
    os.makedirs(out_dir, exist_ok=True)
    built_ref = {}
    results = {}

    def compile_one(name):
        a = owned[name]
        rsp = os.path.join(out_dir, name + ".rsp")
        with open(rsp, "w", encoding="utf-8") as f:
            for x in a.args:
                f.write(x + "\n")
            f.write(f'-out:"{os.path.join(out_dir, name + ".dll")}"\n')
            f.write(f'-refout:"{os.path.join(out_dir, name + ".ref.dll")}"\n')
            for r in a.refs:
                dep = os.path.basename(r)[:-len(".ref.dll")] if r.endswith(".ref.dll") else None
                f.write(f'-r:"{built_ref.get(dep, r)}"\n')
            for s in a.sources:
                f.write(f'"{overlay_files.get(s, s)}"\n')
        t = time.time()
        p = subprocess.run([dotnet, "exec", csc, "/noconfig", f"@{rsp}"], cwd=project,
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
        return name, p.returncode, p.stdout + p.stderr, time.time() - t

    # Compile in dependency waves; assemblies in one wave run in parallel.
    remaining = list(order)
    log = []
    failed = set()
    while remaining:
        wave = [n for n in remaining if not (by_ref[n] & set(remaining) - {n})]
        remaining = [n for n in remaining if n not in wave]
        blocked = [n for n in wave if by_ref[n] & failed]
        wave = [n for n in wave if n not in blocked]
        for n in blocked:
            results[n] = ("skipped", f"skipped: it references {', '.join(sorted(by_ref[n] & failed))}, which failed", 0)
            failed.add(n)
        with ThreadPoolExecutor(max_workers=max(1, len(wave))) as ex:
            for name, rc, text, secs in ex.map(compile_one, wave):
                results[name] = (rc, text, secs)
                log.append(f"===== {name} (exit {rc}, {secs:.1f}s)\n{text}")
                if rc == 0:
                    built_ref[name] = os.path.join(out_dir, name + ".ref.dll")
                    ctx["built"][name] = os.path.join(out_dir, name + ".dll")
                else:
                    failed.add(name)

    with open(os.path.join(out_dir, "compile.log"), "w", encoding="utf-8") as f:
        f.write("\n".join(log))
    errors, warnings = [], 0
    for name in order:
        rc, text, secs = results[name]
        if rc == "skipped":
            continue
        for line in text.splitlines():
            m = ERR_RE.match(line.strip())
            if m and m.group("kind") == "error":
                errors.append(line.strip())
            elif m:
                warnings += 1
            elif rc not in (0, "skipped") and "error" in line.lower():
                errors.append(f"{name}: {line.strip()}")
    back = {os.path.normcase(os.path.normpath(full)): rel for rel, full in overlay_files.items()}
    def project_path(line):
        m = ERR_RE.match(line)
        if not m:
            return line
        f = os.path.normcase(os.path.normpath(m.group("file")))
        return (back.get(f, m.group("file")) + " [overlay]" * (f in back)) + line[len(m.group("file")):]
    errors = list(dict.fromkeys(project_path(e) for e in errors))
    summary = ", ".join(f"{n} {results[n][2]:.1f}s" if results[n][0] != "skipped" else f"{n} skipped" for n in order)
    if not keep and not failed:
        shutil.rmtree(out_dir, ignore_errors=True)
    elif not keep:  # keep the log and .rsp files, not the DLLs
        for fn in os.listdir(out_dir):
            if fn.endswith((".dll", ".pdb")):
                os.remove(os.path.join(out_dir, fn))
    elapsed = time.time() - started
    if failed:
        say(f"COMPILE FAILED ({len(errors)} errors, {elapsed:.1f}s; {summary}):")
        say("\n".join(errors[:max_errors]))
        if len(errors) > max_errors:
            say(f"... {len(errors) - max_errors} more")
        for n in order:
            if results[n][0] == "skipped":
                say(f"{n}: {results[n][1]}")
        say(f"full log: {os.path.join(out_dir, 'compile.log')}")
        return (5, ctx)
    say(f"COMPILED OK offline in {elapsed:.1f}s ({summary}; {warnings} warnings)"
        + (f"\noutput kept in {out_dir}" if keep and not opts.get("internal") else ""))
    return (0, ctx)
