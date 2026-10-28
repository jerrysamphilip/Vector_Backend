"""Pure helpers for run_load.py (no I/O, no network): percentiles, per-endpoint summaries, the weighted
request mix and pass/fail threshold evaluation. Unit-tested in tests/unit/test_v2_loadlib.py."""
from __future__ import annotations

import bisect
import math
import random
import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

ALL = "ALL"


def percentile(values: Sequence[float], pct: float) -> Optional[float]:
    """The pct-th percentile (0-100) with linear interpolation between closest ranks (numpy's default).
    None for no values."""
    if not values:
        return None
    if not 0 <= pct <= 100:
        raise ValueError("pct must be between 0 and 100")
    data = sorted(values)
    if len(data) == 1:
        return float(data[0])
    rank = (len(data) - 1) * pct / 100.0
    lo, hi = math.floor(rank), math.ceil(rank)
    if lo == hi:
        return float(data[lo])
    return data[lo] + (data[hi] - data[lo]) * (rank - lo)


@dataclass
class Sample:
    endpoint: str
    status: int          # HTTP status, 0 for a transport error / timeout
    latency_ms: float
    ok: bool             # status was one this scenario expects
    base_url: str = ""


@dataclass
class EndpointStats:
    endpoint: str
    count: int = 0
    errors: int = 0
    statuses: Dict[str, int] = field(default_factory=dict)
    latencies: List[float] = field(default_factory=list)

    def add(self, s: Sample) -> None:
        self.count += 1
        self.errors += 0 if s.ok else 1
        key = str(s.status)
        self.statuses[key] = self.statuses.get(key, 0) + 1
        self.latencies.append(s.latency_ms)

    def summary(self, duration_s: float) -> Dict[str, object]:
        r = lambda v: None if v is None else round(v, 1)  # noqa: E731
        return {
            "endpoint": self.endpoint, "count": self.count, "errors": self.errors,
            "error_rate": round(self.errors / self.count, 4) if self.count else 0.0,
            "p50": r(percentile(self.latencies, 50)), "p95": r(percentile(self.latencies, 95)),
            "p99": r(percentile(self.latencies, 99)), "max": r(max(self.latencies) if self.latencies else None),
            "rps": round(self.count / duration_s, 2) if duration_s > 0 else 0.0,
            "statuses": dict(sorted(self.statuses.items())),
        }


def summarize(samples: Iterable[Sample], duration_s: float, key: str = "endpoint") -> Dict[str, Dict[str, object]]:
    """Per-endpoint (or per-base_url with key='base_url') summaries plus an ALL row."""
    groups: Dict[str, EndpointStats] = {}
    total = EndpointStats(ALL)
    for s in samples:
        name = getattr(s, key) or "-"
        groups.setdefault(name, EndpointStats(name)).add(s)
        total.add(s)
    out = {name: st.summary(duration_s) for name, st in sorted(groups.items())}
    out[ALL] = total.summary(duration_s)
    return out


# ── Weighted mix ────────────────────────────────────────────────────────────

class WeightedMix:
    """Pick scenario names in proportion to their weights (zero-weight names never come up)."""

    def __init__(self, weights: Mapping[str, float]):
        items = [(k, float(w)) for k, w in weights.items() if float(w) > 0]
        if not items:
            raise ValueError("the mix needs at least one positive weight")
        if any(w < 0 for w in weights.values()):
            raise ValueError("weights must not be negative")
        self.names = [k for k, _ in items]
        self.cumulative: List[float] = []
        running = 0.0
        for _, w in items:
            running += w
            self.cumulative.append(running)
        self.total = running

    def pick(self, rng: random.Random) -> str:
        x = rng.random() * self.total
        return self.names[min(bisect.bisect_right(self.cumulative, x), len(self.names) - 1)]

    def share(self, name: str) -> float:
        if name not in self.names:
            return 0.0
        i = self.names.index(name)
        prev = self.cumulative[i - 1] if i else 0.0
        return (self.cumulative[i] - prev) / self.total


def parse_mix(text: str, base: Optional[Mapping[str, float]] = None, known: Optional[Iterable[str]] = None) -> Dict[str, float]:
    """'contacts_list=30,export_small=0' -> weights, starting from base (overrides only what it names)."""
    out = dict(base or {})
    allowed = set(known) if known is not None else None
    for part in filter(None, (p.strip() for p in (text or "").split(","))):
        name, sep, value = part.partition("=")
        if not sep:
            raise ValueError(f"mix entry {part!r} must look like name=weight")
        name = name.strip()
        if allowed is not None and name not in allowed:
            raise ValueError(f"unknown scenario {name!r}; known: {sorted(allowed)}")
        out[name] = float(value)
    return out


