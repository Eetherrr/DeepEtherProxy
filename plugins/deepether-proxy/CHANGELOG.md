# 更新日志

## 0.5.0（2026-10-27）

**主要功能**

- **额度耗尽时静默换模型（只做 replyer）**：新增 hook `maisaka.replyer.before_request`（BLOCKING，800ms）。
  网页版额度用完 / 冷却 / 熔断时，把本轮 `model_name` 改成**你自己 model_list 里的下一个非本插件模型**；
  插件不持有「兜底模型」配置、不回调其它模型。效果：不再产生 error/traceback/失败快照，只留一条 info；
  额度恢复后下一条请求自动回到网页版
- **自动注册固定为「只注册模型、不接线」**：只确保 Provider 与 `deepether-replyer` / `deepether-utils`
  两条模型存在，**不再有 register_models 开关**，也不改任何任务的 `model_list` / `selection_strategy`；
  哪个任务用哪个模型由用户在 WebUI 自己选（与 CommandCode 通道一致）
- **多账号池**：`credential.web_tokens` 改为**数组**（`list[str]`），可填多个 token。
  每个账号独立 pacing / 熔断 / 会话 / 状态文件；按「不在熔断中 → 最久未用」排序，
  只在额度用完 / 冷却 / token 失效时切下一个账号；其它错误不轮换。全局并发闸门不变

**细节**

- 移除 `behavior.replyer_fallback_model`：让位目标完全由你自己 model_list 的顺序决定
- 移除 `provision.register_models` 与旧版 `wire_tasks` / `selection_strategy`
- `credential.web_token` → `credential.web_tokens`（数组）；书签落盘的单条 token 仍作为兜底来源
- 节奏状态按账号分文件（`antiban_state_<account>.json`；单账号仍用 `antiban_state.json`）
- 新增 `provision.read_task_models()` / `read_model_providers()`：只读任务配置，供让位判定使用（60s 缓存）
- `config_version` 0.4.0 → **0.5.0**（升级会以新默认值重建 config.toml；升级前请备份，`web_tokens` 需要重新填一次）
- **`/deepether-status` 精简为 4~5 行**：只保留 版本/运行时长、凭证、网页版用量、CommandCode 预算、最近错误
  （书签、自适应预算、夜间节律、按模型用量、探活、自动注册明细全部移出聊天）
- **只保留 `/deepether-status` 一个命令**：删除 `/deepether-setup`、`/commandcode-status`、
  `/commandcode-models`、`/commandcode-setup`；重跑注册改为重载插件
- **CommandCode 配置只保留一个 Key**：`[commandcode]` 从 20+ 个字段裁到 `api_key` 一项，
  其余（上游地址、CLI 版本、超时、并发、三档预算、模型注册范围、视觉/工具策略）全部改由代码常量提供；
  **没有 Key 时不再注册 `cc-*` 模型**，状态里也不显示 CC 行
- **让位 hook 覆盖 CommandCode**：replyer 第一个模型是 `cc-*` 且 CC 预算/熔断不可用时，同样改走列表里的下一个模型；
  `/deepether-status` 增加一行 CC 摘要（5 小时预算 + 熔断）
- 单测覆盖：让位目标（顺序/缺失）、容量判定（单/多账号/CC）、hook 改写/放行/异常兜底、
  注册-Only（不改任务配置）、多账号轮换（A 耗尽用 B、全部耗尽报错、任一有余量即不算耗尽）、
  精简后的状态文本与 CC 摘要
- 版本号 0.4.0 → 0.5.0

**已知缺口**

- 让位只覆盖 replyer；utils / memory / fast_model 等后台任务没有对应 hook，额度耗尽时仍按失败处理
- 多账号的 `/deepether-status` 目前只展示第一个账号的节奏，尚未逐账号渲染
## 0.4.0（2026-10-27）
**主要功能：新增 CommandCode（Go 套餐）通道 —— 同一个插件里的第二个 Provider**

