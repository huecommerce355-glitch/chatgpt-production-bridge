# chatgpt-production-bridge

Production transport bridge from a ChatGPT custom GPT to the internal strategy-gateway (v1.0).

## Security Principles
- External AI clients are untrusted.
- Bridge is the only trust boundary.

## Capabilities (v1.0)
- HTTPS adapter skeleton (GET /health, POST /v1/strategy/{context|knowledge|adr|handoff})
- API Key authentication (X-API-Key, scope-gated)
- Scope authorization (strategy:read / strategy:propose / strategy:handoff)
- Audit log (JSONL)
- OpenAPI 3.0 schema
- strategy.* only boundary

## Deferred
JWT, public deployment, GPT Store publication (later phases).

## ADR
ADR-007 chatgpt-production-bridge (proposed).
