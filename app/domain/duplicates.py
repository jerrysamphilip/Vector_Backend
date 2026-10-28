"""
Duplicate contacts: pure matching rules (BR-CM-32, gap list C-08 / C-09 / F-21).

``find_pairs`` compares contacts inside small "blocks" (same email mailbox, same LinkedIn profile,
same phone, same company or corporate e-mail domain) instead of every contact with every other one,
and returns one ``PairMatch`` per pair of contacts with a 0-100 score, a confidence (HIGH / MEDIUM) and
reasons a person can read. Names only count inside the same company or corporate domain: there are
many John Smiths. A phone number or LinkedIn profile shared by more than ``SWITCHBOARD_CAP`` contacts is
a switchboard or a company page, not a person, and is ignored.

Name matching handles nicknames (Bob / Robert), a first initial (J. Smith / John Smith), typos
(difflib similarity), first and last name swapped, and accents (José / Jose).

No I/O; the service loads ``Candidate`` rows, passes the "is this a free / consumer mail domain?" check
(app/utils/free_mail.py) and stores the pairs.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Optional


SWITCHBOARD_CAP = 25          # phone / LinkedIn groups bigger than this are ignored
MAX_NAME_BLOCK = 200          # name comparisons inside one company block (n^2 / 2 at most 20k)
HIGH_SCORE = 85               # score >= HIGH_SCORE -> HIGH confidence, otherwise MEDIUM
MIN_SCORE = 60                # weaker matches are not suggested
TYPO_RATIO = 0.85             # difflib ratio for "similar" first names
LAST_NAME_RATIO = 0.8         # ... and last names (Smith / Smyth)

DomainCheck = Callable[[str], bool]  # True for gmail.com, orange.fr, ...


def _no_personal_domains(_: str) -> bool:
    return False


CONFIDENCE_HIGH = "HIGH"
CONFIDENCE_MEDIUM = "MEDIUM"

REASON_EMAIL = "Same email address (ignoring capitals, +tags and Gmail dots)"
REASON_LINKEDIN = "Same LinkedIn profile"
REASON_PHONE = "Same phone number"
REASON_PHONE_NAME = "Same phone number and a similar name"

NICKNAMES = {
    "bob": "robert", "bobby": "robert", "rob": "robert", "robbie": "robert", "bert": "robert",
    "bill": "william", "billy": "william", "will": "william", "willy": "william", "liam": "william",
    "liz": "elizabeth", "lizzie": "elizabeth", "beth": "elizabeth", "betty": "elizabeth", "eliza": "elizabeth",
    "mike": "michael", "mikey": "michael", "mick": "michael", "jim": "james", "jimmy": "james",
    "jamie": "james", "tom": "thomas", "tommy": "thomas", "dick": "richard", "rick": "richard",
    "ricky": "richard", "rich": "richard", "kate": "katherine", "katie": "katherine", "kathy": "katherine",
    "cathy": "katherine", "catherine": "katherine", "kathryn": "katherine", "jen": "jennifer",
    "jenny": "jennifer", "dave": "david", "davey": "david", "steve": "steven", "stephen": "steven",
    "chris": "christopher", "kit": "christopher", "matt": "matthew", "andy": "andrew", "drew": "andrew",
    "tony": "anthony", "joe": "joseph", "joey": "joseph", "sam": "samuel", "dan": "daniel",
    "danny": "daniel", "ben": "benjamin", "benny": "benjamin", "nick": "nicholas", "nicky": "nicholas",
    "alex": "alexander", "sandy": "alexander", "pat": "patrick", "patty": "patricia", "trish": "patricia",
    "ed": "edward", "eddie": "edward", "ted": "edward", "greg": "gregory", "jeff": "jeffrey",
    "geoff": "jeffrey", "ken": "kenneth", "kenny": "kenneth", "larry": "lawrence", "pete": "peter",
    "sue": "susan", "susie": "susan", "vicky": "victoria", "tori": "victoria", "zach": "zachary",
    "abby": "abigail", "maggie": "margaret", "meg": "margaret", "peggy": "margaret", "chuck": "charles",
    "charlie": "charles", "hank": "henry", "harry": "henry", "jack": "john", "johnny": "john",
    "jon": "john", "fred": "frederick", "freddie": "frederick", "frank": "francis", "fran": "frances",
    "al": "albert", "ron": "ronald", "ronnie": "ronald", "don": "donald", "doug": "douglas",
    "gerry": "gerald", "jerry": "gerald", "tim": "timothy", "timmy": "timothy", "val": "valerie",
    "becky": "rebecca", "becca": "rebecca", "debbie": "deborah", "deb": "deborah", "mandy": "amanda",
    "manny": "manuel", "pepe": "jose", "paco": "francisco", "nate": "nathan", "nat": "nathaniel",
    "phil": "philip", "ray": "raymond", "russ": "russell", "stan": "stanley", "walt": "walter",
}


@dataclass(frozen=True, slots=True)
class Candidate:
    """One contact as the matcher sees it (loaded by the service)."""

    prospect_id: str
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    email: Optional[str] = None
    company_name: Optional[str] = None
    account_id: Optional[str] = None
    phone: Optional[str] = None
    mobile_phone: Optional[str] = None
    linkedin_url: Optional[str] = None


@dataclass(slots=True)
class PairMatch:
    """A suggested pair: ``contact_a`` < ``contact_b``. One reason per kind of signal (email, linkedin,
    phone, name), the strongest one kept; each extra kind adds 5 to the best score (at most 100)."""

    contact_a: str
    contact_b: str
    signals: dict[str, tuple[int, str]] = field(default_factory=dict)

    @property
    def score(self) -> int:
        if not self.signals:
            return 0
        best = max(score for score, _ in self.signals.values())
        return min(100, best + 5 * (len(self.signals) - 1))

    @property
    def reasons(self) -> list[str]:
        return [reason for _, reason in self.signals.values()]

    @property
    def confidence(self) -> str:
        return confidence_for(self.score)

    def add(self, kind: str, score: int, reason: str) -> None:
        current = self.signals.get(kind)
        if current is None or score > current[0]:
            self.signals[kind] = (score, reason)


def confidence_for(score: int) -> str:
    return CONFIDENCE_HIGH if score >= HIGH_SCORE else CONFIDENCE_MEDIUM


def ordered(a: str, b: str) -> tuple[str, str]:
    return (a, b) if a < b else (b, a)


# ── Normalisation ───────────────────────────────────────────────────

def fold(value: Optional[str]) -> str:
    """Lower case, accents removed (José -> jose), only letters / digits / single spaces."""
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text).split())


def email_key(email: Optional[str], is_personal: DomainCheck = _no_personal_domains) -> Optional[str]:
    """The mailbox: case, +tags and (for personal domains) dots in the local part ignored."""
    local, _, domain = (email or "").strip().lower().rpartition("@")
    if not local or not domain:
        return None
    local = local.split("+", 1)[0]
    if domain == "googlemail.com":
        domain = "gmail.com"
    if domain == "gmail.com" or is_personal(domain):
        local = local.replace(".", "")
    return f"{local}@{domain}" if local else None


def corporate_domain(email: Optional[str], is_personal: DomainCheck = _no_personal_domains) -> Optional[str]:
    """The e-mail's domain when it is a company's (not gmail.com, orange.fr, ...)."""
    domain = (email or "").strip().lower().rpartition("@")[2]
    if not domain or "." not in domain or domain == "gmail.com" or is_personal(domain):
        return None
    return domain


def phone_key(phone: Optional[str]) -> Optional[str]:
    """Last 10 digits; None when there are too few digits to mean anything."""
    digits = re.sub(r"\D", "", phone or "")
    return digits[-10:] if len(digits) >= 7 else None


def linkedin_key(url: Optional[str]) -> Optional[str]:
    """The profile slug of a linkedin.com/in/... (or /pub/...) URL."""
    match = re.search(r"linkedin\.com/(?:in|pub)/([^/?#\s]+)", (url or "").strip().lower())
    return match.group(1).strip("/") if match else None


def canonical_first(first: str) -> str:
    """First word of a folded first name, nicknames mapped to the formal name."""
    word = first.split(" ")[0] if first else ""
    return NICKNAMES.get(word, word)


# ── Names ───────────────────────────────────────────────────────────

def _ratio(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio() if a and b else 0.0


def _first_names(f1: str, f2: str) -> tuple[int, str]:
    """(score, wording) for two folded first names; score 0 = not the same person."""
    if f1 == f2:
        return 100, "same name"
    c1, c2 = canonical_first(f1), canonical_first(f2)
    if c1 == c2:
        return 90, "nickname of the same first name"
    stripped1, stripped2 = f1.rstrip("."), f2.rstrip(".")
    if (len(stripped1) == 1 and c2.startswith(stripped1)) or (len(stripped2) == 1 and c1.startswith(stripped2)):
        return 80, "first initial matches"
    if _ratio(c1, c2) >= TYPO_RATIO:
        return 85, "similar spelling"
    return 0, ""


def name_match(first_a: Optional[str], last_a: Optional[str],
               first_b: Optional[str], last_b: Optional[str]) -> tuple[int, str]:
    """(0-100, why) for two people's names; also tries first and last swapped."""
    fa, la, fb, lb = fold(first_a), fold(last_a), fold(first_b), fold(last_b)
    if not (fa and la and fb and lb):
        return 0, ""
    best, why = 0, ""
    for f2, l2, swapped in ((fb, lb, False), (lb, fb, True)):
        last_ratio = 1.0 if la == l2 else _ratio(la, l2)
        if last_ratio < LAST_NAME_RATIO:
            continue
        first_score, wording = _first_names(fa, f2)
        if not first_score:
            continue
        score = first_score if last_ratio == 1.0 else min(first_score, 80)
        if swapped:
            score, wording = min(score, 80), "first and last name swapped"
        elif last_ratio < 1.0:
            wording = "similar spelling" if wording == "same name" else f"{wording}, similar last name"
        if score > best:
            best, why = score, wording
    return best, why


