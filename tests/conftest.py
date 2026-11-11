import pytest

from app.config import Settings, load_settings

VALID_ENV = {
    "PUBLIC_BASE_URL": "https://mail.example.com",
    "UNSUBSCRIBE_SECRET": "x" * 32,
    "MAIL_TRANSPORT": "mock",
    "AUTH_DISABLED": "1",
}


@pytest.fixture
def valid_env() -> dict[str, str]:
    return dict(VALID_ENV)


@pytest.fixture
def settings(valid_env: dict[str, str]) -> Settings:
    return load_settings(valid_env)
