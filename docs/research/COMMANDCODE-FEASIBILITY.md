# CommandCode Go 接入 MaiBot —— 可行性分析

> 分析对象：`DeepEtherProxy` 0.3.2（MaiBot 1.3.11 / maibot-plugin-sdk 2.12.0）
> 新需求：CommandCode 的 **Go 订阅**无法通过官方 Provider API 用 API Key 调用；在本插件内新增反代，
> 把 Go 套餐的模型提供给 MaiBot。
> 分析日期：2026-10-25　｜　协议基线：官方 CLI `command-code@1.88.0`、官方模型目录 92 条
> P0 状态：**已通过**（真 Key，文本/工具/图片/错误路径均实测）；详见 [P0-RESULTS.md](commandcode/P0-RESULTS.md)
> P1 状态：**已交付 0.4.0**（文本/工具/视觉/预算/56 模型注册全部真机跑通）；详见 [P1-RESULTS.md](commandcode/P1-RESULTS.md)
> 证据目录：[协议实证](commandcode/PROTOCOL-EVIDENCE.md)　｜　[模型目录快照](commandcode/models-1.88.0.md)

---

## 0. 结论摘要

**技术可行，工程量中等，运行风险高于现有 DeepSeek 网页版通道；建议分阶段做，不要一次性全量上线。**

| 判断 | 结论 |
|---|---|
| 协议是否可反代 | **已验证**。真 Key + Python `httpx` 直接 `POST /alpha/generate` 返回 200，`content-type: text/event-stream`；本次 10 余次请求没有被 Cloudflare challenge |
| 能否给出“Go 的所有模型” | **能，但要先定义“所有”**：官方目录 92 条模型中，Go 套餐可用 **56 条**（GOAT 11 / Pro 15 / Max 10）。Claude、Gemini、GPT-5.x 主力、Grok 4.6/4.7 等 **不在 Go** |
| 能否接 `planner`（工具调用） | **协议已验证**。工具定义带不带 `type:"function"` 都返回 200；`tool-call.input` 是完整对象；两轮工具回放 200。剩下的是“56 个模型逐个是否稳定” |
| 能否接视觉任务 | **已验证**。`mimeType` 与 `mediaType` 都被接受；`deepseek-v4-flash-vision-exp` 对 1×1 PNG 返回了 `Pink` |
| 能否替代 DeepSeek 网页版通道 | **不能一概而论，取决于选型**。Go 是 **$1/月 → $10 credits** 的金额池；按 6 万字符中文上下文（约 40k tokens）估算，V4 Flash 约 **50 次/天**、Qwen3.7-Flash 约 **260 次/天**、GLM-5.3 约 **6 次/天**。本插件当前默认日预算 400 次调用（可调，不是上游硬配额），Go 与它口径不同；强模型远不够用，最便宜模型可接近 |
| 最大风险 | **ToS 与封号**。官方条款限单账号、禁止 bots/scripts 自动化请求；但用户单账号长期使用（本周期 2,939 次、100% 成功、无封禁）说明“正常单账号低频”实际容忍度较高。多账号池仍是明确红线；技术可行 ≠ 允许 |
| 建议路线 | P0 已通过；直接进入 **P1 文本 MVP**，随后 P2 全量模型 + 预算，P3 工具/planner，P4 视觉。总计约 **9~13 个开发日** |

### 0.1 P0 验证结果（2026-10-25，真 Key）

用户提供了一把 Go Key，P0 已经跑完。完整记录见 [P0-RESULTS.md](commandcode/P0-RESULTS.md)，
原始事件流在 `research/commandcode/p0-events/`。

| # | 问题 | 结果 |
|---|---|---|
| 1 | 真 Key 能否在非 CLI 客户端跑通 `/alpha/generate` | **能**。HTTP 200，`content-type: text/event-stream` |
| 2 | Python 客户端能否过 Cloudflare | **本次能**。10 余次请求全部正常，无 HTML 403/1020 |
| 3 | 结束事件与用量字段 | 同一流同时出现 `finish-step`（`usage`）与 `finish`（`totalUsage`），数值一致 |
| 4 | 非空 `tools` 的形状 | 带不带 `type:"function"` **都返回 200**；`tool-call.input` 是完整对象 |
| 5 | `upgrade_required` 的真实形态 | **已复现**：Go Key 打 `/provider/v1/chat/completions` 返回 403 `permission_error` / `code:"upgrade_required"` |
| 6 | 图片块字段名 | `mimeType` 与 `mediaType` **都被接受**；视觉模型正常返回 `Pink` |
| 7 | `threadId` 是否必填 | **可省略**；不带也 200。带上同一 `threadId` 时第二次请求出现 `cacheReadTokens=256` |
| 8 | 非 Go 模型的错误 | 403 `FORBIDDEN`，message 前缀 `MODEL_NOT_IN_PLAN:`（不是结构化 code） |
| 9 | 计费/用量接口 | `/alpha/whoami`、`/alpha/billing/credits`、`/alpha/billing/subscriptions`、`/alpha/usage/summary` 全部 200 |
| 10 | 工具参数是否分片 | **会分片**：有 `tool-input-start/delta/end`，但最终 `tool-call` 仍给完整 `input` 对象 |

**仍未验证**（不阻塞 P1，但要在 P2/P3 前补）：

- 56 个模型逐一可用性与工具调用稳定性（本次只测了 DeepSeek V4 Flash 系列）； 
- 长上下文、`pause_turn` 续跑、64k 输出上限的实际行为；
- 并发上限、Key TTL、长期封号概率；
- `x-command-code-version` 过期后的拒绝形态；
- 其他上游供应商（本次 `providerMetadata` 出现 `novita`）行为是否一致。

### 0.2 参考实现可借鉴的地方

