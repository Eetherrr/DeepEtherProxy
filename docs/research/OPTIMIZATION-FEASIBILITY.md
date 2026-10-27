# 三项优化需求 · 可行性分析

> 状态：**待决策 / 部分已决策**（结论已落入 records/DECISIONS.md 的 D-004 / D-005 / D-006）
> 日期：**2026-10-27**　基线版本：**0.4.0**（同一插件内两个 Provider：com.ether.deepether 网页版 / com.ether.commandcode CLI 网关）
> 性质：**只做调研，不改代码**。文末给实施顺序与工作量，确认后再动工。
> 调研方式：读插件代码 + 读 MaiBot 1.3.11 宿主源码 + 引用 docs/research/session-reuse.md 的真实账号实测结论。

## 0. 结论速览

| # | 需求 | 可行 | 推荐做法 | 工作量 | 主要风险 |
|---|---|---|---|---|---|
| 1 | 额度耗尽时让 MaiBot 更优雅地换模型 | 可行 | **主：Hook 预改写 model_name**（maisaka.replyer.before_request，BLOCKING）→ 本轮直接指定官方模型，零 error/traceback；**辅：配置摘线让位**覆盖 utils/memory 等无 hook 的任务；再加「保证有官方兜底」 | **约 3 人日**（H 1.0 + B 1.0 + A/C 0.4 + 测试 0.6） | hook 只覆盖 replyer；摘线会写用户配置（热重载竞态） |
| 2 | 会话上下文复用（真正串上下文） | 可行，协议已实测 | **对话指纹 + 严格前缀续聊**（parent_message_id 链），默认关闭、只开 replyer | **3~4 人日** | parent 失效若被误判成限流会烧 15 分钟冷却；串台 |
| 3 | 多账号 token 池 | 技术上可行 | **插件内部轮换**（对外仍是 deepether-replyer 一个模型名）：每账号独立 pacing / 熔断 / 会话 / AIMD，只在「当前账号冷却」时切换 | **5~5.5 人日**（MVP） | 同 IP 多账号本身是强特征，代码消除不了；条款风险 |
| — | 三条共同点 | — | 都不需要改 MaiBot 主程序；都落在插件侧 | — | — |

**一句话**：1 是「改路由表」，2 是「改会话方式」，3 是「加账号维度」；三者互不冲突，建议按 **1 → 3 → 2** 的顺序做（1 见效最快、3 直接解决额度、2 是体验与反封禁优化）。

---

## 1. 需求 1：额度耗尽时「优雅换模型」

### 1.1 现在到底发生了什么（完整链路，逐层举证）

一次网页版额度耗尽，除 /deepether-status 之外，MaiBot 日志里会出现 **3 条 error + 1 条 warning**：

| 层 | 位置 | 内容 | 级别 |
|---|---|---|---|
| 插件 | deepether/provider.py:310 | 「网页版调用失败（原因：pacing_quota: 网页版调用额度已用完（天级上限 400 次），本次改用 Host 官方模型）」 | warning |
| **Runner** | plugin_runtime/runner/runner_main.py:2223 | 「插件 ... LLM Provider ... 执行异常: [pacing_quota] ...」，exc_info=True → **带完整 Python traceback**（实测单行可达 3.4k 字符）；同一事件被 runner 进程与转发通道各打一条，故 **error x2** | **error** |
| Host | llm_models/model_client/plugin_client.py:164-187 | 所有插件异常统一转成 RespParseException（error.code / retryable 被丢弃） | — |
| Host | llm_models/utils_model.py:1227 | 「任务 'replyer' 的模型 'deepether-replyer' 在响应解析多次失败后仍然失败。」 | **error** |
| Host | llm_models/utils_model.py:1442 | 「模型 'deepether-replyer' 尝试失败，切换到下一个模型。原因: 模型 … 重试耗尽」 | warning |
| Host（无兜底时） | utils_model.py:1452 + :1455 | 「所有 N 个模型均尝试失败。」+ emit_llm_error（WebUI/监控事件） | **error + 事件** |

