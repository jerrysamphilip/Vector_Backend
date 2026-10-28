"""Test tiers for scripts/test.py: which suites, and which of their cases, run in smoke / core / full.

The selection lives in system_tests/TIERS.json (docs/TESTING.md, section "Tiers"). This module only reads it:

* resolve a tier (following its `include` chain) into {suite id: entry}; an entry without "cases" runs the
  whole suite, an entry with "cases" runs only the case ids matching those glob patterns;
* match case ids against patterns (fnmatch syntax: * ? [abc] [!abc], case-sensitive);
* decide which suites may share the stack with others (parallel-safe, optional named locks) and group a
  plan into steps (one parallel pool, then the serial suites, then the run_last ones);
* estimate durations from earlier runs (seconds scaled by the share of cases selected) and simulate the
  parallel pool to estimate wall-clock time.

The suites themselves read the patterns from the TEST_CASES environment variable (system_tests/common.py
CaseFilter, system_tests/ui/ui_common.mjs caseFilter). Python 3 standard library only, like the runner.
"""
from __future__ import annotations

import fnmatch
import json
from pathlib import Path

CASES_ENV = "TEST_CASES"
# Rows a suite writes when it crashes or a setup step fails: never filtered out, so a broken run stays visible.
ALWAYS_KEEP = ("*-CRASH", "*-EXC", "*-SETUP", "*-SETUP-*")
TIER_NAMES = ("smoke", "core", "full")
ALIASES = {"all": "full"}
ENTRY_KEYS = {"cases", "args", "env", "parallel", "locks", "estimate_s", "reason"}


class TierError(ValueError):
    """TIERS.json is missing, malformed or names something that does not exist."""


# ── loading and validation ──