参考 [Mars-Sea/dsh-commandcode-provider](https://github.com/Mars-Sea/dsh-commandcode-provider)（MIT，
DSH 插件，长期使用 Go 订阅）后，以下做法可以直接迁移/借鉴：

| # | 借鉴点 | 对本插件的价值 |
|---|---|---|
| 1 | **工具 schema 根统一为 `type:"object"`**（空 schema、`$ref`、`allOf` 都要归一） | 网关会拒绝根不是 object 的工具；MaiBot 的 `parameters` 多数已是 object，但第三方/MCP 工具可能不是 |
| 2 | **历史 reasoning 回放到 assistant content**（`{type:"reasoning",text}`），不要丢 | 某些 DeepSeek thinking-mode 工具循环要求 reasoning 随 tool call 一起回放；本次 V4 Flash 未强制，但保留更安全 |
| 3 | **`MODEL_NOT_IN_PLAN:` 前缀分类** | 403 没有结构化 code，只能按 message 前缀识别；参考实现已踩过 |
| 4 | **`upgrade_required` 走 CLI 通道/账号轮换** | Go 打 Provider API 必失败；我们的路由固定走 `/alpha/generate`，但错误映射要识别它 |
| 5 | **模型目录从 `/provider/v1/models` + 官方 models.md 合并**，带 plan/免费/视觉/上下文标注 | 我们已有 models.md；可补 live context/endpoint 校验和 plan 标注 |
| 6 | **`threadId` + prompt cache** | 同 thread 第二次请求出现 `cacheReadTokens`；按会话复用 thread 可降成本 |
| 7 | **输出预算：gateway 总上限 131,072，按模型上限收紧** | 避免 `max_tokens` 过大被拒；也避免太小把工具调用截断（本次 48 tokens 就截断了） |
| 8 | **`x-taste-learning:false`、`permissionMode:"standard"`、`x-command-code-version`** | 与官方 CLI 形状对齐，降低被识别/被拒的概率 |
| 9 | **多账号轮换** | 参考实现支持，但我们**不采用**：ToS 明确限制单账号，多账号是永久封禁红线 |
| 10 | **`/commandcode` 用量面板** | 对应我们的 `/commandcode-status`；可展示 plan、5h/weekly、余额、平均成本 |
| 11 | **错误细分**：`JSON node count 32768`、输出上限、上下文超限、ZDR 无供应商 | 可作为后续错误分类表；MVP 先覆盖 401/403/429/400/5xx |
| 12 | **孤立 UTF-16 surrogate 清理** | Python `json.dumps` 可能输出 `\ud83d`，严格 JSON 解码器会拒；发送前清理孤立 surrogate |
| 13 | **图片缓存 offload** | 可选优化：旧图替换为占位文本，避免 prompt cache 回退；MVP 不做 |
| 14 | **Key 存储/浏览器登录** | 参考实现支持 studio 登录与 key 旋转；我们 MVP 只做配置/env/auth.json 导入，登录流程留到后续 |

> 参考实现是 MIT；如果直接移植代码，按 MIT 在 NOTICE/README 里保留版权与许可声明。
> 只借鉴协议与设计思路则不需要复制代码。

### 0.3 明确不建议做的事

- **不要做多账号池/轮询**：官方条款明确把多账号列为 material breach，可永久封禁。
- **不要伪装成官方 CLI 的全套指纹**（除网关要求的版本头外，不伪造设备指纹、TLS 指纹、遥测）。
- **不要把 56 个模型默认接进任务**：注册模型 ≠ 接线；默认只注册、不接线，避免误用高成本模型。
- **不要指望插件 Provider 做逐字流式**：Host 侧插件 Provider 不支持流式回调，插件只能读完再整体返回。

### 0.4 可行性评分

| 维度 | 判定 | 依据 |
|---|---|---|
| 技术可行性 | **高（已验证）** | 真 Key + Python 已跑通文本、工具、图片、错误路径；协议是“JSON 信封 + NDJSON”，无 PoW、无复杂加密 |
| 工程可行性 | **中高** | 需要第二个 Provider、结构化消息投影、金额预算；现有插件骨架可复用一半 |
| 运营可行性 | **中** | $10/月按金额计费；强模型只够低频，最便宜模型可到约 260 次/天；实测账户本周期 2,939 次、平均 $0.00189/次，但 weekly 已接近上限 |
| 合规可行性 | **中低** | Terms 禁止自动化请求、限单账号；单账号长期使用实际容忍度较高，但多账号/滥用仍是永久封禁红线 |
| **综合** | **有条件可行（P0 已过，可进入 P1）** | 文本 MVP 先行；planner/视觉 opt-in；单账号、默认关闭、明确免责 |

---

## 1. 需求与边界

### 1.1 原始需求

> CommandCode 的 Go 订阅无法通过官方的端点进行 API Key 调用，为本插件新增反代，
> 给 MaiBot 提供 CommandCode Go 的所有模型。

### 1.2 拆解成可执行目标

| # | 目标 | 验收口径 |
|---|---|---|
| G1 | 在 `deepether-proxy` 插件内新增第二个 LLM Provider | `client_type = com.ether.commandcode`，与现有 `com.ether.deepether` 并存 |
| G2 | 用 Go 套餐的 Studio API Key 调通推理 | 一次文本请求返回正文、reasoning、usage |
| G3 | 把 Go 可用的 56 个模型注册进 `model_config.toml` | 幂等、可筛选、不破坏现有 DeepSeek 配置 |
| G4 | 支持工具调用，使 `planner` 可用 | `planner` 通过 cc 模型发出 `reply` 工具调用并完成一轮 |
| G5 | 成本与额度可见、可控 | 5 小时/周/月三层金额窗口、按模型花费、超限前置失败 |
| G6 | 与现有通道隔离 | 两个 Provider 的凭证、并发、熔断、状态互不影响；可单独关闭 |

### 1.3 不在本期范围

- Embedding、语音转写、图片 embedding：CommandCode 没有对应能力。
- Host 侧逐字流式：插件 Provider 的硬限制（见 §2.4）。
- 多账号、多租户、对外提供 API 服务：与插件定位和 ToS 都冲突。
- 伪装/绕过 Cloudflare 或官方风控：只做协议兼容，不做对抗升级。

### 1.4 术语

| 术语 | 含义 |
|---|---|
| Provider API | `https://api.commandcode.ai/provider/v1/*`，OpenAI/Anthropic 兼容，**Go 不可用** |
| CLI 私有网关 | `POST https://api.commandcode.ai/alpha/generate`，官方 CLI 自己走的推理入口，**Go 可用** |
| Studio API Key | 在 `commandcode.ai/studio` 登录后生成的 key；CLI 把它存到 `~/.commandcode/auth.json` |
| Go | $1/月订阅，$1 换 $10 credits，按模型 API 单价扣费 |
| credits | 以美元计价的用量池；不是请求次数 |

---

## 2. 现状审阅：这个插件到底是什么

### 2.1 运行架构

```
MaiBot Host
  │  model_config.toml: api_providers[].client_type = com.ether.deepether
  │  invoke_llm_provider(operation="response", request)
  ▼
plugin.py  @LLMProvider(CLIENT_TYPE)          ← 唯一入口，异常必须自己兜住
  ▼
DeepEtherProvider.dispatch()
  ├─ adapter_in.build_messages(context_items) → 单条 prompt
  ├─ 并发闸门 / 熔断 / 反封禁节奏
  ├─ WebUpstreamClient.acquire_session()
  ├─ PoW 求解 → POST /api/v0/chat/completion（SSE）
  └─ adapter_out.build_response() → content / reasoning_content / usage
  ▼
Host 按任务 model_list 决定是否使用；失败则切下一个模型
```

关键点：插件**不是 HTTP 服务器**，而是 Host 进程内的 Provider 适配器；`base_url` 只是占位，
真正的上游地址与鉴权都在插件内。这一点决定了新增 CommandCode 通道**不需要开端口**。

### 2.2 现有能力矩阵

| 能力 | 现状 | 说明 |
|---|---|---|
| 文本生成 | ✅ | `replyer` / `utils` 默认接线 |
| reasoning 回传 | ✅ | `thinking_enabled`，进 `reasoning_content` |
| 工具调用 | ❌ | 网页版没有 function calling；`planner` 被显式列为不支持 |
| 视觉 | ❌ | 图片降级为占位文本 |
| embedding / 语音 | ❌ | 直接抛 `NOT_SUPPORTED` |
| 逐字流式 | ❌ | Host 侧插件 Provider 不支持流式回调 |
| 多上游 | ❌ | 单 Provider、单 token、单套节奏 |
| 自动注册 | ✅ | 写 `model_config.toml`，备份 + 热重载 |
| 反封禁 | ✅ | 串行、抖动、三级配额、AIMD、夜间节律、会话复用 |

### 2.3 可复用件（新通道不必重写）

| 模块 | 复用方式 |
|---|---|
| `gate.py` | 并发闸门 + 熔断器可直接复用；按 Provider 各建一个实例 |
| `errors.py` | 统一 `DeepEtherError` + 中文文案；新增 CommandCode 错误码即可 |
| `state.py` | 运行统计；按 Provider 分实例 |
| `status.py` | 状态渲染；新增一个 `render_commandcode_status()`，不动现有函数 |
| `provision.py` | 备份、原子写、幂等、校验的骨架可复用；把硬编码的 DeepSeek 常量泛化 |
| `antiban.py` | 三级配额/AIMD 的思想可借鉴，但 CommandCode 应按“金额窗口”而不是“次数窗口” |

### 2.4 Host 契约复核（决定设计的三条硬约束）

1. **请求是 `context_items`，不是 `message_list`**。
   `request_snapshot.serialize_response_request_snapshot()` 输出 `item_schema_version`、`request_kind`、
   `logical_turn_id`、`temperature`、`max_tokens`、`response_format`、`extra_params`、`model_info`、
   `tool_options`、`context_items`；`api_provider` 是 `plugin_client._invoke_provider()` 另外拼上的
   （`plugin_client.py:169-172`），不在序列化函数里。
   `context_items` 支持 `SystemMessageItem` / `UserMessageItem` / `AssistantMessageItem` /
   `ReasoningItem` / `FunctionCallItem` / `FunctionCallOutputItem`，图片是 base64 的 `image` part。
2. **响应必须能还原成 `output_items`（推荐）或旧标量协议**。
   `plugin_client.py:217-307` 接受 `output_items + item_schema_version=1`；`FunctionCallItem` 会被
   `APIResponse.tool_calls` 派生成 `ToolCall`，`planner` 正是读这个字段。
3. **插件 Provider 不支持 Host 侧流式**。
   `plugin_client.py:73-74` 明确拒绝请求里带 `stream_response_handler` / `async_response_parser`。
   `force_stream_mode` 只被内置 `openai` / `gemini` 客户端读取（例如 `openai_client.py:1627`），
   `PluginLLMClient` 不读它；因此 CommandCode 模型建议设 `force_stream_mode = false`，
   语义上不要暗示流式能力，也避免未来 Host 把两者绑定。

补充（双 Provider 的真实约束）：

- **机制允许，但不是文档化契约**。`runner_main.py:1435-1479` 会收集同一插件的全部
  `@LLMProvider` 方法，`manifest_validator.py:764-781` 只拒绝同一 manifest 内重复的 `client_type`；
  但 Runner 要求 **manifest 声明集合 == 代码注册集合**（`runner_main.py:1458-1466`）。任何一边漏写、
  拼错或多写，返回 `False`，**整个插件注册失败，DeepSeek 通道一起不可用**。
- **跨插件重复声明同一 `client_type`** 会让两个插件都被阻止加载（`plugin_runtime/integration.py:351-360`）。
  所以 `com.ether.commandcode` 必须带归属前缀，且不要与其它插件撞名。
- 因此 **Provider 必须永远注册**；`enabled=false` 只能在 `dispatch()` 内拒绝请求，不能靠“不注册”实现。

### 2.5 测试基线

当前插件测试：**173 passed, 4 skipped**（2026-10-25 本地实测）。
新增通道的回归门槛写成“**≥173 passed 且 0 failed**”，不要把数字写死——新增测试后总数会变；
新测试全部走 mock 网关，不依赖外网与真 Key。

---

## 3. CommandCode Go 的事实核查

> 本节结论来自官方文档、npm `command-code@1.88.0` 的 bundle 与实抓探针；
> 详细证据见 [PROTOCOL-EVIDENCE.md](commandcode/PROTOCOL-EVIDENCE.md)。

### 3.1 两个平面：官方 API 与 CLI 私有网关

| 平面 | 端点 | 鉴权 | Go 是否可用 |
|---|---|---|---|
| Provider API | `/provider/v1/chat/completions`、`/responses`、`/messages` | Bearer | **否**（官方：“Every plan except the Go plan has API access”） |
| 模型列表 | `/provider/v1/models` | **免鉴权** | 可读（实测 200） |
| CLI 私有网关 | `POST /alpha/generate` | Bearer（Studio API Key） | **是** |
| 计费/账户 | `/alpha/whoami`、`/alpha/billing/credits`、`/alpha/billing/subscriptions`、`/alpha/usage/summary` | Bearer | CLI 源码里存在；**未带真 Key 实测**，状态功能需 P0 验证 |

所以需求描述要精确一层：**不是“API Key 不能用”，而是“Go 的 Key 不能打官方 Provider API”**；
同一把 Key 打 `/alpha/generate` 是官方 CLI 的正常路径。插件要做的就是复刻这条路径。
参考实现还支持 `/provider/v1/chat/completions` 与 `/provider/v1/messages`（Claude 专用），
Go 用不到；未来用户升级套餐时可以作为低延迟路径，但 MVP 不做。

### 3.2 鉴权与凭证

- 获取：`command-code` CLI 执行 `cmd login` → 浏览器打开 Studio 授权页 → 回调
  `127.0.0.1:5959-5968` → 落盘 `~/.commandcode/auth.json`（0600）。
- 字段：`apiKey`（兼容 `api_key`）；没有 OAuth refresh token，key 只能静态使用；**TTL 官方未公布**，
  失效后只能重新登录，不能自动续期。
- 环境变量：`COMMAND_CODE_API_KEY`、`COMMANDCODE_CONFIG_DIR`。
- 请求头（官方 CLI `buildCommandAuthHeaders`）：

```http
POST /alpha/generate HTTP/1.1
Host: api.commandcode.ai
Authorization: Bearer <apiKey>
Content-Type: application/json
User-Agent: cli
x-command-code-version: 1.88.0
x-cli-environment: production
x-project-slug: <项目名>
x-taste-learning: false
x-session-id: <UUID>
```

没有设备指纹头；但有**版本校验**（社区实现注释称 “gateway rejects stale versions”）。
`x-command-code-version` 必须接近当前 CLI 版本，插件需要把它做成可配置常量，随 CLI 升级更新。

### 3.3 请求信封（实测 400 的字段清单）

```jsonc
{
  "config": {
    "workingDir": "/path/to/cwd",     // 必填 string
    "date": "2026-10-25",             // 必填 string
    "environment": "linux-x64",       // 必填 string（platform-arch）
    "structure": [],                  // 必填 array
    "isGitRepo": false,               // 必填 boolean
    "currentBranch": "",              // 必填 string
    "mainBranch": "",                 // 必填 string
    "gitStatus": "",                  // 必填 string
    "recentCommits": []               // 必填 array
  },
  "memory": "",                       // 必填字段；官方 CLI 实际会发 null，传空串更稳
  "taste": null,                      // 可选
  "skills": null,                     // 可选
  "permissionMode": "standard",       // CLI 归一值：standard|auto-accept|plan；服务端是否强制未证实
  "threadId": "<UUID>",               // CLI 只发 UUID；服务端是否接受非 UUID 未证实
  "mode": "<session mode>",           // 官方 CLI 带；社区桥可省略
  "promptCache": null,                // 官方 CLI 带；可省略
  "params": {
    "model": "deepseek/deepseek-v4-flash",
    "messages": [ /* 见 §3.4 */ ],
    "tools": [ /* 官方 CLI：{name, description, input_schema}；社区桥可能多一个 type */ ],
    "system": "系统提示词（字符串）",
    "max_tokens": 64000,              // CLI 默认 64k
    "stream": true,                   // 恒 true
    "temperature": 0.7,               // 可选
    "reasoning_effort": "high"        // 可选，模型支持才传
  }
}
```

几个容易踩的点：

- `memory` 与 `config.*` 是 **zod 强校验**，缺一个就 400；错误信息会逐字段列出来。
- `params.system` 是**顶层字符串**，不在 messages 里；多段 system 用 `\n\n` 拼。
- 工具 schema 字段叫 `input_schema`（Anthropic 命名），不是 `parameters`。**P0 已实测：带不带
  `type:"function"` 都返回 200**；MVP 按官方 CLI 形状（无 `type`）发送，`type` 作为兼容候选。
  工具 schema 的根必须是 `type:"object"`，否则会被网关拒绝（参考实现已踩过）。
- `threadId`：CLI 用 UUID 校验，非 UUID 会被丢弃。**P0 已实测：不带 `threadId` 也能 200**；
  但带同一个 `threadId` 的第二次请求出现 `cacheReadTokens=256`，说明它能吃到 prompt cache。
  **P1 实测：带 `threadId=""` 会被 zod 直接拒绝**（400 `Invalid UUID at "threadId"`），
  所以 MVP 生成稳定 UUID 并按会话复用（`protocol.build_envelope()` 也会兜底生成）。
- `permissionMode` 的 `standard|auto-accept|plan` 来自 CLI 源码归一函数；P0 用 `standard` 通过，
  没有穷举其它枚举。
- `mode` 的合法值未在公开文档列出；P0 未发送 `mode` 也 200，MVP 直接不传。

### 3.4 消息形状（不是 OpenAI messages）

```jsonc
// user
{"role": "user", "content": [
  {"type": "text", "text": "你好"},
  {"type": "image", "image": "data:image/png;base64,...", "mimeType": "image/png"}
]}

// assistant（可同时含 reasoning / tool-call）
{"role": "assistant", "content": [
  {"type": "reasoning", "text": "先查一下…"},
  {"type": "text", "text": "我来处理"},
  {"type": "tool-call", "toolCallId": "call_1", "toolName": "reply", "input": {"text": "hi"}}
]}

// tool 结果：独立 role，不在 user 里
{"role": "tool", "content": [
  {"type": "tool-result", "toolCallId": "call_1", "toolName": "reply",
   "output": {"type": "text", "value": "ok"}}
]}
```

`output.type` 用 `text` / `error-text` 区分成功与失败；Host 的 `success=false` 要映射成 `error-text`。
连续的工具结果要合并到同一条 `role:"tool"` 消息里。

> 图片块的字段名：官方 CLI 1.88.0 的 `toWireMessages()` 用 `mimeType`，AI SDK v5 常见的是
> `mediaType`。**P0 已实测两者都返回 200**；MVP 用 `mimeType`，`mediaType` 作为兼容候选。
> 视觉模型对 1×1 PNG 正常返回 `Pink`。

### 3.5 响应：NDJSON 事件流

响应体是**每行一个裸 JSON**，没有 `data:` 前缀。实测 `content-type: text/event-stream`，
但不要按 SSE 解析（没有 `data:` 前缀）。

实测完整事件集（`deepseek/deepseek-v4-flash`，含工具）：

```
start
start-step
reasoning-start / reasoning-delta* / reasoning-end
text-start / text-delta* / text-end
tool-input-start / tool-input-delta* / tool-input-end
tool-call
tool-error            # 参数被截断/非法时出现
finish-step           # usage
finish                # totalUsage
provider-metadata
```

核心事件：

| type | 字段 | 用途 |
|---|---|---|
| `text-start` / `text-delta` / `text-end` | `id`, `text` | 正文增量 |
| `reasoning-start` / `reasoning-delta` / `reasoning-end` | `id`, `text` | 思考增量 |
| `tool-input-start` / `tool-input-delta` / `tool-input-end` | `id`, `delta` | 工具参数流式片段 |
| `tool-call` | `toolCallId` / `toolName` / `input` | **最终完整对象**，以它为准 |
| `tool-error` | `error`, `toolInput`, `toolName` | 参数被截断/非法 |
| `finish-step` | `finishReason` / `rawFinishReason` / `usage` | 结束 + 用量（实测形态） |
| `finish` | `finishReason` / `rawFinishReason` / `totalUsage` | 结束 + 用量（实测形态） |
| `error` | 流内错误（字段名未定；CLI 读 `error`/`statusCode`/`isRetryable`） | 与 HTTP 错误信封是两套，要分别解析 |
| `provider-metadata` | `providerMetadata` | 上游供应商信息，可忽略 |

`pause_turn` **不是事件类型**，而是 `finish` 里的 `finishReason` 取值：服务端要求续跑，
CLI 会重发请求，有次数上限；实现时要把它当“未结束”处理。

usage 形状：

```json
{
  "inputTokens": 1234,
  "outputTokens": 567,
  "inputTokenDetails": {"noCacheTokens": 1000, "cacheReadTokens": 234},
  "outputTokenDetails": {"textTokens": 500, "reasoningTokens": 67}
}
```

`finishReason` 词表包含 `stop` / `end_turn` / `tool_calls` / `tool-calls` / `length` / `max_tokens` /
`max-output-tokens` / `pause_turn`。`pause_turn` 不是结束，要继续；否则会把半截结果当成功返回。
用量字段要兼容 `totalUsage` 与 `usage`（取 `v.totalUsage ?? v.usage`），不能只认一种，否则预算恒为 0。

解析实现要处理：跨 chunk 的半行、末尾无换行、空行、单行超长。社区实现给的单行/总响应上限是
8MB / 64MB，可以作为防线；超过就按协议错误处理，不要把内存吃穿。
不要用 `Content-Type` 判断流类型（可能是 `application/x-ndjson`、`text/event-stream` 或其它），
一律按“逐行 JSON”解析。

### 3.6 模型清单：Go 到底是哪些

官方 CLI 内置的 `models.md` 是“套餐归属”的权威来源（Min plan 列）。当前 92 条模型中：

| Min plan | 数量 | 含义 |
|---|---:|---|
| **Go and above** | **56** | Go 可用 |
| GOAT and above | 11 | Go 不可用 |
| Pro and above | 15 | Go 不可用 |
| Max | 10 | Go 不可用 |

Go 的 56 条按厂商分布：

| 厂商/前缀 | 数量 | 代表模型 |
|---|---:|---|
| Qwen | 10 | `Qwen/Qwen3.8-Max`、`Qwen3.8-Flash`、`Qwen3.8-Omni-Flash` |
| DeepSeek | 6 | `deepseek/deepseek-v4-pro`、`v4-flash`、`v4-flash-vision-exp`、`v4.1-flash` |
| zai-org / z-ai | 7 | `zai-org/GLM-5.3`、`GLM-5.2`、`z-ai/glm-5.3-flash` |
| Moonshot | 5 | `moonshotai/Kimi-K3`、`Kimi-K2.7-Code`、`Kimi-K2.6` |
| MiniMax | 4 | `MiniMaxAI/MiniMax-M3`、`minimax/minimax-m3.1-flash` |
| StepFun | 3 | `stepfun/Step-5-Preview`、`Step-3.7-Flash` |
| Meta | 3 | `meta/muse-spark-1.2-contributor`、`1.3-contributor`、`muse-spark-open` |
| 其他 | 18 | xiaomi、tencent、nvidia、thinkingmachines、ornith、mistral、LongCat、Inkling、免费档，以及闭源但 Go 可用的 `gpt-6-luna` / `gpt-5.6-luna` / `xai/grok-4.5` |

> 表中 id 为简称；`model_identifier` 必须使用 models.md 里的**精确 id**（例如
> `Qwen/Qwen3.8-Flash`、`stepfun/Step-3.7-Flash`），大小写和斜杠都不能改，否则上游会按未知模型拒绝。

**不在 Go 的代表模型**：全部 Claude、全部 Gemini、GPT-5.6 Sol/Terra、GPT-5.5、GPT-5.4、GPT-5.3 Codex、
Grok 4.6/4.7、Qwen3.8-Max-Prime、MiMo V2.6 Pro UltraSpeed、Sakana Fugu Ultra、Muse Wonder。
所以“所有模型”应表述为**“Go 套餐可用的 56 个模型”**，而不是“CommandCode 全部 92 个模型”。

> 套餐页的宣传语是“open models”，但 `models.md` 的 Min plan 列显示少数闭源模型（`gpt-6-luna`、
> `gpt-5.6-luna`、`grok-4.5`、Muse Spark contributor/open）也在 Go。**以 models.md 的 Min plan 为准**，
> 不要按“开源/闭源”推断权限。

完整清单见 [models-1.88.0.md](commandcode/models-1.88.0.md)。

### 3.7 额度、成本与容量

| 项 | 数值 | 说明 |
|---|---|---|
| 订阅价 | $1/月 | Go |
| 换算 | $1 → **$10 credits** | 10x |
| 滚动窗口 | **5 小时 $3 / 周 $6 / 月 $10** | 按金额，不按请求数 |
| 新订户 | 2026-09-28 起改为“按模型 credits” | 每模型约 $6–$10 等值；老订户仍是 flat $10 |
| 单次输出上限 | **64,000 tokens** | CLI 默认 |
| 并发上限 | **官方未公布** | 全站文档无 concurrent/parallel 字样 |
| 超额 | 吃 on-demand credits，否则等窗口重置 | 不是硬断流 |

**容量推算**（按一次调用 **40k 输入 + 500 输出**，约等于当前 6 万字符中文上下文；
中文约 1~1.6 字符/token，所以 6 万字符实际是 **37.5k~60k tokens**，表里取中间偏保守的 40k）：

| 模型 | 单价（$/1M in/out） | 单次成本 | $10 可调用次数 | 折合每天 |
|---|---|---:|---:|---:|
| `deepseek/deepseek-v4-flash` | 0.15 / 0.6 | $0.0063 | ≈ 1,587 | ≈ 53 |
| `deepseek/deepseek-v4-pro` | 0.66 / 1.98 | $0.0274 | ≈ 365 | ≈ 12 |
| `MiniMaxAI/MiniMax-M3` | 0.30 / 1.20 | $0.0126 | ≈ 794 | ≈ 26 |
| `zai-org/GLM-5.3` | 1.40 / 4.40 | $0.0582 | ≈ 172 | ≈ 6 |
| `moonshotai/Kimi-K3` | 3.00 / 15.00 | $0.1275 | ≈ 78 | ≈ 3 |
| `Qwen/Qwen3.8-Flash` | 0.16 / 0.47 | $0.0066 | ≈ 1,507 | ≈ 50 |
| `Qwen/Qwen3.7-Flash` | 0.03 / 0.13 | $0.0013 | ≈ 7,905 | ≈ 264 |
| `stepfun/Step-3.5-Flash` | 0.09 / 0.30 | $0.0038 | ≈ 2,667 | ≈ 89 |
| `xiaomi/mimo-v2.6-flash` | 0.14 / 0.28 | $0.0057 | ≈ 1,742 | ≈ 58 |
| `reflection/beam:free` / `poolside/laguna-s-2.1-free` | 0 / 0 | $0 | 受套餐/配额限制 | 不适用 |

- 如果上下文真的吃满 60k tokens，上表成本再 ×1.5：V4 Flash 约 36 次/天，GLM-5.3 约 4 次/天。
- 本插件当前默认日预算是 `max_per_day = 400`，但它是**插件自己设的节流上限**（可调到 20000，
  另受 3/min、45/h 约束），不是 DeepSeek 上游硬配额；拿它和 Go 的“金额池 ÷ 30”对比是异口径。
- 免费档（`beam:free`、`laguna-s-2.1-free`、`glyph-cluster:free`）单价 $0，但 `glyph-cluster:free`
  有 100 次/日限制，且模型质量/稳定性未知；可以当补充，不能当主力。

结论：**Go 能否替代取决于选型**。最便宜的 Qwen3.7-Flash 约 264 次/天，已经接近插件当前 400 次/天
的自设上限；V4 Flash 约 53 次/天；GLM/Kimi 只有 3~6 次/天。日常群聊走最便宜档，强模型只给
planner 或低频高价值场景；**ToS 与 $10 池是主约束，不是“无限量替代”**。

**真实账户观测（2026-10-25，单账号）**：本周期 2,939 次请求、completed 2,939 / failed 0、
总花费 ≈ $5.555、平均 ≈ $0.00189/次；5h 窗口用了 ≈ $0.508/3，weekly 用了 ≈ $5.549/6（已接近上限）。
这说明两件事：

1. 单账号持续使用（CLI 形状 + 正常频率）在实际观测中没有触发封禁/失败；
2. **weekly 窗口会先于 monthly 成为瓶颈**，预算设计不能只看 $10/月。

### 3.8 与现有 DeepSeek 网页版的协议差异

| 维度 | DeepSeek 网页版 | CommandCode Go |
|---|---|---|
| 鉴权 | 浏览器 localStorage 的 userToken | Studio API Key（`~/.commandcode/auth.json`） |
| 入口 | `chat.deepseek.com/api/v0/*` | `api.commandcode.ai/alpha/generate` |
| 反爬 | PoW（wasmtime + deepseek-pow） | 无 PoW；Cloudflare + CLI 版本头 |
| 请求体 | 单条 `prompt` + session id | 结构化 messages + system + tools + config 信封 |
| 流格式 | 混合帧 SSE（裸增量 + 快照 + patch） | NDJSON（每行一个事件） |
| 工具调用 | 不支持 | 支持（`input_schema` / `tool-call`，P0 已验证） |
| 视觉 | 不支持 | 支持（image 块，`mimeType`/`mediaType` 均可，P0 已验证） |
| 会话 | 先建 chat_session，再复用 | 无 session 创建；`threadId` 可选（P0 已验证） |
| 计费 | 订阅配额（次数/风控） | 金额 credits（5h/周/月窗口） |
| 主要风险 | 账号风控/封禁 | ToS + 封号 + 协议漂移 |

---

## 4. 能力映射：MaiBot 任务 × CommandCode Go

### 4.1 任务矩阵

| 任务 | 需要什么 | Go 能否满足 | 建议 |
|---|---|---|---|
| `planner` | 工具调用、稳定 JSON schema、多轮工具结果 | **可以**（需逐模型实测） | 独立开关；只接工具能力强的模型；保留官方兜底 |
| `replyer` | 纯文本、低延迟、口语化 | 可以 | 用低价模型；默认候选 |
| `utils` | 纯文本/偶尔 JSON | 可以 | 低价模型；默认候选 |
| `memory` / `mid_memory` / `learner` | 严格 JSON | **不稳** | 默认不接；如接，必须保留官方兜底 |
| `fast_model` | 低延迟（回复等待路径） | 不佳 | 不接；社区复现记录里首包约 2~3s，但未系统实测，不能量化承诺 |
| `emoji` | 视觉 + 拼图选择 | 有条件 | 默认不接；视觉阶段再评估 |
| `vlm` | 图片输入 | 可以（视觉模型） | 可选；需处理图片大小与模型白名单 |
| `voice` / `embedding` / `image_embedding` | 专用接口 | **没有** | 继续用官方/其它 Provider |
| `web_search` | 不是模型任务 | — | 不接 |

> 这些模型以编码/agent 场景为主，聊天口吻不一定像 DeepSeek 网页版。`replyer` 接线前要用真实群聊
> 提示词试几条，必要时给 cc 通道单独加口语化后缀；不要假设“模型更强 = 回复更像人”。

### 4.2 planner 为什么这次可以谈

现有文档把 `planner` 列为“永不接”，原因是 DeepSeek 网页版没有 function calling。CommandCode 不同：

- 请求侧：Host 的 `tool_options` 是 `{type:"function", function:{name, description, parameters}}`，
  直接映射成网关的 `{name, description, input_schema: parameters}`。
- 响应侧：网关 `tool-call` 事件给 `{toolCallId, toolName, input}`，映射成 Host 的
  `FunctionCallItem.tool_call = {call_id, func_name, args}`。
- Host 侧：`APIResponse.tool_calls` 从 `FunctionCallItem` 派生，`planner` 读的就是它；
  工具结果再以 `FunctionCallOutputItem` 回到下一轮。

**但“协议支持”不等于“模型稳定”**。需要在 P3 用真实 planner 场景逐模型验证：
是否只发一个 `reply`、参数是否为合法 JSON、是否会只输出文本不调用工具、长 system prompt 下是否退化。
建议候选：Kimi K3、GLM-5.3、DeepSeek V4 Pro、MiniMax M3、Qwen3.8-Max；用 `reasoning_effort=high`。
如果模型倾向于只回文本，优先在 system 里强化“必须调用工具”；网关是否支持 `tool_choice` 字段尚未证实，
不要把它当成默认手段。

P0 + 参考实现给出的四条硬要求：

1. **工具 schema 根必须是 `type:"object"`**。空 schema、`$ref`、`allOf` 都要归一；否则整请求 400。
2. **`max_tokens` 要给够**。P0 用 48 tokens 时工具参数被截成 `"{\"text"` 并出现 `tool-error`；
   256 tokens 时正常返回 `{"text":"OK"}`。planner 调用建议至少 512，且不要把 reasoning 预算压得太死。
3. **保留历史 reasoning**。把 assistant 的 `{type:"reasoning",text}` 连同 `tool-call` 一起回放；
   参考实现记录某些 DeepSeek thinking-mode 工具循环会拒绝缺失 reasoning 的 tool call。P0 的 V4 Flash
   未强制，但保留是更安全的兼容策略。
4. **以最终 `tool-call` 为准**。`tool-input-delta` 是增量文本，可能出现半截 JSON；累积后仍要用最终
   `tool-call.input` 覆盖，并校验参数是 dict。

### 4.3 视觉的边界

- Host 的 `ContextImagePart` 是 base64 + format；网关 image 块的字段名（`mimeType` vs `mediaType`）
  未实测，P0 要单独发图定死；不要假设“拼成 data URI 就一定能过”。
- 只对视觉模型放行；文本模型要么剥离图片（占位文本），要么直接报错。建议默认“剥离 + 日志提示”，
  避免一条图片消息把整轮请求打成 400。
- `vlm` 任务可以接；`emoji` 还涉及拼图与选择逻辑，先不接。

### 4.4 延迟与超时

- 插件 Provider 不支持 Host 流式；用户要等完整结果。
- Host 的 RPC 超时取 `api_provider.timeout`（默认 120s）；CommandCode 推理模型 + 长输出可能超。
  **`api_provider.timeout` 设 300s**，插件内读超时必须**小于**它（建议 270~285s，首包 60s），
  否则超时永远由 Host 先抛出，插件的中文错误映射不会执行。
- 聊天场景建议 `max_tokens` 限制在 4k~8k，避免一次生成 64k 把等待和成本放大。

---

## 5. 方案对比

### 5.1 五种做法

| 方案 | 做法 | 优点 | 缺点 | 结论 |
|---|---|---|---|---|
| **A（推荐）** | 同插件新增第二个 `client_type`：`com.ether.commandcode` | Host 按模型 `api_provider` 天然路由；两套凭证/并发/熔断/状态完全隔离；符合“在本插件内新增” | 插件体量变大；provision/status 要泛化 | **采用** |
| B | 复用 `com.ether.deepether`，按 `model_identifier` 前缀路由 | manifest 不变，改动少 | `timeout`/`max_retry` 共享；错误与节奏耦合；模型改名即路由失效；状态混在一起 | 不采用 |
| C | 另做一个插件 | 隔离最彻底；可独立发版 | 代码重复；用户要装两个插件；与需求“本插件”不符 | 备选 |
| D | 本地跑一个 OpenAI 兼容桥（如 cmdgo-bridge），MaiBot 用内置 `openai` 客户端 | 几乎不改插件；原生流式/工具调用；最快验证 | 多一个 Node 服务；无插件级自动注册/预算/状态；生命周期不受控 | **只用于 P0 对照**，不作为目标架构 |
| E（兜底） | 插件调用本机 `command-code` CLI 的 headless 模式 | 不用逆向协议；CLI 自己处理鉴权/版本/重试 | 需要 Node + CLI；每请求一个进程；工具/用量映射差；headless 输出未验证 | 仅当 Python 协议被 Cloudflare/版本校验挡住时评估，不作为已证实方案 |

### 5.2 为什么 A 不选 B

MaiBot 的模型选择是“模型 → `api_provider` → `client_type`”。两个 `client_type` 意味着：

- `api_provider.timeout` 可以分别设 300 / 180；
- `max_retry` 可以分别设 1；
- 一个通道熔断/冷却不影响另一个；
- 状态命令能明确区分“网页版凭证失效”与“CommandCode Key 失效”；
- 未来关掉任一通道只改一个开关。

B 把这些都塞进一个 Provider，短期省事，长期是排障地狱。

---

## 6. 推荐架构

### 6.1 目录结构（新增部分）

```
plugins/deepether-proxy/
├── _manifest.json                     # llm_providers 增加第二项
├── plugin.py                          # 第二个 @LLMProvider + 生命周期 + 命令
├── deepether/
│   ├── config.py                      # 新增 [commandcode] 配置节
│   ├── provider.py                    # 现有 DeepSeek Provider，不动
│   ├── provision.py                   # 泛化 provider/model/task 写入
│   ├── status.py                      # 新增 CommandCode 状态渲染
│   └── commandcode/                   # 新通道（独立子包，便于单测）
│       ├── __init__.py
│       ├── client.py                  # /alpha/generate + NDJSON 读取
│       ├── protocol.py                # 信封构造、事件类型、finish/usage
│       ├── adapter_in.py              # context_items + tool_options → wire
│       ├── adapter_out.py             # 累积结果 → output_items
│       ├── catalog.py                 # Go 模型目录（生成 + 运行时筛选）
│       ├── budget.py                  # 金额窗口、成本核算、落盘
│       ├── provider.py                # CommandCodeProvider(LLMProviderBase)
│       └── models_go.json             # 由脚本从官方 models.md 生成
└── tests/
    ├── fixtures/cc/*.ndjson           # 录制/构造的事件流
    └── test_cc_*.py
```

生成脚本放在 `scripts/gen_commandcode_catalog.py`：读官方 CLI 的 `reference/models.md`，输出
`models_go.json`；`scripts/build_package.py` 在打包前断言文件存在且正好 56 条，避免“忘了跑生成
脚本”打出空包。

### 6.2 数据流

```
MaiBot Host
  │  request: context_items + tool_options + model_info + api_provider
  ▼
plugin.py  @LLMProvider("com.ether.commandcode")
  ▼
CommandCodeProvider.get_response()
  ├─ 读取 [commandcode] 配置 + Key（配置 / env / auth.json）
  ├─ 预算前置检查（5h/周/月）
  ├─ adapter_in：system / messages / tools / images
  ├─ ConcurrencyGate + CircuitBreaker
  ├─ CommandCodeClient.generate()：POST /alpha/generate，逐行读 NDJSON
  │    ├─ text-delta / reasoning-delta → 累积
  │    ├─ tool-call → 收集
  │    ├─ finish / finish-step → usage / totalUsage / finishReason
  │    ├─ pause_turn → 有界续跑
  │    └─ error → 映射中文错误
  ├─ 成本核算 + 预算落盘
  └─ adapter_out：output_items（ReasoningItem / AssistantMessageItem / FunctionCallItem）
  ▼
Host 还原 APIResponse → planner / replyer / utils
```

### 6.3 请求投影规则（`context_items` → wire）

| Host item | Wire 处理 |
|---|---|
| `SystemMessageItem` | 取所有 text part，按 `\n\n` 拼成 `params.system`；不放进 messages |
| `UserMessageItem` | `{role:"user", content:[text…]}`；图片按模型视觉能力决定 image 块或占位文本 |
| `AssistantMessageItem` | `{role:"assistant", content:[{type:"text"}]}`；相邻的 tool-call 合并进同一条 assistant |
| `FunctionCallItem` | `{type:"tool-call", toolCallId, toolName, input}`；`args` 已是 dict，直接放 `input` |
| `FunctionCallOutputItem` | 独立 `{role:"tool", content:[{type:"tool-result", toolCallId, toolName, output:{type:"text" 或 "error-text", value}}]}`；连续结果合并；`success=false` → `error-text` |
| `ReasoningItem` | **保留**：附到前一条 assistant 的 `{type:"reasoning",text}` 块；DeepSeek thinking-mode 工具循环可能要求 reasoning 随 tool call 回放。只有历史预算超限时才裁剪 |
| `ProviderActivityItem` / `ProviderOpaqueItem` | 丢弃 |
| 未知类型 | 跳过 + 计数 + 日志，绝不抛异常 |

补充规则：

- **`ContextRefusalPart`**（message 里的 `{"type":"refusal","refusal":"..."}`）要渲染成
  `〔模型拒答：…〕` 文本或直接丢弃并计数，不能静默消失。
- **相邻同角色合并**：连续 `user` / `assistant` / `tool` 消息要合并（现有 `adapter_in._merge_adjacent`
  就在做），否则上游可能看到两条连续 user 消息；system 统一并到 `params.system`。
- **历史工具结果也要截断**：Host 每轮重放完整 `context_items`，`max_tool_output_chars` 必须作用在
  adapter_in 的全局投影上，而不是只处理本轮新结果。
- **图片体积**：Host 已做规范化（最长边 8000px、最多 16 段、JPEG q95/PNG，见
  `image_normalizer.py:21-98`）；base64 还会 +33% 体积，多段图片线性放大 token 与费用。
  `max_image_bytes` 要按“编码后大小”判断，并记录被降级的图片数量。
- **工具名缺失**：`FunctionCallOutputItem.tool_name` 为空时，用前面 `FunctionCallItem` 的
  `call_id` 映射回工具名；wire 的 `toolName` 不允许空串。
- **工具 schema 根归一**：每个工具的 `input_schema` 根必须是 `type:"object"`；空 schema、`$ref`、
  `allOf/anyOf/oneOf` 要按参考实现的规则归一，否则整请求 400。
- **孤立 UTF-16 surrogate 清理**：发送前对每个字符串清掉孤立 surrogate（保留成对 emoji），
  否则严格 JSON 解码器可能拒绝 `\ud83d` 这样的半字符。
- **工具结果里的图片**：CLI 传输对 tool-result 图片的支持未单独验证；MVP 可以把图片拆到随后的
  user 消息，或直接降级为文本占位，避免整轮 400。

工具定义：

```python
# Host:  {"type":"function","function":{"name":n,"description":d,"parameters":p}}
# Wire:  {"name":n,"description":d,"input_schema":p}          # 官方 CLI 形状，P0 通过
# 兼容:  {"type":"function","name":n,"description":d,"input_schema":p}  # P0 也通过
```

图片：

```python
# Host:  {"type":"image","image_format":"png","image_base64":"..."}
# Wire:  {"type":"image","image":"data:image/png;base64,...","mimeType":"image/png"}
```

预算：`max_history_chars` 复用现有配置；超长时从最旧的历史开始丢，system 永不丢。

### 6.4 响应投影规则

- 正文 → `AssistantMessageItem`（`parts:[{type:"text",text}]`）。
- reasoning → `ReasoningItem`（`representation:"raw_text"`，`text_parts:[...]`）。
- 工具调用 → 每个一个 `FunctionCallItem`：
  ```json
  {"item_type":"FunctionCallItem","tool_call":{"call_id":"...","func_name":"...","args":{...}}}
  ```
- `item_schema_version = 1`；`meta` 由 Host 注入，插件不造。
- usage → `{"inputTokens"→prompt_tokens, "outputTokens"→completion_tokens, "total_tokens": sum,
  "cacheReadTokens"→prompt_cache_hit_tokens, "noCacheTokens"→prompt_cache_miss_tokens}`；
  同时设置 `prompt_cache_reported=true`，否则 Host 侧缓存命中率与成本统计会失真。
- 空正文且无工具调用 → 视为失败（`UPSTREAM_EMPTY`），不能返回空回复。
- 工具调用 ID 缺失时用 `call_<uuid4hex>` 合成，保证 Host 侧非空。

几个实现注意：

- **工具参数可能是字符串**。网关通常给完整对象，但个别模型会给 JSON 字符串；要按
  `json.loads` → 失败则尝试修复/空对象，并在日志里记录，不能把字符串直接塞进 `args`（Host 要求 dict）。
- **`response_format` 没有原生映射**。Host 的 `memory` / `learner` 等任务要求严格 JSON 时，
  只能把 schema 追加到 system 提示词；解析失败仍会被业务层静默丢弃，所以这些任务默认不接。
- **HTTP/2 依赖**。现有 `httpx` 默认不带 `h2`；CommandCode 走 Cloudflare，建议把 `h2` 加入
  `_manifest.json` 的依赖（或至少作为可选依赖并在状态里提示），否则只能走 HTTP/1.1，指纹更弱。
- **不要把 `tool-call` 只认一种字段名**。兼容 `toolCallId`/`id`、`toolName`/`name`、`input`/`args`/`arguments`，
  这是社区实现已经踩过的兼容点。
- **工具参数会先分片**：按 `id` 累积 `tool-input-delta` 的 `delta`，`tool-input-end` 后仍以最终
  `tool-call.input` 覆盖；只有最终对象才写进 `FunctionCallItem.args`。
- **工具参数被截断要判失败**：`tool-error` / `invalid:true` / `finishReason=length` 同时出现时，
  不能把半截 JSON 当工具调用；返回可重试错误或提高 `max_tokens`。
- **结束事件可能两个都有**：实测同一流里先 `finish-step`（`usage`）后 `finish`（`totalUsage`）。
  取 `finish.totalUsage` 优先，缺失时回退 `finish-step.usage`；不要重复累加。

### 6.5 模型目录与自动注册

**目录来源**：

1. 官方 CLI 包内 `reference/models.md`（含 Min plan、Efforts、单价）→ 构建期生成 `models_go.json`；
2. 运行时可用 `GET /provider/v1/models`（免鉴权）做存在性/上下文长度校验，失败不影响主流程；
3. 用户可在配置里 `include/exclude/extra` 覆盖。

**注册策略**：

| `register_models` | 行为 |
|---|---|
| `all`（默认，满足“所有模型”） | 注册 56 条 Go 模型 |
| `curated` | 只注册 10 条推荐模型，适合不想让 WebUI 模型列表爆炸的用户 |
| `none` | 只注册 Provider，不注册模型（手工配置） |

`curated` 预设（覆盖聊天、工具、视觉、低成本四类；刻意不选 $2/$6 的 Qwen3.8-Max）：

```
deepseek/deepseek-v4-flash           # 日常聊天/后台，便宜
deepseek/deepseek-v4-pro             # 强一点的通用模型
deepseek/deepseek-v4-flash-vision-exp# 视觉
zai-org/GLM-5.3                      # 工具/agent
moonshotai/Kimi-K3                   # 长上下文/工具
MiniMaxAI/MiniMax-M3                 # 多模态/工具
Qwen/Qwen3.8-Flash                   # 快速/低价
Qwen/Qwen3.7-Flash                   # 最便宜付费档（$0.03/$0.13）
stepfun/Step-3.5-Flash               # 低价
xiaomi/mimo-v2.6-flash               # 低价多模态
```

免费档（`reflection/beam:free`、`poolside/laguna-s-2.1-free`、`stealth/glyph-cluster:free`）不放进
`curated`：单价 $0，但 `glyph-cluster:free` 有 100 次/日限制，质量和稳定性未知，适合作补充。

**Provider 条目**：

```toml
[[api_providers]]
name = "commandcode"
base_url = "plugin://com.ether.commandcode"   # 占位；插件不通过 HTTP 访问它
api_key = ""                                  # 插件配置里放 Key；这里留空
client_type = "com.ether.commandcode"
auth_type = "none"
timeout = 300                                 # 必须够长：无 Host 流式，等完整结果
max_retry = 1                                 # 失败一次就轮到下一个模型
```

**命名规则**：`cc-` + 模型 id 的 slug，例如：

| model_identifier | name |
|---|---|
| `deepseek/deepseek-v4-flash` | `cc-deepseek-v4-flash` |
| `zai-org/GLM-5.3` | `cc-glm-5.3` |
| `moonshotai/Kimi-K3` | `cc-kimi-k3` |
| `Qwen/Qwen3.8-Max` | `cc-qwen3.8-max` |
| `MiniMaxAI/MiniMax-M3` | `cc-minimax-m3` |

> slug 不能简单 `replace("/", "-")`：`deepseek/deepseek-v4-flash` 会变成
> `cc-deepseek-deepseek-v4-flash`。需要一张厂商/家族别名表（deepseek、qwen、glm、kimi、minimax、
> mimo、step、longcat…）先去掉重复前缀，再做冲突检测；56 条必须 56 个唯一 name。

**模型条目要点**：

```toml
[[models]]
model_type = "llm"
model_identifier = "deepseek/deepseek-v4-flash"
name = "cc-deepseek-v4-flash"
api_provider = "commandcode"
price_in = 0.0          # 不要填美元价：MaiBot 按人民币统计，填了会误报
price_out = 0.0
send_temperature = false
force_stream_mode = false   # PluginLLMClient 不读它；设 true 没有流式效果，只会误导
visual = false              # 视觉模型单独设 true
extra_params = { cc_effort = "high" }
```

**工具 shape 还没定死**：官方 CLI 1.88.0 的 `toWireTools()` 发 `{name, description, input_schema}`，
不带 `type`；部分社区桥带 `{type:"function", ...}`。探针里 `tools` 是空数组，非空形状从未验证。
P0 必须用真 Key + 真实工具定死；MVP 先按官方 CLI 形状，400 再兼容 `type`，不要同时发两种。

**任务 → 模型**：不要复用 DeepSeek 的固定 `TASK_MODEL_MAP`。CommandCode 的模型是“通用池”，
**0.4.0 的实现按需求方要求只注册模型、完全不接线**：`wire_tasks` / `task_models` /
`tool_capable_models` 这三个字段没有实现，用户自己在 WebUI 里把 `cc-*` 加进任务 `model_list`。
早期草案曾计划下面这套自动接线配置（保留作为未来选项的参考，不要照抄进当前版本）：

```toml
[commandcode]
wire_tasks = "planner,replyer,utils"
task_models = "planner=cc-kimi-k3,replyer=cc-deepseek-v4-flash,utils=cc-deepseek-v4-flash"
tool_capable_models = "cc-kimi-k3,cc-glm-5.3,cc-deepseek-v4-pro,cc-minimax-m3"
```

规则（未来若实现）：

- `task_models` 里的模型名必须是已注册的 `cc-*`；找不到就告警并跳过该任务。
- `planner` 额外校验目标模型在 `tool_capable_models` 白名单里；不在白名单就拒绝接线（避免“配了但永远发不出消息”）。
- 接线时仍把 `selection_strategy` 固定为 `sequential`，并在 `model_list` 末尾保留官方模型兜底。
- `wire_tasks` 默认 `""`：注册模型不等于接线，用户显式开启才生效。

**请求体量控制**：

- `max_tool_output_chars`（默认 20000）：超长工具结果截断并日志告警。MaiBot 自身有
  `read_tool_result` 机制，通常不会把超大结果整段塞回来；这里只是防线。
- `max_image_bytes`（默认 4 MiB）：超过就按 `vision_mode=strip` 降级，避免一张图把请求打爆。
- `max_tokens` 聊天默认 8192；不要沿用 CLI 的 64k 默认值。
- `max_history_chars` 默认 60000，与 §3.7 成本表一致；`planner` 需要更长历史时再调大，
  但成本与超时按比例上升，建议单独给 planner 留预算。

### 6.6 预算、节奏与失败处理

**预算**（按金额，而不是次数）：

- 三层滚动窗口：5 小时 / 7 天 / 30 天；默认上限 `3 / 6 / 9` 美元（月留 $1 余量）。
- 每次 `finish` / `finish-step` 按模型单价核算：`(input - cacheRead) × in + cacheRead × cachePrice + output × out`。
  部分 Qwen 模型还有 cache write 单价（models.md 的 `write $`），MVP 可以不计入，预算会略低估；
  要精确就把 write 单价也放进目录表。
- 状态落盘 `runtime_dir/commandcode_budget.json`，重启不重置。
- 前置检查：剩余额度低于“本次预估最小成本”时直接失败，不把请求发出去再撞 429。
- 预留：给 `planner` 留一小笔（例如 $0.5），避免后台任务把额度吃光。
- **本地硬上限 vs 上游软限制**：上游超额会自动吃 on-demand credits（若已充值），本地 3/6/9 是
  更保守的硬闸。产品上要明确“宁可本地先拒绝，也不让账单失控”；如果计费接口可用，定期对账，
  用上游余额校正本地估算，但**不要把上游当唯一防线**。

**并发与节奏**：

- `max_concurrency` 默认 1~2；官方未公布并发上限，保守起步。
- `max_per_minute` 默认 6；`min_interval_sec` 默认 0（金额已经是主约束）。
- 429 / `model_not_in_plan` / `premium_credits_exhausted` / `insufficient credits` → 冷却 + 中文提示，
  大小写不敏感，不盲目重试。
- 连续失败 → 熔断（复用 `CircuitBreaker`）。

**`pause_turn`**：CLI 内部有续跑次数上限（`nS`，具体值未公开）。MVP 固定最多续跑 2 次：
沿用同一 `threadId`、把已累积的正文/工具调用合并，不重发半截 assistant 消息；两次后仍 `pause_turn`
就按协议错误处理并日志，避免死循环。

**错误映射**：

| 上游信号 | 插件错误码 | 用户可见文案 |
|---|---|---|
| 401 / `UNAUTHORIZED` | `CREDENTIAL_INVALID` | CommandCode Key 失效，请在 Studio 重新生成并填入 |
| 403 + `MODEL_NOT_IN_PLAN`（大小写不敏感，P0 已复现） | `MODEL_NOT_ALLOWED` | 该模型不在当前套餐，请换模型或升级套餐 |
| 403 + `upgrade_required`（P0 已复现，Provider API） | `PLAN_NOT_SUPPORTED` | 当前套餐不支持该调用方式 |
| 429 | `UPSTREAM_HTTP`（retryable） | 触发限流，已进入冷却 |
| 400 + context/token limit | `CONTEXT_TOO_LONG` | 上下文超限，请缩短历史 |
| 400 + `JSON node count cannot exceed 32768` | `REQUEST_TOO_LARGE` | 请求结构过大（消息/工具/图片太多），不是单纯 token 超限；建议压缩历史或新会话 |
| `premium_credits_exhausted` / `insufficient credits`（大小写不敏感） | `BUDGET_EXHAUSTED` | 额度用尽，等待窗口重置或充值 |
| `RENEWAL_PENDING` | `BILLING_PENDING` | 续费处理中，credits 到账后恢复 |
| 5xx / Cloudflare 52x | `UPSTREAM_HTTP`（retryable） | 上游暂时不可用 |
| **HTML 403 / `1020` / `cf-mitigated`** | `UPSTREAM_BLOCKED` | 被上游边缘防护拦截，请检查网络/客户端环境，不要连续重试 |
| 超时 | `UPSTREAM_TIMEOUT` | 等待超时，可调大 timeout |

`UPSTREAM_BLOCKED` / `MODEL_NOT_ALLOWED` / `CONTEXT_TOO_LONG` / `REQUEST_TOO_LARGE` / `BUDGET_EXHAUSTED`
是新增错误码，要同步加进 `errors.py` 与状态/日志，避免只抛一个泛化的 `UPSTREAM_HTTP`。

### 6.7 凭证与状态

**用户怎么拿到 Key**（插件不内置 Node，也不替用户登录）：

```bash
npm i -g command-code      # 官方 CLI
cmd login                  # 浏览器授权，落盘 ~/.commandcode/auth.json
# 取 auth.json 里的 apiKey，填进插件配置 [commandcode] api_key
# Windows: %USERPROFILE%\.commandcode\auth.json
```

也可以直接在 `commandcode.ai/studio` 生成/复制 Key。插件侧来源优先级：

1. `[commandcode] api_key`
2. 环境变量 `COMMAND_CODE_API_KEY`
3. `request.api_provider.api_key`（用户在 MaiBot 服务商页填的）
4. 可选读取 `~/.commandcode/auth.json`（`auto_import_auth_file`，默认关，显式开启才读）

- 日志只出现 `mask_secret()` 后的形态；dump 文件必须脱敏 `Authorization`。
- Key 缺失不影响插件加载：CommandCode Provider 仍注册，但调用时返回 `CREDENTIAL_MISSING`，
  状态命令提示配置步骤；DeepSeek 通道不受影响。
- 配置热更新：`on_config_update(scope="self")` 时重建 cc client（Key / base_url / cli_version /
  timeout / 并发参数），清空 thread 缓存；预算状态保留，避免“改个配置就把额度清零”。
- 未来可选做“本地回调接收 Key”（类似现有书签回环），但 MVP 不做：它会把插件变成登录服务，
  与“只做 Provider”的定位冲突。
- 状态命令：
  - `/commandcode-status`：Key 状态、base_url、CLI 版本、已注册模型数/Go 模型总数、
    5h/周/月已花费与剩余、最近错误、闸门/熔断、目录版本。
  - `/commandcode-models`：列出 Go 模型（可按厂商过滤），标记已注册/未注册。
  - `/commandcode-setup`：重新注册 Provider 与模型。
  - `/commandcode-test [model]`：可选的单次低成本探针（会产生费用，默认不做）。
- **不要试图自动识别套餐**：官方 CLI 的内置知识文件明确说 plan 不能 headlessly 读取
  （归档在 `research/commandcode/plans-1.88.0.md`）；状态里写“按 Go 配置”，
  遇到 `MODEL_NOT_IN_PLAN` 再提示用户。

### 6.8 配置项草案

```toml
[commandcode]
enabled = true
api_key = ""
base_url = "https://api.commandcode.ai"
cli_version = "1.88.0"          # 随官方 CLI 升级更新；过期会被网关拒绝
project_slug = "maibot"
working_dir = ""                 # 空则用插件数据目录
environment = ""                 # 空则自动探测 win32-x64 / linux-x64 / darwin-arm64
taste_learning = false
auto_import_auth_file = false
request_timeout_sec = 285        # 必须小于 api_provider.timeout(300)，否则 Host 先取消
max_concurrency = 2
queue_wait_sec = 10
min_interval_sec = 0.0
max_per_minute = 6
max_tokens = 8192                # 聊天场景不要用 64k
max_history_chars = 60000        # 与 §3.7 成本表一致；planner 要更长时再调，成本按比例上升
max_tool_output_chars = 20000    # 超长工具结果截断防线
max_image_bytes = 4194304        # 4 MiB（按 base64 编码后大小）；超过按 vision_mode 降级
default_effort = ""              # off/low/medium/high/xhigh/max；空=不传
vision_mode = "strip"            # strip | auto | error
tool_mode = "auto"               # auto | off
thread_reuse_calls = 50
register_models = "all"          # all | curated | none
model_include = ""
model_exclude = ""
provider_name = "commandcode"
model_prefix = "cc-"
five_hour_budget_usd = 3.0
weekly_budget_usd = 6.0
monthly_budget_usd = 9.0
budget_reserve_usd = 0.5
persist_budget = true
health_check_interval_min = 0    # 0=关闭；>0 时尝试读计费接口（CLI 源码有 whoami→orgId，未实测）
```

> 旧配置文件没有 `[commandcode]` 时，沿用现有 `getattr(config, "antiban", ...)` 的降级写法，
> 不能让缺节导致插件加载失败。`config_version` 从 0.3.0 升到 0.4.0 会触发 Host 用新默认值重建
> 并**回写用户的 `config.toml`**（`maibot_sdk/config.py:145-164`、`runner_main.py:1071-1075`）：
> 同名旧字段会保留（`web_token` 不丢），但升级前仍要先备份。
>
> `health_check_interval_min` 依赖计费接口。**P0 实测**：`/alpha/whoami` 200（本账户 `org` 为 null）、
> `/alpha/billing/credits`、`/alpha/billing/subscriptions`、`/alpha/usage/summary` 不带 `orgId` 也能 200。
> 实现可以先 whoami，拿不到 orgId 就直接查 credits；接口不稳定时降级为“不显示余额”，不能影响主调用。
>
> `enabled = false` 时：不创建 CommandCode client、不启动探活、provision 跳过该通道；
> 已存在的 `cc-*` 模型保留，调用时返回“通道已关闭”的中文错误。要彻底清掉模型，
> 用 `register_models = "none"` + `/commandcode-setup`，或手工删除。**不要手工删 `api_providers`
> 里的 `commandcode` 条目**：模型仍指向它时，Host 只会给出含糊的回退失败，排障更难。

### 6.9 provision 的改动点

现有 `provision.py` 把 DeepSeek 的常量写死了：

- `DEFAULT_BASE_URL` / `DEFAULT_CLIENT_TYPE` / `DEFAULT_MODEL_IDENTIFIER`
- `DEFAULT_PLAN`（两条 DeepSeek 模型）
- `TASK_MODEL_MAP` / `UNSUPPORTED_WIRE_TASKS`（planner 永远不支持）

建议抽成 `ProviderPlan`：

```python
@dataclass(frozen=True)
class ProviderPlan:
    name: str
    base_url: str
    client_type: str
    timeout: int
    max_retry: int

@dataclass(frozen=True)
class ModelPlan:
    name: str
    model_identifier: str
    provider_name: str
    style: str
    thinking: bool
    visual: bool
    extra_params: dict[str, Any]
```

DeepSeek 的默认值保持不变（`DEFAULT_PLAN` 仍是两条），CommandCode 用生成的 56 条 plan。
`_remove_legacy_planner` 只清理 `deepether-planner`，**不要**误删 `cc-*` 的 planner 模型。
`_validate` 要同时校验两个 Provider，但 `commandcode.enabled = false` 时跳过。
CommandCode 需要独立的 `CC_TASKS_ALL`（含 `planner` / `vlm`，不含 `web_search`）与
`task_models` 解析；DeepSeek 的 `UNSUPPORTED_WIRE_TASKS` 只对 `deepether` 生效，不能复用。

---

## 7. 实施路线

| 阶段 | 内容 | 工期 | 退出条件 |
|---|---|---:|---|
| **P0 协议探针** | 真 Key + Python：文本、工具、图片、错误路径 | **已完成** | 见 [P0-RESULTS.md](commandcode/P0-RESULTS.md)；文本/工具/图片/错误全部拿到真实响应 |
| **P0b 模型覆盖** | 抽 3~5 个 Go 模型做文本+工具冒烟；长上下文、`pause_turn`、64k 输出 | 0.5~1 天 | 至少 3 个模型可用；工具调用无截断；`pause_turn` 行为明确 |
| **P1 文本 MVP** | `commandcode/client.py` + `protocol.py` + `adapter_in/out`（先不支持工具）+ 第二个 `client_type` + 基础状态 | 3~4 天 | `replyer`/`utils` 走 cc 模型能出正文；错误中文可见；≥173 passed 且 0 failed |
| **P2 全量模型 + 预算** | 目录生成脚本、`register_models=all`、provision 泛化、金额窗口、状态命令 | 2~3 天 | 56 条模型幂等写入；预算跨重启生效；`/commandcode-models` 可查 |
| **P3 工具调用 / planner** | 工具 schema 归一、reasoning 回放、`tool-input-delta` 累积、`FunctionCallItem` 回传、planner 实测 | 2~3 天 | 至少 2 个模型通过 `reply` 工具调用；planner 保留官方兜底 |
| **P4 视觉与打磨** | image 块、`vlm` 可选、文档、CHANGELOG、打包 | 1~2 天 | 视觉模型单测通过；README 能力表更新；zip 可安装 |

**P1~P4 的代码与真机冒烟已在 0.4.0 完成**（文本/工具/视觉/预算/56 模型注册；
见 [P1-RESULTS.md](commandcode/P1-RESULTS.md)）。**剩下的不是开发，而是用户在 WebUI 里选模型接线**：
本版本刻意不自动把 `cc-*` 接到任何任务（需求方 2026-10-27 明确「只加模型列表，具体放到哪个功能让用户自己弄」）。
`planner` 的逐模型稳定性仍建议按 §4.2 自行实测后再接。

**P0 已通过，剩余 9~12 个开发日**；按 20~30% 缓冲，约 **2 周**。
只做“P1 文本 MVP + curated 模型注册、不接 planner”约 **4~5 天**；完整 P1+P2 是 **5~7 天**。

### 7.1 P0 探针矩阵（大部分已实测）

P0-1 ~ P0-6、P0-8 已在 2026-10-25 用真 Key 验证（见 [P0-RESULTS.md](commandcode/P0-RESULTS.md)）；
P0-7（长上下文 / `pause_turn`）与 56 模型覆盖留到 P0b。

| # | 变体 | 要回答的问题 |
|---|---|---|
| P0-1 | 最小信封 + 真 Key + 必填头 | 真 Key 能否 200？首包是 `finish` 还是 `finish-step`？usage 字段叫什么？ |
| P0-2 | 去掉 `x-command-code-version` / 换成旧版本号 | 版本头是不是硬校验？ |
| P0-3 | 带 / 不带 `threadId`、`x-session-id` | 对缓存/会话行为的影响（body 校验已证明可省略，头是否必填仍待测） |
| P0-4 | 带 **非空 tools** + 一个“必须调用工具”的提示；分别试带/不带 `type:"function"` | 哪种工具形状能过？是否返回 `tool-call`？参数是对象还是字符串？ |
| P0-5 | 发一张图片，分别试 `mimeType` / `mediaType` | 图片块字段名到底是哪个？ |
| P0-6 | 错误 Key / 不在套餐的模型 / 余额不足 / Cloudflare HTML 403 | 错误码与文案能否稳定映射？ |
| P0-7 | 长上下文（接近 6 万字符）+ 64k 输出上限 | 首包时间、总耗时、是否触发 `pause_turn` |
| P0-8 | `httpx` 默认（HTTP/1.1）vs 装 `h2`（HTTP/2） | Cloudflare 是否区别对待？ |

探针输出要脱敏后留档，直接作为 P1 的 NDJSON fixture。

建议发布节奏：

- `0.4.0`：CommandCode 文本通道 + 全量模型注册 + 预算/状态（P0~P2）。
- `0.5.0`：工具调用/planner（P3，opt-in）。
- `0.6.0`：视觉/`vlm`（P4，opt-in）。

---

## 8. 风险登记册

| # | 风险 | 概率 | 影响 | 证据 | 缓解 |
|---|---|---|---|---|---|
| R1 | **ToS/封号**：自动化请求、单账号限制、检测信号 | 中（单账号正常频率）/ 高（多账号或滥用） | 高（账号/订阅永久损失） | 官方 Terms 原文；本账户 2,939 次/周期、0 failed、无封禁；多账号仍是明确红线 | 单账号、保守频率、不做池、明确免责声明、可一键 `enabled=false`；不默认开启 |
| R2 | **协议漂移**：`/alpha/generate` 无公开文档，CLI 月更 | 高 | 中（通道失效） | CLI 1.31→1.88 的迭代；版本头校验 | 隔离 client、钉版本、golden fixture、调试 dump、快速发版 |
| R3 | **Cloudflare/TLS 指纹**：Python 客户端被 challenge | 低（P0 未复现）/ 中（长期） | 高（若发生则 P0 失败） | P0 10 余次请求全部正常；网关响应带 `server: cloudflare` | 保持 HTTP/2 + 真实头；失败则评估 Node sidecar |
| R4 | **额度耗尽**：$10/月，强模型 3~6 次/天就用完 | 高 | 中（回复失败/降级） | 见 §3.7 容量表 | 金额预算、低价默认、按模型限额、保留额度、官方兜底 |
| R5 | **模型目录/套餐漂移**：Go 列表变化、按模型 credits | 中 | 中 | 官方 2026-09-28 计费调整；文档 56/57 计数差异 | 生成式目录 + 用户覆盖 + `MODEL_NOT_IN_PLAN` 友好提示 |
| R6 | **工具调用不稳**：模型不调用/参数非法/多调用 | 中 | 高（planner 发不出消息） | 编码模型差异；planner 硬依赖 | 逐模型实测、参数校验/修复、只接白名单、官方兜底 |
| R7 | **超时/长响应**：无 Host 流式，等待完整结果 | 中 | 中 | 插件 Provider 限制；64k 输出上限 | `max_tokens` 限制、外层 timeout 300 / 插件 285、不要接 `fast_model` |
| R8 | **配置膨胀/误接线**：56 条模型、planner 接错 | 中 | 低 | WebUI 模型列表；任务 `model_list` | `cc-` 前缀、默认不接线、`curated` 模式、告警 |
| R9 | **Key 生命周期**：TTL 未知、无 refresh | 中 | 中 | 官方未公布；CLI 只有重新登录 | 401 标记失效、状态提示、文档写清重登步骤 |
| R10 | **双通道混淆**：两个 Provider 的错误/状态 | 低 | 低 | 现有状态只覆盖 DeepSeek | 独立命令、日志带 `[cc]` 前缀、状态摘要 |
| R11 | **Host/SDK 升级**：Item schema/注册契约变化 | 低 | 中 | 现有 `item_schema_version` 机制 | 锁 min/max 版本、契约测试、发布前跑集成 |

**R1 仍是主要决策风险**。单账号实测 2,939 次/周期、0 failed、无封禁，说明正常频率的实际容忍度
高于最坏预期；但这不改变 Terms 的单账号/自动化请求条款，也不构成长期保证。多账号池不要做。

---

## 9. 验收标准与测试方案

### 9.1 验收门槛

| Gate | 标准 |
|---|---|
| G0 | **已通过（2026-10-25）**：真 Key + Python 客户端 200；`finish-step`/`finish` 都有；非空 tools 两种形状都 200；图片两种字段都 200 |
| G1 | `replyer` 走 `cc-deepseek-v4-flash` 能返回正文；usage 与费用写入状态 |
| G2 | 56 条 Go 模型写入 `model_config.toml`；重复执行无 diff；DeepSeek 配置不被改写 |
| G3 | `planner` 用至少 2 个 cc 模型完成一次 `reply` 工具调用；失败时能切回官方模型 |
| G4 | 预算：5h/周/月窗口跨重启生效；超限前置失败；429/额度错误文案正确 |
| G5 | 视觉：图片块字段已验证；`vlm` 任务用视觉模型收到图片并正确描述；文本模型按 `strip` 降级 |
| G6 | 回归：≥173 passed 且 0 failed；打包 zip 可在测试实例安装；README/CHANGELOG 更新 |

### 9.2 测试分层

| 层 | 内容 | 是否联网 |
|---|---|---|
| L1 单元 | 信封 golden JSON、NDJSON 解析（半行/多事件/未知事件/错误事件、`finish`/`finish-step`、`usage`/`totalUsage`）、消息与工具映射、output_items、usage/成本、预算窗口、目录筛选、错误映射 | 否 |
| L2 契约 | `httpx.MockTransport` 模拟 200/400/401/403/429/流内 error/`pause_turn`；覆盖非空 tools、图片块、Cloudflare HTML 403；用录制的 `.ndjson` fixture | 否 |
| L3 集成 | 在 `.testenv/verify` 实例安装插件，配 cc 模型，发消息；planner 用测试聊天流 | 本地 |
| L4 真机 | `scripts/probe_commandcode.py`（读 `COMMAND_CODE_API_KEY`），手动执行；输出脱敏后存 `docs/research/commandcode/` | 是（手动） |

新增测试建议：

```
tests/test_cc_protocol.py
tests/test_cc_adapter_in.py
tests/test_cc_adapter_out.py
tests/test_cc_client.py
tests/test_cc_budget.py
tests/test_cc_catalog.py
tests/test_cc_provision.py
tests/test_cc_status.py
tests/fixtures/cc/text.ndjson
tests/fixtures/cc/tool_call.ndjson
tests/fixtures/cc/error.ndjson
```

### 9.3 发布前检查

- `python -m pytest -q`：≥173 passed 且 0 failed。
- `python ../../scripts/build_package.py`：zip 内含 `models_go.json`；在脚本里断言文件存在且 56 条。
- 在测试实例里：安装 → 配 Key → `/commandcode-status` → 发一条消息 → 看日志无 Key 泄漏。
- 断网/错 Key/超时/429/Cloudflare HTML 403 五条失败路径各跑一次，确认中文文案可见、不会静默。
- 确认 `model_config.toml` 备份存在、无重复 Provider、`max_retry=1`、`force_stream_mode=false`、插件 timeout 285 < 外层 300。

---

## 10. 决策与待确认

### 10.1 建议决策

1. **P0 已通过，直接进入 P1**；先花 0.5~1 天做 **P0b**（抽 3~5 个模型 + 长上下文 + `pause_turn`），
   再写 P1 文本 MVP。P0 的结论是“协议成立”，P0b 要回答“模型覆盖面是否成立”。
2. 按 **P1 → P2 → P3** 推进；`0.4.0` 交付文本通道 + 全量模型注册，工具调用单独发 `0.5.0`。
   （2026-10-27 更新：0.4.0 一次性交付文本/工具/视觉/预算，工具调用已并入本版本。）
3. **默认不接线**：注册 56 个模型，但不自动塞进任何任务；由用户显式选择。
   （2026-10-27 需求方确认：**只添加模型列表**，具体放到哪个功能由用户自己在 WebUI 里决定。）
4. **单账号、单 Key**：不做池、不做多租户；把 ToS 风险写进 README。用户实测单账号长期使用
   （2,939 次/周期、0 failed、无封禁）是正面信号，但不构成长期保证。
5. 保留 DeepSeek 网页版通道不变；CommandCode 作为“低价补充 + planner 候选”。最便宜档在容量上可接近当前插件日预算，但 ToS 与 $10 金额池仍是主约束，不能默认当替代。

### 10.2 待确认（不阻塞 P0）

| # | 问题 | 影响 |
|---|---|---|
| Q1 | P0b/P3 继续用同一把 Key 测试吗？当前 weekly 已用 ≈5.55/6，继续高频会先撞周窗口 | 决定测试节奏与是否等窗口重置 |
| Q2 | 确认只做单账号、接受 ToS/封号风险？ | 决定是否默认关闭、README 免责怎么写 |
| Q3 | 模型注册要 `all`（56）还是 `curated`（10）？ | 影响 WebUI 体验与配置体积 |
| Q4 | 是否要把 `planner` 接到 cc？优先哪个模型？ | 决定 P3 的范围与测试成本 |
| Q5 | 预算上限按 $3/$6/$9 还是别的组合？ | 影响默认配置与容量 |
| Q6 | 是否需要视觉（`vlm`/`emoji`）？ | 决定 P4 是否做、图片处理策略 |
| Q7 | 是否接受“CLI 版本头随插件版本更新”的维护方式？ | 决定协议漂移的响应速度 |
| Q8 | 是否直接借鉴/移植 Mars-Sea 的 MIT 实现（含 NOTICE 声明），还是只借鉴设计？ | 影响开发速度与归属声明 |

### 10.3 什么情况下结论会反转

- P0b 发现多数 Go 模型不可用，或工具调用在主力模型上普遍失败 → “所有模型/planner”承诺缩水。
- Python 客户端在长期/多网络环境下被 Cloudflare challenge，P0 的单次通过不能代表长期 → 评估 Node sidecar。
- Go 的真实模型可用列表明显小于 56（按模型 credits 限制）→ “所有模型”承诺缩水。
- Key TTL 很短（小时级）→ 无法无人值守，价值大幅下降。
- 官方开始针对桥接做账号处罚 → 风险从“可能”变成“已知会”，不建议默认开启。

---

## 附录 A：证据索引

| 证据 | 位置 |
|---|---|
| 协议实证报告（端点、信封、事件、额度、风险） | [PROTOCOL-EVIDENCE.md](commandcode/PROTOCOL-EVIDENCE.md) |
| **真 Key P0 实测结果** | [P0-RESULTS.md](commandcode/P0-RESULTS.md)；原始事件流在 `research/commandcode/p0-events/` |
| 官方 CLI 1.88.0 模型目录快照（含 Min plan/单价/Efforts） | [models-1.88.0.md](commandcode/models-1.88.0.md) |
| 官方 CLI 套餐说明（plan 不可 headless 读取） | [plans-1.88.0.md](commandcode/plans-1.88.0.md) |
| 400 缺字段探针 | [probe-400-bad-request.json](commandcode/probe-400-bad-request.json) |
| 401 假 Key 探针 | [probe-401-unauthorized.json](commandcode/probe-401-unauthorized.json) |
| 完整信封探针 | [probe-valid-envelope.json](commandcode/probe-valid-envelope.json) |
| 官方文档 | <https://commandcode.ai/docs/plans/go>、<https://commandcode.ai/docs/provider>、<https://commandcode.ai/docs/resources/pricing-limits>、<https://commandcode.ai/docs/resources/usage-limits> |
| 官方条款 | <https://commandcode.ai/terms> |
| 官方 CLI | `npm i -g command-code`（`command-code@1.88.0`） |
| 社区先例 | [cmdgo-bridge](https://github.com/Patrick-mufeng/cmdgo-bridge)、[cpa-cmdcode-go](https://github.com/megumin31/cpa-cmdcode-go)、[hermes-commandcode-oauth](https://github.com/NanamiMio/hermes-commandcode-oauth) |
| **DSH 参考实现（MIT）** | [Mars-Sea/dsh-commandcode-provider](https://github.com/Mars-Sea/dsh-commandcode-provider)（协议、工具归一、reasoning 回放、目录/用量面板） |

## 附录 B：Host / 插件代码索引

| 结论 | 依据 |
|---|---|
| 插件 Provider 请求字段 | `src/llm_models/request_snapshot.py` `serialize_response_request_snapshot()`；`api_provider` 由 `plugin_client.py:169-172` 附加 |
| 插件 Provider 响应协议 | `src/llm_models/model_client/plugin_client.py:217-307` |
| 插件 Provider 不支持 Host 流式 | `plugin_client.py:73-74` |
| `FunctionCallItem` → `tool_calls` | `src/llm_models/model_client/base_client.py:154-168` |
| 多 `client_type` 机制 | `src/plugin_runtime/runner/runner_main.py:1435-1479`；`manifest_validator.py:764-781` |
| manifest/代码不一致 → 整插件注册失败 | `runner_main.py:1458-1466` |
| 跨插件 client_type 冲突 → 双方都被阻止 | `src/plugin_runtime/integration.py:351-360` |
| 配置版本升级会重建并回写 | `maibot_sdk/config.py:145-164`；`runner_main.py:1071-1075` |
| 图片规范化 | `src/llm_models/image_normalizer.py:21-98` |
| 现有 DeepSeek 链路 | `deepether/provider.py:378-507`、`upstream/web_client.py:521-619` |
| 现有 prompt 投影 | `deepether/adapter_in.py:209-258` |
| 现有响应投影 | `deepether/adapter_out.py:66-105` |
| 现有自动注册 | `deepether/provision.py:285-367`、`480-530` |
| 现有反封禁 | `deepether/antiban.py`、`docs/design/ANTIBAN.md` |

## 附录 C：变更记录

| 日期 | 内容 |
|---|---|
| 2026-10-25 | 初版：基于 `command-code@1.88.0`、官方文档与探针；结论为“技术可行、分阶段、风险高” |