**关键结论**：

- 「切换模型」本身是正常机制（warning 级），用户看到的「抛错」= Runner 的 traceback + Host 的解析失败 error；
- 插件**无法**通过返回 success=false / error.code 改变日志级别（Runner 只认「抛没抛异常」，envelope.py:276-282）；
- 想「零 error」，唯一合法的方向是**让本插件这一轮根本不被调用**。

### 1.2 好消息：MaiBot 官方提供了「在 replyer 请求前换模型」的 Hook

maisaka.replyer.before_request（BLOCKING，允许改参、不允许 abort）在 replyer 真正请求模型前触发；阻塞处理器返回 modified_kwargs 即可改写：

- **model_name**：本次请求指定的具体模型，「指定后只尝试该模型一次，不再按任务模型池轮换」（docs/_ref/maibot/hooks.md:298）；
- task_name / extra_prompt / reply_tool_args 亦可改写（同处 :297-301）；
- Host 实现：maisaka_generator_base.py:1093-1118 取 before_request_result.kwargs → :1115-1118 覆盖 task_name/model_name → utils_model.py:913-922 指定模型后只选它、:1346 只尝试一次；
- Hook 抛异常只会被 except 成一条 warning 然后按原参数继续（maisaka_generator_base.py:1111-1113），不会打断回复。

→ 也就是说：**插件在本地判定「网页版没容量」时，可以把本轮 model_name 改写成一个官方模型名，本轮完全不碰网页版**：0 抛错、0 traceback、0 重试、0 失败快照；额度恢复后下一条请求自动改回去（无状态写入、不改用户配置）。

### 1.3 候选方案对比（更新后）

| 方案 | 做法 | 能否消掉 error/traceback | 覆盖面 | 代价 | 评价 |
|---|---|---|---|---|---|
| **H. Hook 预改写 model_name（推荐）** | 注册 maisaka.replyer.before_request（BLOCKING），无容量时把 model_name 改成官方模型 | **完全消掉** | **仅 replyer**（utils/memory/fast_model 无此 hook） | 每轮 1 次本地 RPC（毫秒级）；需要知道官方模型名 | 最优雅，官方推荐用法 |
| **B. 配置摘线（让位）** | 无容量时把本插件模型从任务 model_list 移除，恢复后插回 | **完全消掉** | **全部任务**（含 utils/memory） | 改写用户 model_config（有备份/原子写/热重载），需状态机 + 迟滞 | 补 H 覆盖不到的后台任务 |
| A. 保证官方兜底 | 每个接线任务 model_list 末尾至少 1 个官方模型 | 只消掉「所有模型失败」+UI 事件 | 全部 | 极小 | **基础卫生，必做** |
| C. 插件侧日志节流 | 同一原因 N 分钟一条 | 只减少插件那条 | 全部 | 极小 | 辅助 |
| D. selection_strategy=balance | 靠 Host 惩罚权重避让 | 不能（首次仍失败） | 全部 | 0 | 不建议：惩罚只增不减，等于一次失败永久冷落 |
| E. 返回占位成功 | 返回「额度已用完」当正文 | 能 | 全部 | 0 | 否决：污染回复/记忆，违背仓库「不用 fallback 掩盖错误」的约定 |
| e2. 内部转 CommandCode | 无容量时改用 cc 通道 | 能 | replyer 及所有接 cc 的任务 | 与 0.3.0「插件就是普通 Provider」的架构决定冲突，需开关 + 预算护栏 | 可选 P1，只在有 cc 预算时考虑 |

**推荐组合：H（replyer）+ B（后台任务兜底）+ A + C。**

### 1.4 H 的落地细节

