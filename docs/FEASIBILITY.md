# DeepEtherProxy 可行性分析报告

> 项目：把 DeepSeek 网页版反代为 MaiBot 的模型能力（MaiBot 第三方插件）
> 文档版本：v0.2（已并入产品决策：优先网页版 / 官方 API 兜底 / 自动获取凭证 / 个人使用 / 长期稳定）
> 日期：2026-10-20
> 核实基线：MaiBot **1.3.11**、**maibot-plugin-sdk 2.12.0**（本机源码实测，非仅依据文档）
> 工作区：`E:\Dev\EtherTool\DeepEtherProxy`

---

## 0. 结论摘要（先看这里）

**技术可行性：可行。**
MaiBot 提供了一个正好对口的扩展点 —— `@LLMProvider`：插件声明一个 `client_type`，主程序就把它注册进 LLM 客户端注册表，之后 `model_config.toml` 里任何模型的 `api_providers[].client_type` 指向它，这条模型走的就是你的插件。**不需要改 MaiBot 主程序一行代码**，且与"插件必须放 `plugins/<name>/`"的约束完全一致。

**但"反代网页版 = 拿到 DeepSeek 最新全部能力"这个前提不成立。**
需要区分三种能力：

| 能力 | 网页版反代能否提供 | 说明 |
|---|---|---|
| 思考模式（reasoning） | ✅ 能 | 网页版本身就返回 `thinking_content` |
| 联网搜索 | ⚠️ 视实现 | 网页版有内置搜索，但结果形态与 MaiBot 的工具调用体系不同 |
| 多轮上下文 | ✅ 能 | 但受 `parent_message_id` 链约束 |
| **Function Calling / 工具调用** | ❌ **不能（原生不支持）** | **这是致命缺口**，见 §4 |
| 结构化输出 `response_format` | ❌ 不能 | 需提示词模拟 |
| 图片输入 / OCR | ⚠️ 部分 | 依赖文件上传接口 |
| Token 用量与计费（usage） | ❌ 不能精确 | 网页版不返回标准 usage |

> ⚠️ 这一条是整个结论的支点：它是【高置信推断 + 多来源一致】，但**尚未实测**。P0 的第一个验证动作就应该是它（见 §10）。

### 0.1 已确认的产品决策（2026-10-20）

| 决策项 | 结论 | 对设计的影响 |
|---|---|---|
| 默认通道 | **优先用网页版反代**；工具调用实在不行才走官方 API | 路由默认 `web_first`，官方 API 是兜底不是主路 |
| 真实动机 | **省群聊对话的 token 成本** | 目标是"替代高频任务"，不是"替代全部任务"，见 §4.4 |
| 凭证获取 | **打开网页登录后自动获取**（不愿手动粘贴） | 需要额外做一条凭证获取通道，见 §6.5 |
| 使用范围 | **仅个人使用** | 可以砍掉多租户、权限、审计；但**不能**砍稳定性 |
| 稳定性要求 | **长期稳定** | 探活、续期、熔断、自动降级、漂移检测全部要做，见 §6.6 |

**这几条合起来会改变结论的强度**：原来最大的问号是"值不值得做"，现在变成"**怎么让它长期跑得住**"。风险重心从 §4（能力缺口）转移到 §6.5（凭证）与 §6.6（稳定性）。

**那能不能让插件自己扛起工具调用（Harness 式中间层）？**
不能。MaiBot 只给插件 `tool.get_definitions`，**没有 `tool.invoke`**——工具执行权被故意收在 Host（`reasoning_engine.py:1401`）。
可行的只有"插件把模型决策翻译成合法 `tool_calls`、Host 执行"这一种 shim 形态，见 §4.3。

**为什么工具调用是致命缺口：**
MaiBot 的核心工作流里，`planner` 任务是"战略核心 —— 决定何时说话、回复谁、调用哪些工具（MCP/插件）"。它强制依赖模型返回 `tool_calls`。而 DeepSeek 网页版是一个面向人的聊天产品，**没有 function calling 接口**。纯反代之后接进 `planner`，模型只会输出一段自然语言，MaiBot 拿不到任何工具调用，MCP、插件工具、@Tool 全部失效。

**推荐路线（详见 §5 / §6 / §7）：**

> **方案 B（混合 Provider，`web_first` 路由）**：网页版是主通道，官方 API 是按任务/按故障的兜底。
>
> 1. **一期（P1~P2）**：先只接 **`replyer`**（已核实可行，见 §4.5），跑通并观察一周后再扩到 `utils` / `mid_memory` —— 这些是 token 大户且不需要工具调用，**省 token 的主要收益就在这里**；
> 2. **`planner` 一期仍走官方 API**，等 P3 的 shim 用回归集量出准确率（≥95%）再决定是否迁移；
> 3. **凭证自动获取（§6.5）与稳定性（§6.6）不是"后续优化"，而是一期就必须有的东西** —— 你要求长期稳定，这两块决定项目能不能真正日用。

**最大的风险已经从"能不能做"变成"能不能长期跑得住"：**

| 新风险 | 为什么是现在最大 | 对策章节 |
|---|---|---|
| 凭证失效且无法自愈 | 自动获取 + 定期续期没做对，就是每天都得手动修 | §6.5 / §6.6 |
| 群聊峰值排队 | 并发约 3，突发消息会排队并撞 `hard_timeout` | §4.2 / §4.4 |
| 协议漂移无人察觉 | 官方前端一发版就可能整体失效 | §6.6 / §9.4 |

**而这三点都不影响"要不要做"——只影响"怎么做才不白做"。**

---

## 1. 目标与约束

### 1.1 项目目标（原始表述）
> 开发一个 MaiBot 插件，用于反代 DeepSeek 网页版，并与 MaiBot 对接提供模型能力，确保能保证 DeepSeek 的最新所有能力。

### 1.2 真实动机（不是"接入新模型"，是"降 token 成本"）

> 主要目的是**省群聊对话的 token**。

这个动机很重要，因为它把项目从"能力扩展"变成"**成本优化**"。成本优化问题要回答的是：
- **省哪部分？** 高频、长上下文、输出短的调用最费 token —— 也就是 `planner` / `utils` / `mid_memory` 这类每轮都跑的辅助任务（§4.4 有量化）；
- **省多少？** 取决于任务路由比例，不是"接上网页版就全省了"；
- **代价是什么？** 并发、延迟、稳定性、封号风险（§4.2 / §8）。

### 1.3 拆解后的可执行目标

1. **P0**：在 `plugins/deepehter-proxy/` 下提供一个能加载、能配置、能卸载的 MaiBot 插件。
2. **P0**：注册一个 `client_type`，让 `model_config.toml` 能把某个模型指向它，并在 MaiBot 里完成一次真实对话。
3. **P0.5**：**凭证自动获取 + 失效自愈**（登录网页即自动就位，不需要手动粘贴）——§6.5。
4. **P1**：把网页版的「思考模式 + 联网搜索 + 多轮上下文」映射为 MaiBot 可消费的响应，并让 `replyer`/`utils`/`mid_memory` 真实跑起来（这是省钱的主战场）。
5. **P2**：稳定性工程：探活、熔断、并发闸门、**自动降级到官方 API**、协议漂移检测 —— §6.6。
6. **P3**：工具调用 shim，把 `planner` 也迁到网页版（有量化验收门槛，见 §7）。
7. **贯穿**：个人使用场景下的可观测性（状态命令 + 中文日志）。

### 1.4 硬约束（来自用户与平台）
- 插件必须位于 `plugins/<plugin-name>/`。
- **不得修改 MaiBot 主程序代码**（除非明确许可）。
- 必须使用 `maibot-plugin-sdk`；入口 `plugin.py`，元信息 `_manifest.json`。
- 必须实现 `on_load`、`on_unload`、`on_config_update`、`create_plugin`。
- 优先使用 `@Tool`、`@Command`、`@HookHandler`、`@EventHandler`、`@API`、`@MessageGateway`；**新插件不使用 `@Action`**。
- 所有用户可见文本优先简体中文。
- 改动边界清晰，并给出测试方式。

---

## 2. MaiBot 插件体系：与本案相关的事实（源码核实）

本章结论来自本机实际安装的 MaiBot 源码，不是文档转述。源码路径约定：
`$MAI = E:\Dev\NapCat\NapCatData\apps\maibot\210720e0`

### 2.1 运行架构
- Host（主进程）与 Runner（子进程）通过 **msgpack RPC** 通信；第三方插件跑在独立 Runner 里。
- **插件不能 `import src.*`**，只能用 `maibot_sdk` + `self.ctx` 能力代理。
- 能力受 manifest `capabilities` 白名单控制，未声明即被拒绝。
- 插件专属可写目录：`data/plugins/{plugin_id}`、`temp/plugins/{plugin_id}`（`self.ctx.paths`）。
- 单次 RPC 默认 30s 超时，组件调用默认 60s；**LLM Provider 调用超时取 `api_provider.timeout`**（见 2.3）。

### 2.2 唯一正确的接入点：`@LLMProvider`

`$MAI/src/llm_models/model_client/__init__.py` 里内置 client_type 只有三个：

```python
_CLIENT_MODULE_BY_TYPE = {
    "openai": ".openai_client",
    "openai_responses": ".openai_responses_client",
    "gemini": ".gemini_client",
}
```

插件注册的 client_type 走另一条路（`$MAI/src/plugin_runtime/host/supervisor.py:1200-1221`）：插件上报 `llm_providers` 后，Host 用 `client_registry.replace_plugin_providers()` 把每个 client_type 绑定到 `PluginLLMClient` 工厂：

```python
client_registry.replace_plugin_providers(
    payload.plugin_id,
    [ClientProviderRegistration(
        client_type=provider.client_type,
        factory=lambda api_provider, provider_client_type=provider.client_type: PluginLLMClient(
            api_provider=api_provider, supervisor=self,
            plugin_id=payload.plugin_id, client_type=provider_client_type,
        ), ...) for provider in payload.llm_providers],
)
```

注册表的实现（`$MAI/src/llm_models/model_client/base_client.py:426-624`）关键行为：
- 双声明必须一致：manifest 的 `llm_providers` 与 `@LLMProvider` 装饰器。
- **client_type 冲突时双方插件都不加载**；与内置类型重名则在注册阶段报错。
- 插件卸载/禁用时 `unregister_plugin_providers()` 注销，之后请求按 MaiBot 的回退策略处理。
- 缓存按 `(event_loop, api_provider.name)` 维度，配置重载会清缓存。

**结论：不需要独立启动一个 HTTP 服务，也不需要让用户改 MaiBot 代码。插件即 provider。**

### 2.3 宿主 → 插件的请求契约（真实字段，非文档）

Host 侧在 `$MAI/src/llm_models/model_client/plugin_client.py:61-101` 组装请求，序列化函数是
`$MAI/src/llm_models/request_snapshot.py:685-698`。插件实际收到的 `request` 字典：

