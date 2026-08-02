# 订阅导入与格式转换实施方案

> 状态：已确认的实施计划，供后续逐步实现、审计和测试使用。本文档本身不包含代码实现，也不保存真实订阅地址、Token 或节点凭据。
>
> 版本：v1.0

## 1. 目标与已确认决策

本方案为现有 sing-box 多节点测速工具增加统一的节点输入层，使多个订阅和手动节点可以共存，并最终交给现有 sing-box 测速引擎。

已确认的默认方案：

| 项目 | 决策 |
|---|---|
| 多订阅 | 支持多个订阅同时存在，分别管理、更新和统计 |
| 重复节点 | 默认合并为一行，保留多个来源关系 |
| 分组 | 订阅自动使用订阅名称；手动节点由用户填写分组，不存在则创建 |
| 输入方式 | 订阅 URL、本地文件、分享链接、配置文本、表单 |
| 首期格式 | sing-box JSON、Clash/Mihomo YAML/JSON 的 `proxies[]`、Base64 URI、多行 URI |
| 首期协议 | VLESS、VMess、Trojan、Shadowsocks、Hysteria2、TUIC |
| 更新 | 第一版只做手动更新，不做自动定时更新 |
| 更新基准 | 以订阅最近一次成功更新内容为准 |
| 消失节点 | 从当前列表移除，测速历史保留 |
| 更新失败 | 保留旧快照和旧节点 |
| 部分解析 | 成功率大于或等于 50% 才替换旧快照 |
| 保存 | 项目目录明文保存；敏感目录纳入 Git 忽略，界面和日志脱敏 |
| Web 默认访问 | 默认监听 `127.0.0.1`；局域网访问需要另行设计认证 |

## 2. 非目标

本阶段不承诺：

- 读取 v2rayN、Karing、Shadowrocket 等客户端私有数据库；
- 转换 Clash 的规则、策略组、TUN、脚本、DNS 运行语义；
- 执行订阅返回的脚本或任意外部程序；
- 强行转换未知协议或不被当前 sing-box 版本支持的协议；
- 修改外部客户端配置；
- 第一版自动定时更新、后台服务或计划任务；
- 在未确认认证方案前开放无认证的局域网订阅管理。

完整客户端配置只提取节点部分：sing-box 的 `outbounds`、Karing 的 `items[].servers`、Clash/Mihomo 的 `proxies[]`。

## 3. 总体架构

```text
远程订阅 URL ─┐
本地文件 ──────┼─> 来源管理器 ─> 格式识别 ─> 协议转换/规范化
分享链接 ─────┤                                      │
配置文本 ─────┤                                      v
表单 ────────┘                              项目通用节点模型
                                                        │
                                     来源/节点关系与当前索引
                                                        │
                         Web UI / API ─> 现有 Ping 和测速引擎
```

边界要求：

1. 下载、识别、转换、持久化与测速分层；
2. 测速模块只接收通用节点，不判断原始格式；
3. 转换结果必须经过目标 sing-box 可接受性校验；
4. 订阅更新使用临时文件和原子替换，失败不能清空旧快照；
5. 不同来源的节点通过 membership 关系管理，不能仅靠 `tag` 去重。

## 4. 当前项目兼容边界

当前 `singbox_speedtest.py` 读取：

- `config/service_core.json` 的 `outbounds`；
- `config/karing_subscribe.json` 的 `items[].servers`；
- 目前支持的协议集合包括 VLESS、VMess、Trojan、Hysteria、Hysteria2、Shadowsocks、SSH、WireGuard、TUIC。

新输入层必须继续兼容现有两个文件，但不应把它们改造成新的持久化数据库：

- `service_core.json` 继续作为本地 sing-box 配置兼容输入；
- `karing_subscribe.json` 继续作为 Karing 兼容输入；
- 新增的订阅和手动节点使用项目自己的 `data/` 存储；
- 原有测速历史必须可继续加载和迁移。

## 5. 支持格式矩阵

### 5.1 格式识别优先级

显式 `format_hint` 优先于自动识别。自动识别建议按以下顺序：

1. JSON；
2. JSON 顶层 `outbounds`、`items[].servers` 或 `proxies[]`；
3. YAML 顶层 `proxies`；
4. 多行 URI；
5. Base64 解码后重新识别 JSON、YAML 或 URI；
6. 失败并返回明确诊断。

不要仅根据 URL 后缀判断格式。HTTP `Content-Type` 只能作为辅助信息。

### 5.2 格式支持

