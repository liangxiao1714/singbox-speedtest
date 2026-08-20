# AGENTS.md

单文件 Python 工具：`singbox_speedtest.py`（~1100 行）= 参数解析 + 测速引擎 + 内嵌 Web UI 三合一。基于 sing-box 的多节点批量测速。

## 架构（不从文件名看出来的事实）

- **前端内嵌在 Python 里**：`HTML = r"""..."""`（约 653 行起）包含全部 HTML/CSS/JS，无构建步骤。改前端 = 改这个字符串；注意它是 raw 字符串，内部不能出现 `"""`。
- **测速引擎是全局单例状态机**：`SpeedTest` 实例挂在 `Handler.tester` 类变量上；状态 idle/pinging/testing/done/stopping；测速串行执行，前端靠轮询 `/api/state` 刷新。
- **API**：GET `/`、`/api/init`、`/api/state`、`/api/config`、`/api/history?tag=`；POST `/api/config`、`/api/ping`、`/api/test`、`/api/stop`。Web 绑定 `0.0.0.0`（局域网可见）。
- **节点数据是读取时快照**：从本地 sing-box 配置文件静态读取（`_load()`，约 142 行，解析 `outbounds` 数组），不从网络拉订阅。改配置文件后需重启工具。

## 命令

```bash
# 唯一的快速自动验证（无测试/lint/CI，工具链为零）
python3 -m py_compile singbox_speedtest.py

# Web 模式（默认 http://127.0.0.1:8088）
python3 singbox_speedtest.py

# CLI 模式
python3 singbox_speedtest.py --cli --filter 日本

# 显式指定配置和 sing-box（WSL/Linux 下通常必须，见下）
python3 singbox_speedtest.py --core /path/to/service_core.json --singbox /path/to/sing-box
```

真实功能验证需要可用的代理节点配置，无法离线测试——改动后至少跑 `py_compile` + 启动到 Web 界面确认无报错。

## 运行前提与陷阱

- **零第三方依赖**：`requirements.txt` 只是注释性说明，不需要 pip install。
- **sing-box 查找顺序与 README 描述相反**：代码是 `args.singbox or find_singbox()`——`--singbox` 参数优先，其次 PATH，最后 v2rayN Windows 固定路径。
- 仓库内 `bin/sing-box.exe` 是本地开发文件，**未被 git 追踪也不会被自动发现**（`find_singbox()` 不查 `bin/`），需 `--singbox bin/sing-box.exe` 显式指定。**不要提交它**（README 明确仓库不含二进制）。
- 默认配置路径基于 `%APPDATA%\karing\karing\`，仅 Windows 存在；WSL/Linux 开发时必须 `--core` 指定。
- 测速历史写在**配置文件同目录**的 `singbox_speedtest_history.json`，不在仓库目录；临时 sing-box 配置写在 `$TEMP/singbox_speedtest/`。

## 仓库约定

- 分支只有 `main`；commit message 用中文，格式 `项目名: 描述`（如 `singbox-speedtest: 基于 sing-box 的多节点测速工具`）。
- 纯标准库是硬约束（README 卖点），不要引入 pip 依赖。
- README 与代码冲突时以代码为准（例如 README 写 3 个测速源，代码 `SPEEDTEST_URLS` 实际 4 个；改动时顺手同步 README）。

<!-- specgit:block:start -->
## SpecGit delivery harness

Managed by `specgit init`. Everything between the markers is rewritten on
re-init; keep manual guidance outside them.

### The delivery story

- Start with `specgit issue <title-or-number>...`: it creates or reuses
  the issues, branches, opens the draft pull request that closes every
  bound issue, and writes `.specgit.yaml`. Re-running resumes; it is
  idempotent.
- Finish with `specgit finish`: the verdict, derived from real git, PR,
  and CI evidence. Exit code 0 is the only "done".

### Repair and diagnostics

- `specgit pr` repairs the pull-request binding: with no arguments it
  auto-discovers the pull request for this head branch, errors with a fix
  when none is found, and refuses with a list when several match.
- `specgit status` shows local evidence only: record, state, drift,
  origin. `specgit doctor` probes git, repository, origin, gh, and
  policy.

### Issue granularity

One issue = one independently verifiable WHY. If a deliverable cannot be
verified on its own evidence, split it before binding.

### Iron rules

- `specgit finish` exit code other than 0: never request merge. Fix the
  delivery, not the gate.
- Never weaken `spec_git/policy.yaml` to make a verdict pass.
- `--json` is the only parse surface: stdout is exactly one JSON
  document; never scrape human-readable output.
<!-- specgit:block:end -->
