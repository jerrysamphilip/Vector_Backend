"""
Region matching for lead assignment rules (BR-SF-03): pure, no I/O.

A rule's value and a contact's country / state match when they name the same place, whatever
the spelling: case and spacing are ignored, and ISO 3166 alpha-2 / alpha-3 codes and common names
are folded to one key ("US", "USA", "United States" -> "US"), as are US state codes and names
("CA", "California" -> "CA"). Anything not in the tables is compared as normalised text.
"""
import re
from typing import Dict, Optional, Tuple

# Canonical alpha-2 code -> (alpha-3, names). Common business countries only; extend as needed.
COUNTRIES: Dict[str, Tuple[str, Tuple[str, ...]]] = {
    "US": ("USA", ("United States", "United States of America", "America", "U.S.", "U.S.A.")),
    "GB": ("GBR", ("United Kingdom", "UK", "Great Britain", "Britain", "England", "U.K.")),
    "IN": ("IND", ("India", "Bharat")),
    "CA": ("CAN", ("Canada",)),
    "AU": ("AUS", ("Australia",)),
    "NZ": ("NZL", ("New Zealand",)),
    "IE": ("IRL", ("Ireland",)),
    "DE": ("DEU", ("Germany", "Deutschland")),
    "FR": ("FRA", ("France",)),
    "ES": ("ESP", ("Spain",)),
    "IT": ("ITA", ("Italy",)),
    "NL": ("NLD", ("Netherlands", "Holland", "The Netherlands")),
    "BE": ("BEL", ("Belgium",)),
    "CH": ("CHE", ("Switzerland",)),
    "AT": ("AUT", ("Austria",)),
    "SE": ("SWE", ("Sweden",)),
    "NO": ("NOR", ("Norway",)),
    "DK": ("DNK", ("Denmark",)),
    "FI": ("FIN", ("Finland",)),
    "PL": ("POL", ("Poland",)),
    "PT": ("PRT", ("Portugal",)),
    "AE": ("ARE", ("United Arab Emirates", "UAE", "U.A.E.")),
    "SA": ("SAU", ("Saudi Arabia", "KSA")),
    "QA": ("QAT", ("Qatar",)),
    "SG": ("SGP", ("Singapore",)),
    "MY": ("MYS", ("Malaysia",)),
    "ID": ("IDN", ("Indonesia",)),
    "PH": ("PHL", ("Philippines",)),
    "TH": ("THA", ("Thailand",)),
    "VN": ("VNM", ("Vietnam", "Viet Nam")),
    "JP": ("JPN", ("Japan",)),
    "KR": ("KOR", ("South Korea", "Korea", "Republic of Korea")),
    "CN": ("CHN", ("China", "People's Republic of China", "PRC")),
    "HK": ("HKG", ("Hong Kong",)),
    "TW": ("TWN", ("Taiwan",)),
    "PK": ("PAK", ("Pakistan",)),
    "BD": ("BGD", ("Bangladesh",)),
    "LK": ("LKA", ("Sri Lanka",)),
    "NP": ("NPL", ("Nepal",)),
    "IL": ("ISR", ("Israel",)),
    "TR": ("TUR", ("Turkey", "Turkiye", "Türkiye")),
    "ZA": ("ZAF", ("South Africa",)),
    "NG": ("NGA", ("Nigeria",)),
    "KE": ("KEN", ("Kenya",)),
    "EG": ("EGY", ("Egypt",)),
    "BR": ("BRA", ("Brazil", "Brasil")),
    "MX": ("MEX", ("Mexico",)),
    "AR": ("ARG", ("Argentina",)),
    "CL": ("CHL", ("Chile",)),
    "CO": ("COL", ("Colombia",)),
}

US_STATES: Dict[str, str] = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California", "CO": "Colorado",
    "CT": "Connecticut", "DE": "Delaware", "DC": "District of Columbia", "FL": "Florida", "GA": "Georgia",
    "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
    "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts",
    "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi", "MO": "Missouri", "MT": "Montana",
    "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico",
    "NY": "New York", "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
    "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota",
    "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont", "VA": "Virginia", "WA": "Washington",
    "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
}


def _fold(text: Optional[str]) -> str:
    """Lower case, punctuation dropped, runs of spaces collapsed ("  U.S.A. " -> "usa")."""
    text = re.sub(r"[.'’]", "", (text or "").lower())
    return " ".join(re.sub(r"[^\w]+", " ", text).split())


def _alias_table() -> Tuple[Dict[str, str], Dict[str, str]]:
    countries: Dict[str, str] = {}
    for code, (alpha3, names) in COUNTRIES.items():
        for alias in (code, alpha3, *names):
            countries[_fold(alias)] = code
    states: Dict[str, str] = {}
    for code, name in US_STATES.items():
        states[_fold(code)] = code
        states[_fold(name)] = code
    return countries, states


_COUNTRY_KEYS, _STATE_KEYS = _alias_table()


def country_key(value: Optional[str]) -> str:
    """One key per country: the alpha-2 code when known, else the folded text ("" when blank)."""
    folded = _fold(value)
    return _COUNTRY_KEYS.get(folded, folded)


def state_key(value: Optional[str]) -> str:
    """One key per US state: its two-letter code when known, else the folded text ("" when blank)."""
    folded = _fold(value)
    return _STATE_KEYS.get(folded, folded)


def region_matches(field: str, rule_value: Optional[str], contact_value: Optional[str]) -> bool:
    """Whether a region rule ("country" or "state") matches the contact's value."""
    key = country_key if field == "country" else state_key
    contact = key(contact_value)
    return bool(contact) and contact == key(rule_value)
