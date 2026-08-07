import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
from bridge_server import handle_request


def request(message_type="strategy.context.request", payload=None, **fields):
    return {"protocol": {"name": "HACP", "version": "1.0"}, "type": message_type,
            "payload": payload or {}, **fields}


def test_auth_success_and_scope(monkeypatch, tmp_path):
    monkeypatch.setenv("BRIDGE_API_KEYS", "read-key:strategy:read")
    monkeypatch.setenv("BRIDGE_AUDIT_LOG", str(tmp_path / "audit.jsonl"))
    status, body = handle_request("POST", "/v1/strategy/context", {"X-API-Key": "read-key"}, request())
    assert status == 200
    assert body["result"]["forwarded_to"] == "strategy-gateway"


def test_auth_failure(monkeypatch, tmp_path):
    monkeypatch.setenv("BRIDGE_API_KEYS", "key:strategy:read")
    monkeypatch.setenv("BRIDGE_AUDIT_LOG", str(tmp_path / "audit.jsonl"))
    assert handle_request("POST", "/v1/strategy/context", body=request())[0] == 401
    assert handle_request("POST", "/v1/strategy/context", {"X-API-Key": "wrong"}, request())[0] == 403


def test_scope_deny(monkeypatch, tmp_path):
    monkeypatch.setenv("BRIDGE_API_KEYS", "key:strategy:read")
    monkeypatch.setenv("BRIDGE_AUDIT_LOG", str(tmp_path / "audit.jsonl"))
    status, body = handle_request("POST", "/v1/strategy/adr", {"X-API-Key": "key"}, request("strategy.adr.propose"))
    assert status == 403 and body["error"]["code"] == "BRIDGE-ERR-002"


def test_non_strategy_message_reject(monkeypatch, tmp_path):
    monkeypatch.setenv("BRIDGE_API_KEYS", "key:strategy:read")
    monkeypatch.setenv("BRIDGE_AUDIT_LOG", str(tmp_path / "audit.jsonl"))
    status, body = handle_request("POST", "/v1/strategy/context", {"X-API-Key": "key"}, request("task.execute"))
    assert status == 403 and body["error"]["code"] == "BRIDGE-ERR-003"


def test_trace_session_propagation(monkeypatch, tmp_path):
    monkeypatch.setenv("BRIDGE_API_KEYS", "key:strategy:read")
    monkeypatch.setenv("BRIDGE_AUDIT_LOG", str(tmp_path / "audit.jsonl"))
    captured = {}
    def mock_gateway(forward):
        captured.update(forward)
        return {"mocked": True}
    status, _ = handle_request("POST", "/v1/strategy/context", {"X-API-Key": "key"},
                               request(payload={"x": 1}, trace_id="tr-1", session_id="sess-1"),
                               gateway_handler=mock_gateway)
    assert status == 200
    assert captured["trace_id"] == "tr-1" and captured["session_id"] == "sess-1"


def test_audit_log_written(monkeypatch, tmp_path):
    audit = tmp_path / "audit.jsonl"
    monkeypatch.setenv("BRIDGE_API_KEYS", "key:strategy:read")
    monkeypatch.setenv("BRIDGE_AUDIT_LOG", str(audit))
    handle_request("POST", "/v1/strategy/context", {"X-API-Key": "key"},
                   request(payload={}, request_id="req-1", trace_id="trace-1"))
    event = json.loads(audit.read_text(encoding="utf-8").splitlines()[-1])
    assert event["request_id"] == "req-1" and event["trace_id"] == "trace-1"
