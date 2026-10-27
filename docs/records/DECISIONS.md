# 决策日志

> 状态：**现行**　最后更新：2026-10-27　适用版本：0.4.0
> 规则：**被否决的方案不删除**。每条决策都要有「背景 / 选项 / 决定 / 影响 / 证据 / 回滚」。

## 索引

| 编号 | 日期 | 主题 | 状态 |
|---|---|---|---|
| D-001 | 2026-10-22 | 移除插件内 fallback，失败交给 Host 调度 | 采纳（0.3.0） |
| D-002 | 2026-10-22 | 容量模型重构（400/天 + AIMD + 夜间 + 按模型 + 保留 + 有界等待） | 采纳（0.3.2） |
| D-003 | 2026-10-27 | CommandCode 单 Key、只注册不接线 | 采纳（0.4.0） |
| D-004 | 2026-10-27 | 额度耗尽时静默换模型：replyer hook | **采纳（0.5.0，范围收窄）** |
| D-005 | 2026-10-27 | 会话上下文复用（parent_message_id 串链） | **否决** |
| D-006 | 2026-10-27 | 多账号 token 池：插件内部轮换 | **采纳（0.5.0）** |
| D-007 | 2026-10-27 | 文档分层与项目 SKILL | 采纳 |
| D-008 | 2026-10-27 | 自动注册改为「只注册模型、不接线」 | 采纳（0.5.0） |
| D-009 | 2026-10-27 | CommandCode 配置只保留一个 Key | 采纳（0.5.0） |

---

### D-001 移除插件内 fallback，失败交给 Host 调度

- **日期 / 状态**：2026-10-22 / 采纳（0.3.0）
- **背景**：0.2.x 的插件会通过 ctx.llm.generate 回调 Host 官方模型做兜底，带来三个坑：递归（兜底模型又指回插件）、超时叠加（外层 RPC + 内层兜底）、prompt 形态不一致（context_items 与 messages 两套）。
- **选项**：
  - A. 保留插件内 fallback（自己兜）；
  - B. 插件只做普通 Provider，失败抛错，由 Host 的 model_list 切换；
  - C. 插件内部直接转另一条通道（CommandCode）。
- **决定**：B。插件不再回调 ctx.llm，manifest 只保留 send.text 能力。
- **影响**：
  - 失败会走 Host 的标准路径 → **必然产生 error 日志**（这一后果由 D-004 解决）；
  - 每个接线任务的 model_list 必须有官方兜底，否则会出现「所有模型均尝试失败」；
  - 自动注册把 max_retry 写成 1，避免同一模型重试 3 次。
- **证据**：CHANGELOG 0.3.0；deepether/fallback.py 已删除；tests/test_provider.py 断言 Provider 不碰 ctx.llm。
- **回滚**：不建议回滚；如需兜底，优先用 C（独立通道）而不是回到 A。

### D-002 容量模型重构

- **日期 / 状态**：2026-10-22 / 采纳（0.3.2）
- **背景**：用户反馈「一天 200 次不够用」。实测（2026-10-21 真机日志）：每条回复 **3.00 次**网页版调用（replyer 0.83 + utils 2.17，utils 占 74%）；未节流峰值 9 次/分、38 次/时；200/天只够 53~67 次回复。
- **选项**：
  - A. 只把 200 改成 600；
  - B. 抬上限 + 四个「提容量不加风险」的机制；
  - C. 砍掉 utils 的网页版路由（调用量 -39%）。
- **决定**：B（日 200→400、时 30→45），配 AIMD（软 0.75 / 硬 0.5、成功 50 次 +0.1、下限 200）、夜间节律（01:00-07:00 ×0.25）、按模型配额、额度保留（60 次给 replyer）、分/时配额有界等待 25s。C 保留为可选项（model_daily_limits / wire_tasks）。
- **影响**：上限提高但仍受「分/时」硬约束；日总量依旧是最容易被判自动化的启发式，所以 AIMD 会自己回撤。
- **证据**：[design/ANTIBAN.md](../design/ANTIBAN.md) §10（实测画像）、CHANGELOG 0.3.2。
- **回滚**：把 max_per_day / max_per_hour 调回，或关 adaptive_budget。

### D-003 CommandCode 单 Key、只注册不接线

- **日期 / 状态**：2026-10-27 / 采纳（0.4.0）
- **背景**：Go 套餐的 Key 在官方 Provider API 上返回 403 upgrade_required，只能走官方 CLI 网关 POST /alpha/generate；官方条款限制单账号、禁止自动化滥用。
- **选项**：
  - A. 支持多 Key 池；
  - B. 单 Key + 只注册模型、不自动接线；
  - C. 不做这条通道。
