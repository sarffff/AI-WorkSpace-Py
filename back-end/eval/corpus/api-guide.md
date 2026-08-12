# 内部平台 API 指南

Base URL：`https://api.internal.acme.dev/v2`

## 认证

所有接口使用 Bearer Token 认证：

```
Authorization: Bearer <access_token>
```

access_token 有效期 **30 分钟**，refresh_token 有效期 7 天。
刷新接口为 `POST /auth/refresh`，请求体 `{"refresh_token": "..."}`。

同一账号最多同时持有 5 个有效 refresh_token，超出后最旧的会被吊销。

## 分页

列表接口统一使用游标分页，**不支持 offset**：

```
GET /documents?limit=50&cursor=eyJpZCI6...
```

`limit` 默认 20，上限 100。响应中的 `next_cursor` 为 null 表示已到末页。

## 限流

按 access_token 维度限流，默认 **每分钟 600 次**。
超限返回 429，响应头 `Retry-After` 给出建议等待秒数。
批量导入类接口单独限流为每分钟 60 次。

## 错误码

| code | HTTP | 含义 |
| ---- | ---- | ---- |
| `INVALID_ARGUMENT` | 400 | 参数校验失败，`details` 字段给出具体字段 |
| `UNAUTHENTICATED` | 401 | token 缺失或已过期 |
| `PERMISSION_DENIED` | 403 | 已认证但无权访问该资源 |
| `NOT_FOUND` | 404 | 资源不存在或对当前账号不可见 |
| `CONFLICT` | 409 | 幂等键重复或版本号冲突 |
| `RESOURCE_EXHAUSTED` | 429 | 触发限流 |
| `INTERNAL` | 500 | 服务端错误，可携带 `trace_id` 报障 |

## 幂等

所有写接口支持 `Idempotency-Key` 请求头，服务端保留该键 **24 小时**。
相同键的重复请求返回首次结果，不会重复执行。

## Webhook

事件推送使用 HMAC-SHA256 签名，签名放在 `X-Acme-Signature` 头，
计算方式为 `hex(hmac_sha256(secret, timestamp + "." + body))`。
超过 5 分钟的时间戳应当拒绝，以防重放攻击。

推送失败按 1 分钟、5 分钟、30 分钟、2 小时、6 小时重试 5 次。
