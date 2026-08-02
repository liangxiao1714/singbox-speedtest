# Phase 0 基线审计报告

> 范围：`docs/subscription-import-plan.md` 第 12 节「阶段 0」与第 17.7 节「sing-box 版本契约」的硬门槛确认。
>
> 性质：只读审计。本文件为本次审计唯一产物；审计过程中未读取或外泄 `config/`、`data/` 下任何文件内容。
>
> 生成时间：2026-08-03（基于工作区当前快照）。

---

## 1. sing-box 版本契约（第 17.7 节硬门槛）

### 1.1 实际二进制版本

命令：

```powershell
& "D:\JolonWorks\singbox-speedtest\bin\sing-box.exe" version
```

完整输出：

```text
sing-box version 1.13.15

Environment: go1.26.5 windows/amd64
Tags: with_gvisor,with_quic,with_dhcp,with_wireguard,with_utls,with_acme,with_clash_api,with_tailscale,with_ccm,with_ocm,with_naive_outbound,with_purego,badlinkname,tfogo_checklinkname0
Revision: 3708fa18766cda1f11b77f6ed9c7bd61688f17df
CGO: disabled
```

| 字段 | 值 |
|---|---|
| 版本字符串 | `sing-box version 1.13.15` |
| 主次版本 | `1.13` |
| 补丁 | `15` |
| 运行时 | `go1.26.5 windows/amd64` |
| 修订号 | `3708fa18766cda1f11b77f6ed9c7bd61688f17df` |
| CGO | `disabled` |
| 来源 | 项目随附 `bin/sing-box.exe`（45,437,440 字节），同目录附带 `libcronet.dll`、`sing-box-LICENSE` |

编译 Tags 含 `with_wireguard`、`with_utls`、`with_quic`、`with_clash_api`，覆盖计划首期六协议（VLESS/VMess/Trojan/Shadowsocks/Hysteria2/TUIC）所需的 TLS/Reality/utls/quic 能力。

### 1.2 `check` 命令行为

测试在系统临时目录构造最小配置后执行（不引用任何真实节点）。

**用例 A：合法配置（inbounds:[] + direct/block outbound + route:{}）**

```text
$ & "bin/sing-box.exe" check -c <valid.json>
（无任何 stdout/stderr 输出）
EXIT_CODE = 0
```

**用例 B：JSON 语法错误**

```text
$ & "bin/sing-box.exe" check -c <invalid.json>
FATAL[0000] decode config at <路径>: invalid character 'B' after object key:value pair: row 1, column 85
EXIT_CODE = 1
```

**用例 C：合法 JSON 但含未知顶层字段（结构错误）**

注入顶层 `"routing": {}`（旧字段名）：

```text
FATAL[0000] decode config at <路径>: routing: json: unknown field "routing"
EXIT_CODE = 1
```

### 1.3 关键观察

1. **退出码语义明确**：合法配置退出码为 `0` 且静默；任何错误退出码为 `1`。可作为阶段 2 节点级校验的硬门槛（`EXIT_CODE == 0` 即通过）。
2. **错误信息为结构级，不回显凭据**：`check` 的 stderr 仅包含字段路径、JSON 解析错误、行列号和被校验文件的本地路径。**不会回显 server、UUID、password 等节点值**，符合第 16.10 节「敏感字段不传播」要求。
3. **stderr 会回显配置文件本地路径**：临时配置路径会出现在 `decode config at <path>` 中。实现时临时文件应使用随机名（第 16.10 节），路径本身不含敏感信息即可接受；不可将含凭据的固定文件名作为路径暴露。
4. **PowerShell 调用注意**：sing-box 将错误写入 stderr，PowerShell 会将其包装为 `RemoteCommandError`；Python `subprocess` 直接捕获 stderr 不受影响，实现应以 Python 管道为准。
5. **Schema 版本敏感（重要）**：sing-box `1.13.x` 拒绝旧顶层字段 `routing`，正确字段名为 `route`。字段映射表必须以本二进制实际接受性为准（第 14 节风险表：「sing-box 版本差异 — 以项目实际二进制校验结果为准」）。阶段 2 生成临时配置时必须使用 `1.13.x` schema。

### 1.4 字段映射表建议值（供阶段 2 消费）

| 列 | 建议值 | 依据 |
|---|---|---|
| `min_version` | `1.13.0` | 项目随附二进制主次版本；低于此版本 schema（如 `routing`）与字段语义可能不兼容 |
| `tested_version` | `1.13.15` | 项目实际随附、本次通过 `check` 验证的二进制版本 |

> 未通过本节确认不得进入阶段 2（第 17.7 节硬门槛）——本节确认完成，门槛达成。

---

## 2. 启动方式基线

### 2.1 当前启动方法（来自 `README.md` 与 `singbox_speedtest.py`）