- **决定**：B。默认 register_models = all（56 条），可选 curated / none；任务接线由用户自行在 WebUI 决定；预算 5h/7d/30d = $3/$6/$9 + $0.5 保留。
- **影响**：CC 通道适合 planner（有工具调用），但用户必须自己选模型；**多账号池永远不适用于 CC**。
- **证据**：CHANGELOG 0.4.0「风险提示」；[research/COMMANDCODE-FEASIBILITY.md](../research/COMMANDCODE-FEASIBILITY.md)。
- **回滚**：关 [commandcode] enabled 即可，不影响 DeepEther 通道。

### D-004 额度耗尽时静默换模型：replyer hook + 后台摘线

- **日期 / 状态**：2026-10-27 / **采纳（0.5.0）**。最终只做 replyer 的 hook 让位：用户明确「不要后台、不要兜底，用户自己会配多个模型」
- **背景**：网页版额度耗尽时，每次请求都会产生 3 条 error（Runner 带 traceback 的 error ×2、Host 解析失败 error）+ 1 条 warning + 一份失败快照。原因是 Host 只有在「模型失败」后才切换，而额度只有插件知道。
- **选项**：
  - A. 靠 selection_strategy（random / balance）——策略不认识额度，balance 的惩罚分抵不过一次回复的 token 量，且首次仍失败；
  - B. 插件返回占位成功——污染回复与记忆，违反 D-001；
  - C. 配置摘线（把自己从 model_list 移除）；
  - D. maisaka.replyer.before_request hook 预改写 model_name；
  - E. 内部转 CommandCode 通道。
- **决定**：只做 **D（replyer hook 预改写 model_name）**。C（后台摘线）与「保证官方兜底」按用户要求**不做**：用户会在任务 `model_list` 里自己配多个模型；A 不作为主方案，B 否决，E 作为可选 P1。
- **影响**：
  - replyer：额度耗尽那一刻起完全不调用网页版，日志只剩插件一条 info，额度恢复后逐请求自愈；
  - 后台任务：日额度耗尽时摘线、恢复后插回，一天最多改写 2 次（带迟滞）；
  - 需要新增配置（官方兜底模型名）与让位状态机（持久化，且 provision 之后要重新应用）。
- **证据**：[research/OPTIMIZATION-FEASIBILITY.md](../research/OPTIMIZATION-FEASIBILITY.md) §1；docs/_ref/maibot/hooks.md:298；runner_main.py:2223；utils_model.py:1227/1442/1452。
- **实现**：plugin.py 的 `maisaka.replyer.before_request`（BLOCKING，800ms）+ provider 的 `capacity_block_reason()` / `replyer_yield_target()`；目标模型优先取 `behavior.replyer_fallback_model`，留空则自动取 replyer 任务 `model_list` 里第一个非本插件模型；只在状态跃迁时打一条 info。
- **回滚**：把 `behavior.replyer_fallback_model` 置空且任务列表里没有官方模型时，自动回到「失败一次再切」的现状。

### D-005 会话上下文复用：否决

- **日期 / 状态**：2026-10-27 / **否决**
- **背景**：上游支持 parent_message_id 串链（已实测），但当前实现只复用 chat_session_id（20 次），每轮全量 prompt、parent_message_id 恒为 None。
- **选项**：
  - A. 实现「对话指纹 + 严格前缀续聊」；
  - B. 维持现状（只复用会话壳）；
  - C. 直接把 session_reuse_calls 调大。
- **决定**：B。
- **理由**：
  1. 收益只有「少传一点 + 更像真人」，**不省 token/额度**（上游按整段会话计费）、也**不省调用次数**；
  2. 代价是串台、biz_code=26 失效、前缀校验、system 漂移一整套状态；
  3. parent 失效当前会冒成「空正文」，被 antiban 误判成限流 → 白烧 15 分钟冷却；
  4. 与项目定位一致：replyer/utils 的上下文由 MaiBot 组装好整段发来，插件只负责转发。
- **影响**：session_reuse_calls 仍然只控制「换会话频率」；[research/session-reuse.md](../research/session-reuse.md) 保留为已否决研究稿。
- **证据**：web_client.py:443-451, :563；[research/session-reuse.md](../research/session-reuse.md)。
- **回滚**：若将来重启该需求，按研究稿的 MVP（3~4 人日）执行，并先补 replyer 真实快照。

### D-006 多账号 token 池：内部轮换

- **日期 / 状态**：2026-10-27 / **计划中**（尚未实现）
- **背景**：单账号额度与冷却会把整条通道变成单点；用户可配置多个网页版凭证。
- **选项**：
  - A. 插件内部轮换（对外仍是一个模型名）；
  - B. 每账号一个模型条目（交给 Host model_list 选）。
