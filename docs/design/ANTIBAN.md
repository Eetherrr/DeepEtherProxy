# 反封禁风险控制（ANTIBAN）

> 状态：**现行设计**　最后更新：2026-10-27　适用版本：0.4.0
> 适用对象：DeepEther Proxy 的网页版通道（chat.deepseek.com 反代）。
> 结论先行：**风险不在"协议能不能通"，而在"这个账号看起来像不像一个人在正常用"**。
> 封禁绝大多数由行为特征触发——请求节奏、会话模式、TLS/HTTP 指纹、失败率——而不是单次请求的内容。

## 1. 封禁是怎么发生的（分层模型）

| 层 | 判定依据 | 典型处罚 | 插件能否干预 |
| --- | --- | --- | --- |
| 网络层 | IP / ASN 信誉、机房 IP、同一 IP 挂几十个 token | 验证码、限流 | 部分（不做代理池，见 §5） |
| 指纹层 | TLS JA3/JA4、HTTP/2 vs 1.1、头部集合与顺序 | 静默降级/挑战 | 部分（h2、头自洽） |
| 协议层 | PoW、必需头、envelope code（40003/40301） | 单请求失败 | 能（已实现） |
| **行为层** | QPS、日调用量、并发数、会话创建频率、失败率 | **429 → 429 累积 → 封号 3 天起** | **能，且收益最大** |
| 内容层 | 滥用/敏感内容 | 直接封号 | 不能（由使用场景决定） |

经验排序（按"最容易触发封禁"→"最不容易"）：
**行为层 > 网络层 > 指纹层 > 协议层**。
所以本项目的投入顺序是：先把行为做像人（§2 措施 1~4），再补指纹（措施 5~6）。

## 2. 措施矩阵

状态图例：✅ 已实现并接入 / 🟡 已实现待接入 / ⬜ 仅方案（文档化）

| # | 措施 | 落点 | 状态 |
| --- | --- | --- | --- |
| 1 | 同账号串行化 + 最小间隔抖动（8~20s） | `deepether/antiban.py` PacingController.acquire | ✅ |
| 2 | 分/时/日三级配额（3/min、45/h、400/day） | 同上 | ✅ |
| 3 | 风控信号分类（40301 不算风控） | `antiban.classify_failure` + provider 错误分流 | ✅ |
| 4 | 会话复用（同一 chat_session 复用 **20** 次再换新） | `upstream/web_client.acquire_session` | ✅ |
| 5 | 浏览器指纹自洽（Accept-Language / sec-ch-ua* / sec-fetch-*） | `constants.BROWSER_XHR_HEADERS` + `web_client._headers` | ✅ |
| 6 | HTTP/2（装了 `h2` 就自动启用；Chrome 一律 h2，直连 h1.1 是弱指纹） | `web_client._HTTP2_AVAILABLE` | ✅（可选依赖） |
| 7 | 风控信号 → 长冷却熔断（429 冷却 15min，疑似封号 6h） | antiban + `gate.CircuitBreaker` | ✅ |
| 8 | 失败时本次调用直接失败（插件不重试、不回调 `ctx.llm`），由 MaiBot 的模型调度处理 | `deepether/provider.py` get_response → 抛 `DeepEtherError` | ✅ |
| 9 | 模型路由控制：只让 replyer/utils 等指定任务走网页版 | 任务 `model_list` + `selection_strategy = sequential` + `api_providers[].max_retry = 1` | ✅（配置即可） |
| 10 | 探活降噪（30min → **60min**） | `credential.health_check_interval_min` | ✅ 0.3.2 |
| 11 | **夜间节律**（01:00-07:00 分/时配额 ×0.25） | `antiban.quiet_hours` / `quiet_hours_ratio` | ✅ 0.3.2 |
| 12 | **自适应日预算 AIMD**（命中风控 ×0.6 回撤，连续成功 100 次 +0.1 回补） | `antiban.adaptive_budget` / `min_adaptive_budget` | ✅ 0.3.2 |
| 13 | **按模型配额**（后台 utils 单独限流，别吃光 replyer 的额度） | `antiban.model_daily_limits` | ✅ 0.3.2 |
| 14 | **额度保留**（日预算最后 N 次只留给 replyer） | `antiban.reserve_daily_budget` / `reserve_models` | ✅ 0.3.2 |
| 15 | 多账号 token 池轮换（每 token 独立 pacing） | 二期 | ⬜ 二期 |
| 16 | TLS 指纹模拟（`curl_cffi` impersonate=chrome） | 可选替换 transport | ⬜ 二期 |
| 19 | **配额有界等待**（分/时打满时 replyer 等窗口滑出，最多 25s，不改变上游速率） | `antiban.quota_wait_max_sec` | ✅ 0.3.2 |
| 20 | **软 403 / 业务限流可识别**（403 无封禁文案、HTTP200+"太频繁" → 退避而不是让用户重授权） | `web_client._handle_error_status` + `classify_failure` | ✅ 0.3.2 |
| 17 | 请求级缓存/合并（相同 prompt 短 TTL 复用、把多次小请求并成一次） | provider 缓存层 | ⬜ 待评估（需先测重复率） |
| 18 | 会话上下文复用（parent_message_id 串起来，只发增量） | 见 docs/research/session-reuse.md | ❌ **已否决**（D-005：不省额度/次数，风险高） |

