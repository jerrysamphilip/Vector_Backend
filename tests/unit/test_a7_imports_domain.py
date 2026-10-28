"""A7 import domain (app/domain/imports.py) and value / file parsing (app/services/import_parsing.py). No database."""
import io
from datetime import date

import pytest
from openpyxl import Workbook

from app.domain.imports import (
    DateOrder,
    ImportInputError,
    ImportLimits,
    ImportMode,
    ImportOptions,
    NewProperty,
    SavedMappingInput,
    Tally,
    parse_mapping,
    progress_percent,
    suggest_mapping,
)
from app.services import import_parsing as ip
from app.services.import_limits import FileRejected

# ── Options ──


def test_options_defaults_and_legacy_update_existing():
    o = ImportOptions.from_raw({})
    assert o.mode is ImportMode.CREATE_AND_UPDATE and o.update_existing and o.date_order is DateOrder.MDY
    assert ImportOptions.from_raw({"update_existing": False}).update_existing is False
    assert ImportOptions.from_raw(None).mode.creates and ImportOptions.from_raw(None).mode.updates


def test_options_modes_and_date_order_are_case_insensitive():
    o = ImportOptions.from_raw({"mode": "update_only", "date_order": "dmy", "default_source": "  Trade show  "})
    assert o.mode is ImportMode.UPDATE_ONLY and not o.mode.creates and o.mode.updates
    assert o.date_order is DateOrder.DMY and o.default_source == "Trade show"
    assert ImportMode.CREATE.creates and not ImportMode.CREATE.updates


@pytest.mark.parametrize("raw", [{"mode": "MERGE"}, {"date_order": "YMD"}, ["not", "a", "dict"]])
def test_options_refuse_unknown_values(raw):
    with pytest.raises(ImportInputError):
        ImportOptions.from_raw(raw)


def test_options_round_trip_and_list_replacement():
    o = ImportOptions.from_raw({"new_list_name": "Expo", "owner_id": "u1", "sheet": "Leads"})
    d = o.with_list("L1").as_dict()
    assert d["list_id"] == "L1" and d["new_list_name"] is None and d["owner_id"] == "u1"
    assert ImportOptions.from_raw(d).list_id == "L1"


def test_limits_must_be_positive():
    assert ImportLimits().max_rows == 50_000 and ImportLimits().sync_max_rows == 1_000
    with pytest.raises(ImportInputError):
        ImportLimits(max_rows=0)


# ── Mapping ──


def test_parse_mapping_drops_skips_and_checks_types():
    assert parse_mapping({"Email": "contact.email", "Notes": "skip", "X": None, "Y": ""}) == {"Email": "contact.email"}
    with pytest.raises(ImportInputError):
        parse_mapping(["Email"])
    with pytest.raises(ImportInputError):
        parse_mapping({"Email": 3})


def test_new_property_target():
    assert NewProperty.parse("contact.email", ("TEXT",)) is None
    assert NewProperty.parse("new:select:Tier", ("TEXT", "SELECT")) == NewProperty("SELECT", "Tier")
    assert NewProperty.parse("new::Notes", ("TEXT",)) == NewProperty("TEXT", "Notes")
    with pytest.raises(ImportInputError):
        NewProperty.parse("new:BLOB:Tier", ("TEXT",))
    with pytest.raises(ImportInputError):
        NewProperty.parse("new:TEXT: ", ("TEXT",))


def test_saved_mapping_input():
    m = SavedMappingInput.from_raw("  Trade   show  ", {"Email": "contact.email", "Junk": "skip"},
                                   {"mode": "CREATE", "list_id": "L1", "date_order": "DMY"})
    assert m.name == "Trade show" and m.mapping == {"Email": "contact.email"}
    assert m.options["mode"] == "CREATE" and m.options["date_order"] == "DMY" and "list_id" not in m.options
    for name, mapping in (("", {"E": "contact.email"}), ("x" * 101, {"E": "contact.email"}), ("ok", {"E": "skip"}),
                          ("bad<name>", {"E": "contact.email"})):
        with pytest.raises(ImportInputError):
            SavedMappingInput.from_raw(name, mapping)