```jsonc
{
  "item_schema_version": 1,
  "request_kind": "response",
  "logical_turn_id": "…",
  "temperature": 0.7,
  "max_tokens": 8192,
  "response_format": null,            // 或 {"format_type": "...", "schema": {...}}
  "extra_params": { },                // 模型配置里的 extra_params 原样透传
  "model_info": {
    "api_provider": "deepehter",      // string
    "name": "deepehter-web",
    "model_identifier": "deepseek-web",
    "model_type": "...",
    "temperature": 0.7,
    "max_tokens": 8192,
    "extra_params": { },
    "force_stream_mode": false,
    "send_temperature": true,
    "visual": false
  },
  "tool_options": [ /* OpenAI function schema 数组 */ ],
  "context_items": [ /* 见下 */ ],
  "api_provider": { /* APIProvider.model_dump()，含 base_url / api_key / timeout / default_headers ... */ }
}
```

两个要点：

1. **消息不在 `message_list` 里，而在 `context_items` 里。** 这与官方文档示例的措辞不一致，以实现为准。
   `context_items` 元素形如（`request_snapshot.py:276-330`）：

   ```jsonc
   {"item_type": "SystemMessageItem",   "meta": {...}, "parts": [{"type": "text", "text": "…"}]}
   {"item_type": "UserMessageItem",     "meta": {...}, "parts": [{"type": "text", ...}, {"type": "image", ...}]}
   {"item_type": "AssistantMessageItem","meta": {...}, "parts": [...]}
   {"item_type": "ReasoningItem",       "meta": {...}, "representation": "raw_text", "text_parts": [...]}
   {"item_type": "FunctionCallItem",    "meta": {...}, "tool_call": {"call_id": "...", "func_name": "...", "args": {...}}}
   {"item_type": "FunctionCallOutputItem", "meta": {...}, "call_id": "...", "output": "...", "tool_name": "...", "success": true}
   ```

   `CONTEXT_ITEM_SCHEMA_VERSION = 1`（`$MAI/src/llm_models/payload_content/context_item.py:17`）。
   这意味着**反代层要把 `context_items` 拍平成网页版能吃的 prompt 文本**，工具调用与工具结果也不例外。

2. **`tool_options` 已经是 OpenAI function schema**（`request_snapshot.py:669-673` 调用 `to_openai_function_schema()`），可以直接原样展示给模型或塞进提示词。

超时：`plugin_client.py:173` → `timeout_ms = api_provider.timeout * 1000`（默认 120s）。**深度思考 + 联网搜索的长回答很容易超过 120s**，必须引导用户调大 `timeout`，或插件内部自己做分段/流式读取后提前返回。

### 2.4 插件 → 宿主的响应契约

`$MAI/src/llm_models/model_client/plugin_client.py:217-307` 决定了两条路径：

**路径 A（推荐，前向兼容）：`output_items` + `item_schema_version`**

```python
return {
    "item_schema_version": 1,          # 必须等于 CONTEXT_ITEM_SCHEMA_VERSION
    "output_items": [
        {"item_type": "ReasoningItem", "representation": "raw_text", "text_parts": ["…"], "summary_parts": []},
        {"item_type": "AssistantMessageItem", "parts": [{"type": "text", "text": "…"}]},
        {"item_type": "FunctionCallItem", "tool_call": {"call_id": "...", "func_name": "...", "args": {...}}},
    ],
    "usage": {...},
}
```
注意 `meta` 由 Host 自己注入（`plugin_client.py:255-262`），插件不用也不该造 item_id。

**路径 B（兼容旧协议，v1 可用）：`content` / `reasoning_content` / `tool_calls`**

```python
return {
    "content": "正文",
    "reasoning_content": "思考内容",
    "tool_calls": [ /* OpenAI 风格，经 deserialize_tool_calls_snapshot 解析 */ ],
    "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    "raw_data": {...},
}
```
走这条时 Host 会打一条 warning：*"插件 LLM Provider 返回旧版 content/reasoning/tool_calls 标量协议；该兼容入口将在下一个 Item schema 版本删除"*（`plugin_client.py:271-276`）。

> **取舍建议**：一期用路径 B 快速跑通；二期切路径 A。理由是路径 B 已明确标注将被删除，而本项目要"跟上最新能力"，长期必须走 A。

`usage` 支持字段（`plugin_client.py:189-214`）：`model_name`、`provider_name`、`prompt_tokens`、`completion_tokens`、`total_tokens`、`prompt_cache_hit_tokens`、`prompt_cache_miss_tokens`。

### 2.5 硬约束清单（会直接决定设计）

| # | 约束 | 证据 | 影响 |
|---|---|---|---|
| C1 | **插件 Provider 不支持 Host 侧流式** | `plugin_client.py:73-74` 直接抛 `RespParseException` | 插件内部可以流式读 SSE，但只能整体返回；MaiBot 侧看不到逐字输出 |
| C2 | 请求是 `context_items` 而非 `message_list` | `request_snapshot.py:685-698` | 必须写 `context_items` → prompt 的投影层 |
| C3 | 响应走 `output_items` 或旧标量协议 | `plugin_client.py:217-307` | 建议二期切 `output_items` |
| C4 | 超时取 `api_provider.timeout`（默认 120s） | `plugin_client.py:173` | 深度思考易超时，需文档指引 + 内部限流 |
| C5 | 插件异常以 `success:false` 返回，不抛给 MaiBot 主流程 | `runner_main.py:2222-2228` | 插件内必须自己兜住所有异常并给出可读中文错误 |
| C6 | 卸载后 client_type 注销，请求回退到下一可用模型 | `base_client.py:604-624` | 天然具备兜底，但要避免"静默降级"难排查 |
| C7 | 不能 `import src.*`；不能用主程序内部结构 | 插件运行边界 | 所有上下文投影逻辑必须自带 |
| C8 | `@LLMProvider` 与 manifest 双声明，冲突即不加载 | `base_client.py:543-570` | client_type 命名要加前缀，例如 `com.ether.deepehter` |

### 2.6 MaiBot 已经自带的能力（决定哪些任务值得迁到网页版）

- MaiBot **已有独立联网搜索子系统**：`$MAI/src/web_search/`，providers 含 `deepseek.py` / `tavily.py` / `stepsearch.py`，并作为 `model_task_config.web_search` 任务存在。→ 网页版的"联网搜索"并非不可替代。
- MaiBot **已经原生支持 DeepSeek 官方 API 的全部现代开关**（`docs/_ref/maibot/model-extra-params.md`）：
  - `openai` 客户端：`thinking = {type = "enabled"}` + `reasoning_effort`（`high` / `max`）
  - `openai_responses` 客户端：`reasoning = {effort = "..."}` + **原生 `web_search` 工具**（DeepSeek v4 flash 联网搜索）
  - WebUI 有 DeepSeek 专用思考/联网开关

> **这条对项目定位很关键**：如果目标只是"让麦麦用上 DeepSeek 的思考与联网"，官方 API 已经覆盖，反代网页版的**真正增量只有两点**：(a) 复用网页版免费/低价额度；(b) 网页版独有形态（如文件上传、内置搜索融合结果）。项目价值应据此重新表述，否则容易做完了发现"没有增量"。

---

## 3. DeepSeek 网页版：能力与协议

> 证据等级标注：【源码】直接读到实现代码；【多源】2 个以上独立来源一致；【单源】单一来源；【推断】基于协议形态的推理。
> 本章协议细节以 2026-09 公开的逆向结论为准，**时效性强，随时可能被官方改动**。

### 3.1 一次完整调用链路

网页版没有公开 API，必须自己拼出这条链路【多源】：

```
MaiBot ──> 插件 ──> ① POST /api/v0/chat_session/create        取得 chat_session_id
                     ② POST /api/v0/chat/create_pow_challenge  取得 PoW 挑战
                     ③ 本地求解 PoW（约 0.3s）
                     ④ POST /api/v0/chat/completion            SSE 增量流（JSON-Patch）
                     ⑤ 把增量翻译成 thinking / content
```

关键点：**每个请求都要领一次挑战并解一次题**，PoW 是一次性的。

### 3.2 鉴权

- 请求头 `Authorization: Bearer <token>`【多源】
- token 来源：浏览器登录 `chat.deepseek.com` 后，`localStorage.userToken` 的 `.value`【多源】
- 也就是说：**凭证获取必须人工做一次**（复制 localStorage 值），插件无法全自动登录（自动登录需要账号密码 + 更严的风控参数，风险显著更高）
- token 会过期/失效，需要用户重新粘贴

> 对插件的含义：`config.toml` 里要有一个 `web_token` 字段，并且必须做「凭证失效」的可读中文报错与探活机制，否则用户只会看到一堆 401。

### 3.3 PoW 挑战（本项目最大的技术门槛，但已有解）

**提交格式**【多源】：

```
x-ds-pow-response: base64(JSON {
    "algorithm": "DeepSeekHashV1",
    "challenge": "<服务端下发>",
    "salt":      "<服务端下发>",
    "answer":    <本地算出的 float>,
    "signature": "<服务端下发>",
    "target_path": "/api/v0/chat/completion"
})
```

- **一次性**、约 **300 秒**过期；重复使用会返回 `40301 INVALID_POW_RESPONSE`【多源】
- 算法并不在 JS 里，而在一个 sha3 WASM 模块中；JS 只负责把 challenge / 前缀 / difficulty 传进去【源码级博客】
- 求解输入前缀：`f"{salt}_{expire_at}_"`，难度为服务端给的 `difficulty`【源码】

**纯 Python 可行性：已验证可行**【源码】。
PyPI 上已有成熟实现 `deepseek-pow`（0.0.1，MIT/Apache 类许可，作者声明非官方）：

```python
# 依赖：wasmtime（原生轮子，Python 3.12 可用）+ pydantic
from deepseek_pow import Challenge, registry

challenge = Challenge(
    algorithm="DeepSeekHashV1",
    challenge="…", salt="…",
    difficulty=144000, expire_at=1783681627181,
    signature="…",
)
solution = registry.get(challenge.algorithm).solve(challenge)
payload = solution.to_answer_payload()   # 直接可 base64 后当请求头
```

它随包内置了一个 26 KB 的 `sha3_wasm_v1.wasm`，用 wasmtime 执行，**不依赖联网下载官方 chunk**，这一点比"动态发现官方 worker chunk"的方案更稳。

**风险**：
- 若官方把算法升级到 `DeepSeekHashV2`，内置 wasm 失效 → 必须更新依赖或自己适配（该库预留了 solver 注册机制）。
- `wasmtime` 是原生扩展，会拖入约 10–20 MB 依赖；对 MaiBot 插件可接受（manifest 支持 `python_package` 依赖声明）。

### 3.4 请求体与会话模型

`POST /api/v0/chat/completion` 的 body 是**扁平结构**【单源，需实测复核】：

```jsonc
{
  "chat_session_id": "…",
  "prompt": "用户这一轮的内容",
  "thinking_enabled": true,
  "search_enabled": false,
  "model_type": "default",
  "parent_message_id": null,
  "ref_file_ids": []
}
```

**网页版是有状态的**：靠 `chat_session_id` + `parent_message_id` 维护对话链。
而 MaiBot 传给插件的是**完整 `context_items`（无状态全量历史）**，两者模型不匹配。两种适配策略：

| 策略 | 做法 | 优点 | 缺点 |
|---|---|---|---|
| **S1 无状态（推荐 v1）** | 每次请求新建 session，把全部 `context_items` 渲染成一个 prompt 一次发出 | 无链断裂、并发安全、易测试、可重放 | 每次多 2 个请求开销；丢掉网页版的原生上下文管理 |
| S2 有状态 | 按会话指纹维护 session + parent_message_id 链 | 开销小、接近网页体验 | 并发/重试/回滚复杂，链一断就雪崩 |

