"""AST mutation testing for the pipeline (a development tool, not part of the pipeline).

    python tools/mutate.py . 1 [import.py extract.py ...]     # project folder, workers, files

Each mutant changes one operator, constant, condition or statement in a scratch copy of the project and
is judged by the test suite: a fast tier first, the slower integration tests for survivors. Timeouts count
as kills. Results are appended to .mutation-results.jsonl (override with MUTATION_RESULTS), so an
interrupted run resumes; BUDGET=<seconds> stops after that long, ONLY_SURVIVORS=1 re-judges survivors.
The tiers below name tests of this project's suite. Surviving mutants need a human look: some are
equivalent to the original (e.g. a default of 0 versus 1 that is only compared with <).
"""
import ast
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

PROJECT = Path(sys.argv[1]).resolve()
WORKERS = int(sys.argv[2])
FILES = sys.argv[3:] or ["import.py", "extract.py", "align.py", "export.py", "main.py"]
LIGHT_EXTRACT = ["tests.test_extract.CrashDetection", "tests.test_extract.NumberTests", "tests.test_extract.TechnicalTests",
                 "tests.test_extract.ExtractMetadataTests", "tests.test_extract.InspectTests"]
HEAVY_FUNCS = {"_run", "_reap", "_Reader", "_probe", "_crashed", "inspect_ffprobe", "_component_depths"}
FAST = {  # tests that exercise a file without needing the ffmpeg corpus
    "import.py": ["tests.test_import"],
    "align.py": ["tests.test_align", "tests.test_export"],
    "export.py": ["tests.test_export"],
    "extract.py": [*LIGHT_EXTRACT, "tests.test_extract.RunTests", "tests.test_extract.FakeProbeTests"],
    "main.py": ["tests.test_pipeline.ExitStatus"],
}
SLOW = {
    "import.py": ["tests.test_pipeline", "tests.test_extract.OracleTests"],
    "extract.py": ["tests.test_extract.OracleTests", "tests.test_pipeline"],
    "align.py": ["tests.test_pipeline"],
    "export.py": ["tests.test_pipeline"],
    "main.py": ["tests.test_pipeline"],
}
CMP = {ast.Eq: ast.NotEq, ast.NotEq: ast.Eq, ast.Lt: ast.LtE, ast.LtE: ast.Lt, ast.Gt: ast.GtE, ast.GtE: ast.Gt,
       ast.In: ast.NotIn, ast.NotIn: ast.In, ast.Is: ast.IsNot, ast.IsNot: ast.Is}


def is_docstring(node, parents):
    return isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)


def candidates(tree):
    """Yield (description, lineno, apply) where apply(tree_copy_nodes_by_index) mutates the copy."""
    nodes = list(ast.walk(tree))
    for idx, node in enumerate(nodes):
        line = getattr(node, "lineno", 0)
        if isinstance(node, ast.Compare):
            for i, op in enumerate(node.ops):
                if type(op) in CMP:
                    yield f"cmp {type(op).__name__}->{CMP[type(op)].__name__}", line, (idx, "cmp", i)
        elif isinstance(node, ast.BoolOp):
            yield f"bool {type(node.op).__name__}->{'Or' if isinstance(node.op, ast.And) else 'And'}", line, (idx, "bool", 0)
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            yield "drop not", line, (idx, "not", 0)
        elif isinstance(node, (ast.If, ast.While, ast.IfExp)):
            yield "negate condition", line, (idx, "negate", 0)
        elif isinstance(node, ast.Constant):
            if isinstance(node.value, bool):
                yield f"const {node.value}->{not node.value}", line, (idx, "bool_const", 0)
            elif isinstance(node.value, int) and not isinstance(node.value, bool):
                yield f"int {node.value}->{node.value + 1}", line, (idx, "int", 0)
        elif isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub, ast.Mult)):
            yield f"arith {type(node.op).__name__}", line, (idx, "arith", 0)
        elif isinstance(node, ast.AugAssign) and isinstance(node.op, (ast.Add, ast.Sub)):
            yield "augassign flip", line, (idx, "aug", 0)
        elif isinstance(node, (ast.Break, ast.Continue)):
            yield f"drop {type(node).__name__}", line, (idx, "pass", 0)
        elif isinstance(node, ast.Return) and node.value is not None and not (isinstance(node.value, ast.Constant) and node.value.value is None):
            yield "return None", line, (idx, "retnone", 0)
        elif isinstance(node, ast.Expr) and not is_docstring(node, {}):
            yield "drop statement", line, (idx, "pass", 0)


