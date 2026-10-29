"""g1b: at head the counter table is gone (0036_drop_metrics_realtime)."""
from tests.conftest import sql


def test_counter_table_is_absent_at_head(client):
    assert sql("SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = DATABASE() "
               "AND table_name = 'campaign_metrics_realtime'") == 0