> **建议 S1**。MaiBot 本来就每轮全量给上下文，插件天然可以"无状态化"，这是把状态型上游变简单的标准做法。

### 3.5 响应流

- 上游是 **SSE + JSON-Patch（op/path/value）增量**，不是 OpenAI 那种 `choices[].delta`【多源】
- 思考内容与正文通过不同 patch path 区分，需分别累积【多源】
- 实现要求：忽略未知 op、断流可重试、最长读超时保护

### 3.6 usage（计费信息）

- 上游只给 `accumulated_token_usage` 总量，**`prompt_tokens` 恒为 0**【单源】
- 后果：MaiBot 的 `price_in` / `cache_price_in` 统计会失真
- 对策：返回 `usage.total_tokens` 即可，并在文档里提示用户把该模型的 `price_in` / `price_out` 设 0（反正网页版不按 token 计费）

### 3.7 能力矩阵：网页版 vs 官方 API

| 能力 | 网页版反代 | DeepSeek 官方 API | 说明 |
|---|---|---|---|
| 思考模式 / reasoning | ✅ | ✅ `thinking.type`、`reasoning_effort` | 网页版开箱即得 |
| 联网搜索 | ✅ 内置 | ✅ `openai_responses` + `web_search` 工具 | 但两者产出形态不同 |
| **Function Calling** | ❌ **无** | ✅ | **决定性差距** |
| 结构化输出 `response_format` | ❌ | ✅ | 只能靠提示词模拟 |
| 图片/多模态输入 | ⚠️ 仅文件上传（提取文本），非视觉 | ✅（v4 `visual=true`） | 需实测 |
| Embedding | ❌ | ❌（DeepSeek 无 embedding API） | 两者都做不了 |
| ASR 语音识别 | ❌ | ❌ | 两者都做不了 |
| usage 精度 | ❌ 仅总量 | ⚠️ 部分 | — |
| 并发上限 | **约 3（账号级）** | 付费额度决定 | 见 §4.2 |
| 稳定性承诺 | 无，随前端发版漂移 | 有 | — |
| 成本 | 账号额度（免费/低价） | 按量计费 | 本项目的核心动机 |

> 关于 Function Calling 的证据说明：网页版请求体只有 `prompt` / `thinking_enabled` / `search_enabled` 一类字段，没有任何 `tools` / `tool_choice` 入口；公开的 OpenAI 兼容反代项目（DS-Direct-Proxy / freeseek 等）也**没有一个宣称支持 tool_calls**。
> 因此"网页版无原生工具调用"是【多源 + 推断】，置信度高，但**尚未由我们实测确认**——这应是 P0 阶段第一个要验证的假设。
> 若实测发现新版网页版已支持工具调用，§4 的结论与整个方案价值都要重估。

### 3.8 合规与稳定性

- **ToS 风险**：逆向网页接口通常违反服务条款，可能导致账号限流或封禁。公开实现普遍附带免责声明并明确"禁止商业用途"。
- **风控**：并发高于 3、请求频率过高、异常行为都会触发风控。社区一致建议并发压到 3 以内。
- **协议漂移**：前端每次发版都可能改接口/字段/加密，这正是"保证最新能力"这句话的**反面**——你在追一个没有契约的目标。
- **无 SLA**：任何时刻都可能整体不可用。

---

## 4. 能力差距矩阵（MaiBot 任务 × 网页版供给）

MaiBot 一次"收到消息 → 发出回复"会触发 **3~6 次 LLM 调用**，分散在 `planner` / `replyer` / `utils` / `fast_model` / `mid_memory` 等任务上（`docs/_ref/maibot/model-config.md`）。

| MaiBot 任务 | 需要什么 | 网页版能否满足 | 结论 |
|---|---|---|---|
| `planner` | 强推理 + **tool_calls** | ❌ 无 function calling | **一期不要接** |
| `replyer` | 高质量文本 + 思考 | ✅ | 一期主力场景 |
| `utils` | 文本分析（表情/学习/取名） | ✅ | 一期可用 |
| `fast_model` | 低延迟短文本 | ⚠️ 延迟偏高 | 二期再评估 |
| `mid_memory` | 长文本摘要 | ✅ 但受上下文长度限制 | 一期可用（需截断策略） |
| `emoji` | 从候选里选一张 | ✅ 文本决策即可 | 一期可用 |
| `vlm` | 视觉输入 | ❌ 非视觉 | 不做 |
| `embedding` | 向量 | ❌ | 不做，回退官方 |
| `web_search` | 返回结构化搜索结果 | ❌ 形态不匹配 | 用 MaiBot 自带 `src/web_search` |
| `voice` (ASR) | 音频转写 | ❌ | 不做，回退官方 |

### 4.1 结论

> **能做的是"文本生成类任务"，做不了的是"工具调度类任务"。**
> 而 MaiBot 的智能程度主要由 `planner`（工具调度）决定。所以：
> - 一期把网页版接在 `replyer` / `utils` / `mid_memory`，**能立刻提升回复文风与思考质量**，风险最低；
> - `planner` 继续用官方 API（`openai_responses` + 工具），保证 MCP / 插件工具正常；
> - 这是"混合架构"，不是"完全替代"。要诚实接受这一点。
>
> 工具调用能否换个方式由插件承担？见 **§4.3**（结论：可以"翻译"，不能"执行"）。

### 4.2 延迟与并发预算（必须提前算）

单次网页版调用开销估算：
- PoW 求解 ≈ 0.3 s
- 建会话 + 领挑战 ≈ 2 个 HTTP 往返
- 首 token ≈ 1~3 s（思考模式更久）
- → **单次 3~15 s**

MaiBot 一轮对话 3~6 次调用，且**账号级并发上限约 3**。

**容量模型（按"个人群聊"重新算，结论比初稿乐观）：**

| 维度 | 估算 | 说明 |
|---|---|---|
| 单次调用耗时 | 3~15 s | PoW + 建会话 + 生成 |
| 有效并发 | 2~3 | 留 1 个余量给风控 |
| **稳态吞吐** | **约 500~2000 次/小时** | `并发2 / 12s` 到 `并发3 / 5s` |
| 典型群聊负载 | 100~500 次/小时 | 依赖频控配置，非每条消息都触发 |
| 突发峰值 | 20 条同时触发 = 20 次排队 | 平均等待可达 50 s+，会撞 `hard_timeout` |

**结论修正**：*稳态完全够用，瓶颈只在突发峰值。* 所以不需要"劝退"，需要的是**排队与降级策略**：

1. 插件内信号量限制并发（默认 2），超出立刻判定"排队超时风险"；
2. 预计超过 `hard_timeout` 的请求**直接走官方 API**，而不是傻等（见 §6.6）；
3. 对`planner` 这类延迟敏感任务，一开始就不放在网页版（§4.4）。

### 4.3 工具调用能否由插件做中间层？

> 结论先行：**"插件翻译 + Host 执行"可行；"插件自己执行工具并持有 loop"（Harness 式）不可行。**

#### 4.3.1 两种"中间层"必须先分开

| | A. 翻译层（shim） | B. 执行层（Harness 式 agent loop） |
|---|---|---|
| 谁决定调用 | 网页版模型 | 网页版模型 |
| 谁解析调用 | 插件 | 插件 |
| **谁执行工具** | **Host** | **插件** |
| 谁持有 loop | Host | 插件 |
| 可行性 | ✅ 架构已预留 | ❌ 缺少能力，需改主程序 |
| 本质 | OpenAI function-calling 协议适配器 | 自建 agent runtime |

#### 4.3.2 B 不可行的证据

MaiBot 给插件的约 80 个能力里，**只有 `tool.get_definitions`，没有 `tool.invoke`**：

```
$MAI/src/plugin_runtime/capabilities/registry.py:88
_register("tool.get_definitions", manager._cap_tool_get_definitions)
```

工具的真实执行在 Host 内部，由 `ToolInvocation` 统一封装：

```
$MAI/src/maisaka/reasoning_engine.py:1401-1419
def _build_tool_invocation(self, tool_call, latest_thought) -> ToolInvocation:
    return ToolInvocation(
        tool_name=tool_call.func_name,
        arguments=self._runtime.expand_message_id_aliases(dict(tool_call.args or {})),
        call_id=tool_call.call_id,
        session_id=..., stream_id=..., reasoning=latest_thought,
    )
```

之后按名字在 **builtin / MCP / 插件 `@Tool`** 三处解析并执行。三条可能的旁路都验证过，全部堵死：

| 旁路 | 结论 | 证据 |
|---|---|---|
| `ctx.llm.generate_with_tools` | 只"带 tools 调一次模型并**返回** `tool_calls`"，不执行 | `capabilities/core.py:667-703` |
| Hook 拦截工具执行 | 没有工具执行点。`maisaka.planner.after_response` 可改参但**不允许 abort** | `hook_catalog.py:16-37`；`docs/_ref/maibot/hooks.md:251` |
| `component.*` 能力 | 插件管理（load/enable/disable），不是工具调用 | `capabilities/registry.py:95-105` |

**要绕过它只能改 MaiBot 主程序，越过了本项目的硬约束。**

#### 4.3.3 但这个分工是 MaiBot 故意切的，插件不该抢

工具执行的上下文全在 Host：会话 `session_id`、群/私聊判定、`message_id` 别名展开、工具可用性判定（`ToolAvailabilityContext`）、超时与熔断、调用日志与统计。插件若私接 MCP 自行执行，等于把安全边界打穿。

#### 4.3.4 插件唯一能自主拥有的 loop 是什么

**单次 provider 调用内部，对网页版自身多做几轮上游请求**，最后只把终答交回 Host。
例如让网页版先用它自己的内置搜索查一轮，再基于结果作答。

这说明一个现实边界：插件"能自己实现一份"的工具，只能是**外部能力型工具**（搜索、计算、抓网页）；而 `reply` / `send_emoji` / `stop` / `fetch_history` / `query_memory` 全部依赖 Host 会话状态，**复制不出来**——所以 `planner` 依然无法用"插件自建 loop"替代。

#### 4.3.5 A 方案的实现要点（P3 真正要写的代码）

关键前提：任何 provider 返回的 `tool_calls` 都进同一个 Host 执行器，所以插件只要产出合法调用即可。

1. `tool_options` 已经是 OpenAI function schema（`request_snapshot.py:669-673`），压成紧凑"能力清单"写进 system prompt。
2. 约定输出格式：只输出一个 JSON，给严格 schema + 2~3 个 few-shot。
3. 解析容错：代码围栏、前后废话、单引号、尾逗号、多调用数组。
4. **工具名白名单校验**：不在 `tool_options` 里的丢弃并回退纯文本，避免 Host 报未知工具。
5. 参数按 schema 校验，失败**不猜**。
6. 输出 `FunctionCallItem`；`call_id` 自生成，`logical_turn_id` 由 Host 注入（`plugin_client.py:255-262`、`context_item.py:335-337`）。
7. 下一轮把 `FunctionCallItem` + `FunctionCallOutputItem` 渲染成网页模型能理解的对话片段。

#### 4.3.6 风险

