# Command Code「Go 套餐」调用通道 —— 实证材料

调研时间：2026-10-25（UTC+8）。所有外网数据由 curl / npm tarball 实抓。

> 归档说明：原始 HTML 快照、CLI tarball 与临时目录已清理，只保留本报告、模型目录快照
> （`models-1.88.0.md`）与三条探针响应（`probe-*.json`）。报告中形如 `src_*.ts`、
> `cpa_*.go`、`txt_*` 的文件引用是调研期的临时文件名，内容结论已在本报告内完整记录。

---

## A. 结论摘要

1. **Go 套餐没有 Provider API**。官方文档明写 "Every plan except the Go plan has API access"；官方端点族是 `https://api.commandcode.ai/provider/v1/{chat/completions,responses,messages,models,systemone}`。Go 唯一可用的推理通道是 CLI 私有网关 `POST https://api.commandcode.ai/alpha/generate`。
2. **`/alpha/generate` 是真实存在的、走 NDJSON 流（非 SSE `data:` 帧）的私有 RPC**，官方 CLI `command-code`(npm, v1.88.0) 里常量 `WT="/alpha/generate"` 可证；我实测该路由存在且 Auth 在 body 校验**之后**（body 不全返回 400，body 齐全+假 key 返回 401）。
3. **鉴权 = 浏览器换取的静态 API key（无 refresh；TTL 官方未公布），`Authorization: Bearer <key>`**，不是 OAuth access token。获取路径：`https://commandcode.ai/studio/auth/cli?callback=http://localhost:PORT/callback&state=...` → 授权页把 `{apiKey,state,userId,userName,keyName}` POST 回本地回调；CLI 落盘 `~/.commandcode/auth.json`（0600）、配置 `~/.commandcode/config.json`。
4. **请求信封强制要求 `memory` 与 `config.{workingDir,date,environment,structure,isGitRepo,currentBranch,mainBranch,gitStatus,recentCommits}`**（我实测的 zod 校验报错逐字段列出）；`params` 内为 `{model,messages,tools,system,max_tokens,stream:true,temperature?,top_p?,reasoning_effort?}`。官方 CLI 还会带 `mode`、`threadId`、`promptCache`。
5. **额度是"钱"而不是"次数"**：Go 共享 $10/月池，另有 5 小时 $3、每周 $6 两档滚动窗口；文档按模型给出请求数估算（如 DeepSeek V4 Flash ≈ 4,620 次/5h、15,400 次/月）。并发上限**官方未公布**（未证实）。

---

## B. 端点与鉴权

### B1. 两个平面（官方文档 + 实测）

| 平面 | 端点 | 鉴权 | 谁能用 |
|---|---|---|---|
| Provider API | `https://api.commandcode.ai/provider/v1/chat/completions` | `Authorization: Bearer <CMD_API_KEY>` | GOAT / Pro / Max / Team / Provider；**Go 除外** |
| Provider API | `.../provider/v1/responses` | 同上 | 同上 |
| Provider API | `.../provider/v1/messages`（Anthropic 形状） | 同上 | 同上（Claude 模型只走这条） |
| Provider API | `.../provider/v1/systemone`（typesafe/jev 决策模型） | 同上 | 同上 |
| 模型列表 | `https://api.commandcode.ai/provider/v1/models` | **免鉴权**（实测 200，`access-control-allow-origin: *`） | 公开 |
| **Go 网关** | `POST https://api.commandcode.ai/alpha/generate` | `Authorization: Bearer <apiKey>` | 所有登录态（CLI 用） |

证据：`txt_p_docs_provider.txt:175-191`（"Every plan except the Go plan has API access"）、`:148-171`（端点表）、`txt_docs_plans_go`；CLI 二进制内 `In={prod:"https://api.commandcode.ai",staging:"https://staging-api.commandcode.ai",local:"http://localhost:9090"}`，`WT="/alpha/generate"`。可信度：**官方源码/文档 + 我实测路由存在**。