1. **判定源**：复用 provider 的 pacing 快照（used / limits / budget_scale / in_cooldown）与熔断状态，封装 capacity_state() → ok / exhausted / cooling（只读、不发请求、不睡眠）；
2. **官方模型名从哪来**：优先读新增配置 behavior.official_fallback_model；留空时由 provision（已能读 model_config）取该任务 model_list 里第一个非本插件模型并缓存；两者都没有 → 不改写（退回现状，只留 warning）；
3. **改写逻辑**：hook 内「无容量 且 目标模型存在」时 kwargs["model_name"] = 目标；只在**状态跃迁**时打一条 info（不是每次）：「网页版无容量（日额度耗尽），本轮 replyer 已改走 deepseek-v4-pro-think」；
4. **必须不抛异常**：整个 hook 包 try/except，异常时原样返回 kwargs（Host 侧虽有一层兜底，但不要依赖它）；
5. **超时**：HookHandler 支持 timeout_ms，建议 800ms（hook 只做本地判断，正常都是微秒级）；
6. **幂等与并发**：只读缓存 + 纯函数式改写，无共享写；与池化（需求 3）组合时，「选哪个官方模型」可由账号池状态决定。

### 1.5 B 的落地细节（覆盖 replyer 以外的任务）

- 触发：日额度耗尽 / 冷却中 / budget_scale 到下限 → 把本插件模型从**已接线任务**的 model_list 移除；
- 恢复：定时检查 + 迟滞（连续 N 分钟有容量才插回），避免反复改写；
- **与自动注册的协作（容易踩的坑）**：插件启动时会 run_provision 重新接线（plugin.py:133、412-449），会覆盖让位效果；让位状态必须持久化，并在 provision 之后重新应用一次，否则「重启后第一条请求又报错」；on_config_update 只处理 scope=self（plugin.py:163-164），写 model_config 触发的 scope=model 更新不会引起自动注册回环；
- 安全：只动自己注册的模型名（deepether-* / cc-*），不删模型定义（WebUI 里仍可见，只是任务不再选它）；写失败就回退到现状；
- 状态可见：「通道让位中：utils/memory 已临时改走官方模型（原因：日额度耗尽，预计 03:12 恢复）」+ 手动恢复入口（/deepether-setup 已有）。

**CommandCode 通道**：它的 BUDGET_EXHAUSTED（commandcode/provider.py:458）同属额度类失败；同一个 hook 也能把 model_name 指向官方模型，B 亦可共用。

### 1.6 已知缺口与风险

| 风险 / 缺口 | 说明 | 缓解 |
|---|---|---|
| 目标模型名不存在 | hook 指定了 model_config 里没有的名字 → Host 报错（utils_model.py:1516-1531） | 落地前校验；取不到就不改写 |
| Hook 覆盖不全 | 只有 replyer / planner 有 before_request；utils / memory / mid_memory / fast_model / learner 没有，日额度耗尽时仍会出现那 3 条 error | 用 B 覆盖后台任务；后台失败不影响用户等待，噪音可接受 |
| Hook 自身故障 | 抛异常会被 Host 记 warning 并继续 | hook 内 try/except；只做本地判断 |
| 改用户配置文件（仅 B） | 用户可能手工改过 model_list；WebUI 保存有 last-writer-wins 竞态 | 只动自家模型名；备份；状态里显示改了什么；写失败回滚 |
| 热重载抖动（仅 B） | 频繁改写会让 Host 反复重载（debounce 600ms、最短 1s，config.py:655/711） | 迟滞 + 最小间隔（两次让位/恢复至少 10 分钟） |
| 失败快照堆积 | Host 每次失败写 logs/maisaka_prompt/llm_error/（utils_model.py:1430-1441） | H 生效后不再产生 |

### 1.7 工作量

| 项 | 人日 |
|---|---|
| H：hook 注册 + capacity_state + 目标模型解析 + 状态跃迁日志 | 1.0 |
| B：让位状态机（触发/恢复/迟滞/持久化）+ 复用 provision 写配置 | 1.0 |
| A：兜底校验 + 状态告警；C：日志节流 | 0.4 |
| 单测（容量判定/hook 改写/目标缺失不改/让位往返/写失败回滚/状态渲染） | 0.6 |
| **合计** | **约 3.0** |

---

## 2. 需求 2：会话上下文复用