- 新增 `client_type = com.ether.commandcode`：复刻官方 CLI 的 `POST /alpha/generate`
  （JSON 信封 + NDJSON 事件流），Go 订阅的 Key 在官方 Provider API 上会 403
  `upgrade_required`，走这条 CLI 网关才可用（真 Key 实测，见 `docs/research/commandcode/P1-RESULTS.md`）
- 支持 **文本 / reasoning / 工具调用 / 视觉**：Context Items 与 wire messages 双向投影，
  工具调用以 `FunctionCallItem` 返回（`planner` 可用），图片按 `mimeType` 发送
- **只注册模型、不自动接线**：`register_models = all`（默认，56 条 Go 模型）/ `curated`（10 条）/ `none`；
  `model_include` / `model_exclude` 可筛选。具体哪个模型放进哪个任务，由用户在 WebUI 里决定
- 模型目录由 `scripts/gen_commandcode_catalog.py` 从官方 `models.md` 生成
  （`deepether/commandcode/models_go.json`，56 条、名字唯一、带单价/上下文/是否视觉）；
  `scripts/build_package.py` 打包前会校验目录与 md 一致，避免打出空目录
- **金额预算**：5 小时 / 7 天 / 30 天三层滚动窗口（默认 $3 / $6 / $9，另留 $0.5 保留额度），
  按模型单价核算成本，前置检查 + 落盘 `commandcode_budget.json`（重启不清零）
- 独立并发闸门 / 熔断器 / 节奏（`max_concurrency`、`min_interval_sec`、`max_per_minute`），
  与 DeepSeek 通道互不影响；错误分类新增 `model_not_allowed` / `plan_not_supported` /
  `upstream_blocked` / `context_too_long` / `request_too_large` / `budget_exhausted` / `billing_pending`
- 新增命令：`/commandcode-status`（Key、预算、熔断、最近错误）、`/commandcode-models`
  （56 条清单，标记是否已注册）、`/commandcode-setup`（重跑自动注册）
- 新增配置节 `[commandcode]`（Key、上游、CLI 版本、超时、并发、预算、视觉/工具策略等），
  `config_version` 0.3.0 → **0.4.0**（升级会以新默认值重建并回写 `config.toml`，升级前请先备份）
- 真机验证脚本 `scripts/probe_commandcode.py`：文本 + 工具 + 视觉 + Provider 全链路；
  Key 只从环境变量或指定文件读取，不打印、不落仓库

**细节**

- P1 真机发现并修复：`threadId` 传空串会被网关 zod 校验拒绝（400 `Invalid UUID at "threadId"`），
  `build_envelope` 现在为空时兜底生成 UUID；Provider 侧按 `thread_reuse_calls` 复用 threadId 吃 prompt cache
- `reasoning_effort` 只在模型目录声明支持时发送；模型参数被截断（`tool-error` / `max-tokens`）时
  直接判失败并提示提高 `max_tokens`，不把半截 JSON 当工具调用
- 历史裁剪（`max_history_chars`）、工具结果截断（`max_tool_output_chars`）、
  图片体积上限（`max_image_bytes`，按 base64 后大小）、孤立 UTF-16 surrogate 清理都已内置
- 测试 173 → **272 passed, 4 skipped**（新增协议/流解析/投影/预算/目录/自动注册/Provider/状态共 99 条）
- 版本号 0.3.2 → 0.4.0

**风险提示（务必阅读）**

- CommandCode 官方条款限制单账号、禁止自动化滥用：本插件只支持 **单 Key**，不做多账号池；
  请按 `docs/design/ANTIBAN.md` 的同类思路保守使用，账号风险由使用者承担
- Go 套餐是金额池（$10/月量级），强模型只够低频；`cc-` 模型默认不会被任何任务使用

## 0.3.2（2026-10-22）

**主要功能：容量模型重构（解决「一天 200 次不够用」）**

