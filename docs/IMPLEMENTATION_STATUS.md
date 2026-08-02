# 订阅导入功能 — 实施进度跟踪

> 本文件是 `subscription-import-plan.md` 的实时进度看板。任何接手者都应先读本文件，再按"如何继续执行"小节介入。
>
> 维护规则：每完成一个阶段或阶段内任务，**实时更新**对应状态（不要批量补写）。状态以"代码已落地并已通过验收"为准，不以意图为准。

## 总览

| 阶段 | 名称 | 状态 | 备注 |
|---|---|---|---|
| 0 | 基线与测试样本 | ✅ 已完成 | 见下方明细，验收通过 |
| 1 | 通用节点层与身份（identity-v1） | ⏳ 待开始 | 下一步；对 `singbox_speedtest.py` 有较深重构 |
| 2 | 格式识别与协议转换 | ⏳ 待开始 | Phase 0 的 sing-box 版本契约已满足其硬门槛 |
| 3 | 单节点管理 | ⏳ 待开始 | 依赖 Phase 1/2 |
| 4 | 多订阅管理 | ⏳ 待开始 | 依赖 Phase 1/2/3 |
| 5 | UI/API 与安全 | ⏳ 待开始 | 含 LAN 认证、CSRF、会话 token；解除 `--host` 回环限制的前提 |
| 6 | 文档与审计 | ⏳ 待开始 | 含 README 最终发布说明 |

**当前分支**：`feature/subscription-import`
**基线计划**：[`subscription-import-plan.md`](./subscription-import-plan.md)（已确认决策，唯一需求事实来源）
**Phase 0 审计**：[`phase0-baseline.md`](./phase0-baseline.md)

## 阶段推进硬规则（来自计划 Section 15 / 16 / 17）

1. **逐阶段推进**，不跨阶段大范围修改；每阶段先读本阶段文件边界与验收标准。
2. **先补测试样本，再实现功能**；每阶段完成后运行回归测试。
3. 变更涉及数据模型时，**先更新迁移方案**再写代码。
4. **不得**将真实订阅 URL、Token、UUID、密码写入代码、文档、fixtures 或日志。
5. **身份算法版本化**（当前目标 `identity-v1`）；升级走 `identity-v2` 并提供迁移映射，不在运行中静默改算法。
6. **`tag` 不是唯一标识**；扫描不得按 `tag` 丢弃节点（Section 16.2）。
7. **默认监听 `127.0.0.1`**；非回环 `--host` 在 Phase 5 认证落地前必须拒绝启动（已在代码中强制）。

## Phase 0 — 已完成明细

### 交付物

| 类别 | 路径 | 说明 |
|---|---|---|
| 审计文档 | `docs/phase0-baseline.md` | sing-box 版本契约、启动基线、git 凭据审计、历史备份状态 |
| 测试基础设施 | `tests/test_baseline.py` | 22 项基线测试（导入、`make_id` 稳定性、fakes 行为） |
| Fake 工具 | `tests/fakes/fake_singbox.py` | 4 场景：ok / fail / crash / leak（含假凭据 stderr） |
| Fake 工具 | `tests/fakes/fake_downloader.py` | 7 场景：ok / http_error / redirect_to_private / timeout / oversized / gzip_bomb / interrupted |
| 脱敏样本 | `tests/fixtures/singbox/` | `full_config.json`（6 协议全）、`outbound_array.json` |
| 脱敏样本 | `tests/fixtures/karing/` | `items_servers.json`（含 `_SUB_DROP_KEYS` 元数据） |
| 脱敏样本 | `tests/fixtures/clash/` | `proxies.yaml`、`proxies.json` |
| 脱敏样本 | `tests/fixtures/uri/` | vless/vmess/trojan/shadowsocks/hysteria2/tuic + multi_line |
| 脱敏样本 | `tests/fixtures/base64/` | `multi_line_b64.txt` |
| 脱敏样本 | `tests/fixtures/html-error/` | login_page / 403 / 500 |
| 脱敏样本 | `tests/fixtures/malformed/` | broken_json / broken_yaml / garbage |
| 脱敏样本 | `tests/fixtures/sensitive/` | 脱敏红线说明 |
| 基线修复 | `singbox_speedtest.py` | 默认 bind `0.0.0.0`→`127.0.0.1`；新增 `--host`；非回环拒绝启动 |

