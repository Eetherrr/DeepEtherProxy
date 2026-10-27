# 网页版会话上下文复用：可行性分析

> 状态：**已否决**（2026-10-27，见 records/DECISIONS.md 的 D-005）；仅作研究留档
> 日期：**2026-10-22**　实测账号：真实 DeepSeek 网页版账号　**决议：不做（2026-10-27，D-005）**
> 现状（0.3.0）：同一 `chat_session_id` 最多复用于 8 次调用，但每次 completion 的 `parent_message_id` 恒为 `None` —— 也就是**没有上下文**。
> 本文回答三个问题：能不能复用同一网页版聊天内的上下文？收益/代价是什么？如果做，怎么落地。

---

## 0. 结论速览

| 问题 | 结论 |
|---|---|
| 技术上可行吗 | **可行，已用真实账号实测确认**：同一 `chat_session_id` 内，把上一轮 assistant 消息 id 填进 `parent_message_id`，再只发新增的一句 prompt，模型能正确延续上下文（实测 42 → ×2 = 84 正确） |
| 能省 token/额度吗 | **不能直接省**。实测会话内 `accumulated_token_usage` 持续累加（1038 → 1063 → 1114），说明上游仍按整段会话处理；网页版本身也不是按 token 计费的通道 |
| 真正的收益 | ① 新建聊天次数从「每 8 次调用 1 个」降到「每个会话窗口 1 个」；② 每次只上传增量（当前是每轮重发全量 prompt，可达数万字符）；③ 同一会话连续追问更像真人（反封禁措施 4 的正向收益） |
| 主要风险 | 串台（不同群/任务共用聊天）、系统提示词漂移、会话变长后变慢/触限、`parent` 失效返回空响应、`search`/`model_type` 切换语义未验证 |
| 建议 | 作为 **0.4 实验特性、默认关闭**，先只在 `deepether-replyer` 上开；必须先做好「严格前缀判定 + 失败回退新会话 + 串台防护」，再谈收益 |

---

## 1. 现状：为什么现在「每次都是新聊天」

代码事实（`deepether/upstream/web_client.py`）：

```python
# acquire_session(reuse_limit=8)：同一个 chat_session_id 复用 8 次
# complete()：
body = {
    "chat_session_id": session_id,
    "prompt": prompt,
    "parent_message_id": None,   # ← 恒为 None
    ...
}
```

两层原因叠加：

1. **每 8 次调用新建一个 `chat_session`**（`antiban.session_reuse_calls = 8`）。网页版侧栏里一个 `chat_session` 就是一个聊天。
2. **同一会话内每轮 `parent_message_id = None`**，等于每次都在会话根节点开了一条新分支，轮与轮之间**没有任何上下文关联**。所以即便会话被复用，看起来仍是一堆互不相干的对话。

实测佐证：`GET /api/v0/chat/history_messages` 对一条链路完整的会话返回 `current_message_id=6`，消息是链式的（见 §2.1）；而另一条会话在 `parent_message_id=None` 下无法回答「我刚才让你回复的两个字是什么」。

---

## 2. 上游机制（2026-10-22 真实账号实测）

> 以下均为实测报文结论；未标注「推断」的都可以直接作为实现依据。实测脚本逻辑与 `deepether/upstream/` 保持一致（同一套请求头、PoW、SSE 累积器）。

### 2.1 消息是链式的，`parent_message_id` 就是上一轮 assistant 消息 id

`GET /api/v0/chat/history_messages?chat_session_id=…` 实测返回：