- 日上限 200 → **400**、时上限 30 → **45**；但抬高天花板不是主要手段，配套 4 个「提容量不加风险」的机制：
  1. **自适应日预算（AIMD）**：软风控 ×0.75 回撤、封禁级信号 ×0.5；连续成功 50 次 +0.1 回补，
     下限 `min_adaptive_budget`（默认 **200**，不低于旧版硬配额）。账号自己收敛到「不触发风控」的水平
  2. **夜间节律**：quiet_hours（默认 01:00-07:00）期间分/时配额 ×0.25。真人不会 24 小时匀速调用，
     「深夜也在满速跑」本身就是最强的机器特征之一
  3. **按模型配额**：model_daily_limits（例如 deepether-utils=180）给后台 utils 单独限流，
     避免高频的 utils 把日额度吃光、真正影响体验的 replyer 反而没额度
  4. **额度保留**：日预算最后 reserve_daily_budget（默认 60）次只留给 reserve_models（默认 replyer），
     额度紧张时先砍后台任务，回复始终保持网页版通道
- 会话复用 8 → **20** 次（一个会话更像真人一个话题的长度，顺带减少建会话往返）
- 凭证探活默认 30 → **60** 分钟（探活本身也是一次请求，属于纯噪声开销）

**细节**

- 新增 7 个配置项（adaptive_budget / min_adaptive_budget / quiet_hours / quiet_hours_ratio /
  model_daily_limits / reserve_daily_budget / reserve_models），全部带中文说明，WebUI 可直接改
- 节奏状态落盘升级到 version 2：额外记录每次调用对应的模型名、预算比例与成功连击；读取时兼容 v1 纯时间戳格式
- /deepether-status 的「反封禁节奏」新增：自适应预算（当前生效上限与比例）、夜间节律是否生效、
  近 24 小时按模型用量、按模型上限、保留额度剩余
- 插件配置 config_version 0.2.0 → **0.3.0**（[antiban] 结构扩展，升级时会以新默认值为骨架重建配置）
- **配额不足改为「有界等待」**：分/时配额一时打满时，replyer 最多等 `quota_wait_max_sec`（默认 25s）
  等窗口滑出，而不是立刻失败把突发整段推给付费模型；后台 utils 不等待（等下去只会拖慢回复）
- **先判后睡**：日额度 / 按模型额度 / 保留额度已经耗尽时立刻失败，不再先白等 8~20 秒
- **冷却不被误清**：只有「在冷却生效之后才被放行」的成功才算恢复；
  `max_concurrency=2` 时旧逻辑会被另一个在途请求的成功清掉刚设的冷却（表现为刚被限流又满速打）
- **软 403 / 业务限流可识别**：403 没有封禁文案时按限流退避（不再让用户去重新授权）；
  `HTTP 200 + code≠0` 的「请求过于频繁/稍后重试」归为可重试的上游错误（旧版是协议错误、完全不退避）；
  `DeepEtherError` 新增 `status` 字段，让分类器能按 HTTP 状态码判断
- **夜间节律按分钟判断**（旧实现只取整点，`23:30-06:30` 这类区间会判错）
- 新增 20 条单测：按模型配额、保留额度、未知模型不被误拦、夜间节律（含跨零点与分钟精度）、
  AIMD 回撤与回补、有界等待（replyer 等 / utils 不等）、先判后睡、冷却误清、403 分流、
  业务限流文案、配置→节奏控制器契约映射、状态落盘 v2 与 v1 兼容、状态渲染新字段
- 版本号 0.3.1 → 0.3.2

## 0.3.1（2026-10-22）
**主要功能**

- **自动注册不再创建 `deepether-planner`**：planner 依赖工具调用（function calling），网页版无法承担，
  默认只注册 `deepether-replyer` / `deepether-utils`；`planner` 继续使用官方模型

**细节**

- 升级后自动清理旧版遗留的 `deepether-planner` 模型定义，以及所有任务 `model_list` 里对它的引用
- `wire_tasks` 收紧：`emoji` 与 `planner` 一样列为不支持（分别需要视觉 / 工具调用），只允许纯文本任务
  （`replyer` / `utils` / `memory` / `mid_memory` / `fast_model` / `learner`）；写进不支持的任务会跳过并给中文告警
