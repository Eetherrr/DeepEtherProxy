# 项目 SKILL：改这个项目的正确姿势

> 状态：**现行**　最后更新：2026-10-27　适用版本：**0.5.0**
> 这份文档是「动手前的检查单」。读完它再改代码，能避开 90% 的返工。

## 0. 怎么用这份文档

- 要改代码：先看 §2 红线 → §3 代码地图 → §4 流程；
- 要调参数：看 §6 默认值依据，别凭感觉改；
- 要接新上游：看 §8 的 recipe 3；
- 出了怪事：先查 [pitfalls/PITFALLS.md](pitfalls/PITFALLS.md)，再查 [records/DECISIONS.md](records/DECISIONS.md)。

## 1. 这个项目是什么

把两类上游包装成 MaiBot 的 LLM Provider（**不改 MaiBot 主程序**）：

| Provider | client_type | 上游 | 能力 |
|---|---|---|---|
| DeepEther | `com.ether.deepether` | chat.deepseek.com 网页版（Bearer token + PoW + SSE） | 纯文本（replyer/utils 等），**无工具调用/无视觉/无 embedding** |
| CommandCode | `com.ether.commandcode` | 官方 CLI 网关 `POST /alpha/generate`（NDJSON） | 文本 + 推理 + **工具调用** + 视觉 |

两条通道**完全隔离**：凭证、并发闸门、熔断、节奏、预算各自独立；一条挂了不影响另一条。

## 2. 红线（违反任何一条都要先停下来讨论）

1. **不改 MaiBot 主程序**：所有能力必须落在插件与 manifest 声明的范围里；
2. **失败必须如实暴露**：不用「占位回复 / 静默成功」掩盖故障（0.3.0 已移除插件内 fallback，这是刻意决定，见 D-001）；
3. **hook 里不发网络请求、不睡眠**：hook 只做本地判断与参数改写，超时预算 800ms 量级；
4. **不主动提高上游速率**：同一账号必须串行 + 抖动；任何「并发保活 / 轮询抢占」都是在拿账号换吞吐；
5. **凭证只以脱敏形式出现**（前 4 位 + 长度），不得写进日志、状态、异常文本；
6. **CommandCode 单 Key**：官方条款禁止多账号/自动化滥用，不做池化（见 D-003）；
7. **配置写入必须可回滚**：备份 → tomllib 复核 → `.tmp` + `os.replace` 原子替换 → 内容无变化不落盘。

## 3. 代码地图（改哪里）

| 模块 | 职责 | 什么时候改它 |
|---|---|---|
| `plugin.py` | 插件入口：生命周期、命令、状态渲染、自动注册触发 | 加命令、改加载流程、改进线 |
| `deepether/config.py` | 强类型配置（所有字段都有中文说明与默认值） | 加配置项（必须同步 config.toml 与 §6） |
| `deepether/provider.py` | DeepEther 编排：投影→熔断→节奏→闸门→上游→错误分类 | 改请求链路、加路由/hook、改错误语义 |
| `deepether/upstream/web_client.py` | 上游 HTTP：建会话、PoW、SSE、错误码 | 上游协议变化、加响应字段 |
| `deepether/upstream/sse.py` | SSE 混合帧累积器 | 增量格式变化、要取 message id |
| `deepether/upstream/pow.py` | PoW 求解（**进程内 wasm，需串行**） | 算法升级；改并发策略前先看 P-04 |
| `deepether/antiban.py` | 反封禁：串行/抖动/三级配额/AIMD/夜间/保留额度/有界等待/风控分类 | 调容量、加风控信号、改冷却 |
| `deepether/gate.py` | 并发闸门 + 三态熔断 | 改并发/熔断语义 |
| `deepether/adapter_in.py` / `adapter_out.py` | context_items ↔ prompt / 响应协议 | Host 协议变化、改提示词风格 |
| `deepether/credential.py` | 凭证存储（当前单 token） | 加多账号（见 D-006） |
| `deepether/loopback.py` / `bookmarklet.py` | 本机授权页 + 书签 + nonce | 改授权体验、多 token 投递 |
| `deepether/provision.py` | 自动注册：写 Host 的 model_config.toml、接线、清理遗留 | 改模型清单、接线规则、让位/回插 |
| `deepether/status.py` | `/deepether-status` 纯函数渲染 | 加可观测字段（必须同时加测试） |
| `deepether/state.py` | 运行期计数（成功/失败/延迟/24h 窗口） | 加统计口径 |
| `deepether/commandcode/*` | CommandCode 通道：协议/客户端/预算/目录/Provider | 只改 CC 相关，不要和 DeepEther 混 |
| `tests/*` | 单元测试（假时钟、假客户端，不触网） | 任何行为改动都要补 |

## 4. 改动流程（六步，缺一步就会返工）

