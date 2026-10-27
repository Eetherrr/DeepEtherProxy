# 踩坑库

> 状态：**现行**　最后更新：2026-10-27　适用版本：0.4.0
> 用法：遇到怪现象先按「症状」检索；修补前先看「根因」和「回归」，不要只改表象。
> 收录标准：**会再次发生** 或 **不看文档就会踩**。每条都要有真实证据（file:line 或日志样本）。

## 速查表

| 编号 | 症状 | 一句话根因 | 状态 |
|---|---|---|---|
| P-01 | 改坏 token 后报「建会话接口可能已变更」，凭证仍显示已就绪 | 上游用 HTTP 200 + code=40003 表达鉴权失败，旧代码只看状态码 | 已修复 0.3.2 |
| P-02 | 无效 token 的探活被判「通过」 | probe_user 只校验 HTTP 200，不解析响应体 | 已修复 0.3.2 |
| P-03 | 一次群聊突发后整条网页版通道 60s 全降级 | 排队拒绝被计入熔断失败次数 | 已修复 0.3.2 |
| P-04 | 并发 2 路时约一半请求 pow_failed | deepseek-pow 的 wasm 实例非线程安全 | 已修复 0.3.2 |
| P-05 | 夜间节律对 23:30-06:30 这类区间判错 | 只取小时、忽略分钟 | 已修复 0.3.2 |
| P-06 | 测试在凌晨 01:00-07:00 必红 | 单测没注入时钟，夜间配额生效 | 已修复（测试注入 minute_provider） |
| P-07 | 一次 429 后容量比升级前更小 | AIMD 下限低于旧硬配额 | 已修复 0.3.2 |
| P-08 | 刚被限流，下一秒又满速打 | 并发在途请求的成功把冷却清掉 | 已修复 0.3.2 |
| P-09 | 403 被当成 token 失效，用户重授权也没用 | 403 无封禁文案时一律映射成凭证失效 | 已修复 0.3.2 |
| P-10 | 一次 PoW 失败就丢掉整个会话 | 任何 DeepEtherError 都 invalidate_session | 未修复（低优先，见 D-005 讨论） |
| P-11 | 日志里同一事件出现两次，按行计数会翻倍 | MaiBot 同时写两个 app 日志汇 | 已规避（统计按 timestamp+event 去重） |
| P-12 | 「网页优先」失效，约一半请求静默走官方 API | selection_strategy 默认 random | 已修复（自动注册固定 sequential） |
| P-13 | 把 queue_wait_sec 设 0 后所有请求都被拒 | asyncio.wait_for(timeout=0) 直接超时 | 已规避（配置下限/文档说明） |
| P-14 | 升级后插件配置里的旧字段消失 | config_version 变化时 Host 以新默认值重建 | 已规避（升级前备份 + 文档说明） |
| P-15 | CommandCode 请求 400 Invalid UUID at threadId | 空串 threadId 被网关 zod 校验拒绝 | 已修复 0.4.0 |
| P-16 | PoW 头缺 target_path，上游 200 但 body 40301 | 上游要求 header 里带 target_path | 已修复 0.2.x |
| P-17 | SSE 解析重复/漏字 | 上游是混合帧（裸增量+全量快照+patch），不是纯 JSON-Patch | 已修复 0.2.x |
| P-18 | 自动改写 model_config 后 Host 反复重载 | 文件监视有 debounce，短时间多次写入会抖动 | 已规避（迟滞 + 最小间隔） |
| P-19 | 额度耗尽时日志出现 3 条 error + traceback | Host 把「模型失败」当异常记录；额度只有插件知道 | 已知行为（应对见 D-004） |
| P-20 | 状态里的「失败」数与实际降级次数对不上 | 失败只在最终抛错时 +1，降级/让位不计失败 | 设计如此（口径见 SKILL §5.6） |
| P-21 | 启动日志里反复出现「所有 0 个模型均尝试失败」 | 某任务的 model_list 为空（常见于 embedding / 表达向量索引） | 环境问题（非插件） |

---

## 详细条目

### P-01 HTTP 200 + 40003 被当成协议错误