| 风险 | 说明 | 对策 |
|---|---|---|
| 准确率 | 网页模型未按 function caller 训练，工具一多就漏调错参 | 控制工具数量与名字长度；只接高频工具 |
| 延迟 | 每次工具往返 = PoW + 建会话 + 全量上下文重发；3 轮工具链≈3 次网页请求 | 限制单轮工具轮数；超时即降级 |
| 上下文膨胀 | 无状态 S1 每轮重发全部历史，工具结果又长 | 工具结果截断 + 摘要 |
| 协议外泄 | 模型把 JSON / "我要调用工具"写进正文 | 输出后处理剥离 |
| 静默降级 | 解析失败无日志，表现为"麦麦突然不会用工具" | 全部降级必须有中文日志 |

#### 4.3.7 决策

- 想要 Harness 式"中间层执行工具"，缺的不是设计，是 MaiBot 没开这个口子；**当前版本不成立**。
- "模型决策 + 插件翻译 + Host 执行"是绕开网页版无原生 FC 的**唯一**可行路径，但它治的是"没有 tool_calls"，不是"没有 agent runtime"。
- 落地仍按 §7：P3 先做**单工具、白名单、失败即回退文本**的最小 shim，用真实回归集量出准确率，再决定是否扩大范围；`planner` 继续走官方 API。

### 4.4 省钱目标与任务路由的冲突（本项目最需要想清楚的一节）

目标既然是"省群聊 token"，就必须回答：**哪部分 token 最贵**。

#### 4.4.1 谁在烧 token

| 任务 | 调用频次 | 典型输入 | 是否需要工具 | 省 token 潜力 |
|---|---|---|---|---|
| `planner` | **每轮 1~2 次，最高频** | 系统提示词 + 工具定义 + 历史，**很长** | **强依赖 tool_calls** | **最高** |
| `replyer` | 每回复 1 次 | 长（planner 结论 + 上下文 + 参考消息） | 否 | 高 |
| `utils` | 每次分析都跑（表情/学习/取名/关系/情绪） | 中~长 | 否 | 高 |
| `mid_memory` | 上下文裁切时 | 很长（整段历史） | 否 | 中 |
| `fast_model` | 断句/挑表达，频次高 | 短 | 否 | 中（但延迟敏感） |
| `emoji`/`vlm`/`embedding` | 低频或不可用 | — | — | 低 |
| `web_search` | 低频 | 短 | — | 低 |

**关键矛盾**：**最费 token 的任务（`planner`）恰好是最需要工具调用的任务**，而网页版恰恰没有工具调用。

#### 4.4.2 粗略成本模型（自己代入单价即可）

```
单轮成本 ≈ Σ(每次调用输入 token × price_in + 输出 token × price_out)
一次典型对话 ≈ 3~6 次调用
假设每次输入 4k、输出 500 token，price_in=1 元/M、price_out=2 元/M
→ 单轮 ≈ 5 × (4k×1 + 0.5k×2)/1e6 ≈ 0.025 元
→ 每天 500 轮 ≈ 12.5 元/天 ≈ 375 元/月
```

> 数量级足以支撑"值得省"的判断。但注意：**这是假设值**，先用你自己的模型单价与真实轮次代入，再决定投入多少工程量。

#### 4.4.3 路由策略（一期默认值，写进配置）

| 任务 | 一期路由 | 二期（shim 达标后） | 理由 |
|---|---|---|---|
| `replyer` | 🟢 网页版 | 网页版 | 纯文本、省 token 明显；**单点接入可行性见 §4.5** |
| `utils` | 🟢 网页版 | 网页版 | 纯文本、调用频繁 |
| `mid_memory` | 🟢 网页版 | 网页版 | 输入长、纯文本，最划算 |
| `fast_model` | 🟡 官方（可选网页） | 官方 | 延迟敏感，网页版首 token 太慢 |
| `planner` | 🔴 官方 API | 视 shim 准确率决定 | 强依赖工具调用 |
| `emoji` | 🟡 官方 | 官方 | 需要严格候选约束 |
| `vlm` / `embedding` / `voice` | 🔴 官方 | 官方 | 网页版不具备 |

**这个路由能省下大约"除 planner 与多模态外"的全部 token**。按 §4.4.2 的假设，若 planner 占 40%，则约省 60%——**这已经是相当可观的收益，且不需要冒 `planner` 瘫痪的风险**。

> 换句话说：**先拿到 60% 的确定收益，再去赌 P3 那 95% 准确率**，比一上来就全量替换理性得多。

### 4.5 只接 `replyer` 一个任务是否可行？（已核实：可行，且是最低风险起点）

> 结论：**可行。`replyer` 是所有任务里最适合单点接入网页版的一个**，可以作为 P1 的交付边界。

#### 4.5.1 为什么 replyer 特别合适（源码依据）

| 事实 | 证据 | 含义 |
|---|---|---|
| 只传 `LLMGenerationOptions(model_name=...)`，**没有 `tool_options`、没有 `response_format`** | `src/chat/replyer/maisaka_generator_base.py:1215` | 纯文本生成，不依赖工具调用能力 |
| `enable_visual_message = model_info.visual` | 同文件 `:799-801` | 模型声明 `visual=false` 时，replyer **不会把图片放进请求**，适配层不必处理图片 |
| 任务 `hard_timeout` 默认 240s | `model_config` | 网页版单次 3~15s，时间余量充足 |
| 只取 `generation_result.response` | 同文件 `:1274` | reasoning 不进入回复正文，思考内容不会污染输出 |

**结论：replyer 不需要工具调用、不需要结构化输出、不带图——正好避开网页版的全部短板。**

#### 4.5.2 好消息：`model_list` 自带主备切换，不用插件兜底

`src/llm_models/utils_model.py:1346`：

```python
max_attempts = 1 if str(model_name or "").strip() else len(self.model_for_task.model_list)
```

失败时（`:1442`）打日志「切换到下一个模型」，循环自动换下一个候选。所以只接 replyer 时，用户侧这样配就自带兜底：

```toml
[model_task_config.replyer]
model_list = ["deepehter-web", "deepseek-official"]   # 主用网页版，失败自动走官方
selection_strategy = "sequential"                     # 必须：默认 random 会随机挑模型
max_tokens = 8192
hard_timeout = 240.0
```

> ⚠️ **`selection_strategy` 必须显式设成 `sequential`。** MaiBot 默认值是 `random`，
> 那样 `model_list` 每轮会随机挑一个模型，约一半调用会静默走官方 API：
> 用户以为在省 token，实际账目和预期都不对，而且日志里没有降级痕迹（R12）。
> 这也是 P2 演练实测发现的坑（见 `docs/P2-ACCEPTANCE.md`）。

→ **§6.7 的插件内降级在这个场景下是第二层保险，可以放到 P2 再做。**

#### 4.5.3 五个必须处理的细节

| # | 细节 | 后果 | 对策 |
|---|---|---|---|
| 1 | **空响应不会自动切换**：`raise_when_empty` 是空实现（`utils_model.py:442` 直接 `del`） | 返回空文本时不会换模型，只会发出一句空回复 | 失败必须返回 `success:false`，让 `PluginLLMClient` 抛 `RespParseException`，才会触发 model_list 切换 |
| 2 | **"只有思考没有正文"的续写特判不覆盖插件 Provider**：`maisaka_generator_base.py:1226` 只对 `openai_responses` 生效 | 我们拿到 reasoning-only 响应时不会被补救 | 插件内部自己保证 content 非空；拿不到正文就重试或判失败 |
| 3 | **延迟直接落在用户等待路径上** | planner 慢无感，replyer 慢就是"麦麦不回话"；预期 5~20s | 提示词收紧（少思考）、并发闸门、超时即降级 |
| 4 | **不要给该模型设 `force_stream_mode = true`** | 插件 Provider 收到 Host 侧流式处理器会直接抛错（`plugin_client.py:73-74`） | replyer 本身不传；配置项里也不要开 |
| 5 | **回复后处理会 strip 与断句** | 网页版若输出 markdown 列表/加粗，观感可能怪 | 提示词明确要求"只输出一段口语化纯文本" |

#### 4.5.4 收益定位

`replyer` 每轮只调 **1 次**，但它的输入是**单次最长**的（人设 + 历史 + 表达习惯 + planner 结论 + 目标消息）。
估计约占一轮 token 的 **30~40%**。

- **只接 replyer** = 最小风险起步，收益约 30~40%；
- **加上 `utils`** = 边际收益更大（调用频次高、纯文本、同样不吃工具）；
- 因此推荐节奏：**replyer 跑通并观察一周 → 再扩 `utils` / `mid_memory`**。

#### 4.5.5 观察指标（跑一周看什么）

| 指标 | 期望 | 不达标说明 |
|---|---|---|
| replyer 降级次数 | 低且稳定 | 网页版可用性问题（凭证/风控/协议） |
| 平均耗时 | < 15s | 需要收紧提示词或提高并发 |
| 空回复次数 | 0 | 细节 1/2 没处理对 |
| 用户感知 | 回复正常、文风更好 | 延迟或 markdown 残留问题 |

---

## 5. 方案对比与推荐

| | A. 纯插件 Provider 内嵌反代 | B. 混合 Provider（网页 + 官方兜底） | C. 独立 sidecar + 薄 Provider 壳 |
|---|---|---|---|
| 形态 | 插件内部完成全部上游调用 | 插件内两条通道，按模型/配置路由 | 单独起一个 OpenAI 兼容 HTTP 服务，插件只做转发 |
| 部署复杂度 | **最低**（只装插件） | 低 | 高（多一个进程要守护） |
| 复用性 | 只服务 MaiBot | 中 | **高**（任何 OpenAI 客户端可用） |
| 故障隔离 | 插件崩 = 模型不可用 | 中 | **好** |
| 调试便利 | 一般 | 一般 | **好**（可 curl） |
| 发布形态 | 一个插件目录 | 一个插件目录 | 插件 + 外部服务 |
| 与"不改主程序"约束 | ✅ | ✅ | ✅ |

**推荐：直接做 B，不做 A，不采用 C。**

理由：

1. **A 是错的起点**：既然要求"长期稳定"，官方兜底通道就不是"二期优化"，而是一期就要有的安全网（§6.6）。先做纯 A 再补 B，等于把 P2 的工作提前返工。
2. **C 不必要**：只是为了"别的工具也能用"才需要 C；当前动机是省 MaiBot 的 token，没有这个诉求。C 还多一个要守护的进程，与"个人使用、长期稳定"相悖。
3. **B 的增量成本很低**：官方通道就是一次标准 OpenAI 兼容调用，代码量远小于反代本身；把它当"降级路径"实现，比当"功能"实现更简单。
4. 反代逻辑（PoW / SSE / prompt 渲染）仍要写成**可独立测试的纯函数模块**，将来真需要 C 时几乎零成本。

> 一句话：**A 是"能跑"，B 是"能长期跑"，你要的是后者。**

### 5.1 决策已定：不再是"做不做"，而是"怎么不白做"

| 已确认 | 结论 |
|---|---|
| 默认通道 | **网页优先**，官方 API 兜底（不是主路） |
| 动机 | 省群聊 token（§4.4 已量化：优先迁 `replyer`/`utils`/`mid_memory`，约省 60%） |
| 范围 | 个人使用，可砍多租户/权限/审计 |
| 硬要求 | **长期稳定** → 凭证自动化与自愈是一期必需项，不是优化项 |

**三条"不白做"的底线**（做不到就等于白写）：