1. **定位**：属于「新需求 / 调参 / 修 bug / 接新上游」哪类？先查 §9 是否已明确「不做」；
2. **研究**：新需求先写 `docs/research/` 一页（问题、约束、选项、证据、工作量）；
3. **决策**：结论写进 [records/DECISIONS.md](records/DECISIONS.md)，包括被否决项；
4. **实现**：按 §3 找模块；配置项必须三处同步（config.py / config.toml / §6 依据）；
5. **验证**：`pytest` 全绿 + 至少一条新用例覆盖新行为；涉及上游的补真机脚本（不进 CI）；
6. **留档**：设计变更改 `design/`，验收写 `records/`，踩坑写 `pitfalls/`。

**提交前自检**

- [ ] 新增配置有默认值 + 中文说明 + 单测断言；
- [ ] 新增错误码：是否该进 `classify_failure` 的技术性白名单（否则会被当风控冷却）；
- [ ] 新增日志不含 token/完整 prompt；
- [ ] 时间相关逻辑可注入时钟（否则测试在凌晨/跨天会红，见 P-06）；
- [ ] 状态落盘有版本号且兼容旧版本；
- [ ] 文档三处（design/records/pitfalls）是否该更新。

## 5. 关键机制速查

### 5.1 模型路由（为什么必须 sequential）

- Host 只看「本次请求是否失败」，**不认识额度**；`sequential` 会永远先选 `model_list` 首位；
- `random` 让网页版占比不可控；`balance` 的惩罚分只有 300，抵不过一次回复的 token 量，且不会随时间衰减；
- 所以「额度耗尽自动切换」不能靠策略，只能靠 **hook 预改写 model_name**（0.5.0 已实现，仅 replyer），见 D-004；
- 自动注册会把 `max_retry` 写成 1：失败一次就交给 Host 调度，不在同一模型上重试。

### 5.2 自动注册（provision）

- **只注册、不接线**（D-008）：只确保 Provider 与 `[[models]]` 存在，绝不改写任务的 `model_list` / `selection_strategy`；
- 启动 / `/deepether-setup` / 自身配置更新时运行；写 `config/model_config.toml`（可探测）；
- Host 有文件监视 + 热重载（debounce ~600ms），**不需要重启**，但频繁改写会抖动 → 任何自动改写都要加迟滞；
- 写入前备份（最多 5 份）、`tomllib` 复核、原子替换、内容无变化不落盘；
- 固定注册 `deepether-replyer` / `deepether-utils` 两条模型（无开关）；`planner` / `emoji` 永不注册。

### 5.3 反封禁（antiban）

五层：串行抖动 → 三级配额 → 风控分类 → 长冷却/退避 → 观测。
容量四件套：AIMD 日预算（软 0.75 / 硬 0.5，成功 50 次 +0.1，下限 200）、夜间节律（01:00-07:00 ×0.25）、按模型配额、额度保留（最后 60 次只给 replyer）。
**有界等待**只对分/时窗口有效（replyer ≤25s），日额度是硬拦截。

### 5.4 会话

- 现状：**只复用 chat_session_id（默认 20 次）**，每轮全量 prompt，`parent_message_id` 恒为 `None`；
- 这是**有意为之**（2026-10-27 决定不做上下文复用，见 D-005）：收益只是少传/更像人，不省 token，却引入串台与误冷却风险；
- 失败时不要无条件 `invalidate_session()`（会平白增加建会话次数）。

### 5.5 凭证与两条通道的配置面

- 网页版：`credential.web_tokens`（数组，可多个账号，按账号轮换）；
- CommandCode：**只有一个 `api_key`**；其余参数是代码常量，不再暴露给用户（D-009）。

- 单 token：配置 `credential.web_token` 优先，其次书签落盘的 `credential.json`；
- 失效标记是**整库唯一**的：这也是多账号池必须先改的结构（见 D-006）；
- 书签通道只绑 127.0.0.1，nonce + Origin 双校验；nonce 是「本机界面」证明，不是账号身份。

### 5.6 状态与可观测

`/deepether-status` 是**唯一命令**，只输出 4~5 行：版本/运行时长、凭证、网页版用量（今日/小时/熔断/冷却）、CommandCode 预算、最近错误。
详细参数一律去 WebUI 看，不再铺在聊天里。口径约定：`失败` 只在「最终抛错」时 +1；让位不计失败。

### 5.7 错误分类（哪些算风控）

- **不算**：PoW 失败、凭证失效/缺失、协议错误、5xx、排队拒绝（本地容量）、内部异常；
- **算限流**：429、403 无封禁文案、业务码里的「频繁/稍后重试」、空正文（软拦截）；
- **算封禁**：403 + 封禁文案（banned/封禁/风控/suspend…）；
- 判错的代价：把技术问题当封号 → 自锁 6 小时；把封号当普通失败 → 继续撞墙。

## 6. 参数默认值依据（改之前先看这里）

