"""CSV / XLSX formula guard on every backend export (TEAM_GAP_LIST P0 #10, backend side).

Audit (2026-10-28): the contacts export (also used by list export), the company export, the import error report
and the reports CSVs (incl. the mailbox / deliverability health report) all pass data cells through
app/services/crm.csv_safe_row. This test keeps it that way: any module of app/ that builds a CSV writer or an
openpyxl workbook must use csv_safe, and the builders really neutralise formula cells."""
import csv
import io
import re
from pathlib import Path
from types import SimpleNamespace

from app.services import crm
from app.services.import_service import errors_csv
from app.services.reports_service import _csv, _Fixed

APP = Path(__file__).resolve().parents[2] / "app"
BUILDERS = re.compile(r"csv\.writer\(|csv\.DictWriter\(|Workbook\(\)|\.to_csv\(|\.to_excel\(")
EVIL = ["=HYPERLINK(\"http://x\")", "+1+1", "-2+3", "@SUM(A1)", "\tcmd", "\rcmd"]


def test_tbe_every_export_builder_uses_the_formula_guard():
    builders = sorted(str(p.relative_to(APP)) for p in APP.rglob("*.py") if BUILDERS.search(p.read_text("utf-8")))
    assert builders, "the export builders moved: update this audit"
    unguarded = [p for p in builders if "csv_safe" not in (APP / p).read_text("utf-8")]
    assert unguarded == [], f"CSV / XLSX built without crm.csv_safe_row: {unguarded}"


def test_tbe_csv_safe_neutralises_formula_cells_only():
    assert crm.csv_safe_row(EVIL) == ["'" + v for v in EVIL]
    assert crm.csv_safe_row(["Acme", "a=b", -5, None, 3.5]) == ["Acme", "a=b", -5, None, 3.5]


def test_tbe_import_error_report_is_guarded():
    job = SimpleNamespace(file_name="leads.csv", errors=[
        {"row": 2, "reason": "=bad", "values": {"Email": "=cmd|' /C calc'!A0", "Name": "Ann"}}])
    name, data = errors_csv(job)
    rows = list(csv.reader(io.StringIO(data.decode("utf-8-sig"))))
    assert name == "leads-errors.csv" and rows[0] == ["Row", "Error", "Email", "Name"]
    assert rows[1] == ["2", "'=bad", "'=cmd|' /C calc'!A0", "Ann"]


def test_tbe_report_csv_guards_data_rows_but_not_its_own_headers():
    rows = list(csv.reader(_csv([_Fixed(["=== SUMMARY ==="]), ["=Campaign", "@mailbox", 3]])))
    assert rows == [["=== SUMMARY ==="], ["'=Campaign", "'@mailbox", "3"]]
