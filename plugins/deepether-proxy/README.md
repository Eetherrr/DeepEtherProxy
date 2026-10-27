# DeepEther Proxy

把 **DeepSeek 网页版**与 **CommandCode（Go 套餐）** 接入 MaiBot 的 LLM Provider 插件。

- 两个独立 Provider：`com.ether.deepether` 与 `com.ether.commandcode`；**不修改 MaiBot 主程序**
- 通道策略：网页版优先（部署时让 MaiBot 能把它排在任务 `model_list` 前面，见「部署前提」）；
  插件内**不保存任何 API key**（CommandCode 的 Key 由你自己填在插件配置里）
- 一期范围（文档 §4.5）：**只接 `replyer`（主）+ `utils`** —— 这两类任务不需要工具调用、不带图，
  正好避开网页版的全部短板；`planner` 强依赖 `tool_calls`，一期不接
- 0.4.0 新增 CommandCode 通道：支持文本 / 工具调用 / 视觉，**默认只注册 56 条 `cc-*` 模型、不自动接线**

## 安装（0.3.1：不需要再手改 model_config.toml）

1. 把本目录放到 MaiBot 的 `plugins/deepether-proxy`，或在 WebUI 插件页直接上传 zip 安装。
2. 启动 MaiBot（或在插件页重载插件）。日志会依次出现：
   `DeepEther Proxy 已加载：…` → `自动注册 model_config 完成`。
3. **插件只做注册，不接线**（0.5.0 起，与 CommandCode 通道一致），Host 会对 `config/model_config.toml` 热重载，**不用重启**：
   - 新增服务商 `deepether`（`client_type = com.ether.deepether`、`auth_type = none`、`timeout = 300`、
     `max_retry = 1`）
   - 固定注册两个模型：`deepether-replyer`（口语化风格、不思考）、`deepether-utils`（不思考）。
     `planner` 需要工具调用，网页版做不到，**不注册**
   - **不改任何任务的 `model_list` / `selection_strategy`**：哪个任务用哪个模型，请在 WebUI 里自己选
4. 在 WebUI 的「模型设置」里能看到上面两个模型，把它们加到你需要的任务里即可；
   重跑注册只需重载插件；状态统一看 `/deepether-status`（只保留这一个命令）。

> **写入是安全的**：先备份原文件（`model_config.toml.deepether.bak-时间戳`，最多留 5 份）、
> 用 tomllib 复核、再原子替换；内容已是最新时完全不落盘（避免反复触发热重载）。
> 解析失败、模型名被其他服务商占用、找不到实例根等情况都只告警，不会改坏配置。
> 旧版遗留的 `deepether-planner` 模型定义与任务引用会被自动清理（它无法承担工具调用）。

### 让 MaiBot 真的用上它（需要你在 WebUI 里做的 2 件事）

插件只是一个普通 LLM Provider，请求失败与其它模型失败的处理方式一致。注册之后还需要：

1. **把模型加进任务的 `model_list`**（例如 replyer 用 `deepether-replyer`），并在它后面补一个官方模型——
   否则这个模型失败时，该任务没有可用模型；
2. **把该任务的 `selection_strategy` 设为 `sequential`**——否则按 `balance` / `random` 可能挑不到它，
   "网页版优先"不成立。

服务商的 `max_retry = 1` 由插件写入，不需要你改。
`planner` **不要接**（网页版没有工具调用能力，插件也不注册）；`emoji` 同理（需要视觉）。

### 额度用完时自动换模型（0.5.0）

网页版额度用完 / 冷却中 / 熔断时，插件会在 replyer 请求前**把本轮改走你 `model_list` 里的下一个模型**，
不再产生 error/traceback（只留一条中文 info），额度恢复后自动回到网页版。

**没有额外配置**：插件不持有「兜底模型」，用哪个模型完全由你自己的 `model_list` 顺序决定。
因此 replyer 任务的列表里需要有一个非 `deepether` 模型（通常就是官方模型）。

> 只覆盖 replyer。utils / memory 等后台任务没有对应 hook，额度用完时仍会按失败处理——
> 这是刻意的（不让插件去改用户的模型配置）。