| 模式 | 命令 | 说明 |
|---|---|---|
| Web 界面（默认） | `python singbox_speedtest.py` | 启动 `ThreadingHTTPServer`，浏览器访问 |
| 命令行模式 | `python singbox_speedtest.py --cli [--filter 关键字]` | 不启动 Web，直接打印 Ping + 测速排名 |

### 2.2 命令行参数与默认值（`parse_args()`，源码第 215–228 行）

| 参数 | 默认值 | 说明 | 源码 |
|---|---|---|---|
| `--core` | `None`（自动定位） | `service_core.json` 路径 | 217 |
| `--subscribe` | `None`（自动定位） | `karing_subscribe.json` 路径 | 218 |
| `--config-dir` | `None`（项目 `config/`） | 配置目录 | 219 |
| `--history` | `None`（项目 `data/singbox_speedtest_history.json`） | 历史文件路径 | 221 |
| `--singbox` | `None`（自动查找：`bin/` → PATH → v2rayN 常见路径） | sing-box 可执行路径 | 222 |
| `--bytes` | `10485760`（10 MB） | 测速下载字节数 | 223 |
| `--timeout` | `25` | 单节点测速超时（秒） | 224 |
| `--port` | `8088` | Web 端口 | 225 |
| `--cli` | `False`（标志） | 启用命令行模式 | 226 |
| `--filter` | `""` | 节点名筛选（仅 CLI） | 227 |

模块级常量（源码第 41–53 行）：`DEFAULT_DL_BYTES = 10 * 1024 * 1024`（第 50–51 行重复赋值，最终生效为 10 MB）、`DEFAULT_TIMEOUT = 25`、`BASE_PORT = 19000`（临时实例本地端口起始）。

### 2.3 ⚠️ Phase 0 基线违规：监听地址绑定 `0.0.0.0`

**违规点**（`singbox_speedtest.py` 第 1326 行）：

```python
srv = ThreadingHTTPServer(("0.0.0.0", args.port), Handler)
```

并在第 1327–1328 行额外打印本机局域网 IP，引导用户从局域网访问：

```text
✓ Web 界面:
  http://127.0.0.1:8088
  http://<本机LAN_IP>:8088
```

**与计划的冲突**：

- 第 16.1 节：「阶段 0～4 必须默认且只能绑定 `127.0.0.1`」；
- 第 17.6 节：「阶段 0～4 只允许 `127.0.0.1`」「传入非回环 `--host` 在阶段 5 之前必须拒绝启动」；
- 第 11 节第 10 条：「Web 默认监听 `127.0.0.1`」。

**当前代码现状**：无 `--host` 参数；硬编码绑定 `0.0.0.0`，无认证、无 CSRF、无 Origin 校验、无启动会话 token，且主动暴露局域网访问入口。这是已知的现状违规（第 16.1 节明确：「当前代码仍可能使用 `0.0.0.0` 监听，这是待修复的现状，不是目标行为」）。

**修复方向（阶段 1 落地，本审计仅记录）**：新增 `--host` 默认 `127.0.0.1`；阶段 5 前拒绝非回环 `--host`；启动日志不再打印带敏感信息的 LAN URL；按第 17.6 节为所有写端点（`/api/config`、`/api/reload`、`/api/ping`、`/api/test`、`/api/stop` 等）增加会话 token / CSRF。

---

## 3. Git 追踪与凭据审计

### 3.1 追踪状态

| 检查 | 命令 | 结果 |
|---|---|---|
| 敏感目录是否被追踪 | `git ls-files config/ data/ bin/` | **空**（全部 gitignored） |
| 历史/备份/日志是否被追踪 | `git ls-files \| findstr /I "history .bak .log"` | **空** |
| 历史中是否曾提交 `config/*.json` 或 `data/*.json` | `git log --all --diff-filter=A --name-only` 过滤 | **空**（从未提交） |

仓库当前追踪的全部文件（共 5 个，无任何敏感文件）：

```text
.gitignore
README.md
requirements.txt
screenshot.png
singbox_speedtest.py
```

### 3.2 `.gitignore` 覆盖（第 31 行，已覆盖全部敏感面）

- `bin/`（第 27 行）— sing-box 二进制与 libcronet.dll；
- `config/`（第 30 行）— `service_core.json`、`karing_subscribe.json`（含真实节点凭据）；
- `data/`（第 31 行）— 测速历史与备份；
- `*.bak`、`*.log`、`*.tmp`（第 13–15 行）— 临时/备份/日志；
- `singbox_speedtest_history.json`（第 10 行）— 历史文件名兜底。

> 注：工作树当前 `.gitignore` / `README.md` / `singbox_speedtest.py` 处于已修改状态，`docs/`、`tests/` 为未追踪（由本审计与并行测试基础设施任务产生）。未引入任何敏感文件。

### 3.3 结论

**真实凭据未泄露进仓库。** `config/`（含真实 UUID/密码/订阅 URL 的两个文件，约 79 KB + 104 KB）、`data/`（历史与备份，约 38 KB + 20 KB）、`bin/`（约 45 MB 二进制）均未被追踪且历史中从未出现。第 11 节第 1 条「`data/`、订阅缓存、节点文件、备份文件全部加入 `.gitignore`」已满足。

