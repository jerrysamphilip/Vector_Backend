# app/services/scoring_engine.py
"""
Contact scoring on fit and engagement (BRD v2.0 BR-CM-43, CM-083): the pure part.

No database, no network, no AI: plain dicts in, plain dicts out, so every rule can be
unit tested. app/services/contact_scoring.py loads the data and stores the results.

A workspace's scoring model is one JSON document:

    {
      "fit_rules": [{"id", "label", "field", "operator", "value", "points"}, ...],
      "engagement": {"email_opened": {"points": 3, "max": 15}, ...},
      "half_life_days": 30,      # recency decay: an event this old counts half (0 = no decay)
      "lookback_days": 180,      # older events are ignored
      "fit_weight": 50,          # share of the total score; the two weights add up to 100
      "engagement_weight": 50,
      "grade_thresholds": {"A": 75, "B": 50, "C": 25},   # below C is D
    }

Fit = sum of the points of every matching rule, clamped to 0-100.
Engagement = per signal min(cap, points x decayed count), summed and clamped to 0-100.
Score = fit x fit_weight/100 + engagement x engagement_weight/100, rounded, clamped to 0-100.
"""
import copy
import math
import re
import uuid
from datetime import date, datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple

from app.core.clock import utcnow

# ── Vocabulary ────────────────────────────────────────────────────

# Fit fields: key -> (label, kind). "number" fields compare numerically.
FIT_FIELDS: Dict[str, Tuple[str, str]] = {
    "designation": ("Job title / seniority", "text"),
    "industry": ("Industry", "text"),
    "employees": ("Company size (employees)", "number"),
    "emp_band": ("Company size band", "text"),
    "annual_revenue": ("Annual revenue", "number"),
    "country": ("Country", "text"),
    "lifecycle_stage": ("Lifecycle stage", "text"),
    "lead_status": ("Lead status", "text"),
    "lead_source": ("Lead source", "text"),
    "email_type": ("Email type (BUSINESS / PERSONAL)", "text"),
    "tags": ("Tags", "list"),
    "has_phone": ("Has a phone number (true / false)", "text"),
}

OPERATORS: Dict[str, str] = {
    "equals": "is",
    "not_equals": "is not",
    "in": "is any of",
    "not_in": "is none of",
    "contains_any": "contains any word of",
    "is_set": "is known",
    "is_not_set": "is unknown",
    "gte": "is at least",
    "lte": "is at most",
    "between": "is between",
}
TEXT_OPERATORS = ("equals", "not_equals", "in", "not_in", "contains_any", "is_set", "is_not_set")
NUMBER_OPERATORS = ("equals", "not_equals", "gte", "lte", "between", "is_set", "is_not_set")
NO_VALUE_OPERATORS = ("is_set", "is_not_set")

ENGAGEMENT_SIGNALS: Dict[str, str] = {
    "email_opened": "Email opened",
    "email_clicked": "Link clicked",
    "email_replied": "Replied",
    "positive_reply": "Positive reply",
    "meeting": "Meeting logged",
    "call": "Call logged",
    "task_completed": "Task completed",
}

GRADES = ("A", "B", "C", "D")

MAX_RULES = 50
MAX_LABEL = 100
MAX_VALUE = 500
POINTS_RANGE = (-100, 100)

