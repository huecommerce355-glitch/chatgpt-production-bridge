#!/usr/bin/env python3
"""Minimal stdlib HTTPS adapter for the ChatGPT production bridge."""
import argparse
import hashlib
import json
import logging
import os
import socket
import ssl
import sys
import time
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Callable, Dict, Mapping, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from urllib.parse import urlparse

MAX_BODY_BYTES = 1024 * 1024
ACTION_SCOPES = {
    "context": "strategy:read",
    "knowledge": "strategy:read",
    "adr": "strategy:propose",
    "handoff": "strategy:handoff",
}
ACTION_TYPES = {
    "context": "strategy.context.request",
    "knowledge": "strategy.knowledge.read",
    "adr": "strategy.adr.propose",
    "handoff": "strategy.handoff",
}
DEFAULT_DOWNSTREAM_TIMEOUT = 5.0
SENSITIVE_RESPONSE_KEYS = {"api_key", "secret", "token"}
LOGGER = logging.getLogger(__name__)


def _error(code: str, message: str) -> Dict[str, str]:
    return {"code": code, "message": message}


def _envelope(status: int, result: Optional[Dict[str, Any]] = None,
              error: Optional[Dict[str, str]] = None,
              **fields: Any) -> Tuple[int, Dict[str, Any]]:
    body: Dict[str, Any] = {"status": "ok" if error is None else "error"}
    body["result" if error is None else "error"] = result if error is None else error
    body.update({key: value for key, value in fields.items() if value is not None})
    return status, body


def _configured_keys() -> Dict[str, set]:
    """Parse key:scope pairs; scope-only tokens extend the preceding key."""
    configured: Dict[str, set] = {}
    current_key: Optional[str] = None
    for item in os.environ.get("BRIDGE_API_KEYS", "").split(","):
        token = item.strip()
        if not token:
            continue
        if token.startswith("strategy:") and current_key:
            configured[current_key].add(token)
            continue
        if ":" not in token:
            continue
        key, scope = token.split(":", 1)
        if key and scope:
            current_key = key
            configured.setdefault(key, set()).add(scope)
    return configured


def _supplied_key(headers: Mapping[str, str], body: Mapping[str, Any]) -> Optional[str]:
    nested = body.get("payload")
    return (headers.get("X-API-Key") or headers.get("x-api-key") or
            body.get("api_key") or (nested.get("api_key") if isinstance(nested, dict) else None))


def _key_id(key: Optional[str]) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:12] if key else "-"


def _audit(event: Dict[str, Any]) -> None:
    encoded = json.dumps(event, ensure_ascii=False, sort_keys=True)
    target = os.environ.get("BRIDGE_AUDIT_LOG")
    if target:
        with open(target, "a", encoding="utf-8") as stream:
            stream.write(encoded + "\n")
    else:
        print(encoded)


