# 更新日志

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

**反封禁（上游账号被风控封禁 3 天后的补救措施）**，设计说明见 `docs/ANTIBAN.md`

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

**P2 验收演练（2026-10-21）发现并修复的问题**（完整报告：`docs/P2-ACCEPTANCE.md`）

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