1. **降级必须可见**：静默降级会让用户在不知情的情况下一直在烧官方 token，比不做更糟（§6.6）。
2. **凭证必须能自愈**：否则"自动获取"只是一次性便利，重启/过期后项目就废了（§6.5）。
3. **不能赌 `planner`**：先把 60% 的确定收益拿到手，再谈工具调用 shim（§4.4.3）。

---


## 6. 推荐架构（若决定开工）

### 6.1 插件目录结构

```
plugins/deepehter-proxy/
├── _manifest.json              # 必须：元信息 + llm_providers 双声明之一
├── plugin.py                   # 必须：入口，create_plugin() + 三个生命周期方法
├── i18n/                       # 可选：zh-CN.json
│   └── zh-CN.json
├── deepehter/                  # 插件内部实现（可独立单测）
│   ├── config.py               # PluginConfigBase 配置模型
│   ├── provider.py             # @LLMProvider 处理函数 + operation 分发
│   ├── adapter_in.py           # context_items -> prompt 渲染
│   ├── adapter_out.py          # 上游结果 -> output_items / 旧标量协议
│   ├── tool_emu.py             # （二期）提示词级工具调用模拟与解析
│   ├── upstream/
│   │   ├── web_client.py       # session / pow / completion / sse
│   │   ├── pow.py              # deepseek-pow 封装 + 失败重试
│   │   └── openai_fallback.py  # （二期）官方 API 直通
│   └── errors.py               # 统一中文错误消息
└── tests/
    ├── fixtures/               # 从真实 MaiBot dump 的 request 快照
    └── test_*.py
```

### 6.2 数据流

```
MaiBot Host
   │  invoke_llm_provider(operation="response", request)
   ▼
@LLMProvider 处理函数             ← 唯一入口，必须 try/except 兜住一切
   │
   ├─ 1. 读取配置（web_token / mode / 并发闸门）
   ├─ 2. adapter_in: context_items + tool_options → 单一 prompt 文本
   ├─ 3. upstream.web_client:
   │      create_session → pow → completion(SSE) → 累积 thinking/content
   ├─ 4. adapter_out: 结果 → {"output_items": …} 或旧标量协议
   └─ 5. usage / 错误包装（中文、可读、可重试判定）
   ▼
返回 dict 给 Host
```

### 6.3 client_type 与 manifest 关键字段

client_type 必须带归属前缀，避免与其他插件冲突（冲突时**双方都不加载**）：

```json
{
  "manifest_version": 2,
  "id": "com.ether.deepehter-proxy",
  "version": "0.1.0",
  "name": "DeepEther Proxy",
  "description": "把 DeepSeek 网页版接入 MaiBot 的 LLM Provider",
  "author": { "name": "…", "url": "https://…" },
  "license": "MIT",
  "urls": { "repository": "https://…" },
  "host_application": { "min_version": "1.3.0", "max_version": "999.999.999" },
  "sdk": { "min_version": "2.12.0", "max_version": "999.999.999" },
  "plugin_type": "provider",
  "capabilities": [],
  "llm_providers": [
    {
      "client_type": "com.ether.deepehter",
      "name": "DeepEther Proxy",
      "description": "DeepSeek 网页版反代 Provider",
      "version": "0.1.0"
    }
  ],
  "i18n": { "default_locale": "zh-CN" }
}
```

- `capabilities` 要**最小化**：纯被动 Provider 用空数组即可（读自己的配置走 `self.config`，不需要 Host 能力）。
  只有真正调用 `ctx.*` 代理时才逐项声明。本项目因为要做**降级到 Host 模型**（§6.7），必须声明
  `"capabilities": ["llm.generate"]`（若兜底也要带工具，再加 `llm.generate_with_tools`）。其余如 `database.query`、`send.text` 用到再加。
  完整清单见 `$MAI/src/plugin_runtime/capabilities/registry.py`（约 80 个能力名）。`ctx.logger` / `ctx.paths` 是 Runner 注入的辅助对象，不需要声明。
- 依赖 `wasmtime` 时用 `dependencies` 声明 `{"type":"python_package","name":"wasmtime","version_spec":">=20"}`。
- `max_version` 别锁死小版本（官方建议 999.999.999 + 只认真约束 `min_version`）。

用户侧的 `model_config.toml` 配置示例：

```toml
[[api_providers]]
name = "deepehter"
base_url = "plugin://com.ether.deepehter"   # 占位，插件不真正访问
api_key = ""                                # auth_type = "none"
client_type = "com.ether.deepehter"
auth_type = "none"
timeout = 300                               # 关键：深度思考常超 120s 默认值

[[models]]
model_identifier = "deepseek-web"
name = "deepehter-web"
api_provider = "deepehter"
price_in = 0.0
price_out = 0.0
send_temperature = false                    # 网页版不吃 temperature
extra_params = { }                          # 思考/联网开关走插件自带配置
```

### 6.4 插件配置项（`config_model`）建议

| 组 | 字段 | 说明 |
|---|---|---|
| 凭证 | `web_token` | 网页版 Bearer token。**可留空**——留空时用 §6.5 的自动获取通道 |
| 凭证 | `credential_port` | 本地接收凭证的回环端口，默认 18765（仅绑 127.0.0.1） |
| 凭证 | `health_check_interval` | 探活间隔（分钟），默认 30 |
| 行为 | `mode` | `web_first`（默认）/ `web_only` / `official_only` |
| 行为 | `thinking_enabled` | 是否开思考模式 |
| 行为 | `search_enabled` | 是否开网页版内置搜索 |
| 并发 | `max_concurrency` | 默认 2，上限 3（风控） |
| 超时 | `request_timeout` | 单请求超时，建议 180 |
| 熔断 | `circuit_fail_threshold` / `circuit_cooldown_sec` | 连续失败 K 次后冷却 M 秒 |
| 兜底 | `fallback_enabled` | 是否允许自动降级（默认 true） |
| 兜底 | `fallback_model_name` | **直选 Host 模型名**（MaiBot `model_config.toml` 里已配置的官方模型）；留空则禁用兜底 |
| 兜底 | `fallback_timeout_ms` | 兜底调用超时，默认 120000 |
| 兜底 | `fallback_with_tools` | 兜底时是否携带 `tool_options`（`planner` 兜底需要，默认 true） |
| 兜底 | `fallback_for_tasks` | 允许降级的任务白名单；**默认不含 `planner`**（避免静默改变行为） |

> **兜底不配置 key、不配置 base_url**：插件通过 `ctx.llm.generate(...)` 回调 Host，复用 MaiBot 已经配好的 `api_providers`（key、base_url、超时、重试、计费统计全部复用）。详见 §6.7。
| 调试 | `dump_request` | 落盘真实 request 到 `runtime_dir`，用于契约测试 |
| 调试 | `verbose_log` | 打印上游链路耗时 |

---

### 6.5 凭证获取：从"手动粘贴"到"打开网页登录自动获取"

要拿的东西只有一个：浏览器里 `localStorage.userToken` 的 `.value`（§3.2）。难点不在"拿"，而在**怎么用最小的依赖、最少的手动步骤、长期反复地拿到**。

#### 6.5.1 方案对比

| 方案 | 用户体验 | 依赖 | 难度 | 长期稳定 | 评价 |
|---|---|---|---|---|---|
| ① 手动粘贴到配置 | 每次过期都要粘一次 | 无 | 极低 | 高 | **永远保留的保底通道** |
| ② 书签小工具 / 油猴脚本 → POST 给插件本地端口 | 打开网页点一下书签 | 浏览器 | 低 | 高 | ✅ **推荐主方案** |
| ③ Playwright 拉起独立浏览器，用户在里面登录，脚本读 localStorage | 打开一个自动化浏览器，登录完自动回填 | `playwright` + Chromium（~150 MB） | 中 | 中 | 可选"傻瓜模式" |
| ④ CDP 连接用户已开的 Chrome（`--remote-debugging-port`） | 需要用特殊参数启动浏览器 | 无额外包 | 中 | 中 | 进阶用户 |
| ⑤ 直接解析浏览器 profile 里的 leveldb | 全自动、零操作 | 无 | 高（文件锁、新版加密） | **低** | ❌ 不推荐，脆且不安全 |

**推荐组合：②为主 + ①保底 + ③作为可选进阶。**
理由：②几乎没有依赖、不碰用户浏览器数据、失败时随时退回①；⑤看起来最"自动"，但它要读浏览器私有存储、受文件锁与新版本加密影响，恰恰是最不稳定的——与"长期稳定"的要求相反。

#### 6.5.2 方案②的实现形态

插件在回环地址起一个极小 HTTP 服务（不放公网）：

```
POST http://127.0.0.1:18765/credential/claim
Body: {"nonce": "<插件生成>", "token": "<localStorage.userToken.value>"}
```

书签小工具（用户拖到书签栏，在已登录的 chat.deepseek.com 页面上点一下）：

```
javascript:fetch('http://127.0.0.1:18765/credential/claim',{method:'POST',
headers:{'content-type':'application/json'},
body:JSON.stringify({nonce:localStorage.getItem('__deepehter_nonce'),
token:JSON.parse(localStorage.getItem('userToken')).value})})
.then(()=>alert('DeepEtherProxy：凭证已送达'))
```

nonce 由插件生成并显示在 WebUI 状态页（或 `/deepehter-status` 命令输出里），用户贴进书签一次即可复用。

#### 6.5.3 必须先实测的三个坑

| 坑 | 说明 | 应对 |
|---|---|---|
| **HTTPS → 回环 HTTP 的混合内容** | 从 `https://chat.deepseek.com` 请求 `http://127.0.0.1` 可能被拦。主流浏览器把 loopback 视为可信源，但**必须实测** | 若被拦，改用 `window.open` 跳转到本地页面并把 token 放在 URL fragment（`#` 不进服务端日志） |
| **CORS** | 本地服务必须放行 `Origin: https://chat.deepseek.com` | 只放行该来源 + 强制 nonce 校验，不要 `*` |
| **DeepSeek 页面 CSP** | CSP 可能禁止 connect-src 到回环 | 与上一条同：改用顶层跳转模式 |

> 这三条都可以用 30 分钟的验证实验一次问清楚（见 §10）。**在没验证前不要把它写进"已完成"**。

#### 6.5.4 凭证失效与续期

- **探活**：每 `health_check_interval` 分钟（默认 30）打一次轻量接口（如 `/api/v0/users/current`）判断 token 是否仍有效。
- **失效处理**：进入"待授权"状态，通过 `/deepehter-status` 与日志用中文明确提示"请重新打开网页点一次书签"，并**自动降级到官方 API**（若开启）。
- **自动续期**：网页版是否存在 refresh 机制**尚未核实**。设计上先做"失效快速告知 + 一键重新授权"，把续期当作加分项而不是前提。
- **落盘**：保存到 `data/plugins/<plugin_id>/credential.json`（插件专属目录），日志中**永不打印完整 token**（只打印前 4 位 + 长度）。

#### 6.5.5 为什么不做"全自动登录"

用账号密码模拟登录是可行的，但它需要携带更严格的风控参数、失败会直接触发风控，且**意味着插件要保管用户密码**。对一个"个人使用、要求长期稳定"的插件，这是明显的负收益。**打开网页点一下**是可接受的成本。

### 6.6 长期稳定性设计（比反代本身更重要）

要求"长期稳定"，就必须假设**上游一定会坏**：token 会过期、协议会改、风控会拦、账号可能被封。稳定性设计的目标不是"不出故障"，而是**出故障时自动降级、且用户能看见**。

