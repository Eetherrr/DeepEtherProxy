# DeepEther Proxy

把 **DeepSeek 网页版**接入 MaiBot 的 LLM Provider 插件。

- 接入方式：```LLMProvider("com.ether.deepether")```，**不修改 MaiBot 主程序**
- 通道策略：网页版优先；不可用时自动降级到 **Host 已配置的官方模型**（插件内不保存任何 API key）
- 一期范围（文档 §4.5）：**只接 `replyer`（主）+ `utils`** —— 这两类任务不需要工具调用、不带图，
  正好避开网页版的全部短板；`planner` 强依赖 `tool_calls`，一期不接

## 安装（0.2.4：不需要再手改 model_config.toml）

1. 把本目录放到 MaiBot 的 `plugins/deepether-proxy`，或在 WebUI 插件页直接上传 zip 安装。
2. 启动 MaiBot（或在插件页重载插件）。日志会依次出现：
   `DeepEther Proxy 已加载` → `自动注册 model_config 完成`。
3. **插件会自动完成三件事**，Host 会对 `config/model_config.toml` 热重载，**不用重启**：
   - 新增服务商 `deepether`（`client_type = com.ether.deepether`、`auth_type = none`、`timeout = 300`）
   - 新增三个模型：`deepether-replyer`（口语化风格、不思考）、`deepether-planner`（开思考）、
     `deepether-utils`（不思考）
   - 把 `deepether-replyer` / `deepether-utils` 插到 `replyer` / `utils` 的 `model_list` 首位，
     并把 `selection_strategy` 固定为 `sequential`（只有按顺序优先才会先走网页版）
4. 在 WebUI 的「模型设置」里应能看到上面三个模型；发 `/deepether-setup` 可随时手动重跑，
   `/deepether-status` 的「自动注册」段落会显示最近一次结果。

> **写入是安全的**：先备份原文件（`model_config.toml.deepether.bak-时间戳`，最多留 5 份）、
> 用 tomllib 复核、再原子替换；内容已是最新时完全不落盘（避免反复触发热重载）。
> 解析失败、模型名被其他服务商占用、找不到实例根等情况都只告警，不会改坏配置。

### 自动注册的可调项（插件配置 `[provision]`）

| 配置 | 默认 | 说明 |
|---|---|---|
| `enabled` | `true` | 插件加载时是否自动注册 |
| `provider_name` | `deepether` | 写入的服务商名称 |
| `model_config_path` | 空 | 留空自动探测；探测不到时填 `config/model_config.toml` 的绝对路径 |
| `wire_tasks` | `replyer,utils` | 要接线的任务；**默认不接 planner**（它强依赖工具调用，接了只会每次先失败一次再降级） |
| `selection_strategy` | `sequential` | 接线时写入的策略，必须 sequential |
| `keep_backup` | `true` | 写前备份 |

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
name = "deepether-planner"        # 默认不接线；planner 强依赖工具调用，只在你想试时手动挂
api_provider = "deepether"
price_in = 0.0
price_out = 0.0
send_temperature = false
force_stream_mode = false
extra_params = { deepether_style = "plain", deepether_thinking = true }

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

4. 最后把任务指向它们（与自动注册的默认行为一致）—— **主备都写上**，网页版失败时 Host 会自动切到下一个（文档 §4.5.2）：

```toml
[model_task_config.replyer]
model_list = ["deepether-replyer", "deepseek-v4-pro-think"]
selection_strategy = "sequential"   # 关键：按顺序优先选网页版
hard_timeout = 240.0

[model_task_config.utils]
model_list = ["deepether-utils", "deepseek-v4-flash"]
selection_strategy = "sequential"
```

> ⚠️ **必须把 `selection_strategy` 设成 `sequential`。** MaiBot 默认是 `random`，
> 那样每次调用会随机挑一个模型，约一半请求会**静默**走官方 API——既不省钱，
> 日志里也看不到任何降级痕迹（正是报告 §6.6.2 说的"以为在省钱其实在烧钱"）。
> `sequential` 才会"优先网页版、失败再按顺序切下一个"。

> 插件把"没有正文"（包括只有思考内容）判成失败：返回 `success:false` → Host 抛异常 → 切下一个模型。
> 所以网页版没吐正文时不会变成一条空回复，而是静默切到官方模型并打一条中文 warning。

## 授权（填网页版 token）

> 网页版用的是 **`localStorage.userToken`（一个 Bearer token）**，**不是 cookie**，抓 cookie 没用。

**这是唯一的授权方式，3 步：**

