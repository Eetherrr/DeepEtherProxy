# P2 稳定性验收演练报告

> 日期：**2026-10-21**　版本：**0.2.1**　执行：Lead + harness-dev（演练 B 脚本）+ p2-auditor（只读审计）
> 环境：MaiBot **1.3.11** / maibot-plugin-sdk **2.12.0** / Python 3.12
> 实例：.testenv/maibot（工作区内的隔离测试实例，WebUI 41082，不碰日用实例）
> 通道：web_first，并发上限 2 / 排队 8s / 熔断 3 次 · 60s，兜底模型 deepseek-v4-flash

## 0. 结论

| 验收项（§9.6 / §7.1） | 判定 | 证据 |
|---|---|---|
| 网页版正常 | ✅ | 网页版调用成功 1.8~2.6 s，基线计数「网页版成功」递增 |
| **网页版正常时突然坏掉 → 麦麦照常回复** | ✅ | 改坏 token 后仍收到回复「这列表是真没更新啊」 |
| **中文 warning** | ✅ | 「触发降级（模型=deepether-replyer）：credential_invalid: 网页版凭证已失效…」 |
| **降级计数 +1（按次）** | ✅ | 状态命令「降级 0 → 3」（一条消息触发 3 次插件调用，各降级一次）；「最近 24 小时 6 次」 |
| 降级到 Host 模型可见 | ✅ | 「已降级到 Host 模型 deepseek-v4-flash…」+「降级调用成功：模型=deepseek-v4-flash，正文 15 字」 |
| 凭证失效标记 + 中文提示 | ✅ | 状态面板「状态：已失效，需要重新授权」/「失效原因：上游 chat_session/create 返回 40003 INVALID_TOKEN」 |
| 熔断期间仍降级不抛错 | ✅ | 「触发降级（模型=deepether-replyer）：熔断中（连续失败 3 次）」，麦麦仍回复 |
| 熔断恢复 | ✅ | 恢复凭证 + 冷却 65 s 后「网页版调用成功：耗时 2186 ms」，「熔断器：closed」 |
| **并发 20 超出闸门 → 直接降级** | ✅ | 20 并发：8 走网页版、**12 被闸门拒绝后立即降级**、0 异常 |
| **不撞超时** | ✅ | 端到端最大 **10.03 s**，为硬超时 240 s 的 4.2%；真实上游 12 并发最大 9.78 s |
| 单测回归 | ✅ | 108 passed, 4 skipped（新增 8 条） |

**演练暴露出 4 个真实缺陷，全部已修复并补了回归测试**（见 §4）。其中 **并发 PoW 求解踩 wasm** 是本次最有价值的发现：并发 2 路时约 50% 求解失败，等于并发越高越用不了网页版。

---

## 1. 演练 A：网页版正常时突然坏掉

### 1.1 为什么这样造故障

| 方式 | 真实度 | 问题 |
|---|---|---|
| 改 credential.json 文件 | 低 | 插件把凭证加载进内存，改文件对运行中的进程**无效** |
| 断网 / 改 base_url | 中 | 官方兜底**也要网**；base_url 是 constants.py 里的常量，没有配置项 |
| **走插件自己的回环授权通道写入无效 token** | **高** | 与「点一次书签换 token」完全同一条代码路径，进程不重启、其它一切正常，只有网页版凭证失效 |

所以演练用第三种：POST 到 127.0.0.1:18765/credential/claim（带 nonce 校验，与书签同源），把 token 换成无效值。

### 1.2 步骤与证据

| 步 | 操作 | 观测 |
|---|---|---|
| 1 | 启动实例，等「已启用终端输入」 | 11.2 s 就绪 |
| 2 | 发消息 | 「网页版调用成功：耗时 1844 ms」；熔断器 closed |
| 3 | 回环通道写入无效 token | 「网页版凭证已更新：p2-d…(53 字符)」 |
| 4 | 发消息 | 麦麦回复正常；「触发降级（模型=deepether-replyer）：credential_invalid: 网页版凭证已失效，请重新打开 chat.deepseek.com 点一次书签」；「已降级到 Host 模型 deepseek-v4-flash」；「降级调用成功：模型=deepseek-v4-flash，正文 15 字」 |
| 5 | /deepether-status | 「降级 0 → 3」、「最近 24 小时 6 次」、「状态：已失效，需要重新授权」、「失效原因：上游 chat_session/create 返回 40003 INVALID_TOKEN」 |
| 6 | 熔断已打开（连续失败 3 次），再发消息 | 「触发降级（模型=deepether-replyer）：熔断中（连续失败 3 次）」，麦麦仍回复（**没抛错、没空回复**） |
| 7 | 回环通道恢复原 token，等冷却 65 s，再发消息 | 「网页版调用成功：耗时 2186 ms」、「熔断器：closed」；最终计数：网页版成功 10 / 失败 0 / 降级 6 |