---

## 4. 历史备份状态（第 17.3 节迁移前置）

`data/` 目录实际内容（仅报告存在性与大小，不读内容）：

| 文件 | 存在 | 大小（字节） |
|---|---|---|
| `singbox_speedtest_history.json` | ✅ | 38,494 |
| `singbox_speedtest_history.json.v1.bak` | ✅ | 20,007 |

**`.v1.bak` 已存在**，符合第 17.3 节「迁移前必须备份原历史，迁移后可重复执行且幂等」的前置要求。该备份由源码 `_migrate_history_keys()` 通过 `shutil.copy2(self.history_path, self.history_path + ".v1.bak")` 生成（第 317 行），在历史关联键迁移（旧 `tag`-keyed → 新 `stable_id`-keyed）前执行。当前迁移路径：

1. `_migrate_history_location`（第 275 行）：旧位置（配置同目录）→ `data/`，原件保留；
2. `_migrate_history_keys`（第 289 行）：先 `shutil.copy2` 生成 `.v1.bak`，再迁移；无法匹配的旧 tag 记录写入 `.orphan.bak`（第 309 行）。

> 迁移幂等性已具备基础（先备份再改写）；后续阶段 1 引入 `identity-v1` 时应复用同一备份约定，避免覆盖现有 `.v1.bak`。

---

## 5. Phase 0 验收对照

对照第 12 节「阶段 0」任务与第 16.9 节「测试基础设施」要求：

| # | 验收项 | 来源 | 状态 | 说明 / 证据 |
|---|---|---|---|---|
| 1 | 记录现有 CLI/Web 启动方式 | 第 12 节 | ✅ 完成 | 见本文件第 2 节；已记录全部参数默认值与 `0.0.0.0` 违规 |
| 2 | 备份现有历史数据 | 第 12 节 / 17.3 | ✅ 完成 | `data/*.v1.bak` 存在（20,007 字节） |
| 3 | 确认 sing-box 版本与 `check` 行为 | 第 12 节 / 17.7 | ✅ 完成 | 见第 1 节；`1.13.15`，`check` 退出码 0/1 语义明确 |
| 4 | 检查 Git 忽略与真实凭据是否误入仓库 | 第 12 节 / 11 | ✅ 完成 | 见第 3 节；config/data/bin 全部未追踪，历史无泄露 |
| 5 | 准备 sing-box/Karing/Clash/URI/Base64 脱敏 fixtures | 第 12 节 / 16.9 | ⏳ 进行中 | 并行任务；`tests/fixtures/` 尚未建立 |
| 6 | 建立可重复测试入口（`python -m unittest discover -s tests -v`） | 16.9 | ⏳ 进行中 | `tests/__init__.py`、`tests/fakes/fake_singbox.py`、`tests/fakes/fake_downloader.py` 已就位（并行任务） |
| 7 | fake downloader / fake sing-box 覆盖正常/错误/重定向/私网/超时/超大/gzip/中断及 check 成功/失败/敏感 stderr | 16.9 | ⏳ 进行中 | 并行任务，本审计未覆盖 |

**本审计覆盖第 12 节阶段 0 的第 1/2/3/4 项与第 17.7 节硬门槛**；第 5/6/7 项（fixtures 与测试基础设施）由其他 agent 并行推进，状态记录如上。

### 残余风险与待办（不阻塞进入阶段 1/2，但需在对应阶段关闭）

- **[高] `0.0.0.0` 监听违规（第 16.1 节）**：阶段 1 必须改为默认 `127.0.0.1` 并新增 `--host`，阶段 5 前拒绝非回环绑定。详见第 2.3 节。
- **[中] sing-box schema 版本绑定 `1.13.x`**：阶段 2 生成的临时配置必须使用 `route`（非 `routing`）等新字段名；字段映射表 `min_version=1.13.0`、`tested_version=1.13.15`，升级核心二进制后必须重跑协议/字段测试（第 17.7 节）。
- **[中] `check` stderr 回显本地配置路径**：阶段 2 临时配置须用随机文件名（第 16.10 节），避免固定名暴露用户目录结构。
- **[低] `DEFAULT_DL_BYTES` 重复赋值**：源码第 50–51 行连续两次赋值（20 MB → 10 MB），功能上以最后一次为准，但属可读性隐患，建议阶段 1 清理。
- **[低] 历史迁移幂等性**：当前 `_migrate_history_keys` 每次启动都 `copy2` 覆盖 `.v1.bak`；阶段 1 引入 `identity-v1` 前应确认是否会用新备份覆盖旧备份。

---

> 本报告不含任何真实 UUID、密码、服务器地址或订阅 URL。所有路径、版本号、退出码均为环境事实，可直接作为后续阶段基线。