```jsonc
"chat_session": { "current_message_id": 6, "title": "幸运数字确认" },
"chat_messages": [
  { "role": "USER",      "message_id": 1, "parent_id": null, "status": "FINISHED",
    "fragments": [{ "type": "REQUEST",   "content": "请记住：我的幸运数字是 42。只回复两个字：明白" }] },
  { "role": "ASSISTANT", "message_id": 2, "parent_id": 1,    "status": "FINISHED",
    "fragments": [{ "type": "RESPONSE",  "content": "明白" }] },
  { "role": "USER",      "message_id": 3, "parent_id": 2,    "status": "FINISHED",
    "fragments": [{ "type": "REQUEST",   "content": "我的幸运数字是多少？只回复数字。" }] },
  { "role": "ASSISTANT", "message_id": 4, "parent_id": 3,    "status": "FINISHED",
    "fragments": [{ "type": "RESPONSE",  "content": "42" }] },
  { "role": "USER",      "message_id": 5, "parent_id": 4,    "status": "FINISHED",
    "fragments": [{ "type": "REQUEST",   "content": "把这个数字乘以 2，只回复结果。" }] },
  { "role": "ASSISTANT", "message_id": 6, "parent_id": 5,    "status": "FINISHED",
    "fragments": [{ "type": "RESPONSE",  "content": "84" }] }
]
```

- completion 请求体的 `parent_message_id` 应填**上一条 assistant 消息 id**（即上一轮的 `response_message_id`）。
- 每一轮 SSE 的**第一帧**就带这一轮的两个 id：

  ```jsonc
  {"request_message_id": 3, "response_message_id": 4, "model_type": "default"}
  ```

  当前 `SseAccumulator` 把这一帧当普通帧吞掉了（不计内容），实现时需要显式取出来存起来。

### 2.2 只发增量 prompt 也有效（这是省上传体积的前提）

同一会话、三轮链式，只有第一轮发完整内容：

| 轮次 | 发送的 prompt | parent | 返回 | 结论 |
|---|---|---|---|---|
| 1 | `请记住：我的幸运数字是 42。只回复两个字：明白` | `null` | `明白幸运数字确认`（模型多说几个字，不影响链路结论） | 建链 |
| 2 | `我的幸运数字是多少？只回复数字。`（**没有重发第 1 轮**） | 2 | `42` | 上下文保留 |
| 3 | `把这个数字乘以 2，只回复结果。` | 4 | `84` | 链式继续有效 |

→ **续聊时只需要发新增的那一句**，不需要把 MaiBot 每轮重发的全量上下文再贴一遍。

### 2.3 非法 parent 的行为：HTTP 200 + 单行 JSON（不是 SSE）

`parent_message_id=999999` 实测：

```jsonc
// HTTP 200，只有 1 行，不是 SSE 流
{"code":0,"msg":"","data":{"biz_code":26,"biz_msg":"invalid message id","biz_data":null}}
```

当前 `SseAccumulator` 会把这一帧吞掉 → 正文为空 → 上层看到 `upstream_empty`。
实现续聊时**必须**识别 `biz_code=26 / invalid message id` 并**换新会话重发一次**，而不是把空响应当作失败抛出去。

### 2.4 会话与历史接口可用（对实现有两处价值）

| 接口 | 实测 | 用途 |
|---|---|---|
| `GET /api/v0/chat_session/fetch_page?count=N` | 返回会话列表（`id` / `title` / `title_type` / `updated_at`） | 观测「到底建了多少聊天」；排障 |
| `GET /api/v0/chat/history_messages?chat_session_id=…` | 返回 `current_message_id` + 全部消息 | 插件重启后恢复 parent 链（持久化 session id 即可反查）；核对上下文是否真的接上 |

### 2.5 会话元信息

- `chat_session/create` 响应带 `ttl_seconds = 259200`（3 天）与 `is_empty: true`。
- 首轮之后 `title` 由首条消息自动生成（`title_type: SYSTEM`）；续聊不会改标题。

### 2.6 token 口径：**注意不要误读**

同一会话内连续三轮的 `accumulated_token_usage`：

| 轮次 | 发送内容 | 累计 token |
|---|---|---|
| 1 | 40 行模拟历史（约 1600 字符）+ 指令 | 1038 |
| 2 | 仅增量（约 10 字符） | 1063 |
| 3 | 仅增量 + 思考 | 1114 |

→ 该字段是**会话内累计**，不是单轮消耗；上游仍然处理整段会话历史。**所以「上下文复用」不是省 token 的手段**，收益在别处（§3.1）。这一点必须在实现前写清楚，避免再出现「以为在省钱其实在烧钱」的预期错位。

### 2.7 实测同时确认的细节