def test_suggest_mapping_aliases_custom_and_company_only():
    m = suggest_mapping(["E-mail", "First Name", "Tier level", "Whatever"], {"tier_level": "Tier level"})
    assert m == {"E-mail": "contact.email", "First Name": "contact.first_name", "Tier level": "custom.tier_level",
                 "Whatever": "skip"}
    m = suggest_mapping(["Company", "Industry", "First name"], {})
    assert m == {"Company": "company.name", "Industry": "company.industry", "First name": "skip"}


def test_progress_and_tally():
    assert progress_percent(None, None) == 0 and progress_percent(50, 200) == 25 and progress_percent(300, 200) == 100
    t = Tally(contacts_created=2)
    c = t.copy()
    c.contacts_created += 1
    assert t.contacts_created == 2 and c.as_dict()["contacts_created"] == 3


# ── Values (C-05) ──


@pytest.mark.parametrize("text,order,expected", [
    ("2026-03-04", DateOrder.MDY, date(2026, 3, 4)),
    ("03/04/2026", DateOrder.MDY, date(2026, 3, 4)),
    ("03/04/2026", DateOrder.DMY, date(2026, 4, 3)),
    ("25/12/2026", DateOrder.MDY, date(2026, 12, 25)),   # unambiguous whatever the order
    ("12/25/26", DateOrder.DMY, date(2026, 12, 25)),
    ("4 Mar 2026", DateOrder.MDY, date(2026, 3, 4)),
    ("March 4, 2026", DateOrder.MDY, date(2026, 3, 4)),
    ("2026-03-04 10:30", DateOrder.MDY, date(2026, 3, 4)),
    ("45000", DateOrder.MDY, date(2023, 3, 15)),          # Excel serial
    ("45000.0", DateOrder.MDY, date(2023, 3, 15)),
])
def test_parse_date(text, order, expected):
    assert ip.parse_date(text, order) == expected


@pytest.mark.parametrize("text", ["13/13/2026", "yesterday", "2026-02-30", "12"])
def test_parse_date_refuses(text):
    with pytest.raises(ValueError):
        ip.parse_date(text)


@pytest.mark.parametrize("text,expected", [
    ("1,234.50", 1234.5), ("$1,200", 1200.0), ("(1,234)", -1234.0), ("-5", -5.0), ("12%", 12.0), ("€ 3 000", 3000.0),
])
def test_parse_number(text, expected):
    assert ip.parse_number(text) == expected


@pytest.mark.parametrize("text", ["", "abc", "1.2.3", "5-3"])
def test_parse_number_refuses(text):
    with pytest.raises(ValueError):
        ip.parse_number(text)


def test_parse_bool():
    assert ip.parse_bool("Yes") and ip.parse_bool("TRUE") and ip.parse_bool("x")
    assert not ip.parse_bool("no") and not ip.parse_bool("False") and not ip.parse_bool("0")
    with pytest.raises(ValueError):
        ip.parse_bool("maybe")


def test_match_choice_by_value_or_label_and_yes_no():
    assert ip.match_choice(["Gold", "Silver"], "gold") == "Gold"
    assert ip.match_choice([{"value": "hi", "label": "High priority"}], "high-priority") == "hi"
    assert ip.match_choice(["Yes", "No"], "true") == "Yes" and ip.match_choice(["Yes", "No"], "n") == "No"
    assert ip.match_choice(["Gold"], "Bronze") is None


def test_email_name_and_phone_helpers():
    assert ip.email_address("Jane Doe <Jane@Acme.com>") == "Jane@Acme.com"
    assert ip.email_address("mailto:j@x.com") == "j@x.com" and ip.email_address(None) is None
    assert ip.split_name("Doe, Jane") == ("Jane", "Doe")
    assert ip.split_name("Jane van der Berg") == ("Jane", "van der Berg") and ip.split_name(" ") == (None, None)
    assert ip.clean_phone("4155550100.0") == "4155550100" and ip.clean_phone("+44 20 7946 0958") == "+44 20 7946 0958"


