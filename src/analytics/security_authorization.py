from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from functools import lru_cache

import httpx

from analytics.config import load_settings

_SERVICE_TOKEN_REUSE_SECONDS = 300.0
_ALLOW_REUSE_SECONDS = 60.0


class SecurityAuthorizationError(RuntimeError):
    """Security could not return a trustworthy authorization decision."""


@dataclass(frozen=True)
class SecurityAuthorizationDecision:
    allowed: bool
    reason_code: str
    user_id: str
    tenant_id: str
    permission_key: str
    role_key: str | None


class SecurityAuthorizationClient:
    def __init__(
        self,
        *,
        base_url: str,
        client_id: str,
        client_secret: str,
        timeout_seconds: float = 5.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not base_url.strip() or not client_id.strip() or not client_secret:
            raise ValueError("Security authorization client configuration is required")
        normalized = base_url.rstrip("/")
        self._token_client = httpx.Client(
            base_url=normalized,
            auth=(client_id, client_secret),
            timeout=timeout_seconds,
            transport=transport,
        )
        self._client = httpx.Client(
            base_url=normalized,
            timeout=timeout_seconds,
            transport=transport,
        )
        self._service_token: str | None = None
        self._service_token_reuse_until = 0.0
        self._service_token_lock = threading.Lock()
        self._allow_cache: dict[
            tuple[str, str, str], tuple[float, SecurityAuthorizationDecision]
        ] = {}
        self._allow_cache_lock = threading.Lock()

    def close(self) -> None:
        self._token_client.close()
        self._client.close()

    def _service_token_for_security(self) -> str:
        now = time.monotonic()
        if self._service_token and now < self._service_token_reuse_until:
            return self._service_token
        with self._service_token_lock:
            now = time.monotonic()
            if self._service_token and now < self._service_token_reuse_until:
                return self._service_token
            try:
                response = self._token_client.post(
                    "/security/v1/service/token",
                    data={"audience": "security"},
                )
            except httpx.HTTPError as exc:
                raise SecurityAuthorizationError(
                    "Security service-token endpoint is unavailable"
                ) from exc
            if response.status_code != 200:
                raise SecurityAuthorizationError(
                    f"Security service-token request failed with HTTP {response.status_code}"
                )
            try:
                payload = response.json()
            except ValueError as exc:
                raise SecurityAuthorizationError(
                    "Security service-token response is not valid JSON"
                ) from exc
            if not isinstance(payload, dict):
                raise SecurityAuthorizationError(
                    "Security service-token response has invalid shape"
                )
            token = payload.get("accessToken")
            if (
                not isinstance(token, str)
                or not token
                or payload.get("tokenType") != "Bearer"
                or payload.get("audience") != "security"
            ):
                raise SecurityAuthorizationError(
                    "Security service-token response has invalid shape"
                )
            expires_in = payload.get("expiresIn")
            reuse_seconds = _SERVICE_TOKEN_REUSE_SECONDS
            if isinstance(expires_in, int) and not isinstance(expires_in, bool) and expires_in > 0:
                reuse_seconds = max(1.0, min(float(expires_in) * 0.9, float(expires_in) - 1.0))
            self._service_token = token
            self._service_token_reuse_until = now + reuse_seconds
            return token

    def check_user_permission(
        self,
        *,
        user_id: str,
        tenant_id: str,
        permission_key: str,
    ) -> SecurityAuthorizationDecision:
        if not user_id or not tenant_id or not permission_key:
            raise ValueError("user_id, tenant_id and permission_key are required")
        key = (user_id, tenant_id, permission_key)
        now = time.monotonic()
        with self._allow_cache_lock:
            cached = self._allow_cache.get(key)
            if cached and cached[0] > now:
                return cached[1]
            if cached:
                self._allow_cache.pop(key, None)

        token = self._service_token_for_security()
        try:
            response = self._client.post(
                "/security/v1/authorization/check",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "userId": user_id,
                    "tenantId": tenant_id,
                    "permissionKey": permission_key,
                },
            )
        except httpx.HTTPError as exc:
            raise SecurityAuthorizationError(
                "Security authorization endpoint is unavailable"
            ) from exc
        if response.status_code != 200:
            raise SecurityAuthorizationError(
                f"Security authorization request failed with HTTP {response.status_code}"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise SecurityAuthorizationError(
                "Security authorization response is not valid JSON"
            ) from exc
        if not isinstance(payload, dict):
            raise SecurityAuthorizationError(
                "Security authorization response has invalid shape"
            )

        allowed = payload.get("allowed")
        reason_code = payload.get("reasonCode")
        role_key = payload.get("roleKey")
        if (
            not isinstance(allowed, bool)
            or not isinstance(reason_code, str)
            or not reason_code
            or payload.get("userId") != user_id
            or payload.get("tenantId") != tenant_id
            or payload.get("permissionKey") != permission_key
            or (role_key is not None and not isinstance(role_key, str))
        ):
            raise SecurityAuthorizationError(
                "Security authorization response does not match the requested decision"
            )

        decision = SecurityAuthorizationDecision(
            allowed=allowed,
            reason_code=reason_code,
            user_id=user_id,
            tenant_id=tenant_id,
            permission_key=permission_key,
            role_key=role_key,
        )
        if allowed:
            with self._allow_cache_lock:
                self._allow_cache[key] = (time.monotonic() + _ALLOW_REUSE_SECONDS, decision)
        return decision


@lru_cache
def get_security_authorization_client() -> SecurityAuthorizationClient:
    settings = load_settings()
    return SecurityAuthorizationClient(
        base_url=settings.security_base_url,
        client_id=settings.security_client_id,
        client_secret=settings.security_client_secret,
    )
