# 各模型任务对「纯文本反代」的适配性

> 状态：**现行设计**　最后更新：2026-10-27　适用版本：0.4.0（结论来自 1.3.11 源码核查）
> 结论先行：这个插件只提供**纯文本生成**——**没有 function calling、没有视觉、没有 embedding、没有逐字流式**。
> 所以 MaiBot 的模型任务里，只有"纯文本进、纯文本出、不要求严格 JSON"的那几个适合接；缺了其中任意一条的，
> 接了也不会报错，而是**在业务层被静默吞掉**（丢记忆、丢评分、不拆句），比报错更难排查。
>
> 结论基于 **MaiBot 1.3.11 源码核查**（2026-10-22）。插件默认自动注册只接 `replyer` + `utils`。
>
> **0.4.0 补充**：本文只针对 **DeepSeek 网页版通道**。同插件的 **CommandCode（Go）通道**支持
> function calling 与视觉：`planner` / `vlm` 可以接 `cc-*` 模型（建议 Kimi K3 / GLM-5.3 / DeepSeek V4 Pro），
> 但**插件不会自动接线**，需要你在 WebUI 里自行选择；JSON 类任务（`memory` / `learner`）仍然不建议。
> 详见 [COMMANDCODE-FEASIBILITY.md](../research/COMMANDCODE-FEASIBILITY.md) 与
> [research/commandcode/P1-RESULTS.md](../research/commandcode/P1-RESULTS.md)。

## 1. 结论速览

| 任务 | 用在哪（源码依据） | 需要工具 | 需要视觉 | 输出格式 | 频率 / 是否等待路径 | 结论 |
|---|---|---|---|---|---|---|
| `planner` | `maisaka/chat_loop_service.py`（主循环决策）；没有 `tool_calls` 就结束本轮 `maisaka/reasoning_engine.py` | **必须**（约 15 个内置工具，`reply` 本身也是工具） | 否 | `tool_calls` | 每条消息多轮 / 关键路径 | **不适用**（硬失败：永远发不出消息） |
| `replyer` | `chat/replyer/maisaka_generator_base.py`（不传 `tool_options`） | 否 | 否 | 纯文本 | 每次回复 / 用户等待 | **推荐**（已默认接线） |
| `utils` | 文本概括/整理等多处（`maisaka/tool_result_condenser.py` 等） | 多数否（仅 WebUI AI 搜索助手例外） | 否 | 多纯文本；仅"回复效果评分"一处要求 JSON 对象 | 后台 + 部分等待 | **推荐**（已默认接线；评分失败只丢评分） |
| `memory` | A_memorix 记忆写回 worker（`services/memory_flow_service.py` → `A_memorix/core/utils/model_routing.py`） | 否 | 否 | **严格 JSON** | 每 N 条消息 / 后台 | 可选用（默认关） |
| `mid_memory` | 聊天回想摘要 `maisaka/memory/mid_term.py` | 否 | 否 | JSON（解析失败丢本次摘要） | 历史裁切时 / 后台 | 可选用（默认关） |
| `learner` | 表达/黑话学习 `learners/expression_learner.py`、`jargon_miner.py` | 否 | 否 | **重 JSON** | 后台批量 | 可选用（默认关） |
| `fast_model` | 回复断句 `chat/utils/llm_sentence_splitter.py`；表达选择 `maisaka_expression_selector.py` | 否 | 否 | 严格 JSON 数组/对象 | **断句在每次回复的等待路径上** | 可选用（慎开：8~20s 抖动拖慢回复） |
| `emoji` | ① 替换决策 `emoji_system/emoji_manager.py`；② 聊天内选择 `maisaka/builtin_tool/send_emoji.py`（可能看拼图） | 否 | ②**是** | ① 正则 ② JSON | ① 罕见 ② 按需 | **不适用**（留空，让 planner/vlm 走视觉逻辑） |
| `web_search` | `web_search/config.py`（`model_list` 存的是联网服务名，不是模型） | — | — | — | — | **不是模型任务，别接** |
| `vlm` / `voice` / `embedding` / `image_embedding` | 看图 / 语音转写 / 向量 | — | `vlm` 必须 | — | — | **不适用** |

## 2. 两个必须讲清的机制

### 2.1 "格式不对"不等于"失败"，也不会切模型

Host 只在**请求级失败**（HTTP 错误 / 网络异常 / 空回复 / 解析异常）时才会换 `model_list` 里的下一个模型；
**"请求成功、但输出格式不符合业务要求"不会切模型**，只会在业务层被吞掉。所以接 JSON 类任务的代价是
**静默丢结果**（丢本次记忆写回、丢本次摘要、丢本次评分），而不是报错，日志里也未必有明确失败。

### 2.2 fast_model 的失败是"降级"而不是"报错"

断句失败 → 降级成"整段不拆句"；表达选择失败 → 降级成"不注入表达"。功能不会崩，但网页版的反封禁节奏
（8~20 秒抖动间隔）会**直接拖慢用户等待**，因为断句处在每次回复的等待路径上。

## 3. 推荐策略

| 档位 | 任务 | 理由 |
|---|---|---|
| **默认接线** | `replyer`、`utils` | 纯文本、不要求工具/视觉；`replyer` 单次等待可接受，`utils` 多在后台 |
| **可选（评估后再开）** | `mid_memory`、`memory`、`learner`、`fast_model` | 纯文本但要求 JSON，或处在等待路径上；开启后这些任务务必保留官方模型兜底 |
| **永不接** | `planner`、`emoji`、`vlm`、`voice`、`embedding`、`image_embedding`、`web_search` | 依赖工具调用 / 视觉 / 向量 / 非模型任务 |

## 4. 怎么开可选任务

1. 在插件配置 `[provision] wire_tasks` 里追加（逗号分隔），例如 `replyer,utils,mid_memory`；
2. 确认该任务 `model_list` 里 `deepether-*` 之后还有官方模型（否则这个模型失败时任务没有其它可选模型）；
3. 自动注册会按任务输出风险提示（JSON 格式 / 等待路径），不阻止接线；
4. `planner` / `emoji` 写进 `wire_tasks` 会被**忽略并告警**，不会接线。

```toml
[provision]
enabled = true
wire_tasks = "replyer,utils"        # 想开可选任务：replyer,utils,mid_memory
```

> **可选任务复用同一个 `deepether-utils` 模型**（plain、不思考），不会为 `memory` / `mid_memory` /
> `fast_model` / `learner` 单独建模型条目——所以「模型清单」永远只有 `deepether-replyer` / `deepether-utils`
> 两个。例如 `wire_tasks = "replyer,utils,mid_memory"` 之后，`mid_memory` 的 `model_list` 首位会出现
> `deepether-utils`。

## 5. 与插件默认行为的关系

- 0.3.1 起自动注册**只创建两个模型**：`deepether-replyer`、`deepether-utils`；`deepether-planner` 不再创建，
  旧版遗留的定义与任务接线会在下次自动注册时清理。
- 自动注册默认**只接线** `replyer`、`utils`；其它纯文本任务要显式写进 `wire_tasks`，且都复用
  `deepether-utils` 这一条模型（没有按任务区分的提示词/思考开关）。
- `planner`、`emoji`、`vlm`、`voice`、`embedding`、`image_embedding` 这些任务与网页版能力不匹配，
  请继续指向官方模型。
