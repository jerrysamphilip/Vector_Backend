"""Unit tests (no database) for the pure rules extracted from the contacts router into
app/services/contact_*_service.py: custom property values, duplicate grouping, history
labels, export cells, bulk edit payloads and activity lead-status moves."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.core.errors import BadRequestError, PermissionDenied
from app.services import contact_activity_service as activities
from app.services import contact_field_service as fields
from app.services import contact_query_service as query
from app.services import contact_write_service as writes


def _def(field_type, options=None, label="Prop"):
    return SimpleNamespace(label=label, field_type=field_type, options=options)


# ── Custom property values ─────────────────────────────────────────

@pytest.mark.parametrize("value", [None, "", "   ", []])
def test_cf_u01_blank_values_remove_the_key(value):
    assert fields.clean_custom_value(_def("TEXT"), value) is None


def test_cf_u02_number_keeps_ints_and_floats():
    assert fields.clean_custom_value(_def("NUMBER"), "42") == 42
    assert isinstance(fields.clean_custom_value(_def("NUMBER"), "42.0"), int)
    assert fields.clean_custom_value(_def("NUMBER"), "2.5") == 2.5
    with pytest.raises(BadRequestError) as exc:
        fields.clean_custom_value(_def("NUMBER", label="Seats"), "many")
    assert exc.value.detail == "'Seats' must be a number"


def test_cf_u03_select_and_multi_checkbox_choices():
    opts = ["Red", "Green", "Blue"]
    assert fields.clean_custom_value(_def("SELECT", opts), "Green") == "Green"
    with pytest.raises(BadRequestError):
        fields.clean_custom_value(_def("RADIO", opts), "Pink")
    # Order follows the definition's options; "a; b" strings are split
    assert fields.clean_custom_value(_def("MULTI_CHECKBOX", opts), "Blue; Red") == ["Red", "Blue"]
    with pytest.raises(BadRequestError) as exc:
        fields.clean_custom_value(_def("MULTI_CHECKBOX", opts, "Colours"), ["Red", "Pink"])
    assert exc.value.detail == "'Colours' has unknown choices ['Pink']; allowed ['Red', 'Green', 'Blue']"


def test_cf_u04_date_phone_url_text():
    assert fields.clean_custom_value(_def("DATE"), "2026-03-01T10:00:00") == "2026-03-01"
    with pytest.raises(BadRequestError):
        fields.clean_custom_value(_def("DATE"), "01/03/2026")
    assert fields.clean_custom_value(_def("PHONE"), " +1 (737) 555-0188 ") == "+1 (737) 555-0188"
    with pytest.raises(BadRequestError):
        fields.clean_custom_value(_def("PHONE"), "call me")
    assert fields.clean_custom_value(_def("URL"), "example.com") == "https://example.com"
    assert fields.clean_custom_value(_def("URL"), "http://x.io") == "http://x.io"
    assert fields.clean_custom_value(_def("TEXT"), "x" * 1200) == "x" * 1000


def test_cf_u05_field_type_validation_and_keys():
    assert fields.validate_field_type("select", ["b", "a", "B"]) == ("SELECT", ["b", "a"])
    assert fields.validate_field_type("text", ["ignored"]) == ("TEXT", None)
    with pytest.raises(BadRequestError):
        fields.validate_field_type("RADIO", [])
    with pytest.raises(BadRequestError):
        fields.validate_field_type("BLOB", None)
    assert fields.field_key_base("Annual Revenue ($)") == "annual_revenue"
    assert fields.field_key_base("!!!") == "field"


def test_cf_u06_merge_custom_values():
    assert writes.merge_custom_values({"a": 1, "b": 2}, {"b": None, "c": 3}) == {"a": 1, "c": 3}
    assert writes.merge_custom_values({"a": 1}, {"a": None}) is None
    assert writes.merge_custom_values(None, {}) is None


# ── Duplicates ─────────────────────────────────────────────────────

def _row(pid, email, first=None, last=None, company=None, account_id=None, phone=None, mobile=None):
    return SimpleNamespace(prospect_id=pid, email=email, first_name=first, last_name=last,
                           company_name=company, account_id=account_id, phone=phone, mobile_phone=mobile)


def test_dup_u01_email_key_ignores_case_tags_and_gmail_dots():
    assert query.email_key("Jane.Doe+news@Gmail.com") == "janedoe@gmail.com"
    assert query.email_key("jane.doe@acme.com") == "jane.doe@acme.com"  # dots matter on company domains


def test_dup_u02_groups_merge_transitively_with_reasons():
    rows = [
        _row("1", "jane@gmail.com", "Jane", "Doe", "Acme"),
        _row("2", "j.ane@gmail.com"),
        _row("3", "other@acme.com", "Jane", "Doe", "ACME"),
        _row("4", "x@y.com", phone="+1 737 555 0188"),
        _row("5", "z@y.com", mobile="(737) 555-0188"),
        _row("6", "alone@y.com"),
    ]
    groups = query.duplicate_groups(rows, limit=10)
    as_sets = sorted((sorted(g["ids"]), sorted(g["reasons"])) for g in groups)
    assert as_sets == [
        # Same full name also falls into the initial+surname bucket
        (["1", "2", "3"], ["Same email address", "Same name and company", "Similar name at the same company"]),
        (["4", "5"], ["Same phone number"]),
    ]
    assert len(query.duplicate_groups(rows, limit=1)) == 1


def test_dup_u03_initial_at_same_company():
    keys = query.duplicate_keys(_row("1", "a@b.com", "Robert", "Smith", account_id="acc"))
    assert ("initial", "r", "smith", "acc") in keys
    assert not [k for k in query.duplicate_keys(_row("1", "a@b.com", "R", "Smith", "Acme")) if k[0] == "initial"]


# ── Filters, history, export ───────────────────────────────────────

def test_q_u01_parse_grades_and_timeline_types():
    assert query.parse_grades("a, B,x") == ["A", "B"]
    assert query.parse_grades("z") == []
    assert query.parse_timeline_types(" note,Call ") == {"NOTE", "CALL"}
    assert query.parse_timeline_types(None) is None


def test_q_u02_history_labels_and_values():
    assert query.history_label("custom.size", {"size": "Company size"}) == "Company size"
    assert query.history_label("custom.gone", {}) == "gone"
    assert query.history_label("owner_id", {}) == "Owner"
    assert query.history_label("email", {}) == "Email"
    assert query.history_label("some_field", {}) == "Some field"
    assert query.history_value("owner_id", "u1", {"u1": "Ann Lee"}, {}) == "Ann Lee"
    assert query.history_value("account_id", "a1", {}, {"a1": "Acme"}) == "Acme"
    assert query.history_value("lead_status", "unknown", {}, {}) == "unknown"
    assert query.history_value("phone", None, {}, {}) is None


def test_q_u03_export_cells_and_columns():
    assert query.export_cell("tags", ["a", "b"]) == "a; b"
    assert query.export_cell("tags", None) == ""
    assert query.export_cell("created_at", datetime(2026, 1, 2, 3, 4, 5)) == "2026-01-02 03:04"
    assert query.export_cell("email", "a@b.com") == "a@b.com"
    assert query.export_columns("email,bogus,phone") == ["email", "phone"]
    assert query.export_columns("bogus") == list(query.COLUMNS)


def test_q_u04_export_right():
    def user(role, custom=None):
        return SimpleNamespace(role=role, custom_permissions=custom)
    assert query.can_export(user("ADMIN"))
    assert query.can_export(user("MANAGER", ["export_data"]))
    assert not query.can_export(user("MANAGER", ["view_analytics"]))
    assert not query.can_export(user("AGENT"))


def test_q_u05_tag_counts_case_insensitive():
    assert query.tag_counts([["VIP", "b"], ["vip"], None, ["a"]]) == [
        {"tag": "VIP", "count": 2}, {"tag": "a", "count": 1}, {"tag": "b", "count": 1}]


# ── Writes and activities ──────────────────────────────────────────

def test_w_u01_bulk_property_data():
    assert writes.bulk_property_data("lead_status", "OPEN") == {"lead_status": "OPEN"}
    assert writes.bulk_property_data("custom.size", 5) == {"custom_fields": {"size": 5}}
    with pytest.raises(BadRequestError) as exc:
        writes.bulk_property_data("email", "x@y.com")
    assert exc.value.detail == "Can't bulk edit 'email'"


def test_w_u02_guards():
    with pytest.raises(BadRequestError):
        writes.check_enum("lead_status", "NOPE", ("NEW", "OPEN"))
    writes.check_enum("lead_status", None, ("NEW",))
    with pytest.raises(PermissionDenied) as exc:
        writes.require_can_delete(SimpleNamespace(role="AGENT"))
    assert exc.value.status_code == 403 and exc.value.detail == "Agents cannot delete contacts"


@pytest.mark.parametrize("kind,outcome,current,expected", [
    ("CALL", None, "NEW", "ATTEMPTED_TO_CONTACT"),
    ("CALL", "Connected", None, "CONNECTED"),
    ("MEETING", None, "OPEN", "CONNECTED"),
    ("EMAIL", None, "NEW", "ATTEMPTED_TO_CONTACT"),
    ("NOTE", None, "NEW", None),
    ("CALL", "Connected", "CONNECTED", None),
])
def test_a_u01_next_lead_status(kind, outcome, current, expected):
    assert activities.next_lead_status(kind, outcome, current) == expected


def test_a_u02_utc_naive():
    aware = datetime(2026, 1, 1, 12, 0, tzinfo=timezone(timedelta(hours=2)))
    assert activities.utc_naive(aware) == datetime(2026, 1, 1, 10, 0)
    naive = datetime(2026, 1, 1, 12, 0)
    assert activities.utc_naive(naive) is naive
    assert activities.utc_naive(None) is None


def test_e_u01_structured_conflict_detail_is_preserved():
    from app.core.errors import ConflictError
    detail = {"message": "A contact with this email already exists.", "prospect_id": "p1"}
    exc = ConflictError(detail["message"], detail=detail)
    assert exc.status_code == 409 and exc.detail == detail