完整 URL 形态：

```
POST {BASE_URL}/alpha/generate
BASE_URL = https://api.commandcode.ai          # prod
         | https://staging-api.commandcode.ai   # --staging
         | http://localhost:9090                # --local
环境选择：--local / --staging 或 COMMANDCODE_API_ENV;  配置文件名随环境变为 config.local.json / config.staging.json / config.json
```

### B2. 请求头（官方 CLI `buildCommandAuthHeaders` 原样）

```js
{
  "Content-Type": "application/json",
  "User-Agent": "cli",                       // 实测二进制内 tk="cli"；独立实现 cpa-cmdcode-go 亦发 "cli"
  "x-command-code-version": <CLI 版本, 如 "1.88.0">,
  "x-cli-environment": "production",         // normalizeCliEnvironment() 归一
  "x-cli-surface": <可选, CLI 前端标识>,
  "x-project-slug": <项目名/目录 slug>,
  "x-taste-learning": "true"|"false",        // 字符串化布尔，"false" 即关闭 taste 学习
  "x-session-id": <会话/thread id>,
  "Authorization": "Bearer <apiKey>",
  // 可选：
  "x-oss-primary-provider": <...>,
  "x-cmd-zdr": "1",                          // 零数据保留开关
  "x-cmd-provider-deepseek-internal": "1",
  "x-oauth-token": "Bearer <oauthToken>",    // 仅 OAuth 供应商路径，/alpha/generate 不用
  "x-oauth-provider": <provider>
}
```

- **没有设备指纹头**：CLI 里没有 machine-id/hwid 类字段（已 grep 官方 bundle 无 `fingerprint` 请求头，只有内部 `recordCliFingerprint` 记录用途）。
- **有 CLI 版本校验**：`x-command-code-version` 与 UA 共同构成"客户端身份"。独立实现注释直言：*"The gateway rejects stale versions"*（`cpa_go_config.go:239-250`）。未证实具体阈值。
- 版本一致性：cmdgo-bridge 用 `user-agent: commandcode/1.31.0` + `x-command-code-version: 1.31.0`（自己钉的），cpa-cmdcode-go 用 `User-Agent: cli` + `x-command-code-version: 1.47.1`。**两种写法都能工作 → 说明 UA 不是硬校验项，版本号才是**（推断，源码级）。

### B3. 登录流程（CLI 实际握手，非标准 OAuth）

```
1. 本地监听 127.0.0.1:5959..5968 的 /callback（绑定 localhost 拼写）
2. 打开 https://commandcode.ai/studio/auth/cli?callback=http%3A%2F%2Flocalhost%3A5959%2Fcallback&state=<32B base64url>
3. 用户在 Studio 授权 → 页面 CORS POST /callback: {apiKey, state, userId, userName, keyName}
4. CLI 校验 state，落盘 apiKey；错误路径以 ?error=<reason> 回跳
5. 10 分钟超时；回调需 Access-Control-Allow-Private-Network: true
```

来源：`src_oauth.ts`（cmdgo-bridge 复刻）+ 官方 bundle 内同名参数 `get("apiKey"|"state"|"userId"|"userName"|"keyName")` 与 `renderCommandAuthDeniedPage`。**不是 OAuth2 code/token 交换，没有 refresh token**；所谓 "OAuth 登录" 实为浏览器换 key。可信度：源码级双份。

---

## C. 请求 / 响应契约

### C1. 请求信封（官方 CLI 原文拼接）

