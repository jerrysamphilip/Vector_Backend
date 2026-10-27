"""
Rule-based reply classification (S-09): the fallback when no LLM key is configured, the daily LLM
budget is spent, or the LLM call failed; and the deterministic first pass that honours a plain
opt-out ("unsubscribe", "stop emailing me") even when the LLM is down.

Pure functions, no I/O. Rules, in order:
  1. RFC 3834 ``Auto-Submitted: auto-replied/auto-generated`` (or X-Autoreply / Precedence:
     auto_reply) -> OUT_OF_OFFICE, confidence 1.0.
  2. An unsubscribe request, negation-aware ("please don't stop emailing me" and "no need to
     unsubscribe" are not opt-outs). A short reply (<= 400 characters) asking it is confident (0.95);
     the same words inside a longer message are flagged (0.6) instead of acted on.
  3. Out-of-office phrases in the subject or the first 500 characters -> OUT_OF_OFFICE (0.8).
  4. Negative phrases ("not interested", "no thanks", ...) -> NEGATIVE; checked before the positive
     ones so "not interested" never counts as "interested".
  5. Positive phrases ("let's talk", "send me pricing", "book a call", ...) -> POSITIVE.
  6. Otherwise NEUTRAL (0.5).
Our own footer ("Please reply 'unsubscribe' ...") pasted back without a quote marker is removed first.
"""

from __future__ import annotations

import re
from typing import Mapping, Optional

from app.domain.inbox import LabelSource, ReplyClassification, ReplyLabel

_AUTO_SUBMITTED_VALUES = {"auto-replied", "auto-generated", "auto-notified"}
_PRECEDENCE_AUTO = {"auto_reply", "auto-reply"}

MAX_CONFIDENT_UNSUBSCRIBE_CHARS = 400

_OOO = re.compile(
    r"(out[\s-]of[\s-](the[\s-])?office|automatic reply|auto-?reply|"
    r"away from (the )?office|on (annual |parental |maternity |sick )?leave|on vacation|on holiday|"
    r"currently (unavailable|out|away|travell?ing)|vacation (auto-?)?respon|"
    r"away from my (desk|email)|limited access to (my )?e-?mail|"
    r"returning (to the office )?on|back in the office on|i will be back on)",
    re.IGNORECASE,
)

_OWN_FOOTER = re.compile(
    r"(?im)^.*(reply\s+['\"]?\s*unsubscribe|wish\s+to\s+receive\s+such\s+business\s+communication|"
    r"/api/tracking/unsubscribe/|unsubscribe<https?://).*$"
)
_QUOTED_LINE = re.compile(r"(?m)^\s*>.*$")

_UNSUB_REQUEST = re.compile(
    r"\b(unsubscribe|opt[\s-]?out|remove\s+(me|us|my\s+(e-?mail|address|name))|take\s+(me|us)\s+off|"
    r"delete\s+(me|my\s+(e-?mail|address|data))|"
    r"stop\s+(e-?mailing|sending|contacting|messaging|mailing|spamming)|"
    r"(do\s+not|don'?t|dont|never)\s+(e-?mail|contact|message|send|mail|write\s+to)\b|"
    r"(do\s+not|don'?t|dont)\s+wish\s+to\s+(receive|get|hear)|"
    r"no\s+longer\s+(wish|want)\s+to\s+(receive|get|hear)|"
    r"no\s+more\s+(e-?mails?|messages?)|"
    r"^\s*stop\s*[.!]*\s*$)",
    re.IGNORECASE | re.MULTILINE,
)
# The opposite of an opt-out: "please don't stop emailing me", "no need to unsubscribe me",
# "I do not want to unsubscribe", "never remove me".
_UNSUB_NEGATED = re.compile(
    r"\b(don'?t|dont|do\s+not|never|not|no\s+need\s+to|no\s+reason\s+to)\s+"
    r"((want|wish|need|have)\s+(you\s+)?to\s+|(you\s+)?(to\s+)?|please\s+)?"
    r"(stop|unsubscribe|remove|opt[\s-]?out|take\s+me\s+off)",
    re.IGNORECASE,
)