#### 6.6.1 五层防线

| 层 | 机制 | 触发条件 | 行为 |
|---|---|---|---|
| **凭证层** | 定时探活 + 失效自愈 | token 401/403 | 标记待授权 → 中文告警 → 可一键重新获取 |
| **协议层** | 启动自检 + 漂移检测 | 建会话/PoW/首增量任一失败 | 记录失败指纹；连续 N 次同类失败 → 判定"协议可能变更"，给出明确日志 |
| **流量层** | 并发信号量 + 排队超时预估 + 熔断 | 并发达上限 / 连续失败 K 次 | 拒绝新请求走降级；冷却 M 秒后重试 |
| **降级层** | 自动切 Host 已配置的官方模型（§6.7） | 上述任一层失败 | 按 `fallback_for_tasks` 白名单降级，**并记录降级计数** |
| **观测层** | `/deepehter-status` + 结构化日志 | 随时 | 一眼看出：凭证状态、最近失败、降级次数、平均耗时 |

#### 6.6.2 "降级必须可见"（最容易做错的一条）

静默降级的后果：用户以为在省 token，实际上一直在烧官方额度，而且**行为特性也变了**（工具调用能力突然回来）。因此：

- 每次降级都打一条**中文 warning**，包含原因（凭证失效 / 超时 / 熔断）；
- `/deepehter-status` 显示"最近 24 小时降级 N 次"；
- 可选：降级比例超过阈值时，通过 `ctx.send` 主动提醒一次（每会话限频）。

#### 6.6.3 自检清单（启动时 + 每小时）

```
① 凭证存在且未过期          → 否则标记"待授权"
② 能建会话                  → 否则"协议异常/网络异常"
③ 能领到并解开 PoW          → 否则"PoW 算法可能升级"
④ 能收到首个增量            → 否则"SSE 解析可能变更"
⑤ 兜底通道可用（如开启）    → 否则"降级不可用"，提高告警级别
```

任一失败都不应该让插件崩溃——插件抛异常只会转成 `success:false`（§2.5 C5），用户看到的是"麦麦不回话了"，非常难排查。

#### 6.6.4 协议漂移的应对成本

不要幻想"一次写好永不失效"。可维护性上真正重要的是：

- 把所有上游假设**集中到 `upstream/` 一个目录**（§6.1），改协议只改一处；
- 每个上游常量（路径、字段名）集中成命名常量，不要散落；
- 保留 `dump_request`/`dump_response` 开关，出问题能立刻拿到原始报文；
- 在 README 里写一份"上游改版排查 5 步"，让未来的自己 10 分钟内能定位。

---

### 6.7 降级怎么走：复用 MaiBot 自己的模型配置（已确认为可行方案）

> 你的问题是："官方兜底能否回退到 MaiBot 自身设置的模型设置？"
> **答案：能，而且比插件自己配 key 更好。** 下面是核实结果。

#### 6.7.1 机制

插件通过 `ctx.llm` 能力代理回调 Host，Host 用**它自己已配置的 `api_providers`** 发起请求：

```python
result = await self.ctx.llm.generate(
    prompt=messages,                    # str 或 [{"role": ..., "content": ...}]
    model_name="deepseek-v4-flash",     # 直选 Host 里已配置的具体模型
    max_tokens=max_tokens,
    timeout_ms=120000,                  # 可选，透传到 RPC
)
# result: {success, response, reasoning, model_name, tool_calls?, prompt_tokens, ...}
```

`tool_options` 场景用 `ctx.llm.generate_with_tools(...)`，返回值里带 `tool_calls`。

#### 6.7.2 源码依据（为什么可行）

| 关注点 | 结论 | 证据 |
|---|---|---|
| 能否直选 Host 模型 | 能。`model_name` 一律按具体模型名解析 | `capabilities/core.py:15-51` |
| 能否拿到 tool_calls | 能，返回体包含 `tool_calls` 列表 | `llm_service_data_models.py:229-240` |
| 嵌套调用会不会死锁 | **不会**。Host 与 Runner 都把入站请求丢进 `create_task`，不阻塞接收循环 | `host/rpc_server.py:389`、`runner/rpc_client.py:269` |
| 超时能否自控 | 能。`**kwargs` 会被合并进 payload，`timeout_ms` 直达 RPC | `maibot_sdk/capabilities/llm.py:73-89` |
| 需要声明能力 | 需要：`llm.generate`（工具兜底再加 `llm.generate_with_tools`） | `capabilities/registry.py:36-40` |

#### 6.7.3 为什么这比"插件自己配 key"好

1. **不用存第二份密钥** —— 密钥只在 MaiBot 配置里，插件不碰；
2. 直接复用 MaiBot 的 base_url、重试、超时、代理设置；
3. 兜底调用的 token 会**自动计入 MaiBot 统计与计费**，账目统一；
4. 用户在 WebUI 里换兜底模型即可，不用改插件配置；
5. 兜底也能带工具调用 → **`planner` 兜底不丢能力**。

#### 6.7.4 三个必须处理的坑

| 坑 | 说明 | 对策 |
|---|---|---|
| **递归** | 若兜底用 `task_name`（如 `planner`），而该任务又指向插件自己，就会无限回调 | 兜底**只允许直选 `model_name`**（校验它不等于本插件的任何模型名）；再加 `contextvars` 深度守卫，深度 ≥1 直接拒绝兜底并报错 |
| **超时叠加** | 外层 provider RPC 超时 = `api_provider.timeout`，内层兜底又有自己的超时，两层叠加可能超过 MaiBot 的 `hard_timeout` | 兜底超时必须小于"外层剩余预算"，建议 `min(fallback_timeout_ms, 外层超时 - 已耗时)` |
| **prompt 形态** | `ctx.llm` 吃的是 str / messages，不是 `context_items` | 复用 `adapter_in` 的投影结果，不要再写一套 |

#### 6.7.5 最终结论

> **按"复用 Host 模型"实现，插件里不出现任何 API key。**
> 你提到的"要用 key 再说"——按这个方案，**不需要**。只有在"兜底模型也不可用、要临时排查"时，才可能需要你提供 key 做对比测试。

---

### 6.8 测试环境：在工作区内自建实例（不碰你的日用实例）

#### 6.8.1 已完成的部署（2026-10-20 实测通过）

| 项 | 值 |
|---|---|
| 位置 | `E:\Dev\EtherTool\DeepEtherProxy\.testenv\maibot` |
| 来源 | 从 `E:\Dev\NapCat\NapCatData\apps\maibot\210720e0` **只读复制**（未触碰你的日用实例 `73fc1ca9`） |
| 版本 | MaiBot 1.3.11 / SDK 2.12.0 / Python 3.12.14 |
| 占用 | 源码 32.9 MB + venv 609 MB = 642 MB |
| WebUI | http://127.0.0.1:**41082** |
| maim_message | **41083** |
| 适配器 | **已停用**（NapCat 适配器移到 `.testenv/disabled-plugins/`，避免与你正在跑的实例抢同一个 QQ 连接） |
| 状态 | 已验证可稳定运行：30 秒后进程存活、WebUI 端口监听、插件 `on_load` 正常执行 |

#### 6.8.2 启动方式（有一个必须知道的坑）

```powershell
$py = "E:\Dev\EtherTool\DeepEtherProxy\.testenv\maibot\.venv\Scripts\python.exe"
$dst = "E:\Dev\EtherTool\DeepEtherProxy\.testenv\maibot"
Start-Process -FilePath $py -ArgumentList "bot.py" -WorkingDirectory $dst
```

> ⚠️ **不要用 `-NoNewWindow` 配合 stdout/stderr 重定向来启动**。
> MaiBot 启动后会读终端 stdin（"`已启用终端输入`"）；一旦 stdin 是 EOF，它会**优雅关闭自身**（日志表现为启动 1 秒后 `host_stop`）。
> 我第一轮就踩了这个坑。要有真实控制台，日志从 `.testenv/maibot/logs/app_*.log.jsonl` 读。
>
> 停止：`Get-Process python | Where-Object { $_.Path -like "*DeepEtherProxy*.testenv*" } | Stop-Process -Force`（或按 PID 精确停）。

#### 6.8.3 与日用实例的隔离清单

| 隔离项 | 处理 |
|---|---|
| 端口 | WebUI 41082 / ws 41083（日用实例是 40082/40083） |
| 数据 | `data/`、`logs/`、`temp/` 都在 `.testenv/maibot` 内，独立数据库 |
| QQ 连接 | NapCat 适配器已停用 —— **测试期不会登录你的 QQ** |
| 配置 | 独立 `config/bot_config.toml`；`model_config.toml` 首次启动自动生成 |
| 插件 | 把被测插件放 `.testenv/maibot/plugins/deepehter-proxy`（用 Junction 指向工作区，改代码即时生效） |

#### 6.8.4 端到端测试还缺的一环：怎么"发消息"

没有适配器就没人给麦麦发消息。三个可选路径，按实现成本排序：

1. **WebUI 的调试能力**（优先尝试）：先确认 WebUI 是否提供注入消息/发起会话的接口（`docs/develop/webui-api`）；
2. **本地直连 maim_message**：向 41083 发一条平台消息，模拟适配器；
3. **写个最小本地适配器**：仅在测试实例里用，不进插件仓库。

> 这一环需要在写代码那次会话开头先确认。**如果三条都不通，退而求其次：P1 阶段先用"直接调用 provider 的契约测试"验证（§9.2），端到端联调等适配器方案定了再做。**

#### 6.8.5 首次启动产生的凭证

WebUI 首次启动会在日志里打印登录 Token（`[WebUI应用] 🔑 WebUI 登录 Token: ...`）。它在 `.testenv/maibot/logs/` 里，用于登录 41082。

---

## 7. 实施路线

> 排序原则：**按"能不能日用"排序，而不是按"功能是否完整"排序。**
> 凭证自动获取与自动降级必须早于工具调用 shim —— 前者决定项目能不能长期跑，后者只是锦上添花。

| 阶段 | 目标 | 交付物 | 估时 |
|---|---|---|---|
| **P0 骨架** | 插件能被 MaiBot 加载、能配置、能卸载；注册 client_type 并在 WebUI 里选到 | `_manifest.json`、`plugin.py`、配置模型、加载日志 | 0.5 天 |
| **P0.5 凭证** | 打开网页点一下即自动就位；定时探活；失效时中文提示 + 一键重授权（§6.5） | 回环凭证服务、书签小工具、`/deepehter-status` 雏形 | 1–2 天 |
| **P1 打通** | **先只接 `replyer`** 跑通（§4.5）：`context_items` → prompt → 网页版 → 纯文本；用 `model_list` 主备兜底。跑稳后再扩 `utils` / `mid_memory` | adapter_in/out、web_client、pow 封装 | 2–3 天 |
| **P2 稳定** | 并发闸门、排队预估、熔断、**自动降级官方 API**、启动/定时自检、降级可见（§6.6） | openai_fallback、circuit breaker、status 命令 | 2–3 天 |
| **P2.5 反封禁** | 让网页版通道"看起来像人在用"：串行 + 抖动间隔 + 三级配额 + 风控信号分类 + 会话复用 + 指纹自洽 + 节奏状态落盘（§7.2、`docs/ANTIBAN.md`） | `antiban.py`（PacingController / classify_failure）、`[antiban]` 配置节、状态命令新增段落 | 1–2 天（**上线前必做**） |
| **P2.6 自动注册** | 插件加载时自动把 Provider 与三个预设模型写进 Host 的 `config/model_config.toml`，并把 `replyer`/`utils` 接到 sequential 主备列表首位；Host 文件监视热重载、无需重启（§7.3） | `deepether/provision.py`、`[provision]` 配置节、`/deepether-setup` 命令、17 条单测 | 0.5 天 |
| **P3 工具 shim** | 提示词级 tool_calls 模拟：`tool_options` 注入 → 解析 JSON → `FunctionCallItem` 交 Host 执行（边界见 §4.3） | tool_emu + 准确率回归集 | 3–5 天（不确定性最高） |
| **P4 收尾** | i18n、README、"上游改版排查 5 步"、发布准备 | 文档 + 测试报告 | 1–2 天 |

