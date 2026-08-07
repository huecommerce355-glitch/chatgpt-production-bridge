# Security boundary and threat model

```text
[External AI client / custom GPT]
             | untrusted HTTPS request
             v
      [Bridge: the only trust boundary]
       API key -> scope -> strategy.* -> audit
             | one-way structured forward
             v
     [strategy-gateway: internal network]
```

The client is never trusted with internal network access or business authority. The bridge must not make strategy decisions; it only authenticates, authorizes, records, and forwards.

| Threat | v1.0 control | Follow-up |
|---|---|---|
| API key leakage | env-configured keys, no plaintext key in audit/forwarded payload | rotation, secret manager, TLS termination policy |
| Replay | request IDs and trace propagation for correlation | nonce/timestamp or signed replay protection |
| Prompt injection | `strategy.*` type boundary; no arbitrary tool/business execution | validate schemas and downstream content |
| DoS | bounded request body; stdlib server skeleton | rate limits, timeouts, queue/backpressure |
| Lateral movement | one-way target restricted to strategy-gateway; no client network passthrough | network policy and egress allowlist |