**演练 A 检查 17 项，全部通过**（.testenv/tools/p2_drill_a_result.json）。

### 1.3 一个必须说清的细节：一条消息 ≠ 一次降级

一条用户消息会触发 **replyer + 若干 utils**（记忆写回、摘要等）多次插件调用，每次失败都会各降级一次，所以状态里「降级」可能一次 +3~+4，不是「整条消息只 +1」。

判据因此定为 **「每次降级计数 +1，且与日志一一对应」**，而不是「整条消息 +1」。日志里同一条插件日志会出现两份（MaiBot 同时写两个 app 日志汇），读数要按 (timestamp, event) 去重。

---

## 2. 演练 B：并发 20 → 超出闸门的直接降级、不撞超时

### 2.1 方法

脚本：.testenv/tools/p2_concurrency_probe.py（harness-dev 交付）

- **复用真实实现**：DeepEtherProvider + ConcurrencyGate + CircuitBreaker + FallbackRunner，配置直接读生产 config.toml（26 字段生效）；
- 替身只有假上游与假 ctx.llm（脱离 MaiBot 拿不到 Host 模型），因此**降级耗时不能代表官方模型**；
- 两种模式：fake（假上游固定 2.5 s 延迟，20 并发）与 real（真实网页版，12 并发）。

> 为什么不是「发 20 条聊天消息」：MaiBot 有消息防抖（安静 2 s 开批），20 条消息会被合并成 1~2 轮对话，**根本制造不出 20 个并发 LLM 请求**。要验证闸门，必须直接压 Provider 入口。

### 2.2 fake 模式：20 并发

| 指标 | 结果 |
|---|---|
| 总请求 | 20 |
| 走通网页版 | 8（2 槽位 × 4 批：2.5 / 5.0 / 7.5 / 10.0 s） |
| **闸门拒绝后立即降级** | **12**（全部在 8077~8078 ms 被拒，降级本身仅 61 ms） |
| 熔断打开后降级 | 0 |
| 未被兜住的异常 | **0** |
| 最大端到端耗时 | **10.03 s**（240 s 硬超时的 4.2%） |
| 闸门观测并发峰值 | **2**（12 条被拒的一条都没挤进上游） |
| 断言 | 10/10 通过 |

### 2.3 real 模式：12 并发（真实上游）

| 指标 | 结果 |
|---|---|
| 走通网页版 | 6 |
| 闸门拒绝后立即降级 | 5 |
| 真实上游偶发失败 → 降级 | 1（upstream_http） |
| 异常 | 0 |
| 最大端到端耗时 | 9.78 s |
| 闸门拒绝计数 = 降级计数 | 5 = 5（一致） |

修复 PoW 并发问题**前**同参数跑过一轮：12 次请求里出现 2 次 pow_failed（见 §4.4），修复后为 0。

### 2.4 语义澄清：「直接降级」不等于「零等待」

闸门语义是**最多排队 queue_wait_sec（8 s）**，等不到才拒绝。所以：

- 被拒请求的耗时 ≈ 8 s（就是排队预算本身），**不是 0**；
- 真正零等待的只有「熔断已打开」分支；
- **最坏界 = queue_wait_sec + 一次上游耗时**。fake 下是 8+2.5 ≈ 10 s；真实上游若接近 request_timeout_sec（180 s），最坏约 188 s，仍 < 240 s，但余量只剩约 52 s。

要更激进地「不撞超时」，把 queue_wait_sec 调小（例如 3~5 s）即可，代价是更多请求提前降级到官方模型。默认 8 s 是「尽量用网页版」取向，属于可调参数而非缺陷。

---

## 3. 验证矩阵对照 §7.1「可以日用」

| 判定项 | 标准 | 实测 |
|---|---|---|
| 凭证 | 连续 7 天免干预；过期 30 min 内发现 | 本次验证「发现」链路（探活 + 失效标记 + 状态可见）；7 天需长期观察 |
| 可用性 | 网页版不可用时麦麦照常回复 | ✅ 演练 A |
| 可观测 | 一条命令看出凭证/最近失败/降级/耗时 | ✅ 状态命令新增「最近 24 小时 N 次」「闸门拒绝 N 次」，另有熔断器与进行中并发 |
| 不静默 | 任何降级都有中文日志且计数可见 | ✅ 每次降级都有「触发降级」+「已降级到 Host 模型」+ 计数 +1 |
| 不雪崩 | 突发 20 条不会全部超时 | ✅ 演练 B：12 条被拒的立即降级，最大 10 s |
| replyer 专项 | 空回复 0 次；平均 < 15 s | ✅ 演练 A 全程空回复 0 次；网页版单次 1.8~2.6 s |