| 格式 | 输入特征 | 首期策略 |
|---|---|---|
| sing-box JSON | 顶层 `outbounds` 或 outbound 数组 | 支持 |
| Karing JSON | `items[].servers` | 支持，兼容现有文件 |
| Clash/Mihomo YAML | 顶层 `proxies[]` | 支持节点转换，不处理规则和策略组 |
| Clash/Mihomo JSON | 顶层 `proxies[]` | 支持节点转换 |
| 多行 URI | 每行一个支持的 scheme | 支持 |
| Base64 URI | 解码后为多行 URI | 支持 |
| 单个 URI | `vless://` 等 | 支持 |
| 其他客户端私有数据库 | 私有或加密结构 | 不支持，需先导出为上述格式 |

### 5.3 协议支持

首期转换器必须覆盖：

- VLESS：UUID、flow、TLS、SNI、Reality、WS、gRPC 及常见传输参数；
- VMess：UUID、security、alter ID 兼容、TLS、SNI、WS、gRPC 及常见传输参数；
- Trojan：password、TLS、SNI、WS、gRPC 及常见传输参数；
- Shadowsocks：method、password、server、port 及目标 sing-box 支持的插件参数；
- Hysteria2：password、TLS、SNI、obfs、up、down 等常用参数；
- TUIC：UUID、password、TLS、SNI、拥塞控制、zero RTT、heartbeat 等常用参数。

Hysteria v1 不纳入本期承诺范围。当前 sing-box 二进制不支持的字段或协议必须标记为 unsupported，而不是猜测映射。

## 6. 项目通用节点模型

所有来源最终转换成内部节点记录。`outbound` 只供后端和测速引擎使用，前端不得默认返回完整凭据。

```json
{
  "node_id": "vless:example.com:443#abc12345",
  "name": "日本东京 01",
  "type": "vless",
  "server": "example.com",
  "server_port": 443,
  "group": "机场 A",
  "source_ids": ["sub_001"],
  "source_type": "subscription",
  "outbound": {
    "type": "vless",
    "tag": "日本东京 01",
    "server": "example.com",
    "server_port": 443,
    "uuid": "<UUID>",
    "tls": {
      "enabled": true,
      "server_name": "example.com"
    }
  },
  "active": true,
  "raw_format": "vless_uri",
  "warnings": [],
  "first_seen_at": "",
  "last_seen_at": ""
}
```

### 6.1 身份字段

#### `node_id`

用于测速历史关联。沿用当前稳定身份思想：

```text
协议:服务器:端口 + 传输/TLS/Reality 结构指纹
```

不包含 tag、订阅名称、UUID 或密码，以便节点改名、订阅重排和凭据变化时尽量延续历史。

#### `source_node_key`

用于判断某个订阅更新时节点是否仍然存在。优先使用原始稳定 ID；没有时使用规范化节点内容和原始索引的组合哈希。该字段不用于公开展示。

#### membership

同一节点可被多个订阅提供，因此必须保存节点与来源的关联，而不是把来源直接写死在节点记录中。

```json
{
  "node_id": "vless:example.com:443#abc12345",
  "source_id": "sub_001",
  "source_node_key": "<HASH>",
  "active": true,
  "first_seen_at": "",
  "last_seen_at": ""
}
```

节点在一个订阅中消失但仍被另一个订阅提供时，全局节点必须继续存在。

## 7. 持久化目录与文件

建议增加以下目录，不破坏现有 `config/`、`data/` 和 `bin/`：

```text
data/
├── sources.json
├── nodes.json
├── memberships.json
├── snapshots/
│   ├── sub_001/
│   │   ├── latest.json
│   │   └── <timestamp>.json
│   └── sub_002/
├── singbox_speedtest_history.json
└── backups/
```

### 7.1 `sources.json`

```json
{
  "version": 1,
  "sources": [
    {
      "source_id": "sub_001",
      "kind": "subscription",
      "name": "机场 A",
      "input_type": "url",
      "url": "https://subscription.example.invalid/<TOKEN>",
      "format_hint": "auto",
      "group_name": "机场 A",
      "enabled": true,
      "last_update_at": "",
      "last_success_at": "",
      "last_error": "",
      "last_content_hash": "",
      "node_count": 0
    }
  ]
}
```

手动节点可归入 `kind: manual` 的来源，或由 `manual` 固定来源加节点自身分组表示。实现时应保持手动节点与订阅更新完全隔离。

### 7.2 `nodes.json`

保存当前有效的规范化节点；删除订阅节点时不能从历史文件删除对应测速记录。

### 7.3 快照

每个订阅至少保存 `latest.json`。是否保留带时间戳的历史原始响应由实现阶段确定，但默认应限制大小和数量。原始响应、标准化节点和备份都属于敏感数据。