### 措施 1~2：节奏与配额（最重要）

- 同一个 token **永远只允许一个在途请求**（不是"最多 3 个"）：网页版账号级并发确实能到 3，但"一个人同时发 3 个问题"本身不是正常行为，而排队 20 秒对聊天机器人是可接受的。
- 两次请求间隔 **8~20s 随机抖动**，不是固定 8s：固定间隔是最容易做周期性检测的特征。抖动比例由 `[antiban] jitter_ratio`（默认 0.25）控制。
- 三级配额同时生效：分钟级挡突发、小时级挡爬坡、天级挡"挂机一整天的机器人"。
- 配额耗尽时本次调用直接失败，由 MaiBot 的模型调度处理（与其它模型失败一致）——网页版通道是"省钱额度"，不是"可用性依赖"。

### 措施 3：能区分"我们错了"和"上游在风控"

- `40301 / INVALID_POW`：PoW 过期或算法升级。这是**技术问题**，重试是合理的，绝不能算作风控信号——否则一次算法升级会让账号被自己锁 6 小时。
- `429`：上游明确在限流 → 计一次 rate_limit。
- `403` + 账号级文案 / `user/current` 返回 40003 之外的封禁码 → 计一次 ban，冷却 6h。
- 正文为空但只有 thinking：软拦截，计 rate_limit（比 ban 保守，但足以触发退避）。

### 措施 4：会话复用

真实用户是在**同一个会话里连续追问**。每轮 `chat_session/create` 的实现会在账号下留下一堆"一句话会话"，这是非常显眼的机器特征；顺带还多了一次往返。
现在按 `[antiban] session_reuse_calls`（默认 8）复用同一会话，上游报错时才作废重建。

### 措施 5：指纹自洽（只补"自洽"，不伪造）

只补**与 UA 字符串自洽**的标准浏览器头：`Accept-Language: zh-CN,zh;q=0.9,en;q=0.8`、`sec-ch-ua`（Chrome 131）、`sec-ch-ua-mobile: ?0`、`sec-ch-ua-platform: "Windows"`、`sec-fetch-{dest,mode,site}`。
**不伪造 `x-client-*` 这类私有头**：取值随前端发版漂移，写错（比如版本号对不上）比不带更可疑。若确需补，请从自己浏览器的 DevTools 复制真实值，并同步更新 UA。

## 3. 默认值是怎么定的（为什么不是更激进/更保守）

