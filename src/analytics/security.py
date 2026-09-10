from dataclasses import dataclass
from functools import lru_cache
from typing import Annotated

import jwt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import PyJWKClient
from jwt.exceptions import PyJWTError

from analytics.config import load_settings
from analytics.security_authorization import (
    SecurityAuthorizationError,
    get_security_authorization_client,
)


@dataclass(frozen=True)
class Principal:
    subject: str


_bearer = HTTPBearer(auto_error=False)


@lru_cache
def _jwks_client() -> PyJWKClient:
    return PyJWKClient(
        load_settings().security_jwks_url,
        cache_keys=True,
        cache_jwk_set=True,
        lifespan=300,
        timeout=5.0,
    )


def get_principal(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> Principal:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(status_code=401, detail="Missing Security token")
    settings = load_settings()
    try:
        signing_key = _jwks_client().get_signing_key_from_jwt(credentials.credentials)
        claims = jwt.decode(
            credentials.credentials,
            signing_key.key,
            algorithms=[signing_key.algorithm_name],
            issuer=settings.security_issuer,
            audience=settings.security_audience,
            options={
                "require": ["exp", "iss", "aud", "sub", "iat", "jti", "actor_type"]
            },
        )
    except (PyJWTError, ValueError, TypeError) as exc:
        raise HTTPException(status_code=401, detail="Invalid Security token") from exc

    subject = claims.get("sub")
    if not isinstance(subject, str) or not subject.strip() or claims.get("actor_type") != "USER":
        raise HTTPException(status_code=401, detail="Invalid Security human token claims")

    forbidden_authority_claims = {
        "tenant_id",
        "permissions",
        "roles",
        "location_id",
        "act",
    }
    if forbidden_authority_claims.intersection(claims):
        raise HTTPException(
            status_code=401,
            detail="Security human token carries unsupported authority claims",
        )
    return Principal(subject=subject.strip())


def require_analytics_read(
    tenant_id: str,
    principal: Annotated[Principal, Depends(get_principal)],
) -> Principal:
    try:
        decision = get_security_authorization_client().check_user_permission(
            user_id=principal.subject,
            tenant_id=tenant_id,
            permission_key="audit.analytics.read",
        )
    except SecurityAuthorizationError as exc:
        raise HTTPException(status_code=503, detail="Security authorization unavailable") from exc
    if not decision.allowed:
        raise HTTPException(status_code=403, detail="Permission denied")
    return principal