```js
POST /alpha/generate
{
  config: {          // 全部必填（zod），缺失即 400
    workingDir: "/path/to/cwd",
    date: "2026-10-25",
    environment: "linux-x64",     // `${platform()}-${arch()}`
    structure: [],                 // 目录结构摘要数组
    isGitRepo: false,
    currentBranch: "",
    mainBranch: "",
    gitStatus: "",
    recentCommits: []
  },
  memory: "",          // 必填 string（官方 CLI 传 null 亦可，服务端校验报 expected string 与 undefined 比较）
  taste: "",           // 实测未在必填清单中报错 → 可选
  skills: null,
  permissionMode: "standard",
  threadId: "<会话 id>",        // 官方 CLI 带；非 UUID 会被 toWireThreadId() 丢弃；cmdgo-bridge 省略
  mode: "<执行模式>",            // 官方 CLI 带；cmdgo-bridge 省略
  promptCache: <...>,           // 官方 CLI 带；cmdgo-bridge 省略
  params: {
    model: "deepseek/deepseek-v4-flash",
    messages: [ ... ],
    tools: [ { name:"...", description:"...", input_schema:{...} } ],
    // 注意：官方 CLI 1.88.0 的 toWireTools() 不发送 type 字段；
    // 部分社区桥发送 { type:"function", name, ... }。真 Key P0 实测两种形状都 200（见 C4）。
    system: "系统提示词（纯字符串）",
    max_tokens: 64000,           // CLI 默认 HT=64e3
    stream: true,                // 恒 true
    temperature: 0.7,            // 可选
    top_p: 0.9,                  // 可选（仅在 bridge 中出现，官方 CLI 未见 top_p）
    reasoning_effort: "high"     // 可选；reasoning 模型才传
  }
}
```

要点：
- **消息不是 OpenAI 形状**，是 AI-SDK/核心消息形状：`role: user|assistant|tool`；content 为块数组。
- 工具调用块：`{type:"tool-call", toolCallId, toolName, input}`；工具结果块：`{type:"tool-result", toolCallId, toolName, output:{type:"text"|"error-text", value}}`。
- assistant 里可带 `{type:"reasoning", text}` 块（历史思考回填）。`role:"tool"` 是**独立消息**，不是 assistant 的一部分（cmdgo-bridge 把纯 tool_result 的 user 消息改写成 tool 消息）。
- `system` 在 params 顶层，**不在 messages 里**；多处 system 用 `\n\n` 拼接。
- 工具 schema 用 `input_schema`（Anthropic 风格命名），不是 `parameters`。
- 空 `tools: []` 合法。

实测证据（假 key、不完整 body）：

```http
HTTP/1.1 400 Bad Request
{"success":false,"error":{"code":"BAD_REQUEST","status":400,
 "message":"Invalid request error. ... Validation error: Invalid input: expected string, received undefined at \"memory\"; ... at \"config.workingDir\"; ... at \"config.structure\" (expected array); ... at \"config.isGitRepo\" (expected boolean); ...",
 "docs":"https://commandcode.ai/docs/reference/errors/bad_request"}}
```

完整信封 + 假 key：

```http
HTTP/1.1 401 Unauthorized
{"success":false,"error":{"code":"UNAUTHORIZED","status":401,
 "message":"Invalid 'Authorization' header or token.",
 "docs":"https://commandcode.ai/docs/reference/errors/unauthorized"}}
```

→ 说明：路由真实、校验顺序 body→auth、错误信封统一为 `{success:false,error:{code,status,message,docs}}`（对比 Provider API 的 `{"error":{message,type,code}}`，两者信封不同）。可信度：**实测**。

### C2. 响应：NDJSON（每行一个 JSON 事件），不是 SSE

- 帧格式：裸 JSON + `\n`，**无 `data:` 前缀**，无 `event:` 行；空行/`:` 开头行可跳过。三份实现（cmdgo-bridge `parseEventStream`、cpa-cmdcode-go `bufio.Scanner` NDJSON、hermes）一致。
- 事件类型（官方 CLI 1.88.0 源码 + cmdgo-bridge 解析 + mock 网关镜像）：