| 参数 | 默认 | 依据 |
| --- | --- | --- |
| `min_interval_sec` | 8 | 人类连续追问的最快节奏大致就是这个量级；比它更快就开始像脚本 |
| `max_interval_sec` | 20 | 再慢对群聊响应影响明显，且收益递减 |
| `max_per_minute` | 3 | 与"并发=1 + 8s 间隔"一致，防止抖动被挤在一起。**这是最强的行为约束，不建议动** |
| `max_per_hour` | 45 | 0.3.2 从 30 上调：30/h 只够"每 2 分钟一条"，活跃群聊很快打满；45/h 仍低于"真人持续对话"的极限 |
| `max_per_day` | 400 | 0.3.2 从 200 上调。**日总量是最容易被判"自动化"的启发式**，所以它只是天花板，真正约束靠分/时两级 + 自适应回撤 |
| `adaptive_budget` | true | 命中软风控 ×0.75 回撤、封禁级信号 ×0.5；连续成功 50 次 +0.1 回补。让账号自己收敛到安全水位 |
| `min_adaptive_budget` | **200** | 回撤下限**不能低于旧版硬配额 200**，否则一次误判就比升级前更难用 |
| `quota_wait_max_sec` | 25 | 分/时配额一时打满时，replyer 最多等 25 秒（等窗口滑出）而不是立刻失败；后台 utils 不等 |
| `quiet_hours` | 01:00-07:00 | 真人睡觉时段。深夜匀速调用是极强的机器特征，压低比封禁便宜得多 |
| `quiet_hours_ratio` | 0.25 | 深夜留 1/4 速率（约 11/h），既不"停摆"也不"整夜挂机" |
| `reserve_daily_budget` | 60 | 额度紧张时先砍后台 utils，保证用户等待的回复仍有网页版额度 |
| `reserve_models` | deepether-replyer | 只有 replyer 能动用保留额度 |
| `model_daily_limits` | 空 | 可选。建议值 `deepether-utils=180`：utils 调用次数通常是 replyer 的 2~4 倍，但单次节省的 token 更少 |
| `cooldown_on_rate_limit_sec` | 900 | 429 后 15min 静默，足以跨过大多数限流窗口 |
| `cooldown_on_ban_sec` | 21600 | 疑似封号后 6h 内不再打上游；封禁通常按天计，重试只会加长 |
| `session_reuse_calls` | 20 | 0.3.2 从 8 上调：一个会话 20 轮追问更接近真人一个话题的长度，顺带减少建会话往返 |

> 这些数字是**保守起步值**，不是调优结论。真实可用上限只能靠线上观测（见 §6）逐步上调；下调是安全的，上调不是。

## 4. 症状 → 处理对照表

| 现象 | 判定 | 立即动作 |
| --- | --- | --- |
| `[pow_failed]` / 40301 | PoW 算法或挑战变了 | 检查 `upstream/pow.py` 与 `deepseek-pow` 版本；**不要**改 antiban |
| `[upstream_http]` 429 | 节奏太快或当天调用过多 | 什么都不用手动做（已自动 15min 冷却）；若反复出现，把 `max_per_hour` 减半 |
| 401/403 + `[credential_invalid]` | token 过期 | 重新从浏览器 localStorage 取 `userToken` |
| 账号被风控封禁（3 天） | 账号级 | 把 `behavior.mode` 改成 `official_only`（自动注册会把 `deepether-*` 从任务 `model_list` 移除），让机制性问题不变成"反复撞墙"；解封后改回 `web_first`，从 `max_per_day=50` 起爬坡 |
| 正文空、只有 thinking | 软拦截/上游变更 | 看 `/deepether-status` 的 `upstream_empty` 计数；连续出现时降到 1 次/分钟 |

## 5. 明确**不**采用的"反封"手段