1. 浏览器登录 [chat.deepseek.com](https://chat.deepseek.com)，按 **F12** 打开开发者工具 → **Console**，粘贴执行：

   ```js
   copy(JSON.parse(localStorage.getItem('userToken')).value)
   ```

   （Firefox 用 `console.log(...)` 代替 `copy`，再手动选中复制）
2. 打开 **MaiBot 的 WebUI → 插件 → DeepEther Proxy → 凭证**，把复制到的 token 粘进 `web_token` 并保存；
   也可以直接编辑 `plugins/deepether-proxy/config.toml` 的 `[credential] web_token`。
3. 在聊天里发送 `/deepether-status`，看到「凭证：已就绪」即成功。

token 过期时（上游返回 `40003 INVALID_TOKEN`），状态命令会显示「已失效，需要重新授权」并给出上面这几步，重取一次即可。
凭证只存在于插件配置与 `data` 目录（都不进版本库），日志里只出现脱敏形式（前 4 位 + 长度）。

> 开发/调试期才需要的「本机授权页 + 书签小工具」默认关闭（`credential.loopback_enabled = false`），
> 打开后会监听 `127.0.0.1:18765`，可以点一下书签自动送达 token。正式使用不需要它。

## 反封禁（0.2.3，建议保持默认）

网页版账号的封禁绝大多数来自**行为特征**（节奏、会话模式、失败率、指纹），不是单次请求内容。
插件内置了一套节奏控制，默认值就是"保守起步"，**不要为了快而关掉它**：

| 配置 | 默认 | 作用 |
|---|---|---|
| `antiban.enabled` | `true` | 总开关。关闭后仅建议用于排障 |
| `antiban.min_interval_sec` / `max_interval_sec` | `8` / `20` | 两次上游请求之间随机抖动等待；同 token 串行（永不在途 2 个） |
| `antiban.max_per_minute` / `max_per_hour` / `max_per_day` | `3` / `30` / `200` | 三级配额，超限直接降级到官方模型（对话不断线） |
| `antiban.cooldown_on_rate_limit_sec` / `cooldown_on_ban_sec` | `900` / `21600` | 429 / 疑似封号后的静默时长 |
| `antiban.session_reuse_calls` | `8` | 同一上游会话复用几次再换新（每轮新建会话本身是机器特征） |
| `antiban.persist_state` | `true` | 把节奏状态写到 `data/antiban_state.json`：重启不重置配额、也不重算冷却 |

- 被限流/封号时不需要你手动做什么：插件会静默降级并在 `/deepether-status` 的「反封禁节奏」段落里显示原因与剩余时间。
- 已封号 3 天这种场景：先把 `behavior.mode` 设成 `official_only` 让机制性重试停下来，解封后把 `max_per_day` 从 50 起爬坡。
- 完整设计与"为什么不做的那些手段"（代理池、随机 UA、伪造 cookie…）见 [docs/ANTIBAN.md](../../docs/ANTIBAN.md)。

## 配置要点

| 配置项 | 说明 |
|---|---|
| `behavior.mode` | `web_first`（默认）/ `web_only` / `official_only` |
| `behavior.prompt_style` | `plain`（默认）/ `replyer`（强约束口语化纯文本）/ `raw`；可被模型 `extra_params.deepether_style` 覆盖 |
| `behavior.thinking_enabled` | 深度思考开关；可被模型 `extra_params.deepether_thinking` 覆盖 |
| `concurrency.max_concurrency` | 网页版账号级并发约 3，默认 2，**不建议改成 3 以上** |
| `fallback.fallback_model_name` | 降级目标：`model_config.toml` 里已配置的模型名；留空则禁用插件内降级 |
| `fallback.fallback_for_models` | 允许降级的**模型名**列表；留空=全部允许。Host 请求里不带任务名，只能按模型名区分 |
| `antiban.*` | 反封禁节奏（见上一节）；WebUI 有独立「反封禁」页签 |
| `debug.dump_request` | 把 Host 原始请求落盘，用于固化契约测试样本 |
| `debug.dump_response` | 把上游原始 SSE 落盘，用于排查协议漂移 |

## 相关文档

| 文档 | 内容 |
|---|---|
| [docs/FEASIBILITY.md](../../docs/FEASIBILITY.md) | 可行性分析：为什么只接 replyer/utils、工具调用缺口、架构取舍 |
| [docs/ANTIBAN.md](../../docs/ANTIBAN.md) | 反封禁设计：分层模型、措施矩阵、默认值依据、实现落点 |
| [docs/P2-ACCEPTANCE.md](../../docs/P2-ACCEPTANCE.md) | P2 稳定性验收演练报告 |
| [docs/upstream-protocol.md](docs/upstream-protocol.md) | 网页版协议实测记录（PoW / SSE 混合帧 / session 路径） |
| [CHANGELOG.md](CHANGELOG.md) | 版本更新记录 |

## 上游改版排查 5 步

1. `/deepether-status` 看凭证状态与最近错误码；
2. 错误码为 `pow_failed` → 先在 `deepether/constants.py` 确认算法名，再考虑升级 `deepseek-pow`；
3. 错误码为 `protocol_error` → 打开 `debug.dump_response`，对比 `deepether/constants.py` 里的路径与增量字段；
4. 错误码为 `credential_invalid` → 重新点一次书签；
5. 所有上游假设都集中在 `deepether/constants.py` 与 `deepether/upstream/`，**只改这两处**。

## 免责声明

通过逆向网页版接口提供模型能力通常违反服务条款，可能导致账号限流或封禁。
本项目仅限个人学习与技术研究，请自行评估风险；需要稳定合规的模型服务请使用 DeepSeek 官方开放平台。
