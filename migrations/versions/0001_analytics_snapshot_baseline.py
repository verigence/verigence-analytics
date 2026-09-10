"""Analytics controlled snapshot baseline.

Revision ID: 0001_analytics_snapshot
Revises:
"""
from alembic import op

revision = "0001_analytics_snapshot"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS analytics")
    op.execute("""
        CREATE TABLE IF NOT EXISTS analytics.dump_runs (
            dump_id uuid PRIMARY KEY,
            tenant_id varchar(128) NOT NULL,
            trigger_kind varchar(32) NOT NULL,
            requested_by varchar(255) NOT NULL,
            status varchar(32) NOT NULL CHECK (status IN ('RUNNING','COMPLETED','FAILED')),
            started_at_utc timestamptz NOT NULL,
            completed_at_utc timestamptz NULL,
            data_as_of_utc timestamptz NULL,
            row_count bigint NOT NULL DEFAULT 0,
            table_counts jsonb NOT NULL DEFAULT '{}'::jsonb,
            error_message text NULL
        )
        """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS ix_analytics_dump_runs_tenant_completed
        ON analytics.dump_runs (tenant_id, completed_at_utc DESC)
        WHERE status='COMPLETED'
        """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS analytics.snapshot_rows (
            dump_id uuid NOT NULL REFERENCES analytics.dump_runs(dump_id) ON DELETE CASCADE,
            tenant_id varchar(128) NOT NULL,
            source_table varchar(128) NOT NULL,
            source_pk varchar(255) NOT NULL,
            row_data jsonb NOT NULL,
            PRIMARY KEY (dump_id, source_table, source_pk)
        )
        """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS ix_analytics_snapshot_lookup
        ON analytics.snapshot_rows (tenant_id, dump_id, source_table)
        """)
    op.execute("""
        COMMENT ON SCHEMA analytics IS
        'Independent Verigence reporting schema. Operational Audit Core remains authoritative.'
        """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS analytics.snapshot_rows")
    op.execute("DROP TABLE IF EXISTS analytics.dump_runs")
    op.execute("DROP SCHEMA IF EXISTS analytics")