- 接线 `memory` / `mid_memory` / `learner` / `fast_model` 时，自动注册结果里会给出一条对应的风险提示
  （JSON 输出格式不稳会被静默丢弃 / `fast_model` 处在回复等待路径上）；可选任务复用 `deepether-utils` 模型，
  不会额外创建模型条目
- 新增 `docs/design/TASK-SUITABILITY.md`：各模型任务对纯文本反代的适配性（源码核查结论、推荐策略与开启方式）
- 版本号 0.3.0 → 0.3.1

## 0.3.0（2026-10-22）

**主要功能**

- **移除插件内 fallback：插件就是普通 Provider，失败交给 MaiBot 的模型调度**。网页版任何失败（凭证缺失/失效、
  熔断、节奏拒绝、上游 HTTP/超时、PoW、协议异常、空正文）都直接抛带中文说明的 `DeepEtherError`
  （Runner 转成 `success=false`），与其它模型失败的处理方式一致；插件不再回调 `ctx.llm`，也不再自己重试
- 删除 `deepether/fallback.py`、配置节 `[fallback]`（`FallbackConfig`）、错误码 `fallback_unavailable`，
  以及 manifest capabilities 里的 `llm.generate` / `llm.generate_with_tools`（只保留 `send.text`）；
  新增错误码 `channel_disabled`（`official_only` 下被调用时的明确说明，文案「通道配置为 official_only，本插件不接管网页版调用」）
- **自动注册写入 `max_retry = 1`**：让这个模型与其它模型一致，失败一次就交给 MaiBot 的模型调度，
  不在同一模型上重试 3 次（Host 默认 `max_retry = 3`）。已有的 `deepether` 服务商会被就地修正
- **任务接线按 `behavior.mode` 分支**：`web_first`（默认）把 `deepether-*` 插到 `model_list` 首位并固定 `sequential`；
  `official_only` 不接线，并把 `deepether-*` 从 `wire_tasks` 对应任务的 `model_list` 里移除（改回 `web_first` 会重新插回首位）；
  `web_only` **已废弃**，仍接受该字面值（旧配置能加载），行为等同 `web_first`，加载/热更新时打一条中文 warning
- 接线后若某任务 `model_list` 里只剩 `deepether` 模型，自动注册结果给出 warning：
  「任务 utils 的 model_list 只有 deepether-utils；该模型失败时这个任务没有其它可选模型，建议在 model_list 末尾补一个官方模型」

**细节**

- 失败日志改为每次失败一条中文 warning：`网页版调用失败（原因：{错误码}: {中文说明}）`
- 插件加载日志去掉「降级=开/关」等字段，只保留通道 / 思考 / 联网 / 并发上限 / 凭证
- `/deepether-status` 统计行改为「网页版成功：X 次｜失败：Y 次（最近 24 小时 Z 次）」，删除「降级计数」；
  反封禁冷却提示改为「期间网页版调用直接失败」；状态命令描述改为「查看 DeepEther Proxy 的凭证、书签与失败统计」
- 版本号 0.2.4 → 0.3.0（`_manifest.json`、`llm_providers[0].version`、`PROVIDER_VERSION`）；
  插件配置 `config_version` 0.1.0 → 0.2.0（`[fallback]` 配置节被移除属破坏性变化）
- 旧 `config.toml` 里的 `[fallback]` 节不需要手工删：Host 检测到配置版本升级时会以插件最新默认配置为骨架重建
  并落盘，旧的 fallback 字段会消失（预期行为），`credential.web_token` 等仍存在的字段原样保留

## 0.2.4

**自动注册（插件加载即写好模型配置，不需要手动加服务商/模型）**

- 新增 `[provision]` 配置节：`enabled`、`provider_name`、`model_config_path`、`wire_tasks`、
  `selection_strategy`、`keep_backup`，全部带中文说明，WebUI 可直接改