### 自动注册的可调项（插件配置 `[provision]`）

| 配置 | 默认 | 说明 |
|---|---|---|
| `enabled` | `true` | 插件加载时是否注册 Provider 与模型 |
| `provider_name` | `deepether` | 写入的服务商名称 |
| `model_config_path` | 空 | 留空自动探测；探测不到时填 `config/model_config.toml` 的绝对路径 |
| `keep_backup` | `true` | 写前备份 |

`behavior.mode` 只控制插件是否接管请求：

| `behavior.mode` | 含义 |
|---|---|
| `web_first`（默认） | 正常接管网页版 |
| `official_only` | 插件拒绝服务（请求直接失败，Host 按你的 `model_list` 走其它模型） |
| `web_only` | 已废弃，行为等同 `web_first` |

### 手动配置（不想自动改文件时）

把 `[provision] enabled` 设为 `false`，然后按下面手工加（内容与插件自动写入的完全一致）：

```toml
[[api_providers]]
name = "deepether"
base_url = "plugin://com.ether.deepether"   # 占位，插件不真正访问
api_key = ""
client_type = "com.ether.deepether"
auth_type = "none"
timeout = 300                               # 关键：深度思考经常超过默认 120s
max_retry = 1                               # 让 MaiBot 的模型调度与其它模型一致（详见「部署前提」）

# 给每个用途建一个模型条目，用 extra_params 区分提示词风格：
# - deepether_style    = "replyer" | "plain" | "raw"
# - deepether_thinking = true | false（utils 关掉思考更快、更省）
[[models]]
model_type = "llm"
model_identifier = "deepseek-web"
name = "deepether-replyer"
api_provider = "deepether"
price_in = 0.0
price_out = 0.0
send_temperature = false
force_stream_mode = false                   # 必须为 false：插件 Provider 不支持 Host 侧流式
extra_params = { deepether_style = "replyer", deepether_thinking = false }

[[models]]
model_type = "llm"
model_identifier = "deepseek-web"
name = "deepether-utils"
api_provider = "deepether"
price_in = 0.0
price_out = 0.0
send_temperature = false
force_stream_mode = false
extra_params = { deepether_style = "plain", deepether_thinking = false }
```

> 只建 `deepether-replyer` / `deepether-utils` 两个模型：`planner` 需要工具调用（function calling），
> 网页版做不到，不要为它建模型条目。

> `max_retry` 是 Host 的 `APIProvider` 字段（不是插件配置项），默认值为 3；写成 1 让这个模型像其它模型一样，
> 失败一次就被 MaiBot 的模型调度接手。

4. 最后把任务指向它们（与自动注册的默认行为一致）—— **主备都写上**，网页版优先：

```toml
[model_task_config.replyer]
model_list = ["deepether-replyer", "deepseek-v4-pro-think"]
selection_strategy = "sequential"   # 关键：按顺序优先选网页版
hard_timeout = 240.0

[model_task_config.utils]
model_list = ["deepether-utils", "deepseek-v4-flash"]
selection_strategy = "sequential"
```

> ⚠️ **必须把 `selection_strategy` 设成 `sequential`。** MaiBot 1.3.11 的默认值是 `balance`
> （0.2.x 的文档写成 `random`），两者都不保证"先网页版"——可能挑不到这个模型，
> 日志里也看不出网页版有没有被调用过。

> 插件把"没有正文"（包括只有思考内容）判成失败，不会返回一条空回复。

## 授权（填网页版 token）

> 网页版用的是 **`localStorage.userToken`（一个 Bearer token）**，**不是 cookie**，抓 cookie 没用。

**这是唯一的授权方式，3 步：**