| 手段 | 为什么不做 |
| --- | --- |
| 代理池换 IP | 机房 IP 权重更差；同一 token 短时间跨多个 IP/ASN 本身就是强异常信号 |
| 随机化 UA / 头 | 头之间必须自洽（UA 版本 vs sec-ch-ua），不一致比一致更可疑 |
| 伪造 cookies / 设备指纹 | 需要真实浏览器上下文，伪造失败率高，且一旦被识别就是"作弊"定性 |
| 多实例共用同一 token | 立刻制造出并发与多 IP，等于主动触发行为检测 |
| PoW 抢跑/缓存堆量 | 让请求速率脱离 pacing 约束，等于绕过措施 1 |

## 6. 观测与止损

- `/deepether-status` 输出里与风险相关的字段：pacing 当前间隔、今日用量、最近一次风控信号类型、冷却剩余时间、会话复用次数、h2 是否启用。
- 关键告警（写日志即可，不要主动推送）：连续 3 次 rate_limit、ban 判定、配额耗尽比例 > 50%（说明配额是该上调还是场景过重需要判断）。
- **残余风险不可避免**：单账号 = 单点。网页版只是"省钱通道"，可用性必须由 Host 侧官方模型兜底保证。任何"为了不断线而重试"的实现都是在拿账号换可用性。

## 7. 待办（二期）

1. token 池：`credential.web_tokens: list[str]`，每个 token 独立 PacingController + 独立冷却，轮换只在"当前 token 冷却中"时发生（不做并发轮询）。
   —— 仍然值得做：它是唯一能**线性提升容量**且不改变单账号行为特征的手段（代价是要有第二个账号）。
2. ~~错峰策略~~ → **0.3.2 已做「夜间节律」**（`quiet_hours` + `quiet_hours_ratio`）。
   尚未做的是"白天高峰主动降配额"，暂不需要：白天空闲时段本来就是最优的调用窗口。
3. TLS 指纹：若 `curl_cffi` 可用，用 `impersonate="chrome"` 的 AsyncSession 替换 httpx transport（接口保持不变）。
4. ~~探活降噪~~ → **0.3.2 已做**：默认 60min，且封禁判定期间不再探活（`PeriodicProbe` 不主动发请求，只依赖调用结果更新状态）。

## 8. 实现落点与运维（0.2.3 已落地）

### 8.1 代码位置

| 关注点 | 文件 | 关键符号 |
| --- | --- | --- |
| 节奏/配额/冷却/退避 | `plugins/deepether-proxy/deepether/antiban.py` | `PacingController.acquire` / `note_success` / `note_failure` / `snapshot` / `dump_state` / `load_state` |
| 风控信号分类 | 同上 | `classify_failure(*, status, code, message, body) -> BanKind | None` |
| 接线与落盘 | `plugins/deepether-proxy/deepether/provider.py` | `_ensure_pacing` / `_pacing_state_path` / `_load_pacing_state` / `_persist_pacing` / `pacing_snapshot` |
| 403 文案分流 | `plugins/deepether-proxy/deepether/upstream/web_client.py` | `_handle_error_status` → `ACCOUNT_RISK` |
| 浏览器头 / h2 | `plugins/deepether-proxy/deepether/constants.py`、`web_client.py` | `BROWSER_XHR_HEADERS`、`_HTTP2_AVAILABLE` |
| 会话复用 | `plugins/deepether-proxy/deepether/upstream/web_client.py` | `acquire_session(*, reuse_limit)` / `invalidate_session()` |
| 状态展示 | `plugins/deepether-proxy/deepether/status.py` | `_render_pacing`（`plugin.py` 传 `pacing=self._provider.pacing_snapshot`） |

调用点固定在 `provider.get_response` → `_request_web`：**熔断检查之后、并发闸门之前** `await pacing.acquire(budget_ms=budget_ms)`；
`acquire` 抛 `DeepEtherError(ErrorCode.PACING_COOLDOWN / PACING_QUOTA)` 时本次调用直接失败
（0.3.0 起插件不内置兜底调用，与其它模型失败一样由 MaiBot 的模型调度处理；
插件只留下一条中文 warning 与失败计数）。

