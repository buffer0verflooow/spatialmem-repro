"""接入层测试：鉴权、协议、背压、健康检查。"""

from __future__ import annotations

import base64

import pytest
from fastapi.testclient import TestClient

from app.config import Settings, get_settings
from app.main import create_app
from app.transport.auth import sign, verify
from tests.conftest import make_jpeg

SECRET = "test-secret"


@pytest.fixture
def client(settings: Settings, monkeypatch):
    """真实启动 app（走 lifespan），配置指向内存后端。"""
    monkeypatch.setattr("app.main.get_settings", lambda: settings)
    get_settings.cache_clear()
    app = create_app()
    with TestClient(app) as c:
        yield c
    get_settings.cache_clear()


def b64(image: bytes) -> str:
    return base64.b64encode(image).decode()


class TestAuth:
    def test_sign_verify_roundtrip(self):
        token = sign("dev-1", SECRET)
        assert verify("dev-1", token, SECRET)

    def test_wrong_secret_rejected(self):
        assert not verify("dev-1", sign("dev-1", "other"), SECRET)

    def test_token_not_transferable_between_devices(self):
        assert not verify("dev-2", sign("dev-1", SECRET), SECRET)

    def test_empty_inputs_rejected(self):
        assert not verify("", "", SECRET)
        assert not verify("dev-1", "", SECRET)

    def test_case_insensitive_and_trimmed(self):
        token = sign("dev-1", SECRET)
        assert verify("dev-1", f"  {token.upper()}  ", SECRET)


class TestHealth:
    def test_healthz(self, client: TestClient):
        assert client.get("/healthz").json() == {"status": "ok"}

    def test_readyz_ok_with_memory_kv(self, client: TestClient):
        resp = client.get("/readyz")
        assert resp.status_code == 200
        assert resp.json()["kv"] is True

    def test_metrics_exposes_our_series(self, client: TestClient):
        body = client.get("/metrics").text
        assert "linksee_e2e_latency_seconds" in body
        assert "linksee_reject_total" in body


class TestHttpFrame:
    def _post(self, client: TestClient, device_id: str, image: bytes, **kw):
        return client.post(
            "/v1/frame",
            json={"device_id": device_id, "image": b64(image), **kw},
            headers={"X-Device-Token": sign(device_id, SECRET)},
        )

    def test_valid_frame_returns_reply(self, client: TestClient):
        resp = self._post(client, "dev-http", make_jpeg(seed=1))
        assert resp.status_code == 200
        body = resp.json()
        assert body["type"] in ("text", "voice", "alert", "noop")
        assert body["latency_ms"] >= 0

    def test_missing_token_401(self, client: TestClient):
        resp = client.post(
            "/v1/frame", json={"device_id": "dev-x", "image": b64(make_jpeg(seed=1))}
        )
        assert resp.status_code == 401

    def test_bad_base64_400(self, client: TestClient):
        resp = client.post(
            "/v1/frame",
            json={"device_id": "dev-x", "image": "!!!not base64!!!"},
            headers={"X-Device-Token": sign("dev-x", SECRET)},
        )
        assert resp.status_code == 400

    def test_empty_image_422(self, client: TestClient):
        resp = client.post(
            "/v1/frame",
            json={"device_id": "dev-x", "image": ""},
            headers={"X-Device-Token": sign("dev-x", SECRET)},
        )
        assert resp.status_code == 422

    def test_seq_echoed_back(self, client: TestClient):
        resp = self._post(client, "dev-seq", make_jpeg(seed=1), seq=42)
        assert resp.json()["seq"] == 42

    def test_duplicate_frame_returns_noop(self, client: TestClient):
        img = make_jpeg(seed=5)
        self._post(client, "dev-dup2", img)
        assert self._post(client, "dev-dup2", img).json()["type"] == "noop"

    def test_manual_trigger_bypasses_dedup(self, client: TestClient):
        img = make_jpeg(seed=6)
        self._post(client, "dev-man", img, trigger="manual")
        resp = self._post(client, "dev-man", img, trigger="manual")
        assert resp.json()["type"] != "noop"


class TestWebSocket:
    def test_unauthorized_connection_closed(self, client: TestClient):
        from starlette.websockets import WebSocketDisconnect

        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/ws/glass/dev-1?token=wrong") as ws:
                ws.receive_json()

    def test_ping_pong(self, client: TestClient):
        token = sign("dev-ws", SECRET)
        with client.websocket_connect(f"/ws/glass/dev-ws?token={token}") as ws:
            ws.send_json({"type": "ping"})
            assert ws.receive_json() == {"type": "pong"}

    def test_frame_roundtrip(self, client: TestClient):
        token = sign("dev-ws2", SECRET)
        with client.websocket_connect(f"/ws/glass/dev-ws2?token={token}") as ws:
            ws.send_json(
                {"type": "frame", "seq": 1, "trigger": "auto", "image": b64(make_jpeg(seed=1))}
            )
            reply = ws.receive_json()
            assert reply["seq"] == 1
            assert reply["type"] in ("text", "voice", "alert", "noop")

    def test_bad_frame_gets_error_but_connection_survives(self, client: TestClient):
        token = sign("dev-ws3", SECRET)
        with client.websocket_connect(f"/ws/glass/dev-ws3?token={token}") as ws:
            ws.send_json({"type": "frame", "image": "@@@bad@@@"})
            err = ws.receive_json()
            assert err["type"] == "error"
            # 连接没断，还能继续用
            ws.send_json({"type": "ping"})
            assert ws.receive_json()["type"] == "pong"

    def test_admin_lists_online_device(self, client: TestClient):
        token = sign("dev-ws4", SECRET)
        with client.websocket_connect(f"/ws/glass/dev-ws4?token={token}") as ws:
            ws.send_json({"type": "ping"})
            ws.receive_json()
            devices = client.get("/admin/devices").json()
            assert "dev-ws4" in devices["device_ids"]


