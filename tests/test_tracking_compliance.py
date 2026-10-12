"""Unsubscribe (GET is read-only, POST / RFC 8058 one-click unsubscribes) and click tracking."""

from urllib.parse import quote

from tests.conftest import sql


def _suppressed(tenant_id, email):
    return sql("SELECT COUNT(*) FROM global_unsubscribes WHERE tenant_id = :t AND LOWER(email) = LOWER(:e)",
               t=tenant_id, e=email)


def test_get_unsubscribe_does_not_change_anything(client, tenant_a, tenant_a_data):
    msg = tenant_a_data["message_id"]
    before_total = sql("SELECT COUNT(*) FROM global_unsubscribes")
    before_events = sql("SELECT COUNT(*) FROM email_events WHERE message_id = :m", m=msg)
    r = client.get(f"/tracking/unsubscribe/{msg}")
    assert r.status_code == 200
    assert "<form" in r.text.lower() and 'method="post"' in r.text.lower()
    assert sql("SELECT COUNT(*) FROM global_unsubscribes") == before_total
    assert sql("SELECT COUNT(*) FROM email_events WHERE message_id = :m", m=msg) == before_events


def test_unknown_message_unsubscribe_404(client):
    assert client.get("/tracking/unsubscribe/not-a-message").status_code == 404
    assert client.post("/tracking/unsubscribe/not-a-message", data={"confirm": "1"}).status_code == 404


def test_one_click_post_unsubscribes(client, tenant_a, tenant_a_data):
    msg, email = tenant_a_data["message_id"], tenant_a_data["prospect_email"]
    r = client.post(f"/tracking/unsubscribe/{msg}", data={"List-Unsubscribe": "One-Click"})
    assert r.status_code == 200, r.text
    assert _suppressed(tenant_a["tenant_id"], email) >= 1


def test_click_unknown_url_shows_interstitial(client, tenant_a_data):
    msg = tenant_a_data["message_id"]
    r = client.get(f"/tracking/click/{msg}?url=" + quote("https://evil.example.com/phish", safe=""))
    assert r.status_code == 200
    assert "evil.example.com" in r.text
    assert "location" not in {k.lower() for k in r.headers}


def test_click_link_from_email_redirects(client, tenant_a_data):
    msg, link = tenant_a_data["message_id"], tenant_a_data["known_link"]
    r = client.get(f"/tracking/click/{msg}?url=" + quote(link, safe=""))
    assert r.status_code == 302
    assert r.headers["location"] == link


def test_click_javascript_url_rejected(client, tenant_a_data):
    msg = tenant_a_data["message_id"]
    r = client.get(f"/tracking/click/{msg}?url=" + quote("javascript:alert(1)", safe=""))
    assert r.status_code == 400