### 8.2 配置键（`[antiban]`，WebUI 有独立「反封禁」页签）

```toml
[antiban]
enabled = true
min_interval_sec = 8.0
max_interval_sec = 20.0
jitter_ratio = 0.25
max_per_minute = 3
max_per_hour = 45
max_per_day = 400
cooldown_on_rate_limit_sec = 900
cooldown_on_ban_sec = 21600
adaptive_backoff = true
adaptive_budget = true
min_adaptive_budget = 200
quota_wait_max_sec = 25
quiet_hours = "01:00-07:00"
quiet_hours_ratio = 0.25
model_daily_limits = ""
reserve_daily_budget = 60
reserve_models = "deepether-replyer"
session_reuse_calls = 20
persist_state = true
```

### 8.3 落盘与"重启不加刑"

- 文件：`<runtime_dir>/antiban_state.json`（`persist_state = false` 时不落盘，仅内存）。
- 写入是"先写 `.tmp` 再 `replace`"的原子替换；读失败/字段脏只写一条 warning，不影响对话。
- 记录内容（0.3.2 起 `version = 2`）：最近调用时刻**与对应模型名**（滑动窗口/按模型配额用）、
  分/时/日用量、`interval_scale`（退避倍数上限 `MAX_INTERVAL_SCALE = 8.0`）、`budget_scale`（自适应预算比例）、
  `success_streak`、`consecutive_failures`、`suspected_ban`、`cooldown_until_wall`（**墙上时钟的绝对结束时刻**）。
  读取时兼容 v1 的纯时间戳列表，旧文件不会导致启动失败。
- 恢复时优先用 `cooldown_until_wall - now` 计算剩余冷却，**不是**把 15 分钟/6 小时重算一遍——
  否则每重启一次就等于加刑一次，反而更容易连续撞墙。
- 反向的坑也已修：`note_success()` 会清空冷却计时，成功一次后退避倍数折半；避免"一次 429 之后永远显示冷却中"。

### 8.4 风控分类的判定顺序（顺序本身就是设计）

1. **技术性问题一律不算风控**：`POW_FAILED`、`CREDENTIAL_INVALID/MISSING`、上游 `40301 / INVALID_POW`、`PROTOCOL_ERROR`、`NOT_SUPPORTED`、`INTERNAL`、HTTP 5xx → 返回 `None`（既不冷却也不退避）。
2. **限流优先于封禁**：先看 `status == 429` 或正文里出现 `429` → `rate_limited`。
   理由：限流响应体经常自带"风控/频率异常"字样，若先匹配封禁关键词，会把一次普通 429 误判成封号并锁 6 小时。
3. 封禁关键词（`_BAN_MARKERS`）→ `banned`。
4. 配额关键词（`_QUOTA_MARKERS`）→ `quota`。
5. 无文案的 `403` → `rate_limited`（比 ban 保守）。
6. `UPSTREAM_EMPTY`（正文空、只有 thinking）→ `rate_limited`。

### 8.5 状态命令看到的样子

`/deepether-status` 的「反封禁节奏」段落（有调用记录时）：

```
【反封禁节奏】
间隔：8.0~20.0 秒（含当前退避倍数 1.0）
用量：近 1 分钟 0/3｜1 小时 4/30｜1 天 4/200
冷却：无
```

冷却中会把"冷却：无"换成 `冷却中：<原因>（约 N 秒后恢复；期间网页版调用直接失败）`，
检测到封禁级信号时追加一行提示，连续失败 > 0 时显示次数（到 3 次升级长冷却）。

### 8.6 回归

- `plugins/deepether-proxy/tests/test_antiban.py`：15 条，假时钟/假 sleeper，不真睡。
- 全量：`cd plugins/deepether-proxy && ..\..\.testenv\maibot\.venv\Scripts\python.exe -m pytest -q`（当前 151 passed, 4 skipped）。