- 续聊时 **`thinking_enabled` 可以逐轮切换**：第 3 轮用 `thinking_enabled=true` + parent 链，正常返回正文 `7`（思考 42 字符）。
- 每轮仍然要**重新领 PoW**（`chat/create_pow_challenge`），这一点不因续聊而减少。
- 会话内 `parent_message_id=None` 的消息会成为根分支；它**看不到**同会话其它分支的内容（实测问「刚才让你回复的两个字」得到大意是「无法查看之前的对话记录」的回复）。

---

## 3. 收益与代价

### 3.1 能拿到的

| 项 | 现状 | 上下文复用后 | 依据 |
|---|---|---|---|
| 新建聊天频率 | 1 个 / 8 次调用（≈12.5 个 / 100 次） | 1 个 / 会话窗口（建议 30 分钟、≤12 轮） | 设计目标 |
| 每轮上传体积 | 全量渲染 prompt（`max_history_chars` 默认 60000，常达数千~数万字符） | 只发新增消息（几十~几百字符） | §2.2 实测 |
| 对话形态 | 每轮新分支 / 频繁新会话 | 同一聊天内连续追问 | docs/design/ANTIBAN.md 措施 4 |
| 插件重启后恢复 | 不支持 | 持久化 `session_id` 后可用 history 接口恢复 parent | §2.4 |

### 3.2 拿不到的 / 会变差的

| 项 | 说明 |
|---|---|
| **不省 token 计费** | 见 §2.6：上游按整段会话处理，`accumulated_token_usage` 继续累加 |
| 会话变长后可能变慢 | 上游 prefill 变长；本次 3 轮仍是 1~2 s，未见退化，但未压测长会话 |
| 失败面变大 | parent 失效 → 空响应（§2.3）；上下文过长 / 会话被删 / TTL 到期都会触发回退 |
| 串台风险 | key 设计不严时会把 A 群的上下文带给 B 群（隐私 + 答非所问），必须靠严格前缀判定 |
| 系统提示词漂移 | MaiBot 每轮的 system 内容可能变化（人格/时间）；旧会话里那份 system 已过时 |
| 搜索/模型类型切换 | `search_enabled`、`model_type` 中途切换的语义未验证；建议 key 里带上这两个参数，变化即新会话 |

---

## 4. 设计草案（推荐：「对话指纹 + 严格前缀续聊」）

### 4.1 先说约束：插件拿不到任务名 / 聊天流 id

Host 发给插件 Provider 的 `response` 请求只有 `context_items`、`model_info`、`extra_params`、`tool_options`、`logical_turn_id` 等（`serialize_response_request_snapshot`），**没有任务名、没有聊天流 id**。所以对话身份只能从 `context_items` 推导。

### 4.2 对话指纹（fingerprint）

- 取渲染后的 `RenderedContext.messages`：`fingerprint = sha1(system 文本 hash + 首条 user 消息 hash)`（或前 2 轮消息）。
- key 建议 `(fingerprint, model_name, thinking_enabled, search_enabled, model_type)`：任一不同 → 新会话，天然避免任务/模型串台。
- 缓存结构：LRU（默认 8 个会话）+ TTL（默认 1800 s）+ 每会话最大轮数（默认 12）与最大字符（默认 40000）。

### 4.3 续聊判定：严格前缀比较

缓存上一轮的 `messages` 列表与 `last_response_message_id`。新请求满足以下全部条件才续聊：

1. key 相同；
2. 新的 `messages` **以缓存列表为严格前缀**（逐项 `role` + `content` 相等）——只有"追加新消息"才算续聊；
3. 新增部分非空；
4. 距上次调用在 `window_sec` 内，且轮数/字符未超限。

否则：新建会话，发送完整 prompt（与 0.3.0 行为一致）。

**系统提示词的处理**（关键取舍）：

| 策略 | 做法 | 优点 | 缺点 |
|---|---|---|---|
| S1 保守 | system 内容变化即判定"不是续聊" | 简单、无重复 | Host 若在 system 里带时间戳，会频繁退化 |
| S2 推荐 | system 不参与前缀比较；续聊时把**当前 system** 放进增量 prompt 首段重发 | 设定始终最新 | 每轮多几百字（仍远小于全量 prompt） |