1. 浏览器登录 [chat.deepseek.com](https://chat.deepseek.com)，按 **F12** 打开开发者工具 → **Console**，粘贴执行：

   ```js
   copy(JSON.parse(localStorage.getItem('userToken')).value)
   ```

   （Firefox 用 `console.log(...)` 代替 `copy`，再手动选中复制）
2. 打开 **MaiBot 的 WebUI → 插件 → DeepEther Proxy → 凭证**，把复制到的 token 加进 `web_tokens`（数组）并保存；
   有多个账号就加多条——插件会让它们在「不在冷却中」的账号之间轮换。
   也可以直接编辑 `plugins/deepether-proxy/config.toml` 的 `[credential] web_tokens = ["..."]`。
3. 在聊天里发送 `/deepether-status`，看到「凭证：已就绪」即成功（状态只有 4~5 行，够看即可）。

token 过期时（上游返回 `40003 INVALID_TOKEN`），状态命令会显示「已失效，需要重新授权」并给出上面这几步，重取一次即可。
凭证只存在于插件配置与 `data` 目录（都不进版本库），日志里只出现脱敏形式（前 4 位 + 长度）。

> 开发/调试期才需要的「本机授权页 + 书签小工具」默认关闭（`credential.loopback_enabled = false`），
> 打开后会监听 `127.0.0.1:18765`，可以点一下书签自动送达 token。正式使用不需要它。

## CommandCode（Go 套餐）通道（0.4.0）

> 适用场景：你买了 CommandCode 的 **Go 订阅**，但官方 Provider API 明确提示
> `Your Go plan doesn't include API access`（403 `upgrade_required`）。Go 的 Key 只能打官方 CLI 的
> `/alpha/generate`，本插件复刻了这条路径（真机验证见
> [docs/research/commandcode/P1-RESULTS.md](../../docs/research/commandcode/P1-RESULTS.md)）。

**两步启用：**

1. **填 Key**：在 [commandcode.ai/studio](https://commandcode.ai/studio) 生成 API Key
   （或 CLI 登录后取 `~/.commandcode/auth.json` 的 `apiKey`），填到插件配置
   `[commandcode] api_key`；也可以设环境变量 `COMMAND_CODE_API_KEY`。
2. **重载插件**：自动注册会写入服务商 `commandcode`（`client_type = com.ether.commandcode`、
   `timeout = 300`、`max_retry = 1`）与 56 条 Go 模型（`cc-*`），**不会把它们接到任何任务**。

**自己决定用在哪（WebUI → 模型设置）：**

| 任务 | 建议模型 | 说明 |
|---|---|---|
| `replyer` | `cc-qwen3.7-flash` / `cc-deepseek-v4-flash` | 最便宜档；口语化需要自己试几条提示词 |
| `utils` | 同上 | 后台文本，价格优先 |
| `planner` | `cc-kimi-k3` / `cc-glm-5.3` / `cc-deepseek-v4-pro` | **必须选工具能力强的模型**，并保留官方模型兜底 |
| `vlm` | `cc-deepseek-v4-flash-vision-exp` 等视觉模型 | 文本模型会自动剥离图片并放占位文本（`vision_mode=strip`） |
| `memory` / `learner` | 不建议 | 严格 JSON 输出不稳，解析失败会被业务层静默丢弃 |

**配置：只需要一个 Key。** `[commandcode]` 里只有 `api_key` 一项；留空表示不使用这条通道（不会注册 `cc-*`）。

其余参数全部是代码内默认值，不再暴露：5 小时 **$3** / 7 天 **$6** / 30 天 **$9**（另留 $0.5 保留额度）、
并发 2、每分钟 6 次、`max_tokens` 8192、模型注册范围 `all`（56 条）、`vision_mode=strip`、`tool_mode=auto`。
预算记账落盘在运行目录 `commandcode_budget.json`，重启不清零；超限直接失败，不会把请求发出去撞 429。

**状态：** 不再单独提供 CC 命令。`/deepether-status` 的最后一行会给出 CC 的 5 小时预算与熔断状态；
模型清单请在 WebUI 的模型设置里查看。

**注意：**

- **单账号、单 Key**：官方条款把多账号列为 material breach，本插件不做账号池；
- Go 是 **$10/月量级金额池**，强模型只够低频（GLM-5.3 约 6 次/天、Kimi K3 约 3 次/天，按 6 万字符上下文估算）；
- 插件 Provider 不支持 Host 侧逐字流式，回复要等完整结果；聊天 `max_tokens` 默认 8192，不要照搬 CLI 的 64k；
- 工具参数被截断（`max_tokens` 太小）会直接判失败并提示，不会把半截 JSON 当工具调用。

## 反封禁（0.3.2：容量可调 + 自适应收敛，建议先保持默认）

网页版账号的封禁绝大多数来自**行为特征**（节奏、会话模式、失败率、指纹），不是单次请求内容。
插件内置了一套节奏控制，默认值就是"保守起步"，**不要为了快而关掉它**：

| 配置 | 默认 | 作用 |
|---|---|---|
| `antiban.enabled` | `true` | 总开关。关闭后仅建议用于排障 |
| `antiban.min_interval_sec` / `max_interval_sec` | `8` / `20` | 两次上游请求之间随机抖动等待；同 token 串行（永不在途 2 个） |
| `antiban.max_per_minute` / `max_per_hour` / `max_per_day` | `3` / `45` / `400` | 三级配额，超限时本次调用直接失败，由 MaiBot 的模型调度处理。**分/时是最强的行为约束**，日上限只是天花板 |
| `antiban.adaptive_budget` / `min_adaptive_budget` | `true` / `200` | 自适应日预算（AIMD）：软风控 ×0.75、封禁级 ×0.5 回撤，连续成功 50 次 +0.1 回补；下限 200 不低于旧版 |
| `antiban.quota_wait_max_sec` | `25` | 分/时配额一时打满时 replyer 最多等 25 秒（等窗口滑出），后台 utils 不等——不改变上游速率，但不把突发整段推给付费模型 |
| `antiban.quiet_hours` / `quiet_hours_ratio` | `01:00-07:00` / `0.25` | 夜间节律：深夜把分/时速率压到 1/4，避免 24 小时匀速不停；留空关闭 |
| `antiban.model_daily_limits` | 空 | 按模型配额，例如 `deepether-utils=180`：给后台 utils 单独限流，别让它吃光 replyer 的额度 |
| `antiban.reserve_daily_budget` / `reserve_models` | `60` / `deepether-replyer` | 日预算最后 N 次只留给高优先级模型，额度紧张时先砍后台任务 |
| `antiban.cooldown_on_rate_limit_sec` / `cooldown_on_ban_sec` | `900` / `21600` | 429 / 疑似封号后的静默时长 |
| `antiban.session_reuse_calls` | `20` | 同一上游会话复用几次再换新（每轮新建会话本身是机器特征） |
| `antiban.persist_state` | `true` | 把节奏状态写到 `data/antiban_state.json`：重启不重置配额、也不重算冷却 |

- 被限流/封号时不需要你手动做什么：`/deepether-status` 会显示冷却原因与剩余时间，
  本次调用失败，与其它模型失败一样由 MaiBot 的模型调度处理。
- 已封号 3 天这种场景：把 `behavior.mode` 设成 `official_only`，自动注册会把 `deepether-*` 从任务
  `model_list` 里移除，让机制性重试完全停下来；解封后改回 `web_first`，并把 `max_per_day` 从 50 起爬坡。
- 完整设计与"为什么不做的那些手段"（代理池、随机 UA、伪造 cookie…）见 [docs/design/ANTIBAN.md](../../docs/design/ANTIBAN.md)。

## 配置要点

| 配置项 | 说明 |
|---|---|
| `behavior.mode` | `web_first`（默认）/ `official_only`；`web_only` 已废弃（仍可加载，等同 `web_first` 并打 warning） |
| `behavior.prompt_style` | `plain`（默认）/ `replyer`（强约束口语化纯文本）/ `raw`；可被模型 `extra_params.deepether_style` 覆盖 |
| `behavior.thinking_enabled` | 深度思考开关；可被模型 `extra_params.deepether_thinking` 覆盖 |
| `concurrency.max_concurrency` | 网页版账号级并发约 3，默认 2，**不建议改成 3 以上** |
| `antiban.*` | 反封禁节奏（见上一节）；WebUI 有独立「反封禁」页签 |
| `commandcode.api_key` | CommandCode Go 通道的 Key（**唯一配置项**）；留空不启用，见「CommandCode（Go 套餐）通道」 |
| `debug.dump_request` | 把 Host 原始请求落盘，用于固化契约测试样本 |
| `debug.dump_response` | 把上游原始 SSE 落盘，用于排查协议漂移 |

> **0.3.0 移除了 `[fallback]` 配置节**（`fallback_enabled` / `fallback_model_name` /
> `fallback_timeout_ms` / `fallback_with_tools` / `fallback_for_models` 全部作废）：
> 插件只是一个普通 Provider，旧配置升级时这些字段会被自动移除，WebUI 里旧字段消失属预期。

## 相关文档

| 文档 | 内容 |
|---|---|
| [docs/SKILL.md](../../docs/SKILL.md) | **项目规范**：红线、代码地图、改动流程、参数依据、验收清单（改代码前必读） |
| [docs/README.md](../../docs/README.md) | 文档索引与维护规则 |
| [docs/FEASIBILITY.md](../../docs/FEASIBILITY.md) | 可行性分析：为什么只接 replyer/utils、工具调用缺口、架构取舍 |
| [docs/design/ANTIBAN.md](../../docs/design/ANTIBAN.md) | 反封禁设计：分层模型、措施矩阵、默认值依据、实现落点 |
| [docs/design/TASK-SUITABILITY.md](../../docs/design/TASK-SUITABILITY.md) | 各模型任务适配性：哪些能接、哪些永远别接 |
| [docs/records/DECISIONS.md](../../docs/records/DECISIONS.md) | 决策日志（含被否决方案） |
| [docs/pitfalls/PITFALLS.md](../../docs/pitfalls/PITFALLS.md) | 踩坑库：现象 → 根因 → 证据 → 修法 → 回归 |
| [docs/research/COMMANDCODE-FEASIBILITY.md](../../docs/research/COMMANDCODE-FEASIBILITY.md) | CommandCode Go 通道的可行性、协议与实施路线 |
| [docs/research/commandcode/P1-RESULTS.md](../../docs/research/commandcode/P1-RESULTS.md) | 0.4.0 真机验证结果（文本 / 工具 / 视觉 / 预算 / threadId 坑） |
| [docs/research/session-reuse.md](../../docs/research/session-reuse.md) | 会话上下文复用：**已否决**（D-005），保留研究结论 |
| [docs/records/P2-ACCEPTANCE.md](../../docs/records/P2-ACCEPTANCE.md) | P2 稳定性验收演练报告（0.2.x 行为，历史记录） |
| [docs/upstream-protocol.md](docs/upstream-protocol.md) | 网页版协议实测记录（PoW / SSE 混合帧 / session 路径） |
| [CHANGELOG.md](CHANGELOG.md) | 版本更新记录 |

## 模型没被优先调度？4 项检查

1. 任务 `model_list` 里，`deepether-*` 之后有没有其它模型；
2. 该任务 `selection_strategy` 是否为 `sequential`；
3. 服务商 `max_retry` 是否为 `1`（在 `[[api_providers]]` 里，不是插件配置）；
4. Host 日志里有没有这个模型的调用记录（插件日志只记录自己这条通道的成功与失败）。

## 上游改版排查 5 步

1. `/deepether-status` 看凭证状态与最近错误码；
2. 错误码为 `pow_failed` → 先在 `deepether/constants.py` 确认算法名，再考虑升级 `deepseek-pow`；
3. 错误码为 `protocol_error` → 打开 `debug.dump_response`，对比 `deepether/constants.py` 里的路径与增量字段；
4. 错误码为 `credential_invalid` → 重新点一次书签；
5. 所有上游假设都集中在 `deepether/constants.py` 与 `deepether/upstream/`，**只改这两处**。

## 免责声明

通过逆向网页版接口提供模型能力通常违反服务条款，可能导致账号限流或封禁。
本项目仅限个人学习与技术研究，请自行评估风险；需要稳定合规的模型服务请使用 DeepSeek 官方开放平台。

CommandCode 通道复刻的是官方 CLI 的私有网关（`/alpha/generate`）。官方条款限制单账号、
禁止自动化滥用；Go 订阅按 credits 计费。请只使用自己的单个账号，不要把模型转售或对外提供 API 服务。
