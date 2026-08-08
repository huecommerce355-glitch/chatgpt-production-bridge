import json
import socket
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread
from urllib.error import URLError

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import bridge_server
from bridge_server import handle_request


def request(message_type="strategy.context.request", payload=None, **fields):
    return {"protocol": {"name": "HACP", "version": "1.0"}, "type": message_type,
            "payload": payload or {}, **fields}


class Response:
    def __init__(self, body, status=200):
        self.body = json.dumps(body).encode("utf-8")
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def getcode(self):
        return self.status

    def read(self):
        return self.body


def configured(monkeypatch, tmp_path):
    monkeypatch.setenv("BRIDGE_API_KEYS", "bridge-key:strategy:read")
    monkeypatch.setenv("BRIDGE_AUDIT_LOG", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("STRATEGY_GATEWAY_URL", "http://gateway.test:8080")
    monkeypatch.setenv("STRATEGY_GATEWAY_API_KEY", "downstream-key")


def test_downstream_forwarding_and_response_filtering(monkeypatch, tmp_path):
    configured(monkeypatch, tmp_path)
    captured = {}

    def fake_urlopen(req, timeout):
        captured.update(url=req.full_url, headers=dict(req.header_items()), body=json.loads(req.data), timeout=timeout)
        return Response({"status": "ok", "result": {"real": True, "token": "remove-me"}})

    monkeypatch.setattr(bridge_server, "urlopen", fake_urlopen)
    status, body = handle_request(
        "POST", "/v1/strategy/context", {"X-API-Key": "bridge-key"},
        request(payload={"api_key": "client-secret", "project_id": "p1"}, request_id="r1"),
    )
    assert status == 200
    assert body == {"status": "ok", "result": {"real": True}, "request_id": "r1"}
    assert captured["url"] == "http://gateway.test:8080/strategy/context"
    assert captured["headers"]["Content-type"] == "application/json"
    assert captured["headers"]["X-api-key"] == "downstream-key"
    assert captured["body"]["payload"] == {"project_id": "p1"}


def test_downstream_timeout_maps_to_004(monkeypatch, tmp_path):
    configured(monkeypatch, tmp_path)
    monkeypatch.setattr(bridge_server, "urlopen", lambda *args, **kwargs: (_ for _ in ()).throw(socket.timeout()))
    status, body = handle_request("POST", "/v1/strategy/context", {"X-API-Key": "bridge-key"}, request())
    assert status == 502 and body["error"]["code"] == "BRIDGE-ERR-004"


def test_downstream_http_and_network_failures_map_to_005(monkeypatch, tmp_path):
    configured(monkeypatch, tmp_path)

    def fail(*args, **kwargs):
        raise URLError(ConnectionRefusedError("refused"))

    monkeypatch.setattr(bridge_server, "urlopen", fail)
    status, body = handle_request("POST", "/v1/strategy/context", {"X-API-Key": "bridge-key"}, request())
    assert status == 502 and body["error"]["code"] == "BRIDGE-ERR-005"

    from io import BytesIO
    from urllib.error import HTTPError
    error_body = {"status": "error", "error": {"code": "ERR-STR-500", "message": "bad downstream"}}
    monkeypatch.setattr(
        bridge_server, "urlopen",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            HTTPError("http://gateway.test", 500, "error", {}, BytesIO(json.dumps(error_body).encode()))
        ),
    )
    status, body = handle_request("POST", "/v1/strategy/context", {"X-API-Key": "bridge-key"}, request())
    assert status == 502
    assert body["error"]["code"] == "BRIDGE-ERR-005"
    assert body["error"]["detail"]["response"]["error"]["code"] == "ERR-STR-500"


def test_downstream_strategy_error_is_transparent(monkeypatch, tmp_path):
    configured(monkeypatch, tmp_path)
    monkeypatch.setenv("BRIDGE_API_KEYS", "bridge-key:strategy:read,strategy:handoff")
    error_body = {"status": "error", "error": {"code": "ERR-STR-002", "message": "invalid strategy"}}
    monkeypatch.setattr(bridge_server, "urlopen", lambda *args, **kwargs: Response(error_body))
    status, body = handle_request("POST", "/v1/strategy/handoff", {"X-API-Key": "bridge-key"}, request("strategy.handoff"))
    assert status == 200 and body == error_body


class DownstreamHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers["Content-Length"])
        incoming = json.loads(self.rfile.read(length))
        action = self.path.rsplit("/", 1)[-1]
        result = {"business": action, "received_type": incoming["type"], "api_key": "never-return"}
        encoded = json.dumps({"status": "ok", "result": result}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, *args):
        return


@pytest.mark.parametrize("action,message_type", [
    ("context", "strategy.context.request"),
    ("knowledge", "strategy.knowledge.read"),
    ("adr", "strategy.adr.propose"),
    ("handoff", "strategy.handoff"),
])
def test_real_local_http_relay(monkeypatch, tmp_path, action, message_type):
    monkeypatch.setenv("BRIDGE_API_KEYS", "bridge-key:strategy:read,strategy:propose,strategy:handoff")
    monkeypatch.setenv("BRIDGE_AUDIT_LOG", str(tmp_path / "audit.jsonl"))
    try:
        downstream = HTTPServer(("127.0.0.1", 0), DownstreamHandler)
    except PermissionError:
        pytest.skip("sandbox disallows local listening sockets")
    thread = Thread(target=downstream.serve_forever, daemon=True)
    thread.start()
    try:
        monkeypatch.setenv("STRATEGY_GATEWAY_URL", "http://127.0.0.1:%s" % downstream.server_port)
        headers = {"X-API-Key": "bridge-key"}
        status, body = handle_request("POST", "/v1/strategy/" + action, headers, request(message_type))
        assert status == 200
        assert body["result"]["business"] == action
        assert "api_key" not in json.dumps(body)
    finally:
        downstream.shutdown()
        downstream.server_close()


def test_unconfigured_gateway_keeps_skeleton(monkeypatch, tmp_path):
    monkeypatch.setenv("BRIDGE_API_KEYS", "bridge-key:strategy:read")
    monkeypatch.setenv("BRIDGE_AUDIT_LOG", str(tmp_path / "audit.jsonl"))
    monkeypatch.delenv("STRATEGY_GATEWAY_URL", raising=False)
    status, body = handle_request("POST", "/v1/strategy/context", {"X-API-Key": "bridge-key"}, request())
    assert status == 200 and body["result"]["forwarded_to"] == "strategy-gateway"