## 8. 订阅更新和生命周期

### 8.1 成功更新

```text
下载到临时文件
  → 限制大小并识别格式
  → 解析并转换节点
  → 节点级校验
  → 成功率判断
  → 生成新快照
  → 原子替换 latest
  → 重建当前节点索引
```

成功更新后记录：

- 新增数；
- 更新数；
- 删除数；
- 保持不变数；
- 转换失败数；
- 警告数；
- 最后成功时间。

### 8.2 失败和回滚

网络错误、HTTP 错误、空响应、HTML 错误页、格式不识别、全部节点转换失败等情况，均不得覆盖旧快照。

更新失败时：

- 当前旧节点继续可测速；
- 来源状态显示失败原因；
- 保留旧 `latest.json`；
- 不把旧节点标记为消失；
- 不删除历史。

### 8.3 部分成功阈值

默认规则：

```text
可用转换节点数 / 输入节点数 >= 50%  → 允许替换
低于 50%                              → 保留旧快照
```

允许替换时，成功转换的节点进入当前列表，失败节点记录原始索引、协议、错误类别和脱敏诊断。实现应防止错误页面被识别成“空订阅”。

### 8.4 节点消失和重新出现

成功更新中未出现的节点：

- 取消对应 membership 的 active 状态；
- 从当前测速列表移除；
- 保留历史；
- 在历史/已移除节点视图中可查询。

重新出现时：

- 恢复 membership；
- 尽量复用 `node_id`；
- 继续使用历史记录；
- 记录重新出现时间。

## 9. 单节点导入流程

### 9.1 分享链接

1. 粘贴单条 URI；
2. 根据 scheme 识别协议；
3. 解析 URL 编码、Base64 和协议字段；
4. 生成通用 outbound；
5. 进行 sing-box 配置校验；
6. 选择名称和分组；
7. 预览后保存。

### 9.2 配置文本

1. 粘贴 JSON、YAML、Base64 或多行 URI；
2. 识别格式；
3. 提取节点列表；
4. 显示成功、失败和警告；
5. 勾选需要导入的节点；
6. 指定分组；
7. 保存为手动来源节点。

### 9.3 表单

表单按协议动态显示字段，不使用包含所有协议字段的超大表单。保存前必须生成与其他输入方式相同的通用节点模型，避免表单拥有独立的数据格式。

## 10. UI 和 API 规划

### 10.1 UI

新增订阅管理区域：

- 添加、编辑、删除订阅；
- 立即更新、全部更新；
- 启用/禁用；
- 节点数量、最后成功时间、错误状态；
- 更新差异；
- 订阅分组筛选。

新增单节点区域，包含“分享链接”“配置文本”“表单”三个 Tab。

节点列表增加：

- 来源/分组；
- 多来源标识；
- 手动/订阅标识；
- 转换警告；
- 当前有效状态；
- 最后出现时间。

### 10.2 API

```text
GET    /api/sources
POST   /api/sources
PUT    /api/sources/{source_id}
DELETE /api/sources/{source_id}
POST   /api/sources/{source_id}/update
POST   /api/sources/update-all
GET    /api/sources/{source_id}/diff

POST   /api/nodes/parse
POST   /api/nodes
PUT    /api/nodes/{node_id}
DELETE /api/nodes/{node_id}
GET    /api/nodes
GET    /api/nodes/removed
```

`POST /api/nodes/parse` 只做预览，不落盘。更新 API 应返回新增、更新、删除、保持、失败和警告数量，而不是只有布尔值。

测速中的节点更新必须使用锁或状态判断，避免更新过程与测速任务同时替换节点索引。

## 11. 安全边界

用户确认使用项目目录明文保存，因此以下信息允许本地明文保存，但必须按敏感数据处理：

- 订阅 URL 和 Token；
- UUID；
- 各协议密码；
- 原始订阅响应；
- 转换后的 outbound。

实现要求：

1. `data/`、订阅缓存、节点文件、备份文件全部加入 `.gitignore`；
2. 不在日志中打印完整 URL、UUID、密码或原始订阅内容；
3. 前端和普通 API 默认脱敏；
4. 临时 sing-box 配置在成功、失败、异常终止后都清理；
5. 订阅下载只允许 `http`/`https`；
6. 限制连接超时、读取超时、响应大小和重定向次数；
7. 防止重定向绕过 SSRF 检查；
8. 拒绝 `file://`、`ftp://`、`gopher://`、`data:` 等无关协议；
9. 默认拒绝回环、链路本地和私有地址，除非未来明确增加白名单；
10. Web 默认监听 `127.0.0.1`；
11. 在增加局域网访问前必须设计访问令牌或认证；
12. 删除来源时明确处理快照，但不自动删除测速历史。