DEFAULT_MODEL: Dict[str, Any] = {
    "fit_rules": [
        {"id": "exec", "label": "Executive or founder", "field": "designation", "operator": "contains_any",
         "value": "chief, ceo, cto, cfo, coo, cmo, cio, founder, co-founder, owner, president, vp, vice president",
         "points": 30},
        {"id": "mgmt", "label": "Director or manager", "field": "designation", "operator": "contains_any",
         "value": "director, head, manager", "points": 15},
        {"id": "size_large", "label": "200+ employees", "field": "employees", "operator": "gte",
         "value": "200", "points": 15},
        {"id": "size_mid", "label": "50-199 employees", "field": "employees", "operator": "between",
         "value": "50-199", "points": 8},
        {"id": "revenue", "label": "Revenue of 10M or more", "field": "annual_revenue", "operator": "gte",
         "value": "10000000", "points": 10},
        {"id": "industry", "label": "Target industry", "field": "industry", "operator": "in",
         "value": "Software, Computer Software, SaaS, Information Technology, Technology, Internet, Financial Services",
         "points": 15},
        {"id": "country", "label": "Target country", "field": "country", "operator": "in",
         "value": "United States, USA, US, United Kingdom, UK, Canada, Australia, Germany, India",
         "points": 5},
        {"id": "business_email", "label": "Business email", "field": "email_type", "operator": "equals",
         "value": "BUSINESS", "points": 10},
        {"id": "personal_email", "label": "Personal email", "field": "email_type", "operator": "equals",
         "value": "PERSONAL", "points": -10},
        {"id": "qualified_stage", "label": "Qualified lifecycle stage", "field": "lifecycle_stage", "operator": "in",
         "value": "MQL, SQL, OPPORTUNITY", "points": 10},
        {"id": "unqualified", "label": "Unqualified lead", "field": "lead_status", "operator": "equals",
         "value": "UNQUALIFIED", "points": -20},
    ],
    "engagement": {
        "email_opened": {"points": 3, "max": 15},
        "email_clicked": {"points": 6, "max": 20},
        "email_replied": {"points": 15, "max": 30},
        "positive_reply": {"points": 20, "max": 20},
        "meeting": {"points": 15, "max": 30},
        "call": {"points": 8, "max": 16},
        "task_completed": {"points": 3, "max": 9},
    },
    "half_life_days": 30,
    "lookback_days": 180,
    "fit_weight": 50,
    "engagement_weight": 50,
    "grade_thresholds": {"A": 75, "B": 50, "C": 25},
}


class ScoringModelError(ValueError):
    """The submitted scoring model is invalid; the message says why."""


def default_model() -> Dict[str, Any]:
    return copy.deepcopy(DEFAULT_MODEL)


# ── Helpers ───────────────────────────────────────────────────────

def _round(value: float) -> int:
    """Half up (Python's round() rounds half to even)."""
    return int(math.floor(value + 0.5))


def clamp(value: float, low: float = 0, high: float = 100) -> float:
    return max(low, min(high, value))


def split_values(value: Any) -> List[str]:
    """'a, b ,c' or ['a', 'b'] -> ['a', 'b', 'c'] (trimmed, blanks dropped)."""
    if value is None:
        return []
    items = value if isinstance(value, (list, tuple)) else str(value).split(",")
    return [str(v).strip() for v in items if str(v).strip()]


_NUM_RE = re.compile(r"^-?\d+(?:\.\d+)?$")
_RANGE_RE = re.compile(r"^(\d+(?:\.\d+)?)(?:-|to|\.\.)(\d+(?:\.\d+)?)$")