- **现象**：把 token 改坏后，日志报「protocol_error: 上游没有返回 chat_session_id，建会话接口可能已变更」，而 /deepether-status 仍显示「已就绪」。
- **根因**：上游对无效 token 返回 **HTTP 200** + `{"code": 40003, "msg": "INVALID_TOKEN"}`。旧实现只在 HTTP 401/403 时判鉴权失败，成功路径不解析业务码。
- **证据**：真机 probe（2026-10-21）；web_client._raise_for_envelope 的引入；tests/test_web_client.py::test_invalid_token_envelope_is_credential_invalid。
- **修法**：统一校验响应体 code：40003 / INVALID_TOKEN / AUTHORIZATION FAILED → credential_invalid + 落盘失效标记；其它非 0 码 → protocol_error 并带 code/msg。
- **回归**：token 改坏演练（docs/records/P2-ACCEPTANCE.md 的 17 项检查）+ 4 条单测。
- **状态**：已修复 0.3.2。

### P-02 无效 token 的探活被判「通过」

- **现象**：/deepether-status 显示「探活通过」，但实际对话全部 401。
- **根因**：probe_user 只看 HTTP 状态码，不看响应体；无效 token 同样返回 200。
- **证据**：web_client.probe_user；单测 test_authorization_failed_message_is_credential_invalid。
- **修法**：probe_user 解析 body 并复用同一套 envelope 校验。
- **回归**：单测覆盖「200 + 40003」分支。
- **状态**：已修复 0.3.2。

### P-03 排队拒绝被计入熔断

- **现象**：群聊一次突发（例如 20 条消息）之后，接下来 60 秒内所有请求都以「熔断中」直接降级，网页版完全不用。
- **根因**：闸门排队超时抛 CONCURRENCY_REJECTED，被与「上游失败」一起记进熔断器；连续 3 次就把熔断打开。
- **证据**：provider 的 except 分支；单测 test_queue_rejection_does_not_open_circuit。
- **修法**：排队拒绝属于本地容量信号，不计入熔断失败。
- **回归**：20 并发闸门演练（docs/records/P2-ACCEPTANCE.md）。
- **状态**：已修复 0.3.2。

### P-04 并发求解 PoW 踩 wasm

- **现象**：并发 2 路时约一半请求报 pow_failed（wasm trap: out of bounds memory access / call stack exhausted）。
- **根因**：deepseek-pow 的求解器是进程内共享 wasm 实例，非线程安全；插件用 asyncio.to_thread 并发求解会互相踩线性内存。
- **证据**：复现脚本 6 轮 × 2 并发，修复前失败 6 次、修复后 0 次；单测 test_concurrent_solve_is_serialized。
- **修法**：pow.py 加模块级 threading.Lock 串行求解（单次约 100ms，代价远小于误降级）。
- **回归**：复现脚本 + 单测（断言同一时刻只有一个求解在跑）。
- **状态**：已修复 0.3.2。

### P-05 夜间节律只按整点判断

- **现象**：配置 23:30-06:30 时，23:45 判成「非夜间」、06:45 仍判成夜间。
- **根因**：原来只取 datetime.now().hour，再乘 60 当分钟用。
- **证据**：antiban._quiet_factor；单测 test_quiet_hours_minute_precision。
- **修法**：改为注入 minute_provider（0~1439），按分钟比较。
- **回归**：跨零点 + 分钟边界单测。
- **状态**：已修复 0.3.2。

### P-06 测试依赖系统时间

- **现象**：同一套测试在白天全绿，凌晨 01:00-07:00 跑必红（配额被夜间节律缩小）。
- **根因**：单测构造 PacingController 时没有注入时间源，读取了真实本地时间。
- **证据**：把时间桩设成 03:00 后，6 条既有用例失败（评审实测）。
- **修法**：测试 helper 统一注入 minute_provider=12:00；任何时间相关逻辑都必须可注入。
- **回归**：tests/test_antiban.py 全量（在任意时间跑都应通过）。
- **状态**：已修复 0.3.2。

### P-07 AIMD 下限低于旧配额

- **现象**：一次 429 之后，可用额度比升级前（200/天）还小。
- **根因**：min_adaptive_budget 初值 80、回撤比例 0.6，两次风控就掉到 144。
- **证据**：评审结论；单测 test_adaptive_budget_decays_and_recovers。
- **修法**：下限提到 200；软风控 0.75、封禁 0.5；回补 50 次 +0.1。
- **回归**：AIMD 单测（回撤/回补/下限）。
- **状态**：已修复 0.3.2。

### P-08 并发成功误清冷却