## 12. 分阶段执行计划

### 阶段 0：基线和测试样本

**目标：** 固定现有行为，建立脱敏测试输入。

任务：

- 记录现有 CLI/Web 启动方式；
- 备份现有历史数据；
- 准备 sing-box、Karing、Clash、URI、Base64 的脱敏 fixtures；
- 确认实际 `sing-box.exe` 版本和 `check` 命令行为；
- 检查 Git 忽略和真实凭据是否误入仓库。

验收：原有配置可加载、可测速，基线测试可重复。

### 阶段 1：通用节点层和身份

任务：

- 定义 source、node、membership 结构；
- 将现有两个扫描入口适配到统一模型；
- 把运行态唯一键从 `tag` 改为内部稳定键；
- 兼容现有历史文件；
- 不改变现有测速协议和流程。

验收：同名节点不互相覆盖，改名后历史仍能关联，现有 Karing 和 sing-box 输入行为不回归。

### 阶段 2：格式识别和协议转换

任务：

- 实现 JSON、YAML、URI、Base64 识别；
- 实现六种协议转换器；
- 建立字段映射和不支持字段清单；
- 生成标准 outbound；
- 对转换结果执行 sing-box 校验。

验收：每种协议至少有有效、缺字段、错误字段、传输/TLS/Reality 样本；失败节点不影响其他节点。

### 阶段 3：单节点管理

任务：

- 分享链接预览与保存；
- 配置文本批量解析、选择和保存；
- 动态表单生成节点；
- 分组不存在时自动创建；
- 手动节点编辑、删除和持久化。

验收：三种方式生成相同通用结构，重启后仍存在，删除手动节点不删除历史。

### 阶段 4：多订阅管理

任务：

- `sources.json` 和快照管理；
- 多来源独立更新；
- 自动按订阅分组；
- 重复节点合并和 membership；
- 更新差异；
- 成功率阈值、原子替换、失败回滚；
- 消失和重新出现生命周期。

验收：两个以上订阅可共存；一个订阅失败不影响其他订阅；同节点跨订阅不会被错误删除；订阅节点消失后历史仍可查看。

### 阶段 5：UI/API 与安全

任务：

- 订阅管理界面；
- 单节点三个输入 Tab；
- 解析预览和错误展示；
- 已移除节点历史；
- URL 和内容大小限制；
- 默认本机监听；
- 敏感字段脱敏。

验收：测速、更新、删除、解析预览之间不会互相覆盖；敏感内容不出现在普通页面、API 和日志。

### 阶段 6：文档和审计

任务：

- 更新 README；
- 增加格式支持矩阵；
- 增加导入、备份、恢复和隐私说明；
- 复核所有新增 API、文件写入、网络访问和临时文件；
- 运行完整测试矩阵。

## 13. 测试和审计清单

### 解析测试

- sing-box 完整配置；
- outbound 数组；
- Karing `items[].servers`；
- Clash YAML/JSON `proxies[]`；
- 纯 URI 多行文本；
- Base64 URI；
- 空输入、乱码、HTML、错误 JSON/YAML；
- 重复 tag、重复节点、同名不同参数。

### 协议测试

每种协议至少覆盖：

- 最小合法配置；
- URL 编码；
- TLS/SNI；
- Reality（适用协议）；
- WebSocket；
- gRPC；
- 缺少必要凭据；
- 未知参数；
- `sing-box check` 失败。

### 生命周期测试

- 两个订阅同时存在；
- 新增、更新、删除、重新出现；
- 一个来源失败，另一个正常；
- 返回 HTML 或空内容；
- 成功率高于/低于 50%；
- 同一节点跨订阅；
- 手动节点不受订阅刷新影响；
- 删除订阅后历史保留。

### 安全测试

- 真实 Token 不进入日志、历史和普通 API；
- `.gitignore` 覆盖新增数据目录；
- 禁止非 HTTP(S) URL；
- 禁止回环、内网和重定向 SSRF；
- 响应大小和超时限制生效；
- 解析内容不执行脚本；
- 临时文件在所有退出路径清理；
- Web 默认不暴露到局域网。

### 回归测试

- 现有 `service_core.json` 可加载；
- 现有 `karing_subscribe.json` 可加载；
- 现有测速、Ping、TCP 探测、历史趋势不回归；
- CLI 参数行为保持兼容，新增参数不覆盖原参数语义。

## 14. 风险与处理原则

