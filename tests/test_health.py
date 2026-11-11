import subprocess
import sys

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


def test_healthz(settings: Settings) -> None:
    client = TestClient(create_app(settings))
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_settings_on_app_state(settings: Settings) -> None:
    assert create_app(settings).state.settings is settings


def test_module_app_refuses_to_start_with_bad_env() -> None:
    result = subprocess.run(
        [sys.executable, "-c", "import app.main; app.main.app"],
        env={"PATH": "", "UNSUBSCRIBE_SECRET": "short"},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert "PUBLIC_BASE_URL is required" in result.stderr
    assert "UNSUBSCRIBE_SECRET must be at least 32" in result.stderr
    assert "short" not in result.stderr
