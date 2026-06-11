#!/usr/bin/env python3
"""Cyclomatic complexity audit for a Python repo.

Runs three views in one pass:
  1. ruff C901 violations (functions above a complexity threshold)
  2. radon repo-wide average + grade distribution
  3. Reference-weighted hotspot ranking (CC * static reference count)

Outputs human-readable tables (default) or JSON (--format json) suitable
for CI artifacts and trend tracking.

Requires `ruff` and `radon` on PATH. Under `uv`, invoke as:
    uv run --with radon python audit.py --scope src
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

# Names so common they collide with builtins, dunder methods, or recur across
# many classes — their reference counts are unreliable and skew weighting.
DEFAULT_NOISE_NAMES = {
    # builtins / common method names
    "list", "dict", "set", "get", "str", "int", "float", "bool", "type", "len",
    "open", "print", "next", "iter", "range", "sum", "min", "max", "any", "all",
    "map", "filter", "sorted", "reversed", "enumerate", "zip", "tuple",
    "format", "repr", "hash", "id", "input", "object", "property",
    "staticmethod", "classmethod", "super",
    "isinstance", "issubclass", "callable", "hasattr", "getattr", "setattr",
    "delattr", "vars", "dir",
    # mutating method names
    "copy", "add", "update", "keys", "values", "items", "pop", "append",
    "extend", "remove", "clear", "count", "index", "join", "split", "strip",
    "replace", "find", "close", "read", "write", "send", "recv",
    # ubiquitous lifecycle names
    "start", "stop", "run", "main", "setup", "teardown", "execute", "cancel",
    "complete", "reset", "reload", "emit", "step",
}

# Filesystem patterns to skip when scanning for references.
DEFAULT_EXCLUDE_PATTERNS = re.compile(
    r"(^|/)(tests?|\.venv|venv|node_modules|\.mypy_cache|\.ruff_cache|"
    r"__pycache__|build|dist|\.git|\.tox|\.pytest_cache)(/|$)"
)

WORD_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\b")


def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, check=False)


def _require_tool(name: str) -> None:
    if shutil.which(name) is None:
        sys.stderr.write(
            f"error: `{name}` not found on PATH. Install via `pip install {name}` "
            f"or run this script under `uv run --with {name}`.\n"
        )
        sys.exit(2)


def phase1_ruff_violations(scope: list[str], exclude: list[str], threshold: int) -> dict:
    """Run ruff C901 and parse violations."""
    _require_tool("ruff")
    cmd = ["ruff", "check", "--select", "C901", "--no-cache"]
    for ex in exclude:
        cmd += ["--exclude", ex]
    if threshold != 10:
        cmd += ["--config", f"lint.mccabe.max-complexity = {threshold}"]
    cmd += scope
    proc = _run(cmd)
    text = proc.stdout

    # Each violation: "C901 `name` is too complex (N > T)" then "   --> path:line:col"
    pattern = re.compile(
        r"C901 `([^`]+)` is too complex \((\d+) > \d+\).*?--> ([^:\n]+):(\d+):",
        re.DOTALL,
    )
    rows = [
        {"name": m.group(1), "cc": int(m.group(2)), "file": m.group(3), "line": int(m.group(4))}
        for m in pattern.finditer(text)
    ]
    rows.sort(key=lambda r: -r["cc"])
    return {
        "threshold": threshold,
        "count": len(rows),
        "violations": rows,
    }


def phase2_radon_distribution(scope: list[str], exclude: list[str]) -> dict:
    """Run radon and compute average + grade distribution from JSON."""
    _require_tool("radon")
    cmd = ["radon", "cc"] + scope
    for ex in exclude:
        cmd += ["--exclude", f"**/{ex}/**"]
    cmd += ["-j"]
    proc = _run(cmd)
    out = proc.stdout
    start = out.find("{")
    if start < 0:
        return {"error": "radon produced no JSON output", "stderr": proc.stderr}
    try:
        data = json.loads(out[start:])
    except json.JSONDecodeError as e:
        return {"error": f"failed to parse radon output: {e}"}

    blocks = []
    seen: set[tuple[str, int, str]] = set()

    def emit(name: str, cc: int, file: str, line: int) -> None:
        key = (file, line, name)
        if key in seen:
            return
        seen.add(key)
        blocks.append({"name": name, "cc": cc, "file": file, "line": line})

    def walk(items: list, path: str) -> None:
        for b in items:
            if b["type"] == "class":
                walk(b.get("methods", []), path)
            else:
                emit(b["name"], b["complexity"], path, b["lineno"])
                for c in b.get("closures", []):
                    emit(c["name"], c["complexity"], path, c["lineno"])

    for path, items in data.items():
        walk(items, path)

    n = len(blocks)
    avg = sum(b["cc"] for b in blocks) / n if n else 0.0

    def grade(cc: int) -> str:
        if cc <= 5:
            return "A"
        if cc <= 10:
            return "B"
        if cc <= 20:
            return "C"
        if cc <= 30:
            return "D"
        if cc <= 40:
            return "E"
        return "F"

    dist: dict[str, int] = defaultdict(int)
    for b in blocks:
        dist[grade(b["cc"])] += 1

    return {
        "total_blocks": n,
        "plain_average_cc": round(avg, 3),
        "grade_distribution": {g: dist.get(g, 0) for g in "ABCDEF"},
        "_blocks": blocks,  # internal, used by phase 3
    }


def phase3_weighted_hotspots(
    blocks: list[dict],
    scope: list[str],
    exclude_pattern: re.Pattern[str],
    noise_names: set[str],
    include_tests: bool,
) -> dict:
    """Count word-boundary references per function name; rank by CC * refs."""
    if not blocks:
        return {"weighted_average_cc": 0.0, "hotspots": [], "buckets": []}

    ref_counts: dict[str, int] = defaultdict(int)
    files_scanned = 0
    for root in scope:
        for p in Path(root).rglob("*.py"):
            path_str = str(p)
            if not include_tests and exclude_pattern.search(path_str):
                continue
            try:
                text = p.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            files_scanned += 1
            for m in WORD_RE.finditer(text):
                ref_counts[m.group(1)] += 1

    name_def_count: dict[str, int] = defaultdict(int)
    for b in blocks:
        name_def_count[b["name"]] += 1

    for b in blocks:
        raw = ref_counts.get(b["name"], 0)
        b["refs"] = max(raw - name_def_count[b["name"]], 0)
        b["noisy_name"] = b["name"] in noise_names

    # Weighted average over all blocks (floor weight at 1 so cold paths still count once).
    weights = [max(b["refs"], 1) for b in blocks]
    w_all = sum(weights)
    avg_all = sum(b["cc"] * w for b, w in zip(blocks, weights)) / w_all

    clean = [b for b in blocks if not b["noisy_name"]]
    if clean:
        cw = [max(b["refs"], 1) for b in clean]
        avg_clean = sum(b["cc"] * w for b, w in zip(clean, cw)) / sum(cw)
    else:
        avg_clean = avg_all

    # Hotspots: rank by CC * refs, excluding noisy names.
    hotspots = sorted(clean, key=lambda b: -(b["cc"] * b["refs"]))[:25]
    hotspots = [
        {
            "name": b["name"],
            "cc": b["cc"],
            "refs": b["refs"],
            "score": b["cc"] * b["refs"],
            "file": b["file"],
            "line": b["line"],
        }
        for b in hotspots
    ]

    # CC distribution by reference bucket.
    buckets = [(0, 0), (1, 1), (2, 5), (6, 20), (21, 100), (101, 1000), (1001, 10**9)]
    bucket_stats = []
    for lo, hi in buckets:
        matching = [b for b in clean if lo <= b["refs"] <= hi]
        if not matching:
            continue
        label = str(lo) if lo == hi else (f"{lo}-{hi}" if hi < 10**9 else f"{lo}+")
        bucket_stats.append({
            "refs_range": label,
            "function_count": len(matching),
            "avg_cc": round(sum(b["cc"] for b in matching) / len(matching), 3),
        })

    return {
        "files_scanned": files_scanned,
        "weighted_average_cc_all": round(avg_all, 3),
        "weighted_average_cc_clean": round(avg_clean, 3),
        "noise_names_filtered": sorted(noise_names),
        "hotspots": hotspots,
        "buckets": bucket_stats,
    }


def render_text(report: dict) -> str:
    out: list[str] = []
    out.append("=" * 78)
    out.append("Complexity Audit")
    out.append("=" * 78)

    p1 = report["violations"]
    out.append(f"\n[1] Ruff C901 violations (threshold > {p1['threshold']})")
    out.append(f"    Count: {p1['count']}")
    if p1["violations"]:
        out.append(f"    Top {min(10, len(p1['violations']))}:")
        out.append(f"      {'CC':>5}  {'name':<45}  file:line")
        out.append(f"      {'-'*5}  {'-'*45}  {'-'*40}")
        for v in p1["violations"][:10]:
            out.append(f"      {v['cc']:>5}  {v['name'][:45]:<45}  {v['file']}:{v['line']}")

    p2 = report["distribution"]
    if "error" in p2:
        out.append(f"\n[2] Radon distribution: ERROR — {p2['error']}")
    else:
        out.append(f"\n[2] Repo-wide distribution ({p2['total_blocks']} functions/methods)")
        out.append(f"    Plain average CC: {p2['plain_average_cc']}")
        out.append("    Grade distribution:")
        total = p2["total_blocks"] or 1
        for g, c in p2["grade_distribution"].items():
            pct = 100.0 * c / total
            bar = "#" * int(pct / 2)
            out.append(f"      {g}: {c:>5} ({pct:5.1f}%) {bar}")

    p3 = report["hotspots"]
    out.append(f"\n[3] Reference-weighted hotspots ({p3['files_scanned']} files scanned)")
    out.append(f"    Weighted avg CC (all):                 {p3['weighted_average_cc_all']}")
    out.append(f"    Weighted avg CC (noise names removed): {p3['weighted_average_cc_clean']}")
    if p3["hotspots"]:
        out.append(f"\n    Top {len(p3['hotspots'])} hotspots (CC × refs):")
        out.append(f"      {'CC':>4}  {'refs':>5}  {'score':>7}  {'name':<40}  file:line")
        out.append(f"      {'-'*4}  {'-'*5}  {'-'*7}  {'-'*40}  {'-'*30}")
        for h in p3["hotspots"]:
            out.append(
                f"      {h['cc']:>4}  {h['refs']:>5}  {h['score']:>7}  "
                f"{h['name'][:40]:<40}  {h['file']}:{h['line']}"
            )
    if p3["buckets"]:
        out.append("\n    Average CC by reference bucket:")
        for b in p3["buckets"]:
            out.append(
                f"      refs {b['refs_range']:>10}: avg CC = {b['avg_cc']:>5.2f}  "
                f"(n={b['function_count']})"
            )

    out.append("")
    out.append("Caveats: static reference count ≠ runtime call frequency; method-name")
    out.append("collisions inflate counts; CC is a proxy for branchiness, not readability.")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--scope", action="append", default=None,
        help="Directory to analyze (repeatable). Defaults to top-level package dirs.",
    )
    parser.add_argument(
        "--exclude", action="append", default=None,
        help="Directory name to exclude (repeatable). Default: tests, .venv, build, dist, node_modules.",
    )
    parser.add_argument("--threshold", type=int, default=10, help="ruff C901 threshold (default 10).")
    parser.add_argument(
        "--noise-names", default=None,
        help="Comma-separated extra names to exclude from weighting (additive to defaults).",
    )
    parser.add_argument("--include-tests", action="store_true", help="Include test directories in reference scan.")
    parser.add_argument("--format", choices=["text", "json"], default="text")
    parser.add_argument(
        "--fail-on-max-cc", type=int, default=None,
        help="Exit nonzero if any function exceeds this CC (CI gate).",
    )
    parser.add_argument(
        "--fail-on-weighted-avg", type=float, default=None,
        help="Exit nonzero if weighted-average CC exceeds this value.",
    )
    args = parser.parse_args(argv)

    scope = args.scope or _autodetect_scope()
    exclude = args.exclude or ["tests", ".venv", "venv", "build", "dist", "node_modules"]
    noise_names = set(DEFAULT_NOISE_NAMES)
    if args.noise_names:
        noise_names.update(n.strip() for n in args.noise_names.split(",") if n.strip())

    violations = phase1_ruff_violations(scope, exclude, args.threshold)
    distribution = phase2_radon_distribution(scope, exclude)
    blocks = distribution.pop("_blocks", []) if "_blocks" in distribution else []
    hotspots = phase3_weighted_hotspots(
        blocks, scope, DEFAULT_EXCLUDE_PATTERNS, noise_names, args.include_tests
    )

    report = {
        "scope": scope,
        "exclude": exclude,
        "violations": violations,
        "distribution": distribution,
        "hotspots": hotspots,
    }

    if args.format == "json":
        print(json.dumps(report, indent=2))
    else:
        print(render_text(report))

    # CI gates
    exit_code = 0
    if args.fail_on_max_cc is not None:
        worst = max((v["cc"] for v in violations["violations"]), default=0)
        if worst > args.fail_on_max_cc:
            sys.stderr.write(
                f"\nFAIL: max function CC {worst} exceeds gate {args.fail_on_max_cc}\n"
            )
            exit_code = 1
    if args.fail_on_weighted_avg is not None:
        wavg = hotspots.get("weighted_average_cc_clean", 0.0)
        if wavg > args.fail_on_weighted_avg:
            sys.stderr.write(
                f"\nFAIL: weighted-avg CC {wavg} exceeds gate {args.fail_on_weighted_avg}\n"
            )
            exit_code = 1

    return exit_code


def _autodetect_scope() -> list[str]:
    """Pick top-level dirs that look like source. Conservative fallback to cwd."""
    candidates: list[str] = []
    cwd = Path.cwd()
    # Common conventions
    for name in ("src", "lib"):
        if (cwd / name).is_dir():
            candidates.append(name)
    # Package dirs containing __init__.py at top level
    for child in cwd.iterdir():
        if not child.is_dir() or child.name.startswith("."):
            continue
        if child.name in {"tests", "test", "docs", "examples", "scripts", "build", "dist"}:
            continue
        if (child / "__init__.py").is_file():
            candidates.append(child.name)
    # Also include conventional extras if present
    for extra in ("examples", "scripts"):
        if (cwd / extra).is_dir():
            candidates.append(extra)
    if not candidates:
        candidates = ["."]
    return candidates


if __name__ == "__main__":
    sys.exit(main())
