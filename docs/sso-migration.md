# Health 原生 OIDC 接入

Health 与 Garden、Stock 一样，使用 Shadow Identity 的唯一 issuer。Health 运行在 NAS，
但浏览器登录由云端 Identity 完成；不复制 Authelia PostgreSQL，也不在 NAS 部署第二个
身份签发方。

```text
Browser -> Health /login -> Shadow Identity
        <- code ----------
Browser -> Health /auth/callback
Health  -> Identity /token（code + PKCE verifier）
Health  -> 校验签名、issuer、audience、nonce、iat、groups
Browser <- Host-only HttpOnly opaque session cookie
```

安全约束：

- 只允许 Authorization Code + PKCE S256；
- callback、post logout URI 必须精确登记且使用 HTTPS；
- OIDC Token 仅在回调中使用，不写浏览器存储、日志或业务数据库；
- 浏览器只保存随机 Session handle，数据库只保存其 SHA-256；
- 身份稳定键为 `(issuer, subject)`；
- `health-users` 在应用内校验；
- Android、BLE、Agent、MCP 和提醒继续使用各自 Bearer，不混用浏览器 Session；
- Nginx 清空 `Remote-User`、`Remote-Groups` 等历史代理身份头。

生产切换顺序：登记 `shadow-health` client、写入受限 secret 文件、部署应用、验证 callback，
最后移除 Nginx `auth_request`。紧急回滚只能整体回滚上一个制品；
`SHADOW_AUTH_MODE=legacy-forward` 仅用于短时回滚窗口，不作为长期双登录模式。
该模式现在还必须设置带时区的 `SHADOW_LEGACY_FORWARD_UNTIL`，且截止时间不得超过启动时刻
72 小时；到期后身份验证、登录入口和 `readyz` 都会失败，必须回到 OIDC 或发布新的受控回滚。