| type | 字段 | 含义 |
|---|---|---|
| `text-start` | — | 文本块开始 |
| `text-delta` | `text: string` | 正文增量 |
| `reasoning-start` | — | 思考块开始 |
| `reasoning-delta` | `text: string` | 思考增量（thinking） |
| `tool-call` | `toolCallId`, `toolName`, `input`（也可 `id`/`args`/`arguments`） | 工具调用（一次性给出完整 input，非分片 JSON 字符串） |
| `finish` | `finishReason`/`rawFinishReason`, `totalUsage` | 结束 + 用量（**官方 CLI 1.88.0 源码形态**） |
| `finish-step` | `finishReason`/`rawFinishReason`, `usage` | 结束 + 用量（**cmdgo-bridge / mock 形态**） |

> 真 Key 实测（C4）：同一个流里先 `finish-step` 再 `finish`，两套字段都出现且数值一致；
> 解析器取 `finish.totalUsage` 优先，缺失回退 `finish-step.usage`。

- `usage` 形状（网关原样）：

```json
{"inputTokens":1234,"outputTokens":567,
 "inputTokenDetails":{"noCacheTokens":1000,"cacheReadTokens":234},
 "outputTokenDetails":{"textTokens":500,"reasoningTokens":67}}
```

- finishReason 词表：`stop` / `end_turn` / `tool_calls` / `tool-calls` / `length` / `max_tokens` / `max-output-tokens` / `pause_turn`（`pause_turn` 表示服务端要求续跑，CLI 会重发请求直到 `nS` 次上限）。
- 无 `model`/`id` 等 OpenAI 字段，需自行合成 `chatcmpl-*`。
- 工具调用**不分片**：一个 `tool-call` 事件给全量 `input` 对象 → OpenAI 侧需 `JSON.stringify` 成 `arguments` 字符串。

### C3. 官方 CLI 1.88.0 源码补充（用于消除歧义）

以下来自 npm `command-code@1.88.0` 的 `dist/cli.mjs`（压缩后变量名除外），是**源码级**证据；
未带真 Key 实测，服务端是否接受这些形状仍需 P0 验证。

- **结束事件**：官方 CLI 的 `consumeStream()` 判断 `type === "finish"`，读 `v.totalUsage`：
  ```js
  if ("finish" === v.type) {
    const e = v.totalUsage;
    e && (p = { inputTokens: e.inputTokens ?? 0, outputTokens: e.outputTokens ?? 0, ... });
    i = v.rawFinishReason ?? v.finishReason;
  }
  ```
  cmdgo-bridge 解析的是 `finish-step` + `usage`。**两种都要兼容**，以真 Key 实捕为准。
- **工具定义**：`toWireTools()` 返回 `{name, description, input_schema}`，**没有 `type`**：
  ```js
  function toWireTools(e){return e.map(e=>({name:e.name,description:e.description,input_schema:e.input_schema}))}
  ```
  社区桥的 `{type:"function", ...}` 是否被接受，未验证；探针里 `tools` 是空数组，非空形状未发过。
- **图片块**：`toWireMessages()` 里 image 块为：
  ```js
  { type:"image", image:`data:${media_type};base64,${data}`, mimeType: media_type }
  ```
  AI SDK v5 常见字段是 `mediaType`；真 Key P0 实测两者都 200（C4），MVP 用 `mimeType`。
- **permissionMode**：CLI 的 `toWirePermissionMode()` 归一为 `standard` / `auto-accept` / `plan`
  （`bypass` 映射为 `auto-accept`）。
- **threadId**：CLI 的 `toWireThreadId()` 用 UUID 校验，非 UUID 会变成 `undefined`；服务端是否接受
  非 UUID 未验证。
- **计费/账户接口**（CLI 源码常量）：
  ```
  /alpha/whoami
  /alpha/billing/credits?orgId=...
  /alpha/billing/subscriptions?orgId=...
  /alpha/usage/summary?orgId=...&since=...
  ```
  调用顺序是先 `/alpha/whoami` 取 `org.id`，再查 credits/subscriptions；**真 Key P0 实测**：本账户
  `org=null` 时 credits/subscriptions 不带 orgId 也能 200（C4）。
