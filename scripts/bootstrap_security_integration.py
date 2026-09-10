from __future__ import annotations

import os
from uuid import uuid4

from argon2 import PasswordHasher
from sqlalchemy import create_engine, text


def _database_url() -> str:
    url = os.environ["ANALYTICS_DB_URL"]
    for prefix, replacement in (
        ("postgresql+asyncpg://", "postgresql+psycopg://"),
        ("postgresql://", "postgresql+psycopg://"),
        ("postgres://", "postgresql+psycopg://"),
    ):
        if url.startswith(prefix):
            return replacement + url[len(prefix) :]
    return url


def main() -> None:
    secret = os.environ.get("ANALYTICS_SECRET_KEY", "")
    if not secret:
        raise SystemExit("ANALYTICS_SECRET_KEY is required")

    engine = create_engine(_database_url())
    secret_hash = PasswordHasher().hash(secret)

    with engine.begin() as connection:
        created_by = connection.execute(
            text(
                """
                SELECT user_id
                FROM security.platform_user_role_assignments
                WHERE role_key='platform.super_admin' AND status='ACTIVE'
                ORDER BY user_id
                LIMIT 1
                """
            )
        ).scalar_one_or_none()
        if created_by is None:
            raise SystemExit("No active platform.super_admin is available for credential audit")

        existing = connection.execute(
            text(
                """
                SELECT p.principal_id, p.actor_type
                FROM security.service_integrations si
                JOIN security.security_principals p ON p.principal_id=si.principal_id
                WHERE si.integration_key='analytics'
                """
            )
        ).mappings().first()

        if existing is None:
            principal_id = uuid4()
            connection.execute(
                text(
                    """
                    INSERT INTO security.security_principals
                        (principal_id,actor_type,principal_name,status,created_at_utc,updated_at_utc)
                    VALUES
                        (:principal_id,'SERVICE_INTEGRATION','analytics','ACTIVE',now(),now())
                    """
                ),
                {"principal_id": principal_id},
            )
            connection.execute(
                text(
                    """
                    INSERT INTO security.service_integrations
                        (principal_id,integration_key,description,created_at_utc)
                    VALUES
                        (:principal_id,'analytics','Verigence Analytics service',now())
                    """
                ),
                {"principal_id": principal_id},
            )
        else:
            if existing["actor_type"] != "SERVICE_INTEGRATION":
                raise SystemExit("Existing analytics integration is not a SERVICE_INTEGRATION")
            principal_id = existing["principal_id"]
            connection.execute(
                text(
                    """
                    UPDATE security.security_principals
                    SET principal_name='analytics', status='ACTIVE', updated_at_utc=now()
                    WHERE principal_id=:principal_id
                    """
                ),
                {"principal_id": principal_id},
            )

        credential = connection.execute(
            text(
                """
                SELECT credential_id, principal_id
                FROM security.principal_credentials
                WHERE client_id='analytics'
                """
            )
        ).mappings().first()
        if credential is not None and credential["principal_id"] != principal_id:
            raise SystemExit("client_id analytics is already bound to another principal")

        if credential is None:
            connection.execute(
                text(
                    """
                    INSERT INTO security.principal_credentials
                        (credential_id,principal_id,client_id,secret_hash,status,valid_from_utc,
                         valid_to_utc,created_by_user_id,created_at_utc,last_used_at_utc)
                    VALUES
                        (:credential_id,:principal_id,'analytics',:secret_hash,'ACTIVE',now(),
                         NULL,:created_by,now(),NULL)
                    """
                ),
                {
                    "credential_id": uuid4(),
                    "principal_id": principal_id,
                    "secret_hash": secret_hash,
                    "created_by": created_by,
                },
            )
        else:
            connection.execute(
                text(
                    """
                    UPDATE security.principal_credentials
                    SET secret_hash=:secret_hash, status='ACTIVE', valid_from_utc=now(),
                        valid_to_utc=NULL, created_by_user_id=:created_by
                    WHERE credential_id=:credential_id
                    """
                ),
                {
                    "secret_hash": secret_hash,
                    "created_by": created_by,
                    "credential_id": credential["credential_id"],
                },
            )

    print("ANALYTICS_SECURITY_SERVICE_IDENTITY=PASS|integration=analytics|client_id=analytics")


if __name__ == "__main__":
    main()