def parse_number(value: Any) -> Optional[float]:
    """'10,000,000' / '10000000' / 1e7 -> 10000000.0; anything else -> None."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if math.isfinite(value) else None
    text = str(value).replace(",", "").replace("_", "").replace(" ", "")
    return float(text) if _NUM_RE.match(text) else None


def parse_range(value: Any) -> Optional[Tuple[float, float]]:
    """'50-199', '50 to 199', '1,000..5,000' -> (low, high) with low <= high."""
    text = str(value or "").replace(",", "").replace("_", "").replace(" ", "").lower()
    m = _RANGE_RE.match(text)
    if not m:
        return None
    low, high = float(m.group(1)), float(m.group(2))
    return (low, high) if low <= high else (high, low)


_FIRST_NUMBER = re.compile(r"\d[\d,]*")


def employees_from_band(band: Optional[str]) -> Optional[int]:
    """Lower bound of an employee band: '51-200' -> 51, '10000+' -> 10000, '1,001-5,000' -> 1001."""
    if not band:
        return None
    m = _FIRST_NUMBER.search(str(band))
    if not m:
        return None
    try:
        return int(m.group(0).replace(",", ""))
    except ValueError:
        return None


def _num_text(number: float) -> str:
    """10000000.0 -> '10000000', 2.5 -> '2.5' (no scientific notation)."""
    return str(int(number)) if number == int(number) else str(number)


def _norm(value: Any) -> str:
    return str(value).strip().lower()


def _contains_word(text: str, keyword: str) -> bool:
    """Whole-word match, so 'cto' does not match 'director'."""
    kw = keyword.strip().lower()
    if not kw:
        return False
    return re.search(r"(?<![a-z0-9])" + re.escape(kw) + r"(?![a-z0-9])", text) is not None


def _is_blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip()) or (isinstance(value, (list, tuple)) and not value)


# ── Validation ────────────────────────────────────────────────────

def _int_in(value: Any, low: int, high: int, what: str) -> int:
    if isinstance(value, bool):
        raise ScoringModelError(f"{what} must be a whole number between {low} and {high}")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ScoringModelError(f"{what} must be a whole number between {low} and {high}")
    if not math.isfinite(number) or number != int(number) or not low <= number <= high:
        raise ScoringModelError(f"{what} must be a whole number between {low} and {high}")
    return int(number)


def validate_rule(raw: Any, index: int = 0) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        raise ScoringModelError(f"Fit rule {index + 1} must be an object")
    where = f"Fit rule {index + 1}"
    field = str(raw.get("field") or "").strip()
    if field not in FIT_FIELDS:
        raise ScoringModelError(f"{where}: field must be one of {', '.join(FIT_FIELDS)}")
    operator = str(raw.get("operator") or "").strip()
    kind = FIT_FIELDS[field][1]
    allowed = NUMBER_OPERATORS if kind == "number" else TEXT_OPERATORS
    if operator not in allowed:
        raise ScoringModelError(f"{where}: operator for {field} must be one of {', '.join(allowed)}")
    value = raw.get("value")
    if isinstance(value, (list, tuple)):
        value = ", ".join(split_values(value))
    value = "" if value is None else str(value).strip()
    if len(value) > MAX_VALUE:
        raise ScoringModelError(f"{where}: value is longer than {MAX_VALUE} characters")
    if operator in NO_VALUE_OPERATORS:
        value = ""
    elif operator == "between":
        rng = parse_range(value)
        if rng is None:
            raise ScoringModelError(f"{where}: 'between' needs a range like 50-199")
        value = f"{_num_text(rng[0])}-{_num_text(rng[1])}"
    elif kind == "number":
        number = parse_number(value)
        if number is None:
            raise ScoringModelError(f"{where}: value must be a number")
        value = _num_text(number)
    elif not split_values(value):
        raise ScoringModelError(f"{where}: value is required")
    points = _int_in(raw.get("points"), POINTS_RANGE[0], POINTS_RANGE[1], f"{where}: points")
    label = str(raw.get("label") or "").strip()[:MAX_LABEL]
    if not label:
        label = f"{FIT_FIELDS[field][0]} {OPERATORS[operator]} {value}".strip()[:MAX_LABEL]
    rule_id = str(raw.get("id") or "").strip()[:36]
    if not re.match(r"^[A-Za-z0-9_-]{1,36}$", rule_id or ""):
        rule_id = uuid.uuid4().hex[:12]
    return {"id": rule_id, "label": label, "field": field, "operator": operator, "value": value, "points": points}


def validate_model(raw: Any) -> Dict[str, Any]:
    """Return a clean, complete model; missing parts fall back to the defaults."""
    if not isinstance(raw, dict):
        raise ScoringModelError("The scoring model must be an object")
    base = default_model()

    rules_raw = raw.get("fit_rules", base["fit_rules"])
    if not isinstance(rules_raw, list):
        raise ScoringModelError("fit_rules must be a list")
    if len(rules_raw) > MAX_RULES:
        raise ScoringModelError(f"At most {MAX_RULES} fit rules")
    rules, seen = [], set()
    for i, r in enumerate(rules_raw):
        rule = validate_rule(r, i)
        if rule["id"] in seen:
            rule["id"] = uuid.uuid4().hex[:12]
        seen.add(rule["id"])
        rules.append(rule)

    eng_raw = raw.get("engagement", {})
    if not isinstance(eng_raw, dict):
        raise ScoringModelError("engagement must be an object")
    unknown = set(eng_raw) - set(ENGAGEMENT_SIGNALS)
    if unknown:
        raise ScoringModelError(f"Unknown engagement signal(s): {', '.join(sorted(unknown))}")
    engagement = {}
    for key in ENGAGEMENT_SIGNALS:
        cfg = eng_raw.get(key, base["engagement"][key])
        if not isinstance(cfg, dict):
            raise ScoringModelError(f"engagement.{key} must be an object with points and max")
        engagement[key] = {
            "points": _int_in(cfg.get("points", base["engagement"][key]["points"]), 0, 100, f"{ENGAGEMENT_SIGNALS[key]} points"),
            "max": _int_in(cfg.get("max", base["engagement"][key]["max"]), 0, 100, f"{ENGAGEMENT_SIGNALS[key]} cap"),
        }

    half_life = _int_in(raw.get("half_life_days", base["half_life_days"]), 0, 365, "Half-life (days)")
    lookback = _int_in(raw.get("lookback_days", base["lookback_days"]), 1, 730, "Look-back window (days)")
    fit_weight = _int_in(raw.get("fit_weight", base["fit_weight"]), 0, 100, "Fit weight")
    eng_weight = _int_in(raw.get("engagement_weight", base["engagement_weight"]), 0, 100, "Engagement weight")
    if fit_weight + eng_weight != 100:
        raise ScoringModelError("Fit weight and engagement weight must add up to 100")

    thr_raw = raw.get("grade_thresholds", base["grade_thresholds"])
    if not isinstance(thr_raw, dict):
        raise ScoringModelError("grade_thresholds must be an object with A, B and C")
    thresholds = {g: _int_in(thr_raw.get(g, base["grade_thresholds"][g]), 1, 100, f"Grade {g} threshold")
                  for g in ("A", "B", "C")}
    if not thresholds["A"] > thresholds["B"] > thresholds["C"]:
        raise ScoringModelError("Grade thresholds must decrease: A > B > C")

    return {
        "fit_rules": rules, "engagement": engagement, "half_life_days": half_life, "lookback_days": lookback,
        "fit_weight": fit_weight, "engagement_weight": eng_weight, "grade_thresholds": thresholds,
    }


def load_model(stored: Any) -> Dict[str, Any]:
    """A stored model, made valid (a broken stored document falls back to the defaults)."""
    try:
        return validate_model(stored if isinstance(stored, dict) else {})
    except ScoringModelError:
        return default_model()


# ── Fit ───────────────────────────────────────────────────────────

def _field_value(contact: Dict[str, Any], field: str) -> Any:
    if field == "employees":
        value = contact.get("employees")
        return value if value is not None else employees_from_band(contact.get("emp_band"))
    if field == "has_phone":
        if "has_phone" in contact:
            return "true" if contact["has_phone"] else "false"
        return "true" if (contact.get("phone") or contact.get("mobile_phone")) else "false"
    return contact.get(field)


def rule_matches(rule: Dict[str, Any], contact: Dict[str, Any]) -> bool:
    field, op, value = rule["field"], rule["operator"], rule.get("value")
    actual = _field_value(contact, field)
    if op == "is_set":
        return not _is_blank(actual)
    if op == "is_not_set":
        return _is_blank(actual)
    if _is_blank(actual):
        return op in ("not_equals", "not_in")

    if FIT_FIELDS[field][1] == "number":
        number = parse_number(actual)
        if number is None:
            return False
        if op == "between":
            rng = parse_range(value)
            return rng is not None and rng[0] <= number <= rng[1]
        target = parse_number(value)
        if target is None:
            return False
        return {"equals": number == target, "not_equals": number != target,
                "gte": number >= target, "lte": number <= target}.get(op, False)

    actual_items = [_norm(a) for a in actual] if isinstance(actual, (list, tuple)) else [_norm(actual)]
    targets = [_norm(v) for v in split_values(value)]
    if op == "equals":
        return bool(targets) and targets[0] in actual_items
    if op == "not_equals":
        return not targets or targets[0] not in actual_items
    if op == "in":
        return any(a in targets for a in actual_items)
    if op == "not_in":
        return not any(a in targets for a in actual_items)
    if op == "contains_any":
        return any(_contains_word(a, t) for a in actual_items for t in targets)
    return False


def fit_breakdown(model: Dict[str, Any], contact: Dict[str, Any]) -> Tuple[int, List[Dict[str, Any]]]:
    rows, total = [], 0
    for rule in model["fit_rules"]:
        matched = rule_matches(rule, contact)
        if matched:
            total += rule["points"]
        rows.append({**rule, "matched": matched, "contributed": rule["points"] if matched else 0})
    return _round(clamp(total)), rows


# ── Engagement ────────────────────────────────────────────────────

def _as_datetime(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value.replace(tzinfo=None) if value.tzinfo else value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    return None


def _events(items: Iterable[Any]) -> Iterable[Tuple[datetime, float]]:
    """Accepts datetimes, (datetime, count) pairs or {"at": datetime, "count": n} dicts."""
    for item in items or ():
        if isinstance(item, dict):
            at, count = item.get("at"), item.get("count", 1)
        elif isinstance(item, (list, tuple)):
            at, count = (item[0], item[1] if len(item) > 1 else 1)
        else:
            at, count = item, 1
        at = _as_datetime(at)
        try:
            count = float(count)
        except (TypeError, ValueError):
            continue
        if at is not None and count > 0:
            yield at, count


def decay_weight(age_days: float, half_life_days: int) -> float:
    if half_life_days <= 0:
        return 1.0
    return 0.5 ** (max(age_days, 0.0) / half_life_days)


def engagement_breakdown(model: Dict[str, Any], activity: Dict[str, Any], now: datetime) -> Tuple[int, List[Dict[str, Any]]]:
    now = _as_datetime(now)  # type: ignore[assignment]  # a datetime in gives a datetime out
    half_life, lookback = model["half_life_days"], model["lookback_days"]
    rows, total = [], 0.0
    for key, label in ENGAGEMENT_SIGNALS.items():
        cfg = model["engagement"][key]
        count, weighted = 0, 0.0
        for at, n in _events((activity or {}).get(key, ())):
            age = (now - at).total_seconds() / 86400.0
            if age > lookback:
                continue
            count += int(n)
            weighted += n * decay_weight(age, half_life)
        contributed = min(float(cfg["max"]), cfg["points"] * weighted)
        total += contributed
        rows.append({"signal": key, "label": label, "count": count, "weighted_count": round(weighted, 2),
                     "points": cfg["points"], "max": cfg["max"], "contributed": round(contributed, 1)})
    return _round(clamp(total)), rows


# ── Total ─────────────────────────────────────────────────────────

def grade_for(score: Optional[int], thresholds: Dict[str, int]) -> Optional[str]:
    if score is None:
        return None
    for g in ("A", "B", "C"):
        if score >= thresholds[g]:
            return g
    return "D"


def compute_score(model: Dict[str, Any], contact: Dict[str, Any], activity: Dict[str, Any],
                  now: Optional[datetime] = None) -> Dict[str, Any]:
    """Score one contact. `model` must be valid (validate_model / load_model)."""
    now = now or utcnow()
    fit, fit_rows = fit_breakdown(model, contact or {})
    engagement, eng_rows = engagement_breakdown(model, activity or {}, now)
    score = _round(clamp(fit * model["fit_weight"] / 100.0 + engagement * model["engagement_weight"] / 100.0))
    return {
        "score": score,
        "fit": fit,
        "engagement": engagement,
        "grade": grade_for(score, model["grade_thresholds"]),
        "fit_rules": fit_rows,
        "engagement_signals": eng_rows,
        "fit_weight": model["fit_weight"],
        "engagement_weight": model["engagement_weight"],
    }
