"""g1b: campaign_metrics_realtime is gone (0036): no model maps it, nothing imports a writer for it."""
import importlib.util
from pathlib import Path

import app.models  # noqa: F401  (registers every table)
from app.models import Base
from app.services.metrics_service import MetricsService

MIGRATION = Path(__file__).resolve().parents[2] / "alembic" / "versions" / "0036_drop_metrics_realtime.py"


def test_no_model_maps_the_counter_table():
    assert "campaign_metrics_realtime" not in Base.metadata.tables
    assert not any("campaign_metrics_realtime" in str(fk.target_fullname)
                   for t in Base.metadata.tables.values() for fk in t.foreign_keys)


def test_metrics_service_only_reads():
    assert [n for n in vars(MetricsService) if not n.startswith("_")] == ["get_metrics_dict"]


def test_migration_follows_0035_and_recreates_the_previous_ddl():
    spec = importlib.util.spec_from_file_location("m0036", MIGRATION)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert (mod.revision, mod.down_revision) == ("0036_drop_metrics_realtime", "0035_schema_parity")
    ddl = " ".join(mod.CREATE_TABLE.split())
    assert ddl.startswith("CREATE TABLE campaign_metrics_realtime ( campaign_id varchar(36) NOT NULL,")
    assert "updated_at datetime(6) DEFAULT CURRENT_TIMESTAMP(6)" in ddl
    assert "FOREIGN KEY (campaign_id) REFERENCES campaigns (campaign_id)" in ddl
    assert len(mod.revision) <= 32  # alembic_version.version_num is VARCHAR(32)