def apply(tree, spec):
    idx, kind, i = spec
    nodes = list(ast.walk(tree))
    node = nodes[idx]
    if kind == "cmp":
        node.ops[i] = CMP[type(node.ops[i])]()
    elif kind == "bool":
        node.op = ast.Or() if isinstance(node.op, ast.And) else ast.And()
    elif kind == "not":
        # replace the UnaryOp by its operand: mutate parent via marker
        node.op = ast.UAdd()   # +x : truthiness of x preserved for ints/bools; for others raises TypeError => kill; use sentinel below
        node.__class__ = ast.UnaryOp
    elif kind == "negate":
        node.test = ast.UnaryOp(op=ast.Not(), operand=node.test)
    elif kind == "bool_const":
        node.value = not node.value
    elif kind == "int":
        node.value += 1
    elif kind == "arith":
        node.op = ast.Sub() if isinstance(node.op, ast.Add) else ast.Add()
    elif kind == "aug":
        node.op = ast.Sub() if isinstance(node.op, ast.Add) else ast.Add()
    elif kind == "retnone":
        node.value = ast.Constant(value=None)
    elif kind == "pass":
        # turn the statement into `pass` by swapping class in place
        node.__class__ = ast.Pass
        for field in list(node._fields):
            if hasattr(node, field):
                delattr(node, field)
    ast.fix_missing_locations(tree)


def _limits():
    import resource
    resource.setrlimit(resource.RLIMIT_AS, (1536 * 1024 * 1024, 1536 * 1024 * 1024))


def run_tests(workdir, targets, timeout):
    cmd = [sys.executable, "-W", "error", "-m", "unittest", "-f", *targets]
    try:
        r = subprocess.run(cmd, cwd=workdir, capture_output=True, text=True, timeout=timeout, preexec_fn=_limits)
        return r.returncode == 0
    except subprocess.TimeoutExpired:
        return False  # hangs are kills


def heavy_lines(fname):
    tree = ast.parse((PROJECT / fname).read_text())
    spans = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in HEAVY_FUNCS:
            spans.append((node.lineno, node.end_lineno))
    return spans


def fast_tier(fname, line):
    if fname == "extract.py" and not any(a <= line <= b for a, b in heavy_lines(fname)):
        return LIGHT_EXTRACT
    return FAST[fname]


def judge(job):
    fname, desc, line, spec = job
    work = Path(tempfile.mkdtemp(prefix="mut-"))
    try:
        shutil.copytree(PROJECT, work / "p", ignore=shutil.ignore_patterns("__pycache__", ".git", "*.pyc", ".mutation-results.jsonl"))
        w = work / "p"
        tree = ast.parse((PROJECT / fname).read_text())
        apply(tree, spec)
        try:
            (w / fname).write_text(ast.unparse(tree))
        except Exception:  # unparse failures: skip
            return dict(file=fname, line=line, desc=desc, status="skipped")
        compile((w / fname).read_text(), fname, "exec")
        if not run_tests(w, fast_tier(fname, line), 60):
            return dict(file=fname, line=line, desc=desc, status="killed-fast")
        if not run_tests(w, SLOW[fname], 240):
            return dict(file=fname, line=line, desc=desc, status="killed-full")
        return dict(file=fname, line=line, desc=desc, status="SURVIVED")
    except SyntaxError:
        return dict(file=fname, line=line, desc=desc, status="skipped")
    finally:
        shutil.rmtree(work, ignore_errors=True)



def key(job):
    name, desc, line, spec = job
    return f"{name}|{line}|{desc}|{spec[0]}|{spec[1]}|{spec[2]}"


if __name__ == "__main__":
    budget = float(os.environ.get("BUDGET", "250"))
    only = os.environ.get("ONLY_SURVIVORS") == "1"
    out = Path(os.environ.get("MUTATION_RESULTS", PROJECT / ".mutation-results.jsonl"))

    def latest():
        """Verdicts so far: the file is append-only and a later line replaces an earlier one for the same mutant."""
        found = {}
        if out.exists():
            for entry in out.read_text().splitlines():
                row = json.loads(entry)
                found[row["key"]] = row
        return found

    done = latest()
    jobs = []
    for f in FILES:
        tree = ast.parse((PROJECT / f).read_text())
        for desc, line, spec in candidates(tree):
            jobs.append((f, desc, line, spec))
    if only:
        todo = [j for j in jobs if done.get(key(j), {}).get("status") == "SURVIVED"]
    else:
        todo = [j for j in jobs if key(j) not in done]
    print(f"{len(jobs)} mutants, {len(todo)} to run, budget {budget:.0f}s", flush=True)
    t0 = time.time()
    with open(out, "a") as sink:
        for job in todo:
            if time.time() - t0 > budget:
                break
            res = judge(job)
            res["key"] = key(job)
            sink.write(json.dumps(res) + "\n")
            sink.flush()
            if res["status"] == "SURVIVED":
                print(f"  survived {res['file']}:{res['line']} {res['desc']}", flush=True)
    from collections import Counter
    mine = [r for r in latest().values() if r["file"] in FILES]
    c = Counter(r["status"] for r in mine)
    scored = len(mine) - c["skipped"]
    judged = {r["key"] for r in mine}
    remaining = len([j for j in jobs if key(j) not in judged])
    killed = c["killed-fast"] + c["killed-full"]
    print(dict(c), f"| judged {len(mine)} | remaining {remaining} | score so far {100 * killed / max(scored, 1):.1f}%", flush=True)
