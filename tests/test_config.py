import pytest
from cryptography.fernet import Fernet

from app.config import ConfigError, Settings, load_settings

SECRET = "s3cr3t-" + "y" * 30


def errors_for(env: dict[str, str]) -> list[str]:
    with pytest.raises(ConfigError) as exc_info:
        load_settings(env)
    return exc_info.value.errors


def assert_error(env: dict[str, str], *fragments: str) -> None:
    errors = errors_for(env)
    assert any(all(f in e for f in fragments) for e in errors), errors


def test_minimal_valid_env_uses_defaults(valid_env: dict[str, str]) -> None:
    s = load_settings(valid_env)
    assert s.public_base_url == "https://mail.example.com"
    assert s.ms_authority == "https://login.microsoftonline.com/consumers"
    assert s.auth_disabled is True
    assert s.mail_transport == "mock"
    assert (s.send_rate_per_minute, s.daily_send_cap) == (6, 250)
    assert (s.max_recipients, s.max_upload_mb) == (2000, 5)
    assert s.token_cache_key is None
    assert (s.data_dir, s.port, s.tz, s.log_level) == ("/data", 8765, "America/New_York", "INFO")


def test_full_production_env() -> None:
    key = Fernet.generate_key().decode()
    s = load_settings(
        {
            "PUBLIC_BASE_URL": "https://the-calico-barn.jakewelliott.com",
            "UNSUBSCRIBE_SECRET": SECRET,
            "MS_CLIENT_ID": "00000000-0000-0000-0000-000000000000",
            "CF_ACCESS_TEAM_DOMAIN": "jakewelliott.cloudflareaccess.com",
            "CF_ACCESS_AUD": "abc123",
            "SEND_RATE_PER_MINUTE": "10",
            "DAILY_SEND_CAP": "100",
            "MAX_RECIPIENTS": "50",
            "MAX_UPLOAD_MB": "2",
            "TOKEN_CACHE_KEY": key,
            "DATA_DIR": "/tmp/data",
            "PORT": "9000",
            "TZ": "UTC",
            "LOG_LEVEL": "debug",
        }
    )
    assert s.mail_transport == "graph"
    assert s.auth_disabled is False
    assert s.cf_access_aud == "abc123"
    assert (s.send_rate_per_minute, s.daily_send_cap, s.max_recipients) == (10, 100, 50)
    assert (s.max_upload_mb, s.port, s.log_level) == (2, 9000, "DEBUG")
    assert s.token_cache_key == key


def test_empty_env_reports_every_required_variable() -> None:
    errors = errors_for({})
    joined = "\n".join(errors)
    for name in (
        "PUBLIC_BASE_URL",
        "UNSUBSCRIBE_SECRET",
        "CF_ACCESS_TEAM_DOMAIN",
        "CF_ACCESS_AUD",
        "MS_CLIENT_ID",
    ):
        assert name in joined
    assert len(errors) == 5


def test_blank_values_count_as_missing(valid_env: dict[str, str]) -> None:
    valid_env["PUBLIC_BASE_URL"] = "   "
    assert_error(valid_env, "PUBLIC_BASE_URL is required")


# PUBLIC_BASE_URL


def test_public_base_url_trailing_slash_is_stripped(valid_env: dict[str, str]) -> None:
    valid_env["PUBLIC_BASE_URL"] = "https://mail.example.com/"
    assert load_settings(valid_env).public_base_url == "https://mail.example.com"


def test_public_base_url_http_allowed(valid_env: dict[str, str]) -> None:
    valid_env["PUBLIC_BASE_URL"] = "http://localhost:8765"
    assert load_settings(valid_env).public_base_url == "http://localhost:8765"


@pytest.mark.parametrize(
    "value",
    ["mail.example.com", "ftp://mail.example.com", "https://", "/relative", "https://x.com?a=1"],
)
def test_public_base_url_must_be_absolute_http_url(valid_env: dict[str, str], value: str) -> None:
    valid_env["PUBLIC_BASE_URL"] = value
    assert_error(valid_env, "PUBLIC_BASE_URL must")


def test_public_base_url_double_trailing_slash_rejected(valid_env: dict[str, str]) -> None:
    valid_env["PUBLIC_BASE_URL"] = "https://mail.example.com//"
    assert_error(valid_env, "PUBLIC_BASE_URL must not end with a slash")


# UNSUBSCRIBE_SECRET


def test_unsubscribe_secret_too_short_does_not_leak_value(valid_env: dict[str, str]) -> None:
    valid_env["UNSUBSCRIBE_SECRET"] = "hunter2-short"
    errors = errors_for(valid_env)
    assert any("UNSUBSCRIBE_SECRET must be at least 32" in e for e in errors)
    assert not any("hunter2" in e for e in errors)


def test_unsubscribe_secret_exactly_32_ok(valid_env: dict[str, str]) -> None:
    valid_env["UNSUBSCRIBE_SECRET"] = "z" * 32
    load_settings(valid_env)