---

## 4. 演练暴露的问题与修复（版本 0.2.1）

### 4.1 HTTP 200 下的鉴权失败被误判成「协议变更」

- **现象**：改坏 token 后日志报「protocol_error: 上游没有返回 chat_session_id，建会话接口可能已变更」，凭证仍是「已就绪」，用户不会去重新授权。
- **根因**：上游对无效 token 返回 **HTTP 200** + {code: 40003, msg: INVALID_TOKEN, data: null}。原实现只看 HTTP 状态码（401/403），业务错误码没人解析。这和 PoW 的 40301 是同一类坑，文档里早写过，只是没落到 JSON 接口的成功路径上。
- **修复**：web_client._raise_for_envelope() 统一校验响应体 code；40003 / INVALID_TOKEN / AUTHORIZATION FAILED → credential_invalid + 落盘失效标记 + 中文提示「重新打开 chat.deepseek.com 点一次书签」；其它非 0 码 → 带 code 与 msg 的 protocol_error（避免把协议漂移误报成需要授权）。
- **验证**：演练 A 第 4/5 步（状态面板出现 40003 INVALID_TOKEN）+ 4 条单测。

### 4.2 凭证探活只看 HTTP 状态码

- **现象**：无效 token 也是 HTTP 200，探活会把「凭证已失效」判成「探活通过」（§6.6.3 ① 直接失效）。
- **修复**：probe_user() 同样解析响应体并归类。
- **验证**：单测 test_authorization_failed_message_is_credential_invalid。

### 4.3 排队拒绝被计入熔断器 → 突发放大成 60 s 全降级

- **现象**：20 并发时被闸门拒绝的请求也执行 circuit.record_failure()；12 次拒绝远超阈值 3，只要没有后续成功把熔断重置，接下来 60 s 内所有请求都会以「熔断中」绕过网页版——一次群聊突发被放大成一段故障。
- **修复**：provider 里 concurrency_rejected 不再计入熔断失败（请求根本没到上游，属于本地容量信号，不是「上游连续失败」）。
- **验证**：单测 test_queue_rejection_does_not_open_circuit（阈值 1 时排队拒绝后熔断器仍 closed）。

### 4.4 并发求解 PoW 会踩 wasm（最严重）

- **现象**：演练 B real 模式下出现 pow_failed。单独复现：**2 路并发求解 12 次，失败 6 次**，错误是 wasm trap（out of bounds memory access / call stack exhausted）。
- **根因**：deepseek-pow 的求解器是**进程内共享 wasm 实例**，非线程安全；插件用 asyncio.to_thread 并发求解时互相踩线性内存。而 max_concurrency 默认 2 —— 正好是「两路并发」，**一半网页版调用会莫名降级**，命中率随并发升高而恶化。
- **修复**：pow.py 加模块级 threading.Lock 串行化求解（单次 ~100 ms，串行代价远小于误降级）。
- **验证**：复现脚本 6 轮 × 2 并发：修复前失败 6 次 → 修复后 **0 次**；演练 B real 模式重跑 pow_failed 从 2 次变 0 次；新增单测 test_concurrent_solve_is_serialized。

### 4.5 测试实例的路由策略会让「省 token」变成随机事件

- **现象**：第一次演练的基线校验失败——replyer 选了 deepseek-v4-pro-think（官方）而不是网页版。
- **根因**：MaiBot 的 selection_strategy 默认 random，model_list = [deepether-replyer, deepseek-v4-pro-think] 会被**随机挑一个**，约一半调用直接走官方 API，日志里没有任何降级痕迹。
- **修复**：测试实例 model_config.toml 的 replyer/utils 改为 selection_strategy = sequential；插件 README 与 docs/FEASIBILITY.md §4.5.2 的配置样例补上这一行并加警示。
- **影响面**：这是**用户照抄文档就会踩**的坑，属于「以为在省钱其实在烧钱」（风险 R12），修文档比修代码重要。

### 4.6 顺带补强的三项（审计发现）

| 项 | 改动 |
|---|---|
| half_open 探针被取消 | 请求被 Host 取消时探针名额不归还 → 熔断器永久停在 half_open、之后永久降级。新增 CircuitBreaker.abort_probe() + provider 在 finally 里兜底归还 |
| 书签授权后不启动探活 | 原实现只在「启动时已有凭证」才拉起定时探活，先启动后授权则永远不探活。改为授权成功后补启动 |
| manifest 缺能力声明 | fallback_with_tools 会调用 ctx.llm.generate_with_tools，但 manifest 只声明了 llm.generate；补 llm.generate_with_tools |

