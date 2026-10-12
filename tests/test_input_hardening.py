"""Bad input is a 4xx, never a 500: oversize values, NUL characters, out-of-range numbers."""


def test_oversize_value_is_422_not_500(client, tenant_a):
    r = client.post("/api/company-profiles", headers=tenant_a["headers"],
                    json={"profile_name": "A" * 5000, "company_name": "Co", "description": "x"})
    assert r.status_code in (422, 409), r.text


def test_nul_character_rejected(client, tenant_a):
    r = client.post("/api/company-profiles", headers=tenant_a["headers"],
                    json={"profile_name": "bad\u0000name", "company_name": "Co"})
    assert r.status_code == 422
    assert r.json()["code"] == "VALIDATION_ERROR"


def test_negative_limit_rejected(client, tenant_a):
    r = client.get("/deliverability/integrations/events?provider=x&domain_name=y&limit=-1",
                   headers=tenant_a["headers"])
    assert r.status_code == 422


def test_forecast_fiscal_year_bounded(client, tenant_a):
    r = client.get("/sales-reports/forecast?fy=-1", headers=tenant_a["headers"])
    assert r.status_code == 422


def test_campaign_lists_endpoint_works(client, tenant_a):
    made = client.post("/lists", headers=tenant_a["headers"], json={"list_name": "Wizard picker list"})
    assert made.status_code == 201, made.text
    r = client.get("/campaigns/lists", headers=tenant_a["headers"])
    assert r.status_code == 200, r.text
    rows = {x["list_name"]: x for x in r.json()}
    assert rows["Wizard picker list"]["prospect_count"] == 0