### 7.1 "可以日用"的判定门槛（P0.5 + P2 完成即达成）

| 判定项 | 标准 |
|---|---|
| 凭证 | 连续 7 天无需手动干预；过期后 30 分钟内能发现并提示 |
| 可用性 | 网页版不可用时，麦麦**照常回复**（走官方兜底），用户只看到一条 warning |
| 可观测 | 一条命令看出：凭证状态、最近失败原因、24h 降级次数、平均耗时 |
| 不静默 | 任何降级都有中文日志，且计数可见 |
| 不雪崩 | 群聊突发 20 条消息时，不会全部超时——超出的直接走兜底 |
| replyer 专项 | 空回复 0 次；平均耗时 < 15s；`model_list` 主备切换在日志中可见（§4.5.5） |

**这五项达标，项目就算成功；P3 只是把省的钱从 60% 提到接近 100%。**

### 7.3 P2.6 自动注册：把"配置模型"这一步也去掉

原来的安装流程要求用户自己往 `model_config.toml` 加服务商、模型、任务接线，这是最容易卡住的一步
（WebUI 的"新增供应商"只认已注册的 client_type，插件没加载时根本选不到）。
P2.6 让插件自己写：

| 动作 | 细节 |
|---|---|
| 定位配置 | 显式 `model_config_path` → `DEEPETHER_MODEL_CONFIG` → `MAIBOT_INSTANCE_DIR` → 插件目录向上找实例根 |
| 写 Provider | `name=deepether`、`base_url=plugin://com.ether.deepether`、`client_type=com.ether.deepether`、`auth_type=none`、`api_key=""`、`timeout=300` |
| 写模型 | `deepether-replyer`（replyer 风格、不思考）、`deepether-planner`（开思考）、`deepether-utils`（不思考） |
| 接线任务 | `replyer`/`utils` 的 `model_list` 首位插入对应模型，`selection_strategy` 固定 `sequential`；`planner` 默认不接 |
| 安全 | 先备份（最多 5 份）→ tomlkit 修改 → tomllib 复核 → `.tmp` + `os.replace` 原子替换；无变化不落盘（避免反复热重载） |
| 生效 | Host 的 FileWatcher 监听 `model_config.toml`（`src\config\config.py`），写完自动 `reload_config(scope=model)` |

实测（verify 实例 41094）：装 0.2.4 → 重启一次 → 日志出现"自动注册已写入…新增模型…接线任务…"，
`GET /api/webui/config/model` 立即返回三个新模型与 sequential 接线；再次重启日志变为"无需改动"（幂等）。
已知宿主小问题：WebUI 卸载插件时若 `__pycache__` 非空会报 `WinError 145`，需停实例后手动删目录。

### 7.2 P2.5 反封禁：为什么它必须排在 P3 之前

P0~P2 解决"能不能跑"，但跑起来之后最先杀死这套方案的不是 bug，而是**上游封号**——本项目实测已因行为特征被封 3 天。
封禁不来自单次请求内容，而来自节奏、会话模式、失败率、TLS/HTTP 指纹这几层（分层模型见 `docs/ANTIBAN.md` §1）。

P2.5 的交付物（0.2.3 已全部落地）：

| 措施 | 实现 | 默认值 |
|---|---|---|
| 同 token 串行 + 抖动间隔 | `antiban.PacingController.acquire` | 串行、8~20s、抖动 0.25 |
| 分/时/日三级配额 | 同上（滑动窗口） | 3/min、30/h、200/day |
| 风控信号分类（技术性错误不算风控） | `antiban.classify_failure` → `BanKind` | PoW/凭证/5xx → 不冷却 |
| 长冷却与自适应退避 | 429→15min，疑似封号→6h；连续 3 次升级、成功折半 | 上限 ×8 |
| 会话复用 | `web_client.acquire_session(reuse_limit)` | 8 次 |
| 指纹自洽 + 可选 h2 | `constants.BROWSER_XHR_HEADERS`、`_HTTP2_AVAILABLE` | 不伪造私有头 |
| 状态落盘（重启不加刑） | `runtime_dir/antiban_state.json` + `cooldown_until_wall` | 原子写 |
| 降级不变差 | `acquire` 抛 `PACING_COOLDOWN/QUOTA` → 直接走 Host 模型 | 对话不断线 |

设计取舍与"明确不做的手段"（代理池、随机 UA、伪造 cookie、多实例共用 token、PoW 抢跑）见 `docs/ANTIBAN.md` §5。

> P2 验收结论（2026-10-21）：可用性 / 不静默 / 不雪崩 / 可观测 四项已实测通过；
> 凭证项与 replyer 专项属于「跑一周」类指标，当前数据见 [P2-ACCEPTANCE.md](P2-ACCEPTANCE.md)。

**关键路径风险在 P3**：提示词级工具调用成功率取决于模型配合度。

P3 的验收门槛（可量化，写进回归集，不达标就不扩大接入范围）：

| 指标 | 门槛 | 说明 |
|---|---|---|
| 回归集规模 | ≥ 20 条固定样例 | 覆盖 1~3 个工具、含"应当不调用"的诱导样例、含参数缺失/非法样例 |
| 工具名解析准确率 | **≥ 95%** | 该调时调对、不该调时不调 |
| 参数 schema 合法率 | **≥ 90%** | 按 `tool_options` 的 JSON Schema 校验 |
| 无效调用回退率 | **100%** | 未知工具 / 坏 JSON 一律回退纯文本，且不向上抛异常 |
| 正文协议残留 | **0 处** | 最终回复中不出现 JSON、围栏、"我要调用工具"字样 |

未达标时的行为必须是**安全降级**：不调用工具、返回纯文本、打中文日志说明原因——而不是让 Host 收到一个非法 `tool_calls`。

---

## 8. 风险登记册

| # | 风险 | 概率 | 影响 | 对策 |
|---|---|---|---|---|
| R1 | **ToS 违规 / 账号封禁** | 高 | 高 | 文档显著免责声明；限制并发；只作个人用途；不做商业分发 |
| R2 | PoW 算法升级（V2）导致全挂 | 中 | 高 | 用可插拔 solver 注册表；探活失败时给出明确中文提示；关注上游库更新 |
| R3 | 前端协议漂移（字段/路径改动） | 高 | 高 | 适配层集中在一处；加"协议自检"命令；错误信息带上游原始响应片段 |
| R4 | token 过期 | 高 | 中 | 401 时提示"请重新获取 token"；`/deepehter-status` 命令查看凭证状态（§6.5） |
| R5 | 并发 3 天花板导致排队超时 | 高 | 中 | 插件内信号量 + 快速失败 + 明确日志；预计超时直接走兜底（§4.2） |
| R6 | 深度思考超 120s 默认超时 | 中 | 中 | 文档要求 `timeout=300`；插件内做软超时与中断 |
| R7 | 工具调用模拟准确率低 | 高 | 中 | 一期不接 `planner`；二期加严格 JSON 校验 + 失败不猜测（§4.3） |
| R8 | usage 不准确影响计费统计 | 确定 | 低 | 价格配 0；文档说明 |
| R9 | 上游返回非 UTF-8 / 乱码 | 低 | 低 | 解码容错 |
| R10 | 插件异常导致 MaiBot 主流程卡住 | 低 | 高 | 全部 await 加超时；异常一律转 `success:false` 中文错误 |
| R11 | 群聊突发峰值排队撞 `hard_timeout` | 中 | 中 | 并发闸门 + 排队预估；预计超时直接走兜底（§4.2 / §6.6） |
| R12 | **静默降级导致"以为在省钱其实在烧钱"** | 中 | **高** | 降级必须打日志 + 计数 + 状态命令可见（§6.6.2） |
| R13 | 浏览器安全策略拦截凭证回传（CORS / mixed content / CSP） | 中 | 中 | 备选"顶层跳转 + URL fragment"模式；保底手动粘贴（§6.5.3） |
| R14 | 凭证回环端口被其他程序占用 | 低 | 低 | 端口可配置 + 启动时探测并换端口，日志提示 |
| R15 | 网页版额度/频率限制变化 | 中 | 中 | 探活失败计数 + 熔断；接受"降级到官方"为常态兜底 |
| R16 | **降级递归**（兜底任务又指向本插件） | 中 | 高 | 兜底只允许直选 `model_name` + `contextvars` 深度守卫（§6.7.4） |
| R17 | 兜底超时与外层超时叠加导致整体超时 | 中 | 中 | 兜底超时取"剩余预算"，不做固定值（§6.7.4） |

---

## 9. 测试方案

> 目标：**不依赖 MaiBot 主程序也能测掉 80% 的逻辑**，剩下的用真实实例联调。

### 9.1 L1 单元测试（无网络、无 MaiBot）

用 `pytest`，全部纯函数化：

| 测什么 | 断言点 |
|---|---|
| `adapter_in` | 给定 6 种 `item_type` 的 `context_items`，渲染出的 prompt 含角色分隔、工具调用与工具结果的正确文本化、图片被降级为占位 |
| `adapter_out` | 输出结构含 `item_schema_version=1` 且 `output_items` 合法；空正文/只有思考/只有工具调用三种边界 |
| `sse` 累积器 | 喂"JSON-Patch 增量"样本，校验 thinking 与 content 正确分离、未知 op 被忽略、断流不崩 |
| `tool_emu` 解析 | 模型输出的"伪工具调用"能被解析成结构化 `tool_calls`；非法 JSON → 不猜测、返回纯文本 |
| `tool_emu` 准确率回归 | 跑 §7 的 ≥20 条固定样例集，断言工具名准确率 ≥95%、参数合法率 ≥90%、无效调用 100% 回退纯文本 |
| 配置模型 | 默认值、必填校验、`web_token` 缺失时的中文报错 |

```powershell
cd plugins\deepehter-proxy
python -m pytest tests -q
python -m json.tool _manifest.json > $null   # manifest 语法校验
```

### 9.2 L2 契约测试（golden fixture）

- 打开插件配置 `dump_request = true`，在真实 MaiBot 里跑一轮对话，插件把收到的 `request` 原样写入 `runtime_dir`。
- 把该 JSON 固化成 `tests/fixtures/request_response_basic.json`。
- 单测里直接喂这份 fixture，验证 adapter 全链路，**从此不需要 MaiBot 在跑**。
- 反向也验一次：用 fixture 的 `output_items` 结构，人工构造合法/非法样本，确认与 `CONTEXT_ITEM_SCHEMA_VERSION = 1` 契约一致。

