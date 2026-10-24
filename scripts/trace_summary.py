"""Summarise logs/traces.jsonl: request outcomes, latency, and per-dependency health.

    python scripts/trace_summary.py [path] [--last N]
"""

import json
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

args = [a for a in sys.argv[1:] if not a.startswith("--")]
path = Path(args[0]) if args else Path(__file__).resolve().parents[1] / "logs" / "traces.jsonl"
last = int(sys.argv[sys.argv.index("--last") + 1]) if "--last" in sys.argv else None

traces = [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.exists() else []
if last:
    traces = traces[-last:]
if not traces:
    sys.exit(f"No traces in {path}")


def pct(values, q):
    values = sorted(values)
    return values[min(len(values) - 1, int(round(q * (len(values) - 1))))]


durations = [t["duration_ms"] / 1000 for t in traces]
print(f"{len(traces)} requests  ({traces[0]['started_at']} → {traces[-1]['started_at']})")
print(f"  outcome: {dict(Counter(t['status'] for t in traces))}")
print(f"  latency: median {statistics.median(durations):.1f}s · p90 {pct(durations, 0.9):.1f}s · max {max(durations):.1f}s")
verifiers = Counter("agent" if "Verifier agent" in (t.get("verifier") or "") else "local fallback" for t in traces if t.get("verifier"))
print(f"  verification by: {dict(verifiers)} · passed: {sum(bool(t.get('verification_passed')) for t in traces)}/{len(traces)}")

by_name: dict[str, list] = defaultdict(list)
for t in traces:
    for s in t["spans"]:
        name = f"{s['name']}[{s['source']}]" if s.get("source") else s["name"]
        by_name[name].append(s)

print(f"\n  {'step':<42}{'calls':>6}{'ok':>6}{'fail':>6}{'median':>9}{'max':>9}")
for name in sorted(by_name):
    spans = by_name[name]
    ms = [s["duration_ms"] / 1000 for s in spans]
    ok = sum(s["status"] in ("ok", "not_found") for s in spans)
    print(f"  {name:<42}{len(spans):>6}{ok:>6}{len(spans) - ok:>6}{statistics.median(ms):>8.1f}s{max(ms):>8.1f}s")

errors = Counter(s.get("error") or s.get("detail") for t in traces for s in t["spans"]
                 if s["status"] not in ("ok", "not_found"))
if errors:
    print("\n  most common failures:")
    for err, n in errors.most_common(5):
        print(f"    {n:>3} × {err}")
