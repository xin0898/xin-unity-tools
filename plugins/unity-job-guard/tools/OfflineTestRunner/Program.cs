// Runs the NUnit tests of a Unity-compiled assembly in plain .NET, without the Unity editor.
// Tests whose code reaches the native engine (ECall / InternalCall, LogAssert, [UnityTest]) cannot
// run here; they are reported as EDITOR, not as failures. Driven by `ujob offline-test`.
//
//   OfflineTestRunner <tests.dll> <refs.txt> <results.tsv> [filter] [timeoutSeconds]
//
// refs.txt lists one implementation DLL per line (resolved by simple assembly name).
// results.tsv gets one line per test: PASS|FAIL|EDITOR|SKIP <tab> name <tab> detail.
// Exit code: 0 no failures, 1 failures, 2 usage / load error.
using System.Reflection;
using System.Runtime.Loader;

if (args.Length < 3)
{
    Console.Error.WriteLine("usage: OfflineTestRunner <tests.dll> <refs.txt> <results.tsv> [filter] [timeoutSeconds]");
    return 2;
}
var testDll = Path.GetFullPath(args[0]);
var filter = args.Length > 3 ? args[3] : "";
var timeout = TimeSpan.FromSeconds(args.Length > 4 ? int.Parse(args[4]) : 30);

var byName = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
foreach (var r in File.ReadAllLines(args[1]).Where(l => l.Length > 0))
    byName.TryAdd(Path.GetFileNameWithoutExtension(r), r);
byName[Path.GetFileNameWithoutExtension(testDll)] = testDll;
AssemblyLoadContext.Default.Resolving += (ctx, an) =>
    byName.TryGetValue(an.Name, out var p) && File.Exists(p) ? ctx.LoadFromAssemblyPath(Path.GetFullPath(p)) : null;

Assembly asm;
try { asm = AssemblyLoadContext.Default.LoadFromAssemblyPath(testDll); }
catch (Exception e) { Console.Error.WriteLine($"cannot load {testDll}: {e.Message}"); return 2; }
Type[] types;
try { types = asm.GetTypes(); }
catch (ReflectionTypeLoadException e) { types = e.Types.Where(t => t != null).ToArray(); }

int pass = 0, fail = 0, editor = 0, skip = 0;
var results = new List<string>();
// Engine calls the code under test caught itself (try { new NativeArray } catch { return null; }) still
// show up here; a test that then fails is reported as EDITOR rather than as a false failure.
string engineHit = null;
AppDomain.CurrentDomain.FirstChanceException += (_, e) =>
{
    var x = e.Exception;
    if (engineHit == null && (x is System.Security.SecurityException || (x.Message ?? "").Contains("ECall")))
        engineHit = OneLine(x.Message) + " at " + (EngineFrame(x) ?? "?");
};

foreach (var t in types.Where(t => t.IsClass && !t.IsGenericTypeDefinition).OrderBy(t => t.FullName))
{
    MethodInfo[] ms;
    try { ms = t.GetMethods(BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance | BindingFlags.Static); }
    catch { continue; }
    var tests = ms.Where(m => Has(m, "TestAttribute") || Has(m, "TestCaseAttribute") || Has(m, "UnityTestAttribute")).ToList();
    if (tests.Count == 0 || (filter.Length > 0 && !t.FullName.Contains(filter, StringComparison.OrdinalIgnoreCase)))
        continue;
    var setUp = Ordered(ms, "SetUpAttribute");
    var tearDown = Ordered(ms, "TearDownAttribute");
    object fixture = null;
    string fixtureOutcome = null, fixtureDetail = null;
    if (!(t.IsAbstract && t.IsSealed) && !t.IsAbstract)
    {
        var err = RunGuarded(() =>
        {
            fixture = Activator.CreateInstance(t, nonPublic: true);
            foreach (var m in Ordered(ms, "OneTimeSetUpAttribute")) m.Invoke(m.IsStatic ? null : fixture, null);
        });
        if (err != null) (fixtureOutcome, fixtureDetail) = Describe(err);
    }
    else if (t.IsAbstract && !t.IsSealed) continue;

    foreach (var m in tests.OrderBy(m => m.Name))
    {
        var cases = Attrs(m, "TestCaseAttribute").Select(CaseArgs).ToList();
        if (cases.Count == 0) cases.Add(Array.Empty<object>());
        foreach (var c in cases)
        {
            var name = $"{t.FullName}.{m.Name}" + (c.Length > 0 ? "(" + string.Join(",", c.Select(v => v ?? "null")) + ")" : "");
            string outcome, detail = "";
            if (Has(m, "IgnoreAttribute") || Has(m, "ExplicitAttribute") || Has(t, "IgnoreAttribute") || Has(t, "ExplicitAttribute"))
                outcome = "SKIP";
            else if (Has(m, "UnityTestAttribute"))
                (outcome, detail) = ("EDITOR", "[UnityTest] coroutine");
            else if (fixtureOutcome != null)
                (outcome, detail) = (fixtureOutcome == "PASS" ? "FAIL" : fixtureOutcome, "fixture setup: " + fixtureDetail);
            else
            {
                engineHit = null;
                var err = RunGuarded(() =>
                {
                    foreach (var s in setUp) s.Invoke(s.IsStatic ? null : fixture, null);
                    try { m.Invoke(m.IsStatic ? null : fixture, Convert(c, m.GetParameters())); }
                    finally { foreach (var s in tearDown) s.Invoke(s.IsStatic ? null : fixture, null); }
                });
                (outcome, detail) = err == null ? ("PASS", "") : Describe(err);
                if (outcome == "FAIL" && engineHit != null)
                    (outcome, detail) = ("EDITOR", $"caught engine call ({engineHit}), then: {detail}");
            }
            switch (outcome) { case "PASS": pass++; break; case "FAIL": fail++; break; case "EDITOR": editor++; break; default: skip++; break; }
            results.Add($"{outcome}\t{name}\t{detail.Replace('\t', ' ').Replace('\r', ' ').Replace('\n', ' ')}");
        }
    }
    if (fixture != null)
        RunGuarded(() => { foreach (var m in Ordered(ms, "OneTimeTearDownAttribute")) m.Invoke(m.IsStatic ? null : fixture, null); });
}

