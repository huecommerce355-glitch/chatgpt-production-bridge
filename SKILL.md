---
name: chatgpt-production-bridge
description: "Production transport bridge from a ChatGPT custom GPT to the internal strategy-gateway."
metadata:
  version: 1.0.0
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

本版本只提供标准库 HTTPS adapter 骨架；默认构造并返回转发请求，实际 gateway 连接由后续部署阶段接入。

## 边界

Bridge 只负责传输、认证、scope 授权和审计，不做业务决策、不执行策略动作，也不提供公网部署或 GPT Store 配置。允许的消息类型必须是 `strategy.*`，端点仅为 `context`、`knowledge`、`adr`、`handoff`。

详细协议见 [references/openapi.yaml](references/openapi.yaml)，信任边界和威胁模型见 [references/security_boundary.md](references/security_boundary.md)，审计格式见 [references/audit_log.md](references/audit_log.md)。