class RelayError(Exception):
    """An error that can be safely returned by the bridge relay."""

    def __init__(self, status: int, code: str, message: str, detail: Any = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.detail = detail


def _filtered(value: Any) -> Any:
    """Remove sensitive response fields recursively without mutating input."""
    if isinstance(value, dict):
        return {key: _filtered(item) for key, item in value.items()
                if str(key).lower() not in SENSITIVE_RESPONSE_KEYS}
    if isinstance(value, list):
        return [_filtered(item) for item in value]
    return value


def _downstream_error(detail: Any) -> RelayError:
    return RelayError(502, "BRIDGE-ERR-005", "downstream error", detail=detail)


def _decode_json(data: bytes) -> Any:
    try:
        return json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {"raw": data.decode("utf-8", errors="replace")}


class StrategyGatewayConnector:
    """Small stdlib-only HTTP connector for strategy-gateway."""

    def __init__(self, base_url: Optional[str] = None, api_key: Optional[str] = None,
                 timeout: Optional[float] = None) -> None:
        self.base_url = (base_url if base_url is not None else
                         os.environ.get("STRATEGY_GATEWAY_URL", "")).rstrip("/")
        self.api_key = api_key if api_key is not None else os.environ.get("STRATEGY_GATEWAY_API_KEY")
        configured_timeout = (timeout if timeout is not None else
                              os.environ.get("DOWNSTREAM_TIMEOUT_SECONDS", DEFAULT_DOWNSTREAM_TIMEOUT))
        try:
            self.timeout = float(configured_timeout)
        except (TypeError, ValueError):
            self.timeout = DEFAULT_DOWNSTREAM_TIMEOUT

    def forward(self, forward: Dict[str, Any]) -> Dict[str, Any]:
        if not self.base_url:
            return {"forwarded_to": "strategy-gateway", "request": forward}
        message_type = forward.get("type", "")
        action = next((name for name, kind in ACTION_TYPES.items() if kind == message_type), None)
        if action is None:
            raise _downstream_error({"message": "unsupported strategy message type"})
        target = "%s/strategy/%s" % (self.base_url, action)
        encoded = json.dumps(forward, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["X-API-Key"] = self.api_key
        request = Request(target, data=encoded, headers=headers, method="POST")
        try:
            with urlopen(request, timeout=self.timeout) as response:
                status = getattr(response, "status", response.getcode())
                body = _decode_json(response.read())
        except HTTPError as exc:
            body = _decode_json(exc.read())
            detail = {"http_status": exc.code, "response": body}
            raise _downstream_error(detail) from exc
        except (socket.timeout, TimeoutError) as exc:
            raise RelayError(502, "BRIDGE-ERR-004", "downstream timeout") from exc
        except URLError as exc:
            if isinstance(exc.reason, (socket.timeout, TimeoutError)):
                raise RelayError(502, "BRIDGE-ERR-004", "downstream timeout") from exc
            raise _downstream_error({"reason": str(exc.reason)}) from exc
        except OSError as exc:
            raise _downstream_error({"reason": str(exc)}) from exc
        if not 200 <= status < 300:
            raise _downstream_error({"http_status": status, "response": body})
        return body if isinstance(body, dict) else {"result": body}


def _relay_result(result: Dict[str, Any], request_id: Optional[str], trace_id: Optional[str]) -> Tuple[int, Dict[str, Any]]:
    if result.get("status") == "error":
        response = dict(result)
        if request_id is not None:
            response.setdefault("request_id", request_id)
        if trace_id is not None:
            response.setdefault("trace_id", trace_id)
        return 200, _filtered(response)
    downstream_result = result.get("result", result)
    return _envelope(200, result=_filtered(downstream_result), request_id=request_id, trace_id=trace_id)


def _process(method: str, path: str, headers: Mapping[str, str], body: Any,
             gateway_handler: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]],
             audit_stage: Optional[Callable[[str, int, Optional[str]], None]] = None) -> Tuple[Tuple[int, Dict[str, Any]], Optional[str], Optional[str]]:
    parsed_path = urlparse(path).path
    request_id = body.get("request_id") if isinstance(body, dict) else None
    payload = body.get("payload") if isinstance(body, dict) else None
    trace_id = body.get("trace_id") if isinstance(body, dict) else None
    session_id = body.get("session_id") if isinstance(body, dict) else None
    if isinstance(payload, dict):
        request_id = payload.get("request_id", request_id)
        trace_id = payload.get("trace_id", trace_id)
        session_id = payload.get("session_id", session_id)
    if method.upper() == "GET" and parsed_path == "/health":
        return _envelope(200, result={"ok": True}, request_id=request_id, trace_id=trace_id), request_id, trace_id
    if method.upper() != "POST" or not parsed_path.startswith("/v1/strategy/"):
        return _envelope(404, error=_error("BRIDGE-ERR-001", "endpoint not found")), request_id, trace_id
    action = parsed_path[len("/v1/strategy/"):]
    if action not in ACTION_SCOPES:
        return _envelope(404, error=_error("BRIDGE-ERR-001", "endpoint not found")), request_id, trace_id
    if not os.environ.get("BRIDGE_API_KEYS"):
        return _envelope(503, error=_error("BRIDGE-ERR-004", "bridge API keys are not configured")), request_id, trace_id
    if not isinstance(body, dict):
        return _envelope(400, error=_error("BRIDGE-ERR-001", "request body must be a JSON object")), request_id, trace_id
    supplied = _supplied_key(headers, body)
    keys = _configured_keys()
    if not supplied:
        return _envelope(401, error=_error("BRIDGE-ERR-001", "API key is required")), request_id, trace_id
    if supplied not in keys:
        return _envelope(403, error=_error("BRIDGE-ERR-001", "invalid API key")), request_id, trace_id
    if ACTION_SCOPES[action] not in keys[supplied]:
        return _envelope(403, error=_error("BRIDGE-ERR-002", "API key scope is not permitted")), request_id, trace_id
    if audit_stage:
        audit_stage("received", 200, None)
    message_type = body.get("type")
    if not isinstance(message_type, str) or not message_type.startswith("strategy."):
        return _envelope(403, error=_error("BRIDGE-ERR-003", "only strategy.* messages are accepted")), request_id, trace_id
    if message_type != ACTION_TYPES[action]:
        return _envelope(400, error=_error("BRIDGE-ERR-001", "message type does not match endpoint")), request_id, trace_id
    if not isinstance(payload, dict):
        return _envelope(400, error=_error("BRIDGE-ERR-001", "payload must be a JSON object")), request_id, trace_id
    forwarded_payload = dict(payload)
    forwarded_payload.pop("api_key", None)
    forward = {"protocol": body.get("protocol"), "type": message_type,
                "request_id": request_id, "trace_id": trace_id,
                "session_id": session_id, "payload": forwarded_payload}
    if gateway_handler is None and os.environ.get("STRATEGY_GATEWAY_URL"):
        gateway_handler = StrategyGatewayConnector().forward
    elif gateway_handler is None:
        LOGGER.warning("STRATEGY_GATEWAY_URL is not configured; using skeleton forwarding behavior")
    downstream_url = None
    if os.environ.get("STRATEGY_GATEWAY_URL"):
        downstream_url = "%s/strategy/%s" % (os.environ["STRATEGY_GATEWAY_URL"].rstrip("/"), action)
    if audit_stage:
        audit_stage("forwarded", 0, downstream_url)
    try:
        result = gateway_handler(forward) if gateway_handler else {"forwarded_to": "strategy-gateway", "request": forward}
        response = _relay_result(result, request_id, trace_id)
    except RelayError as exc:
        detail = exc.detail
        error = _error(exc.code, exc.message)
        if detail is not None:
            error["detail"] = _filtered(detail)
        response = exc.status, {"status": "error", "error": error,
                                "request_id": request_id, "trace_id": trace_id}
        if audit_stage:
            audit_stage("failed", exc.status, downstream_url)
        return response, request_id, trace_id
    if audit_stage:
        audit_stage("completed", response[0], downstream_url)
    return response, request_id, trace_id


