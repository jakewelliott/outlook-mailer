"""Settings loaded from environment variables (see the table in docs/PLAN.md).

``load_settings`` checks every variable and raises ``ConfigError`` listing each problem by
variable name. Secret values never appear in error messages or in ``repr(Settings)``.
"""

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from cryptography.fernet import Fernet

MAIL_TRANSPORTS = ("graph", "mock")
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
MIN_UNSUBSCRIBE_SECRET_LEN = 32


class ConfigError(Exception):
    """One or more environment variables are missing or invalid."""

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        lines = "\n".join(f"  - {e}" for e in errors)
        super().__init__(f"Invalid configuration:\n{lines}")


@dataclass(frozen=True)
class Settings:
    public_base_url: str
    unsubscribe_secret: str = field(repr=False)
    ms_client_id: str | None = None
    ms_authority: str = "https://login.microsoftonline.com/consumers"
    cf_access_team_domain: str | None = None
    cf_access_aud: str | None = None
    auth_disabled: bool = False
    mail_transport: str = "graph"
    send_rate_per_minute: int = 6
    daily_send_cap: int = 250
    max_recipients: int = 2000
    max_upload_mb: int = 5
    token_cache_key: str | None = field(default=None, repr=False)
    data_dir: str = "/data"
    port: int = 8765
    tz: str = "America/New_York"
    log_level: str = "INFO"


def _get(env: Mapping[str, str], name: str) -> str | None:
    """Return the stripped value, or None if unset or blank."""
    value = env.get(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def _http_url_error(value: str) -> str | None:
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return "must be an absolute http(s) URL, e.g. https://mail.example.com"
    if parts.query or parts.fragment:
        return "must not contain a query string or fragment"
    return None


def _positive_int(
    env: Mapping[str, str], name: str, default: int, errors: list[str], maximum: int | None = None
) -> int:
    raw = _get(env, name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        errors.append(f"{name} must be a positive whole number, got {raw!r}")
        return default
    if value <= 0:
        errors.append(f"{name} must be a positive whole number, got {raw!r}")
    elif maximum is not None and value > maximum:
        errors.append(f"{name} must be at most {maximum}, got {raw!r}")
    return value


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    """Build ``Settings`` from ``env`` (default: ``os.environ``). Raises ``ConfigError``."""
    if env is None:
        env = os.environ
    errors: list[str] = []

    public_base_url = _get(env, "PUBLIC_BASE_URL")
    if public_base_url is None:
        errors.append("PUBLIC_BASE_URL is required (e.g. https://mail.example.com)")
    else:
        public_base_url = public_base_url.removesuffix("/")
        if problem := _http_url_error(public_base_url):
            errors.append(f"PUBLIC_BASE_URL {problem}, got {public_base_url!r}")
        elif public_base_url.endswith("/"):
            errors.append("PUBLIC_BASE_URL must not end with a slash")

    unsubscribe_secret = _get(env, "UNSUBSCRIBE_SECRET")
    if unsubscribe_secret is None:
        errors.append("UNSUBSCRIBE_SECRET is required (a random string of 32+ characters)")
    elif len(unsubscribe_secret) < MIN_UNSUBSCRIBE_SECRET_LEN:
        errors.append(
            f"UNSUBSCRIBE_SECRET must be at least {MIN_UNSUBSCRIBE_SECRET_LEN} characters "
            f"(it has {len(unsubscribe_secret)})"
        )

    auth_disabled_raw = _get(env, "AUTH_DISABLED") or "0"
    if auth_disabled_raw not in ("0", "1"):
        errors.append(f"AUTH_DISABLED must be 0 or 1, got {auth_disabled_raw!r}")
    auth_disabled = auth_disabled_raw == "1"

    cf_access_team_domain = _get(env, "CF_ACCESS_TEAM_DOMAIN")
    cf_access_aud = _get(env, "CF_ACCESS_AUD")
    if not auth_disabled:
        if cf_access_team_domain is None:
            errors.append(
                "CF_ACCESS_TEAM_DOMAIN is required unless AUTH_DISABLED=1 "
                "(e.g. yourteam.cloudflareaccess.com)"
            )
        if cf_access_aud is None:
            errors.append("CF_ACCESS_AUD is required unless AUTH_DISABLED=1")
    if cf_access_team_domain is not None and (
        "/" in cf_access_team_domain or ":" in cf_access_team_domain
    ):
        errors.append(
            "CF_ACCESS_TEAM_DOMAIN must be a bare hostname without scheme or path "
            f"(e.g. yourteam.cloudflareaccess.com), got {cf_access_team_domain!r}"
        )

    mail_transport = (_get(env, "MAIL_TRANSPORT") or "graph").lower()
    if mail_transport not in MAIL_TRANSPORTS:
        errors.append(
            f"MAIL_TRANSPORT must be one of {', '.join(MAIL_TRANSPORTS)}, got {mail_transport!r}"
        )

    ms_client_id = _get(env, "MS_CLIENT_ID")
    if mail_transport == "graph" and ms_client_id is None:
        errors.append("MS_CLIENT_ID is required when MAIL_TRANSPORT=graph")

    ms_authority = _get(env, "MS_AUTHORITY") or Settings.ms_authority
    ms_authority = ms_authority.removesuffix("/")
    if problem := _http_url_error(ms_authority):
        errors.append(f"MS_AUTHORITY {problem}, got {ms_authority!r}")

    token_cache_key = _get(env, "TOKEN_CACHE_KEY")
    if token_cache_key is not None:
        try:
            Fernet(token_cache_key)
        except (ValueError, TypeError):
            errors.append(
                "TOKEN_CACHE_KEY is not a valid Fernet key (32 url-safe base64-encoded bytes; "
                "generate one with Fernet.generate_key())"
            )

    log_level = (_get(env, "LOG_LEVEL") or "INFO").upper()
    if log_level not in LOG_LEVELS:
        errors.append(f"LOG_LEVEL must be one of {', '.join(LOG_LEVELS)}, got {log_level!r}")

    settings_kwargs = dict(
        send_rate_per_minute=_positive_int(env, "SEND_RATE_PER_MINUTE", 6, errors),
        daily_send_cap=_positive_int(env, "DAILY_SEND_CAP", 250, errors),
        max_recipients=_positive_int(env, "MAX_RECIPIENTS", 2000, errors),
        max_upload_mb=_positive_int(env, "MAX_UPLOAD_MB", 5, errors),
        port=_positive_int(env, "PORT", 8765, errors, maximum=65535),
    )

    if errors:
        raise ConfigError(errors)

    assert public_base_url is not None and unsubscribe_secret is not None
    return Settings(
        public_base_url=public_base_url,
        unsubscribe_secret=unsubscribe_secret,
        ms_client_id=ms_client_id,
        ms_authority=ms_authority,
        cf_access_team_domain=cf_access_team_domain,
        cf_access_aud=cf_access_aud,
        auth_disabled=auth_disabled,
        mail_transport=mail_transport,
        token_cache_key=token_cache_key,
        data_dir=_get(env, "DATA_DIR") or "/data",
        tz=_get(env, "TZ") or "America/New_York",
        log_level=log_level,
        **settings_kwargs,
    )