- **plan 不可 headless 读取**：CLI 内置 `reference/plans.md` 原文：
  > The user's plan is not readable headlessly: ask the user which plan they are on...
  已归档为 `plans-1.88.0.md`。
- **headless 模式**：CLI bundle 里有 `headlessInteraction` 代码路径，但 headless 输出能否稳定映射
  工具调用与用量**未验证**；方案 E 只能作为兜底设想。

### C4. 真 Key 实测（2026-10-25，覆盖 C2/C3 的未定项）

用户提供 Go Key 后，Python `httpx` 直接请求 `POST /alpha/generate`，结果：

| 项 | 实测 |
|---|---|
| HTTP / Content-Type | 200，`text/event-stream`；没有被 Cloudflare challenge |
| 事件 | `start`、`start-step`、`reasoning-start/delta/end`、`text-start/delta/end`、`tool-input-start/delta/end`、`tool-call`、`tool-error`、`finish-step`、`finish`、`provider-metadata` |
| 结束事件 | **同时出现** `finish-step`（`usage`）和 `finish`（`totalUsage`）；两者数值一致 |
| 工具形状 | 带/不带 `type:"function"` **都 200**；最终 `tool-call.input` 是完整对象 |
| 工具参数 | 确实会分片：`tool-input-delta.delta` 累积；`max_tokens=48` 会截断并触发 `tool-error` |
| 图片 | `mimeType` 与 `mediaType` **都 200**；视觉模型对 1×1 PNG 返回 `Pink` |
| threadId | 省略也 200；同 thread 第二次请求出现 `cacheReadTokens=256` |
| 非 Go 模型 | 403 `{"success":false,"error":{"code":"FORBIDDEN","status":403,"message":"MODEL_NOT_IN_PLAN: ..."}}` |
| Go 打 Provider API | 403 `{"error":{"type":"permission_error","code":"upgrade_required",...}}` |
| 计费接口 | whoami/credits/subscriptions/usage 均 200；本账户 `org=null` 时 credits 不带 orgId 也可用 |

完整记录与原始事件流：[P0-RESULTS.md](P0-RESULTS.md)、`p0-events/`。

---

## D. 模型清单与限制

### D1. 模型列表来源

1. `GET https://api.commandcode.ai/provider/v1/models` —— **免鉴权**，实测 200，93 条，字段 `{id,object,created,owned_by,name,context_length,supported_endpoints[]}`。`supported_endpoints` ∈ `/chat/completions` | `/responses` | `/messages`。
2. 官方 CLI 内置 `dist/bundled/command-code-knowledge/reference/models.md`（我下载了 npm 包原文），含 **Min plan** 列（Go < GOAT < Pro < Max）+ `Efforts` 列 + 每 1M token 价。这是判定"Go 能否用某模型"的权威依据。

### D2. Go 套餐可用模型（Min plan = "Go and above"，官方 models.md 共 56 条；docs 页面计数 57）

