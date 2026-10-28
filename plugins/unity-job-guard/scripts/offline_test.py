"""Run a Unity project's NUnit (Edit Mode) tests in plain .NET, without the Unity editor.

  ujob offline-test [--filter TEXT] [--timeout S] [--all] [--overlay DIR]

First compiles the changed project assemblies offline (see offline-compile; --all and --overlay are
passed through), then loads the test assemblies with the real Unity engine DLLs and runs every
[Test] / [TestCase] in a .NET process. Code that reaches the native engine (ECall/InternalCall:
ScriptableObject.CreateInstance, Debug.Log, AssetDatabase, GameObjects, LogAssert...) cannot run
outside Unity; those tests are reported as "needs-editor", not as failures, with the engine call
that stopped them. Pure logic (rules, economy, planners, parsers, maths) runs here, in parallel
with other sessions and with a busy editor.

  --filter TEXT   only fixtures whose full name contains TEXT (case-insensitive)
  --timeout S     per-test timeout, default 30

Exit codes: 0 no failures, 1 test failures (listed), 5 compile errors, 4 no compiler / no .NET SDK.
Results (one line per test): TempArtifacts/Logs/OfflineTests/<run>/results.tsv.
A test that passes here can still differ in Unity (Mono vs CoreCLR, no engine): it is a fast check,
not a replacement for the editor run before a feature is called done.
"""
import hashlib
import os
import shutil
import subprocess
import time

import offline_compile as oc

HERE = os.path.dirname(os.path.abspath(__file__))
RUNNER_SRC = os.path.join(os.path.dirname(HERE), "tools", "OfflineTestRunner")


def say(msg):
    print(msg, flush=True)