---

## 5. 代码与测试

| 文件 | 改动 |
|---|---|
| deepether/constants.py | 新增响应封装字段与鉴权错误码/标记常量 |
| deepether/upstream/web_client.py | _raise_for_envelope / _is_auth_error；_post_json 与 probe_user 均校验响应体 |
| deepether/upstream/pow.py | 求解串行化（wasm 非线程安全） |
| deepether/provider.py | 排队拒绝不计熔断；探针名额兜底归还；gate_rejected |
| deepether/gate.py | probe_in_flight / abort_probe |
| deepether/state.py | 降级时间窗（最近 24 小时） |
| deepether/status.py | 状态新增「最近 24 小时 N 次」「闸门拒绝 N 次」 |
| plugin.py | 书签授权后启动探活；状态传闸门拒绝数 |
| _manifest.json | 补 llm.generate_with_tools；版本 0.2.1 |
| 测试 | +8 条：响应封装分类 4、熔断不误触发 1、探针名额归还 1、状态窗口 1、PoW 串行 1 |

    $ .testenv\maibot\.venv\Scripts\python.exe -m pytest plugins\deepether-proxy\tests -q
    108 passed, 4 skipped

---

## 6. 已知限制（本次未改，需知情）

1. **兜底超时只能约束「插件愿意等多久」**：ctx.llm.generate(timeout_ms=...) 在 SDK 里是 RPC 超时，Host 的 _cap_llm_generate 不把它传给官方模型请求。所以 remaining_ms 裁剪不能真正掐断官方模型调用，只能保证插件不会无限等。要彻底解决得改 SDK/Host，不在插件边界内。
2. **递归守卫只在同进程内成立**：contextvars 深度计数跨 Host→Runner 的新任务不传递，真正的「兜底又指回本插件」递归只能靠「兜底只允许直选 model_name」这条约束兜住。
3. **启动自检只做了凭证探活**：§6.6.3 里「建会话 / PoW / 首增量」的自检没有开机自动跑（会消耗额度、可能触发风控），只在 /deepether-status 排查时体现。
4. **统计是进程内累计**：重载插件会清零；24 小时窗口靠内存时间戳，重启后归零。
5. **「网页版失败」计数不等于降级次数**：降级成功时不累加 web_failure（避免与「降级」重复计数），排查时以「降级」为准。
6. **真实上游偶发抖动**：演练期间观察到若干次 upstream_http（网络异常）与一次 upstream_empty。都是同一条降级路径兜住的，不影响可用性，但说明「降级不是只在断网时才发生」，日常会有零星降级。

---

## 7. 复现命令

    # 单测
    .testenv\maibot\.venv\Scripts\python.exe -m pytest plugins\deepether-proxy\tests -q

    # 演练 A：网页版正常时突然坏掉（约 4~6 分钟）
    .testenv\maibot\.venv\Scripts\python.exe .testenv\tools\p2_drill_a.py

    # 演练 B：并发闸门（fake 20 并发，约 12 秒）
    .testenv\maibot\.venv\Scripts\python.exe .testenv\tools\p2_concurrency_probe.py --mode fake --concurrency 20

    # 演练 B 真实上游版（12 并发，约 10 秒，会真实消耗网页版额度）
    .testenv\maibot\.venv\Scripts\python.exe .testenv\tools\p2_concurrency_probe.py --mode real --concurrency 12

    # PoW 并发求解回归（复现 wasm 冲突，修复后应为 0 失败）
    .testenv\maibot\.venv\Scripts\python.exe .testenv\tools\probe_pow_concurrency.py

## 8. 证据文件

| 文件 | 内容 |
|---|---|
| .testenv/tools/p2_drill_a_result.json | 演练 A 17 项检查的逐项结果 |
| .testenv/tools/p2_drill_a_transcript.txt | 演练 A 的完整终端记录（含麦麦全部回复） |
| .testenv/tools/p2_drill_a_pluginlog.txt | 演练 A 期间插件相关日志抽取 |
| .testenv/tools/p2_concurrency_probe_fake.txt | 演练 B fake 模式 20 并发完整输出 |
| .testenv/tools/p2_concurrency_probe_real.txt | 演练 B real 模式 12 并发完整输出 |
| .testenv/tools/p2_concurrency_probe.py | 演练 B 脚本（可重复执行） |
