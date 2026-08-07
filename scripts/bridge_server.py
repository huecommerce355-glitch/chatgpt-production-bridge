#!/usr/bin/env python3
"""Minimal stdlib HTTPS adapter for the ChatGPT production bridge."""
import argparse
import hashlib
import json
import os
import ssl
import sys
import time
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Callable, Dict, Mapping, Optional, Tuple
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


def _process(method: str, path: str, headers: Mapping[str, str], body: Any,
             gateway_handler: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]]) -> Tuple[Tuple[int, Dict[str, Any]], Optional[str], Optional[str]]:
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
    result = gateway_handler(forward) if gateway_handler else {"forwarded_to": "strategy-gateway", "request": forward}
    return _envelope(200, result=result, request_id=request_id, trace_id=trace_id), request_id, trace_id


def handle_request(method: str, path: str, headers: Optional[Mapping[str, str]] = None,
                   body: Any = None, gateway_handler: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None,
                   client_ip: str = "offline") -> Tuple[int, Dict[str, Any]]:
    """Handle one request without listening on a socket; used by offline tests."""
    started = time.monotonic()
    headers = headers or {}
    (status, response), request_id, trace_id = _process(method, path, headers, body, gateway_handler)
    if not request_id:
        request_id = str(uuid.uuid4())
    _audit({"time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "ip": client_ip,
            "key_id": _key_id(_supplied_key(headers, body) if isinstance(body, dict) else None),
            "method": method.upper(), "path": urlparse(path).path, "request_id": request_id,
            "trace_id": trace_id, "status": status, "elapsed_ms": round((time.monotonic() - started) * 1000, 3)})
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
    args = parser.parse_args()
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