- 插件加载时自动把 Provider（`deepether`，base_url=`plugin://com.ether.deepether`，auth_type=none，
  timeout=300）与三个预设模型写进 Host 的 `config/model_config.toml`：
  `deepether-replyer`（style=replyer、不思考）、`deepether-planner`（style=plain、开思考）、
  `deepether-utils`（style=plain、不思考），通过模型级 `extra_params` 固定风格与思考开关
- 任务接线：把上述模型插到 `replyer` / `utils` 的 `model_list` 首位并把
  `selection_strategy` 固定为 `sequential`（只有按顺序优先才会先走网页版）；可用 `wire_tasks` 改或留空。
  默认不接 `planner`：它强依赖工具调用，网页版没有函数调用，接了只会每次先失败一次再降级
- 写入安全：定位实例根（显式路径 → `DEEPETHER_MODEL_CONFIG` → `MAIBOT_INSTANCE_DIR` → 插件目录向上查找），
  改前备份（最多 5 份）、`tomllib` 复核、`.tmp` + `os.replace` 原子替换；内容无变化时完全不落盘，
  避免反复触发 Host 热重载；解析失败/名字被占用等情况只告警不改坏配置
- Host 侧对 `model_config.toml` 有文件监视 + 热重载，写完立即生效，**不需要重启**
- 新增 `/deepether-setup` 命令手动重跑；`/deepether-status` 新增「自动注册」段落
- 新增 17 条单测（`tests/test_provision.py`）：注册/接线、幂等、备份与轮转、缺节自建、
  坏 TOML 不动文件、模型名冲突、路径探测优先级、任务名解析

## 0.2.3

**反封禁（上游账号被风控封禁 3 天后的补救措施）**，设计说明见 `docs/design/ANTIBAN.md`

- 新增 `[antiban]` 配置节（12 个旋钮）：默认串行 + 8~20 秒抖动间隔、3/分 · 30/时 · 200/天 三级配额、
  限流冷却 15 分钟、疑似封禁冷却 6 小时，配置项全部带中文说明，WebUI 可直接改
- 新增 `deepether/antiban.py`：`PacingController` 负责串行化、抖动间隔、三级配额与自适应退避
  （连续 3 次限流自动升级长冷却，成功一次折半恢复）；`classify_failure()` 把失败分成
  限流/封禁/配额三类，PoW 过期、凭证失效、5xx、并发拒绝都不会被误判成风控
- 节奏状态落盘到 `runtime_dir/antiban_state.json`（**重启不再等于配额清零**），冷却按墙上时钟的
  绝对结束时刻恢复，重启不会"加刑"
- 会话复用：同一个上游会话默认连续用 8 次再换（`antiban.session_reuse_calls`），
  每轮新建会话本身就是机器特征
- 请求头补齐真实浏览器的 `Accept-Language` / `sec-ch-ua` / `sec-fetch-*`；装了 `h2` 就自动走 HTTP/2。
  刻意**不**伪造 `x-client-*` 私有头（取值随前端版本漂移，写错比不带更可疑）
- 403 现在会先看响应文案：明确封禁 → `account_risk`（停止重试、长冷却）；否则按凭证失效处理
- 状态命令新增「反封禁节奏」段落：当前间隔区间与退避倍数、分/时/日用量、冷却原因与剩余时间
- 新增 15 条单测（`tests/test_antiban.py`）：间隔抖动、预算不足、三级配额、限流/封禁冷却、
  连续失败升级、状态落盘与容错、并发串行化、风控分类各分支

## 0.2.2

**发布形态定稿**：默认关闭本地授权服务（`credential.loopback_enabled = false`），
授权改为「浏览器控制台复制 userToken → 填进插件配置 `credential.web_token`（WebUI 可改）」，
状态命令在未启用本地服务时直接给出这 4 步。

**联网搜索实测**

