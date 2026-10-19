"""Downloads with non-Latin file names (W-12), in-process API tests against MySQL.

The quote PDF is named after the proposal title; a Japanese or accented title used to break
the hand-built Content-Disposition header (Starlette encodes headers as latin-1 -> 500).
"""
import re
from urllib.parse import unquote

import pytest

from tests.conftest import RUN, register_tenant


@pytest.fixture(scope="module")
def ws(client):
    return register_tenant(client, "dlname")


def _proposal(client, h, title: str) -> str:
    stage = next(s for s in client.get("/sales/stages", headers=h).json() if s["status"] == "OPEN")
    r = client.post("/opportunities", headers=h, json={"name": f"Deal {RUN}", "stage_id": stage["stage_id"]})
    assert r.status_code == 201, r.text
    r = client.post("/proposals", headers=h, json={"opportunity_id": r.json()["opportunity_id"], "title": title})
    assert r.status_code == 201, r.text
    return r.json()["proposal_id"]


@pytest.mark.parametrize("title,fallback", [
    ("見積書 東京", "download.pdf"),
    ("Café Offre", "Cafe Offre.pdf"),
])
def test_quote_pdf_with_non_latin_title(client, ws, title, fallback):
    h = ws["headers"]
    pid = _proposal(client, h, title)
    r = client.get(f"/proposals/{pid}/pdf", headers=h)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "application/pdf"
    assert r.content.startswith(b"%PDF")
    cd = r.headers["content-disposition"]
    assert cd.startswith("attachment; ")
    assert re.search(r'filename="([^"]*)"', cd).group(1) == fallback
    star = re.search(r"filename\*=UTF-8''(\S+)", cd).group(1)
    assert unquote(star) == f"{title}.pdf"


def test_contacts_export_has_both_filename_params(client, ws):
    r = client.get("/contacts/export", headers=ws["headers"], params={"format": "csv"})
    if r.status_code == 404:
        pytest.skip("contacts export route not mounted here")
    assert r.status_code == 200, r.text
    cd = r.headers["content-disposition"]
    assert 'filename="' in cd and "filename*=UTF-8''" in cd