## 9. 容量与风险：抬高上限之外还能做什么（0.3.2）

> 用户痛点："一天 200 次不够用"。先把结论说清楚：**单纯把 200 改成 600 是最差的解法**——
> 日总量是最容易被判"自动化"的启发式；而真正决定风险的短期速率（分/时）并没有变。
> 0.3.2 的做法是「抬高天花板 + 四个让行为更自然的机制」，并保留继续往上爬的路径。

### 9.1 影响风险的因素，按可操作性排序

| 因素 | 影响 | 能否干预 | 0.3.2 的做法 |
| --- | --- | --- | --- |
| 短时速率（分/时） | **最强** | 能 | 仍然 3/分、45/时；夜间再 ×0.25 |
| 行为是否"像人"（作息、会话、间隔分布） | 强 | 能 | 夜间节律、会话复用 20 次、8~20s 抖动 |
| 失败率/异常率 | 强 | 能 | 技术性失败与风控分离；失败不重试 |
| 日总量 | 中（启发式） | 能 | 400/天天花板 + AIMD 自动收敛 |
| 单请求规模（一次发几万字符） | 中 | 部分 | `max_history_chars` 裁剪 |
| 请求内容（敏感/滥用） | 强 | 不能 | 由使用场景决定 |
| IP / ASN 信誉 | 中 | 不能（不建议代理池） | — |
| TLS/HTTP 指纹 | 中 | 部分 | h2 + 浏览器头自洽；`curl_cffi` 见 §7 |

### 9.2 五个"降调用量"的手段（比提高上限更根本）

> 反封禁的本质是"单位时间的请求数"。能少发一次，就少一次风险，而且不损失任何功能。

1. **按任务分级路由**（零成本，立刻可做）：把 `wire_tasks` 收成只有 `replyer`，
   utils 回官方模型。utils 调用频次通常是 replyer 的 2~4 倍（记忆写回、摘要、评分），
   但单次输入只有 1k token 上下、输出常常只有几个字——**它占调用量的大头，占节省额度的比例却小**。
   取舍点是"调用次数 vs 节省 token"，需要按自己的群聊活跃度算（见 `docs/design/TASK-SUITABILITY.md`）。
2. **按模型配额**（0.3.2 已实现）：不完全撤掉 utils，但给它一个上限（如 `deepether-utils=180`），
   水位越界后自动改走官方模型。相当于"先把便宜的额度花掉，剩下留给关键路径"。
3. **额度保留**（0.3.2 已实现）：日预算最后 N 次只给 replyer，避免"上午闲聊把额度用光、晚上回复全走官方"。
4. **请求级缓存/合并**（未实现，需先测）：utils 类任务里存在"同样输入问同样问题"的情况
   （例如同一段记忆重复写回）。对 `hash(model + prompt)` 做 60~300s 的短缓存可直接省掉这部分调用。
   **先测量再实现**：如果重复率低于 10%，加缓存只增加复杂度。
5. ~~会话上下文复用~~（docs/research/session-reuse.md）：**已否决**（D-005）——只省上传体积、不省额度，风险高。
   它**不减少调用次数**，但让"每轮重发全量 prompt"变成"只发新增一句"，更像真人，也降低单请求特征。

### 9.3 要不要"日上限 600 / 1000"？

可以，但要配合两件事，否则等于把风险预算一次性花掉：

1. **开了自适应预算**（默认开）：命中 429 会自动回撤到 60%，连续成功后再爬回来——上限只是天花板，不是常态；
2. **盯住状态命令里的三行**：`近 1 小时 X/45`、`自适应预算：当前上限 N 次/天`、`近 24 小时按模型`。
   如果小时级长期贴顶、或 `budget_scale` 一直往下掉，说明该降频（回到 §9.2 的 1/2/3），而不是继续调上限。