def runner_dll():
    """Build the runner once per source version into a per-user cache."""
    dotnet = shutil.which("dotnet")
    if not dotnet:
        return None, "no `dotnet` SDK on PATH (install the .NET SDK 8 or newer)"
    h = hashlib.sha1()
    for fn in sorted(os.listdir(RUNNER_SRC)):
        if fn.endswith((".cs", ".csproj")):
            with open(os.path.join(RUNNER_SRC, fn), "rb") as f:
                h.update(fn.encode() + f.read())
    base = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~/.cache"), "xin-unity-tools", "OfflineTestRunner")
    out = os.path.join(base, h.hexdigest()[:12])
    dll = os.path.join(out, "OfflineTestRunner.dll")
    if os.path.isfile(dll):
        return dll, None
    tmp = out + f".build{os.getpid()}"
    p = subprocess.run([dotnet, "build", RUNNER_SRC, "-c", "Release", "-nologo", "-v", "q", "-o", tmp,
                        f"-p:BaseIntermediateOutputPath={tmp}{os.sep}obj{os.sep}"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    if p.returncode != 0:
        return None, "runner build failed:\n" + (p.stdout + p.stderr)[-3000:]
    try:
        os.replace(tmp, out)
    except OSError:  # another session built it at the same time
        shutil.rmtree(tmp, ignore_errors=True)
    return dll, None


def has_tests(project, asm):
    """Any Assets/ source of the assembly that uses NUnit (skips third-party assemblies quickly)."""
    for s in asm.sources:
        if not s.startswith("Assets/"):
            continue
        try:
            with open(os.path.join(project, s), encoding="utf-8", errors="ignore") as f:
                if "NUnit.Framework" in f.read():
                    return True
        except OSError:
            pass
    return False


def main(project, args):
    opts, bad = oc.parse_args(args, extra=("--filter", "--timeout"))
    if bad:
        say(f"offline-test: unknown argument {bad}\n{__doc__}")
        return 64
    started = time.time()
    runner, err = runner_dll()
    if not runner:
        say(f"NO TEST RUNNER: {err}")
        return 4
    opts["keep"] = True
    rc, ctx = oc.build(project, opts)
    if rc != 0:  # compile errors were printed; their log stays in the compile folder
        return rc
    owned, built = ctx["owned"], ctx["built"]
    script_asm = os.path.join(project, "Library", "ScriptAssemblies")

    def impl(ref):
        if ref.endswith(".ref.dll") and "/Bee/" in ref.replace("\\", "/"):
            name = os.path.basename(ref)[:-len(".ref.dll")]
            return built.get(name) or os.path.join(script_asm, name + ".dll")
        return ref if os.path.isabs(ref) else os.path.join(project, ref)

    refs = {}
    for a in owned.values():
        for r in a.refs:
            path = impl(r)
            refs.setdefault(os.path.splitext(os.path.basename(path))[0].lower(), path)
    for name in owned:
        refs[name.lower()] = built.get(name) or os.path.join(script_asm, name + ".dll")
    test_asms = [n for n, a in owned.items()
                 if any(os.path.basename(r).lower() == "nunit.framework.dll" for r in a.refs) and has_tests(project, a)]

    runs = os.path.join(project, "TempArtifacts", "Logs", "OfflineTests")
    if os.path.isdir(runs):
        for old in os.listdir(runs):
            if time.time() - os.path.getmtime(os.path.join(runs, old)) > 6 * 3600:
                shutil.rmtree(os.path.join(runs, old), ignore_errors=True)
    log_dir = os.path.join(runs, time.strftime("%Y%m%d-%H%M%S") + f"-{os.getpid()}")
    os.makedirs(log_dir, exist_ok=True)
    refs_txt = os.path.join(log_dir, "refs.txt")
    with open(refs_txt, "w", encoding="utf-8") as f:
        f.write("\n".join(refs.values()))

    totals = {"PASS": 0, "FAIL": 0, "EDITOR": 0, "SKIP": 0}
    failures, crashed = [], []
    dotnet = shutil.which("dotnet")
    for name in sorted(test_asms):
        dll = refs[name.lower()]
        out = os.path.join(log_dir, f"{name}.tsv")
        p = subprocess.run([dotnet, runner, dll, refs_txt, out, opts.get("filter") or "", str(opts.get("timeout") or 30)],
                           cwd=project, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if not os.path.isfile(out):
            crashed.append(f"{name}: runner exited {p.returncode} without results\n{(p.stdout + p.stderr)[-2000:]}")
            continue
        with open(out, encoding="utf-8") as f:
            for line in f:
                parts = line.rstrip("\n").split("\t")
                if len(parts) < 2:
                    continue
                totals[parts[0]] = totals.get(parts[0], 0) + 1
                if parts[0] == "FAIL":
                    failures.append(f"{parts[1]}\n    {parts[2] if len(parts) > 2 else ''}")
        if p.returncode not in (0, 1):
            crashed.append(f"{name}: runner exited {p.returncode}\n{(p.stdout + p.stderr)[-2000:]}")
    with open(os.path.join(log_dir, "results.tsv"), "w", encoding="utf-8") as f:
        for name in sorted(test_asms):
            part = os.path.join(log_dir, f"{name}.tsv")
            if os.path.isfile(part):
                with open(part, encoding="utf-8") as src:
                    f.write(src.read())
    if ctx.get("out_dir"):
        shutil.rmtree(ctx["out_dir"], ignore_errors=True)
    elapsed = time.time() - started
    summary = (f"pass {totals['PASS']}, fail {totals['FAIL']}, needs-editor {totals['EDITOR']}, "
               f"skipped {totals['SKIP']} ({elapsed:.0f}s, {', '.join(sorted(test_asms))})")
    results = os.path.join(log_dir, "results.tsv")
    if failures or crashed:
        say(f"OFFLINE TESTS FAILED: {summary}")
        for x in failures[:opts["max_errors"]]:
            say("FAIL " + x)
        if len(failures) > opts["max_errors"]:
            say(f"... {len(failures) - opts['max_errors']} more")
        for x in crashed:
            say("RUNNER " + x)
        say(f"results: {results}")
        return 1
    say(f"OFFLINE TESTS OK: {summary}\nresults: {results}")
    return 0