def name_reason(why: str, place: str) -> str:
    return f"Same name at the same {place}" if why == "same name" else f"Similar name at the same {place} ({why})"


# ── Pair finding ────────────────────────────────────────────────────

def _blocks(candidates: Sequence[Candidate], is_personal: DomainCheck) -> dict[tuple[str, ...], list[str]]:
    """Blocking keys -> contact ids. Name blocks are per company / domain and name prefix, on both
    first and last name, so a first/last swap still lands in a shared block."""
    blocks: dict[tuple[str, ...], list[str]] = {}

    def put(key: tuple[str, ...], pid: str) -> None:
        ids = blocks.setdefault(key, [])
        if pid not in ids:
            ids.append(pid)

    for c in candidates:
        key = email_key(c.email, is_personal)
        if key:
            put(("email", key), c.prospect_id)
        key = linkedin_key(c.linkedin_url)
        if key:
            put(("linkedin", key), c.prospect_id)
        for phone in (c.phone, c.mobile_phone):
            key = phone_key(phone)
            if key:
                put(("phone", key), c.prospect_id)
        places = []
        if c.account_id:
            places.append(("company", c.account_id))
        elif fold(c.company_name):
            places.append(("company", fold(c.company_name)))
        domain = corporate_domain(c.email, is_personal)
        if domain:
            places.append(("domain", domain))
        prefixes = {fold(c.first_name)[:2], fold(c.last_name)[:2]} - {""}
        for kind, place in places:
            for prefix in prefixes:
                put(("name", kind, place, prefix), c.prospect_id)
    return blocks


