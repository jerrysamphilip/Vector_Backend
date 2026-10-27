"""Import / upload guards (gaps #3, #24): column lengths, CSV decoding, XLSX limits, upload re-check."""
import io
import zipfile

import pytest
from sqlalchemy.exc import DataError

from app.models.account import Account
from app.models.prospect import Prospect
from app.routers import import_router
from app.services import import_limits
from app.services.prospect_upload_service import field_problems, is_sendable_address, recheck_accepted


# ── Column lengths from the model ──

def test_column_limits_follow_the_model():
    limits = import_limits.column_limits(Prospect)
    assert limits["designation"] == 150 and limits["first_name"] == 100 and limits["email"] == 255
    assert "tags" not in limits and "custom_fields" not in limits  # JSON has no length
    assert import_limits.column_limits(Account, ["name", "postal_code", "annual_revenue"]) == {
        "name": 255, "postal_code": 20}


def test_oversized_fields_names_the_field():
    limits = {"designation": 150, "first_name": 100}
    problems = import_limits.oversized_fields(
        {"designation": "x" * 151, "first_name": "Ann", "tags": ["a"] * 500}, limits, {"designation": "Job title"})
    assert problems == ["Job title is too long (151 characters, maximum 150)"]
    assert import_limits.oversized_fields({"designation": "x" * 150}, limits) == []


def test_import_row_with_long_job_title_is_a_row_error():
    with pytest.raises(import_router.RowError, match="Job title is too long"):
        import_router._check_lengths({"designation": "T" * 200}, import_router.CONTACT_LIMITS,
                                     import_router.CONTACT_TARGETS)
    import_router._check_lengths({"designation": "CTO"}, import_router.CONTACT_LIMITS, import_router.CONTACT_TARGETS)


def test_data_error_reason_names_the_column():
    exc = DataError("INSERT ...", {}, Exception(1406, "Data too long for column 'designation' at row 1"))
    assert import_router._data_error_reason(exc).startswith("Job title:")
    assert import_router._data_error_reason(DataError("x", {}, Exception("odd"))).startswith("A value")


# ── CSV decoding ──

def test_decode_csv_utf8_with_and_without_bom():
    assert import_limits.decode_csv("email,name\nj@x.com,José\n".encode("utf-8")).endswith("José\n")
    assert import_limits.decode_csv("﻿email\n".encode("utf-8")) == "email\n"


def test_decode_csv_falls_back_to_cp1252():
    raw = "email,first_name\njose@acme.com,José “Pepe”\n".encode("cp1252")
    with pytest.raises(UnicodeDecodeError):
        raw.decode("utf-8")
    assert "José “Pepe”" in import_limits.decode_csv(raw)


def test_decode_csv_refuses_undecodable_bytes():
    with pytest.raises(import_limits.FileRejected):
        import_limits.decode_csv(b"email\n\x81\x8d\x8f\x90\x9d\xff\xfe")


def test_parse_reads_cp1252_csv():
    df = import_router._parse("email,first_name\njose@acme.com,José\n".encode("cp1252"), "contacts.csv")
    assert df.iloc[0]["first_name"] == "José"


# ── XLSX guards ──

def _zip(entries):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buf.getvalue()


def test_xlsx_zip_bomb_is_refused_before_reading():
    raw = _zip({"xl/worksheets/sheet1.xml": b"0" * (3 * 1024 * 1024)})
    assert len(raw) < 100 * 1024  # small on disk
    with pytest.raises(import_limits.FileRejected, match="expands to"):
        import_limits.check_xlsx_archive(raw, max_uncompressed=2 * 1024 * 1024)
    import_limits.check_xlsx_archive(raw)  # under the real 250 MB limit


def test_xlsx_that_is_not_a_zip_is_refused():
    with pytest.raises(import_limits.FileRejected, match="not a valid .xlsx"):
        import_router._parse(b"not a zip", "contacts.xlsx")


def test_column_cap():
    import_limits.check_column_count(range(200))
    with pytest.raises(import_limits.FileRejected, match="201 columns"):
        import_limits.check_column_count(range(201))


# ── Upload re-check of ACCEPTED rows ──

def _row(**kw):
    base = {"row": 1, "email": "Ann@Acme.com", "first_name": "Ann", "last_name": "Lee", "company_name": "Acme",
            "status": "ACCEPTED"}
    return {**base, **kw}


def test_recheck_normalises_a_good_row():
    row, problem = recheck_accepted(_row())
    assert problem is None and row is not None
    assert row["email"] == "ann@acme.com" and row["email_type"] == "BUSINESS"


@pytest.mark.parametrize("change,reason", [
    ({"email": "not-an-email"}, "Invalid or personal email"),
    ({"email": "ann@gmail.com"}, "Invalid or personal email"),
    ({"email": None}, "Missing email address"),
    ({"first_name": " "}, "Missing required field: First Name"),
    ({"company_name": None}, "Missing required field: Company Name"),
    ({"designation": "D" * 151}, "Designation is too long (151 characters, maximum 150)"),
    ({"first_name": "Ann\r\nBcc: x@y.com"}, "First Name contains control characters"),
])
def test_recheck_rejects_tampered_rows(change, reason):
    row, problem = recheck_accepted(_row(**change))
    assert row is None and reason in problem


def test_sendable_address():
    assert is_sendable_address("a.b+c@acme.co.uk")
    assert not is_sendable_address("a b@acme.com")
    assert not is_sendable_address("josé@acme.com")
    assert not is_sendable_address("a@acme_corp.com")
    assert field_problems({"email": "ok@acme.com", "first_name": "Ann"}) == []
