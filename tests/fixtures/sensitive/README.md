# 敏感数据脱敏说明（fixtures 红线）

本目录及整个 `tests/fixtures/` 树用于存放**脱敏测试样本**，供格式识别、协议转换、生命周期和安全测试使用。

## 红线：什么属于敏感数据

下列信息允许在项目目录明文保存（用户已确认），但**严禁**进入本仓库、日志、普通 API、UI 或 fixtures（除下方允许的假值外）：

- 订阅 URL 及其中的 Token / 用户 ID；
- 各协议 UUID / 密码 / 密钥（VLESS UUID、VMess id、Trojan password、Shadowsocks method 密钥、Hysteria2 password、TUIC password 等）；
- 原始订阅响应正文；
- 转换后的 `outbound` 完整内容（含凭据）；
- 临时 sing-box 配置、备份文件、带时间戳的快照。

## 允许使用的假值（fixtures 必须仅使用这些）

| 类别 | 允许值 | 依据 |
|---|---|---|
| 域名 | `example.com` `example.org` `example.net` `sub.example.com` | RFC 2606 |
| 占位 URL | `https://subscription.example.invalid/<TOKEN>` | RFC 2606 `.invalid` |
| IPv4 | `203.0.113.0/24`、`198.51.100.0/24` | RFC 5737 |
| IPv6 | `2001:db8::` | RFC 3849 |
| UUID | `00000000-0000-0000-0000-000000000000`、`11111111-1111-1111-1111-111111111111` | 明显为假 |
| 密码 | `password`、`test-password`、`fake-secret-123`、`obfs-password`、`dummy` | 明显为假 |
| 端口 | 443、80、8443、20000-20010 | 常见/高端口 |

## 测试用途

- Phase 5 安全测试应断言：真实订阅 URL、UUID、password、原始响应、完整 outbound **不会**出现在普通 API 响应、日志输出、历史文件或前端可读位置。
- `tests/fakes/fake_singbox.py` 的 `scenario="leak"` 提供一个**含假凭据**的 stderr 样本（零 UUID + `fake-secret-123`），用于验证调用方不会原样记录 sing-box stderr。
- 任何 fixture 不得包含真实服务器地址、真实订阅 Token 或看起来像真实凭据的字符串。