- **决定**：A。B 只作为可选「诊断面」（只注册不接线）。
- **关键约束**：
  - 每账号独立：pacing（额度/冷却）、熔断、AIMD、失效标记、会话缓存；
  - 全局共享：夜间节律、并发闸门（max_concurrency 不变）；
  - **只在当前账号冷却/额度耗尽时切换**，绝不并发 fan-out；
  - 同 IP 多账号是账号群特征，代码无法消除 → README 必须写明风险，建议 2 个账号起步；
  - CommandCode 不参与（D-003）。
- **实现（0.5.0）**：`credential.web_tokens` 为数组；provider 按账号维护 client / pacing / circuit / last-used，按「不在熔断 → 最久未用」轮换；额度/冷却/token 失效时切下一个账号，其它错误不轮换；节奏状态按账号分文件。
- **剩余**：状态命令逐账号渲染（当前只展示第一个账号）。
- **证据**：[research/OPTIMIZATION-FEASIBILITY.md](../research/OPTIMIZATION-FEASIBILITY.md) §3；[design/ANTIBAN.md](../design/ANTIBAN.md) §7.1。
- **回滚**：保留 web_token 单账号字段作为兼容位；关掉池化开关即回到单账号。

### D-007 文档分层与项目 SKILL

- **日期 / 状态**：2026-10-27 / 采纳
- **背景**：文档散落在 docs 根目录，类型混杂（研究、设计、验收、历史），新需求容易找不到依据。
- **决定**：按类型分目录 —— design/（现行设计）、records/（决策与验收）、pitfalls/（踩坑库）、research/（专题研究，可能过时）、_ref/（外部快照）；根目录保留 README.md（索引）与 SKILL.md（规范）；新增 [records/DECISIONS.md](DECISIONS.md) 与 [pitfalls/PITFALLS.md](../pitfalls/PITFALLS.md)。
- **影响**：所有相对链接已重写并通过断链校验（30 个链接，0 断链）；后续新需求必须按 README §2 的流程留档。
- **证据**：本次整理脚本 .testenv/tools/reorg_docs2.py；备份在 .testenv/backup_docs_*。
- **回滚**：从备份目录整体还原。

### D-008 自动注册改为「只注册模型、不接线」

- **日期 / 状态**：2026-10-27 / 采纳（0.5.0）
- **背景**：0.2.4~0.4.0 的自动注册会把 `deepether-*` 插进任务 `model_list` 首位并强制 `sequential`。用户要求：注册只负责「把模型放进模型列表」，任务用哪个模型由用户在 WebUI 自己选——与 CommandCode 通道的做法对齐，同时让配置更简单。
- **决定**：
  - 只确保 Provider 与 `[[models]]` 存在；**不改任何任务的 `model_list` / `selection_strategy`**；
  - 移除 `provision.wire_tasks` 与 `provision.selection_strategy`，改为 `provision.register_models`（replyer / utils / all / none，默认 replyer,utils）；
  - 仍清理 0.3.0 遗留的 `deepether-planner`（模型定义 + 任务引用）——它无法承担工具调用，留着只会误导。
- **影响**：开箱即用变成「注册好了，去 WebUI 里选」；用户必须自己确认任务的 `selection_strategy = sequential` 才会优先走网页版（README 有说明）。
- **证据**：provision.py 的 `parse_register_models` / `read_task_models`；tests/test_provision.py 断言「任务配置一个字节都不改」。
- **回滚**：需要自动接线时，改回旧版 provision（备份见 .testenv/backup_docs_* 与 CHANGELOG 0.4.0）。

### D-009 CommandCode 配置只保留一个 Key

- **日期 / 状态**：2026-10-27 / 采纳（0.5.0）
- **背景**：0.4.0 的 `[commandcode]` 有 20+ 个字段（上游地址、CLI 版本、超时、并发、三档预算、注册范围、视觉/工具策略…），与「配置要简单、开箱即用」冲突。用户明确：这条通道只需要填一个 Key。
- **决定**：`CommandCodeConfig` 只保留 `api_key`；其余参数改为代码常量（`bundle` 默认值：$3/$6/$9 + $0.5 保留、并发 2、每分钟 6、`max_tokens` 8192、注册 `all`、`vision_mode=strip`、`tool_mode=auto`、thread 复用 50 次）。没有 Key 时不注册 `cc-*` 模型，状态里也不显示 CC 行。
- **影响**：用户侧只需 `api_key` 一项；想改预算/并发得改代码常量（属于高级定制，不走配置）。已有配置在版本升级时被重建，旧字段消失。
- **证据**：config.py 的 `CommandCodeConfig`；provision.cc_settings_from_config 以 Key 判断是否注册；tests/test_cc_provider.py、test_cc_provision.py、test_config.py。
- **回滚**：若将来需要按用户调预算，再加回单个 `budget_usd` 字段即可，不必恢复整套字段。
