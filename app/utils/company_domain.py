# app/utils/company_domain.py
"""
Company domains (BR-CM-03/04): syntax check and the registrable domain a company is known by.

Subdomains collapse to the registrable domain, so contacts at us.acme.com and hr.acme.com land on
the company acme.com, and hr.acme.co.uk on acme.co.uk. There is no public-suffix-list dependency:
a curated set of common multi-part suffixes plus the usual "<co|com|org|...>.<2-letter ccTLD>"
pattern covers business domains well enough. The RFC 2606 / 6761 reserved names (example.com,
*.example, *.test, ...) are treated as suffixes, so placeholder and test domains keep one label.
"""

import re
from typing import List, Optional

# Multi-part public suffixes that do not follow the "<generic>.<ccTLD>" pattern below
MULTI_PART_SUFFIXES = frozenset({
    "co.uk", "org.uk", "ac.uk", "gov.uk", "ltd.uk", "plc.uk", "me.uk", "net.uk", "sch.uk", "nhs.uk",
    "com.au", "net.au", "org.au", "edu.au", "gov.au", "asn.au", "id.au",
    "co.nz", "org.nz", "net.nz", "govt.nz", "ac.nz",
    "co.in", "net.in", "org.in", "firm.in", "gen.in", "ind.in", "ac.in", "gov.in", "res.in",
    "co.jp", "ne.jp", "or.jp", "ac.jp", "go.jp", "ad.jp", "ed.jp", "gr.jp", "lg.jp",
    "co.kr", "or.kr", "ne.kr", "ac.kr", "go.kr", "re.kr",
    "com.br", "net.br", "org.br", "gov.br", "edu.br", "ind.br",
    "com.cn", "net.cn", "org.cn", "gov.cn", "edu.cn", "ac.cn",
    "com.hk", "org.hk", "net.hk", "edu.hk", "gov.hk",
    "com.sg", "org.sg", "net.sg", "edu.sg", "gov.sg",
    "com.tw", "org.tw", "net.tw", "edu.tw", "gov.tw",
    "com.mx", "org.mx", "net.mx", "gob.mx", "edu.mx",
    "co.za", "org.za", "net.za", "gov.za", "ac.za", "web.za",
    "co.il", "org.il", "net.il", "ac.il", "gov.il",
    "com.tr", "org.tr", "net.tr", "gen.tr", "gov.tr", "edu.tr",
    "com.ar", "com.co", "com.pe", "com.ve", "com.ec", "com.uy", "com.py", "com.bo",
    "com.my", "com.ph", "com.pk", "com.sa", "com.eg", "com.vn", "com.ng", "com.ua", "com.pl",
    "co.id", "co.th", "co.ke", "co.at", "or.at", "co.hu", "co.ma", "co.tz", "co.ug",
    "gc.ca", "qc.ca", "on.ca", "bc.ca", "ab.ca",
    # Hosting platforms where each customer gets a subdomain
    "github.io", "herokuapp.com", "azurewebsites.net", "cloudfront.net", "appspot.com",
    "blogspot.com", "wordpress.com", "netlify.app", "vercel.app", "pages.dev",
    # Reserved for documentation and tests (RFC 2606)
    "example.com", "example.net", "example.org",
})
RESERVED_TLDS = frozenset({"example", "test", "invalid", "localhost", "local"})
_GENERIC_SECOND_LEVEL = frozenset({"co", "com", "net", "org", "gov", "edu", "ac", "or", "ne", "go", "gob", "mil"})

_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_TLD_RE = re.compile(r"^(?:[a-z]{2,63}|xn--[a-z0-9-]{1,59})$")


def split_domain(domain: Optional[str]) -> List[str]:
    """Lower-cased labels of a domain ("Acme.COM." -> ["acme", "com"]); [] for blank input."""
    text = (domain or "").strip().lower().strip(".")
    return text.split(".") if text else []


def public_suffix_length(labels: List[str]) -> int:
    """How many trailing labels form the public suffix ("com" -> 1, "co.uk" -> 2)."""
    if len(labels) >= 2 and ".".join(labels[-2:]) in MULTI_PART_SUFFIXES:
        return 2
    if len(labels) >= 3 and len(labels[-1]) == 2 and labels[-2] in _GENERIC_SECOND_LEVEL:
        return 2
    return 1


def is_valid_domain(domain: Optional[str]) -> bool:
    """Letters/digits/hyphen labels, at least two of them, and an alphabetic (or IDN) TLD."""
    labels = split_domain(domain)
    return (
        len(labels) >= 2
        and len(".".join(labels)) <= 253
        and all(_LABEL_RE.fullmatch(label) for label in labels)
        and bool(_TLD_RE.fullmatch(labels[-1]))
    )


def registrable_domain(domain: Optional[str]) -> Optional[str]:
    """us.acme.com -> acme.com, hr.acme.co.uk -> acme.co.uk; a bare suffix is returned unchanged."""
    labels = split_domain(domain)
    if not labels:
        return None
    if labels[-1] in RESERVED_TLDS:
        return ".".join(labels[-2:])
    keep = public_suffix_length(labels) + 1
    return ".".join(labels[-keep:])


def bare_domain(value: Optional[str]) -> Optional[str]:
    """'https://www.Acme.com:443/about?x' or 'ceo@acme.com' -> 'www'-less host 'acme.com' (not validated)."""
    text = (value or "").strip().lower()
    if not text:
        return None
    if "@" in text and "://" not in text:
        text = text.rsplit("@", 1)[1]
    text = re.sub(r"^[a-z][a-z0-9+.-]*://", "", text)
    text = re.split(r"[/?#]", text, maxsplit=1)[0]
    text = re.sub(r":\d+$", "", text).strip(".")
    if text.startswith("www."):
        text = text[4:]
    return text or None
