# Audit log

Each request appends one JSON object to `BRIDGE_AUDIT_LOG`; when unset, the object is printed to stdout. The `key_id` is a short SHA-256 identifier, never the API key itself.

Schema:

```json
{
  "time": "RFC3339 UTC string",
  "ip": "client address",
  "key_id": "12-char key identifier or -",
  "method": "GET|POST",
  "path": "request path",
  "request_id": "request correlation id",
  "trace_id": "trace id or null",
  "status": 200,
  "elapsed_ms": 0.123,
  "stage": "completed",
  "downstream_url": "http://127.0.0.1:8080/strategy/context"
}
```

`stage` is one of `received`, `forwarded`, `completed`, or `failed`. The
`downstream_url` field is present for relay stages when a downstream URL is
configured; secrets are never written to the audit event.

Example:

```json
{"time":"2026-08-08T00:00:00Z","ip":"127.0.0.1","key_id":"0b8e...f1a2","method":"POST","path":"/v1/strategy/context","request_id":"req-1","trace_id":"trace-1","status":200,"elapsed_ms":0.42}
```