### 2.1 现状：只复用了「会话壳」，没有复用上下文

用户看到的完全正确：

| 事实 | 位置 |
|---|---|
| 会话状态只有**全局单槽**两字段：_session_id / _session_uses | web_client.py:123-125 |
| acquire_session：次数没到上限就直接返回旧 session_id，否则新建 | web_client.py:426-451 |
| complete() 请求体**硬编码 parent_message_id: None**，且没有该参数入口 | web_client.py:557-565（parent 在 :563） |
| 每轮都发**全量 prompt**（渲染结果），不是增量 | provider.py:471-489 |
| invalidate_session 不区分 key，任何失败都作废整个会话 | web_client.py:453-457、provider.py:486-489 |
| 状态命令里与会话相关的展示为**零** | status.py:105-119、134-216 |
| 配置项 session_reuse_calls = 20 只描述「复用同一个会话」 | config.py:234-239 |

**结论**：现存的「复用」= 同一个 chat_session_id 连续用 20 次，但每次仍是会话根节点上的新分支（parent=None），轮与轮之间**没有上下文关联**。所以它只降低了「建会话次数」，没有让模型记住上一轮。

### 2.2 上游机制（引用 docs/research/session-reuse.md 的真实账号实测）

- 消息是链式的，parent_message_id 填**上一条 assistant 消息 id** 即可续聊（实测：记住 42 → ×2 → 84 正确）；
- 每轮第一帧带 request_message_id / response_message_id，后者就是下一轮的 parent；
- **只发增量**即可延续上下文（这是唯一真正的带宽收益）；
- 非法 parent = **HTTP 200 + 单行 JSON biz_code=26 invalid message id**（不是 SSE）；
- 会话内 accumulated_token_usage **持续累加** → 上游仍按整段会话处理，**不省 token/额度**；
- parent=None 的同一会话消息看不到其它分支；
- GET chat/history_messages 可反查 current_message_id，用于重启后恢复（阶段 2）。

### 2.3 关键约束：插件拿不到「聊天流 id」

Host 发给插件的请求只有 context_items / model_info / extra_params / tool_options / logical_turn_id，**没有任务名、没有群/会话 id**（实测 fixture：tests/fixtures/request_utils.json 的 meta 只有 item_id/logical_turn_id/timestamp）。

→ 对话身份只能从内容推导：**对比内容指纹 + 前缀**。这决定了设计必须「宁可少续，不可串台」。

### 2.4 MVP 设计（详见 docs/research/session-reuse.md §4）

1. 新模块 deepether/session_reuse.py：LRU(8) + TTL(1800s) + 每会话 max_turns / max_chars，进程内不落盘；
2. 隔离键 = sha1(system 文本 + 首条 user 消息) + model_name + thinking + search + model_type；
3. 续聊条件：key 相同 + 新 messages 是缓存列表的**严格前缀**（放宽项：允许**最后一条**内容以旧内容为前缀地增长，否则 replyer 若每轮重写整段会永远不命中）+ 有增量 + 窗口内；
4. 增量 prompt：只拼新增消息 + 当前 system（S2 策略，「以上为最新设定，请以这里为准」）；
5. 失败回退：新增错误码 PARENT_INVALID（区分 biz_code=26 与空正文）→ 作废该 key → 新会话 → 全量重发一次；仍失败才按正常失败路径抛；
6. 开关：[session] enabled=false、only_models="deepether-replyer"（utils 不参与：每次内容不同，前缀天然不成立）；
7. 必须 per-key asyncio.Lock（concurrency=2 时同 key 可能并行）。

### 2.5 收益与边界（避免宣传误导）

| 项 | 现状 | 续聊后 |
|---|---|---|
| 每轮上传体积 | 全量渲染 prompt（数千~数万字符） | 只见增量（几十~几百字符） |
| 建会话往返 | 每 20 次 1 个新会话 | 可降到每会话窗口 1 个（但注意：若把 max_turns 设成 12，反而比现在 20 更频繁） |
| token / 额度 | — | **不省**（上游按整段会话计） |
| 调用次数 | — | **不省**（配额是次数制） |
| 像不像真人 | 同会话多个根分支 | 同一聊天内连续追问，明显更像 |

