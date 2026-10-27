# CommandCode Go —— P1 真机验证（0.4.0）

> 时间：2026-10-27（UTC+8）
> 方法：`scripts/probe_commandcode.py`（读 Key 文件，不打印 Key），走插件自己的
> `CommandCodeClient` / `build_envelope` / `CommandCodeProvider` 真实链路，不是另写一份请求。
> 账户：`individual-go-v1`（`Eetherrr`，plan 字段为 `null`，与 P0 同一账户）
> 安全：Key 只放在 `.testenv/commandcode/api_key.txt`（已被 `.gitignore` 覆盖），未进入仓库与日志。

## 1. 结论

| 探针 | 结果 |
|---|---|
| `/alpha/whoami` / `/alpha/billing/credits` | **200**；`monthlyCredits ≈ 3.99`，`fiveHour used ≈ 0.336/3`，`weekly used ≈ 0.336/6` |
| 文本（`deepseek/deepseek-v4-flash`） | **200**；`finish=stop`，正文 `OK`，reasoning 78 字；usage 46/46（in/out），上游 `novita` |
| 工具（同模型，`reply` 工具，`reasoning_effort=off`） | **200**；`finish=tool-calls`，`tool-call.input={"text":"OK"}`；usage 280/36 |
| 视觉（`deepseek/deepseek-v4-flash-vision-exp`） | **200**；正文「颜色」，usage 232/34；网关把请求路由到 `deepseek/deepseek-v4.1-flash`，单次成本 ≈ `$0.0001104` |
| 第二个厂商（`Qwen/Qwen3.7-Flash`） | **200**；文本 `OK`（usage 30/248），工具调用 **成功**（`{"text":"OK"}`，usage 285/200）；网关路由到 `alibaba/qwen3.7-flash`，单次成本 ≈ `$0.00003` |
| Provider 全链路（Host 请求形状 → 投影 → 预算 → `output_items`） | **200**；`["ReasoningItem","AssistantMessageItem"]`，预算落账 1 条 |
| 模型目录 | 56 条 Go 模型，`cc-*` 名字唯一，`scripts/gen_commandcode_catalog.py --check` 通过 |

## 2. 真机发现（已修）

### 2.1 `threadId` 空串会 400

第一次探针把 `threadId=""` 发出去，网关返回：

```json
{"success":false,"error":{"code":"BAD_REQUEST","status":400,
 "message":"Invalid request error. Often missing required parameters or typo. HINT: Validation error: Invalid UUID at \"threadId\""}}
```

结论：**`threadId` 可以不带；一旦带上就必须是合法 UUID，空串会被 zod 拒绝。**
已在 `protocol.build_envelope()` 里兜底：`thread_id or uuid4()`，Provider 侧本来就会生成并复用 UUID。

### 2.2 工具形状与 P0 一致

- 发送形状：`{"name","description","input_schema"}`（无 `type`），200；
- 最终 `tool-call.input` 是完整对象，不需要从 `tool-input-delta` 拼；
- `reasoning_effort=off` 时工具调用没有 reasoning 前缀，`max_tokens=512` 足够。

### 2.3 视觉模型会被网关改写路由

请求 `deepseek/deepseek-v4-flash-vision-exp`，`providerMetadata.gateway.routing` 里
`originalModelId` 显示为 `deepseek/deepseek-v4.1-flash`、`resolvedProvider=deepseek`。
说明上游可能把旧的 vision-exp 别名路由到新模型；**目录仍按 models.md 的精确 id 注册**，
不要依据一次响应去改名。

### 2.4 账户窗口

本次探测时 weekly 与 fiveHour 的 `used` 相同，说明新周期刚开始、没有跨周历史；
P0 观察到的“weekly 先撞上限”仍是需要保留的容量判断（见可行性报告 §3.7）。

另外观测到账户 `fiveHour.used` 在两次探测之间从 `≈0.336` 涨到 `≈0.458`，远高于这几次小请求的
token 成本（单次约 `$0.00003`~`$0.00011`）。可能是同一账号上还有其它 CLI 使用，或上游计费口径
包含非 token 项。**结论：本地预算只能当保守硬闸，不能当作上游账单的精确镜像；以
`/alpha/billing/credits` 为准定期对账。**

### 2.5 第二个厂商可用

`Qwen/Qwen3.7-Flash`（网关路由到 `alibaba`）文本与工具调用均 200，`tool-call.input` 仍是完整对象。
这支持“目录里多厂商模型按同一协议工作”的判断，但**不构成 56 个模型逐个可用的承诺**。

### 2.6 prompt cache 不严格依赖 threadId

复跑探针时，工具探针（每次都是新的随机 `threadId`）出现了 `cacheReadTokens=256`
（`prompt_tokens=280`、`cache_miss=24`）。说明缓存命中至少部分按**请求内容前缀**生效，
不是“换 threadId 就必然全价”。Provider 仍然按模型复用 threadId（能给就尽量给），
但预算不要假设“新会话 = 无缓存”。

## 3. 仍未验证

- 56 个模型逐一的文本/工具稳定性（本次只测了 DeepSeek V4 Flash 系列与 Qwen3.7-Flash）；
- 长上下文（6 万字符级）下的首包时间与 `pause_turn` 实际触发；
- 并发上限（本插件默认 `max_concurrency=2`）；
- Key TTL 与 `x-command-code-version` 过期后的拒绝形态；
- 计费接口在 `orgId=null` 账户上的长期稳定性（本次 whoami/credits 均 200）。

## 4. 复现方式

```bash
# 1) 准备 Key（不要提交进仓库）
set COMMAND_CODE_API_KEY=user_xxx          # Windows
export COMMAND_CODE_API_KEY=user_xxx       # bash

# 2) 真机探针（文本 + 工具 + 视觉 + Provider 全链路）
python scripts/probe_commandcode.py --skip-vision   # 只想省钱时跳过视觉

# 3) 只校验目录
python scripts/gen_commandcode_catalog.py --check
```