_NEGATIVE = re.compile(
    r"\b(not\s+(interested|a\s+(good\s+)?fit|relevant|looking|the\s+right\s+(person|time))|"
    r"no\s+(thanks|thank\s+you|interest|need)|no,?\s+thanks|"
    r"(we'?re|we\s+are|i'?m|i\s+am)\s+(all\s+set|good|covered|happy\s+with)|"
    r"(already|currently)\s+(have|use|using|work\s+with)|"
    r"(do\s+not|don'?t|dont)\s+(need|want)|"
    r"not\s+at\s+this\s+time|pass\s+on\s+this|decline|wrong\s+person)",
    re.IGNORECASE,
)
_POSITIVE = re.compile(
    r"\b(interested|sounds\s+(good|great|interesting)|let'?s\s+(talk|chat|connect|meet|schedule)|"
    r"(book|schedule|set\s+up|arrange)\s+(a\s+)?(call|meeting|demo|time|chat)|"
    r"(send|share)\s+(me\s+)?(more|the\s+)?(info|information|details|pricing|deck|proposal)|"
    r"(what|how\s+much)\s+(is|are|does)\s+(the\s+)?(price|pricing|cost)|pricing|"
    r"happy\s+to\s+(chat|talk|connect|meet)|free\s+(on|for\s+a\s+call)|tell\s+me\s+more|"
    r"call\s+me|give\s+me\s+a\s+call|available\s+(on|for|next)|demo)\b",
    re.IGNORECASE,
)


def _header(headers: Optional[Mapping[str, Optional[str]]], name: str) -> str:
    if not headers:
        return ""
    for key, value in headers.items():
        if key.lower() == name.lower():
            return (value or "").strip().lower()
    return ""


def is_auto_reply_header(headers: Optional[Mapping[str, Optional[str]]]) -> bool:
    """RFC 3834 Auto-Submitted, or the X-Autoreply / Precedence headers older servers set."""
    if _header(headers, "Auto-Submitted") in _AUTO_SUBMITTED_VALUES:
        return True
    if _header(headers, "X-Autoreply") in ("yes", "true") or _header(headers, "X-Autorespond"):
        return True
    return _header(headers, "Precedence") in _PRECEDENCE_AUTO


def clean_reply_text(text: Optional[str]) -> str:
    """The person's own words: our footer and quoted ('>') lines removed, typographic quotes made plain."""
    cleaned = _OWN_FOOTER.sub("", text or "")
    cleaned = _QUOTED_LINE.sub("", cleaned)
    return cleaned.replace("’", "'").replace("‘", "'").strip()


def _sentences(text: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.!?])\s+|\n+", text) if s.strip()]


def is_unsubscribe_request(text: Optional[str]) -> bool:
    """True when some sentence asks to be removed / to stop receiving mail and is not negated."""
    for sentence in _sentences(clean_reply_text(text)):
        if _UNSUB_REQUEST.search(sentence) and not _UNSUB_NEGATED.search(sentence):
            return True
    return False


def unsubscribe_confidence(text: Optional[str]) -> Optional[float]:
    """0.95 for a short opt-out, 0.6 when the request sits inside a longer message, None if none."""
    if not is_unsubscribe_request(text):
        return None
    return 0.95 if len(clean_reply_text(text)) <= MAX_CONFIDENT_UNSUBSCRIBE_CHARS else 0.6


def is_out_of_office_text(subject: Optional[str], body: Optional[str]) -> bool:
    return bool(_OOO.search(f"{subject or ''}\n{(body or '')[:500]}"))


def pre_classify(subject: Optional[str], body: Optional[str],
                 headers: Optional[Mapping[str, Optional[str]]] = None) -> Optional[ReplyClassification]:
    """The cases decided without an LLM: auto-reply header and a plain opt-out. None otherwise."""
    if is_auto_reply_header(headers):
        return ReplyClassification(ReplyLabel.OUT_OF_OFFICE, 1.0, LabelSource.HEADER,
                                   "Auto-Submitted header marks an automatic reply")
    confidence = unsubscribe_confidence(body)
    if confidence is not None and confidence >= 0.95:
        return ReplyClassification(ReplyLabel.UNSUBSCRIBE, confidence, LabelSource.RULES,
                                   "The reply asks to be removed or to stop receiving emails")
    return None


def classify_by_rules(subject: Optional[str], body: Optional[str],
                      headers: Optional[Mapping[str, Optional[str]]] = None) -> ReplyClassification:
    """Full rule-based classification; always returns a label."""
    decided = pre_classify(subject, body, headers)
    if decided is not None:
        return decided
    text = clean_reply_text(body)
    confidence = unsubscribe_confidence(text)
    if confidence is not None:
        return ReplyClassification(ReplyLabel.UNSUBSCRIBE, confidence, LabelSource.RULES,
                                   "Possible opt-out inside a longer reply")
    if is_out_of_office_text(subject, text):
        return ReplyClassification(ReplyLabel.OUT_OF_OFFICE, 0.8, LabelSource.RULES, "Out-of-office wording")
    if _NEGATIVE.search(text):
        return ReplyClassification(ReplyLabel.NEGATIVE, 0.7, LabelSource.RULES, "Declining wording")
    if _POSITIVE.search(text):
        return ReplyClassification(ReplyLabel.POSITIVE, 0.7, LabelSource.RULES, "Interested wording")
    return ReplyClassification(ReplyLabel.NEUTRAL, 0.5, LabelSource.RULES, "No clear intent")