开源/低价（全部 Go 可用，摘 id）：
`deepseek/deepseek-v4-pro`、`deepseek/deepseek-v4-flash`、`deepseek/deepseek-v4-flash-vision-exp`、`deepseek/deepseek-v4-flash-fast`、`deepseek/deepseek-v4.1-flash`、`deepseek/deepseek-v4.1-flash-fast`、`moonshotai/Kimi-K3`、`moonshotai/Kimi-K2.7-Code`、`Kimi-K2.7-Code-Highspeed`、`Kimi-K2.6`、`Kimi-K2.5`、`z-ai/glm-5.3-flash`、`z-ai/glm-5.3-flashx`、`zai-org/GLM-5.3`、`GLM-5.2`、`GLM-5.2-Fast`、`GLM-5.1`、`GLM-5`、`minimax/minimax-m3.1-flash`、`MiniMaxAI/MiniMax-M3`、`MiniMax-M2.7`、`MiniMax-M2.5`、`ornith/ornith-1.5-122b`、`xiaomi/mimo-v2.6-pro`、`mimo-v2.6-flash`、`Qwen/Qwen3.8-Omni-Flash`、`Qwen3.8-Max-0902`、`Qwen3.8-Max`、`Qwen3.8-27B`、`Qwen3.8-Flash`、`Qwen3.7-Max`、`Qwen3.7-Plus`、`Qwen3.7-Flash`、`Qwen3.6-Max-Preview`、`Qwen3.6-Plus`、`meituan/LongCat-2.0`、`stepfun/Step-5-Preview`、`Step-3.7-Flash`、`Step-3.5-Flash`、`tencent/hy3-paid`、`hy4-preview`、`nvidia/nemotron-3-ultra-550b-a55b`、`nemotron-3.5-super-vl`、`thinkingmachines/inkling`、`inkling-small`、`reflection/beam:free`、`poolside/laguna-s-2.1-free`、`inclusionai/ling-3.1-flash`、`mistral/mistral-large-4`、`stealth/glyph-cluster:free`

闭源但含在 Go：`gpt-6-luna`、`gpt-5.6-luna`、`meta/muse-spark-1.2-contributor`、`meta/muse-spark-1.3-contributor`、`meta/muse-spark-open`、`xai/grok-4.5`

**明确不含 Go**：全部 Claude（`claude-*`，GOAT/Pro/Max）、`gpt-6-astra`/`gpt-6.1-sol`/`gpt-6-sol`/`gpt-5.6-sol`/`gpt-5.6-terra`/`gpt-5.5`/`gpt-5.4`/`gpt-5.3-codex`/`gpt-5.4-mini`、全部 `google/gemini-*`、`sakana/fugu-ultra`、`meta/muse-spark-1.1/1.2/1.3`(非 contributor)、`meta/muse-wonder-1.0`、`xai/grok-4.6/4.7`、`Qwen/Qwen3.8-Max-Prime`(GOAT+)、`xiaomi/mimo-v2.6-pro-ultraspeed`(GOAT+)。
免费档：`reflection/beam:free`（0 计费）、`poolside/laguna-s-2.1-free`（0 计费）、`stealth/glyph-cluster:free`（0 计费，**100 次/日**上限）。

### D3. 额度（官方文档实测值）

- 订阅 $1/月 → **$10 credits**（10x）。
- 双层窗口：**5 小时 $3 / 周 $6 / 月 $10**（皆以"钱"计，不是请求数）。
- 2026-09-28 起新订户改"按模型月度 credits"（每模型 $6–$10 等值），老订户本周期仍为 flat $10。
- 官方请求数估算（Go 页面表格）：DeepSeek V4 Flash 4,620/5h、9,230/周、15,400/月；DeepSeek V4.1 Flash 7,690/15,400/25,600；GLM-5.2 203/406/677；GPT-5.6 Luna 1,330/2,660/…
- 超额不断流：窗口超限后自动消耗 on-demand credits（若已充值）；否则等窗口重置。
- **并发上限：官方文档全站未出现 concurrent/parallel request 字样（已 grep 全部抓取文本）→ 未公布 / 未证实**。第三方实现里也没有并发闸门，只做多账号轮询。
- 单次输出默认上限 **64,000 tokens**（CLI `HT=64e3`；cmdgo-bridge 默认 `maxTokens: 64000`）。
- 上下文：以 `/provider/v1/models` 的 `context_length` 为准（1M 居多，部分 200K/256K/262K/500K/1.05M）。

---

## E. 客户端实现要点

