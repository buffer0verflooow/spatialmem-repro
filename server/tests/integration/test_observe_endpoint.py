"""/v1/observe 端点测试（dev 模式 + mock 后端）。"""

import base64

import pytest
from fastapi.testclient import TestClient

from app.config import Settings, get_settings
from app.main import create_app
from tests.conftest import make_jpeg


@pytest.fixture
def client(settings: Settings, monkeypatch):
    monkeypatch.setattr("app.main.get_settings", lambda: settings)
    get_settings.cache_clear()
    app = create_app()
    with TestClient(app) as c:
        yield c
    get_settings.cache_clear()


def b64(image: bytes) -> str:
    return base64.b64encode(image).decode()


def test_observe_returns_structured_json(client):
    resp = client.post(
        "/v1/observe",
        json={"frame": b64(make_jpeg(seed=1)), "hint": "这是什么"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert set(body.keys()) == {
        "name", "color", "location", "attributes", "confidence", "support"
    }
    assert isinstance(body["name"], str)
    assert isinstance(body["location"], str)
    assert isinstance(body["support"], dict)
    assert set(body["support"].keys()) == {"name", "color", "location", "attributes"}


def test_observe_bad_base64_returns_400(client):
    resp = client.post("/v1/observe", json={"frame": "!!!not-base64!!!"})
    assert resp.status_code == 400


def test_observe_empty_frame_returns_400(client):
    resp = client.post("/v1/observe", json={"frame": b64(b"")})
    assert resp.status_code == 400