### 2.6 风险（按严重度）

1. **parent 失效被误判成限流**（最危险）：非法 parent 现在是「HTTP200 + 空正文」→ UPSTREAM_EMPTY → antiban 判 rate_limited → 冷却 900s。实现时必须新增 PARENT_INVALID 并加进 antiban 的技术性失败白名单，且回退在 _call_web 内部完成，不进失败记账。
2. **串台**：现在 session 是全局单槽；串链前必须 per-key 会话，否则 A 群上下文会进 B 群。
3. **max_history_chars 裁剪打碎前缀** → 安全失败（换新会话），但要在日志里可区分。
4. **同 key 并发** → 必须 Lock。
5. **system 漂移**（人格/时间变化）：S2 重发当前 system 可缓解，不能消除。
6. **长会话未压测**（仅 3 轮实测正常），会话上限未知。
7. **可观测性缺口**：状态命令要加 continuations / new_chats / parent_fallbacks。

### 2.7 工作量：3~4 人日

| 项 | 人日 |
|---|---|
| sse.py：StreamSnapshot 增 id + biz_code=26 识别 | 0.3 |
| web_client：complete(parent_message_id=)、PARENT_INVALID、antiban 白名单 | 0.3 |
| session_reuse 新模块（指纹/前缀/LRU/增量渲染/Lock） | 0.8 |
| provider 接线（续聊判定 + 内部回退 + 计数） | 0.5 |
| config + status（6 键 + 3 行状态） | 0.3 |
| 单测（11 组） | 0.7 |
| 真机灰度（5 条，不进 CI） | 0.5 |
| **合计** | **3.4 → 3~4** |

**建议定位**：0.5 实验特性、默认关闭、只开 replyer；先补一份真实 replyer 请求快照验证「追加式」假设，再动工。
---

## 3. 需求 3：多账号 token 池

### 3.1 现状事实

| 事实 | 位置 |
|---|---|
| 凭证是**单值**：CredentialRecord 只有 token/source/updated_at；失效标记整库唯一 | credential.py:3、26-38、161-175、194-199 |
| 但取 token 已经是**每请求回调**（token_provider），轮换不需要动请求链路 | provider.py:111-119、137；web_client.py:172-181 |
| PacingController 注释就写着「一个账号一个实例」，**编排层只建了一个** | antiban.py:272 与 provider.py:214-215、238-245 |
| 熔断器 / 并发闸门也是单例 → 池化后 A 号失败会连坐 B 号 | provider.py:150-174 |
| 会话是全局单槽，而 session_id **与账号绑定**，跨账号复用是最强异常特征 | web_client.py:123-125、426-457 |
| 配置面：credential.web_token 单值；antiban 全为全局标量，已有 CSJSON 字符串先例（model_daily_limits） | config.py:69-107、278 |
| provision 只注册一个 client_type + 两个模型，任务→模型是硬映射 | provision.py:29、128-146、671-681 |
| 文档早已定方向：web_tokens: list[str]、每 token 独立 pacing、仅冷却时轮换、不做并发轮询 | docs/design/ANTIBAN.md:121-124 |
| CommandCode 是**单 Key 硬红线**（条款：多账号 = material breach） | CHANGELOG.md:42；docs/research/COMMANDCODE-FEASIBILITY.md:24/68/80/1135 |

### 3.2 方案对比（5 分制）