| 风险 | 处理 |
|---|---|
| 订阅返回错误页 | 识别 HTML/异常内容，保留旧快照 |
| 协议字段差异 | 显式字段映射，未知字段告警，不猜测 |
| sing-box 版本差异 | 以项目实际二进制校验结果为准 |
| 同名节点覆盖 | 使用 node_id/运行时唯一键，不使用 tag 作为唯一键 |
| 跨订阅删除 | 使用 membership，按来源撤销关联 |
| 凭据泄露 | 明文目录 Git 忽略、日志和 API 脱敏 |
| 流量消耗 | 导入后默认只预览，不自动全量测速 |
| 更新和测速冲突 | 状态锁、临时快照、原子替换 |
| 无认证 Web | 默认 `127.0.0.1`，局域网访问另行认证 |

## 15. 执行规则

后续实施必须按阶段推进，不跨阶段大范围修改：

1. 每阶段先阅读本阶段文件边界和验收标准；
2. 先补测试样本，再实现功能；
3. 每阶段完成后运行回归测试；
4. 变更涉及数据模型时先更新迁移方案；
5. 不将真实订阅 URL、Token、UUID、密码写入代码、文档、fixtures 或日志；
6. 代码审计重点检查输入解析、文件写入、网络请求、进程启动、敏感信息传播和并发状态；
7. 任何未支持格式都必须明确失败，不得静默转换为错误节点。

本文件是后续开发和审计的基线；若实现中需要改变已确认决策，应先更新“已确认决策”和对应验收标准，再执行代码变更。

## 16. 实施基线补充约束（优先于前文简化示例）

本节是对前文模型示例的强制解释，供实现、测试和审计使用。若前文示例与本节冲突，以本节为准。

### 16.1 默认监听地址迁移

当前代码仍可能使用 `0.0.0.0` 监听，这是待修复的现状，不是目标行为。阶段 0～4 必须默认且只能绑定 `127.0.0.1`。阶段 5 完成认证、授权、敏感字段审计和局域网安全测试前，不得开放无认证 LAN 监听。

建议新增 `--host`，默认值为 `127.0.0.1`。显式使用其他地址必须经过阶段门槛和认证设计；启动日志只显示实际绑定地址，不打印带敏感信息的 URL。

### 16.2 `tag` 不是唯一标识

扫描阶段不得按 `tag` 丢弃节点。相同名称的不同节点必须全部保留。`tag` 只用于展示和 sing-box 可读标签；内部使用：

- `node_id`：跨重启、用于历史关联的稳定身份；
- `runtime_key`：本次运行中唯一的任务/结果键，可由 `node_id` 与 membership 上下文派生。

`self.nodes`、结果、日志、进度、测速任务、历史查询和 API 选择必须迁移到 `node_id`/`runtime_key`。旧的 `tags` 请求参数只能作为明确的过渡兼容层，不能继续作为内部主键。

### 16.3 四层数据关系

全局 `node` 不得用单值 `source_id`、`group` 或 `outbound` 表示多来源事实。实现必须区分：

1. `node`：跨来源合并后的逻辑身份和公共展示信息；
2. `source_node`：某来源中的原始条目及 `source_node_key`；
3. `membership`：节点、来源、来源分组之间的关系；
4. `membership_outbound`：该来源关系下实际使用的 outbound、版本、哈希和警告。

同一 `node_id` 的多个 membership 必须全部保留。若其 outbound 不同，必须按 membership 追踪并以确定性排序选择运行实例，记录冲突，不得静默覆盖。节点公共 API 应返回 `sources[]`、`groups[]` 等集合字段。

`node_id` 的 identity 算法必须版本化，并覆盖会影响连接行为的协议、服务器、端口、传输、TLS、Reality 及协议专属结构字段。历史稳定身份可以不包含凭据，但不能因此忽略未纳入指纹的传输参数。

### 16.4 更新事实来源与事务

一次订阅更新涉及 payload、normalized 节点、membership、source 状态和派生索引，不能只对 `latest.json` 做原子替换。每次更新必须有 `update_id` 和 manifest，至少记录：schema/version、source_id、各文件哈希、候选数、成功数、重复数、失败数、阈值/骤降决策和提交状态。

推荐事实来源：原始响应为版本化 payload；解析结果为 normalized；来源当前 membership 为当前 committed manifest；全局 `nodes.json` 是可重建的派生索引；`sources.json` 保存来源配置和状态。

更新应先写新版本目录并标记 `prepared`，完整校验后原子切换 current pointer，再标记 `committed`。进程中断时启动恢复只能选择完整 committed 版本；未完成版本隔离或删除，不能暴露给测速/API。派生索引损坏时必须能从 committed manifest 重建，旧有效版本不能被半成品覆盖。

