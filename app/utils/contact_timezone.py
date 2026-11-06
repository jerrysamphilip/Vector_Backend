"""
A contact's time zone (CM-01, BR-CM-01): derived from the contact's country and state/province, or set by
a user (any valid IANA name, which later derivation never overwrites; see ``refresh_contact_timezone``).

Derivation:
  * United States (or no country, with a recognised US state): the US state map of business_calendar;
  * a single-zone country: that country's zone, whatever the state;
  * a multi-zone country (Canada, Australia, Brazil, Mexico...): the state/province's zone when the
    mapping is clear, else None;
  * anything else (unknown country, US contact without a state): None, so scheduling falls back to the
    campaign's time zone instead of guessing New York.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any, Optional
from zoneinfo import available_timezones

from app.utils.business_calendar import US_STATE_NAME_TO_CODE, US_STATE_TIMEZONES, get_timezone_for_state

_US_NAMES = {"us", "usa", "u.s.", "u.s.a.", "united states", "united states of america", "america"}

# Country (lower-cased name, ISO alpha-2 and common aliases) -> its single time zone.
_SINGLE_ZONE: dict[str, tuple[str, ...]] = {
    "Asia/Kolkata": ("india", "in", "ind", "bharat"),
    "Europe/London": ("united kingdom", "uk", "gb", "gbr", "great britain", "britain", "england", "scotland",
                      "wales", "northern ireland"),
    "Europe/Dublin": ("ireland", "ie", "irl", "republic of ireland"),
    "Europe/Berlin": ("germany", "de", "deu", "deutschland"),
    "Europe/Paris": ("france", "fr", "fra"),
    "Europe/Amsterdam": ("netherlands", "nl", "nld", "the netherlands", "holland"),
    "Europe/Madrid": ("spain", "es", "esp", "españa", "espana"),
    "Europe/Rome": ("italy", "it", "ita", "italia"),
    "Europe/Brussels": ("belgium", "be", "bel"),
    "Europe/Luxembourg": ("luxembourg", "lu", "lux"),
    "Europe/Zurich": ("switzerland", "ch", "che"),
    "Europe/Vienna": ("austria", "at", "aut"),
    "Europe/Stockholm": ("sweden", "se", "swe"),
    "Europe/Oslo": ("norway", "no", "nor"),
    "Europe/Copenhagen": ("denmark", "dk", "dnk"),
    "Europe/Helsinki": ("finland", "fi", "fin"),
    "Europe/Warsaw": ("poland", "pl", "pol"),
    "Europe/Prague": ("czech republic", "czechia", "cz", "cze"),
    "Europe/Budapest": ("hungary", "hu", "hun"),
    "Europe/Bucharest": ("romania", "ro", "rou"),
    "Europe/Athens": ("greece", "gr", "grc"),
    "Europe/Istanbul": ("turkey", "türkiye", "turkiye", "tr", "tur"),
    "Europe/Kyiv": ("ukraine", "ua", "ukr"),
    "Asia/Jerusalem": ("israel", "il", "isr"),
    "Asia/Singapore": ("singapore", "sg", "sgp"),
    "Asia/Dubai": ("united arab emirates", "uae", "ae", "are", "u.a.e."),
    "Asia/Riyadh": ("saudi arabia", "sa", "sau", "ksa"),
    "Asia/Qatar": ("qatar", "qa", "qat"),
    "Asia/Tokyo": ("japan", "jp", "jpn"),
    "Asia/Seoul": ("south korea", "korea", "republic of korea", "kr", "kor"),
    "Asia/Shanghai": ("china", "cn", "chn", "people's republic of china", "prc"),
    "Asia/Hong_Kong": ("hong kong", "hk", "hkg"),
    "Asia/Taipei": ("taiwan", "tw", "twn"),
    "Asia/Manila": ("philippines", "ph", "phl"),
    "Asia/Kuala_Lumpur": ("malaysia", "my", "mys"),
    "Asia/Bangkok": ("thailand", "th", "tha"),
    "Asia/Ho_Chi_Minh": ("vietnam", "viet nam", "vn", "vnm"),
    "Asia/Karachi": ("pakistan", "pk", "pak"),
    "Asia/Dhaka": ("bangladesh", "bd", "bgd"),
    "Asia/Colombo": ("sri lanka", "lk", "lka"),
    "Asia/Kathmandu": ("nepal", "np", "npl"),
    "Pacific/Auckland": ("new zealand", "nz", "nzl"),
    "Africa/Johannesburg": ("south africa", "za", "zaf"),
    "Africa/Lagos": ("nigeria", "ng", "nga"),
    "Africa/Nairobi": ("kenya", "ke", "ken"),
    "Africa/Cairo": ("egypt", "eg", "egy"),
    "America/Bogota": ("colombia", "co", "col"),
    "America/Lima": ("peru", "pe", "per"),
    "America/Santiago": ("chile", "cl", "chl"),
    "America/Argentina/Buenos_Aires": ("argentina", "ar", "arg"),
}
COUNTRY_TIMEZONES: dict[str, str] = {alias: zone for zone, aliases in _SINGLE_ZONE.items() for alias in aliases}

# Multi-zone countries: state/province (lower-cased name or code) -> zone, where the mapping is clear.
_CANADA = {
    "America/Toronto": ("on", "ontario", "qc", "quebec", "québec"),
    "America/Vancouver": ("bc", "british columbia"),
    "America/Edmonton": ("ab", "alberta"),
    "America/Winnipeg": ("mb", "manitoba"),
    "America/Regina": ("sk", "saskatchewan"),
    "America/Halifax": ("ns", "nova scotia", "pe", "pei", "prince edward island", "nb", "new brunswick"),
    "America/St_Johns": ("nl", "newfoundland and labrador", "newfoundland"),
    "America/Whitehorse": ("yt", "yukon"),
    "America/Yellowknife": ("nt", "northwest territories"),
}
_AUSTRALIA = {
    "Australia/Sydney": ("nsw", "new south wales", "act", "australian capital territory"),
    "Australia/Melbourne": ("vic", "victoria"),
    "Australia/Brisbane": ("qld", "queensland"),
    "Australia/Adelaide": ("sa", "south australia"),
    "Australia/Perth": ("wa", "western australia"),
    "Australia/Hobart": ("tas", "tasmania"),
    "Australia/Darwin": ("nt", "northern territory"),
}
_BRAZIL = {
    "America/Sao_Paulo": ("sp", "são paulo", "sao paulo", "rj", "rio de janeiro", "mg", "minas gerais",
                          "pr", "paraná", "parana", "sc", "santa catarina", "rs", "rio grande do sul",
                          "df", "distrito federal", "go", "goiás", "goias", "es", "espírito santo",
                          "espirito santo", "ba", "bahia"),
    "America/Recife": ("pe", "pernambuco"),
    "America/Fortaleza": ("ce", "ceará", "ceara"),
    "America/Manaus": ("am", "amazonas"),
}
_MEXICO = {
    "America/Mexico_City": ("cdmx", "ciudad de méxico", "ciudad de mexico", "mexico city", "jalisco", "jal",
                            "nuevo león", "nuevo leon", "nl", "puebla", "guanajuato", "querétaro", "queretaro"),
    "America/Tijuana": ("baja california", "bc"),
    "America/Cancun": ("quintana roo", "roo"),
}


def _by_state(groups: dict[str, tuple[str, ...]]) -> dict[str, str]:
    return {alias: zone for zone, aliases in groups.items() for alias in aliases}


MULTI_ZONE_STATES: dict[str, dict[str, str]] = {}
for _names, _groups in ((("canada", "ca", "can"), _CANADA), (("australia", "au", "aus"), _AUSTRALIA),
                        (("brazil", "brasil", "br", "bra"), _BRAZIL), (("mexico", "méxico", "mx", "mex"), _MEXICO)):
    for _name in _names:
        MULTI_ZONE_STATES[_name] = _by_state(_groups)


@lru_cache(maxsize=1)
def _iana_names() -> frozenset[str]:
    return frozenset(available_timezones())


def valid_timezone(name: Optional[str]) -> Optional[str]:
    """``name`` (stripped) when it is an IANA time zone name, else None."""
    cleaned = (name or "").strip()
    return cleaned if cleaned in _iana_names() else None


def _us_state_zone(state: str) -> Optional[str]:
    """Strict US state lookup (code or full name; no prefix guessing, so "Maharashtra" is not "MA")."""
    cleaned = state.strip()
    code = cleaned.upper() if len(cleaned) == 2 else US_STATE_NAME_TO_CODE.get(cleaned.lower())
    return US_STATE_TIMEZONES.get(code or "")


def derive_timezone(country: Optional[str], state: Optional[str]) -> Optional[str]:
    """The zone a contact's country and state/province imply, or None when it is not clear."""
    country_key = (country or "").strip().lower()
    state_value = (state or "").strip()
    if not country_key or country_key in _US_NAMES:
        return _us_state_zone(state_value) if state_value else None
    if country_key in COUNTRY_TIMEZONES:
        return COUNTRY_TIMEZONES[country_key]
    states = MULTI_ZONE_STATES.get(country_key)
    if states and state_value:
        return states.get(state_value.lower())
    return None