1. **拿 token**：跑 `cmd login`（npm `command-code`，bin 名 `cmd`/`cmdc`/`commandcode`/`command-code`）后读 `~/.commandcode/auth.json` 的 `apiKey`（兼容字段 `api_key`）；或自行起 5959-5968 回调复刻 studio 握手（见 B3）。env 覆盖：`COMMAND_CODE_API_KEY`、`COMMANDCODE_CONFIG_DIR`。
2. **本地持久化布局**：`~/.commandcode/config.json`（prod；`config.staging.json` / `config.local.json`）、`auth.json`（0600，key）、`providers.json`（BYOK 供应商）。CLI 另有 env `COMMANDCODE_API_ENV`、`--local/--staging`。
3. **必需头**：`Authorization: Bearer`、`content-type: application/json`、`user-agent: cli`、`x-command-code-version`（必须接近当前 CLI 版本）、`x-cli-environment: production`、`x-session-id`（放 threadId）、`x-project-slug`、`x-taste-learning: false`。缺 `x-command-code-version` 的行为未实测。
4. **流式解析**：按 `\n` 切行 → `JSON.parse` → 判 `type`；不要按 SSE 规则找 `data:`；需处理跨 chunk 半行、末尾无换行、超长行（建议 8MB/事件、64MB/响应上限，cpa 的取值）。
5. **工具调用**：请求侧 `tools[].input_schema`；响应侧 `tool-call` 事件给完整 `input` 对象；回填历史时用 `{type:"tool-call",toolCallId,toolName,input}` + 独立 `role:"tool"` 消息携带 `{type:"tool-result",...,output:{type,value}}`。工具名需与请求侧一致（CLI 有 `toWireToolName` 归一，长名可能被改写）。
6. **多轮/续跑**：`finishReason === "pause_turn"` 要重发（带上已有消息）而不是当作结束；CLI 有 `nS` 次上限。
7. **超时**：cmdgo-bridge 单请求 300s 预算；cpa 的 transport `ResponseHeaderTimeout: 60s`、Dial 15s。上游首 token 可能很慢（reasoning 模型）。
8. **降级/失败语义**：401 → 凭据失效（重新登录/换 key）；403 且 body 含 `MODEL_NOT_IN_PLAN` → 该模型不在套餐；429 → 限流（退避）；400 含 context/token limit → 上下文超限。CLI 内部错误码前缀：`MODEL_NOT_IN_PLAN:`、`PREMIUM_CREDITS_EXHAUSTED:`、`RENEWAL_PENDING`。

---

## F. 风险与不确定性

| # | 风险 | 证据 | 可信度 |
|---|---|---|---|
| 1 | **403 `upgrade_required`**（Go 打 Provider 端点） | 无任何官方文档出现该字符串（grep models/pricing/provider/errors 页均 0 命中）；来自 cmdgo-bridge README 与题面描述；我实测 Provider 端点对无效 key 返回 401 `UNAUTHORIZED`，无 key 亦 401 — 无法在无 Go 订阅下复现 403 | 传闻/未证实（官方口径只到"Go 无 API 访问"） |
| 2 | **协议漂移**：CLI 版本驱动格式 | `/alpha/generate` 不在任何公开文档；CLI 一月内从 1.31→1.88 迭代；`config/memory/params` 结构随 CLI 变；cpa 注释"gateway rejects stale versions" | 源码级 |
| 3 | **cmdgo-bridge 模型过滤已过期**（可复现的逻辑缺陷） | 其 `isGoModel()` 硬编码例外集只有 `gpt-5.6-luna / xai/grok-4.5 / meta/muse-spark-1.2-contributor`：漏掉 `gpt-6-luna`、`meta/muse-spark-open`、`meta/muse-spark-1.3-contributor`（真 Go 被误杀）；误放 `Qwen/Qwen3.8-Max-Prime`、`xiaomi/mimo-v2.6-pro-ultraspeed`（实为 GOAT+） | 源码级（可用 models.md 对拍） |
| 4 | **ToS：多账号 = 明确违规** | Terms：限 1 账号；"creating or operating multiple accounts… material breach… permanent, lifetime ban"；"Automated tools… must not be used to… perform automated searches or requests"；平台自称用 email/支付/设备与网络标识/使用模式检测 | 官方 Terms 原文（实测抓取） |
| 5 | **单点封禁**：所有第三方桥都要复用 CLI 指纹（同版本头、同 session 形态） | 桥/插件实现均伪装 CLI；服务端若上强度校验（TLS 指纹、session 连续性、promptCache/threadId 缺失）即可批量识别 | 推断 |
| 6 | **token 生命周期不明** | 官方未公布 key TTL；CLI 无 refresh token，只有重新登录；hermes 明确"不做 OAuth refresh，key 静态" | 未证实（TTL）/源码级（无 refresh） |
| 7 | **Cloudflare 前置**：所有响应带 `server: cloudflare`、`cf-ray`、`x-trace-id`；异常流量会先被 CF 拦（我的一次无 auth 请求 25s 超时未回包，重试才 401，说明边缘行为不稳） | 实测 | 实测 |
| 8 | **额度口径会变**：2026-09-28 起 per-model credits；$6–$10 区间按模型浮动；promo/boosted 模型随时调整 | Go 计划页 Info 块 | 官方 |
| 9 | **代理要求**：未发现强制代理；直连 api.commandcode.ai 可用（实测）。中国大陆可达性未测 | 实测部分 | 部分实测 |
| 10 | **无官方并发参数**：并发只受 429/额度间接约束；多账号轮询是社区做法而非官方支持 | 文档 grep 0 命中 | 未公布 |

