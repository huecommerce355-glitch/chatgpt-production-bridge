---
name: chatgpt-production-bridge
description: "Production transport bridge from a ChatGPT custom GPT to the internal strategy-gateway."
metadata:
  version: 1.1.0
  layer: strategy
  domain: bridge
---

# chatgpt-production-bridge

## 安全原则

- External AI clients are untrusted.
- Bridge is the only trust boundary.

Custom GPT 请求必须先经过本 bridge 的认证、scope 授权、`strategy.*` 边界检查和审计，再考虑向内网转发。API key 只能通过环境变量配置，审计日志不得记录密钥原文。

## 架构

```text
ChatGPT (custom GPT) ── HTTPS ──> chatgpt-production-bridge ──单向──> strategy-gateway (内网)
```

本版本提供标准库 HTTPS adapter 和可选的真实 relay。未配置下游 URL 时保留 v1.0.1 骨架行为；配置后请求真实转发到 strategy-gateway。

## Real Relay

- `STRATEGY_GATEWAY_URL` 或启动参数 `--strategy-gateway-url` 配置下游地址，例如 `http://127.0.0.1:8080`。
- `STRATEGY_GATEWAY_API_KEY` 仅用于 bridge 到 strategy-gateway 的 `X-API-Key`，与 bridge 自身的 `BRIDGE_API_KEYS` 分离。
- 下游超时默认为 5 秒，可用 `DOWNSTREAM_TIMEOUT_SECONDS` 调整。
- 下游超时返回 HTTP 502 / `BRIDGE-ERR-004`；网络错误或非 2xx 下游响应返回 HTTP 502 / `BRIDGE-ERR-005`，并在 detail 中保留下游错误信息。
- 下游 HTTP 200 且 `status: error` 的 strategy 错误透传给 ChatGPT。响应中的 `api_key`、`secret`、`token` 字段会被递归移除。
- 审计事件使用 `stage`：认证后的 `received`、发起下游请求的 `forwarded`、成功返回的 `completed`、转发失败的 `failed`。

## 边界

Bridge 只负责传输、认证、scope 授权和审计，不做业务决策、不执行策略动作，也不提供公网部署或 GPT Store 配置。允许的消息类型必须是 `strategy.*`，端点仅为 `context`、`knowledge`、`adr`、`handoff`。

详细协议见 [references/openapi.yaml](references/openapi.yaml)，信任边界和威胁模型见 [references/security_boundary.md](references/security_boundary.md)，审计格式见 [references/audit_log.md](references/audit_log.md)。