def resolve_timezone(timezone: Optional[str], country: Optional[str], state: Optional[str]) -> Optional[str]:
    """The contact's zone: the stored one when valid, else the derived one, else None."""
    return valid_timezone(timezone) or derive_timezone(country, state)


def contact_timezone_of(contact: Any) -> Optional[str]:
    """resolve_timezone for a contact-like object (``timezone``, ``poc_country``, ``poc_state``)."""
    if contact is None:
        return None
    return resolve_timezone(getattr(contact, "timezone", None), getattr(contact, "poc_country", None),
                            getattr(contact, "poc_state", None))


def _derived_values(country: Optional[str], state: Optional[str]) -> set[Optional[str]]:
    """What derivation gives (or gave, before CM-01: US state map with a New York default) for a location."""
    values = {derive_timezone(country, state)}
    if state:
        values.add(get_timezone_for_state(state))
    return values


def refresh_contact_timezone(contact: Any, old_country: Optional[str], old_state: Optional[str]) -> None:
    """After a location change, re-derive the zone unless a user set it: a stored zone that is not what
    the old country/state implied was chosen by a user and is kept."""
    stored = getattr(contact, "timezone", None)
    if stored and stored not in _derived_values(old_country, old_state):
        return
    contact.timezone = derive_timezone(getattr(contact, "poc_country", None), getattr(contact, "poc_state", None))