| 参数 | 默认 | 依据 | 调高的风险 |
|---|---|---|---|
| `concurrency.max_concurrency` | 2 | 账号级并发约 3，留 1 给风控 | 与「串行」冲突，等于主动制造并发特征 |
| `concurrency.queue_wait_sec` | 8 | 突发时宁可拒绝也不撞 hard_timeout | 等待变长，replyer 体感变慢 |
| `antiban.min/max_interval_sec` | 8 / 20 | 人类连续追问的量级 | 低于 8s 开始像脚本 |
| `antiban.max_per_minute` | 3 | 挡突发，与串行一致 | 最容易被检出的维度，别动 |
| `antiban.max_per_hour` | 45 | 实测未节流峰值 38/h | 峰值上调等于贴着风控红线跑 |
| `antiban.max_per_day` | 400 | 普通群聊 180~360/天 | 日总量是最容易被判自动化的启发式 |
| `antiban.min_adaptive_budget` | 200 | 不低于旧版硬配额 | 低于它就比升级前更难用 |
| `antiban.quota_wait_max_sec` | 25 | 等窗口滑出即可，不改变上游速率 | 等太久 replyer 会超时 |
| `antiban.quiet_hours / ratio` | 01:00-07:00 / 0.25 | 真人睡觉时段 | 深夜满速是最强机器特征 |
| `antiban.reserve_daily_budget` | 60 | 保证回复始终有额度 | 太小 → 高峰期回复先没额度 |
| `antiban.session_reuse_calls` | 20 | 一个话题的长度 | 太长 → 单会话变长变慢 |
| `credential.health_check_interval_min` | 60 | 探活本身也是请求 | 太频繁是纯噪声开销 |
| `behavior.request_timeout_sec` | 180 | 深度思考可能很久 | 超过 Host hard_timeout 没意义 |

## 7. 测试与验收

```powershell
.testenv\maibot\.venv\Scripts\python.exe -m pytest plugins\deepether-proxy\tests -q
```

- 基线：**272 passed, 4 skipped**（0.4.0）；
- 必须补的用例类型：时间相关（注入时钟）、状态落盘（v1 兼容）、并发（同 key 串行）、错误分类（技术性 vs 风控）、hook（不抛异常 / 目标缺失不改写）；
- 真机验收（需真实 token，不进 CI）：凭证有效 → 单轮回复 → 额度/冷却行为 → 让位与回切 → 状态命令字段。

## 8. Recipes

**① 加一个配置项**：`config.py` 加 Field（默认值 + 中文说明）→ `config.toml` 同步 → `provider.py` 映射到内部 dataclass → `test_config` 断言默认值 → 本文 §6 补一行依据。

**② 加错误码 / 风控信号**：`errors.py` 加常量 → `web_client` 抛点带 `status` → `antiban.classify_failure` 判断（**先决定它算不算风控**）→ `provider` 日志 → 单测覆盖「算/不算」两条分支。

**③ 接一个新上游**：`docs/research/<upstream>/` 写协议实证（请求头/信封/流/错误）→ `constants.py` 集中所有路径与字段 → `client.py` 只做 IO → `adapter_*.py` 做投影 → `provider.py` 编排（独立 pacing/budget）→ 真机探针脚本。

**④ 调整容量**：先看 `docs/design/ANTIBAN.md` §9/§10 的实测数据 → 只调「时/日」不要动「分」→ 同步更新 §6 依据与状态命令 → 观察一周无 429 再上台阶。

**⑤ 排查上游改版**：`debug.dump_response` 打开 → 看 SSE 原始帧 → 对照 `docs/research/commandcode/PROTOCOL-EVIDENCE.md` 或 `plugins/deepether-proxy/docs/upstream-protocol.md` → 只改 `constants.py` 与 `upstream/`。

## 9. 已知限制与「明确不做」

| 项 | 状态 |
|---|---|
| 会话上下文复用（parent_message_id 串链） | **不做**（D-005）：只省上传体积、不省额度，风险高 |
| 多账号 token 池 | 计划中（D-006）：内部轮换，对外一个模型名 |
| 额度耗尽静默换模型 | ✅ 已实现（0.5.0，D-004）：只覆盖 replyer；后台任务按约定保持原样 |
| CommandCode 多账号 | **永不做**（条款红线） |
| 兜底超时精确裁剪 | 受 SDK 限制：`timeout_ms` 只约束插件等待，不约束官方模型请求 |
| 递归守卫跨进程 | `contextvars` 不跨 Host→Runner，只能靠「兜底只直选模型名」约束 |
| 启动自检 | 只做凭证探活（建会话/PoW 有成本，不自动跑） |
| 统计口径 | 进程内累计，重载清零 |

## 10. 文档规范

见 [README.md](README.md) §2/§3：先研究 → 再决策 → 再实现 → 留验收 → 记踩坑；被否决方案保留。

## 11. 术语

| 术语 | 含义 |
|---|---|
| 通道 / Provider | 一个 client_type 对应一条上游通道 |
| 接线 / wire | 把 `deepether-*` 或 `cc-*` 插进任务 `model_list` |
| 让位 | 无容量时把自己从 `model_list` 临时摘掉，恢复后插回 |
| 摘线 | 让位的具体动作（从 model_list 移除） |
| 节奏 / pacing | 串行 + 抖动 + 配额 + 冷却的总称 |
| 有界等待 | 分/时配额打满时短暂等待窗口滑出，而不是立刻失败 |
| 归位 / 回切 | 容量恢复后把模型插回首位、把 hook 改写撤销 |
