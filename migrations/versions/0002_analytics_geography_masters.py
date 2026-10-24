"""Geography and outlet masters owned by the Analytics schema.

Revision ID: 0002_analytics_geography_masters
Revises: 0001_analytics_snapshot
"""
from alembic import op

revision = "0002_analytics_geography_masters"
down_revision = "0001_analytics_snapshot"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS analytics.geo_pin_master (
            pincode varchar(6) PRIMARY KEY,
            state_name text NOT NULL,
            district_name text,
            locality_names jsonb NOT NULL DEFAULT '[]'::jsonb,
            source_name text NOT NULL,
            source_url text NOT NULL,
            refreshed_at timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS analytics.geo_boundary_master (
            boundary_key text PRIMARY KEY,
            state_name text NOT NULL,
            boundary_type text NOT NULL CHECK (boundary_type IN ('PIN','DISTRICT')),
            content_gzip bytea NOT NULL,
            source_name text NOT NULL,
            source_url text NOT NULL,
            refreshed_at timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS analytics.outlet_location_master (
            tenant_id varchar(128) NOT NULL,
            outlet_id varchar(64) NOT NULL,
            dealer_id varchar(64),
            dealer_name text,
            outlet_name text NOT NULL,
            outlet_code text,
            pincode varchar(6),
            latitude numeric,
            longitude numeric,
            location_precision text NOT NULL,
            source_dump_id uuid,
            refreshed_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (tenant_id,outlet_id)
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS ix_analytics_geo_pin_district ON analytics.geo_pin_master (district_name,pincode)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_analytics_outlet_location_tenant_dealer ON analytics.outlet_location_master (tenant_id,dealer_id)")
    op.execute("COMMENT ON TABLE analytics.geo_pin_master IS 'Postal locality and district lookup owned by Analytics; sourced from the India postal PIN directory mirror.'")
    op.execute("COMMENT ON TABLE analytics.geo_boundary_master IS 'Compressed Odisha PIN and district GeoJSON used only by Analytics map reports.'")
    op.execute("COMMENT ON TABLE analytics.outlet_location_master IS 'Analytics copy of outlet map fields from Analytics snapshots; Audit Core remains read-only and authoritative.'")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS analytics.outlet_location_master")
    op.execute("DROP TABLE IF EXISTS analytics.geo_boundary_master")
    op.execute("DROP TABLE IF EXISTS analytics.geo_pin_master")