def test_secrets_not_in_repr(valid_env: dict[str, str]) -> None:
    valid_env["UNSUBSCRIBE_SECRET"] = SECRET
    valid_env["TOKEN_CACHE_KEY"] = Fernet.generate_key().decode()
    s = load_settings(valid_env)
    assert SECRET not in repr(s)
    assert valid_env["TOKEN_CACHE_KEY"] not in repr(s)


# Cloudflare Access / AUTH_DISABLED


def test_cf_access_required_when_auth_enabled(valid_env: dict[str, str]) -> None:
    valid_env["AUTH_DISABLED"] = "0"
    assert_error(valid_env, "CF_ACCESS_TEAM_DOMAIN is required unless AUTH_DISABLED=1")
    assert_error(valid_env, "CF_ACCESS_AUD is required unless AUTH_DISABLED=1")


def test_auth_disabled_defaults_to_off(valid_env: dict[str, str]) -> None:
    del valid_env["AUTH_DISABLED"]
    assert_error(valid_env, "CF_ACCESS_AUD is required")


def test_cf_access_not_required_when_auth_disabled(valid_env: dict[str, str]) -> None:
    s = load_settings(valid_env)
    assert s.cf_access_team_domain is None and s.cf_access_aud is None


@pytest.mark.parametrize("value", ["true", "yes", "2"])
def test_auth_disabled_must_be_0_or_1(valid_env: dict[str, str], value: str) -> None:
    valid_env["AUTH_DISABLED"] = value
    assert_error(valid_env, "AUTH_DISABLED must be 0 or 1")


@pytest.mark.parametrize("value", ["https://team.cloudflareaccess.com", "team.example.com/path"])
def test_cf_access_team_domain_must_be_bare_host(valid_env: dict[str, str], value: str) -> None:
    valid_env["CF_ACCESS_TEAM_DOMAIN"] = value
    assert_error(valid_env, "CF_ACCESS_TEAM_DOMAIN must be a bare hostname")


# MAIL_TRANSPORT / MS_CLIENT_ID


def test_ms_client_id_required_for_graph(valid_env: dict[str, str]) -> None:
    valid_env["MAIL_TRANSPORT"] = "graph"
    assert_error(valid_env, "MS_CLIENT_ID is required when MAIL_TRANSPORT=graph")


def test_mail_transport_defaults_to_graph(valid_env: dict[str, str]) -> None:
    del valid_env["MAIL_TRANSPORT"]
    assert_error(valid_env, "MS_CLIENT_ID is required")
    valid_env["MS_CLIENT_ID"] = "client-id"
    assert load_settings(valid_env).mail_transport == "graph"


def test_ms_client_id_not_required_for_mock(valid_env: dict[str, str]) -> None:
    assert load_settings(valid_env).ms_client_id is None


def test_mail_transport_invalid(valid_env: dict[str, str]) -> None:
    valid_env["MAIL_TRANSPORT"] = "smtp"
    assert_error(valid_env, "MAIL_TRANSPORT must be one of graph, mock", "'smtp'")


# Numeric variables


@pytest.mark.parametrize(
    "name", ["SEND_RATE_PER_MINUTE", "DAILY_SEND_CAP", "MAX_RECIPIENTS", "MAX_UPLOAD_MB", "PORT"]
)
@pytest.mark.parametrize("value", ["0", "-1", "abc", "1.5"])
def test_numeric_vars_must_be_positive_ints(
    valid_env: dict[str, str], name: str, value: str
) -> None:
    valid_env[name] = value
    assert_error(valid_env, f"{name} must be a positive whole number", repr(value))


def test_port_upper_bound(valid_env: dict[str, str]) -> None:
    valid_env["PORT"] = "70000"
    assert_error(valid_env, "PORT must be at most 65535")


# TOKEN_CACHE_KEY


def test_token_cache_key_invalid(valid_env: dict[str, str]) -> None:
    valid_env["TOKEN_CACHE_KEY"] = "not-a-fernet-key"
    errors = errors_for(valid_env)
    assert any("TOKEN_CACHE_KEY is not a valid Fernet key" in e for e in errors)
    assert not any("not-a-fernet-key" in e for e in errors)


# Others


def test_log_level_invalid(valid_env: dict[str, str]) -> None:
    valid_env["LOG_LEVEL"] = "LOUD"
    assert_error(valid_env, "LOG_LEVEL must be one of")


def test_ms_authority_must_be_url(valid_env: dict[str, str]) -> None:
    valid_env["MS_AUTHORITY"] = "consumers"
    assert_error(valid_env, "MS_AUTHORITY must be an absolute http(s) URL")


def test_multiple_errors_are_all_reported(valid_env: dict[str, str]) -> None:
    valid_env.update({"PORT": "x", "MAIL_TRANSPORT": "nope", "DAILY_SEND_CAP": "0"})
    errors = errors_for(valid_env)
    assert len(errors) == 3
    message = str(ConfigError(errors))
    assert message.startswith("Invalid configuration:")


def test_settings_is_frozen(settings: Settings) -> None:
    with pytest.raises(AttributeError):
        settings.port = 1  # type: ignore[misc]