- **现象**：刚收到 429 进入 15 分钟冷却，下一秒又有请求满速打出去。
- **根因**：note_success 无条件清空 cooldown；max_concurrency=2 时另一个在途请求的成功会把刚设的冷却抹掉。
- **证据**：单测 test_ban_cooldown_and_recovery 的第二段（冷却期间的成功不得清封禁）。
- **修法**：记录 _last_admitted_at 与 _cooldown_since，只有「冷却生效之后被放行」的成功才算恢复。
- **回归**：熔断/冷却单测。
- **状态**：已修复 0.3.2。

### P-09 软 403 被当成凭证失效

- **现象**：上游返回 403 但没有任何封禁文案，用户被提示去重新授权；授权后继续撞，且完全没有退避。
- **根因**：web_client 把 401/403 一律映射成 credential_invalid，antiban 又把 credential_invalid 列进「技术性失败」白名单，403→rate_limited 的分支成了死代码。
- **证据**：web_client._handle_error_status 的三分支（封禁 / 软限流 / 鉴权）；DeepEtherError.status。
- **修法**：403 + 封禁文案 → account_risk；403 无封禁且不像鉴权 → upstream_http(retryable) 带 status，交给 antiban 判限流；只有 401 / 明确的鉴权文案才标失效。
- **回归**：test_403_without_ban_text_is_treated_as_rate_limit 等 4 条。
- **状态**：已修复 0.3.2。

### P-10 任何失败都作废会话

- **现象**：偶发 PoW 超时之后，下一条请求立刻新建会话；建会话次数明显偏高（机器特征）。
- **根因**：provider 在 except 里无条件 invalidate_session()，不区分错误类型。
- **证据**：provider 的失败分支；web_client.invalidate_session。
- **修法**（未做）：只在会话相关错误（invalid message id / 建会话失败）时作废；这是 D-005 取消上下文复用后仍值得做的小优化。
- **回归**：待补（断言 PoW 失败不丢会话）。
- **状态**：未修复（低优先）。

### P-11 两个日志汇导致计数翻倍

- **现象**：按日志行统计「降级次数」会得到实际值的 2 倍。
- **根因**：MaiBot 同时写两个 app 日志文件（runner 进程 + 转发通道），同一事件出现两次，时间戳相差 30~130ms。
- **证据**：日志镜像实测；统计脚本按 (timestamp, event) 去重后才对得上。
- **修法**：任何基于日志的统计必须去重；插件自身的计数以 RuntimeState 为准。
- **回归**：验收脚本里的 count_degrade_events。
- **状态**：已规避。

### P-12 selection_strategy 默认 random

- **现象**：「网页优先」失效，约一半 replyer 请求直接走官方模型；日志里看不到降级痕迹，token 却在烧。
- **根因**：MaiBot 的 model_list 选择策略默认 random，随机挑一个模型。
- **证据**：utils_model.py 的 random 分支；自动注册现在固定写 sequential。
- **修法**：自动注册接线时强制 selection_strategy=sequential；README 明确警示。
- **回归**：provision 单测（接线后断言策略）。
- **状态**：已修复（自动注册 + 文档）。

### P-13 queue_wait_sec = 0 等于永远拒绝

- **现象**：把排队等待设成 0 想「不排队」，结果所有请求都被拒。
- **根因**：asyncio.wait_for(coro, timeout=0) 会直接取消协程，即使信号量可用也拿不到。
- **证据**：单测里复现（两个请求全部被拒）。
- **修法**：配置下限与文档说明；测试里用 1 秒而不是 0。
- **回归**：test_gate_rejects_after_queue_timeout 等。
- **状态**：已规避。

### P-14 插件配置版本升级会重建 config.toml

- **现象**：升级插件后，旧配置里的字段「消失」或回到默认值。
- **根因**：Host 检测到 config_version 变化时，以插件最新默认配置为骨架重建并回写；同名且仍存在的字段保留，被删除的字段消失。
- **证据**：0.3.0 移除 [fallback] 时的实际行为。
- **修法**：破坏性配置变更前备份；CHANGELOG 标注「升级前请先备份」；不删字段只加字段时也要考虑重建影响。
- **回归**：无自动化（属于 Host 行为），靠发布检查单。
- **状态**：已规避（流程约定）。

### P-15 CommandCode threadId 空串被拒

