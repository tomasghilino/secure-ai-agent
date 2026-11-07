from fastapi.testclient import TestClient

from app import __version__
from app.main import create_app


def test_liveness_reports_ok_and_version() -> None:
    client = TestClient(create_app())

    response = client.get("/health/live")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["version"] == __version__
