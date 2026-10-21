# 反封禁风险控制（ANTIBAN）

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
| 2 | 分/时/日三级配额（3/min、30/h、200/day） | 同上 | ✅ |
| 3 | 风控信号分类（40301 不算风控） | `antiban.classify_failure` + provider 错误分流 | ✅ |
| 4 | 会话复用（同一 chat_session 复用 8 次再换新） | `upstream/web_client.acquire_session` | ✅ |
| 5 | 浏览器指纹自洽（Accept-Language / sec-ch-ua* / sec-fetch-*） | `constants.BROWSER_XHR_HEADERS` + `web_client._headers` | ✅ |
| 6 | HTTP/2（装了 `h2` 就自动启用；Chrome 一律 h2，直连 h1.1 是弱指纹） | `web_client._HTTP2_AVAILABLE` | ✅（可选依赖） |
| 7 | 风控信号 → 长冷却熔断（429 冷却 15min，疑似封号 6h） | antiban + `gate.CircuitBreaker` | ✅ |
| 8 | 失败快速降级，绝不"重试到死" | provider._get_response_impl → fallback | ✅ |
| 9 | 降级成本控制：只让 replyer 等指定模型走网页版 | `[fallback] fallback_for_models` | ✅（配置即可） |
| 10 | 探活降噪（30min → 60min + 抖动） | `credential.health_check_interval_min` | ⬜ 建议 |
| 11 | 错峰使用（夜间/低峰优先，白天高峰降配额） | antiban 扩展 | ⬜ 二期 |
| 12 | 多账号 token 池轮换（每 token 独立 pacing） | 二期 | ⬜ 二期 |
| 13 | TLS 指纹模拟（`curl_cffi` impersonate=chrome） | 可选替换 transport | ⬜ 二期 |

### 措施 1~2：节奏与配额（最重要）

- 同一个 token **永远只允许一个在途请求**（不是"最多 3 个"）：网页版账号级并发确实能到 3，但"一个人同时发 3 个问题"本身不是正常行为，而排队 20 秒对聊天机器人是可接受的。
- 两次请求间隔 **8~20s 随机抖动**，不是固定 8s：固定间隔是最容易做周期性检测的特征。抖动比例由 `[antiban] jitter_ratio`（默认 0.25）控制。
- 三级配额同时生效：分钟级挡突发、小时级挡爬坡、天级挡"挂机一整天的机器人"。
- 配额耗尽时**不是失败**，而是直接走 Host 官方模型（降级），对话体验不断线——网页版通道是"省钱额度"，不是"可用性依赖"。

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
| `max_per_minute` | 3 | 与"并发=1 + 8s 间隔"一致，防止抖动被挤在一起 |
| `max_per_hour` | 30 | 相当于"每 2 分钟一条"，覆盖正常群聊活跃度 |
| `max_per_day` | 200 | 真人重度用户的上限；超过这个量几乎必然是自动化 |
| `cooldown_on_rate_limit_sec` | 900 | 429 后 15min 静默，足以跨过大多数限流窗口 |
| `cooldown_on_ban_sec` | 21600 | 疑似封号后 6h 内不再打上游；封禁通常按天计，重试只会加长 |
| `session_reuse_calls` | 8 | 一个会话 8 轮追问，接近真人一个话题的长度 |

> 这些数字是**保守起步值**，不是调优结论。真实可用上限只能靠线上观测（见 §6）逐步上调；下调是安全的，上调不是。

## 4. 症状 → 处理对照表

| 现象 | 判定 | 立即动作 |
| --- | --- | --- |
| `[pow_failed]` / 40301 | PoW 算法或挑战变了 | 检查 `upstream/pow.py` 与 `deepseek-pow` 版本；**不要**改 antiban |
| `[upstream_http]` 429 | 节奏太快或当天调用过多 | 什么都不用手动做（已自动 15min 冷却）；若反复出现，把 `max_per_hour` 减半 |
| 401/403 + `[credential_invalid]` | token 过期 | 重新从浏览器 localStorage 取 `userToken` |
| 账号被风控封禁（3 天） | 账号级 | 立即把 `behavior.mode` 改成 `official_only`，让机制性问题不变成"反复撞墙"；解封后从 `max_per_day=50` 起爬坡 |
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
2. 错峰策略：按本地时间给配额加权（例如 01:00-08:00 允许 1.5 倍）。
3. TLS 指纹：若 `curl_cffi` 可用，用 `impersonate="chrome"` 的 AsyncSession 替换 httpx transport（接口保持不变）。
4. 探活降噪：默认 60min + ±20% 抖动，且账号封禁状态下跳过探活。

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

调用点固定为 `provider._get_response_impl`：**熔断检查之后、并发闸门之前** `await pacing.acquire(budget_ms=budget_ms)`；
`acquire` 抛 `DeepEtherError(ErrorCode.PACING_COOLDOWN / PACING_QUOTA)` 时整条请求直接走 `_fallback_or_raise`（降级到 Host 模型，不报错给用户）。

### 8.2 配置键（`[antiban]`，WebUI 有独立「反封禁」页签）

```toml
[antiban]
enabled = true
min_interval_sec = 8.0
max_interval_sec = 20.0
jitter_ratio = 0.25
max_per_minute = 3
max_per_hour = 30
max_per_day = 200
cooldown_on_rate_limit_sec = 900
cooldown_on_ban_sec = 21600
adaptive_backoff = true
session_reuse_calls = 8
persist_state = true
```

### 8.3 落盘与"重启不加刑"

- 文件：`<runtime_dir>/antiban_state.json`（`persist_state = false` 时不落盘，仅内存）。
- 写入是"先写 `.tmp` 再 `replace`"的原子替换；读失败/字段脏只写一条 warning，不影响对话。
- 记录内容：最近调用时刻（滑动窗口用）、分/时/日用量、`interval_scale`（退避倍数上限 `MAX_INTERVAL_SCALE = 8.0`）、
  `consecutive_failures`、`suspected_ban`、`cooldown_until_wall`（**墙上时钟的绝对结束时刻**）。
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

冷却中会把"冷却：无"换成 `冷却中：<原因>（约 N 秒后恢复；期间自动走官方模型）`，
检测到封禁级信号时追加一行提示，连续失败 > 0 时显示次数（到 3 次升级长冷却）。

### 8.6 回归

- `plugins/deepether-proxy/tests/test_antiban.py`：15 条，假时钟/假 sleeper，不真睡。
- 全量：`128 passed, 4 skipped`（`.testenv/maibot/.venv/Scripts/python.exe -m pytest -q`）。

