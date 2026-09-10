from dataclasses import dataclass
from functools import lru_cache
from typing import Annotated

import jwt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import PyJWKClient
from jwt.exceptions import PyJWTError

from analytics.config import load_settings


@dataclass(frozen=True)
class Principal:
    subject: str
    tenant_id: str
    permissions: tuple[str, ...]


_bearer = HTTPBearer(auto_error=False)


@lru_cache
def _jwks_client() -> PyJWKClient:
    return PyJWKClient(load_settings().security_jwks_url)


def get_principal(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> Principal:
    if credentials is None:
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
            options={"require": ["exp", "iss", "aud", "sub", "tenant_id", "permissions"]},
        )
    except (PyJWTError, ValueError, TypeError) as exc:
        raise HTTPException(status_code=401, detail="Invalid Security token") from exc

    subject = claims.get("sub")
    tenant_id = claims.get("tenant_id")
    permissions = claims.get("permissions")
    if not isinstance(subject, str) or not isinstance(tenant_id, str) or not isinstance(permissions, list):
        raise HTTPException(status_code=401, detail="Invalid Security token claims")
    return Principal(subject, tenant_id, tuple(str(item) for item in permissions))


def require_analytics_read(
    tenant_id: str,
    principal: Annotated[Principal, Depends(get_principal)],
) -> Principal:
    if principal.tenant_id != tenant_id:
        raise HTTPException(status_code=403, detail="Tenant mismatch")
    if "audit.analytics.read" not in principal.permissions:
        raise HTTPException(status_code=403, detail="Permission denied")
    return principal