def _each_pair(ids: Sequence[str]) -> Iterable[tuple[str, str]]:
    for i, a in enumerate(ids):
        for b in ids[i + 1:]:
            yield ordered(a, b)


class _Pairs:
    def __init__(self, people: dict[str, Candidate]) -> None:
        self.people = people
        self.found: dict[tuple[str, str], PairMatch] = {}

    def add(self, pair: tuple[str, str], kind: str, score: int, reason: str) -> None:
        match = self.found.get(pair)
        if match is None:
            match = self.found[pair] = PairMatch(*pair)
        match.add(kind, score, reason)

    def names(self, a: str, b: str) -> tuple[int, str]:
        pa, pb = self.people[a], self.people[b]
        return name_match(pa.first_name, pa.last_name, pb.first_name, pb.last_name)

    def key_block(self, kind: str, ids: Sequence[str]) -> None:
        for a, b in _each_pair(ids):
            if kind == "email":
                self.add((a, b), kind, 100, REASON_EMAIL)
            elif kind == "linkedin":
                self.add((a, b), kind, 95, REASON_LINKEDIN)
            else:
                score, _why = self.names(a, b)
                if score:
                    self.add((a, b), kind, 90, REASON_PHONE_NAME)
                else:
                    self.add((a, b), kind, 60, REASON_PHONE)

    def name_block(self, place_kind: str, ids: Sequence[str]) -> None:
        place = "company" if place_kind == "company" else "e-mail domain"
        for a, b in _each_pair(ids):
            score, why = self.names(a, b)
            if score:
                self.add((a, b), "name", score, name_reason(why, place))


def find_pairs(candidates: Sequence[Candidate], is_personal: DomainCheck = _no_personal_domains) -> list[PairMatch]:
    """Every likely duplicate pair among ``candidates`` with score >= MIN_SCORE, best first.
    ``is_personal`` tells free mail domains (no company there; dots in Gmail-style addresses ignored)."""
    pairs = _Pairs({c.prospect_id: c for c in candidates})
    for key, ids in _blocks(candidates, is_personal).items():
        if len(ids) < 2:
            continue
        kind = key[0]
        if kind == "name":
            if len(ids) <= MAX_NAME_BLOCK:
                pairs.name_block(key[1], ids)
        elif kind == "email" or len(ids) <= SWITCHBOARD_CAP:
            pairs.key_block(kind, ids)
    found = [m for m in pairs.found.values() if m.score >= MIN_SCORE]
    return sorted(found, key=lambda m: (-m.score, m.contact_a, m.contact_b))


# ── Merge preview (BR-CM-33, C-19) ──────────────────────────────────

def default_choice(values: Sequence[tuple[str, object]]) -> Optional[str]:
    """Which record's value a merge keeps by default: the survivor's (first) when it has one, else
    the first record that has a value. ``values`` is [(prospect_id, value)], survivor first."""
    for pid, value in values:
        if value not in (None, ""):
            return pid
    return None


def values_differ(values: Iterable[object]) -> bool:
    filled = {str(v).strip().lower() for v in values if v not in (None, "")}
    return len(filled) > 1
