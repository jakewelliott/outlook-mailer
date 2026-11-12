"""Cloudflare Access JWT verification for admin requests.

Cloudflare Access adds a signed JWT to every request it lets through, in the
``Cf-Access-Jwt-Assertion`` header (cloudflared forwards it unchanged). The app checks it so
that someone reaching the container directly (e.g. on the LAN port) cannot use the admin pages.

Only the header is read, never the ``CF_Authorization`` cookie: Access always sets the header on
requests it has authorised, so a cookie fallback would only widen what the app accepts.
"""

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx
import jwt

log = logging.getLogger("outlook_mailer.auth")

JWT_HEADER = "cf-access-jwt-assertion"
ALGORITHM = "RS256"
LEEWAY_SECONDS = 30
FETCH_TIMEOUT_SECONDS = 5.0
# Keys are refreshed when older than this, so rotated keys are picked up without a restart.
KEYS_MAX_AGE_SECONDS = 3600.0
# With keys cached, a refetch (unknown ``kid``, or stale keys) is tried at most this often after
# the last attempt, so forged tokens or a Cloudflare outage can't make us hammer the endpoint.
MIN_REFETCH_INTERVAL_SECONDS = 30.0


class KeysUnavailable(Exception):
    """The signing keys could not be fetched and none are cached."""


class AccessDenied(Exception):
    """The token is missing or invalid. The reason is for logs only, never for the response."""


@dataclass(frozen=True)
class Identity:
    email: str


class AccessKeySet:
    """Caches the team's public signing keys, fetched with async httpx.

    ``_fetched_at`` is when the keys were last fetched successfully (their age);
    ``_attempted_at`` is when a fetch was last tried, successful or not. With keys cached, a
    refetch (keys stale, or a token names an unknown ``kid``) is tried at most every
    ``MIN_REFETCH_INTERVAL_SECONDS`` after the last attempt, so neither forged tokens nor an
    outage at Cloudflare make us hammer the certs endpoint; the cached keys are used meanwhile.
    With nothing cached every request tries (and fails with ``KeysUnavailable``, a 503).
    """

    def __init__(self, certs_url: str, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.certs_url = certs_url
        self._transport = transport
        self._keys: dict[str, jwt.PyJWK] = {}
        self._fetched_at: float | None = None
        self._attempted_at: float | None = None
        self._lock = asyncio.Lock()

    @staticmethod
    def _since(stamp: float | None) -> float:
        return float("inf") if stamp is None else time.monotonic() - stamp

    def _age(self) -> float:
        return self._since(self._fetched_at)

    def _should_refresh(self, kid: str) -> bool:
        if not self._keys:
            return True
        wanted = self._age() > KEYS_MAX_AGE_SECONDS or kid not in self._keys
        return wanted and self._since(self._attempted_at) > MIN_REFETCH_INTERVAL_SECONDS

    async def get(self, kid: str) -> jwt.PyJWK | None:
        """Return the key for ``kid``, or None if Cloudflare does not publish it.

        Raises ``KeysUnavailable`` if no keys are cached and they can't be fetched.
        """
        stale = self._age() > KEYS_MAX_AGE_SECONDS
        if kid in self._keys and not stale:
            return self._keys[kid]
        async with self._lock:
            # Another request may have refreshed the keys while we waited.
            if self._should_refresh(kid):
                await self._refresh()
        return self._keys.get(kid)

    async def _refresh(self) -> None:
        self._attempted_at = time.monotonic()
        try:
            async with httpx.AsyncClient(
                timeout=FETCH_TIMEOUT_SECONDS, transport=self._transport
            ) as client:
                response = await client.get(self.certs_url)
                response.raise_for_status()
                keys = _parse_jwks(response.json())
        except (httpx.HTTPError, ValueError) as exc:
            if self._keys:
                log.warning("Could not refresh Cloudflare Access keys, using cached ones: %s", exc)
                return
            log.error("Could not fetch Cloudflare Access keys from %s: %s", self.certs_url, exc)
            raise KeysUnavailable from exc
        self._keys = keys
        self._fetched_at = time.monotonic()


def _parse_jwks(data: Any) -> dict[str, jwt.PyJWK]:
    if not isinstance(data, dict) or not isinstance(data.get("keys"), list):
        raise ValueError("certs response has no 'keys' list")
    keys: dict[str, jwt.PyJWK] = {}
    for item in data["keys"]:
        if isinstance(item, dict) and item.get("kty") == "RSA" and isinstance(item.get("kid"), str):
            try:
                keys[item["kid"]] = jwt.PyJWK(item, algorithm=ALGORITHM)
            except jwt.PyJWTError:
                continue
    if not keys:
        raise ValueError("certs response has no usable RSA keys")
    return keys


class AccessVerifier:
    def __init__(
        self, team_domain: str, audience: str, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self.issuer = f"https://{team_domain}"
        self.audience = audience
        self.keys = AccessKeySet(f"{self.issuer}/cdn-cgi/access/certs", transport)

    async def verify(self, token: str | None) -> Identity:
        """Return the identity in a valid token.

        Raises ``AccessDenied`` for a missing or invalid token, ``KeysUnavailable`` if the keys
        can't be fetched.
        """
        if not token:
            raise AccessDenied("no Cf-Access-Jwt-Assertion header")
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as exc:
            raise AccessDenied(f"malformed token: {exc}") from None
        kid = header.get("kid")
        if header.get("alg") != ALGORITHM or not isinstance(kid, str):
            raise AccessDenied("token is not RS256 or has no kid")
        key = await self.keys.get(kid)
        if key is None:
            raise AccessDenied("token signed with an unknown key")
        try:
            claims = jwt.decode(
                token,
                key.key,
                algorithms=[ALGORITHM],
                audience=self.audience,
                issuer=self.issuer,
                leeway=LEEWAY_SECONDS,
                options={"require": ["exp", "iat", "iss", "aud"]},
            )
        except jwt.PyJWTError as exc:
            raise AccessDenied(f"invalid token: {exc}") from None
        # Service tokens carry common_name instead of email.
        who = claims.get("email") or claims.get("common_name")
        if not isinstance(who, str) or not who:
            raise AccessDenied("token has no email")
        return Identity(email=who)