> 一句话：**上限是"允许用到多少"，行为才是"看起来像不像人"**。0.3.2 把前者做成可调 + 自收敛，
> 把后者做成默认更接近真人（夜里睡、会话连续、额度不够先砍后台）。
## 10. 实测调用画像（2026-10-22，用于定默认值）

> 数据源：`.testenv/maibot/logs/app_*.log.jsonl`（79 个文件、27828 行，覆盖 2026-10-20 01:03 ~ 10-21 22:41）。
> 这批日志**早于 antiban 上线**，所以数字代表「未节流的真实需求」，正好用来定配额。
> 去重口径：插件日志在两个日志文件里是镜像（时间戳差 30~130ms），按「事件文本 + ≤1s 时间窗」去重；
> 校验：`Token使用情况` 117 条 == 网页版成功 56 + 触发降级 61，一对一配对成立。

### 10.1 一条回复到底要几次调用

| 指标 | 实测 |
| --- | --- |
| 每条「用户消息 → 回复」 | **3.00 次**网页版调用（健康区段，区间 2.5~4.0） |
| 构成 | replyer 0.83 次（26%）+ **utils 2.17 次（74%）** |
| planner | 2.2 次/消息，但走 Host 官方模型，**不占网页版配额** |

**这是最关键的结论**：调用量的大头不是"回复"本身，而是后台 utils（记忆写回/摘要/评分）。
少发一次 utils，等价于直接多出一次额度，而且用户完全无感。

### 10.2 单次调用的规模：utils「高频但极小」

| 任务 | 耗时 p50 | 正文 | 思考 | 上下文条数 | 输入 token |
| --- | --- | --- | --- | --- | --- |
| utils（A_Memorix 写回） | 1750 ms | 2 字 | 0 | 恒 1 条 | ~1174 |
| replyer | 2500 ms | 28.9 字 | 93 字 | 9~26 条 | ~2226 |

- utils 的 prompt 里 **~85% 是固定模板**，只有用户原话在变；
- replyer 的 prompt 是 append-only 增长（历史条数 9 → 26）。

### 10.3 未节流的真实峰值

| 窗口 | 峰值 |
| --- | --- |
| 任意 60 秒 | **9 次** |
| 任意 1 小时 | **38 次** |
| 单小时最高（按整点） | 38 次 |

→ 所以 0.3.2 的 **3/分** 会把这个瞬时峰值压掉约 3 倍（这是有意保留的强约束），
而 **45/时** 刚好覆盖实测的 38/时。

### 10.4 换算表：额度能撑多活跃

| 日上限 | 可支撑回复数 | 等效活跃度（按 6 小时在线） |
| --- | --- | --- |
| 200（旧） | 53~67 次 | 11 次/时 → 每 5 分钟一句 |
| **400（0.3.2 默认）** | 106~133 次 | 约 20 次/时 → 普通/偏活跃群 |
| 600 | 160~200 次 | 约 30 次/时 → 热闹群 |

| 时上限 | 可支撑回复数/时 | 说明 |
| --- | --- | --- |
| 30（旧） | 10 次 | 活跃群半小时就打满，**小时配额比日配额先顶** |
| **45（0.3.2 默认）** | 15 次 | 覆盖普通群聊 |
| 60 | 20 次 | 热闹群；风险也相应上升，建议先观察一周 |

> 结论：**时/日两级要一起提**。只把日上限翻倍，小时级仍会在高峰期把请求全挡掉。

### 10.5 为什么没做「响应缓存」

实测：utils 每条 prompt 都不同（固定模板占 85%，但变量每次变），
`hash(model + prompt)` 的精确命中率 **≈ 0**；即使"同一条消息回写了两次"，两次 prompt 也差 1 个 token。
所以按 prompt 做响应缓存**没有收益**。

真正有效的一刀是在**源头**减少 utils 调用（Host 侧记忆 worker 的频次/去重），
或按 §9.2 的 1/2/3（收窄 `wire_tasks`、按模型配额、额度保留）把 utils 的网页版调用掐掉一部分。