# ── Thresholds ──────────────────────────────────────────────────────────────

METRICS_MAX = ("p50", "p95", "p99", "max", "error_rate")   # must stay at or below the target
METRICS_MIN = ("rps",)                                     # must reach at least the target
_TARGET_RE = re.compile(r"^\s*([A-Za-z0-9_]+)\.([a-z0-9_]+)\s*=\s*([0-9.]+)\s*$")


@dataclass(frozen=True)
class Target:
    endpoint: str
    metric: str
    value: float
    source: str = ""   # requirement it comes from

    @property
    def op(self) -> str:
        return ">=" if self.metric in METRICS_MIN else "<="


def parse_target(text: str, source: str = "CLI") -> Target:
    m = _TARGET_RE.match(text)
    if not m:
        raise ValueError(f"target {text!r} must look like endpoint.metric=value, e.g. contacts_list.p95=1000")
    endpoint, metric, value = m.group(1), m.group(2), float(m.group(3))
    if metric not in METRICS_MAX + METRICS_MIN:
        raise ValueError(f"unknown metric {metric!r}; use one of {METRICS_MAX + METRICS_MIN}")
    return Target(endpoint, metric, value, source)


def merge_targets(defaults: Sequence[Target], overrides: Sequence[Target]) -> List[Target]:
    """Overrides replace the default with the same endpoint.metric; new ones are added."""
    merged: Dict[Tuple[str, str], Target] = {(t.endpoint, t.metric): t for t in defaults}
    for t in overrides:
        merged[(t.endpoint, t.metric)] = t
    return list(merged.values())


def evaluate(summary: Mapping[str, Mapping[str, object]], targets: Sequence[Target],
             min_samples: int = 1) -> List[Dict[str, object]]:
    """One verdict per target: PASS / FAIL, or SKIP when the endpoint has fewer than min_samples requests."""
    verdicts = []
    for t in targets:
        row = summary.get(t.endpoint)
        actual = None if row is None else row.get(t.metric)
        count = 0 if row is None else int(row.get("count") or 0)
        if row is None or actual is None or count < min_samples:
            status = "SKIP"
        elif t.op == ">=":
            status = "PASS" if float(actual) >= t.value else "FAIL"
        else:
            status = "PASS" if float(actual) <= t.value else "FAIL"
        verdicts.append({"target": f"{t.endpoint}.{t.metric} {t.op} {t.value:g}", "actual": actual,
                         "count": count, "status": status, "source": t.source})
    return verdicts


def breached(verdicts: Sequence[Mapping[str, object]]) -> bool:
    return any(v["status"] == "FAIL" for v in verdicts)


# ── Markdown ────────────────────────────────────────────────────────────────

def _cell(v: object) -> str:
    return "-" if v is None else str(v)


def markdown_report(meta: Mapping[str, object], summary: Mapping[str, Mapping[str, object]],
                    by_replica: Mapping[str, Mapping[str, object]], verdicts: Sequence[Mapping[str, object]]) -> str:
    lines = ["# Load test result", ""]
    lines += [f"- **{k}**: {_cell(v)}" for k, v in meta.items()]
    lines += ["", "## Per endpoint (latency in ms)", "",
              "| endpoint | count | errors | error rate | p50 | p95 | p99 | max | req/s | statuses |",
              "|---|---:|---:|---:|---:|---:|---:|---:|---:|---|"]
    for name, row in summary.items():
        statuses = " ".join(f"{k}:{v}" for k, v in dict(row.get("statuses") or {}).items())
        lines.append("| " + " | ".join(_cell(x) for x in (
            f"**{name}**" if name == ALL else name, row["count"], row["errors"], row["error_rate"], row["p50"],
            row["p95"], row["p99"], row["max"], row["rps"], statuses)) + " |")
    if len(by_replica) > 2:  # more than one replica plus ALL
        lines += ["", "## Per API replica", "", "| base URL | count | errors | p95 | req/s | statuses |",
                  "|---|---:|---:|---:|---:|---|"]
        for name, row in by_replica.items():
            statuses = " ".join(f"{k}:{v}" for k, v in dict(row.get("statuses") or {}).items())
            lines.append(f"| {name} | {row['count']} | {row['errors']} | {_cell(row['p95'])} | {row['rps']} | {statuses} |")
    lines += ["", "## Thresholds", "", "| target | actual | requests | result | source |", "|---|---:|---:|---|---|"]
    for v in verdicts:
        lines.append(f"| {v['target']} | {_cell(v['actual'])} | {v['count']} | {v['status']} | {v['source']} |")
    lines += ["", f"**Overall: {'FAIL' if breached(verdicts) else 'PASS'}**", ""]
    return "\n".join(lines)