> 无论哪种，增量 prompt 里的 system 都与"网页版会话里那份旧 system"并存；S2 建议在重发时加一句「（以上为最新设定，请以这里为准）」，避免模型按旧设定继续。

### 4.4 增量 prompt 怎么渲染

复用现有渲染器，避免两套格式：取"新增的非 system 消息"，按 `render_prompt_text` 的格式拼接（`用户：… / 助手：…` + footer + 风格 suffix）。

> 注意：MaiBot 下一轮 context 里会包含上一条**助手回复**（可能来自官方模型、也可能经过后处理/断句，与网页版原始输出不完全一致）。实测把新增消息里包含的助手消息一起发过去不会有问题，反而能让网页版对齐"实际发出去的那句话"。增量通常只有 1~2 条，开销可忽略。

### 4.5 失败与回退（必须实现，否则会比现在更差）

| 情况 | 处理 |
|---|---|
| `biz_code=26 invalid message id` / 续聊请求正文为空 | 作废该 key 的缓存 → 新建会话 → 发完整 prompt 重试一次；仍失败才按正常失败路径返回 |
| 会话被删 / TTL 到期 / history 查不到 | 同上 |
| 续聊返回明显答非所问 | 不做语义检测（成本高）；靠"每会话 ≤N 轮 + TTL"控制影响面 |
| 上一轮本身失败（没有 response id） | 不写缓存；本轮按新会话处理 |
| 插件重启 | 默认丢弃缓存（`resume_after_restart=false`）；后续可用 history 接口恢复（可选） |

### 4.6 并发

- 同一 key 内必须串行（`asyncio.Lock`），否则链会断/错。
- 不同 key 之间仍受现有 pacing 串行 + 并发闸门约束，不改变反封禁模型。
- `utils` 类请求的 prompt 每次内容都不同，指纹前缀不成立 → 自然不会续聊，不会污染 replyer 的会话。

### 4.7 配置草案（新增 `[session_reuse]`，默认关闭）

| 字段 | 默认 | 说明 |
|---|---|---|
| `enabled` | `false` | 总开关；首版建议只在灰度环境开 |
| `window_sec` | `1800` | 同一会话允许续聊的时间窗 |
| `max_turns` | `12` | 单个网页版会话最多接多少轮；到了就换新会话 |
| `max_chars` | `40000` | 单会话累计字符上限；到了就换新会话 |
| `max_sessions` | `8` | 缓存多少个会话（LRU） |
| `send_system_on_continue` | `true` | S2：续聊时重发当前 system |
| `only_models` | `["deepether-replyer"]` | 只对这些模型开启续聊（默认避开 utils/planner） |
| `resume_after_restart` | `false` | 是否用 history 接口恢复（阶段 2） |

---

## 5. 落地拆解（若采纳）

| 步 | 文件 | 改动 |
|---|---|---|
| 1 | `deepether/upstream/sse.py` | `StreamSnapshot` 增加 `request_message_id` / `response_message_id`；识别 `biz_code` 错误帧（26 = invalid message id） |
| 2 | `deepether/upstream/web_client.py` | `complete(..., parent_message_id=None)`；`parent` 失效时抛专用错误（如 `PARENT_INVALID`），由上层决定"换会话重试"而不是直接失败 |
| 3 | 新模块 `deepether/session_reuse.py` | 指纹、LRU+TTL、前缀判定、增量渲染、可选持久化 |
| 4 | `deepether/provider.py` | 调用前查表决定"续聊/新会话"；失败回退；统计续聊与回退次数 |
| 5 | `deepether/config.py` + `config.toml` | 新增 `[session_reuse]`；默认 false |
| 6 | `deepether/state.py` / `status.py` | 增加 `continuations` / `new_chats` / `parent_fallbacks` 计数（状态命令可见） |
| 7 | `tests/` | 指纹/前缀/增量渲染/回退路径单测；真机验证脚本不进 CI（需真实 token） |

工作量估计：**2~3 人日**（不含真机压测）。风险集中在 §4.3/§4.5，建议先写单测再真机。

---

## 6. 验收与观测

真机验证（用真实账号、单独测试会话）：

