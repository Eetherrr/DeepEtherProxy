# CommandCode Go —— 真 Key P0 实测结果

> 实测时间：2026-10-25（UTC+8）
> 样本：用户提供的 Go 账户（`individual-go-v1`，active，周期 2026-10-18 ~ 2026-11-18）
> 方法：Python 3.12 + `httpx`（HTTP/1.1），直接 `POST https://api.commandcode.ai/alpha/generate`
> 安全：Key 只在临时文件中读取，未写入本仓库、未进入日志；原始事件流已脱敏归档到 `p0-events/`。

## 1. 结论

| 问题 | 结果 |
|---|---|
| 真 Key 能否在 Python 客户端跑通 `/alpha/generate` | **能**，HTTP 200，`content-type: text/event-stream` |
| 是否被 Cloudflare challenge | **没有**；本次 10 余次请求全部正常返回，无 HTML 403/1020 |
| 结束事件与用量 | 同一流里同时出现 `finish-step`（`usage`）和 `finish`（`totalUsage`）；两者字段形状一致 |
| 工具调用 | **能**；`tools` 带不带 `type:"function"` 都返回 200；最终 `tool-call.input` 是完整对象 |
| 工具参数流式 | 真实存在 `tool-input-start` / `tool-input-delta` / `tool-input-end`；最终 `tool-call` 仍给完整对象 |
| 图片 | `mimeType` 与 `mediaType` **都被接受**；视觉模型能正常描述图片 |
| `threadId` | 可省略；不带也能 200（本次 `no_thread` 探针） |
| 非 Go 模型 | 403 `FORBIDDEN`，message 前缀 `MODEL_NOT_IN_PLAN:` |
| Go Key 打 Provider API | 403 `permission_error` / `code:"upgrade_required"`，与题面一致 |
| 计费/用量接口 | 200；可读 plan、credits、5h/weekly 窗口、usage summary |
| Prompt cache | 同 `threadId` 第二次请求出现 `cacheReadTokens`（本次 256），缓存生效 |

## 2. 账户侧观测（不含 PII）

| 项 | 值 |
|---|---|
| planId | `individual-go-v1` |
| status | `active` |
| 周期 | 2026-10-18 ~ 2026-11-18 |
| 本周期请求数 | 2,939（completed 2,939 / failed 0 / successRate 100） |
| 本周期总花费 | ≈ $5.555 |
| 平均单次成本 | ≈ $0.00189 |
| credits 余额 | monthlyCredits ≈ 4.45 |
| 5h 窗口 | used ≈ 0.508 / cap 3 |
| weekly 窗口 | used ≈ 5.549 / cap 6（接近上限） |

> 说明：这是单一账户、单周期观测，不是容量承诺；`weekly` 已接近上限，继续高频调用会先撞周窗口。

## 3. 文本探针（`text`）

请求：`deepseek/deepseek-v4-flash`，短 prompt，`max_tokens=48`，默认 reasoning。
结果：200，耗时 ≈ 3.07s，正文 `OK`，reasoning 正常返回。

事件序列（节选）：

```
start
start-step
reasoning-start
reasoning-delta × 13
reasoning-end
text-start
text-delta
text-end
finish-step   { finishReason, rawFinishReason, usage: {...} }
finish        { finishReason, rawFinishReason, totalUsage: {...} }
provider-metadata
```

`finish` 的 `totalUsage`：

```json
{
  "inputTokens": 46,
  "inputTokenDetails": {"noCacheTokens": 46, "cacheReadTokens": 0},
  "outputTokens": 34,
  "outputTokenDetails": {"textTokens": 2, "reasoningTokens": 32},
  "totalTokens": 80,
  "reasoningTokens": 32,
  "cachedInputTokens": 0
}
```

`finish-step` 的 `usage` 与 `finish.totalUsage` 数值一致；`finish-step` 额外带 `providerMetadata`。

## 4. 工具探针（`tool_official` / `tool_typed`）

两种工具定义都返回 200：

```jsonc
// official shape（无 type）
{"name":"reply","description":"Send a chat reply",
 "input_schema":{"type":"object","properties":{"text":{"type":"string"}},"required":["text"]}}

// typed shape
{"type":"function","name":"reply","description":"Send a chat reply",
 "input_schema":{"type":"object","properties":{"text":{"type":"string"}},"required":["text"]}}
```

`max_tokens=256`、`reasoning_effort=off` 时：

```
tool-input-start
tool-input-delta × 9
tool-input-end
tool-call  {"toolCallId":"call_...","toolName":"reply","input":{"text":"OK"}}
finish-step { finishReason:"tool-calls", usage:{...} }
finish      { finishReason:"tool-calls", totalUsage:{...} }
```