class TestBackpressure:
    async def test_latest_frame_wins_old_one_dropped(self):
        """每设备只保留最新 1 帧（§5.1）。旧帧必须被丢弃而不是排队。"""
        from app.gate import LatestOnlySlot

        slot: LatestOnlySlot[int] = LatestOnlySlot()
        assert slot.put(1) is False  # 空槽，没挤掉东西
        assert slot.put(2) is True  # 挤掉了 1
        assert slot.put(3) is True  # 挤掉了 2
        assert await slot.get() == 3  # 只剩最新的

    async def test_close_unblocks_consumer(self):
        from app.gate import LatestOnlySlot

        slot: LatestOnlySlot[int] = LatestOnlySlot()
        slot.close()
        assert await slot.get() is None


@pytest.fixture
def read_client(settings: Settings, monkeypatch):
    """阅读模式专用 client：切分上限压到 4 字，保证 mock 的每份文档都产生多片。"""
    tuned = settings.model_copy(
        update={"reply_max_chars": 4, "gate_read_burst": 20.0}
    )
    monkeypatch.setattr("app.main.get_settings", lambda: tuned)
    get_settings.cache_clear()
    app = create_app()
    with TestClient(app) as c:
        yield c
    get_settings.cache_clear()


class TestReadModeHttp:
    def _post(self, client: TestClient, device_id: str, image: bytes, **kw):
        return client.post(
            "/v1/frame",
            json={"device_id": device_id, "image": b64(image), **kw},
            headers={"X-Device-Token": sign(device_id, SECRET)},
        )

    def test_read_returns_all_segments_at_once(self, read_client: TestClient):
        """HTTP 是单响应，没有连续下发语义——一次把全部分片给出去。"""
        resp = self._post(read_client, "dev-read", make_jpeg(seed=1), trigger="read")

        body = resp.json()
        assert body["type"] == "read"
        assert len(body["segments"]) > 1
        assert body["total"] == len(body["segments"])

    def test_realtime_frame_has_no_segments(self, read_client: TestClient):
        resp = self._post(read_client, "dev-rt", make_jpeg(seed=1), trigger="auto")
        assert resp.json()["segments"] is None

    def test_read_bypasses_dedup(self, read_client: TestClient):
        """对着同一份菜单拍第二次必须还能读——不豁免去重功能就废了。"""
        img = make_jpeg(seed=9)
        self._post(read_client, "dev-read2", img, trigger="read")
        resp = self._post(read_client, "dev-read2", img, trigger="read")

        assert resp.json()["type"] == "read"


class TestReadModeWebSocket:
    def test_segments_arrive_consecutively(self, read_client: TestClient):
        token = sign("dev-wsr", SECRET)
        with read_client.websocket_connect(f"/ws/glass/dev-wsr?token={token}") as ws:
            ws.send_json(
                {
                    "type": "frame",
                    "seq": 7,
                    "trigger": "read",
                    "image": b64(make_jpeg(seed=1)),
                }
            )
            first = ws.receive_json()
            received = [first]
            while not received[-1]["end"]:
                received.append(ws.receive_json())

        assert len(received) > 1
        assert all(m["type"] == "read" for m in received)
        assert [m["index"] for m in received] == list(range(1, len(received) + 1))
        assert all(m["total"] == len(received) for m in received)

    def test_all_segments_echo_the_same_frame_seq(self, read_client: TestClient):
        """seq 是帧序号：8 个分片属于同一次请求，不是 8 个不同的帧。"""
        token = sign("dev-wsr2", SECRET)
        with read_client.websocket_connect(f"/ws/glass/dev-wsr2?token={token}") as ws:
            ws.send_json(
                {
                    "type": "frame",
                    "seq": 33,
                    "trigger": "read",
                    "image": b64(make_jpeg(seed=2)),
                }
            )
            received = [ws.receive_json()]
            while not received[-1]["end"]:
                received.append(ws.receive_json())

        assert all(m["seq"] == 33 for m in received)

    def test_realtime_frame_still_single_message(self, read_client: TestClient):
        token = sign("dev-wsr3", SECRET)
        with read_client.websocket_connect(f"/ws/glass/dev-wsr3?token={token}") as ws:
            ws.send_json(
                {"type": "frame", "seq": 1, "trigger": "auto", "image": b64(make_jpeg(seed=3))}
            )
            msg = ws.receive_json()

        assert msg["type"] != "read"
        assert msg["total"] == 1
        assert msg["end"] is True