### 16.5 50% 阈值精确定义

```text
candidate_count = 输入中被识别为节点条目的条目数（去重前）
supported_count = 成功解析、规范化、必填字段校验并通过 sing-box check 的条目数
failed_count = candidate_count - supported_count
```

不支持协议、缺字段、格式错误、校验失败均计入失败；重复条目仍计入候选数并单独统计。`candidate_count == 0`、空响应、空节点数组、HTML/登录页、JSON 错误对象都直接判定更新失败，不能替换旧快照。只有 `supported_count / candidate_count >= 0.5` 才具备替换资格。

还必须有异常骤降保护：新成功数低于旧成功数的 50%，或结果明显变成单一异常模板/协议时，标记 `suspicious`，默认保留旧快照并要求显式确认。所有判断写入更新结果和 manifest。

### 16.6 安全下载器契约

订阅下载器只允许绝对 `http`/`https` URL，拒绝用户信息、非必要 scheme、超长 URL 和默认环境代理。默认不使用 `HTTP_PROXY`、`HTTPS_PROXY`、`ALL_PROXY` 等环境变量。

每次初始请求和重定向都必须重新解析 DNS 并检查实际 IPv4/IPv6 连接目标，拒绝 loopback、private、link-local、multicast、unspecified 和保留地址；默认最多 3 次重定向且禁止 HTTPS 降级。必须独立限制 DNS、连接、TLS、读取、总时长、响应字节数、解压后字节数和重定向次数，读取必须流式计数。

压缩只支持明确允许的编码，并在压缩前后限制大小，防止解压炸弹。下载内容不执行脚本。实现阶段必须为 DNS rebinding、IPv4-mapped IPv6、私网重定向、无 Content-Length、gzip 超限和环境代理编写测试。

### 16.7 格式依赖和映射交付物

Clash/Mihomo YAML 必须使用安全加载器，明确依赖及最低版本并写入项目依赖文件；缺少依赖必须返回可识别错误。Base64 只在 JSON/YAML/URI 直接识别失败后尝试，最多解码一层，允许的标准/URL-safe 变体、padding、编码前后大小上限必须写入测试；解码失败不能当成空订阅。

阶段 2 必须交付六种协议的字段映射表，标明目标字段、类型、是否必需、默认值、转换方式、不支持行为、警告/错误类别和对应 fixture。`sing-box check` 通过只代表结构可接受，不代表完整保持原客户端语义；丢失字段必须报告。

### 16.8 Legacy 兼容矩阵

现有 `service_core.json` 和 `karing_subscribe.json` 中已支持的 SSH、WireGuard、Hysteria 等节点必须通过回归测试保持原有本地配置加载行为；这不等于它们都属于新订阅转换器的首期承诺。实现必须在协议矩阵中分别标记 `legacy-compatible` 与 `new-import-supported`。Hysteria v1 首期明确为新导入 `unsupported`，不得静默转换。

### 16.9 阶段 0 测试基础设施

阶段 0 必须建立可重复的测试入口和脱敏 fixtures，例如：

```text
tests/fixtures/{singbox,karing,clash,uri,base64,html-error,malformed,sensitive}/
tests/fakes/{fake_downloader.py,fake_singbox.py}
```

在项目实际环境中固定测试命令（至少支持 `python -m unittest discover -s tests -v`，若选择 pytest 则必须在依赖和文档中统一）。fake downloader 要覆盖正常、HTTP 错误、重定向、私网 DNS、超时、超大响应、gzip 和中断；fake sing-box 要覆盖 check 成功/失败、异常退出及敏感 stderr。所有 fixture 只能使用保留域名、文档 IP、假 UUID 和假密码。

### 16.10 敏感字段传播和失败处理

订阅 URL/Token、UUID、password、原始响应、normalized outbound、临时配置和备份均属于敏感数据。允许项目目录明文保存，但必须：

- 数据、快照、备份全部 Git 忽略；
- 普通 API、UI、日志、历史不返回完整凭据、URL 或 outbound；
- 历史只保存必要的测速结果和脱敏节点快照；
- 临时文件使用随机名，并在成功、失败、check 失败、启动失败、进程终止和异常路径通过 `finally` 清理；
- 写入、rename、磁盘空间或权限失败时事务不得提交，旧状态必须保留；
- 持久化失败必须返回明确错误，不得静默吞掉；
- 启动时只清理可确认的孤儿临时文件，不删除未确认的 committed 版本。

