# DeepEtherProxy

[![release](https://img.shields.io/github/v/release/Eetherrr/DeepEtherProxy)](https://github.com/Eetherrr/DeepEtherProxy/releases/latest)
[![license](https://img.shields.io/github/license/Eetherrr/DeepEtherProxy)](LICENSE)
![MaiBot](https://img.shields.io/badge/MaiBot-1.3.11-blue)
![python](https://img.shields.io/badge/python-3.12-blue)

把 **DeepSeek 网页版**与 **CommandCode（Go 套餐）** 接成 **MaiBot** 的 LLM Provider，
以 **MaiBot 第三方插件** 形式提供（`plugins/deepether-proxy/`），**不修改 MaiBot 主程序**。

> 定位：**省群聊 token 的补充通道**，不是官方 API 的替代品。
> DeepSeek 通道只接 `replyer` + `utils`（纯文本、不吃工具调用）；`planner` 仍走官方模型。
> 0.4.0 新增 CommandCode Go 通道：支持文本 / 工具调用 / 视觉，但**只注册 56 条 `cc-*` 模型，
> 具体接到哪个任务由用户在 WebUI 里决定**。

## 快速开始

1. 从 [Releases](https://github.com/Eetherrr/DeepEtherProxy/releases/latest) 下载最新版 `deepether-proxy-<version>.zip`
2. MaiBot WebUI → 插件页 → 上传 zip 安装 → **重启一次 MaiBot**
3. WebUI → 插件 → DeepEther Proxy → 凭证，填入网页版 token：

   ```js
   // 浏览器登录 chat.deepseek.com 后，F12 → Console 执行
   copy(JSON.parse(localStorage.getItem('userToken')).value)
   ```

4. 聊天里发 `/deepether-status`，看到「凭证：已就绪」即完成
5. （可选，0.4.0）要启用 CommandCode Go 通道：在插件配置 `[commandcode] api_key` 填入 Studio Key，
   重载插件；`/commandcode-status` 能看到 56 条模型与预算，再去 WebUI 把它们加进任务的 `model_list`

插件加载时会自动把服务商与两个模型写进 `config/model_config.toml` 并接好任务列表，
**不需要手动改配置**（详见下文「自动注册」）。完整安装/授权/排查见
[插件 README](plugins/deepether-proxy/README.md)。

## 能力边界

| 能做 | 说明 |
|---|---|
| `replyer` / `utils` 文本生成 | 纯文本、不需要工具调用；含思考内容回传 |
| 反封禁节奏控制 | 串行 + 抖动间隔 + 三级配额 + 自适应日预算 / 夜间节律 / 按模型配额 / 额度保留 + 会话复用 + 指纹自洽，详见 [docs/design/ANTIBAN.md](docs/design/ANTIBAN.md) |
| 额度耗尽静默换模型 | replyer 请求前由 hook 改走官方模型，不再刷 error/traceback（0.5.0，边界见 [docs/records/DECISIONS.md](docs/records/DECISIONS.md) D-004） |
| CommandCode Go 文本 / 工具 / 视觉 | 第二个 Provider（`com.ether.commandcode`），56 条模型；工具调用可支撑 `planner`，视觉模型可接 `vlm`；金额预算与独立熔断 |

| 做不到 | 原因 |
|---|---|
| DeepSeek 通道的 `planner` 工具调用 | 网页版没有 function calling（CommandCode 通道可以，需用户在 WebUI 接线） |
| DeepSeek 通道的视觉 / embedding / 语音 | 网页版没有对应接口，请把这三类任务指向官方模型 |
| 逐字流式 | Host 侧插件 Provider 不支持流式回调，插件内部读完上游后整体返回 |

## 文档

| 文档 | 内容 |
|---|---|
| [docs/README.md](docs/README.md) | **文档索引与维护规则**（先看这里：按「要做什么」导航） |
| [docs/SKILL.md](docs/SKILL.md) | **项目规范**：红线、代码地图、改动流程、参数依据、验收清单 |
| [docs/FEASIBILITY.md](docs/FEASIBILITY.md) | 总纲：可行性结论、能力差距、架构取舍、实施路线（部分章节已被取代） |
| [docs/design/ANTIBAN.md](docs/design/ANTIBAN.md) | 反封禁设计：封禁分层模型、措施矩阵、默认值依据、排查对照表 |
| [docs/design/TASK-SUITABILITY.md](docs/design/TASK-SUITABILITY.md) | 各模型任务适配性：哪些能接、哪些永远别接 |
| [docs/records/DECISIONS.md](docs/records/DECISIONS.md) | 决策日志：每条决定的背景、选项、影响、回滚（含**被否决**方案） |
| [docs/pitfalls/PITFALLS.md](docs/pitfalls/PITFALLS.md) | 踩坑库：现象 → 根因 → 证据 → 修法 → 回归 |
| [docs/records/P2-ACCEPTANCE.md](docs/records/P2-ACCEPTANCE.md) | P2 稳定性验收演练（0.2.x 历史记录） |
| [docs/research/COMMANDCODE-FEASIBILITY.md](docs/research/COMMANDCODE-FEASIBILITY.md) | CommandCode Go 通道可行性：协议、工具/视觉、预算、风险 |
| [docs/research/commandcode/P1-RESULTS.md](docs/research/commandcode/P1-RESULTS.md) | 0.4.0 真机验证：文本 / 工具 / 视觉 / 预算 / threadId 坑 |
| [docs/research/session-reuse.md](docs/research/session-reuse.md) | 会话上下文复用：**已否决**（D-005），保留研究结论 |
| [docs/research/OPTIMIZATION-FEASIBILITY.md](docs/research/OPTIMIZATION-FEASIBILITY.md) | 三项优化需求可行性：额度切换 / 会话 / 账号池（待决策） |
| [plugins/deepether-proxy/README.md](plugins/deepether-proxy/README.md) | 安装、授权、配置、排查（用户向） |
| [plugins/deepether-proxy/docs/upstream-protocol.md](plugins/deepether-proxy/docs/upstream-protocol.md) | 网页版协议实测记录（混合帧 SSE、PoW 细节） |
| [CHANGELOG.md](plugins/deepether-proxy/CHANGELOG.md) | 版本更新记录 |

**先看结论**：[docs/FEASIBILITY.md](docs/FEASIBILITY.md) —— 技术可行，但
"反代网页版 = 拿到 DeepSeek 全部最新能力"**不成立**，核心缺口是工具调用（function calling），
而它正是 MaiBot `planner` 的命脉。

## 自动注册（0.3.1）

装好插件、启动 MaiBot 即可，**不用手改 `model_config.toml`**：插件加载时会自动写入

- 服务商 `deepether`（`client_type = com.ether.deepether`、`auth_type = none`、`timeout = 300`、
  `max_retry = 1`）：让 MaiBot 的模型调度与其它模型一致
- 两个模型：`deepether-replyer`（口语化风格、不思考）、`deepether-utils`（不思考）
- 把 `deepether-replyer` / `deepether-utils` 接到 `replyer` / `utils` 的 `model_list` 首位，并把
  `selection_strategy` 固定为 `sequential`（只有按顺序优先才会先走网页版）

> `planner` 需要工具调用（function calling），网页版做不到，因此**不注册、也不接线**；`planner` 继续用官方模型。

`wire_tasks` 可选纯文本任务（`memory` / `mid_memory` / `fast_model` / `learner`，都复用 `deepether-utils` 这一条模型），
但 `planner` 需要工具调用、`emoji` 需要视觉，写进去会被忽略；详见 [docs/design/TASK-SUITABILITY.md](docs/design/TASK-SUITABILITY.md)。

接线方式由插件配置 `behavior.mode` 决定：

| `behavior.mode` | 自动注册做什么 |
|---|---|
| `web_first`（默认） | 接线到任务 `model_list` 首位（网页版优先） |
| `official_only` | 不接管网页版（自动注册把 `deepether-*` 模型移出任务 `model_list`） |
| `web_only` | 已废弃，行为等同 `web_first` |

自动注册结果里若提示「该任务 `model_list` 只有 `deepether` 模型」，按提示在该任务末尾补一个官方模型即可。

### CommandCode（0.4.0）：只注册模型，不接线

插件加载时还会写入服务商 `commandcode`（`client_type = com.ether.commandcode`、`timeout = 300`、
`max_retry = 1`）与 **56 条 Go 模型**（`cc-*`，来自官方 `models.md` 的 `Go and above`）。
**不会把这些模型加到任何任务**——具体用哪个模型跑 `replyer` / `planner` / `vlm`，由你在
WebUI → 模型设置里决定（建议清单见 [插件 README](plugins/deepether-proxy/README.md)）。

- `register_models`：`all`（默认）/ `curated`（10 条）/ `none`（只留 Provider，并移除已注册的 `cc-*`）
- 切换 `none` 时会把被移除的模型名从各任务 `model_list` 里一并摘掉，避免 Host 找不到模型
- 金额预算默认 5h $3 / 周 $6 / 月 $9，落盘 `commandcode_budget.json`；`/commandcode-status` 可查

Host 对 `model_config.toml` 有文件监视 + 热重载，写完立即生效、不用二次重启。
写入前自动备份（最多 5 份），内容无变化时不落盘，避免反复触发热重载。
手动重跑用 `/deepether-setup`；关掉自动写入用插件配置 `[provision] enabled = false`。

## 反封禁（0.2.3 起，0.3.2 扩容）

上游账号因"行为特征"被风控是这套方案最大的运行风险（本项目实测被封过 3 天）。0.2.3 起把
"让这个账号看起来像一个人在正常用"做成可配置能力，完整设计见 [docs/design/ANTIBAN.md](docs/design/ANTIBAN.md)：

- **节奏**：同 token 串行（并发 1）+ 每次间隔 **8~20 秒随机抖动**，不是固定间隔
- **配额**：3/分 · 45/时 · 400/天 三级滑动窗口，超限时本次调用直接失败（与其它模型失败一样由 MaiBot 调度处理）
- **自适应日预算（0.3.2）**：命中风控日预算 ×0.6 回撤、连续成功再回补，实际额度自己收敛到安全水位；
  另有 **夜间节律**（01:00-07:00 速率 ×0.25）、**按模型配额**（给后台 utils 单独限流）、
  **额度保留**（日预算最后 60 次只给 replyer，保证回复有额度）
- **冷却**：429 → 静默 15 分钟；疑似封号 → 6 小时；连续 3 次风控信号升级长冷却，成功一次折半恢复
- **会话复用**：同一上游会话连续用 20 次再换，避免"一堆一句话会话"这种机器特征
- **指纹自洽**：补齐真实浏览器的 `Accept-Language` / `sec-ch-ua*` / `sec-fetch-*`；装了 `h2` 自动走 HTTP/2；
  **不**伪造 `x-client-*` 私有头（取值随前端漂移，写错比不带更可疑）
- **状态可见**：`/deepether-status` 的「反封禁节奏」段落给出间隔、用量、冷却剩余；
  状态落盘到 `runtime_dir/antiban_state.json`，**重启不重置配额、也不重算冷却**

## 仓库结构

```
plugins/deepether-proxy/         插件本体（plugin.py + _manifest.json + deepether/ + tests/）
docs/                            设计与验收文档（含 research/commandcode/ 协议证据）
scripts/build_package.py         打包脚本：校验模型目录 → dist/<name>-<version>.zip
scripts/gen_commandcode_catalog.py  从官方 models.md 生成 56 条 Go 模型目录
scripts/probe_commandcode.py     真机探针（文本 / 工具 / 视觉 / Provider 全链路）
```

## 本地开发与测试

```bash
git clone https://github.com/Eetherrr/DeepEtherProxy.git
cd DeepEtherProxy/plugins/deepether-proxy
pip install maibot-plugin-sdk pytest httpx wasmtime deepseek-pow tomlkit
python -m pytest -q          # 当前：269 passed, 4 skipped（以 CI 结果为准）
python ../../scripts/gen_commandcode_catalog.py --check   # 模型目录是否与 models.md 一致
python ../../scripts/build_package.py   # 生成 dist/deepether-proxy-<version>.zip
```

把 `plugins/deepether-proxy` 放进 MaiBot 的 `plugins/` 目录（开发期可用目录联接）即可联调。

## CI 与发版

| 工作流 | 触发 | 做什么 |
|---|---|---|
| [`.github/workflows/ci.yml`](.github/workflows/ci.yml) | push / PR 到 `main`，或手动 | 校验 manifest 与 `config.toml` 可解析 → `pytest` → 打包（含 CommandCode 模型目录一致性校验）→ 上传 zip 产物 |
| [`.github/workflows/release.yml`](.github/workflows/release.yml) | 推送 `v*` tag，或手动指定 tag | 先跑测试做闸门 → 打包 → 校验 tag 与 manifest 版本一致并生成 Release 正文 → 发布/更新 Release 并上传 zip |

发版步骤：

```bash
# 1. 改版本号：_manifest.json / PROVIDER_VERSION / CHANGELOG.md
# 2. 提交并推送
git commit -am "feat: 0.6.0 — ..."
git push
# 3. 打 tag 触发 Release 工作流
git tag v0.6.0
git push origin v0.6.0
```

> tag 与 `_manifest.json` 版本不一致时工作流会直接失败，避免发出"号不对"的包。

## 版本基线

| 组件 | 版本 |
|---|---|
| MaiBot | 1.3.11 |
| maibot-plugin-sdk | 2.12.0 |
| Python | 3.12 |

## 免责声明

通过逆向网页版接口提供模型能力通常违反服务条款，**可能导致账号限流或封禁**。
本项目的反封禁措施只能降低概率，**不能保证账号安全**；本项目仅限个人学习与技术研究，
请自行评估风险。需要稳定合规的模型服务，请使用 [DeepSeek 官方开放平台](https://api-docs.deepseek.com/)。

CommandCode 通道复刻官方 CLI 的私有网关；官方条款限制单账号、禁止自动化滥用，Go 订阅按 credits 计费。
请只使用自己的单个账号，不要转售模型或对外提供 API 服务。