---

## G. 来源列表

**官方（实测抓取）**
- https://commandcode.ai/docs/plans/go （Go 计划：$1→$10、57 模型、5h $3/周 $6/月 $10、请求数估算表）
- https://commandcode.ai/docs/provider （Provider API：端点表 + "Every plan except the Go plan has API access"）
- https://commandcode.ai/docs/resources/pricing-limits
- https://commandcode.ai/docs/resources/usage-limits
- https://commandcode.ai/docs/troubleshooting/errors （该路径页面无 `upgrade_required` 字样）
- https://commandcode.ai/terms （单账号、反自动化条款）
- https://api.commandcode.ai/provider/v1/models （免鉴权，93 模型，含 `supported_endpoints`）
- npm `command-code@1.88.0`：`dist/cli.mjs`（`WT="/alpha/generate"`、`buildCommandAuthHeaders`、`In={prod:...}`、`~/.commandcode/config.json`）、`dist/bundled/command-code-knowledge/reference/models.md`（Min plan/Efforts/价格）、`reference/plans.md`、`dist/index.mjs`(bin)

**第三方实现（交叉验证）**
- https://github.com/Patrick-mufeng/cmdgo-bridge （README + `src/protocol.ts` 信封与 NDJSON 解析、`src/oauth.ts` 登录握手、`src/models.ts` 模型过滤、`src/openai.ts` 头与错码映射）
- https://github.com/megumin31/cpa-cmdcode-go （`go/gateway_client.go` 头集合 `User-Agent: cli`、`go/config.go` auth.json/版本策略、`go/event_decoder.go` NDJSON 状态机）
- https://github.com/NanamiMio/hermes-commandcode-oauth （README：`/alpha/generate` transport、只读 `~/.commandcode/auth.json`、64k 输出、2026-10-08 真机验证记录、403 归类为模型级失败）
- https://github.com/MAXeaglet/commandcode-proxy 、https://github.com/synthetic-coworkers/cmdcode2api （cmdgo-bridge 声明的参考实现，未逐行核对）

**实测探针记录**
- `POST /alpha/generate` 不完整信封 + 假 key → `400 BAD_REQUEST`（zod 逐字段报错）
- `POST /alpha/generate` 完整信封 + 假 key → `401 UNAUTHORIZED`
- `POST /provider/v1/chat/completions` 假 key / 无 key → `401`，信封 `{"error":{message,type,code}}`
- `GET /provider/v1/models` 无 key → `200`