阶段验收还必须覆盖：同 tag 不丢失、跨订阅 membership 删除、同 identity 不同 outbound、各事务阶段中断恢复、阈值边界与骤降、SSRF/DNS/IPv6/压缩、默认 bind、历史迁移、API/日志/备份脱敏和临时文件清理。

## 17. 实施前确定性契约

本节消除身份、迁移、删除、API 和版本兼容方面的实现分歧。

### 17.1 identity v1

`node_id` 使用版本化算法 `identity-v1`。其输入是规范化后的连接结构，不包含 `tag`、订阅名、UUID、密码或其他账号凭据；但包含会改变连接行为的非凭据字段：

```text
protocol
server（小写、规范化域名/IP）
server_port
transport.type/path/service_name/server_name
transport.headers/early_data 等实际生效字段
tls.enabled/server_name/insecure/alpn/utls 指纹
reality.enabled/public_key/short_id
协议专属非凭据字段：flow、security、method、obfs 类型、TUIC 拥塞控制等
```

字段按固定排序递归规范化：对象键字典序排序；字符串保持协议语义所需大小写，域名小写；列表只在协议声明无序时排序；空值和默认值按映射表统一；未知会影响连接的字段不得静默忽略，而应进入 warning 并阻止合并。最终对 canonical JSON 做哈希，形成：

```text
<protocol>:<server>:<server_port>#<identity-v1-hash>
```

凭据变化默认不改变 `node_id`，但必须生成新的 `membership_outbound.version` 和 content hash。若同一 `node_id` 下凭据不同，仍保留为不同 membership outbound，不得把一个凭据覆盖另一个凭据。identity 算法升级使用 `identity-v2`，提供迁移映射和旧 ID 保留策略，不在运行中静默改变算法。

`source_node_key-v1` 优先使用来源稳定 ID；没有稳定 ID 时对去除名称、来源元数据后的 canonical source entry 做哈希。不得默认把原始索引加入 key；只有来源条目完全无可区分字段时才使用索引，并标记 `weak=true`。因此节点重排不应改变强 key。

本规则替代第 6.1 节中关于“规范化节点内容和原始索引组合哈希”的简化描述：原始索引不得作为默认输入。`weak` 必须持久化在 `source_node`，更新时优先按强 key 匹配；弱 key 只能在来源条目无任何可区分字段时使用，并在差异报告中提示可能发生误匹配。

`membership_id = hash(source_id + ":" + source_node_key-v1)`。同一 `source_id`、同一 `node_id` 下，不同 `source_node_key` 必须允许存在多个 membership；`membership_outbound` 以 `membership_id` 为唯一父级。更新时按 `source_node_key` 匹配和替换，不能按 `node_id` 或 `tag` 覆盖。来源内相同 identity 的多个条目也必须分别保存并报告冲突。

### 17.2 同一 node 的 outbound 选择

当前运行实例使用 `runtime_key = node_id + membership_id + outbound_version`，因此同一逻辑 node 的不同来源配置不会互相覆盖。默认合并展示不等于强制只启动一个 outbound。

测速选择规则：

1. 用户从来源详情明确选择 membership 时，使用该 membership outbound；
2. 未指定 membership 时，按 `source_priority`（默认创建顺序）、`source_id`、`membership_id` 字典序确定性选择；
3. 被选择 outbound 启动或校验失败时，不自动用另一个凭据重试并伪装成同一结果；可记录失败，并由用户选择其他来源重测；
4. 结果保存使用 `runtime_key`，历史归档到公共 `node_id`；结果中记录来源和 outbound version；
5. 同一来源内的 outbound 冲突不丢弃，必须显示冲突诊断。

### 17.3 旧配置和历史迁移

来源优先级固定为：显式导入 source > 已登记的新订阅 source > 兼容读取的 Karing `items[].servers` > 兼容读取的 `service_core.json`；同一 `source_id` 不重复导入。旧文件只读，不被新系统改写。

迁移步骤：

1. 为每个旧节点计算 `identity-v1`；
2. 生成旧 tag 到候选 node_id 的映射；
3. 一个 tag 对应多个 node_id 时全部保留，并生成迁移冲突报告；
4. 多个旧 tag 对应一个 node_id 时合并历史到 node_id，并保留原 tag 来源信息；
5. 旧 `results[tag]` 按当前节点的 node_id/runtime_key 转换；无法唯一映射的结果不删除，写入 migration-orphan 备份；
6. 旧 API 的 `tags` 只在过渡期解析为候选 node_id，多个匹配时返回歧义，不自动选择；
7. 迁移前备份原历史，迁移后可重复执行且幂等。

### 17.4 本地文件输入