def load(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        raise TierError(f"cannot read {path}: {exc}") from exc
    except ValueError as exc:
        raise TierError(f"{path} is not valid JSON: {exc}") from exc
    problems = structure_problems(data)
    if problems:
        raise TierError(f"{path}: " + "; ".join(problems))
    return data


def structure_problems(data):
    if not isinstance(data, dict) or not isinstance(data.get("tiers"), dict):
        return ["top level must be an object with a 'tiers' object"]
    problems = []
    for name, tier in data["tiers"].items():
        problems += _tier_problems(data, name, tier)
    for key, prop in (data.get("suites") or {}).items():
        problems += _safety_problems(data, key, prop)
    problems += _include_cycles(data)
    return problems


def _tier_problems(data, name, tier):
    if not isinstance(tier, dict):
        return [f"tier {name}: must be an object"]
    out = [f"tier {name}: includes unknown tier {inc}" for inc in tier.get("include", []) if inc not in data["tiers"]]
    if not tier.get("all_suites") and not isinstance(tier.get("suites"), dict):
        out.append(f"tier {name}: needs 'suites' (or all_suites: true)")
    for key, entry in (tier.get("suites") or {}).items():
        out += [f"tier {name} / {key}: {p}" for p in entry_problems(entry)]
    return out


def entry_problems(entry):
    if not isinstance(entry, dict):
        return ["entry must be an object"]
    out = [f"unknown key '{k}'" for k in entry if k not in ENTRY_KEYS]
    if not str(entry.get("reason") or "").strip():
        out.append("every entry needs a one-line 'reason'")
    return out + _value_problems(entry)


def _value_problems(entry):
    out = []
    cases = entry.get("cases")
    if cases is not None and (not isinstance(cases, list) or not cases or not all(isinstance(c, str) and c for c in cases)):
        out.append("'cases' must be a non-empty list of case-id patterns (leave it out to run the whole suite)")
    if "args" in entry and not (isinstance(entry["args"], list) and all(isinstance(a, str) for a in entry["args"])):
        out.append("'args' must be a list of strings")
    if "env" in entry and not (isinstance(entry["env"], dict) and all(isinstance(v, str) for v in entry["env"].values())):
        out.append("'env' must map names to strings")
    if "parallel" in entry and not isinstance(entry["parallel"], bool):
        out.append("'parallel' must be true or false")
    return out


def _safety_problems(data, key, prop):
    if not isinstance(prop, dict) or not isinstance(prop.get("parallel"), bool):
        return [f"suites / {key}: needs parallel: true|false"]
    out = [] if str(prop.get("reason") or "").strip() else [f"suites / {key}: needs a 'reason'"]
    locks = data.get("locks") or {}
    out += [f"suites / {key}: unknown lock '{lk}' (define it under 'locks')" for lk in prop.get("locks", []) if lk not in locks]
    return out


def _include_cycles(data):
    def walk(name, seen):
        if name in seen:
            return [f"tier include cycle: {' -> '.join(seen + [name])}"]
        found = []
        for inc in data["tiers"].get(name, {}).get("include", []):
            if inc in data["tiers"]:
                found += walk(inc, seen + [name])
        return found
    return sorted({p for name in data["tiers"] for p in walk(name, [])})


# ── case-id patterns ──

def patterns_from(spec):
    """'A-*, B-01' -> ['A-*', 'B-01'] (the TEST_CASES format)."""
    return [p.strip() for p in str(spec or "").split(",") if p.strip()]


def matches(case_id, patterns):
    cid = str(case_id)
    return any(fnmatch.fnmatchcase(cid, p) for p in list(patterns) + list(ALWAYS_KEEP))


def _tokens(pattern):
    """fnmatch pattern -> tokens: ('*',), ('?',), ('set', chars, negated) or ('lit', char)."""
    out, i = [], 0
    while i < len(pattern):
        ch = pattern[i]
        end = pattern.find("]", i + 2) if ch == "[" else -1
        if ch in "*?":
            out.append((ch,))
        elif end > 0:
            body = pattern[i + 1:end]
            neg = body.startswith("!")
            out.append(("set", _expand_set(body[1:] if neg else body), neg))
            i = end
        else:
            out.append(("lit", ch))
        i += 1
    return out


def _expand_set(body):
    chars, i = set(), 0
    while i < len(body):
        if i + 2 < len(body) and body[i + 1] == "-":
            chars.update(chr(c) for c in range(ord(body[i]), ord(body[i + 2]) + 1))
            i += 3
        else:
            chars.add(body[i])
            i += 1
    return chars


def _step(token, ch):
    kind = token[0]
    if kind == "lit":
        return token[1] == ch
    if kind == "set":
        return (ch in token[1]) != token[2]
    return True  # '?'


def could_match_prefix(pattern, prefix):
    """True when some case id starting with `prefix` can match `pattern`. Suites use it to skip a whole
    sub-suite / area / phase / journey whose ids all share a prefix that no selected pattern can reach."""
    toks = _tokens(pattern)
    states = _closure({0}, toks)
    for ch in prefix:
        nxt = set()
        for s in states:
            if s < len(toks) and toks[s][0] == "*":
                nxt.add(s)
            elif s < len(toks) and _step(toks[s], ch):
                nxt.add(s + 1)
        states = _closure(nxt, toks)
        if not states:
            return False
    return True


def _closure(states, toks):
    out = set(states)
    for s in sorted(states):
        while s < len(toks) and toks[s][0] == "*":
            s += 1
            out.add(s)
    return out


def split_cases(case_ids, patterns):
    """(selected, excluded) lists, in the given order. patterns None = the whole suite."""
    if patterns is None:
        return list(case_ids), []
    sel = [c for c in case_ids if matches(c, patterns)]
    chosen = set(sel)
    return sel, [c for c in case_ids if c not in chosen]


def filter_cases(cases, patterns):
    """Recorded case dicts of a cut suite -> (kept, dropped): drops rows the pattern does not select and
    rows a suite carried over from an earlier run (carried_over: true)."""
    if patterns is None:
        return list(cases), []
    kept, dropped = [], []
    for case in cases:
        (kept if matches(case.get("id", ""), patterns) and not case.get("carried_over") else dropped).append(case)
    return kept, dropped


# ── tier resolution ──

def _key_targets(key, suites):
    """Suite ids a tier key names: 'layer:<name>', a glob over suite ids, or one exact id."""
    if key.startswith("layer:"):
        return [s["id"] for s in suites if s["layer"] == key[6:]]
    if any(ch in key for ch in "*?["):
        return [s["id"] for s in suites if fnmatch.fnmatchcase(s["id"], key)]
    return [key] if any(s["id"] == key for s in suites) else []


def _specificity(key):
    return 0 if key.startswith("layer:") else (1 if any(ch in key for ch in "*?[") else 2)


def merge_entry(prev, own, tier):
    """A tier's own entry wins for args / env / parallel / locks / estimate_s / reason; its cases are the
    union with the included tier's, and a whole suite on either side means the whole suite."""
    entry = {k: v for k, v in own.items() if k != "cases"}
    entry["tier"] = tier
    prev_cases = None if prev is None else prev.get("cases")
    own_cases = own.get("cases")
    if prev is None:
        cases = own_cases
    elif prev_cases is None or own_cases is None:
        cases = None
    else:
        cases = list(dict.fromkeys(prev_cases + own_cases))
    if cases is not None:
        entry["cases"] = cases
    return entry


def resolve(data, name, suites):
    """{suite id: entry} for tier `name` (aliases: all -> full). Raises TierError for unknown names."""
    name = ALIASES.get(name, name)
    tier = data["tiers"].get(name)
    if tier is None:
        raise TierError(f"unknown tier '{name}' (known: {', '.join(data['tiers'])})")
    out = {}
    for inc in tier.get("include", []):
        out.update(resolve(data, inc, suites))
    if tier.get("all_suites"):
        for s in suites:
            out[s["id"]] = merge_entry(out.get(s["id"]), {"reason": tier.get("reason", "every suite")}, name)
    unknown = []
    for key in sorted(tier.get("suites") or {}, key=_specificity):
        targets = _key_targets(key, suites)
        if not targets and _specificity(key) == 2:
            unknown.append(key)
        for sid in targets:
            out[sid] = merge_entry(out.get(sid), tier["suites"][key], name)
    if unknown:
        raise TierError(f"tier {name} names unknown suite(s): {', '.join(unknown)}")
    return out


# ── parallel safety and plan steps ──

def safety(data, suite_id, entry=None):
    """{"parallel", "locks", "reason"} for a suite: the tier entry's override, else its TIERS.json
    'suites' property (exact id before glob keys), else serial."""
    props = data.get("suites") or {}
    prop = props.get(suite_id)
    if prop is None:
        prop = next((v for k, v in props.items() if any(ch in k for ch in "*?[") and fnmatch.fnmatchcase(suite_id, k)), None)
    base = dict(prop) if prop else {"parallel": False, "reason": "not classified in TIERS.json 'suites': serial until reviewed"}
    if entry and "parallel" in entry:
        base = {"parallel": entry["parallel"], "locks": entry.get("locks", []),
                "reason": f"tier override: {entry.get('reason', '')}"}
    return {"parallel": bool(base["parallel"]), "locks": list(base.get("locks") or []), "reason": base.get("reason", "")}


def plan_steps(suites, safe_of, jobs):
    """Execution steps. jobs <= 1: every suite alone, in the given order. Otherwise one parallel pool with
    every parallel-safe suite (in order), then the serial suites one at a time, then the run_last ones."""
    if jobs <= 1:
        return [{"parallel": False, "suites": [s]} for s in suites]
    pool = [s for s in suites if safe_of(s)["parallel"] and not s.get("run_last")]
    rest = [s for s in suites if s not in pool]
    rest.sort(key=lambda s: 1 if s.get("run_last") else 0)
    steps = [{"parallel": True, "suites": pool}] if pool else []
    return steps + [{"parallel": False, "suites": [s]} for s in rest]


def can_start(locks, running_locks):
    return not (set(locks) & set(running_locks))


def simulate(items, jobs):
    """Wall-clock seconds of a pool: items [(seconds, locks)] started in order (first fit) on `jobs`
    slots, never two at once that share a lock."""
    pending = list(items)
    running = []  # (end, locks)
    now = 0.0
    while pending or running:
        started = True
        while started:
            started = False
            held = [lk for _, locks in running for lk in locks]
            for i, (secs, locks) in enumerate(pending):
                if len(running) < max(1, jobs) and can_start(locks, held):
                    running.append((now + float(secs or 0), locks))
                    pending.pop(i)
                    started = True
                    break
        if running:
            running.sort(key=lambda r: r[0])
            now = running.pop(0)[0]
    return now


# ── estimates ──

def estimate(entry, timing, catalog_ids, fallback_s):
    """(seconds, source) for one suite in a tier. timing: {"seconds", "cases": [ids], "filtered"} from an
    earlier run, or None. A cut suite scales the per-case time by the cases it selects."""
    if entry.get("estimate_s"):
        return float(entry["estimate_s"]), "tiers file"
    if not timing or not timing.get("seconds"):
        base, n_base, src = float(fallback_s or 0), max(1, len(catalog_ids)), "registry"
    else:
        base, n_base, src = float(timing["seconds"]), len(timing.get("cases") or []), "run " + str(timing.get("run", ""))
        if not n_base:
            n_base = max(1, len(catalog_ids))
    if entry.get("cases") is None:
        if timing and timing.get("filtered") and catalog_ids:
            return base * len(catalog_ids) / n_base, src + " (scaled)"
        return base, src
    selected, _ = split_cases(catalog_ids, entry["cases"])
    if not catalog_ids:
        return base, src + " (no case list: whole-suite time)"
    return base * len(selected) / n_base, src + f" x {len(selected)}/{n_base} cases"