- **现象**：请求返回 400 `Invalid UUID at "threadId"`。
- **根因**：网关用 zod 校验 threadId，必须是合法 UUID；空串不行。
- **证据**：docs/research/commandcode/P1-RESULTS.md。
- **修法**：build_envelope 为空时兜底生成 UUID；Provider 侧按 thread_reuse_calls 复用 threadId 吃 prompt cache。
- **回归**：test_cc_protocol / test_cc_provider。
- **状态**：已修复 0.4.0。

### P-16 PoW 头缺 target_path

- **现象**：completion 请求 HTTP 200，但 body 是 `{"code":40301,"msg":"INVALID_POW_RESPONSE"}`。
- **根因**：上游要求 x-ds-pow-response 里带 target_path；SDK 默认的 answer payload 不带。
- **证据**：plugins/deepether-proxy/docs/upstream-protocol.md「关键坑」。
- **修法**：solve_pow 组装 payload 时补 target_path；answer 用整数形态。
- **回归**：test_pow + 真机。
- **状态**：已修复。

### P-17 SSE 是混合帧

- **现象**：按 JSON-Patch 解析会重复拼接或漏字。
- **根因**：上游混了三种帧：裸增量 `{"v": "文本"}`、全量状态快照、patch；裸增量没有路径，必须靠「当前片段类型」分流。
- **证据**：docs/upstream-protocol.md；upstream/sse.py。
- **修法**：维护当前片段类型 + 前缀重同步。
- **回归**：tests/test_sse.py。
- **状态**：已修复。

### P-18 热重载 debounce 与自动改写

- **现象**：频繁自动改写 model_config.toml 会让 Host 反复重载，日志刷屏、短暂不可用。
- **根因**：Host 文件监视有 debounce（约 600ms）与最短间隔（约 1s），并会清客户端缓存。
- **证据**：Host 配置监听实现；provision 的「内容无变化不落盘」策略。
- **修法**：任何自动改写加迟滞与最小间隔（建议 ≥10 分钟），并保证幂等。
- **回归**：provision 单测（幂等、无变化不落盘）。
- **状态**：已规避（策略约定）。

### P-19 额度耗尽必然产生 Host error

- **现象**：网页版额度用完后，每条请求都会出现 Runner 的 error + traceback、Host 的解析失败 error、切换 warning，以及失败快照。
- **根因**：只要让 Host 判定模型失败，就必然走这套日志；插件无法自报「soft fail」（success 由 Runner 写）。
- **证据**：runner_main.py:2223；plugin_client.py:184；utils_model.py:1227/1442/1452。
- **修法**（计划）：replyer 用 hook 预改写 model_name；其它任务摘线；保证官方兜底。见 D-004。
- **回归**：待实现后补「额度耗尽 0 error」验收。
- **状态**：已知行为（未修复）。

### P-20 统计口径：失败 ≠ 降级

- **现象**：状态命令里的「失败」数与用户感知的「降级次数」不一致。
- **根因**：失败计数只在最终抛错时 +1；降级/让位成功不计失败；降级另有一套时间窗计数。
- **证据**：state.py 的 record_failure/record_fallback；status 渲染。
- **修法**：排查时以「降级」与「最近 24 小时」为准；不要拿「失败」倒推降级。
- **回归**：test_status 渲染断言。
- **状态**：设计如此（口径说明）。

### P-21 空 model_list 造成的启动噪音

- **现象**：启动日志里反复出现 @B@所有 0 个模型均尝试失败。@B@，并伴随 @B@表达向量历史补建任务异常退出@B@。
- **根因**：某个任务的 @B@model_list@B@ 是空的（embedding / 表达向量索引最常见），Host 自然「尝试 0 个模型」。
- **证据**：真实日志 41 次（均为 expression_vector_index），与 deepether 无关。
- **修法**：给 embedding 配一个向量模型，或忽略这条（不影响对话）。排障时**不要**把它算到插件头上。
- **回归**：无（属于部署配置）。
- **状态**：环境问题（非插件缺陷）。

---

## 怎么新增一条

1. 复现一次，记下**现象原文**（日志/状态截图文字）；
2. 定位到**机制**（不是「某行代码」），写清为什么；
3. 贴**证据**（file:line 或日志样本）；
4. 写**修法**与**回归**（哪条测试/演练能挡住复发）；
5. 更新顶部速查表。