- 第一次用 `max_tokens=48` 时，工具参数被截断成 `"{\"text"`，并出现 `tool-error` / `invalid:true`；
  **`max_tokens` 太小会把工具调用截断**，planner 场景要给足预算。
- 最终 `tool-call.input` 是对象，不是 JSON 字符串；`tool-input-delta` 是增量文本，解析器可以累积，
  但以最终 `tool-call` 为准。
- 两种工具形状都接受，说明服务端对 `type` 字段宽容；MVP 按官方 CLI 形状（无 `type`）即可。

## 5. 工具回放探针（`tool_loop`）

两轮：

1. 第一轮带 tools，模型返回 `tool-call` + reasoning；
2. 第二轮把 assistant 的 `reasoning` 块 + `tool-call` 块 + `role:"tool"` 的 `tool-result` 回放。

结果：带 reasoning 回放 **200**；不带 reasoning 的负向对照也 **200**（该模型/供应商没有强制报错）。
第二轮因 `max_tokens=96` 被 reasoning 吃满，没有正文，但请求本身成功。

> 参考实现（Mars-Sea）的 issue #34 记录：某些 DeepSeek thinking-mode 工具循环会拒绝缺少
> reasoning 的 assistant tool call。本次 `deepseek/deepseek-v4-flash` 未复现，但**保留 reasoning 是更安全
> 的兼容策略**，不要主动丢弃。

## 6. 图片探针（`vision_mimetype` / `vision_mediatype`）

模型：`deepseek/deepseek-v4-flash-vision-exp`；1×1 PNG。

| 字段 | 结果 |
|---|---|
| `mimeType` | 200；图片被接受，模型开始描述（本次 reasoning 用满 24 tokens，没出正文） |
| `mediaType` | 200；正文 `Pink`，reasoning 正常 |

→ 两种字段名服务端都接受；MVP 用官方 CLI 的 `mimeType` 即可，`mediaType` 作为兼容候选。

## 7. 错误探针

### 7.1 非 Go 模型（`/alpha/generate`）

```http
HTTP 403 application/json
{"success":false,"error":{"code":"FORBIDDEN","status":403,
 "message":"MODEL_NOT_IN_PLAN: Claude Sonnet 5.5 available in GOAT and above plans or extra on demand usage",
 "docs":"https://commandcode.ai/docs/reference/errors/forbidden"}}
```

注意：`MODEL_NOT_IN_PLAN` 是 **message 前缀**，不是结构化 code；分类必须做字符串前缀匹配（大小写不敏感）。

### 7.2 Go Key 打 Provider API

```http
POST /provider/v1/chat/completions
HTTP 403 application/json
{"error":{"message":"Your Go plan doesn't include API access. Upgrade to Provider or higher at
 https://commandcode.ai/billing to use these endpoints.","type":"permission_error","code":"upgrade_required"}}
```

→ `upgrade_required` 真实存在；Go 走 Provider API 必失败，必须走 `/alpha/generate`。

### 7.3 无 `threadId`

不带 `threadId` 的完整信封：200，正文 `OK`，usage 正常。说明 `threadId` 可省略；
但同 `threadId` 的第二请求出现 `cacheReadTokens=256`，带上有助于缓存。

## 8. 计费/用量接口

| 端点 | 状态 | 返回要点 |
|---|---|---|
| `GET /alpha/whoami` | 200 | `user` 有 email/id/name/userName；本账户 `org` 为 null |
| `GET /alpha/billing/credits` | 200 | `credits.{monthlyCredits,purchasedCredits,freeCredits}`、`windowLimits.{fiveHour,weekly}` |
| `GET /alpha/billing/subscriptions` | 200 | `data.planId/status/currentPeriodStart/currentPeriodEnd/renewalPending` |
| `GET /alpha/usage/summary` | 200 | `totalCount/totalCost/averageCost/totalTokensIn/Out/periodBasis` |

`whoami` 的 `org` 为 null 时，credits/subscriptions **不带 orgId 也能返回 200**；
状态功能可以先 whoami，失败再直接查 credits，不要因为 orgId 缺失就放弃。

## 9. 仍未验证

- 56 个模型逐一可用性 / 工具调用稳定性（本次只测了 DeepSeek V4 Flash 系列）。
- 长上下文、`pause_turn` 续跑、64k 输出上限的实际行为。
- 并发上限、Key TTL、长期封号概率。
- `x-command-code-version` 过期后的具体拒绝形态。
- 其他上游供应商（本次 `providerMetadata` 出现 `novita`）是否行为一致。

原始事件流：`p0-events/*.ndjson`（已脱敏，含完整事件类型与 usage）。