| 维度 | A 内部轮换（对外一个模型名） | B 每账号一个模型条目 |
|---|---|---|
| 额度/冷却隔离 | 4（需 per-account pacing，本来就要做） | 4（同样需要） |
| 失败可见性 | 2（Host 只看到一个模型，用户不知哪个号挂了） | 5（每号一个模型，Host 自带按模型统计） |
| WebUI 配置形态 | 4（一个多行字段） | 2（N 个模型 + token 映射，易配错） |
| 状态文件兼容 | 3（v2→v3：accounts 字典） | 4（键用现成模型名） |
| 与 Host sequential 配合 | **5**（对 Host 完全透明） | **2**（只能靠失败后切换，每次多付 8~20s 抖动） |
| 迁移成本 | 3（改 4 文件） | 2（改 6 文件 + 配置面膨胀） |
| **合计** | **21 / 30** | **19 / 30** |

**推荐 A**：两个方案都要付同一份「per-account pacing/session」改造；真正的分歧是「可见性 vs 调度代价」，而 B 的代价恰好落在 replyer 的用户等待路径上。B 可作为二期的「诊断面」（只注册不接线，像 CommandCode 的惯例）。

### 3.3 推荐设计（MVP）

1. **credential.json v2**：{"version":2,"accounts":[{id,label,token,source,updated_at,invalid_reason}]}；读到 v1 单对象就就地按单账号处理（现有容错分支正好可用）。
2. **provider 编排**：_effective_token → _account_for(request)；self._pacing → dict[account_id, PacingController]；_pacing_state_path 写 v3（旧 v2 扁平文件整块归入第一个账号）。
3. **web_client**：_session_id/_session_uses → 按账号分槽；invalidate_session(account_id)。
4. **熔断器按账号键控**（含 abort_probe 路径）；并发闸门保持单例，并在文档写明「池化先解决可用性，不放大吞吐」。
5. **轮换策略**：仅在目标账号进入冷却/额度耗尽时切换；选择「无冷却 + 最久未用」；**绝不做并发 fan-out**；session sticky 到该账号。
6. **失效隔离**：401/40003 只标该账号 invalid（现在是一个全局标记）。
7. **AIMD 必须每账号独立**（共享会让一个 429 砍掉全部账号的日预算）；**夜间节律保持全局**（作息是环境属性，各账号节律不同反而更假）；**session 复用计数必须每账号独立**。
8. **状态命令**：每账号一行（脱敏 token、状态、间隔倍数、近 1 天用量/上限、冷却剩余、最近错误）。
9. **配置形态**：credential.web_tokens 多行（每行 label=token 或纯 token），保留 web_token 作为单账号兼容位；[antiban] 保持全局（per-account override 留二期）。
10. **书签通道**：claim body 增加可选 label，页面加一个输入框；nonce 保持全局一个（它证明「来自本机界面」，不是账号身份）。

### 3.4 风险（诚实评估）

1. **同 IP 多账号是账号群特征，代码消除不了**。真实可用的前提是「账号来自不同自然人/不同网络环境」；同一台机器同一条宽带挂 3 个号，风险高于收益。这条必须写进 README。
2. **隔离做不全，池化会更脆**：共享熔断器会让 A 的 401 停掉 B；共享 invalid 标记会让一个号过期判死全部。
3. **轮换策略是红线**：只要在冷却时切换；按请求轮询/并发保活会把单账号 8~20s 抖动变成 N 倍速率，等于主动制造多账号并发特征。
4. **条款边界**：池化只作用于 DeepSeek 网页版通道；CommandCode 单 Key 到底，不共用这套代码路径。
5. **容量不会线性翻倍**：并发闸门仍是单例（max_concurrency=2），池化先解决「某账号冷却期间整条通道不可用」。
6. **全员冷却 = 回到单账号的失败**：必须保留官方模型兜底。

### 3.5 工作量：MVP 约 5~5.5 人日 + 二期 3 人日

| 项 | 人日 |
|---|---|
| credential v2（含迁移/容错） | 0.5 |
| provider 多账号编排（pacing 字典 + 熔断键控 + 轮换） | 1.5 |
| web_client 会话分槽 | 0.5 |
| 配置字段 + 解析 + 校验 | 0.3 |
| 书签/loopback 多账号投递 | 0.3 |
| 状态命令每账号渲染 | 0.4 |
| 测试（8 组） | 1.0 |
| 文档（ANTIBAN 落地、README 免责） | 0.5 |
| **合计** | **5.0~5.5** |