- `search_enabled = true` 实测通过：上游会推 `TOOL_SEARCH` 片段（content 是 "Found 19 web pages" 进度文案）
- 修两个解析问题：① 工具片段污染正文；② 片段级 `status: FINISHED` 被误判成整体结束导致**提前掐断流**
- 新增正文清洗：剥掉网页版内部引用角标 `[citation:N]` / `[reference:N]`
- 默认仍为 `false`：搜索会多抓一轮网页、多 1~2 s，一期省 token 场景不需要

**范围与其他**

- 一期范围按文档 §4.5 收窄为 **replyer（主）+ utils**，planner 一期不接
- **空正文即失败**（含"只有思考没有正文"）：返回 success:false 让 Host 走 model_list 主备切换（§4.5.3 细节 1/2）
- 新增按模型覆盖提示词风格：`extra_params.deepether_style`；新增按模型覆盖思考开关：`extra_params.deepether_thinking`
- 降级白名单由"任务"改为"模型名"（`fallback_for_models`）：Host 请求里不携带任务名

## 0.2.1

**P2 验收演练（2026-10-21）发现并修复的问题**（完整报告：`docs/records/P2-ACCEPTANCE.md`）

- **并发求解 PoW 会踩 wasm**：迭代求解器是进程内共享 wasm 实例、非线程安全，2 路并发实测约一半请求 trap
  （out of bounds memory access / call stack exhausted）→ pow_failed → 降级；而 max_concurrency 默认就是 2，
  等于并发越高越用不了网页版。改为模块级锁串行求解（单次 ~100 ms，代价远小于误降级）
- 上游 `HTTP 200 + {code: 40003, msg: INVALID_TOKEN}` 现在归类为**凭证失效**：中文提示改为「重新打开 chat.deepseek.com 点一次书签」，
  同时落盘失效标记、状态命令显示「已失效」；修复前会被误判成 `protocol_error`，既不提示重新授权也不标记失效
- 凭证探活同样解析响应体的 `code`：修复前无效 token 会被判成「探活通过」
- 并发闸门的**排队拒绝不再计入熔断器**：修复前一次群聊突发（20 条同时到达）就能累计 3 次「失败」把熔断打开
- half_open 探针请求被 Host 取消时归还名额（原来会永久停在 half_open，之后永久降级）
- 「先启动、后用书签授权」的场景现在也会拉起定时探活
- 状态命令新增「最近 24 小时 N 次降级」与「闸门拒绝 N 次」；manifest 补 `llm.generate_with_tools` 能力声明
- 文档：`model_list` 主备切换**必须**配 `selection_strategy = "sequential"`；MaiBot 默认 `random` 会让约一半请求静默走官方 API
- 新增 8 条单测：响应封装分类 4、排队拒绝不触发熔断 1、探针名额归还 1、状态窗口 1、PoW 串行化 1

## 0.2.0

- **P1 已端到端跑通（2026-10-21 实测）**：MaiBot → 插件 → 网页版 → 回复正文，单次 1.6~2.6 s，9 次调用 0 次降级
- 上游协议实测修正三处（详见 `docs/upstream-protocol.md`）：
  - PoW 请求头**必须含 `target_path`**，否则上游返回 HTTP 200 + `{"code":40301}`
  - session id 在 `data.biz_data.chat_session.id`
  - SSE 是**混合帧**（裸 `{"v": text}` 增量 + 全量快照 + patch），按"当前片段类型" THINK/RESPONSE 分流
- 修复请求头名/值混用（`User-Agent` / `Origin` / `Referer` 曾被当成头名）
- P2：并发闸门、三态熔断、降级到 Host 模型（contextvars 递归守卫 + 剩余预算裁剪）、定时凭证探活、降级可见统计

## 0.1.0

- P0：插件骨架（manifest 双声明、Provider 注册、配置模型、生命周期）
- P0.5：书签小工具 + 回环凭证接收（仅 127.0.0.1，nonce + Origin 双校验）+ `/deepether-status`