### 关键事实（后续阶段必读）

- **sing-box 版本**：`1.13.15`（go1.26.5）。`check` 合法配置返回 0/静默；错误配置返回 1，stderr 仅含结构错误 + 本地文件路径，**不泄露凭据**。
- **schema 陷阱**：1.13.x 用顶层 `route`，**不是** `routing`。Phase 2 生成临时配置时必须用 `route`。
- 字段映射表的 `min_version=1.13.0`，`tested_version=1.13.15`。
- 临时配置文件名应随机化（Section 16.10），使 `check` stderr 暴露的本地路径无害。
- `config/`、`data/`、`bin/` 在 `.gitignore` 中，git 追踪文件数 = 0，历史从未提交真实凭据。

### Phase 0 验收结果

```
python -m unittest discover -s tests -v
→ Ran 22 tests, OK (skipped=1)
```
- 1 项跳过：`test_minimal_singbox_fixture_loadable`（可选金丝雀，skipIf 守卫生效；Phase 1 可对齐 fixture 文件名使其启用）。
- JSON fixtures 全部解析通过；`malformed/broken_json.json` 按设计抛 JSONDecodeError。
- `--host` 默认 `127.0.0.1`，非回环地址拒绝启动已强制。

### 已知遗留（不阻塞 Phase 1）

- **会话 token / CSRF**（Section 17.6）：Phase 0 只修了 bind，写操作 token 保护**未实现**。建议作为 Phase 1 的前置安全任务。
- **`0.0.0.0` LAN 访问行为变化**：README 已同步说明默认仅回环；依赖旧 LAN 行为的用户需等待 Phase 5。

## Phase 1 — 下一步范围预览

目标（计划 Section 12 阶段1 + Section 16.3 / 17.1）：

- 定义四层数据关系：`node` / `source_node` / `membership` / `membership_outbound`。
- 实现 `identity-v1`：规范化连接结构 → 固定排序 canonical JSON → 哈希 → `<protocol>:<server>:<port>#<hash>`。**不含** tag/订阅名/UUID/密码，但**包含**影响连接行为的传输/TLS/Reality/协议专属字段。
- 实现 `source_node_key-v1`（优先来源稳定 ID；弱 key 仅在无可区分字段时使用并标记 `weak=true`）。
- 把运行态主键从 `tag` 迁移到 `node_id` / `runtime_key`：`self.nodes`、`self.results`、日志、进度、测速任务、历史查询、API 选择。
- 适配现有两个扫描入口（`service_core.json` / `karing_subscribe.json`）到统一模型；旧文件只读。
- 历史迁移：为旧节点计算 identity-v1，旧 `results[tag]` 迁到 `node_id`，无法映射的写入 migration-orphan 备份（迁移前必须备份原件，幂等可重复）。

验收：同名节点不互相覆盖；改名后历史仍能关联；现有 Karing/sing-box 输入行为不回归。

## 如何继续执行（给接手者）

1. **克隆并切到本分支**：`git checkout feature/subscription-import`。
2. **读计划**：`docs/subscription-import-plan.md`（尤其目标阶段的"任务"和"验收"，以及 Section 16/17 的强制契约）。
3. **读本文件**：确认上一阶段已 ✅，再开始下一阶段；未完成阶段不得跳过。
4. **运行回归基线**：`python -m unittest discover -s tests -v`，应保持 22 项通过。
5. **遵循脱敏红线**：fixtures 仅用 RFC 保留域名 / 文档 IP / 假 UUID / 假密码（见 `tests/fixtures/sensitive/README.md`）。
6. **每完成一个任务**：实时更新本文件的对应状态与交付物表；代码涉及行为变化时同步 README（Section 17.8）。
7. **提交规范**：按阶段 + 主题拆分提交；提交信息以 `feat(phaseN)` / `test(phaseN)` / `docs(phaseN)` / `fix(phaseN)` 前缀。

## 测试入口

```bash
# 基线回归（Phase 0 起）
python -m unittest discover -s tests -v

# 校验 fixtures 完整性（排除故意损坏的 malformed/）
python -c "import json,glob; [json.load(open(f,encoding='utf-8')) for f in glob.glob('tests/fixtures/**/*.json', recursive=True) if 'malformed' not in f.replace(chr(92),'/').split('/')]; print('JSON fixtures valid')"
```