### 3.6 需要补的测试（8 组）

1. credential v1→v2 迁移；坏 JSON / 空 token 仍按「未配置」处理；
2. 轮换矩阵：A 冷却→用 B；B 冷却→回 A；**A+B 都冷却→立刻失败**（不等待、不并发）；
3. 隔离断言：A 的 401 不改 B 的 state、不打开 B 的熔断；A 的 budget_scale 不影响 B；
4. 状态文件 v3 往返；旧 v2 归入首账号且不丢冷却；重启不刷新额度；
5. 会话**绝不跨账号复用**；reuse 到顶只影响该账号；
6. 闸门不被放大、仍串行；
7. 状态文本 0/1/2 账号三种渲染、脱敏不泄露完整 token；
8. 配置解析：web_tokens 空/单条/多条/含空白/重复去重/与 web_token 并存时的优先级。

---

## 4. 建议实施顺序与决策点

### 4.1 顺序：1 → 3 → 2

| 顺序 | 为什么先做 | 见效 |
|---|---|---|
| **1 优雅换模型** | 约 3 人日、直接影响"日志里一堆错误"和"额度耗尽后仍被反复调用"；H 是纯插件侧（不写用户配置），也是 3 的前置（池化后仍需要"全员耗尽"时的让位） | 立刻：replyer 的 error/traceback 全部消失，日志只剩一条 info |
| **3 多账号池** | 直接解决"额度不够用"；但必须先把 1 的让位与兜底做好，否则池化只是把单点故障变成多点 | 容量与可用性 |
| **2 会话上下文** | 3~4 人日、默认关闭的实验特性；不影响前两项；先补真实 replyer 快照验证假设 | 体验 + 更像真人（不省额度） |

### 4.2 需要你拍板的四件事

1. **官方模型名给谁**：H 需要一个「额度耗尽时改走哪个模型」的名字（配置项 behavior.official_fallback_model）；留空可由 provision 自动探测该任务 model_list 里的第一个非本插件模型。确认用哪种。
2. **是否允许插件改写 model_config.toml**（方案 1-B 的前提，用于覆盖 utils/memory 等无 hook 的任务）。不允许的话，replyer 仍能零 error，但后台任务的 3 条 error 会保留。
3. **多账号的账号来源**：是否真的有 2~3 个不同自然人/不同网络的账号？如果只是同一台机器再登录一次，建议不做池化（风险 > 收益）。
4. **会话上下文复用的范围**：只开 replyer（推荐）、还是也开 utils？是否接受"默认关闭、灰度验证后再开"。

---

## 5. 附录：关键证据索引

| 主题 | 证据 |
|---|---|
| 换模型的日志链路 | runner_main.py:2222-2228（error+traceback）、plugin_client.py:183-184、utils_model.py:1221-1230/1442/1452 |
| 换模型的既有能力 | utils_model.py:1346（max_attempts）、1445（切换）、1455（emit_llm_error） |
| Hook 换模型 | docs/_ref/maibot/hooks.md:251-258、293-301（model_name 指定后只试一次）；maisaka_generator_base.py:1093-1118；utils_model.py:913-922 |
| 失败快照 | utils_model.py:1430-1441（logs/maisaka_prompt/llm_error/） |
| 让位所需能力 | provision.py:398-420（备份/原子写/复核）、613-700（_wire_tasks）、886-908（移除模型） |
| 会话现状 | web_client.py:123-125、426-457、557-565；provider.py:471-489 |
| 会话可行性 | docs/research/session-reuse.md:51-69、80-90、92-102、116-126、132、160-241 |
| 池化现状 | credential.py:26-38、161-175；provider.py:111-119、137、150-174、214-245；web_client.py:123-125 |
| 池化方向 | docs/design/ANTIBAN.md:41、105-113、121-124 |
| 条款边界 | CHANGELOG.md:42；docs/research/COMMANDCODE-FEASIBILITY.md:24/68/80/1135 |