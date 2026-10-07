# app/db/test_unsubscribe_metrics.py
"""
Read-only check: confirms unsubscribed_count now flows through
CampaignAnalyticsService for real campaigns in the database.

Does not write anything. Safe to run against any environment.
"""

from app.core.database import SessionLocal
from app.models.campaign import Campaign
from app.services.campaign_analytics_service import CampaignAnalyticsService


def test_unsubscribed_count_present():
    db = SessionLocal()
    try:
        analytics = CampaignAnalyticsService(db)

        campaigns = db.query(Campaign).limit(5).all()
        if not campaigns:
            print("No campaigns found in this database — nothing to check.")
            return

        print(f"{'Campaign':40} {'Sent':>6} {'Opened':>7} {'Bounced':>8} {'Unsub':>6}")
        print("-" * 72)

        ids = [c.campaign_id for c in campaigns]
        bulk = analytics.get_bulk_metrics(ids)

        for c in campaigns:
            m = bulk.get(c.campaign_id, {})
            assert "unsubscribed_count" in m, "unsubscribed_count missing from get_bulk_metrics()"
            name = (c.campaign_name or c.campaign_id)[:40]
            print(f"{name:40} {m.get('sent_count', 0):>6} {m.get('opened_count', 0):>7} "
                  f"{m.get('bounced_count', 0):>8} {m.get('unsubscribed_count', 0):>6}")

            single = analytics.get_dynamic_metrics(c.campaign_id)
            assert "unsubscribed_count" in single, "unsubscribed_count missing from get_dynamic_metrics()"
            assert single["unsubscribed_count"] == m["unsubscribed_count"], (
                "get_dynamic_metrics() and get_bulk_metrics() disagree on unsubscribed_count"
            )

        print("\nPASS — unsubscribed_count is present and consistent across both metrics paths.")
    finally:
        db.close()


if __name__ == "__main__":
    test_unsubscribed_count_present()