def handle_request(method: str, path: str, headers: Optional[Mapping[str, str]] = None,
                   body: Any = None, gateway_handler: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None,
                   client_ip: str = "offline") -> Tuple[int, Dict[str, Any]]:
    """Handle one request without listening on a socket; used by offline tests."""
    started = time.monotonic()
    headers = headers or {}
    audit_events = []
    def audit_stage(stage: str, status_value: int, downstream_url: Optional[str]) -> None:
        audit_events.append((stage, status_value, downstream_url))
    (status, response), request_id, trace_id = _process(method, path, headers, body, gateway_handler, audit_stage)
    if not request_id:
        request_id = str(uuid.uuid4())
    if not audit_events:
        audit_events.append(("failed" if status >= 400 else "completed", status, None))
    for stage, stage_status, downstream_url in audit_events:
        event = {"time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "ip": client_ip,
                 "key_id": _key_id(_supplied_key(headers, body) if isinstance(body, dict) else None),
                 "method": method.upper(), "path": urlparse(path).path, "request_id": request_id,
                 "trace_id": trace_id, "status": status if stage_status == 0 else stage_status,
                 "elapsed_ms": round((time.monotonic() - started) * 1000, 3), "stage": stage}
        if downstream_url:
            event["downstream_url"] = downstream_url
        _audit(event)
    return status, response


class BridgeRequestHandler(BaseHTTPRequestHandler):
    def _send(self, status: int, body: Dict[str, Any]) -> None:
        encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self) -> None:
        status, body = handle_request("GET", self.path, self.headers, client_ip=self.client_address[0])
        self._send(status, body)

    def do_POST(self) -> None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 0 or length > MAX_BODY_BYTES:
                self._send(413, {"status": "error", "error": _error("BRIDGE-ERR-001", "request body too large")})
                return
            body = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            self._send(400, {"status": "error", "error": _error("BRIDGE-ERR-001", str(exc))})
            return
        status, response = handle_request("POST", self.path, self.headers, body, client_ip=self.client_address[0])
        self._send(status, response)

    def log_message(self, format: str, *args: Any) -> None:
        return


def main() -> int:
    parser = argparse.ArgumentParser(description="ChatGPT production bridge")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8443)
    parser.add_argument("--certfile", default=os.environ.get("BRIDGE_CERTFILE"))
    parser.add_argument("--keyfile", default=os.environ.get("BRIDGE_KEYFILE"))
    parser.add_argument("--strategy-gateway-url", default=os.environ.get("STRATEGY_GATEWAY_URL"))
    args = parser.parse_args()
    if args.strategy_gateway_url:
        os.environ["STRATEGY_GATEWAY_URL"] = args.strategy_gateway_url
    server = HTTPServer((args.host, args.port), BridgeRequestHandler)
    if bool(args.certfile) != bool(args.keyfile):
        parser.error("--certfile and --keyfile must be provided together")
    if args.certfile:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(args.certfile, args.keyfile)
        server.socket = context.wrap_socket(server.socket, server_side=True)
    print("bridge listening on %s:%s" % (args.host, args.port))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