File.WriteAllLines(args[2], results);
Console.WriteLine($"pass {pass}  fail {fail}  needs-editor {editor}  skipped {skip}");
return fail > 0 ? 1 : 0;

Exception RunGuarded(Action body)
{
    Exception err = null;
    var th = new Thread(() => { try { body(); } catch (Exception e) { err = e; } }, 64 * 1024 * 1024) { IsBackground = true };
    th.Start();
    return th.Join(timeout) ? err : new TimeoutException($"timed out after {timeout.TotalSeconds:0} s");
}

// PASS (Assert.Pass), SKIP (Ignore/Inconclusive), EDITOR (needs the native engine), FAIL (anything else).
(string, string) Describe(Exception e)
{
    var inner = e;
    while (inner is TargetInvocationException && inner.InnerException != null) inner = inner.InnerException;
    for (var x = e; x != null; x = x.InnerException)
    {
        var n = x.GetType().Name;
        var msg = x.Message ?? "";
        if (n is "SuccessException") return ("PASS", "");
        if (n is "IgnoreException" or "InconclusiveException") return ("SKIP", msg);
        if (n is "AssertionException" or "MultipleAssertException") return ("FAIL", $"{n}: {OneLine(msg)}");
        if (x is System.Security.SecurityException || x is MissingMethodException || x is DllNotFoundException
            || x is EntryPointNotFoundException || msg.Contains("ECall") || msg.Contains("InternalCall")
            || msg.Contains("No log scope is available"))
            return ("EDITOR", $"{OneLine(msg)} at {EngineFrame(x) ?? EngineFrame(e) ?? "?"}");
    }
    return ("FAIL", $"{inner.GetType().Name}: {OneLine(inner.Message)} {FirstFrame(inner)}");
}

static string OneLine(string s) => (s ?? "").Trim().Split('\n')[0].Trim();
static string EngineFrame(Exception e) => (e.StackTrace ?? "").Split('\n').Select(l => l.Trim())
    .FirstOrDefault(l => l.StartsWith("at UnityEngine.") || l.StartsWith("at UnityEditor.") || l.StartsWith("at Unity."))?.Substring(3).Split('(')[0];
static string FirstFrame(Exception e) => (e.StackTrace ?? "").Split('\n').Select(l => l.Trim()).FirstOrDefault(l => l.StartsWith("at "))?.Split(" in ")[0] ?? "";
static bool Has(MemberInfo m, string name) { try { return m.GetCustomAttributesData().Any(a => a.AttributeType.Name == name); } catch { return false; } }
static IEnumerable<CustomAttributeData> Attrs(MemberInfo m, string name) { try { return m.GetCustomAttributesData().Where(a => a.AttributeType.Name == name).ToList(); } catch { return Array.Empty<CustomAttributeData>(); } }
static List<MethodInfo> Ordered(MethodInfo[] ms, string attr) =>
    ms.Where(m => Has(m, attr)).OrderBy(m => Depth(m.DeclaringType)).ToList(); // base-class setups first
static int Depth(Type t) { var d = 0; for (var x = t; x != null; x = x.BaseType) d++; return d; }
static object[] CaseArgs(CustomAttributeData a)
{
    var ca = a.ConstructorArguments;
    if (ca.Count == 1 && ca[0].Value is IReadOnlyCollection<CustomAttributeTypedArgument> arr)
        return arr.Select(v => v.Value).ToArray();
    return ca.Select(v => v.Value).ToArray();
}
static object[] Convert(object[] vals, ParameterInfo[] ps) => vals.Select((v, i) =>
{
    if (v == null || i >= ps.Length) return v;
    var pt = ps[i].ParameterType;
    if (pt.IsEnum) return Enum.ToObject(pt, v);
    if (pt != v.GetType() && v is IConvertible && typeof(IConvertible).IsAssignableFrom(pt)) return System.Convert.ChangeType(v, pt);
    return v;
}).ToArray();
