"""
Custom property rules (C-17): choice options as ``{value, label}``, the fixed data type, text length limits
and boolean parsing. Pure: no I/O. Errors are ``PropertyRuleError`` (a ``ValueError``).

Options. Definitions created before C-17 store options as plain strings; the stored contact / company values
are those strings. ``normalize_options`` reads either form (a string ``"Gold"`` is ``{"value": "Gold",
"label": "Gold"}``), so old rows are migrated on read and never rewritten. A new string option gets
``value == label``; editing keeps the value of an option matched by value or label, so renaming a label
(``{"value": "Gold", "label": "Gold tier"}``) never orphans the values already stored.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any, Optional

CHOICE_TYPES = ("SELECT", "RADIO", "MULTI_CHECKBOX")
TEXT_TYPES = ("TEXT", "LONG_TEXT")
# Default and largest length for a text property (TEXT values were always cut at 1000)
TEXT_LIMITS = {"TEXT": 1000, "LONG_TEXT": 10000}
OPTION_TEXT_MAX = 100

_TRUE = ("true", "1", "yes", "y", "on")
_FALSE = ("false", "0", "no", "n", "off")


class PropertyRuleError(ValueError):
    """A custom property definition or value breaks a rule (the service maps it to 400)."""


def _option_pair(raw: Any) -> tuple[str, str]:
    """(value, label) of one stored or requested option; blanks are ("", "")."""
    if isinstance(raw, dict):
        label = str(raw.get("label") if raw.get("label") not in (None, "") else raw.get("value") or "").strip()
        value = str(raw.get("value") if raw.get("value") not in (None, "") else "").strip()
        return value, label
    text = str(raw if raw is not None else "").strip()
    return text, text


def stored_options(raw: Optional[Iterable[Any]]) -> list[dict[str, str]]:
    """A definition's stored options (strings or dicts) as ``[{value, label}]``; blanks are dropped."""
    out = []
    for item in raw or []:
        value, label = _option_pair(item)
        if value or label:
            out.append({"value": value or label, "label": label or value})
    return out


def option_values(raw: Optional[Iterable[Any]]) -> list[str]:
    return [o["value"] for o in stored_options(raw)]


def option_value_for(raw: Optional[Iterable[Any]], given: Any) -> Optional[str]:
    """The stored value a submitted choice means: an exact value, else a label ignoring case."""
    text = str(given).strip()
    options = stored_options(raw)
    for option in options:
        if option["value"] == text:
            return option["value"]
    for option in options:
        if option["label"].lower() == text.lower():
            return option["value"]
    return None


def _matching_option(current: Sequence[dict[str, str]], value: str, label: str) -> Optional[dict[str, str]]:
    """The current option an edited one refers to: by its explicit value, else by label or value text."""
    if value:
        return next((o for o in current if o["value"] == value), None)
    wanted = label.lower()
    return next((o for o in current if wanted in (o["label"].lower(), o["value"].lower())), None)


def normalize_options(requested: Optional[Iterable[Any]],
                      current: Optional[Iterable[Any]] = None) -> list[dict[str, str]]:
    """Requested options (strings or ``{value, label}``) as ``[{value, label}]``.

    A ``{value, label}`` keeps its value and takes the new label (a rename). A plain string, or a dict without
    a value, that names a current option (by value or label) keeps that option unchanged; otherwise its label
    becomes the value. Repeated options are dropped (first wins, ignoring case); two options with the same
    explicit value are an error."""
    existing = stored_options(current)
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in requested or []:
        value, label = _option_pair(item)
        explicit = isinstance(item, dict) and bool(value)
        if not label:
            continue
        match = _matching_option(existing, value if explicit else "", label)
        if explicit:
            option = {"value": value[:OPTION_TEXT_MAX], "label": label[:OPTION_TEXT_MAX]}
        elif match:
            option = dict(match)
        else:
            option = {"value": label[:OPTION_TEXT_MAX], "label": label[:OPTION_TEXT_MAX]}
        if option["value"].lower() in seen:
            if explicit:
                raise PropertyRuleError(f"Duplicate option value '{option['value']}'")
            continue
        seen.add(option["value"].lower())
        out.append(option)
    return out


def check_type_unchanged(current: str, requested: Optional[str]) -> None:
    """A property's data type is fixed once created (stored values would no longer fit it)."""
    if requested and requested.strip().upper() != current:
        raise PropertyRuleError("A property's data type cannot be changed after it is created")


def checked_max_length(field_type: str, max_length: Optional[int]) -> Optional[int]:
    """An optional character limit, for text properties only, within the type's largest length."""
    if max_length is None:
        return None
    limit = TEXT_LIMITS.get(field_type)
    if limit is None:
        raise PropertyRuleError("Only text properties can have a maximum length")
    if not 1 <= max_length <= limit:
        raise PropertyRuleError(f"max_length must be between 1 and {limit}")
    return max_length


def text_limit(field_type: str, max_length: Optional[int]) -> int:
    return max_length or TEXT_LIMITS.get(field_type, TEXT_LIMITS["TEXT"])


def parse_boolean(value: Any, label: str) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    raise PropertyRuleError(f"'{label}' must be true or false")


def check_can_deactivate(is_system: bool) -> None:
    if is_system:
        raise PropertyRuleError("System properties cannot be deactivated")
