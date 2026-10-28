"""C-17 domain rules (app/domain/custom_properties.py): {value, label} options read from either stored form,
renames keep values, the fixed data type, text limits, booleans and the deactivation guard. Pure."""
import pytest

from app.domain import custom_properties as cp
from app.domain.custom_properties import PropertyRuleError


def test_tbe_old_string_options_read_as_value_label_pairs():
    assert cp.stored_options(["Gold", " Silver ", "", None]) == [
        {"value": "Gold", "label": "Gold"}, {"value": "Silver", "label": "Silver"}]
    assert cp.stored_options([{"value": "g", "label": "Gold tier"}, {"label": "Bronze"}, {"value": "x"},
                              {"value": "", "label": ""}]) == [
        {"value": "g", "label": "Gold tier"}, {"value": "Bronze", "label": "Bronze"}, {"value": "x", "label": "x"}]
    assert cp.stored_options(None) == [] and cp.option_values(["A", {"value": "b", "label": "B"}]) == ["A", "b"]


def test_tbe_a_choice_matches_by_value_then_by_label():
    options = [{"value": "gold", "label": "Gold tier"}, "Silver"]
    assert cp.option_value_for(options, "gold") == "gold"
    assert cp.option_value_for(options, " gold TIER ") == "gold"
    assert cp.option_value_for(options, "silver") == "Silver"
    assert cp.option_value_for(options, "Bronze") is None


def test_tbe_new_plain_options_take_their_label_as_value_and_drop_repeats():
    assert cp.normalize_options(["b", "a", "B", "  ", {"label": ""}]) == [
        {"value": "b", "label": "b"}, {"value": "a", "label": "a"}]
    assert cp.normalize_options(None) == []
    long = "x" * 150
    assert cp.normalize_options([long]) == [{"value": "x" * 100, "label": "x" * 100}]


def test_tbe_renaming_a_label_keeps_the_stored_value():
    current = ["Gold", "Silver"]  # stored before C-17
    renamed = cp.normalize_options([{"value": "Gold", "label": "Gold tier"}, "Silver", "Bronze"], current)
    assert renamed == [{"value": "Gold", "label": "Gold tier"}, {"value": "Silver", "label": "Silver"},
                       {"value": "Bronze", "label": "Bronze"}]
    # an old client sending the values back as strings keeps the new label
    assert cp.normalize_options(["Gold", "silver"], renamed) == [
        {"value": "Gold", "label": "Gold tier"}, {"value": "Silver", "label": "Silver"}]
    # a label-only dict naming a current option keeps it; an explicit new value is a new option
    assert cp.normalize_options([{"label": "gold tier"}, {"value": "plat", "label": "Platinum"}], renamed) == [
        {"value": "Gold", "label": "Gold tier"}, {"value": "plat", "label": "Platinum"}]


def test_tbe_two_options_with_one_explicit_value_are_refused():
    with pytest.raises(PropertyRuleError, match="Duplicate option value 'A'"):
        cp.normalize_options(["a", {"value": "A", "label": "Other"}])


def test_tbe_data_type_is_fixed():
    cp.check_type_unchanged("NUMBER", None)
    cp.check_type_unchanged("NUMBER", " number ")
    with pytest.raises(PropertyRuleError, match="cannot be changed"):
        cp.check_type_unchanged("NUMBER", "TEXT")


def test_tbe_max_length_only_for_text_and_within_the_type_limit():
    assert cp.checked_max_length("TEXT", None) is None
    assert cp.checked_max_length("TEXT", 20) == 20 and cp.checked_max_length("LONG_TEXT", 10000) == 10000
    with pytest.raises(PropertyRuleError, match="between 1 and 1000"):
        cp.checked_max_length("TEXT", 1001)
    with pytest.raises(PropertyRuleError, match="between 1 and 10000"):
        cp.checked_max_length("LONG_TEXT", 0)
    with pytest.raises(PropertyRuleError, match="Only text properties"):
        cp.checked_max_length("NUMBER", 5)
    assert cp.text_limit("TEXT", None) == 1000 and cp.text_limit("LONG_TEXT", None) == 10000
    assert cp.text_limit("TEXT", 5) == 5 and cp.text_limit("URL", None) == 1000


@pytest.mark.parametrize("raw,expected", [(True, True), (False, False), ("Yes", True), (" 0 ", False),
                                          ("on", True), ("n", False), (1, True)])
def test_tbe_booleans(raw, expected):
    assert cp.parse_boolean(raw, "Opted in") is expected


def test_tbe_bad_boolean_and_system_deactivation():
    with pytest.raises(PropertyRuleError, match="'Opted in' must be true or false"):
        cp.parse_boolean("maybe", "Opted in")
    cp.check_can_deactivate(False)
    with pytest.raises(PropertyRuleError, match="System properties cannot be deactivated"):
        cp.check_can_deactivate(True)