def test_custom_value_by_type():
    assert ip.custom_value("NUMBER", "Seats", [], "1,200", DateOrder.MDY) == 1200
    assert ip.custom_value("NUMBER", "Ratio", [], "(2.5)", DateOrder.MDY) == -2.5
    assert ip.custom_value("DATE", "Renewal", [], "04/03/2026", DateOrder.DMY) == "2026-03-04"
    assert ip.custom_value("SELECT", "Tier", ["Gold"], "GOLD", DateOrder.MDY) == "Gold"
    assert ip.custom_value("MULTI_CHECKBOX", "Tags", ["A", "B"], "a; b", DateOrder.MDY) == ["A", "B"]
    assert ip.custom_value("PHONE", "Fax", [], "201555.0", DateOrder.MDY) == "201555"
    with pytest.raises(ip.ValueProblem, match="Seats: 'many' is not a number"):
        ip.custom_value("NUMBER", "Seats", [], "many", DateOrder.MDY)
    with pytest.raises(ip.ValueProblem, match="is not a date"):
        ip.custom_value("DATE", "Renewal", [], "soon", DateOrder.MDY)


def test_data_error_reason():
    exc = Exception("(1406, \"Data too long for column 'designation' at row 1\")")
    assert ip.data_error_reason(exc, {"designation": "Job title"}).startswith("Job title:")
    assert ip.data_error_reason(Exception("odd"), {}).startswith("A value")


# ── Files (C-06) ──

LIMITS = ImportLimits(max_rows=5, max_columns=4, max_bytes=200_000)


def test_csv_headers_are_cleaned_and_deduplicated():
    raw = "Email;email; ;Name\na@x.com;b@x.com;;Ann\n\n;;;\n".encode("cp1252")
    t = ip.read_table(raw, "people.csv", LIMITS)
    assert t.headers == ["Email", "email (2)", "Column 3", "Name"]
    assert t.rows == [{"Email": "a@x.com", "email (2)": "b@x.com", "Column 3": None, "Name": "Ann"}]


def test_csv_limits():
    with pytest.raises(FileRejected, match="at most 5 rows"):
        ip.read_table(("e\n" + "a@x.com\n" * 6).encode(), "x.csv", LIMITS)
    with pytest.raises(FileRejected, match="5 columns"):
        ip.read_table(b"a,b,c,d,e\n1,2,3,4,5\n", "x.csv", LIMITS)
    with pytest.raises(FileRejected, match="larger than"):
        ip.read_table(b"e\n" + b"x" * 300_000, "x.csv", LIMITS)
    with pytest.raises(FileRejected, match="empty"):
        ip.read_table(b"", "x.csv", LIMITS)


def _xlsx(sheets):
    wb = Workbook()
    wb.remove(wb.active)
    for name, rows in sheets.items():
        ws = wb.create_sheet(name)
        for row in rows:
            ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_xlsx_sheet_choice_and_cell_text():
    from datetime import datetime
    raw = _xlsx({"Empty": [], "Leads": [["Email", "Phone", "Since", "Active"],
                                         ["a@x.com", 4155550100.0, datetime(2026, 3, 4), True]],
                 "Other": [["Email"], ["o@x.com"]]})
    t = ip.read_table(raw, "book.xlsx", LIMITS)
    assert t.sheet_names == ["Empty", "Leads", "Other"] and t.sheet == "Leads"  # first sheet with data
    assert t.rows == [{"Email": "a@x.com", "Phone": "4155550100", "Since": "2026-03-04", "Active": "true"}]
    assert ip.read_table(raw, "book.xlsx", LIMITS, sheet="Other").rows == [{"Email": "o@x.com"}]
    with pytest.raises(FileRejected, match="Sheet 'Nope' was not found"):
        ip.read_table(raw, "book.xlsx", LIMITS, sheet="Nope")


def test_xlsx_not_a_zip_is_refused():
    with pytest.raises(FileRejected, match="not a valid .xlsx"):
        ip.read_table(b"PK-not-really", "book.xlsx", LIMITS)


def test_table_frame():
    t = ip.read_table(b"email,first_name\nj@x.com,Jos\n", "x.csv", LIMITS)
    assert ip.table_frame(t).iloc[0]["first_name"] == "Jos"