### 9.3 L3 集成测试（真实 MaiBot 1.3.11）

本机已有可用实例：`E:\Dev\NapCat\NapCatData\apps\maibot\73fc1ca9`（含多个第三方插件）。
插件目录：该实例的 `plugins/`。安装方式二选一：

```powershell
# 方式一：复制（每次改代码要重新复制）
Copy-Item -Recurse -Force ".\plugins\deepehter-proxy" "E:\Dev\NapCat\NapCatData\apps\maibot\73fc1ca9\plugins\"

# 方式二：目录联接，改代码即时生效（推荐开发期）
New-Item -ItemType Junction -Path "E:\Dev\NapCat\NapCatData\apps\maibot\73fc1ca9\plugins\deepehter-proxy" -Target ".\plugins\deepehter-proxy"
```

> ⚠️ 这两条命令会写工作区之外的目录（MaiBot 安装目录），需要你明确许可后我再执行。

验收步骤：
1. 启动 MaiBot，日志出现插件加载成功、无 manifest 报错；
2. WebUI 插件页能看到该插件且状态"已加载"；
3. WebUI 模型配置新增 `api_providers`（`client_type = com.ether.deepehter`）与对应 `models`；
4. 把 `model_task_config.replyer.model_list` 指向该模型；
5. 在 QQ 里说一句话，确认回复来自网页版（日志中有上游链路耗时）；
6. 禁用插件 → 请求回退到下一可用模型，不报未捕获异常。

### 9.4 L4 协议探活与回归

插件提供 `/deepehter-status` 命令，依次检查：token 是否有效 → 能否建会话 → PoW 能否求解 → 能否收到首个增量。任一失败给出对应中文提示。这条命令也是用户排查问题的第一入口。

### 9.5 发布前检查清单

- [ ] `_manifest.json` 通过 MaiBot `ManifestValidator`（严格模式，无多余字段）
- [ ] `llm_providers` 与 `@LLMProvider` 的 `client_type` 完全一致
- [ ] 三个生命周期方法 + `create_plugin()` 齐全
- [ ] `capabilities` 最小化且与实际调用一致
- [ ] 无 token / cookie / 日志 / `.venv` / `config.toml` 入库
- [ ] 所有用户可见文本为简体中文
- [ ] 未修改 MaiBot 任何文件（`git status` 或文件对比留证）
- [ ] 凭证文件不在仓库内（`data/plugins/` 与 `config.toml` 已 gitignore）
- [ ] 降级路径经过演练，且日志中可见

### 9.6 凭证与降级专项测试（对应"长期稳定"要求）

| 用例 | 操作 | 期望 |
|---|---|---|
| 自动获取 | 登录网页后点书签 | 3 秒内 `/deepehter-status` 显示"凭证有效" |
| 错误 nonce | 用过期的 nonce 提交 | 拒绝并记录，不落盘 |
| 非回环访问 | 从另一台机器访问凭证端口 | 连接被拒（只绑 127.0.0.1） |
| 凭证过期 | 手工把 token 改坏 | 401 → 标记待授权 → 中文提示 → 按配置降级 |
| **降级演练** | 断网 / 改错 base_url 模拟上游挂掉 | 麦麦照常回复；日志有降级 warning；状态命令计数 +1 |
| 熔断恢复 | 连续失败超过阈值后恢复网络 | 冷却期内不重试；冷却后自动恢复网页版 |
| 端到端寿命 | 连续跑 24 小时（可缩短为 200 轮压测） | 无内存泄漏、无 token 泄漏日志、降级计数合理 |

> 这一节的用例比"功能测试"更接近你要求的"长期稳定"——**建议作为 P2 的验收依据**。
>
> **2026-10-21 已按此表演练**：断网/改坏 token 降级、并发 20 闸门两条主线全绿，
> 演练暴露的 3 个问题（HTTP 200 下的鉴权失败误分类、探活漏判、排队拒绝误开熔断）已修复，
> 详见 **[P2-ACCEPTANCE.md](P2-ACCEPTANCE.md)**。

---

## 10. 决策记录与剩余问题

### 10.1 已确认（2026-10-20）

| 问题 | 答复 | 落点 |
|---|---|---|
| 默认通道 | 优先网页版，工具调用不行再用官方 API | §0.1、§4.4.3、§6.4 `mode = web_first` |
| 动机 | 省群聊对话 token | §1.2、§4.4 |
| 凭证 | 打开网页登录后自动获取 | §6.5 |
| 范围 | 仅个人使用 | §0.1、§8（可砍多租户/权限） |
| 稳定性 | 必须长期稳定 | §6.6、§7.1 日用门槛 |
| 测试实例 | **不碰日用实例**，在工作区自建一个 | §6.8（已部署完成，已验证可运行） |
| 官方兜底 | **复用 MaiBot 自己的模型配置**（不额外配 key） | §6.7（源码核实可行） |

### 10.2 动手前建议先做的 30 分钟验证实验

这一步能一次性排除最大的三个不确定性，**建议由我先做，拿到结果再动工**：

1. 网页版接口当前是否仍如 2026-09 描述（§3.1 链路能否走通）；
2. 请求体能否接受 `tools` 字段 —— 直接决定 P3 形态；
3. 一次完整回答的端到端耗时 —— 决定并发与超时参数；
4. **浏览器能否从 `https://chat.deepseek.com` 向 `http://127.0.0.1` 成功 POST** —— 决定 §6.5 走"fetch"还是"顶层跳转"。

### 10.3 仍需你回答的问题

> ①② 已回答并落到 §6.7 / §6.8。以下只剩余影响**默认参数**、不影响架构的问题。

1. **群聊规模**：常驻几个群、大概多少活跃消息量？决定 `max_concurrency` 与熔断阈值默认值（§4.2）。
2. **多模态**：群里发图频繁吗？`vlm` 任务是否必须可用（这块网页版大概率做不了）。
3. **对外接口**：是否需要顺带提供一个 OpenAI 兼容的本地 HTTP 入口（供 MaiBot 之外的工具用）？需要则偏向方案 C（§5）。
4. **兜底模型**：测试实例里 `model_config.toml` 是自动生成的，还没配官方 provider。写代码前需要你给一个**模型名 + key**，或者由我先把测试实例的 `api_providers` 配成你指定的官方模型（§6.7.4 的兜底验证需要它）。

---

## 11. 参考来源

**MaiBot 官方文档**（已缓存到 `docs/_ref/maibot/`）
- 插件开发指南：https://docs.mai-mai.org/plugin/
- LLMProvider 组件：https://docs.mai-mai.org/plugin/llmprovider
- Manifest：https://docs.mai-mai.org/plugin/manifest
- 生命周期：https://docs.mai-mai.org/plugin/lifecycle
- 配置管理：https://docs.mai-mai.org/plugin/config
- API 参考：https://docs.mai-mai.org/plugin/api-reference
- 插件接入：https://docs.mai-mai.org/develop/plugin
- 模型配置：https://docs.mai-mai.org/manual/configuration/model-config
- 模型额外参数：https://docs.mai-mai.org/manual/configuration/model-extra-params

**本地源码（决定性证据）**
- `$MAI/src/llm_models/model_client/plugin_client.py` — 插件 Provider 宿主侧桥接
- `$MAI/src/llm_models/request_snapshot.py` — 请求/响应序列化契约
- `$MAI/src/llm_models/payload_content/context_item.py` — Context Item schema
- `$MAI/src/llm_models/model_client/base_client.py` — client_type 注册表
- `$MAI/src/plugin_runtime/host/supervisor.py` — Provider 注册流程
- `$MAI/src/plugin_runtime/runner/runner_main.py` — Runner 侧调用与错误规范化
- `$MAI/src/plugin_runtime/capabilities/registry.py` — 插件能力白名单（无 `tool.invoke`）
- `$MAI/src/maisaka/reasoning_engine.py` — 工具执行入口（`_build_tool_invocation`）
- `$MAI/src/web_search/` — MaiBot 自带联网搜索子系统

**DeepSeek 网页版逆向**
- DS-Direct-Proxy 逆向要点（2026-09）：https://juejin.cn/post/7684900473053855790
- CSDN：wasm + worker 求解 `x-ds-pow-response`：https://blog.csdn.net/2302_76420666/article/details/157974736
- `deepseek-pow`（纯 Python PoW 求解）：https://pypi.org/project/deepseek-pow/
- freeseek：https://github.com/vinson0522/freeseek
- deepseek-web2api：https://github.com/fysy84/deepseek-web2api
- DeepSeek 官方 API 文档（对照用）：https://api-docs.deepseek.com/

---

## 附录 A：本次核实的版本基线

| 组件 | 版本 |
|---|---|
| MaiBot | 1.3.11 |
| maibot-plugin-sdk | 2.12.0 |
| Python | >= 3.12 |
| CONTEXT_ITEM_SCHEMA_VERSION | 1 |
| DeepSeek 网页版协议 | 以 2026-09 公开逆向结论为准（易变） |

---

## 附录 B：变更记录

| 版本 | 日期 | 变更 |
|---|---|---|
| v0.1 | 2026-10-19 | 初稿：MaiBot 插件体系核实（基于 1.3.11 源码）、DeepSeek 网页版协议与 PoW 可行性、能力差距、方案对比、实施路线、风险与测试方案 |
| v0.2 | 2026-10-20 | 并入产品决策：① 明确"网页优先 + 官方兜底"，方案结论由"以 A 起步"改为"直接做 B"；② 新增 §1.2 真实动机（省群聊 token）与 §4.4 任务路由冲突分析；③ 修正 §4.2 并发结论（从"劝退点"改为"稳态够用、峰值需排队与降级"）；④ 新增 §6.5 凭证自动获取与 §6.6 长期稳定性设计；⑤ §7 重排里程碑（凭证与稳定性前移），新增 §7.1 日用门槛；⑥ §9 新增 §9.6 凭证与降级专项测试；⑦ §10 改为决策记录 + 剩余问题；⑧ 风险登记册新增 R11–R15 |
| v0.3 | 2026-10-20 | ① 新增 §6.7：降级改为**复用 Host 已配置模型**（源码核实可行，插件不存 key），含递归/超时/prompt 形态三个坑；② 新增 §6.8：在工作区自建测试实例并**实测通过**（含 stdin EOF 导致退出的坑）；③ §6.4 兜底配置改为 `fallback_model_name`；④ §6.6 降级层措辞同步；⑤ §6.3 capabilities 增加 `llm.generate`；⑥ §8 新增 R16 降级递归、R17 超时叠加；⑦ §10 记录"测试实例"与"兜底方案"决策，剩余问题收敛为 4 项 |
| v0.4 | 2026-10-20 | ① 新增 §4.5：**只接 `replyer` 单任务的可行性**（源码核实：无 tools、无 response_format、`visual=false` 不带图、240s 超时），含 `model_list` 自带主备切换、五个必须处理的细节、收益定位与一周观察指标；② §4.4.3 路由表加交叉引用；③ §0 推荐路线与 §7 P1 收窄为"先只接 replyer"；④ §7.1 日用门槛新增 replyer 专项 |
| v0.5（待定） | — | 待验证实验回填：网页版现状、`tools` 字段是否被接受、端到端耗时、浏览器能否向回环地址 POST 凭证；以及测试实例的消息注入路径（§6.8.4） |