1. 同一群连续 20 轮：网页版侧栏只应新增 1 个聊天（用 `fetch_page` 数会话）；
2. 第 2 轮起请求体只有增量（日志里 `prompt_len` 明显变小），且回答能引用前文；
3. 两个不同群分别问「专属暗号」：互不串台；
4. 故意把缓存的 `parent_id` 改坏：应自动换新会话并成功，`parent_fallbacks +1`；
5. 断 token：失败路径与 0.3.0 一致（本次调用失败，由 MaiBot 按模型配置处理），不产生额外重试。

观测指标（写进 `/deepether-status`）：

- `new_chats`（新建网页版会话次数）/ `continuations`（续聊次数）/ `parent_fallbacks`（回退次数）
- 平均耗时是否变差（长会话 prefill 变长）
- `web_failure` 不得因续聊而上升

---

## 7. 未验证 / 待确认

| 项 | 状态 |
|---|---|
| `search_enabled=true` 时续聊是否保留引用/搜索结果语义 | 未验证（搜索会多一轮 TOOL_SEARCH 片段） |
| 同一会话内切换 `model_type` | 未验证；设计上 key 里带该参数，变化即新会话 |
| 会话最大消息数 / 最大上下文长度 | 未知；需长会话压测（建议 20~50 轮） |
| 同一 session 的多分支在网页版 UI 里是否显示为多个「聊天」 | 未验证（需要浏览器自动化）；本方案只看 API 侧事实 |
| TTL（3 天）到期 / 手动删除会话后的 history 行为 | 未验证；已按"失败回退"设计 |
| 长会话是否更容易触发风控 | 未知；反封禁收益（更像真人）与风险（单会话很长）需要灰度观察 |

---

## 8. 结论

- **可行**，而且实现成本不高：上游本来就有 `parent_message_id` 链，历史接口也齐全。
- **收益不是"省 token"**，而是「少建聊天 + 只传增量 + 对话更像真人」；如果要省 token，这个功能帮不上忙。
- 主要复杂度在**对话身份判定、系统提示词漂移、失败回退与串台防护**，不是上游协议。
- 建议在 **0.4** 以默认关闭的实验特性实现，先只覆盖 `deepether-replyer`，用真机灰度验证 §6 的 5 条验收后再考虑默认开启。

---

## 附录 A：实测记录（2026-10-22，真实账号）

| # | 动作 | 结果 |
|---|---|---|
| 1 | `GET /api/v0/users/current` 探活 | HTTP 200，`code=0`，token 有效 |
| 2 | `POST chat_session/create` | 返回 session id；`ttl_seconds=259200` |
| 3 | 第 1 轮：`请记住：我的幸运数字是 42。只回复两个字：明白`，parent=null | 正文 `明白幸运数字确认`；`request_message_id=1, response_message_id=2` |
| 4 | 第 2 轮：只发 `我的幸运数字是多少？只回复数字。`，parent=2 | 正文 `42`；id 3/4 |
| 5 | 第 3 轮：只发 `把这个数字乘以 2，只回复结果。`，parent=4 | 正文 `84`；id 5/6 |
| 6 | 同会话、parent=null，问「刚才让你回复的两个字是什么」 | 答「无法查看之前的对话记录」→ 证明上下文只由 parent 链携带 |
| 7 | 40 行长 prompt + parent=null | `accumulated_token_usage=1038`；正文 `明白…` |
| 8 | 只发 `请只回复：好`，parent=2 | `accumulated_token_usage=1063`；正文 `好` |
| 9 | 只发 `请只回复数字 7`，parent=4，`thinking_enabled=true` | 正文 `7`；思考 42 字；`accumulated_token_usage=1114` |
| 10 | `parent_message_id=999999` | HTTP 200 单行 JSON：`biz_code=26, biz_msg="invalid message id"`，无正文 |
| 11 | `GET chat_session/fetch_page?count=10` | 返回 10 个会话（id/title/updated_at） |
| 12 | `GET chat/history_messages?chat_session_id=…` | 返回链式消息（USER 1 → ASSISTANT 2 → USER 3 → …），`current_message_id=6` |