本地文件属于 `kind: local_file` source，与 URL 订阅和手动 source 分开。首期支持用户通过显式路径导入，不支持 Web 请求任意路径：

- CLI 可使用 `--import-file` 或等价参数；
- Web API 只允许上传到受限临时目录，禁止前端提交任意服务器路径；
- 文件大小、扩展名和内容类型均需校验；
- 导入后保存标准化快照，默认不持续监控原文件；
- 用户再次点击“重新读取”才更新该 source；
- 本地文件 source 失败时沿用上次成功快照；
- 原始文件是否复制到快照目录由用户显式选择，默认只保存标准化结果。

配置文本解析只做预览，确认保存后创建 `kind: manual` source 或 manual membership；表单也必须走同一规范化管线。

### 17.5 来源删除、恢复和快照保留

删除 source 是事务操作：先将其所有 membership 标记 inactive，再重建全局节点索引；其他 source 或手动 membership 仍存在的 node 不删除。测速历史永不因删除 source 自动删除。

默认删除策略：

- 立即从当前节点列表移除 source membership；
- 删除 source 配置、current pointer、normalized 快照和原始 payload；
- 同步删除该 source 的未提交临时目录；
- 保留脱敏的删除审计记录和 node_id 关联，使历史节点可查询；
- 不提供默认恢复；若未来支持恢复，必须从用户备份重新导入；
- 删除失败时保留完整旧 source，不执行半删除。

删除审计记录仅允许保存：`event_id`、`source_id`、脱敏来源名称、操作时间、删除结果和节点数量；不得保存 URL、Token、UUID、密码、完整 `source_node_key`、完整 server/domain、完整 outbound 或原始响应。审计记录保留 30 天后清理，存放在已 Git 忽略的数据目录中。

更新期间的历史快照默认只保留最近 3 个成功版本，且受总大小上限约束；删除 source 时按上述规则清理，清理失败必须告警。历史测速记录独立保留。

### 17.6 API、CSRF 和绑定门槛

本机默认监听不等于不做边界控制。阶段 0～4：

- 只允许 `127.0.0.1`；
- 不接受来自其他地址的连接；
- 写操作要求启动会话 token（存于本地权限受限文件或启动时显示），UI 请求携带该 token；
- 写 API 检查 `Origin`/`Referer` 或使用同源随机 CSRF token；
- 普通 API 永不返回完整 URL、凭据、原始响应或 outbound；
- 删除、更新和导入返回明确的操作结果，不返回堆栈。

传入非回环 `--host` 在阶段 5 之前必须拒绝启动。阶段 5 若开放 LAN，必须增加认证、CSRF/Origin 防护、敏感字段权限、失败限速和安全测试；否则不提供 LAN 模式。

启动会话 token 不是每个请求只能使用一次：每次进程启动生成至少 256-bit 随机 token，写入仅当前用户可读的本地文件，或仅在控制台一次显示；浏览器通过显式初始化流程取得后，在同一启动会话中使用。所有 POST/PUT/DELETE 端点，包括现有 `/api/config`、`/api/reload`、`/api/ping`、`/api/test`、`/api/stop`，都必须统一要求 `Authorization` 或同等 CSRF 保护。缺失/错误 token 返回 401，来源校验失败返回 403；token 文件权限不合格时拒绝启动写 API。未来若开放 LAN，必须改为正式认证，不得仅依赖本机 token。

### 17.7 sing-box 版本契约

阶段 0 是进入阶段 2 的硬门槛：必须先确定最低目标版本、测试版本、可执行文件来源、校验命令，并固定该二进制（项目随附或由测试环境明确提供）作为 fixture 校验基线。未完成这些确认不得实现或验收阶段 2。字段映射表增加 `min_version` 和 `tested_version` 列：

- 低于最低版本：启动时报告不兼容，不生成可能错误的配置；
- 字段仅在较新版本可用：节点标记 unsupported/version-mismatch；
- `sing-box check` 以实际使用的目标版本为准；
- 不承诺不同版本之间完全兼容，升级核心时必须重新运行字段和协议测试。

### 17.8 本计划与 README 的关系

当前 README 描述的是现状，包括旧的局域网访问和仅 `service_core.json` 输入行为；在阶段 5/6 完成前，不应把目标功能写成已实现。实施每个阶段后同步更新对应 README 章节：

- 阶段 1：更新默认 bind、旧输入兼容和 API 标识；
- 阶段 2～4：按实际已实现格式更新支持矩阵；
- 阶段 5：只在认证和安全验收完成后说明 LAN 访问；
- 阶段 6：发布最终导入、备份、删除和隐私说明。
